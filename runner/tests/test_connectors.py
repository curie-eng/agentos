"""Mounting declared connectors into the agent's MCP configuration (#1118).

The property under test is that the author never writes the URL, and that the
URL the agent dials is the one the Service actually has.
"""

from __future__ import annotations

import json
from pathlib import Path

from aci_protocol import BootEnv, Budget
from curie_runner import RunnerConfig
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


# --------------------------------------------------------------------------- #
# The whole chain, worker render to mounted server -- #1195
#
# Every test above hands derive_mcp_servers a scope directly, which is the one
# thing a real boot never does. The scope has to survive BootEnv.render_worker,
# BootEnv.from_env, and RunnerConfig before it reaches this function, and a
# break anywhere along that path is invisible: the scope arrives as None, the
# hosted connector is treated as "not exercisable in this tier", and the agent
# boots without its tools while nothing errors.
# --------------------------------------------------------------------------- #

# What the chart/docker substrate contributes; no worker producer writes these.
_SUBSTRATE_ENV = {"CURIE_SANDBOX_ID": "curie-sandbox-abc123", "CURIE_RUNNER_PORT": "8080"}


def _config_for(
    plugin_dir: Path,
    *,
    release: str | None = None,
    agent: str | None = None,
    namespace: str | None = None,
) -> RunnerConfig:
    """A RunnerConfig built the way a real boot builds one.

    Through the real producer and the real consumer parse, never by
    constructing the dataclass: a hand-built config would assert on this test's
    own idea of the boot env instead of on the one the worker actually renders.
    """

    env = (
        BootEnv.render_worker(
            plugin_dir=str(plugin_dir),
            session_id="agent-abc-thread-1",
            budget=Budget(max_output_tokens_per_run=4096, max_usd_per_day=5.0),
            memory_ref="http://api:8000/agents/agent-abc/state/memory",
            history_ref="http://api:8000/agents/agent-abc/state/transcript/t1",
            connector_release=release,
            connector_agent=agent,
            connector_namespace=namespace,
        )
        | _SUBSTRATE_ENV
    )
    return RunnerConfig.from_env(env)


def test_a_scope_rendered_by_the_worker_reaches_the_mounted_connector(tmp_path: Path) -> None:
    # The acceptance criterion for #1195: a cluster boot that declares a hosted
    # connector must end up dialing the Service Curie created for it.
    config = _config_for(
        _bundle(tmp_path, HOSTED), release="curie", agent="acme-dev", namespace="curie-prod"
    )
    servers = derive_mcp_servers(
        config.session.plugin_dir,
        release=config.connector_release,
        agent=config.connector_agent,
        namespace=config.connector_namespace,
    )
    assert servers["grafana"]["url"] == (
        "http://curie-acme-dev-mcp-grafana.curie-prod.svc.cluster.local:8000/mcp"
    )


def test_a_boot_that_renders_no_scope_still_mounts_no_hosted_connector(tmp_path: Path) -> None:
    # The other half. "Declared but not exercisable in this tier" (#1093) is the
    # honest answer when the worker sends no scope, so the fix must reach the
    # scope through the boot env and must never invent one.
    config = _config_for(_bundle(tmp_path, HOSTED))
    servers = derive_mcp_servers(
        config.session.plugin_dir,
        release=config.connector_release,
        agent=config.connector_agent,
        namespace=config.connector_namespace,
    )
    assert servers == {}
