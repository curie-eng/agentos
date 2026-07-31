"""Mounting declared connectors into the agent's MCP configuration (#1118).

The property under test is that the author never writes the URL, and that the
URL the agent dials is the one the Service actually has.
"""

from __future__ import annotations

import json
from pathlib import Path

from curie_runner.connectors import derive_mcp_servers

HOSTED = "connectors:\n  grafana:\n    image: grafana/mcp-grafana:0.17.2\n    secrets: [T]\n"
REMOTE = "connectors:\n  internal:\n    url: https://mcp.internal/mcp\n"

SCOPE = {"release": "curie", "agent": "acme-dev", "namespace": "curie"}


def _bundle(root: Path, connectors: str | None = None, mcp: dict | None = None) -> Path:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "b", "version": "0.1.0", "description": "t"}), encoding="utf-8"
    )
    if connectors is not None:
        (root / "connectors.yaml").write_text(connectors, encoding="utf-8")
    if mcp is not None:
        (root / ".mcp.json").write_text(json.dumps(mcp), encoding="utf-8")
    return root


def test_the_author_never_writes_the_url(tmp_path: Path) -> None:
    # The whole point of ADR-0086. The URL embeds the release, the agent, and
    # the namespace -- all assigned at install and deploy, none knowable to
    # whoever wrote the bundle.
    servers = derive_mcp_servers(_bundle(tmp_path, HOSTED), **SCOPE)
    assert servers["grafana"]["url"] == (
        "http://curie-acme-dev-mcp-grafana.curie.svc.cluster.local:8000/mcp"
    )


def test_two_agents_from_one_bundle_dial_different_servers(tmp_path: Path) -> None:
    # Since #1116 the Service is agent-scoped, so a hand-written URL is
    # guaranteed wrong for at least one of two agents sharing a bundle. Deriving
    # per-boot is what makes one bundle serve both.
    root = _bundle(tmp_path, HOSTED)
    dev = derive_mcp_servers(root, release="curie", agent="acme-dev", namespace="curie")
    prod = derive_mcp_servers(root, release="curie", agent="sre-prod", namespace="curie")
    assert dev["grafana"]["url"] != prod["grafana"]["url"]


def test_no_connectors_file_mounts_nothing(tmp_path: Path) -> None:
    assert derive_mcp_servers(_bundle(tmp_path), **SCOPE) == {}


def test_hosted_connector_without_a_scope_mounts_nothing(tmp_path: Path) -> None:
    # The skill tier hosts nothing, so there is no Service to point at. Mounting
    # a URL that resolves nowhere would turn "not available here" into a
    # connection refused mid-turn.
    servers = derive_mcp_servers(
        _bundle(tmp_path, HOSTED), release=None, agent=None, namespace=None
    )
    assert servers == {}


def test_a_remote_connector_needs_no_scope(tmp_path: Path) -> None:
    # It carries its own absolute url, so it is exercisable in every tier --
    # including the ones that host nothing.
    servers = derive_mcp_servers(
        _bundle(tmp_path, REMOTE), release=None, agent=None, namespace=None
    )
    assert servers["internal"]["url"] == "https://mcp.internal/mcp"


def test_remote_and_hosted_together_mount_only_what_this_tier_can_reach(tmp_path: Path) -> None:
    root = _bundle(tmp_path, HOSTED + REMOTE.replace("connectors:\n", ""))
    both = derive_mcp_servers(root, **SCOPE)
    assert set(both) == {"grafana", "internal"}
    tierless = derive_mcp_servers(root, release=None, agent=None, namespace=None)
    assert set(tierless) == {"internal"}


def test_unreadable_connectors_file_mounts_nothing_rather_than_crashing(tmp_path: Path) -> None:
    # Deploy already validated this, so reaching here means the bundle changed
    # underneath us. Losing the connector's tools is visible; losing the whole
    # session to a boot crash is worse.
    servers = derive_mcp_servers(
        _bundle(tmp_path, "connectors:\n  g:\n   image: [unclosed\n"), **SCOPE
    )
    assert servers == {}


def test_invalid_connectors_file_mounts_nothing(tmp_path: Path) -> None:
    servers = derive_mcp_servers(
        _bundle(tmp_path, "connectors:\n  Bad_Name:\n    image: x:1\n"), **SCOPE
    )
    assert servers == {}


def test_no_plugin_dir_is_not_an_error(tmp_path: Path) -> None:
    assert derive_mcp_servers(None, **SCOPE) == {}


# --------------------------------------------------------------------------- #
# Reaching a hosted connector on a tier that cannot host it -- #1160
# --------------------------------------------------------------------------- #
HOSTED_WITH_FALLBACK = (
    "connectors:\n"
    "  grafana:\n"
    "    image: grafana/mcp-grafana:0.17.2\n"
    "    unhosted_url: http://host.docker.internal:8765/mcp\n"
)


def test_a_fallback_makes_a_hosted_connector_reachable_at_the_skill_tier(tmp_path: Path) -> None:
    # The skill tier hosts nothing, but the developer has mcp-grafana running.
    # Without this the eval lane silently loses its connector.
    servers = derive_mcp_servers(
        _bundle(tmp_path, HOSTED_WITH_FALLBACK), release=None, agent=None, namespace=None
    )
    assert servers["grafana"]["url"] == "http://host.docker.internal:8765/mcp"


def test_the_cluster_still_uses_the_service_it_created(tmp_path: Path) -> None:
    # A fallback that won everywhere would repoint a production agent at
    # someone's laptop. The derived URL must win wherever Curie hosts.
    servers = derive_mcp_servers(_bundle(tmp_path, HOSTED_WITH_FALLBACK), **SCOPE)
    assert "svc.cluster.local" in servers["grafana"]["url"]
    assert "8765" not in servers["grafana"]["url"]


def test_a_hosted_connector_without_a_fallback_still_mounts_nothing(tmp_path: Path) -> None:
    servers = derive_mcp_servers(
        _bundle(tmp_path, HOSTED), release=None, agent=None, namespace=None
    )
    assert servers == {}
