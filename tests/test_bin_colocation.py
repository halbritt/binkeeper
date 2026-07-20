"""BINK-31 tests for witnessed anchor↔bin co-location evidence."""

from __future__ import annotations

from io import BytesIO

import psycopg
import pytest

from binkeeper.bin_colocation import (
    BinColocationError,
    colocation_pairs,
    harvest_photo_colocations,
    record_colocation,
)
from binkeeper.bin_geo import DecodedCode


def _code(
    code: str, *, left: int = 0, top: int = 0, width: int = 100, height: int = 100
) -> DecodedCode:
    return DecodedCode(
        code=code,
        is_anchor=code.startswith("LOC-"),
        left=left,
        top=top,
        width=width,
        height=height,
    )


def test_same_size_anchor_pairs_at_full_strength() -> None:
    pairs = colocation_pairs([_code("LOC-014"), _code("AGR-001", left=150)])
    assert len(pairs) == 1
    assert pairs[0].anchor_code == "LOC-014"
    assert pairs[0].member_code == "AGR-001"
    assert pairs[0].strength == 1.0


def test_small_distant_anchor_records_low_strength() -> None:
    # The anchor appears far smaller than the member label: a wide shot.
    pairs = colocation_pairs([_code("LOC-014", width=20, height=20), _code("AGR-001", left=400)])
    assert pairs[0].strength == 0.3


def test_second_anchor_in_frame_gets_residual_strength_only() -> None:
    pairs = colocation_pairs(
        [
            _code("LOC-001", left=0),
            _code("LOC-002", left=1000),
            _code("AGR-001", left=100),
        ]
    )
    by_anchor = {pair.anchor_code: pair for pair in pairs}
    assert by_anchor["LOC-001"].strength == 1.0  # nearest
    assert by_anchor["LOC-002"].strength == 0.2  # residual
    assert len(pairs) == 2


def test_no_anchor_or_no_member_yields_no_pairs() -> None:
    assert colocation_pairs([_code("AGR-001"), _code("AGR-002", left=150)]) == []
    assert colocation_pairs([_code("LOC-001")]) == []


def test_record_colocation_is_idempotent_and_append_only(conn: psycopg.Connection) -> None:
    first = record_colocation(
        conn,
        anchor_code="LOC-014",
        member_code="AGR-001",
        strength=1.0,
        idempotency_key="coloc-1",
    )
    replay = record_colocation(
        conn,
        anchor_code="LOC-014",
        member_code="AGR-001",
        strength=1.0,
        idempotency_key="coloc-1",
    )
    assert (first, replay) == (True, False)
    assert conn.execute("SELECT count(*) FROM colocation_observations").fetchone() == (1,)
    with pytest.raises(psycopg.Error):
        conn.execute("DELETE FROM colocation_observations")


def test_record_colocation_rejects_non_anchor(conn: psycopg.Connection) -> None:
    with pytest.raises(BinColocationError):
        record_colocation(conn, anchor_code="AGR-001", member_code="AGR-002", strength=1.0)


def test_harvest_appends_pairs_from_a_multi_code_photo(conn: psycopg.Connection) -> None:
    import qrcode
    from PIL import Image

    tiles = []
    for payload in ("LOC-014", "AGR-001", "AGR-002"):
        buf = BytesIO()
        qrcode.make(payload).save(buf, format="PNG")
        buf.seek(0)
        tiles.append(Image.open(buf).convert("RGB"))
    sheet = Image.new("RGB", (sum(t.width for t in tiles), max(t.height for t in tiles)), "white")
    x = 0
    for tile in tiles:
        sheet.paste(tile, (x, 0))
        x += tile.width
    out = BytesIO()
    sheet.save(out, format="PNG")

    pairs = harvest_photo_colocations(conn, out.getvalue(), evidence_ref="photo:test")

    assert sorted(pair.member_code for pair in pairs) == ["AGR-001", "AGR-002"]
    assert all(pair.anchor_code == "LOC-014" for pair in pairs)
    rows = conn.execute(
        "SELECT member_code, observation_strength, source_kind, evidence_ref "
        "FROM colocation_observations ORDER BY member_code"
    ).fetchall()
    assert [row[0] for row in rows] == ["AGR-001", "AGR-002"]
    assert all(row[1] == 1.0 for row in rows)  # equal-size QR tiles read as same-shelf
    assert all(row[2] == "qr_colocation" and row[3] == "photo:test" for row in rows)


def test_harvest_without_anchor_appends_nothing(conn: psycopg.Connection) -> None:
    import qrcode

    buf = BytesIO()
    qrcode.make("AGR-001").save(buf, format="PNG")
    assert harvest_photo_colocations(conn, buf.getvalue()) == []
    assert conn.execute("SELECT count(*) FROM colocation_observations").fetchone() == (0,)
