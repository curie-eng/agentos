"""The SDK adapter seam: a ModelSession protocol and its claude-agent-sdk impl.

The runner owns exactly one long-lived model session per process (one session per
sandbox), which is the source of prompt-cache affinity across turns. The session
is driven in the SDK's **streaming-input mode**: ``query`` pushes a user message
(initial or a mid-run steer), ``receive_turn`` yields the SDK messages for the
current turn until its terminal result, and ``interrupt`` is the native hard stop.
Steering is therefore first-class, not emulated: a ``query`` issued while a turn's
``receive_turn`` iterator is live is incorporated at the next loop boundary.

The protocol is the fake seam: unit tests and the conformance suite supply a
scripted ModelSession, so the model (the only external dependency) is mocked at
this boundary and nothing above it is. ``aci-protocol`` is never mocked.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    SdkPluginConfig,
    ServerToolResultBlock,
    ServerToolUseBlock,
    TaskBudget,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._cli_version import __cli_version__
from claude_agent_sdk._internal.session_store import project_key_for_directory
from claude_agent_sdk.types import (
    CanUseTool,
    McpSdkServerConfig,
    PermissionMode,
    SessionKey,
    SessionStore,
    SessionStoreEntry,
)

from .history import ConversationMessage

_SDK_SESSION_NAMESPACE = uuid.UUID("83efb74f-f09e-4db6-b898-9ed8d7084ba8")


class _SeededSessionStore:
    """Process-local SDK store used only to materialize a portable prefix."""

    def __init__(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        self._key = key
        self._entries = list(entries)

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        if key == self._key:
            self._entries.extend(entries)

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        return list(self._entries) if key == self._key and self._entries else None


@dataclass(frozen=True)
class StructuredResume:
    """Claude SDK options needed to reconstruct one portable prefix."""

    session_id: str
    resume: str | None
    session_store: SessionStore | None
    session_key: SessionKey


def build_structured_resume(
    messages: tuple[ConversationMessage, ...],
    *,
    curie_session_id: str,
    cwd: str | None,
) -> StructuredResume:
    """Materialize portable messages into the SDK's ephemeral resume envelope.

    Only role/content is sourced from durable storage. UUIDs and the local JSONL
    envelope are deterministic adapter details reconstructed on every runner;
    no provider-native transcript is made into Curie's persistence contract.
    """

    session_id = str(uuid.uuid5(_SDK_SESSION_NAMESPACE, curie_session_id))
    key: SessionKey = {
        "project_key": project_key_for_directory(cwd),
        "session_id": session_id,
    }
    if not messages:
        return StructuredResume(
            session_id=session_id,
            resume=None,
            session_store=None,
            session_key=key,
        )

    effective_cwd = str(Path(cwd).resolve()) if cwd is not None else os.getcwd()
    entries: list[SessionStoreEntry] = []
    parent_uuid: str | None = None
    for index, message in enumerate(messages):
        canonical = json.dumps(message.to_dict(), separators=(",", ":"), sort_keys=True)
        entry_uuid = str(uuid.uuid5(uuid.UUID(session_id), f"{index}:{canonical}"))
        entry = cast(
            "SessionStoreEntry",
            {
                "parentUuid": parent_uuid,
                "isSidechain": False,
                "userType": "external",
                "cwd": effective_cwd,
                "sessionId": session_id,
                "version": __cli_version__,
                "gitBranch": "",
                "type": message.role,
                "message": message.to_dict(),
                "uuid": entry_uuid,
                # This is adapter envelope metadata, not conversation time. Keep it
                # stable so separate runners materialize identical local transcripts.
                "timestamp": "1970-01-01T00:00:00.000Z",
            },
        )
        entries.append(entry)
        parent_uuid = entry_uuid
    store = _SeededSessionStore(key, entries)
    return StructuredResume(
        session_id=session_id,
        resume=session_id,
        session_store=cast("SessionStore", store),
        session_key=key,
    )


def _content_block_to_dict(block: object) -> dict[str, Any] | None:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        result: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
        }
        if block.is_error is not None:
            result["is_error"] = block.is_error
        return result
    if isinstance(block, ServerToolUseBlock):
        return {
            "type": "server_tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    if isinstance(block, ServerToolResultBlock):
        return {
            "type": "server_tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
        }
    return None


def model_message_to_conversation(message: object) -> ConversationMessage | None:
    """Project one SDK message into Curie's portable role/content shape."""

    if isinstance(message, UserMessage):
        if isinstance(message.content, str):
            content: str | list[dict[str, Any]] = message.content
        else:
            content = [
                projected
                for block in message.content
                if (projected := _content_block_to_dict(block)) is not None
            ]
        return ConversationMessage(role="user", content=content)
    if isinstance(message, AssistantMessage):
        return ConversationMessage(
            role="assistant",
            content=[
                projected
                for block in message.content
                if (projected := _content_block_to_dict(block)) is not None
            ],
        )
    return None


