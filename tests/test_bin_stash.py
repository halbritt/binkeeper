"""BINK-24 tests for pure stash-batch routing over the P1 router."""

from __future__ import annotations

import psycopg
import pytest

from binkeeper.bin_passport import BinPassport
from binkeeper.bin_route import route_text_item
from binkeeper.bin_stash import StashRunError, record_stash_run, route_batch


def _passport(
    bin_code: str,
    *,
    theme: str,
    current_site: str = "alameda-garage",
    accepts: tuple[str, ...] = (),
    sibling_contents: tuple[str, ...] = (),
    capacity_state: str = "half",
) -> BinPassport:
    return BinPassport(
        bin_code=bin_code,
        theme=theme,
        home_site=current_site,
        current_site=current_site,
        owner_phrase=None,
        accepts=accepts,
        excludes=(),
        examples=(),
        sibling_contents=sibling_contents,
        physical_constraints=(),
        volume_profile=None,
        capacity_state=capacity_state,  # type: ignore[arg-type]
        location_confidence=0.92,
        passport_confidence=0.86,
        provenance_refs=(f"capture:{bin_code}#metadata.bin_profile",),
    )


CABLES = _passport(
    "AGR-010",
    theme="electronics cables",
    accepts=("USB-C cables", "HDMI adapters"),
    sibling_contents=("USB-C cables, HDMI adapters, power bricks",),
)
FASTENERS = _passport(
    "AGR-011",
    theme="fasteners hardware",
    accepts=("wood screws", "zip ties"),
    sibling_contents=("screws, bolts, zip ties",),
)


def test_route_batch_partitions_deck_and_pending() -> None:
    batch = route_batch(
        items=["USB-C cable", "zip ties", "mystery gadget"],
        site="alameda-garage",
        passports=[CABLES, FASTENERS],
    )
    assert [item.text for item in batch.deck] == ["USB-C cable", "zip ties"]
    assert [item.text for item in batch.pending] == ["mystery gadget"]
    assert batch.abstain_rate == 1 / 3
    assert "no_accepting_passport" in batch.abstain_flag_counts()


def test_route_batch_single_item_matches_single_router() -> None:
    single = route_text_item(
        text="USB-C cable", site="alameda-garage", passports=[CABLES, FASTENERS]
    )
    batch = route_batch(items=["USB-C cable"], site="alameda-garage", passports=[CABLES, FASTENERS])
    assert batch.items[0].route.to_json() == single.to_json()


def test_wave_plan_opens_each_bin_exactly_once() -> None:
    batch = route_batch(
        items=["USB-C cable", "HDMI adapter", "wood screws", "zip ties"],
        site="alameda-garage",
        passports=[CABLES, FASTENERS],
    )
    stops = {stop.bin_code: stop for stop in batch.wave_plan}
    assert sorted(stops) == ["AGR-010", "AGR-011"]
    assert stops["AGR-010"].item_texts == ("USB-C cable", "HDMI adapter")
    assert stops["AGR-011"].item_texts == ("wood screws", "zip ties")
    assert stops["AGR-011"].capacity_state == "half"
    assert len(batch.wave_plan) == len(stops)


def test_empty_batch_is_empty_everything() -> None:
    batch = route_batch(items=[], site="alameda-garage", passports=[CABLES])
    assert batch.items == ()
    assert batch.wave_plan == ()
    assert batch.abstain_rate == 0.0
    assert batch.to_json()["item_count"] == 0


def test_batch_json_shape_is_stable() -> None:
    payload = route_batch(
        items=["USB-C cable"], site="alameda-garage", passports=[CABLES]
    ).to_json()
    assert payload["deck_count"] == 1
    assert payload["pending_count"] == 0
    assert payload["wave_plan"][0]["bin_code"] == "AGR-010"
    assert payload["items"][0]["disposition"] == "deck"


# --- IO: recorded stash runs (BINK-25) ---------------------------------------


def test_record_stash_run_fans_out_linked_receipts(conn: psycopg.Connection) -> None:
    record = record_stash_run(
        conn,
        items=["usb cable", "zip ties", "zip ties"],
        site="alameda-garage",
        idempotency_key="run-1",
    )
    assert record.already_existed is False
    assert len(record.receipts) == 3  # duplicate texts still get their own receipts
    linked = conn.execute(
        "SELECT count(*) FROM bin_routing_requests WHERE stash_run_id = %s",
        (record.stash_run_id,),
    ).fetchone()
    assert linked == (3,)
    payload = record.to_json()
    assert payload["item_count"] == 3
    assert payload["deck_count"] + payload["pending_count"] == 3


