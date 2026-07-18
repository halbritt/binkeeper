"""RFC 0093 P2b tests for the read-only placement-feedback fold module.

This is the unit/DB verification packet (``pkt-unit-verification``). It is the
behavioral gate that certifies Stages 2-4 of :mod:`binkeeper.bin_placement_feedback`
(``pkt-feedback-module``): the module's own acceptance checks only cover its
static/API/read-only surface, so its correctness under real corrections is proven
here.

Two of these tests are named regression guards over prior major-fix reviews and
are called out explicitly:

- ``test_cross_scope_decision_is_inert`` — **AC-SCOPE-ISOLATION-01**, the
  review-171 guard: a same-``bin_code`` decision recorded in another
  tenant/corpus is inert and never leaks into this scope's fold.
- ``test_two_distinct_items_toward_one_bin_both_fold`` — **AC-MULTI-FOLD-01**,
  the review-152 guard: two *different* items corrected toward the *same* bin
  both survive grouping and both fold.

Acceptance-check coverage (one or more tests each):

    AC-KEY-01              test_key_normalization_folds_case_and_separators
    AC-ASSOC-01            test_association_attaches_each_accept_to_its_bin
    AC-COLLISION-01        test_collision_keeps_only_latest_accept_across_bins
    AC-TEMPORAL-01         test_temporal_reversal_resolves_both_directions
    AC-MULTI-FOLD-01       test_two_distinct_items_toward_one_bin_both_fold
    AC-FOLD-ACCEPT-01      test_accept_directive_folds_into_passport_accepts
    AC-FOLD-EXCLUDE-01     test_exclude_directive_folds_into_passport_excludes
    AC-PROV-01             test_provenance_adds_one_ref_per_surviving_directive
    AC-ADD-01              test_fold_is_additive_and_preserves_existing_values
                           test_fold_dedupes_repeated_phrase_but_still_records_ref
    AC-VERSION-01          test_version_stamp_confined_to_folded_bins
    AC-DETERMINISM-01      test_fold_pipeline_is_deterministic_under_reordering
    AC-LOAD-01             test_load_placement_corrections_derives_from_receipt
                           test_load_folded_feedback_reports_full_derivation
                           test_blank_scope_is_refused
    AC-DERIVE-01           test_loader_derives_every_decision_kind
    AC-SCOPE-ISOLATION-01  test_cross_scope_decision_is_inert
    AC-C11A-PASSPORT-01    test_unfolded_sibling_passport_is_byte_identical
    AC-KILL-LOAD-01        test_load_level_kill_switch_returns_base_passports

Tests are deterministic and make no live model calls. The pure Stage 1/3/4 tests
run without a database; the loader/scope/kill-switch/C11a tests use the shared
``conn`` fixture (skipped when ``ENGRAM_TEST_DATABASE_URL`` is unset).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from binkeeper.bin_passport import (
    BIN_PASSPORT_PROJECTOR_VERSION,
    BinPassport,
    load_bin_passports,
)
from binkeeper.bin_placement_feedback import (
    BIN_PLACEMENT_FEEDBACK_VERSION,
    PLACEMENT_FEEDBACK_ENABLED_ENV,
    FoldedFeedback,
    PlacementCorrection,
    PlacementFeedbackError,
    fold_corrections,
    group_winners_by_identity,
    load_folded_bin_passports,
    load_folded_feedback,
    load_placement_corrections,
    normalize_item_key,
    resolve_corrections,
)

NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)
T0 = NOW
T1 = NOW + timedelta(hours=1)
T2 = NOW + timedelta(hours=2)

HOME_TENANT = "personal"
HOME_CORPUS = "personal"


# --------------------------------------------------------------------------- #
# Pure fixture builders (no database)
# --------------------------------------------------------------------------- #


def _correction(
    *,
    item_text: str,
    bin_code: str,
    directive_kind: str,
    decided_at: datetime,
    seq: int = 0,
    decision_external_id: str | None = None,
    decision_kind: str | None = None,
    tenant_id: str = HOME_TENANT,
    corpus_id: str = HOME_CORPUS,
) -> PlacementCorrection:
    """Build one accept/exclude directive with a normalized item key."""
    external_id = decision_external_id or f"dec-{seq}-{directive_kind}-{bin_code}"
    kind = decision_kind or ("accept" if directive_kind == "accept" else "reject")
    return PlacementCorrection(
        decision_external_id=external_id,
        decision_id=external_id,
        seq=seq,
        decision_kind=kind,
        directive_kind=directive_kind,  # type: ignore[arg-type]
        bin_code=bin_code,
        item_text=item_text,
        item_match_key=normalize_item_key(item_text),
        decided_at=decided_at,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )


def _passport(
    bin_code: str,
    *,
    theme: str = "misc",
    accepts: tuple[str, ...] = (),
    excludes: tuple[str, ...] = (),
    provenance_refs: tuple[str, ...] = ("capture:base#metadata.bin_profile",),
) -> BinPassport:
    """Build a base passport with its default (unfolded) projector version."""
    return BinPassport(
        bin_code=bin_code,
        theme=theme,
        home_site="alameda-garage",
        current_site="alameda-garage",
        owner_phrase=None,
        accepts=accepts,
        excludes=excludes,
        examples=(),
        sibling_contents=(),
        physical_constraints=(),
        volume_profile=None,
        capacity_state="half",
        location_confidence=0.92,
        passport_confidence=0.86,
        provenance_refs=provenance_refs,
    )


def _pipeline(
    corrections: list[PlacementCorrection],
    passports: list[BinPassport],
) -> list[BinPassport]:
    """Run the pure resolve -> group -> fold pipeline over fixtures."""
    grouped = group_winners_by_identity(resolve_corrections(corrections))
    return fold_corrections(passports, grouped)


# --------------------------------------------------------------------------- #
# Stage 1 — key normalization (AC-KEY-01)
# --------------------------------------------------------------------------- #


def test_key_normalization_folds_case_and_separators() -> None:
    # Case is folded and every run of non-alphanumerics collapses to one space,
    # so differently-spelled owner phrasings share one match key.
    assert normalize_item_key("USB-C Cable") == "usb c cable"
    assert normalize_item_key("usb-c   cable") == "usb c cable"
    assert normalize_item_key("USB-C Cable") == normalize_item_key("usb-c   cable")
    assert normalize_item_key("  #10-32  Machine Screw ") == "10 32 machine screw"
    # An item that normalizes to empty carries no association.
    assert normalize_item_key("") == ""
    assert normalize_item_key("   ") == ""
    assert normalize_item_key("!!!") == ""


# --------------------------------------------------------------------------- #
# Stage 3 — association / collision / temporal (AC-ASSOC/COLLISION/TEMPORAL)
# --------------------------------------------------------------------------- #


def test_association_attaches_each_accept_to_its_bin() -> None:
    # Each accept winner attaches its item to its bin; exclude directives are
    # independent and retained; grouping is per bin identity.
    accept_a = _correction(
        item_text="USB-C cable",
        bin_code="ALA-014",
        directive_kind="accept",
        decided_at=T0,
        seq=1,
    )
    accept_b = _correction(
        item_text="cotton thread",
        bin_code="FAB-003",
        directive_kind="accept",
        decided_at=T1,
        seq=2,
    )
    exclude = _correction(
        item_text="battery pack",
        bin_code="ALA-014",
        directive_kind="exclude",
        decided_at=T2,
        seq=3,
        decision_kind="reject",
    )

    grouped = group_winners_by_identity(resolve_corrections([accept_a, accept_b, exclude]))

    assert set(grouped) == {"ALA-014", "FAB-003"}
    assert {(d.directive_kind, d.item_match_key) for d in grouped["ALA-014"]} == {
        ("accept", "usb c cable"),
        ("exclude", "battery pack"),
    }
    assert {(d.directive_kind, d.item_match_key) for d in grouped["FAB-003"]} == {
        ("accept", "cotton thread"),
    }


def test_collision_keeps_only_latest_accept_across_bins() -> None:
    # One item cannot be accepted into two bins at once: the latest-decided
    # association survives and the earlier one is dropped.
    earlier = _correction(
        item_text="USB-C cable",
        bin_code="ALA-014",
        directive_kind="accept",
        decided_at=T0,
        seq=1,
    )
    later = _correction(
        item_text="usb-c   cable",
        bin_code="ALA-022",
        directive_kind="accept",
        decided_at=T1,
        seq=2,
    )

    grouped = group_winners_by_identity(resolve_corrections([earlier, later]))

    assert set(grouped) == {"ALA-022"}
    assert "ALA-014" not in grouped
    assert grouped["ALA-022"][0].item_match_key == "usb c cable"

    # The guard is genuinely temporal: flip which decision is later and the
    # surviving bin flips with it.
    earlier_flip = _correction(
        item_text="USB-C cable",
        bin_code="ALA-014",
        directive_kind="accept",
        decided_at=T1,
        seq=2,
    )
    later_flip = _correction(
        item_text="usb-c   cable",
        bin_code="ALA-022",
        directive_kind="accept",
        decided_at=T0,
        seq=1,
    )
    grouped_flip = group_winners_by_identity(resolve_corrections([earlier_flip, later_flip]))
    assert set(grouped_flip) == {"ALA-014"}


def test_temporal_reversal_resolves_both_directions() -> None:
    # Corrections sharing an item and a target bin are a temporal stream where
    # the most recent decision wins — in both directions.
    accept_first = _correction(
        item_text="wood screw",
        bin_code="BIN-1",
        directive_kind="accept",
        decided_at=T0,
        seq=1,
    )
    reject_later = _correction(
        item_text="wood screw",
        bin_code="BIN-1",
        directive_kind="exclude",
        decided_at=T1,
        seq=2,
        decision_kind="reject",
    )
    # Later reject overrides earlier accept (input order should not matter).
    winners = resolve_corrections([reject_later, accept_first])
    assert [w.directive_kind for w in winners] == ["exclude"]

    reject_first = _correction(
        item_text="wood screw",
        bin_code="BIN-1",
        directive_kind="exclude",
        decided_at=T0,
        seq=1,
        decision_kind="reject",
    )
    accept_later = _correction(
        item_text="wood screw",
        bin_code="BIN-1",
        directive_kind="accept",
        decided_at=T1,
        seq=2,
    )
    # Later accept overrides earlier reject.
    winners = resolve_corrections([accept_later, reject_first])
    assert [w.directive_kind for w in winners] == ["accept"]


def test_two_distinct_items_toward_one_bin_both_fold() -> None:
    # AC-MULTI-FOLD-01 — review-152 major-fix regression guard.
    # Two *different* items corrected toward the *same* bin must both survive
    # grouping and both fold; neither collides the other out.
    usb = _correction(
        item_text="USB-C cable",
        bin_code="ALA-014",
        directive_kind="accept",
        decided_at=T0,
        seq=1,
        decision_external_id="dec-usb",
    )
    hdmi = _correction(
        item_text="HDMI adapter",
        bin_code="ALA-014",
        directive_kind="accept",
        decided_at=T1,
        seq=2,
        decision_external_id="dec-hdmi",
    )

    grouped = group_winners_by_identity(resolve_corrections([hdmi, usb]))

    assert set(grouped) == {"ALA-014"}
    assert len(grouped["ALA-014"]) == 2
    assert {d.item_text for d in grouped["ALA-014"]} == {"USB-C cable", "HDMI adapter"}

    base = _passport("ALA-014", accepts=("existing",))
    folded = fold_corrections([base], grouped)[0]

    assert folded.accepts[0] == "existing"
    assert set(folded.accepts[1:]) == {"USB-C cable", "HDMI adapter"}
    assert len(folded.accepts) == 3
    # One provenance ref per surviving directive — both distinct items recorded.
    added_refs = set(folded.provenance_refs) - set(base.provenance_refs)
    assert added_refs == {
        "placement-decision:dec-usb#accept",
        "placement-decision:dec-hdmi#accept",
    }


# --------------------------------------------------------------------------- #
# Stage 4 — fold semantics (AC-FOLD-ACCEPT/EXCLUDE, PROV, ADD, VERSION)
# --------------------------------------------------------------------------- #


def test_accept_directive_folds_into_passport_accepts() -> None:
    accept = _correction(
        item_text="machine screw",
        bin_code="BIN-1",
        directive_kind="accept",
        decided_at=T0,
        seq=1,
    )
    base = _passport("BIN-1", accepts=("washers",), excludes=("liquids",))

    folded = _pipeline([accept], [base])[0]

    assert folded.accepts == ("washers", "machine screw")
    assert folded.excludes == ("liquids",)  # excludes untouched by an accept
    assert folded.projector_version == BIN_PLACEMENT_FEEDBACK_VERSION


def test_exclude_directive_folds_into_passport_excludes() -> None:
    # A reject yields an exclude directive; an override's exclude arm folds the
    # same way. Both land in excludes, leaving accepts untouched.
    reject = _correction(
        item_text="liquid cleaner",
        bin_code="BIN-1",
        directive_kind="exclude",
        decided_at=T0,
        seq=1,
        decision_kind="reject",
    )
    override_exclude = _correction(
        item_text="loose battery",
        bin_code="BIN-1",
        directive_kind="exclude",
        decided_at=T1,
        seq=2,
        decision_kind="override",
    )
    base = _passport("BIN-1", accepts=("machine screws",), excludes=("dust",))

    folded = _pipeline([reject, override_exclude], [base])[0]

    assert folded.accepts == ("machine screws",)
    assert set(folded.excludes) == {"dust", "liquid cleaner", "loose battery"}
    assert folded.excludes[0] == "dust"  # existing value stays first
    assert folded.projector_version == BIN_PLACEMENT_FEEDBACK_VERSION


def test_provenance_adds_one_ref_per_surviving_directive() -> None:
    accept = _correction(
        item_text="machine screw",
        bin_code="BIN-1",
        directive_kind="accept",
        decided_at=T0,
        seq=1,
        decision_external_id="dec-a",
    )
    exclude = _correction(
        item_text="loose battery",
        bin_code="BIN-1",
        directive_kind="exclude",
        decided_at=T1,
        seq=2,
        decision_external_id="dec-b",
        decision_kind="reject",
    )
    base = _passport("BIN-1", provenance_refs=("capture:BIN-1#metadata.bin_profile",))

    folded = _pipeline([accept, exclude], [base])[0]

    # Exactly one ref per surviving directive, pointing back to the owner
    # decision and its directive arm; base refs preserved and still first.
    assert folded.provenance_refs[0] == "capture:BIN-1#metadata.bin_profile"
    added = folded.provenance_refs[len(base.provenance_refs) :]
    assert len(added) == 2
    assert set(added) == {
        "placement-decision:dec-a#accept",
        "placement-decision:dec-b#exclude",
    }


def test_fold_is_additive_and_preserves_existing_values() -> None:
    base = _passport(
        "BIN-1",
        accepts=("alpha", "beta"),
        excludes=("gamma",),
        provenance_refs=("ref-1", "ref-2"),
    )
    accept = _correction(
        item_text="delta",
        bin_code="BIN-1",
        directive_kind="accept",
        decided_at=T0,
        seq=1,
    )
    exclude = _correction(
        item_text="epsilon",
        bin_code="BIN-1",
        directive_kind="exclude",
        decided_at=T1,
        seq=2,
        decision_kind="reject",
    )

    folded = _pipeline([accept, exclude], [base])[0]

    # Nothing existing is removed or reordered — only appended.
    assert folded.accepts[:2] == ("alpha", "beta")
    assert folded.excludes[0] == "gamma"
    assert folded.provenance_refs[:2] == ("ref-1", "ref-2")
    assert set(base.accepts) <= set(folded.accepts)
    assert set(base.excludes) <= set(folded.excludes)
    assert set(base.provenance_refs) <= set(folded.provenance_refs)
    assert "delta" in folded.accepts
    assert "epsilon" in folded.excludes


def test_fold_dedupes_repeated_phrase_but_still_records_ref() -> None:
    # An accept for a phrase already present does not duplicate the phrase, but a
    # provenance ref is still added for the surviving directive.
    base = _passport("BIN-1", accepts=("USB-C cable",), provenance_refs=("ref-1",))
    accept = _correction(
        item_text="USB-C cable",
        bin_code="BIN-1",
        directive_kind="accept",
        decided_at=T0,
        seq=1,
        decision_external_id="dec-dup",
    )

    folded = _pipeline([accept], [base])[0]

    assert folded.accepts == ("USB-C cable",)
    assert folded.provenance_refs == ("ref-1", "placement-decision:dec-dup#accept")


def test_version_stamp_confined_to_folded_bins() -> None:
    folded_bin = _passport("BIN-1", accepts=("x",))
    untouched_bin = _passport("BIN-2", accepts=("y",))
    accept = _correction(
        item_text="z",
        bin_code="BIN-1",
        directive_kind="accept",
        decided_at=T0,
        seq=1,
    )

    grouped = group_winners_by_identity(resolve_corrections([accept]))
    result = fold_corrections([folded_bin, untouched_bin], grouped)
    by_code = {passport.bin_code: passport for passport in result}

    # Only the bin that actually folded carries the feedback stamp; the sibling
    # keeps its base projector version and is returned as the same object.
    assert by_code["BIN-1"].projector_version == BIN_PLACEMENT_FEEDBACK_VERSION
    assert by_code["BIN-2"].projector_version == BIN_PASSPORT_PROJECTOR_VERSION
    assert by_code["BIN-2"] is untouched_bin
    assert BIN_PLACEMENT_FEEDBACK_VERSION != BIN_PASSPORT_PROJECTOR_VERSION


def test_fold_pipeline_is_deterministic_under_reordering() -> None:
    corrections = [
        _correction(
            item_text="USB-C cable",
            bin_code="ALA-014",
            directive_kind="accept",
            decided_at=T0,
            seq=1,
            decision_external_id="dec-1",
        ),
        _correction(
            item_text="HDMI adapter",
            bin_code="ALA-014",
            directive_kind="accept",
            decided_at=T1,
            seq=2,
            decision_external_id="dec-2",
        ),
        _correction(
            item_text="USB-C cable",
            bin_code="ALA-014",
            directive_kind="exclude",
            decided_at=T2,
            seq=3,
            decision_external_id="dec-3",
            decision_kind="reject",
        ),
        _correction(
            item_text="cotton thread",
            bin_code="FAB-003",
            directive_kind="accept",
            decided_at=T1,
            seq=4,
            decision_external_id="dec-4",
        ),
    ]
    passports = [_passport("ALA-014", accepts=("power bricks",)), _passport("FAB-003")]

    def run(order: list[PlacementCorrection]) -> object:
        winners = resolve_corrections(order)
        grouped = group_winners_by_identity(winners)
        folded = fold_corrections(passports, grouped)
        return (
            [w.to_json() for w in winners],
            {code: [d.to_json() for d in items] for code, items in grouped.items()},
            [passport.to_json() for passport in folded],
        )

    forward = run(corrections)
    reversed_ = run(list(reversed(corrections)))
    shuffled = run([corrections[2], corrections[0], corrections[3], corrections[1]])

    assert forward == reversed_
    assert forward == shuffled
    # Repeated evaluation of identical input is byte-stable too.
    assert run(corrections) == forward


# --------------------------------------------------------------------------- #
# Stage 2 — loader scope + derivation (DB-backed)
# --------------------------------------------------------------------------- #


def _insert_routing_request(
    conn: psycopg.Connection,
    *,
    external_id: str,
    input_text: str,
    site: str = "alameda-garage",
    requested_at: datetime = NOW,
    tenant_id: str = HOME_TENANT,
    corpus_id: str = HOME_CORPUS,
) -> object:
    """Insert one immutable text-route receipt; return its id. Test-only."""
    route_sha = hashlib.sha256(f"{external_id}:{input_text}".encode()).hexdigest()
    row = conn.execute(
        """
        INSERT INTO bin_routing_requests (
            tenant_id, corpus_id, external_id, request_kind, input_text, site,
            requested_at, router_version, item_card_json, route_result_json,
            route_result_sha256
        )
        VALUES (%s, %s, %s, 'text', %s, %s, %s, 'bin-route.test.v1', %s, %s, %s)
        RETURNING id
        """,
        (
            tenant_id,
            corpus_id,
            external_id,
            input_text,
            site,
            requested_at,
            Jsonb({"text": input_text}),
            Jsonb({"recommended_bin_code": None}),
            route_sha,
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def _insert_decision(
    conn: psycopg.Connection,
    *,
    external_id: str,
    routing_request_id: object,
    decision_kind: str,
    decided_at: datetime,
    recommended_bin_code: str | None = None,
    selected_bin_code: str | None = None,
    tenant_id: str = HOME_TENANT,
    corpus_id: str = HOME_CORPUS,
) -> None:
    """Append one owner placement decision over a receipt. Test-only."""
    conn.execute(
        """
        INSERT INTO bin_placement_decisions (
            tenant_id, corpus_id, external_id, routing_request_id, decision_kind,
            recommended_bin_code, selected_bin_code, decided_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tenant_id,
            corpus_id,
            external_id,
            routing_request_id,
            decision_kind,
            recommended_bin_code,
            selected_bin_code,
            decided_at,
        ),
    )


