from __future__ import annotations

import json

import psycopg
import pytest

from binkeeper.cli import build_parser, execute
from binkeeper.mcp import call_tool, tool_schemas
from binkeeper.mcp_stdio import response_for_line


def test_cli_help_names_standalone_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit, match="0"):
        build_parser().parse_args(["--help"])
    help_text = capsys.readouterr().out
    assert "trip-scan" in help_text
    assert "bin-passport" in help_text
    assert "bin-search" in help_text
    assert "bin-placement-decision" in help_text
    assert "engram" not in help_text.lower()


def test_cli_rejects_unknown_trip_action() -> None:
    with pytest.raises(SystemExit, match="2"):
        build_parser().parse_args(["trip-scan", "--action", "teleport"])


def test_mcp_schema_snapshot_uses_only_binkeeper_names() -> None:
    schemas = tool_schemas()
    assert [schema["name"] for schema in schemas] == [
        "binkeeper.trip_scan",
        "binkeeper.bin_where",
        "binkeeper.bin_belief",
        "binkeeper.bin_passport",
        "binkeeper.bin_search",
        "binkeeper.bin_route",
        "binkeeper.bin_stash_route",
        "binkeeper.bin_placement_decision",
        "binkeeper.bin_sweep",
    ]
    rendered = json.dumps(schemas, sort_keys=True)
    assert "engram" not in rendered.lower()
    assert all(schema["inputSchema"]["additionalProperties"] is False for schema in schemas)


def test_mcp_stdio_error_responses_are_stable() -> None:
    assert response_for_line("not-json") == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "parse error"},
    }
    assert response_for_line('{"jsonrpc":"2.0","id":7,"method":"missing"}') == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32601, "message": "method not found"},
    }


def test_cli_write_refuses_before_mutating_when_writer_authority_is_closed(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BINKEEPER_WRITES_ENABLED", raising=False)
    args = build_parser().parse_args(
        ["trip-scan", "--action", "place", "--bin", "TST-FROZEN", "--site", "site-a"]
    )

    with pytest.raises(RuntimeError, match="writer is frozen"):
        execute(args, conn)

    assert conn.execute(
        "SELECT count(*) FROM bin_trip_events WHERE bin_code = 'TST-FROZEN'"
    ).fetchone() == (0,)


def test_mcp_write_uses_the_same_frozen_writer_gate(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BINKEEPER_WRITES_ENABLED", raising=False)

    with pytest.raises(RuntimeError, match="writer is frozen"):
        call_tool(
            conn,
            "binkeeper.trip_scan",
            {"action": "place", "bin_code": "TST-MCP-FROZEN", "site": "site-a"},
        )

    assert conn.execute(
        "SELECT count(*) FROM bin_trip_events WHERE bin_code = 'TST-MCP-FROZEN'"
    ).fetchone() == (0,)


def test_cli_and_mcp_share_idempotent_trip_behavior(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BINKEEPER_WRITES_ENABLED", "1")
    args = build_parser().parse_args(
        [
            "trip-scan",
            "--action",
            "place",
            "--bin",
            "TST-CLI",
            "--site",
            "site-a",
            "--idempotency-key",
            "same-action",
        ]
    )
    cli_result = execute(args, conn)
    mcp_result = call_tool(
        conn,
        "binkeeper.trip_scan",
        {
            "action": "place",
            "bin_code": "TST-CLI",
            "site": "site-a",
            "idempotency_key": "same-action",
        },
    )
    assert cli_result["event_id"] == mcp_result["event_id"]
    assert cli_result["already_existed"] is False
    assert mcp_result["already_existed"] is True


def test_compat_trip_arrive_without_bin_reconciles_every_loaded_bin(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BINKEEPER_WRITES_ENABLED", "1")
    for argv in (
        ["trip-scan", "--action", "open", "--trip", "TRIP-COMPAT", "--to-site", "site-b"],
        ["trip-scan", "--action", "load", "--trip", "TRIP-COMPAT", "--bin", "TST-A"],
        ["trip-scan", "--action", "load", "--trip", "TRIP-COMPAT", "--bin", "TST-B"],
    ):
        execute(build_parser().parse_args(argv), conn)

    result = execute(
        build_parser().parse_args(["trip-scan", "--action", "arrive", "--trip", "TRIP-COMPAT"]),
        conn,
    )

    assert len(result["arrived"]) == 2
    assert result["trip"]["arrived"] == ["TST-A", "TST-B"]
    assert result["trip"]["unaccounted"] == []


def test_mcp_route_sweep_and_stash_are_read_only_callable(conn: psycopg.Connection) -> None:
    route = call_tool(conn, "binkeeper.bin_route", {"text": "usb cable", "site": "site-a"})
    assert route["item_card"]["label"] == "usb cable"
    sweep = call_tool(conn, "binkeeper.bin_sweep", {})
    assert "items" in sweep
    stash = call_tool(
        conn, "binkeeper.bin_stash_route", {"items": ["usb cable", "zip ties"], "site": "site-a"}
    )
    assert stash["item_count"] == 2
    assert stash["deck_count"] + stash["pending_count"] == 2


def test_mcp_placement_decision_uses_the_frozen_writer_gate(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BINKEEPER_WRITES_ENABLED", raising=False)
    with pytest.raises(RuntimeError, match="writer is frozen"):
        call_tool(
            conn,
            "binkeeper.bin_placement_decision",
            {"request_id": "00000000-0000-0000-0000-000000000000", "decision": "reject"},
        )
    assert conn.execute("SELECT count(*) FROM bin_placement_decisions").fetchone() == (0,)
