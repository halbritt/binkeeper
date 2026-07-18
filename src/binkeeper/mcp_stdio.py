"""Line-delimited JSON-RPC transport for the standalone MCP tools."""

from __future__ import annotations

import json
import sys

from binkeeper.database import connect
from binkeeper.mcp import call_tool, tool_schemas


def _error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def response_for_line(line: str) -> dict[str, object]:
    """Handle one request without allowing a bad request to stop the transport."""
    try:
        request = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return _error(None, -32700, "parse error")
    if not isinstance(request, dict):
        return _error(None, -32600, "invalid request")

    request_id = request.get("id")
    method = request.get("method")
    try:
        if method == "tools/list":
            result = {"tools": tool_schemas()}
        elif method == "tools/call":
            params = request.get("params", {})
            if not isinstance(params, dict):
                return _error(request_id, -32602, "invalid params")
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                return _error(request_id, -32602, "invalid params")
            with connect(role="owner") as conn:
                result = call_tool(conn, str(params.get("name", "")), arguments)
        elif method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "serverInfo": {"name": "binkeeper", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            }
        else:
            return _error(request_id, -32601, "method not found")
    except Exception:
        return _error(request_id, -32000, "tool execution failed")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> None:
    for line in sys.stdin:
        print(json.dumps(response_for_line(line)), flush=True)
