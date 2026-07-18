"""RFC 0093 P2a — append-only BinKeeper placement feedback ledger.

This module records immutable route receipts and owner decisions over those
receipts. It does not alter bin location, captures, passports, or route scoring.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

import psycopg
from psycopg.types.json import Jsonb

from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID
from binkeeper.bin_route import TextRoute, bin_route

PLACEMENT_LEDGER_SCHEMA_VERSION: Final[str] = "bin_placement_ledger.v1.rfc0093-p2a"
DEFAULT_PLACEMENT_SOURCE_LABEL: Final[str] = os.environ.get(
    "BINKEEPER_BIN_PLACEMENT_SOURCE_LABEL", "manual"
)
DEFAULT_PLACEMENT_ACTOR: Final[str] = os.environ.get("BINKEEPER_BIN_PLACEMENT_ACTOR", "owner")

DecisionKind = Literal[
    "accept",
    "reject",
    "override",
    "split",
    "merge",
    "not_an_item",
    "create_new_bin",
]
DECISION_KINDS: Final[tuple[DecisionKind, ...]] = (
    "accept",
    "reject",
    "override",
    "split",
    "merge",
    "not_an_item",
    "create_new_bin",
)
_SELECTED_BIN_DECISIONS: Final[frozenset[DecisionKind]] = frozenset(
    {"accept", "override", "create_new_bin"}
)


class PlacementLedgerError(ValueError):
    """Domain root for placement-ledger failures."""


@dataclass(frozen=True)
class TextRoutingRequest:
    """Owner request to route one typed item and record the immutable receipt."""

    text: str
    site: str
    requested_at: datetime | None = None
    source_label: str = DEFAULT_PLACEMENT_SOURCE_LABEL
    idempotency_key: str | None = None
    tenant_id: str = DEFAULT_BIN_TENANT_ID
    corpus_id: str = DEFAULT_BIN_CORPUS_ID


@dataclass(frozen=True)
class PlacementDecisionAppend:
    """Owner placement decision over one recorded routing request."""

    routing_request_id: str
    decision_kind: DecisionKind
    selected_bin_code: str | None = None
    actor: str = DEFAULT_PLACEMENT_ACTOR
    decided_at: datetime | None = None
    reason: str | None = None
    decision_payload: Mapping[str, object] = field(default_factory=dict)
    idempotency_key: str | None = None
    tenant_id: str = DEFAULT_BIN_TENANT_ID
    corpus_id: str = DEFAULT_BIN_CORPUS_ID


@dataclass(frozen=True)
class RoutingRequestRecord:
    """The result of recording a route receipt."""

    routing_request_id: str
    external_id: str
    seq: int
    already_existed: bool
    route_result_sha256: str
    route_result: Mapping[str, object]

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON result shape."""
        return {
            "routing_request_id": self.routing_request_id,
            "external_id": self.external_id,
            "seq": self.seq,
            "already_existed": self.already_existed,
            "route_result_sha256": self.route_result_sha256,
            "route_result": dict(self.route_result),
        }


