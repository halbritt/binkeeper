"""RFC 0088 T3c — append-only resting orders keyed to physical sites.

A resting order is an action that should surface only when the owner is near a
site, for example "confirm this stale bin next time you are at the shop". Orders
can name a bin, but they never mutate bin location; only ``bin_trip_events`` can
move a bin.

Layering (RFC 0012 pure/IO split):
- Pure: ``validate_order_event`` / ``fold_orders`` / ``list_folded_orders``.
- IO: ``record_order_event`` / ``create_resting_order`` /
  ``clear_resting_order`` / ``list_resting_orders``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal

import psycopg
from psycopg.types.json import Jsonb

DEFAULT_BIN_TENANT_ID: Final[str] = os.environ.get("ENGRAM_BIN_TENANT_ID", "personal")
DEFAULT_BIN_CORPUS_ID: Final[str] = os.environ.get("ENGRAM_BIN_CORPUS_ID", "personal")
DEFAULT_ORDER_SOURCE_LABEL: Final[str] = "manual"
DEFAULT_RESTING_ORDER_ACTION: Final[str] = "confirm_bin"
RESTING_ORDER_SCHEMA_VERSION: Final[str] = "bin_resting_order_event.v1"

OrderEventKind = Literal["create", "clear"]
ORDER_EVENT_KINDS: Final[tuple[OrderEventKind, ...]] = ("create", "clear")


class BinOrderError(ValueError):
    """Domain root for resting-order failures (RFC 0012 per-stage family)."""


@dataclass(frozen=True, init=False)
class RestingOrderEvent:
    """One append-only resting-order event."""

    seq: int
    event_kind: OrderEventKind
    order_id: str
    occurred_at: datetime
    site: str | None = None
    bin_code: str | None = None
    action_kind: str | None = None
    instruction: str | None = None
    cleared_reason: str | None = None
    priority: float | None = None

    def __init__(
        self,
        seq: int,
        event_kind: OrderEventKind,
        order_id: str,
        occurred_at: datetime,
        *,
        site: str | None = None,
        bin_code: str | None = None,
        action: str | None = None,
        reason: str | None = None,
        action_kind: str | None = None,
        instruction: str | None = None,
        cleared_reason: str | None = None,
        priority: float | None = None,
    ) -> None:
        """Create an order event, accepting action/reason or action_kind/instruction."""
        if event_kind not in ORDER_EVENT_KINDS:
            allowed = ", ".join(ORDER_EVENT_KINDS)
            raise BinOrderError(f"unknown event_kind {event_kind!r}; allowed: {allowed}")
        object.__setattr__(self, "seq", seq)
        object.__setattr__(self, "event_kind", event_kind)
        object.__setattr__(self, "order_id", order_id)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "site", site)
        object.__setattr__(self, "bin_code", bin_code)
        object.__setattr__(
            self,
            "action_kind",
            action_kind if action_kind is not None else action,
        )
        object.__setattr__(
            self,
            "instruction",
            instruction if instruction is not None else reason,
        )
        object.__setattr__(self, "cleared_reason", cleared_reason)
        object.__setattr__(self, "priority", priority)

    @property
    def action(self) -> str | None:
        """Compatibility alias for callers that name action_kind as action."""
        return self.action_kind

    @property
    def reason(self) -> str | None:
        """Compatibility alias for callers that name instruction as reason."""
        return self.instruction

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one resting-order event."""
        return {
            "seq": self.seq,
            "event_kind": self.event_kind,
            "order_id": self.order_id,
            "occurred_at": self.occurred_at.isoformat(),
            "site": self.site,
            "bin_code": self.bin_code,
            "action": self.action,
            "reason": self.reason,
            "action_kind": self.action_kind,
            "instruction": self.instruction,
            "cleared_reason": self.cleared_reason,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class RestingOrder:
    """The folded state of one logical resting order."""

    order_id: str
    active: bool
    site: str | None
    bin_code: str | None
    action_kind: str | None
    instruction: str | None
    created_seq: int | None
    created_at: datetime | None
    last_event_seq: int
    last_event_at: datetime
    cleared_at: datetime | None = None
    cleared_reason: str | None = None
    priority: float | None = None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one folded resting order."""
        return {
            "order_id": self.order_id,
            "active": self.active,
            "site": self.site,
            "bin_code": self.bin_code,
            "action_kind": self.action_kind,
            "instruction": self.instruction,
            "created_seq": self.created_seq,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_event_seq": self.last_event_seq,
            "last_event_at": self.last_event_at.isoformat(),
            "cleared_at": self.cleared_at.isoformat() if self.cleared_at else None,
            "cleared_reason": self.cleared_reason,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class OrderRecordResult:
    """The outcome of an idempotent resting-order append."""

    external_id: str
    event_id: str
    seq: int
    order_id: str
    already_existed: bool

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON result shape."""
        return {
            "external_id": self.external_id,
            "event_id": self.event_id,
            "seq": self.seq,
            "order_id": self.order_id,
            "already_existed": self.already_existed,
        }


def validate_order_event(
    *,
    event_kind: str,
    order_id: str,
    site: str | None,
    action_kind: str | None,
    instruction: str | None,
) -> OrderEventKind:
    """Validate a resting-order event shape and return its normalized kind."""
    if event_kind not in ORDER_EVENT_KINDS:
        allowed = ", ".join(ORDER_EVENT_KINDS)
        raise BinOrderError(f"unknown event_kind {event_kind!r}; allowed: {allowed}")
    if not (order_id and order_id.strip()):
        raise BinOrderError("resting-order events require an order_id")
    if event_kind == "create":
        if not (site and site.strip()):
            raise BinOrderError("create resting-order events require a site")
        if not (action_kind and action_kind.strip()):
            raise BinOrderError("create resting-order events require an action_kind")
        if not (instruction and instruction.strip()):
            raise BinOrderError("create resting-order events require an instruction")
    kind: OrderEventKind = event_kind  # type: ignore[assignment]
    return kind


def fold_orders(events: Sequence[RestingOrderEvent]) -> dict[str, RestingOrder]:
    """Fold resting-order events into per-order states. Pure and total."""
    states: dict[str, RestingOrder] = {}
    for event in sorted(events, key=lambda item: item.seq):
        if event.event_kind == "create":
            states[event.order_id] = RestingOrder(
                order_id=event.order_id,
                active=True,
                site=event.site,
                bin_code=event.bin_code,
                action_kind=event.action_kind,
                instruction=event.instruction,
                created_seq=event.seq,
                created_at=event.occurred_at,
                last_event_seq=event.seq,
                last_event_at=event.occurred_at,
                priority=event.priority,
            )
            continue
        previous = states.get(event.order_id)
        if previous is None:
            states[event.order_id] = RestingOrder(
                order_id=event.order_id,
                active=False,
                site=event.site,
                bin_code=event.bin_code,
                action_kind=event.action_kind,
                instruction=event.instruction,
                created_seq=None,
                created_at=None,
                last_event_seq=event.seq,
                last_event_at=event.occurred_at,
                cleared_at=event.occurred_at,
                cleared_reason=event.cleared_reason,
                priority=event.priority,
            )
            continue
        states[event.order_id] = RestingOrder(
            order_id=previous.order_id,
            active=False,
            site=previous.site,
            bin_code=previous.bin_code,
            action_kind=previous.action_kind,
            instruction=previous.instruction,
            created_seq=previous.created_seq,
            created_at=previous.created_at,
            last_event_seq=event.seq,
            last_event_at=event.occurred_at,
            cleared_at=event.occurred_at,
            cleared_reason=event.cleared_reason,
            priority=previous.priority,
        )
    return states


def list_folded_orders(
    events: Sequence[RestingOrderEvent],
    *,
    site: str | None = None,
    active_only: bool = True,
) -> list[RestingOrder]:
    """List folded orders, optionally scoped to one site. Pure and total."""
    selected = [
        order
        for order in fold_orders(events).values()
        if (not active_only or order.active) and (site is None or order.site == site)
    ]
    return sorted(
        selected,
        key=lambda order: (
            order.site or "",
            order.created_at or order.last_event_at,
            order.order_id,
        ),
    )


def fold_resting_orders(
    events: Sequence[RestingOrderEvent],
    *,
    site: str | None = None,
    active_only: bool = False,
) -> list[RestingOrder]:
    """Compatibility alias for the pure folded-order listing."""
    return list_folded_orders(events, site=site, active_only=active_only)


def _canonical_json(payload: dict[str, object]) -> str:
    """Deterministic JSON for hashing (sorted keys, compact)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _derive_order_id(
    *,
    site: str,
    bin_code: str | None,
    action_kind: str,
    instruction: str,
    tenant_id: str,
    corpus_id: str,
) -> str:
    """Derive a stable order id when the caller does not provide one."""
    basis = _canonical_json(
        {
            "tenant_id": tenant_id,
            "corpus_id": corpus_id,
            "site": site,
            "bin_code": bin_code,
            "action_kind": action_kind,
            "instruction": instruction,
        }
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"resting:{digest[:24]}"


def order_event_external_id(
    *,
    event_kind: str,
    order_id: str,
    occurred_at: datetime,
    idempotency_key: str | None,
    tenant_id: str,
    corpus_id: str,
) -> str:
    """Derive the idempotency handle for one resting-order event."""
    if idempotency_key and idempotency_key.strip():
        basis = f"key:{idempotency_key.strip()}"
    else:
        basis = _canonical_json(
            {
                "tenant_id": tenant_id,
                "corpus_id": corpus_id,
                "event_kind": event_kind,
                "order_id": order_id,
                "occurred_at": occurred_at.isoformat(),
            }
        )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"resting-order:{digest[:40]}"


_ORDER_COLUMNS = (
    "seq, event_kind, order_id, occurred_at, site, bin_code, "
    "action_kind, instruction, cleared_reason, priority"
)


def _row_to_order_event(row: tuple[object, ...]) -> RestingOrderEvent:
    """Map a SELECT row (in _ORDER_COLUMNS order) to a RestingOrderEvent."""
    return RestingOrderEvent(
        seq=int(row[0]),  # type: ignore[arg-type]
        event_kind=row[1],  # type: ignore[arg-type]
        order_id=row[2],  # type: ignore[arg-type]
        occurred_at=row[3],  # type: ignore[arg-type]
        site=row[4],  # type: ignore[arg-type]
        bin_code=row[5],  # type: ignore[arg-type]
        action_kind=row[6],  # type: ignore[arg-type]
        instruction=row[7],  # type: ignore[arg-type]
        cleared_reason=row[8],  # type: ignore[arg-type]
        priority=float(str(row[9])) if row[9] is not None else None,
    )


def record_order_event(
    conn: psycopg.Connection,
    *,
    event_kind: str,
    order_id: str,
    site: str | None = None,
    bin_code: str | None = None,
    action_kind: str | None = DEFAULT_RESTING_ORDER_ACTION,
    instruction: str | None = None,
    reason: str | None = None,
    cleared_reason: str | None = None,
    priority: float | None = None,
    occurred_at: datetime | None = None,
    source_label: str = DEFAULT_ORDER_SOURCE_LABEL,
    idempotency_key: str | None = None,
    metadata: dict[str, object] | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> OrderRecordResult:
    """Append one resting-order event idempotently."""
    when = occurred_at or datetime.now(UTC)
    create_instruction = instruction or reason
    kind = validate_order_event(
        event_kind=event_kind,
        order_id=order_id,
        site=site,
        action_kind=action_kind if event_kind == "create" else None,
        instruction=create_instruction if event_kind == "create" else None,
    )
    if priority is not None and priority < 0.0:
        raise BinOrderError("priority must be non-negative when provided")
    external_id = order_event_external_id(
        event_kind=kind,
        order_id=order_id,
        occurred_at=when,
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
    raw_payload: dict[str, object] = {
        "schema_version": RESTING_ORDER_SCHEMA_VERSION,
        "metadata": metadata or {},
        "idempotency_key": idempotency_key,
    }
    insert_action = action_kind if kind == "create" else None
    insert_instruction = create_instruction if kind == "create" else None
    insert_priority = priority if kind == "create" else None
    with conn.transaction():
        inserted = conn.execute(
            """
            INSERT INTO bin_resting_order_events (
                tenant_id, corpus_id, external_id, event_kind, order_id,
                site, bin_code, action_kind, instruction, cleared_reason,
                priority, occurred_at, source_label, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, corpus_id, external_id) DO NOTHING
            RETURNING id::text, seq
            """,
            (
                tenant_id,
                corpus_id,
                external_id,
                kind,
                order_id,
                site if kind == "create" else None,
                bin_code if kind == "create" else None,
                insert_action,
                insert_instruction,
                cleared_reason if kind == "clear" else None,
                insert_priority,
                when,
                source_label,
                Jsonb(raw_payload),
            ),
        ).fetchone()
        if inserted is not None:
            return OrderRecordResult(
                external_id,
                str(inserted[0]),
                int(inserted[1]),
                order_id,
                False,
            )
        existing = conn.execute(
            """
            SELECT id::text, seq FROM bin_resting_order_events
            WHERE tenant_id = %s AND corpus_id = %s AND external_id = %s
            """,
            (tenant_id, corpus_id, external_id),
        ).fetchone()
    if existing is None:  # pragma: no cover - conflict without a row is impossible
        raise BinOrderError("idempotent insert conflicted but no prior row was found")
    return OrderRecordResult(external_id, str(existing[0]), int(existing[1]), order_id, True)


def create_resting_order(
    conn: psycopg.Connection,
    *,
    site: str,
    instruction: str | None = None,
    reason: str | None = None,
    bin_code: str | None = None,
    action_kind: str = DEFAULT_RESTING_ORDER_ACTION,
    priority: float | None = None,
    order_id: str | None = None,
    occurred_at: datetime | None = None,
    source_label: str = DEFAULT_ORDER_SOURCE_LABEL,
    idempotency_key: str | None = None,
    metadata: dict[str, object] | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> OrderRecordResult:
    """Create or reopen one resting order idempotently."""
    resolved_instruction = instruction or reason
    if not (resolved_instruction and resolved_instruction.strip()):
        raise BinOrderError("create_resting_order requires instruction or reason")
    resolved_order_id = order_id or _derive_order_id(
        site=site,
        bin_code=bin_code,
        action_kind=action_kind,
        instruction=resolved_instruction,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
    return record_order_event(
        conn,
        event_kind="create",
        order_id=resolved_order_id,
        site=site,
        bin_code=bin_code,
        action_kind=action_kind,
        instruction=resolved_instruction,
        priority=priority,
        occurred_at=occurred_at,
        source_label=source_label,
        idempotency_key=idempotency_key,
        metadata=metadata,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )


def clear_resting_order(
    conn: psycopg.Connection,
    *,
    order_id: str,
    reason: str | None = None,
    cleared_reason: str | None = None,
    occurred_at: datetime | None = None,
    source_label: str = DEFAULT_ORDER_SOURCE_LABEL,
    idempotency_key: str | None = None,
    metadata: dict[str, object] | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> OrderRecordResult:
    """Clear one resting order idempotently."""
    return record_order_event(
        conn,
        event_kind="clear",
        order_id=order_id,
        action_kind=None,
        instruction=None,
        cleared_reason=cleared_reason or reason,
        occurred_at=occurred_at,
        source_label=source_label,
        idempotency_key=idempotency_key,
        metadata=metadata,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )


def load_order_events(
    conn: psycopg.Connection,
    *,
    order_id: str | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[RestingOrderEvent]:
    """Load resting-order events in seq order, optionally scoped to one order."""
    if order_id is not None:
        rows = conn.execute(
            f"""
            SELECT {_ORDER_COLUMNS} FROM bin_resting_order_events
            WHERE tenant_id = %s AND corpus_id = %s AND order_id = %s
            ORDER BY seq
            """,
            (tenant_id, corpus_id, order_id),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {_ORDER_COLUMNS} FROM bin_resting_order_events
            WHERE tenant_id = %s AND corpus_id = %s
            ORDER BY seq
            """,
            (tenant_id, corpus_id),
        ).fetchall()
    return [_row_to_order_event(row) for row in rows]


def list_resting_orders(
    conn: psycopg.Connection,
    *,
    site: str | None = None,
    status: str | None = None,
    active_only: bool = True,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[RestingOrder]:
    """List folded resting orders, optionally scoped to one site/status."""
    if status == "cleared":
        desired_active: bool | None = False
    elif status == "active":
        desired_active = True
    elif status is None:
        desired_active = True if active_only else None
    else:
        raise BinOrderError("status must be 'active', 'cleared', or None")
    orders = list_folded_orders(
        load_order_events(conn, tenant_id=tenant_id, corpus_id=corpus_id),
        site=site,
        active_only=False,
    )
    if desired_active is None:
        return orders
    return [order for order in orders if order.active is desired_active]


def resting_orders(
    conn: psycopg.Connection,
    *,
    site: str | None = None,
    status: str | None = None,
    active_only: bool = True,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[RestingOrder]:
    """Compatibility alias for listing folded resting orders."""
    return list_resting_orders(
        conn,
        site=site,
        status=status,
        active_only=active_only,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
