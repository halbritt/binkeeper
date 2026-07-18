"""RFC 0093 P0 — tests for pure bin volume metadata parsing."""

from __future__ import annotations

import pytest

from binkeeper.bin_volume import normalize_volume_metadata, parse_volume_profile


def test_parses_gallon_tote_label() -> None:
    profile = parse_volume_profile("27 gal tote")
    assert profile.volume_label == "27 gal tote"
    assert profile.volume_value == 27
    assert profile.volume_unit == "gal"
    assert profile.canonical_volume_liters == pytest.approx(102.206118168)
    assert profile.form_factor == "lidded_tote"
    assert profile.capacity_state == "unknown"


def test_parses_quart_open_tote_and_half_capacity() -> None:
    profile = parse_volume_profile("12 qt open tote half full")
    assert profile.volume_value == 12
    assert profile.volume_unit == "qt"
    assert profile.canonical_volume_liters == pytest.approx(11.356235352)
    assert profile.form_factor == "open_tote"
    assert profile.capacity_state == "half"


def test_parses_liters_and_explicit_profile_fields() -> None:
    profile = normalize_volume_metadata(
        {
            "bin_profile": {
                "volume_label": "64 L drawer",
                "volume_value": "64",
                "volume_unit": "liters",
                "form_factor": "drawer",
                "capacity_state": "Full",
            }
        }
    )
    assert profile.volume_label == "64 L drawer"
    assert profile.volume_value == 64
    assert profile.volume_unit == "l"
    assert profile.canonical_volume_liters == pytest.approx(64)
    assert profile.form_factor == "drawer"
    assert profile.capacity_state == "full"


def test_parses_milliliters_and_bag_form_factor() -> None:
    profile = parse_volume_profile("500 ml parts bag sparse")
    assert profile.volume_value == 500
    assert profile.volume_unit == "ml"
    assert profile.canonical_volume_liters == pytest.approx(0.5)
    assert profile.form_factor == "bag"
    assert profile.capacity_state == "sparse"


def test_parses_cubic_feet_crate() -> None:
    profile = parse_volume_profile("1.5 cu ft crate")
    assert profile.volume_value == 1.5
    assert profile.volume_unit == "cu_ft"
    assert profile.canonical_volume_liters == pytest.approx(42.475269888)
    assert profile.form_factor == "crate"


def test_preserves_unparseable_label_without_guessing_volume() -> None:
    profile = parse_volume_profile("shoebox of adapters")
    assert profile.volume_label == "shoebox of adapters"
    assert profile.volume_value is None
    assert profile.volume_unit == "unknown"
    assert profile.canonical_volume_liters is None
    assert profile.form_factor == "box"


def test_explicit_volume_without_label_normalizes_aliases() -> None:
    profile = parse_volume_profile(
        volume_value="5",
        volume_unit="gallons",
        form_factor="bucket",
        capacity_state="over full",
    )
    assert profile.volume_label is None
    assert profile.volume_value == 5
    assert profile.volume_unit == "gal"
    assert profile.canonical_volume_liters == pytest.approx(18.92705892)
    assert profile.form_factor == "bucket"
    assert profile.capacity_state == "overfull"


def test_capacity_state_uses_conservative_tighter_state_when_label_mentions_two() -> None:
    profile = parse_volume_profile("27 gal tote half/tight")
    assert profile.capacity_state == "tight"


def test_invalid_or_empty_metadata_degrades_to_unknowns() -> None:
    profile = normalize_volume_metadata(
        {
            "volume_label": "large clear bin",
            "volume_value": -1,
            "volume_unit": "bananas",
            "form_factor": "not-a-shape",
            "capacity_state": "not sure",
        }
    )
    assert profile.volume_label == "large clear bin"
    assert profile.volume_value is None
    assert profile.volume_unit == "unknown"
    assert profile.canonical_volume_liters is None
    assert profile.form_factor == "unknown"
    assert profile.capacity_state == "unknown"


def test_fractional_volume_label() -> None:
    profile = parse_volume_profile("1 1/2 qt box")
    assert profile.volume_value == pytest.approx(1.5)
    assert profile.volume_unit == "qt"
    assert profile.canonical_volume_liters == pytest.approx(1.419529419)
    assert profile.form_factor == "box"
