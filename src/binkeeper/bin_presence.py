"""RFC 0088 T3c — append-only site presence events for presence-gated bin work.

Presence answers "where is the owner/device right now?" for reachability-aware
surfaces. It is a side-channel over sites: recording presence can surface local
orders or sweeps, but it never changes a bin's location. The move ledger in
``bin_inventory.py`` remains the only relocation authority.

Layering (RFC 0012 pure/IO split):
- Pure: ``presence_recency`` / ``fold_presence`` / ``current_site``.
- IO: ``record_presence_event`` / ``record_presence`` /
  ``list_presence_events`` / ``current_presence``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import psycopg
from psycopg.types.json import Jsonb

DEFAULT_BIN_TENANT_ID: Final[str] = os.environ.get("BINKEEPER_BIN_TENANT_ID", "personal")
DEFAULT_BIN_CORPUS_ID: Final[str] = os.environ.get("BINKEEPER_BIN_CORPUS_ID", "personal")
DEFAULT_PRESENCE_SOURCE_LABEL: Final[str] = "manual"
PRESENCE_TABLE: Final[str] = "bin_presence_events"
PRESENCE_EVENT_SCHEMA_VERSION: Final[str] = "bin_presence_event.v1"
BIN_PRESENCE_WINDOW_HOURS: Final[float] = float(
    os.environ.get("BINKEEPER_BIN_PRESENCE_WINDOW_HOURS", "6")
)
BIN_PRESENCE_WINDOW_SECONDS: Final[float] = BIN_PRESENCE_WINDOW_HOURS * 3600.0

PresenceEventKind = Literal["arrive", "dwell", "depart"]
PRESENCE_EVENT_KINDS: Final[tuple[PresenceEventKind, ...]] = ("arrive", "dwell", "depart")
PresenceEventStatus = Literal["present", "absent"]
PresenceStatus = Literal["present", "stale", "absent", "unknown"]


class BinPresenceError(ValueError):
    """Domain root for bin-presence failures (RFC 0012 per-stage family)."""


@dataclass(frozen=True, init=False)
class PresenceEvent:
    """One append-only site-presence event."""

    seq: int
    event_kind: PresenceEventKind
    site: str
    occurred_at: datetime
    status: PresenceEventStatus
    source_label: str = DEFAULT_PRESENCE_SOURCE_LABEL
    accuracy_m: float | None = None

    def __init__(
        self,
        seq: int,
        event_kind: PresenceEventKind,
        site: str,
        occurred_at: datetime | None = None,
        *,
        observed_at: datetime | None = None,
        status: str | None = None,
        source_label: str = DEFAULT_PRESENCE_SOURCE_LABEL,
        accuracy_m: float | None = None,
    ) -> None:
        """Create a presence event, accepting occurred_at or observed_at vocabulary."""
        when = occurred_at or observed_at
        if when is None:
            raise BinPresenceError("PresenceEvent requires occurred_at or observed_at")
        if event_kind not in PRESENCE_EVENT_KINDS:
            allowed = ", ".join(PRESENCE_EVENT_KINDS)
            raise BinPresenceError(f"unknown event_kind {event_kind!r}; allowed: {allowed}")
        if status is not None and status not in {"present", "absent"}:
            raise BinPresenceError("presence status must be 'present' or 'absent'")
        normalized_status: PresenceEventStatus = (
            "absent" if status == "absent" or event_kind == "depart" else "present"
        )
        object.__setattr__(self, "seq", seq)
        object.__setattr__(self, "event_kind", event_kind)
        object.__setattr__(self, "site", site)
        object.__setattr__(self, "occurred_at", when)
        object.__setattr__(self, "status", normalized_status)
        object.__setattr__(self, "source_label", source_label)
        object.__setattr__(self, "accuracy_m", accuracy_m)

    @property
    def observed_at(self) -> datetime:
        """Compatibility alias for capture-web payload language."""
        return self.occurred_at

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one presence event."""
        return {
            "seq": self.seq,
            "event_kind": self.event_kind,
            "site": self.site,
            "status": self.status,
            "occurred_at": self.occurred_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "source_label": self.source_label,
            "accuracy_m": self.accuracy_m,
        }


@dataclass(frozen=True)
class PresenceState:
    """The folded current-site answer from presence events."""

    status: PresenceStatus
    site: str | None
    last_event_seq: int | None
    last_event_at: datetime | None
    recency: float
    age_seconds: float | None
    stale_after_seconds: float

    @property
    def is_present(self) -> bool:
        """True when the fold has a fresh current site."""
        return self.status == "present"

    @property
    def event_seq(self) -> int | None:
        """The event seq that established current presence, or None when not present."""
        return self.last_event_seq if self.is_present else None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a presence fold."""
        return {
            "status": self.status,
            "site": self.site,
            "is_present": self.is_present,
            "event_seq": self.event_seq,
            "last_event_seq": self.last_event_seq,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "recency": round(self.recency, 4),
            "age_seconds": round(self.age_seconds, 3) if self.age_seconds is not None else None,
            "stale_after_seconds": self.stale_after_seconds,
        }


@dataclass(frozen=True)
class PresenceRecordResult:
    """The outcome of an idempotent presence append."""

    external_id: str
    event_id: str
    seq: int
    occurred_at: datetime
    already_existed: bool

    @property
    def observed_at(self) -> datetime:
        """Compatibility alias for capture-web payload language."""
        return self.occurred_at

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON result shape."""
        return {
            "external_id": self.external_id,
            "event_id": self.event_id,
            "seq": self.seq,
            "occurred_at": self.occurred_at.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "already_existed": self.already_existed,
        }


