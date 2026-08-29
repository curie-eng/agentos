"""Translate claude-agent-sdk messages into ACI outbound events.

This is the pure mapping at the heart of the runner: it turns each SDK message
(assistant text, tool calls, terminal result, rate-limit signal) into zero or
more ACI outbound events (text_delta / tool_note / side_effect_flag / error /
final). It is stateful only through ``TurnState`` (side-effect dedup, carried
error classification) and side-effect free otherwise, so it is unit-testable
without a session, a network, or the HTTP layer.

Budget and interrupt outcomes are *not* decided here: this layer reports the
model's own terminal status (done vs classified-failure), and the session applies
budget/interrupt overrides on top. Keeping that split is what lets the same
translation serve both the live HTTP turn and the conformance producer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aci_protocol import (
    ErrorEvent,
    Final,
    OutboundEvent,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
    ToolNote,
)
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from .approval import APPROVAL_TOOL_NAME, guard_reserved_summary
from .otel import _GenerationSpan
from .side_effects import SideEffectClassifier


@dataclass
class TurnState:
    """Mutable per-turn state threaded through translation."""

    side_effect_emitted: bool = False
    error_classification: str | None = None
    # The summary passed to the approval-request tool (ADR-0010), captured off
    # the ToolUseBlock so the session can end the turn awaiting-approval. None
    # when no approval was requested this turn.
    approval_summary: str | None = None
    # The approval route the request named (#247): a manifest-declared route
    # the platform binds to a channel per deployment. None routes to the
    # requesting channel.
    approval_route: str | None = None
    # Durable gate provenance (#544, Decision C). ``approval_gate_kind`` is
    # 'policy' when the model asked for a business-decision approval and
    # 'permission' when the runner's tool gate denied a real tool call (merged
    # from the ApprovalGate in the session). ``approval_granted_tool`` is the
    # trusted tool name a permission gate authorizes for the resume turn; a
    # policy gate never carries one (Decision A), so it stays None here.
    approval_gate_kind: str | None = None
    approval_granted_tool: str | None = None
    # Assistant text streamed during the turn, accumulated so a DONE result with
    # an empty ``result`` can still deliver the model's answer. Reasoning models
    # routed through OpenRouter (e.g. z-ai/glm-5.2) emit the answer as a TextBlock
    # but their empty-signature thinking block trips the SDK's result extraction,
    # leaving ``ResultMessage.result`` empty (issue #107).
    assistant_text: str = ""
    # Count of ALL tool calls this turn (every ToolUseBlock), the evidence signal
    # the false-completion check keys on (#517). Distinct from
    # ``side_effect_emitted``, which flips only for non-idempotent tools: a
    # read-only investigation (Read/Grep/WebSearch) IS tool-call evidence but
    # leaves ``side_effect_emitted`` False, so this counter -- not that flag -- is
    # the right "did any tool run" signal.
    tool_call_count: int = 0
    # The delivered text of the terminal ``final`` for a successful turn, set by
    # the session loop when a DONE/idle final is produced. It is the assistant
    # reply recorded into the conversation transcript (#20); left None on a
    # failure/budget/auth final so those turns are not persisted as history.
    final_text: str | None = None
    # Runner-internal, NOT a wire field (#1852): set by
    # ``SessionRunner._merge_gate_block`` when the runner's OWN approval gate
    # asked the CLI to stop the turn. A gated deny now carries the SDK's
    # turn-stopping flags, so the CLI aborts and its terminal result arrives
    # is_error-shaped -- which ``_translate_result`` classifies as a failure.
    # This flag is what lets ``_apply_approval_override`` flip a turn the CLI
    # reported as errored *because we interrupted it*, instead of reporting a
    # failure with nothing to approve.
    approval_halt_requested: bool = False


def translate_message(
    message: object,
    state: TurnState,
    classifier: SideEffectClassifier,
    gen: _GenerationSpan | None,
) -> list[OutboundEvent]:
    """Map one SDK message to the ACI outbound events it produces."""

    if isinstance(message, AssistantMessage):
        return _translate_assistant(message, state, classifier, gen)
    if isinstance(message, ResultMessage):
        return _translate_result(message, state, gen)
    if isinstance(message, RateLimitEvent):
        # status is one of allowed / allowed_warning / rejected; only a hard
        # rejection is an ACI error. The warning states are advisory (the model
        # is still allowed to continue) and must not inject a failure event into
        # an otherwise-successful run.
        if message.rate_limit_info.status == "rejected":
            state.error_classification = "rate-limit"
            return [ErrorEvent(message="model rate limit reached", classification="rate-limit")]
        return []
    # UserMessage, SystemMessage, and StreamEvent carry no outbound-visible
    # content in the v0.1 contract; they are intentionally dropped.
    return []


def _translate_assistant(
    message: AssistantMessage,
    state: TurnState,
    classifier: SideEffectClassifier,
    gen: _GenerationSpan | None,
) -> list[OutboundEvent]:
    events: list[OutboundEvent] = []

    # Backfill the generation model from the SDK's own report when CURIE_MODEL
    # was unset at span open (record_model no-ops once a model is already stamped).
    if gen is not None:
        gen.record_model(getattr(message, "model", None))

    error = getattr(message, "error", None)
    if error:
        state.error_classification = error
        events.append(ErrorEvent(message=f"model error: {error}", classification=error))

    for block in message.content:
        if isinstance(block, TextBlock):
            if block.text:
                state.assistant_text += block.text
                events.append(TextDelta(text=block.text))
        elif isinstance(block, ToolUseBlock):
            events.append(ToolNote(text=f"running tool {block.name}", tool=block.name))
            # Every tool call is evidence for the false-completion check (#517),
            # including the approval-request tool below and read-only tools.
            state.tool_call_count += 1
            if gen is not None:
                gen.tool_span(block.name)
            if block.name == APPROVAL_TOOL_NAME:
                # A policy gate fired (ADR-0010). Capture the summary (and the
                # optional route, #247) at the wire level so the real path
                # (executed in-process tool) and the fake path (scripted
                # ToolUseBlock) exercise one seam.
                payload = block.input if isinstance(block.input, dict) else {}
                summary = str(payload.get("summary") or "").strip()
                if summary:
                    # The summary is the model's own argument (attacker-
                    # influenced). Guard it out of the reserved permission-gate
                    # namespace so it can never masquerade as a genuine
                    # can_use_tool denial the worker would grant a bypass for
                    # (#430, ADR-0035).
                    state.approval_summary = guard_reserved_summary(summary)
                    route = str(payload.get("route") or "").strip()
                    state.approval_route = route or None
                    # A policy gate authorizes a business decision, never a tool
                    # (#544, Decision A): stamp the provenance and leave
                    # approval_granted_tool None so the worker can never mint a
                    # bypass grant from a model-authored request (#430).
                    state.approval_gate_kind = "policy"
            if classifier.is_side_effecting(block.name) and not state.side_effect_emitted:
                events.append(
                    SideEffectFlag(tool=block.name, detail="non-idempotent tool executed")
                )
                state.side_effect_emitted = True
    return events


def _translate_result(
    message: ResultMessage,
    state: TurnState,
    gen: _GenerationSpan | None,
) -> list[OutboundEvent]:
    if gen is not None:
        gen.record_usage(message.usage)

    subtype = message.subtype or ""
    if message.is_error or subtype.startswith("error"):
        text = message.result or "run failed"
        events: list[OutboundEvent] = []
        if state.error_classification is None:
            events.append(
                ErrorEvent(message=text, classification=subtype or "server-error")
            )
        events.append(Final(text=text, status=SessionStatus.CLASSIFIED_FAILURE))
        return events

    # The SDK's ``result`` is authoritative when present. When it is empty on an
    # otherwise-successful turn, fall back to the assistant text streamed this turn
    # so a reasoning model whose result-extraction returned empty (issue #107)
    # still delivers its answer. Provider-agnostic: it only fires when result is
    # empty, so non-reasoning models and the fake-model path are unaffected.
    #
    # Stamp the turn's token usage on the successful final (#390) so a consumer
    # (the eval runner) can attribute a dollar cost to the turn. Only the clean
    # DONE final carries usage: a classified failure never grades, and the
    # interrupt/approval overrides in the session reconstruct their own final.
    input_tokens, output_tokens = _usage_tokens(message.usage)
    return [
        Final(
            text=message.result or state.assistant_text,
            status=SessionStatus.DONE,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    ]


def _usage_tokens(usage: object) -> tuple[int | None, int | None]:
    """Read ``input_tokens``/``output_tokens`` off the SDK result's usage block.

    The SDK reports usage as a mapping (the Anthropic wire shape); a missing
    block or a non-integer value yields ``None`` so the wire never carries a
    fabricated count. Cache-token fields are deliberately not surfaced here --
    the eval cost model prices prompt/completion tokens only, and each eval case
    runs a fresh conversation (little cache benefit to attribute).
    """
    if not isinstance(usage, Mapping):
        return None, None

    def _int(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return _int("input_tokens"), _int("output_tokens")
