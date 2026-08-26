"""Red paths for `scripts/assert-gates-are-live-tools.py`.

This is the runtime half of the gate check, and it answers the objection that the
source-derived half cannot: it asks each connector for its **published** tool
metadata over the real MCP handshake and compares that, rather than inferring
write-ness from a `server.py`. A gate naming a tool no connector publishes arms
nothing, and nothing anywhere reports it -- the connector is healthy, the tool
runs, no approval card is ever posted.

The stub here speaks enough streamable-HTTP MCP to be a real client's peer: it
issues an `Mcp-Session-Id` on initialize and rejects later requests that omit it,
and it expects `notifications/initialized` before it will answer `tools/list`.
That matters, because an earlier version of the script skipped both and was only
ever tested against a stub that ignored them -- it returned HTTP 400 the first
time it met an actual FastMCP connector.
"""

import http.server
import json
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

StartConnector = Callable[[list[dict[str, object]]], str]

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "scripts" / "assert-gates-are-live-tools.py"

WRITE_TOOL: dict[str, object] = {
    "name": "restart_deployment",
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}
READ_TOOL: dict[str, object] = {
    "name": "search_traces",
    "annotations": {"readOnlyHint": True},
}
UNANNOTATED: dict[str, object] = {"name": "do_something"}


class _Handler(http.server.BaseHTTPRequestHandler):
    tools: list[dict[str, object]] = []
    require_session = True

    def log_message(self, *_args: object) -> None:  # keep pytest output clean
        pass

    def _send(
        self, code: int, payload: dict[str, object] | None, session: str | None = None
    ) -> None:
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        method = request.get("method")
        if method == "initialize":
            self._send(200, {"jsonrpc": "2.0", "id": request["id"], "result": {}}, session="s-1")
            return
        if self.require_session and self.headers.get("Mcp-Session-Id") != "s-1":
            # What a real streamable-HTTP server does to a client that forgot.
            self._send(400, {"error": "missing Mcp-Session-Id"})
            return
        if method == "notifications/initialized":
            self._send(202, None)
            return
        if method == "tools/list":
            self._send(
                200,
                {"jsonrpc": "2.0", "id": request["id"], "result": {"tools": self.tools}},
            )
            return
        self._send(404, {"error": f"unexpected method {method}"})


@pytest.fixture
def connector() -> Iterator[StartConnector]:
    """Start a stub connector; yields a factory returning its URL for given tools."""
    servers: list[http.server.HTTPServer] = []

    def start(tools: list[dict[str, object]]) -> str:
        handler = type("Handler", (_Handler,), {"tools": tools})
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{server.server_address[1]}/mcp"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def bundle(root: Path, gates: list[str]) -> Path:
    b = root / "bundle"
    (b / ".claude-plugin").mkdir(parents=True)
    (b / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "version": "0.0.1",
                "approvalPolicy": {"gates": [{"gate": g} for g in gates]},
            }
        ),
        encoding="utf-8",
    )
    return b


def run(bundle_dir: Path, *connectors: str) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(CHECKER), "--bundle", str(bundle_dir), "--timeout", "10"]
    for spec in connectors:
        args += ["--connector", spec]
    return subprocess.run(args, capture_output=True, text=True, check=False)


def test_gate_naming_a_published_write_tool_passes(
    tmp_path: Path, connector: StartConnector
) -> None:
    url = connector([WRITE_TOOL, READ_TOOL])
    b = bundle(tmp_path, ["mcp__k8s-write__restart_deployment"])
    r = run(b, f"k8s-write={url}")
    assert r.returncode == 0, r.stdout + r.stderr


def test_gate_naming_no_published_tool_fails_and_suggests_the_closest(
    tmp_path: Path, connector: StartConnector
) -> None:
    """The failure the source-derived check cannot see: a gate that arms nothing.

    Curie's own deploy-time error message recommends the plugin-prefixed form for
    connector tools, and following it produces a gate that validates, deploys, and
    never fires. That is this case.
    """
    url = connector([WRITE_TOOL])
    b = bundle(tmp_path, ["mcp__plugin_sre-bot_k8s-write__restart_deployment"])
    r = run(b, f"k8s-write={url}")
    assert r.returncode == 1, r.stdout
    assert "GATE ARMS NOTHING" in r.stderr, r.stderr
    assert "mcp__k8s-write__restart_deployment" in r.stderr, r.stderr


def test_published_write_tool_without_a_gate_fails(
    tmp_path: Path, connector: StartConnector
) -> None:
    url = connector([WRITE_TOOL, READ_TOOL])
    b = bundle(tmp_path, [])
    r = run(b, f"k8s-write={url}")
    assert r.returncode == 1, r.stdout
    assert "UNGATED WRITE TOOL" in r.stderr, r.stderr
    assert "mcp__k8s-write__restart_deployment" in r.stderr, r.stderr


def test_read_only_tools_need_no_gate(tmp_path: Path, connector: StartConnector) -> None:
    url = connector([READ_TOOL])
    b = bundle(tmp_path, [])
    r = run(b, f"tempo={url}")
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_tool_publishing_no_annotation_counts_as_write(
    tmp_path: Path, connector: StartConnector
) -> None:
    """Absent metadata is treated as needing a gate rather than as safe."""
    url = connector([UNANNOTATED])
    b = bundle(tmp_path, [])
    r = run(b, f"vague={url}")
    assert r.returncode == 1, r.stdout
    assert "mcp__vague__do_something" in r.stderr, r.stderr


def test_an_unreachable_connector_fails_rather_than_skipping(tmp_path: Path) -> None:
    """An unproven assertion is the thing this script exists to prevent assuming."""
    b = bundle(tmp_path, ["mcp__k8s-write__restart_deployment"])
    r = run(b, "k8s-write=http://127.0.0.1:1/mcp")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "could not list tools" in r.stderr, r.stderr


def test_the_session_handshake_is_actually_performed(
    tmp_path: Path, connector: StartConnector
) -> None:
    """The stub rejects any post without the session id it issued on initialize.

    So this passing means the script really does carry `Mcp-Session-Id` and send
    `notifications/initialized` -- the two steps whose absence made an earlier
    version return HTTP 400 against a real FastMCP connector while passing against
    a permissive stub.
    """
    url = connector([WRITE_TOOL])
    b = bundle(tmp_path, ["mcp__k8s-write__restart_deployment"])
    r = run(b, f"k8s-write={url}")
    assert r.returncode == 0, r.stdout + r.stderr
