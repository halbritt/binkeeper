"""Existing-bin profile, photo, location, and print-intent behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import psycopg
import pytest

from binkeeper.bin_inventory import bin_where
from binkeeper.bin_manage import (
    BinLabelPrintIntent,
    BinManageError,
    BinPhotoAddition,
    BinPlacement,
    BinProfileUpdate,
    append_bin_photo,
    place_existing_bin,
    reserve_label_print_intent,
    update_bin_profile,
)
from binkeeper.bin_passport import bin_passport
from binkeeper.bin_register import register_bin
from binkeeper.personal_memory import PersonalCaptureConflict

PACIFIC_DAYLIGHT = timezone(timedelta(hours=-7))


def test_profile_update_appends_a_snapshot_and_projects_it_as_current(
    conn: psycopg.Connection,
) -> None:
    registered = register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        theme="Painting",
        contents_text="paint rollers",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )

    result = update_bin_profile(
        conn,
        BinProfileUpdate(
            bin_code="AGR-014",
            theme="Power tools",
            contents="cordless drills and impact drivers",
            home_site="oakland-wood-west",
            action_id="2d21c8a9-b984-4469-aa21-bbc6b9b6ab94",
            observed_at=datetime(2026, 7, 14, 10, tzinfo=UTC),
        ),
    )

    passport = bin_passport(conn, "AGR-014", now=datetime(2026, 7, 14, 11, tzinfo=UTC))
    original = conn.execute(
        "SELECT raw_payload->'metadata' FROM captures WHERE id = %s",
        (registered.capture_id,),
    ).fetchone()
    assert result.already_existed is False
    assert original is not None
    assert original[0]["bin_profile"] == {"theme": "Painting"}
    assert passport.theme == "Power tools"
    assert passport.home_site == "oakland-wood-west"
    assert passport.sibling_contents == ("cordless drills and impact drivers",)


def test_profile_update_replay_reuses_the_same_capture(conn: psycopg.Connection) -> None:
    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    update = BinProfileUpdate(
        bin_code="AGR-014",
        theme="Power tools",
        contents="cordless drills",
        home_site="alameda-garage",
        action_id="2d21c8a9-b984-4469-aa21-bbc6b9b6ab94",
        observed_at=datetime(2026, 7, 14, 3, tzinfo=PACIFIC_DAYLIGHT),
    )

    first = update_bin_profile(conn, update)
    replay = update_bin_profile(
        conn,
        BinProfileUpdate(
            bin_code=update.bin_code,
            theme=update.theme,
            contents=update.contents,
            home_site=update.home_site,
            action_id=update.action_id,
            observed_at=datetime(2026, 7, 14, 11, tzinfo=UTC),
        ),
    )

    assert replay.capture_id == first.capture_id
    assert replay.already_existed is True
    stored_time = conn.execute(
        "SELECT observed_at FROM captures WHERE id = %s",
        (first.capture_id,),
    ).fetchone()
    assert stored_time == (datetime(2026, 7, 14, 10, tzinfo=UTC),)
    count = conn.execute(
        """
        SELECT count(*) FROM captures
        WHERE raw_payload->'metadata'->>'profile_mode' = 'snapshot'
        """
    ).fetchone()
    assert count == (1,)


def test_profile_action_id_cannot_be_reused_for_different_contents(
    conn: psycopg.Connection,
) -> None:
    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    first = BinProfileUpdate(
        bin_code="AGR-014",
        theme="Power tools",
        contents="cordless drills",
        home_site="alameda-garage",
        action_id="2d21c8a9-b984-4469-aa21-bbc6b9b6ab94",
        observed_at=datetime(2026, 7, 14, 10, tzinfo=UTC),
    )
    changed = BinProfileUpdate(
        bin_code="AGR-014",
        theme="Power tools",
        contents="sanders",
        home_site="alameda-garage",
        action_id=first.action_id,
        observed_at=datetime(2026, 7, 14, 11, tzinfo=UTC),
    )

    update_bin_profile(conn, first)

    with pytest.raises(PersonalCaptureConflict):
        update_bin_profile(conn, changed)


def test_photo_addition_appends_a_link_without_changing_the_profile(
    conn: psycopg.Connection,
) -> None:
    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        theme="Painting",
        contents_text="paint rollers",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )

    result = append_bin_photo(
        conn,
        BinPhotoAddition(
            bin_code="AGR-014",
            photo_sha256="a" * 64,
            action_id="e5062bf5-61e0-4f46-b744-b46b1f81a896",
            observed_at=datetime(2026, 7, 14, 5, tzinfo=PACIFIC_DAYLIGHT),
        ),
    )
    replay = append_bin_photo(
        conn,
        BinPhotoAddition(
            bin_code="AGR-014",
            photo_sha256="a" * 64,
            action_id="e5062bf5-61e0-4f46-b744-b46b1f81a896",
            observed_at=datetime(2026, 7, 14, 13, tzinfo=UTC),
        ),
    )

    metadata = conn.execute(
        "SELECT raw_payload->'metadata' FROM captures WHERE id = %s",
        (result.capture_id,),
    ).fetchone()
    passport = bin_passport(conn, "AGR-014", now=datetime(2026, 7, 14, 13, tzinfo=UTC))
    assert metadata is not None
    assert replay.capture_id == result.capture_id
    assert replay.already_existed is True
    assert metadata[0]["photo"] == {"sha256": "a" * 64, "blob_ref": "a" * 64}
    assert "profile_mode" not in metadata[0]
    assert passport.theme == "Painting"
    assert passport.sibling_contents == ("paint rollers",)


def test_location_change_appends_a_place_event_without_changing_return_to_site(
    conn: psycopg.Connection,
) -> None:
    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        theme="Tools",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )

    result = place_existing_bin(
        conn,
        BinPlacement(
            bin_code="AGR-014",
            site="oakland-fab-east",
            action_id="c32d9090-2270-4590-901d-b44c253c9113",
            occurred_at=datetime(2026, 7, 14, 14, tzinfo=UTC),
        ),
    )

    assert result.already_existed is False
    assert bin_where(conn, "AGR-014").site == "oakland-fab-east"
    passport = bin_passport(conn, "AGR-014", now=datetime(2026, 7, 14, 15, tzinfo=UTC))
    assert passport.current_site == "oakland-fab-east"
    assert passport.home_site == "alameda-garage"


def test_location_action_id_cannot_be_reused_for_a_different_site(
    conn: psycopg.Connection,
) -> None:
    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    first = BinPlacement(
        bin_code="AGR-014",
        site="oakland-fab-east",
        action_id="c32d9090-2270-4590-901d-b44c253c9113",
        occurred_at=datetime(2026, 7, 14, 14, tzinfo=UTC),
    )

    place_existing_bin(conn, first)

    with pytest.raises(BinManageError):
        place_existing_bin(
            conn,
            BinPlacement(
                bin_code=first.bin_code,
                site="alameda-storage",
                action_id=first.action_id,
                occurred_at=datetime(2026, 7, 14, 15, tzinfo=UTC),
            ),
        )


def test_label_print_intent_is_reserved_at_most_once(conn: psycopg.Connection) -> None:
    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    intent = BinLabelPrintIntent(
        bin_code="AGR-014",
        action_id="3a581aa2-589e-4626-a31b-62124a193917",
        requested_at=datetime(2026, 7, 14, 9, tzinfo=PACIFIC_DAYLIGHT),
        queue="OmezizyD450",
        tspl_sha256="b" * 64,
    )

    first = reserve_label_print_intent(conn, intent)
    replay = reserve_label_print_intent(
        conn,
        BinLabelPrintIntent(
            bin_code=intent.bin_code,
            action_id=intent.action_id,
            requested_at=datetime(2026, 7, 14, 17, tzinfo=UTC),
            queue=intent.queue,
            tspl_sha256=intent.tspl_sha256,
        ),
    )

    assert first.already_existed is False
    assert replay.already_existed is True
    assert replay.capture_id == first.capture_id
    rows = conn.execute(
        """
        SELECT raw_payload->'metadata'->>'kind'
        FROM captures
        WHERE raw_payload->'metadata'->>'bin_code' = 'AGR-014'
          AND raw_payload->'metadata'->>'kind' = 'bin_label_print_intent'
        """
    ).fetchall()
    assert rows == [("bin_label_print_intent",)]
