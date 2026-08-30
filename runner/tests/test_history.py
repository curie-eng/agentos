"""The conversation-history port: resolution, turn shape, preamble, the state-API
store, and the per-turn append that persists a thread's transcript (#20).

The StateApiTranscriptStore is exercised against a tiny in-memory fake of the
#248 log-shaped state endpoints (GET the key, POST .../append), so load/append
round-trip over real HTTP without the API. The write side is exercised by driving
a real turn through the SessionRunner with the fake model.
"""

import anyio
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from curie_runner.history import (
    HistoryError,
    NullTranscriptStore,
    StateApiTranscriptStore,
    TranscriptStore,
    TurnRecord,
    format_conversation_preamble,
    resolve_history,
)


def _fake_state_app() -> tuple[web.Application, list]:
    """A minimal fake of the state key at /agents/A/state/transcript/t1."""
    log: list = []
    app = web.Application()
    key = "/agents/A/state/transcript/t1"

    async def get_key(request: web.Request) -> web.Response:
        if not log:
            return web.json_response({"detail": "not found"}, status=404)
        return web.json_response(
            {"namespace": "transcript", "key": "t1", "value": list(log), "version": len(log)}
        )

    async def append_key(request: web.Request) -> web.Response:
        body = await request.json()
        log.append(body["item"])
        return web.json_response(
            {"namespace": "transcript", "key": "t1", "value": list(log), "version": len(log)}
        )

    app.router.add_get(key, get_key)
    app.router.add_post(f"{key}/append", append_key)
    return app, log


def test_turn_record_round_trip() -> None:
    rec = TurnRecord(
        user="what changed?", assistant="the deploy bumped v3", ts="2026-07-14T00:00:00+00:00"
    )
    assert TurnRecord.from_dict(rec.to_dict()) == rec


def test_resolve_absent_ref_is_null_store() -> None:
    store = resolve_history(None, {})
    assert isinstance(store, NullTranscriptStore)
    assert anyio.run(store.load) == []
    # Append on the null store is a silent no-op.
    anyio.run(store.append, TurnRecord(user="u", assistant="a"))


def test_resolve_http_ref_is_state_store() -> None:
    store = resolve_history("http://api:8000/agents/A/state/transcript/t1", {})
    assert isinstance(store, StateApiTranscriptStore)


def test_resolve_unsupported_scheme_raises() -> None:
    # An old SDK-resume id (or any non-http ref) is rejected loudly, not silently
    # dropped, so a misconfigured ref fails visibly at boot.
    with pytest.raises(HistoryError):
        resolve_history("sdk-session-abc123", {})
    with pytest.raises(HistoryError):
        resolve_history("s3://bucket/hist", {})


def test_preamble_empty_is_none() -> None:
    assert format_conversation_preamble([]) is None


def test_preamble_includes_user_and_assistant_text_oldest_first() -> None:
    turns = [
        TurnRecord(user="deploy the app", assistant="pushed to dev"),
        TurnRecord(user="and prod?", assistant="promoted to prod"),
    ]
    preamble = format_conversation_preamble(turns)
    assert preamble is not None
    assert "deploy the app" in preamble
    assert "pushed to dev" in preamble
    assert "and prod?" in preamble
    assert "promoted to prod" in preamble
    # Oldest first: the first turn's user text precedes the second turn's.
    assert preamble.index("deploy the app") < preamble.index("and prod?")


# --- preamble windowing (the preamble must be bounded) ---------------------------


def test_preamble_windows_by_max_turns_keeping_the_tail() -> None:
    # A long thread must not render an unbounded preamble: with an explicit small
    # max_turns, only the most-recent turns survive and an elision note flags that
    # earlier turns were dropped.
    turns = [
        TurnRecord(user=f"user-msg-{i}", assistant=f"assistant-msg-{i}") for i in range(50)
    ]
    preamble = format_conversation_preamble(turns, max_turns=5)
    assert preamble is not None
    # The newest turn's content is kept; an old (dropped) turn's is not.
    assert "user-msg-49" in preamble
    assert "user-msg-0" not in preamble
    # The truncation is announced.
    assert "elided" in preamble


