"""RFC 0088 T3b — tests for the peripheral-OCR harvester (src/binkeeper/bin_ocr_harvest.py).

The vision read (``read_visible_bin_codes``) is tested against a fake OpenAI-shaped
client with no network. The harvest worker is tested against the migrated test DB via
the ``conn`` fixture with BOTH the OCR reader and the vault opener injected as stubs, so
neither a live model nor a real blob vault is touched: the test controls exactly which
codes each photo "shows" and which photos are fetchable.
"""

from __future__ import annotations

import json
from datetime import datetime

import psycopg
import pytest
from psycopg.types.json import Jsonb

from binkeeper import bin_ocr_harvest, bin_vision
from binkeeper.bin_harvest import SiteAnchor
from binkeeper.bin_inventory import bin_where, load_bin_observations, record_event
from binkeeper.bin_ocr_harvest import harvest_peripheral_ocr
from binkeeper.bin_vision import read_visible_bin_codes
from binkeeper.cli import build_parser, execute, harvest_exit_code

# Two well-separated sites (~1.1 km apart) — a fix at either centre is unambiguous.
SEPARATED = {
    "garage": SiteAnchor(lat=37.0, lon=-122.0, radius_m=100.0),
    "storage": SiteAnchor(lat=37.01, lon=-122.0, radius_m=100.0),
}
# Two overlapping sites — the midpoint is inside both (the abstain case).
OVERLAPPING = {
    "garage": SiteAnchor(lat=37.0, lon=-122.0, radius_m=200.0),
    "storage": SiteAnchor(lat=37.0, lon=-122.001, radius_m=200.0),
}


# --- pure: read_visible_bin_codes -------------------------------------------


class _FakeVisionClient:
    """A vision client that returns a canned raw answer (no network)."""

    model = "fake-vl"

    def __init__(self, answer: str) -> None:
        self._answer = answer

    def analyze(self, prompt: str, image: bytes) -> str:
        return self._answer


def test_read_visible_bin_codes_parses_and_dedupes():
    client = _FakeVisionClient('{"bin_codes": ["AGR-001", "agr-001", "OFE-014"]}')
    assert read_visible_bin_codes(client, b"img") == ["AGR-001", "OFE-014"]


def test_read_visible_bin_codes_drops_non_matching_tokens():
    client = _FakeVisionClient('{"bin_codes": ["not a code", "AGR-2", "AGR-002"]}')
    # "AGR-2" is too short (needs 2+ digits); "not a code" has no code; only AGR-002 stands.
    assert read_visible_bin_codes(client, b"img") == ["AGR-002"]


def test_read_visible_bin_codes_empty_on_unparseable():
    assert read_visible_bin_codes(_FakeVisionClient("no json here"), b"img") == []


def test_read_visible_bin_codes_empty_list():
    assert read_visible_bin_codes(_FakeVisionClient('{"bin_codes": []}'), b"img") == []


# --- IO: the harvest worker against the migrated test DB --------------------


def _insert_bin_capture(
    conn: psycopg.Connection,
    *,
    external_id: str,
    bin_code: str,
    lat: float | None = None,
    lon: float | None = None,
    accuracy_m: float | None = 8.0,
    photo_sha: str | None = "sha-photo",
    captured_at: datetime | None = None,
) -> None:
    """Insert one bin_capture carrying (optionally) a GPS fix and a photo blob ref."""
    meta: dict[str, object] = {"kind": "bin_capture", "bin_code": bin_code}
    if lat is not None and lon is not None:
        gps: dict[str, object] = {"lat": lat, "lon": lon}
        if accuracy_m is not None:
            gps["accuracy_m"] = accuracy_m
        meta["gps"] = gps
    if photo_sha is not None:
        meta["photo"] = {"sha256": photo_sha}
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


def _reader(*codes: str):
    """A stub OCR reader that reports the same codes for every photo."""
    return lambda _image: list(codes)


def _opener(data: bytes = b"\xff\xd8\xffJPEG"):
    """A stub vault opener that returns the same bytes for any sha."""
    return lambda _conn, _sha: data


def _missing_opener():
    """A stub vault opener that can't fetch anything (photo gone/undecryptable)."""
    return lambda _conn, _sha: None


