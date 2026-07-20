"""RFC 0088 T3c tests for the read-only presence-gated sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg

from binkeeper.bin_inventory import record_event
from binkeeper.bin_presence import record_presence
from binkeeper.bin_sweep import bin_sweep

NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def _place(conn: psycopg.Connection, bin_code: str, site: str, occurred_at: datetime) -> None:
    record_event(
        conn,
        event_kind="place",
        bin_code=bin_code,
        site=site,
        occurred_at=occurred_at,
        idempotency_key=f"sweep-place-{bin_code}",
    )


def _arrive(conn: psycopg.Connection, site: str, occurred_at: datetime = NOW) -> None:
    record_presence(
        conn,
        site=site,
        event_kind="arrive",
        occurred_at=occurred_at,
        idempotency_key=f"sweep-arrive-{site}",
    )


def test_sweep_ranks_current_site_bins_worst_first(conn: psycopg.Connection) -> None:
    _place(conn, "SWP-STALE", "alameda-garage", NOW - timedelta(days=90))
    _place(conn, "SWP-FRESH", "alameda-garage", NOW - timedelta(days=1))
    _place(conn, "SWP-OTHER", "alameda-storage", NOW - timedelta(days=90))
    _arrive(conn, "alameda-garage", NOW)

    result = bin_sweep(conn, now=NOW, limit=8)

    assert result.abstained is False
    assert result.site == "alameda-garage"
    codes = [item.bin_code for item in result.items]
    # Only the current site's bins; the storage bin is excluded.
    assert set(codes) == {"SWP-STALE", "SWP-FRESH"}
    # Worst-first: the older placement has the higher P(stale).
    assert codes[0] == "SWP-STALE"
    assert result.items[0].p_stale >= result.items[-1].p_stale
    assert all(item.action for item in result.items)


def test_sweep_abstains_without_current_presence(conn: psycopg.Connection) -> None:
    _place(conn, "SWP-A", "alameda-garage", NOW - timedelta(days=30))

    result = bin_sweep(conn, now=NOW)

    assert result.abstained is True
    assert result.reason == "no_current_presence"
    assert result.items == ()
    assert result.site is None


def test_sweep_abstains_when_named_site_is_not_the_present_one(conn: psycopg.Connection) -> None:
    _place(conn, "SWP-A", "alameda-garage", NOW - timedelta(days=30))
    _arrive(conn, "alameda-garage", NOW)

    result = bin_sweep(conn, site="oakland-fab-east", now=NOW)

    assert result.abstained is True
    assert result.reason == "named_site_not_present"


def test_sweep_honors_a_matching_named_site(conn: psycopg.Connection) -> None:
    _place(conn, "SWP-A", "alameda-garage", NOW - timedelta(days=30))
    _arrive(conn, "alameda-garage", NOW)

    result = bin_sweep(conn, site="alameda-garage", now=NOW, limit=5)

    assert result.abstained is False
    assert result.site == "alameda-garage"


def test_sweep_never_mutates_the_ledgers(conn: psycopg.Connection) -> None:
    _place(conn, "SWP-A", "alameda-garage", NOW - timedelta(days=90))
    _arrive(conn, "alameda-garage", NOW)
    counts = (
        "SELECT (SELECT count(*) FROM bin_trip_events), (SELECT count(*) FROM bin_presence_events)"
    )
    before = conn.execute(counts).fetchone()

    bin_sweep(conn, now=NOW)

    assert conn.execute(counts).fetchone() == before


def test_sweep_prompts_a_rescan_for_a_confused_bin(conn: psycopg.Connection) -> None:
    _place(conn, "SWP-CONF", "alameda-garage", NOW - timedelta(days=30))
    for day in (3, 2):
        record_event(
            conn,
            event_kind="browse",
            bin_code="SWP-CONF",
            site="alameda-garage",
            occurred_at=NOW - timedelta(days=day),
            idempotency_key=f"sweep-browse-{day}",
        )
    _place(conn, "SWP-CALM", "alameda-garage", NOW - timedelta(days=30))
    _arrive(conn, "alameda-garage", NOW)

    result = bin_sweep(conn, now=NOW, limit=8)

    by_code = {item.bin_code: item for item in result.items}
    assert by_code["SWP-CONF"].confusion == 2
    assert by_code["SWP-CONF"].rescan is True
    assert "re-scan its contents" in by_code["SWP-CONF"].action
    assert by_code["SWP-CALM"].rescan is False
    assert "confirm it is still here" in by_code["SWP-CALM"].action


def test_sweep_batches_anchored_bins_into_one_shelf_task(conn: psycopg.Connection) -> None:
    from binkeeper.bin_colocation import record_colocation

    _place(conn, "SWP-A1", "alameda-garage", NOW - timedelta(days=60))
    _place(conn, "SWP-A2", "alameda-garage", NOW - timedelta(days=50))
    _place(conn, "SWP-SOLO", "alameda-garage", NOW - timedelta(days=40))
    for code in ("SWP-A1", "SWP-A2"):
        record_colocation(
            conn,
            anchor_code="LOC-003",
            member_code=code,
            strength=1.0,
            observed_at=NOW - timedelta(days=3),
            idempotency_key=f"sweep-coloc-{code}",
        )
    _arrive(conn, "alameda-garage", NOW)

    result = bin_sweep(conn, now=NOW, limit=8)

    assert len(result.anchor_tasks) == 1
    task = result.anchor_tasks[0]
    assert task.anchor_code == "LOC-003"
    assert task.bin_codes == ("SWP-A1", "SWP-A2")
    assert "Photograph LOC-003 once" in task.action
    assert task.summed_regret > 0
    # The solo bin stays a per-bin item, never an anchor task.
    assert all("SWP-SOLO" not in t.bin_codes for t in result.anchor_tasks)


def test_sweep_ignores_demoted_anchors(conn: psycopg.Connection) -> None:
    from binkeeper.bin_colocation import record_colocation

    _place(conn, "SWP-D1", "alameda-garage", NOW - timedelta(days=60))
    _place(conn, "SWP-D2", "alameda-garage", NOW - timedelta(days=60))
    for code in ("SWP-D1", "SWP-D2"):
        record_colocation(
            conn,
            anchor_code="LOC-009",
            member_code=code,
            strength=1.0,
            observed_at=NOW - timedelta(days=400),
            idempotency_key=f"sweep-dem-{code}",
        )
    _arrive(conn, "alameda-garage", NOW)

    result = bin_sweep(conn, now=NOW, limit=8)

    assert result.anchor_tasks == ()
