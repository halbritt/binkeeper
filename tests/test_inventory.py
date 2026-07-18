from __future__ import annotations

from datetime import UTC, datetime, timedelta

from binkeeper.inventory import (
    EventIdentity,
    TripEvent,
    event_external_id,
    fold_bin_location,
    trip_checksum,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_fold_unknown_when_no_events() -> None:
    assert fold_bin_location("TST-001", []).status == "unknown"


def test_fold_place_is_at_site() -> None:
    events = [
        TripEvent(
            seq=1,
            event_kind="place",
            occurred_at=NOW,
            bin_code="TST-001",
            site="site-a",
        )
    ]

    location = fold_bin_location("TST-001", events)

    assert (location.status, location.site) == ("at_site", "site-a")


def test_fold_load_is_in_transit_then_arrive_wins() -> None:
    events = [
        TripEvent(1, "place", NOW, bin_code="TST-001", site="site-a"),
        TripEvent(2, "load", NOW + timedelta(minutes=1), trip_id="TRIP-1", bin_code="TST-001"),
    ]

    in_transit = fold_bin_location("TST-001", events)

    assert (in_transit.status, in_transit.trip_id) == ("in_transit", "TRIP-1")

    events.append(
        TripEvent(
            3,
            "arrive",
            NOW + timedelta(minutes=2),
            trip_id="TRIP-1",
            bin_code="TST-001",
            site="site-b",
        )
    )

    arrived = fold_bin_location("TST-001", events)

    assert (arrived.status, arrived.site) == ("at_site", "site-b")


def test_trip_checksum_flags_unaccounted() -> None:
    events = [
        TripEvent(1, "open", NOW, trip_id="TRIP-1", from_site="site-a", to_site="site-b"),
        TripEvent(2, "load", NOW, trip_id="TRIP-1", bin_code="TST-001"),
        TripEvent(3, "load", NOW, trip_id="TRIP-1", bin_code="TST-002"),
        TripEvent(4, "arrive", NOW, trip_id="TRIP-1", bin_code="TST-001", site="site-b"),
    ]

    checksum = trip_checksum("TRIP-1", events)

    assert checksum.unaccounted == ("TST-002",)
    assert checksum.reconciled is False
    assert checksum.is_open is True
    assert (checksum.from_site, checksum.to_site) == ("site-a", "site-b")


def test_trip_checksum_reconciled_and_closed() -> None:
    events = [
        TripEvent(1, "open", NOW, trip_id="TRIP-1", to_site="site-b"),
        TripEvent(2, "load", NOW, trip_id="TRIP-1", bin_code="TST-001"),
        TripEvent(3, "arrive", NOW, trip_id="TRIP-1", bin_code="TST-001", site="site-b"),
        TripEvent(4, "close", NOW, trip_id="TRIP-1"),
    ]

    checksum = trip_checksum("TRIP-1", events)

    assert checksum.reconciled is True
    assert checksum.is_closed is True
    assert checksum.is_open is False


def test_external_id_keyed_is_stable_and_distinct() -> None:
    event = EventIdentity(
        event_kind="place",
        bin_code="TST-001",
        site="site-a",
        occurred_at=NOW,
        tenant_id="synthetic",
        corpus_id="synthetic",
    )

    first = event_external_id(event, idempotency_key="save-1")
    replay = event_external_id(event, idempotency_key="save-1")
    distinct = event_external_id(event, idempotency_key="save-2")

    assert first == replay != distinct
    assert first.startswith("trip:")
