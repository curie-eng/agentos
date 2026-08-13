"""SandboxSubstrate lifecycle logic against real Valkey + the in-memory
cluster fake (the K8s control plane is the one external service faked here;
the real cluster path is the e2e in test_e2e_k8scratch.py)."""

from __future__ import annotations

import time

import pytest
from aci_protocol import BootEnv
from curie_worker.sandbox import (
    AffinityStore,
    ClaimTimeoutError,
    ClaimView,
    NoRouteError,
    RouteRecord,
    RouteState,
    SandboxHandle,
    SandboxSubstrate,
    SandboxView,
    SubstrateConfig,
    SuspendedThreadError,
)

from .conftest import FakeClaim, FakeSandbox, FakeSandboxClient

# Named from the boot contract, NOT re-imported from the module under test:
# asserting against the substrate's own constant passes under any rename on
# either side, so it proves the resume env agrees with itself rather than with
# the runner that has to read it (#488).
HISTORY_ENV = BootEnv.env_key("history_ref")
SESSION_ENV = BootEnv.env_key("session_id")


@pytest.fixture
def substrate(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> SandboxSubstrate:
    return SandboxSubstrate(fake_k8s, affinity, config)


def test_claim_binds_and_routes_thread(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient
) -> None:
    handle = substrate.claim("1700000000.000100")

    assert handle.sandbox_name.startswith("sbx-curie-thread-")
    assert handle.service_fqdn.endswith(".svc.cluster.local")
    assert handle.base_url == f"http://{handle.service_fqdn}:8080"
    assert fake_k8s.claims[handle.claim_name].env == {}

    # Same thread claims again -> same binding, no second claim created.
    again = substrate.claim("1700000000.000100")
    assert again == handle
    assert len(fake_k8s.created) == 1

    # A different thread gets a different sandbox (no cross-talk).
    other = substrate.claim("1700000000.000999")
    assert other.sandbox_name != handle.sandbox_name


def test_lookup_returns_none_when_sandbox_gone(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient
) -> None:
    handle = substrate.claim("T1")
    assert substrate.lookup("T1") == handle

    # Cluster-side deletion out from under the route (node loss, manual kill).
    fake_k8s.sandboxes.pop(handle.sandbox_name)
    assert substrate.lookup("T1") is None


def test_claim_timeout_cleans_up_claim(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> None:
    fake_k8s.bind_ready = False
    substrate = SandboxSubstrate(fake_k8s, affinity, config)

    with pytest.raises(ClaimTimeoutError):
        substrate.claim("T1")
    # The unbound claim is not leaked and no route was recorded.
    assert fake_k8s.deleted == fake_k8s.created
    assert affinity.get("T1") is None


def test_lost_race_adopts_winner_and_retires_loser(
    substrate: SandboxSubstrate,
    fake_k8s: FakeSandboxClient,
    affinity: AffinityStore,
) -> None:
    # A competing worker recorded a route for T1 between our create and put.
    winner_handle = SandboxHandle(
        thread_key="T1",
        claim_name="claim-winner",
        sandbox_name="sbx-claim-winner",
        namespace="test-ns",
        service_fqdn="sbx-claim-winner.test-ns.svc.cluster.local",
        port=8080,
        session_id="sess-w",
    )
    original_create = fake_k8s.create_claim
    # The winner's sandbox really exists (adoption requires a live winner).
    fake_k8s.claims["claim-winner"] = FakeClaim(
        name="claim-winner", env={}, labels={}, sandbox_name="sbx-claim-winner"
    )
    fake_k8s.sandboxes["sbx-claim-winner"] = FakeSandbox(
        name="sbx-claim-winner",
        service_fqdn="sbx-claim-winner.test-ns.svc.cluster.local",
    )

    def create_then_lose(name: str, **kwargs: object) -> None:
        original_create(name, **kwargs)  # type: ignore[arg-type]
        affinity.put_if_absent("T1", RouteRecord(handle=winner_handle), ttl_seconds=60)

    fake_k8s.create_claim = create_then_lose  # type: ignore[method-assign]

    handle = substrate.claim("T1")
    assert handle == winner_handle
    # The loser's claim was deleted, not leaked; the winner's was kept.
    assert "claim-winner" not in fake_k8s.deleted
    assert len(fake_k8s.deleted) == 1


def test_suspend_resume_rehydrates_from_history(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient, affinity: AffinityStore
) -> None:
    first = substrate.claim("T1")
    substrate.suspend("T1", history_ref="sdk-session-abc")

    # Suspended: mode flipped, route no longer live.
    assert fake_k8s.sandboxes[first.sandbox_name].operating_mode == "Suspended"
    record = affinity.get("T1")
    assert record is not None and record.state is RouteState.SUSPENDED
    assert substrate.lookup("T1") is None
    # A claim() while suspended must not silently fork a second live session
    # for the thread without the history; the kernel resumes explicitly.

    resumed = substrate.resume("T1")
    assert resumed.claim_name != first.claim_name
    assert resumed.session_id == first.session_id
    assert resumed.history_ref == "sdk-session-abc"
    # The new claim injects the rehydrate env for the replacement runner.
    env = fake_k8s.claims[resumed.claim_name].env
    assert env[HISTORY_ENV] == "sdk-session-abc"
    assert env[SESSION_ENV] == first.session_id
    # Old claim retired; route is live again on the new claim.
    assert first.claim_name in fake_k8s.deleted
    assert substrate.lookup("T1") == resumed


def test_suspend_and_resume_require_route(substrate: SandboxSubstrate) -> None:
    with pytest.raises(NoRouteError):
        substrate.suspend("nope", history_ref=None)
    with pytest.raises(NoRouteError):
        substrate.resume("nope")


def test_release_deletes_claim_and_route(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient, affinity: AffinityStore
) -> None:
    handle = substrate.claim("T1")
    assert substrate.release("T1")
    assert handle.claim_name not in fake_k8s.claims
    assert handle.sandbox_name not in fake_k8s.sandboxes
    assert affinity.get("T1") is None
    assert not substrate.release("T1")


def test_reap_orphans_deletes_unrouted_claims(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient, affinity: AffinityStore
) -> None:
    live = substrate.claim("T-live")
    orphan = substrate.claim("T-orphan")
    # The orphan's route expires (simulated by guarded delete), its claim stays.
    affinity.delete_if_claim("T-orphan", orphan.claim_name)

    reaped = substrate.reap_orphans()
    assert reaped == [orphan.claim_name]
    assert orphan.claim_name not in fake_k8s.claims
    assert live.claim_name in fake_k8s.claims
    # Reap is idempotent.
    assert substrate.reap_orphans() == []


def test_claim_rebinds_when_sandbox_died_under_live_route(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient, affinity: AffinityStore
) -> None:
    first = substrate.claim("T1")
    # Cluster-side death out from under the route (node loss, manual kill):
    # the stale route must not win the re-claim race and hand back a dead
    # handle, and the stale claim must be retired.
    fake_k8s.sandboxes.pop(first.sandbox_name)

    second = substrate.claim("T1")
    assert second.claim_name != first.claim_name
    assert second.sandbox_name in fake_k8s.sandboxes
    assert first.claim_name in fake_k8s.deleted
    assert substrate.lookup("T1") == second


def test_claim_race_never_adopts_dead_winner(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient, affinity: AffinityStore
) -> None:
    # A competing route lands mid-claim but its sandbox is already gone; the
    # claimer must clear the stale route and bind fresh, never return a handle
    # to a nonexistent sandbox.
    dead = SandboxHandle(
        thread_key="T1",
        claim_name="claim-dead",
        sandbox_name="sbx-claim-dead",
        namespace="test-ns",
        service_fqdn="sbx-claim-dead.test-ns.svc.cluster.local",
        port=8080,
        session_id="sess-d",
    )
    original_create = fake_k8s.create_claim
    injected = False

    def create_then_race(name: str, **kwargs: object) -> None:
        nonlocal injected
        original_create(name, **kwargs)  # type: ignore[arg-type]
        if not injected:
            injected = True
            affinity.put_if_absent("T1", RouteRecord(handle=dead), ttl_seconds=60)

    fake_k8s.create_claim = create_then_race  # type: ignore[method-assign]

    handle = substrate.claim("T1")
    assert handle.sandbox_name in fake_k8s.sandboxes
    assert "claim-dead" in fake_k8s.deleted
    assert substrate.lookup("T1") == handle


class _SlowBindNoFqdnClient(FakeSandboxClient):
    """A fake whose claim binds only after a wall-clock threshold and whose
    bound sandbox never gets a serviceFQDN.

    The bind readiness is TIME-based (measured against ``time.monotonic()``),
    not iteration-count-based, so poll jitter cannot starve phase 1 -- the claim
    reports ready once ``bind_after_seconds`` of real time has elapsed since the
    first create_claim, regardless of how many polls happened. serviceFQDN is
    always empty, so phase 2 (await_service_fqdn) can only ever time out.
    """

    bind_after_seconds: float = 1.2
    _bind_deadline: float | None = None

    def create_claim(self, name: str, **kwargs: object) -> None:
        if self._bind_deadline is None:
            self._bind_deadline = time.monotonic() + self.bind_after_seconds
        super().create_claim(name, **kwargs)  # type: ignore[arg-type]

    def get_claim(self, name: str) -> ClaimView | None:
        claim = self.claims.get(name)
        if claim is None:
            return None
        ready = self._bind_deadline is not None and time.monotonic() >= self._bind_deadline
        return ClaimView(
            name=claim.name,
            ready=ready,
            sandbox_name=claim.sandbox_name if ready else None,
        )

    def get_sandbox(self, name: str) -> SandboxView | None:
        view = super().get_sandbox(name)
        if view is None:
            return None
        return SandboxView(
            name=view.name,
            ready=view.ready,
            service_fqdn="",
            operating_mode=view.operating_mode,
        )


def test_claim_budget_is_end_to_end_across_bind_and_fqdn(
    affinity: AffinityStore, config: SubstrateConfig
) -> None:
    # The bind + FQDN phases must share ONE end-to-end deadline equal to
    # claim_timeout_seconds (2.0s), not a fresh 2.0s each. Bind takes ~1.2s of
    # wall clock, then FQDN never arrives. Under a shared budget the whole claim
    # aborts at ~2.0s; under per-phase budgets it runs ~1.2 + 2.0 = ~3.2s.
    # We assert < 2.6 (0.6s of slack over the 2.0 target) so CI jitter cannot
    # flip a correctly-budgeted run, while ~3.2s of per-phase behavior stays red.
    fake_k8s = _SlowBindNoFqdnClient()
    substrate = SandboxSubstrate(fake_k8s, affinity, config)

    started = time.monotonic()
    with pytest.raises(ClaimTimeoutError):
        substrate.claim("T1")
    elapsed = time.monotonic() - started

    assert elapsed < 2.6
    # The unbound claim is not leaked despite the timeout.
    assert fake_k8s.deleted == fake_k8s.created


def test_claim_timeout_error_names_the_budget(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> None:
    fake_k8s.bind_ready = False
    substrate = SandboxSubstrate(fake_k8s, affinity, config)

    with pytest.raises(ClaimTimeoutError) as excinfo:
        substrate.claim("T1")
    # The error message names the configured budget so the signature change
    # (one shared deadline) does not silently drop the timeout value.
    assert str(config.claim_timeout_seconds) in str(excinfo.value)


def test_claim_on_suspended_route_refuses_to_fork(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient
) -> None:
    substrate.claim("T1")
    substrate.suspend("T1", history_ref="h-1")
    # The kernel must resume explicitly; a plain claim on a suspended thread
    # would silently fork a second session without the history.
    with pytest.raises(SuspendedThreadError):
        substrate.claim("T1")
    resumed = substrate.resume("T1")
    assert substrate.lookup("T1") == resumed


# --- Per-sandbox runner token (issue #63) -------------------------------------
# The env-var name is the cross-package contract with the runner; asserted by its
# literal string.
RUNNER_TOKEN_ENV = "CURIE_RUNNER_TOKEN"


def test_resume_mints_fresh_runner_token(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient
) -> None:
    # A resume creates a new claim; the old token died with the old claim, so the
    # new claim env must carry a freshly minted, non-empty runner token.
    substrate.claim("T1")
    substrate.suspend("T1", history_ref="h-1")
    resumed = substrate.resume("T1")

    env = fake_k8s.claims[resumed.claim_name].env
    assert env.get(RUNNER_TOKEN_ENV), "resume must mint a fresh runner token into the claim env"


def test_claim_handle_carries_env_runner_token(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient
) -> None:
    # The token in the claim env and the token on the returned handle must be the
    # same value, so claim-time and call-time always agree.
    handle = substrate.claim("T1", env={RUNNER_TOKEN_ENV: "tok-19"})

    assert handle.token == "tok-19"
    assert fake_k8s.claims[handle.claim_name].env[RUNNER_TOKEN_ENV] == "tok-19"


def test_resume_merges_caller_boot_env(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient
) -> None:
    # The approval resume path (#244): a suspended pod is gone (ADR-0003), so
    # the replacement must boot with the same bound env a fresh claim gets
    # (bundle ref, budget) or it comes up generic. Session identity and the
    # recorded history ref are preserved on top of the caller env.
    substrate.claim("T1")
    substrate.suspend("T1", history_ref="h-42")

    boot_env = {
        "CURIE_BUNDLE_REF": "bundles/agent-v7.tgz",
        "CURIE_BUDGET": '{"max_output_tokens_per_run": 1, "max_usd_per_day": 1.0}',
        RUNNER_TOKEN_ENV: "tok-fresh",
    }
    resumed = substrate.resume("T1", env=boot_env)

    env = fake_k8s.claims[resumed.claim_name].env
    assert env["CURIE_BUNDLE_REF"] == "bundles/agent-v7.tgz"
    assert env["CURIE_BUDGET"] == boot_env["CURIE_BUDGET"]
    # A caller-minted token is kept (binding.boot_env mints one per claim).
    assert env[RUNNER_TOKEN_ENV] == "tok-fresh"
    # Session identity and the recorded history ref are preserved.
    assert env[SESSION_ENV] == resumed.session_id
    assert env[HISTORY_ENV] == "h-42"
    # The caller's mapping is not mutated.
    assert SESSION_ENV not in boot_env