def test_preamble_windows_by_max_bytes_keeping_the_tail() -> None:
    # A tiny byte budget caps the rendered size: the oldest turns are dropped, the
    # most-recent kept, and the elision note appears. Driven by an explicit
    # max_bytes so the test does not depend on the default's exact value.
    turns = [TurnRecord(user=f"u{i}", assistant=f"a{i}") for i in range(50)]
    unbounded = format_conversation_preamble(turns, max_turns=None, max_bytes=None)
    assert unbounded is not None
    preamble = format_conversation_preamble(turns, max_bytes=400)
    assert preamble is not None
    assert "elided" in preamble
    # Most-recent kept, oldest dropped.
    assert "u49" in preamble
    assert "u0" not in preamble
    # Truncation actually shrank the output and stayed near the budget.
    assert len(preamble.encode("utf-8")) < len(unbounded.encode("utf-8"))
    assert len(preamble.encode("utf-8")) <= 2 * 400


def test_preamble_short_transcript_is_byte_identical_and_unnoted() -> None:
    # Backward-compat: a small transcript under the defaults renders with NO
    # elision note and is byte-identical to the uncapped (max_turns=None,
    # max_bytes=None) output for the same records.
    turns = [
        TurnRecord(user="deploy the app", assistant="pushed to dev"),
        TurnRecord(user="and prod?", assistant="promoted to prod"),
    ]
    uncapped = format_conversation_preamble(turns, max_turns=None, max_bytes=None)
    defaulted = format_conversation_preamble(turns)
    assert defaulted == uncapped
    assert defaulted is not None
    assert "elided" not in defaulted


def test_state_store_load_empty_is_empty() -> None:
    app, _ = _fake_state_app()

    async def go() -> None:
        async with TestServer(app) as server:
            url = str(server.make_url("/agents/A/state/transcript/t1"))
            store = StateApiTranscriptStore(url, token=None)
            assert await store.load() == []

    anyio.run(go)


def test_state_store_append_then_load_round_trip() -> None:
    app, log = _fake_state_app()

    async def go() -> None:
        async with TestServer(app) as server:
            url = str(server.make_url("/agents/A/state/transcript/t1"))
            store = StateApiTranscriptStore(url, token="k")
            await store.append(
                TurnRecord(user="q1", assistant="a1", ts="2026-07-14T00:00:00+00:00")
            )
            await store.append(TurnRecord(user="q2", assistant="a2"))
            loaded = await store.load()
            assert loaded == [
                TurnRecord(user="q1", assistant="a1", ts="2026-07-14T00:00:00+00:00"),
                TurnRecord(user="q2", assistant="a2"),
            ]
            assert len(log) == 2

    anyio.run(go)


def test_state_store_load_rejects_non_array() -> None:
    app = web.Application()

    async def get_key(_request: web.Request) -> web.Response:
        return web.json_response(
            {"namespace": "transcript", "key": "t1", "value": {"not": "a list"}, "version": 1}
        )

    app.router.add_get("/agents/A/state/transcript/t1", get_key)

    async def go() -> None:
        async with TestServer(app) as server:
            url = str(server.make_url("/agents/A/state/transcript/t1"))
            store = StateApiTranscriptStore(url, token=None)
            with pytest.raises(HistoryError):
                await store.load()

    anyio.run(go)


def _recording_runner(store: TranscriptStore, *, script=None, ceiling: int = 0):
    """A SessionRunner wired to the fake model and a recording transcript store."""
    from curie_runner import RunTracer, SideEffectClassifier
    from curie_runner.fake import FakeModelSession, default_turn
    from curie_runner.session import SessionRunner

    return SessionRunner(
        session_factory=lambda: FakeModelSession(script or default_turn),
        ceiling=ceiling,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="t",
        session_id="sess-hist",
        history_store=store,
    )


class _RecordingStore:
    def __init__(self) -> None:
        self.turns: list[TurnRecord] = []

    async def load(self) -> list[TurnRecord]:
        return list(self.turns)

    async def append(self, record: TurnRecord) -> None:
        self.turns.append(record)


def _run_recording_turn(runner, event):
    """Drive an inbound event through the real runner turn lifecycle."""
    from aci_protocol import Final, parse_ndjson_line

    async def go() -> Final:
        await runner.start()
        lines = [line async for line in runner.run_inbound(event)]
        final = parse_ndjson_line(lines[-1])
        assert isinstance(final, Final)
        return final

    return anyio.run(go)


