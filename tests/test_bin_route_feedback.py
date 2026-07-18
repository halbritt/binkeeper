"""RFC 0093 P2b — end-to-end routing tests over the wired ``bin_route``.

These tests exercise the *routing path* once ``bin_route`` has been swapped to
load owner-corrected passports (``load_folded_bin_passports``). They are the
route-level counterpart to the fold-module unit tests: rather than poke the fold
functions directly, they seed real ``bin_routing_requests`` /
``bin_placement_decisions`` ledger rows plus base ``captures``, then assert how a
``bin_route`` answer changes (or, deliberately, does *not* change) once the fold
is in effect.

Acceptance-check coverage (packet ``pkt-route-verification``):

- ``AC-EFFECT-ACCEPT-ROUTE-01`` — an owner accept folds the item phrase into the
  bin's ``accepts`` so a re-route raises ``semantic_fit`` and, when the base
  route abstained, gains the recommendation:
  :func:`test_accept_fold_raises_semantic_fit_on_the_route`,
  :func:`test_create_new_bin_is_inert_until_materialized_then_live`.
- ``AC-EFFECT-EXCLUDE-ROUTE-01`` — an in-effect exclude adds an
  ``excluded_trait`` hard filter to the matching item's candidate:
  :func:`test_exclude_fold_hard_filters_the_matching_item_on_the_route`.
- ``AC-KILL-ROUTE-01`` — the kill switch returns the base route, byte-for-byte:
  :func:`test_kill_switch_returns_the_base_route`.
- ``AC-RECEIPTS-01`` — routing is read-only; it never mutates the immutable
  route receipts, owner decisions, or captures:
  :func:`test_routing_never_mutates_receipts_or_captures`.
- ``AC-SIBLING-DIFFERS-01`` — a fold changes only the corrected bin; a sibling
  (including the loser of an accept collision) is never contaminated:
  :func:`test_accept_collision_never_contaminates_the_losing_sibling`.
- ``AC-C11A-ROUTE-01`` — an unfolded candidate's per-candidate route evidence is
  byte-identical beside a folded sibling in the same route:
  :func:`test_c11a_unfolded_candidate_is_byte_identical_beside_a_folded_sibling`.
- ``AC-C11B-01`` — the whole-set route is byte-identical to the base under the
  kill switch, and diverges legitimately (only in the folded bin) when a bin
  folds: :func:`test_c11b_whole_set_route_byte_identity_and_divergence`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from binkeeper.bin_passport import load_bin_passports
from binkeeper.bin_placement_feedback import (
    BIN_PLACEMENT_FEEDBACK_VERSION,
    PLACEMENT_FEEDBACK_ENABLED_ENV,
    load_folded_feedback,
)
from binkeeper.bin_route import RouteCandidate, TextRoute, bin_route, route_text_item

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
SITE = "alameda-garage"
TENANT = "personal"
CORPUS = "personal"
ROUTER_VERSION = "bin-route.v1.rfc0093-p1"


# --------------------------------------------------------------------------- #
# Seeding helpers
# --------------------------------------------------------------------------- #


def _insert_bin_capture(
    conn: psycopg.Connection,
    *,
    external_id: str,
    bin_code: str,
    contents_text: str,
    site: str = SITE,
    theme: str | None = None,
    accepts: tuple[str, ...] = (),
    excludes: tuple[str, ...] = (),
    examples: tuple[str, ...] = (),
    physical_constraints: tuple[str, ...] = (),
    capacity_state: str = "sparse",
    tenant_id: str = TENANT,
    corpus_id: str = CORPUS,
) -> None:
    """Append one immutable ``bin_capture`` so ``load_bin_passports`` sees a bin."""
    profile: dict[str, object] = {
        "accepts": list(accepts),
        "excludes": list(excludes),
        "examples": list(examples),
        "physical_constraints": list(physical_constraints),
        "capacity_state": capacity_state,
    }
    if theme is not None:
        profile["theme"] = theme
    metadata: dict[str, object] = {
        "kind": "bin_capture",
        "schema_version": "bin_capture.v1",
        "bin_code": bin_code,
        "site": site,
        "captured_at": NOW.isoformat(),
        "contents_text": contents_text,
        "bin_profile": profile,
    }
    source_row = conn.execute(
        "INSERT INTO sources (source_kind, external_id, raw_payload) "
        "VALUES ('capture', %s, '{}') RETURNING id",
        (f"route-feedback-src-{external_id}",),
    ).fetchone()
    assert source_row is not None
    conn.execute(
        """
        INSERT INTO captures (
            source_id, source_kind, external_id, raw_payload,
            capture_type, content_text, observed_at, tenant_id, corpus_id
        )
        VALUES (%s, 'capture', %s, %s, 'observation', %s, %s, %s, %s)
        """,
        (
            source_row[0],
            external_id,
            Jsonb({"metadata": metadata}),
            f"bin {bin_code} @ {site} - {contents_text}",
            NOW,
            tenant_id,
            corpus_id,
        ),
    )


def _seed_correction(
    conn: psycopg.Connection,
    *,
    key: str,
    item_text: str,
    decision_kind: str,
    recommended: str | None = None,
    selected: str | None = None,
    decided_at: datetime = NOW,
    site: str = SITE,
    tenant_id: str = TENANT,
    corpus_id: str = CORPUS,
) -> str:
    """Append one immutable route receipt + owner decision. Returns decision external_id.

    The fold module reads only ``(decision_kind, recommended_bin_code,
    selected_bin_code)`` off the decision and ``input_text`` off its joined
    receipt, so the receipt's route snapshot is intentionally opaque here.
    """
    rr_external = f"rr-{key}"
    rr_sha = hashlib.sha256(rr_external.encode("utf-8")).hexdigest()
    rr_row = conn.execute(
        """
        INSERT INTO bin_routing_requests (
            tenant_id, corpus_id, external_id, request_kind, input_text, site,
            requested_at, source_label, router_version, item_card_json,
            route_result_json, route_result_sha256, raw_payload
        ) VALUES (%s, %s, %s, 'text', %s, %s, %s, 'manual', %s, %s, %s, %s, %s)
        RETURNING id::text
        """,
        (
            tenant_id,
            corpus_id,
            rr_external,
            item_text,
            site,
            decided_at,
            ROUTER_VERSION,
            Jsonb({}),
            Jsonb({}),
            rr_sha,
            Jsonb({}),
        ),
    ).fetchone()
    assert rr_row is not None
    decision_external = f"dec-{key}"
    conn.execute(
        """
        INSERT INTO bin_placement_decisions (
            tenant_id, corpus_id, external_id, routing_request_id,
            decision_kind, recommended_bin_code, selected_bin_code, actor,
            decided_at, decision_payload, raw_payload
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'owner', %s, %s, %s)
        """,
        (
            tenant_id,
            corpus_id,
            decision_external,
            rr_row[0],
            decision_kind,
            recommended,
            selected,
            decided_at,
            Jsonb({}),
            Jsonb({}),
        ),
    )
    return decision_external


def _accept_ref(decision_external: str) -> str:
    return f"placement-decision:{decision_external}#accept"


def _exclude_ref(decision_external: str) -> str:
    return f"placement-decision:{decision_external}#exclude"


def _base_route(conn: psycopg.Connection, *, text: str, site: str = SITE) -> TextRoute:
    """Route over the *base* (unfolded) passports — the pre-feedback answer."""
    passports = load_bin_passports(conn, now=NOW, tenant_id=TENANT, corpus_id=CORPUS)
    return route_text_item(text=text, site=site, passports=passports)


def _candidate(route: TextRoute, bin_code: str) -> RouteCandidate:
    return next(candidate for candidate in route.candidates if candidate.bin_code == bin_code)


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _ledger_snapshot(conn: psycopg.Connection) -> dict[str, list[tuple[object, ...]]]:
    """A stable, fully-ordered snapshot of every feedback ledger + capture row."""
    requests = conn.execute(
        """
        SELECT external_id, input_text, site, route_result_json::text, route_result_sha256
        FROM bin_routing_requests ORDER BY seq
        """
    ).fetchall()
    decisions = conn.execute(
        """
        SELECT external_id, routing_request_id::text, decision_kind,
               recommended_bin_code, selected_bin_code, decided_at
        FROM bin_placement_decisions ORDER BY seq
        """
    ).fetchall()
    captures = conn.execute(
        "SELECT external_id, raw_payload::text FROM captures ORDER BY external_id"
    ).fetchall()
    return {"requests": requests, "decisions": decisions, "captures": captures}


# --------------------------------------------------------------------------- #
# AC-EFFECT-ACCEPT-ROUTE-01
# --------------------------------------------------------------------------- #


def test_accept_fold_raises_semantic_fit_on_the_route(conn: psycopg.Connection) -> None:
    """An owner accept folds the phrase into ``accepts``: ``semantic_fit`` rises
    from 0 and the previously-abstaining route now recommends the bin."""
    # Base bin has no textual association with the phrase at all: the base route
    # cannot recommend it (text_fit == 0 -> "no_accepting_passport").
    _insert_bin_capture(
        conn,
        external_id="cap-elx",
        bin_code="ELX-100",
        contents_text="assorted salvaged hardware",
        theme="salvaged hardware bin",
        accepts=("bracket", "bolt"),
    )

    base = _base_route(conn, text="ferrite bead")
    base_elx = _candidate(base, "ELX-100")
    assert base.recommended_bin_code is None
    assert "no_accepting_passport" in base.abstain_flags
    assert base_elx.score_parts.semantic_fit == 0.0

    # Owner routed "ferrite bead", the route abstained, and the owner selected
    # ELX-100 (an override onto a bin the route did not recommend -> a single
    # accept directive; no recommendation to suppress).
    decision = _seed_correction(
        conn,
        key="accept-ferrite",
        item_text="ferrite bead",
        decision_kind="override",
        recommended=None,
        selected="ELX-100",
    )

    folded = bin_route(conn, text="ferrite bead", site=SITE, now=NOW)
    folded_elx = _candidate(folded, "ELX-100")

    assert folded.recommended_bin_code == "ELX-100"
    assert folded.abstain_flags == ()
    assert folded_elx.score_parts.semantic_fit == pytest.approx(1.0)
    assert folded_elx.score_parts.semantic_fit > base_elx.score_parts.semantic_fit
    # The corrected candidate carries the owner-decision provenance.
    assert _accept_ref(decision) in folded_elx.provenance_refs
    assert _accept_ref(decision) not in base_elx.provenance_refs


# --------------------------------------------------------------------------- #
# AC-EFFECT-EXCLUDE-ROUTE-01
# --------------------------------------------------------------------------- #


def test_exclude_fold_hard_filters_the_matching_item_on_the_route(
    conn: psycopg.Connection,
) -> None:
    """An in-effect exclude adds an ``excluded_trait`` hard filter, dropping the
    bin's viability for the matching item on the live route."""
    _insert_bin_capture(
        conn,
        external_id="cap-glu",
        bin_code="GLU-200",
        contents_text="epoxy, super glue, adhesives",
        theme="adhesives and glues",
        accepts=("super glue", "epoxy"),
    )

    base = _base_route(conn, text="super glue")
    base_glu = _candidate(base, "GLU-200")
    assert base.recommended_bin_code == "GLU-200"
    assert base_glu.hard_filters == ()
    assert base_glu.viable is True

    # Owner rejected "super glue" out of the bin the route recommended it into.
    _seed_correction(
        conn,
        key="reject-glue",
        item_text="super glue",
        decision_kind="reject",
        recommended="GLU-200",
    )

    folded = bin_route(conn, text="super glue", site=SITE, now=NOW)
    folded_glu = _candidate(folded, "GLU-200")

    assert folded_glu.hard_filters == ("excluded_trait",)
    assert folded_glu.viable is False
    assert folded_glu.score_parts.physical_fit == 0.0
    # No other bin matches, so the item can no longer route into GLU-200.
    assert folded.recommended_bin_code != "GLU-200"
    assert folded.recommended_bin_code is None
    assert "excluded_trait" in folded.abstain_flags


