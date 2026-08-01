"""The connector reconciler against a real cluster (ADR-0090, #1184).

Gated: runs only with ``CURIE_CONNECTOR_E2E=1`` and a reachable cluster, the
same shape as ``sandbox/test_e2e_k8scratch.py``. Everything else in this
directory runs against a fake, which is right for the decision logic and
useless for the questions here -- whether a server-side apply of a Service
without a clusterIP is accepted, whether items in a List carry a `kind`,
whether a Secret survives the stringData/data round trip. A fake answers all
three however it was written to.

Creates and destroys its own namespace, so it collides with nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from curie_worker.connector_apply import execute
from curie_worker.connector_k8s import KubernetesConnectorClient
from curie_worker.connector_reconcile import OWNER_LABEL, plan

pytestmark = pytest.mark.skipif(
    os.environ.get("CURIE_CONNECTOR_E2E") != "1",
    reason="cluster e2e; set CURIE_CONNECTOR_E2E=1 with a reachable KUBECONFIG",
)

AGENT = "e2e-agent"
OTHER_AGENT = "e2e-other"


def kubectl(*args: str) -> str:
    return subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def namespace() -> Iterator[str]:
    name = f"curie-conn-e2e-{uuid.uuid4().hex[:8]}"
    kubectl("create", "namespace", name)
    try:
        yield name
    finally:
        subprocess.run(
            ["kubectl", "delete", "namespace", name, "--wait=false"], capture_output=True
        )


@pytest.fixture(scope="module")
def client() -> KubernetesConnectorClient:
    return KubernetesConnectorClient()


def connector_objects(agent: str, name: str) -> list[dict[str, Any]]:
    """One connector's worth of objects: the four kinds, minimally shaped."""

    labels = {OWNER_LABEL: agent}
    selector = {"app.kubernetes.io/name": name}
    return [
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "labels": labels},
            # No clusterIP -- the field a create-then-replace applier cannot
            # round-trip, since the server assigns it and then rejects "".
            "spec": {"selector": selector, "ports": [{"name": "http", "port": 8080}]},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": selector},
                "template": {
                    "metadata": {"labels": {**selector, **labels}},
                    "spec": {
                        "containers": [
                            {
                                "name": "server",
                                "image": "ghcr.io/example/mcp-example:1",
                                "ports": [{"name": "http", "containerPort": 8080}],
                            }
                        ]
                    },
                },
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": f"{name}-allow", "labels": labels},
            "spec": {"podSelector": {"matchLabels": selector}, "policyTypes": ["Ingress"]},
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {"name": f"{name}-secrets", "labels": labels},
            "stringData": {"EXAMPLE_TOKEN": "fixture-value-not-real"},
        },
    ]


def reconcile(
    client: KubernetesConnectorClient, namespace: str, desired: list[dict[str, Any]], agent: str
) -> tuple[Any, Any]:
    live = client.list_owned(namespace, agent)
    computed = plan(desired, live, agent=agent)
    return computed, execute(client, computed, namespace=namespace, agent=agent)


def test_the_reconciler_converges_and_stays_converged(client, namespace) -> None:
    desired = connector_objects(AGENT, "conn-a")

    computed, report = reconcile(client, namespace, desired, AGENT)
    assert report.ok, report.failures
    assert len(report.applied) == 4

    # The property the whole drift design exists for. A second pass that still
    # applies is a loop that rewrites the cluster forever.
    for _ in range(2):
        computed, report = reconcile(client, namespace, desired, AGENT)
        assert computed.is_noop, f"not converged: apply={computed.apply} drift={computed.drifted}"


def test_a_human_edit_is_detected_and_reverted(client, namespace) -> None:
    desired = connector_objects(AGENT, "conn-a")
    reconcile(client, namespace, desired, AGENT)

    kubectl("-n", namespace, "set", "image", "deploy/conn-a", "server=ghcr.io/example/tampered:9")
    computed, report = reconcile(client, namespace, desired, AGENT)

    assert computed.drifted == [("Deployment", "conn-a")]
    assert report.ok, report.failures
    live_image = kubectl(
        "-n",
        namespace,
        "get",
        "deploy",
        "conn-a",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].image}",
    )
    assert live_image == "ghcr.io/example/mcp-example:1"


def test_a_secret_does_not_drift_on_the_encoding_round_trip(client, namespace) -> None:
    desired = connector_objects(AGENT, "conn-a")
    reconcile(client, namespace, desired, AGENT)

    stored = json.loads(kubectl("-n", namespace, "get", "secret", "conn-a-secrets", "-o", "json"))
    assert "data" in stored and "stringData" not in stored, (
        "the server re-encodes; that is the point"
    )

    computed, _ = reconcile(client, namespace, desired, AGENT)
    assert computed.is_noop


def test_list_owned_returns_objects_the_plan_can_identify(client, namespace) -> None:
    # Items inside a List carry no `kind` of their own. Unstamped, every object
    # has an empty kind, matches nothing declared, and is planned for deletion.
    reconcile(client, namespace, connector_objects(AGENT, "conn-a"), AGENT)
    live = client.list_owned(namespace, AGENT)
    assert {o["kind"] for o in live} == {"Service", "Deployment", "NetworkPolicy", "Secret"}
    assert all(o.get("apiVersion") for o in live)


def test_one_agent_never_prunes_anothers_connector(client, namespace) -> None:
    # Two agents in one namespace each own a connector (#1116). The first
    # dropping its own must not touch the second's.
    mine = connector_objects(AGENT, "conn-a")
    theirs = connector_objects(OTHER_AGENT, "conn-b")
    for desired, agent in ((mine, AGENT), (theirs, OTHER_AGENT)):
        # Asserted rather than assumed: a flaky apply during setup otherwise
        # surfaces below as "the other agent's connector was disturbed", which
        # points at the wrong thing entirely.
        _, setup = reconcile(client, namespace, desired, agent)
        assert setup.ok, f"setup for {agent} did not apply cleanly: {setup.failures}"

    _, report = reconcile(client, namespace, [], AGENT)
    assert len(report.deleted) == 4

    computed, _ = reconcile(client, namespace, theirs, OTHER_AGENT)
    assert computed.is_noop, "the other agent's connector was disturbed"
    assert len(client.list_owned(namespace, OTHER_AGENT)) == 4


def test_a_handwritten_object_is_never_touched(client, namespace) -> None:
    # sre-bot ran a hand-written connector beside a Curie-managed one through
    # its whole migration. An unlabelled object must be invisible.
    kubectl("-n", namespace, "create", "service", "clusterip", "handwritten", "--tcp=8080:8080")
    reconcile(client, namespace, [], AGENT)
    assert kubectl("-n", namespace, "get", "svc", "handwritten", "-o", "name")
