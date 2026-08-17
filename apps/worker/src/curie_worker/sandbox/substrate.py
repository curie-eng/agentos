"""The sandbox lifecycle substrate: claim, route, suspend/resume, reap.

The one-live-session-per-thread routing rule (detailed-architecture section 2)
is implemented here as: the affinity store maps ``thread_key`` to exactly one
claim; ``claim()`` returns the existing live binding or creates one from the
warm pool; a lost creation race is resolved by deleting the loser's claim and
adopting the winner's. F1 (the worker kernel) composes these primitives; it
never touches Kubernetes directly.

Cold-restart rehydrate design (ADR-0003, PT-1 finding: suspend/resume is a cold
pod restart, the live process never survives): ``suspend()`` records the
caller-supplied history ref on the route; ``resume()`` retires the suspended
claim and creates a NEW claim whose per-claim env injects
``CURIE_HISTORY_REF`` (and the original ``CURIE_SESSION_ID``), so the
replacement runner boots rehydrating from stored history rather than assuming
process or cache warmth. The runner resolves the ref to the thread's transcript
namespace on the durable state store and replays the prior turns as a boot-time
system-prompt preamble; the ref is a harness-agnostic state-store URL, not an SDK
resume id (ADR-0029 superseded that framing). Producing the ref -- the thread's
transcript key -- is the caller's job.
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

from aci_protocol import BootEnv

from ..binding import RUNNER_TOKEN_ENV
from .affinity import AffinityStore
from .types import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    THREAD_HASH_LABEL,
    ClaimTimeoutError,
    NoRouteError,
    RouteRecord,
    RouteState,
    SandboxClient,
    SandboxHandle,
    SandboxView,
    SubstrateConfig,
    SuspendedThreadError,
)

# The resume overlay writes these into the replacement claim's per-claim env, and
# the runner reads them out of the same boot contract -- same consumer, so both
# are named from the ONE declaration in ``aci_protocol.BootEnv`` (#488, ADR-0049)
# rather than retyped. A local literal would drift silently on a rename: the
# replacement sandbox boots and answers, having quietly lost its history.
HISTORY_ENV = BootEnv.env_key("history_ref")
SESSION_ENV = BootEnv.env_key("session_id")

logger = logging.getLogger(__name__)

# Leeway added to ``claim_timeout_seconds`` before the reaper will treat an
# unrouted claim as litter. Deliberately generous, for four reasons that all
# push the measured age HIGHER than the true age, which is the direction that
# reaps a live claim early:
#
# 1. Kubernetes serializes ``creationTimestamp`` at whole-second granularity,
#    so a claim can read up to 1s older than it is.
# 2. Nothing keeps the worker's clock and the API server's clock in sync;
#    single-digit seconds of skew is routine, more on a loaded CI node, which
#    is exactly where this race fires.
# 3. ``claim_timeout_seconds`` does not start until _claim_fresh arms its
#    deadline, and ``create_claim`` returns before that with NO request
#    timeout on the underlying API call, so the claim already exists (and
#    already looks reapable) for the whole create-call latency first.
# 4. The put_if_absent race-resolution tail runs affinity reads and
#    ``get_sandbox`` calls, also with no request timeout, AFTER the deadline
#    has passed and therefore entirely outside the budget.
#
# The margin is what absorbs 3 and 4, which nothing else bounds today. Adding
# request timeouts to the Kubernetes calls is a separate change, not this one.
#
# The asymmetry is why the number is padded rather than tuned, and it is the
# reason not to "optimize" it toward zero: overshooting costs at most 30 extra
# seconds of claim retention against a 3600s route TTL (under 1% of worst-case
# litter lifetime, zero steady-state cost), while undershooting kills a live
# sandbox under a blocked turn. A stall that exceeds even this margin does not
# lose data: the claim is reaped, the creator's route then points at a gone
# sandbox, and the next claim() takes the existing _evict_stale path, which
# drops the stale route and rebinds. Slow turn, not corruption.
REAP_GRACE_MARGIN_SECONDS = 30.0


class SandboxSubstrate:
    """Provision, route, and reap runner sandboxes for conversation threads."""

    def __init__(
        self,
        k8s: SandboxClient,
        affinity: AffinityStore,
        config: SubstrateConfig,
    ) -> None:
        self._k8s = k8s
        self._affinity = affinity
        self._config = config

    # -- claim / lookup -------------------------------------------------------

    def claim(self, thread_key: str, *, env: dict[str, str] | None = None) -> SandboxHandle:
        """Return the thread's live sandbox, claiming a warm one if needed.

        ``env`` is per-claim env injection (the resume path uses it for the
        history ref); the fast path passes none so the claim binds a pre-warmed
        generic sandbox.
        """

        record = self._affinity.get(thread_key)
        if record is not None:
            if record.state is RouteState.SUSPENDED:
                raise SuspendedThreadError(thread_key)
            sandbox = self._k8s.get_sandbox(record.handle.sandbox_name)
            if sandbox is not None and sandbox.operating_mode == "Running":
                self._affinity.touch(thread_key, self._config.route_ttl_seconds)
                return record.handle
            # The sandbox died (or was suspended) out from under a live route:
            # a stale route must never be handed back, and must not win the
            # re-claim race below. Evict it and bind fresh.
            self._evict_stale(thread_key, record)

        return self._claim_fresh(thread_key, env=env, state=RouteState.LIVE)

    def lookup(self, thread_key: str) -> SandboxHandle | None:
        """The thread's live handle, or None (no route, suspended, or the
        cluster-side sandbox is gone/not ready)."""

        record = self._affinity.get(thread_key)
        if record is None or record.state is not RouteState.LIVE:
            return None
        sandbox = self._k8s.get_sandbox(record.handle.sandbox_name)
        if sandbox is None or sandbox.operating_mode != "Running":
            return None
        return record.handle

    # -- suspend / resume -------------------------------------------------------

    def suspend(self, thread_key: str, *, history_ref: str | None) -> None:
        """Suspend the thread's sandbox and record the rehydrate ref.

        The pod is deleted by the controller (PT-1: suspend is pod deletion);
        the route flips to SUSPENDED with the longer TTL so ``resume()`` can
        rebuild session state later.
        """

        record = self._affinity.get(thread_key)
        if record is None:
            raise NoRouteError(thread_key)
        self._k8s.set_sandbox_mode(record.handle.sandbox_name, "Suspended")
        self._affinity.mark_suspended(
            thread_key, history_ref, self._config.suspended_route_ttl_seconds
        )

    def resume(
        self, thread_key: str, *, env: dict[str, str] | None = None
    ) -> SandboxHandle:
        """Rehydrate a suspended thread into a fresh claim.

        The suspended claim is retired (its process and cache are gone either
        way) and a new claim is created with ``CURIE_HISTORY_REF`` injected,
        so the replacement runner boots resuming from stored history.

        ``env`` is the caller's bound boot env (bundle ref, budget, state
        refs), the same one a fresh ``claim()`` would inject. The suspended
        pod was deleted (ADR-0003), so the replacement boots from env alone;
        resuming without it would boot a generic, bundle-less runner. The
        session identity and any recorded history ref are preserved on top,
        and the runner token is minted fresh when the caller did not already
        mint one (issue #63: the old token died with the old claim).
        """

        record = self._affinity.get(thread_key)
        if record is None:
            raise NoRouteError(thread_key)
        old = record.handle
        boot = dict(env) if env else {}
        boot.setdefault(SESSION_ENV, old.session_id)
        if not boot.get(RUNNER_TOKEN_ENV):
            boot[RUNNER_TOKEN_ENV] = secrets.token_urlsafe(32)
        if old.history_ref is not None:
            boot.setdefault(HISTORY_ENV, old.history_ref)

        self._k8s.delete_claim(old.claim_name)
        self._affinity.delete_if_claim(thread_key, old.claim_name)
        return self._claim_fresh(
            thread_key,
            env=boot,
            state=RouteState.LIVE,
            session_id=old.session_id,
            history_ref=old.history_ref,
        )

    # -- release / reap -------------------------------------------------------

    def release(self, thread_key: str) -> bool:
        """End the thread's session: delete the claim (the claim's lifecycle
        deletes its sandbox and pod) and drop the route. True if a route
        existed."""

        record = self._affinity.get(thread_key)
        if record is None:
            return False
        self._k8s.delete_claim(record.handle.claim_name)
        self._affinity.delete_if_claim(thread_key, record.handle.claim_name)
        return True

    def reap_orphans(self) -> list[str]:
        """Delete substrate-managed claims that no live route references AND
        that are older than the bind-window grace.

        Routes expire from Valkey by TTL (idle threads); the corresponding
        claims are then orphans on the cluster. Runs from a periodic worker
        tick. Returns the deleted claim names.

        "No route" alone does NOT mean litter. ``_claim_fresh`` writes the
        thread's route only after the claim binds a sandbox and that sandbox
        publishes a dial target, so a claim still inside that window is live
        and routeless at the same time. Reaping it deletes a running sandbox
        out from under a blocked creator, which then polls a claim that no
        longer exists until its deadline and raises ClaimTimeoutError blaming
        a saturated node. Age disambiguates: past the grace no creator can
        still be waiting, because ``_claim_fresh`` deletes its own claim on
        both failure paths.

        The grace is pinned to ``claim_timeout_seconds`` because that is the
        one budget every in-flight claim is created under: ``create_claim`` is
        called from exactly one place, ``_claim_fresh``, reached only from
        ``claim()`` and ``resume()``. A future second creation path with a
        different budget would silently fall outside this guard.

        The clock here is deliberately wall clock, not the ``time.monotonic``
        ``_claim_fresh`` measures its deadline with: ``created_at`` comes from
        another process on another node, and there is no shared monotonic
        epoch to compare it against. That mismatch is precisely what
        REAP_GRACE_MARGIN_SECONDS pads for; do not unify the two clocks.
        """

        live = self._affinity.live_claim_names()
        deleted: list[str] = []
        selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"
        grace = self._config.claim_timeout_seconds + REAP_GRACE_MARGIN_SECONDS
        cutoff = datetime.now(UTC) - timedelta(seconds=grace)
        for claim in self._k8s.list_claims(label_selector=selector):
            if claim.name in live:
                continue
            if claim.created_at is None:
                # Unknown age is never reaped, in the fail-safe direction. It
                # is also the one state that exempts a claim indefinitely
                # instead of merely delaying it, so it must announce itself:
                # an adapter bug or a parse defect that yields None for every
                # claim turns orphan reaping off entirely, and claims then
                # accumulate to ResourceQuota exhaustion with nothing anywhere
                # reporting it.
                logger.warning(
                    "sandbox claim %s reports no creation instant; its age is unknown "
                    "so it will not be reaped. Orphan reaping is disabled for every "
                    "claim the substrate client answers this way.",
                    claim.name,
                )
                continue
            if claim.created_at >= cutoff:
                # Inside the bind window: a creator may still be waiting on it.
                # The comparison is >=, so a claim exactly at the grace is
                # spared. Ties go to the creator; do not simplify this to >.
                continue
            self._k8s.delete_claim(claim.name)
            deleted.append(claim.name)
        return deleted

    # -- internals --------------------------------------------------------------

    def _claim_fresh(
        self,
        thread_key: str,
        *,
        env: dict[str, str] | None,
        state: RouteState,
        session_id: str | None = None,
        history_ref: str | None = None,
    ) -> SandboxHandle:
        config = self._config
        nonce = uuid.uuid4().hex[:6]
        name = config.claim_name_for(thread_key, nonce)
        thread_hash = name.rsplit("-", 1)[0].rsplit("-", 1)[-1]

        self._k8s.create_claim(
            name,
            pool=config.warm_pool,
            env=env,
            labels={THREAD_HASH_LABEL: thread_hash},
        )
        deadline = time.monotonic() + config.claim_timeout_seconds
        try:
            sandbox_name = self._await_bound(name, deadline)
            bound = self._await_service_fqdn(sandbox_name, deadline)
        except Exception:
            self._k8s.delete_claim(name)
            raise

        handle = SandboxHandle(
            thread_key=thread_key,
            claim_name=name,
            sandbox_name=sandbox_name,
            namespace=config.namespace,
            service_fqdn=bound.service_fqdn or "",
            port=bound.port if bound.port is not None else config.runner_port,
            session_id=session_id or f"thread-{thread_hash}",
            history_ref=history_ref,
            token=(env or {}).get(RUNNER_TOKEN_ENV, ""),
        )
        record = RouteRecord(handle=handle, state=state)
        for _ in range(3):
            if self._affinity.put_if_absent(thread_key, record, config.route_ttl_seconds):
                return handle
            # Lost the race: another worker recorded a route first. Adopt the
            # winner only if its sandbox is actually alive; a stale route
            # (dead sandbox) is evicted and the put retried.
            winner = self._affinity.get(thread_key)
            if winner is None:
                continue
            if winner.state is RouteState.SUSPENDED:
                self._k8s.delete_claim(name)
                raise SuspendedThreadError(thread_key)
            sandbox = self._k8s.get_sandbox(winner.handle.sandbox_name)
            if sandbox is not None and sandbox.operating_mode == "Running":
                self._k8s.delete_claim(name)
                return winner.handle
            self._evict_stale(thread_key, winner)
        self._k8s.delete_claim(name)
        raise NoRouteError(f"could not record a route for {thread_key} after repeated races")

    def _evict_stale(self, thread_key: str, record: RouteRecord) -> None:
        """Retire a route whose sandbox is gone: delete its claim (idempotent)
        and drop the route, guarded so a fresher route is never deleted."""

        self._k8s.delete_claim(record.handle.claim_name)
        self._affinity.delete_if_claim(thread_key, record.handle.claim_name)

    def _await_bound(self, claim_name: str, deadline: float) -> str:
        while time.monotonic() < deadline:
            claim = self._k8s.get_claim(claim_name)
            if claim is not None and claim.ready and claim.sandbox_name:
                return claim.sandbox_name
            time.sleep(self._config.poll_interval_seconds)
        # Name the likeliest cause and where to look. This message is the only
        # thread an operator has: the turn surfaces to the user as an opaque
        # `runner-error`, and the shape that produces it most often -- a
        # CPU-starved node -- sets NO node condition, so pods read Running,
        # probes green, disk fine, and every dashboard says healthy.
        #
        # Seen in production: a Langfuse/ClickHouse merge burst took a 2-vCPU
        # node to 0.0% idle. The sandbox pod was created and its bundle-fetch
        # init container started, but nothing finished inside the deadline, so
        # all three attempts timed out identically with nothing to go on.
        raise ClaimTimeoutError(
            f"claim {claim_name} not bound within {self._config.claim_timeout_seconds}s. "
            "The sandbox pod was created but did not become ready in time. The most "
            "common cause is a CPU-saturated node -- which reports no node condition, "
            "so pods still look Running and probes still pass. Check `top`/`kubectl top "
            "node` for idle CPU (not just node conditions), and whether a component "
            "whose CPU LIMIT approaches the node's capacity is bursting; under "
            "contention the kernel divides CPU by requests, and bundle-fetch and the "
            "runner request only 50m each. Other causes: the agent-sandbox controller "
            "not reconciling, or an unfetchable bundle ref."
        )

    def _await_service_fqdn(self, sandbox_name: str, deadline: float) -> SandboxView:
        while time.monotonic() < deadline:
            sandbox = self._k8s.get_sandbox(sandbox_name)
            if sandbox is not None and sandbox.service_fqdn:
                return sandbox
            time.sleep(self._config.poll_interval_seconds)
        raise ClaimTimeoutError(
            f"sandbox {sandbox_name} has no serviceFQDN within "
            f"{self._config.claim_timeout_seconds}s (is spec.service true in the template?)"
        )
