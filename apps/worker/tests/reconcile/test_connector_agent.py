"""Reconciling one agent (ADR-0090, #1184).

Most of these are about the operator-supplied Secret, because that is the only
way this step destroys something. Everything else it can get wrong is recoverable
on the next pass.
"""

from __future__ import annotations

from typing import Any

import pytest
from curie_worker.connector_agent import (
    RenderedConnectors,
    own,
    reconcile_agent,
)
from curie_worker.connector_reconcile import OWNER_LABEL, stamp_hash

AGENT = "sre-bot"
NS = "curie"
SECRET_NAME = "curie-sre-bot-connector-secrets"


def manifest(kind: str, name: str) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": kind, "metadata": {"name": name}, "spec": {"x": 1}}


def live_copy(obj: dict[str, Any], agent: str = AGENT) -> dict[str, Any]:
    """As the cluster would return it after the reconciler applied it."""

    stored = stamp_hash(own(obj, agent))
    stored["metadata"]["uid"] = "u-1"
    return stored


class Source:
    def __init__(self, rendered: RenderedConnectors) -> None:
        self._rendered = rendered
        self.calls: list[tuple[str, str]] = []

    def rendered(self, *, agent_id: str, version_id: str) -> RenderedConnectors:
        self.calls.append((agent_id, version_id))
        return self._rendered


class FakeClient:
    def __init__(self, live: list[dict[str, Any]] | None = None) -> None:
        self._live = live or []
        self.applied: list[str] = []
        self.deleted: list[tuple[str, str]] = []

    def list_owned(self, namespace: str, owner: str) -> list[dict[str, Any]]:
        return [
            o for o in self._live if (o["metadata"].get("labels") or {}).get(OWNER_LABEL) == owner
        ]

    def apply(self, namespace: str, obj: dict[str, Any]) -> None:
        self.applied.append(obj["metadata"]["name"])

    def delete(self, namespace: str, kind: str, name: str) -> None:
        self.deleted.append((kind, name))


def run(source: Source, client: FakeClient):
    return reconcile_agent(
        source, client, agent=AGENT, agent_id="a-1", version_id="v-1", namespace=NS
    )


