"""RFC 0093 P0 tests for read-only bin passports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from binkeeper.bin_inventory import BinBelief, BinLocation, record_event
from binkeeper.bin_passport import (
    BinCaptureEvidence,
    bin_passport,
    build_volume_profile,
    load_known_bin_codes,
    parse_volume_label,
    project_bin_passport,
)

NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=UTC)


def _capture(
    external_id: str,
    *,
    bin_code: str = "ALA-014",
    site: str = "alameda-garage",
    contents_text: str | None = "USB-C cables, HDMI adapters",
    profile: dict[str, object] | None = None,
    profile_snapshot: bool = False,
    captured_at: datetime = NOW,
) -> BinCaptureEvidence:
    metadata: dict[str, object] = {
        "kind": "bin_capture",
        "bin_code": bin_code,
        "site": site,
        "captured_at": captured_at.isoformat(),
    }
    if contents_text is not None:
        metadata["contents_text"] = contents_text
    if profile is not None:
        metadata["bin_profile"] = profile
    if profile_snapshot:
        metadata["profile_mode"] = "snapshot"
        metadata["profile_fields"] = ["theme", "home_site", "contents_text"]
    return BinCaptureEvidence(
        external_id=external_id,
        bin_code=bin_code,
        site=site,
        contents_text=contents_text,
        metadata=metadata,
        captured_at=captured_at,
    )


def _belief(bin_code: str, location: BinLocation, confidence: float) -> BinBelief:
    return BinBelief(
        bin_code=bin_code,
        location=location,
        confidence=confidence,
        half_life_days=120.0,
        floor=0.5,
        abstained=False,
        age_days=1.0,
    )


def test_project_passport_uses_latest_profile_and_move_belief() -> None:
    old = _capture(
        "cap-old",
        contents_text="mixed cable overflow",
        profile={"theme": "misc cables", "accepts": ["legacy cables"]},
        captured_at=NOW - timedelta(days=2),
    )
    new = _capture(
        "cap-new",
        contents_text="USB-C cables, HDMI adapters, power bricks",
        profile={
            "theme": "electronics cables",
            "home_site": "alameda-garage",
            "owner_phrase": "cords and adapters I would look for in the garage",
            "accepts": ["USB-C cables", "HDMI adapters"],
            "excludes": ["batteries", "liquids"],
            "examples": ["USB-C cable"],
            "physical_constraints": ["small clean electronics only"],
            "volume_label": "27 gal tote",
            "form_factor": "lidded_tote",
            "capacity_state": "half",
            "confidence": 0.9,
        },
        captured_at=NOW,
    )
    location = BinLocation(
        bin_code="ALA-014",
        status="at_site",
        site="alameda-storage",
        trip_id=None,
        last_event_seq=42,
        last_event_at=NOW,
    )

    passport = project_bin_passport(
        "ALA-014",
        [new, old],
        belief=_belief("ALA-014", location, confidence=0.82),
    )

    assert passport.theme == "electronics cables"
    assert passport.home_site == "alameda-garage"
    assert passport.current_site == "alameda-storage"
    assert passport.location_confidence == pytest.approx(0.82)
    assert passport.capacity_state == "half"
    assert passport.accepts == ("legacy cables", "USB-C cables", "HDMI adapters")
    assert passport.sibling_contents == (
        "mixed cable overflow",
        "USB-C cables, HDMI adapters, power bricks",
    )
    assert passport.volume_profile is not None
    assert passport.volume_profile.volume_label == "27 gal tote"
    assert passport.volume_profile.volume_unit == "gal"
    assert passport.volume_profile.canonical_volume_liters == pytest.approx(102.2061)
    assert "bin_trip_events:seq:42" in passport.provenance_refs
    assert "capture:cap-new#metadata.bin_profile.volume_label" in passport.provenance_refs


def test_profile_snapshot_replaces_current_theme_home_and_contents() -> None:
    original = _capture(
        "cap-original",
        contents_text="old paint rollers and drop cloths",
        profile={"theme": "Painting", "home_site": "alameda-garage"},
        captured_at=NOW - timedelta(days=1),
    )
    updated = _capture(
        "cap-updated",
        contents_text="cordless drills and impact drivers",
        profile={"theme": "Power tools", "home_site": "oakland-wood-west"},
        profile_snapshot=True,
        captured_at=NOW,
    )

    passport = project_bin_passport("ALA-014", [original, updated])

    assert passport.theme == "Power tools"
    assert passport.home_site == "oakland-wood-west"
    assert passport.sibling_contents == ("cordless drills and impact drivers",)


def test_profile_snapshot_can_explicitly_clear_current_profile_fields() -> None:
    original = _capture(
        "cap-original",
        contents_text="paint rollers",
        profile={"theme": "Painting", "home_site": "alameda-garage"},
        captured_at=NOW - timedelta(days=1),
    )
    cleared = _capture(
        "cap-cleared",
        contents_text="",
        profile={"theme": "", "home_site": ""},
        profile_snapshot=True,
        captured_at=NOW,
    )

    passport = project_bin_passport("ALA-014", [original, cleared])

    assert passport.theme is None
    assert passport.home_site is None
    assert passport.sibling_contents == ()


def test_managed_snapshot_preserves_profile_fields_outside_its_declared_scope() -> None:
    original = _capture(
        "cap-original",
        profile={
            "theme": "Painting",
            "home_site": "alameda-garage",
            "owner_phrase": "shop painting supplies",
            "capacity_state": "half",
        },
        captured_at=NOW - timedelta(days=1),
    )
    updated = _capture(
        "cap-updated",
        profile={"theme": "Power tools", "home_site": "oakland-wood-west"},
        profile_snapshot=True,
        captured_at=NOW,
    )

    passport = project_bin_passport("ALA-014", [original, updated])

    assert passport.theme == "Power tools"
    assert passport.owner_phrase == "shop painting supplies"
    assert passport.capacity_state == "half"


def test_photo_only_capture_does_not_erase_capture_backed_current_site() -> None:
    original = _capture(
        "cap-original",
        site="alameda-storage",
        captured_at=NOW - timedelta(days=1),
    )
    photo = BinCaptureEvidence(
        external_id="cap-photo",
        bin_code="ALA-014",
        site=None,
        contents_text=None,
        metadata={
            "kind": "bin_capture",
            "bin_code": "ALA-014",
            "captured_at": NOW.isoformat(),
            "contents_text": "",
            "photo": {"sha256": "a" * 64},
        },
        captured_at=NOW,
        content_text="bin ALA-014 photo added",
    )

    passport = project_bin_passport("ALA-014", [original, photo])

    assert passport.current_site == "alameda-storage"


def test_volume_profile_parses_label_and_keeps_capacity_separate() -> None:
    profile = {"volume_label": "12 qt clear tote", "capacity_state": "tight"}

    volume = build_volume_profile(profile, source_ref="capture:cap#metadata.bin_profile")

    assert volume is not None
    assert volume.volume_value == pytest.approx(12.0)
    assert volume.volume_unit == "qt"
    assert volume.canonical_volume_liters == pytest.approx(11.356235352)
    assert "capacity_state" not in volume.to_json()

    passport = project_bin_passport(
        "ALA-015",
        [_capture("cap", bin_code="ALA-015", profile=profile)],
    )
    assert passport.capacity_state == "tight"
    assert passport.volume_profile is not None


def test_unknown_volume_label_preserves_raw_label_without_false_normalization() -> None:
    assert parse_volume_label("shoebox") is None

    profile = {"volume_label": "shoebox", "capacity_state": "jammed"}
    passport = project_bin_passport(
        "ALA-016",
        [_capture("cap", bin_code="ALA-016", profile=profile)],
    )

    assert passport.capacity_state == "unknown"
    assert passport.volume_profile is not None
    assert passport.volume_profile.volume_label == "shoebox"
    assert passport.volume_profile.canonical_volume_liters is None


def _insert_bin_capture(
    conn: psycopg.Connection,
    *,
    external_id: str,
    bin_code: str,
    site: str,
    contents_text: str,
    profile: dict[str, object] | None = None,
) -> None:
    metadata: dict[str, object] = {
        "kind": "bin_capture",
        "schema_version": "bin_capture.v1",
        "bin_code": bin_code,
        "site": site,
        "captured_at": NOW.isoformat(),
        "contents_text": contents_text,
    }
    if profile is not None:
        metadata["bin_profile"] = profile
    source_row = conn.execute(
        "INSERT INTO sources (source_kind, external_id, raw_payload) "
        "VALUES ('capture', %s, '{}') RETURNING id",
        (f"passport-src-{external_id}",),
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


def test_bin_passport_loads_existing_evidence_without_writes(conn: psycopg.Connection) -> None:
    _insert_bin_capture(
        conn,
        external_id="db-cap-1",
        bin_code="DB-001",
        site="alameda-garage",
        contents_text="TIG cups and gas lenses",
        profile={
            "theme": "TIG consumables",
            "volume_value": 5,
            "volume_unit": "gal",
            "capacity_state": "sparse",
        },
    )
    record_event(
        conn,
        event_kind="place",
        bin_code="DB-001",
        site="alameda-garage",
        occurred_at=NOW,
        idempotency_key="passport-place-db-001",
    )
    before = conn.execute(
        "SELECT (SELECT count(*) FROM captures), (SELECT count(*) FROM bin_trip_events)"
    ).fetchone()
    assert before is not None

    passport = bin_passport(conn, "DB-001", now=NOW)

    after = conn.execute(
        "SELECT (SELECT count(*) FROM captures), (SELECT count(*) FROM bin_trip_events)"
    ).fetchone()
    assert after == before
    assert passport.theme == "TIG consumables"
    assert passport.current_site == "alameda-garage"
    assert passport.capacity_state == "sparse"
    assert passport.volume_profile is not None
    assert passport.volume_profile.canonical_volume_liters == pytest.approx(18.92705892)
    assert "DB-001" in load_known_bin_codes(conn)
