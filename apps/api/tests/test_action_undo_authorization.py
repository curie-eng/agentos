"""Who may undo (ADR-0117 decision 3), against real Postgres.

"An undo requires the authorization the forward action required, and no more."

Both halves of that sentence are load-bearing and this file tests both. An action
whose tool was gated by an approval policy needs an authorizer of that same
route, resolved against membership the way ADR-0034 resolves an approver. An
action nobody had to approve is not gated on the way back either -- the state
being restored is one the cluster was already in, and it got there without anyone
approving it.

What is deliberately NOT inherited is the self-approval refusal (#246). That rule
stops a requester approving their own REQUEST. An undo is not a request: the
actor is putting back a state that predates it, so requiring them not to be the
turn's author would be MORE authorization than the forward action needed, which
is the half of decision 3 that says "and no more".
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from curie_api.approvers import MembershipVerdict
from curie_api.deps import get_approver_sets
from curie_api.main import create_app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("clean_db")

LEFT = {"spec": {"replicas": 10}}
PRIOR = {"spec": {"replicas": 3}}
TARGET = {"kind": "Deployment", "namespace": "public", "name": "api"}


class _Set:
    """An approver set that answers however the test says."""

    def __init__(self, verdict: MembershipVerdict, name: str = "explicit-users") -> None:
        self._verdict = verdict
        self._name = name

    @property
    def audit_name(self) -> str:
        return self._name

    async def contains(self, actor: str, actor_channel: str | None) -> MembershipVerdict:
        return self._verdict


@pytest.fixture
def gated_client(_disposable_db: Any, request: Any) -> Any:
    """A client whose approver set is whatever the test parametrized."""

    verdict = getattr(request, "param", MembershipVerdict(member=True))
    app = create_app()
    app.dependency_overrides[get_approver_sets] = lambda: (lambda approval, binding: _Set(verdict))
    with TestClient(app) as client:
        yield client


def _seed_approval(client: Any, headers: Any) -> str:
    """A real approval to gate against.

    Not a made-up id: an unreadable gate is its own refusal path (see
    ``test_a_deleted_gate_fails_closed``), so the member/non-member tests have to
    exercise a gate that genuinely resolves.
    """

    created = client.post(
        "/approvals",
        json={
            "conversation_id": f"th-{uuid.uuid4().hex[:8]}",
            "author": "U-author",
            "summary": "scale public/api to 10",
            "reply_kind": "slack",
            "reply_channel": "C1",
            "reply_placeholder": "p-1",
            "dedupe_key": uuid.uuid4().hex,
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _gated_action(client: Any, headers: Any, approval_id: str | None) -> dict[str, Any]:
    opened = client.post(
        "/actions",
        json={
            "conversation_id": "C1",
            "call_id": "toolu_01",
            "tool": "scale_deployment",
            "arguments": {"replicas": 10},
            "gate_approval_id": approval_id,
            "dedupe_key": f"event-{uuid.uuid4()}:toolu_01",
        },
        headers=headers,
    ).json()
    return dict(
        client.post(
            f"/actions/{opened['id']}/complete",
            json={
                "failed": False,
                "result": {"ok": True},
                "prior_state": PRIOR,
                "post_state": LEFT,
                "target": TARGET,
            },
            headers=headers,
        ).json()
    )


def _undo(client: Any, headers: Any, action_id: str, actor: str = "U-operator") -> Any:
    return client.post(
        f"/actions/{action_id}/undo",
        json={"actor": actor, "observed_state": LEFT},
        headers=headers,
    )


def test_an_ungated_action_is_not_gated_on_the_way_back(
    client: Any, auth_headers: Any
) -> None:
    """Nobody approved the change, so nobody has to approve putting it back."""

    action = _gated_action(client, auth_headers, None)

    response = _undo(client, auth_headers, action["id"])

    assert response.status_code == 200
    audit = client.get(f"/actions/{action['id']}/audit", headers=auth_headers).json()
    assert audit[0]["authorizer"] == "ungated"
    assert audit[0]["authorized"] is True


@pytest.mark.parametrize("gated_client", [MembershipVerdict(member=True)], indirect=True)
def test_a_member_of_the_gating_route_may_undo(
    gated_client: Any, auth_headers: Any
) -> None:
    """Someone who could have permitted the change may put it back."""

    action = _gated_action(gated_client, auth_headers, _seed_approval(gated_client, auth_headers))

    response = _undo(gated_client, auth_headers, action["id"])

    assert response.status_code == 200
    audit = gated_client.get(f"/actions/{action['id']}/audit", headers=auth_headers).json()
    assert audit[0]["authorizer"] == "explicit-users"


@pytest.mark.parametrize(
    "gated_client", [MembershipVerdict(member=False, reason="not in #sre")], indirect=True
)
def test_a_non_member_is_refused_with_the_set_s_own_reason(
    gated_client: Any, auth_headers: Any
) -> None:
    """The set explains itself: only it knows whether it refused on a list or a group."""

    action = _gated_action(gated_client, auth_headers, _seed_approval(gated_client, auth_headers))

    response = _undo(gated_client, auth_headers, action["id"])

    assert response.status_code == 403
    assert response.json()["detail"] == "not in #sre"
    entry = gated_client.get(f"/actions/{action['id']}/audit", headers=auth_headers).json()[0]
    assert entry["action"] == "refused_unauthorized"
    assert entry["authorized"] is False


@pytest.mark.parametrize(
    "gated_client",
    [MembershipVerdict(member=True, undetermined=True, reason="Slack unreachable")],
    indirect=True,
)
def test_an_undetermined_set_fails_closed(gated_client: Any, auth_headers: Any) -> None:
    """`member` is meaningless when the set could not establish membership.

    Failing open here would let a Slack outage authorize a write into a
    customer's infrastructure.
    """

    action = _gated_action(gated_client, auth_headers, _seed_approval(gated_client, auth_headers))

    assert _undo(gated_client, auth_headers, action["id"]).status_code == 403


@pytest.mark.parametrize("gated_client", [MembershipVerdict(member=True)], indirect=True)
def test_a_refused_authorization_leaves_the_record_untouched(
    gated_client: Any, auth_headers: Any
) -> None:
    """Same invariant as the conflict rule: a refusal changes nothing."""

    action = _gated_action(gated_client, auth_headers, _seed_approval(gated_client, auth_headers))
    gated_client.app.dependency_overrides[get_approver_sets] = lambda: (
        lambda approval, binding: _Set(MembershipVerdict(member=False, reason="no"))
    )

    _undo(gated_client, auth_headers, action["id"])

    after = gated_client.get(f"/actions/{action['id']}", headers=auth_headers).json()
    assert after["undone_at"] is None


def test_the_gating_approval_is_recorded_from_the_worker(
    client: Any, auth_headers: Any
) -> None:
    """The record carries which approval gated the call, or None when none did."""

    approval_id = str(uuid.uuid4())

    action = _gated_action(client, auth_headers, approval_id)

    assert action["gate_approval_id"] == approval_id


def test_a_gate_that_cannot_be_read_fails_closed(client: Any, auth_headers: Any) -> None:
    """A deleted approval is an unreadable gate, not an absent one.

    Reading it as absent would let the approval sweeper turn a gated action into
    a freely undoable one -- a permission check quietly deleting itself.
    """

    action = _gated_action(client, auth_headers, str(uuid.uuid4()))

    response = _undo(client, auth_headers, action["id"])

    assert response.status_code == 403
    assert "can no longer be read" in response.json()["detail"]
