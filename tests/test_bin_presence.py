"""RFC 0088 T3c tests for site-presence events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from binkeeper.bin_inventory import bin_where, record_event
from binkeeper.bin_presence import (
    BIN_PRESENCE_WINDOW_SECONDS,
    PresenceEvent,
    current_presence,
    current_site,
    fold_presence,
    list_current_presence,
    list_presence_events,
    presence_recency,
    record_presence,
    record_presence_event,
)

NOW = datetime(2026, 6, 24, 18, 0, 0, tzinfo=UTC)


def _presence(
    seq: int,
    event_kind: str,
    site: str,
    occurred_at: datetime,
) -> PresenceEvent:
    return PresenceEvent(
        seq=seq,
        event_kind=event_kind,  # type: ignore[arg-type]
        site=site,
        occurred_at=occurred_at,
    )


def test_presence_recency_decays_and_depart_is_absent() -> None:
    fresh = _presence(1, "arrive", "alameda-garage", NOW)
    stale = _presence(2, "dwell", "alameda-garage", NOW - timedelta(seconds=10))
    departed = _presence(3, "depart", "alameda-garage", NOW)

    assert presence_recency(fresh, now=NOW, stale_after_seconds=10.0) == 1.0
    assert presence_recency(stale, now=NOW, stale_after_seconds=10.0) == 0.0
    assert presence_recency(departed, now=NOW, stale_after_seconds=10.0) == 0.0


def test_fold_presence_returns_latest_fresh_site() -> None:
    events = [
        _presence(1, "arrive", "alameda-garage", NOW - timedelta(minutes=20)),
        _presence(2, "dwell", "alameda-storage", NOW - timedelta(minutes=5)),
    ]

    state = fold_presence(events, now=NOW, stale_after_seconds=BIN_PRESENCE_WINDOW_SECONDS)

    assert state.status == "present"
    assert state.site == "alameda-storage"
    assert current_site(events, now=NOW) == "alameda-storage"


def test_fold_presence_returns_stale_or_absent() -> None:
    stale = fold_presence(
        [_presence(1, "arrive", "alameda-garage", NOW - timedelta(hours=2))],
        now=NOW,
        stale_after_seconds=30 * 60,
    )
    absent = fold_presence(
        [_presence(2, "depart", "alameda-garage", NOW)],
        now=NOW,
    )

    assert stale.status == "stale"
    assert stale.site is None
    assert absent.status == "absent"
    assert absent.site is None


def test_record_presence_is_idempotent_and_loads_once(conn: psycopg.Connection) -> None:
    first = record_presence(
        conn,
        event_kind="arrive",
        site="alameda-garage",
        occurred_at=NOW,
        idempotency_key="presence-1",
    )
    second = record_presence_event(
        conn,
        event_kind="arrive",
        site="alameda-garage",
        observed_at=NOW,
        idempotency_key="presence-1",
    )

    assert first.already_existed is False
    assert second.already_existed is True
    assert first.seq == second.seq
    assert [event.seq for event in list_presence_events(conn)] == [first.seq]


def test_current_presence_reads_only_recent_site(conn: psycopg.Connection) -> None:
    record_presence(
        conn,
        event_kind="arrive",
        site="alameda-garage",
        occurred_at=NOW - timedelta(hours=2),
        idempotency_key="old-presence",
    )
    record_presence(
        conn,
        event_kind="arrive",
        site="alameda-storage",
        occurred_at=NOW,
        idempotency_key="new-presence",
    )

    fresh = current_presence(conn, now=NOW, window=timedelta(minutes=30))
    stale = list_current_presence(conn, now=NOW + timedelta(hours=2), window_minutes=30)

    assert fresh.site == "alameda-storage"
    assert stale.site is None


def test_presence_table_is_append_only(conn: psycopg.Connection) -> None:
    record_presence(
        conn,
        event_kind="arrive",
        site="alameda-garage",
        occurred_at=NOW,
        idempotency_key="presence-append-only",
    )

    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE bin_presence_events SET site = 'moved'")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM bin_presence_events")


def test_record_presence_never_changes_bin_where(conn: psycopg.Connection) -> None:
    record_event(
        conn,
        event_kind="place",
        bin_code="PRES-001",
        site="alameda-storage",
        occurred_at=NOW - timedelta(hours=1),
    )
    before = bin_where(conn, "PRES-001").to_json()

    record_presence(
        conn,
        event_kind="arrive",
        site="alameda-garage",
        occurred_at=NOW,
        idempotency_key="presence-does-not-move-bin",
    )

    assert bin_where(conn, "PRES-001").to_json() == before