def validate_presence_event(*, event_kind: str, site: str) -> PresenceEventKind:
    """Validate a presence event shape and return its normalized kind."""
    if event_kind not in PRESENCE_EVENT_KINDS:
        allowed = ", ".join(PRESENCE_EVENT_KINDS)
        raise BinPresenceError(f"unknown event_kind {event_kind!r}; allowed: {allowed}")
    if not (site and site.strip()):
        raise BinPresenceError("presence events require a non-empty site")
    kind: PresenceEventKind = event_kind  # type: ignore[assignment]
    return kind


def presence_recency(
    event: PresenceEvent,
    *,
    now: datetime,
    stale_after_seconds: float = BIN_PRESENCE_WINDOW_SECONDS,
) -> float:
    """Return a currentness score in [0,1] for one presence event."""
    if event.event_kind == "depart" or event.status == "absent":
        return 0.0
    if stale_after_seconds <= 0.0:
        return 0.0
    age_seconds = (now - event.occurred_at).total_seconds()
    if age_seconds <= 0.0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - (age_seconds / stale_after_seconds)))


def fold_presence(
    events: Sequence[PresenceEvent],
    *,
    now: datetime,
    stale_after_seconds: float = BIN_PRESENCE_WINDOW_SECONDS,
) -> PresenceState:
    """Fold presence events into a current-site state. Pure and total."""
    if not events:
        return PresenceState("unknown", None, None, None, 0.0, None, stale_after_seconds)
    latest = max(events, key=lambda event: (event.occurred_at, event.seq))
    age_seconds = (now - latest.occurred_at).total_seconds()
    recency = presence_recency(latest, now=now, stale_after_seconds=stale_after_seconds)
    if latest.event_kind == "depart" or latest.status == "absent":
        return PresenceState(
            "absent",
            None,
            latest.seq,
            latest.occurred_at,
            recency,
            age_seconds,
            stale_after_seconds,
        )
    status: PresenceStatus = "present" if recency > 0.0 else "stale"
    return PresenceState(
        status,
        latest.site if status == "present" else None,
        latest.seq,
        latest.occurred_at,
        recency,
        age_seconds,
        stale_after_seconds,
    )


def fold_current_presence(
    events: Sequence[PresenceEvent],
    *,
    now: datetime,
    window: timedelta | None = None,
) -> PresenceState:
    """Compatibility wrapper using a timedelta window."""
    stale_after_seconds = (
        window.total_seconds() if window is not None else BIN_PRESENCE_WINDOW_SECONDS
    )
    return fold_presence(events, now=now, stale_after_seconds=stale_after_seconds)


def current_site(
    events: Sequence[PresenceEvent],
    *,
    now: datetime,
    stale_after_seconds: float = BIN_PRESENCE_WINDOW_SECONDS,
) -> str | None:
    """Return the current site if presence is fresh enough, otherwise None."""
    return fold_presence(events, now=now, stale_after_seconds=stale_after_seconds).site


