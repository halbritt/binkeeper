"""RFC 0093 P2b — read-only placement-feedback fold over bin passports.

RFC 0093 P2a records immutable text-route receipts (``bin_routing_requests``)
and owner placement decisions over those receipts (``bin_placement_decisions``).
This module is the read side of that ledger: it folds owner corrections into
rebuildable bin passports so a later route reflects "what the owner corrected
last time" without ever mutating a past receipt, a capture, a location, or a
recorded decision.

The fold is deliberately conservative:

- **Scope isolation.** Corrections are loaded strictly inside the caller's
  ``(tenant_id, corpus_id)`` by explicit ``d.tenant_id``/``d.corpus_id``
  predicates on the decisions table. A same-``bin_code`` decision recorded in a
  different scope is inert (never leaks across tenants/corpora). A blank scope is
  refused outright (:class:`PlacementFeedbackError`) rather than run with an
  under-specified predicate.
- **Correction, not just endorsement.** An ``accept`` strengthens the accepted
  bin; a ``reject`` excludes the item from its recommended bin; an ``override``
  both accepts the owner-selected bin *and* excludes the bin the route wrongly
  recommended, so the old wrong recommendation cannot win again next time.
- **Additive.** A fold only appends to a passport's ``accepts``/``excludes`` and
  ``provenance_refs``. Nothing existing is removed or reordered. A bin that
  received no surviving correction is returned byte-identical to its base
  passport, keeping the projector version stamp confined to folded bins.
- **Read-only.** Every database access here is a ``SELECT``. This module writes
  no rows and runs no models.

To avoid an import cycle this module never imports :mod:`binkeeper.bin_placement`
(which imports :mod:`binkeeper.bin_route`) or :mod:`binkeeper.bin_route` itself
(which, once wired, imports :func:`load_folded_bin_passports` from here). It
reads only :mod:`binkeeper.bin_passport` and :mod:`binkeeper.bin_inventory`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, Literal

import psycopg

from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID
from binkeeper.bin_passport import BinPassport, load_bin_passports

# --------------------------------------------------------------------------- #
# Stage 1 — types, tunables, exception
# --------------------------------------------------------------------------- #

#: Projector version stamped onto a passport once at least one owner correction
#: folds into it. Base (unfolded) passports keep their original projector
#: version, so the feedback stamp stays confined to bins that actually changed.
BIN_PLACEMENT_FEEDBACK_VERSION: Final[str] = os.environ.get(
    "BINKEEPER_BIN_PLACEMENT_FEEDBACK_VERSION", "bin-passport-feedback.v1.rfc0093-p2b"
)

#: Environment kill switch. When this variable is set to a falsey value the fold
#: is skipped entirely and :func:`load_folded_bin_passports` returns the base
#: passports unchanged. Feedback folding is enabled by default.
PLACEMENT_FEEDBACK_ENABLED_ENV: Final[str] = "BINKEEPER_BIN_PLACEMENT_FEEDBACK_ENABLED"

#: Prefix for the provenance ref minted for each surviving correction directive.
PLACEMENT_DECISION_PROVENANCE_PREFIX: Final[str] = "placement-decision:"

DirectiveKind = Literal["accept", "exclude"]

#: Owner decision kinds that name an owner-selected target bin the fold treats as
#: authoritative and REQUIRED. ``override`` moves an item to the selected bin (and
#: suppresses the recommendation it replaced); ``create_new_bin`` routes it into a
#: freshly created bin. A row of either kind missing its selected bin is malformed
#: and yields no correction — the fold never falls back to the recommendation the
#: owner declined.
_SELECTED_TARGET_DECISION_KINDS: Final[frozenset[str]] = frozenset({"override", "create_new_bin"})
#: Owner decision kinds that exclude an item phrase from its recommended bin.
_EXCLUDE_DECISION_KINDS: Final[frozenset[str]] = frozenset({"reject"})

_FALSEY_ENV_VALUES: Final[frozenset[str]] = frozenset({"0", "false", "no", "off", ""})
_ITEM_KEY_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")


class PlacementFeedbackError(ValueError):
    """Domain root for placement-feedback fold failures."""


@dataclass(frozen=True)
class PlacementCorrection:
    """One owner placement directive joined to its immutable routing receipt.

    A correction is a single accept/exclude directive: the owner associated
    ``item_text`` with bin ``bin_code`` (``accept``) or excluded it from that
    bin (``exclude``). ``item_match_key`` is the normalized item used to key
    resolution; ``bin_code`` is the identity used to group winners. One owner
    decision may yield two directives — an ``override`` away from a different
    recommendation emits both an accept (for the selected bin) and an exclude
    (for the recommended bin) — so ``directive_kind`` distinguishes them.
    """

    decision_external_id: str
    decision_id: str
    seq: int
    decision_kind: str
    directive_kind: DirectiveKind
    bin_code: str
    item_text: str
    item_match_key: str
    decided_at: datetime
    tenant_id: str = DEFAULT_BIN_TENANT_ID
    corpus_id: str = DEFAULT_BIN_CORPUS_ID

    @property
    def identity(self) -> str:
        """The grouping identity for this correction: the target bin code."""
        return self.bin_code

    @property
    def resolution_key(self) -> tuple[str, str]:
        """The ``(item_match_key, identity)`` key resolution folds over."""
        return (self.item_match_key, self.bin_code)

    @property
    def provenance_ref(self) -> str:
        """Stable provenance pointer to the owner decision behind this directive."""
        return (
            f"{PLACEMENT_DECISION_PROVENANCE_PREFIX}"
            f"{self.decision_external_id}#{self.directive_kind}"
        )

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one correction directive."""
        return {
            "decision_external_id": self.decision_external_id,
            "decision_id": self.decision_id,
            "seq": self.seq,
            "decision_kind": self.decision_kind,
            "directive_kind": self.directive_kind,
            "bin_code": self.bin_code,
            "item_text": self.item_text,
            "item_match_key": self.item_match_key,
            "decided_at": self.decided_at.isoformat(),
            "tenant_id": self.tenant_id,
            "corpus_id": self.corpus_id,
            "provenance_ref": self.provenance_ref,
        }