# --------------------------------------------------------------------------- #
# AC-EFFECT-ACCEPT-ROUTE-01 (create_new_bin: inert-until-materialized then live)
# --------------------------------------------------------------------------- #


def test_create_new_bin_is_inert_until_materialized_then_live(
    conn: psycopg.Connection,
) -> None:
    """A ``create_new_bin`` correction naming a bin with no base passport is
    inert (the route is byte-identical to base); once that bin is materialized by
    a capture, the same correction folds and the route goes live."""
    _insert_bin_capture(
        conn,
        external_id="cap-decoy",
        bin_code="DEC-800",
        contents_text="painter's tape and drop cloths",
        theme="painting supplies",
        accepts=("painter's tape",),
    )
    decision = _seed_correction(
        conn,
        key="cnb-grommet",
        item_text="brass grommet",
        decision_kind="create_new_bin",
        selected="NEW-900",
    )

    # Phase A — inert. NEW-900 has no base passport, so the directive folds into
    # nothing: the route is byte-identical to the base route and NEW-900 never
    # appears as a candidate. The correction is nonetheless recorded.
    base_a = _base_route(conn, text="brass grommet")
    folded_a = bin_route(conn, text="brass grommet", site=SITE, now=NOW)
    assert _canonical(folded_a.to_json()) == _canonical(base_a.to_json())
    assert "NEW-900" not in {c.bin_code for c in folded_a.candidates}

    feedback = load_folded_feedback(conn, now=NOW, tenant_id=TENANT, corpus_id=CORPUS)
    assert "NEW-900" in feedback.grouped  # directive survives resolution ...
    assert "NEW-900" not in {p.bin_code for p in feedback.passports}  # ... but is inert

    # Phase B — materialize NEW-900 with a capture that does NOT itself accept the
    # phrase, so only the fold can make it route.
    _insert_bin_capture(
        conn,
        external_id="cap-new900",
        bin_code="NEW-900",
        contents_text="freshly emptied tote",
        theme="new empties",
        accepts=("odds and ends",),
        capacity_state="empty",
    )

    base_b = _base_route(conn, text="brass grommet")
    folded_b = bin_route(conn, text="brass grommet", site=SITE, now=NOW)
    folded_new = _candidate(folded_b, "NEW-900")

    assert base_b.recommended_bin_code != "NEW-900"  # base still cannot place it
    assert folded_b.recommended_bin_code == "NEW-900"  # the fold is now live
    assert folded_new.score_parts.semantic_fit == pytest.approx(1.0)
    assert _accept_ref(decision) in folded_new.provenance_refs


