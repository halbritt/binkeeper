"""BINK-24 — pure batch routing over the P1 text router (stash-run core).

A stash run routes a batch of typed or vision-derived item texts in one pass:
each item goes through :func:`binkeeper.bin_route.route_text_item` unchanged,
the results partition into a swipeable *deck* (the router recommends a bin) and
a *pending pile* (the router abstained), and the deck compiles into a wave plan
grouped so each destination bin is opened exactly once. Read-only and advisory:
nothing here writes receipts or placement state — that is BINK-25/26 scope.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg

from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID
from binkeeper.bin_passport import BinPassport
from binkeeper.bin_route import TextRoute, route_text_item


@dataclass(frozen=True)
class StashItemRoute:
    """One batch item with its advisory route."""

    index: int
    text: str
    route: TextRoute

    @property
    def routable(self) -> bool:
        """Return whether the router recommends a bin (deck) or abstains (pending)."""
        return self.route.recommended_bin_code is not None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one batch item."""
        return {
            "index": self.index,
            "text": self.text,
            "disposition": "deck" if self.routable else "pending",
            "route": self.route.to_json(),
        }


@dataclass(frozen=True)
class WaveStop:
    """One destination bin in a wave plan, opened exactly once."""

    bin_code: str
    capacity_state: str
    item_indexes: tuple[int, ...]
    item_texts: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a wave stop."""
        return {
            "bin_code": self.bin_code,
            "capacity_state": self.capacity_state,
            "item_indexes": list(self.item_indexes),
            "item_texts": list(self.item_texts),
        }


@dataclass(frozen=True)
class StashBatchRoute:
    """A routed batch: deck, pending pile, wave plan, and abstention stats."""

    site: str
    items: tuple[StashItemRoute, ...]
    wave_plan: tuple[WaveStop, ...]

    @property
    def deck(self) -> tuple[StashItemRoute, ...]:
        """Return the routable items in batch order."""
        return tuple(item for item in self.items if item.routable)

    @property
    def pending(self) -> tuple[StashItemRoute, ...]:
        """Return the abstained items in batch order."""
        return tuple(item for item in self.items if not item.routable)

    @property
    def abstain_rate(self) -> float:
        """Return the abstained fraction of the batch (0.0 for an empty batch)."""
        return len(self.pending) / len(self.items) if self.items else 0.0

    def abstain_flag_counts(self) -> dict[str, int]:
        """Return how often each abstain flag occurred across the pending pile."""
        counts: dict[str, int] = {}
        for item in self.pending:
            for flag in item.route.abstain_flags:
                counts[flag] = counts.get(flag, 0) + 1
        return dict(sorted(counts.items()))

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for the routed batch."""
        return {
            "site": self.site,
            "item_count": len(self.items),
            "deck_count": len(self.deck),
            "pending_count": len(self.pending),
            "abstain_rate": round(self.abstain_rate, 4),
            "abstain_flag_counts": self.abstain_flag_counts(),
            "wave_plan": [stop.to_json() for stop in self.wave_plan],
            "items": [item.to_json() for item in self.items],
        }


def route_batch(
    *,
    items: Sequence[str],
    site: str,
    passports: Sequence[BinPassport],
    liveness: Mapping[str, float] | None = None,
) -> StashBatchRoute:
    """Route a batch of item texts and compile the wave plan. Pure.

    Each item routes independently through the unchanged single-item router, so
    a batch of one is byte-identical to ``route_text_item``. The wave plan
    groups deck items by recommended bin, ordered by bin code (within-site walk
    order is campaign-C scope), so each destination bin appears exactly once.
    """
    routed = tuple(
        StashItemRoute(
            index=index,
            text=text,
            route=route_text_item(text=text, site=site, passports=passports, liveness=liveness),
        )
        for index, text in enumerate(items)
    )
    capacity_by_bin = {passport.bin_code: passport.capacity_state for passport in passports}
    by_bin: dict[str, list[StashItemRoute]] = {}
    for item in routed:
        code = item.route.recommended_bin_code
        if code is not None:
            by_bin.setdefault(code, []).append(item)
    wave_plan = tuple(
        WaveStop(
            bin_code=code,
            capacity_state=capacity_by_bin.get(code, "unknown"),
            item_indexes=tuple(item.index for item in members),
            item_texts=tuple(item.text for item in members),
        )
        for code, members in sorted(by_bin.items())
    )
    return StashBatchRoute(site=site, items=routed, wave_plan=wave_plan)


def stash_route_batch(
    conn: psycopg.Connection,
    *,
    items: Sequence[str],
    site: str,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> StashBatchRoute:
    """Load owner-corrected passports once and route the whole batch. Read-only."""
    from binkeeper.bin_placement_feedback import load_folded_bin_passports
    from binkeeper.bin_route import _load_liveness  # shared lane gate

    passports = load_folded_bin_passports(conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id)
    liveness = _load_liveness(conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id)
    return route_batch(items=items, site=site, passports=passports, liveness=liveness)
