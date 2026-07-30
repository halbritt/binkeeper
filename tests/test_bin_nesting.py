from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from binkeeper.bin_nesting import BinContainmentAppend


def test_pack_event_makes_the_container_the_bins_current_parent() -> None:
    from binkeeper.bin_nesting import BinContainmentEvent, fold_bin_containment

    event = BinContainmentEvent(
        seq=1,
        event_kind="pack",
        bin_code="AGR-001",
        container_code="AGR-010",
        site="alameda-garage",
        occurred_at=datetime(2026, 7, 30, 6, tzinfo=UTC),
    )

    graph = fold_bin_containment([event])

    assert graph.parent_of("AGR-001") == "AGR-010"
    assert graph.path_for("AGR-001") == ("AGR-010",)


def test_containment_fold_rejects_cycles() -> None:
    from binkeeper.bin_nesting import (
        BinContainmentEvent,
        BinNestingError,
        fold_bin_containment,
    )

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    events = [
        BinContainmentEvent(1, "pack", "AGR-001", "AGR-010", "alameda-garage", when),
        BinContainmentEvent(2, "pack", "AGR-010", "AGR-001", "alameda-garage", when),
    ]

    with pytest.raises(BinNestingError, match="cycle"):
        fold_bin_containment(events)


def test_containment_fold_rejects_an_unpack_from_the_wrong_container() -> None:
    from binkeeper.bin_nesting import (
        BinContainmentEvent,
        BinNestingError,
        fold_bin_containment,
    )

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    events = [
        BinContainmentEvent(1, "pack", "AGR-001", "AGR-010", "alameda-garage", when),
        BinContainmentEvent(2, "unpack", "AGR-001", "AGR-099", "alameda-garage", when),
    ]

    with pytest.raises(BinNestingError, match="not inside"):
        fold_bin_containment(events)


def test_containment_fold_requires_unpack_before_reparenting() -> None:
    from binkeeper.bin_nesting import (
        BinContainmentEvent,
        BinNestingError,
        fold_bin_containment,
    )

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    events = [
        BinContainmentEvent(1, "pack", "AGR-001", "AGR-010", "alameda-garage", when),
        BinContainmentEvent(2, "pack", "AGR-001", "AGR-020", "alameda-garage", when),
    ]

    with pytest.raises(BinNestingError, match="already inside"):
        fold_bin_containment(events)


def test_pack_append_is_idempotent_and_projects_the_current_parent(
    conn: psycopg.Connection,
) -> None:
    from binkeeper.bin_nesting import load_bin_containment, record_bin_containment
    from binkeeper.bin_register import register_bin

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    for code in ("AGR-001", "AGR-010"):
        register_bin(conn, bin_code=code, site="alameda-garage", observed_at=when)

    first = record_bin_containment(
        conn,
        BinContainmentAppend(
            event_kind="pack",
            bin_code="AGR-001",
            container_code="AGR-010",
            occurred_at=when,
            idempotency_key="pack-001-into-010",
        ),
    )
    replay = record_bin_containment(
        conn,
        BinContainmentAppend(
            event_kind="pack",
            bin_code="AGR-001",
            container_code="AGR-010",
            occurred_at=when,
            idempotency_key="pack-001-into-010",
        ),
    )

    assert first.already_existed is False
    assert replay.event_id == first.event_id
    assert replay.already_existed is True
    assert load_bin_containment(conn).parent_of("AGR-001") == "AGR-010"


def test_contained_bin_follows_its_outer_containers_location(
    conn: psycopg.Connection,
) -> None:
    from binkeeper.bin_inventory import bin_where, record_event
    from binkeeper.bin_nesting import record_bin_containment
    from binkeeper.bin_register import register_bin

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    for code in ("AGR-001", "AGR-010"):
        register_bin(conn, bin_code=code, site="alameda-garage", observed_at=when)
    record_bin_containment(
        conn,
        BinContainmentAppend(
            event_kind="pack",
            bin_code="AGR-001",
            container_code="AGR-010",
            occurred_at=when,
            idempotency_key="pack-001-into-010",
        ),
    )

    record_event(
        conn,
        event_kind="place",
        bin_code="AGR-010",
        site="alameda-storage",
        occurred_at=when,
        idempotency_key="move-container",
    )

    location = bin_where(conn, "AGR-001")
    assert location.site == "alameda-storage"
    assert location.container_code == "AGR-010"
    assert location.containment_path == ("AGR-010",)
    assert location.location_source_bin == "AGR-010"


def test_nested_bin_follows_the_outermost_containers_location(
    conn: psycopg.Connection,
) -> None:
    from binkeeper.bin_inventory import bin_where, record_event
    from binkeeper.bin_nesting import record_bin_containment
    from binkeeper.bin_register import register_bin

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    for code in ("AGR-001", "AGR-010", "AGR-020"):
        register_bin(conn, bin_code=code, site="alameda-garage", observed_at=when)
    for child, container in (("AGR-001", "AGR-010"), ("AGR-010", "AGR-020")):
        record_bin_containment(
            conn,
            BinContainmentAppend(
                event_kind="pack",
                bin_code=child,
                container_code=container,
                occurred_at=when,
                idempotency_key=f"pack-{child}-into-{container}",
            ),
        )
    record_event(
        conn,
        event_kind="place",
        bin_code="AGR-020",
        site="alameda-storage",
        occurred_at=when,
        idempotency_key="move-outermost-container",
    )

    location = bin_where(conn, "AGR-001")
    assert location.site == "alameda-storage"
    assert location.container_code == "AGR-010"
    assert location.containment_path == ("AGR-010", "AGR-020")
    assert location.location_source_bin == "AGR-020"


