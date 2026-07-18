"""Small standalone MCP tool contract using BinKeeper-owned names."""

from __future__ import annotations

import argparse
from typing import Any

import psycopg

from binkeeper.cli import execute


def tool_schemas() -> list[dict[str, Any]]:
    scope = {
        "tenant": {"type": "string", "default": "personal"},
        "corpus": {"type": "string", "default": "personal"},
    }
    return [
        {
            "name": "binkeeper.trip_scan",
            "description": "Append one physical move or trip event.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "place",
                            "open",
                            "load",
                            "arrive",
                            "close",
                            "confirm",
                            "contradict",
                        ],
                    },
                    "bin_code": {"type": ["string", "null"]},
                    "trip_id": {"type": ["string", "null"]},
                    "site": {"type": ["string", "null"]},
                    "from_site": {"type": ["string", "null"]},
                    "to_site": {"type": ["string", "null"]},
                    "idempotency_key": {"type": ["string", "null"]},
                    **scope,
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
        *[
            {
                "name": f"binkeeper.{name}",
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": {"bin_code": {"type": "string"}, **scope},
                    "required": ["bin_code"],
                    "additionalProperties": False,
                },
            }
            for name, description in (
                ("bin_where", "Fold the current location."),
                ("bin_belief", "Read location confidence and abstention."),
                ("bin_passport", "Render a provenance-bearing passport."),
            )
        ],
        {
            "name": "binkeeper.bin_search",
            "description": "Run local exact and lexical inventory search.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    **scope,
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    ]


def call_tool(conn: psycopg.Connection, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    command = name.removeprefix("binkeeper.").replace("_", "-")
    namespace = argparse.Namespace(command=command, tenant="personal", corpus="personal")
    for key, value in arguments.items():
        setattr(namespace, key, value)
    if command == "trip-scan":
        for key in (
            "bin_code",
            "trip_id",
            "site",
            "from_site",
            "to_site",
            "occurred_at",
            "idempotency_key",
        ):
            if not hasattr(namespace, key):
                setattr(namespace, key, None)
        namespace.source_label = "mcp"
    elif command == "bin-search" and not hasattr(namespace, "limit"):
        namespace.limit = 20
    return execute(namespace, conn)
