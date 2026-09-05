"""The route is the authority for a warm bind's conversation credential.

ADR-0122 d3: the per-conversation token lives where the route lives, on the
``RouteRecord`` in Valkey, never only in a worker's memory. A warm-bind claim
records the freshly minted credential as PENDING before any first event leaves
the worker, so a crash, a lost response, or a second replica recovers it from
the store; the transition to APPLIED is a fenced CAS on the existing claim +
generation authority, and a stale owner cannot win it.

The Valkey-backed cases run against the real store (never mocked) and skip
when it is unreachable; the pure record-shape cases always run.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from curie_worker.binding import RUNNER_TOKEN_ENV
from curie_worker.sandbox import HISTORY_ENV, SESSION_ENV, SandboxSubstrate, SubstrateConfig
from curie_worker.sandbox.affinity import AffinityStore
from curie_worker.sandbox.types import (
    AdoptionState,
    RouteRecord,
    RouteState,
    SandboxHandle,
)

from .conftest import FakeSandboxClient

_CONV = "conversation-credential-A-0123456789"
_CONV_B = "conversation-credential-B-0123456789"
_SESSION = "agent-acme-thread-C0EXAMPLE1-1700000000.000100"
_HISTORY = "http://api.example.com/agents/acme/state/transcript/thread-1"


def _handle(**overrides: object) -> SandboxHandle:
    base = SandboxHandle(
        thread_key="T1",
        claim_name="claim-a",
        sandbox_name="sbx-claim-a",
        namespace="ns",
        service_fqdn="sbx-claim-a.ns.svc.cluster.local",
        port=8080,
        session_id=_SESSION,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# --- record shape: compatible with every route already in Valkey ------------------


def test_legacy_route_records_rehydrate_as_not_adoptable() -> None:
    raw = json.dumps(
        {
            "thread_key": "T1",
            "claim_name": "claim-a",
            "sandbox_name": "sbx-claim-a",
            "namespace": "ns",
            "service_fqdn": "sbx-claim-a.ns.svc.cluster.local",
            "port": 8080,
            "session_id": _SESSION,
            "token": "per-claim-token",
            "state": "live",
        }
    )
    record = RouteRecord.from_json(raw)
    assert record.handle.adoption_state is AdoptionState.NONE
    assert record.handle.token == "per-claim-token"


def test_cold_route_records_keep_the_legacy_wire_shape() -> None:
    """A worker built before the field must still rehydrate every cold route.

    ``SandboxHandle(**payload)`` on that build raises on an unknown key, and an
    unreadable route is evicted or its claim reaped; so the default state is
    written in the pre-field shape and only pending/applied routes carry it.
    """

    payload = json.loads(RouteRecord(handle=_handle(token="per-claim-token")).to_json())
    assert "adoption_state" not in payload
    legacy_shape = {key: value for key, value in payload.items() if key != "state"}
    # Exactly the constructor signature the 628feac worker has (no extra key).
    assert set(legacy_shape) == {
        "thread_key",
        "claim_name",
        "sandbox_name",
        "namespace",
        "service_fqdn",
        "port",
        "session_id",
        "history_ref",
        "token",
        "workspace_repo",
        "workspace_materialized_head",
        "publication_visible_outcome_revision",
        "generation",
    }
    for state in (AdoptionState.PENDING, AdoptionState.APPLIED):
        carried = json.loads(RouteRecord(handle=_handle(adoption_state=state)).to_json())
        assert carried["adoption_state"] == state.value


def test_adoption_state_round_trips_as_a_plain_string() -> None:
    record = RouteRecord(handle=_handle(token=_CONV, adoption_state=AdoptionState.PENDING))
    payload = json.loads(record.to_json())
    assert payload["adoption_state"] == "pending"
    loaded = RouteRecord.from_json(record.to_json())
    assert loaded.handle.adoption_state is AdoptionState.PENDING
    assert loaded.handle == record.handle


def test_unknown_adoption_state_is_rejected_not_guessed() -> None:
    payload = json.loads(RouteRecord(handle=_handle()).to_json())
    payload["adoption_state"] = "maybe"
    with pytest.raises(ValueError):
        RouteRecord.from_json(json.dumps(payload))


# --- the pending route is written before any first event ---------------------------


def test_warm_bind_claim_records_pending_credential_before_any_event(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> None:
    substrate = SandboxSubstrate(fake_k8s, affinity, config)
    handle = substrate.claim(
        "T-warm",
        session_id=_SESSION,
        history_ref=_HISTORY,
        conversation_token=_CONV,
    )
    # The claim carried no identity env: identity travels over the ACI.
    assert fake_k8s.claims[handle.claim_name].env == {}
    assert handle.adoption_state is AdoptionState.PENDING
    assert handle.token == _CONV
    assert handle.session_id == _SESSION
    assert handle.history_ref == _HISTORY
    # ...and the route in the store already carries all of it.
    stored = affinity.get("T-warm")
    assert stored is not None
    assert stored.state is RouteState.LIVE
    assert stored.handle == handle


def test_warm_bind_refuses_identity_env(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> None:
    substrate = SandboxSubstrate(fake_k8s, affinity, config)
    for env in (
        {SESSION_ENV: "other-session"},
        {HISTORY_ENV: _HISTORY},
        {RUNNER_TOKEN_ENV: "per-claim-token"},
    ):
        with pytest.raises(ValueError):
            substrate.claim("T-mixed", env=env, session_id=_SESSION, conversation_token=_CONV)
    assert fake_k8s.created == []
    assert affinity.get("T-mixed") is None


def test_cold_claim_is_unchanged_and_never_pending(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> None:
    substrate = SandboxSubstrate(fake_k8s, affinity, config)
    handle = substrate.claim(
        "T-cold", env={SESSION_ENV: _SESSION, RUNNER_TOKEN_ENV: "per-claim-token"}
    )
    assert handle.adoption_state is AdoptionState.NONE
    assert handle.token == "per-claim-token"
    assert substrate.mark_adoption_applied("T-cold", expected=handle) is None


# --- the applied transition is fenced -----------------------------------------------


def test_mark_applied_is_fenced_on_claim_generation_token_and_state(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> None:
    substrate = SandboxSubstrate(fake_k8s, affinity, config)
    pending = substrate.claim("T-fence", session_id=_SESSION, conversation_token=_CONV)

    # A stale owner: wrong generation, wrong claim, or a different credential.
    assert (
        substrate.mark_adoption_applied(
            "T-fence", expected=replace(pending, generation=pending.generation + 1)
        )
        is None
    )
    assert (
        substrate.mark_adoption_applied(
            "T-fence", expected=replace(pending, claim_name="claim-stale")
        )
        is None
    )
    assert (
        substrate.mark_adoption_applied("T-fence", expected=replace(pending, token=_CONV_B)) is None
    )
    untouched = affinity.get("T-fence")
    assert untouched is not None
    assert untouched.handle.adoption_state is AdoptionState.PENDING

    applied = substrate.mark_adoption_applied("T-fence", expected=pending)
    assert applied is not None
    assert applied.adoption_state is AdoptionState.APPLIED
    assert applied == replace(pending, adoption_state=AdoptionState.APPLIED)
    stored = affinity.get("T-fence")
    assert stored is not None and stored.handle == applied

    # Applying twice (a retry that lost its first response) is idempotent.
    assert substrate.mark_adoption_applied("T-fence", expected=pending) == applied
    assert substrate.mark_adoption_applied("T-fence", expected=applied) == applied
    # ...but a stale pending holder with another credential still loses.
    assert (
        substrate.mark_adoption_applied("T-fence", expected=replace(pending, token=_CONV_B)) is None
    )


def test_mark_applied_refuses_a_suspended_or_missing_route(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> None:
    substrate = SandboxSubstrate(fake_k8s, affinity, config)
    pending = substrate.claim("T-susp", session_id=_SESSION, conversation_token=_CONV)
    substrate.suspend("T-susp", history_ref=_HISTORY)
    assert substrate.mark_adoption_applied("T-susp", expected=pending) is None
    stored = affinity.get("T-susp")
    assert stored is not None and stored.state is RouteState.SUSPENDED
    assert substrate.mark_adoption_applied("T-none", expected=pending) is None


def test_route_replaced_under_a_pending_owner_loses_the_fence(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> None:
    substrate = SandboxSubstrate(fake_k8s, affinity, config)
    pending = substrate.claim("T-race", session_id=_SESSION, conversation_token=_CONV)
    # Another owner evicted and re-claimed the thread (a new claim name).
    substrate.release("T-race")
    fresh = substrate.claim("T-race", session_id=_SESSION, conversation_token=_CONV_B)
    assert fresh.claim_name != pending.claim_name
    assert substrate.mark_adoption_applied("T-race", expected=pending) is None
    stored = affinity.get("T-race")
    assert stored is not None
    assert stored.handle == fresh
    assert stored.handle.adoption_state is AdoptionState.PENDING


def test_creation_race_loser_adopts_the_winners_pending_route(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> None:
    substrate = SandboxSubstrate(fake_k8s, affinity, config)
    winner = substrate.claim("T-dup", session_id=_SESSION, conversation_token=_CONV)
    # A second replica racing the same first message: it must converge on the
    # winner's credential rather than minting a second live one.
    loser_view = SandboxSubstrate(fake_k8s, affinity, config)
    adopted = loser_view.claim("T-dup", session_id=_SESSION, conversation_token=_CONV_B)
    assert adopted == winner
    assert adopted.token == _CONV
    assert adopted.adoption_state is AdoptionState.PENDING


def test_resume_of_a_pending_route_cold_creates_with_a_fresh_per_claim_token(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> None:
    substrate = SandboxSubstrate(fake_k8s, affinity, config)
    pending = substrate.claim("T-res", session_id=_SESSION, conversation_token=_CONV)
    substrate.suspend("T-res", history_ref=_HISTORY)
    resumed = substrate.resume("T-res", env={"CURIE_BUDGET": "{}"})
    # The replacement is a cold pod bound at boot: per-claim mode, never pending.
    assert resumed.adoption_state is AdoptionState.NONE
    assert resumed.token and resumed.token != _CONV
    assert resumed.session_id == pending.session_id
    assert resumed.history_ref == _HISTORY
    assert resumed.generation == pending.generation + 1
    env = fake_k8s.claims[resumed.claim_name].env
    assert env[SESSION_ENV] == _SESSION and env[HISTORY_ENV] == _HISTORY
    assert env[RUNNER_TOKEN_ENV] == resumed.token
