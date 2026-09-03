"""LIVE smoke against a real claude-agent-sdk session.

The Anthropic-path tests run only when a real credential is present
(``CLAUDE_CODE_OAUTH_TOKEN`` or ``ANTHROPIC_API_KEY``). Without one, those tests
are skipped and reported as such -- the suite never fabricates a live result.
Mirrors the PT-2 proofs: a trivial message is answered, a mid-run steer changes
course, and turn 2 shows a warm prompt cache
(``cache_read_input_tokens > 0``).
A third live test covers the OpenRouter path, gated on ``OPENROUTER_API_KEY``.
"""

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import pytest
from aci_protocol import Event, Final, SessionStatus, parse_ndjson, parse_ndjson_line
from curie_runner import RunTracer, SideEffectClassifier, build_options
from curie_runner.adapter import (
    ClaudeAgentSession,
    build_structured_resume,
    model_message_to_conversation,
)
from curie_runner.history import (
    ConversationMessage,
    ConversationReplay,
    HarnessReplayState,
    TurnRecord,
    build_conversation_replay,
)
from curie_runner.session import SessionRunner

_HAS_CRED = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))
_OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
_LIVE_REQUESTED = os.environ.get("CURIE_E2E_LIVE") == "1"

_WORKSPACE_REQUIRED_TOOLS = frozenset(
    {"Read", "Edit", "Bash", "mcp__curie__publish_changes"}
)


class _LiveTranscriptStore:
    def __init__(self) -> None:
        self.records: list[TurnRecord] = []

    async def load(self) -> list[TurnRecord]:
        return list(self.records)

    async def append(self, record: TurnRecord) -> None:
        self.records.append(record)


def _production_sre_source() -> Path:
    return Path(__file__).parents[2] / "examples" / "sre-bot"


