"""Approval-gate lifecycle tests (#244, ADR-0010): suspend on pending, resume
on resolve, durable across a worker restart.

Same harness discipline as the other kernel suites: real Valkey, the real
substrate over a fake Kubernetes client, an in-process fake ACI runner. Only
Slack, the model, and the approval API (a recording fake at the
``ApprovalCreator`` seam) are faked.
"""

from __future__ import annotations

import asyncio
import uuid

import aiohttp
import pytest
from aci_protocol import Final, QueuedTurn, ReplyHandle, SessionStatus, TextDelta
from channel_protocol import ConfirmIntent
from curie_worker.approvals import (
    ApprovalBackendError,
    ApprovalRequest,
    CreatedApproval,
    SettledApproval,
)
from curie_worker.sandbox.types import RouteState

DONE = SessionStatus.DONE
AWAITING = SessionStatus.AWAITING_APPROVAL


class RecordingApprovals:
    """An ApprovalCreator fake that records requests and mints stable ids."""

    def __init__(self, *, fail: bool = False) -> None:
        self.requests: list[ApprovalRequest] = []
        self.fail = fail

    async def create(self, request: ApprovalRequest) -> CreatedApproval:
        if self.fail:
            raise ApprovalBackendError("approval API unavailable")
        self.requests.append(request)
        return CreatedApproval(id=f"appr-{len(self.requests)}", status="pending")


class RecordingReader:
    """An ApprovalReader fake: hands back one settled record, records the reads.

    Separate from ``RecordingApprovals`` for the same reason the kernel takes two
    parameters (#1084): most tests need only the create half, and a combined fake
    would make every one of them carry a read they never exercise.
    """

    def __init__(self, record: SettledApproval | None) -> None:
        self.record = record
        self.reads: list[str] = []

    async def get(self, approval_id: str) -> SettledApproval | None:
        self.reads.append(approval_id)
        return self.record


def _qevent(
    text: str,
    *,
    thread: str = "th-appr",
    event_id: str | None = None,
    placeholder: str = "p-1",
    endpoint: str | None = None,
) -> QueuedTurn:
    return QueuedTurn(
        event_id=event_id or uuid.uuid4().hex,
        conversation_id=thread,
        author="U1",
        text=text,
        reply_handle=ReplyHandle(
            channel="C1", placeholder=placeholder, endpoint=endpoint
        ),
        received_at="2026-07-14T00:00:00+00:00",
    )


def _awaiting_script(summary: str) -> list:
    return [
        TextDelta(text="Requesting sign-off"),
        Final(text="Requesting sign-off", status=AWAITING, approval_summary=summary),
    ]


def test_awaiting_approval_creates_record_and_suspends(make_harness) -> None:
    async def go() -> None:
        approvals = RecordingApprovals()
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Give ACME a 20% discount")
            ev = _qevent("please discount", event_id="ev-appr-1")
            await h.kernel.process_event(ev)

            # The durable record was created with the turn's identity: the
            # dedupe key is the event id and the reply handle rides along so a
            # resolution can resume into the same placeholder.
            assert len(approvals.requests) == 1
            req = approvals.requests[0]
            assert req.summary == "Give ACME a 20% discount"
            assert req.dedupe_key == "ev-appr-1"
            assert req.conversation_id == ev.conversation_id
            assert req.reply_channel == "C1"
            assert req.reply_placeholder == "p-1"
            assert req.author == "U1"

            # The sandbox was suspended and the route flipped to SUSPENDED.
            modes = [s.operating_mode for s in h.fake_k8s.sandboxes.values()]
            assert modes == ["Suspended"]
            record = h.substrate._affinity.get(ev.conversation_id)
            assert record is not None and record.state is RouteState.SUSPENDED

            # The placeholder carries the pending notice with the record id,
            # and the event is done (no retry loop).
            assert h.sink.last_text is not None
            assert "Awaiting approval (appr-1)" in h.sink.last_text
            assert "Give ACME a 20% discount" in h.sink.last_text
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_multiparagraph_summary_yields_a_single_block_parseable_notice(
    make_harness,
) -> None:
    """A model-authored multi-paragraph summary must not break the CLI notice
    parse (#817).

    The notice is a control string the CLI splits on blank lines, requiring the
    marker-leading block (cli/src/chat.rs parse_approval_id, the #766
    keep-alive). A blank line inside the summary would strand the resumed reply
    (or, on the route-bound path, report the raw notice as a false success). The
    kernel collapses the interpolated summary to one logical line, so the notice
    stays a single block whose trailing ``\\n\\n``-split segment starts with the
    marker -- while the durable record keeps the original summary."""

    async def go() -> None:
        approvals = RecordingApprovals()
        async with make_harness(approvals=approvals) as h:
            summary = "First paragraph of the summary.\n\nSecond paragraph.\nThird line."
            h.runner.default_script = _awaiting_script(summary)
            ev = _qevent("please discount", event_id="ev-appr-multi")
            await h.kernel.process_event(ev)

            # The durable record keeps the original multi-paragraph summary; only
            # the notice display is collapsed.
            assert len(approvals.requests) == 1
            assert approvals.requests[0].summary == summary

            # The placeholder notice is a single logical block: splitting on the
            # blank-line delimiter, the trailing block is the marker-leading
            # notice, exactly what the CLI parser anchors on.
            text = h.sink.last_text
            assert text is not None
            blocks = text.split("\n\n")
            notice = blocks[-1]
            assert notice.startswith("Awaiting approval (appr-1)")
            assert "The session is paused" in notice
            # The summary's own blank line is gone; it reads as one line.
            summary_line, _, _ = notice.partition("\n")
            assert "First paragraph of the summary." in summary_line
            assert "Second paragraph." in summary_line
            assert "Third line." in summary_line

    asyncio.run(go())


