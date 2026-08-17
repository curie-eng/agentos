"""The API renders connector manifests but never applies them (ADR-0086, #1063).

The split is the security property worth pinning: rendering is a pure function,
so the API needs no cluster access to do it, and its deliberately read-only RBAC
(`pods: list`, `pods/log: get`) is untouched. That matters because the API is
the component that receives webhooks from the internet. Applying happens in the
CLI, under the operator's own kubectl credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from curie_api import bundles


def _bundle(root: Path, connectors_yaml: str | None = None) -> Path:
    inner = root / "b"
    (inner / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (inner / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "acme-bot", "version": "0.1.0", "description": "t"}), encoding="utf-8"
    )
    (inner / "skills" / "acme-bot").mkdir(parents=True, exist_ok=True)
    (inner / "skills" / "acme-bot" / "SKILL.md").write_text(
        "---\nname: acme-bot\ndescription: t\n---\nhi\n", encoding="utf-8"
    )
    if connectors_yaml is not None:
        (inner / "connectors.yaml").write_text(connectors_yaml, encoding="utf-8")
    return root


HOSTED = (
    "connectors:\n"
    "  grafana:\n"
    "    image: grafana/mcp-grafana:0.17.2\n"
    "    args: [-t, streamable-http]\n"
    "    env: {GRAFANA_URL: 'https://g.example.com'}\n"
    "    secrets: [GRAFANA_TOKEN]\n"
)


# These four are forwarded into `connector_render.render` as four bare strings,
# so they MUST stay distinct from one another. Held equal, the forward is pinned
# by nothing: swapping any two of them leaves every test in this file green
# while the rendered NetworkPolicies select no pod at all. A real install
# already drives at least one apart -- `curie cluster up --namespace my-agent
# --release my-agent` against a chart whose `nameOverride` is empty leaves
# app_name `curie` while release and namespace are both `my-agent`.
RELEASE = "acme-rel"
AGENT = "acme-bot"
NAMESPACE = "acme-ns"
APP_NAME = "curie"
SECRET_NAME = f"{RELEASE}-{AGENT}-connectors"


def _render(root: Path, agent: str = AGENT) -> list[dict]:
    return bundles.render_connector_manifests(
        bundles.read_connectors(root),
        release=RELEASE,
        agent=agent,
        namespace=NAMESPACE,
        app_name=APP_NAME,
        secret_name=f"{RELEASE}-{agent}-connectors",
    )


def test_bundle_without_connectors_renders_nothing(tmp_path: Path) -> None:
    assert _render(_bundle(tmp_path)) == []


def test_hosted_connector_renders_the_full_object_set(tmp_path: Path) -> None:
    objs = _render(_bundle(tmp_path, HOSTED))
    kinds = [o["kind"] for o in objs]
    # TWO NetworkPolicies, and asserting the count alone would not say why:
    # egress attached to the sandbox (where it may go) and ingress attached to
    # the connector (who may arrive). The connector is unauthenticated by
    # design -- the sandbox has no credential to authenticate with -- so
    # without the ingress half every pod in the namespace can reach something
    # holding a production credential.
    assert kinds == ["Service", "Deployment", "NetworkPolicy", "NetworkPolicy"]
    directions = sorted(p["spec"]["policyTypes"][0] for p in objs if p["kind"] == "NetworkPolicy")
    assert directions == ["Egress", "Ingress"]


def test_rendering_needs_no_cluster_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole reason the API may do this: it is a pure function. If rendering
    # ever reached for a kube client, the API would need cluster-write RBAC --
    # on the service that receives internet webhooks.
    import curie_api.k8s as k8s_mod

    def _explode(*_a: object, **_k: object) -> None:
        raise AssertionError("rendering must not touch the cluster")

    for attr in dir(k8s_mod):
        if attr.startswith("_"):
            continue
        obj = getattr(k8s_mod, attr)
        if callable(obj) and not isinstance(obj, type):
            monkeypatch.setattr(k8s_mod, attr, _explode, raising=False)

    assert _render(_bundle(tmp_path, HOSTED))  # renders fine with k8s poisoned


def test_secret_is_referenced_not_inlined(tmp_path: Path) -> None:
    dep = next(o for o in _render(_bundle(tmp_path, HOSTED)) if o["kind"] == "Deployment")
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    token = next(e for e in env if e["name"] == "GRAFANA_TOKEN")
    assert token["valueFrom"]["secretKeyRef"]["name"] == SECRET_NAME
    assert "value" not in token


def _policy(objs: list[dict], direction: str) -> dict:
    # Selecting "the NetworkPolicy" by kind picks whichever sorts first now that
    # two ship, so a rename or a reorder would silently test the wrong object.
    return next(
        o for o in objs if o["kind"] == "NetworkPolicy" and o["spec"]["policyTypes"] == [direction]
    )


def test_egress_policy_uses_a_podselector(tmp_path: Path) -> None:
    # The ClusterIP form silently never matches; see connector_render's module
    # docstring. Pinned here too because this is the path a real deploy takes.
    np = _policy(_render(_bundle(tmp_path, HOSTED)), "Egress")
    assert "podSelector" in np["spec"]["egress"][0]["to"][0]


def test_ingress_policy_admits_only_the_sandbox(tmp_path: Path) -> None:
    # Same ClusterIP trap on the way in, and the same reason it matters: this is
    # the path a real deploy takes, so a rule that parses but never matches
    # would leave the connector open while looking closed.
    connectors = HOSTED.replace(
        "    args: [-t, streamable-http]\n",
        "    args: [-t, streamable-http]\n    port: 9876\n",
    )
    root = _bundle(tmp_path, connectors)
    objs = _render(root)
    np = _policy(objs, "Ingress")
    src = np["spec"]["ingress"][0]["from"]
    assert len(src) == 1
    assert "podSelector" in src[0] and "ipBlock" not in src[0]
    svc = next(o for o in objs if o["kind"] == "Service")
    assert (
        svc["spec"]["ports"][0]["port"]
        == np["spec"]["ingress"][0]["ports"][0]["port"]
        == 9876
    )


def test_mcp_entry_url_matches_the_rendered_service(tmp_path: Path) -> None:
    root = _bundle(tmp_path, HOSTED)
    svc = next(o for o in _render(root) if o["kind"] == "Service")
    entries = bundles.connector_mcp_entries(
        bundles.read_connectors(root), release=RELEASE, agent=AGENT, namespace=NAMESPACE
    )
    assert svc["metadata"]["name"] in entries["grafana"]["url"]


IDENTITY = (
    "connectors:\n"
    "  grafana:\n"
    "    image: grafana/mcp-grafana:0.17.2\n"
    "    env: {SELF_URL: '${CURIE_CONNECTOR_URL}'}\n"
)


def test_each_of_the_four_names_lands_where_it_belongs(tmp_path: Path) -> None:
    # `render_connector_manifests` hands release, agent, namespace and app_name
    # to the renderer as four interchangeable-looking strings. Swapping any two
    # produces manifests that parse, apply, and are wrong: release for app_name
    # makes the sandbox selector read `app.kubernetes.io/name: acme-rel` instead
    # of `curie`, both NetworkPolicies then select no pod, and every connector
    # tool call dies as a bare connection timeout with no policy error anywhere.
    # Each of the four is asserted at the place only IT can reach, against whole
    # values rather than substrings, so no swap survives.
    root = _bundle(tmp_path, IDENTITY)
    objs = _render(root)
    name = f"{RELEASE}-{AGENT}-mcp-grafana"
    dns = f"{name}.{NAMESPACE}.svc.cluster.local"
    sandbox = {
        "app.kubernetes.io/name": APP_NAME,
        "app.kubernetes.io/instance": RELEASE,
        "app.kubernetes.io/component": "runner-sandbox",
    }

    # app_name and release: the sandbox selector, on both policies.
    assert _policy(objs, "Egress")["spec"]["podSelector"]["matchLabels"] == sandbox
    ingress_from = _policy(objs, "Ingress")["spec"]["ingress"][0]["from"][0]
    assert ingress_from["podSelector"]["matchLabels"] == sandbox

    # release and agent: the object name, in that order, plus part-of.
    dep = next(o for o in objs if o["kind"] == "Deployment")
    assert {o["metadata"]["name"] for o in objs} == {name, f"{name}-allow", f"{name}-allow-ingress"}
    assert dep["metadata"]["labels"] == {
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/part-of": RELEASE,
    }

    # namespace: the only place it shows up is the Service DNS Curie derives.
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "SELF_URL", "value": f"http://{dns}:8000"} in env
    entries = bundles.connector_mcp_entries(
        bundles.read_connectors(root), release=RELEASE, agent=AGENT, namespace=NAMESPACE
    )
    assert entries["grafana"] == {"type": "http", "url": f"http://{dns}:8000/mcp"}


def test_remote_connector_contributes_an_entry_but_no_objects(tmp_path: Path) -> None:
    root = _bundle(tmp_path, "connectors:\n  x:\n    url: https://mcp.internal/mcp\n")
    assert _render(root) == []
    entries = bundles.connector_mcp_entries(
        bundles.read_connectors(root), release=RELEASE, agent=AGENT, namespace=NAMESPACE
    )
    assert entries["x"]["url"] == "https://mcp.internal/mcp"


def test_two_agents_sharing_a_release_render_distinct_objects(tmp_path: Path) -> None:
    # The deploy path's half of #1116: same bundle, same release, two agents.
    # Release-scoped naming made these identical, so deploying one silently
    # overwrote the other's Deployment and credential.
    root = _bundle(tmp_path, HOSTED)
    dev = {o["metadata"]["name"] for o in _render(root, agent="acme-dev")}
    prod = {o["metadata"]["name"] for o in _render(root, agent="sre-prod")}
    assert not dev & prod, f"agents collide: {dev} vs {prod}"


REFERENCED = (
    "connectors:\n"
    "  grafana:\n"
    "    image: grafana/mcp-grafana:0.17.2\n"
    "    secrets:\n"
    "      - name: GRAFANA_TOKEN\n"
    "        from_secret: grafana-mcp\n"
)


def test_a_referenced_secret_is_not_something_the_caller_must_resolve(tmp_path: Path) -> None:
    # The property ADR-0090 depends on: with every credential referenced, the
    # deploy path handles none, so a reconciler holding no secrets can apply
    # this connector.
    declared = bundles.read_connectors(_bundle(tmp_path, REFERENCED))
    assert bundles.owned_secret_keys(declared) == []


def test_a_literal_secret_still_must_be_resolved(tmp_path: Path) -> None:
    declared = bundles.read_connectors(_bundle(tmp_path, HOSTED))
    assert bundles.owned_secret_keys(declared) == ["GRAFANA_TOKEN"]


FILE_ONLY = (
    "connectors:\n"
    "  k8s:\n"
    "    image: ghcr.io/containers/kubernetes-mcp-server:latest\n"
    "    secret_files: {K8S_KUBECONFIG: /secrets/kubeconfig}\n"
)


def test_a_secret_file_is_something_the_caller_must_resolve(tmp_path: Path) -> None:
    # #1424: with no `secrets:` entry at all this came back empty, so the CLI
    # skipped creating the Secret and the pod hung on FailedMount against the
    # volume the renderer had already emitted `optional: false`.
    declared = bundles.read_connectors(_bundle(tmp_path, FILE_ONLY))
    assert bundles.owned_secret_keys(declared) == ["K8S_KUBECONFIG"]


def test_a_referenced_secret_points_outside_curies_own_secret(tmp_path: Path) -> None:
    dep = next(o for o in _render(_bundle(tmp_path, REFERENCED)) if o["kind"] == "Deployment")
    env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
    ref = next(e for e in env if e["name"] == "GRAFANA_TOKEN")["valueFrom"]["secretKeyRef"]
    assert ref["name"] == "grafana-mcp"
    assert "value" not in next(e for e in env if e["name"] == "GRAFANA_TOKEN")
