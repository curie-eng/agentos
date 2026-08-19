"""The concurrency kernel: route one Slack event to a runner turn, get every
failure mode right.

Rules implemented here (detailed-architecture section 2b):

1. One live session per thread. A follow-up to a thread with a live turn is a
   *steer* into that turn, not a new turn. The per-thread lock plus opening the
   new turn *before* releasing the lock guarantees a thread never has two turns.
2. The finish race. A steer that arrives as the turn ends returns 409; the kernel
   then opens a fresh turn on the same (idle) sandbox. This check-and-fall-back
   is the compare-and-swap the worker owns.
3. Steer vs interrupt. Default is steer; ``interrupt_thread`` is the explicit
   hard stop (a Slack :stop: affordance would call it). We never keyword-guess.
5. No auto-retry after side effects. A failed run that emitted ``side_effect_flag``
   escalates to a human instead of retrying; the flag is persisted the instant it
   is seen so a crash mid-side-effect still escalates on reclaim. Flag-clean
   failures retry by error classification (rate-limit / runner-error are
   transient; budget-exceeded and everything else escalate).

Idempotency: the Slack event id gates a ``done`` marker so a redelivered or
reclaimed event that already finished is skipped.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import aiohttp
from aci_protocol import (
    ErrorEvent,
    Event,
    Final,
    GateKind,
    OutboundEvent,
    QueuedTurn,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
    ToolNote,
)
from channel_protocol import (
    MESSAGE_VERSION,
    Action,
    ConfirmIntent,
    OutboundMessage,
)
from pydantic import ValidationError

from .approval_cards import ApprovalCardStore
from .approvals import (
    ApprovalBackendError,
    ApprovalCreator,
    ApprovalReader,
    ApprovalRequest,
)
from .behaviorpacks import (
    BehaviorPacks,
    NavPack,
    match_greeting,
    match_help,
    sample_load,
    sample_tip,
)
from .binding import DECISION_ENV, GRANT_TOOL_ENV, RESUMED_KIND_ENV, BindingResolver
from .config import WorkerConfig
from .killswitch import KillSwitch
from .markers import Markers
from .runner_client import RunnerClient, RunnerError, TurnStream
from .sandbox import SandboxSubstrate
from .sandbox.types import (
    CapacityExhaustedError,
    SandboxError,
    SandboxHandle,
    SuspendedThreadError,
)
from .slack_sink import SettledCard, SlackSink
from .threadlock import ThreadLock

logger = logging.getLogger(__name__)

# Failure classifications that are worth retrying (transient). Everything else
# (budget-exceeded, model/server errors) escalates rather than looping.
RETRYABLE_CLASSIFICATIONS = frozenset({"rate-limit", "runner-error"})

# The platform-authored prefix that marks an approval resume turn as an EXPIRY
# (vs a resolve). Set by ``resumequeue.build_expiry_resume_turn`` on both expiry
# paths (the #412 sweeper and a past-SLA resolve); ``build_resume_turn`` uses
# ``[approval resolved]`` instead. This text marker -- not the turn author -- is
# the expiry discriminator: ``author`` is caller-supplied on a resolve
# (``resolved_by``), and "system" is the codebase's reserved machine-actor name
# (e.g. the sweeper's audit rows), so a resolver named "system" would otherwise
# get its resolved card wrongly stamped expired. The marker is a stable
# platform contract on a platform-authored turn -- not user-intent guessing.
_EXPIRY_RESUME_MARKER = "[approval expired]"


def _approval_id_from_resume_event(event_id: str) -> str | None:
    """The approval id inside a resume turn's deterministic event id (#1084).

    The inverse of ``resumequeue.resume_event_id``: ``approval-<id>-resolved``,
    a key the API documents as frozen because the worker's done-marker dedupes
    on it, and which the expiry and resolve paths deliberately share. Reading
    the id off it beats parsing the platform-authored prose, which is written
    for a model rather than for a parser. Returns None on any other shape, so a
    non-resume event never reaches the reader.
    """

    if not event_id.startswith("approval-") or not event_id.endswith("-resolved"):
        return None
    middle = event_id[len("approval-") : -len("-resolved")]
    return middle or None

# How long an operator-requested reset waits on the courtesy interrupt before
# giving up and releasing anyway (#739). Deliberately seconds, not minutes: the
# runner client's own request timeout is 600s, and a wedged runner accepts the
# TCP connect then answers nothing, so an unbounded await there blocks the whole
# maintenance tick. A healthy runner answers an interrupt in well under a second.
# Note the coupling this creates with `RunnerClient.connect_timeout_s` (10.0, see
# runner_client.py): on this path 5s always fires first, so a runner that is not
# even accepting connections surfaces as this timeout rather than as the client's
# connect error. That is fine here because the release runs either way, but keep
# the two in mind together -- dropping the connect timeout below this value would
# silently change which error an operator sees in the log.
_RESET_INTERRUPT_TIMEOUT_S = 5.0

# How long a single thread's interrupt gets during the kill switch's fan-out
# over an agent's live threads (#742) before that thread is logged as failed
# and the fan-out moves on. Unlike `release_thread`, there is no fallback
# release to run afterward on this path, so a timeout here is surfaced via
# logging rather than swallowed -- but it must still be a bound, because an
# unbounded await on one wedged thread would otherwise leave the kill switch,
# "the one control that is supposed to work when things are broken," unable to
# signal the agent's other threads for as long as `RunnerClient.interrupt`'s
# own request budget.
_KILL_INTERRUPT_TIMEOUT_S = 5.0

# The substrate release itself (#743) runs on `asyncio.to_thread`, which is not
# cancellable -- a hang in the K8s control plane (as opposed to a wedged
# runner, already bounded above) would otherwise park the maintenance tick on
# this await indefinitely, the same stall shape as an unbounded interrupt.
# Wrapping it in `asyncio.wait_for` cannot stop the underlying thread (the
# executor slot stays occupied until the call actually returns), but it does
# return control to the caller so the tick is not held hostage by it; a timed
# out release surfaces as any other release failure does -- logged by the
# drain loop's per-request handler, with the request already popped so a
# fresh reset request is needed to retry.
_RESET_RELEASE_TIMEOUT_S = 5.0

# The release runs under the same per-thread route lock the turn path holds
# around `_route_and_start` (#734), so a reset and a turn-start on the same
# thread cannot interleave. Acquiring that lock is bounded like the interrupt
# and the release above, and for the same reason: a wedged or slow-cold-claiming
# turn can hold the route lock for up to the substrate's claim timeout, and a
# reset must not park the maintenance tick waiting on it. A lock that cannot be
# taken in time raises, which the drain loop treats as a failed release (left in
# the in-progress set, reported unconfirmed, retried by a fresh operator
# request) rather than falling back to the old, unsafe lock-free release.
_RESET_LOCK_ACQUIRE_TIMEOUT_S = 5.0


@dataclass
class TurnOutcome:
    """The result of streaming one turn, feeding the retry/escalate decision."""

    terminal_ok: bool
    saw_side_effect: bool = False
    classification: str | None = None
    text: str = ""
    status: SessionStatus | None = None
    steered: bool = False
    # The approval summary and route off an awaiting-approval final (ADR-0010,
    # #247), persisted onto the durable record by the pause path. None on
    # every other status; route also None when the request named none.
    approval_summary: str | None = None
    approval_route: str | None = None
    # Gate provenance off the awaiting-approval final (#544, Decision C):
    # 'permission'|'policy' and the denied tool name (permission gate only).
    # Threaded onto the durable record. None from an older runner.
    approval_gate_kind: str | None = None
    approval_granted_tool: str | None = None


@dataclass
class _RouteResult:
    steered: bool
    handle: SandboxHandle | None = None
    turn: TurnStream | None = None
    # An enabled greeting/help pack matched a provably-fresh thread (no existing
    # route) under the route lock: the canned reply to deliver instead of
    # claiming a sandbox or starting a model turn. None on every other path.
    canned_reply: str | None = None


@dataclass
class _LockEntry:
    """A per-thread in-process lock plus a holder/waiter refcount so the entry
    can be evicted when idle (otherwise the map grows one entry per thread ever
    seen, an unbounded leak in a long-running worker)."""

    lock: asyncio.Lock
    refs: int = 0


@dataclass
class _StreamAccumulator:
    text_parts: list[str] = field(default_factory=list)
    saw_side_effect: bool = False
    classification: str | None = None
    status: SessionStatus | None = None
    final_text: str | None = None
    approval_summary: str | None = None
    approval_route: str | None = None
    approval_gate_kind: str | None = None
    approval_granted_tool: str | None = None

    def rendered(self) -> str:
        return self.final_text if self.final_text is not None else "".join(self.text_parts)


class _ThrottledReply:
    """Coalesces chat.update edits while streaming; always flushes the final. In
    no-edit mode intermediate edits are suppressed entirely, so the placeholder
    gets exactly one update (the final)."""

    def __init__(
        self,
        sink: SlackSink,
        *,
        channel: str,
        ts: str,
        min_interval_s: float,
        nav: NavPack | None = None,
        no_edit: bool = False,
        endpoint: str | None = None,
        best_effort: bool = False,
    ) -> None:
        self._sink = sink
        self._channel = channel
        self._ts = ts
        self._min_interval_s = min_interval_s
        self._no_edit = no_edit
        self._last = 0.0
        self._last_text: str | None = None
        # Whether this turn's reply delivery is best-effort (#708): set only for an
        # approval-resume turn (the caller derives it from _is_approval_resume). The
        # granted tool already ran in the runner, so an undeliverable reply to a
        # now-dead CLI stub with no default transport completes the turn rather than
        # dead-lettering the resolved approval. Threaded to the sink per update.
        self._best_effort = best_effort
        # The bound agent's hub-button pack, forwarded to the sink so a render of
        # a COMPLETE structured reply can add the no-dead-ends hub button (in
        # practice the final flush, which is the update that carries one). None
        # when unbound/disabled.
        self._nav = nav
        # This turn's reply endpoint (issue #19): routes the edit back to the
        # ingress that enqueued the turn. None uses the sink's worker default.
        self._endpoint = endpoint

    async def stream(self, text: str) -> None:
        if self._no_edit:
            return
        if not text or text == self._last_text:
            return
        now = time.monotonic()
        if now - self._last < self._min_interval_s:
            return
        self._last = now
        self._last_text = text
        await self._sink.update(
            channel=self._channel,
            ts=self._ts,
            text=text,
            nav=self._nav,
            endpoint=self._endpoint,
            best_effort_unreachable=self._best_effort,
        )

    async def finalize(self, text: str) -> None:
        if text == self._last_text:
            return
        self._last_text = text
        await self._sink.update(
            channel=self._channel,
            ts=self._ts,
            text=text or "(no response)",
            nav=self._nav,
            endpoint=self._endpoint,
            best_effort_unreachable=self._best_effort,
        )


class Kernel:
    """Routes events to runner turns and enforces the concurrency rules."""

    def __init__(
        self,
        *,
        substrate: SandboxSubstrate,
        runner: RunnerClient,
        sink: SlackSink,
        lock: ThreadLock,
        markers: Markers,
        config: WorkerConfig,
        binding: BindingResolver | None = None,
        killswitch: KillSwitch | None = None,
        approvals: ApprovalCreator | None = None,
        # Separate from ``approvals`` on purpose (#1084): the pause path needs
        # only the create half, and a test fake for it should not have to grow a
        # read method it never calls. In production both are the one
        # ``ApprovalClient``.
        approval_reader: ApprovalReader | None = None,
        card_store: ApprovalCardStore | None = None,
    ) -> None:
        self._substrate = substrate
        self._runner = runner
        self._sink = sink
        self._lock = lock
        self._markers = markers
        self._config = config
        # Deployment-to-runtime binding and the kill switch are optional: when
        # absent the kernel runs a generic sandbox (the F1 behavior); when present
        # it resolves channel -> agent -> bundle/budget and gates killed agents.
        self._binding = binding
        self._killswitch = killswitch
        # The approval-record backend (#244). When absent (unwired tests, a
        # deployment without the API), an awaiting-approval run degrades to an
        # escalation instead of suspending a session nothing could ever resume.
        self._approvals = approvals
        self._approval_reader = approval_reader
        # Remembers where each suspended thread's approval card was posted so an
        # EXPIRY can disable it (#419); absent (unwired tests) simply skips the
        # card teardown -- the resolve-click path still heals a card on click.
        self._card_store = card_store
        # Which threads are running which agent, so a kill interrupts the agent's
        # live turns. Populated while a turn owner streams.
        self._active_by_agent: dict[uuid.UUID, set[str]] = {}
        # In-process per-thread lock over the route/start critical section only.
        # asyncio.Lock is FIFO, so same-thread events from one worker open/steer
        # the runner in arrival order (ordering preserved under concurrent sends).
        # The cross-worker guarantee is the Valkey ThreadLock; this adds
        # deterministic ordering within a process without blocking steering,
        # because it is released before the stream is consumed.
        self._order_locks: dict[str, _LockEntry] = {}

    async def process_event(self, qevent: QueuedTurn) -> None:
        """Handle one queued Slack event to a terminal state (success or escalate).

        Returns normally once the event is terminally handled; the consumer then
        acks it. Raising leaves the entry pending for crash-recovery reclaim.
        """
        event_id = qevent.event_id
        thread = qevent.conversation_id

        # Acquire the per-thread order lock BEFORE any await, so concurrent
        # same-thread events queue in task-arrival order (asyncio.Lock is FIFO and
        # an uncontended acquire does not yield). It is released as soon as this
        # event's turn is started or steered (``_release_order`` in _attempt), so
        # streaming and steering are never blocked; holding it across the marker
        # checks is what keeps those awaits from reordering arrivals.
        entry = self._acquire_order_entry(thread)
        await entry.lock.acquire()
        release_state = {"done": False}

        def release_order() -> None:
            if not release_state["done"]:
                release_state["done"] = True
                entry.lock.release()
                self._release_order_entry(thread, entry)

        try:
            if await self._markers.is_done(event_id):
                logger.info("event %s already done; skipping", event_id)
                return

            # If this is an approval resume, settle its live card before running
            # the continuation: expired (#419) or resolved (#1084). Best-effort,
            # and gated on the resume event id so an ordinary turn pays nothing.
            await self._finalize_settled_card(qevent)

            # Crash-safety: a prior attempt executed a side effect but never
            # reached done (worker died mid-run). Do not auto-retry the action.
            if await self._markers.saw_side_effect(event_id):
                await self._escalate(
                    qevent,
                    "A prior attempt started an action before the worker restarted; "
                    "not retrying automatically. Flagging for a human.",
                )
                await self._markers.mark_done(event_id)
                return

            # Deployment-to-runtime binding: resolve which agent/version this
            # channel runs, and refuse a killed agent. An unmapped channel is a
            # polite drop, not a crash.
            boot_env: dict[str, str] | None = None
            agent_id: uuid.UUID | None = None
            nav: NavPack | None = None
            packs: BehaviorPacks | None = None
            approval_routes: dict[str, Any] | None = None
            if self._binding is not None:
                resolved = await self._binding.resolve(qevent.reply_handle.channel)
                if resolved is None:
                    await self._drop_with_message(
                        qevent, "No agent is configured for this channel yet."
                    )
                    return
                if self._killswitch is not None and await self._killswitch.is_killed(
                    resolved.agent_id
                ):
                    await self._drop_with_message(
                        qevent,
                        "This agent is paused by an operator. Try again once it resumes.",
                    )
                    return
                agent_id = resolved.agent_id
                boot_env = self._binding.boot_env(resolved, thread)
                # One-shot post-approval allowance (#430, ADR-0035): when THIS turn is the
                # resume of a genuinely-approved permission-gate approval, deliver a single
                # gated-tool grant so the approved action completes once; the gate re-arms
                # on the next claim. Server-side and tool-name-scoped; never minted by the
                # sandbox. getattr: binding doubles may not carry the method, like the
                # approval_routes probe below. See docs/interfaces/approval/INTERFACE.md.
                grant_fn = getattr(self._binding, "approval_grant_tool", None)
                grant_tool = (
                    await grant_fn(qevent.event_id, resolved.agent_id)
                    if grant_fn is not None
                    else None
                )
                if grant_tool:
                    boot_env[GRANT_TOOL_ENV] = grant_tool
                # Decision A2 marker (#544): an authority-free FACT carrying the
                # resumed approval's gate kind (the actual gate_kind column value,
                # e.g. 'policy' or 'permission'). After the approved-only gate in
                # approval_resumed_kind, only a genuinely approved approval injects
                # it at all. The runner's observe-only turn-end reconciliation acts
                # only on 'policy' (warning if the approved business action never
                # ran); a 'permission' marker is inert there. Grants nothing
                # (contrast the grant above); getattr-tolerant of binding doubles
                # that do not carry the method, like the grant.
                resumed_kind_fn = getattr(self._binding, "approval_resumed_kind", None)
                resumed_kind = (
                    await resumed_kind_fn(qevent.event_id, resolved.agent_id)
                    if resumed_kind_fn is not None
                    else None
                )
                if resumed_kind:
                    boot_env[RESUMED_KIND_ENV] = resumed_kind
                # ADR-0076 Stone 3 (#889, epic #512): the resolved terminal
                # decision (approved/rejected/expired), authority-free like the
                # marker above, so the runner can stamp it on the turn's OTel
                # span. Reports all three terminal statuses, not just approved
                # (contrast resumed_kind), closing the "did an approval get
                # requested" gap ADR-0038 named open. getattr-tolerant of
                # binding doubles that do not carry the method, like the two above.
                decision_fn = getattr(self._binding, "approval_decision", None)
                decision = (
                    await decision_fn(qevent.event_id, resolved.agent_id)
                    if decision_fn is not None
                    else None
                )
                if decision:
                    boot_env[DECISION_ENV] = decision
                # Resolve the agent's packs once here (a pure parse, no I/O) and
                # reuse: the nav pack is threaded to the final render, the same
                # packs feed the shimmer below.
                packs = self._binding.packs_for(resolved)
                # getattr: binding doubles (tests, alternate resolvers) may not
                # carry the routes attribute; absent means unbound (#247).
                approval_routes = getattr(resolved, "approval_routes", None)
                nav = packs.nav
                # Raise the shimmer (#1312). Deliberately placed HERE, after the
                # binding resolved and before ``_attempt`` claims a sandbox: a
                # channel we are about to refuse never gets a caption that would
                # flicker straight back off, and a cold claim (up to
                # claim_timeout) is spent with the shimmer already lit rather than
                # in silence. Best-effort and outside the concurrency-critical
                # section, like the clear below.
                if self._config.shimmer:
                    await self._set_shimmer(qevent, packs)

            attempt = 0
            while True:
                attempt += 1
                outcome = await self._attempt(qevent, release_order, boot_env, agent_id, nav, packs)

                if outcome.status is SessionStatus.AWAITING_APPROVAL:
                    # A gate fired (ADR-0010): persist the durable record, then
                    # suspend the session until a human resolves it. The event
                    # is done -- the resolution arrives as its own queued turn.
                    await self._pause_for_approval(qevent, outcome, agent_id, approval_routes)
                    await self._markers.mark_done(event_id)
                    return

                if outcome.terminal_ok:
                    await self._markers.mark_done(event_id)
                    return

                if outcome.saw_side_effect:
                    await self._escalate(
                        qevent,
                        f"The run hit an error ({outcome.classification or 'unknown'}) after "
                        "starting an action; not retrying automatically. Flagging for a human.",
                    )
                    await self._markers.mark_done(event_id)
                    return

                retryable = outcome.classification in RETRYABLE_CLASSIFICATIONS
                if not retryable or attempt >= self._config.max_attempts:
                    await self._escalate(
                        qevent,
                        f"The run failed ({outcome.classification or 'unknown'}) after "
                        f"{attempt} attempt(s). Flagging for a human.",
                    )
                    await self._markers.mark_done(event_id)
                    return

                await asyncio.sleep(self._backoff(attempt))
        finally:
            release_order()
            # Lower the assistant-thread "shimmer" raised above, on every exit
            # path (success, escalate, drop, or error). Best-effort and
            # idempotent -- it never repeats an action or blocks the turn, so it
            # is safe outside the concurrency-critical section above.
            #
            # Unconditional on the exit paths that never raised one (an unmapped
            # channel, a paused agent, an already-done event) on purpose: clearing
            # a status that was never set is a no-op on Slack's side, and the
            # alternative is tracking "did we set it" across every early return in
            # this function, which is more state on the sacred path for no gain.
            if self._config.shimmer:
                await self._sink.clear_status(
                    channel=qevent.reply_handle.channel,
                    thread_ts=qevent.conversation_id,
                    endpoint=qevent.reply_handle.endpoint,
                )

    def _acquire_order_entry(self, thread: str) -> _LockEntry:
        entry = self._order_locks.get(thread)
        if entry is None:
            entry = _LockEntry(asyncio.Lock())
            self._order_locks[thread] = entry
        entry.refs += 1
        return entry

    def _release_order_entry(self, thread: str, entry: _LockEntry) -> None:
        entry.refs -= 1
        if entry.refs == 0 and self._order_locks.get(thread) is entry:
            del self._order_locks[thread]

    async def reap_orphans(self) -> list[str]:
        """Periodic tick: delete substrate claims no live route references."""
        return await asyncio.to_thread(self._substrate.reap_orphans)

    async def interrupt_thread(self, thread_key: str, reason: str) -> bool:
        """Hard-stop the thread's live turn. True if a live runner was signalled."""
        handle = await asyncio.to_thread(self._substrate.lookup, thread_key)
        if handle is None:
            return False
        await self._runner.interrupt(handle.base_url, reason, token=handle.token or None)
        return True

    async def release_thread(self, thread_key: str) -> bool:
        """Force-release the thread's sandbox (#713, an operator action): delete
        its substrate claim and route so the next message cold-creates fresh,
        picking up current model/Slack config instead of adopting a sandbox
        that may be running stale env from when it first booted. History is
        not lost -- a cold-created sandbox rehydrates its transcript from the
        durable state store on claim, the same as any other fresh claim.

        Any live turn is interrupted first so `release` never yanks the claim
        out from under a running turn without at least trying to stop it
        cleanly, but the interrupt is a courtesy and never a precondition
        (#739). The case that matters is a live handle whose runner is
        unresponsive: a wedged runner accepts the TCP connect and then answers
        `/v1/interrupt` never, so the call rides `RunnerClient`'s own 600s
        request timeout. That is exactly the sandbox an operator is resetting,
        and awaiting it unbounded costs twice: the release line below is never
        reached, and the reset request has already been SPOPped off the pending
        set by the maintenance tick, so a raise loses it permanently rather
        than retrying next tick. The same tick also owns stream reclaim and
        orphan reaping, which stall for the whole window behind it.

        So the interrupt is bounded to `_RESET_INTERRUPT_TIMEOUT_S` and any
        failure (timeout, transport error, non-200) is logged and swallowed; the
        release then runs unconditionally. `CancelledError` is deliberately not
        swallowed, so worker shutdown still cuts through.

        The release itself is also bounded, to `_RESET_RELEASE_TIMEOUT_S` (#743):
        a hang in the K8s control plane rather than the runner would otherwise
        stall this `asyncio.to_thread` -- and therefore the whole maintenance
        tick behind it -- indefinitely too, since `to_thread` is not
        cancellable. A timed-out release is NOT swallowed here; it propagates
        like any other release failure, which the drain loop's per-request
        handler already logs and isolates from the rest of the batch.

        The release runs under the SAME per-thread route lock the turn path
        holds around `_route_and_start` (#734). Without it the release is
        lock-free while a concurrent turn for this thread holds only that lock,
        so a message arriving in the window between the interrupt above and the
        release below can `claim()`-adopt the very sandbox this is tearing down
        and open a turn on it -- which the release then yanks mid-run. The
        interrupt cannot cover that turn: it fired before the turn existed, so
        it no-oped. Taking the lock makes the two mutually exclusive. A reset
        that wins the lock drops the route while holding it, so a turn waiting
        to route then cold-creates a fresh sandbox (exactly the reset's intent)
        instead of adopting the doomed one. A turn that wins the lock opens
        first and the reset serializes behind its `_route_and_start`, then tears
        it down as an ordinary live-thread reset (the turn replays on a fresh
        sandbox via the `runner-error` retry path described below). Either way
        no turn is ever left streaming on a sandbox this released out from under
        it without the reset first having serialized against its start.

        The lock hold spans only the release (interrupt-then-lock, not the
        reverse), so the bounded-but-possibly-slow courtesy interrupt does not
        extend the window turn-starts for this thread are blocked; the hold is
        the release bound (a few seconds), well under the lock's TTL.

        The failure log is an ERROR, not a warning: it is the only record that a
        sandbox was pulled out from under a turn that may still be running, and
        there is no retry that would produce a second, louder signal. The two
        success shapes are logged apart from it (and from each other) so an
        operator can tell "the live turn was killed" from "nothing was running"
        from "we released blind".

        Behavioral note for the unconditional release: when the thread really did
        have a live turn, tearing its sandbox down mid-run drops the turn stream,
        which `_consume` classifies as `runner-error`. That is in
        `RETRYABLE_CLASSIFICATIONS`, so the driving loop in `_run_event` retries
        the turn, and the retry re-claims, which cold-creates a fresh sandbox on
        current config -- exactly the state the reset was asking for. The
        no-auto-retry-after-side-effects rule still holds: an attempt that saw a
        `SideEffectFlag` escalates to a human instead of replaying. So a reset of
        a live thread is a replay on a fresh sandbox, not a lost turn, and the
        thread is never left routeless.

        True if a route existed to release."""
        try:
            interrupted = await asyncio.wait_for(
                self.interrupt_thread(thread_key, "operator requested a sandbox reset"),
                _RESET_INTERRUPT_TIMEOUT_S,
            )
        except Exception:
            logger.error(
                "reset: interrupt did not land for thread %s (timed out or errored); "
                "releasing the sandbox anyway, so a turn that is still live loses it "
                "mid-run and replays on a fresh sandbox",
                thread_key,
                exc_info=True,
            )
        else:
            if interrupted:
                logger.info(
                    "reset: interrupted the live turn on thread %s before releasing",
                    thread_key,
                )
            else:
                logger.info("reset: no live runner to interrupt on thread %s", thread_key)
        # Serialize the teardown against `_route_and_start` for this thread by
        # holding the same route lock the turn path holds (#734). Both the lock
        # acquisition and the release run on their own bounds, so a wedged turn
        # or control plane cannot park the maintenance tick here; a lock that
        # cannot be taken in time propagates as a release failure, same as a
        # timed-out release.
        lock_key = self._config.lock_key(thread_key)
        # This outer bound shadows `acquire`'s own configured
        # `lock_acquire_timeout_s` (45s by default): whichever fires first wins,
        # and `_RESET_LOCK_ACQUIRE_TIMEOUT_S` is the shorter one, so it is the
        # effective bound on the reset path. Both raise `TimeoutError` (the
        # inner one as `LockAcquireTimeout`), so the caller sees one shape.
        token = await asyncio.wait_for(self._lock.acquire(lock_key), _RESET_LOCK_ACQUIRE_TIMEOUT_S)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._substrate.release, thread_key),
                _RESET_RELEASE_TIMEOUT_S,
            )
        finally:
            await self._lock.release(lock_key, token)

    def attach_killswitch(self, killswitch: KillSwitch) -> None:
        """Wire the kill switch after construction (it needs interrupt_agent)."""
        self._killswitch = killswitch

    async def interrupt_agent(self, agent_id: uuid.UUID) -> int:
        """Interrupt every live turn belonging to an agent (kill switch). Returns
        the number of turns signalled. The kill flag stays set (the API owns it),
        so new runs are refused by the is_killed check until resume.

        Threads are signalled concurrently, and each interrupt is individually
        bounded to `_KILL_INTERRUPT_TIMEOUT_S` (#742): a wedged runner on one
        thread must not delay -- let alone block -- the interrupt reaching the
        agent's other live threads. A timed-out or otherwise failed interrupt is
        logged and does not stop the rest of the fan-out; there is no fallback
        release to run afterward on this path (unlike `release_thread`), so the
        failure is surfaced via logging rather than swallowed."""
        threads = list(self._active_by_agent.get(agent_id, set()))

        async def _interrupt_one(thread: str) -> bool:
            try:
                return await asyncio.wait_for(
                    self.interrupt_thread(thread, f"agent {agent_id} killed by operator"),
                    _KILL_INTERRUPT_TIMEOUT_S,
                )
            except Exception:
                logger.error(
                    "kill: interrupt did not land for thread %s of agent %s (timed out "
                    "or errored); continuing to signal its other live threads",
                    thread,
                    agent_id,
                    exc_info=True,
                )
                return False

        results = await asyncio.gather(*(_interrupt_one(thread) for thread in threads))
        signalled = sum(results)
        logger.info("kill: interrupted %d live turn(s) for agent %s", signalled, agent_id)
        return signalled

    def _register_run(self, agent_id: uuid.UUID | None, thread: str) -> None:
        if agent_id is not None:
            self._active_by_agent.setdefault(agent_id, set()).add(thread)

    def _unregister_run(self, agent_id: uuid.UUID | None, thread: str) -> None:
        if agent_id is None:
            return
        threads = self._active_by_agent.get(agent_id)
        if threads is not None:
            threads.discard(thread)
            if not threads:
                del self._active_by_agent[agent_id]

    async def _drop_with_message(self, qevent: QueuedTurn, message: str) -> None:
        """Edit the placeholder with a reason and mark the event done (a polite
        drop for an unmapped channel or a paused agent, never a crash)."""
        await self._sink.update(
            channel=qevent.reply_handle.channel,
            ts=qevent.reply_handle.placeholder,
            text=message,
            endpoint=qevent.reply_handle.endpoint,
        )
        await self._markers.mark_done(qevent.event_id)

    async def _set_shimmer(self, qevent: QueuedTurn, packs: BehaviorPacks) -> None:
        """Raise the shimmer for this turn, and own the only side that lowers it.

        The caption is this agent's sampled load line (+ tip), seeded by the thread
        ts, falling back to the operator's generic ``status_text`` when the agent
        enables neither pack. That fallback is why this is unconditional now: the
        dispatcher used to set the generic caption before enqueueing, so a slow
        Slack call delayed the durable ``XADD`` of the turn, and set and clear
        lived in two different processes where a fast turn could clear before the
        set landed and strand a caption until Slack's own timeout (#1312). Both
        halves are on this side now, ordered by the same ``await`` chain, so that
        race cannot be expressed rather than merely being tested for.

        Best-effort: the sink swallows errors, so a workspace without the
        assistant feature costs one debug line and nothing else.
        """
        load = sample_load(packs, qevent.conversation_id)
        tip = sample_tip(packs, qevent.conversation_id)
        if load and tip:
            caption = f"{load}\n\nTip: {tip}"
        elif load:
            caption = load
        elif tip:
            caption = f"Tip: {tip}"
        else:
            caption = self._config.status_text
        if not caption:
            # An operator who blanks status_text wants no caption at all; setting
            # an empty status would read as a clear, not as a shimmer.
            return
        await self._sink.set_status(
            channel=qevent.reply_handle.channel,
            thread_ts=qevent.conversation_id,
            status=caption,
            endpoint=qevent.reply_handle.endpoint,
        )

    # -- internals ------------------------------------------------------------

    async def _attempt(
        self,
        qevent: QueuedTurn,
        release_order: Callable[[], None],
        boot_env: dict[str, str] | None = None,
        agent_id: uuid.UUID | None = None,
        nav: NavPack | None = None,
        packs: BehaviorPacks | None = None,
    ) -> TurnOutcome:
        thread = qevent.conversation_id

        # Surface a booting state on the placeholder so the (up to claim_timeout)
        # cold-boot wait is not silent. Best-effort and outside the per-thread lock:
        # a Slack failure here must never fail the turn, and this must not lengthen
        # the critical section. Fires once per attempt (retries re-affirm it).
        # Suppressed under no-edit streaming: that mode's contract is exactly one
        # chat.update (the final edit), so it opts out of the pre-boot edit too.
        if not self._config.slack_no_edit_streaming:
            try:
                await self._sink.update(
                    channel=qevent.reply_handle.channel,
                    ts=qevent.reply_handle.placeholder,
                    text=self._config.booting_text,
                    endpoint=qevent.reply_handle.endpoint,
                )
            except Exception:
                logger.warning("booting-state update failed for %s", qevent.event_id)

        event = self._to_event(qevent)

        # Critical section: decide steer-vs-new-turn and, if new, open the turn so
        # it is active before we release the Valkey lock (rule 1: no two live
        # turns per thread across workers). Then release the order lock so the
        # next same-thread event can route, and release the Valkey lock before
        # streaming so a follow-up can steer.
        try:
            async with self._lock.hold(self._config.lock_key(thread)):
                route = await self._route_and_start(thread, event, boot_env, packs)
        except CapacityExhaustedError as exc:
            release_order()
            rejection = exc.rejection
            logger.warning(
                "sandbox capacity exhausted for event %s: quota=%s resource=%s "
                "requested=%s used=%s hard=%s",
                qevent.event_id,
                rejection.quota_name,
                rejection.resource,
                rejection.requested,
                rejection.used,
                rejection.hard,
            )
            if self._is_approval_resume(qevent.event_id):
                return TurnOutcome(terminal_ok=False, classification="runner-error")
            await self._sink.update(
                channel=qevent.reply_handle.channel,
                ts=qevent.reply_handle.placeholder,
                text=(
                    "This agent is at sandbox capacity. ResourceQuota "
                    f"{rejection.quota_name} rejected {rejection.resource}: "
                    f"requested {rejection.requested}, observed usage "
                    f"{rejection.used}, hard limit {rejection.hard}. Try again "
                    "after another conversation releases its sandbox."
                ),
                endpoint=qevent.reply_handle.endpoint,
            )
            return TurnOutcome(terminal_ok=True)
        except (RunnerError, aiohttp.ClientError, TimeoutError, SandboxError) as exc:
            # The turn was never accepted (transient runner 5xx, runner not ready,
            # claim timeout, route-lock acquire timeout). Convert to a retryable
            # outcome so process_event backs off and retries within max_attempts,
            # instead of letting the entry escape to the consumer and sit pending
            # for the whole reclaim window.
            release_order()
            logger.warning("turn start failed for %s: %r", qevent.event_id, exc)
            return TurnOutcome(terminal_ok=False, classification="runner-error")
        release_order()

        if route.canned_reply is not None:
            # An enabled greeting/help pack matched a provably-fresh thread under
            # the route lock (ADR-0018). Deliver the canned reply onto the
            # placeholder and return terminal-ok so process_event marks the event
            # done. No run was registered, no sandbox claimed, no turn started.
            await self._sink.update(
                channel=qevent.reply_handle.channel,
                ts=qevent.reply_handle.placeholder,
                text=route.canned_reply,
                endpoint=qevent.reply_handle.endpoint,
            )
            return TurnOutcome(terminal_ok=True)

        if route.steered:
            # Delivered into the thread's live turn; that turn streams the output
            # onto its own placeholder. Retire this follow-up's placeholder so it
            # does not sit stuck on "working" in the thread.
            #
            # Steering is best-effort by design (mirror Claude Code, arch 2b rule
            # 3): the follow-up joins the live turn's context. If that owning turn
            # later fails and retries, the retry replays only its own event, so a
            # steer folded into a since-failed turn is not itself replayed. This is
            # the accepted MVP semantic; durable per-steer replay is a deliberate
            # follow-up, flagged to the orchestrator rather than silently assumed.
            await self._sink.update(
                channel=qevent.reply_handle.channel,
                ts=qevent.reply_handle.placeholder,
                text="Folded into the in-progress reply above.",
                endpoint=qevent.reply_handle.endpoint,
            )
            return TurnOutcome(terminal_ok=True, steered=True)

        assert route.handle is not None and route.turn is not None
        # Register this owner turn so a kill for its agent interrupts it, then
        # stream; unregister when the turn ends.
        self._register_run(agent_id, thread)
        try:
            # Close the precheck-vs-register race: a kill that landed between the
            # is_killed precheck and this registration would have interrupted zero
            # turns. Recheck now that the turn is registered and interrupt it.
            if (
                agent_id is not None
                and self._killswitch is not None
                and await self._killswitch.is_killed(agent_id)
            ):
                await self.interrupt_thread(thread, f"agent {agent_id} killed by operator")
            return await self._consume(qevent, route.turn, nav)
        finally:
            self._unregister_run(agent_id, thread)

    async def _route_and_start(
        self,
        thread: str,
        event: Event,
        boot_env: dict[str, str] | None,
        packs: BehaviorPacks | None = None,
    ) -> _RouteResult:
        # Greeting/help pre-model short-circuit (ADR-0018): under the per-thread
        # route lock, if an enabled greeting/help pack matches the message text AND
        # the thread has no existing route, it is provably a NEW turn (it cannot be
        # a steer -- rule 1 holds by construction, since the lookup and the routing
        # both run under this same lock), so answer canned without claiming a
        # sandbox or starting a model turn. Any existing route falls through to the
        # normal claim -> steer/start_turn path below.
        if packs is not None:
            reply = match_greeting(packs, event.text) or match_help(packs, event.text)
            if reply is not None:
                existing = await asyncio.to_thread(self._substrate.lookup, thread)
                if existing is None:
                    return _RouteResult(steered=False, canned_reply=reply)
        # claim() adopts the thread's live sandbox and refreshes its route TTL
        # (so a busy thread past route_ttl is not reaped), or claims a warm one /
        # resumes a suspended one. On a fresh claim the boot env binds the agent's
        # bundle + budget; on an adopt the live sandbox is already bound, so the
        # env is ignored. Then try to steer: a live turn takes the follow-up;
        # otherwise (fresh sandbox, or the finish-race 409) we open a new turn.
        #
        # Timed separately from the model turn itself (#718): a cold claim (no
        # warm pool hit, a fresh `docker run`/pod create) and a slow model
        # response present identically to an end user ("it's just slow"), but
        # have completely different fixes (a warm pool vs. a faster/cheaper
        # model). This is the only place that can measure claim latency at
        # all -- the runner's own per-turn logging starts only once its
        # process is already up, so it cannot see the wait that got it there.
        claim_started = time.monotonic()
        handle = await self._claim_or_resume(thread, boot_env)
        claim_ms = round((time.monotonic() - claim_started) * 1000)
        logger.info("claim latency for %s: %d ms", thread, claim_ms)
        if await self._runner.steer(handle.base_url, event, token=handle.token or None):
            return _RouteResult(steered=True)
        turn = await self._runner.start_turn(handle.base_url, event, token=handle.token or None)
        return _RouteResult(steered=False, handle=handle, turn=turn)

    async def _claim_or_resume(self, thread: str, boot_env: dict[str, str] | None) -> SandboxHandle:
        try:
            return await asyncio.to_thread(self._substrate.claim, thread, env=boot_env)
        except SuspendedThreadError:
            # Resume with the same bound boot env a fresh claim gets (bundle
            # ref, budget, refs): a suspended pod was deleted (ADR-0003), so
            # the replacement boots from env alone; without this it would come
            # up generic, without the agent's bundle.
            return await asyncio.to_thread(self._substrate.resume, thread, env=boot_env)

    @staticmethod
    def _is_approval_resume(event_id: str) -> bool:
        # The deterministic key the API stamps on every approval resume turn
        # (``approval-<id>-resolved``; resumequeue.resume_event_id). The suffix is
        # a frozen historical contract shared by the resolve and expiry paths, so
        # matching it here does not couple to a mutable string.
        return event_id.startswith("approval-") and event_id.endswith("-resolved")

    async def _finalize_settled_card(self, qevent: QueuedTurn) -> None:
        """Settle the approval card when its approval resumes (#419, #1084).

        Every terminal transition ends here, because the card outlives the
        decision and nothing else in the system owns it. An EXPIRY (#419) has no
        click at all: the #412 sweeper or a past-SLA resolve flips the record and
        enqueues a ``[approval expired]`` turn, and the buttons would otherwise
        keep looking live. A RESOLVE (#1084) has a click only sometimes -- a
        resolution that arrived through ``POST /approvals/{id}/resolve`` or
        ``curie <tier> approvals --resolve`` never touched Slack, so the card
        stayed live there too, and every later click earned a 409.

        Both forms render through the SAME function the dispatcher's click path
        is pinned against, so a CLI resolve and a button click leave the same
        card behind. That convergence is the point of #1084; two renderers on one
        surface is what it was filed to stop.

        Idempotence, and why an unconditional re-stamp is safe: ``pop`` is a
        GETDEL of the ref, the only pointer the platform still holds to the
        posted card, so at most one turn holds it at a time; of the turns that
        hold it, only the one whose approval the ref belongs to keeps it and
        stamps, and every other holder puts it back. A redelivery after a stamp
        finds nothing. Within the pass that does claim the ref, the stamp may
        land on a card the dispatcher already stamped from a click, which is a
        rewrite to an equivalent card rather than a second verdict -- the shared
        renderer is what makes "equivalent" true, and a test pins it. A card
        stamped here and then clicked late gets the existing already-resolved
        refusal from the API, unchanged.

        Fully best-effort: nothing here may fail the resume, the cheap event-id
        check gates all of it so ordinary turns pay nothing, and a record that
        cannot be read leaves BOTH the card and its ref alone rather than
        stamping a verdict the kernel had to guess. That makes an unreadable
        record a deferral instead of a permanent loss (#1199): ``ApprovalReader``
        never raises, so before #1199 one transient blip stranded the card with
        live-looking buttons forever. That closes a one-way door, but it is not
        an unconditional later settle: this runs once per ``process_event``,
        outside the retry loop, and an otherwise-healthy turn goes on to write
        the done marker, after which every redelivery is skipped. The surviving
        ref is therefore revisited only when that same delivery ALSO failed to
        reach ``mark_done`` and the entry is reclaimed (ADR-0039, #505).

        Two honest consequences. An approval-resume turn whose ref is already
        gone now pays one record read before finding that out, a cost that lands
        only on approval-resume turns. And a PERMANENT reason for no outcome (no
        reader configured, an id that does not parse, a record somehow not
        resolved) leaves the ref in Valkey until its TTL lapses or a later
        approval on the same thread overwrites it, rather than being cleaned up
        eagerly -- telling transient from permanent here would mean guessing.
        That lingering ref is NOT harmless on its own: a later approval's resume
        on the same thread would pop it and stamp on it. What makes it safe is
        the pairing check below, not the entry being per-thread and TTL-bounded.

        The pairing check, and why it puts the ref back: the entry is keyed by
        thread, so ``remember`` carries the approval id (#1199) and a popped ref
        whose id is not the one this resume is settling is not stamped -- that
        card belongs to another, still-pending approval, and stamping it would
        state this approval's verdict about that one. It is written back
        conditionally (``ApprovalCardStore.restore``, a ``SET NX``), because
        destroying it would strand that other approval's card with live buttons
        nothing can ever settle: the same harm #1199 is about, arriving through
        the refusal instead of through the record read, and terminal on the
        expiry path where no click exists to heal it.
        """

        if self._card_store is None or not self._is_approval_resume(qevent.event_id):
            return
        # Computed once, and the only thing the two forms disagree about. It is
        # an explicit flag rather than "no outcome to stamp" because those two
        # facts coincide only by way of the early return below: soften that
        # return and an APPROVED card whose record blipped would render EXPIRED.
        is_expiry = qevent.text.startswith(_EXPIRY_RESUME_MARKER)
        try:
            # Expiry states only that nobody decided, so it needs no record read;
            # a resolve states what was decided, and that comes from the record.
            # The read is the only step that differs, so it is the only step
            # inside the branch: the pop, the pairing check and its put-back
            # below are written once and apply to both, because a check present
            # on one path only is a wrong-card stamp on the other.
            outcome: SettledCard | None = None
            if not is_expiry:
                # Read first, pop second (#1199). The read is the step that can
                # come back empty for a reason that later passes could recover
                # from, and the pop is irreversible; doing them in this order is
                # what keeps a blip a deferral. The pop is still the GETDEL that
                # makes the stamp exactly-once, just claimed one step later.
                outcome = await self._settled_from_record(qevent)
                if outcome is None:
                    # Logged because a deliberate non-stamp otherwise looks
                    # identical to there having been no card at all: nothing was
                    # popped, so the ref is still there for a later pass.
                    logger.info(
                        "no readable approval outcome for thread %s -- "
                        "leaving its card ref in place, not stamped",
                        qevent.conversation_id,
                    )
                    return
            ref = await self._card_store.pop(qevent.conversation_id)
            if ref is None:
                return
            # The ref is keyed by thread, so this is what tells "my card" from a
            # card another approval on this thread posted -- either by
            # overwriting the ref inside the record-read window above, or by
            # leaving a stale one behind that this resume just popped. An EMPTY
            # id is an entry remembered before #1199 (they outlive a deploy);
            # it stamps exactly as it did then, because refusing there would
            # strand every pre-upgrade card instead of protecting anything.
            resume_approval_id = _approval_id_from_resume_event(qevent.event_id)
            if ref.approval_id and ref.approval_id != resume_approval_id:
                # Put back conditionally so the approval it really belongs to can
                # still settle its own card; a newer entry, if one arrived, wins.
                await self._card_store.restore(qevent.conversation_id, ref)
                # Logged because a deliberate non-stamp otherwise looks
                # identical to there having been no card at all.
                logger.info(
                    "approval card for thread %s belongs to approval %s, not %s -- not stamped",
                    qevent.conversation_id,
                    ref.approval_id,
                    resume_approval_id,
                )
                return
            if is_expiry:
                # The branch above left the outcome unread, on purpose: an expiry
                # says only that nobody decided.
                settled = SettledCard(requested_by=ref.requested_by)
            else:
                # The resolve branch returned above unless it read an outcome.
                assert outcome is not None
                settled = SettledCard(
                    requested_by=ref.requested_by,
                    decision=outcome.decision,
                    resolver=outcome.resolver,
                    note=outcome.note,
                )
            # Emit the channel-neutral summary (ADR-0020) plus the semantic
            # outcome; the adapter renders the buttonless settled card below the
            # seam.
            await self._sink.update_message(
                channel=ref.channel,
                ts=ref.ts,
                message=OutboundMessage(version=MESSAGE_VERSION, text=ref.summary),
                endpoint=ref.endpoint,
                settled=settled,
            )
            logger.info("settled approval card for thread %s", qevent.conversation_id)
        except Exception as exc:  # noqa: BLE001 - card teardown is best-effort
            logger.warning(
                "approval card teardown failed for thread %s: %s",
                qevent.conversation_id,
                exc,
            )

    async def _settled_from_record(self, qevent: QueuedTurn) -> SettledCard | None:
        """The resolved outcome to stamp, read from the durable record.

        Read, not parsed. The resume turn does state the decision, the resolver
        and the note, but it states them in a sentence written for a language
        model; reconstructing them by regex would make the card's correctness
        depend on that wording. The approval id comes out of the resume turn's
        deterministic ``event_id`` instead, which is a frozen key shared with
        ``resumequeue.resume_event_id``.

        None means "do not stamp": no reader configured, an id that does not
        parse, a record that could not be read, or a record that is somehow not
        resolved. Leaving a live-looking card is a smaller wrong than stamping a
        verdict nobody confirmed.
        """

        if self._approval_reader is None:
            return None
        approval_id = _approval_id_from_resume_event(qevent.event_id)
        if approval_id is None:
            return None
        record = await self._approval_reader.get(approval_id)
        if record is None or record.status not in ("approved", "rejected"):
            return None
        return SettledCard(
            requested_by="",
            decision=record.status,
            resolver=record.resolved_by,
            note=record.resolution_note,
        )

    async def _pause_for_approval(
        self,
        qevent: QueuedTurn,
        outcome: TurnOutcome,
        agent_id: uuid.UUID | None,
        approval_routes: dict[str, Any] | None = None,
    ) -> None:
        """Persist the approval, suspend the session, and leave the pending notice.

        Ordering is deliberate: the durable record exists before the sandbox is
        suspended, so there is never a suspended session without a record that
        can wake it. The converse crash (record created, suspend or notice
        lost) self-heals -- creation is idempotent on the event id, and the
        resume path cold-claims a fresh sandbox regardless (ADR-0003).

        ``approval_routes`` is the agent's per-deployment route-binding map
        (#247): when the request named a route bound to a channel, the card is
        routed there and that channel's members become the approvers. A named
        but UNBOUND route (declared in the manifest, not bound in this agent's
        deployment config) is ESCALATED loudly rather than routed to the
        requesting channel (#544, Decision B, reversing #247): silently widening
        authority to whoever happens to be in the requesting channel is exactly
        the failure AC2 closes. No approval is created in that case.
        """

        thread = qevent.conversation_id
        summary = outcome.approval_summary or outcome.text or "Approval requested"

        # Resolve the manifest route (#247) to its workspace channel. A named
        # route that resolves to no binding escalates instead of widening (#544).
        route = outcome.approval_route
        card_channel = qevent.reply_handle.channel
        if route:
            binding = (approval_routes or {}).get(route)
            bound = binding.get("channel") if isinstance(binding, dict) else None
            if bound:
                card_channel = str(bound)
            else:
                logger.warning(
                    "approval route %r is not bound for agent %s; escalating "
                    "rather than routing the card to the requesting channel",
                    route,
                    agent_id,
                )
                await self._escalate(
                    qevent,
                    f"The run requested approval via route {route!r}, but that "
                    "route is not bound to a channel for this agent; flagging for "
                    "a human instead of widening the request to this channel.",
                )
                return

        if self._approvals is None:
            await self._escalate(
                qevent,
                "The run requested an approval, but no approval backend is "
                "configured on this worker; flagging for a human instead of pausing.",
            )
            return

        try:
            created = await self._approvals.create(
                ApprovalRequest(
                    agent_id=agent_id,
                    conversation_id=thread,
                    author=qevent.author,
                    summary=summary,
                    reply_channel=qevent.reply_handle.channel,
                    reply_placeholder=qevent.reply_handle.placeholder,
                    reply_endpoint=qevent.reply_handle.endpoint,
                    dedupe_key=qevent.event_id,
                    route=route,
                    card_channel=card_channel,
                    # The ACI ``final`` frame types this as a bare ``str``, so an
                    # unrecognized value only fails when the shared model
                    # validates it (#492/#544: it is authority-bearing, so it is
                    # rejected, never degraded to None). The cast defers to that
                    # validation; ValidationError below is the rejection path.
                    gate_kind=cast("GateKind | None", outcome.approval_gate_kind),
                    granted_tool=outcome.approval_granted_tool,
                )
            )
        except (ApprovalBackendError, ValidationError) as exc:
            # ValidationError: the shared model rejected the payload at
            # construction (#492) -- an unknown gate_kind, or an empty
            # conversation_id/author/dedupe_key, which the wire's QueuedTurn does
            # not constrain. The API rejected these with a 422 before the model
            # was shared, which surfaced here as ApprovalBackendError; both still
            # escalate to a human rather than stranding the turn.
            logger.warning("approval create failed for %s: %s", qevent.event_id, exc)
            await self._escalate(
                qevent,
                "The run requested an approval, but the approval record could "
                "not be created; flagging for a human instead of pausing.",
            )
            return

        try:
            await asyncio.to_thread(self._substrate.suspend, thread, history_ref=None)
        except SandboxError as exc:
            # Non-fatal: the record is durable and the resume path cold-claims a
            # fresh sandbox either way; a still-live sandbox is just reaped when
            # its route expires.
            logger.warning("suspend failed for thread %s: %s", thread, exc)

        base = outcome.text.strip()
        # The notice is a control string the CLI parses by splitting on blank
        # lines and requiring the marker-leading block (cli/src/chat.rs
        # parse_approval_id, the #766 keep-alive). A model-authored blank line or
        # newline in ``summary`` would break that delimiter and strand the
        # resumed reply (#817), so collapse the interpolated summary to one
        # logical line -- the notice is always a single clean block. The durable
        # ``Approval`` record and the Block Kit card keep the original summary.
        notice_summary = " ".join(summary.split())
        notice = (
            f"Awaiting approval ({created.id}): {notice_summary}\n"
            "The session is paused and will resume once an authorized member "
            "resolves this request."
        )
        await self._sink.update(
            channel=qevent.reply_handle.channel,
            ts=qevent.reply_handle.placeholder,
            text=f"{base}\n\n{notice}" if base else notice,
            endpoint=qevent.reply_handle.endpoint,
        )

        # In the requesting channel the card joins the thread and rides the
        # trigger's transport; a route-bound channel has no such thread and is
        # policy, not a per-turn reply, so it posts top-level over the worker's
        # default Slack transport.
        in_requesting_channel = card_channel == qevent.reply_handle.channel
        card_endpoint = qevent.reply_handle.endpoint if in_requesting_channel else None
        # The approval interaction (#246, ADR-0010/0020): a channel-neutral
        # Confirm intent (Approve/Reject) emitted WITHOUT any Block Kit -- the
        # Slack adapter renders it into the approval card's buttons below the
        # seam. The confirm/cancel actions carry the durable record id so a click
        # resolves exactly this approval through the API's server-side authorizer.
        # Building the message is inside the best-effort try (the channel-neutral
        # models validate on construction, unlike the old blocks builder) so the
        # pause -- the durable record and the resume path -- stands with or without
        # the card, exactly as before.
        try:
            card_message = OutboundMessage(
                version=MESSAGE_VERSION,
                text=summary,
                interaction=ConfirmIntent(
                    kind="confirm",
                    id=created.id,
                    prompt=summary,
                    confirm=Action(label="Approve", value=created.id),
                    cancel=Action(label="Reject", value=created.id),
                    # An approval decision may carry a reason (#1053). This says
                    # only that, semantically; each adapter decides how to
                    # collect it (ADR-0020: the interaction is the port, the
                    # widget is the adapter). Slack renders a dialog on the
                    # click, the terminal renderer a typed reply. The note is
                    # optional in every rendering, and the note the approver
                    # leaves already reaches the requester -- the API stores it
                    # on the record and build_resume_turn interpolates it into
                    # the platform-authored resume text.
                    #
                    # UNCONDITIONAL, and that is the decision rather than an
                    # oversight (#1076). It costs an approver who wants no note
                    # one extra click, on every approval, in every deployment,
                    # so it was worth stating rather than leaving as the only
                    # reachable arm of a branch that looked configurable.
                    #
                    # Rejected: exposing a per-agent or env toggle. A reason is
                    # the half of a rejection the requester actually needs, and
                    # the dialog is what makes leaving one the default rather
                    # than a thing you remember to do from the CLI. A toggle
                    # would also keep TWO settled-card render paths alive
                    # permanently, which is what #1073 and #1084 exist to
                    # collapse into one. And the evidence for whether operators
                    # want the opt-out does not exist yet: discussion #1061
                    # settles that the approval plane's plumbing gets fixed
                    # first and governance knobs are decided from evidence
                    # after, which applies to this knob as much as to #1054's.
                    #
                    # If that evidence arrives, this field is still the one flip
                    # -- but the flip needs an operator-written source, never a
                    # bundle-declared one (the #520 anti-hollow-out rule: an
                    # agent must not widen how its own approvals are collected).
                    allow_free_text=True,
                ),
            )
            card_ts = await self._sink.post(
                channel=card_channel,
                message=card_message,
                requested_by=qevent.author,
                thread_ts=thread if in_requesting_channel else None,
                endpoint=card_endpoint,
            )
        except Exception as exc:  # noqa: BLE001 - the pause stands without the card
            logger.warning("approval card post failed for %s: %s", created.id, exc)
        else:
            # Remember where the card landed so an EXPIRY -- which, unlike a
            # resolve, carries no click to locate the card -- can disable it
            # later (#419). Best-effort: a lost memory only means the card is not
            # auto-disabled, and the resolve-click path still heals it.
            if card_ts and self._card_store is not None:
                try:
                    await self._card_store.remember(
                        thread,
                        channel=card_channel,
                        ts=card_ts,
                        summary=summary,
                        endpoint=card_endpoint,
                        # Pair the ref to the approval it belongs to (#1199).
                        # The entry is keyed by thread, so this is the only
                        # thing that lets the resume turn tell "my card" from a
                        # card another approval on this thread left behind.
                        # ``resumequeue.resume_event_id`` builds that turn's
                        # event id as ``approval-<id>-resolved`` from this same
                        # id, so this is exactly the string
                        # ``_approval_id_from_resume_event`` recovers there.
                        approval_id=str(created.id),
                        # The settled rebuild shows the same "Requested by" line
                        # the live card did, and once the sandbox is gone this
                        # is the worker's only copy of it (#1084).
                        requested_by=qevent.author,
                    )
                except Exception as exc:  # noqa: BLE001 - best-effort memory
                    logger.warning("remembering approval card for %s failed: %s", created.id, exc)
        logger.info("thread %s suspended awaiting approval %s", thread, created.id)

    async def _consume(
        self, qevent: QueuedTurn, turn: TurnStream, nav: NavPack | None = None
    ) -> TurnOutcome:
        acc = _StreamAccumulator()
        reply = _ThrottledReply(
            self._sink,
            channel=qevent.reply_handle.channel,
            ts=qevent.reply_handle.placeholder,
            min_interval_s=self._config.slack_edit_min_interval_s,
            nav=nav,
            no_edit=self._config.slack_no_edit_streaming,
            endpoint=qevent.reply_handle.endpoint,
            # Reply delivery is best-effort ONLY on an approval-resume turn (the
            # granted tool has already executed in the runner): a dead reply
            # endpoint with no default transport completes the turn instead of
            # dead-lettering the resolved approval. Recognized structurally by the
            # resume event_id, the same platform-authored resume signal the #419
            # card teardown keys off (see the marker note above _is_approval_resume).
            # This intentionally covers BOTH resume flavors -- the ``[approval
            # resolved]`` resolve path and the ``[approval expired]`` expiry path --
            # since both carry the ``approval-<id>-resolved`` event_id matched by
            # _is_approval_resume. That shared coverage is deliberate and
            # plan-ratified, not an oversight.
            best_effort=self._is_approval_resume(qevent.event_id),
        )
        try:
            # ``async with`` releases the aiohttp response on every exit path
            # (normal end, apply-frame error, or a mid-stream transport drop), so
            # the connection is never leaked.
            async with turn:
                async for frame in turn:
                    await self._apply_frame(frame, acc, reply, qevent.event_id)
        except (aiohttp.ClientError, TimeoutError) as exc:
            # Stream dropped mid-run (sandbox killed, network fault). No final.
            logger.warning("turn stream dropped for %s: %s", qevent.event_id, exc)
            return TurnOutcome(
                terminal_ok=False,
                saw_side_effect=acc.saw_side_effect,
                classification=acc.classification or "runner-error",
                text=acc.rendered(),
            )

        return await self._finish(acc, reply)

    async def _apply_frame(
        self,
        frame: OutboundEvent,
        acc: _StreamAccumulator,
        reply: _ThrottledReply,
        event_id: str,
    ) -> None:
        if isinstance(frame, TextDelta):
            acc.text_parts.append(frame.text)
            await reply.stream(acc.rendered())
        elif isinstance(frame, ToolNote):
            # Surfaced for context but not part of the answer buffer.
            await reply.stream(acc.rendered())
        elif isinstance(frame, SideEffectFlag):
            acc.saw_side_effect = True
            # Persist immediately so a crash before done still blocks auto-retry.
            await self._markers.mark_side_effect(event_id)
        elif isinstance(frame, ErrorEvent):
            acc.classification = frame.classification or acc.classification
        elif isinstance(frame, Final):
            acc.status = frame.status
            acc.final_text = frame.text
            acc.approval_summary = frame.approval_summary
            acc.approval_route = frame.approval_route
            acc.approval_gate_kind = frame.approval_gate_kind
            acc.approval_granted_tool = frame.approval_granted_tool

    async def _finish(self, acc: _StreamAccumulator, reply: _ThrottledReply) -> TurnOutcome:
        if acc.status in (SessionStatus.DONE, SessionStatus.IDLE_AWAITING_INPUT):
            text = acc.rendered()
            await reply.finalize(text)
            return TurnOutcome(
                terminal_ok=True,
                saw_side_effect=acc.saw_side_effect,
                text=text,
                status=acc.status,
            )
        if acc.status is SessionStatus.AWAITING_APPROVAL:
            # Terminal for this turn, but the placeholder edit is deferred to
            # _pause_for_approval so the pending notice can carry the created
            # record's id (or the escalation, when no backend is wired).
            return TurnOutcome(
                terminal_ok=True,
                saw_side_effect=acc.saw_side_effect,
                text=acc.rendered(),
                status=acc.status,
                approval_summary=acc.approval_summary,
                approval_route=acc.approval_route,
                approval_gate_kind=acc.approval_gate_kind,
                approval_granted_tool=acc.approval_granted_tool,
            )
        # classified-failure, or the stream ended with no final at all.
        return TurnOutcome(
            terminal_ok=False,
            saw_side_effect=acc.saw_side_effect,
            classification=acc.classification or "runner-error",
            text=acc.rendered(),
            status=acc.status,
        )

    async def _escalate(self, qevent: QueuedTurn, message: str) -> None:
        logger.warning("escalating event %s: %s", qevent.event_id, message)
        await self._sink.update(
            channel=qevent.reply_handle.channel,
            ts=qevent.reply_handle.placeholder,
            text=message,
            endpoint=qevent.reply_handle.endpoint,
        )

    def _backoff(self, attempt: int) -> float:
        raw: float = self._config.retry_backoff_base_s * (2 ** (attempt - 1))
        return min(self._config.retry_backoff_max_s, raw)

    @staticmethod
    def _to_event(qevent: QueuedTurn) -> Event:
        return Event(
            type="message",
            text=qevent.text,
            user=qevent.author,
            ts=qevent.conversation_id,
        )
