"""A large bundle upload must not stall GET /health on the uvicorn worker.

``deploy.validate_archive`` extracts the archive to a temp dir and runs
``plugin_format.validate_bundle``. Called directly from the async upload
handler, that work blocked the event loop, so every other request on the
worker waited. This drives a real one-worker uvicorn with an archive that
takes at least 200 ms to validate, polls GET /health concurrently, and
requires health p99 during validation to stay under 50 ms.

Health samples are restricted to requests that overlap the validation
window. p99 of the whole PUT would be dominated by body-transfer and
object-store samples, which already run off the loop, and would hide the
stall this test exists to catch.
"""

from __future__ import annotations

import io
import logging
import os
import socket
import tarfile
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from curie_api import deploy
from curie_api.main import create_app

MANIFEST = '{"name": "demo-plugin", "version": "0.1.0"}'
_MIN_VALIDATE_S = 0.200
_HEALTH_P99_S = 0.050
_PROBE_THREADS = 4
_PROBE_INTERVAL_S = 0.005
_MAX_PAD_BYTES = 150 * 1024 * 1024
_MAX_MEMBERS = 8000


def _skill(name: str) -> bytes:
    return f"---\nname: {name}\ndescription: does {name} things\n---\n\n# {name}\n".encode()


def _tar_plain(files: dict[str, bytes], top: str = "demo-plugin") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for rel, content in files.items():
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _archive(n_extra: int, pad_bytes: int) -> bytes:
    files: dict[str, bytes] = {
        ".claude-plugin/plugin.json": MANIFEST.encode(),
        "skills/alpha/SKILL.md": _skill("alpha"),
    }
    for i in range(n_extra):
        files[f"assets/p{i}.txt"] = f"{i}\n".encode()
    if pad_bytes:
        chunk = os.urandom(1024)
        files["assets/pad.bin"] = (chunk * ((pad_bytes // 1024) + 1))[:pad_bytes]
    return _tar_plain(files)


def _slow_archive() -> tuple[bytes, float]:
    """Grow an archive until ``validate_archive`` takes at least 200 ms."""

    n_extra = 4000
    pad_bytes = 0
    last_elapsed = 0.0
    archive = b""
    while True:
        archive = _archive(n_extra, pad_bytes)
        started = time.perf_counter()
        deploy.validate_archive(archive)
        last_elapsed = time.perf_counter() - started
        if last_elapsed >= _MIN_VALIDATE_S:
            return archive, last_elapsed
        if n_extra < _MAX_MEMBERS:
            n_extra = min(_MAX_MEMBERS, n_extra * 2)
            continue
        if pad_bytes == 0:
            pad_bytes = 16 * 1024 * 1024
            continue
        if pad_bytes < _MAX_PAD_BYTES:
            pad_bytes = min(_MAX_PAD_BYTES, pad_bytes * 2)
            continue
        raise AssertionError(
            f"could not build an archive that takes {_MIN_VALIDATE_S:.3f}s to "
            f"validate (last {last_elapsed:.3f}s, members={n_extra}, pad={pad_bytes})"
        )


def _percentile(samples: list[float], p: float) -> float:
    if not samples:
        raise AssertionError("no health samples overlapped validation")
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), int((p / 100) * len(ordered) + 0.999999)))
    return ordered[rank - 1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _UvicornThread(threading.Thread):
    def __init__(self, app: Any, host: str, port: int) -> None:
        super().__init__(name="curie-api-offloop", daemon=True)
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
                lifespan="on",
            )
        )

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


