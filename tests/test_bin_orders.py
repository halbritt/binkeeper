"""RFC 0088 T3c tests for append-only resting orders."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from binkeeper.bin_inventory import bin_where, record_event
from binkeeper.bin_orders import (
    RestingOrderEvent,
    clear_resting_order,
    create_resting_order,
    fold_orders,
    list_folded_orders,
    list_resting_orders,
)

NOW = datetime(2026, 6, 24, 18, 0, 0, tzinfo=UTC)


def _event(
    seq: int,
    event_kind: str,
    order_id: str,
    *,
    site: str | None = "alameda-garage",
    bin_code: str | None = "ORD-001",
    instruction: str | None = "confirm bin",
) -> RestingOrderEvent:
    return RestingOrderEvent(
        seq=seq,
        event_kind=event_kind,  # type: ignore[arg-type]
        order_id=order_id,
        occurred_at=NOW + timedelta(minutes=seq),
        site=site,
        bin_code=bin_code,
        action_kind="confirm_bin" if event_kind == "create" else None,
        instruction=instruction if event_kind == "create" else None,
        cleared_reason="done" if event_kind == "clear" else None,
    )


def test_order_create_is_active_clear_is_inactive() -> None:
    folded = fold_orders(
        [
            _event(1, "create", "ORDER-1"),
            _event(2, "clear", "ORDER-1", site=None, bin_code=None, instruction=None),
        ]
    )

    order = folded["ORDER-1"]
    assert order.active is False
    assert order.site == "alameda-garage"
    assert order.bin_code == "ORD-001"
    assert order.cleared_reason == "done"


def test_order_listing_filters_by_site_and_active_state() -> None:
    events = [
        _event(1, "create", "ORDER-GARAGE", site="alameda-garage"),
        _event(2, "create", "ORDER-STORAGE", site="alameda-storage"),
        _event(3, "clear", "ORDER-GARAGE", site=None, bin_code=None, instruction=None),
    ]

    active_storage = list_folded_orders(events, site="alameda-storage", active_only=True)
    all_garage = list_folded_orders(events, site="alameda-garage", active_only=False)
    active_garage = list_folded_orders(events, site="alameda-garage", active_only=True)

    assert [order.order_id for order in active_storage] == ["ORDER-STORAGE"]
    assert [order.order_id for order in all_garage] == ["ORDER-GARAGE"]
    assert active_garage == []


def test_order_fold_is_deterministic_for_unsorted_events() -> None:
    orders = list_folded_orders(
        [
            _event(2, "clear", "ORDER-1", site=None, bin_code=None, instruction=None),
            _event(1, "create", "ORDER-1"),
            _event(3, "create", "ORDER-2"),
        ],
        active_only=True,
    )

    assert [order.order_id for order in orders] == ["ORDER-2"]


def test_resting_orders_are_idempotent_and_filterable(conn: psycopg.Connection) -> None:
    first = create_resting_order(
        conn,
        site="alameda-garage",
        bin_code="ORD-001",
        instruction="confirm ORD-001",
        occurred_at=NOW,
        idempotency_key="order-create",
    )
    second = create_resting_order(
        conn,
        site="alameda-garage",
        bin_code="ORD-001",
        instruction="confirm ORD-001",
        occurred_at=NOW,
        idempotency_key="order-create",
    )
    clear_resting_order(
        conn,
        order_id=first.order_id,
        cleared_reason="confirmed",
        occurred_at=NOW + timedelta(minutes=1),
        idempotency_key="order-clear",
    )

    assert first.already_existed is False
    assert second.already_existed is True
    assert first.seq == second.seq
    assert list_resting_orders(conn, site="alameda-garage", active_only=True) == []
    cleared = list_resting_orders(conn, site="alameda-garage", active_only=False)
    assert [order.order_id for order in cleared] == [first.order_id]
    assert cleared[0].cleared_reason == "confirmed"


def test_order_table_is_append_only(conn: psycopg.Connection) -> None:
    create_resting_order(
        conn,
        site="alameda-garage",
        bin_code="ORD-002",
        instruction="confirm ORD-002",
        occurred_at=NOW,
        idempotency_key="order-append-only",
    )

    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE bin_resting_order_events SET site = 'moved'")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM bin_resting_order_events")


def test_resting_order_never_changes_bin_where(conn: psycopg.Connection) -> None:
    record_event(
        conn,
        event_kind="place",
        bin_code="ORD-ANCHOR",
        site="alameda-storage",
        occurred_at=NOW - timedelta(hours=1),
    )
    before = bin_where(conn, "ORD-ANCHOR").to_json()

    create_resting_order(
        conn,
        site="alameda-garage",
        bin_code="ORD-ANCHOR",
        instruction="confirm misplaced-looking bin",
        occurred_at=NOW,
        idempotency_key="order-does-not-move-bin",
    )

    assert bin_where(conn, "ORD-ANCHOR").to_json() == before
