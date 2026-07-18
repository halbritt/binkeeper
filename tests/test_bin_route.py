"""RFC 0093 P1 tests for text-only bin routing."""

from __future__ import annotations

import pytest

from binkeeper.bin_passport import BinPassport
from binkeeper.bin_route import route_text_item


def _passport(
    bin_code: str,
    *,
    theme: str,
    current_site: str = "alameda-garage",
    owner_phrase: str | None = None,
    accepts: tuple[str, ...] = (),
    excludes: tuple[str, ...] = (),
    examples: tuple[str, ...] = (),
    sibling_contents: tuple[str, ...] = (),
    capacity_state: str = "half",
    passport_confidence: float = 0.86,
    location_confidence: float = 0.92,
) -> BinPassport:
    return BinPassport(
        bin_code=bin_code,
        theme=theme,
        home_site=current_site,
        current_site=current_site,
        owner_phrase=owner_phrase,
        accepts=accepts,
        excludes=excludes,
        examples=examples,
        sibling_contents=sibling_contents,
        physical_constraints=(),
        volume_profile=None,
        capacity_state=capacity_state,  # type: ignore[arg-type]
        location_confidence=location_confidence,
        passport_confidence=passport_confidence,
        provenance_refs=(f"capture:{bin_code}#metadata.bin_profile",),
    )


def test_text_router_recommends_obvious_matching_bin() -> None:
    electronics = _passport(
        "ALA-014",
        theme="electronics cables",
        owner_phrase="cords and adapters I would look for in the garage",
        accepts=("USB-C cables", "HDMI adapters"),
        excludes=("batteries", "liquids"),
        examples=("USB-C cable",),
        sibling_contents=("USB-C cables, HDMI adapters, power bricks",),
    )
    fabric = _passport(
        "FAB-003",
        theme="fabric and sewing notions",
        accepts=("thread", "fabric scraps"),
        sibling_contents=("cotton fabric, thread, zipper pulls",),
    )

    route = route_text_item(
        text="USB-C cable",
        site="alameda-garage",
        passports=[fabric, electronics],
    )

    assert route.recommended_bin_code == "ALA-014"
    assert route.abstain_flags == ()
    assert route.top_candidate is not None
    assert route.top_candidate.bin_code == "ALA-014"
    assert route.top_candidate.score_parts.semantic_fit == pytest.approx(1.0)
    assert route.top_candidate.score_parts.sibling_fit > 0.5
    assert [candidate.bin_code for candidate in route.alternatives] == ["FAB-003"]
    assert route.to_json()["recommended_bin_code"] == "ALA-014"


def test_text_router_abstains_on_close_tie_with_sorted_candidates() -> None:
    usb_cables = _passport(
        "ALA-014",
        theme="electronics cables",
        accepts=("USB-C cables", "data cables"),
        sibling_contents=("USB-C cables, HDMI adapters",),
    )
    charging_cables = _passport(
        "ALA-022",
        theme="charging cables",
        accepts=("charging cables", "USB cables"),
        sibling_contents=("phone chargers, USB cables",),
    )

    route = route_text_item(
        text="USB cable",
        site="alameda-garage",
        passports=[charging_cables, usb_cables],
        tie_margin=0.2,
    )

    assert route.recommended_bin_code is None
    assert route.abstain_flags == ("close_tie",)
    assert route.top_candidate is not None
    assert route.candidates[0].total_score >= route.candidates[1].total_score
    assert {candidate.bin_code for candidate in route.candidates[:2]} == {"ALA-014", "ALA-022"}


def test_text_router_abstains_when_only_matching_bin_is_full() -> None:
    full_electronics = _passport(
        "ALA-014",
        theme="electronics cables",
        accepts=("USB-C cables", "HDMI adapters"),
        sibling_contents=("USB-C cables, HDMI adapters",),
        capacity_state="full",
    )
    weak_local = _passport(
        "ALA-099",
        theme="paint supplies",
        accepts=("brushes", "rollers"),
        sibling_contents=("paint brushes, rollers",),
    )

    route = route_text_item(
        text="USB-C cable",
        site="alameda-garage",
        passports=[full_electronics, weak_local],
    )

    assert route.recommended_bin_code is None
    assert "full_bin" in route.abstain_flags
    full_candidate = next(
        candidate for candidate in route.candidates if candidate.bin_code == "ALA-014"
    )
    assert full_candidate.hard_filters == ("full_bin",)


