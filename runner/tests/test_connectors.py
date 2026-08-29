"""Mounting declared connectors into the agent's MCP configuration (#1118).

The property under test is that the author never writes the URL, and that the
URL the agent dials is the one the Service actually has.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aci_protocol import BootEnv, Budget
from curie_runner import RunnerConfig
from curie_runner.__main__ import build_runner
from curie_runner.approval import APPROVAL_SERVER_NAME
from curie_runner.connectors import build_mcp_servers, derive_mcp_servers
from curie_runner.state import STATE_SERVER_NAME
from plugin_format.connectors import RESERVED_CONNECTOR_NAMES

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


def test_non_utf8_connectors_file_mounts_nothing_rather_than_crashing(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    (root / "connectors.yaml").write_bytes(b"\x80")
    assert derive_mcp_servers(root, **SCOPE) == {}


def test_explicit_mapping_tag_on_a_scalar_mounts_nothing(tmp_path: Path) -> None:
    servers = derive_mcp_servers(
        _bundle(tmp_path, "connectors: !!map foo\n"), **SCOPE
    )
    assert servers == {}


def test_duplicate_connector_name_mounts_nothing(tmp_path: Path) -> None:
    servers = derive_mcp_servers(
        _bundle(
            tmp_path,
            "connectors:\n"
            "  grafana:\n"
            "    url: https://first.example.com/mcp\n"
            "  grafana:\n"
            "    url: https://second.example.com/mcp\n",
        ),
        **SCOPE,
    )
    assert servers == {}, "duplicate grafana must mount nothing"


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


# --------------------------------------------------------------------------- #
# A connector name colliding with a platform server -- #1200
# --------------------------------------------------------------------------- #
BUDGET = '{"max_output_tokens_per_run": 10000, "max_usd_per_day": 1.0}'


def _boot_env(monkeypatch, tmp_path: Path, suffix: str) -> dict[str, str]:
    monkeypatch.setenv("CURIE_STATE_URL", "http://state.invalid/agents/a/state")
    monkeypatch.setenv("CURIE_STATE_TOKEN", "t")
    return {
        "CURIE_PLUGIN_DIR": str(_bundle(tmp_path)),
        "CURIE_SESSION_ID": f"s-{suffix}",
        "CURIE_SANDBOX_ID": f"b-{suffix}",
        "CURIE_BUDGET": BUDGET,
    }


def test_the_platform_approval_server_survives_a_colliding_connector_name() -> None:
    # Both maps are plain dict keys on one channel, so somebody wins. Losing a
    # connector is visible and diagnosable; losing request_approval is silent --
    # a skill that calls it fails, or a self-imposed approval never raises. The
    # merge therefore fails safe toward the platform.
    approval = {"platform": "approval"}
    state = {"platform": "state"}
    servers = build_mcp_servers(
        platform={APPROVAL_SERVER_NAME: approval, STATE_SERVER_NAME: state},
        derived={
            "curie": {"type": "http", "url": "http://impostor/mcp"},
            "curie-state": {"type": "http", "url": "http://impostor/state/mcp"},
            "grafana": {"type": "http", "url": "http://grafana/mcp"},
        },
    )
    assert servers[APPROVAL_SERVER_NAME] is approval
    assert servers[STATE_SERVER_NAME] is state
    assert servers["grafana"]["url"] == "http://grafana/mcp"


def test_a_declared_connector_still_mounts_alongside_the_platform_servers() -> None:
    # The control on the same seam: safety must not be bought by dropping the
    # connectors the bundle declared.
    grafana = {"type": "http", "url": "http://grafana/mcp"}
    servers = build_mcp_servers(
        platform={APPROVAL_SERVER_NAME: {"platform": "approval"}},
        derived={"grafana": grafana},
    )
    assert servers["grafana"] is grafana
    assert APPROVAL_SERVER_NAME in servers


def test_every_platform_mcp_server_the_boot_path_mounts_is_reserved(tmp_path, monkeypatch) -> None:
    # Every platform MCP server must be MOUNTED here, not merely importable.
    # This pin reads the servers this boot actually mounted, so a
    # conditionally-mounted platform server -- like curie-state, which mounts
    # only when CURIE_STATE_URL is set -- is invisible to the pin unless its
    # mounting env is set in _boot_env. If you add a platform server behind its
    # own condition, set that condition in _boot_env too, or the pin goes blind
    # to it and the reserved list silently stops being complete.
    env = _boot_env(monkeypatch, tmp_path, "pin")
    # fake_model=False: the fake branch never builds mcp_servers at all, so a
    # fake boot would pin nothing.
    runner = build_runner(RunnerConfig.from_env(env), fake_model=False)
    mounted = runner._factory()._options.mcp_servers

    assert APPROVAL_SERVER_NAME in mounted
    assert STATE_SERVER_NAME in mounted
    # The bundle declares no connectors, so every remaining key is a platform
    # key by construction. A third platform server added later reddens this
    # until it is reserved -- which is why the fence is two exact names and not
    # the `curie-` prefix.
    assert set(mounted) == RESERVED_CONNECTOR_NAMES


def test_the_boot_path_mounts_the_platform_approval_server_over_a_colliding_connector(
    tmp_path, monkeypatch
) -> None:
    # The pin on the WIRING, not on the helper. build_mcp_servers can stay
    # perfectly correct while the boot path stops routing through it -- that is
    # exactly the pre-#1200 defect, an inline literal spreading the derived map
    # last -- and every other test here stays green, because the boot-path pin
    # above uses a zero-connector bundle whose set comparison is order-blind.
    #
    # The colliding input is unconstructible through a real bundle by
    # construction: this branch's own validator rejects a connector named
    # `curie`, so connectors.py:_read logs and returns {} before any merge
    # happens. So the collision has to be INJECTED, and the injection point is
    # derive_mcp_servers -- the UPSTREAM INPUT of the closure, at the boundary,
    # not the thing being asserted. What is under test is the precedence wiring
    # inside build_runner's factory; that runs for real. The plan rejected
    # monkeypatching derive_mcp_servers as an ALTERNATIVE to extracting
    # build_mcp_servers; this is not that. Do not delete this as a mock.
    impostor = {"type": "http", "url": "http://impostor/mcp"}
    grafana = {"type": "http", "url": "http://grafana/mcp"}
    monkeypatch.setattr(
        "curie_runner.__main__.derive_mcp_servers",
        lambda *a, **k: {APPROVAL_SERVER_NAME: impostor, "grafana": grafana},
    )
    env = _boot_env(monkeypatch, tmp_path, "collide")
    runner = build_runner(RunnerConfig.from_env(env), fake_model=False)
    mounted = runner._factory()._options.mcp_servers

    assert mounted[APPROVAL_SERVER_NAME] is not impostor
    # Positive identification, not merely "not the impostor": the platform's
    # approval server is the in-process SDK server, which no connector can be.
    assert mounted[APPROVAL_SERVER_NAME]["type"] == "sdk"
    # The control on the boot path: the platform winning must not cost the
    # bundle the connectors it declared.
    assert mounted["grafana"] is grafana


def test_the_reserved_list_matches_the_runner_constants() -> None:
    # plugin_format re-enumerates these names because runner depends on it and
    # never the reverse. This is the pin that keeps the copy honest: rename
    # either constant here and the deploy-time guard stops fencing it.
    assert RESERVED_CONNECTOR_NAMES == {APPROVAL_SERVER_NAME, STATE_SERVER_NAME}


# --------------------------------------------------------------------------- #
# An agent name that forges the object-name join -- #1446
#
# `mcp_entry` derives the URL through `object_name`, which builds
# `{release}-{agent}-mcp-{connector}`. Since #1446 that function fails closed on
# an agent name that would forge a SECOND `-mcp-` (it contains the delimiter, or
# it ends in `-mcp`), because two different `(agent, connector)` pairs would
# otherwise render one Service, one Deployment, both NetworkPolicies and one
# `app.kubernetes.io/name` pod selector -- and the connector is unauthenticated
# by design (ADR-0086), so that name is the only thing binding a sandbox to a
# credential.
#
# The connector-name half of that rule is already fail-soft here: `_read` runs
# `validate_connectors` and returns None on any error, so a forging CONNECTOR
# name logs and mounts nothing. The AGENT name has no such path -- it arrives on
# the boot env, is never validated in this module, and goes straight into the
# `mcp_entry` comprehension in `derive_mcp_servers`. An uncaught raise there
# contradicts this module's own documented contract (see `_read`): "Log and
# mount nothing rather than fail the turn ... a crashed boot loses the whole
# session." Losing the connector's tools is visible in the agent's tool list and
# recoverable by renaming the agent; losing the boot is the entire session, for
# every turn, until someone reads a stack trace out of a sandbox pod's logs.
# --------------------------------------------------------------------------- #
FORGING_AGENT = "a-mcp-b"


def test_a_forging_agent_name_mounts_nothing_rather_than_crashing_the_boot(
    tmp_path: Path, caplog
) -> None:
    root = _bundle(tmp_path, HOSTED)

    with caplog.at_level(logging.WARNING, logger="curie_runner.connectors"):
        servers = derive_mcp_servers(
            root, release="curie", agent=FORGING_AGENT, namespace="curie"
        )

    assert servers == {}, f"a forging agent name must mount nothing, got {servers}"
    # Silence would be worse than the crash it replaces: the agent boots, its
    # tool list is quietly short one connector, and every tool call fails as
    # "no such tool" with nothing anywhere naming the cause. The warning must
    # carry the AGENT NAME, because that is the single thing an operator has to
    # change and it is assigned at deploy time, not written in the bundle.
    assert any(
        FORGING_AGENT in record.getMessage() for record in caplog.records
    ), caplog.text

    # The control, in the same test on purpose: if `_bundle`, `HOSTED`, or
    # `derive_mcp_servers` broke for any reason unrelated to #1446, the
    # assertion above would pass vacuously on an empty dict. Same bundle, same
    # release, same namespace -- only the agent name differs.
    assert derive_mcp_servers(root, **SCOPE)["grafana"]["url"] == (
        "http://curie-acme-dev-mcp-grafana.curie.svc.cluster.local:8000/mcp"
    )


def test_an_agent_name_that_only_looks_like_the_join_still_mounts(tmp_path: Path) -> None:
    # A LEADING `mcp-` on the agent side is unambiguous -- the only alternative
    # split of `curie-mcp-x-mcp-grafana` leaves an empty agent -- so it must
    # keep working. This is the test that reddens if someone "simplifies" the
    # rule to a bare `'-mcp-' in name` substring ban or a
    # `base.count('-mcp-') == 1` guard: both over-reject, and an over-rejecting
    # rule silently strips a live agent of its connectors for no security gain.
    servers = derive_mcp_servers(
        _bundle(tmp_path, HOSTED), release="curie", agent="mcp-x", namespace="curie"
    )
    assert servers["grafana"]["url"] == (
        "http://curie-mcp-x-mcp-grafana.curie.svc.cluster.local:8000/mcp"
    )
