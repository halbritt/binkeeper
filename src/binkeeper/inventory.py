"""Pure physical-inventory fold contract, re-exported from the canonical core.

The dataclasses and folds live in :mod:`binkeeper.bin_inventory`; this module
keeps the original extracted-contract import surface (including the
:class:`EventIdentity` signature of :func:`event_external_id`) without a second
implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from binkeeper.bin_inventory import (
    BinLocation,
    LocationStatus,
    TripChecksum,
    TripEvent,
    fold_bin_location,
    trip_checksum,
)
from binkeeper.bin_inventory import event_external_id as _event_external_id

__all__ = [
    "BinLocation",
    "EventIdentity",
    "LocationStatus",
    "TripChecksum",
    "TripEvent",
    "event_external_id",
    "fold_bin_location",
    "trip_checksum",
]


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


def event_external_id(event: EventIdentity, *, idempotency_key: str | None) -> str:
    """Derive the stable deduplication handle for one move event."""
    return _event_external_id(
        event_kind=event.event_kind,
        trip_id=event.trip_id,
        bin_code=event.bin_code,
        site=event.site,
        occurred_at=event.occurred_at,
        idempotency_key=idempotency_key,
        tenant_id=event.tenant_id,
        corpus_id=event.corpus_id,
    )