def test_text_router_recommends_viable_match_despite_rejected_matching_alternative() -> None:
    full_electronics = _passport(
        "ALA-014",
        theme="electronics cables",
        accepts=("USB-C cables", "HDMI adapters"),
        sibling_contents=("USB-C cables, HDMI adapters",),
        capacity_state="full",
    )
    available_electronics = _passport(
        "ALA-022",
        theme="electronics cables",
        accepts=("USB-C cables", "data cables"),
        sibling_contents=("USB-C cables, data cables",),
    )

    route = route_text_item(
        text="USB-C cable",
        site="alameda-garage",
        passports=[full_electronics, available_electronics],
    )

    assert route.recommended_bin_code == "ALA-022"
    assert route.abstain_flags == ()
    rejected_candidate = next(
        candidate for candidate in route.candidates if candidate.bin_code == "ALA-014"
    )
    assert rejected_candidate.hard_filters == ("full_bin",)


def test_text_router_rejects_wrong_site_match_without_cross_site_route() -> None:
    remote_electronics = _passport(
        "ALA-014",
        theme="electronics cables",
        current_site="alameda-storage",
        accepts=("USB-C cables", "HDMI adapters"),
        sibling_contents=("USB-C cables, HDMI adapters",),
    )
    weak_local = _passport(
        "ALA-099",
        theme="paint supplies",
        accepts=("brushes", "rollers"),
        sibling_contents=("paint brushes, rollers",),
    )

    route = route_text_item(
        text="USB-C cable",
        site="alameda-garage",
        passports=[remote_electronics, weak_local],
    )

    assert route.recommended_bin_code is None
    assert "wrong_site" in route.abstain_flags
    remote_candidate = next(
        candidate for candidate in route.candidates if candidate.bin_code == "ALA-014"
    )
    assert remote_candidate.hard_filters == ("wrong_site",)


def test_text_router_rejects_matching_physical_constraint() -> None:
    clean_supplies = _passport(
        "ALA-033",
        theme="clean shop supplies",
        accepts=("shop supplies", "adhesives"),
        sibling_contents=("glue sticks, tape, clean rags",),
    )
    constrained = BinPassport(
        bin_code="ALA-044",
        theme="electronics cables",
        home_site="alameda-garage",
        current_site="alameda-garage",
        owner_phrase=None,
        accepts=("USB-C cables", "electronics supplies"),
        excludes=(),
        examples=(),
        sibling_contents=("USB-C cables, HDMI adapters",),
        physical_constraints=("no liquids",),
        volume_profile=None,
        capacity_state="half",
        location_confidence=0.92,
        passport_confidence=0.86,
        provenance_refs=("capture:ALA-044#metadata.bin_profile",),
    )

    route = route_text_item(
        text="liquid electronics cleaner",
        site="alameda-garage",
        passports=[clean_supplies, constrained],
    )

    assert route.recommended_bin_code is None
    assert "physical_constraint" in route.abstain_flags
    constrained_candidate = next(
        candidate for candidate in route.candidates if candidate.bin_code == "ALA-044"
    )
    assert constrained_candidate.hard_filters == ("physical_constraint",)
    assert constrained_candidate.score_parts.physical_fit == 0.0


def test_text_router_abstains_on_low_location_confidence() -> None:
    stale_electronics = _passport(
        "ALA-014",
        theme="electronics cables",
        accepts=("USB-C cables", "HDMI adapters"),
        sibling_contents=("USB-C cables, HDMI adapters",),
        location_confidence=0.3,
    )

    route = route_text_item(
        text="USB-C cable",
        site="alameda-garage",
        passports=[stale_electronics],
    )

    assert route.recommended_bin_code is None
    assert route.abstain_flags == ("no_viable_candidate", "location_abstained")
    assert route.candidates[0].hard_filters == ("location_abstained",)


def test_text_router_abstains_when_no_passport_accepts_unknown_item() -> None:
    electronics = _passport(
        "ALA-014",
        theme="electronics cables",
        accepts=("USB-C cables", "HDMI adapters"),
        sibling_contents=("USB-C cables, HDMI adapters",),
    )
    fabric = _passport(
        "FAB-003",
        theme="fabric and sewing notions",
        accepts=("thread", "fabric scraps"),
        sibling_contents=("cotton fabric, thread, zipper pulls",),
    )

    route = route_text_item(
        text="unlabeled ceramic mystery puck",
        site="alameda-garage",
        passports=[electronics, fabric],
    )

    assert route.recommended_bin_code is None
    assert route.abstain_flags == ("top_score_below_floor", "no_accepting_passport")
    assert route.top_candidate is not None
    assert route.top_candidate.score_parts.text_fit == 0.0


def test_text_router_ignores_single_letter_label_fragments() -> None:
    electronics = _passport(
        "ALA-014",
        theme="electronics cables",
        accepts=("USB-C cables", "HDMI adapters"),
        sibling_contents=("USB-C cables, HDMI adapters",),
    )

    route = route_text_item(
        text="C clamp",
        site="alameda-garage",
        passports=[electronics],
    )

    assert route.recommended_bin_code is None
    assert route.abstain_flags == ("top_score_below_floor", "no_accepting_passport")
    assert route.top_candidate is not None
    assert route.top_candidate.score_parts.text_fit == 0.0