def test_successful_turn_is_appended_to_the_transcript() -> None:
    from aci_protocol import Event, SessionStatus

    store = _RecordingStore()
    runner = _recording_runner(store)
    final = _run_recording_turn(
        runner, Event(type="message", text="what changed?", user="U", ts="1")
    )

    assert final.status is SessionStatus.DONE
    # The fully delivered DONE final records its reply once, not once per
    # translated frame or once per store retry.
    assert len(store.turns) == 1
    assert store.turns[0].user == "what changed?"
    # default_turn's terminal result text.
    assert store.turns[0].assistant == "all done"
    assert store.turns[0].ts  # a timestamp was stamped


def test_synthetic_done_after_incomplete_turn_is_not_appended_to_the_transcript() -> None:
    # The runner synthesizes DONE when the SDK ends after streamed text but omits
    # its ResultMessage. That closes the wire stream, not an assistant reply, so
    # it must preserve the incomplete-turn history contract.
    from aci_protocol import Event, SessionStatus
    from claude_agent_sdk import AssistantMessage, TextBlock

    store = _RecordingStore()
    final = _run_recording_turn(
        _recording_runner(
            store,
            script=lambda: [
                AssistantMessage(
                    content=[TextBlock(text="partial streamed answer")], model="fake-model"
                )
            ],
        ),
        Event(type="message", text="q", user="U", ts="1"),
    )

    assert final.status is SessionStatus.DONE
    assert store.turns == []


def test_classified_failure_turn_is_not_appended_to_the_transcript() -> None:
    from aci_protocol import Event, SessionStatus
    from claude_agent_sdk import ResultMessage

    def script():
        return [
            ResultMessage(
                subtype="error_during_execution",
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=1,
                session_id="fake-session",
                result="model failed",
                usage=None,
            )
        ]
    store = _RecordingStore()
    final = _run_recording_turn(
        _recording_runner(store, script=script),
        Event(type="message", text="q", user="U", ts="1"),
    )

    assert final.status is SessionStatus.CLASSIFIED_FAILURE
    assert store.turns == []


def test_auth_rejection_turn_is_not_appended_to_the_transcript() -> None:
    from aci_protocol import Event, SessionStatus
    from claude_agent_sdk import AssistantMessage

    store = _RecordingStore()
    final = _run_recording_turn(
        _recording_runner(
            store,
            script=lambda: [
                AssistantMessage(content=[], model="fake-model", error="authentication_failed")
            ],
        ),
        Event(type="message", text="q", user="U", ts="1"),
    )

    assert final.status is SessionStatus.CLASSIFIED_FAILURE
    assert store.turns == []


def test_budget_halted_turn_is_not_appended_to_the_transcript() -> None:
    from aci_protocol import Event, SessionStatus
    from claude_agent_sdk import AssistantMessage, TextBlock

    store = _RecordingStore()
    final = _run_recording_turn(
        _recording_runner(
            store,
            ceiling=1,
            script=lambda: [
                AssistantMessage(
                    content=[TextBlock(text="thinking")],
                    model="fake-model",
                    usage={"output_tokens": 2},
                )
            ],
        ),
        Event(type="message", text="q", user="U", ts="1"),
    )

    assert final.status is SessionStatus.CLASSIFIED_FAILURE
    assert store.turns == []


def test_awaiting_approval_turn_is_not_appended_to_the_transcript() -> None:
    from aci_protocol import Event, SessionStatus
    from curie_runner.fake import approval_turn

    store = _RecordingStore()
    final = _run_recording_turn(
        _recording_runner(store, script=lambda: approval_turn("Approve the action")),
        Event(type="message", text="q", user="U", ts="1"),
    )

    assert final.status is SessionStatus.AWAITING_APPROVAL
    assert store.turns == []


