#!/usr/bin/env python3
"""Deterministic read-only MCP server used by the correlation ladder.

The only durable receipt is a countable marker. Request bodies, tool arguments,
session identifiers, and headers are deliberately never written to stdout.
"""

from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


TOOL = {
    "name": "receipt_read",
    "description": "Return a deterministic, non-sensitive read receipt.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}


def response_for(message: dict[str, Any]) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    if request_id is None:
        return None
    if method == "initialize":
        requested = message.get("params", {}).get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": requested or "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "curie-mcp-receipt", "version": "1"},
            },
        }
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [TOOL]}}
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or params.get("name") != TOOL["name"]:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "unknown read-only tool"},
            }
        print("MCP_RECEIPT tools/call", flush=True)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": "receipt-ok"}],
                "structuredContent": {"receipt": "ok"},
                "isError": False,
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": "method not found"},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *args: object) -> None:
        # The base implementation includes the request target and client IP.
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_048_576:
                raise ValueError("invalid request size")
            decoded = json.loads(self.rfile.read(length))
            messages = decoded if isinstance(decoded, list) else [decoded]
            if not messages or not all(isinstance(item, dict) for item in messages):
                raise ValueError("request is not a JSON-RPC object")
            replies = [reply for item in messages if (reply := response_for(item)) is not None]
        except (json.JSONDecodeError, TypeError, ValueError):
            body = json.dumps(
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "invalid request"}},
                separators=(",", ":"),
            ).encode()
            self.send_response(HTTPStatus.BAD_REQUEST)
        else:
            if not replies:
                self.send_response(HTTPStatus.ACCEPTED)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload: object = replies if isinstance(decoded, list) else replies[0]
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
