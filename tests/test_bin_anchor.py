"""BINK-30 tests for reviewed anchor label printing."""

from __future__ import annotations

import psycopg
import pytest

from binkeeper import bin_label
from binkeeper.bin_anchor import BinAnchorError, print_anchor_label


def test_anchor_print_refuses_non_anchor_codes(conn: psycopg.Connection) -> None:
    with pytest.raises(BinAnchorError):
        print_anchor_label(conn, anchor_code="AGR-001", action_id="a1")


def test_anchor_print_fails_closed_without_a_queue(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "")
    result = print_anchor_label(conn, anchor_code="LOC-001", action_id="a1")
    assert result.outcome == "failed"
    count = conn.execute("SELECT count(*) FROM capture_evidence").fetchone()
    assert count == (0,)  # nothing reserved when no queue is configured


def test_anchor_print_reserves_once_and_replays(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "TestQueue")
    monkeypatch.setattr(
        bin_label,
        "send_to_printer",
        lambda tspl, cups_queue: sent.append(cups_queue),
    )
    first = print_anchor_label(conn, anchor_code="loc-014", action_id="a2")
    replay = print_anchor_label(conn, anchor_code="LOC-014", action_id="a2")

    assert first.outcome == "sent"
    assert first.anchor_code == "LOC-014"
    assert replay.outcome == "replayed"
    assert sent == ["TestQueue"]  # exactly one printer attempt
    count = conn.execute(
        "SELECT count(*) FROM captures WHERE raw_payload->>'source_label' = 'anchor-print'"
    ).fetchone()
    assert count == (1,)
