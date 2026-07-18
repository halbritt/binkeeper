"""RFC 0088 T3b tests for the transcript-deixis liveness lane and history_fit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from binkeeper.bin_liveness import (
    BinLivenessError,
    harvest_item_liveness,
    item_liveness_scores,
    normalize_phrase,
)
from binkeeper.bin_passport import BinPassport
from binkeeper.bin_route import route_text_item
from binkeeper.liveness_adapter import LivenessMention, StaticLivenessSource

NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def _insert_bin_capture(conn: psycopg.Connection, *, bin_code: str, accepts: list[str]) -> None:
    metadata = {
        "kind": "bin_capture",
        "schema_version": "bin_capture.v1",
        "bin_code": bin_code,
        "site": "oakland-fab-east",
        "captured_at": NOW.isoformat(),
        "contents_text": "misc gear",
        "bin_profile": {"theme": "hand tools", "accepts": accepts, "capacity_state": "half"},
    }
    src = conn.execute(
        "INSERT INTO sources (source_kind, external_id, raw_payload) "
        "VALUES ('capture', %s, '{}') RETURNING id",
        (f"liveness-binsrc-{bin_code}",),
    ).fetchone()
    assert src is not None
    conn.execute(
        """
        INSERT INTO captures (source_id, source_kind, external_id, raw_payload,
            capture_type, content_text, observed_at)
        VALUES (%s, 'capture', %s, %s, 'observation', %s, %s)
        """,
        (
            src[0],
            f"bincap-{bin_code}",
            Jsonb({"metadata": metadata}),
            f"bin {bin_code} @ oakland - misc gear",
            NOW,
        ),
    )


def _insert_text(
    conn: psycopg.Connection,
    *,
    external_id: str,
    source_kind: str,
    content_text: str,
    observed_at: datetime = NOW,
    privacy_tier: int = 1,
) -> None:
    src = conn.execute(
        "INSERT INTO sources (source_kind, external_id, raw_payload) "
        "VALUES (%s, %s, '{}') RETURNING id",
        (source_kind, f"liveness-src-{external_id}"),
    ).fetchone()
    assert src is not None
    conn.execute(
        """
        INSERT INTO captures (source_id, source_kind, external_id, raw_payload,
            capture_type, content_text, observed_at, privacy_tier)
        VALUES (%s, %s, %s, '{}', 'observation', %s, %s, %s)
        """,
        (src[0], source_kind, external_id, content_text, observed_at, privacy_tier),
    )


def _export_source(conn: psycopg.Connection) -> StaticLivenessSource:
    rows = conn.execute(
        """
        SELECT source_kind, external_id, content_text, observed_at, privacy_tier
        FROM captures
        WHERE content_text IS NOT NULL AND observed_at IS NOT NULL
        """
    ).fetchall()
    return StaticLivenessSource(
        tuple(
            LivenessMention(
                source_kind=str(row[0]),
                source_ref=str(row[1]),
                content_text=str(row[2]),
                mentioned_at=row[3],
                privacy_tier=int(row[4]),
            )
            for row in rows
        )
    )


def test_harvest_records_liveness_from_approved_text(conn: psycopg.Connection) -> None:
    _insert_bin_capture(conn, bin_code="FAB-003", accepts=["calipers", "hex keys"])
    _insert_text(
        conn,
        external_id="note-1",
        source_kind="capture",
        content_text="I finally found the calipers today",
    )

    source = _export_source(conn)
    first = harvest_item_liveness(conn, now=NOW, source=source)
    second = harvest_item_liveness(conn, now=NOW, source=source)  # idempotent

    assert first.mentions_recorded >= 1
    assert second.mentions_recorded == 0
    scores = item_liveness_scores(conn, now=NOW)
    assert scores.get(normalize_phrase("calipers"), 0.0) > 0.0


def test_harvest_ignores_excluded_sources(conn: psycopg.Connection) -> None:
    _insert_bin_capture(conn, bin_code="FAB-004", accepts=["micrometer"])
    _insert_text(
        conn,
        external_id="mail-1",
        source_kind="gmail",
        content_text="your micrometer order shipped",
    )

    harvest_item_liveness(conn, now=NOW, source=_export_source(conn))

    rows = conn.execute(
        "SELECT count(*) FROM bin_item_liveness WHERE source_kind = 'gmail'"
    ).fetchone()
    assert rows == (0,)
    assert item_liveness_scores(conn, now=NOW).get(normalize_phrase("micrometer"), 0.0) == 0.0


def test_source_policy_refuses_email_and_work_corpus(conn: psycopg.Connection) -> None:
    for forbidden in ("gmail", "claude_code", "git"):
        with pytest.raises(BinLivenessError, match="forbids"):
            harvest_item_liveness(conn, now=NOW, source_kinds=["capture", forbidden])


def test_missing_offline_adapter_is_explicitly_unavailable(
    conn: psycopg.Connection,
) -> None:
    _insert_bin_capture(conn, bin_code="FAB-ADAPTER", accepts=["torque wrench"])

    result = harvest_item_liveness(conn, now=NOW)

    assert result.source_status == "unavailable"
    assert result.mentions_recorded == 0
    assert result.source_reason == "offline liveness export is not configured"


def test_liveness_scores_decay_with_age(conn: psycopg.Connection) -> None:
    _insert_bin_capture(conn, bin_code="FAB-005", accepts=["torque wrench"])
    _insert_text(
        conn,
        external_id="old",
        source_kind="capture",
        content_text="used the torque wrench",
        observed_at=NOW - timedelta(days=400),
    )
    harvest_item_liveness(conn, now=NOW, source=_export_source(conn))

    key = normalize_phrase("torque wrench")
    fresh = item_liveness_scores(conn, now=NOW - timedelta(days=390))[key]
    stale = item_liveness_scores(conn, now=NOW)[key]
    assert 0.0 < stale < fresh <= 1.0


def test_liveness_is_append_only(conn: psycopg.Connection) -> None:
    _insert_bin_capture(conn, bin_code="FAB-006", accepts=["calipers"])
    _insert_text(conn, external_id="note-2", source_kind="capture", content_text="the calipers")
    harvest_item_liveness(conn, now=NOW, source=_export_source(conn))

    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE bin_item_liveness SET phrase_norm = 'x'")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM bin_item_liveness")


def _passport(bin_code: str, *, accepts: tuple[str, ...]) -> BinPassport:
    return BinPassport(
        bin_code=bin_code,
        theme="hand tools",
        home_site="alameda-garage",
        current_site="alameda-garage",
        owner_phrase=None,
        accepts=accepts,
        excludes=(),
        examples=(),
        sibling_contents=(),
        physical_constraints=(),
        volume_profile=None,
        capacity_state="half",  # type: ignore[arg-type]
        location_confidence=0.9,
        passport_confidence=0.85,
        provenance_refs=(),
    )


def test_history_fit_bonus_when_item_matches_a_live_phrase() -> None:
    passport = _passport("TOOL-1", accepts=("calipers", "micrometer"))
    live = {normalize_phrase("calipers"): 0.8}

    with_liveness = route_text_item(
        text="calipers", site="alameda-garage", passports=[passport], liveness=live
    )
    without = route_text_item(text="calipers", site="alameda-garage", passports=[passport])

    assert with_liveness.top_candidate is not None and without.top_candidate is not None
    assert with_liveness.top_candidate.score_parts.history_fit == pytest.approx(0.8)
    assert without.top_candidate.score_parts.history_fit == 0.0
    # history_fit is an additive bonus, so the live route scores strictly higher.
    assert with_liveness.top_candidate.total_score > without.top_candidate.total_score


def test_history_fit_is_zero_when_item_does_not_match() -> None:
    passport = _passport("TOOL-2", accepts=("calipers",))
    live = {normalize_phrase("calipers"): 0.9}

    route = route_text_item(
        text="wrenches", site="alameda-garage", passports=[passport], liveness=live
    )

    assert route.candidates[0].score_parts.history_fit == 0.0