def _insert_bin_capture(
    conn: psycopg.Connection,
    *,
    external_id: str,
    bin_code: str,
    contents_text: str,
    accepts: tuple[str, ...],
    site: str = "alameda-garage",
) -> None:
    """Insert one bin_capture so the bin is known and has a base passport."""
    metadata: dict[str, object] = {
        "kind": "bin_capture",
        "schema_version": "bin_capture.v1",
        "bin_code": bin_code,
        "site": site,
        "captured_at": NOW.isoformat(),
        "contents_text": contents_text,
        "bin_profile": {
            "theme": contents_text,
            "accepts": list(accepts),
            "capacity_state": "half",
        },
    }
    source_row = conn.execute(
        "INSERT INTO sources (source_kind, external_id, raw_payload) "
        "VALUES ('capture', %s, '{}') RETURNING id",
        (f"feedback-src-{external_id}",),
    ).fetchone()
    assert source_row is not None
    conn.execute(
        """
        INSERT INTO captures (
            source_id, source_kind, external_id, raw_payload,
            capture_type, content_text, observed_at
        )
        VALUES (%s, 'capture', %s, %s, 'observation', %s, %s)
        """,
        (
            source_row[0],
            external_id,
            Jsonb({"metadata": metadata}),
            f"bin {bin_code} @ {site} - {contents_text}",
            NOW,
        ),
    )


