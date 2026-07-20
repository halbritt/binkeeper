"""RFC 0088 T3c presence-gated sweep: a read-only camera-pan over a site's stalest bins.

Given the owner's *fresh current presence*, this ranks that site's bins by the
landed bin-priority P(stale) belief and returns the N lowest-confidence bins,
worst-first, as a single list a person can pan a camera across and re-confirm.

Read-only by construction: it only reads the current-presence fold and the
bin-priority belief (both themselves read-only projections over the landed
ledgers). It issues no INSERT/UPDATE/DELETE, never relocates a bin, and never
writes any row of the movement or presence ledgers. Only the deliberate move
ledger relocates a bin; the sweep is a corroborative projection. When there is no
fresh current presence, or a named site is not the site the owner is standing at,
the sweep abstains cleanly with an explicit reason rather than guessing.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import psycopg

from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID
from binkeeper.bin_presence import current_presence
from binkeeper.bin_priority import ReconfirmCandidate, reconfirm_priorities

BIN_SWEEP_DEFAULT_LIMIT: Final[int] = int(os.environ.get("BINKEEPER_BIN_SWEEP_LIMIT", "8"))
# Confusion streak (opened-took-nothing looks, BINK-20) at which the sweep
# prompts a contents re-scan instead of a mere location confirm.
BIN_CONFUSION_RESCAN_THRESHOLD: Final[int] = int(
    os.environ.get("BINKEEPER_BIN_CONFUSION_RESCAN_THRESHOLD", "2")
)

_ABSTAIN_NO_PRESENCE: Final[str] = "no_current_presence"
_ABSTAIN_NAMED_SITE_NOT_PRESENT: Final[str] = "named_site_not_present"
_ABSTAIN_NO_BINS_AT_SITE: Final[str] = "no_bins_at_site"


class BinSweepError(ValueError):
    """Domain root for sweep failures (RFC 0012 family)."""


@dataclass(frozen=True)
class SweepItem:
    """One camera-pan row: a stale bin at the current site and how to confirm it."""

    bin_code: str
    confidence: float
    p_stale: float
    action: str
    confusion: int = 0
    rescan: bool = False

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a sweep item."""
        return {
            "bin_code": self.bin_code,
            "confidence": round(self.confidence, 4),
            "p_stale": round(self.p_stale, 4),
            "action": self.action,
            "confusion": self.confusion,
            "rescan": self.rescan,
        }


@dataclass(frozen=True)
class SweepResult:
    """The ordered camera-pan answer, or a clean abstention with a reason."""

    site: str | None
    items: tuple[SweepItem, ...]
    abstained: bool
    reason: str | None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a sweep result."""
        return {
            "site": self.site,
            "abstained": self.abstained,
            "reason": self.reason,
            "items": [item.to_json() for item in self.items],
        }


def bin_sweep(
    conn: psycopg.Connection,
    *,
    site: str | None = None,
    limit: int = BIN_SWEEP_DEFAULT_LIMIT,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> SweepResult:
    """Return the current site's stalest bins, worst-first. Read-only, presence-gated.

    ``site`` may be named only to *confirm* the site the presence fold already
    resolves; a named site the owner is not standing at abstains. A non-positive
    ``limit`` yields an empty (non-abstaining) list at the resolved site.
    """
    when = now or datetime.now(UTC)
    presence = current_presence(conn, now=when, tenant_id=tenant_id, corpus_id=corpus_id)
    if not presence.is_present or presence.site is None:
        return SweepResult(None, (), True, _ABSTAIN_NO_PRESENCE)

    resolved_site = presence.site
    named = site.strip() if isinstance(site, str) and site.strip() else None
    if named is not None and named != resolved_site:
        return SweepResult(None, (), True, _ABSTAIN_NAMED_SITE_NOT_PRESENT)

    candidates = reconfirm_priorities(conn, now=when, tenant_id=tenant_id, corpus_id=corpus_id)
    ranked = _rank_at_site(candidates, resolved_site, limit)
    if not ranked and limit > 0:
        return SweepResult(resolved_site, (), True, _ABSTAIN_NO_BINS_AT_SITE)
    items = tuple(
        SweepItem(
            bin_code=candidate.bin_code,
            confidence=candidate.confidence,
            p_stale=candidate.p_stale,
            action=_confirm_action(candidate, resolved_site),
            confusion=candidate.confusion,
            rescan=candidate.confusion >= BIN_CONFUSION_RESCAN_THRESHOLD,
        )
        for candidate in ranked
    )
    return SweepResult(resolved_site, items, False, None)


def _rank_at_site(
    candidates: Sequence[ReconfirmCandidate], site: str, limit: int
) -> list[ReconfirmCandidate]:
    """Keep this site's bins and order them worst-first (highest P(stale) first)."""
    at_site = [candidate for candidate in candidates if candidate.site == site]
    ordered = sorted(at_site, key=lambda candidate: (-candidate.p_stale, candidate.bin_code))
    if limit <= 0:
        return []
    return ordered[:limit]


def _confirm_action(candidate: ReconfirmCandidate, site: str) -> str:
    """Name the cheapest confirming action for one stale bin."""
    if candidate.confusion >= BIN_CONFUSION_RESCAN_THRESHOLD:
        return (
            f"{candidate.bin_code} keeps disappointing searches "
            f"({candidate.confusion} fruitless openings) — open it and re-scan its contents."
        )
    return f"Look at {candidate.bin_code} at {site} and confirm it is still here."