def test_interrupted_turn_is_not_appended_to_the_transcript() -> None:
    # An interrupt reclassifies the terminal result to IDLE_AWAITING_INPUT. Drive
    # that delivered terminal through run_inbound, rather than asserting only the
    # internal state, to pin that it is not a completed assistant reply.
    from aci_protocol import Event, Final, SessionStatus, parse_ndjson_line

    store = _RecordingStore()
    runner = _recording_runner(store)

    async def go() -> Final:
        await runner.start()
        stream = runner.run_inbound(Event(type="message", text="q", user="U", ts="1"))
        await stream.__anext__()  # The first streamed frame makes the turn live.
        await runner.interrupt("user stop")
        lines = [line async for line in stream]
        final = parse_ndjson_line(lines[-1])
        assert isinstance(final, Final)
        return final

    final = anyio.run(go)

    assert final.status is SessionStatus.IDLE_AWAITING_INPUT
    assert store.turns == []


def test_compose_system_prompt_orders_memory_then_conversation_then_base() -> None:
    # Boot delivery (ADR-0029): durable memory leads, then this thread's recovered
    # conversation, then the bundle/env system prompt. Any part may be absent.
    from curie_runner.__main__ import _compose_system_prompt

    assert _compose_system_prompt("BASE", "MEM", "CONV", model=None) == "MEM\n\nCONV\n\nBASE"
    assert _compose_system_prompt("BASE", None, "CONV", model=None) == "CONV\n\nBASE"
    assert _compose_system_prompt("BASE", "MEM", None, model=None) == "MEM\n\nBASE"
    assert _compose_system_prompt(None, None, None, model=None) is None


def test_compose_system_prompt_appends_configured_model() -> None:
    from curie_runner.__main__ import _compose_system_prompt

    assert (
        _compose_system_prompt("BASE", "MEM", "CONV", model="z-ai/glm-5.2")
        == "MEM\n\nCONV\n\nBASE\n\nConfigured model: z-ai/glm-5.2"
    )


def test_build_runner_forwards_configured_model_to_session_prompt(tmp_path) -> None:
    from curie_runner import RunnerConfig
    from curie_runner.__main__ import build_runner

    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.json").write_text(
        '{"name": "modelwiring", "version": "0.1.0", "description": "test"}'
    )
    config = RunnerConfig.from_env(
        {
            "CURIE_PLUGIN_DIR": str(tmp_path),
            "CURIE_SESSION_ID": "s-model",
            "CURIE_SANDBOX_ID": "b-model",
            "CURIE_BUDGET": (
                '{"max_output_tokens_per_run": 1000, "max_usd_per_day": 1.0}'
            ),
            "CURIE_MODEL": "z-ai/glm-5.2",
        }
    )
    runner = build_runner(config, fake_model=False)
    options = runner._factory()._options

    assert options.system_prompt == "Configured model: z-ai/glm-5.2"


def test_record_turn_swallows_store_failure() -> None:
    # A transient store failure must never fail a turn the user already answered.
    from aci_protocol import Event
    from curie_runner import RunTracer, SideEffectClassifier
    from curie_runner.session import SessionRunner
    from curie_runner.translate import TurnState

    class _BoomStore:
        async def load(self) -> list[TurnRecord]:
            return []

        async def append(self, record: TurnRecord) -> None:
            raise HistoryError("state API unavailable")

    runner = SessionRunner(
        session_factory=lambda: None,  # type: ignore[arg-type,return-value]
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="t",
        session_id="s",
        history_store=_BoomStore(),
    )
    state = TurnState()
    state.final_text = "answer"
    # Must not raise.
    anyio.run(lambda: runner._record_turn(Event(type="message", text="q", user="U", ts="1"), state))


def test_successful_turn_survives_transcript_store_failure() -> None:
    # A delivered successful answer remains DONE when its best-effort transcript
    # append fails. This drives the full public inbound path so an early return
    # before append cannot make the regression pass.
    from aci_protocol import Event, SessionStatus

    class _BoomStore:
        def __init__(self) -> None:
            self.append_attempts = 0

        async def load(self) -> list[TurnRecord]:
            return []

        async def append(self, record: TurnRecord) -> None:
            self.append_attempts += 1
            raise HistoryError("state API unavailable")

    store = _BoomStore()
    final = _run_recording_turn(
        _recording_runner(store),
        Event(type="message", text="what changed?", user="U", ts="1"),
    )

    assert final.status is SessionStatus.DONE
    assert store.append_attempts == 1
