"""Kernel-level F2 tests: deployment binding + kill switch, against real Valkey,
the real substrate, and a fake runner. The Postgres resolution SQL is tested
separately (tests/binding); here a stub binding supplies canned resolutions so
the kernel behaviors are exercised deterministically."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable

from aci_protocol import Final, QueuedTurn, ReplyHandle, SessionStatus, TextDelta
from curie_worker.behaviorpacks import BehaviorPacks
from curie_worker.binding import (
    BUDGET_ENV,
    BUNDLE_REF_ENV,
    PLUGIN_DIR_ENV,
    ResolvedDeployment,
)
from curie_worker.killswitch import kill_key

DONE = SessionStatus.DONE
IDLE = SessionStatus.IDLE_AWAITING_INPUT


class StubBinding:
    """A BindingResolver-shaped stub with canned per-channel resolutions."""

    def __init__(self, by_channel: dict[str, ResolvedDeployment]) -> None:
        self._by_channel = by_channel

    async def resolve(self, channel: str) -> ResolvedDeployment | None:
        return self._by_channel.get(channel)

    def boot_env(self, resolved: ResolvedDeployment, thread_key: str) -> dict[str, str]:
        env = {
            BUDGET_ENV: '{"max_output_tokens_per_run":100000,"max_usd_per_day":10.0}',
            PLUGIN_DIR_ENV: "/bundles/current",
        }
        if resolved.bundle_ref is not None:
            env[BUNDLE_REF_ENV] = resolved.bundle_ref
        return env

    def packs_for(self, resolved: ResolvedDeployment) -> BehaviorPacks:
        return BehaviorPacks.from_config(resolved.behavior_packs)


def _resolved(agent_id: uuid.UUID, *, bundle: str | None = "bundles/x.zip") -> ResolvedDeployment:
    return ResolvedDeployment(
        agent_id=agent_id,
        agent_name="test-agent",
        version_id=uuid.uuid4(),
        version_label="v1",
        bundle_ref=bundle,
        max_usd_per_day=None,
        max_output_tokens_per_run=None,
    )


def _qevent(
    text: str, *, channel: str, thread: str = "th-1", placeholder: str = "p-1"
) -> QueuedTurn:
    return QueuedTurn(
        event_id=uuid.uuid4().hex,
        conversation_id=thread,
        author="U1",
        text=text,
        reply_handle=ReplyHandle(channel=channel, placeholder=placeholder),
        received_at="2026-07-05T00:00:00+00:00",
    )


async def _wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def test_unmapped_channel_is_a_polite_drop(make_harness) -> None:
    async def go() -> None:
        async with make_harness(binding=StubBinding({})) as h:
            h.runner.default_script = [Final(text="hi", status=DONE)]
            ev = _qevent("hello", channel="C-unknown")
            await h.kernel.process_event(ev)

            assert h.runner.opened == []  # no turn ever opened
            assert h.sink.last_text is not None and "no agent" in h.sink.last_text.lower()
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_bound_channel_claims_sandbox_with_boot_env(make_harness) -> None:
    async def go() -> None:
        agent_id = uuid.uuid4()
        resolved = _resolved(agent_id, bundle="bundles/x.zip")
        binding = StubBinding({"C-bound": resolved})
        async with make_harness(binding=binding) as h:
            h.runner.default_script = [Final(text="answer", status=DONE)]
            await h.kernel.process_event(_qevent("hi", channel="C-bound", thread="th-1"))

            assert h.runner.opened == ["hi"]
            assert h.sink.last_text == "answer"
            # The sandbox was claimed WITH exactly the resolved boot env
            # (BUNDLE_REF / PLUGIN_DIR / BUDGET), unmodified.
            env = h.fake_k8s.claim_envs[-1]
            assert env == binding.boot_env(resolved, "th-1")
            assert env is not None
            assert env[BUNDLE_REF_ENV] == "bundles/x.zip"
            assert env[PLUGIN_DIR_ENV] == "/bundles/current"
            assert "max_usd_per_day" in env[BUDGET_ENV]

    asyncio.run(go())


def test_killed_agent_refuses_new_runs(make_harness) -> None:
    async def go() -> None:
        agent_id = uuid.uuid4()
        binding = StubBinding({"C-bound": _resolved(agent_id)})
        async with make_harness(binding=binding, with_killswitch=True) as h:
            await h.async_redis.set(kill_key(agent_id), "1")  # operator killed it
            h.runner.default_script = [Final(text="answer", status=DONE)]

            await h.kernel.process_event(_qevent("hi", channel="C-bound"))

            assert h.runner.opened == []  # refused before opening a turn
            assert h.sink.last_text is not None and "paused" in h.sink.last_text.lower()

    asyncio.run(go())


class _StubKillSwitch:
    """Scripted is_killed: returns the sequence in order (last value repeats)."""

    def __init__(self, killed_sequence: list[bool]) -> None:
        self._seq = list(killed_sequence)
        self.calls = 0

    async def is_killed(self, _agent_id: uuid.UUID) -> bool:
        value = self._seq[min(self.calls, len(self._seq) - 1)]
        self.calls += 1
        return value


def test_kill_between_precheck_and_register_is_caught(make_harness) -> None:
    async def go() -> None:
        agent_id = uuid.uuid4()
        binding = StubBinding({"C-bound": _resolved(agent_id)})
        async with make_harness(binding=binding) as h:
            # Precheck sees the agent alive; by the time the turn is registered the
            # kill has landed. The post-register recheck must interrupt it.
            h.kernel.attach_killswitch(_StubKillSwitch([False, True]))
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="working")]
            h.runner.tail = [Final(text="stopped", status=IDLE)]

            await h.kernel.process_event(_qevent("hi", channel="C-bound", thread="tRace"))

            assert h.runner.interrupts == 1  # the just-opened turn was interrupted

    asyncio.run(go())


def test_kill_interrupts_a_live_turn(make_harness) -> None:
    async def go() -> None:
        agent_id = uuid.uuid4()
        binding = StubBinding({"C-bound": _resolved(agent_id)})
        async with make_harness(binding=binding, with_killswitch=True) as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="working")]
            h.runner.tail = [Final(text="stopped", status=IDLE)]

            ev = _qevent("hi", channel="C-bound", thread="tK")
            t1 = asyncio.create_task(h.kernel.process_event(ev))
            await _wait_until(lambda: h.runner.turn_active)

            # Killing the agent interrupts its registered live turn.
            signalled = await h.kernel.interrupt_agent(agent_id)
            assert signalled == 1
            assert h.runner.interrupts == 1

            await t1
            assert h.sink.last_text == "stopped"

    asyncio.run(go())


def _resolved_with_packs(behavior_packs: dict) -> ResolvedDeployment:
    return ResolvedDeployment(
        agent_id=uuid.uuid4(),
        agent_name="test-agent",
        version_id=uuid.uuid4(),
        version_label="v1",
        bundle_ref="bundles/x.zip",
        max_usd_per_day=None,
        max_output_tokens_per_run=None,
        behavior_packs=behavior_packs,
    )


def test_shimmer_caption_uses_the_agents_load_pack(make_harness) -> None:
    # Connector: with shimmer on, the kernel sets the assistant status to the
    # agent's sampled load line (+ tip), not the dispatcher's generic text.
    async def go() -> None:
        packs = {
            "load": {"enabled": True, "lines": ["Crunching the numbers..."]},
            "tips": {"enabled": True, "tips": ["I can rank leaks by $"]},
        }
        binding = StubBinding({"C-bound": _resolved_with_packs(packs)})
        async with make_harness(binding=binding, shimmer=True) as h:
            h.runner.default_script = [Final(text="done", status=DONE)]
            await h.kernel.process_event(_qevent("hi", channel="C-bound", thread="tSh"))
            assert h.sink.status_sets, "expected a shimmer caption to be set"
            _, thread_ts, caption = h.sink.status_sets[-1]
            assert thread_ts == "tSh"
            assert caption == "Crunching the numbers...\n\nTip: I can rank leaks by $"

    asyncio.run(go())


def test_shimmer_no_caption_when_agent_has_no_load_or_tips(make_harness) -> None:
    # Shimmer on but the agent enables neither pack: the kernel sets no caption,
    # so the dispatcher's generic status stays.
    async def go() -> None:
        binding = StubBinding({"C-bound": _resolved_with_packs({})})
        async with make_harness(binding=binding, shimmer=True) as h:
            h.runner.default_script = [Final(text="done", status=DONE)]
            await h.kernel.process_event(_qevent("hi", channel="C-bound"))
            assert h.sink.status_sets == []


def test_shimmer_off_never_sets_a_caption(make_harness) -> None:
    async def go() -> None:
        packs = {"load": {"enabled": True, "lines": ["Working..."]}}
        binding = StubBinding({"C-bound": _resolved_with_packs(packs)})
        # Explicitly OFF: shimmer now defaults ON (#1182), so leaning on the
        # default here would silently stop exercising the off path.
        async with make_harness(binding=binding, shimmer=False) as h:
            h.runner.default_script = [Final(text="done", status=DONE)]
            await h.kernel.process_event(_qevent("hi", channel="C-bound"))
            assert h.sink.status_sets == []

    asyncio.run(go())


# -- nav pack reaches the final rendered reply --------------------------------
# The kernel threads the bound agent's nav pack to the sink's ``update`` for the
# final structured reply, so a bound agent whose nav pack is enabled gets the
# no-dead-ends hub button on its rendered Block Kit. An agent with nav
# disabled/absent gets none.

# A structured reply with buttons, none of which links to the hub command.
_REPLY_WITH_BUTTONS = (
    "```curie-reply\n"
    '{"text": "here you go", "buttons": [["Details", "details"]]}\n'
    "```"
)


def test_bound_agent_with_enabled_nav_gets_hub_button_on_final_reply(make_harness) -> None:
    from curie_worker.behaviorpacks import NavPack
    from curie_worker.blocks import render

    async def go() -> None:
        packs = {"nav": {"enabled": True, "hub_label": "Help", "hub_command": "help"}}
        binding = StubBinding({"C-bound": _resolved_with_packs(packs)})
        async with make_harness(binding=binding) as h:
            h.runner.default_script = [Final(text=_REPLY_WITH_BUTTONS, status=DONE)]
            await h.kernel.process_event(_qevent("hi", channel="C-bound", thread="tNav"))

            # The sink's final update was threaded the agent's enabled nav pack.
            assert h.sink.last_nav == NavPack(
                enabled=True, hub_label="Help", hub_command="help"
            )
            # ...and rendering that final reply with it surfaces the hub button.
            assert h.sink.last_text is not None
            _text, blocks = render(h.sink.last_text, nav=h.sink.last_nav)
            assert blocks is not None
            ids = [
                e["action_id"]
                for b in blocks
                if b["type"] == "actions"
                for e in b["elements"]
            ]
            assert "help" in ids

    asyncio.run(go())


def test_malformed_packs_blob_still_completes_the_turn(make_harness) -> None:
    # Regression: a malformed behavior_packs blob must NOT brick the channel.
    # from_config is total, so packs_for can't raise; process_event resolves an
    # all-off default and the turn finishes normally (no poison-loop). This
    # exercises the every-bound-turn path where packs_for now runs; the shimmer
    # flag is left at its default (now ON, #1182) because the defensiveness under
    # test does not depend on it either way.
    async def go() -> None:
        # A nested field of the wrong type would raise pydantic.ValidationError
        # if from_config were not defensive.
        binding = StubBinding(
            {"C-bound": _resolved_with_packs({"nav": {"enabled": "banana"}})}
        )
        async with make_harness(binding=binding) as h:
            h.runner.default_script = [Final(text="answer", status=DONE)]
            ev = _qevent("hi", channel="C-bound", thread="tBad")

            # No exception escapes (a raise here would leave the event pending and
            # crash-loop on reclaim).
            await h.kernel.process_event(ev)

            # The turn completed: a normal final update landed and the event is done.
            assert h.runner.opened == ["hi"]
            assert h.sink.last_text == "answer"
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_bound_agent_without_nav_gets_no_hub_button(make_harness) -> None:
    from curie_worker.blocks import render

    async def go() -> None:
        binding = StubBinding({"C-bound": _resolved_with_packs({})})  # nav absent
        async with make_harness(binding=binding) as h:
            h.runner.default_script = [Final(text=_REPLY_WITH_BUTTONS, status=DONE)]
            await h.kernel.process_event(_qevent("hi", channel="C-bound", thread="tNoNav"))

            # No enabled nav reached the sink, so no hub button reaches the render.
            assert not (h.sink.last_nav and h.sink.last_nav.enabled)
            assert h.sink.last_text is not None
            _text, blocks = render(h.sink.last_text, nav=h.sink.last_nav)
            assert blocks is not None
            ids = [
                e["action_id"]
                for b in blocks
                if b["type"] == "actions"
                for e in b["elements"]
            ]
            assert "help" not in ids

    asyncio.run(go())


# -- greeting/help pre-model short-circuit (ADR-0018) --------------------------
# The kernel wiring under test (in ``_route_and_start``, under the per-thread
# lock, BEFORE claiming a sandbox): if an ENABLED greeting/help pack matches the
# message text AND the thread is provably fresh (``substrate.lookup(thread) is
# None`` -- no existing route), short-circuit -- deliver the canned reply onto the
# placeholder, mark the event done, and DO NOT claim a sandbox or call start_turn.
# A thread that already has a route is NEVER short-circuited (rule 1: it steers).
#
# Observables used here (mirroring the harness the other kernel tests use):
#   * canned reply delivered   -> h.sink.last_text / h.sink.updates
#   * event marked done        -> done_key exists in Valkey
#   * start_turn NEVER called   -> h.runner.opened == []   (the fake runner records
#                                   every /v1/event, which is start_turn, in .opened)
#   * NO sandbox claimed        -> h.fake_k8s.claim_envs == []  (the real substrate
#                                   creates a claim only via the fake K8s client,
#                                   which records every create in .claim_envs)
#   * steered (live thread)     -> h.runner.steers == [<follow-up text>]
#
# No additive harness change is needed: a FRESH thread has never been claimed, so
# the real substrate's lookup() returns None on its own; a LIVE thread is set up
# exactly as the existing steer test does (a first turn held active via runner.hold),
# which leaves a route in Valkey so lookup() returns a handle.

_GREET = "Hey! I'm your revenue assistant -- ask me anything."
_HELP = "I can rank revenue leaks by $ impact and draft the outreach for you."


def test_fresh_thread_greeting_short_circuits_no_sandbox_no_model(make_harness) -> None:
    # THE HEADLINE COST WIN: a bare "hi" opening a fresh thread is answered from
    # the placeholder with the agent's canned greeting -- no sandbox, no model.
    async def go() -> None:
        packs = {"greeting": {"enabled": True, "phrases": ["hi", "hello"], "reply": _GREET}}
        binding = StubBinding({"C-bound": _resolved_with_packs(packs)})
        async with make_harness(binding=binding) as h:
            # If the short-circuit did NOT fire, the runner would serve this and the
            # turn would complete with "MODEL"; the assertions below would then fail
            # on opened/last_text/claim_envs -- i.e. the feature is missing.
            h.runner.default_script = [Final(text="MODEL", status=DONE)]
            ev = _qevent("hi", channel="C-bound", thread="tGreet")

            await h.kernel.process_event(ev)

            # The canned greeting was delivered onto the placeholder...
            assert h.sink.last_text == _GREET
            assert any(ts == "p-1" and text == _GREET for _c, ts, text in h.sink.updates)
            # ...the event is done...
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))
            # ...and NO model turn was started and NO sandbox was claimed.
            assert h.runner.opened == []
            assert h.fake_k8s.claim_envs == []

    asyncio.run(go())


def test_mid_live_thread_greeting_steers_and_is_not_short_circuited(make_harness) -> None:
    # RULE 1 provoking test: a greeting arriving on a thread that ALREADY has a
    # live turn must STEER into that turn -- the short-circuit must NOT swallow it
    # (that would drop a steer to a live turn). A wiring that matches "hi" before
    # the lookup-is-None gate (e.g. in process_event, as the doc first sketched)
    # would deliver the canned greeting and drop the steer -> this test fails.
    async def go() -> None:
        packs = {"greeting": {"enabled": True, "phrases": ["hi", "hello"], "reply": _GREET}}
        binding = StubBinding({"C-bound": _resolved_with_packs(packs)})
        async with make_harness(binding=binding) as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="working")]
            h.runner.tail = [Final(text="done", status=DONE)]

            # First turn opens and hangs active, leaving a live route on the thread.
            e1 = _qevent("do the thing", channel="C-bound", thread="tLive", placeholder="ph-1")
            t1 = asyncio.create_task(h.kernel.process_event(e1))
            await _wait_until(lambda: h.runner.turn_active)

            # Follow-up greeting on the SAME (now non-fresh) thread.
            e2 = _qevent("hi", channel="C-bound", thread="tLive", placeholder="ph-2")
            await h.kernel.process_event(e2)

            # It steered into the live turn; no second turn was opened.
            assert h.runner.steers == ["hi"]
            assert h.runner.opened == ["do the thing"]
            # The canned greeting was NEVER delivered (the short-circuit did not fire).
            assert all(_GREET not in text for _c, _ts, text in h.sink.updates)
            # The follow-up's placeholder was retired as a fold, not a canned reply.
            folded = [u for u in h.sink.updates if u[1] == "ph-2"]
            assert folded and "folded" in folded[-1][2].lower()

            hold.set()
            await t1

    asyncio.run(go())


def test_fresh_thread_help_short_circuits_no_sandbox_no_model(make_harness) -> None:
    # The help half of the niceties battery: a bare "help" on a fresh thread is
    # answered canned (greeting-then-help chain; greeting absent here, help fires).
    async def go() -> None:
        packs = {"help": {"enabled": True, "phrases": ["help", "what can you do"], "reply": _HELP}}
        binding = StubBinding({"C-bound": _resolved_with_packs(packs)})
        async with make_harness(binding=binding) as h:
            h.runner.default_script = [Final(text="MODEL", status=DONE)]
            ev = _qevent("help", channel="C-bound", thread="tHelp")

            await h.kernel.process_event(ev)

            assert h.sink.last_text == _HELP
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))
            assert h.runner.opened == []
            assert h.fake_k8s.claim_envs == []

    asyncio.run(go())


def test_fresh_thread_non_matching_message_runs_a_normal_turn(make_harness) -> None:
    # The short-circuit fires ONLY on a match: a real request on a fresh thread,
    # even with the greeting pack enabled, claims a sandbox and runs the model.
    async def go() -> None:
        packs = {"greeting": {"enabled": True, "phrases": ["hi", "hello"], "reply": _GREET}}
        binding = StubBinding({"C-bound": _resolved_with_packs(packs)})
        async with make_harness(binding=binding) as h:
            h.runner.default_script = [
                TextDelta(text="Your pipeline "),
                Final(text="Your pipeline is $2.1M.", status=DONE),
            ]
            ev = _qevent("what's my pipeline?", channel="C-bound", thread="tReal")

            await h.kernel.process_event(ev)

            # Normal path: model turn started, sandbox claimed, streamed reply.
            assert h.runner.opened == ["what's my pipeline?"]
            assert h.sink.last_text == "Your pipeline is $2.1M."
            assert len(h.fake_k8s.claim_envs) == 1
            assert h.sink.last_text != _GREET
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_disabled_greeting_pack_never_short_circuits(make_harness) -> None:
    # Opt-in: with the greeting pack DISABLED, even a bare "hi" runs a normal turn
    # (match_greeting returns None for a disabled pack).
    async def go() -> None:
        packs = {"greeting": {"enabled": False, "phrases": ["hi", "hello"], "reply": _GREET}}
        binding = StubBinding({"C-bound": _resolved_with_packs(packs)})
        async with make_harness(binding=binding) as h:
            h.runner.default_script = [Final(text="MODEL", status=DONE)]
            ev = _qevent("hi", channel="C-bound", thread="tOff")

            await h.kernel.process_event(ev)

            assert h.runner.opened == ["hi"]
            assert h.sink.last_text == "MODEL"
            assert len(h.fake_k8s.claim_envs) == 1
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())
