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
from plugin_format.connectors import ConnectorSpec, validate_connectors

HOSTED = ConnectorSpec(
    image="grafana/mcp-grafana:0.17.2",
    args=["-t", "streamable-http", "-disable-write"],
    env={"GRAFANA_URL": "https://g.example.com"},
    secrets=["GRAFANA_TOKEN"],
)
REMOTE = ConnectorSpec(url="https://mcp.internal/mcp", headers={"Authorization": "Bearer ${T}"})


def _objs(
    release: str = "acme-bot",
    app: str = "acme-bot",
    spec: ConnectorSpec = HOSTED,
) -> list[dict]:
    return r.render(
        release=release,
        agent="acme-bot",
        namespace="acme-bot",
        app_name=app,
        connector="grafana",
        spec=spec,
        secret_name="conn-secrets",
    )


# Two NetworkPolicies ship per connector now, so selecting "the NetworkPolicy"
# by kind picks whichever happens to be first and silently tests the wrong
# object. Select by direction.
def _egress_np(objs: list) -> dict:
    return next(
        o for o in objs if o["kind"] == "NetworkPolicy" and o["spec"]["policyTypes"] == ["Egress"]
    )


def _ingress_np(objs: list) -> dict:
    return next(
        o for o in objs if o["kind"] == "NetworkPolicy" and o["spec"]["policyTypes"] == ["Ingress"]
    )


# --------------------------------------------------------------------------- #
# The ClusterIP trap -- the defect this renderer exists to prevent
# --------------------------------------------------------------------------- #
def test_egress_rule_uses_a_podselector_never_an_ipblock() -> None:
    # A NetworkPolicy naming a Service ClusterIP can NEVER match: kube-proxy
    # DNATs the destination to a pod IP before the policy is evaluated. The
    # symptom is a bare connection refused, and on a CNI that ignores
    # NetworkPolicy (minikube's default) the broken rule looks identical to a
    # correct one -- so it survives local testing and fails in a real cluster.
    np = _egress_np(_objs())
    to = np["spec"]["egress"][0]["to"][0]
    assert "podSelector" in to
    assert "ipBlock" not in to


def test_egress_selects_exactly_the_pods_rail_1_denies() -> None:
    # Too narrow and the allow widens nothing (NetworkPolicy is additive, it
    # cannot narrow -- ADR-0067) so the sandbox still cannot reach the
    # connector. Too broad -- e.g. only `component` -- and it also grants egress
    # to every OTHER release's sandboxes in the namespace. Both fail silently.
    np = _egress_np(
        r.render(
            release="relA",
            agent="a",
            namespace="ns",
            app_name="acme-bot",
            connector="g",
            spec=HOSTED,
            secret_name="s",
        )
    )
    assert np["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "acme-bot",
        "app.kubernetes.io/instance": "relA",
        "app.kubernetes.io/component": "runner-sandbox",
    }