def test_ocr_records_observation_for_the_photos_own_bin(conn: psycopg.Connection):
    _insert_bin_capture(conn, external_id="cap-1", bin_code="AGR-001", lat=37.0, lon=-122.0)
    summary = harvest_peripheral_ocr(
        conn, sites=SEPARATED, read_codes=_reader("AGR-001"), open_photo=_opener()
    )
    assert summary.photos_read == 1
    assert summary.codes_matched == 1
    assert summary.recorded == 1
    observations = load_bin_observations(conn, "AGR-001")
    assert len(observations) == 1
    assert observations[0].observed_site == "garage"
    assert observations[0].source_kind == "peripheral_ocr"
    assert observations[0].evidence_ref == "cap-1"


def test_ocr_corroborates_a_known_peripheral_bin_in_the_background(conn: psycopg.Connection):
    # The photo is about AGR-001 but also shows AGR-002 (a bin the ledger knows) on the
    # same shelf: peripheral OCR corroborates AGR-002 at the same site, zero extra action.
    record_event(conn, event_kind="place", bin_code="AGR-002", site="garage")
    _insert_bin_capture(conn, external_id="cap-2", bin_code="AGR-001", lat=37.0, lon=-122.0)
    summary = harvest_peripheral_ocr(
        conn, sites=SEPARATED, read_codes=_reader("AGR-001", "AGR-002"), open_photo=_opener()
    )
    assert summary.codes_matched == 2
    assert summary.recorded == 2
    assert load_bin_observations(conn, "AGR-002")[0].source_kind == "peripheral_ocr"


def test_ocr_drops_unknown_code(conn: psycopg.Connection):
    # A misread or a bin we don't know: corroborate nothing (the hallucination gate).
    _insert_bin_capture(conn, external_id="cap-3", bin_code="AGR-001", lat=37.0, lon=-122.0)
    summary = harvest_peripheral_ocr(
        conn, sites=SEPARATED, read_codes=_reader("AGR-001", "ZZZ-999"), open_photo=_opener()
    )
    assert summary.codes_matched == 1
    assert summary.unknown_codes_dropped == 1
    assert summary.recorded == 1
    assert load_bin_observations(conn, "ZZZ-999") == []


def test_ocr_is_idempotent(conn: psycopg.Connection):
    _insert_bin_capture(conn, external_id="cap-4", bin_code="AGR-001", lat=37.0, lon=-122.0)
    first = harvest_peripheral_ocr(
        conn, sites=SEPARATED, read_codes=_reader("AGR-001"), open_photo=_opener()
    )
    second = harvest_peripheral_ocr(
        conn, sites=SEPARATED, read_codes=_reader("AGR-001"), open_photo=_opener()
    )
    assert first.recorded == 1
    assert second.recorded == 0
    assert second.already == 1
    assert len(load_bin_observations(conn, "AGR-001")) == 1


def test_ocr_abstains_on_ambiguous_geofence_without_reading_photo(conn: psycopg.Connection):
    _insert_bin_capture(conn, external_id="cap-5", bin_code="AGR-001", lat=37.0, lon=-122.0005)
    summary = harvest_peripheral_ocr(
        conn, sites=OVERLAPPING, read_codes=_reader("AGR-001"), open_photo=_opener()
    )
    assert summary.skipped_ambiguous == 1
    assert summary.photos_read == 0  # a location signal with no site is never even OCR'd
    assert summary.recorded == 0


def test_ocr_skips_capture_without_photo(conn: psycopg.Connection):
    _insert_bin_capture(
        conn, external_id="cap-6", bin_code="AGR-001", lat=37.0, lon=-122.0, photo_sha=None
    )
    summary = harvest_peripheral_ocr(
        conn, sites=SEPARATED, read_codes=_reader("AGR-001"), open_photo=_opener()
    )
    assert summary.skipped_no_photo == 1
    assert summary.recorded == 0


def test_ocr_skips_capture_without_gps(conn: psycopg.Connection):
    _insert_bin_capture(conn, external_id="cap-7", bin_code="AGR-001")  # no GPS fix
    summary = harvest_peripheral_ocr(
        conn, sites=SEPARATED, read_codes=_reader("AGR-001"), open_photo=_opener()
    )
    assert summary.skipped_no_gps == 1
    assert summary.recorded == 0