def _ledger_counts(conn: psycopg.Connection) -> tuple[int, int]:
    """Return (routing_requests, decisions) row counts for read-only checks."""
    row = conn.execute(
        "SELECT (SELECT count(*) FROM bin_routing_requests), "
        "(SELECT count(*) FROM bin_placement_decisions)"
    ).fetchone()
    assert row is not None
    return (int(row[0]), int(row[1]))


def test_load_placement_corrections_derives_from_receipt(conn: psycopg.Connection) -> None:
    request_id = _insert_routing_request(conn, external_id="req-1", input_text="USB-C Cable")
    _insert_decision(
        conn,
        external_id="dec-1",
        routing_request_id=request_id,
        decision_kind="accept",
        recommended_bin_code="ALA-014",
        selected_bin_code="ALA-014",
        decided_at=T0,
    )
    before = _ledger_counts(conn)

    corrections = load_placement_corrections(conn, now=NOW)

    # The loader is read-only — no rows written.
    assert _ledger_counts(conn) == before
    assert len(corrections) == 1
    correction = corrections[0]
    assert correction.directive_kind == "accept"
    assert correction.bin_code == "ALA-014"
    assert correction.item_text == "USB-C Cable"
    assert correction.item_match_key == "usb c cable"
    assert correction.decision_external_id == "dec-1"
    assert correction.tenant_id == HOME_TENANT
    assert correction.corpus_id == HOME_CORPUS
    assert correction.provenance_ref == "placement-decision:dec-1#accept"


