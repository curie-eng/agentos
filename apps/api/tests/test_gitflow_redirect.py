"""AC4 proof by execution: a cross-host redirect gets no request from the clone.

A separate file because this pair needs a REAL `git` subprocess and two loopback
HTTP servers, and needs neither Postgres nor RustFS. `test_gitflow_unit.py` mocks
`subprocess.run` as its contract, and `test_gitflow_integration.py` requires the
database fixtures and declares "no github.com and no network" in its header.
Everything here binds 127.0.0.1 only; nothing leaves the box.

The setup is an *origin* server that 302-redirects every request to a *different
hostname* (`localhost`, which resolves to the same loopback address but is a
distinct host to git), and a *target* server that records every request it
receives. The load-bearing assertion is that the target's request list is empty:
strictly stronger than "the target received no Authorization header".
"""

import os
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from curie_api.config import Settings
from curie_api.gitflow import GitFlowError, clone_and_archive

_REPO = "octo/demo-agent"
_VALID_SHA1 = "a" * 40


@dataclass
class _Servers:
    origin_port: int
    target_port: int
    origin_requests: list[str]
    target_requests: list[str]


def _origin_handler(
    recorded: list[str], target_port: int
) -> type[BaseHTTPRequestHandler]:
    class _Origin(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
            safe_path = self.path.replace("\r", "").replace("\n", "")
            recorded.append(safe_path)
            # A different HOSTNAME, same loopback address: cross-host from git's
            # point of view, and reachable without any external network.
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{target_port}{safe_path}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    return _Origin


def _target_handler(recorded: list[str]) -> type[BaseHTTPRequestHandler]:
    class _Target(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
            recorded.append(self.path)
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return _Target


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's proxy or personal gitconfig out of these assertions.

    A proxy or a global `http.followRedirects` would otherwise decide where the
    loopback requests actually go, and both tests would be measuring the machine
    rather than the code.
    """

    for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


@pytest.fixture
def redirect_servers() -> Iterator[_Servers]:
    origin_requests: list[str] = []
    target_requests: list[str] = []

    target = ThreadingHTTPServer(("127.0.0.1", 0), _target_handler(target_requests))
    target_port = int(target.server_address[1])
    origin = ThreadingHTTPServer(
        ("127.0.0.1", 0), _origin_handler(origin_requests, target_port)
    )
    origin_port = int(origin.server_address[1])

    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (target, origin)
    ]
    for thread in threads:
        thread.start()
    try:
        yield _Servers(origin_port, target_port, origin_requests, target_requests)
    finally:
        for server in (origin, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)


def test_a_cross_host_redirect_receives_no_request_from_the_clone(
    redirect_servers: _Servers,
) -> None:
    """The pinned clone treats a redirect as an error, so nothing reaches the target.

    Grounded in `git help config` (git version 2.43.0, the version in this
    worktree), on `http.followRedirects`: "If set to initial, git will follow
    redirects only for the initial request to a remote, but not for subsequent
    follow-up HTTP requests. ... The default is initial." The request that
    carries the `http.<url>.extraheader` credential IS that initial request, so
    git's default is not safe here; `-c http.followRedirects=false` makes any
    redirect a hard error instead.

    Deliberately NOT asserted: that the target saw no Authorization header.
    `_clone_credential_env` withholds the credential from every non-`https://`
    URL, so over loopback http that assertion would pass without exercising
    anything. The https case is covered by the chain: zero redirects followed
    (this test), the credential is https-only
    (`test_clone_does_not_send_the_credential_over_plain_http`), the header is
    keyed to the derived origin
    (`test_clone_credential_is_keyed_to_the_derived_origin`), and git is only
    ever handed the derived URL
    (`test_clone_hands_git_the_derived_origin_not_the_payload_url`).
    """

    base = f"http://127.0.0.1:{redirect_servers.origin_port}"
    settings = Settings(github_clone_base=base, github_token="ghs-secret-token")
    payload_url = f"{base}/{_REPO}.git"

    with pytest.raises(GitFlowError):
        clone_and_archive(payload_url, _VALID_SHA1, settings, repo_full_name=_REPO)

    # Non-vacuity: git really did reach the origin and really was handed a 302.
    assert redirect_servers.origin_requests != [], "git never reached the origin"
    assert redirect_servers.target_requests == []


def test_git_follows_the_initial_redirect_without_the_pin(
    redirect_servers: _Servers, tmp_path: Path
) -> None:
    """Control: an unpinned git DOES reach the other host, so the pin is the cause.

    Same grounding as above: `git help config` says of `http.followRedirects`
    that "The default is initial", and the clone's first request to a remote is
    exactly an initial request. This test is the observed behavior the fix
    defends against. Without it, the pinned test would pass just as happily
    against a server that never redirected at all.
    """

    origin_url = f"http://127.0.0.1:{redirect_servers.origin_port}/{_REPO}.git"
    subprocess.run(
        ["git", "clone", "--quiet", "--mirror", origin_url, str(tmp_path / "mirror")],
        check=False,
        capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        timeout=60,
    )

    # Both assertions matter: the first proves the redirect actually fired, so a
    # green run can never mean "the servers were never exercised".
    assert redirect_servers.origin_requests != [], "git never reached the origin"
    assert redirect_servers.target_requests != []
