"""RFC 0088 T2 — tests for the bin move/trip ledger (src/engram/bin_inventory.py).

Pure folds (validate_event / fold_bin_location / trip_checksum / external id)
run without a database; the IO append, idempotency, append-only guard, and the
trip reconciliation flow run against the migrated test DB via the ``conn``
fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from binkeeper.bin_inventory import (
    BinInventoryError,
    LocationObservation,
    TripEvent,
    adjudicate_observation,
    arrive_all,
    as_of_site,
    bin_belief,
    bin_half_life_days,
    bin_where,
    compute_belief,
    compute_source_reliabilities,
    decayed_confidence,
    event_external_id,
    fold_bin_location,
    observation_contradicts,
    record_event,
    record_observation,
    source_reliability,
    trip_checksum,
    trip_status,
    validate_event,
)

NOW = datetime(2026, 6, 24, 18, 0, 0, tzinfo=UTC)


def _event(
    seq: int,
    kind: str,
    *,
    trip_id: str | None = None,
    bin_code: str | None = None,
    site: str | None = None,
    from_site: str | None = None,
    to_site: str | None = None,
) -> TripEvent:
    return TripEvent(
        seq=seq,
        event_kind=kind,  # type: ignore[arg-type]
        occurred_at=NOW + timedelta(minutes=seq),
        trip_id=trip_id,
        bin_code=bin_code,
        site=site,
        from_site=from_site,
        to_site=to_site,
    )


# --- pure: validate_event ---------------------------------------------------


def test_validate_place_requires_bin_and_site():
    assert (
        validate_event(event_kind="place", trip_id=None, bin_code="ALA-1", site="garage") == "place"
    )
    with pytest.raises(BinInventoryError):
        validate_event(event_kind="place", trip_id=None, bin_code="ALA-1", site=None)
    with pytest.raises(BinInventoryError):
        validate_event(event_kind="place", trip_id=None, bin_code=None, site="garage")


def test_validate_open_and_close_reject_a_bin():
    assert validate_event(event_kind="open", trip_id="T1", bin_code=None, site=None) == "open"
    with pytest.raises(BinInventoryError):
        validate_event(event_kind="open", trip_id="T1", bin_code="ALA-1", site=None)


def test_validate_load_requires_trip_arrive_requires_site():
    assert validate_event(event_kind="load", trip_id="T1", bin_code="ALA-1", site=None) == "load"
    with pytest.raises(BinInventoryError):
        validate_event(event_kind="load", trip_id=None, bin_code="ALA-1", site=None)
    with pytest.raises(BinInventoryError):
        validate_event(event_kind="arrive", trip_id="T1", bin_code="ALA-1", site=None)


def test_validate_rejects_unknown_kind():
    with pytest.raises(BinInventoryError):
        validate_event(event_kind="teleport", trip_id=None, bin_code="ALA-1", site="x")


def test_validate_confirm_requires_bin_and_site_no_trip():
    assert (
        validate_event(event_kind="confirm", trip_id=None, bin_code="ALA-1", site="garage")
        == "confirm"
    )
    with pytest.raises(BinInventoryError):
        validate_event(event_kind="confirm", trip_id=None, bin_code="ALA-1", site=None)


def test_validate_contradict_requires_bin_no_site_no_trip():
    assert (
        validate_event(event_kind="contradict", trip_id=None, bin_code="ALA-1", site=None)
        == "contradict"
    )
    with pytest.raises(BinInventoryError):
        validate_event(event_kind="contradict", trip_id=None, bin_code=None, site=None)


# --- pure: fold_bin_location ------------------------------------------------


def test_fold_unknown_when_no_events():
    assert fold_bin_location("ALA-1", []).status == "unknown"


def test_fold_place_is_at_site():
    loc = fold_bin_location("ALA-1", [_event(1, "place", bin_code="ALA-1", site="garage")])
    assert (loc.status, loc.site) == ("at_site", "garage")


def test_fold_load_is_in_transit_then_arrive_wins():
    events = [
        _event(1, "place", bin_code="ALA-1", site="garage"),
        _event(2, "load", trip_id="T1", bin_code="ALA-1"),
    ]
    mid = fold_bin_location("ALA-1", events)
    assert (mid.status, mid.trip_id) == ("in_transit", "T1")
    events.append(_event(3, "arrive", trip_id="T1", bin_code="ALA-1", site="storage"))
    end = fold_bin_location("ALA-1", events)
    assert (end.status, end.site) == ("at_site", "storage")


def test_fold_ignores_other_bins():
    events = [
        _event(1, "place", bin_code="ALA-1", site="garage"),
        _event(2, "place", bin_code="ALA-2", site="shop"),
    ]
    assert fold_bin_location("ALA-1", events).site == "garage"


# --- pure: trip_checksum ----------------------------------------------------


def test_trip_checksum_flags_unaccounted():
    events = [
        _event(1, "open", trip_id="T1", from_site="garage", to_site="storage"),
        _event(2, "load", trip_id="T1", bin_code="ALA-1"),
        _event(3, "load", trip_id="T1", bin_code="ALA-2"),
        _event(4, "arrive", trip_id="T1", bin_code="ALA-1", site="storage"),
    ]
    checksum = trip_checksum("T1", events)
    assert checksum.unaccounted == ("ALA-2",)
    assert checksum.reconciled is False
    assert checksum.is_open is True
    assert (checksum.from_site, checksum.to_site) == ("garage", "storage")


def test_trip_checksum_reconciled_and_closed():
    events = [
        _event(1, "open", trip_id="T1", to_site="storage"),
        _event(2, "load", trip_id="T1", bin_code="ALA-1"),
        _event(3, "arrive", trip_id="T1", bin_code="ALA-1", site="storage"),
        _event(4, "close", trip_id="T1"),
    ]
    checksum = trip_checksum("T1", events)
    assert checksum.reconciled is True
    assert checksum.is_closed is True
    assert checksum.is_open is False


# --- pure: external id idempotency ------------------------------------------


def test_external_id_keyed_is_stable_and_distinct():
    common = dict(
        event_kind="place",
        trip_id=None,
        bin_code="ALA-1",
        site="garage",
        occurred_at=NOW,
        tenant_id="personal",
        corpus_id="personal",
    )
    a1 = event_external_id(idempotency_key="save-1", **common)
    a2 = event_external_id(idempotency_key="save-1", **common)
    b = event_external_id(idempotency_key="save-2", **common)
    assert a1 == a2 != b
    assert a1.startswith("trip:")


def test_external_id_content_hash_when_no_key():
    common = dict(
        event_kind="place",
        trip_id=None,
        bin_code="ALA-1",
        site="garage",
        occurred_at=NOW,
        tenant_id="personal",
        corpus_id="personal",
    )
    assert event_external_id(idempotency_key=None, **common) == event_external_id(
        idempotency_key=None, **common
    )


# --- IO: against the migrated test DB ---------------------------------------


def test_place_then_bin_where_at_site(conn: psycopg.Connection):
    record_event(conn, event_kind="place", bin_code="ALA-001", site="alameda-garage")
    loc = bin_where(conn, "ALA-001")
    assert (loc.status, loc.site) == ("at_site", "alameda-garage")


def test_record_event_is_idempotent(conn: psycopg.Connection):
    first = record_event(
        conn,
        event_kind="place",
        bin_code="ALA-002",
        site="alameda-garage",
        idempotency_key="save-x",
    )
    second = record_event(
        conn,
        event_kind="place",
        bin_code="ALA-002",
        site="alameda-garage",
        idempotency_key="save-x",
    )
    assert first.already_existed is False
    assert second.already_existed is True
    assert first.seq == second.seq


def test_bin_trip_events_is_append_only(conn: psycopg.Connection):
    record_event(conn, event_kind="place", bin_code="ALA-003", site="alameda-garage")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE bin_trip_events SET site = 'moved'")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM bin_trip_events")


def test_trip_flow_and_arrive_all(conn: psycopg.Connection):
    record_event(
        conn,
        event_kind="open",
        trip_id="TRIP-1",
        from_site="alameda-garage",
        to_site="alameda-storage",
    )
    record_event(conn, event_kind="load", trip_id="TRIP-1", bin_code="ALA-010")
    record_event(conn, event_kind="load", trip_id="TRIP-1", bin_code="ALA-011")
    record_event(
        conn, event_kind="arrive", trip_id="TRIP-1", bin_code="ALA-010", site="alameda-storage"
    )

    mid = trip_status(conn, "TRIP-1")
    assert mid.unaccounted == ("ALA-011",)
    assert bin_where(conn, "ALA-011").status == "in_transit"

    results = arrive_all(conn, "TRIP-1")
    assert len(results) == 1
    end = trip_status(conn, "TRIP-1")
    assert end.reconciled is True
    assert bin_where(conn, "ALA-011").site == "alameda-storage"


# --- pure T3: half-life, decay, belief --------------------------------------

DAY = timedelta(days=1)


def _move(
    seq: int,
    kind: str,
    occurred_at: datetime,
    *,
    bin_code: str = "ALA-1",
    site: str | None = None,
) -> TripEvent:
    return TripEvent(
        seq=seq,
        event_kind=kind,  # type: ignore[arg-type]
        occurred_at=occurred_at,
        bin_code=bin_code,
        site=site,
    )


def test_half_life_defaults_under_two_moves():
    assert bin_half_life_days([], default_days=120) == 120
    assert bin_half_life_days([_move(1, "place", NOW, site="garage")], default_days=120) == 120


def test_half_life_learns_median_gap_and_ignores_confirm():
    events = [
        _move(1, "place", NOW, site="garage"),
        _move(2, "confirm", NOW + 5 * DAY, site="garage"),  # re-verify, not a move
        _move(3, "place", NOW + 30 * DAY, site="garage"),
        _move(4, "place", NOW + 90 * DAY, site="garage"),
    ]
    # move gaps are 30d and 60d -> median 45d (the +5d confirm is excluded)
    assert bin_half_life_days(events) == 45.0


def test_decayed_confidence_curve():
    assert decayed_confidence(NOW, 30, NOW) == 1.0
    assert decayed_confidence(NOW, 30, NOW + 30 * DAY) == pytest.approx(0.5)
    assert decayed_confidence(NOW, 30, NOW + 300 * DAY) < 0.01
    assert decayed_confidence(NOW, 30, NOW, shock=0.6) == pytest.approx(0.4)
    assert decayed_confidence(NOW, 30, NOW, shock=2.0) == 0.0


def test_compute_belief_unknown_abstains():
    belief = compute_belief("ALA-1", [], now=NOW)
    assert belief.abstained is True
    assert belief.confidence == 0.0
    assert belief.location.status == "unknown"


def test_compute_belief_fresh_serves_old_abstains():
    fresh = compute_belief("ALA-1", [_move(1, "place", NOW, site="garage")], now=NOW, floor=0.5)
    assert fresh.abstained is False
    assert fresh.confidence == 1.0
    old = compute_belief(
        "ALA-1", [_move(1, "place", NOW, site="garage")], now=NOW + 400 * DAY, floor=0.5
    )
    assert old.abstained is True
    assert old.confidence < 0.5


def test_compute_belief_confirm_resets():
    events = [
        _move(1, "place", NOW, site="garage"),
        _move(2, "confirm", NOW + 400 * DAY, site="garage"),
    ]
    belief = compute_belief("ALA-1", events, now=NOW + 400 * DAY, floor=0.5)
    assert belief.abstained is False
    assert belief.confidence == 1.0


def test_compute_belief_contradiction_shocks_below_floor():
    events = [
        _move(1, "place", NOW, site="garage"),
        _move(2, "contradict", NOW),  # a shock after placement; carries no site
    ]
    belief = compute_belief("ALA-1", events, now=NOW, floor=0.5)
    assert belief.confidence == pytest.approx(0.4)  # base 1.0 - shock 0.6
    assert belief.abstained is True
    assert belief.location.site == "garage"  # location unchanged by a contradiction


# --- IO T3: belief against the migrated test DB -----------------------------


def test_bin_belief_db_fresh_then_confirm(conn: psycopg.Connection):
    record_event(conn, event_kind="place", bin_code="BEL-001", site="alameda-garage")
    belief = bin_belief(conn, "BEL-001")
    assert belief.location.site == "alameda-garage"
    assert belief.abstained is False
    record_event(conn, event_kind="confirm", bin_code="BEL-001", site="alameda-garage")
    assert bin_belief(conn, "BEL-001").abstained is False


def test_bin_belief_db_stale_abstains_but_keeps_location(conn: psycopg.Connection):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    record_event(
        conn, event_kind="place", bin_code="BEL-002", site="alameda-garage", occurred_at=old
    )
    belief = bin_belief(conn, "BEL-002")
    assert belief.abstained is True
    assert belief.location.site == "alameda-garage"


def test_bin_belief_db_contradict_drops_confidence(conn: psycopg.Connection):
    record_event(conn, event_kind="place", bin_code="BEL-003", site="alameda-garage")
    before = bin_belief(conn, "BEL-003").confidence
    record_event(conn, event_kind="contradict", bin_code="BEL-003")
    after = bin_belief(conn, "BEL-003").confidence
    assert after < before


# --- pure T3b: observations corroborate, never relocate ---------------------


def _obs(
    seq: int,
    observed_site: str,
    observed_at: datetime,
    *,
    bin_code: str = "ALA-1",
    source_kind: str = "photo_gps",
    strength: float = 1.0,
) -> LocationObservation:
    return LocationObservation(
        seq=seq,
        bin_code=bin_code,
        observed_site=observed_site,
        source_kind=source_kind,
        observed_at=observed_at,
        observation_strength=strength,
    )


def test_as_of_site_folds_ledger_as_of_time():
    events = [
        _move(1, "place", NOW, site="garage"),
        _move(2, "place", NOW + 10 * DAY, site="storage"),
    ]
    assert as_of_site("ALA-1", events, NOW + 5 * DAY) == "garage"
    assert as_of_site("ALA-1", events, NOW + 20 * DAY) == "storage"
    assert as_of_site("ALA-1", events, NOW - DAY) is None


def test_adjudicate_observation_hit_miss_unadjudicable():
    events = [_move(1, "place", NOW, site="garage")]
    assert adjudicate_observation(_obs(1, "garage", NOW + DAY), events) is True
    assert adjudicate_observation(_obs(2, "storage", NOW + DAY), events) is False
    assert adjudicate_observation(_obs(3, "garage", NOW - DAY), events) is None


def test_source_reliability_prior_and_posterior():
    # photo_gps prior (4, 1) -> 0.8 before any outcome; misses drag it down.
    assert source_reliability("photo_gps", hits=0, misses=0) == pytest.approx(0.8)
    assert source_reliability("photo_gps", hits=0, misses=4) == pytest.approx(4 / 9)
    # an unknown source starts at a neutral 0.5 prior and must earn trust.
    assert source_reliability("mystery", hits=0, misses=0) == pytest.approx(0.5)
    assert source_reliability("mystery", hits=3, misses=0) == pytest.approx(4 / 5)


def test_observation_contradicts():
    loc = fold_bin_location("ALA-1", [_move(1, "place", NOW, site="garage")])
    assert observation_contradicts(_obs(1, "garage", NOW), loc) is False
    assert observation_contradicts(_obs(2, "storage", NOW), loc) is True
    # no folded site (unknown bin) -> nothing to contradict.
    assert observation_contradicts(_obs(3, "storage", NOW), fold_bin_location("ALA-9", [])) is False


def test_compute_belief_corroboration_refreshes_stale():
    events = [_move(1, "place", NOW, site="garage")]
    stale = compute_belief("ALA-1", events, now=NOW + 400 * DAY, floor=0.5)
    assert stale.abstained is True

    refreshed = compute_belief(
        "ALA-1",
        events,
        now=NOW + 400 * DAY,
        floor=0.5,
        observations=[_obs(1, "garage", NOW + 399 * DAY)],
        source_reliability_by_kind={"photo_gps": 0.9},
    )
    assert refreshed.abstained is False
    assert refreshed.confidence > stale.confidence
    assert refreshed.corroborated_by_source == "photo_gps"
    assert refreshed.observation_count == 1


def test_compute_belief_reliability_caps_the_lift():
    events = [_move(1, "place", NOW, site="garage")]
    # a perfectly-fresh observation, but a 0.3-reliable source lifts confidence only to ~0.3.
    belief = compute_belief(
        "ALA-1",
        events,
        now=NOW + 400 * DAY,
        floor=0.5,
        observations=[_obs(1, "garage", NOW + 400 * DAY, source_kind="transcript_deixis")],
        source_reliability_by_kind={"transcript_deixis": 0.3},
    )
    assert belief.confidence == pytest.approx(0.3, abs=0.01)
    assert belief.abstained is True


def test_compute_belief_cross_site_observation_never_relocates():
    events = [_move(1, "place", NOW, site="garage")]
    belief = compute_belief(
        "ALA-1",
        events,
        now=NOW + 400 * DAY,
        floor=0.5,
        observations=[_obs(1, "storage", NOW + 400 * DAY)],
        source_reliability_by_kind={"photo_gps": 0.9},
    )
    assert belief.location.site == "garage"  # a disagreeing observation never moves the bin
    assert belief.corroborated_by_source is None
    assert belief.observation_count == 0
    assert belief.abstained is True


# --- IO T3b: observation stream against the migrated test DB ----------------


def test_record_observation_idempotent(conn: psycopg.Connection):
    first = record_observation(
        conn,
        bin_code="OBS-001",
        observed_site="alameda-garage",
        source_kind="photo_gps",
        idempotency_key="o-1",
    )
    second = record_observation(
        conn,
        bin_code="OBS-001",
        observed_site="alameda-garage",
        source_kind="photo_gps",
        idempotency_key="o-1",
    )
    assert first.already_existed is False
    assert second.already_existed is True
    assert first.seq == second.seq


def test_record_observation_rejects_empty_site(conn: psycopg.Connection):
    with pytest.raises(BinInventoryError):
        record_observation(conn, bin_code="OBS-002", observed_site="", source_kind="photo_gps")


def test_location_observation_is_append_only(conn: psycopg.Connection):
    record_observation(
        conn, bin_code="OBS-003", observed_site="alameda-garage", source_kind="photo_gps"
    )
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE location_observation SET observed_site = 'moved'")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM location_observation")


def test_bin_belief_observation_refreshes_stale(conn: psycopg.Connection):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    record_event(
        conn, event_kind="place", bin_code="OBS-004", site="alameda-garage", occurred_at=old
    )
    assert bin_belief(conn, "OBS-004").abstained is True
    record_observation(
        conn, bin_code="OBS-004", observed_site="alameda-garage", source_kind="photo_gps"
    )
    refreshed = bin_belief(conn, "OBS-004")
    assert refreshed.abstained is False
    assert refreshed.corroborated_by_source == "photo_gps"


def test_compute_source_reliabilities_learns_from_moves(conn: psycopg.Connection):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    record_event(
        conn, event_kind="place", bin_code="REL-001", site="alameda-garage", occurred_at=base
    )
    record_observation(
        conn,
        bin_code="REL-001",
        observed_site="alameda-garage",  # agrees with the ledger -> a hit
        source_kind="photo_gps",
        observed_at=base + timedelta(days=1),
    )
    record_observation(
        conn,
        bin_code="REL-001",
        observed_site="alameda-storage",  # disagrees with the ledger -> a miss
        source_kind="transcript_deixis",
        observed_at=base + timedelta(days=2),
    )
    rels = compute_source_reliabilities(conn)
    assert rels["photo_gps"] == pytest.approx(5 / 6)  # prior (4,1) + 1 hit
    assert rels["transcript_deixis"] == pytest.approx(1 / 4)  # prior (1,2) + 1 miss