def test_pending_state_survives_worker_restart_and_resumes_on_resolve(
    make_harness,
) -> None:
    """The epic's acceptance shape: suspend, replace every worker-side object
    (a fresh harness over the same Valkey routes), then deliver the resolution
    turn and watch the session resume and complete."""

    async def go() -> None:
        approvals = RecordingApprovals()
        thread = "th-restart"
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Refund order 42")
            await h.kernel.process_event(_qevent("refund?", thread=thread))
            record = h.substrate._affinity.get(thread)
            assert record is not None and record.state is RouteState.SUSPENDED

        # "Restart": a brand-new kernel/substrate/runner (nothing in-process
        # survives) over the same Valkey affinity keys. The suspended route is
        # still there because it lives in Valkey, not worker memory.
        async with make_harness(approvals=approvals) as h2:
            record = h2.substrate._affinity.get(thread)
            assert record is not None and record.state is RouteState.SUSPENDED

            # The resolution turn (what the API enqueues on resolve): the
            # kernel must resume the suspended thread, boot a replacement
            # sandbox WITH the bound boot env, and run the turn to done.
            h2.runner.default_script = [
                Final(text="Refund processed.", status=DONE)
            ]
            resume_turn = _qevent(
                "[approval resolved] approved by U9", thread=thread, event_id="ev-resolve-1"
            )
            await h2.kernel.process_event(resume_turn)

            # The suspended claim was retired and a fresh one created; the
            # route is LIVE again and the reply landed.
            record = h2.substrate._affinity.get(thread)
            assert record is not None and record.state is RouteState.LIVE
            assert h2.sink.last_text == "Refund processed."
            assert h2.runner.opened == ["[approval resolved] approved by U9"]

    asyncio.run(go())


def test_resume_injects_boot_env_into_replacement_claim(make_harness) -> None:
    """The dormant-path fix: a resume must boot the replacement sandbox with
    the same bound env a fresh claim gets (bundle ref, budget), not a generic
    env -- the suspended pod is gone (ADR-0003) and env is all a boot has."""

    async def go() -> None:
        approvals = RecordingApprovals()
        async with make_harness(approvals=approvals) as h:
            thread = "th-envmerge"
            h.runner.default_script = _awaiting_script("Ship it")
            await h.kernel.process_event(_qevent("ship?", thread=thread))

            h.runner.default_script = [Final(text="Shipped.", status=DONE)]
            boot_env = {
                "CURIE_BUNDLE_REF": "bundles/agent-v7.tgz",
                "CURIE_BUDGET": '{"max_output_tokens_per_run": 1, "max_usd_per_day": 1.0}',
            }
            handle = await h.kernel._claim_or_resume(thread, boot_env)
            assert handle is not None

            resumed_env = h.fake_k8s.claim_envs[-1]
            assert resumed_env is not None
            assert resumed_env["CURIE_BUNDLE_REF"] == "bundles/agent-v7.tgz"
            assert "CURIE_BUDGET" in resumed_env
            # The substrate still guarantees session identity and a fresh
            # runner token on the replacement claim.
            assert resumed_env.get("CURIE_SESSION_ID")
            assert resumed_env.get("CURIE_RUNNER_TOKEN")

    asyncio.run(go())


