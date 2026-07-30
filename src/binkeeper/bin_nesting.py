"""Physical bin-in-bin containment projected from append-only owner evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, cast

import psycopg
from psycopg.types.json import Jsonb

from binkeeper.bin_inventory import (
    DEFAULT_BIN_CORPUS_ID,
    DEFAULT_BIN_TENANT_ID,
    BinLocation,
    RecordResult,
    bin_where,
    fold_bin_location,
    load_bin_events,
    record_event,
)
from binkeeper.bin_passport import load_known_bin_codes

ContainmentEventKind = Literal["pack", "unpack"]
CONTAINMENT_EVENT_SCHEMA_VERSION = "bin_containment_event.v1"


class BinNestingError(ValueError):
    """Physical containment evidence cannot form a valid current graph."""


@dataclass(frozen=True)
class BinContainmentEvent:
    """One owner-attested physical containment event."""

    seq: int
    event_kind: ContainmentEventKind
    bin_code: str
    container_code: str
    site: str
    occurred_at: datetime


@dataclass(frozen=True)
class BinContainmentAppend:
    """One requested append to the physical-containment evidence ledger."""

    event_kind: str
    bin_code: str
    container_code: str
    idempotency_key: str
    occurred_at: datetime | None = None
    source_label: str = "manual"
    tenant_id: str = DEFAULT_BIN_TENANT_ID
    corpus_id: str = DEFAULT_BIN_CORPUS_ID


@dataclass(frozen=True)
class _PreparedAppend:
    event_kind: ContainmentEventKind
    bin_code: str
    container_code: str
    idempotency_key: str
    occurred_at: datetime
    source_label: str
    tenant_id: str
    corpus_id: str
    external_id: str


@dataclass(frozen=True)
class BinContainmentGraph:
    """The current single-parent containment graph."""

    parents: dict[str, str]

    def parent_of(self, bin_code: str) -> str | None:
        return self.parents.get(bin_code)

    def children_of(self, container_code: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                bin_code
                for bin_code, parent_code in self.parents.items()
                if parent_code == container_code
            )
        )

    def path_for(self, bin_code: str) -> tuple[str, ...]:
        path: list[str] = []
        current = bin_code
        seen = {bin_code}
        while parent := self.parents.get(current):
            if parent in seen:
                raise BinNestingError("bin containment would create a cycle")
            path.append(parent)
            seen.add(parent)
            current = parent
        return tuple(path)


def fold_bin_containment(events: Sequence[BinContainmentEvent]) -> BinContainmentGraph:
    """Fold ordered pack and unpack evidence into current parent relationships."""
    parents: dict[str, str] = {}
    for event in sorted(events, key=lambda candidate: candidate.seq):
        if event.event_kind == "pack":
            if current_parent := parents.get(event.bin_code):
                raise BinNestingError(f"{event.bin_code} is already inside {current_parent}")
            parents[event.bin_code] = event.container_code
            BinContainmentGraph(parents).path_for(event.bin_code)
        elif parents.get(event.bin_code) != event.container_code:
            raise BinNestingError(
                f"{event.bin_code} is not inside {event.container_code} at unpack"
            )
        else:
            del parents[event.bin_code]
    return BinContainmentGraph(parents)


def load_bin_containment(
    conn: psycopg.Connection,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> BinContainmentGraph:
    """Rebuild the current physical containment graph from its evidence ledger."""
    rows = conn.execute(
        """
        SELECT seq, event_kind, bin_code, container_code, site, occurred_at
        FROM bin_containment_events
        WHERE tenant_id = %s AND corpus_id = %s
        ORDER BY seq
        """,
        (tenant_id, corpus_id),
    ).fetchall()
    return fold_bin_containment(
        [
            BinContainmentEvent(
                seq=int(row[0]),
                event_kind=cast(ContainmentEventKind, row[1]),
                bin_code=str(row[2]),
                container_code=str(row[3]),
                site=str(row[4]),
                occurred_at=row[5],
            )
            for row in rows
        ]
    )


def effective_bin_location(
    conn: psycopg.Connection,
    bin_code: str,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> BinLocation:
    """Resolve a bin at the location of its outermost physical container."""
    graph = load_bin_containment(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    path = graph.path_for(bin_code)
    location_source = path[-1] if path else bin_code
    direct = fold_bin_location(
        location_source,
        load_bin_events(
            conn,
            location_source,
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        ),
    )
    if not path:
        return direct
    return replace(
        direct,
        bin_code=bin_code,
        container_code=path[0],
        containment_path=path,
        location_source_bin=location_source,
    )


def require_top_level_for_move(
    conn: psycopg.Connection,
    bin_code: str,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> None:
    """Serialize with containment writes and reject direct moves of contained bins."""
    _lock_scope(conn, tenant_id, corpus_id)
    parent = load_bin_containment(
        conn,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    ).parent_of(bin_code)
    if parent is not None:
        raise BinNestingError(f"{bin_code} is inside {parent}; unpack it before moving it")


def record_bin_containment(
    conn: psycopg.Connection,
    append: BinContainmentAppend,
) -> RecordResult:
    """Append one owner-attested pack or unpack event idempotently."""
    prepared = _prepare_append(append)
    with conn.transaction():
        _lock_scope(conn, prepared.tenant_id, prepared.corpus_id)
        existing = _existing_event(
            conn,
            prepared.external_id,
            prepared.tenant_id,
            prepared.corpus_id,
        )
        if existing is not None:
            _require_same_action(
                existing,
                prepared.event_kind,
                prepared.bin_code,
                prepared.container_code,
            )
            return RecordResult(
                prepared.external_id,
                str(existing[0]),
                int(existing[1]),
                True,
            )

        event_site = _validated_event_site(conn, prepared)
        inserted = _insert_containment_event(conn, prepared, event_site)
        if prepared.event_kind == "unpack":
            _record_unpack_location(conn, prepared, event_site)
    if inserted is None:
        raise BinNestingError("containment event insert returned no row")
    return RecordResult(
        prepared.external_id,
        str(inserted[0]),
        int(inserted[1]),
        False,
    )


def _prepare_append(append: BinContainmentAppend) -> _PreparedAppend:
    if append.event_kind not in ("pack", "unpack"):
        raise BinNestingError("containment action must be pack or unpack")
    event_kind = cast(ContainmentEventKind, append.event_kind)
    bin_code = _required_text(append.bin_code, "bin_code")
    container_code = _required_text(append.container_code, "container_code")
    idempotency_key = _required_text(append.idempotency_key, "idempotency_key")
    source_label = _required_text(append.source_label, "source_label")
    if bin_code == container_code:
        raise BinNestingError("a bin cannot contain itself")
    return _PreparedAppend(
        event_kind=event_kind,
        bin_code=bin_code,
        container_code=container_code,
        idempotency_key=idempotency_key,
        occurred_at=append.occurred_at or datetime.now(UTC),
        source_label=source_label,
        tenant_id=append.tenant_id,
        corpus_id=append.corpus_id,
        external_id=_external_id(
            idempotency_key,
            append.tenant_id,
            append.corpus_id,
        ),
    )


def _validated_event_site(
    conn: psycopg.Connection,
    append: _PreparedAppend,
) -> str:
    known = set(
        load_known_bin_codes(
            conn,
            tenant_id=append.tenant_id,
            corpus_id=append.corpus_id,
        )
    )
    missing = [code for code in (append.bin_code, append.container_code) if code not in known]
    if missing:
        raise BinNestingError(f"unknown bin {missing[0]!r}")
    graph = load_bin_containment(
        conn,
        tenant_id=append.tenant_id,
        corpus_id=append.corpus_id,
    )
    container_location = bin_where(
        conn,
        append.container_code,
        tenant_id=append.tenant_id,
        corpus_id=append.corpus_id,
    )
    if append.event_kind == "unpack":
        return _validated_unpack_site(append, graph, container_location)
    return _validated_pack_site(conn, append, graph, container_location)


def _validated_pack_site(
    conn: psycopg.Connection,
    append: _PreparedAppend,
    graph: BinContainmentGraph,
    container_location: BinLocation,
) -> str:
    if current_parent := graph.parent_of(append.bin_code):
        raise BinNestingError(f"{append.bin_code} is already inside {current_parent}")
    if append.bin_code in graph.path_for(append.container_code):
        raise BinNestingError("bin containment would create a cycle")
    child_location = bin_where(
        conn,
        append.bin_code,
        tenant_id=append.tenant_id,
        corpus_id=append.corpus_id,
    )
    if (
        child_location.status != "at_site"
        or container_location.status != "at_site"
        or child_location.site is None
        or child_location.site != container_location.site
    ):
        raise BinNestingError("both bins must be at the same known site before packing")
    return child_location.site


def _validated_unpack_site(
    append: _PreparedAppend,
    graph: BinContainmentGraph,
    container_location: BinLocation,
) -> str:
    if graph.parent_of(append.bin_code) != append.container_code:
        raise BinNestingError(f"{append.bin_code} is not inside {append.container_code}")
    if container_location.status != "at_site" or container_location.site is None:
        raise BinNestingError("the container must be at a known site before unpacking")
    return container_location.site


def _insert_containment_event(
    conn: psycopg.Connection,
    append: _PreparedAppend,
    event_site: str,
) -> tuple[str, int] | None:
    row = conn.execute(
        """
        INSERT INTO bin_containment_events (
            tenant_id, corpus_id, external_id, event_kind, bin_code,
            container_code, site, occurred_at, source_label, raw_payload
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id::text, seq
        """,
        (
            append.tenant_id,
            append.corpus_id,
            append.external_id,
            append.event_kind,
            append.bin_code,
            append.container_code,
            event_site,
            append.occurred_at,
            append.source_label,
            Jsonb(
                {
                    "schema_version": CONTAINMENT_EVENT_SCHEMA_VERSION,
                    "idempotency_key": append.idempotency_key,
                }
            ),
        ),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), int(str(row[1]))


def _record_unpack_location(
    conn: psycopg.Connection,
    append: _PreparedAppend,
    event_site: str,
) -> None:
    record_event(
        conn,
        event_kind="place",
        bin_code=append.bin_code,
        site=event_site,
        occurred_at=append.occurred_at,
        source_label=append.source_label,
        idempotency_key=f"containment-unpack-place:{append.idempotency_key}",
        metadata={"containment_external_id": append.external_id},
        tenant_id=append.tenant_id,
        corpus_id=append.corpus_id,
    )


def _lock_scope(
    conn: psycopg.Connection,
    tenant_id: str,
    corpus_id: str,
) -> None:
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('binkeeper-containment'), hashtext(%s))",
        (json.dumps([tenant_id, corpus_id], separators=(",", ":")),),
    )


def _existing_event(
    conn: psycopg.Connection,
    external_id: str,
    tenant_id: str,
    corpus_id: str,
) -> tuple[str, int, str, str, str] | None:
    row = conn.execute(
        """
        SELECT id::text, seq, event_kind, bin_code, container_code
        FROM bin_containment_events
        WHERE tenant_id = %s AND corpus_id = %s AND external_id = %s
        """,
        (tenant_id, corpus_id, external_id),
    ).fetchone()
    if row is None:
        return None
    return str(row[0]), int(str(row[1])), str(row[2]), str(row[3]), str(row[4])


def _require_same_action(
    existing: tuple[str, int, str, str, str],
    event_kind: ContainmentEventKind,
    bin_code: str,
    container_code: str,
) -> None:
    if tuple(str(value) for value in existing[2:]) != (
        event_kind,
        bin_code,
        container_code,
    ):
        raise BinNestingError("idempotency key was already used for another containment action")


def _required_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise BinNestingError(f"{field} is required")
    return normalized


def _external_id(idempotency_key: str, tenant_id: str, corpus_id: str) -> str:
    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "corpus_id": corpus_id,
            "idempotency_key": idempotency_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"containment:{hashlib.sha256(payload.encode()).hexdigest()[:40]}"
