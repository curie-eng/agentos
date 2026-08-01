"""Drift detection against what an API server really returns (ADR-0090, #1184).

Every case here runs against `fixtures/live_connector_objects.json`, captured
from a real cluster by applying the renderer's own output and reading it back.
That matters more than it sounds: the bug these tests exist for is invisible to
hand-written fixtures, because it is caused by fields nobody writes down --
`clusterIP`, `ipFamilies`, `progressDeadlineSeconds`, a `protocol: TCP` the
server inserts into a port declared without one, and an `args: []` the server
drops rather than stores.

Plain equality reported the captured Service and Deployment as drifted the
instant after a successful apply. The NetworkPolicy compared equal, because it
acquires no defaults -- so a suite written around a NetworkPolicy would have
passed while the reconciler rewrote every Deployment in the cluster forever.
"""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
from typing import Any

import pytest
from curie_worker.connector_reconcile import (
    HASH_ANNOTATION,
    OWNER_LABEL,
    _contains,
    _curie_owned_view,
    plan,
    stamp_hash,
)

AGENT = "driftcheck"
FIXTURE = Path(__file__).parent / "fixtures" / "live_connector_objects.json"


@pytest.fixture
def captured() -> dict[str, list[dict[str, Any]]]:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def desired(captured: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return copy.deepcopy(captured["desired"])


@pytest.fixture
def live(captured: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return copy.deepcopy(captured["live"])


def one(objs: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return next(o for o in objs if o["kind"] == kind)


# --------------------------------------------------------------------------- #
# The hot loop
# --------------------------------------------------------------------------- #
def test_a_freshly_applied_object_set_is_already_converged(desired, live) -> None:
    # The whole point. If this fails the reconciler rewrites every object on
    # every pass and logs "drift corrected" forever.
    assert plan(desired, live, agent=AGENT).is_noop


@pytest.mark.parametrize("kind", ["Service", "Deployment", "NetworkPolicy"])
def test_no_single_kind_reports_drift_on_its_own(desired, live, kind) -> None:
    # Parametrized per kind because the kinds fail differently: Service picks up
    # clusterIP and ipFamilies, Deployment picks up strategy and
    # progressDeadlineSeconds, NetworkPolicy picks up nothing at all.
    assert plan([one(desired, kind)], [one(live, kind)], agent=AGENT).is_noop


def test_repeated_passes_stay_converged(desired, live) -> None:
    for _ in range(3):
        assert plan(desired, live, agent=AGENT).is_noop


def test_plain_equality_would_have_missed_this(desired, live) -> None:
    # A guard on the guard: if the captured fixture ever stops containing
    # server-defaulted fields, the tests above would pass for the wrong reason
    # and silently stop protecting anything.
    naive = [
        k
        for k in ("Service", "Deployment")
        if _curie_owned_view(one(desired, k)) == _curie_owned_view(one(live, k))
    ]
    assert not naive, f"fixture no longer carries server defaults for {naive}"


def test_an_empty_collection_is_not_drift(desired, live) -> None:
    # The renderer emits `args: []`; the API server does not persist it. Absent
    # and declared-empty are the same state.
    dep = one(desired, "Deployment")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    assert container.get("args") == [], "fixture no longer exercises the empty-list case"
    assert "args" not in one(live, "Deployment")["spec"]["template"]["spec"]["containers"][0]
    assert plan([dep], [one(live, "Deployment")], agent=AGENT).is_noop


# --------------------------------------------------------------------------- #
# What must still be caught
# --------------------------------------------------------------------------- #
def test_a_human_edit_to_a_declared_field_is_drift(desired, live) -> None:
    # `kubectl set image` leaves our declaration untouched, so the hash still
    # matches. Only comparing values catches this one.
    tampered = one(live, "Deployment")
    tampered["spec"]["template"]["spec"]["containers"][0]["image"] = "ghcr.io/example/tampered:9"
    assert plan(desired, live, agent=AGENT).drifted == [
        ("Deployment", tampered["metadata"]["name"])
    ]


def test_a_field_removed_from_the_declaration_is_drift(desired, live) -> None:
    # The case containment cannot see: a shrunk declaration is still *contained*
    # by the larger live object. `hostAliases` is not a hypothetical -- a
    # connector silently missing it is healthy in `kubectl get pods` and 403s
    # every call (#1156).
    dep, live_dep = one(desired, "Deployment"), one(live, "Deployment")
    # Yesterday's declaration had hostAliases and was applied. Today's does not.
    # The live object therefore carries BOTH the field and yesterday's digest --
    # getting that second part right is what makes this test mean anything.
    previously = copy.deepcopy(dep)
    previously["spec"]["template"]["spec"]["hostAliases"] = [
        {"ip": "10.0.0.9", "hostnames": ["example.invalid"]}
    ]
    live_dep["spec"]["template"]["spec"]["hostAliases"] = previously["spec"]["template"]["spec"][
        "hostAliases"
    ]
    live_dep["metadata"]["annotations"][HASH_ANNOTATION] = stamp_hash(previously)["metadata"][
        "annotations"
    ][HASH_ANNOTATION]

    assert _contains(_curie_owned_view(dep), _curie_owned_view(live_dep)), (
        "containment alone should NOT see this -- that is why the hash exists"
    )
    assert plan([dep], [live_dep], agent=AGENT).drifted == [("Deployment", dep["metadata"]["name"])]


def test_dropping_one_env_entry_is_drift(desired, live) -> None:
    live_dep = one(live, "Deployment")
    env = live_dep["spec"]["template"]["spec"]["containers"][0].setdefault("env", [])
    env.append({"name": "SNUCK_IN", "value": "1"})
    assert plan(desired, [live_dep], agent=AGENT).drifted


# --------------------------------------------------------------------------- #
# Secrets, where the two sides do not even use the same field name
# --------------------------------------------------------------------------- #
def secret(value: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": "creds", "labels": {OWNER_LABEL: AGENT}},
        "stringData": {"GRAFANA_TOKEN": value},
    }


def as_stored(obj: dict[str, Any]) -> dict[str, Any]:
    """A Secret as the API server returns it: `stringData` in, `data` out."""

    stored = stamp_hash(obj)
    stored["data"] = {
        k: base64.b64encode(v.encode()).decode() for k, v in stored.pop("stringData").items()
    }
    return stored


def test_a_secret_is_not_drifted_just_for_coming_back_encoded() -> None:
    # We write `stringData`; the server returns the same bytes base64 under
    # `data`. Comparing the field names directly means every pass rewrites every
    # credential -- verified against a real API server, not assumed.
    want = secret("not-a-real-token-just-a-fixture")
    assert plan([want], [as_stored(want)], agent=AGENT).is_noop


def test_a_rotated_credential_is_drift() -> None:
    stored = as_stored(secret("not-a-real-token-just-a-fixture"))
    assert plan([secret("rotated-fixture-value")], [stored], agent=AGENT).drifted == [
        ("Secret", "creds")
    ]


def test_an_undecodable_secret_value_does_not_crash_the_pass() -> None:
    # One malformed Secret must not take down reconciliation for every agent.
    stored = as_stored(secret("x"))
    stored["data"]["GRAFANA_TOKEN"] = "!!!not-base64!!!"
    assert plan([secret("x")], [stored], agent=AGENT).drifted == [("Secret", "creds")]


# --------------------------------------------------------------------------- #
# The stamp itself
# --------------------------------------------------------------------------- #
def test_planned_objects_carry_their_digest(desired) -> None:
    # An object applied without its digest is one the next pass cannot recognize
    # as converged -- the hot loop, reintroduced.
    for obj in plan(desired, [], agent=AGENT).apply:
        assert obj["metadata"]["annotations"][HASH_ANNOTATION]


def test_stamping_is_idempotent(desired) -> None:
    once = stamp_hash(one(desired, "Service"))
    assert stamp_hash(once) == once


def test_stamping_does_not_mutate_its_input(desired) -> None:
    dep = one(desired, "Deployment")
    before = copy.deepcopy(dep)
    stamp_hash(dep)
    assert dep == before


def test_an_object_the_cli_created_is_adopted_not_ignored(desired, live) -> None:
    # The CLI applier stamps an owner label but no digest. The reconciler should
    # take it over on the first pass rather than treat it as foreign.
    live_svc = one(live, "Service")
    del live_svc["metadata"]["annotations"][HASH_ANNOTATION]
    result = plan([one(desired, "Service")], [live_svc], agent=AGENT)
    assert result.drifted == [("Service", live_svc["metadata"]["name"])]


def test_ownership_still_gates_everything(desired, live) -> None:
    # Unchanged from #1187, asserted here because the hash must not become a
    # second, weaker way to claim an object.
    for obj in live:
        obj["metadata"]["labels"][OWNER_LABEL] = "some-other-agent"
    result = plan(desired, live, agent=AGENT)
    assert result.delete == [], "another agent's objects are not ours to prune"
    assert len(result.apply) == len(desired)
