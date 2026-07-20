"""RFC 0088 registration: GPS EXIF parsing + site-anchor persistence (pure, no DB)."""

from __future__ import annotations

import io

import pytest

from binkeeper.bin_geo import (
    _dms_to_deg,
    decode_bin_code,
    extract_gps,
    normalize_bin_code,
    upsert_site_anchor,
)
from binkeeper.bin_harvest import load_sites


def _qr_png(payload: str) -> bytes:
    import qrcode

    buf = io.BytesIO()
    qrcode.make(payload).save(buf, format="PNG")
    return buf.getvalue()


def test_decode_bin_code_reads_the_qr() -> None:
    assert decode_bin_code(_qr_png("AGR-001")) == "AGR-001"


def test_decode_bin_code_extracts_a_code_from_a_url_qr() -> None:
    # A QR that wraps the code in a URL still yields the code.
    assert decode_bin_code(_qr_png("https://x/bin/OFE-014")) == "OFE-014"


def test_decode_bin_code_none_without_a_qr() -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "white").save(buf, format="PNG")
    assert decode_bin_code(buf.getvalue()) is None
    assert decode_bin_code(b"not an image") is None


def test_normalize_bin_code_upcases_and_validates() -> None:
    assert normalize_bin_code("agr-001") == "AGR-001"
    assert normalize_bin_code("bin AGR-001 here") == "AGR-001"
    assert normalize_bin_code("garbage") is None
    assert normalize_bin_code(None) is None


def test_dms_to_deg_northeast_is_positive() -> None:
    assert _dms_to_deg((37, 45, 50.08), "N") == pytest.approx(37.763911, abs=1e-5)


def test_dms_to_deg_west_is_negative() -> None:
    deg = _dms_to_deg((122, 13, 35.24), "W")
    assert deg is not None and deg < 0


def test_dms_to_deg_rejects_malformed() -> None:
    assert _dms_to_deg(None, "N") is None
    assert _dms_to_deg(("x", "y", "z"), "N") is None


def test_extract_gps_returns_none_without_exif() -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(buf, format="JPEG")
    assert extract_gps(buf.getvalue()) is None


def test_extract_gps_returns_none_on_garbage() -> None:
    assert extract_gps(b"not an image") is None


def test_upsert_site_anchor_establishes_then_refuses_to_overwrite(tmp_path) -> None:
    sites_file = tmp_path / "sites.json"

    assert upsert_site_anchor("alameda-garage", 37.7639, -122.2265, path=sites_file) is True
    loaded = load_sites(sites_file)
    assert loaded["alameda-garage"].lat == pytest.approx(37.7639)
    assert loaded["alameda-garage"].lon == pytest.approx(-122.2265)

    # A second establish for the same site is a no-op (won't drift the anchor).
    assert upsert_site_anchor("alameda-garage", 1.0, 2.0, path=sites_file) is False
    assert load_sites(sites_file)["alameda-garage"].lat == pytest.approx(37.7639)

    # ...unless the owner explicitly re-surveys it.
    assert upsert_site_anchor("alameda-garage", 1.0, 2.0, path=sites_file, overwrite=True) is True
    assert load_sites(sites_file)["alameda-garage"].lat == pytest.approx(1.0)


def test_upsert_site_anchor_writes_owner_only_permissions(tmp_path) -> None:
    sites_file = tmp_path / "sites.json"
    upsert_site_anchor("cargo-trailer", 37.7, -122.2, path=sites_file)
    assert (sites_file.stat().st_mode & 0o077) == 0  # precise location: 0600, no group/other


def _composite(*payloads: str) -> bytes:
    """One image holding several QR codes side by side."""
    from io import BytesIO

    import qrcode
    from PIL import Image

    tiles = []
    for payload in payloads:
        buf = BytesIO()
        qrcode.make(payload).save(buf, format="PNG")
        buf.seek(0)
        tiles.append(Image.open(buf).convert("RGB"))
    width = sum(tile.width for tile in tiles)
    height = max(tile.height for tile in tiles)
    sheet = Image.new("RGB", (width, height), "white")
    x = 0
    for tile in tiles:
        sheet.paste(tile, (x, 0))
        x += tile.width
    out = BytesIO()
    sheet.save(out, format="PNG")
    return out.getvalue()


def test_decode_all_codes_returns_every_symbol_with_rects() -> None:
    from binkeeper.bin_geo import decode_all_codes

    image = _composite("LOC-014", "AGR-001", "AGR-002")
    decoded = decode_all_codes(image)

    assert sorted(entry.code for entry in decoded) == ["AGR-001", "AGR-002", "LOC-014"]
    by_code = {entry.code: entry for entry in decoded}
    assert by_code["LOC-014"].is_anchor is True
    assert by_code["AGR-001"].is_anchor is False
    assert all(entry.width > 0 and entry.height > 0 for entry in decoded)
    # The tiles were pasted left-to-right, so the rects must be distinct.
    lefts = {entry.left for entry in decoded}
    assert len(lefts) == 3


def test_decode_bin_code_skips_anchor_codes() -> None:
    from binkeeper.bin_geo import decode_bin_code

    assert decode_bin_code(_composite("LOC-014", "AGR-001")) == "AGR-001"
    assert decode_bin_code(_composite("LOC-014")) is None


def test_is_anchor_code_uses_the_reserved_prefix() -> None:
    from binkeeper.bin_geo import is_anchor_code

    assert is_anchor_code("LOC-014") is True
    assert is_anchor_code("loc-014") is True
    assert is_anchor_code("AGR-014") is False
    assert is_anchor_code("shelf three") is False