# --------------------------------------------------------------------------- #
# AC-SIBLING-DIFFERS-01 (collision never contaminates the losing sibling)
# --------------------------------------------------------------------------- #


def test_accept_collision_never_contaminates_the_losing_sibling(
    conn: psycopg.Connection,
) -> None:
    """When one item is accepted into two bins, only the latest-decided bin folds;
    the losing sibling's candidate stays byte-identical to its base evidence."""
    _insert_bin_capture(
        conn,
        external_id="cap-mach",
        bin_code="MET-500",
        contents_text="machinist odds and ends",
        theme="machinist drawer",
        accepts=("machinist tools",),
    )
    _insert_bin_capture(
        conn,
        external_id="cap-prec",
        bin_code="MET-600",
        contents_text="precision measuring instruments",
        theme="precision metrology",
        accepts=("micrometer",),
    )

    # The owner first accepted "vernier caliper" into MET-500, then later moved
    # it to MET-600. The later decision wins the item; MET-500 must not keep it.
    loser = _seed_correction(
        conn,
        key="cal-early",
        item_text="vernier caliper",
        decision_kind="override",
        selected="MET-500",
        decided_at=NOW - timedelta(hours=2),
    )
    winner = _seed_correction(
        conn,
        key="cal-late",
        item_text="vernier caliper",
        decision_kind="override",
        selected="MET-600",
        decided_at=NOW,
    )

    base = _base_route(conn, text="vernier caliper")
    folded = bin_route(conn, text="vernier caliper", site=SITE, now=NOW)

    base_loser = _candidate(base, "MET-500")
    folded_loser = _candidate(folded, "MET-500")
    folded_winner = _candidate(folded, "MET-600")

    # The winner folded: it accepts the item and recommends it.
    assert folded.recommended_bin_code == "MET-600"
    assert folded_winner.score_parts.semantic_fit == pytest.approx(1.0)
    assert _accept_ref(winner) in folded_winner.provenance_refs

    # The loser is uncontaminated: byte-identical to its base candidate, no
    # accept fold, and none of either decision's provenance leaked into it.
    assert _canonical(folded_loser.to_json()) == _canonical(base_loser.to_json())
    assert folded_loser.score_parts.semantic_fit == 0.0
    assert _accept_ref(loser) not in folded_loser.provenance_refs
    assert _accept_ref(winner) not in folded_loser.provenance_refs