def test_loader_derives_every_decision_kind(conn: psycopg.Connection) -> None:
    # One shared receipt; many decisions of every kind. Assert the exact set of
    # (decision, directive, bin) directives the loader derives.
    request_id = _insert_routing_request(conn, external_id="req-multi", input_text="widget")
    rows: list[tuple[str, str, str | None, str | None]] = [
        ("d-accept-sel", "accept", "REC-A", "SEL-A"),
        ("d-accept-rec", "accept", "REC-B", None),
        ("d-accept-empty", "accept", None, None),
        ("d-reject", "reject", "REC-R", None),
        ("d-override-both", "override", "REC-O", "SEL-O"),
        ("d-override-same", "override", "SEL-N", "SEL-N"),
        ("d-override-norec", "override", None, "SEL-M"),
        ("d-override-malformed", "override", "REC-X", None),
        ("d-create", "create_new_bin", None, "NEW-1"),
        ("d-create-rec", "create_new_bin", "REC-C", "NEW-2"),
        ("d-create-malformed", "create_new_bin", "REC-Y", None),
        ("d-split", "split", "REC-S", "SEL-S"),
        ("d-merge", "merge", "REC-M", "SEL-M2"),
        ("d-not-item", "not_an_item", "REC-N", "SEL-N2"),
    ]
    for index, (external_id, kind, recommended, selected) in enumerate(rows):
        _insert_decision(
            conn,
            external_id=external_id,
            routing_request_id=request_id,
            decision_kind=kind,
            recommended_bin_code=recommended,
            selected_bin_code=selected,
            decided_at=T0 + timedelta(minutes=index),
        )

    corrections = load_placement_corrections(conn)
    derived = {(c.decision_external_id, c.directive_kind, c.bin_code) for c in corrections}

    assert derived == {
        ("d-accept-sel", "accept", "SEL-A"),
        ("d-accept-rec", "accept", "REC-B"),
        ("d-reject", "exclude", "REC-R"),
        ("d-override-both", "accept", "SEL-O"),
        ("d-override-both", "exclude", "REC-O"),
        ("d-override-same", "accept", "SEL-N"),
        ("d-override-norec", "accept", "SEL-M"),
        ("d-create", "accept", "NEW-1"),
        ("d-create-rec", "accept", "NEW-2"),
    }
    # Kinds that name no directive (or a malformed target) yield nothing.
    silent = {
        "d-accept-empty",
        "d-override-malformed",
        "d-create-malformed",
        "d-split",
        "d-merge",
        "d-not-item",
    }
    assert silent.isdisjoint({c.decision_external_id for c in corrections})


