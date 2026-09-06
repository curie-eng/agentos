"""LIVE smoke against a real claude-agent-sdk session.

The Anthropic-path tests run only when a real credential is present
(``CLAUDE_CODE_OAUTH_TOKEN`` or ``ANTHROPIC_API_KEY``). Without one, those tests
are skipped and reported as such -- the suite never fabricates a live result.
Mirrors the PT-2 proofs: a trivial message is answered, a mid-run steer changes
course, and turn 2 shows a warm prompt cache
(``cache_read_input_tokens > 0``).
A third live test covers the OpenRouter path, gated on ``OPENROUTER_API_KEY``.
"""

import logging
import os

import anyio
import pytest
from aci_protocol import Event, SessionStatus, parse_ndjson
from curie_runner import RunTracer, SideEffectClassifier, build_options
from curie_runner.adapter import ClaudeAgentSession
from curie_runner.approval import (
    APPROVAL_SERVER_NAME,
    PUBLISH_TOOL_NAME,
    ApprovalGate,
    build_approval_gate,
    build_approval_hook,
    build_approval_server,
    build_can_use_tool,
)
from curie_runner.session import SessionRunner

_HAS_CRED = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))
_OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")


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


# --- the managed-workspace publication gate on a real model (#2294) --------------
#
# Both tests mount the in-process ``curie`` MCP server carrying
# ``publish_changes`` and ask the model to call it. They differ ONLY in whether
# the two SDK-level gate layers are wired, which is exactly the difference
# between the production shape and the live run that lost its approval record.


def _live_model_and_env() -> tuple[str | None, dict[str, str] | None]:
    """The model/env pair for a live run, preferring the OpenRouter path.

    Mirrors ``test_live_openrouter_cache_reuse``: the OpenRouter key is fed
    through ``resolve_model_credential`` into the SDK ``env`` exactly as the
    runner does in production. With only an Anthropic credential present, the
    ambient credential and default model are used instead.
    """

    from curie_runner.sdk_auth import CREDENTIALS_ENV, resolve_model_credential

    if _OPENROUTER_KEY:
        env: dict[str, str] = {CREDENTIALS_ENV: _OPENROUTER_KEY}
        resolve_model_credential(env)
        return os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4.5"), env
    return None, None


_PUBLISH_PROMPT = (
    "You are working in a git checkout and the change is finished. Call the"
    " mcp__curie__publish_changes tool exactly once with title 'Live gate check'"
    " and a one-sentence body, then end your turn and say the request is pending."
)

_PUBLISH_SYSTEM_PROMPT = (
    "You are a terse test agent. When asked to publish changes, use the"
    " mcp__curie__publish_changes tool. Do not use any other tool."
)


def _publish_runner(trace_name: str, *, gated: bool) -> tuple[SessionRunner, ApprovalGate]:
    gate = build_approval_gate(operator_tools=None, policy_routes={}, managed_workspace=True)
    assert gate is not None
    model, env = _live_model_and_env()
    options = build_options(
        plugins=[],
        model=model,
        system_prompt=_PUBLISH_SYSTEM_PROMPT,
        max_turns=6,
        max_budget_usd=1.0,
        resume=None,
        env=env,
        mcp_servers={
            APPROVAL_SERVER_NAME: build_approval_server(gate, managed_workspace=True)
        },
        # The production shape wires both SDK gate layers; the second test omits
        # them so the tool body actually executes (permission_mode falls back to
        # bypassPermissions), which is the live shape that lost its record.
        hooks=build_approval_hook(gate) if gated else None,
        can_use_tool=build_can_use_tool(gate) if gated else None,
    )
    runner = SessionRunner(
        session_factory=lambda: ClaudeAgentSession(options),
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name=trace_name,
        session_id=trace_name,
        approval_gate=gate,
    )
    return runner, gate


def _live_publish_lines(runner: SessionRunner) -> list[str]:
    async def go() -> list[str]:
        await runner.start()
        try:
            return [
                line
                async for line in runner.run_turn(
                    Event(type="message", text=_PUBLISH_PROMPT, user="U-live", ts="1.0")
                )
            ]
        finally:
            await runner.close()

    return anyio.run(go)


@pytest.mark.skipif(
    not (_HAS_CRED or _OPENROUTER_KEY),
    reason="no live credential (CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY / OPENROUTER_API_KEY)",
)
def test_live_publish_with_both_gate_layers_pauses_awaiting_approval(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """L1, the production shape: hook + can_use_tool wired.

    The publish call is denied before execution, recorded once, and the turn ends
    awaiting-approval carrying the trusted permission-gate provenance the worker
    keys the publication path on.
    """

    runner, gate = _publish_runner("live-publish-gated", gated=True)

    with caplog.at_level(logging.WARNING, logger="curie_runner.session"):
        events = parse_ndjson("".join(_live_publish_lines(runner)))

    final = events[-1]
    assert final.type == "final"
    assert final.status is SessionStatus.AWAITING_APPROVAL
    assert final.approval_summary is not None
    assert PUBLISH_TOOL_NAME in final.approval_summary
    assert final.approval_gate_kind == "permission"
    assert final.approval_granted_tool == PUBLISH_TOOL_NAME
    assert gate.publication_title
    # A gate layer denied the call and asked the CLI to stop the turn.
    assert gate.pending_halt is True
    # And therefore no layer missed it. The real SDK streams the ToolUseBlock
    # BEFORE dispatching the PreToolUse hook, so the stream observer writes the
    # record first even here -- "who wrote it first" must NOT be what the warning
    # keys on, or it fires on the fully-gated production path (observed live).
    assert not any(
        "fallback" in record.getMessage().lower() for record in caplog.records
    ), caplog.text


@pytest.mark.skipif(
    not (_HAS_CRED or _OPENROUTER_KEY),
    reason="no live credential (CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY / OPENROUTER_API_KEY)",
)
def test_live_publish_without_gate_layers_still_pauses_via_the_stream(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """L2, the observed failing shape (#2294): neither gate layer is wired.

    With no hook and no permission callback the session runs bypassPermissions,
    so the in-process tool body executes and returns its defensive ``is_error``
    and the turn ends clean. The runner-owned stream observer is then the only
    thing that can record the pending approval -- before #2294 this finalized
    DONE with nothing to approve.
    """

    runner, gate = _publish_runner("live-publish-ungated", gated=False)

    with caplog.at_level(logging.WARNING, logger="curie_runner.session"):
        events = parse_ndjson("".join(_live_publish_lines(runner)))

    final = events[-1]
    assert final.type == "final"
    assert final.status is SessionStatus.AWAITING_APPROVAL
    assert final.status is not SessionStatus.DONE
    assert final.approval_summary is not None
    assert PUBLISH_TOOL_NAME in final.approval_summary
    assert final.approval_gate_kind == "permission"
    assert final.approval_granted_tool == PUBLISH_TOOL_NAME
    assert gate.publication_title
    # Nothing in this path may claim a halt the runner never requested -- and
    # that absence is precisely what proves neither gate layer ever denied it.
    assert gate.pending_halt is False
    # So the operator-visible warning MUST fire here: a layer that was supposed
    # to decide did not.
    assert any(
        "publication" in record.getMessage().lower()
        and "fallback" in record.getMessage().lower()
        for record in caplog.records
    ), caplog.text