# --------------------------------------------------------------------------- #
# AC-C11A-ROUTE-01
# --------------------------------------------------------------------------- #


def test_c11a_unfolded_candidate_is_byte_identical_beside_a_folded_sibling(
    conn: psycopg.Connection,
) -> None:
    """A bin that received no correction has a per-candidate route evidence blob
    byte-identical to its base, even when a sibling in the same route folds."""
    _insert_bin_capture(
        conn,
        external_id="cap-fold",
        bin_code="FLD-300",
        contents_text="assorted fasteners",
        theme="fastener bin",
        accepts=("wood screw",),
    )
    _insert_bin_capture(
        conn,
        external_id="cap-sib",
        bin_code="SIB-400",
        contents_text="soldering irons and flux",
        theme="soldering station spares",
        accepts=("solder", "flux"),
    )

    # Fold only FLD-300 (accept a brand-new phrase into it).
    _seed_correction(
        conn,
        key="c11a-accept",
        item_text="toggle latch",
        decision_kind="override",
        selected="FLD-300",
    )

    # Route an item so both bins appear as candidates in one answer.
    folded = bin_route(conn, text="toggle latch", site=SITE, now=NOW)
    base = _base_route(conn, text="toggle latch")

    folded_fold = _candidate(folded, "FLD-300")
    base_fold = _candidate(base, "FLD-300")
    folded_sib = _candidate(folded, "SIB-400")
    base_sib = _candidate(base, "SIB-400")

    # The unfolded sibling is byte-identical beside the folded bin.
    assert _canonical(folded_sib.to_json()) == _canonical(base_sib.to_json())
    # ...and the folded bin genuinely changed, so this is a real "beside a folded
    # sibling" comparison and not a no-op.
    assert _canonical(folded_fold.to_json()) != _canonical(base_fold.to_json())
    assert folded_fold.score_parts.semantic_fit == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# AC-KILL-ROUTE-01
