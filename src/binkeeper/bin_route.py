"""RFC 0093 P1 — pure text-only BinKeeper placement router.

The router is advisory: it scores typed item text against rebuildable bin
passports, returns score evidence, and never writes placement state.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import psycopg

from binkeeper.bin_inventory import BIN_LOCATION_FLOOR, DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID
from binkeeper.bin_passport import BinPassport
from binkeeper.bin_placement_feedback import load_folded_bin_passports

BIN_ROUTE_ROUTER_VERSION: Final[str] = os.environ.get(
    "ENGRAM_BIN_ROUTE_ROUTER_VERSION", "bin-route.v1.rfc0093-p1"
)
DEFAULT_PLACEMENT_FLOOR: Final[float] = float(
    os.environ.get("ENGRAM_BIN_ROUTE_PLACEMENT_FLOOR", "0.60")
)
DEFAULT_TIE_MARGIN: Final[float] = float(os.environ.get("ENGRAM_BIN_ROUTE_TIE_MARGIN", "0.05"))
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_FULL_CAPACITY_STATES: Final[frozenset[str]] = frozenset({"full", "overfull"})
_CONSTRAINT_REJECT_TOKENS: Final[frozenset[str]] = frozenset(
    {"no", "not", "without", "exclude", "excludes", "excluded"}
)
_SCORE_WEIGHTS: Final[dict[str, float]] = {
    "semantic_fit": 0.25,
    "sibling_fit": 0.18,
    "owner_phrase_fit": 0.10,
    "physical_fit": 0.15,
    "volume_fit": 0.12,
    "site_fit": 0.12,
    "confidence_fit": 0.08,
}
# RFC 0088 T3b: history_fit is an ADDITIVE bonus over the base score (not a
# rebalanced weight), so a route with history_fit == 0 (the default, and the
# case whenever the liveness lane is off or empty) is byte-identical to a
# router with no history_fit at all.
BIN_ROUTE_HISTORY_FIT_WEIGHT: Final[float] = float(
    os.environ.get("ENGRAM_BIN_ROUTE_HISTORY_FIT_WEIGHT", "0.05")
)
BIN_LIVENESS_SERVING_ENABLED: Final[bool] = os.environ.get(
    "ENGRAM_BIN_LIVENESS_ENABLED", ""
).strip().lower() not in {"", "0", "false", "no", "off"}
_CAPACITY_FIT: Final[dict[str, float]] = {
    "unknown": 0.55,
    "empty": 1.0,
    "sparse": 1.0,
    "half": 0.85,
    "tight": 0.45,
    "full": 0.0,
    "overfull": 0.0,
}


@dataclass(frozen=True)
class ItemCard:
    """Text item card used by the P1 router."""

    label: str
    tokens: tuple[str, ...]
    source: str = "text"

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a text item card."""
        return {
            "label": self.label,
            "tokens": list(self.tokens),
            "source": self.source,
        }


@dataclass(frozen=True)
class RouteScoreParts:
    """Placement score components from RFC 0093 §5."""

    semantic_fit: float
    sibling_fit: float
    owner_phrase_fit: float
    physical_fit: float
    volume_fit: float
    site_fit: float
    confidence_fit: float
    history_fit: float = 0.0

    @property
    def total_score(self) -> float:
        """Return the weighted route score plus the additive history_fit bonus."""
        base = sum(getattr(self, name) * weight for name, weight in _SCORE_WEIGHTS.items())
        return base + BIN_ROUTE_HISTORY_FIT_WEIGHT * self.history_fit

    @property
    def text_fit(self) -> float:
        """Return the best text-evidence component."""
        return max(self.semantic_fit, self.sibling_fit, self.owner_phrase_fit)

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for score parts."""
        return {
            "semantic_fit": round(self.semantic_fit, 4),
            "sibling_fit": round(self.sibling_fit, 4),
            "owner_phrase_fit": round(self.owner_phrase_fit, 4),
            "physical_fit": round(self.physical_fit, 4),
            "volume_fit": round(self.volume_fit, 4),
            "site_fit": round(self.site_fit, 4),
            "confidence_fit": round(self.confidence_fit, 4),
            "history_fit": round(self.history_fit, 4),
        }


@dataclass(frozen=True)
class RouteCandidate:
    """One scored candidate bin for an item card."""

    bin_code: str
    score_parts: RouteScoreParts
    hard_filters: tuple[str, ...]
    provenance_refs: tuple[str, ...]

    @property
    def total_score(self) -> float:
        """Return the weighted score for this candidate."""
        return self.score_parts.total_score

    @property
    def viable(self) -> bool:
        """Return whether no hard filter rejected this bin."""
        return len(self.hard_filters) == 0

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a route candidate."""
        return {
            "bin_code": self.bin_code,
            "total_score": round(self.total_score, 4),
            "score_parts": self.score_parts.to_json(),
            "hard_filters": list(self.hard_filters),
            "provenance_refs": list(self.provenance_refs),
        }


