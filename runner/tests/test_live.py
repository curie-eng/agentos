"""LIVE smoke against a real claude-agent-sdk session.

The Anthropic-path tests run only when a real credential is present
(``CLAUDE_CODE_OAUTH_TOKEN`` or ``ANTHROPIC_API_KEY``). Without one, those tests
are skipped and reported as such -- the suite never fabricates a live result.
Mirrors the PT-2 proofs: a trivial message is answered, a mid-run steer changes
course, and turn 2 shows a warm prompt cache
(``cache_read_input_tokens > 0``).
A third live test covers the OpenRouter path, gated on ``OPENROUTER_API_KEY``.
"""

import os
import shlex
from typing import Any

import anyio
import pytest
from aci_protocol import Event, Final, SessionStatus, parse_ndjson, parse_ndjson_line
from curie_runner import RunTracer, SideEffectClassifier, build_options
from curie_runner.adapter import (
    ClaudeAgentSession,
    build_structured_resume,
)
from curie_runner.history import (
    ConversationMessage,
    TurnRecord,
    build_conversation_replay,
)
from curie_runner.session import SessionRunner

_HAS_CRED = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))
_OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_LIVE_REQUESTED = os.environ.get("CURIE_E2E_LIVE") == "1"


class _LiveTranscriptStore:
    def __init__(self) -> None:
        self.records: list[TurnRecord] = []

    async def load(self) -> list[TurnRecord]:
        return list(self.records)

    async def append(self, record: TurnRecord) -> None:
        self.records.append(record)


@pytest.mark.skipif(
    not _HAS_CRED,
    reason="no live credential (CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY) in env",
)
def test_live_runner_answers_trivial_message() -> None:
    options = build_options(
        plugins=[], model=None,
        system_prompt="You are a terse test agent.",
        max_turns=2, max_budget_usd=1.0, resume=None,
    )
    runner = SessionRunner(
        session_factory=lambda: ClaudeAgentSession(options),
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="live-smoke",
    )

    lines: list[str] = []

    async def go() -> None:
        await runner.start()
        try:
            async for line in runner.run_turn(
                Event(type="message", text="Reply with the single word: pong", user="U", ts="1")
            ):
                lines.append(line)
        finally:
            await runner.close()

    anyio.run(go)
    events = parse_ndjson("".join(lines))
    assert events[-1].type == "final"
    assert events[-1].status == SessionStatus.DONE


@pytest.mark.skipif(
    not _HAS_CRED,
    reason="no live credential (CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY) in env",
)
def test_live_steer_and_cache_reuse() -> None:
    # Steering + prompt-cache reuse at the SDK level (the PT-2 pattern): a mid-run
    # steer redirects the agent, and turn 2 reads the cache the first turn wrote.
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
    )

    async def go() -> dict:
        out: dict = {}
        opts = ClaudeAgentOptions(
            max_turns=8,
            allowed_tools=["Bash"],
            permission_mode="bypassPermissions",
            system_prompt="You are a test agent. Obey the most recent instruction. " * 40,
        )
        async with ClaudeSDKClient(opts) as client:
            await client.query(
                "Run these Bash commands one at a time: `echo step-1`, then "
                "`echo step-2`, then `echo step-3`."
            )
            seen: list[str] = []
            pushed = False
            usages: list[dict] = []
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, ToolUseBlock):
                            cmd = str(b.input.get("command", ""))
                            seen.append(cmd)
                            if not pushed and "step-1" in cmd:
                                await client.query(
                                    "CHANGE OF PLANS: stop. Run exactly `echo REDIRECTED` and stop."
                                )
                                pushed = True
                        if isinstance(b, TextBlock):
                            pass
                if isinstance(msg, ResultMessage):
                    if isinstance(msg.usage, dict):
                        usages.append(msg.usage)
                    break
            out["redirected"] = any("REDIRECTED" in c for c in seen)

            # Turn 2 reuses the stable system prefix cached on turn 1.
            await client.query("Say `ok`.")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    if isinstance(msg.usage, dict):
                        usages.append(msg.usage)
                    break
            out["turn2_cache_read"] = int(
                (usages[-1] or {}).get("cache_read_input_tokens") or 0
            )
        return out

    result = anyio.run(go)
    assert result["redirected"], "mid-run steer did not change course"
    assert result["turn2_cache_read"] > 0, "no prompt-cache reuse on turn 2"


