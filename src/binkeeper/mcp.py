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
                            "fetch",
                            "not_found",
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
        {
            "name": "binkeeper.bin_route",
            "description": "Advisory: route one typed item text over bin passports at a site.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "site": {"type": "string"},
                    **scope,
                },
                "required": ["text", "site"],
                "additionalProperties": False,
            },
        },
        {
            "name": "binkeeper.bin_stash_route",
            "description": "Advisory: route a batch of item texts and compile a wave plan.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "site": {"type": "string"},
                    **scope,
                },
                "required": ["items", "site"],
                "additionalProperties": False,
            },
        },
        {
            "name": "binkeeper.bin_placement_decision",
            "description": "Append one owner placement decision over a route receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "string"},
                    "decision": {
                        "type": "string",
                        "enum": [
                            "accept",
                            "reject",
                            "override",
                            "split",
                            "merge",
                            "not_an_item",
                            "create_new_bin",
                        ],
                    },
                    "selected_bin": {"type": ["string", "null"]},
                    "actor": {"type": "string", "default": "owner"},
                    "reason": {"type": ["string", "null"]},
                    "idempotency_key": {"type": ["string", "null"]},
                    **scope,
                },
                "required": ["request_id", "decision"],
                "additionalProperties": False,
            },
        },
        {
            "name": "binkeeper.bin_sweep",
            "description": "List the stalest bins to re-confirm, regret-ranked.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "site": {"type": ["string", "null"]},
                    "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 100},
                    **scope,
                },
                "required": [],
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
    elif command == "bin-placement-decision":
        for key in ("selected_bin", "reason", "idempotency_key"):
            if not hasattr(namespace, key):
                setattr(namespace, key, None)
        if not hasattr(namespace, "actor"):
            namespace.actor = "owner"
    elif command == "bin-sweep":
        for key in ("site", "limit"):
            if not hasattr(namespace, key):
                setattr(namespace, key, None)
    elif command == "bin-stash-route" and not hasattr(namespace, "items_file"):
        namespace.items_file = None
    return execute(namespace, conn)
