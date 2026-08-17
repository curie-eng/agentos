"""Black box coverage for the cluster NetworkPolicy enforcement gate."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml
from plugin_format.connectors import validate_connectors

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check-netpol-enforcement.sh"
WEATHER_CONNECTORS = REPO_ROOT / "examples" / "weather" / "connectors.yaml"


@dataclass(frozen=True)
class GateRun:
    stdout: str
    stderr: str
    returncode: int
    kubectl_calls: list[list[str]]


def _run_gate(
    tmp_path: Path,
    *,
    connectors: tuple[str, ...],
    sandbox_unreachable: tuple[str, ...] = (),
    outside_reachable: tuple[str, ...] = (),
    sandbox_dns_unreachable: bool = False,
    outside_dns_unreachable: bool = False,
    sandbox_to_deny_target_reachable: bool = False,
    outside_to_deny_target_reachable: bool = True,
    connector_not_ready: tuple[str, ...] = (),
) -> GateRun:
    fake = tmp_path / "kubectl"
    log = tmp_path / "kubectl.jsonl"
    fake.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from urllib.parse import urlparse

args = sys.argv[1:]
with open(os.environ["FAKE_KUBECTL_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")

connectors = tuple(filter(None, os.environ.get("FAKE_CONNECTORS", "").split(",")))
sandbox_unreachable = set(
    filter(None, os.environ.get("FAKE_SANDBOX_UNREACHABLE", "").split(","))
)
outside_reachable = set(
    filter(None, os.environ.get("FAKE_OUTSIDE_REACHABLE", "").split(","))
)
sandbox_dns_unreachable = os.environ.get("FAKE_SANDBOX_DNS_UNREACHABLE") == "1"
outside_dns_unreachable = os.environ.get("FAKE_OUTSIDE_DNS_UNREACHABLE") == "1"
sandbox_to_deny_target_reachable = (
    os.environ.get("FAKE_SANDBOX_TO_DENY_TARGET_REACHABLE") == "1"
)
outside_to_deny_target_reachable = (
    os.environ.get("FAKE_OUTSIDE_TO_DENY_TARGET_REACHABLE") == "1"
)
connector_not_ready = set(
    filter(None, os.environ.get("FAKE_CONNECTOR_NOT_READY", "").split(","))
)


def connector_deployment() -> str | None:
    for connector in connectors:
        if f"deployment/{connector}" in args or f"deploy/{connector}" in args:
            return connector
        for resource in ("deployment", "deploy"):
            if resource in args and args.index(resource) + 1 < len(args):
                if args[args.index(resource) + 1] == connector:
                    return connector
    return None

if "delete" in args or "apply" in args:
    raise SystemExit(0)

if "wait" in args or ("rollout" in args and "status" in args):
    deployment = connector_deployment()
    if deployment is not None:
        raise SystemExit(1 if deployment in connector_not_ready else 0)
    raise SystemExit(0)

if "get" in args and "networkpolicy" in args:
    raise SystemExit(0)

if "get" in args and "pod" in args:
    print("192.0.2.10", end="")
    raise SystemExit(0)

if "get" in args and "svc" in args:
    if "-l" in args:
        if connectors:
            print("\\n".join(connectors))
        raise SystemExit(0)
    print("8000", end="")
    raise SystemExit(0)

if "exec" in args:
    exec_index = args.index("exec")
    pod = args[exec_index + 1]
    command = args[args.index("--") + 1 :]
    if command and command[0] == "getent":
        if pod == "netpol-probe-sandbox":
            raise SystemExit(1 if sandbox_dns_unreachable else 0)
        if pod == "netpol-probe-outside":
            raise SystemExit(1 if outside_dns_unreachable else 0)
        raise SystemExit(1)
    urls = [value for value in command if value.startswith("http://")]
    if urls:
        service = urlparse(urls[0]).hostname or ""
        if service == "192.0.2.10":
            if pod == "netpol-probe-sandbox":
                raise SystemExit(0 if sandbox_to_deny_target_reachable else 1)
            if pod == "netpol-probe-outside":
                raise SystemExit(0 if outside_to_deny_target_reachable else 1)
            raise SystemExit(1)
        if service not in connectors:
            raise SystemExit(1)
        if pod == "netpol-probe-sandbox":
            raise SystemExit(1 if service in sandbox_unreachable else 0)
        if pod == "netpol-probe-outside":
            raise SystemExit(0 if service in outside_reachable else 1)

print(f"unexpected kubectl invocation: {args!r}", file=sys.stderr)
raise SystemExit(90)
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}{os.pathsep}{env['PATH']}",
            "FAKE_KUBECTL_LOG": str(log),
            "FAKE_CONNECTORS": ",".join(connectors),
            "FAKE_SANDBOX_UNREACHABLE": ",".join(sandbox_unreachable),
            "FAKE_OUTSIDE_REACHABLE": ",".join(outside_reachable),
            "FAKE_SANDBOX_DNS_UNREACHABLE": (
                "1" if sandbox_dns_unreachable else "0"
            ),
            "FAKE_OUTSIDE_DNS_UNREACHABLE": (
                "1" if outside_dns_unreachable else "0"
            ),
            "FAKE_SANDBOX_TO_DENY_TARGET_REACHABLE": (
                "1" if sandbox_to_deny_target_reachable else "0"
            ),
            "FAKE_OUTSIDE_TO_DENY_TARGET_REACHABLE": (
                "1" if outside_to_deny_target_reachable else "0"
            ),
            "FAKE_CONNECTOR_NOT_READY": ",".join(connector_not_ready),
        }
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "curie", "curie", "curie"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    return GateRun(result.stdout, result.stderr, result.returncode, calls)


def _connector_curl_calls(calls: list[list[str]]) -> set[tuple[str, str]]:
    attempts: set[tuple[str, str]] = set()
    for call in calls:
        if "exec" not in call or "--" not in call:
            continue
        command = call[call.index("--") + 1 :]
        urls = [value for value in command if value.startswith("http://")]
        if not urls:
            continue
        service = urlparse(urls[0]).hostname or ""
        if "-mcp-" in service:
            attempts.add((call[call.index("exec") + 1], service))
    return attempts


def _curl_events(calls: list[list[str]]) -> list[tuple[int, str, str]]:
    events: list[tuple[int, str, str]] = []
    probe_roles = {
        "netpol-probe-sandbox": "sandbox",
        "netpol-probe-outside": "outside",
    }
    for index, call in enumerate(calls):
        if "exec" not in call or "--" not in call:
            continue
        command = call[call.index("--") + 1 :]
        urls = [value for value in command if value.startswith("http://")]
        if not urls:
            continue
        pod = call[call.index("exec") + 1]
        role = probe_roles.get(pod, pod)
        host = urlparse(urls[0]).hostname or ""
        target = "deny_target" if host == "192.0.2.10" else host
        events.append((index, role, target))
    return events


def _dns_events(calls: list[list[str]]) -> list[tuple[int, str]]:
    events: list[tuple[int, str]] = []
    for index, call in enumerate(calls):
        if "exec" not in call or "--" not in call:
            continue
        command = call[call.index("--") + 1 :]
        if command[:2] == ["getent", "hosts"]:
            events.append((index, call[call.index("exec") + 1]))
    return events


def _deployment_readiness_events(
    calls: list[list[str]], connectors: tuple[str, ...]
) -> list[tuple[int, str]]:
    events: list[tuple[int, str]] = []
    for index, call in enumerate(calls):
        if "wait" not in call and not ("rollout" in call and "status" in call):
            continue
        for connector in connectors:
            direct_resource = any(
                value in call for value in (f"deployment/{connector}", f"deploy/{connector}")
            )
            separate_resource = any(
                resource in call
                and call.index(resource) + 1 < len(call)
                and call[call.index(resource) + 1] == connector
                for resource in ("deployment", "deploy")
            )
            if direct_resource or separate_resource:
                events.append((index, connector))
    return events


def test_weather_ladder_bundle_declares_a_hosted_connector() -> None:
    assert WEATHER_CONNECTORS.is_file(), "the fixed weather ladder bundle has no connectors.yaml"
    parsed, errors = validate_connectors(yaml.safe_load(WEATHER_CONNECTORS.read_text()))

    assert errors == []
    assert parsed is not None
    assert any(connector.is_hosted for connector in parsed.connectors.values())


def test_zero_connector_services_fails(tmp_path: Path) -> None:
    result = _run_gate(tmp_path, connectors=())

    assert result.returncode != 0
    assert "found 0 connector Services" in result.stdout + result.stderr


def test_non_enforcing_cni_fails_before_connector_checks(tmp_path: Path) -> None:
    result = _run_gate(
        tmp_path,
        connectors=("curie-mcp-alpha",),
        sandbox_to_deny_target_reachable=True,
    )

    assert result.returncode != 0
    assert "CNI is not enforcing NetworkPolicy" in result.stderr
    assert not any(
        target.startswith("curie-mcp-")
        for _, _, target in _curl_events(result.kubectl_calls)
    )


def test_outside_probe_must_reach_known_listener_before_connector_denial(tmp_path: Path) -> None:
    connector = "curie-mcp-alpha"
    result = _run_gate(
        tmp_path,
        connectors=(connector,),
        outside_to_deny_target_reachable=False,
    )

    assert result.returncode != 0
    assert "outside probe" in result.stderr
    assert "deny target" in result.stderr
    assert ("outside", "deny_target") in {
        (role, target) for _, role, target in _curl_events(result.kubectl_calls)
    }
    assert ("outside", connector) not in {
        (role, target) for _, role, target in _curl_events(result.kubectl_calls)
    }


def test_outside_probe_must_resolve_dns_before_connector_denial(tmp_path: Path) -> None:
    connector = "curie-mcp-alpha"
    result = _run_gate(
        tmp_path,
        connectors=(connector,),
        outside_dns_unreachable=True,
    )

    assert result.returncode != 0
    assert "outside probe cannot resolve DNS" in result.stderr
    dns_events = _dns_events(result.kubectl_calls)
    assert any(pod == "netpol-probe-outside" for _, pod in dns_events)
    outside_connector_curls = [
        index
        for index, role, target in _curl_events(result.kubectl_calls)
        if role == "outside" and target == connector
    ]
    assert outside_connector_curls == []


def test_every_connector_checks_sandbox_allow_and_outside_deny(tmp_path: Path) -> None:
    connectors = ("curie-mcp-alpha", "curie-mcp-beta")
    result = _run_gate(tmp_path, connectors=connectors)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "connector Service count: 2" in result.stdout
    assert _connector_curl_calls(result.kubectl_calls) == {
        ("netpol-probe-sandbox", "curie-mcp-alpha"),
        ("netpol-probe-outside", "curie-mcp-alpha"),
        ("netpol-probe-sandbox", "curie-mcp-beta"),
        ("netpol-probe-outside", "curie-mcp-beta"),
    }


def test_connector_denials_follow_outside_control_and_deployment_readiness(
    tmp_path: Path,
) -> None:
    connectors = ("curie-mcp-alpha", "curie-mcp-beta")
    result = _run_gate(tmp_path, connectors=connectors)

    assert result.returncode == 0, result.stdout + result.stderr
    curl_indexes = {
        (role, target): index for index, role, target in _curl_events(result.kubectl_calls)
    }
    readiness_indexes = {
        connector: index
        for index, connector in _deployment_readiness_events(result.kubectl_calls, connectors)
    }
    assert ("sandbox", "deny_target") in curl_indexes
    assert ("outside", "deny_target") in curl_indexes
    assert set(readiness_indexes) == set(connectors)
    for connector in connectors:
        assert readiness_indexes[connector] < curl_indexes[("sandbox", connector)]
        assert readiness_indexes[connector] < curl_indexes[("outside", connector)]
        assert curl_indexes[("outside", "deny_target")] < curl_indexes[("outside", connector)]


def test_connector_readiness_failure_is_not_reported_as_policy_failure(tmp_path: Path) -> None:
    connectors = ("curie-mcp-alpha", "curie-mcp-beta")
    result = _run_gate(
        tmp_path,
        connectors=connectors,
        connector_not_ready=("curie-mcp-beta",),
    )

    assert result.returncode != 0
    assert "Deployment curie-mcp-beta" in result.stderr
    assert "ready" in result.stderr
    assert "sandbox cannot reach connector Service curie-mcp-beta" not in result.stderr
    assert ("sandbox", "curie-mcp-beta") not in {
        (role, target) for _, role, target in _curl_events(result.kubectl_calls)
    }


def test_outside_access_to_any_connector_fails(tmp_path: Path) -> None:
    connectors = ("curie-mcp-alpha", "curie-mcp-beta")
    result = _run_gate(
        tmp_path,
        connectors=connectors,
        outside_reachable=("curie-mcp-beta",),
    )

    assert result.returncode != 0
    assert "outside probe reached connector Service curie-mcp-beta:8000" in result.stderr


def test_sandbox_access_loss_fails(tmp_path: Path) -> None:
    connectors = ("curie-mcp-alpha", "curie-mcp-beta")
    result = _run_gate(
        tmp_path,
        connectors=connectors,
        sandbox_unreachable=("curie-mcp-beta",),
    )

    assert result.returncode != 0
    assert "sandbox cannot reach connector Service curie-mcp-beta:8000" in result.stderr
