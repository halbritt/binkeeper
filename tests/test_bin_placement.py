"""RFC 0093 P2a tests for append-only placement routing receipts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from binkeeper.bin_inventory import bin_where, record_event
from binkeeper.bin_placement import (
    PlacementDecisionAppend,
    PlacementLedgerError,
    TextRoutingRequest,
    record_placement_decision,
    record_text_routing_request,
)

NOW = datetime(2026, 7, 2, 17, 0, 0, tzinfo=UTC)


def _insert_bin_capture(
    conn: psycopg.Connection,
    *,
    external_id: str,
    bin_code: str,
    site: str,
    contents_text: str,
    accepts: tuple[str, ...],
    capacity_state: str = "half",
) -> None:
    metadata: dict[str, object] = {
        "kind": "bin_capture",
        "schema_version": "bin_capture.v1",
        "bin_code": bin_code,
        "site": site,
        "captured_at": NOW.isoformat(),
        "contents_text": contents_text,
        "bin_profile": {
            "theme": contents_text,
            "accepts": list(accepts),
            "capacity_state": capacity_state,
        },
    }
    source_row = conn.execute(
        "INSERT INTO sources (source_kind, external_id, raw_payload) "
        "VALUES ('capture', %s, '{}') RETURNING id",
        (f"placement-src-{external_id}",),
    ).fetchone()
    assert source_row is not None
    conn.execute(
        """
        INSERT INTO captures (
            source_id, source_kind, external_id, raw_payload,
            capture_type, content_text, observed_at
        )
        VALUES (%s, 'capture', %s, %s, 'observation', %s, %s)
        """,
        (
            source_row[0],
            external_id,
            Jsonb({"metadata": metadata}),
            f"bin {bin_code} @ {site} - {contents_text}",
            NOW,
        ),
    )


def _place_bin(conn: psycopg.Connection, bin_code: str, site: str) -> None:
    record_event(
        conn,
        event_kind="place",
        bin_code=bin_code,
        site=site,
        occurred_at=NOW,
        idempotency_key=f"place-{bin_code}",
    )


def test_text_route_receipt_is_idempotent_and_keeps_snapshot(
    conn: psycopg.Connection,
) -> None:
    _insert_bin_capture(
        conn,
        external_id="placement-cap-usb",
        bin_code="PLC-001",
        site="alameda-garage",
        contents_text="electronics cables",
        accepts=("USB-C cables", "HDMI adapters"),
    )
    _place_bin(conn, "PLC-001", "alameda-garage")

    request = TextRoutingRequest(
        text="USB-C cable",
        site="alameda-garage",
        requested_at=NOW,
        idempotency_key="route-usb-c",
    )

    first = record_text_routing_request(conn, request)
    second = record_text_routing_request(conn, request)

    assert first.already_existed is False
    assert second.already_existed is True
    assert second.routing_request_id == first.routing_request_id
    assert second.route_result_sha256 == first.route_result_sha256
    assert first.route_result["recommended_bin_code"] == "PLC-001"
    assert conn.execute("SELECT count(*) FROM bin_routing_requests").fetchone() == (1,)


def test_owner_decision_is_idempotent_and_never_moves_bin(conn: psycopg.Connection) -> None:
    _insert_bin_capture(
        conn,
        external_id="placement-cap-fastener",
        bin_code="PLC-002",
        site="alameda-garage",
        contents_text="small fasteners",
        accepts=("machine screws", "washers"),
    )
    _place_bin(conn, "PLC-002", "alameda-garage")
    before_location = bin_where(conn, "PLC-002").to_json()

    route = record_text_routing_request(
        conn,
        TextRoutingRequest(
            text="machine screw",
            site="alameda-garage",
            requested_at=NOW,
            idempotency_key="route-machine-screw",
        ),
    )
    decision = PlacementDecisionAppend(
        routing_request_id=route.routing_request_id,
        decision_kind="accept",
        decided_at=NOW + timedelta(minutes=1),
        idempotency_key="accept-machine-screw",
    )

    first = record_placement_decision(conn, decision)
    second = record_placement_decision(conn, decision)

    assert first.already_existed is False
    assert second.already_existed is True
    assert second.placement_decision_id == first.placement_decision_id
    assert first.recommended_bin_code == "PLC-002"
    assert first.selected_bin_code == "PLC-002"
    assert bin_where(conn, "PLC-002").to_json() == before_location
    assert conn.execute("SELECT count(*) FROM bin_placement_decisions").fetchone() == (1,)


def test_accept_decision_refuses_abstained_route(conn: psycopg.Connection) -> None:
    _insert_bin_capture(
        conn,
        external_id="placement-cap-paint",
        bin_code="PLC-003",
        site="alameda-garage",
        contents_text="paint supplies",
        accepts=("rollers", "brushes"),
    )
    _place_bin(conn, "PLC-003", "alameda-garage")
    route = record_text_routing_request(
        conn,
        TextRoutingRequest(
            text="unlabeled ceramic mystery puck",
            site="alameda-garage",
            requested_at=NOW,
            idempotency_key="route-abstained",
        ),
    )

    with pytest.raises(PlacementLedgerError, match="non-abstained recommendation"):
        record_placement_decision(
            conn,
            PlacementDecisionAppend(
                routing_request_id=route.routing_request_id,
                decision_kind="accept",
                decided_at=NOW + timedelta(minutes=1),
                idempotency_key="accept-abstained",
            ),
        )


def test_placement_ledgers_are_append_only(conn: psycopg.Connection) -> None:
    _insert_bin_capture(
        conn,
        external_id="placement-cap-tools",
        bin_code="PLC-004",
        site="alameda-garage",
        contents_text="hand tools",
        accepts=("pliers", "wrenches"),
    )
    _place_bin(conn, "PLC-004", "alameda-garage")
    route = record_text_routing_request(
        conn,
        TextRoutingRequest(
            text="pliers",
            site="alameda-garage",
            requested_at=NOW,
            idempotency_key="route-pliers",
        ),
    )
    record_placement_decision(
        conn,
        PlacementDecisionAppend(
            routing_request_id=route.routing_request_id,
            decision_kind="accept",
            decided_at=NOW + timedelta(minutes=1),
            idempotency_key="accept-pliers",
        ),
    )

    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE bin_routing_requests SET site = 'moved'")
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM bin_placement_decisions")
