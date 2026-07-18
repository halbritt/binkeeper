"""RFC 0088 T3b — tests for the photo-GPS harvester (src/binkeeper/bin_harvest.py).

Pure geofence resolution (single / ambiguous-abstain / no-fix / too-loose) + the
GPS-accuracy → strength curve + sites loading run without a database; the backfill
worker, idempotency, ambiguity abstain, and the gated contradiction emission run
against the migrated test DB via the ``conn`` fixture.
"""

from __future__ import annotations

import json
from datetime import datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

from binkeeper.bin_harvest import (
    SiteAnchor,
    gps_strength,
    harvest_photo_gps,
    load_sites,
    resolve_site,
)
from binkeeper.bin_inventory import bin_where, load_bin_observations, record_event

# Two well-separated sites (~1.1 km apart) — a fix at either centre is unambiguous.
SEPARATED = {
    "garage": SiteAnchor(lat=37.0, lon=-122.0, radius_m=100.0),
    "storage": SiteAnchor(lat=37.01, lon=-122.0, radius_m=100.0),
}
# Two overlapping sites (~89 m apart, 200 m radii) — the midpoint is inside both.
OVERLAPPING = {
    "garage": SiteAnchor(lat=37.0, lon=-122.0, radius_m=200.0),
    "storage": SiteAnchor(lat=37.0, lon=-122.001, radius_m=200.0),
}


# --- pure: resolve_site -----------------------------------------------------


def test_resolve_site_single_unambiguous():
    res = resolve_site(37.0, -122.0, SEPARATED, accuracy_m=8.0)
    assert (res.site, res.ambiguous, res.too_loose) == ("garage", False, False)
    assert res.resolved is True


def test_resolve_site_ambiguous_abstains():
    res = resolve_site(37.0, -122.0005, OVERLAPPING, accuracy_m=8.0)
    assert res.site is None
    assert res.ambiguous is True
    assert set(res.candidates) == {"garage", "storage"}


def test_resolve_site_no_fix_within_any_geofence():
    res = resolve_site(38.0, -122.0, SEPARATED, accuracy_m=8.0)
    assert res.site is None
    assert res.ambiguous is False
    assert res.candidates == ()


def test_resolve_site_too_loose_fix_abstains():
    res = resolve_site(37.0, -122.0, SEPARATED, accuracy_m=500.0)
    assert res.site is None
    assert res.too_loose is True


def test_resolve_site_ignores_unsurveyed_anchors():
    sites = {"garage": SiteAnchor(lat=None, lon=None, radius_m=100.0)}
    assert resolve_site(37.0, -122.0, sites).site is None


# --- pure: gps_strength -----------------------------------------------------


def test_gps_strength_curve():
    assert gps_strength(5.0) == 1.0  # tight fix
    assert gps_strength(None) == pytest.approx(0.7)  # unknown accuracy
    assert gps_strength(200.0) == 0.0  # beyond max
    mid = gps_strength(57.5)  # halfway between good (15) and max (100)
    assert 0.4 < mid < 0.6


# --- pure: load_sites -------------------------------------------------------


def test_load_sites_ignores_underscore_keys_and_nulls(tmp_path):
    path = tmp_path / "sites.json"
    path.write_text(
        json.dumps(
            {
                "_README": "ignore me",
                "garage": {"lat": 37.0, "lon": -122.0, "radius_m": 120},
                "storage": {"lat": None, "lon": None, "radius_m": 120},
            }
        )
    )
    sites = load_sites(path)
    assert set(sites) == {"garage", "storage"}
    assert sites["garage"].lat == 37.0
    assert sites["storage"].lat is None  # unsurveyed, but loaded


def test_load_sites_missing_file_is_empty():
    assert load_sites("/nonexistent/sites.json") == {}


# --- IO: the harvest worker against the migrated test DB --------------------


