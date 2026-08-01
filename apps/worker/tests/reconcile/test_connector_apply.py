"""Carrying out a reconcile plan (ADR-0090, #1184).

These target the orderings, not the happy path. The happy path is one line;
the orderings are what decide whether a transient cluster error degrades into
an outage.
"""

from __future__ import annotations

from typing import Any

import pytest
from curie_worker.connector_apply import ApplyReport, ConnectorClient, execute
from curie_worker.connector_reconcile import OWNER_LABEL, ReconcilePlan


def obj(kind: str, name: str, owner: str | None = "a") -> dict[str, Any]:
    labels = {OWNER_LABEL: owner} if owner else {}
    return {"kind": kind, "metadata": {"name": name, "labels": labels}}


class FakeClient:
    """In-memory model of the cluster. Fails whatever it is told to."""

    def __init__(self, fail_apply: set[str] | None = None, fail_delete: set[str] | None = None):
        self.applied: list[str] = []
        self.deleted: list[str] = []
        self.order: list[str] = []
        self._fail_apply = fail_apply or set()
        self._fail_delete = fail_delete or set()

    def list_owned(self, namespace: str, owner: str) -> list[dict[str, Any]]:
        return []

    def apply(self, namespace: str, o: dict[str, Any]) -> None:
        name = o["metadata"]["name"]
        if name in self._fail_apply:
            raise RuntimeError(f"apply {name} exploded")
        self.applied.append(name)
        self.order.append(f"apply:{name}")

    def delete(self, namespace: str, kind: str, name: str) -> None:
        if name in self._fail_delete:
            raise RuntimeError(f"delete {name} exploded")
        self.deleted.append(name)
        self.order.append(f"delete:{name}")


def test_it_satisfies_the_protocol() -> None:
    client: ConnectorClient = FakeClient()
    assert client is not None


# --------------------------------------------------------------------------- #
# Apply strictly before delete
# --------------------------------------------------------------------------- #
def test_applies_before_deleting() -> None:
    # Extra objects are inert; missing ones are a broken agent. Ordering is the
    # difference between those two outcomes when something fails.
    c = FakeClient()
    plan = ReconcilePlan(apply=[obj("Service", "new")], delete=[("Secret", "stale")])
    execute(c, plan, namespace="ns", agent="a")
    assert c.order == ["apply:new", "delete:stale"]


def test_a_failed_apply_cancels_the_prune_entirely() -> None:
    # The failure this prevents: a transient API error while applying, followed
    # by a prune that removes the working connector the apply failed to
    # replace. The agent would lose its tools because of a blip.
    c = FakeClient(fail_apply={"new"})
    plan = ReconcilePlan(apply=[obj("Service", "new")], delete=[("Secret", "stale")])
    report = execute(c, plan, namespace="ns", agent="a")
    assert c.deleted == [], "nothing may be pruned after a failed apply"
    assert not report.ok
    assert report.failures[0][0:2] == ("Service", "new")


# --------------------------------------------------------------------------- #
# One bad object must not strand the rest
# --------------------------------------------------------------------------- #
def test_a_failed_delete_does_not_abandon_the_others() -> None:
    # A finalizer or an RBAC gap on one object would otherwise leave every
    # later object orphaned -- a pod still running with a credential mounted.
    c = FakeClient(fail_delete={"stuck"})
    plan = ReconcilePlan(delete=[("Secret", "stuck"), ("Service", "next")])
    report = execute(c, plan, namespace="ns", agent="a")
    assert c.deleted == ["next"]
    assert [f[1] for f in report.failures] == ["stuck"]


def test_every_apply_is_attempted_even_after_one_fails() -> None:
    c = FakeClient(fail_apply={"bad"})
    plan = ReconcilePlan(apply=[obj("Service", "bad"), obj("Service", "good")])
    report = execute(c, plan, namespace="ns", agent="a")
    assert c.applied == ["good"]
    assert len(report.failures) == 1


# --------------------------------------------------------------------------- #
# Refuse to write what cannot later be pruned
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("owner", [None, "someone-else"])
def test_refuses_to_apply_an_object_it_could_not_own(owner: str | None) -> None:
    # An object without this agent's owner label is invisible to every future
    # prune. Writing one creates something Curie can never clean up.
    c = FakeClient()
    report = execute(
        c, ReconcilePlan(apply=[obj("Service", "orphan", owner=owner)]), namespace="ns", agent="a"
    )
    assert c.applied == []
    assert OWNER_LABEL in report.failures[0][2]


# --------------------------------------------------------------------------- #
# Nothing to do
# --------------------------------------------------------------------------- #
def test_an_empty_plan_touches_nothing() -> None:
    c = FakeClient()
    report = execute(c, ReconcilePlan(), namespace="ns", agent="a")
    assert c.order == []
    assert report.ok
    assert report == ApplyReport()


def test_drift_correction_is_logged_because_it_overrides_a_human(caplog) -> None:
    # The one action here that reverts someone's kubectl edit. Silent would be
    # indistinguishable from a cluster ignoring them.
    c = FakeClient()
    plan = ReconcilePlan(apply=[obj("Deployment", "dep")], drifted=[("Deployment", "dep")])
    with caplog.at_level("INFO"):
        execute(c, plan, namespace="ns", agent="a")
    assert any("drift corrected" in r.getMessage() for r in caplog.records)
