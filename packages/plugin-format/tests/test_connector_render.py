"""Deriving Kubernetes objects from a declared connector (ADR-0086, #1063).

The value of deriving rather than documenting is that specific defects become
unrepresentable. These tests pin the two that were actually hit by hand, so a
refactor cannot quietly reintroduce them.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from plugin_format import connector_render as r
from plugin_format.connectors import ConnectorSpec

HOSTED = ConnectorSpec(
    image="grafana/mcp-grafana:0.17.2",
    args=["-t", "streamable-http", "-disable-write"],
    env={"GRAFANA_URL": "https://g.example.com"},
    secrets=["GRAFANA_TOKEN"],
)
REMOTE = ConnectorSpec(url="https://mcp.internal/mcp", headers={"Authorization": "Bearer ${T}"})


def _objs(release: str = "sre-bot", app: str = "sre-bot") -> list[dict]:
    return r.render(release, "sre-bot", app, "grafana", HOSTED, "conn-secrets")


# --------------------------------------------------------------------------- #
# The ClusterIP trap -- the defect this renderer exists to prevent
# --------------------------------------------------------------------------- #
def test_egress_rule_uses_a_podselector_never_an_ipblock() -> None:
    # A NetworkPolicy naming a Service ClusterIP can NEVER match: kube-proxy
    # DNATs the destination to a pod IP before the policy is evaluated. The
    # symptom is a bare connection refused, and on a CNI that ignores
    # NetworkPolicy (minikube's default) the broken rule looks identical to a
    # correct one -- so it survives local testing and fails in a real cluster.
    np = next(o for o in _objs() if o["kind"] == "NetworkPolicy")
    to = np["spec"]["egress"][0]["to"][0]
    assert "podSelector" in to
    assert "ipBlock" not in to


def test_egress_selects_exactly_the_pods_rail_1_denies() -> None:
    # Too narrow and the allow widens nothing (NetworkPolicy is additive, it
    # cannot narrow -- ADR-0067) so the sandbox still cannot reach the
    # connector. Too broad -- e.g. only `component` -- and it also grants egress
    # to every OTHER release's sandboxes in the namespace. Both fail silently.
    np = next(
        o
        for o in r.render("relA", "ns", "sre-bot", "g", HOSTED, "s")
        if o["kind"] == "NetworkPolicy"
    )
    assert np["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "sre-bot",
        "app.kubernetes.io/instance": "relA",
        "app.kubernetes.io/component": "runner-sandbox",
    }


def test_two_releases_do_not_select_each_others_sandboxes() -> None:
    a = next(
        o for o in r.render("relA", "ns", "app", "g", HOSTED, "s") if o["kind"] == "NetworkPolicy"
    )
    b = next(
        o for o in r.render("relB", "ns", "app", "g", HOSTED, "s") if o["kind"] == "NetworkPolicy"
    )
    assert a["spec"]["podSelector"] != b["spec"]["podSelector"]


# --------------------------------------------------------------------------- #
# The host-header trap
# --------------------------------------------------------------------------- #
def test_host_aliases_cover_every_name_the_sandbox_could_dial() -> None:
    # Servers that guard against DNS rebinding default their allowlist to
    # loopback, so an in-cluster caller reaching them by Service DNS gets
    # `forbidden: host not allowed`. Curie named the Service, so Curie can
    # supply the full set; an author would have to guess it.
    aliases = r.host_aliases("sre-bot", "grafana", "ns", 8000)
    assert "sre-bot-mcp-grafana:8000" in aliases
    assert "sre-bot-mcp-grafana.ns:8000" in aliases
    assert "sre-bot-mcp-grafana.ns.svc.cluster.local:8000" in aliases


def test_injected_url_matches_the_service_that_was_rendered() -> None:
    # Hand-writing this URL is how a bundle ends up with an address that does
    # not resolve in the tier it is deployed to.
    svc = next(o for o in _objs() if o["kind"] == "Service")
    url = r.mcp_entry("sre-bot", "sre-bot", "grafana", HOSTED)["url"]
    assert svc["metadata"]["name"] in url
    assert url.endswith("/mcp")


# --------------------------------------------------------------------------- #
# Hardening the author never writes, and so cannot forget
# --------------------------------------------------------------------------- #
def test_container_is_hardened_by_construction() -> None:
    dep = next(o for o in _objs() if o["kind"] == "Deployment")
    pod = dep["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["resources"]["limits"]["memory"]


def test_secrets_travel_by_reference_never_as_a_literal() -> None:
    dep = next(o for o in _objs() if o["kind"] == "Deployment")
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    entry = next(e for e in env if e["name"] == "GRAFANA_TOKEN")
    assert entry["valueFrom"]["secretKeyRef"]["name"] == "conn-secrets"
    assert "value" not in entry, "a secret must never be inlined into the manifest"


def test_plain_env_is_passed_through() -> None:
    dep = next(o for o in _objs() if o["kind"] == "Deployment")
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "GRAFANA_URL", "value": "https://g.example.com"} in env


# --------------------------------------------------------------------------- #
# Remote connectors own no objects
# --------------------------------------------------------------------------- #
def test_remote_connector_renders_nothing_to_run() -> None:
    assert r.render("sre-bot", "ns", "app", "internal", REMOTE, "s") == []


def test_remote_connector_keeps_its_own_url_and_headers() -> None:
    entry = r.mcp_entry("sre-bot", "ns", "internal", REMOTE)
    assert entry["url"] == "https://mcp.internal/mcp"
    assert entry["headers"]["Authorization"] == "Bearer ${T}"


@pytest.mark.parametrize("kind", ["Service", "Deployment", "NetworkPolicy"])
def test_hosted_connector_renders_the_full_set(kind: str) -> None:
    assert any(o["kind"] == kind for o in _objs())


# --------------------------------------------------------------------------- #
# Anti-drift: the selector is only correct if it matches the CHART's
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")
def test_selector_matches_what_the_chart_actually_renders() -> None:
    # The two failure modes are both silent, so asserting against my own belief
    # about the labels proves nothing. Render the real chart and compare.
    chart = Path(__file__).resolve().parents[3] / "charts" / "curie"
    if not chart.is_dir():  # package tested outside the monorepo
        pytest.skip("chart not present")
    out = subprocess.run(
        ["helm", "template", "myrel", str(chart), "--set", "nameOverride=sre-bot"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    chart_selector = None
    for doc in yaml.safe_load_all(out):
        if (
            doc
            and doc.get("kind") == "NetworkPolicy"
            and "runner-default-deny-egress" in doc["metadata"]["name"]
        ):
            chart_selector = doc["spec"]["podSelector"]["matchLabels"]
    assert chart_selector, "could not find Rail 1's default-deny egress policy"
    assert r.sandbox_selector("myrel", "sre-bot") == chart_selector