# --------------------------------------------------------------------------- #


def test_kill_switch_returns_the_base_route(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the kill switch engaged, ``bin_route`` ignores every correction and
    returns exactly the base route."""
    _insert_bin_capture(
        conn,
        external_id="cap-kill",
        bin_code="KIL-700",
        contents_text="assorted grommets",
        theme="grommet bin",
        accepts=("rubber grommet",),
    )
    decision = _seed_correction(
        conn,
        key="kill-accept",
        item_text="cable gland",
        decision_kind="override",
        selected="KIL-700",
    )

    # Feedback on: the correction is live and folds into KIL-700.
    live = bin_route(conn, text="cable gland", site=SITE, now=NOW)
    live_kil = _candidate(live, "KIL-700")
    assert live.recommended_bin_code == "KIL-700"
    assert _accept_ref(decision) in live_kil.provenance_refs

    # Kill switch on: the very same call collapses to the base route.
    monkeypatch.setenv(PLACEMENT_FEEDBACK_ENABLED_ENV, "0")
    killed = bin_route(conn, text="cable gland", site=SITE, now=NOW)
    killed_kil = _candidate(killed, "KIL-700")
    base = _base_route(conn, text="cable gland")

    assert _canonical(killed.to_json()) == _canonical(base.to_json())
    assert killed.recommended_bin_code != "KIL-700"
    assert _accept_ref(decision) not in killed_kil.provenance_refs
    # The corrected version genuinely differed, so the kill switch really reverted.
    assert _canonical(killed.to_json()) != _canonical(live.to_json())


# --------------------------------------------------------------------------- #
# AC-RECEIPTS-01
# --------------------------------------------------------------------------- #


def test_routing_never_mutates_receipts_or_captures(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routing over folded passports is read-only: the immutable route receipts,
    owner decisions, and captures are byte-for-byte unchanged after routing."""
    _insert_bin_capture(
        conn,
        external_id="cap-receipt",
        bin_code="RCP-010",
        contents_text="zip ties and cable management",
        theme="cable management",
        accepts=("zip tie",),
    )
    _seed_correction(
        conn,
        key="receipt-accept",
        item_text="hook and loop strap",
        decision_kind="override",
        selected="RCP-010",
    )

    before = _ledger_snapshot(conn)

    # Route several times, with feedback on and with the kill switch, to be sure
    # neither path writes.
    bin_route(conn, text="hook and loop strap", site=SITE, now=NOW)
    bin_route(conn, text="zip tie", site=SITE, now=NOW)
    monkeypatch.setenv(PLACEMENT_FEEDBACK_ENABLED_ENV, "0")
    bin_route(conn, text="hook and loop strap", site=SITE, now=NOW)

    after = _ledger_snapshot(conn)
    assert after == before


# --------------------------------------------------------------------------- #
# AC-C11B-01
# --------------------------------------------------------------------------- #


def test_c11b_whole_set_route_byte_identity_and_divergence(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-set route byte-identity: under the kill switch the entire route
    answer equals the base; when a bin folds, the feedback route legitimately
    diverges from that base — and the divergence is confined to the folded bin,
    while an untouched sibling's candidate stays byte-identical."""
    _insert_bin_capture(
        conn,
        external_id="cap-w-fold",
        bin_code="WFD-100",
        contents_text="assorted clamps",
        theme="clamp bin",
        accepts=("bar clamp",),
    )
    _insert_bin_capture(
        conn,
        external_id="cap-w-sib",
        bin_code="WSB-200",
        contents_text="assorted abrasives",
        theme="abrasives bin",
        accepts=("sandpaper",),
    )
    # A phrase with no textual overlap with either base bin, so the base route
    # cannot place it and only the fold can make WFD-100 win.
    _seed_correction(
        conn,
        key="c11b-accept",
        item_text="hose pinch",
        decision_kind="override",
        selected="WFD-100",
    )

    base = _base_route(conn, text="hose pinch")
    feedback_on = bin_route(conn, text="hose pinch", site=SITE, now=NOW)

    # Capture the fold bundle while feedback is still enabled (before the kill
    # switch below) so the version-stamp check reflects the folded passports.
    feedback = load_folded_feedback(conn, now=NOW, tenant_id=TENANT, corpus_id=CORPUS)

    monkeypatch.setenv(PLACEMENT_FEEDBACK_ENABLED_ENV, "0")
    kill = bin_route(conn, text="hose pinch", site=SITE, now=NOW)

    # Whole-set kill-switch byte-identity to the base route.
    assert _canonical(kill.to_json()) == _canonical(base.to_json())
    # Legitimate whole-set divergence when a bin folds.
    assert _canonical(feedback_on.to_json()) != _canonical(base.to_json())
    assert feedback_on.recommended_bin_code == "WFD-100"
    assert base.recommended_bin_code != "WFD-100"

    # The divergence is confined: the folded bin's candidate changed, the
    # untouched sibling's candidate did not.
    assert _canonical(_candidate(feedback_on, "WFD-100").to_json()) != _canonical(
        _candidate(base, "WFD-100").to_json()
    )
    assert _canonical(_candidate(feedback_on, "WSB-200").to_json()) == _canonical(
        _candidate(base, "WSB-200").to_json()
    )
    # The feedback version stamp lands only on the folded bin's passport.
    folded_passport = next(p for p in feedback.passports if p.bin_code == "WFD-100")
    sibling_passport = next(p for p in feedback.passports if p.bin_code == "WSB-200")
    assert folded_passport.projector_version == BIN_PLACEMENT_FEEDBACK_VERSION
    assert sibling_passport.projector_version != BIN_PLACEMENT_FEEDBACK_VERSION