def _insert_bin_capture(
    conn: psycopg.Connection,
    *,
    external_id: str,
    bin_code: str,
    lat: float | None = None,
    lon: float | None = None,
    accuracy_m: float | None = 8.0,
    captured_at: datetime | None = None,
) -> None:
    """Insert one bin_capture capture (the T1 metadata contract) for the harvester.

    Omitting ``lat``/``lon`` simulates a capture with no GPS fix (a valid evidence
    record the instant it lands; the harvester just skips it).
    """
    meta: dict[str, object] = {"kind": "bin_capture", "bin_code": bin_code}
    if lat is not None and lon is not None:
        gps: dict[str, object] = {"lat": lat, "lon": lon}
        if accuracy_m is not None:
            gps["accuracy_m"] = accuracy_m
        meta["gps"] = gps
    if captured_at is not None:
        meta["captured_at"] = captured_at.isoformat()
    source_row = conn.execute(
        "INSERT INTO sources (source_kind, external_id, raw_payload) "
        "VALUES ('capture', %s, '{}') RETURNING id",
        (f"src-{external_id}",),
    ).fetchone()
    assert source_row is not None
    conn.execute(
        """
        INSERT INTO captures (source_id, source_kind, external_id, raw_payload, capture_type)
        VALUES (%s, 'capture', %s, %s, 'observation')
        """,
        (source_row[0], external_id, Jsonb({"metadata": meta})),
    )


def test_harvest_records_observation_from_capture_gps(conn: psycopg.Connection):
    _insert_bin_capture(conn, external_id="cap-1", bin_code="ALA-1", lat=37.0, lon=-122.0)
    summary = harvest_photo_gps(conn, sites=SEPARATED)
    assert summary.recorded == 1
    observations = load_bin_observations(conn, "ALA-1")
    assert len(observations) == 1
    assert observations[0].observed_site == "garage"
    assert observations[0].source_kind == "photo_gps"
    assert observations[0].evidence_ref == "cap-1"


def test_harvest_is_idempotent(conn: psycopg.Connection):
    _insert_bin_capture(conn, external_id="cap-2", bin_code="ALA-2", lat=37.0, lon=-122.0)
    first = harvest_photo_gps(conn, sites=SEPARATED)
    second = harvest_photo_gps(conn, sites=SEPARATED)
    assert first.recorded == 1
    assert second.recorded == 0
    assert second.already == 1
    assert len(load_bin_observations(conn, "ALA-2")) == 1


def test_harvest_abstains_on_ambiguous_geofence(conn: psycopg.Connection):
    _insert_bin_capture(conn, external_id="cap-3", bin_code="ALA-3", lat=37.0, lon=-122.0005)
    summary = harvest_photo_gps(conn, sites=OVERLAPPING)
    assert summary.recorded == 0
    assert summary.skipped_ambiguous == 1
    assert load_bin_observations(conn, "ALA-3") == []


def test_harvest_skips_capture_without_gps(conn: psycopg.Connection):
    _insert_bin_capture(conn, external_id="cap-4", bin_code="ALA-4")  # no GPS fix
    summary = harvest_photo_gps(conn, sites=SEPARATED)
    assert summary.recorded == 0
    assert summary.skipped_no_gps == 1


def test_harvest_emits_contradiction_when_trusted_and_enabled(conn: psycopg.Connection):
    # The move ledger places the bin at the garage; a photo's GPS is unambiguously at
    # the storage geofence — a cross-site disagreement.
    record_event(conn, event_kind="place", bin_code="ALA-5", site="garage")
    _insert_bin_capture(conn, external_id="cap-5", bin_code="ALA-5", lat=37.01, lon=-122.0)
    summary = harvest_photo_gps(conn, sites=SEPARATED, emit_contradictions=True)
    assert summary.contradictions_emitted == 1
    # The bin is NOT relocated — only the move ledger moves it; the shock just lowers
    # confidence (the T3b invariant).
    assert bin_where(conn, "ALA-5").site == "garage"


def test_harvest_suppresses_contradiction_when_disabled(conn: psycopg.Connection):
    record_event(conn, event_kind="place", bin_code="ALA-6", site="garage")
    _insert_bin_capture(conn, external_id="cap-6", bin_code="ALA-6", lat=37.01, lon=-122.0)
    summary = harvest_photo_gps(conn, sites=SEPARATED, emit_contradictions=False)
    assert summary.contradictions_emitted == 0
    assert summary.contradictions_suppressed == 1


def test_harvest_no_contradiction_when_observation_corroborates(conn: psycopg.Connection):
    # Ledger and the photo GPS agree on the garage — no disagreement to escalate.
    record_event(conn, event_kind="place", bin_code="ALA-7", site="garage")
    _insert_bin_capture(conn, external_id="cap-7", bin_code="ALA-7", lat=37.0, lon=-122.0)
    summary = harvest_photo_gps(conn, sites=SEPARATED, emit_contradictions=True)
    assert summary.recorded == 1
    assert summary.contradictions_emitted == 0
    assert summary.contradictions_suppressed == 0
