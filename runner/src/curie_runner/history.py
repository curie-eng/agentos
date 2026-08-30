"""Durable, harness-neutral structured conversation history.

ADR-0119 replaces the legacy rendered boot-prompt transcript with ordered
role/content messages. The durable record is provider-neutral: user and assistant
roles, opaque JSON content blocks (including tool calls/results), terminal and
approval context, plus an explicit stable summary record at compaction boundaries.
The Claude adapter materializes these records into its provider-native resume
envelope at boot; no provider transcript format is persisted here.

Design (ADR-0029):

- **History lives outside the sandbox** (ADR-0003, stateless-first). An unplanned
  runner-pod restart is a new pod with empty scratch, so a restarted thread must
  rehydrate from an external, durable resource reached over the network at boot.
- **The backing reuses the durable state store** landed for #23/#248 and #264
  (``apps/api`` ``/agents/{agent_id}/state/{namespace}/{key}``, Postgres JSONB),
  rather than inventing a new datastore. The thread's transcript is the
  log-shaped key ``.../state/transcript/<thread_key>``: the ``append`` endpoint
  gives the append-only write and ``get`` gives load.
- **Harness-agnostic storage.** A harness must consume the structured prefix or
  explicitly declare the capability absent. Rendering this data into a system
  prompt is not a supported fallback.

``CURIE_HISTORY_REF`` resolution: the ref is the URL of the thread's transcript
key on the state API (e.g. ``http://api:8000/agents/<id>/state/transcript/<thread>``).
The runner authenticates with ``CURIE_HISTORY_TOKEN`` (a runner-local knob, like
``CURIE_MEMORY_TOKEN`` -- NOT part of the frozen ACI ``SessionConfig``, so no
frozen-contract change). An ``s3://`` or other scheme is reserved for a future
loader and rejected loudly today.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast, runtime_checkable

import aiohttp
from aci_protocol import BootEnv

logger = logging.getLogger(__name__)

# Runner-local env carrying the bearer the state API expects (X-API-Key). Not a
# model credential and not part of the frozen ACI SessionConfig -- resolved the
# same way as CURIE_MEMORY_TOKEN and the other runner-local knobs. The worker
# declares and renders it, so the name is read from that one declaration (#488).
HISTORY_TOKEN_ENV = BootEnv.env_key("history_token")


class HistoryError(RuntimeError):
    """A history reference could not be resolved or dereferenced."""


class StructuredReplayUnsupported(HistoryError):
    """The selected harness cannot consume a recovered structured prefix."""


JsonContent = str | list[dict[str, Any]]


def _json_copy(value: JsonContent) -> JsonContent:
    """Return a detached JSON-safe copy of message content."""

    return cast("JsonContent", json.loads(json.dumps(value)))


@dataclass(frozen=True)
class ConversationMessage:
    """One portable prior message, preserving its role and content blocks."""

    role: str
    content: JsonContent

    def __post_init__(self) -> None:
        if self.role not in ("user", "assistant"):
            raise HistoryError(f"unsupported conversation role: {self.role!r}")
        if not isinstance(self.content, (str, list)):
            raise HistoryError("conversation message content must be a string or block list")
        if isinstance(self.content, list) and not all(
            isinstance(block, Mapping) for block in self.content
        ):
            raise HistoryError("conversation content blocks must be JSON objects")

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": _json_copy(self.content)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConversationMessage:
        role = data.get("role")
        content = data.get("content")
        if not isinstance(role, str) or not isinstance(content, (str, list)):
            raise HistoryError("invalid structured conversation message")
        blocks: JsonContent
        if isinstance(content, str):
            blocks = content
        elif all(isinstance(block, Mapping) for block in content):
            blocks = [dict(block) for block in content]
        else:
            raise HistoryError("conversation content blocks must be JSON objects")
        return cls(role=role, content=blocks)


@dataclass(frozen=True)
class ApprovalContext:
    """Durable approval/suspend context carried with the turn that paused."""

    summary: str | None = None
    route: str | None = None
    gate_kind: str | None = None
    granted_tool: str | None = None
    decision: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "summary": self.summary,
            "route": self.route,
            "gate_kind": self.gate_kind,
            "granted_tool": self.granted_tool,
            "decision": self.decision,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ApprovalContext:
        def optional_string(name: str) -> str | None:
            value = data.get(name)
            return value if isinstance(value, str) else None

        return cls(
            summary=optional_string("summary"),
            route=optional_string("route"),
            gate_kind=optional_string("gate_kind"),
            granted_tool=optional_string("granted_tool"),
            decision=optional_string("decision"),
        )


@dataclass(frozen=True)
class TurnRecord:
    """One durable turn with a legacy projection and structured messages.

    ``ts`` is set at append time (RFC3339 UTC) so a reloaded transcript keeps its
    order and a turn is timestamped for debugging. The pair is the minimal
    harness-agnostic unit: any harness can render it as prior context.
    """

    user: str
    assistant: str
    ts: str = ""
    messages: tuple[ConversationMessage, ...] = ()
    status: str = "done"
    approval: ApprovalContext | None = None

    def __post_init__(self) -> None:
        if not self.messages:
            object.__setattr__(
                self,
                "messages",
                (
                    ConversationMessage(role="user", content=self.user),
                    ConversationMessage(
                        role="assistant",
                        content=[{"type": "text", "text": self.assistant}],
                    ),
                ),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "turn",
            "user": self.user,
            "assistant": self.assistant,
            "ts": self.ts,
            "messages": [message.to_dict() for message in self.messages],
            "status": self.status,
            "approval": self.approval.to_dict() if self.approval is not None else None,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TurnRecord:
        raw_messages = data.get("messages")
        messages: tuple[ConversationMessage, ...] = ()
        if raw_messages is not None:
            if not isinstance(raw_messages, list) or not all(
                isinstance(item, Mapping) for item in raw_messages
            ):
                raise HistoryError("invalid structured conversation messages")
            messages = tuple(
                ConversationMessage.from_dict(item)
                for item in raw_messages
            )
        raw_approval = data.get("approval")
        return cls(
            user=str(data.get("user", "")),
            assistant=str(data.get("assistant", "")),
            ts=str(data.get("ts", "")),
            messages=messages,
            status=str(data.get("status", "done")),
            approval=(
                ApprovalContext.from_dict(raw_approval)
                if isinstance(raw_approval, Mapping)
                else None
            ),
        )


@dataclass(frozen=True)
class SummaryRecord:
    """A stable compaction boundary plus the un-compacted structured tail."""

    content: str
    digest: str
    source_turns: int
    through_ts: str
    tail: tuple[TurnRecord, ...] = ()
    ts: str = ""

    @property
    def messages(self) -> tuple[ConversationMessage, ...]:
        prefix = (
            ConversationMessage(
                role="user",
                content=(
                    "# Durable conversation summary\n\n"
                    "This summary was persisted at an explicit compaction boundary.\n\n"
                    f"{self.content}"
                ),
            ),
            ConversationMessage(
                role="assistant",
                content=[
                    {
                        "type": "text",
                        "text": "I will preserve this stable summary as prior context.",
                    }
                ],
            ),
        )
        return prefix + tuple(message for turn in self.tail for message in turn.messages)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "summary",
            "content": self.content,
            "digest": self.digest,
            "source_turns": self.source_turns,
            "through_ts": self.through_ts,
            "tail": [turn.to_dict() for turn in self.tail],
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SummaryRecord:
        raw_tail = data.get("tail")
        if raw_tail is not None and (
            not isinstance(raw_tail, list)
            or not all(isinstance(item, Mapping) for item in raw_tail)
        ):
            raise HistoryError("invalid structured summary tail")
        tail = (
            tuple(TurnRecord.from_dict(item) for item in raw_tail)
            if isinstance(raw_tail, list)
            else ()
        )
        return cls(
            content=str(data.get("content", "")),
            digest=str(data.get("digest", "")),
            source_turns=int(data.get("source_turns", 0)),
            through_ts=str(data.get("through_ts", "")),
            tail=tail,
            ts=str(data.get("ts", "")),
        )


HistoryRecord = TurnRecord | SummaryRecord


@dataclass(frozen=True)
class ConversationReplay:
    """The exact portable prefix a fresh harness must reconstruct."""

    messages: tuple[ConversationMessage, ...] = ()
    source_turns: int = 0
    summary_digest: str | None = None

    @property
    def present(self) -> bool:
        return bool(self.messages)


def close_suspended_tool_calls(
    messages: Sequence[ConversationMessage],
) -> tuple[ConversationMessage, ...]:
    """Close dangling permission-gated tool calls with a denial result.

    An interrupting permission denial can end the harness iterator immediately
    after its ``tool_use`` message. Provider APIs require every prior tool call
    to have a following ``tool_result`` before another user message can be
    submitted. Persist an explicit, truthful negative result for only the calls
    that remain unmatched; a result already supplied by the harness is preserved
    byte-for-byte and this operation is idempotent.
    """

    pending: dict[str, None] = {}
    for message in messages:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            block_type = block.get("type")
            if message.role == "assistant" and block_type == "tool_use":
                tool_use_id = block.get("id")
                if isinstance(tool_use_id, str):
                    pending[tool_use_id] = None
            elif message.role == "user" and block_type == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    pending.pop(tool_use_id, None)
    if not pending:
        return tuple(messages)
    return (
        *messages,
        ConversationMessage(
            role="user",
            content=[
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "Tool call was not executed; awaiting human approval.",
                    "is_error": True,
                }
                for tool_use_id in pending
            ],
        ),
    )


@runtime_checkable
class TranscriptStore(Protocol):
    """The history port: load prior turns, append the latest one.

    Deliberately narrow -- no query language and no in-place rewrite. A concrete
    store dereferences a ``history_ref`` to a durable, rehydratable backing that
    lives outside the sandbox; compaction is itself an append-only summary.
    """

    async def load(self) -> list[HistoryRecord]:
        """Return prior turns, oldest first (empty when none)."""
        ...

    async def append(self, record: HistoryRecord) -> None:
        """Durably append one turn; it must survive an unplanned restart."""
        ...


class NullTranscriptStore:
    """The no-history store used when ``CURIE_HISTORY_REF`` is unset.

    ``load`` yields nothing and ``append`` is a silent no-op, so the boot and
    per-turn paths are uniform whether or not a thread has a transcript ref.
    """

    async def load(self) -> list[HistoryRecord]:
        return []

    async def append(self, record: HistoryRecord) -> None:  # noqa: ARG002 - null sink
        return None


class StateApiTranscriptStore:
    """Transcript backed by the durable state store (#23/#248/#264), the default.

    ``history_ref`` is the URL of the thread's transcript key on the state API
    (``.../agents/<id>/state/transcript/<thread_key>``). Load is a GET of that
    key; append is a POST to the key's ``/append`` endpoint. The state API
    enforces the size caps and the Postgres JSONB backing gives durability across
    an unplanned restart for free.
    """

    def __init__(self, key_url: str, token: str | None) -> None:
        # Normalize to no trailing slash so the /append URL composes cleanly.
        self._key_url = key_url.rstrip("/")
        self._token = token

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._token} if self._token else {}

    async def load(self) -> list[HistoryRecord]:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(self._key_url, headers=self._headers()) as resp:
                if resp.status == 404:
                    # No transcript written yet -- a fresh thread, not an error.
                    return []
                if resp.status != 200:
                    body = await resp.text()
                    raise HistoryError(f"history load failed: {resp.status} {body[:200]}")
                payload = await resp.json()
        value = payload.get("value")
        if not isinstance(value, list):
            raise HistoryError("transcript log is not a JSON array")
        records: list[HistoryRecord] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise HistoryError("invalid transcript record: expected a JSON object")
            if item.get("type") == "summary":
                records.append(SummaryRecord.from_dict(item))
            elif "user" in item:
                records.append(TurnRecord.from_dict(item))
            else:
                raise HistoryError("invalid transcript record: unknown record shape")
        return records

    async def append(self, record: HistoryRecord) -> None:
        timeout = aiohttp.ClientTimeout(total=15)
        body = json.dumps({"item": record.to_dict()})
        headers = {**self._headers(), "Content-Type": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{self._key_url}/append", data=body, headers=headers
            ) as resp:
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise HistoryError(f"history append failed: {resp.status} {text[:200]}")


def resolve_history(history_ref: str | None, env: Mapping[str, str]) -> TranscriptStore:
    """Resolve ``CURIE_HISTORY_REF`` to a concrete ``TranscriptStore`` at boot.

    An absent ref yields the ``NullTranscriptStore`` (history is optional). An
    ``http(s)://`` ref is the state-API transcript-key URL and yields the default
    ``StateApiTranscriptStore``. Any other scheme (an old SDK ``resume`` id,
    ``s3://``, ...) is reserved for a future loader and rejected loudly rather
    than silently dropped, so a misconfigured ref fails visibly at boot.
    """

    if not history_ref:
        return NullTranscriptStore()
    if history_ref.startswith(("http://", "https://")):
        return StateApiTranscriptStore(history_ref, env.get(HISTORY_TOKEN_ENV))
    raise HistoryError(
        f"unsupported CURIE_HISTORY_REF scheme: {history_ref!r} "
        "(only http(s):// state-API refs are implemented today)"
    )


def _replay_bytes(messages: Sequence[ConversationMessage]) -> int:
    return len(
        json.dumps(
            [message.to_dict() for message in messages],
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _turn_summary_line(turn: TurnRecord) -> str:
    tool_names: list[str] = []
    tool_results: list[str] = []
    for message in turn.messages:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if block.get("type") == "tool_use":
                tool_names.append(str(block.get("name") or "unknown"))
            elif block.get("type") == "tool_result":
                result = str(block.get("content") or "")
                tool_results.append(result[:300])
    parts = [f"User: {turn.user}", f"Assistant: {turn.assistant}"]
    if tool_names:
        parts.append(f"Tools: {', '.join(tool_names)}")
    if tool_results:
        parts.append(f"Tool results: {' | '.join(tool_results)}")
    if turn.approval is not None:
        approval = turn.approval
        parts.append(
            "Approval: "
            f"status={turn.status} kind={approval.gate_kind or '-'} "
            f"route={approval.route or '-'} decision={approval.decision or '-'} "
            f"summary={approval.summary or '-'}"
        )
    return "\n".join(parts)


def _make_summary(
    prior: SummaryRecord | None,
    compacted: Sequence[TurnRecord],
    tail: Sequence[TurnRecord],
    *,
    max_bytes: int | None,
) -> SummaryRecord:
    source = {
        "prior_digest": prior.digest if prior is not None else None,
        "prior_content": prior.content if prior is not None else None,
        "turns": [turn.to_dict() for turn in compacted],
    }
    canonical = json.dumps(source, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    sections: list[str] = []
    if prior is not None:
        sections.append(prior.content)
    sections.extend(_turn_summary_line(turn) for turn in compacted)
    content = "\n\n".join(sections)
    # The summary is written once at this explicit boundary. Bounding it here is
    # stable: later appends never rewrite it; only a later compaction creates a
    # new record. The digest keeps omitted material falsifiable.
    budget = max(512, (max_bytes // 2) if max_bytes is not None else 8_000)
    encoded = content.encode("utf-8")
    if len(encoded) > budget:
        prefix = encoded[: max(0, budget - 96)].decode("utf-8", errors="ignore")
        content = f"{prefix}\n\n[older detail summarized; digest={digest}]"
    source_turns = (prior.source_turns if prior is not None else 0) + len(compacted)
    through_ts = compacted[-1].ts if compacted else (prior.through_ts if prior else "")
    return SummaryRecord(
        content=content,
        digest=digest,
        source_turns=source_turns,
        through_ts=through_ts,
        tail=tuple(tail),
        ts=datetime.now(UTC).isoformat(),
    )


def build_conversation_replay(
    records: Sequence[HistoryRecord],
    *,
    max_turns: int | None = 40,
    max_bytes: int | None = 16_000,
) -> tuple[ConversationReplay, SummaryRecord | None]:
    """Build a structured prefix and, only at a boundary, a new stable summary.

    Summary records are append-only. A record embeds the short un-compacted tail,
    so appending the summary after the turns it represents does not reorder or
    rewrite stored history. Subsequent turns extend that exact replay prefix until
    one of the bounds is crossed again.
    """

    latest_summary: SummaryRecord | None = None
    latest_summary_index = -1
    for index, record in enumerate(records):
        if isinstance(record, SummaryRecord):
            latest_summary = record
            latest_summary_index = index

    appended_turns = [
        record
        for record in records[latest_summary_index + 1 :]
        if isinstance(record, TurnRecord)
    ]
    active_turns = [
        *((latest_summary.tail) if latest_summary is not None else ()),
        *appended_turns,
    ]

    if latest_summary is not None:
        current_messages = latest_summary.messages + tuple(
            message for turn in appended_turns for message in turn.messages
        )
        source_turns = latest_summary.source_turns + len(active_turns)
    else:
        current_messages = tuple(
            message for turn in active_turns for message in turn.messages
        )
        source_turns = len(active_turns)

    over_turns = max_turns is not None and len(active_turns) > max_turns
    over_bytes = max_bytes is not None and _replay_bytes(current_messages) > max_bytes
    if not over_turns and not over_bytes:
        return (
            ConversationReplay(
                messages=current_messages,
                source_turns=source_turns,
                summary_digest=latest_summary.digest if latest_summary else None,
            ),
            None,
        )

    if not active_turns:
        return ConversationReplay(), None
    keep_count = max(
        1,
        min(len(active_turns), (max_turns or len(active_turns)) // 2),
    )
    compacted = active_turns[:-keep_count]
    tail: Sequence[TurnRecord] = active_turns[-keep_count:]
    if not compacted:
        compacted = active_turns[:-1]
        tail = active_turns[-1:]
    summary = _make_summary(latest_summary, compacted, tail, max_bytes=max_bytes)

    # If a byte-only bound is still exceeded, move more of the tail into the
    # stable summary until the remaining replay fits or one latest turn remains.
    while max_bytes is not None and len(summary.tail) > 1:
        if _replay_bytes(summary.messages) <= max_bytes:
            break
        compacted = [*compacted, summary.tail[0]]
        tail = summary.tail[1:]
        summary = _make_summary(latest_summary, compacted, tail, max_bytes=max_bytes)

    return (
        ConversationReplay(
            messages=summary.messages,
            source_turns=summary.source_turns + len(summary.tail),
            summary_digest=summary.digest,
        ),
        summary,
    )


# Sane structured-replay caps. Crossing one creates a new durable summary;
# ordinary appends never move or rewrite the already-cached prefix.
DEFAULT_REPLAY_MAX_TURNS = 40
DEFAULT_REPLAY_MAX_BYTES = 16_000


def utcnow_iso() -> str:
    """An RFC3339 UTC timestamp for a turn record's ``ts``."""
    return datetime.now(UTC).isoformat()