@dataclass(frozen=True)
class PlacementDecisionRecord:
    """The result of recording one placement decision."""

    placement_decision_id: str
    external_id: str
    seq: int
    routing_request_id: str
    decision_kind: DecisionKind
    recommended_bin_code: str | None
    selected_bin_code: str | None
    already_existed: bool

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON result shape."""
        return {
            "placement_decision_id": self.placement_decision_id,
            "external_id": self.external_id,
            "seq": self.seq,
            "routing_request_id": self.routing_request_id,
            "decision_kind": self.decision_kind,
            "recommended_bin_code": self.recommended_bin_code,
            "selected_bin_code": self.selected_bin_code,
            "already_existed": self.already_existed,
        }


def record_text_routing_request(
    conn: psycopg.Connection, request: TextRoutingRequest
) -> RoutingRequestRecord:
    """Route one typed item and append its immutable request receipt."""
    requested_at = request.requested_at or datetime.now(UTC)
    text = _required_text(request.text, "text")
    site = _required_text(request.site, "site")
    source_label = _required_text(request.source_label, "source_label")
    route = bin_route(
        conn,
        text=text,
        site=site,
        now=requested_at,
        tenant_id=request.tenant_id,
        corpus_id=request.corpus_id,
    )
    route_result = route.to_json()
    route_hash = _json_sha256(route_result)
    external_id = routing_request_external_id(
        request=request,
        requested_at=requested_at,
        route_result_sha256=route_hash,
    )
    return _append_routing_request(
        conn,
        route=route,
        request=request,
        requested_at=requested_at,
        source_label=source_label,
        external_id=external_id,
        route_result=route_result,
        route_result_sha256=route_hash,
    )


def record_placement_decision(
    conn: psycopg.Connection, decision: PlacementDecisionAppend
) -> PlacementDecisionRecord:
    """Append one owner placement decision over an immutable route receipt."""
    decided_at = decision.decided_at or datetime.now(UTC)
    decision_kind = _decision_kind(decision.decision_kind)
    actor = _required_text(decision.actor, "actor")
    request_row = _load_routing_request(conn, decision)
    recommended_bin_code = _route_recommended_bin(request_row.route_result)
    selected_bin_code = _selected_bin_code(
        decision_kind=decision_kind,
        selected_bin_code=decision.selected_bin_code,
        recommended_bin_code=recommended_bin_code,
    )
    external_id = placement_decision_external_id(
        decision=decision,
        decision_kind=decision_kind,
        selected_bin_code=selected_bin_code,
        decided_at=decided_at,
    )
    return _append_placement_decision(
        conn,
        request_row=request_row,
        decision=decision,
        decision_kind=decision_kind,
        actor=actor,
        decided_at=decided_at,
        recommended_bin_code=recommended_bin_code,
        selected_bin_code=selected_bin_code,
        external_id=external_id,
    )


def routing_request_external_id(
    *,
    request: TextRoutingRequest,
    requested_at: datetime,
    route_result_sha256: str,
) -> str:
    """Derive the idempotency handle for a text-routing receipt."""
    if request.idempotency_key and request.idempotency_key.strip():
        basis = f"key:{request.idempotency_key.strip()}"
    else:
        basis = _canonical_json(
            {
                "tenant_id": request.tenant_id,
                "corpus_id": request.corpus_id,
                "request_kind": "text",
                "input_text": request.text.strip(),
                "site": request.site.strip(),
                "requested_at": requested_at.isoformat(),
                "route_result_sha256": route_result_sha256,
            }
        )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"bin-route:{digest[:40]}"


def placement_decision_external_id(
    *,
    decision: PlacementDecisionAppend,
    decision_kind: DecisionKind,
    selected_bin_code: str | None,
    decided_at: datetime,
) -> str:
    """Derive the idempotency handle for a placement decision."""
    if decision.idempotency_key and decision.idempotency_key.strip():
        basis = f"key:{decision.idempotency_key.strip()}"
    else:
        basis = _canonical_json(
            {
                "tenant_id": decision.tenant_id,
                "corpus_id": decision.corpus_id,
                "routing_request_id": decision.routing_request_id,
                "decision_kind": decision_kind,
                "selected_bin_code": selected_bin_code,
                "decided_at": decided_at.isoformat(),
            }
        )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"bin-placement:{digest[:40]}"


@dataclass(frozen=True)
class _RoutingRequestRow:
    routing_request_id: str
    route_result: Mapping[str, object]


def _append_routing_request(
    conn: psycopg.Connection,
    *,
    route: TextRoute,
    request: TextRoutingRequest,
    requested_at: datetime,
    source_label: str,
    external_id: str,
    route_result: Mapping[str, object],
    route_result_sha256: str,
) -> RoutingRequestRecord:
    raw_payload = {
        "schema_version": PLACEMENT_LEDGER_SCHEMA_VERSION,
        "idempotency_key": request.idempotency_key,
    }
    with conn.transaction():
        inserted = conn.execute(
            """
            INSERT INTO bin_routing_requests (
                tenant_id, corpus_id, external_id, request_kind, input_text, site,
                requested_at, source_label, router_version, item_card_json,
                route_result_json, route_result_sha256, raw_payload
            ) VALUES (%s, %s, %s, 'text', %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, corpus_id, external_id) DO NOTHING
            RETURNING id::text, seq
            """,
            (
                request.tenant_id,
                request.corpus_id,
                external_id,
                request.text.strip(),
                request.site.strip(),
                requested_at,
                source_label,
                route.router_version,
                Jsonb(route.item_card.to_json()),
                Jsonb(dict(route_result)),
                route_result_sha256,
                Jsonb(raw_payload),
            ),
        ).fetchone()
        if inserted is not None:
            return RoutingRequestRecord(
                str(inserted[0]),
                external_id,
                int(inserted[1]),
                False,
                route_result_sha256,
                route_result,
            )
        existing = _select_routing_request_by_external_id(
            conn, request.tenant_id, request.corpus_id, external_id
        )
    if existing is None:  # pragma: no cover - conflict without row is impossible
        raise PlacementLedgerError("idempotent route insert conflicted without a prior row")
    return existing


def _select_routing_request_by_external_id(
    conn: psycopg.Connection,
    tenant_id: str,
    corpus_id: str,
    external_id: str,
) -> RoutingRequestRecord | None:
    row = conn.execute(
        """
        SELECT id::text, seq, route_result_sha256, route_result_json
        FROM bin_routing_requests
        WHERE tenant_id = %s AND corpus_id = %s AND external_id = %s
        """,
        (tenant_id, corpus_id, external_id),
    ).fetchone()
    if row is None:
        return None
    return RoutingRequestRecord(
        str(row[0]),
        external_id,
        int(row[1]),
        True,
        str(row[2]),
        _object_json(row[3], "route_result_json"),
    )


def _append_placement_decision(
    conn: psycopg.Connection,
    *,
    request_row: _RoutingRequestRow,
    decision: PlacementDecisionAppend,
    decision_kind: DecisionKind,
    actor: str,
    decided_at: datetime,
    recommended_bin_code: str | None,
    selected_bin_code: str | None,
    external_id: str,
) -> PlacementDecisionRecord:
    raw_payload = {
        "schema_version": PLACEMENT_LEDGER_SCHEMA_VERSION,
        "idempotency_key": decision.idempotency_key,
    }
    with conn.transaction():
        inserted = conn.execute(
            """
            INSERT INTO bin_placement_decisions (
                tenant_id, corpus_id, external_id, routing_request_id,
                decision_kind, recommended_bin_code, selected_bin_code, actor,
                decided_at, reason, decision_payload, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, corpus_id, external_id) DO NOTHING
            RETURNING id::text, seq
            """,
            (
                decision.tenant_id,
                decision.corpus_id,
                external_id,
                request_row.routing_request_id,
                decision_kind,
                recommended_bin_code,
                selected_bin_code,
                actor,
                decided_at,
                _optional_text(decision.reason, "reason"),
                Jsonb(dict(decision.decision_payload)),
                Jsonb(raw_payload),
            ),
        ).fetchone()
        if inserted is not None:
            return PlacementDecisionRecord(
                str(inserted[0]),
                external_id,
                int(inserted[1]),
                request_row.routing_request_id,
                decision_kind,
                recommended_bin_code,
                selected_bin_code,
                False,
            )
        existing = _select_decision_by_external_id(
            conn, decision.tenant_id, decision.corpus_id, external_id
        )
    if existing is None:  # pragma: no cover - conflict without row is impossible
        raise PlacementLedgerError("idempotent decision insert conflicted without a prior row")
    return existing


def _select_decision_by_external_id(
    conn: psycopg.Connection,
    tenant_id: str,
    corpus_id: str,
    external_id: str,
) -> PlacementDecisionRecord | None:
    row = conn.execute(
        """
        SELECT id::text, seq, routing_request_id::text, decision_kind,
               recommended_bin_code, selected_bin_code
        FROM bin_placement_decisions
        WHERE tenant_id = %s AND corpus_id = %s AND external_id = %s
        """,
        (tenant_id, corpus_id, external_id),
    ).fetchone()
    if row is None:
        return None
    return PlacementDecisionRecord(
        str(row[0]),
        external_id,
        int(row[1]),
        str(row[2]),
        _decision_kind(str(row[3])),
        _optional_text(row[4], "recommended_bin_code"),
        _optional_text(row[5], "selected_bin_code"),
        True,
    )


def _load_routing_request(
    conn: psycopg.Connection, decision: PlacementDecisionAppend
) -> _RoutingRequestRow:
    row = conn.execute(
        """
        SELECT id::text, route_result_json
        FROM bin_routing_requests
        WHERE tenant_id = %s AND corpus_id = %s AND id = %s
        """,
        (decision.tenant_id, decision.corpus_id, decision.routing_request_id),
    ).fetchone()
    if row is None:
        raise PlacementLedgerError(f"routing request {decision.routing_request_id!r} was not found")
    return _RoutingRequestRow(str(row[0]), _object_json(row[1], "route_result_json"))


def _selected_bin_code(
    *,
    decision_kind: DecisionKind,
    selected_bin_code: str | None,
    recommended_bin_code: str | None,
) -> str | None:
    if decision_kind == "accept":
        if recommended_bin_code is None:
            raise PlacementLedgerError("accept decisions require a non-abstained recommendation")
        return recommended_bin_code
    if decision_kind in _SELECTED_BIN_DECISIONS:
        return _required_text(selected_bin_code, "selected_bin_code")
    return _optional_text(selected_bin_code, "selected_bin_code")


def _route_recommended_bin(route_result: Mapping[str, object]) -> str | None:
    return _optional_text(route_result.get("recommended_bin_code"), "recommended_bin_code")


def _decision_kind(value: str) -> DecisionKind:
    if value not in DECISION_KINDS:
        allowed = ", ".join(DECISION_KINDS)
        raise PlacementLedgerError(f"unknown decision_kind {value!r}; allowed: {allowed}")
    return value  # type: ignore[return-value]


def _required_text(value: object, field_name: str) -> str:
    text = _optional_text(value, field_name)
    if text is None:
        raise PlacementLedgerError(f"{field_name} is required")
    return text


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PlacementLedgerError(f"{field_name} must be a string")
    stripped = value.strip()
    return stripped or None


def _object_json(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PlacementLedgerError(f"{field_name} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _canonical_json(payload: Mapping[str, object]) -> str:
    """Deterministic JSON for hashes and idempotency handles."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