class ModelSession(Protocol):
    """One long-lived model session the runner drives turn by turn."""

    async def connect(self) -> None:
        """Start the session (spawn/attach the harness), rehydrating if configured."""
        ...

    async def query(self, text: str) -> None:
        """Push a user message into the session (initial turn or mid-run steer)."""
        ...

    def receive_turn(self) -> AsyncIterator[Any]:
        """Yield SDK messages for the current turn, ending at its terminal result."""
        ...

    async def interrupt(self) -> None:
        """Hard-stop the in-flight turn at the next safe boundary."""
        ...

    async def close(self) -> None:
        """Tear down the session."""
        ...


def build_options(
    *,
    plugins: list[SdkPluginConfig],
    model: str | None,
    system_prompt: str | None,
    max_turns: int,
    max_budget_usd: float | None,
    resume: str | None,
    session_id: str | None = None,
    session_store: SessionStore | None = None,
    thinking: dict[str, Any] | None = None,
    task_budget_hint: int | None = None,
    env: dict[str, str] | None = None,
    hooks: dict[str, list[HookMatcher]] | None = None,
    mcp_servers: dict[str, McpSdkServerConfig] | None = None,
    can_use_tool: CanUseTool | None = None,
    cwd: str | None = None,
) -> ClaudeAgentOptions:
    """Assemble ClaudeAgentOptions for the session.

    ``resume`` is the provider-native rehydrate path (ADR-0003,
    stateless-first). For Curie's portable history it names an ephemeral SDK
    session envelope rebuilt by :func:`build_structured_resume`; it never points
    the provider at Curie's durable state URL or assumes surviving local state.

    The three ACI budget fields map to distinct SDK controls: ``max_budget_usd``
    is the daily USD cap enforced natively; ``task_budget_hint`` becomes the SDK
    ``task_budget`` so the model self-paces (ACI section 6b, a soft hint, not a
    ceiling); and the hard per-run output-token ceiling is enforced by the runner
    itself (see budget.py).
    """

    task_budget = TaskBudget(total=task_budget_hint) if task_budget_hint else None
    # The permission posture (#245, ADR-0010): with a can_use_tool callback the
    # session runs in default permission mode and every tool call is decided by
    # the callback (approval-required tools are denied and pause the run; all
    # others are allowed, preserving the pre-gate behavior). Without one there
    # is nothing to decide, so the historical bypassPermissions posture is kept
    # verbatim -- an unconfigured agent sees zero behavior change.
    permission_mode: PermissionMode = "default" if can_use_tool is not None else "bypassPermissions"
    # OMITTED, not defaulted, when the operator set nothing (#1182, ADR-0098).
    # Passing thinking=None would be a value the SDK could act on; leaving the
    # key out is the only way to say "no opinion", which is what an unconfigured
    # install has always said and must keep saying.
    thinking_option: dict[str, Any] = {"thinking": cast("Any", thinking)} if thinking else {}
    cwd_option: dict[str, Any] = {"cwd": cwd} if cwd is not None else {}
    return ClaudeAgentOptions(
        plugins=plugins,
        model=model,
        **thinking_option,
        **cwd_option,
        system_prompt=system_prompt,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        resume=resume,
        session_id=session_id if resume is None else None,
        session_store=session_store,
        task_budget=task_budget,
        permission_mode=permission_mode,
        can_use_tool=can_use_tool,
        env=env or {},
        # In-bundle PreToolUse guardrails from the manifest hooks field (#272).
        # Empty/None means no bundle hooks; the SDK default applies. The event
        # keys are the SDK's HookEvent literals (we emit only "PreToolUse").
        hooks=cast("Any", hooks),
        # In-process platform tools (the approval-request gate, ADR-0010).
        mcp_servers=cast("Any", mcp_servers or {}),
    )


class ClaudeAgentSession:
    """ModelSession backed by a real claude-agent-sdk streaming-input session."""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self._options = options
        self._client = ClaudeSDKClient(options)

    async def connect(self) -> None:
        await self._client.connect()

    async def query(self, text: str) -> None:
        await self._client.query(text)

    def receive_turn(self) -> AsyncIterator[Any]:
        return self._client.receive_response()

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def close(self) -> None:
        await self._client.disconnect()
