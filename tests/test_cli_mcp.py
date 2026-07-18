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


def test_cli_and_mcp_share_idempotent_trip_behavior(conn: psycopg.Connection) -> None:
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
