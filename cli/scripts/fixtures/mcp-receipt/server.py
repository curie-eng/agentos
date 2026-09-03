#!/usr/bin/env python3
"""Deterministic read-only MCP server used by the correlation ladder.

This fixture deliberately uses only the Python standard library so its image
does not acquire an independent MCP dependency. It implements the stateful
Streamable HTTP handshake used by the runner: initialize, the initialized
notification, a session-scoped SSE GET, tools/list, tools/call, and DELETE.

The only durable receipt is a countable marker. Request bodies, tool arguments,
session identifiers, and headers are deliberately never written to stdout.
Reachability is restricted by the hosted connector's network boundary; the
receipt marker is evidence of a call, not an authentication mechanism.
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_REQUEST_BYTES = 1_048_576
RUNNER_PROTOCOL_VERSION = "2025-03-26"
SUPPORTED_PROTOCOL_VERSIONS = frozenset(
    {
        "2024-11-05",
        RUNNER_PROTOCOL_VERSION,
        "2025-06-18",
        "2025-11-25",
    }
)
DEFAULT_PROTOCOL_VERSION = "2025-11-25"
SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"

TOOL = {
    "name": "receipt_read",
    "description": "Return a deterministic, non-sensitive read receipt.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "annotations": {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


@dataclass
class Session:
    protocol_version: str
    initialized: bool = False
    terminated: threading.Event = field(default_factory=threading.Event)


_sessions: dict[str, Session] = {}
_sessions_lock = threading.RLock()


def _jsonrpc_error(
    request_id: str | int | None, code: int, message: str
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _new_session(protocol_version: str) -> tuple[str, Session]:
    session_id = secrets.token_urlsafe(24)
    session = Session(protocol_version=protocol_version)
    with _sessions_lock:
        _sessions[session_id] = session
    return session_id, session


def _get_session(session_id: str | None) -> Session | None:
    if not session_id:
        return None
    with _sessions_lock:
        return _sessions.get(session_id)


def _terminate_session(session_id: str) -> bool:
    with _sessions_lock:
        session = _sessions.pop(session_id, None)
    if session is None:
        return False
    session.terminated.set()
    return True


def response_for(message: dict[str, Any], session: Session) -> dict[str, Any] | None:
    """Return one JSON-RPC response, or ``None`` for a notification."""

    method = message.get("method")
    if method == "notifications/initialized" and "id" not in message:
        session.initialized = True
        return None

    if "id" not in message or message.get("id") is None:
        # Notifications other than initialized are accepted without a response.
        return None

    request_id = message["id"]
    if not isinstance(request_id, str | int) or isinstance(request_id, bool):
        return _jsonrpc_error(None, -32600, "invalid request id")
    if not session.initialized:
        return _jsonrpc_error(request_id, -32002, "session is not initialized")
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [TOOL]}}
    if method == "tools/call":
        params = message.get("params")
        arguments = params.get("arguments", {}) if isinstance(params, dict) else None
        if (
            not isinstance(params, dict)
            or params.get("name") != TOOL["name"]
            or arguments not in ({}, None)
        ):
            return _jsonrpc_error(request_id, -32602, "unknown or invalid read-only tool")
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
    return _jsonrpc_error(request_id, -32601, "method not found")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *args: object) -> None:
        # The base implementation includes the request target and client IP.
        return

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any] | list[dict[str, Any]],
        *,
        session_id: str | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if session_id is not None:
            self.send_header(SESSION_HEADER, session_id)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(
        self, status: HTTPStatus, *, session_id: str | None = None
    ) -> None:
        self.send_response(status)
        if session_id is not None:
            self.send_header(SESSION_HEADER, session_id)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _require_path(self) -> bool:
        if self.path == "/mcp":
            return True
        self._send_json(
            HTTPStatus.NOT_FOUND,
            _jsonrpc_error(None, -32601, "MCP endpoint not found"),
        )
        return False

    def _accepts(self, content_type: str) -> bool:
        accept = self.headers.get("Accept", "")
        return "*/*" in accept or content_type in accept

    def _require_session(self) -> tuple[str, Session] | None:
        session_id = self.headers.get(SESSION_HEADER)
        session = _get_session(session_id)
        if session_id is None:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                _jsonrpc_error(None, -32600, "missing MCP session"),
            )
            return None
        if session is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                _jsonrpc_error(None, -32600, "unknown MCP session"),
            )
            return None
        requested_version = self.headers.get(PROTOCOL_HEADER)
        if requested_version != session.protocol_version:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                _jsonrpc_error(None, -32600, "invalid MCP protocol version"),
                session_id=session_id,
            )
            return None
        return session_id, session

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._require_path():
            return
        if not self._accepts("text/event-stream"):
            self._send_json(
                HTTPStatus.NOT_ACCEPTABLE,
                _jsonrpc_error(None, -32600, "SSE response is not accepted"),
            )
            return
        required = self._require_session()
        if required is None:
            return
        session_id, session = required

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header(SESSION_HEADER, session_id)
        self.end_headers()
        try:
            # A comment commits the response as SSE without fabricating a JSON-RPC
            # notification. There are no server-initiated messages in this fixture.
            self.wfile.write(b": receipt stream\n\n")
            self.wfile.flush()
            while not session.terminated.wait(timeout=10):
                self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._require_path():
            return
        if not self._accepts("application/json") or not self._accepts(
            "text/event-stream"
        ):
            self._send_json(
                HTTPStatus.NOT_ACCEPTABLE,
                _jsonrpc_error(None, -32600, "MCP response types are not accepted"),
            )
            return
        if (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip()
            != "application/json"
        ):
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                _jsonrpc_error(None, -32600, "expected application/json"),
            )
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
            decoded = json.loads(self.rfile.read(length))
            messages = decoded if isinstance(decoded, list) else [decoded]
            if not messages or not all(isinstance(item, dict) for item in messages):
                raise ValueError("request is not a JSON-RPC object")
        except (json.JSONDecodeError, TypeError, ValueError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                _jsonrpc_error(None, -32700, "invalid request"),
            )
            return

        initialize = len(messages) == 1 and messages[0].get("method") == "initialize"
        if initialize:
            message = messages[0]
            request_id = message.get("id")
            params = message.get("params")
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            if request_id is None or not isinstance(requested, str):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    _jsonrpc_error(request_id, -32602, "invalid initialize request"),
                )
                return
            negotiated = (
                requested
                if requested in SUPPORTED_PROTOCOL_VERSIONS
                else DEFAULT_PROTOCOL_VERSION
            )
            session_id, _session = _new_session(negotiated)
            self._send_json(
                HTTPStatus.OK,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": negotiated,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "curie-mcp-receipt", "version": "1"},
                    },
                },
                session_id=session_id,
            )
            return

        required = self._require_session()
        if required is None:
            return
        session_id, session = required
        replies = [
            reply
            for item in messages
            if (reply := response_for(item, session)) is not None
        ]
        if not replies:
            self._send_empty(HTTPStatus.ACCEPTED, session_id=session_id)
            return
        payload: dict[str, Any] | list[dict[str, Any]] = (
            replies if isinstance(decoded, list) else replies[0]
        )
        self._send_json(HTTPStatus.OK, payload, session_id=session_id)

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._require_path():
            return
        required = self._require_session()
        if required is None:
            return
        session_id, _session = required
        _terminate_session(session_id)
        self._send_empty(HTTPStatus.NO_CONTENT)


if __name__ == "__main__":
    # Hosted connector NetworkPolicy is the isolation boundary, so the process
    # must listen on the pod/container interface rather than loopback.
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