@pytest.mark.skipif(
    not _OPENROUTER_KEY,
    reason="no OPENROUTER_API_KEY (sk-or-...) in env",
)
def test_live_openrouter_cache_reuse() -> None:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
    from curie_runner.sdk_auth import CREDENTIALS_ENV, resolve_model_credential

    env: dict[str, str] = {CREDENTIALS_ENV: _OPENROUTER_KEY}
    resolve_model_credential(env)
    model = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5")

    async def go() -> dict:
        usages: list[dict] = []
        opts = ClaudeAgentOptions(
            model=model,
            env=env,
            max_turns=2,
            permission_mode="bypassPermissions",
            system_prompt="You are a terse test agent. " * 40,
        )
        async with ClaudeSDKClient(opts) as client:
            await client.query("Reply with the single word: alpha")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    if isinstance(msg.usage, dict):
                        usages.append(msg.usage)
                    break

            await client.query("Reply with the single word: beta")
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    if isinstance(msg.usage, dict):
                        usages.append(msg.usage)
                    break

        return usages[-1] if usages else {}

    usage = anyio.run(go)
    assert int((usage or {}).get("cache_read_input_tokens") or 0) > 0, (
        "no prompt-cache reuse on turn 2 through the OpenRouter path"
    )


@pytest.mark.skipif(
    not _HAS_CRED,
    reason="no live credential (CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY) in env",
)
def test_live_permission_gate_pauses_awaiting_approval() -> None:
    """The #245 acceptance criterion on a real model: a tool configured as
    approval-required is intercepted by can_use_tool (never executed) and the
    turn ends awaiting-approval with the blocked call in the summary."""

    from curie_runner.approval import ApprovalGate, build_can_use_tool

    gate = ApprovalGate(required=frozenset({"Bash"}))
    options = build_options(
        plugins=[],
        model=None,
        system_prompt=(
            "You are a terse test agent. When asked to run a command, use the"
            " Bash tool."
        ),
        max_turns=4,
        max_budget_usd=1.0,
        resume=None,
        can_use_tool=build_can_use_tool(gate),
    )
    runner = SessionRunner(
        session_factory=lambda: ClaudeAgentSession(options),
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="live-permission-gate",
        session_id="live-gate",
        approval_gate=gate,
    )

    async def go() -> list[str]:
        await runner.start()
        lines = [
            line
            async for line in runner.run_turn(
                Event(
                    type="message",
                    text="Run the shell command `echo curie-gate-live` and report its output.",
                    user="U-live",
                    ts="1.0",
                )
            )
        ]
        await runner.close()
        return lines

    lines = anyio.run(go)
    events = [parse_ndjson(line) for line in lines]
    final = events[-1]
    assert final.type == "final"
    assert final.status is SessionStatus.AWAITING_APPROVAL
    assert final.approval_summary is not None
    assert final.approval_summary.startswith("Tool call awaiting approval: Bash")
    # The blocked command never executed and never produced output text
    # claiming it ran; the summary records what WOULD have run.
    assert "echo curie-gate-live" in final.approval_summary