def test_load_folded_feedback_reports_full_derivation(conn: psycopg.Connection) -> None:
    _insert_bin_capture(
        conn,
        external_id="cap-x",
        bin_code="BIN-X",
        contents_text="fasteners",
        accepts=("nuts",),
    )
    request_id = _insert_routing_request(conn, external_id="req-x", input_text="hex nut")
    _insert_decision(
        conn,
        external_id="dec-x",
        routing_request_id=request_id,
        decision_kind="accept",
        recommended_bin_code="BIN-X",
        selected_bin_code="BIN-X",
        decided_at=T0,
    )

    bundle = load_folded_feedback(conn, now=NOW)

    assert isinstance(bundle, FoldedFeedback)
    assert bundle.feedback_enabled is True
    assert bundle.feedback_version == BIN_PLACEMENT_FEEDBACK_VERSION
    assert {c.decision_external_id for c in bundle.corrections} == {"dec-x"}
    assert {w.decision_external_id for w in bundle.winners} == {"dec-x"}
    assert set(bundle.grouped) == {"BIN-X"}
    assert bundle.folded_bin_codes == ("BIN-X",)
    folded = {passport.bin_code: passport for passport in bundle.passports}
    assert "hex nut" in folded["BIN-X"].accepts
    # to_json is a stable serializable receipt.
    payload = bundle.to_json()
    assert payload["feedback_enabled"] is True
    assert payload["folded_bin_codes"] == ["BIN-X"]


