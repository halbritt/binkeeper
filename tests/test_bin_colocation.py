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


# --- containment fold (BINK-32) ------------------------------------------------


def _sighting(anchor: str, days_ago: float, strength: float = 1.0):
    from datetime import UTC, datetime, timedelta

    from binkeeper.bin_colocation import ColocationSighting

    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
    return now, ColocationSighting(
        anchor_code=anchor, observed_at=now - timedelta(days=days_ago), strength=strength
    )


def test_fold_containment_fresh_sighting_wins_confidently() -> None:
    from binkeeper.bin_colocation import fold_containment

    now, sighting = _sighting("LOC-014", days_ago=9)
    belief = fold_containment("AGR-001", [sighting], now=now)
    assert belief.anchor_code == "LOC-014"
    assert belief.abstained is False
    assert belief.confidence > 0.9
    assert belief.age_days is not None and round(belief.age_days) == 9
    assert belief.to_json() is not None


def test_fold_containment_decays_below_floor_and_abstains() -> None:
    from binkeeper.bin_colocation import fold_containment

    now, sighting = _sighting("LOC-014", days_ago=400)
    belief = fold_containment("AGR-001", [sighting], now=now)
    assert belief.abstained is True
    assert belief.to_json() is None  # shelf tier falls back to site level


def test_fold_containment_contradiction_shocks_the_winner() -> None:
    from binkeeper.bin_colocation import fold_containment

    now, old_home = _sighting("LOC-001", days_ago=5)
    _, s2 = _sighting("LOC-001", days_ago=4)
    _, elsewhere = _sighting("LOC-002", days_ago=1, strength=0.6)
    belief = fold_containment("AGR-001", [old_home, s2, elsewhere], now=now)
    # LOC-001 still holds more mass, but the later LOC-002 sighting shocks it.
    assert belief.anchor_code == "LOC-001"
    assert belief.confidence < 0.7


def test_fold_containment_recent_mass_switches_the_winner() -> None:
    from binkeeper.bin_colocation import fold_containment

    now, faded = _sighting("LOC-001", days_ago=300)
    _, fresh1 = _sighting("LOC-002", days_ago=2)
    _, fresh2 = _sighting("LOC-002", days_ago=1)
    belief = fold_containment("AGR-001", [faded, fresh1, fresh2], now=now)
    assert belief.anchor_code == "LOC-002"
    assert belief.abstained is False


def test_bin_where_carries_the_shelf_tier(conn: psycopg.Connection) -> None:
    import argparse
    from datetime import UTC, datetime

    from binkeeper.bin_inventory import record_event
    from binkeeper.cli import execute

    record_event(conn, event_kind="place", bin_code="AGR-001", site="alameda-garage")
    record_colocation(
        conn,
        anchor_code="LOC-014",
        member_code="AGR-001",
        strength=1.0,
        observed_at=datetime.now(UTC),
        idempotency_key="where-1",
    )
    args = argparse.Namespace(
        command="bin-where", bin_code="AGR-001", tenant="personal", corpus="personal"
    )
    payload = execute(args, conn)
    assert payload["site"] == "alameda-garage"
    assert payload["anchor"]["anchor_code"] == "LOC-014"
    assert payload["anchor"]["confidence"] > 0.9

    bare = argparse.Namespace(
        command="bin-where", bin_code="AGR-999", tenant="personal", corpus="personal"
    )
    assert execute(bare, conn)["anchor"] is None


# --- anchor demotion (BINK-33) --------------------------------------------------


def test_demotion_abstains_even_when_old_mass_would_clear_the_floor(
    conn: psycopg.Connection,
) -> None:
    from datetime import UTC, datetime, timedelta

    from binkeeper.bin_colocation import bin_containment

    now = datetime(2026, 7, 20, tzinfo=UTC)
    # Ten heavy sightings 200 days ago: decayed mass still caps confidence at 1.0,
    # so without demotion this shelf claim would serve despite 200 unseen days.
    for index in range(10):
        record_colocation(
            conn,
            anchor_code="LOC-777",
            member_code="AGR-001",
            strength=1.0,
            observed_at=now - timedelta(days=200, minutes=index),
            idempotency_key=f"dem-{index}",
        )
    belief = bin_containment(conn, "AGR-001", now=now)
    assert belief.anchor_code == "LOC-777"
    assert belief.confidence >= 0.5  # the floor alone would have served this
    assert belief.abstained is True  # demotion horizon (180d) folds it to unverified
    assert belief.to_json() is None


def test_fresh_anchor_is_not_demoted(conn: psycopg.Connection) -> None:
    from datetime import UTC, datetime, timedelta

    from binkeeper.bin_colocation import bin_containment, demoted_anchors

    now = datetime(2026, 7, 20, tzinfo=UTC)
    record_colocation(
        conn,
        anchor_code="LOC-001",
        member_code="AGR-001",
        strength=1.0,
        observed_at=now - timedelta(days=5),
        idempotency_key="fresh-1",
    )
    assert demoted_anchors(conn, now=now) == frozenset()
    assert bin_containment(conn, "AGR-001", now=now).abstained is False