def test_pack_rejects_bins_at_different_sites_without_appending(
    conn: psycopg.Connection,
) -> None:
    from binkeeper.bin_nesting import BinNestingError, record_bin_containment
    from binkeeper.bin_register import register_bin

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    register_bin(conn, bin_code="AGR-001", site="alameda-garage", observed_at=when)
    register_bin(conn, bin_code="AGR-010", site="alameda-storage", observed_at=when)

    with pytest.raises(BinNestingError, match="same known site"):
        record_bin_containment(
            conn,
            BinContainmentAppend(
                event_kind="pack",
                bin_code="AGR-001",
                container_code="AGR-010",
                occurred_at=when,
                idempotency_key="invalid-cross-site-pack",
            ),
        )

    assert conn.execute("SELECT count(*) FROM bin_containment_events").fetchone() == (0,)


def test_contained_bin_must_be_unpacked_before_a_direct_move(
    conn: psycopg.Connection,
) -> None:
    from binkeeper.bin_inventory import record_event
    from binkeeper.bin_nesting import BinNestingError, record_bin_containment
    from binkeeper.bin_register import register_bin

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    for code in ("AGR-001", "AGR-010"):
        register_bin(conn, bin_code=code, site="alameda-garage", observed_at=when)
    record_bin_containment(
        conn,
        BinContainmentAppend(
            event_kind="pack",
            bin_code="AGR-001",
            container_code="AGR-010",
            occurred_at=when,
            idempotency_key="pack-001-into-010",
        ),
    )

    with pytest.raises(BinNestingError, match="unpack"):
        record_event(
            conn,
            event_kind="place",
            bin_code="AGR-001",
            site="alameda-storage",
            occurred_at=when,
            idempotency_key="move-contained-bin",
        )


def test_unpack_preserves_the_container_site_and_is_idempotent(
    conn: psycopg.Connection,
) -> None:
    from binkeeper.bin_inventory import bin_where, record_event
    from binkeeper.bin_nesting import load_bin_containment, record_bin_containment
    from binkeeper.bin_register import register_bin

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    for code in ("AGR-001", "AGR-010"):
        register_bin(conn, bin_code=code, site="alameda-garage", observed_at=when)
    record_bin_containment(
        conn,
        BinContainmentAppend(
            event_kind="pack",
            bin_code="AGR-001",
            container_code="AGR-010",
            occurred_at=when,
            idempotency_key="pack-001-into-010",
        ),
    )
    record_event(
        conn,
        event_kind="place",
        bin_code="AGR-010",
        site="alameda-storage",
        occurred_at=when,
        idempotency_key="move-container",
    )

    first = record_bin_containment(
        conn,
        BinContainmentAppend(
            event_kind="unpack",
            bin_code="AGR-001",
            container_code="AGR-010",
            occurred_at=when,
            idempotency_key="unpack-001-from-010",
        ),
    )
    replay = record_bin_containment(
        conn,
        BinContainmentAppend(
            event_kind="unpack",
            bin_code="AGR-001",
            container_code="AGR-010",
            occurred_at=when,
            idempotency_key="unpack-001-from-010",
        ),
    )

    assert first.already_existed is False
    assert replay.event_id == first.event_id
    assert replay.already_existed is True
    assert load_bin_containment(conn).parent_of("AGR-001") is None
    assert bin_where(conn, "AGR-001").site == "alameda-storage"


def test_passport_projects_effective_site_and_containment_path(
    conn: psycopg.Connection,
) -> None:
    from binkeeper.bin_inventory import record_event
    from binkeeper.bin_nesting import record_bin_containment
    from binkeeper.bin_passport import bin_passport
    from binkeeper.bin_register import register_bin

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    for code in ("AGR-001", "AGR-010"):
        register_bin(conn, bin_code=code, site="alameda-garage", observed_at=when)
    record_bin_containment(
        conn,
        BinContainmentAppend(
            event_kind="pack",
            bin_code="AGR-001",
            container_code="AGR-010",
            occurred_at=when,
            idempotency_key="pack-001-into-010",
        ),
    )
    record_event(
        conn,
        event_kind="place",
        bin_code="AGR-010",
        site="alameda-storage",
        occurred_at=when,
        idempotency_key="move-container",
    )

    passport = bin_passport(conn, "AGR-001", now=when)
    assert passport.current_site == "alameda-storage"
    assert passport.container_code == "AGR-010"
    assert passport.containment_path == ("AGR-010",)


def test_container_passport_lists_its_immediate_contained_bins(
    conn: psycopg.Connection,
) -> None:
    from binkeeper.bin_nesting import record_bin_containment
    from binkeeper.bin_passport import bin_passport
    from binkeeper.bin_register import register_bin

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    for code in ("AGR-001", "AGR-002", "AGR-010"):
        register_bin(conn, bin_code=code, site="alameda-garage", observed_at=when)
    for child in ("AGR-001", "AGR-002"):
        record_bin_containment(
            conn,
            BinContainmentAppend(
                event_kind="pack",
                bin_code=child,
                container_code="AGR-010",
                occurred_at=when,
                idempotency_key=f"pack-{child}-into-010",
            ),
        )

    passport = bin_passport(conn, "AGR-010", now=when)
    assert passport.contained_bin_codes == ("AGR-001", "AGR-002")
