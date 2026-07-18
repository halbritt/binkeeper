"""RFC 0088 T3c — reachability-aware re-confirm prioritizer (the "20-mile bin" fix).

The self-healing loop must never ask the owner to make a special trip. So the
re-confirm queue is not "every bin below the floor"; it ranks by **expected
regret** and then **discounts by reachability**, so a far storage-unit bin you
rarely visit sinks rather than nags, while the stale bin at the shop you are
about to walk into rises.

    expected_regret = P(stale) x P(you'll-need-it) x cost-of-being-wrong
    priority        = expected_regret x reachability        (the trip discount)

``P(stale)`` is real: it is ``1 - confidence`` from the T3a/T3b belief spine
(``compute_belief``), so this reads off the same decay + corroboration math that
serving uses. ``P(you'll-need-it)`` and ``cost-of-being-wrong`` are honest priors
for now (uniform unless the owner sets per-site factors), with a per-site
``reachability`` the one lever that actually changes the ordering today. Presence
frequency learning ``reachability`` from real arrivals is a later T3c slice.

This is a **read-only instrument** (like ``bin_belief``): it never writes and it
does not change ``bin_where`` / ``bin_belief`` answers. It is always available so
the prioritizer can be eyeballed before any re-confirm surface is turned on.

Layering (RFC 0012 pure/IO split):
- Pure: ``expected_regret`` / ``reconfirm_priority`` / ``build_candidate`` /
  ``rank_reconfirm`` — total functions over a belief + factors, unit-tested
  without a database.
- IO: ``load_all_bin_codes`` / ``load_site_factors`` / ``reconfirm_priorities``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import psycopg

from binkeeper.bin_inventory import (
    BIN_LOCATION_FLOOR,
    DEFAULT_BIN_CORPUS_ID,
    DEFAULT_BIN_TENANT_ID,
    BinBelief,
    compute_belief,
    compute_source_reliabilities,
    load_bin_events,
    load_bin_observations,
)

# --- tunables (ENGRAM_-prefixed, read at module top per RFC 0012) -----------

# The prior probability that any given bin is one you'll need soon, before a
# per-bin access signal exists. A flat 0.5 keeps the ranking driven by P(stale)
# and reachability until usage is learned (a later slice).
BIN_NEED_PRIOR: Final[float] = float(os.environ.get("ENGRAM_BIN_NEED_PRIOR", "0.5"))
# Default cost-of-being-wrong for a site with no override (relative units).
BIN_DEFAULT_COST: Final[float] = float(os.environ.get("ENGRAM_BIN_DEFAULT_COST", "1.0"))
# Default reachability for a site with no override: 1.0 = no trip discount.
BIN_DEFAULT_REACHABILITY: Final[float] = float(
    os.environ.get("ENGRAM_BIN_DEFAULT_REACHABILITY", "1.0")
)
# Optional per-site factor overrides (reachability / cost). Missing file => no
# overrides (every site uses the defaults above), so the prioritizer is a safe
# no-op-shaped instrument until the owner fills it in.
DEFAULT_SITE_FACTORS_FILE: Final[str] = os.environ.get(
    "ENGRAM_BIN_SITE_FACTORS_FILE",
    str(Path(__file__).resolve().parents[2] / "scripts" / "bin-capture" / "site-factors.json"),
)


class BinPriorityError(Exception):
    """Domain root for prioritizer failures (RFC 0012 per-stage family)."""


def _clamp01(value: float) -> float:
    """Clamp a value into [0, 1]."""
    return max(0.0, min(1.0, value))


# --- pure factors + regret math ---------------------------------------------


@dataclass(frozen=True)
class SiteFactors:
    """Per-site levers that shape a bin's re-confirm priority.

    ``reachability`` in [0,1] is how easily/often the owner is near this site; a
    low value sinks that site's bins so the system never asks for a special trip.
    ``cost_of_wrong`` (>= 0, relative units) scales how much being wrong about a
    bin here actually hurts (a mid-job fab consumable costs more than attic junk).
    """

    reachability: float = BIN_DEFAULT_REACHABILITY
    cost_of_wrong: float = BIN_DEFAULT_COST


@dataclass(frozen=True)
class ReconfirmCandidate:
    """One bin's place in the re-confirm queue, with the math that put it there."""

    bin_code: str
    site: str | None
    status: str
    confidence: float
    p_stale: float
    p_need: float
    cost_of_wrong: float
    reachability: float
    expected_regret: float
    priority: float
    abstained: bool
    age_days: float | None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one ranked candidate."""
        return {
            "bin_code": self.bin_code,
            "site": self.site,
            "status": self.status,
            "confidence": round(self.confidence, 4),
            "p_stale": round(self.p_stale, 4),
            "p_need": round(self.p_need, 4),
            "cost_of_wrong": round(self.cost_of_wrong, 4),
            "reachability": round(self.reachability, 4),
            "expected_regret": round(self.expected_regret, 6),
            "priority": round(self.priority, 6),
            "abstained": self.abstained,
            "age_days": round(self.age_days, 2) if self.age_days is not None else None,
        }


def expected_regret(p_stale: float, p_need: float, cost_of_wrong: float) -> float:
    """Expected regret of leaving a bin un-reconfirmed. Pure and total.

    ``P(stale) x P(you'll-need-it) x cost-of-being-wrong``. Probabilities are
    clamped to [0,1]; a negative cost is floored at 0 (no negative regret).
    """
    return _clamp01(p_stale) * _clamp01(p_need) * max(0.0, cost_of_wrong)


def reconfirm_priority(
    p_stale: float, p_need: float, cost_of_wrong: float, reachability: float
) -> float:
    """Expected regret discounted by reachability (the special-trip discount). Pure.

    A bin at a site you almost never reach (``reachability`` near 0) sinks to a
    near-zero priority no matter how stale, so it is surfaced as cold-storage
    rather than nagged about.
    """
    return expected_regret(p_stale, p_need, cost_of_wrong) * _clamp01(reachability)


def build_candidate(
    belief: BinBelief,
    *,
    factors: SiteFactors | None = None,
    need_prior: float = BIN_NEED_PRIOR,
) -> ReconfirmCandidate:
    """Turn a bin's belief + its site factors into a ranked re-confirm candidate. Pure.

    ``P(stale)`` is ``1 - confidence`` straight from the belief spine, so a freshly
    placed or well-corroborated bin has low regret and an old, uncorroborated one
    high. An ``unknown`` bin (no location event) has confidence 0, so it floats up.
    """
    resolved = factors or SiteFactors()
    p_stale = _clamp01(1.0 - belief.confidence)
    p_need = _clamp01(need_prior)
    regret = expected_regret(p_stale, p_need, resolved.cost_of_wrong)
    priority = regret * _clamp01(resolved.reachability)
    return ReconfirmCandidate(
        bin_code=belief.bin_code,
        site=belief.location.site,
        status=belief.location.status,
        confidence=belief.confidence,
        p_stale=p_stale,
        p_need=p_need,
        cost_of_wrong=resolved.cost_of_wrong,
        reachability=resolved.reachability,
        expected_regret=regret,
        priority=priority,
        abstained=belief.abstained,
        age_days=belief.age_days,
    )


def rank_reconfirm(candidates: Sequence[ReconfirmCandidate]) -> list[ReconfirmCandidate]:
    """Order candidates by priority (desc), tie-broken by staleness then bin code. Pure."""
    return sorted(
        candidates,
        key=lambda c: (-c.priority, -c.p_stale, c.bin_code),
    )


# --- IO ---------------------------------------------------------------------


def load_all_bin_codes(
    conn: psycopg.Connection,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[str]:
    """Distinct bin codes that have ever had a move event, in code order."""
    rows = conn.execute(
        """
        SELECT DISTINCT bin_code FROM bin_trip_events
        WHERE tenant_id = %s AND corpus_id = %s AND bin_code IS NOT NULL
        ORDER BY bin_code
        """,
        (tenant_id, corpus_id),
    ).fetchall()
    return [str(row[0]) for row in rows]


def load_site_factors(
    path: str | os.PathLike[str] = DEFAULT_SITE_FACTORS_FILE,
) -> dict[str, SiteFactors]:
    """Load per-site reachability/cost overrides (missing file => no overrides).

    The file is a JSON object keyed by site slug, each value an object with
    optional ``reachability`` and ``cost_of_wrong``. Keys beginning with ``_``
    (e.g. a ``_README``) are ignored, so a documented template stays harmless.
    """
    factors_path = Path(path)
    if not factors_path.exists():
        return {}
    try:
        raw = json.loads(factors_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BinPriorityError(f"could not read site factors file {factors_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BinPriorityError(f"site factors file {factors_path} must be a JSON object")
    factors: dict[str, SiteFactors] = {}
    for slug, entry in raw.items():
        if slug.startswith("_") or not isinstance(entry, dict):
            continue
        reachability = entry.get("reachability")
        cost = entry.get("cost_of_wrong")
        factors[slug] = SiteFactors(
            reachability=(
                float(reachability) if reachability is not None else BIN_DEFAULT_REACHABILITY
            ),
            cost_of_wrong=float(cost) if cost is not None else BIN_DEFAULT_COST,
        )
    return factors


def reconfirm_priorities(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    site_factors: Mapping[str, SiteFactors] | None = None,
    need_prior: float = BIN_NEED_PRIOR,
    floor: float = BIN_LOCATION_FLOOR,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[ReconfirmCandidate]:
    """Rank every known bin for re-confirmation by reachability-discounted regret.

    Reuses the belief spine: source reliabilities are projected once for the whole
    corpus (a read-time projection), then each bin's belief folds its own events +
    corroborating observations against that, so the priority reads off the same
    confidence serving would. Read-only; never writes.
    """
    when = now or datetime.now(UTC)
    resolved_factors = dict(site_factors) if site_factors is not None else load_site_factors()
    reliabilities = compute_source_reliabilities(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    candidates: list[ReconfirmCandidate] = []
    for bin_code in load_all_bin_codes(conn, tenant_id=tenant_id, corpus_id=corpus_id):
        events = load_bin_events(conn, bin_code, tenant_id=tenant_id, corpus_id=corpus_id)
        observations = load_bin_observations(
            conn, bin_code, tenant_id=tenant_id, corpus_id=corpus_id
        )
        belief = compute_belief(
            bin_code,
            events,
            now=when,
            floor=floor,
            observations=observations,
            source_reliability_by_kind=reliabilities or None,
        )
        factors = resolved_factors.get(belief.location.site) if belief.location.site else None
        candidates.append(build_candidate(belief, factors=factors, need_prior=need_prior))
    return rank_reconfirm(candidates)