@pytest.fixture
def live_api(_disposable_db: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A one-worker uvicorn serving ``create_app`` on a free loopback port."""

    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    host = "127.0.0.1"
    port = _free_port()
    thread = _UvicornThread(create_app(), host, port)
    thread.start()
    deadline = time.time() + 30
    url = f"http://{host}:{port}"
    last_exc: Exception | None = None
    while time.time() < deadline:
        if thread.server.started:
            try:
                response = httpx.get(f"{url}/health", timeout=1.0)
                if response.status_code == 200:
                    break
            except httpx.HTTPError as exc:
                last_exc = exc
        time.sleep(0.05)
    else:
        thread.stop()
        thread.join(timeout=5)
        raise RuntimeError(f"uvicorn did not become healthy: {last_exc}")
    try:
        yield url
    finally:
        thread.stop()
        thread.join(timeout=15)


def _create_version(http: httpx.Client, headers: dict[str, str]) -> tuple[str, str]:
    agent = http.post(
        "/agents",
        json={
            "name": "archive-offloop-agent",
            "channel": {"kind": "slack", "address": "C0EXAMPLE1"},
        },
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    version = http.post(
        f"/agents/{agent.json()['id']}/versions",
        json={"version_label": "v1", "created_by": "bconn"},
        headers=headers,
    )
    assert version.status_code == 201, version.text
    return agent.json()["id"], version.json()["id"]


def test_health_p99_stays_under_50ms_during_a_slow_bundle_upload(
    live_api: str,
    auth_headers: dict[str, str],
    clean_db: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, validate_s = _slow_archive()
    assert validate_s >= _MIN_VALIDATE_S

    window = {"start": 0.0, "end": 0.0}
    real_validate = deploy.validate_archive

    def _timed_validate(*args: Any, **kwargs: Any) -> tuple[str, str]:
        window["start"] = time.perf_counter()
        try:
            return real_validate(*args, **kwargs)
        finally:
            window["end"] = time.perf_counter()

    monkeypatch.setattr(deploy, "validate_archive", _timed_validate)

    samples: list[tuple[float, float]] = []
    stop = threading.Event()
    probe_errors: list[str] = []
    log = logging.getLogger("curie_api")
    previous_level = log.level
    log.setLevel(logging.ERROR)

    def _probe() -> None:
        with httpx.Client(base_url=live_api, timeout=30.0) as probe:
            try:
                warmup = probe.get("/health")
            except httpx.HTTPError as exc:
                probe_errors.append(str(exc))
                return
            if warmup.status_code != 200:
                probe_errors.append(f"health {warmup.status_code}")
                return
            while not stop.is_set():
                started = time.perf_counter()
                try:
                    response = probe.get("/health")
                except httpx.HTTPError as exc:
                    probe_errors.append(str(exc))
                    return
                finished = time.perf_counter()
                if response.status_code != 200:
                    probe_errors.append(f"health {response.status_code}")
                    return
                samples.append((started, finished))
                if stop.wait(_PROBE_INTERVAL_S):
                    break

    probers = [threading.Thread(target=_probe, daemon=True) for _ in range(_PROBE_THREADS)]
    headers = auth_headers
    try:
        with httpx.Client(base_url=live_api, timeout=180.0) as http:
            agent_id, version_id = _create_version(http, headers)
            warmup = http.get("/health")
            assert warmup.status_code == 200
            for thread in probers:
                thread.start()
            time.sleep(0.05)
            response = http.put(
                f"/agents/{agent_id}/versions/{version_id}/bundle",
                files={"file": ("demo.tar", archive)},
                headers=headers,
            )
    finally:
        stop.set()
        for thread in probers:
            if thread.is_alive() or thread.ident is not None:
                thread.join(timeout=10)
        log.setLevel(previous_level)

    assert not probe_errors, probe_errors
    assert response.status_code == 201, response.text
    assert window["start"] > 0 and window["end"] >= window["start"]
    observed_validate_s = window["end"] - window["start"]
    assert observed_validate_s >= _MIN_VALIDATE_S, (
        f"upload-path validate_archive took {observed_validate_s * 1000:.1f}ms, "
        f"need >= {_MIN_VALIDATE_S * 1000:.0f}ms"
    )

    overlapping = [
        finished - started
        for started, finished in samples
        if started < window["end"] and finished > window["start"]
    ]
    p99 = _percentile(overlapping, 99)
    print(
        f"HEALTH_P99_MS={p99 * 1000:.2f} VALIDATE_MS={observed_validate_s * 1000:.2f} "
        f"PRECHECK_VALIDATE_MS={validate_s * 1000:.2f} SAMPLES={len(overlapping)} "
        f"ARCHIVE_BYTES={len(archive)}",
        flush=True,
    )
    assert p99 < _HEALTH_P99_S, (
        f"health p99 during validate_archive was {p99 * 1000:.1f}ms "
        f"(limit {_HEALTH_P99_S * 1000:.0f}ms); validation took "
        f"{observed_validate_s * 1000:.1f}ms over {len(overlapping)} overlapping samples"
    )