def test_ocr_skips_when_photo_unreadable(conn: psycopg.Connection):
    _insert_bin_capture(conn, external_id="cap-8", bin_code="AGR-001", lat=37.0, lon=-122.0)
    summary = harvest_peripheral_ocr(
        conn, sites=SEPARATED, read_codes=_reader("AGR-001"), open_photo=_missing_opener()
    )
    assert summary.skipped_photo_unreadable == 1
    assert summary.recorded == 0


def test_ocr_emits_contradiction_when_trusted_and_enabled(conn: psycopg.Connection):
    # The move ledger places AGR-001 at the garage; a photo unambiguously at the storage
    # geofence OCRs the bin there — a cross-site disagreement. peripheral_ocr's fresh
    # reliability equals its prior mean (0.6), exactly meeting the contradiction gate.
    record_event(conn, event_kind="place", bin_code="AGR-001", site="garage")
    _insert_bin_capture(conn, external_id="cap-9", bin_code="AGR-001", lat=37.01, lon=-122.0)
    summary = harvest_peripheral_ocr(
        conn,
        sites=SEPARATED,
        read_codes=_reader("AGR-001"),
        open_photo=_opener(),
        emit_contradictions=True,
    )
    assert summary.contradictions_emitted == 1
    # The bin is NOT relocated — only the move ledger moves it (the T3b invariant).
    assert bin_where(conn, "AGR-001").site == "garage"


def test_ocr_suppresses_contradiction_when_disabled(conn: psycopg.Connection):
    record_event(conn, event_kind="place", bin_code="AGR-001", site="garage")
    _insert_bin_capture(conn, external_id="cap-10", bin_code="AGR-001", lat=37.01, lon=-122.0)
    summary = harvest_peripheral_ocr(
        conn,
        sites=SEPARATED,
        read_codes=_reader("AGR-001"),
        open_photo=_opener(),
        emit_contradictions=False,
    )
    assert summary.contradictions_emitted == 0
    assert summary.contradictions_suppressed == 1


def test_ocr_max_photos_caps_the_pass(conn: psycopg.Connection):
    for i in range(3):
        _insert_bin_capture(
            conn, external_id=f"cap-cap-{i}", bin_code="AGR-001", lat=37.0, lon=-122.0
        )
    summary = harvest_peripheral_ocr(
        conn, sites=SEPARATED, read_codes=_reader("AGR-001"), open_photo=_opener(), max_photos=2
    )
    assert summary.photos_read == 2


# --- CLI: the bin-ocr-harvest subcommand (the nightly true-up entry point) ---