class GrantBinding:
    """A binding stand-in that answers approval_grant_tool by event id (#430).

    resolve/boot_env behave like the routed double; approval_grant_tool returns
    the granted tool ONLY for the one resume event id it was configured with,
    mirroring the worker's real derivation from durable approval state.
    """

    def __init__(self, *, grant_event_id: str, grant_tool: str) -> None:
        self.grant_event_id = grant_event_id
        self.grant_tool = grant_tool
        self.agent_id = uuid.uuid4()

    async def resolve(self, channel: str):  # noqa: ANN201
        from curie_worker.binding import ResolvedDeployment

        return ResolvedDeployment(
            agent_id=self.agent_id,
            agent_name="test-agent",
            version_id=uuid.uuid4(),
            version_label="v1",
            bundle_ref=None,
            max_usd_per_day=None,
            max_output_tokens_per_run=None,
        )

    def packs_for(self, resolved):  # noqa: ANN001, ANN201
        from curie_worker.behaviorpacks import BehaviorPacks

        return BehaviorPacks.from_config(None)

    def budget_for(self, resolved):  # noqa: ANN001, ANN201
        from aci_protocol import Budget

        return Budget(max_output_tokens_per_run=1000, max_usd_per_day=1.0)

    def boot_env(self, resolved, thread_key):  # noqa: ANN001, ANN201
        return {"CURIE_SESSION_ID": f"s-{thread_key}"}

    async def approval_grant_tool(self, event_id: str, agent_id):  # noqa: ANN001, ANN201
        return self.grant_tool if event_id == self.grant_event_id else None


def test_resume_claim_injects_approval_grant_tool_env(make_harness) -> None:
    """#430: a resume claim for an approved permission-gate approval injects
    CURIE_APPROVAL_GRANT_TOOL into the boot env passed to the replacement
    claim; a fresh (non-approval) mention injects nothing (the gate re-arms)."""

    async def go() -> None:
        from curie_api.resumequeue import resume_event_id

        grant_event = resume_event_id(uuid.uuid4())
        binding = GrantBinding(
            grant_event_id=grant_event, grant_tool="mcp__github__create_issue"
        )
        async with make_harness(binding=binding) as h:
            # The resume turn carries the approval resume event id -> the grant
            # for the approved tool lands in the boot env of the fresh claim.
            h.runner.default_script = [Final(text="Issue created.", status=DONE)]
            await h.kernel.process_event(
                _qevent(
                    "proceed with the approved action",
                    thread="th-grant",
                    event_id=grant_event,
                )
            )
            resumed_env = h.fake_k8s.claim_envs[-1]
            assert resumed_env is not None
            assert resumed_env.get("CURIE_APPROVAL_GRANT_TOOL") == "mcp__github__create_issue"

            # A fresh, unrelated mention has a different event id -> no grant env
            # (re-armed), so an adopted/warm follow-up cannot inherit an allowance.
            await h.kernel.process_event(
                _qevent("hello there", thread="th-fresh", event_id="ev-fresh-1")
            )
            fresh_env = h.fake_k8s.claim_envs[-1]
            assert fresh_env is not None
            assert "CURIE_APPROVAL_GRANT_TOOL" not in fresh_env

    asyncio.run(go())