@pytest.mark.skipif(
    not _LIVE_REQUESTED,
    reason="set CURIE_E2E_LIVE=1 for disposable structured-replay provider evidence",
)
def test_live_structured_replay_cache_hit_and_changed_prefix_negative(tmp_path) -> None:
    """Fresh SDK clients hit only for an identical portable history prefix.

    The provider behavior behind this test was observed with the pinned SDK and
    is recorded with version/output evidence in
    ``docs/spikes/1902-claude-agent-sdk-structured-replay.md``. Durable storage
    supplies only role/content; the SDK JSONL envelope is rebuilt per client.
    """

    from claude_agent_sdk import ClaudeSDKClient, ResultMessage

    stable_text = f"stable-prefix-{tmp_path.name} " * 900
    original = (
        ConversationMessage(role="user", content=stable_text),
        ConversationMessage(
            role="assistant",
            content=[{"type": "text", "text": "Stable prefix acknowledged."}],
        ),
    )
    changed = (
        ConversationMessage(role="user", content=f"changed {stable_text}"),
        original[1],
    )

    async def run_prefix(messages: tuple[ConversationMessage, ...]) -> dict[str, Any]:
        resume = build_structured_resume(
            messages,
            curie_session_id="live-cache-prefix-1902",
            cwd=str(tmp_path),
        )
        options = build_options(
            plugins=[],
            model=None,
            system_prompt="Reply tersely and do not use tools.",
            max_turns=2,
            max_budget_usd=1.0,
            resume=resume.resume,
            session_id=resume.session_id,
            session_store=resume.session_store,
            cwd=str(tmp_path),
        )
        async with ClaudeSDKClient(options) as client:
            await client.query("Reply with the single word: observed")
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    return message.usage if isinstance(message.usage, dict) else {}
        raise AssertionError("live SDK response had no terminal result")

    async def go() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        # First call primes the provider cache. The second reconstructs the exact
        # same prefix in a fresh client; the third changes one durable message.
        return (
            await run_prefix(original),
            await run_prefix(original),
            await run_prefix(changed),
        )

    primed, identical, different = anyio.run(go)
    identical_read = int(identical.get("cache_read_input_tokens") or 0)
    different_read = int(different.get("cache_read_input_tokens") or 0)
    identical_create = int(identical.get("cache_creation_input_tokens") or 0)
    different_create = int(different.get("cache_creation_input_tokens") or 0)

    assert (
        int(primed.get("cache_read_input_tokens") or 0)
        + int(primed.get("cache_creation_input_tokens") or 0)
        > 0
    )
    assert identical_read > 0
    assert different_read < identical_read
    assert different_create > identical_create