def test_blank_scope_is_refused(conn: psycopg.Connection) -> None:
    # An under-specified scope predicate is exactly the cross-tenant/corpus leak
    # PD1/PD11 forbid, so it is refused rather than run blank.
    with pytest.raises(PlacementFeedbackError):
        load_placement_corrections(conn, tenant_id="")
    with pytest.raises(PlacementFeedbackError):
        load_placement_corrections(conn, corpus_id="   ")
    with pytest.raises(PlacementFeedbackError):
        load_folded_bin_passports(conn, tenant_id="")
    with pytest.raises(PlacementFeedbackError):
        load_folded_feedback(conn, corpus_id="")


def test_cross_scope_decision_is_inert(conn: psycopg.Connection) -> None:
    # AC-SCOPE-ISOLATION-01 — review-171 major-fix regression guard.
    # A same-bin_code decision recorded in another tenant OR another corpus must
    # never leak into this scope's fold.
    _insert_bin_capture(
        conn,
        external_id="cap-ala014",
        bin_code="ALA-014",
        contents_text="electronics cables",
        accepts=("USB-C cables",),
    )

    home_request = _insert_routing_request(conn, external_id="home-req", input_text="phone charger")
    _insert_decision(
        conn,
        external_id="home-dec",
        routing_request_id=home_request,
        decision_kind="accept",
        recommended_bin_code="ALA-014",
        selected_bin_code="ALA-014",
        decided_at=T0,
    )

    # Same bin_code, different tenant.
    tenant_request = _insert_routing_request(
        conn,
        external_id="rival-tenant-req",
        input_text="tenant leak",
        tenant_id="rival-tenant",
    )
    _insert_decision(
        conn,
        external_id="rival-tenant-dec",
        routing_request_id=tenant_request,
        decision_kind="accept",
        recommended_bin_code="ALA-014",
        selected_bin_code="ALA-014",
        decided_at=T1,
        tenant_id="rival-tenant",
    )

    # Same bin_code, different corpus.
    corpus_request = _insert_routing_request(
        conn,
        external_id="rival-corpus-req",
        input_text="corpus leak",
        corpus_id="rival-corpus",
    )
    _insert_decision(
        conn,
        external_id="rival-corpus-dec",
        routing_request_id=corpus_request,
        decision_kind="accept",
        recommended_bin_code="ALA-014",
        selected_bin_code="ALA-014",
        decided_at=T2,
        corpus_id="rival-corpus",
    )

    # The home scope sees only its own correction.
    home_corrections = load_placement_corrections(conn)
    assert {c.decision_external_id for c in home_corrections} == {"home-dec"}

    folded = load_folded_bin_passports(conn)
    ala = next(passport for passport in folded if passport.bin_code == "ALA-014")
    assert "phone charger" in ala.accepts
    assert "tenant leak" not in ala.accepts
    assert "corpus leak" not in ala.accepts
    assert "placement-decision:home-dec#accept" in ala.provenance_refs
    assert all("rival" not in ref for ref in ala.provenance_refs)

    # Each rival scope independently sees only its own correction.
    tenant_corrections = load_placement_corrections(conn, tenant_id="rival-tenant")
    assert {c.decision_external_id for c in tenant_corrections} == {"rival-tenant-dec"}
    corpus_corrections = load_placement_corrections(conn, corpus_id="rival-corpus")
    assert {c.decision_external_id for c in corpus_corrections} == {"rival-corpus-dec"}