def test_no_backend_escalates_instead_of_stranding(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:  # no approvals client wired
            h.runner.default_script = _awaiting_script("Anything")
            ev = _qevent("gate this")
            await h.kernel.process_event(ev)

            assert h.sink.last_text is not None
            assert "no approval backend" in h.sink.last_text
            # Not suspended: a pause nothing could resume would strand the thread.
            modes = [s.operating_mode for s in h.fake_k8s.sandboxes.values()]
            assert modes == ["Running"]
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_backend_failure_escalates_and_does_not_suspend(make_harness) -> None:
    async def go() -> None:
        approvals = RecordingApprovals(fail=True)
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Anything")
            ev = _qevent("gate this")
            await h.kernel.process_event(ev)

            assert h.sink.last_text is not None
            assert "could not be created" in h.sink.last_text
            modes = [s.operating_mode for s in h.fake_k8s.sandboxes.values()]
            assert modes == ["Running"]
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_unknown_gate_kind_escalates_instead_of_stranding_the_turn(make_harness) -> None:
    """#492/#544: ``gate_kind`` is authority-bearing, so the shared wire model
    rejects an unrecognized value rather than degrading it to None (which would
    route it through the prefix fallback and silently widen authority).

    The ACI ``final`` frame types the field as a bare ``str``, so a runner can
    emit anything and the rejection lands at the worker, at construction. Before
    the model was shared this same value was rejected by the API with a 422,
    surfacing as ``ApprovalBackendError`` and escalating; the local raise must
    escalate identically. If it escaped ``_pause_for_approval`` the consumer
    would leave the entry pending, redeliver it until the delivery cap, and
    dead-letter it -- a full LLM re-run per redelivery and silence for the user.
    The done marker is the proof it did not: it is only written once the turn is
    terminally handled."""

    async def go() -> None:
        approvals = RecordingApprovals()
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = [
                TextDelta(text="Requesting sign-off"),
                Final(
                    text="Requesting sign-off",
                    status=AWAITING,
                    approval_summary="Anything",
                    approval_gate_kind="not-a-real-gate",
                ),
            ]
            ev = _qevent("gate this", thread="th-bad-gate")
            await h.kernel.process_event(ev)

            # Escalated to a human, exactly as the 422 path did.
            assert h.sink.last_text is not None
            assert "could not be created" in h.sink.last_text
            # No record was created from the rejected payload.
            assert approvals.requests == []
            # Not suspended: a session no resolution could ever wake.
            modes = [s.operating_mode for s in h.fake_k8s.sandboxes.values()]
            assert modes == ["Running"]
            # Terminally handled, so the entry is acked rather than redelivered.
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_pause_emits_a_confirm_intent_for_the_approval_card(make_harness) -> None:
    """#246, ADR-0020: pausing emits a channel-neutral Confirm intent into the
    approval's thread whose confirm/cancel actions carry the record id (the Slack
    adapter renders it into Block Kit buttons -- see test_slack_sink.py), alongside
    the placeholder notice. The kernel never builds Block Kit itself."""

    async def go() -> None:
        approvals = RecordingApprovals()
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Give ACME a 20% discount")
            ev = _qevent("please discount", thread="th-card")
            await h.kernel.process_event(ev)

            assert len(h.sink.posts) == 1
            channel, message, requested_by, thread_ts, _endpoint = h.sink.posts[0]
            assert channel == "C1"
            assert thread_ts == "th-card"
            assert requested_by == "U1"
            # The mandatory text fallback carries the summary; the adapter derives
            # the "Approval required: ..." card fallback from it below the seam.
            assert message.text == "Give ACME a 20% discount"
            # A Confirm intent, not buttons: the record id rides both actions so a
            # click resolves exactly this approval.
            intent = message.interaction
            assert isinstance(intent, ConfirmIntent)
            assert intent.id == "appr-1"
            assert intent.confirm.value == "appr-1"
            assert intent.cancel.value == "appr-1"
            assert (intent.confirm.label, intent.cancel.label) == ("Approve", "Reject")
            # #1053: the decision may carry a reason. Asserted at the KERNEL,
            # not only at the renderer, because this is the field that decides
            # whether the real product path offers a note at all -- a renderer
            # test alone would stay green with the kernel emitting the default.
            #
            # #1076: and asserted UNCONDITIONALLY, because the value being the
            # same for every card is itself the decision. Nothing here reads
            # config, so if a toggle is ever added this assertion is where the
            # change surfaces, rather than the always-on behavior quietly
            # becoming sometimes-on.
            assert intent.allow_free_text is True

    asyncio.run(go())


def test_every_posted_card_carries_the_note_variant(make_harness) -> None:
    """#1076: always-on is the decision, so pin it across card SHAPES.

    ``allow_free_text`` is set at one call site with no config behind it, which
    is easy to read as an accident of that call site. This drives the two shapes
    that differ -- the in-thread card of an UNROUTED approval and the top-level
    card of a ROUTED one -- and asserts both carry it. A change that made it
    conditional on the routing branch would fail here rather than surfacing only
    as a UX difference nobody tests.
    """

    async def go() -> None:
        # Unrouted: the card joins the requesting thread.
        async with make_harness(approvals=RecordingApprovals()) as h:
            h.runner.default_script = _awaiting_script("Give ACME a 20% discount")
            await h.kernel.process_event(_qevent("please discount", thread="th-unrouted"))
            unrouted = h.sink.posts[0]

        # Routed: the card posts top-level in the bound channel.
        binding = RoutedBinding({"finance": {"channel": "C_FINANCE"}})
        async with make_harness(approvals=RecordingApprovals(), binding=binding) as h:
            h.runner.default_script = _awaiting_routed_script("Approve the invoice", "finance")
            await h.kernel.process_event(_qevent("please invoice", thread="th-routed"))
            routed = h.sink.posts[0]

        # The two really are different shapes, or this test proves nothing.
        assert unrouted[3] == "th-unrouted", "the unrouted card must be in-thread"
        assert routed[3] is None, "the routed card must be top-level"

        for label, (channel, message, _by, _ts, _endpoint) in (
            ("unrouted", unrouted),
            ("routed", routed),
        ):
            intent = message.interaction
            assert isinstance(intent, ConfirmIntent)
            assert intent.allow_free_text is True, (
                f"the {label} card for {channel} came without the note variant; "
                "every card carries it (#1076)"
            )

    asyncio.run(go())


def test_escalation_paths_post_no_card(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:  # no approvals backend wired
            h.runner.default_script = _awaiting_script("Anything")
            await h.kernel.process_event(_qevent("gate this"))
            assert h.sink.posts == []

    asyncio.run(go())


def _awaiting_routed_script(summary: str, route: str) -> list:
    return [
        TextDelta(text="Requesting sign-off"),
        Final(
            text="Requesting sign-off",
            status=AWAITING,
            approval_summary=summary,
            approval_route=route,
        ),
    ]


class RoutedBinding:
    """A minimal binding stand-in: one channel -> one agent with route bindings."""

    def __init__(self, routes: dict | None) -> None:
        self.routes = routes
        self.agent_id = uuid.uuid4()

    async def resolve(self, channel: str):  # noqa: ANN201
        from curie_worker.binding import ResolvedDeployment

        return ResolvedDeployment(
            agent_id=self.agent_id,
            agent_name="test-agent",
            version_id=uuid.uuid4(),
            version_label="v1",
            bundle_ref=None,
            max_usd_per_day=None,
            max_output_tokens_per_run=None,
            approval_routes=self.routes,
        )

    def packs_for(self, resolved):  # noqa: ANN001, ANN201
        from curie_worker.behaviorpacks import BehaviorPacks

        return BehaviorPacks.from_config(None)

    def budget_for(self, resolved):  # noqa: ANN001, ANN201
        from aci_protocol import Budget

        return Budget(max_output_tokens_per_run=1000, max_usd_per_day=1.0)

    def boot_env(self, resolved, thread_key):  # noqa: ANN001, ANN201
        return {"CURIE_SESSION_ID": f"s-{thread_key}"}


def test_routed_approval_cards_go_to_the_bound_channel(make_harness) -> None:
    """#247: the manifest route resolves through the agent's bindings; the card
    lands in the bound channel (top-level, no foreign thread) and the record
    carries route + card_channel so the authorizer counts THAT channel. #451:
    the triggering turn has no per-turn endpoint (a Slack-triggered turn), so
    the card also rides the worker's default Slack transport (``None``)."""

    async def go() -> None:
        approvals = RecordingApprovals()
        binding = RoutedBinding({"managers": {"channel": "C_MGRS"}})
        async with make_harness(approvals=approvals, binding=binding) as h:
            h.runner.default_script = _awaiting_routed_script(
                "Discount for ACME", "managers"
            )
            await h.kernel.process_event(_qevent("discount?", thread="th-routed"))

            req = approvals.requests[0]
            assert req.route == "managers"
            assert req.card_channel == "C_MGRS"
            # Card posted to the bound channel, top-level (no thread there).
            channel, message, _requested_by, thread_ts, endpoint = h.sink.posts[0]
            assert channel == "C_MGRS"
            assert thread_ts is None
            assert isinstance(message.interaction, ConfirmIntent)
            assert endpoint is None

    asyncio.run(go())


def test_unbound_route_escalates_instead_of_routing_to_the_requesting_channel(
    make_harness,
) -> None:
    """(19, #544 Decision B / AC2) A named but UNBOUND route escalates loudly:
    no approval is created and no card is posted, so authority never widens to
    the requesting channel. This deliberately REVERSES #247's silent
    channel-fallback (the behavior this test used to assert) -- the fallback was
    the same silent widening from the other end.
    """

    async def go() -> None:
        approvals = RecordingApprovals()
        binding = RoutedBinding(None)  # agent has no bindings at all
        async with make_harness(approvals=approvals, binding=binding) as h:
            h.runner.default_script = _awaiting_routed_script("Anything", "managers")
            ev = _qevent("gate", thread="th-unbound")
            await h.kernel.process_event(ev)

            # No approval was created for the unresolvable route ...
            assert approvals.requests == []
            # ... and no card was posted anywhere (never widened to a channel).
            assert h.sink.posts == []
            # The human-visible escalation names the unbound route.
            assert h.sink.last_text is not None
            assert "managers" in h.sink.last_text
            # The event is terminally handled (done), not left to retry.
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_routeless_approval_keeps_prior_behavior(make_harness) -> None:
    async def go() -> None:
        approvals = RecordingApprovals()
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Plain request")
            await h.kernel.process_event(_qevent("gate", thread="th-plain"))

            req = approvals.requests[0]
            assert req.route is None
            assert req.card_channel == "C1"

    asyncio.run(go())


# --- Card transport follows the card's channel, not the trigger (#451) --------

_CLI_STUB = "http://localhost:8155"


def test_routed_card_ignores_the_triggering_turns_endpoint(make_harness) -> None:
    """#451: the card's channel is policy (the manifest route binding), so its
    transport must be too. A CLI-triggered turn carries a local stub endpoint;
    delivering a route-bound card through it posts the card at the stub instead
    of the real Slack workspace, so the bound channel never sees it. ``None``
    means the worker's default Slack transport."""

    async def go() -> None:
        approvals = RecordingApprovals()
        binding = RoutedBinding({"managers": {"channel": "C_MGRS"}})
        async with make_harness(approvals=approvals, binding=binding) as h:
            h.runner.default_script = _awaiting_routed_script(
                "Discount for ACME", "managers"
            )
            await h.kernel.process_event(
                _qevent("discount?", thread="th-cli-routed", endpoint=_CLI_STUB)
            )

            channel, _message, _requested_by, thread_ts, endpoint = h.sink.posts[0]
            assert channel == "C_MGRS"
            assert thread_ts is None
            assert endpoint is None

    asyncio.run(go())


def test_card_routed_to_requesting_channel_keeps_the_trigger_endpoint(
    make_harness,
) -> None:
    """The inverse of the routed case: when the route binds back to the channel
    that asked, the card belongs to that conversation -- it threads under it and
    rides the same transport the trigger arrived on, so a CLI-stub turn's card
    stays at the stub."""

    async def go() -> None:
        approvals = RecordingApprovals()
        binding = RoutedBinding({"managers": {"channel": "C1"}})  # the requesting channel
        async with make_harness(approvals=approvals, binding=binding) as h:
            h.runner.default_script = _awaiting_routed_script("Ship it", "managers")
            await h.kernel.process_event(
                _qevent("ship?", thread="th-self-routed", endpoint=_CLI_STUB)
            )

            channel, _message, _requested_by, thread_ts, endpoint = h.sink.posts[0]
            assert channel == "C1"
            assert thread_ts == "th-self-routed"
            assert endpoint == _CLI_STUB

    asyncio.run(go())


# --- Expired-approval card teardown (#419) ------------------------------------


def _resume_turn(
    text: str, *, thread: str, approval_id: str, author: str
) -> QueuedTurn:
    """The API's approval resume turn: the deterministic ``approval-<id>-resolved``
    event id both the resolve and expiry paths stamp, replayed into the same
    placeholder. The expiry path authors it as "system"; a resolve names the
    resolver."""

    return QueuedTurn(
        event_id=f"approval-{approval_id}-resolved",
        conversation_id=thread,
        author=author,
        text=text,
        reply_handle=ReplyHandle(channel="C1", placeholder="p-1", endpoint=None),
        received_at="2026-07-14T00:00:00+00:00",
    )


def test_expiry_resume_disables_the_approval_card(make_harness) -> None:
    """#419: an EXPIRED approval's resume turn (author "system", enqueued by the
    #412 sweeper or a past-SLA resolve attempt) disables the live card in place --
    buttons gone, an expiry line in their stead -- mirroring the resolved-card
    edit, since no click will ever arrive to do it."""

    async def go() -> None:
        approvals = RecordingApprovals()
        thread = "th-expire-card"
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Give ACME a 20% discount")
            await h.kernel.process_event(_qevent("please discount", thread=thread))

            # The live card was posted and its location remembered, because an
            # expiry (unlike a resolve) carries no click to locate the card.
            assert len(h.sink.posts) == 1
            assert await h.async_redis.exists(h.config.approval_card_key(thread))
            card_ts = "posted-1"  # the FakeSink's returned ts for the first post

            # The expiry resume turn the sweeper enqueues (author "system").
            h.runner.default_script = [Final(text="Acknowledged the expiry.", status=DONE)]
            await h.kernel.process_event(
                _resume_turn(
                    "[approval expired] not approved in time",
                    thread=thread,
                    approval_id="appr-1",
                    author="system",
                )
            )

            # The card was edited in place: same ts, and a channel-neutral message
            # carrying the remembered summary. The buttonless expired-card render
            # (no actions block, an expiry line) is the adapter's job below the
            # seam -- asserted in test_slack_sink.py.
            assert len(h.sink.card_updates) == 1
            channel, ts, message, endpoint, settled = h.sink.card_updates[0]
            # Expiry means nobody decided, so the settled outcome carries no
            # decision and the adapter renders the expired form (#1084).
            assert settled is not None and settled.decision is None
            assert (channel, ts) == ("C1", card_ts)
            assert endpoint is None
            assert message.text == "Give ACME a 20% discount"

            # The memory was consumed (GETDEL), so a redelivery no-ops.
            assert not await h.async_redis.exists(h.config.approval_card_key(thread))

            # The continuation still streamed into the placeholder.
            assert h.sink.last_text == "Acknowledged the expiry."

    asyncio.run(go())


def test_resolve_resume_stamps_the_card_from_the_record(make_harness) -> None:
    """#1084: a RESOLVE resume settles the card, it no longer leaves it live.

    This asserted the opposite until #1084, on the premise that "the dispatcher
    already did from the click". That holds only when there WAS a click: a
    resolution through ``POST /approvals/{id}/resolve`` or ``curie <tier>
    approvals --resolve`` never touches Slack, so the card kept its buttons and
    every later click earned a 409. The worker is the only component that still
    knows where the card is, so settling it belongs here.

    The verdict comes from the durable record, not from the platform-authored
    resume prose -- that sentence is written for a model, and rebuilding a
    decision out of it by regex is how the card would start lying after a
    wording change.
    """

    async def go() -> None:
        reader = RecordingReader(
            SettledApproval(
                status="approved", resolved_by="U9", resolution_note="approved for Q3"
            )
        )
        thread = "th-resolve-card"
        async with make_harness(approvals=RecordingApprovals(), approval_reader=reader) as h:
            h.runner.default_script = _awaiting_script("Refund order 42")
            await h.kernel.process_event(_qevent("refund?", thread=thread))
            assert await h.async_redis.exists(h.config.approval_card_key(thread))

            h.runner.default_script = [Final(text="Refunded.", status=DONE)]
            await h.kernel.process_event(
                _resume_turn(
                    "[approval resolved] approved by U9",
                    thread=thread,
                    approval_id="appr-1",
                    author="U9",
                )
            )

            # The card was settled, with the outcome read off the record.
            assert len(h.sink.card_updates) == 1
            _channel, _ts, message, _endpoint, settled = h.sink.card_updates[0]
            assert message.text == "Refund order 42", "the summary must survive"
            assert settled is not None
            assert settled.decision == "approved"
            assert settled.resolver == "U9"
            assert settled.note == "approved for Q3"
            # And the requester the live card named is carried into the rebuild,
            # so the settled card is not missing a line the original had.
            # "U1" is the author `_qevent` stamps on the triggering turn, which
            # is exactly what `approval_card` rendered as "Requested by".
            assert settled.requested_by == "U1"

            # The read was keyed off the resume turn's deterministic event id.
            assert reader.reads == ["appr-1"]

            # The memory is still consumed, so a later approval cannot collide.
            assert not await h.async_redis.exists(h.config.approval_card_key(thread))

    asyncio.run(go())


def test_a_resolve_resume_leaves_the_card_alone_when_the_record_cannot_be_read(
    make_harness,
) -> None:
    """No record, no stamp (#1084).

    The kernel would have to invent a decision to render anything, and a card
    stating a verdict nobody confirmed is worse than one still showing buttons:
    the buttons are at least honest about the platform not having told Slack
    yet, and the next click gets the real answer from the API.
    """

    async def go() -> None:
        reader = RecordingReader(None)
        thread = "th-unreadable-record"
        async with make_harness(approvals=RecordingApprovals(), approval_reader=reader) as h:
            h.runner.default_script = _awaiting_script("Refund order 42")
            await h.kernel.process_event(_qevent("refund?", thread=thread))

            h.runner.default_script = [Final(text="Refunded.", status=DONE)]
            await h.kernel.process_event(
                _resume_turn(
                    "[approval resolved] approved by U9",
                    thread=thread,
                    approval_id="appr-1",
                    author="U9",
                )
            )

            assert h.sink.card_updates == []
            # The memory is consumed either way: a ref left behind would be
            # popped by an unrelated later approval on the same thread.
            assert not await h.async_redis.exists(h.config.approval_card_key(thread))

    asyncio.run(go())


def test_a_resolve_resume_with_no_reader_configured_still_resumes(make_harness) -> None:
    """Progressive enhancement, per ADR-0020: a deployment with nothing to read
    the record with settles no card and continues the run normally. The stamp is
    an enrichment on a decision that already happened."""

    async def go() -> None:
        thread = "th-no-reader"
        async with make_harness(approvals=RecordingApprovals()) as h:
            h.runner.default_script = _awaiting_script("Refund order 42")
            await h.kernel.process_event(_qevent("refund?", thread=thread))

            h.runner.default_script = [Final(text="Refunded.", status=DONE)]
            await h.kernel.process_event(
                _resume_turn(
                    "[approval resolved] approved by U9",
                    thread=thread,
                    approval_id="appr-1",
                    author="U9",
                )
            )

            assert h.sink.card_updates == []
            # The run itself continued: the resumed reply was delivered.
            assert h.sink.updates, "the resume must still produce a reply"

    asyncio.run(go())


def test_resolve_authored_by_a_system_named_actor_does_not_expire_the_card(
    make_harness,
) -> None:
    """#419 hardening: the expiry-vs-resolve discriminator is the platform text
    marker, NOT the author. A resolver whose identity is literally "system" (the
    codebase's reserved machine-actor name) must not get its RESOLVED card wrongly
    stamped expired -- the ``[approval resolved]`` text keeps it off the expiry
    path."""

    async def go() -> None:
        approvals = RecordingApprovals()
        thread = "th-system-resolver"
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Refund order 42")
            await h.kernel.process_event(_qevent("refund?", thread=thread))
            assert await h.async_redis.exists(h.config.approval_card_key(thread))

            h.runner.default_script = [Final(text="Refunded.", status=DONE)]
            await h.kernel.process_event(
                _resume_turn(
                    "[approval resolved] approved by system",
                    thread=thread,
                    approval_id="appr-1",
                    author="system",  # a resolver literally named "system"
                )
            )

            # Author is "system" but the text says RESOLVED: the card is left to
            # the dispatcher, never stamped expired by the worker.
            assert h.sink.card_updates == []
            assert not await h.async_redis.exists(h.config.approval_card_key(thread))

    asyncio.run(go())


# --- Best-effort resume reply when the CLI stub endpoint is dead (#708) --------


def test_resume_reply_best_effort_completes_offline_when_endpoint_is_dead(
    make_harness,
) -> None:
    """AC-708-1/2 (#708, PRIMARY): a resolved approval's resume turn whose per-turn
    reply endpoint is the now-dead CLI stub, delivered on a worker with NO distinct
    default transport (the pure-offline local loop), must still COMPLETE -- the
    granted tool executes exactly once and the turn reaches terminal ACK -- instead
    of dead-lettering because the reply cannot be delivered.

    Today the reply-delivery ``update`` raises the aiohttp transport error
    ``_with_transport_fallback`` re-raises when there is no distinct default (#530
    only rescues the has-default case), so ``process_event`` escapes; the consumer
    then leaves the entry pending, redelivers it to the delivery cap, and
    dead-letters it (#505) -- a full re-run per redelivery and the resolved approval
    never completes. The done marker is the proof it did NOT: it is written only
    once the turn is terminally handled (the consumer then acks it).

    The fix makes a resume turn's reply best-effort: the kernel gates the new
    ``best_effort_unreachable`` flag on ``_is_approval_resume(event_id)`` for the
    reply-delivery ``update`` calls (streaming edits + final reply). The resume
    ``event_id`` shape (``approval-<uuid>-resolved``) is authored by
    ``resumequeue.resume_event_id`` -- the format authority the worker recognizer
    keys off across the api/worker seam.
    """

    async def go() -> None:
        from curie_api.resumequeue import resume_event_id

        grant_event = resume_event_id(uuid.uuid4())
        binding = GrantBinding(
            grant_event_id=grant_event, grant_tool="mcp__github__create_issue"
        )
        async with make_harness(binding=binding) as h:
            # Offline local loop: the reply endpoint (the CLI stub) is dead, and the
            # worker sink has NO distinct default transport to fall back to.
            h.sink.dead_endpoints.add(_CLI_STUB)

            # The granted tool runs to done in the runner during the resume turn.
            h.runner.default_script = [Final(text="Issue created.", status=DONE)]
            resume_turn = QueuedTurn(
                event_id=grant_event,
                conversation_id="th-offline-resume",
                author="U9",
                text="[approval resolved] approved by U9",
                reply_handle=ReplyHandle(
                    channel="C1", placeholder="p-1", endpoint=_CLI_STUB
                ),
                received_at="2026-07-14T00:00:00+00:00",
            )

            # Must NOT raise: a dead reply endpoint on a resume turn no longer
            # dead-letters the resolved approval.
            await h.kernel.process_event(resume_turn)

            # Terminal ACK, not dead-letter.
            assert await h.async_redis.exists(h.config.done_key(grant_event))

            # The granted tool executed exactly once: the resume turn opened a
            # single runner turn, and that claim carried the one-shot #430 grant.
            assert h.runner.opened == ["[approval resolved] approved by U9"]
            resumed_env = h.fake_k8s.claim_envs[-1]
            assert resumed_env is not None
            assert (
                resumed_env.get("CURIE_APPROVAL_GRANT_TOOL")
                == "mcp__github__create_issue"
            )

    asyncio.run(go())


def test_normal_turn_reply_stays_loud_when_endpoint_is_dead(make_harness) -> None:
    """AC-708-4 (#708): the best-effort swallow is scoped to resume turns. A NORMAL
    (non ``approval-<uuid>-resolved``) turn hitting the same dead endpoint + no
    distinct default must STILL fail loudly -- a fresh local turn whose stub crashed
    mid-turn is a genuine failure that must surface, not silently complete. Extends
    ``test_no_fallback_when_no_default_is_configured``'s intent to the kernel.

    The transport error propagates out of ``process_event`` (leaving the entry
    pending for reclaim), so the turn is NOT marked done -- the inverse of the
    resume case above."""

    async def go() -> None:
        async with make_harness() as h:  # no binding -> a plain, non-resume turn
            h.sink.dead_endpoints.add(_CLI_STUB)
            h.runner.default_script = [Final(text="done", status=DONE)]
            ev = _qevent(
                "hello",
                thread="th-normal-dead",
                event_id="ev-normal-1",  # not the resume shape
                endpoint=_CLI_STUB,
            )

            with pytest.raises(aiohttp.ClientError):
                await h.kernel.process_event(ev)

            # Not silently completed: no done marker was written.
            assert not await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())