def test_cli_ocr_harvest_refuses_before_reading_when_writer_authority_is_closed(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BINKEEPER_WRITES_ENABLED", raising=False)
    _insert_bin_capture(
        conn, external_id="cap-cli-frozen", bin_code="AGR-001", lat=37.0, lon=-122.0
    )

    with pytest.raises(RuntimeError, match="writer is frozen"):
        execute(build_parser().parse_args(["bin-ocr-harvest"]), conn)

    assert load_bin_observations(conn, "AGR-001") == []


def _wire_cli_defaults(monkeypatch: pytest.MonkeyPatch, *codes: str) -> None:
    """Point the CLI's default harvest wiring at stubs: no model, no vault, no sites file."""
    monkeypatch.setattr(bin_ocr_harvest, "load_sites", lambda: SEPARATED)
    monkeypatch.setattr(bin_ocr_harvest, "_default_code_reader", lambda: _reader(*codes))
    monkeypatch.setattr(bin_ocr_harvest, "_default_photo_opener", lambda: _opener())


def test_cli_ocr_harvest_records_and_prints_summary(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BINKEEPER_WRITES_ENABLED", "1")
    _wire_cli_defaults(monkeypatch, "AGR-001")
    _insert_bin_capture(conn, external_id="cap-cli", bin_code="AGR-001", lat=37.0, lon=-122.0)

    payload = execute(build_parser().parse_args(["bin-ocr-harvest"]), conn)

    assert payload["photos_read"] == 1
    assert payload["recorded"] == 1
    observations = load_bin_observations(conn, "AGR-001")
    assert len(observations) == 1
    assert observations[0].source_kind == "peripheral_ocr"


def test_cli_ocr_harvest_max_photos_flag_caps_the_pass(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BINKEEPER_WRITES_ENABLED", "1")
    _wire_cli_defaults(monkeypatch, "AGR-001")
    for i in range(3):
        _insert_bin_capture(
            conn, external_id=f"cap-cli-{i}", bin_code="AGR-001", lat=37.0, lon=-122.0
        )

    payload = execute(build_parser().parse_args(["bin-ocr-harvest", "--max-photos", "2"]), conn)

    assert payload["photos_read"] == 2


def test_cli_ocr_harvest_rejects_negative_max_photos() -> None:
    # A negative cap would silently invert "0 = no cap" into "read nothing, exit green".
    with pytest.raises(SystemExit, match="2"):
        build_parser().parse_args(["bin-ocr-harvest", "--max-photos", "-1"])


def test_cli_ocr_harvest_default_cap_comes_from_the_env_tunable() -> None:
    args = build_parser().parse_args(["bin-ocr-harvest"])
    assert args.max_photos == bin_ocr_harvest.BIN_OCR_MAX_PHOTOS


def test_cli_ocr_harvest_local_only_refuses_a_cloud_provider(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The nightly unit passes --local-only: a partially applied env pin must fail the
    # run before a single photo is read, never silently export photos to a cloud model.
    monkeypatch.setenv("BINKEEPER_WRITES_ENABLED", "1")
    monkeypatch.setattr(bin_vision, "DEFAULT_VISION_PROVIDER", "openrouter")
    _insert_bin_capture(conn, external_id="cap-cli-cloud", bin_code="AGR-001", lat=37.0, lon=-122.0)

    with pytest.raises(ValueError, match="local"):
        execute(build_parser().parse_args(["bin-ocr-harvest", "--local-only"]), conn)

    assert load_bin_observations(conn, "AGR-001") == []


def test_cli_ocr_harvest_local_only_allows_the_local_provider(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BINKEEPER_WRITES_ENABLED", "1")
    monkeypatch.setattr(bin_vision, "DEFAULT_VISION_PROVIDER", "local")
    _wire_cli_defaults(monkeypatch, "AGR-001")
    _insert_bin_capture(conn, external_id="cap-cli-local", bin_code="AGR-001", lat=37.0, lon=-122.0)

    payload = execute(build_parser().parse_args(["bin-ocr-harvest", "--local-only"]), conn)

    assert payload["recorded"] == 1


def test_cli_ocr_harvest_exit_code_flags_an_unconfigured_geofence() -> None:
    # sites_configured == 0 means no photo can ever resolve: misconfiguration, not a
    # quiet night. The nightly unit must go red, not report success forever.
    assert harvest_exit_code({"sites_configured": 0, "photos_read": 0, "codes_matched": 0}) == 3


def test_cli_ocr_harvest_exit_code_flags_a_backend_that_reads_no_codes() -> None:
    # Photos were fetched but not one code came back (known or unknown): the usual
    # cause is a dead/unpulled vision model, which read_visible_bin_codes swallows.
    payload = {
        "sites_configured": 2,
        "photos_read": 3,
        "codes_matched": 0,
        "unknown_codes_dropped": 0,
    }
    assert harvest_exit_code(payload) == 4


def test_cli_ocr_harvest_exit_code_is_zero_for_ordinary_passes() -> None:
    quiet_night = {
        "sites_configured": 2,
        "photos_read": 0,
        "codes_matched": 0,
        "unknown_codes_dropped": 0,
    }
    assert harvest_exit_code(quiet_night) == 0
    misreads_only = {
        "sites_configured": 2,
        "photos_read": 2,
        "codes_matched": 0,
        "unknown_codes_dropped": 1,
    }
    assert harvest_exit_code(misreads_only) == 0
    productive = {
        "sites_configured": 2,
        "photos_read": 2,
        "codes_matched": 3,
        "unknown_codes_dropped": 0,
    }
    assert harvest_exit_code(productive) == 0


def test_ocr_summary_json_is_stable(conn: psycopg.Connection):
    _insert_bin_capture(conn, external_id="cap-json", bin_code="AGR-001", lat=37.0, lon=-122.0)
    summary = harvest_peripheral_ocr(
        conn, sites=SEPARATED, read_codes=_reader("AGR-001"), open_photo=_opener()
    )
    payload = json.loads(json.dumps(summary.to_json()))
    assert payload["recorded"] == 1
    assert payload["emit_contradictions"] is False