# --------------------------------------------------------------------------- #
# The credential hazard
# --------------------------------------------------------------------------- #
def test_it_never_prunes_the_operator_supplied_secret() -> None:
    # THE bug this module exists for. The CLI mints that Secret from values the
    # cluster does not hold, stamps the same owner label the reconciler prunes
    # on, and the API's render never emits it. A plain "own it and no longer
    # declare it -> delete it" pass therefore deletes the credential, and every
    # connector pod fails on its next restart.
    secret = live_copy({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": SECRET_NAME}})
    dep = manifest("Deployment", "curie-sre-bot-mcp-grafana")
    client = FakeClient([secret, live_copy(dep)])
    source = Source(
        RenderedConnectors(
            manifests=[dep], owned_secret_name=SECRET_NAME, owned_secret_keys=["GRAFANA_TOKEN"]
        )
    )

    outcome = run(source, client)

    assert client.deleted == [], "the operator's credential must never be pruned"
    assert outcome.skipped is None
    assert outcome.ok


def test_the_protected_secret_is_also_never_applied() -> None:
    # We have no values for it. Applying would either fail or, worse, succeed
    # with an empty credential.
    secret = live_copy({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": SECRET_NAME}})
    client = FakeClient([secret])
    source = Source(
        RenderedConnectors(
            manifests=[manifest("Service", "svc")],
            owned_secret_name=SECRET_NAME,
            owned_secret_keys=["GRAFANA_TOKEN"],
        )
    )

    run(source, client)
    assert SECRET_NAME not in client.applied


def test_an_agent_whose_credential_was_never_provisioned_is_skipped() -> None:
    # Applying a Deployment whose secretKeyRef points at nothing yields pods
    # stuck rather than failing loudly, several layers from the cause.
    client = FakeClient([])
    source = Source(
        RenderedConnectors(
            manifests=[manifest("Deployment", "dep")],
            owned_secret_name=SECRET_NAME,
            owned_secret_keys=["GRAFANA_TOKEN"],
        )
    )

    outcome = run(source, client)

    assert outcome.skipped is not None
    assert "curie cluster deploy" in outcome.skipped, "the message must say what to do"
    # Deliberately NOT the Secret's name or its keys. Both are identifiers
    # rather than values, but they are of no use here -- the operator's action
    # is the same either way -- and leaving them out keeps a credential name out
    # of a log stream that may be shipped somewhere less protected.
    assert SECRET_NAME not in outcome.skipped
    assert "GRAFANA_TOKEN" not in outcome.skipped
    assert client.applied == [] and client.deleted == []


def test_a_reference_form_bundle_has_no_exception_at_all() -> None:
    # `secrets: [{from_secret: ...}]` (#1163) points at a Secret someone else
    # provisioned, so no key needs resolving and nothing is protected. This is
    # the path ADR-0090 calls the prerequisite.
    client = FakeClient([])
    source = Source(RenderedConnectors(manifests=[manifest("Deployment", "dep")]))

    outcome = run(source, client)

    assert outcome.skipped is None
    assert client.applied == ["dep"]


def test_a_stale_secret_is_still_pruned_when_no_keys_are_owned() -> None:
    # The protection is scoped to the credential the operator supplies. An
    # owned Secret with no owned keys is ordinary garbage and must still go, or
    # removing a connector leaves its credential behind forever.
    stale = live_copy({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "stale"}})
    client = FakeClient([stale])
    source = Source(RenderedConnectors(manifests=[]))

    run(source, client)
    assert client.deleted == [("Secret", "stale")]


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #
def test_rendered_objects_are_stamped_with_the_owner() -> None:
    # The renderer does not set it -- it is a deployment fact, not a bundle
    # fact. Unstamped, the applier refuses to write the object, and rightly so.
    client = FakeClient([])
    run(Source(RenderedConnectors(manifests=[manifest("Service", "svc")])), client)
    assert client.applied == ["svc"]


def test_own_does_not_mutate_the_rendered_object() -> None:
    # The rendered set may be reused across agents; stamping one must not label
    # it for another.
    obj = manifest("Service", "svc")
    own(obj, AGENT)
    assert "labels" not in obj["metadata"]


def test_another_agents_objects_are_never_visible() -> None:
    theirs = live_copy(manifest("Deployment", "theirs"), agent="other-bot")
    client = FakeClient([theirs])
    run(Source(RenderedConnectors(manifests=[])), client)
    assert client.deleted == []


# --------------------------------------------------------------------------- #
# Ordinary operation
# --------------------------------------------------------------------------- #
def test_a_converged_agent_does_no_work() -> None:
    dep = manifest("Deployment", "dep")
    client = FakeClient([live_copy(dep)])
    outcome = run(Source(RenderedConnectors(manifests=[dep])), client)

    assert outcome.plan is not None and outcome.plan.is_noop
    assert client.applied == [] and client.deleted == []
    assert outcome.ok


def test_the_version_asked_for_is_the_one_rendered() -> None:
    source = Source(RenderedConnectors(manifests=[]))
    run(source, FakeClient([]))
    assert source.calls == [("a-1", "v-1")]


def test_a_failed_apply_is_reported_not_raised() -> None:
    # One agent failing must not abort the pass for every other agent.
    class Exploding(FakeClient):
        def apply(self, namespace: str, obj: dict[str, Any]) -> None:
            raise RuntimeError("apiserver said no")

    outcome = run(Source(RenderedConnectors(manifests=[manifest("Service", "svc")])), Exploding([]))
    assert not outcome.ok
    assert outcome.report is not None and outcome.report.failures


@pytest.mark.parametrize("keys", [[], ["TOKEN"]])
def test_a_missing_owned_secret_name_is_not_treated_as_a_protected_object(keys) -> None:
    # An empty name would protect ("Secret", "") -- matching nothing, but also
    # skipping every agent if the absence check were written carelessly.
    client = FakeClient([])
    outcome = run(
        Source(
            RenderedConnectors(
                manifests=[manifest("Service", "svc")], owned_secret_name="", owned_secret_keys=keys
            )
        ),
        client,
    )
    assert outcome.skipped is None
    assert client.applied == ["svc"]
