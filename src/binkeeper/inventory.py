"""Pure physical-inventory folds extracted from Engram's RFC 0088 contract."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

LocationStatus = Literal["at_site", "in_transit", "unknown"]


@dataclass(frozen=True)
class TripEvent:
    """One append-only physical move event."""

    seq: int
    event_kind: str
    occurred_at: datetime
    trip_id: str | None = None
    bin_code: str | None = None
    from_site: str | None = None
    to_site: str | None = None
    site: str | None = None


@dataclass(frozen=True)
class BinLocation:
    """The current location projected from a bin's move events."""

    bin_code: str
    status: LocationStatus
    site: str | None
    trip_id: str | None
    last_event_seq: int | None
    last_event_at: datetime | None


@dataclass(frozen=True)
class TripChecksum:
    """The reconciliation projection for one physical trip."""

    trip_id: str
    from_site: str | None
    to_site: str | None
    is_open: bool
    is_closed: bool
    loaded: tuple[str, ...]
    arrived: tuple[str, ...]
    unaccounted: tuple[str, ...]
    reconciled: bool


@dataclass(frozen=True)
class EventIdentity:
    """Stable identity inputs for one move event."""

    event_kind: str
    occurred_at: datetime
    tenant_id: str
    corpus_id: str
    trip_id: str | None = None
    bin_code: str | None = None
    site: str | None = None


def fold_bin_location(bin_code: str, events: Sequence[TripEvent]) -> BinLocation:
    """Project a bin's current location from its ordered move evidence."""
    relevant = [
        event
        for event in events
        if event.bin_code == bin_code and event.event_kind in {"place", "load", "arrive", "confirm"}
    ]
    if not relevant:
        return BinLocation(bin_code, "unknown", None, None, None, None)
    latest = max(relevant, key=lambda event: event.seq)
    if latest.event_kind == "load":
        return BinLocation(
            bin_code,
            "in_transit",
            None,
            latest.trip_id,
            latest.seq,
            latest.occurred_at,
        )
    return BinLocation(bin_code, "at_site", latest.site, None, latest.seq, latest.occurred_at)


def trip_checksum(trip_id: str, events: Sequence[TripEvent]) -> TripChecksum:
    """Project the bins still unaccounted for on a physical trip."""
    own_events = [event for event in events if event.trip_id == trip_id]
    opened = any(event.event_kind == "open" for event in own_events)
    closed = any(event.event_kind == "close" for event in own_events)
    loaded = _ordered_unique(
        event.bin_code
        for event in own_events
        if event.event_kind == "load" and event.bin_code is not None
    )
    arrived = _ordered_unique(
        event.bin_code
        for event in own_events
        if event.event_kind == "arrive" and event.bin_code is not None
    )
    arrived_set = set(arrived)
    unaccounted = tuple(bin_code for bin_code in loaded if bin_code not in arrived_set)
    return TripChecksum(
        trip_id=trip_id,
        from_site=next(
            (event.from_site for event in own_events if event.event_kind == "open"), None
        ),
        to_site=next((event.to_site for event in own_events if event.event_kind == "open"), None),
        is_open=opened and not closed,
        is_closed=closed,
        loaded=loaded,
        arrived=arrived,
        unaccounted=unaccounted,
        reconciled=not unaccounted,
    )


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def event_external_id(event: EventIdentity, *, idempotency_key: str | None) -> str:
    """Derive the stable deduplication handle for one move event."""
    if idempotency_key and idempotency_key.strip():
        basis = f"key:{idempotency_key.strip()}"
    else:
        basis = json.dumps(
            {
                "tenant_id": event.tenant_id,
                "corpus_id": event.corpus_id,
                "event_kind": event.event_kind,
                "trip_id": event.trip_id,
                "bin_code": event.bin_code,
                "site": event.site,
                "occurred_at": event.occurred_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"trip:{digest[:40]}"