@pytest.mark.skipif(
    not _LIVE_REQUESTED,
    reason="set CURIE_E2E_LIVE=1 for disposable structured-replay provider evidence",
)
def test_live_cross_runner_approval_exact_once_and_cache_observable(
    tmp_path, monkeypatch
) -> None:
    """A suspended real tool call resumes once, then its one-shot grant expires."""

    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
    from curie_runner import session as session_module
    from curie_runner.approval import (
        ApprovalGate,
        build_approval_hook,
        build_can_use_tool,
    )

    metric_calls: list[tuple[str, float, dict[str, str] | None]] = []
    real_record_metric = session_module.record_metric

    def observe_metric(
        name: str,
        value: float = 1,
        *,
        attributes: dict[str, str] | None = None,
    ) -> None:
        metric_calls.append((name, value, attributes))
        real_record_metric(name, value, attributes=attributes)

    monkeypatch.setattr(session_module, "record_metric", observe_metric)

    marker_file = tmp_path / "approval-executions.txt"
    command = f"printf 'approved-once\\n' >> {shlex.quote(str(marker_file))}"
    system_prompt = (
        "You are a deterministic test agent. When explicitly asked to run a shell "
        "command, call Bash with that exact command once and then stop."
    )
    store = _LiveTranscriptStore()

    def options_for(
        gate: ApprovalGate,
        replay: tuple[ConversationMessage, ...],
    ):
        resume = build_structured_resume(
            replay,
            curie_session_id="live-approval-thread-1902",
            cwd=str(tmp_path),
        )
        return build_options(
            plugins=[],
            model=None,
            system_prompt=system_prompt,
            max_turns=4,
            max_budget_usd=1.0,
            resume=resume.resume,
            session_id=resume.session_id,
            session_store=resume.session_store,
            hooks=build_approval_hook(gate),
            can_use_tool=build_can_use_tool(gate),
            cwd=str(tmp_path),
        )

    async def drive(runner: SessionRunner, text: str) -> Final:
        await runner.start()
        final: Final | None = None
        try:
            async for line in runner.run_turn(
                Event(type="message", text=text, user="U0EXAMPLE", ts="1")
            ):
                event = parse_ndjson_line(line)
                if isinstance(event, Final):
                    final = event
        finally:
            await runner.close()
        assert final is not None
        return final

    blocked_gate = ApprovalGate(required=frozenset({"Bash"}))
    blocked = SessionRunner(
        session_factory=lambda: ClaudeAgentSession(options_for(blocked_gate, ())),
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="live-structured-approval-block",
        session_id="live-approval-thread-1902",
        history_store=store,
        approval_gate=blocked_gate,
    )
    first = anyio.run(drive, blocked, f"Run this exact shell command: {command}")
    assert first.status is SessionStatus.AWAITING_APPROVAL
    assert not marker_file.exists()
    assert len(store.records) == 1

    replay, summary = build_conversation_replay(store.records)
    assert summary is None
    assert replay.messages[-1].role == "user"
    assert replay.messages[-1].content[0]["type"] == "tool_result"

    allowed_commands: list[str] = []
    resumed_gate = ApprovalGate(required=frozenset({"Bash"}), grant_tool="Bash")
    permission_callback = build_can_use_tool(resumed_gate)

    async def observe_permission(tool_name, tool_input, context):
        decision = await permission_callback(tool_name, tool_input, context)
        if isinstance(decision, PermissionResultAllow):
            allowed_commands.append(str(tool_input.get("command") or ""))
        assert isinstance(decision, (PermissionResultAllow, PermissionResultDeny))
        return decision

    resumed_options = options_for(resumed_gate, replay.messages)
    # Production's PreToolUse hook spends the grant. Keep can_use_tool as an
    # observation-only wrapper for any SDK path that reaches it.
    resumed_options.can_use_tool = observe_permission
    resumed = SessionRunner(
        session_factory=lambda: ClaudeAgentSession(resumed_options),
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="live-structured-approval-resume",
        session_id="live-approval-thread-1902",
        approval_gate=resumed_gate,
        approval_decision="approved",
        history_resumed=True,
    )
    async def drive_resumed_turns() -> tuple[Final, Final]:
        await resumed.start()
        finals: list[Final] = []
        try:
            for text in (
                "The prior Bash call is approved. Retry that exact call once now, then stop.",
                "Attempt the same Bash command one more time.",
            ):
                final: Final | None = None
                async for line in resumed.run_turn(
                    Event(type="message", text=text, user="U0EXAMPLE", ts="2")
                ):
                    event = parse_ndjson_line(line)
                    if isinstance(event, Final):
                        final = event
                assert final is not None
                finals.append(final)
        finally:
            await resumed.close()
        return finals[0], finals[1]

    approved, duplicate = anyio.run(drive_resumed_turns)
    assert approved.status is SessionStatus.DONE
    assert marker_file.read_text().splitlines() == ["approved-once"]

    # Same runner, later turn: reset() expires the one-shot grant. Asking for the
    # action again must pause without another write.
    assert duplicate.status is SessionStatus.AWAITING_APPROVAL
    assert marker_file.read_text().splitlines() == ["approved-once"]
    assert allowed_commands == []  # hook allow skips can_use_tool in the pinned SDK

    cache_calls = [
        call for call in metric_calls if call[0] == "curie.history.resume.cache_read"
    ]
    assert len(cache_calls) == 1
    assert cache_calls[0][1] > 0
    assert cache_calls[0][2] == {
        "service.name": "curie-runner",
        "source": "runner",
        "cache_hit": "true",
    }