def test_record_stash_run_is_idempotent_at_run_and_receipt_level(
    conn: psycopg.Connection,
) -> None:
    first = record_stash_run(
        conn, items=["usb cable"], site="alameda-garage", idempotency_key="run-2"
    )
    replay = record_stash_run(
        conn, items=["usb cable"], site="alameda-garage", idempotency_key="run-2"
    )
    assert replay.already_existed is True
    assert replay.stash_run_id == first.stash_run_id
    assert [r["external_id"] for r in replay.receipts] == [r["external_id"] for r in first.receipts]
    counts = conn.execute(
        "SELECT (SELECT count(*) FROM stash_runs), (SELECT count(*) FROM bin_routing_requests)"
    ).fetchone()
    assert counts == (1, 1)


def test_stash_runs_are_append_only(conn: psycopg.Connection) -> None:
    record = record_stash_run(
        conn, items=["usb cable"], site="alameda-garage", idempotency_key="run-3"
    )
    with pytest.raises(psycopg.Error):
        conn.execute("DELETE FROM stash_runs WHERE id = %s", (record.stash_run_id,))


def test_record_stash_run_rejects_an_empty_batch(conn: psycopg.Connection) -> None:
    with pytest.raises(StashRunError):
        record_stash_run(conn, items=["  ", ""], site="alameda-garage")


# --- IO: wave plan + completion evidence (BINK-27) ----------------------------


def _decide(conn: psycopg.Connection, request_id: str, kind: str, selected: str | None) -> None:
    from binkeeper.bin_placement import PlacementDecisionAppend, record_placement_decision

    record_placement_decision(
        conn,
        PlacementDecisionAppend(
            routing_request_id=request_id,
            decision_kind=kind,  # type: ignore[arg-type]
            selected_bin_code=selected,
        ),
    )


def test_wave_plan_groups_placed_decisions_by_bin(conn: psycopg.Connection) -> None:
    from binkeeper.bin_stash import wave_plan_for_run

    run = record_stash_run(
        conn,
        items=["usb cable", "hdmi adapter", "zip ties", "mystery"],
        site="alameda-garage",
        idempotency_key="wave-1",
    )
    receipts = {r["seq"]: r for r in run.receipts}
    ordered = [receipts[k]["routing_request_id"] for k in sorted(receipts)]
    _decide(conn, str(ordered[0]), "override", "AGR-002")
    _decide(conn, str(ordered[1]), "override", "AGR-002")
    _decide(conn, str(ordered[2]), "override", "AGR-001")
    _decide(conn, str(ordered[3]), "not_an_item", None)

    plan = wave_plan_for_run(conn, stash_run_id=run.stash_run_id)

    assert [stop.bin_code for stop in plan.stops] == ["AGR-001", "AGR-002"]
    by_code = {stop.bin_code: stop for stop in plan.stops}
    assert by_code["AGR-002"].item_texts == ("usb cable", "hdmi adapter")
    assert by_code["AGR-001"].item_texts == ("zip ties",)
    assert plan.completed_count == 0
    assert all(stop.completed is False for stop in plan.stops)


def test_completing_a_stop_is_idempotent_and_enriches_contents(
    conn: psycopg.Connection,
) -> None:
    from datetime import UTC, datetime

    from binkeeper.bin_passport import bin_passport
    from binkeeper.bin_register import register_bin
    from binkeeper.bin_stash import complete_wave_stop, wave_plan_for_run

    register_bin(
        conn,
        bin_code="AGR-001",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    run = record_stash_run(
        conn, items=["zip ties", "usb cable"], site="alameda-garage", idempotency_key="wave-2"
    )
    ordered = sorted(run.receipts, key=lambda r: int(r["seq"]))
    for receipt in ordered:
        _decide(conn, str(receipt["routing_request_id"]), "override", "AGR-001")

    assert complete_wave_stop(conn, stash_run_id=run.stash_run_id, bin_code="AGR-001") is True
    assert complete_wave_stop(conn, stash_run_id=run.stash_run_id, bin_code="AGR-001") is False

    plan = wave_plan_for_run(conn, stash_run_id=run.stash_run_id)
    assert plan.completed_count == 1
    assert plan.stops[0].completed is True
    passport = bin_passport(conn, "AGR-001")
    assert "zip ties; usb cable" in passport.sibling_contents


def test_completing_an_unknown_stop_raises(conn: psycopg.Connection) -> None:
    from binkeeper.bin_stash import complete_wave_stop

    run = record_stash_run(
        conn, items=["usb cable"], site="alameda-garage", idempotency_key="wave-3"
    )
    with pytest.raises(StashRunError):
        complete_wave_stop(conn, stash_run_id=run.stash_run_id, bin_code="AGR-999")