@dataclass(frozen=True)
class TextRoute:
    """Advisory text-route answer for one item card."""

    item_card: ItemCard
    site: str
    candidates: tuple[RouteCandidate, ...]
    top_candidate: RouteCandidate | None
    alternatives: tuple[RouteCandidate, ...]
    abstain_flags: tuple[str, ...]
    router_version: str = BIN_ROUTE_ROUTER_VERSION

    @property
    def recommended_bin_code(self) -> str | None:
        """Return the recommended bin code, or None when the router abstains."""
        if self.abstain_flags or self.top_candidate is None:
            return None
        return self.top_candidate.bin_code

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for the text-route answer."""
        return {
            "router_version": self.router_version,
            "item_card": self.item_card.to_json(),
            "site": self.site,
            "recommended_bin_code": self.recommended_bin_code,
            "abstain_flags": list(self.abstain_flags),
            "top_candidate": self.top_candidate.to_json() if self.top_candidate else None,
            "alternatives": [candidate.to_json() for candidate in self.alternatives],
            "candidates": [candidate.to_json() for candidate in self.candidates],
        }


def route_text_item(
    *,
    text: str,
    site: str,
    passports: Sequence[BinPassport],
    placement_floor: float = DEFAULT_PLACEMENT_FLOOR,
    tie_margin: float = DEFAULT_TIE_MARGIN,
    liveness: Mapping[str, float] | None = None,
) -> TextRoute:
    """Route one typed item string over fixture or loaded bin passports.

    ``liveness`` maps a normalized bin-vocabulary phrase to its RFC 0088 T3b
    decayed recency in (0, 1]. When None (the default, and whenever the liveness
    lane is off) every ``history_fit`` is 0 and the route is byte-identical to a
    router with no liveness enrichment at all.
    """
    item_card = item_card_from_text(text)
    candidates = tuple(
        sorted(
            (_candidate_for(item_card, site, passport, liveness) for passport in passports),
            key=lambda candidate: (-candidate.total_score, candidate.bin_code),
        )
    )
    viable_candidates = tuple(candidate for candidate in candidates if candidate.viable)
    top_candidate = viable_candidates[0] if viable_candidates else None
    alternatives = _alternatives(candidates, top_candidate)
    abstain_flags = _abstain_flags(
        candidates=candidates,
        viable_candidates=viable_candidates,
        top_candidate=top_candidate,
        placement_floor=placement_floor,
        tie_margin=tie_margin,
    )
    return TextRoute(
        item_card=item_card,
        site=site,
        candidates=candidates,
        top_candidate=top_candidate,
        alternatives=alternatives,
        abstain_flags=abstain_flags,
    )


def bin_route(
    conn: psycopg.Connection,
    *,
    text: str,
    site: str,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> TextRoute:
    """Load owner-corrected passports and route one typed item. Read-only.

    Passports are loaded through ``load_folded_bin_passports`` so each reflects
    surviving owner placement corrections; the base passport loader and
    :func:`route_text_item` are left unchanged.
    """
    passports = load_folded_bin_passports(conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id)
    liveness = _load_liveness(conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id)
    return route_text_item(text=text, site=site, passports=passports, liveness=liveness)


def _load_liveness(
    conn: psycopg.Connection, *, now: datetime | None, tenant_id: str, corpus_id: str
) -> Mapping[str, float] | None:
    """Load the T3b item-liveness scores when the lane is enabled, else None."""
    if not BIN_LIVENESS_SERVING_ENABLED:
        return None
    from binkeeper.bin_liveness import item_liveness_scores

    return item_liveness_scores(conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id)


def item_card_from_text(text: str) -> ItemCard:
    """Build the P1 text item card without model or vision calls."""
    label = text.strip()
    return ItemCard(label=label, tokens=tuple(sorted(_tokens_for(label))))


def _candidate_for(
    item_card: ItemCard,
    site: str,
    passport: BinPassport,
    liveness: Mapping[str, float] | None = None,
) -> RouteCandidate:
    hard_filters = _hard_filters(item_card, site, passport)
    score_parts = _score_parts(item_card, site, passport, hard_filters, liveness)
    return RouteCandidate(
        bin_code=passport.bin_code,
        score_parts=score_parts,
        hard_filters=hard_filters,
        provenance_refs=passport.provenance_refs,
    )


def _hard_filters(item_card: ItemCard, site: str, passport: BinPassport) -> tuple[str, ...]:
    filters: list[str] = []
    item_tokens = set(item_card.tokens)
    if _exclusion_score(item_tokens, passport.excludes) >= 0.5:
        filters.append("excluded_trait")
    if _physical_constraint_rejects(item_tokens, passport.physical_constraints):
        filters.append("physical_constraint")
    if passport.capacity_state in _FULL_CAPACITY_STATES:
        filters.append("full_bin")
    if passport.current_site is None:
        filters.append("location_abstained")
    elif passport.current_site != site:
        filters.append("wrong_site")
    if passport.location_confidence < BIN_LOCATION_FLOOR:
        filters.append("location_abstained")
    return _unique(filters)


def _score_parts(
    item_card: ItemCard,
    site: str,
    passport: BinPassport,
    hard_filters: Sequence[str],
    liveness: Mapping[str, float] | None = None,
) -> RouteScoreParts:
    item_tokens = set(item_card.tokens)
    semantic_fit = max(
        _phrase_score(item_tokens, passport.accepts),
        _phrase_score(item_tokens, passport.examples),
        _phrase_score(item_tokens, _strings(passport.theme)),
    )
    return RouteScoreParts(
        semantic_fit=semantic_fit,
        sibling_fit=_phrase_score(item_tokens, passport.sibling_contents),
        owner_phrase_fit=_phrase_score(item_tokens, _strings(passport.owner_phrase)),
        physical_fit=0.0 if _has_physical_reject(hard_filters) else 1.0,
        volume_fit=_CAPACITY_FIT.get(passport.capacity_state, 0.55),
        site_fit=1.0 if passport.current_site == site else 0.0,
        confidence_fit=_confidence_fit(passport),
        history_fit=_history_fit(item_tokens, passport, liveness),
    )


def _history_fit(
    item_tokens: set[str],
    passport: BinPassport,
    liveness: Mapping[str, float] | None,
) -> float:
    """Reward routing toward a bin whose matching contents are currently live.

    For each of the bin's vocabulary phrases that overlaps the item, take its
    decayed liveness (how recently items like it were mentioned in approved local
    text) and return the strongest. 0 when the lane is off or nothing matches --
    keeping the route byte-identical in that case.
    """
    if not liveness or not item_tokens:
        return 0.0
    from binkeeper.bin_liveness import normalize_phrase

    best = 0.0
    for phrase in (*passport.accepts, *passport.examples, *passport.sibling_contents):
        if _overlap_score(item_tokens, _tokens_for(phrase)) <= 0.0:
            continue
        best = max(best, liveness.get(normalize_phrase(phrase), 0.0))
    return best


def _abstain_flags(
    *,
    candidates: Sequence[RouteCandidate],
    viable_candidates: Sequence[RouteCandidate],
    top_candidate: RouteCandidate | None,
    placement_floor: float,
    tie_margin: float,
) -> tuple[str, ...]:
    flags: list[str] = []
    if not candidates:
        return ("no_passports",)
    if top_candidate is None:
        return _all_filtered_flags(candidates)
    if top_candidate.total_score < placement_floor:
        flags.append("top_score_below_floor")
    if top_candidate.score_parts.text_fit == 0.0:
        flags.append("no_accepting_passport")
    if _has_close_tie(viable_candidates, tie_margin):
        flags.append("close_tie")
    if flags:
        flags.extend(_matching_reject_flags(candidates))
    return _unique(flags)


def _all_filtered_flags(candidates: Sequence[RouteCandidate]) -> tuple[str, ...]:
    flags = ["no_viable_candidate"]
    for candidate in candidates:
        flags.extend(candidate.hard_filters)
    return _unique(flags)


def _has_close_tie(candidates: Sequence[RouteCandidate], tie_margin: float) -> bool:
    if len(candidates) < 2:
        return False
    if candidates[0].score_parts.text_fit == 0.0 or candidates[1].score_parts.text_fit == 0.0:
        return False
    return candidates[0].total_score - candidates[1].total_score <= tie_margin


def _matching_reject_flags(candidates: Sequence[RouteCandidate]) -> tuple[str, ...]:
    flags: list[str] = []
    for candidate in candidates:
        if candidate.viable or candidate.score_parts.text_fit == 0.0:
            continue
        flags.extend(candidate.hard_filters)
    return _unique(flags)


def _alternatives(
    candidates: Sequence[RouteCandidate],
    top_candidate: RouteCandidate | None,
) -> tuple[RouteCandidate, ...]:
    return tuple(candidate for candidate in candidates if candidate is not top_candidate)


def _phrase_score(item_tokens: set[str], phrases: Sequence[str]) -> float:
    if not item_tokens:
        return 0.0
    return max(
        (_overlap_score(item_tokens, _tokens_for(phrase)) for phrase in phrases),
        default=0.0,
    )


def _exclusion_score(item_tokens: set[str], phrases: Sequence[str]) -> float:
    """Return the strongest excluded-trait match, scored against the trait.

    Exclusion is a hard placement guard, not a ranking signal, so — unlike
    :func:`_phrase_score`, which divides matched tokens by the item token count
    and therefore dilutes as the item label grows — an excluded trait is scored
    *relative to the excluded phrase*: the fraction of the trait's own tokens
    present in the item. A one-token trait such as ``"liquid"`` fully matches the
    label ``"liquid detergent bottle"`` (1/1) instead of scoring 1/3 and slipping
    under the hard-filter threshold, and every token of a fully-present
    multi-token trait counts, independent of how many other words the label
    carries.
    """
    if not item_tokens:
        return 0.0
    return max(
        (_containment_score(item_tokens, _tokens_for(phrase)) for phrase in phrases),
        default=0.0,
    )


def _physical_constraint_rejects(item_tokens: set[str], constraints: Sequence[str]) -> bool:
    for constraint in constraints:
        constraint_tokens = _tokens_for(constraint)
        if not constraint_tokens & _CONSTRAINT_REJECT_TOKENS:
            continue
        rejected_tokens = constraint_tokens - _CONSTRAINT_REJECT_TOKENS
        if item_tokens & rejected_tokens:
            return True
    return False


def _has_physical_reject(filters: Sequence[str]) -> bool:
    return "excluded_trait" in filters or "physical_constraint" in filters


def _overlap_score(item_tokens: set[str], phrase_tokens: set[str]) -> float:
    if not phrase_tokens:
        return 0.0
    return len(item_tokens & phrase_tokens) / len(item_tokens)


def _containment_score(item_tokens: set[str], phrase_tokens: set[str]) -> float:
    if not phrase_tokens:
        return 0.0
    return len(item_tokens & phrase_tokens) / len(phrase_tokens)


def _tokens_for(text: str) -> set[str]:
    tokens: set[str] = set()
    for match in _TOKEN_RE.finditer(text):
        token = _normalize_token(match.group(0))
        if _is_useful_token(token):
            tokens.add(token)
    return tokens


def _normalize_token(token: str) -> str:
    lower_token = token.lower()
    if len(lower_token) > 4 and lower_token.endswith("ies"):
        return f"{lower_token[:-3]}y"
    if len(lower_token) > 4 and lower_token.endswith(("ches", "shes", "sses", "xes", "zes")):
        return lower_token[:-2]
    if len(lower_token) > 3 and lower_token.endswith("s") and not lower_token.endswith("ss"):
        return lower_token[:-1]
    return lower_token


def _is_useful_token(token: str) -> bool:
    return not (len(token) == 1 and token.isalpha())


def _confidence_fit(passport: BinPassport) -> float:
    confidence = (passport.passport_confidence + passport.location_confidence) / 2.0
    return max(0.0, min(1.0, confidence))


def _strings(value: str | None) -> tuple[str, ...]:
    return (value,) if value else ()


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen)
