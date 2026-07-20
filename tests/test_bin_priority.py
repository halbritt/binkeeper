"""RFC 0088 T3c — tests for the re-confirm prioritizer (src/engram/bin_priority.py).

Pure regret math + ranking (including the "20-mile bin sinks" reachability
discount) run without a database; the corpus-wide projection, distinct-code
enumeration, and the site-factors reorder run against the migrated test DB via
the ``conn`` fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from binkeeper.bin_inventory import BinBelief, BinLocation, LocationStatus, record_event
from binkeeper.bin_priority import (
    BinPriorityError,
    SiteFactors,
    build_candidate,
    expected_regret,
    load_all_bin_codes,
    load_site_factors,
    rank_reconfirm,
    reconfirm_priorities,
    reconfirm_priority,
)

NOW = datetime(2026, 6, 24, 18, 0, 0, tzinfo=UTC)


def _belief(
    bin_code: str,
    confidence: float,
    *,
    site: str | None = "garage",
    status: LocationStatus = "at_site",
    abstained: bool = False,
    age_days: float | None = 10.0,
) -> BinBelief:
    location = BinLocation(
        bin_code=bin_code,
        status=status,
        site=site,
        trip_id=None,
        last_event_seq=1,
        last_event_at=NOW,
    )
    return BinBelief(
        bin_code=bin_code,
        location=location,
        confidence=confidence,
        half_life_days=120.0,
        floor=0.5,
        abstained=abstained,
        age_days=age_days,
    )


# --- pure: regret math ------------------------------------------------------


def test_expected_regret_is_the_product():
    assert expected_regret(0.5, 0.5, 1.0) == pytest.approx(0.25)
    assert expected_regret(1.0, 1.0, 2.0) == pytest.approx(2.0)


def test_expected_regret_clamps_probabilities_and_floors_cost():
    assert expected_regret(1.5, 2.0, 1.0) == pytest.approx(1.0)  # probs clamp to 1
    assert expected_regret(0.5, 0.5, -3.0) == 0.0  # negative cost floored


def test_reconfirm_priority_discounts_by_reachability():
    full = reconfirm_priority(1.0, 1.0, 1.0, reachability=1.0)
    far = reconfirm_priority(1.0, 1.0, 1.0, reachability=0.05)
    assert far < full
    # An unreachable site sinks to zero priority no matter how stale.
    assert reconfirm_priority(1.0, 1.0, 1.0, reachability=0.0) == 0.0


# --- pure: build_candidate + ranking ----------------------------------------


def test_build_candidate_p_stale_is_one_minus_confidence():
    candidate = build_candidate(_belief("ALA-1", 0.3), need_prior=0.5)
    assert candidate.p_stale == pytest.approx(0.7)
    assert candidate.p_need == pytest.approx(0.5)
    assert candidate.expected_regret == pytest.approx(0.7 * 0.5 * 1.0)
    assert candidate.priority == pytest.approx(candidate.expected_regret)  # reachability 1.0


def test_unknown_bin_floats_up_with_full_staleness():
    candidate = build_candidate(
        _belief("ALA-9", 0.0, site=None, status="unknown", abstained=True, age_days=None)
    )
    assert candidate.p_stale == pytest.approx(1.0)


def test_rank_orders_by_priority_then_staleness():
    a = build_candidate(_belief("A", 0.1))  # very stale
    b = build_candidate(_belief("B", 0.9))  # fresh
    ranked = rank_reconfirm([b, a])
    assert [c.bin_code for c in ranked] == ["A", "B"]


def test_rank_sinks_the_unreachable_bin():
    # A bone-stale bin at an unreachable site vs a moderately stale reachable one.
    far = build_candidate(_belief("FAR", 0.0), factors=SiteFactors(reachability=0.05))
    near = build_candidate(_belief("NEAR", 0.5), factors=SiteFactors(reachability=1.0))
    ranked = rank_reconfirm([far, near])
    assert [c.bin_code for c in ranked] == ["NEAR", "FAR"]


# --- pure: site factors loader ----------------------------------------------


def test_load_site_factors_missing_file_is_empty(tmp_path):
    assert load_site_factors(tmp_path / "nope.json") == {}


def test_load_site_factors_parses_overrides_and_skips_underscore(tmp_path):
    path = tmp_path / "factors.json"
    path.write_text(
        '{"_README": "doc", "storage": {"reachability": 0.1, "cost_of_wrong": 2.0}, '
        '"shop": {"reachability": 0.9}}',
        encoding="utf-8",
    )
    factors = load_site_factors(path)
    assert set(factors) == {"storage", "shop"}
    assert factors["storage"].reachability == pytest.approx(0.1)
    assert factors["storage"].cost_of_wrong == pytest.approx(2.0)
    assert factors["shop"].cost_of_wrong == pytest.approx(1.0)  # default


def test_load_site_factors_rejects_non_object(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(BinPriorityError):
        load_site_factors(path)


# --- IO: against the migrated test DB ---------------------------------------


def test_load_all_bin_codes_is_distinct_and_sorted(conn: psycopg.Connection):
    record_event(conn, event_kind="place", bin_code="PRI-002", site="alameda-garage")
    record_event(conn, event_kind="confirm", bin_code="PRI-002", site="alameda-garage")
    record_event(conn, event_kind="place", bin_code="PRI-001", site="alameda-storage")
    assert load_all_bin_codes(conn) == ["PRI-001", "PRI-002"]


def test_reconfirm_priorities_ranks_stale_above_fresh(conn: psycopg.Connection):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    record_event(
        conn, event_kind="place", bin_code="PRI-STALE", site="alameda-storage", occurred_at=old
    )
    record_event(
        conn, event_kind="place", bin_code="PRI-FRESH", site="alameda-garage", occurred_at=NOW
    )
    ranked = reconfirm_priorities(conn, now=NOW)
    codes = [c.bin_code for c in ranked]
    assert codes.index("PRI-STALE") < codes.index("PRI-FRESH")
    stale = next(c for c in ranked if c.bin_code == "PRI-STALE")
    assert stale.p_stale > 0.9


def test_reconfirm_priorities_reachability_reorders(conn: psycopg.Connection):
    old = datetime(2020, 1, 1, tzinfo=UTC)
    half_life_ago = NOW - timedelta(days=120)  # ~one default half-life => confidence ~0.5
    record_event(
        conn, event_kind="place", bin_code="FAR-1", site="alameda-storage", occurred_at=old
    )
    record_event(
        conn,
        event_kind="place",
        bin_code="NEAR-1",
        site="oakland-fab-east",
        occurred_at=half_life_ago,
    )
    # Uniform reachability: the bone-stale FAR-1 outranks the half-fresh NEAR-1.
    uniform = [c.bin_code for c in reconfirm_priorities(conn, now=NOW)]
    assert uniform.index("FAR-1") < uniform.index("NEAR-1")
    # Discount the storage unit hard: NEAR-1 now rises above the un-reachable FAR-1.
    factors = {"alameda-storage": SiteFactors(reachability=0.05)}
    discounted = [c.bin_code for c in reconfirm_priorities(conn, now=NOW, site_factors=factors)]
    assert discounted.index("NEAR-1") < discounted.index("FAR-1")


def test_reconfirm_priorities_empty_corpus_is_empty(conn: psycopg.Connection):
    assert reconfirm_priorities(conn, now=NOW) == []


def test_confusion_multiplies_cost_and_jumps_the_queue():
    calm = build_candidate(_belief("CLM-1", 0.5), confusion=0, confusion_weight=0.5)
    confused = build_candidate(_belief("CNF-1", 0.5), confusion=2, confusion_weight=0.5)
    assert calm.cost_of_wrong == 1.0
    assert confused.cost_of_wrong == 2.0  # 1.0 * (1 + 0.5 * 2)
    assert confused.priority == 2 * calm.priority
    assert confused.confusion == 2
    ranked = rank_reconfirm([calm, confused])
    assert ranked[0].bin_code == "CNF-1"


def test_zero_weight_disables_the_confusion_multiplier():
    candidate = build_candidate(_belief("CNF-2", 0.5), confusion=5, confusion_weight=0.0)
    assert candidate.cost_of_wrong == 1.0
    assert candidate.confusion == 5