# --------------------------------------------------------------------------- #
# Stage 4 — load-level views (C11a byte-identity, kill switch)
# --------------------------------------------------------------------------- #


def test_unfolded_sibling_passport_is_byte_identical(conn: psycopg.Connection) -> None:
    # AC-C11A-PASSPORT-01: a bin that received no correction is returned
    # byte-identical to its base passport, even beside a folded sibling.
    _insert_bin_capture(
        conn,
        external_id="cap-a",
        bin_code="BIN-A",
        contents_text="fasteners",
        accepts=("machine screws",),
    )
    _insert_bin_capture(
        conn,
        external_id="cap-b",
        bin_code="BIN-B",
        contents_text="adhesives",
        accepts=("epoxy",),
    )
    request_id = _insert_routing_request(conn, external_id="req-a", input_text="wood screw")
    _insert_decision(
        conn,
        external_id="dec-a",
        routing_request_id=request_id,
        decision_kind="accept",
        recommended_bin_code="BIN-A",
        selected_bin_code="BIN-A",
        decided_at=T0,
    )

    base = {passport.bin_code: passport for passport in load_bin_passports(conn)}
    folded = {passport.bin_code: passport for passport in load_folded_bin_passports(conn)}

    # Untouched sibling: byte-identical to its base passport and same version.
    assert folded["BIN-B"].to_json() == base["BIN-B"].to_json()
    assert folded["BIN-B"] == base["BIN-B"]
    assert folded["BIN-B"].projector_version == BIN_PASSPORT_PROJECTOR_VERSION

    # Folded bin: differs, carries the accept and the feedback version.
    assert folded["BIN-A"].to_json() != base["BIN-A"].to_json()
    assert "wood screw" in folded["BIN-A"].accepts
    assert folded["BIN-A"].projector_version == BIN_PLACEMENT_FEEDBACK_VERSION
    assert base["BIN-A"].projector_version == BIN_PASSPORT_PROJECTOR_VERSION