def _production_sre_bundle(tmp_path: Path) -> Path:
    source = _production_sre_source()
    bundle = tmp_path / "sre-bot"
    (bundle / ".claude-plugin").mkdir(parents=True)
    (bundle / "skills" / "sre-bot").mkdir(parents=True)
    (bundle / ".claude-plugin" / "plugin.json").write_text(
        (source / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (bundle / "skills" / "sre-bot" / "SKILL.md").write_text(
        (source / "skills" / "sre-bot" / "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    # The checked-in example's connector build declarations require the
    # deploy-generated connectors.lock.yaml. This runner fixture preserves the
    # exact SRE manifest, skill, toolPolicy, and approvalPolicy while keeping
    # the policy's direct connector namespace. Non-build URL connectors need no
    # generated lock file. Loopback port 9 refuses promptly if probed.
    (bundle / "connectors.yaml").write_text(
        "connectors:\n"
        "  kubernetes:\n"
        "    url: http://127.0.0.1:9/kubernetes\n"
        "  self-upgrade:\n"
        "    url: http://127.0.0.1:9/self-upgrade\n",
        encoding="utf-8",
    )
    manifest = json.loads(
        (bundle / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == "sre-bot"
    assert manifest["toolPolicy"]["enforcement"] == "curie/mcp-tool-policy@1"
    assert manifest["approvalPolicy"]["gates"]
    return bundle


def _assert_production_workspace_init(
    init: dict[str, Any], *, bundle: Path
) -> None:
    """Require the real mounted catalogue, not an options-only substitute."""

    source = _production_sre_source()
    manifest = bundle / ".claude-plugin" / "plugin.json"
    skill = bundle / "skills" / "sre-bot" / "SKILL.md"
    assert (
        manifest.is_file()
        and skill.is_file()
        and manifest.read_text(encoding="utf-8")
        == (source / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        and skill.read_text(encoding="utf-8")
        == (source / "skills" / "sre-bot" / "SKILL.md").read_text(encoding="utf-8")
        and (bundle / "connectors.yaml").is_file()
        and not (bundle / ".mcp.json").exists()
    ), (
        "generic or plugin-less bundle cannot satisfy SRE workspace acceptance"
    )
    raw_tools = init.get("tools")
    assert isinstance(raw_tools, list), "SDK init carried no concrete tools catalogue"
    tools = {str(tool) for tool in raw_tools}
    missing = sorted(_WORKSPACE_REQUIRED_TOOLS - tools)
    assert not missing, f"mounted SDK init catalogue missing required tools: {missing}"
    assert init.get("cwd") == "/workspace", (
        f"mounted SDK init cwd was {init.get('cwd')!r}, expected '/workspace'"
    )


def _catalog_config(bundle: Path, session_id: str):
    from curie_runner.config import RunnerConfig

    env = {
        "CURIE_PLUGIN_DIR": str(bundle),
        "CURIE_SESSION_ID": session_id,
        "CURIE_SANDBOX_ID": f"sandbox-{session_id}",
        "CURIE_BUDGET": (
            '{"max_output_tokens_per_run": 10000, "max_usd_per_day": 1.0}'
        ),
    }
    if model := os.environ.get("CURIE_MODEL"):
        env["CURIE_MODEL"] = model
    return RunnerConfig.from_env(env)


def _install_init_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict[str, Any]]:
    from claude_agent_sdk import SystemMessage
    from curie_runner import __main__ as boot

    init_messages: list[dict[str, Any]] = []

    class InitObservingSession(ClaudeAgentSession):
        def receive_turn(self):
            upstream = super().receive_turn()

            async def observe():
                async for message in upstream:
                    # claude-agent-sdk 0.2.135 message_parser.py preserves the
                    # CLI init frame as SystemMessage(subtype="init"). The
                    # catalogue and cwd are observed SDK output, not inferred
                    # from ClaudeAgentOptions.
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        init_messages.append(dict(message.data))
                    yield message

            return observe()

    monkeypatch.setattr(boot, "ClaudeAgentSession", InitObservingSession)
    return init_messages


def _drive_live_catalog(runner: SessionRunner) -> Final:
    async def go() -> Final:
        await runner.start()
        final: Final | None = None
        try:
            async for line in runner.run_turn(
                Event(
                    type="message",
                    text="Reply with only: workspace-ready. Do not call any tool.",
                    user="U0EXAMPLE",
                    ts="1",
                )
            ):
                parsed = parse_ndjson_line(line)
                if isinstance(parsed, Final):
                    final = parsed
        finally:
            await runner.close()
        assert final is not None, "real SDK turn emitted no terminal Final"
        return final

    return anyio.run(go)


def test_workspace_catalog_assertion_rejects_missing_and_pluginless_init(
    tmp_path: Path,
) -> None:
    bundle = _production_sre_bundle(tmp_path / "production")
    complete = list(_WORKSPACE_REQUIRED_TOOLS)
    without_bash = [tool for tool in complete if tool != "Bash"]
    with pytest.raises(AssertionError, match="missing required tools.*Bash"):
        _assert_production_workspace_init(
            {"tools": without_bash, "cwd": "/workspace"}, bundle=bundle
        )

    generic = tmp_path / "generic-plugin"
    generic.mkdir()
    with pytest.raises(AssertionError, match="generic or plugin-less bundle"):
        _assert_production_workspace_init(
            {"tools": complete, "cwd": "/workspace"}, bundle=generic
        )


@pytest.mark.skipif(
    not _LIVE_REQUESTED,
    reason="set CURIE_E2E_LIVE=1 for real SDK mounted workspace catalogue evidence",
)
def test_live_claim_time_workspace_init_catalogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from curie_runner import __main__ as boot

    workspace = Path("/workspace")
    assert workspace.is_dir() and (workspace / ".git").exists(), (
        "CURIE_E2E_LIVE=1 workspace catalogue proof requires a mounted "
        "/workspace checkout"
    )
    bundle = _production_sre_bundle(tmp_path)
    init_messages = _install_init_observer(monkeypatch)
    runner = boot.build_runner(
        _catalog_config(bundle, f"claim-{uuid4()}"),
        workspace_path=workspace,
    )

    final = _drive_live_catalog(runner)

    assert final.status is SessionStatus.DONE
    assert init_messages, "real SDK claim-time boot emitted no init frame"
    _assert_production_workspace_init(init_messages[-1], bundle=bundle)


@pytest.mark.skipif(
    not _LIVE_REQUESTED,
    reason="set CURIE_E2E_LIVE=1 for real SDK late workspace catalogue evidence",
)
def test_live_late_workspace_replacement_init_catalogue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from curie_runner import __main__ as boot

    workspace = Path("/workspace")
    assert workspace.is_dir() and (workspace / ".git").exists(), (
        "CURIE_E2E_LIVE=1 workspace catalogue proof requires a mounted "
        "/workspace checkout"
    )
    bundle = _production_sre_bundle(tmp_path)
    session_id = f"late-{uuid4()}"
    config = _catalog_config(bundle, session_id)

    # The first claim is the idle, unmounted standing-by session. It has the
    # same thread identity and production bundle, but its options-only shape is
    # deliberately not accepted as catalogue evidence. The fresh replacement
    # below is the only runner driven through a real SDK init frame.
    boot.build_runner(
        config,
        conversation_replay=ConversationReplay(),
        workspace_path=None,
    )
    init_messages = _install_init_observer(monkeypatch)
    replacement = boot.build_runner(
        config,
        conversation_replay=ConversationReplay(),
        workspace_path=workspace,
    )

    final = _drive_live_catalog(replacement)

    assert final.status is SessionStatus.DONE
    assert init_messages, "fresh late workspace runner emitted no SDK init frame"
    _assert_production_workspace_init(init_messages[-1], bundle=bundle)


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
    reason="set CURIE_E2E_LIVE=1 for provider-side web-search evidence",
)
def test_live_provider_web_search_default_and_bundle_opt_out(tmp_path: Path) -> None:
    """The provider executes default WebSearch; the opt-out removes it.

    Anthropic documents ``web_search`` as a server tool whose result blocks
    arrive in the model response, and the Agent SDK documents ``WebSearch`` as
    the Claude Code built-in name. These are external API facts, not inferred
    from Curie's option mapping:
    https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
    https://github.com/anthropics/claude-agent-sdk-python#using-tools
    """

    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        SystemMessage,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )
    from curie_runner.__main__ import build_runner
    from curie_runner.config import RunnerConfig

    def bundle_options(enabled: bool) -> ClaudeAgentOptions:
        bundle = tmp_path / ("default" if enabled else "opted-out")
        manifest = bundle / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{"name": "acme-web-search"}', encoding="utf-8")
        if not enabled:
            (bundle / "curie.bundle.json").write_text(
                '{"webSearch": false}', encoding="utf-8"
            )
        run_id = str(uuid4())
        config = RunnerConfig.from_env(
            {
                "CURIE_PLUGIN_DIR": str(bundle),
                "CURIE_SESSION_ID": run_id,
                "CURIE_SANDBOX_ID": run_id,
                "CURIE_BUDGET": (
                    '{"max_output_tokens_per_run": 10000, "max_usd_per_day": 1.0}'
                ),
            }
        )
        return build_runner(config)._factory()._options

    async def observe(options: ClaudeAgentOptions, prompt: str) -> dict[str, Any]:
        observed: dict[str, Any] = {
            "catalog": None,
            "tool_ids": {},
            "result_ids": set(),
            "terminal_success": False,
        }
        async with ClaudeSDKClient(options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    observed["catalog"] = "WebSearch" in (message.data.get("tools") or [])
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            observed["tool_ids"][block.name] = block.id
                if isinstance(message, UserMessage):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            observed["result_ids"].add(block.tool_use_id)
                if isinstance(message, ResultMessage):
                    observed["terminal_success"] = not message.is_error
                    break
        return observed

    async def go() -> tuple[dict[str, Any], dict[str, Any]]:
        default = await observe(
            bundle_options(True),
            "Use WebSearch to find Anthropic's official web search tool "
            "documentation, then reply with only: done",
        )
        opted_out = await observe(bundle_options(False), "Reply with only: done")
        return default, opted_out

    default, opted_out = anyio.run(go)

    assert default["catalog"] is True
    assert "WebSearch" in default["tool_ids"]
    assert default["tool_ids"]["WebSearch"] in default["result_ids"]
    assert default["terminal_success"] is True

    assert opted_out["catalog"] is False
    assert "WebSearch" not in opted_out["tool_ids"]
    assert opted_out["terminal_success"] is True


@pytest.mark.skipif(
    not _LIVE_REQUESTED,
    reason="set CURIE_E2E_LIVE=1 for disposable structured-replay provider evidence",
)
def test_live_structured_replay_cache_hit_and_changed_prefix_negative(tmp_path) -> None:
    """Fresh SDK clients hit only for an identical native history checkpoint.

    The provider behavior behind this test was observed with the pinned SDK and
    is recorded with version/output evidence in
    ``docs/spikes/1902-claude-agent-sdk-structured-replay.md``. Portable
    role/content stays authoritative; the matching Claude harness additionally
    persists an opaque checkpoint so the provider's own cache-breakpoint shape
    survives the runner boundary.
    """

    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )

    marker = f"native-cache-marker-{tmp_path.name}"
    source_prompt = f"Use Bash to run exactly `printf '{marker}\\n'`, then report its output."
    system_prompt = (
        "You are a deterministic test agent. Use Bash exactly when asked, then "
        "answer tersely and do not repeat a tool call."
    )

    async def source_checkpoint() -> tuple[
        tuple[ConversationMessage, ...], HarnessReplayState
    ]:
        seeded = build_structured_resume(
            (), curie_session_id="live-cache-prefix-1902", cwd=str(tmp_path)
        )
        options = build_options(
            plugins=[],
            model=None,
            system_prompt=system_prompt,
            max_turns=4,
            max_budget_usd=1.0,
            resume=seeded.resume,
            session_id=seeded.session_id,
            session_store=seeded.session_store,
            cwd=str(tmp_path),
        )
        session = ClaudeAgentSession(options)
        portable: list[ConversationMessage] = [
            ConversationMessage(role="user", content=source_prompt)
        ]
        await session.connect()
        try:
            await session.query(source_prompt)
            async for message in session.receive_turn():
                projected = model_message_to_conversation(message)
                if projected is not None:
                    portable.append(projected)
                if isinstance(message, ResultMessage):
                    break
            checkpoint = await session.export_replay_state()
        finally:
            await session.close()
        assert checkpoint is not None
        assert checkpoint.kind == "checkpoint"
        return tuple(portable), checkpoint

    async def run_checkpoint(
        messages: tuple[ConversationMessage, ...],
        checkpoint: HarnessReplayState,
    ) -> tuple[dict[str, Any], str]:
        resume = build_structured_resume(
            messages,
            curie_session_id="live-cache-prefix-1902",
            cwd=str(tmp_path),
            harness_replay=checkpoint,
        )
        options = build_options(
            plugins=[],
            model=None,
            system_prompt=system_prompt,
            max_turns=2,
            max_budget_usd=1.0,
            resume=resume.resume,
            session_id=resume.session_id,
            session_store=resume.session_store,
            cwd=str(tmp_path),
        )
        async with ClaudeSDKClient(options) as client:
            await client.query(
                "What exact output did the prior Bash call produce? Do not use tools."
            )
            texts: list[str] = []
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    texts.extend(
                        block.text for block in message.content if isinstance(block, TextBlock)
                    )
                if isinstance(message, ResultMessage):
                    usage = message.usage if isinstance(message.usage, dict) else {}
                    return usage, "".join(texts)
        raise AssertionError("live SDK response had no terminal result")

    async def go() -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any]]:
        messages, checkpoint = await source_checkpoint()
        # Negative arm: keep portable recovery identical but change one message
        # in the optional native checkpoint. Exact provider prefix matching must
        # invalidate that layer.
        changed_payload = checkpoint.to_dict()
        changed_one = False
        for entry in changed_payload["entries"]:
            message = entry.get("message")
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                message["content"] = f"changed-prefix {content}"
                changed_one = True
                break
        assert changed_one, "captured SDK checkpoint had no mutable user message"
        changed = HarnessReplayState.from_dict(changed_payload)
        primed, _ = await run_checkpoint(messages, checkpoint)
        identical, recovered = await run_checkpoint(messages, checkpoint)
        different, _ = await run_checkpoint(messages, changed)
        return primed, identical, recovered, different

    primed, identical, recovered, different = anyio.run(go)
    identical_read = int(identical.get("cache_read_input_tokens") or 0)
    different_read = int(different.get("cache_read_input_tokens") or 0)
    identical_create = int(identical.get("cache_creation_input_tokens") or 0)
    different_create = int(different.get("cache_creation_input_tokens") or 0)

    assert marker in recovered
    assert (
        int(primed.get("cache_read_input_tokens") or 0)
        + int(primed.get("cache_creation_input_tokens") or 0)
        > 0
    )
    assert identical_read > 0
    assert different_read < identical_read or different_create > identical_create, (
        "changing the recovered native history did not invalidate its cache layer: "
        f"identical={identical!r}, changed={different!r}"
    )


@pytest.mark.skipif(
    not _LIVE_REQUESTED,
    reason="set CURIE_E2E_LIVE=1 for real SDK approval-catalog evidence",
)
def test_live_mcp_policy_catalog_approval_exact_once_and_cache_observable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real SDK catalog hides refusals while one-shot approval stays visible."""

    from claude_agent_sdk import SystemMessage
    from curie_runner import __main__ as boot
    from curie_runner import session as session_module
    from curie_runner.config import RunnerConfig

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
    fixture = (
        Path(__file__).parent / "fixtures" / "mcp_tool_capability_server.py"
    ).resolve()
    bundle = tmp_path / "approval-catalog"
    (bundle / ".claude-plugin").mkdir(parents=True)
    (bundle / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": "acme-bot",
                "systemPrompt": (
                    "You are a deterministic test agent. When asked to write a test "
                    "marker, call the operations write_approval MCP tool exactly once "
                    "with the requested value, wait for its result, and then stop."
                ),
                "toolPolicy": {
                    "enforcement": "curie/mcp-tool-policy@1",
                    "allow": ["operations/read_allowed"],
                    "approvalRequired": ["operations/write_approval"],
                    "deny": ["operations/write_denied"],
                },
            }
        ),
        encoding="utf-8",
    )
    (bundle / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "operations": {
                        "command": sys.executable,
                        "args": [str(fixture)],
                        "env": {
                            "CURIE_TEST_TOOL_MODE": "policy-catalog",
                            "CURIE_TEST_CALL_MARKER": str(marker_file),
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    prefix = "mcp__plugin_acme-bot_operations__"
    read_allowed = f"{prefix}read_allowed"
    write_approval = f"{prefix}write_approval"
    write_denied = f"{prefix}write_denied"
    write_unmatched = f"{prefix}write_unmatched"
    session_id = str(uuid4())
    store = _LiveTranscriptStore()

    def config_for(*, grant_tool: str | None = None) -> RunnerConfig:
        env = {
            "CURIE_PLUGIN_DIR": str(bundle),
            "CURIE_SESSION_ID": session_id,
            "CURIE_SANDBOX_ID": f"sandbox-{session_id}",
            "CURIE_BUDGET": (
                '{"max_output_tokens_per_run": 10000, "max_usd_per_day": 1.0}'
            ),
        }
        if model := os.environ.get("CURIE_MODEL"):
            env["CURIE_MODEL"] = model
        if grant_tool is not None:
            env["CURIE_APPROVAL_GRANT_TOOL"] = grant_tool
            env["CURIE_APPROVAL_DECISION"] = "approved"
        return RunnerConfig.from_env(env)

    catalogs: list[tuple[str, ...]] = []

    class CatalogObservingSession(ClaudeAgentSession):
        def receive_turn(self):
            upstream = super().receive_turn()

            async def observe():
                async for message in upstream:
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        tools = message.data.get("tools")
                        if isinstance(tools, list):
                            catalogs.append(tuple(str(tool) for tool in tools))
                    yield message

            return observe()

    monkeypatch.setattr(boot, "ClaudeAgentSession", CatalogObservingSession)

    async def drive(runner: SessionRunner, text: str, ts: str) -> Final:
        await runner.start()
        final: Final | None = None
        try:
            async for line in runner.run_turn(
                Event(type="message", text=text, user="U0EXAMPLE", ts=ts)
            ):
                event = parse_ndjson_line(line)
                if isinstance(event, Final):
                    final = event
        finally:
            await runner.close()
        assert final is not None
        return final

    blocked = boot.build_runner(config_for(), history_store=store)
    first = anyio.run(
        drive,
        blocked,
        (
            "Write one test marker by calling write_approval exactly once with "
            "value `approved-once`. Do not call any other tool."
        ),
        "1",
    )
    assert first.status is SessionStatus.AWAITING_APPROVAL
    assert first.approval_summary is not None
    assert write_approval in first.approval_summary
    assert "approved-once" in first.approval_summary
    assert not marker_file.exists()

    # claude-agent-sdk 0.2.135 is pinned in uv.lock. Its types.py:1850-1855
    # specifies that disallowed_tools are removed from model context, and its
    # message_parser.py:281-284 preserves the CLI init frame as
    # SystemMessage(subtype="init"). Inspecting that frame proves the real SDK
    # catalog behavior rather than only asserting Curie's option construction.
    assert catalogs
    initial_catalog = set(catalogs[0])
    assert {read_allowed, write_approval} <= initial_catalog
    assert write_denied not in initial_catalog
    assert write_unmatched not in initial_catalog

    assert len(store.records) == 1
    assert store.records[0].harness_replay is not None
    replay, summary = build_conversation_replay(store.records)
    assert summary is None
    assert replay.messages[-1].role == "user"
    assert replay.messages[-1].content[0]["type"] == "tool_result"

    resumed = boot.build_runner(
        config_for(grant_tool=write_approval),
        conversation_replay=replay,
    )

    async def drive_resumed_turns() -> tuple[Final, Final]:
        await resumed.start()
        finals: list[Final] = []
        try:
            for text, ts in (
                (
                    "The prior write_approval call is approved. Retry it exactly once "
                    "with value `approved-once`, wait for the result, and stop.",
                    "2",
                ),
                (
                    "Write another test marker by calling write_approval exactly once "
                    "with value `duplicate`.",
                    "3",
                ),
            ):
                final: Final | None = None
                async for line in resumed.run_turn(
                    Event(type="message", text=text, user="U0EXAMPLE", ts=ts)
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
    assert marker_file.read_text(encoding="utf-8").splitlines() == [
        "write_approval"
    ]

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

    # The projection at resumed boot must not spend the grant. The first real
    # call consumes it; the same tool on the next turn creates a fresh pause.
    assert duplicate.status is SessionStatus.AWAITING_APPROVAL
    assert duplicate.approval_summary is not None
    assert write_approval in duplicate.approval_summary
    assert "duplicate" in duplicate.approval_summary
    assert marker_file.read_text(encoding="utf-8").splitlines() == [
        "write_approval"
    ]


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
        harness_replay: HarnessReplayState | None = None,
    ):
        resume = build_structured_resume(
            replay,
            curie_session_id="live-approval-thread-1902",
            cwd=str(tmp_path),
            harness_replay=harness_replay,
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
    assert store.records[0].harness_replay is not None
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
    resumed_options = options_for(
        resumed_gate, replay.messages, replay.harness_replay
    )
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