@dataclass(frozen=True)
class FoldedFeedback:
    """The full read-only fold bundle: folded passports plus their derivation.

    This is the inspection/receipt surface. ``passports`` is what a router
    should route over. ``corrections``/``winners``/``grouped`` explain exactly
    which owner decisions survived and where they folded, so a receipt can be
    rebuilt without re-querying.
    """

    passports: tuple[BinPassport, ...]
    corrections: tuple[PlacementCorrection, ...]
    winners: tuple[PlacementCorrection, ...]
    grouped: Mapping[str, tuple[PlacementCorrection, ...]]
    feedback_enabled: bool
    feedback_version: str = BIN_PLACEMENT_FEEDBACK_VERSION

    @property
    def folded_bin_codes(self) -> tuple[str, ...]:
        """Bin codes that received at least one surviving correction directive."""
        if not self.feedback_enabled:
            return ()
        return tuple(sorted(self.grouped))

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for the fold bundle."""
        return {
            "feedback_enabled": self.feedback_enabled,
            "feedback_version": self.feedback_version,
            "folded_bin_codes": list(self.folded_bin_codes),
            "passports": [passport.to_json() for passport in self.passports],
            "corrections": [correction.to_json() for correction in self.corrections],
            "winners": [winner.to_json() for winner in self.winners],
            "grouped": {
                bin_code: [directive.to_json() for directive in directives]
                for bin_code, directives in self.grouped.items()
            },
        }


def normalize_item_key(text: str) -> str:
    """Normalize item text to a stable match key.

    Case is folded and every run of non-alphanumeric characters becomes a single
    space, so ``"USB-C Cable"`` and ``"usb-c   cable"`` share one key. An item
    that normalizes to the empty string carries no association.
    """
    return " ".join(_ITEM_KEY_TOKEN_RE.findall(text.casefold()))


# --------------------------------------------------------------------------- #
# Stage 2 — load owner corrections, scoped to the caller (PD1 / PD11)
# --------------------------------------------------------------------------- #


def load_placement_corrections(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[PlacementCorrection]:
    """Load owner placement decisions as accept/exclude directives. Read-only.

    Corrections are scoped strictly to ``(tenant_id, corpus_id)`` by explicit
    ``d.tenant_id``/``d.corpus_id`` predicates on the decisions table (PD1/PD11):
    a decision recorded in another scope, even for the same ``bin_code``, is
    never returned here. Each decision is joined to its immutable routing receipt
    (same-scope) to recover the item text, then reduced to its accept/exclude
    directive(s) — an ``override`` away from a different recommendation yields
    both an accept and an exclude. Decisions that yield no directive
    (``split``/``merge``/``not_an_item``), name no required target bin, or carry
    no resolvable item are dropped. ``now`` is accepted for loader symmetry;
    corrections are current owner policy and are not filtered by it.
    """
    _require_scope(tenant_id, corpus_id)
    rows = conn.execute(
        """
        SELECT
            d.id::text,
            d.external_id,
            d.seq,
            d.decision_kind,
            d.recommended_bin_code,
            d.selected_bin_code,
            d.decided_at,
            r.input_text
        FROM bin_placement_decisions d
        JOIN bin_routing_requests r
            ON r.tenant_id = d.tenant_id
           AND r.corpus_id = d.corpus_id
           AND r.id = d.routing_request_id
        WHERE d.tenant_id = %s
          AND d.corpus_id = %s
          AND r.tenant_id = %s
          AND r.corpus_id = %s
        ORDER BY d.decided_at, d.seq
        """,
        (tenant_id, corpus_id, tenant_id, corpus_id),
    ).fetchall()
    corrections: list[PlacementCorrection] = []
    for row in rows:
        corrections.extend(_corrections_from_row(row, tenant_id=tenant_id, corpus_id=corpus_id))
    return corrections


def _corrections_from_row(
    row: Sequence[object],
    *,
    tenant_id: str,
    corpus_id: str,
) -> list[PlacementCorrection]:
    """Derive the accept/exclude directives from a decision-join row (0-2).

    One owner decision reduces to zero, one, or two directives: an ``override``
    away from a different recommendation yields both an accept (for the selected
    bin) and an exclude (for the recommended bin); every other kind yields at
    most one. Rows that name no required target bin or carry no resolvable item
    text are dropped.
    """
    (
        decision_id,
        external_id,
        seq,
        decision_kind,
        recommended_bin_code,
        selected_bin_code,
        decided_at,
        input_text,
    ) = row
    directives = _directives_for_decision(
        str(decision_kind), recommended_bin_code, selected_bin_code
    )
    if not directives:
        return []
    item_text = _clean_text(input_text)
    if item_text is None:
        return []
    item_match_key = normalize_item_key(item_text)
    if not item_match_key:
        return []
    if not isinstance(decided_at, datetime):
        return []
    decision_kind_str = str(decision_kind)
    return [
        PlacementCorrection(
            decision_external_id=str(external_id),
            decision_id=str(decision_id),
            seq=int(seq),  # type: ignore[arg-type]
            decision_kind=decision_kind_str,
            directive_kind=directive_kind,
            bin_code=bin_code,
            item_text=item_text,
            item_match_key=item_match_key,
            decided_at=decided_at,
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
        for directive_kind, bin_code in directives
    ]


def _directives_for_decision(
    decision_kind: str,
    recommended_bin_code: object,
    selected_bin_code: object,
) -> list[tuple[DirectiveKind, str]]:
    """Map one decision to its accept/exclude directives (0, 1, or 2).

    - ``accept``: the owner endorsed the recommendation, so a single accept
      directive strengthens the accepted bin — the owner-selected target when the
      row records one, otherwise the recommendation it endorsed.
    - ``override``: the owner moved the item to a different bin. This yields an
      accept directive for the owner-selected bin AND — when the route had
      recommended a *different* bin — an exclude directive that suppresses that
      now-wrong recommendation, so it cannot win again next time.
    - ``create_new_bin``: the owner routed the item into a freshly created bin.
      Only an accept directive for that new bin is emitted; it stays inert until
      the bin is materialized (:func:`fold_corrections` skips bins with no base
      passport). The prior recommendation is left untouched.
    - ``reject``: a single exclude directive removes the item from the bin it was
      recommended into.

    ``override`` and ``create_new_bin`` name an owner-selected target, so a row
    that is missing it is malformed and yields no directive — the fold never
    silently reinforces a recommendation the owner declined. Plain ``accept`` may
    still fall back to the recommendation it endorsed. ``split``, ``merge`` and
    ``not_an_item`` carry no association and yield nothing.
    """
    recommended = _clean_text(recommended_bin_code)
    selected = _clean_text(selected_bin_code)

    if decision_kind == "accept":
        bin_code = selected or recommended
        if bin_code is None:
            return []
        return [("accept", bin_code)]

    if decision_kind in _SELECTED_TARGET_DECISION_KINDS:
        if selected is None:
            return []
        directives: list[tuple[DirectiveKind, str]] = [("accept", selected)]
        if decision_kind == "override" and recommended is not None and recommended != selected:
            directives.append(("exclude", recommended))
        return directives

    if decision_kind in _EXCLUDE_DECISION_KINDS:
        bin_code = recommended or selected
        if bin_code is None:
            return []
        return [("exclude", bin_code)]

    return []


# --------------------------------------------------------------------------- #
# Stage 3 — resolve corrections and group winners by identity (PD10)
# --------------------------------------------------------------------------- #


def resolve_corrections(
    corrections: Iterable[PlacementCorrection],
) -> list[PlacementCorrection]:
    """Keep the latest owner directive per ``(item_match_key, identity)``.

    Corrections sharing an item and a target bin are a temporal stream: the most
    recent decision wins, ordered by ``decided_at`` then append ``seq``. This is
    where reversal is honoured in both directions — a later ``reject`` overrides
    an earlier ``accept`` on the same item/bin, and a later ``accept`` overrides
    an earlier ``reject``. An ``override``'s accept (on the selected bin) and
    exclude (on the recommended bin) fall on distinct keys, so each is resolved
    against later decisions on its own bin. The result is deterministically
    ordered.
    """
    winners: dict[tuple[str, str], PlacementCorrection] = {}
    for correction in corrections:
        key = correction.resolution_key
        current = winners.get(key)
        if current is None or _decision_order(correction) > _decision_order(current):
            winners[key] = correction
    return sorted(winners.values(), key=lambda c: (c.item_match_key, c.bin_code))


def group_winners_by_identity(
    winners: Iterable[PlacementCorrection],
) -> dict[str, tuple[PlacementCorrection, ...]]:
    """Group surviving directives by bin identity, guarding item collisions.

    Association: each accept winner attaches an item to a bin. Collision: when
    one item has surviving accept associations to more than one bin, only the
    latest-decided association survives (an item cannot be simultaneously
    accepted into two bins); the losing associations are dropped. Exclude
    directives are independent and are all retained. Directives from distinct
    items toward the same bin are all kept, so two different items routed to one
    bin both fold.
    """
    winner_list = list(winners)
    best_accept: dict[str, PlacementCorrection] = {}
    for correction in winner_list:
        if correction.directive_kind != "accept":
            continue
        current = best_accept.get(correction.item_match_key)
        if current is None or _decision_order(correction) > _decision_order(current):
            best_accept[correction.item_match_key] = correction

    grouped: dict[str, list[PlacementCorrection]] = {}
    for correction in winner_list:
        if (
            correction.directive_kind == "accept"
            and best_accept.get(correction.item_match_key) is not correction
        ):
            continue
        grouped.setdefault(correction.bin_code, []).append(correction)

    return {
        bin_code: tuple(sorted(directives, key=lambda c: (c.item_match_key, _decision_order(c))))
        for bin_code, directives in sorted(grouped.items())
    }


# --------------------------------------------------------------------------- #
# Stage 4 — fold directives into passports and load folded views
# --------------------------------------------------------------------------- #


def fold_corrections(
    passports: Sequence[BinPassport],
    grouped: Mapping[str, Sequence[PlacementCorrection]],
) -> list[BinPassport]:
    """Fold grouped owner directives into their bins' passports. Additive.

    For each base passport with surviving directives, accept phrases append to
    ``accepts``, exclude phrases append to ``excludes`` (each deduped against the
    values already present), one provenance ref is added per surviving directive,
    and the passport is stamped with the feedback projector version. A passport
    with no directives is returned unchanged — same object, same projector
    version — so unfolded siblings stay byte-identical to their base and the
    version stamp is confined to bins that actually folded. Directives that name
    a bin with no base passport are inert (a create-new-bin correction does
    nothing until that bin is materialized). Passport order is preserved.
    """
    folded: list[BinPassport] = []
    for passport in passports:
        directives = grouped.get(passport.bin_code)
        if not directives:
            folded.append(passport)
            continue
        accepts = list(passport.accepts)
        excludes = list(passport.excludes)
        provenance_refs = list(passport.provenance_refs)
        for directive in directives:
            if directive.directive_kind == "accept":
                if directive.item_text not in accepts:
                    accepts.append(directive.item_text)
            elif directive.item_text not in excludes:
                excludes.append(directive.item_text)
            provenance_refs.append(directive.provenance_ref)
        folded.append(
            replace(
                passport,
                accepts=tuple(accepts),
                excludes=tuple(excludes),
                provenance_refs=tuple(provenance_refs),
                projector_version=BIN_PLACEMENT_FEEDBACK_VERSION,
            )
        )
    return folded


def load_folded_bin_passports(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[BinPassport]:
    """Load base passports and fold owner corrections into them. Read-only.

    This is the drop-in replacement for
    :func:`binkeeper.bin_passport.load_bin_passports` on the routing path: same
    arguments, same return type, but each passport reflects surviving owner
    corrections. With the kill switch engaged the base passports are returned
    unchanged.
    """
    return _load_and_fold(conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id)[0]


def load_folded_feedback(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> FoldedFeedback:
    """Load the full fold bundle: folded passports plus their derivation. Read-only."""
    passports, corrections, winners, grouped, enabled = _load_and_fold(
        conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id
    )
    return FoldedFeedback(
        passports=tuple(passports),
        corrections=tuple(corrections),
        winners=tuple(winners),
        grouped=grouped,
        feedback_enabled=enabled,
    )


def _load_and_fold(
    conn: psycopg.Connection,
    *,
    now: datetime | None,
    tenant_id: str,
    corpus_id: str,
) -> tuple[
    list[BinPassport],
    list[PlacementCorrection],
    list[PlacementCorrection],
    dict[str, tuple[PlacementCorrection, ...]],
    bool,
]:
    """Run the shared load → resolve → group → fold pipeline once."""
    _require_scope(tenant_id, corpus_id)
    base = load_bin_passports(conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id)
    if not _feedback_enabled():
        return base, [], [], {}, False
    corrections = load_placement_corrections(
        conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id
    )
    winners = resolve_corrections(corrections)
    grouped = group_winners_by_identity(winners)
    folded = fold_corrections(base, grouped)
    return folded, corrections, winners, grouped, True


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _require_scope(tenant_id: str, corpus_id: str) -> None:
    """Guard the caller-supplied fold scope.

    The fold is only meaningful inside a concrete ``(tenant_id, corpus_id)``: an
    empty scope predicate is exactly the cross-tenant/corpus leak PD1/PD11
    forbid, so a blank tenant or corpus is a caller-contract violation raised as
    :class:`PlacementFeedbackError` rather than something silently accepted.
    (Malformed *rows*, by contrast, are dropped, not raised — this guards the
    scope, not the data.)
    """
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise PlacementFeedbackError("tenant_id must be a non-empty string")
    if not isinstance(corpus_id, str) or not corpus_id.strip():
        raise PlacementFeedbackError("corpus_id must be a non-empty string")


def _feedback_enabled() -> bool:
    """Whether feedback folding is active (kill switch not engaged)."""
    raw = os.environ.get(PLACEMENT_FEEDBACK_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY_ENV_VALUES


def _decision_order(correction: PlacementCorrection) -> tuple[datetime, int]:
    """Deterministic recency key: decision time, then append sequence."""
    return (_aware(correction.decided_at), correction.seq)


def _aware(value: datetime | None) -> datetime:
    """Return a timezone-aware datetime for ordering, defaulting to the epoch min."""
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _clean_text(value: object) -> str | None:
    """Return a stripped non-empty string, or None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