def _canonical_json(payload: dict[str, object]) -> str:
    """Deterministic JSON for hashing (sorted keys, compact)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def presence_external_id(
    *,
    event_kind: str,
    site: str,
    occurred_at: datetime,
    source_label: str,
    idempotency_key: str | None,
    tenant_id: str,
    corpus_id: str,
) -> str:
    """Derive the idempotency handle for one presence event."""
    if idempotency_key and idempotency_key.strip():
        basis = f"key:{idempotency_key.strip()}"
    else:
        basis = _canonical_json(
            {
                "tenant_id": tenant_id,
                "corpus_id": corpus_id,
                "event_kind": event_kind,
                "site": site,
                "occurred_at": occurred_at.isoformat(),
                "source_label": source_label,
            }
        )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"presence:{digest[:40]}"


_PRESENCE_COLUMNS = "seq, event_kind, site, occurred_at, source_label, accuracy_m"


def _row_to_presence(row: tuple[object, ...]) -> PresenceEvent:
    """Map a SELECT row (in _PRESENCE_COLUMNS order) to a PresenceEvent."""
    return PresenceEvent(
        seq=int(row[0]),  # type: ignore[arg-type]
        event_kind=row[1],  # type: ignore[arg-type]
        site=row[2],  # type: ignore[arg-type]
        occurred_at=row[3],  # type: ignore[arg-type]
        source_label=row[4],  # type: ignore[arg-type]
        accuracy_m=float(str(row[5])) if row[5] is not None else None,
    )


def record_presence_event(
    conn: psycopg.Connection,
    *,
    site: str,
    event_kind: str = "arrive",
    occurred_at: datetime | None = None,
    observed_at: datetime | None = None,
    source: str | None = None,
    source_label: str = DEFAULT_PRESENCE_SOURCE_LABEL,
    accuracy_m: float | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, object] | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> PresenceRecordResult:
    """Append one site-presence event idempotently."""
    kind = validate_presence_event(event_kind=event_kind, site=site)
    if accuracy_m is not None and accuracy_m < 0.0:
        raise BinPresenceError("accuracy_m must be non-negative when provided")
    when = occurred_at or observed_at or datetime.now(UTC)
    resolved_source = source or source_label
    external_id = presence_external_id(
        event_kind=kind,
        site=site,
        occurred_at=when,
        source_label=resolved_source,
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
    raw_payload: dict[str, object] = {
        "schema_version": PRESENCE_EVENT_SCHEMA_VERSION,
        "metadata": metadata or {},
        "idempotency_key": idempotency_key,
    }
    with conn.transaction():
        inserted = conn.execute(
            """
            INSERT INTO bin_presence_events (
                tenant_id, corpus_id, external_id, event_kind, site,
                occurred_at, source_label, accuracy_m, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, corpus_id, external_id) DO NOTHING
            RETURNING id::text, seq
            """,
            (
                tenant_id,
                corpus_id,
                external_id,
                kind,
                site,
                when,
                resolved_source,
                accuracy_m,
                Jsonb(raw_payload),
            ),
        ).fetchone()
        if inserted is not None:
            return PresenceRecordResult(
                external_id,
                str(inserted[0]),
                int(inserted[1]),
                when,
                False,
            )
        existing = conn.execute(
            """
            SELECT id::text, seq, occurred_at FROM bin_presence_events
            WHERE tenant_id = %s AND corpus_id = %s AND external_id = %s
            """,
            (tenant_id, corpus_id, external_id),
        ).fetchone()
    if existing is None:  # pragma: no cover - conflict without a row is impossible
        raise BinPresenceError("idempotent insert conflicted but no prior row was found")
    return PresenceRecordResult(external_id, str(existing[0]), int(existing[1]), existing[2], True)


def record_presence(
    conn: psycopg.Connection,
    *,
    site: str,
    event_kind: str = "arrive",
    occurred_at: datetime | None = None,
    observed_at: datetime | None = None,
    source: str | None = None,
    source_label: str = DEFAULT_PRESENCE_SOURCE_LABEL,
    accuracy_m: float | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, object] | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> PresenceRecordResult:
    """Compatibility wrapper for ``record_presence_event``."""
    return record_presence_event(
        conn,
        site=site,
        event_kind=event_kind,
        occurred_at=occurred_at,
        observed_at=observed_at,
        source=source,
        source_label=source_label,
        accuracy_m=accuracy_m,
        idempotency_key=idempotency_key,
        metadata=metadata,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )


def list_presence_events(
    conn: psycopg.Connection,
    *,
    site: str | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[PresenceEvent]:
    """Load presence events in seq order, optionally scoped to one site."""
    if site is not None:
        rows = conn.execute(
            f"""
            SELECT {_PRESENCE_COLUMNS} FROM bin_presence_events
            WHERE tenant_id = %s AND corpus_id = %s AND site = %s
            ORDER BY seq
            """,
            (tenant_id, corpus_id, site),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {_PRESENCE_COLUMNS} FROM bin_presence_events
            WHERE tenant_id = %s AND corpus_id = %s
            ORDER BY seq
            """,
            (tenant_id, corpus_id),
        ).fetchall()
    return [_row_to_presence(row) for row in rows]


def load_presence_events(
    conn: psycopg.Connection,
    *,
    site: str | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[PresenceEvent]:
    """Compatibility alias for loading presence events."""
    return list_presence_events(conn, site=site, tenant_id=tenant_id, corpus_id=corpus_id)


def current_presence(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    window: timedelta | None = None,
    stale_after_seconds: float | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> PresenceState:
    """Read and fold current site presence from the event stream."""
    seconds = (
        stale_after_seconds
        if stale_after_seconds is not None
        else window.total_seconds()
        if window is not None
        else BIN_PRESENCE_WINDOW_SECONDS
    )
    events = list_presence_events(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    return fold_presence(events, now=now or datetime.now(UTC), stale_after_seconds=seconds)


def list_current_presence(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    window_minutes: int | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> PresenceState:
    """CLI-friendly current-presence reader with a minute-based window."""
    seconds = window_minutes * 60.0 if window_minutes is not None else None
    return current_presence(
        conn,
        now=now,
        stale_after_seconds=seconds,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
