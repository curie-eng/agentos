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
from typing import Any

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


# --------------------------------------------------------------------------- #
# A source-built connector resolves to its pinned digest before render -- ADR 0113
#
# The API stays a pure renderer: `apply_lock` reads a fact the bundle already
# carries and never resolves, builds, or contacts a registry. What changes is
# that the render path must APPLY it. An earlier draft of this work claimed the
# resolution happened in `render_connector_manifests` and never wired it, which
# is the defect these tests exist to make impossible.
# --------------------------------------------------------------------------- #
BUILT = (
    "connectors:\n"
    "  k8s-write:\n"
    "    build:\n"
    "      context: connectors/k8s-write\n"
    "      platforms: [linux/amd64, linux/arm64]\n"
    "    env: {K8S_WRITE_ALLOWLIST: 'acme-ns/acme-api'}\n"
)

# `<repo>@sha256:` plus 64 lowercase hex is the OCI manifest digest form
# (https://github.com/opencontainers/distribution-spec/blob/main/spec.md#pulling-manifests),
# which is what `docker buildx build --push --metadata-file` reports as
# `containerimage.digest`. Spelled out rather than derived from the
# implementation, so a change to how Curie composes the reference fails here.
DIGEST_IMAGE = (
    "ghcr.io/acme-corp/acme-bot-k8s-write-mcp@sha256:"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


def _built(root: Path) -> Path:
    """A bundle whose one connector is declared as source, with its context."""

    _bundle(root, BUILT)
    context = root / "b" / "connectors" / "k8s-write"
    context.mkdir(parents=True, exist_ok=True)
    (context / "Dockerfile").write_text(
        "FROM scratch\nCOPY server.py /server.py\n", encoding="utf-8"
    )
    (context / "server.py").write_text("print('acme')\n", encoding="utf-8")
    return root


def _write_lock(root: Path, *, image: str = DIGEST_IMAGE, delivery: str = "registry") -> None:
    from plugin_format import connector_lock
    from plugin_format.connectors import ConnectorBuild

    build = ConnectorBuild.model_validate(
        {"context": "connectors/k8s-write", "platforms": ["linux/amd64", "linux/arm64"]}
    )
    digest = connector_lock.source_digest_of(root / "b" / "connectors" / "k8s-write", build)
    (root / "b" / connector_lock.CONNECTOR_LOCK_FILE).write_text(
        "version: 1\n"
        "connectors:\n"
        "  k8s-write:\n"
        f"    image: {image}\n"
        f"    delivery: {delivery}\n"
        "    platforms: [linux/amd64, linux/arm64]\n"
        f"    source_digest: {digest}\n",
        encoding="utf-8",
    )


def test_read_connector_lock_returns_none_when_the_bundle_carries_no_lock(tmp_path: Path) -> None:
    # None is the shape every caller must handle: an ordinary `image:` bundle
    # has no lock and never will. Raising here would break every existing
    # version's render.
    assert bundles.read_connector_lock(_bundle(tmp_path, HOSTED)) is None


def test_a_built_connector_renders_the_locked_digest_and_no_build(tmp_path: Path) -> None:
    from plugin_format import connector_lock

    root = _built(tmp_path)
    _write_lock(root)
    declared = connector_lock.apply_lock(
        bundles.read_connectors(root), bundles.read_connector_lock(root), portable=False
    )
    dep = next(
        o
        for o in bundles.render_connector_manifests(
            declared,
            release=RELEASE,
            agent=AGENT,
            namespace=NAMESPACE,
            app_name=APP_NAME,
            secret_name=SECRET_NAME,
        )
        if o["kind"] == "Deployment"
    )
    assert dep["spec"]["template"]["spec"]["containers"][0]["image"] == DIGEST_IMAGE


def test_render_connector_manifests_stays_a_pure_function_of_its_arguments(tmp_path: Path) -> None:
    # The resolution happens one level up, in the router. This function keeps
    # receiving an ordinary ConnectorsFile and grew no lock parameter, which is
    # what keeps it a pure function of its arguments under ADR-0087. Handed an
    # UNRESOLVED build it must raise rather than quietly emit `image: null`.
    root = _built(tmp_path)
    _write_lock(root)
    with pytest.raises(ValueError):
        bundles.render_connector_manifests(
            bundles.read_connectors(root),
            release=RELEASE,
            agent=AGENT,
            namespace=NAMESPACE,
            app_name=APP_NAME,
            secret_name=SECRET_NAME,
        )


def test_the_mcp_entry_is_the_same_before_and_after_the_lock_is_applied(tmp_path: Path) -> None:
    # The URL is derived from the Service, never from the image, so skill,
    # local and cluster mount one byte-identical entry whether the connector was
    # built from source or pulled by an authored reference. That identity is
    # what ADR 0113's parity claim rests on, and it is why nothing in the runner
    # or in `.mcp.json` has to learn that a connector was built.
    from plugin_format import connector_lock
    from plugin_format.connectors import validate_connectors

    root = _built(tmp_path)
    _write_lock(root)
    resolved = connector_lock.apply_lock(
        bundles.read_connectors(root), bundles.read_connector_lock(root), portable=False
    )
    authored, errors = validate_connectors(
        {
            "connectors": {
                "k8s-write": {
                    "image": DIGEST_IMAGE,
                    "env": {"K8S_WRITE_ALLOWLIST": "acme-ns/acme-api"},
                }
            }
        }
    )
    assert errors == []
    kwargs = {"release": RELEASE, "agent": AGENT, "namespace": NAMESPACE}
    assert bundles.connector_mcp_entries(resolved, **kwargs) == bundles.connector_mcp_entries(
        authored, **kwargs
    )


# --------------------------------------------------------------------------- #
# Through the real route: the API consumer that must apply the lock (AC-A17)
#
# Real Postgres + real RustFS from the compose stack, matching the round-trip
# tests in test_bundles.py: nothing here is mocked. Asserting on the helper
# alone would leave the router free to keep calling `read_connectors` directly,
# which is exactly the wiring gap review finding 8 named -- the resolution was
# claimed and never wired. Deleting the `apply_lock` call in the router must
# make this fail, and it fails as `image: None`, not as an import error.
# --------------------------------------------------------------------------- #
def _archive(root: Path) -> bytes:
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(root / "b", arcname="acme-bot")
    return buf.getvalue()


def _version_with_bundle(client: Any, headers: dict[str, str], archive: bytes) -> tuple[str, str]:
    agent = client.post(
        "/agents",
        json={"name": "acme-bot", "channel": {"kind": "slack", "address": "C0EXAMPLE1"}},
        headers=headers,
    ).json()
    version = client.post(
        f"/agents/{agent['id']}/versions",
        json={"version_label": "v1", "created_by": "acme"},
        headers=headers,
    ).json()
    upload = client.put(
        f"/agents/{agent['id']}/versions/{version['id']}/bundle",
        files={"file": ("acme-bot.tar.gz", archive)},
        headers=headers,
    )
    assert upload.status_code == 201, upload.text
    return agent["id"], version["id"]


def test_the_connectors_route_renders_the_locked_digest(
    tmp_path: Path, client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    root = _built(tmp_path)
    _write_lock(root)
    agent_id, version_id = _version_with_bundle(client, auth_headers, _archive(root))

    resp = client.get(
        f"/agents/{agent_id}/versions/{version_id}/connectors",
        params={"release": RELEASE, "namespace": NAMESPACE, "app_name": APP_NAME},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    dep = next(o for o in body["manifests"] if o["kind"] == "Deployment")
    image = dep["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == DIGEST_IMAGE, "the router must apply the lock before rendering"
    # The entry is Service-derived, so it is identical to the entry an authored
    # `image:` connector of the same name produces. That is the parity claim.
    assert body["mcp_entries"]["k8s-write"]["url"].startswith(
        f"http://{RELEASE}-acme-bot-mcp-k8s-write.{NAMESPACE}.svc.cluster.local:"
    )


def test_a_lockless_build_bundle_never_becomes_a_version(
    tmp_path: Path, client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # The upload path's half of the intake rule. The bundle is refused before it
    # is stored, so the version carries no bundle_ref and the connectors route
    # has nothing to render -- rather than the deployment going active with a
    # connector that was never built.
    root = _built(tmp_path)  # no lock written
    agent = client.post(
        "/agents",
        json={"name": "acme-bot", "channel": {"kind": "slack", "address": "C0EXAMPLE1"}},
        headers=auth_headers,
    ).json()
    version = client.post(
        f"/agents/{agent['id']}/versions",
        json={"version_label": "v1", "created_by": "acme"},
        headers=auth_headers,
    ).json()
    resp = client.put(
        f"/agents/{agent['id']}/versions/{version['id']}/bundle",
        files={"file": ("acme-bot.tar.gz", _archive(root))},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text
    assert "connectors.lock_missing" in resp.text