def test_two_releases_do_not_select_each_others_sandboxes() -> None:
    a = _egress_np(
        r.render(
            release="relA",
            agent="a",
            namespace="ns",
            app_name="app",
            connector="g",
            spec=HOSTED,
            secret_name="s",
        )
    )
    b = _egress_np(
        r.render(
            release="relB",
            agent="a",
            namespace="ns",
            app_name="app",
            connector="g",
            spec=HOSTED,
            secret_name="s",
        )
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
    aliases = r.host_aliases("acme-bot", "a", "grafana", "ns", 8000)
    assert "acme-bot-a-mcp-grafana:8000" in aliases
    assert "acme-bot-a-mcp-grafana.ns:8000" in aliases
    assert "acme-bot-a-mcp-grafana.ns.svc.cluster.local:8000" in aliases


def test_injected_url_matches_the_service_that_was_rendered() -> None:
    # Hand-writing this URL is how a bundle ends up with an address that does
    # not resolve in the tier it is deployed to.
    svc = next(o for o in _objs() if o["kind"] == "Service")
    url = r.mcp_entry("acme-bot", "acme-bot", "acme-bot", "grafana", HOSTED)["url"]
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
    assert (
        r.render(
            release="acme-bot",
            agent="a",
            namespace="ns",
            app_name="app",
            connector="internal",
            spec=REMOTE,
            secret_name="s",
        )
        == []
    )


def test_remote_connector_keeps_its_own_url_and_headers() -> None:
    entry = r.mcp_entry("acme-bot", "a", "ns", "internal", REMOTE)
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
        ["helm", "template", "myrel", str(chart), "--set", "nameOverride=acme-bot"],
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
    assert r.sandbox_selector("myrel", "acme-bot") == chart_selector


# --------------------------------------------------------------------------- #
# Cross-AGENT collision -- #1116
# --------------------------------------------------------------------------- #
DEV = ConnectorSpec(image="grafana/mcp-grafana:0.17.2", env={"GRAFANA_URL": "https://dev.g"})
PROD = ConnectorSpec(image="grafana/mcp-grafana:0.17.2", env={"GRAFANA_URL": "https://prod.g"})


def test_two_agents_in_one_release_do_not_share_object_names() -> None:
    # Curie runs many agents per release. Release-scoped names meant acme-dev and
    # sre-prod both rendered `curie-mcp-grafana`, so deploying prod silently
    # repointed the DEV agent at the prod endpoint -- and, because the Secret was
    # release-scoped too, handed it the prod token. Nothing errored.
    dev = [
        o["metadata"]["name"]
        for o in r.render(
            release="curie",
            agent="acme-dev",
            namespace="ns",
            app_name="curie",
            connector="grafana",
            spec=DEV,
            secret_name="s",
        )
    ]
    prod = [
        o["metadata"]["name"]
        for o in r.render(
            release="curie",
            agent="sre-prod",
            namespace="ns",
            app_name="curie",
            connector="grafana",
            spec=PROD,
            secret_name="s",
        )
    ]
    assert not set(dev) & set(prod), f"agents share object names: {dev} vs {prod}"


def test_two_agents_do_not_share_pod_labels() -> None:
    # The Service selector is these labels. Sharing them would route one agent's
    # traffic to the other's pods even with distinct object names.
    dev = next(
        o
        for o in r.render(
            release="curie",
            agent="acme-dev",
            namespace="ns",
            app_name="curie",
            connector="grafana",
            spec=DEV,
            secret_name="s",
        )
        if o["kind"] == "Service"
    )
    prod = next(
        o
        for o in r.render(
            release="curie",
            agent="sre-prod",
            namespace="ns",
            app_name="curie",
            connector="grafana",
            spec=PROD,
            secret_name="s",
        )
        if o["kind"] == "Service"
    )
    assert dev["spec"]["selector"] != prod["spec"]["selector"]


def test_each_agent_gets_its_own_url() -> None:
    dev = r.mcp_entry("curie", "acme-dev", "ns", "grafana", DEV)["url"]
    prod = r.mcp_entry("curie", "sre-prod", "ns", "grafana", PROD)["url"]
    assert dev != prod


def test_over_long_names_stay_valid_dns_labels() -> None:
    name = r.object_name("a" * 30, "b" * 30, "c" * 40)
    assert len(name) <= 63
    assert name[0].isalnum() and name[-1].isalnum()


def test_over_long_names_that_share_a_prefix_still_differ() -> None:
    # Clipping alone would map these onto one object, reintroducing the very
    # collision the agent scoping exists to prevent.
    a = r.object_name("release", "agent-with-a-very-long-name-number-one", "grafana")
    b = r.object_name("release", "agent-with-a-very-long-name-number-two", "grafana")
    assert len(a) <= 63 and len(b) <= 63
    assert a != b


# --------------------------------------------------------------------------- #
# Placeholders: values only Curie can know -- #1156
# --------------------------------------------------------------------------- #
HOSTED_WITH_HOSTS = ConnectorSpec(
    image="grafana/mcp-grafana:0.17.2",
    args=["-t", "streamable-http", "-allowed-hosts", "${CURIE_ALLOWED_HOSTS}"],
)


def _dep(agent: str = "acme-dev", spec: ConnectorSpec = HOSTED_WITH_HOSTS) -> dict:
    return next(
        o
        for o in r.render(
            release="acme-bot",
            agent=agent,
            namespace="acme-bot",
            app_name="acme-bot",
            connector="grafana",
            spec=spec,
            secret_name="s",
        )
        if o["kind"] == "Deployment"
    )


def test_allowed_hosts_expands_to_every_name_the_sandbox_could_dial() -> None:
    # Servers that guard against DNS rebinding default their allowlist to
    # loopback, so without this the connector starts and answers every in-cluster
    # call with `forbidden: host not allowed` -- healthy in `kubectl get pods`,
    # working for nobody.
    args = _dep()["spec"]["template"]["spec"]["containers"][0]["args"]
    value = args[args.index("-allowed-hosts") + 1]
    assert "${" not in value, "placeholder reached the container unsubstituted"
    for alias in r.host_aliases("acme-bot", "acme-dev", "grafana", "acme-bot", 8000):
        assert alias in value


def test_each_agent_gets_its_own_allowlist() -> None:
    # Since #1116 the Service name is agent-scoped, so one hardcoded allowlist
    # cannot serve two agents built from the same bundle.
    def hosts(agent: str) -> str:
        a = _dep(agent)["spec"]["template"]["spec"]["containers"][0]["args"]
        return a[a.index("-allowed-hosts") + 1]

    assert hosts("acme-dev") != hosts("sre-prod")


def test_placeholders_expand_in_env_too() -> None:
    spec = ConnectorSpec(image="x:1", env={"SELF_URL": "${CURIE_CONNECTOR_URL}"})
    env = _dep(spec=spec)["spec"]["template"]["spec"]["containers"][0]["env"]
    entry = next(e for e in env if e["name"] == "SELF_URL")
    assert entry["value"].startswith("http://acme-bot-acme-dev-mcp-grafana.acme-bot")


def test_text_without_placeholders_is_untouched() -> None:
    spec = ConnectorSpec(image="x:1", args=["-t", "streamable-http"], env={"A": "b"})
    c = _dep(spec=spec)["spec"]["template"]["spec"]["containers"][0]
    assert c["args"] == ["-t", "streamable-http"]
    assert {"name": "A", "value": "b"} in c["env"]


# --------------------------------------------------------------------------- #
# What an unhostable tier mounts -- #1160
# --------------------------------------------------------------------------- #
def test_a_hosted_connector_with_a_fallback_is_reachable_where_it_cannot_be_hosted() -> None:
    spec = ConnectorSpec(image="x:1", unhosted_url="http://host.docker.internal:8765/mcp")
    assert r.unhosted_mcp_entry(spec) == {
        "type": "http",
        "url": "http://host.docker.internal:8765/mcp",
    }


def test_a_hosted_connector_with_no_fallback_mounts_nothing_rather_than_a_dead_url() -> None:
    # None is a real answer: "declared but not exercisable here" (#1093). A URL
    # that resolves nowhere would turn that into a connection refused mid-turn.
    assert r.unhosted_mcp_entry(ConnectorSpec(image="x:1")) is None


def test_a_remote_connector_needs_no_fallback_to_stay_reachable() -> None:
    entry = r.unhosted_mcp_entry(REMOTE)
    assert entry is not None
    assert entry["url"] == "https://mcp.internal/mcp"


def test_the_fallback_never_displaces_the_derived_url_where_curie_hosts() -> None:
    # The whole point is that `cluster` keeps hosting it. A fallback that won
    # everywhere would silently repoint a production agent at someone's laptop.
    spec = ConnectorSpec(image="x:1", unhosted_url="http://host.docker.internal:8765/mcp")
    hosted = r.mcp_entry("curie", "acme-dev", "curie", "grafana", spec)
    assert "svc.cluster.local" in hosted["url"]
    assert "8765" not in hosted["url"]


# --------------------------------------------------------------------------- #
# A referenced Secret renders the same shape as an owned one -- #1163
# --------------------------------------------------------------------------- #
def test_a_referenced_secret_points_at_the_secret_the_author_named() -> None:
    from plugin_format.connectors import SecretRef

    spec = ConnectorSpec(image="x:1", secrets=[SecretRef(name="TOKEN", from_secret="grafana-mcp")])
    dep = next(
        o
        for o in r.render(
            release="rel",
            agent="ag",
            namespace="ns",
            app_name="app",
            connector="g",
            spec=spec,
            secret_name="curie-owned",
        )
        if o["kind"] == "Deployment"
    )
    entry = next(
        e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "TOKEN"
    )
    ref = entry["valueFrom"]["secretKeyRef"]
    assert ref["name"] == "grafana-mcp", "must point at the out-of-band Secret, not Curie's"
    assert ref["key"] == "TOKEN"
    assert "value" not in entry


def test_owned_and_referenced_secrets_are_indistinguishable_to_the_container() -> None:
    # Both render a secretKeyRef and never a literal. The container cannot tell
    # which is which, so nothing downstream needs to care.
    from plugin_format.connectors import SecretRef

    spec = ConnectorSpec(image="x:1", secrets=["OWNED", SecretRef(name="REFD", from_secret="ext")])
    dep = next(
        o
        for o in r.render(
            release="rel",
            agent="ag",
            namespace="ns",
            app_name="app",
            connector="g",
            spec=spec,
            secret_name="curie-owned",
        )
        if o["kind"] == "Deployment"
    )
    env = {e["name"]: e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["OWNED"]["valueFrom"]["secretKeyRef"]["name"] == "curie-owned"
    assert env["REFD"]["valueFrom"]["secretKeyRef"]["name"] == "ext"
    for name in ("OWNED", "REFD"):
        assert "value" not in env[name], f"{name} must never be inlined"


def test_a_referenced_secret_is_not_optional() -> None:
    # A missing referenced Secret must stop the pod, not start it credential-less
    # and 401 on every call -- which reads as "the tool is broken".
    from plugin_format.connectors import SecretRef

    spec = ConnectorSpec(image="x:1", secrets=[SecretRef(name="T", from_secret="ext")])
    dep = next(
        o
        for o in r.render(
            release="rel",
            agent="ag",
            namespace="ns",
            app_name="app",
            connector="g",
            spec=spec,
            secret_name="s",
        )
        if o["kind"] == "Deployment"
    )
    entry = next(
        e for e in dep["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "T"
    )
    assert entry["valueFrom"]["secretKeyRef"]["optional"] is False


# -- sealed_secrets: the ADR-0094 contract slice ------------------------------


def test_sealed_secrets_is_optional_and_absent_by_default() -> None:
    """Additive: an existing bundle must be unaffected by the new field."""

    spec = ConnectorSpec(image="grafana/mcp-grafana:0.17.2")
    assert spec.sealed_secrets == {}
    assert spec.secret_names() == []


def test_sealed_secret_names_join_the_other_forms() -> None:
    """All three holders answer the same question, so one list answers it."""

    spec = ConnectorSpec(
        image="x",
        secrets=["PLAIN"],
        sealed_secrets={"SEALED": "AgBv3n2K"},
    )
    assert spec.secret_names() == ["PLAIN", "SEALED"]
    # Only the plain one is Curie's to resolve a value for.
    assert spec.resolved_secrets() == ["PLAIN"]


def test_a_declared_sealed_secret_is_refused_until_decryption_exists() -> None:
    parsed, errors = validate_connectors(
        {"connectors": {"grafana": {"image": "x", "sealed_secrets": {"TOKEN": "AgB"}}}}
    )
    assert "connectors.sealed_secrets_unsupported" in [code for code, _ in errors]
    assert parsed is None
    diagnostic = next(
        message for code, message in errors if code == "connectors.sealed_secrets_unsupported"
    )
    assert "secrets:" in diagnostic


def test_an_empty_sealed_blob_is_reported_on_its_own() -> None:
    """A distinct diagnostic, so the eventual decrypt path inherits the check."""

    _, errors = validate_connectors(
        {"connectors": {"grafana": {"image": "x", "sealed_secrets": {"TOKEN": "  "}}}}
    )
    assert "connectors.empty_sealed_value" in [code for code, _ in errors]


# --- secret_files (#1402) ---------------------------------------------------
#
# `secrets:` renders a secretKeyRef and nothing else, so a server that
# authenticates from a FILE -- a kubeconfig, a TLS client cert, GCP
# service-account JSON -- had no way to receive its credential at all. These
# pin the projection, and the guards that keep it from becoming a second, worse
# way to leak one.

FILE_SPEC = ConnectorSpec(
    image="ghcr.io/containers/kubernetes-mcp-server:latest",
    args=["--kubeconfig", "/secrets/kubeconfig", "--read-only"],
    secret_files={"K8S_READONLY_KUBECONFIG": "/secrets/kubeconfig"},
)


def _file_dep(spec: ConnectorSpec) -> dict:
    return r.render_deployment("acme-bot", "acme-bot", "acme-bot", "k8s", spec, "conn-secrets")


def test_secret_file_lands_at_the_declared_path_not_a_directory() -> None:
    # subPath is what makes the mount a FILE at exactly the declared path.
    # Without it Kubernetes mounts a DIRECTORY there, and `--kubeconfig
    # /secrets/kubeconfig` would open a directory and fail in a way that reads
    # like a malformed credential.
    mount = _file_dep(FILE_SPEC)["spec"]["template"]["spec"]["containers"][0]["volumeMounts"][0]
    assert mount["mountPath"] == "/secrets/kubeconfig"
    assert mount["subPath"] == "kubeconfig"
    assert mount["readOnly"] is True


def test_secret_file_reads_the_same_per_agent_secret_as_env_secrets() -> None:
    # Same store, same Secret, same resolution -- only the delivery differs.
    vol = _file_dep(FILE_SPEC)["spec"]["template"]["spec"]["volumes"][0]["secret"]
    assert vol["secretName"] == "conn-secrets"
    assert vol["items"] == [{"key": "K8S_READONLY_KUBECONFIG", "path": "kubeconfig"}]


def test_secret_file_is_readable_by_the_nonroot_user_that_must_open_it() -> None:
    # Regression: this shipped as 0400 and the server died with
    #   open /secrets/kubeconfig: permission denied
    # on a file that was mounted correctly. Secret volume files are owned by
    # root:fsGroup, NOT by runAsUser, so owner-only is unreadable by the 65532
    # the container runs as. 0440 + a matching fsGroup is the narrowest pair
    # that actually opens.
    pod = _file_dep(FILE_SPEC)["spec"]["template"]["spec"]
    assert pod["volumes"][0]["secret"]["defaultMode"] == 0o440
    assert pod["securityContext"]["fsGroup"] == pod["securityContext"]["runAsUser"]


def test_secret_file_is_not_optional() -> None:
    # Matches `secrets:`: a missing key must stop the pod rather than start a
    # server that 401s on every call.
    vol = _file_dep(FILE_SPEC)["spec"]["template"]["spec"]["volumes"][0]["secret"]
    assert vol["optional"] is False


def test_fsgroup_is_absent_when_nothing_is_projected() -> None:
    # fsGroup applies to EVERY volume in the pod, so it is set only when a
    # secret is actually projected as a file.
    assert "fsGroup" not in _file_dep(HOSTED)["spec"]["template"]["spec"]["securityContext"]


def test_each_secret_file_gets_its_own_volume() -> None:
    # One volume per file, so each carries only its own key. A single volume
    # with several items would put every credential in one directory, where a
    # server that reads a directory would see the others.
    spec = ConnectorSpec(
        image="x:1",
        secret_files={"A": "/secrets/a.pem", "B": "/secrets/b.json"},
    )
    pod = _file_dep(spec)["spec"]["template"]["spec"]
    assert len(pod["volumes"]) == 2
    keys = sorted(v["secret"]["items"][0]["key"] for v in pod["volumes"])
    assert keys == ["A", "B"]
    for v in pod["volumes"]:
        assert len(v["secret"]["items"]) == 1


def test_a_connector_without_secret_files_is_unchanged() -> None:
    # Additive to a frozen contract: a bundle that does not use this must
    # render exactly what it rendered before, with no empty volumes key.
    pod = _file_dep(HOSTED)["spec"]["template"]["spec"]
    assert "volumes" not in pod
    assert "volumeMounts" not in pod["containers"][0]


def test_secret_files_are_reported_as_both_declared_and_resolved() -> None:
    # `secret_names()` feeds the duplicate-name validator; `resolved_secrets()`
    # is what the deploy path is told to WRITE into the per-agent Secret. The
    # second was missing, so the volume pointed at a key nobody wrote.
    assert "K8S_READONLY_KUBECONFIG" in FILE_SPEC.secret_names()
    assert "K8S_READONLY_KUBECONFIG" in FILE_SPEC.resolved_secrets()


def test_every_rendered_volume_key_is_one_resolved_secrets_claims() -> None:
    # Why this invariant matters, and what #1424 violated. A secret volume is
    # rendered `optional: false`, so a key the deploy path never resolved is
    # not a degraded connector: the kubelet cannot mount it and the pod never
    # starts, stuck on `FailedMount ... secret not found`. Pinned as the class
    # -- every projected key must be one `resolved_secrets()` claims -- so the
    # fix cannot be satisfied by handling only this one spec's shape.
    # Boundary: this pins the renderer against the accessor in process; the
    # deploy path itself is covered by the API-level test in
    # `apps/api/tests/test_bundle_connectors.py`.
    spec = ConnectorSpec(
        image="x:1",
        secrets=["ENV_TOKEN"],
        secret_files={"KUBECONFIG": "/secrets/kubeconfig", "CA_BUNDLE": "/secrets/ca.pem"},
    )
    resolved = spec.resolved_secrets()
    projected = [
        item["key"]
        for vol in _file_dep(spec)["spec"]["template"]["spec"]["volumes"]
        for item in vol["secret"]["items"]
    ]
    assert projected, "expected the spec to project at least one key"
    for key in projected:
        assert key in resolved, f"volume projects {key!r}, absent from resolved_secrets()"


@pytest.mark.parametrize(
    "data,code",
    [
        (
            {"image": "x:1", "secret_files": {"A": "secrets/x"}},
            "connectors.secret_file_relative_path",
        ),
        ({"image": "x:1", "secret_files": {"A": "/tmp/x"}}, "connectors.secret_file_in_tmp"),
        (
            {"image": "x:1", "secret_files": {"A": "/s/x", "B": "/s/x"}},
            "connectors.secret_file_path_collision",
        ),
        (
            {"image": "x:1", "secrets": ["A"], "secret_files": {"A": "/s/x"}},
            "connectors.secret_both_env_and_file",
        ),
        (
            {"url": "https://m/mcp", "secret_files": {"A": "/s/x"}},
            "connectors.remote_has_secret_files",
        ),
    ],
)
def test_secret_file_misuse_is_refused(data: dict, code: str) -> None:
    _, errors = validate_connectors({"connectors": {"k": data}})
    assert code in [c for c, _ in errors]


def test_a_well_formed_secret_file_validates() -> None:
    parsed, errors = validate_connectors(
        {"connectors": {"k": {"image": "x:1", "secret_files": {"A": "/secrets/kubeconfig"}}}}
    )
    assert errors == []
    assert parsed is not None


# --------------------------------------------------------------------------- #
# Ingress: who may REACH the connector
# --------------------------------------------------------------------------- #
def test_connector_accepts_traffic_only_from_the_sandbox() -> None:
    # The egress policy governs where the sandbox may go; it places no limit on
    # who may arrive. Without an ingress rule every pod in the namespace can
    # call a connector that holds a production credential and authenticates
    # nobody -- because the sandbox has no credential to authenticate WITH, so
    # the network is the entire access control.
    np = _ingress_np(
        r.render(
            release="release-r",
            agent="agent-a",
            namespace="namespace-n",
            app_name="app-name",
            connector="connector-c",
            spec=HOSTED,
            secret_name="connector-secret",
        )
    )
    src = np["spec"]["ingress"][0]["from"]
    assert len(src) == 1, "exactly one source: the sandbox"
    assert src[0]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "app-name",
        "app.kubernetes.io/instance": "release-r",
        "app.kubernetes.io/component": "runner-sandbox",
    }
    assert set(src[0]) == {"podSelector"}
    assert src[0]["podSelector"]["matchLabels"] == r.sandbox_selector("release-r", "app-name")


def test_ingress_policy_selects_the_connector_not_the_sandbox() -> None:
    # Getting this backwards yields a policy that parses, applies, and protects
    # the wrong pod -- the sandbox gains an ingress restriction it does not need
    # while the connector keeps none.
    objs = r.render(
        release="release-r",
        agent="agent-a",
        namespace="namespace-n",
        app_name="app-name",
        connector="connector-c",
        spec=HOSTED,
        secret_name="connector-secret",
    )
    ing = _ingress_np(objs)
    egr = _egress_np(objs)
    # The two policies must select OPPOSITE ends of the same hop: egress is
    # attached to the sandbox, ingress to the connector. Swapping them still
    # parses and still applies -- it just protects the wrong pod.
    assert ing["spec"]["podSelector"] != egr["spec"]["podSelector"]
    assert (
        ing["spec"]["podSelector"]["matchLabels"]
        == egr["spec"]["egress"][0]["to"][0]["podSelector"]["matchLabels"]
    ), "ingress must select the pod the egress rule points AT"
    assert (
        egr["spec"]["podSelector"]["matchLabels"]
        == ing["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
    ), "ingress must admit the pod the egress rule is attached to"
    dep = next(o for o in objs if o["kind"] == "Deployment")
    assert ing["spec"]["podSelector"]["matchLabels"] == dep["spec"]["template"]["metadata"][
        "labels"
    ]


def test_ingress_is_port_scoped_to_the_connector_port() -> None:
    ports = _ingress_np(_objs())["spec"]["ingress"][0]["ports"]
    assert ports == [{"protocol": "TCP", "port": HOSTED.port}]


def test_ingress_uses_a_podselector_never_an_ipblock() -> None:
    # Same trap as the egress rule: a ClusterIP ipBlock can never match, and it
    # looks correct on a CNI that ignores NetworkPolicy.
    src = _ingress_np(_objs())["spec"]["ingress"][0]["from"][0]
    assert "podSelector" in src and "ipBlock" not in src


def test_two_releases_do_not_admit_each_others_sandboxes() -> None:
    # A too-broad source selector would let another release's sandbox read
    # production through this connector, silently.
    a = _ingress_np(
        r.render(
            release="relA",
            agent="a",
            namespace="ns",
            app_name="app",
            connector="g",
            spec=HOSTED,
            secret_name="s",
        )
    )
    b = _ingress_np(
        r.render(
            release="relB",
            agent="a",
            namespace="ns",
            app_name="app",
            connector="g",
            spec=HOSTED,
            secret_name="s",
        )
    )
    assert a["spec"]["ingress"][0]["from"] != b["spec"]["ingress"][0]["from"]


def test_the_two_policies_do_not_collide_on_name() -> None:
    names = [o["metadata"]["name"] for o in _objs() if o["kind"] == "NetworkPolicy"]
    assert len(names) == len(set(names)) == 2


def test_a_remote_connector_renders_no_policies() -> None:
    # Nothing is hosted, so there is nothing in-cluster to admit traffic to.
    assert not [
        o
        for o in r.render(
            release="rel",
            agent="a",
            namespace="ns",
            app_name="app",
            connector="x",
            spec=REMOTE,
            secret_name="s",
        )
    ]


# --------------------------------------------------------------------------- #
# `spec.port` is read in six places, and every one of them has to read the
# DECLARED port. The default is 8000, so a reader that hardcodes it keeps
# working for every connector that never sets `port:` and breaks only for the
# ones that do -- silently, and with a different symptom per reader. These
# render at TWO different NON-default ports for exactly that reason: asserting
# at 8000 would pin nothing, and asserting at one other port would pin nothing
# against a reader that hardcodes THAT value instead.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("port", [9876, 9999])
def test_a_wrong_container_port_leaves_the_service_routing_to_a_refused_port(port: int) -> None:
    # The Service names its targetPort "http", so the container's port NAME is
    # what resolves it. A containerPort that is not the declared port makes
    # "http" resolve to a port nothing listens on, and every call is refused.
    spec = ConnectorSpec(image="grafana/mcp-grafana:0.17.2", port=port)
    container = next(o for o in _objs(spec=spec) if o["kind"] == "Deployment")["spec"]["template"][
        "spec"
    ]["containers"][0]
    assert container["ports"] == [{"name": "http", "containerPort": port}]


@pytest.mark.parametrize("port", [9876, 9999])
def test_a_wrong_egress_port_leaves_rail_1_denying_the_port_the_connector_listens_on(
    port: int,
) -> None:
    # Rail 1 is default-deny, so this rule is the only thing that opens the hop.
    # Opened on the wrong port it denies the real one, and the sandbox's call
    # times out with no policy error anywhere to say why.
    spec = ConnectorSpec(image="grafana/mcp-grafana:0.17.2", port=port)
    egress = _egress_np(_objs(spec=spec))["spec"]["egress"][0]
    assert egress["ports"] == [{"protocol": "TCP", "port": port}]


@pytest.mark.parametrize("port", [9876, 9999])
def test_a_wrong_url_port_makes_the_sandbox_dial_a_port_nothing_serves(port: int) -> None:
    # The author never writes this URL, so a hardcoded port here is not
    # correctable from the bundle: the sandbox dials the wrong port on the right
    # pod and the MCP server never answers.
    spec = ConnectorSpec(image="grafana/mcp-grafana:0.17.2", port=port)
    url = r.mcp_entry("acme-rel", "acme-bot", "acme-ns", "grafana", spec)["url"]
    assert url == f"http://acme-rel-acme-bot-mcp-grafana.acme-ns.svc.cluster.local:{port}/mcp"


@pytest.mark.parametrize("port", [9876, 9999])
def test_connector_env_port_placeholders_use_the_declared_port(port: int) -> None:
    spec = ConnectorSpec(
        image="grafana/mcp-grafana:0.17.2",
        port=port,
        env={
            "ALLOWED_HOSTS": "${CURIE_ALLOWED_HOSTS}",
            "CONNECTOR_PORT": "${CURIE_CONNECTOR_PORT}",
            "CONNECTOR_URL": "${CURIE_CONNECTOR_URL}",
        },
    )
    env = next(o for o in _objs(spec=spec) if o["kind"] == "Deployment")["spec"]["template"][
        "spec"
    ]["containers"][0]["env"]
    assert {entry["name"]: entry["value"] for entry in env} == {
        "ALLOWED_HOSTS": (
            f"acme-bot-acme-bot-mcp-grafana:{port},"
            f"acme-bot-acme-bot-mcp-grafana.acme-bot:{port},"
            f"acme-bot-acme-bot-mcp-grafana.acme-bot.svc.cluster.local:{port}"
        ),
        "CONNECTOR_PORT": str(port),
        "CONNECTOR_URL": (
            f"http://acme-bot-acme-bot-mcp-grafana.acme-bot.svc.cluster.local:{port}"
        ),
    }