@pytest.mark.parametrize("kill_value", ["0", "false", "no", "off", ""])
def test_load_level_kill_switch_returns_base_passports(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    kill_value: str,
) -> None:
    # AC-KILL-LOAD-01: with the kill switch engaged the fold is skipped and the
    # base passports are returned unchanged.
    _insert_bin_capture(
        conn,
        external_id="cap-k",
        bin_code="KILL-1",
        contents_text="fasteners",
        accepts=("screws",),
    )
    request_id = _insert_routing_request(conn, external_id="req-k", input_text="lag bolt")
    _insert_decision(
        conn,
        external_id="dec-k",
        routing_request_id=request_id,
        decision_kind="accept",
        recommended_bin_code="KILL-1",
        selected_bin_code="KILL-1",
        decided_at=T0,
    )

    base = {passport.bin_code: passport for passport in load_bin_passports(conn)}

    # Enabled (switch absent) folds the correction — establishes the baseline.
    monkeypatch.delenv(PLACEMENT_FEEDBACK_ENABLED_ENV, raising=False)
    enabled = {p.bin_code: p for p in load_folded_bin_passports(conn)}
    assert "lag bolt" in enabled["KILL-1"].accepts

    # Engaged: base passports returned unchanged, no fold, no feedback version.
    monkeypatch.setenv(PLACEMENT_FEEDBACK_ENABLED_ENV, kill_value)
    disabled = {p.bin_code: p for p in load_folded_bin_passports(conn)}
    assert disabled["KILL-1"].to_json() == base["KILL-1"].to_json()
    assert "lag bolt" not in disabled["KILL-1"].accepts
    assert disabled["KILL-1"].projector_version == BIN_PASSPORT_PROJECTOR_VERSION

    bundle = load_folded_feedback(conn)
    assert bundle.feedback_enabled is False
    assert bundle.corrections == ()
    assert bundle.winners == ()
    assert bundle.grouped == {}
    assert bundle.folded_bin_codes == ()
