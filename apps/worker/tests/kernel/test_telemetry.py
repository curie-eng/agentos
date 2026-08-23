"""Whole-turn worker telemetry regressions for issue #1817.

These are seam tests, not mocks of Valkey or HTTP.  Queue delivery and
settlement use the real local Valkey, while the kernel harness keeps its
existing in-process runner and reply sink.  Reverting transport extraction or
any worker instrumentation must therefore break parent IDs/status/events while
the unchanged routing assertions continue to guard the sacred kernel.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from aci_protocol import Final, QueuedTurn, ReplyHandle, SessionStatus, TextDelta
from curie_dispatcher.queue import to_stream_fields
from curie_telemetry import TRACE_CONTEXT_FIELD, inject_trace_context
from curie_telemetry.attributes import event_names_for
from curie_worker import consumer as consumer_module
from curie_worker.behaviorpacks import BehaviorPacks
from curie_worker.consumer import Consumer
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind, StatusCode

DONE = SessionStatus.DONE
FAILED = SessionStatus.CLASSIFIED_FAILURE

_WORKER_EVENTS = frozenset(
    {
        "trace_context.invalid",
        "messaging.ack",
        "messaging.pending",
        "messaging.dead_letter",
        "worker.queue.wait",
        "worker.dedupe.checked",
        "worker.dedupe.skip",
        "worker.lock.wait",
        "worker.lock.acquired",
        "worker.route.start",
        "worker.route.steer",
        "worker.route.finish_race",
        "worker.reply.final",
        "worker.retry.scheduled",
        "worker.retry.stopped",
        "worker.completion.settled",
        "worker.terminal",
    }
)

_MALFORMED_SENTINEL = "SENSITIVE-CARRIER-CONTENT-MUST-NOT-BE-LOGGED"
_VALID_LOOKING_TRACEPARENT = (
    "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01"
)
_MALFORMED_CARRIERS = (
    f'["{_MALFORMED_SENTINEL}"]',
    (
        f'{{"traceparent":"{_VALID_LOOKING_TRACEPARENT}",'
        f'"unexpected":"{_MALFORMED_SENTINEL}"}}'
    ),
    f'{{"traceparent":"invalid-{_MALFORMED_SENTINEL}"}}',
    f'{{"traceparent":"{"x" * 8192}{_MALFORMED_SENTINEL}"}}',
)


def test_worker_event_vocabulary_is_the_closed_shared_registry() -> None:
    assert event_names_for("curie-worker") == _WORKER_EVENTS


def _qevent(
    text: str = "safe test turn",
    *,
    thread: str = "thread-example",
    event_id: str | None = None,
) -> QueuedTurn:
    return QueuedTurn(
        event_id=event_id or uuid.uuid4().hex,
        conversation_id=thread,
        author="U0EXAMPLE1",
        text=text,
        reply_handle=ReplyHandle(
            kind="slack",
            channel="C0EXAMPLE1",
            placeholder="1700000000.000100",
        ),
        received_at="2026-07-05T00:00:00+00:00",
    )


def _event_names(span: ReadableSpan) -> set[str]:
    return {event.name for event in span.events}


def _span_payload(span: ReadableSpan) -> str:
    """Only exported names/attributes/events, never timing or object reprs."""

    return repr(
        (
            span.name,
            dict(span.attributes or {}),
            [
                (event.name, dict(event.attributes or {}))
                for event in span.events
            ],
            span.status.description,
        )
    )


async def _wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


async def _deliver_new(
    consumer: Consumer,
    h: Any,
    fields: dict[str, str],
) -> str:
    """XADD, group-deliver and settle one entry without a racing read loop."""

    entry_id = await h.async_redis.xadd(h.config.stream, fields)
    delivered = await h.async_redis.xreadgroup(
        h.config.consumer_group,
        h.config.consumer_name,
        {h.config.stream: ">"},
        count=1,
    )
    assert delivered and delivered[0][1]
    [(read_id, read_fields)] = delivered[0][1]
    assert read_id == entry_id
    await consumer._dispatch(read_id, read_fields)
    # A fast handler can finish between ``_dispatch`` returning and a snapshot
    # of ``_inflight``; its done callback then removes the task before the test
    # can await it.  The entry remains in ``_inflight_ids`` until _handle's
    # finally block has ended the process span, so its removal is the stable,
    # observable settlement boundary these assertions need.
    await _wait_until(lambda: read_id not in consumer._inflight_ids)
    return entry_id


async def _reclaim_and_settle(consumer: Consumer) -> None:
    await consumer._reclaim_once()
    await _wait_until(lambda: not consumer._inflight_ids)


def test_valid_valkey_carrier_parents_process_span_and_success_acks(
    make_harness,
    span_recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        log_events: list[tuple[str, int, int]] = []

        def record_log_event(
            _logger: logging.Logger,
            event: str,
            *,
            level: int = logging.INFO,
        ) -> None:
            context = trace.get_current_span().get_span_context()
            log_events.append((event, level, context.span_id))

        monkeypatch.setattr(consumer_module, "emit_log_event", record_log_event)
        async with make_harness(tracer=span_recorder.tracer) as h:
            h.runner.default_script = [Final(text="safe answer", status=DONE)]
            consumer = Consumer(
                redis=h.async_redis,
                kernel=h.kernel,
                config=h.config,
                tracer=span_recorder.tracer,
            )
            await consumer.ensure_group()

            with span_recorder.tracer.start_as_current_span("test.producer") as producer:
                producer_context = producer.get_span_context()
                carrier = inject_trace_context()
            assert carrier is not None

            prompt = "PRIVATE-PROMPT-MUST-NOT-BE-AN-ATTRIBUTE"
            fields = to_stream_fields(_qevent(prompt))
            fields[TRACE_CONTEXT_FIELD] = carrier
            await _deliver_new(consumer, h, fields)

            process = span_recorder.one("process curie:runs")
            assert process.kind is SpanKind.CONSUMER
            assert process.status.status_code is StatusCode.OK
            assert process.attributes is not None
            assert process.attributes["messaging.system"] == "valkey"
            assert process.attributes["messaging.destination.name"] == h.config.stream
            assert process.attributes["messaging.operation.type"] == "process"
            assert isinstance(process.attributes["curie.queue.wait_ms"], int)
            assert process.attributes["curie.queue.wait_ms"] >= 0
            assert process.context is not None
            assert process.context.trace_id == producer_context.trace_id
            assert process.parent is not None
            assert process.parent.span_id == producer_context.span_id
            assert "messaging.ack" in _event_names(process)
            assert "worker.queue.wait" in _event_names(process)
            assert h.sink.last_text == "safe answer"
            assert log_events == [
                ("worker.turn.completed", logging.INFO, process.context.span_id)
            ]
            pending = await h.async_redis.xpending(
                h.config.stream, h.config.consumer_group
            )
            assert pending["pending"] == 0
            assert all(
                prompt not in _span_payload(span) for span in span_recorder.spans()
            )

    asyncio.run(go())


def test_queue_wait_uses_valkey_enqueue_time_and_malformed_ids_omit_safely(
    make_harness,
    span_recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        async with make_harness(tracer=span_recorder.tracer) as h:
            consumer = Consumer(
                redis=h.async_redis,
                kernel=h.kernel,
                config=h.config,
                tracer=span_recorder.tracer,
            )
            monkeypatch.setattr(time, "time_ns", lambda: 1_700_000_001_500_000_000)

            measured, _ = consumer._process_span("1700000000000-0", {})
            measured.end()
            malformed, _ = consumer._process_span("not-a-stream-id", {})
            malformed.end()

            measured_span, malformed_span = span_recorder.spans(
                name="process curie:runs"
            )
            assert measured_span.attributes is not None
            assert measured_span.attributes["curie.queue.wait_ms"] == 1_500
            assert "worker.queue.wait" in _event_names(measured_span)
            assert malformed_span.attributes is not None
            assert "curie.queue.wait_ms" not in malformed_span.attributes
            assert "worker.queue.wait" not in _event_names(malformed_span)

    asyncio.run(go())


def test_payload_only_valkey_entry_starts_a_root_and_still_completes(
    make_harness,
    span_recorder,
) -> None:
    async def go() -> None:
        async with make_harness(tracer=span_recorder.tracer) as h:
            h.runner.default_script = [Final(text="payload-only answer", status=DONE)]
            consumer = Consumer(
                redis=h.async_redis,
                kernel=h.kernel,
                config=h.config,
                tracer=span_recorder.tracer,
            )
            await consumer.ensure_group()
            fields = to_stream_fields(_qevent(thread="payload-only-thread"))
            fields.pop(TRACE_CONTEXT_FIELD, None)

            await _deliver_new(consumer, h, fields)

            process = span_recorder.one("process curie:runs")
            assert process.parent is None
            assert process.status.status_code is StatusCode.OK
            assert "messaging.ack" in _event_names(process)
            assert h.sink.last_text == "payload-only answer"

    asyncio.run(go())


@pytest.mark.parametrize("raw", _MALFORMED_CARRIERS)
def test_malformed_valkey_carrier_is_value_free_warning_and_fail_open(
    raw: str,
    make_harness,
    span_recorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def go() -> None:
        async with make_harness(tracer=span_recorder.tracer) as h:
            h.runner.default_script = [Final(text="malformed-carrier answer", status=DONE)]
            consumer = Consumer(
                redis=h.async_redis,
                kernel=h.kernel,
                config=h.config,
                tracer=span_recorder.tracer,
            )
            await consumer.ensure_group()
            fields = to_stream_fields(_qevent(thread="malformed-carrier-thread"))
            fields[TRACE_CONTEXT_FIELD] = raw

            with caplog.at_level(logging.WARNING):
                await _deliver_new(consumer, h, fields)

            process = span_recorder.one("process curie:runs")
            assert process.parent is None
            assert process.status.status_code is StatusCode.OK
            assert "trace_context.invalid" in _event_names(process)
            assert "messaging.ack" in _event_names(process)
            warnings = [
                record
                for record in caplog.records
                if record.levelno >= logging.WARNING
                and record.getMessage() == "ignored malformed trace context"
            ]
            assert warnings, "malformed carrier was ignored without a diagnostic"
            assert raw not in caplog.text
            assert _MALFORMED_SENTINEL not in caplog.text
            assert h.sink.last_text == "malformed-carrier answer"

    asyncio.run(go())


def test_processing_exception_marks_error_and_leaves_entry_pending(
    make_harness,
    span_recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def go() -> None:
        log_events: list[tuple[str, int, int]] = []

        def record_log_event(
            _logger: logging.Logger,
            event: str,
            *,
            level: int = logging.INFO,
        ) -> None:
            context = trace.get_current_span().get_span_context()
            log_events.append((event, level, context.span_id))

        monkeypatch.setattr(consumer_module, "emit_log_event", record_log_event)
        async with make_harness(tracer=span_recorder.tracer) as h:
            consumer = Consumer(
                redis=h.async_redis,
                kernel=h.kernel,
                config=h.config,
                tracer=span_recorder.tracer,
            )
            await consumer.ensure_group()

            async def fail(_qevent: QueuedTurn) -> None:
                raise RuntimeError("injected runner failure")

            h.kernel.process_event = fail  # type: ignore[method-assign]
            entry_id = await _deliver_new(consumer, h, to_stream_fields(_qevent()))

            process = span_recorder.one("process curie:runs")
            assert process.status.status_code is StatusCode.ERROR
            assert "messaging.pending" in _event_names(process)
            assert process.context is not None
            assert log_events == [
                ("worker.turn.failed", logging.ERROR, process.context.span_id)
            ]
            pending = await h.async_redis.xpending_range(
                h.config.stream,
                h.config.consumer_group,
                min=entry_id,
                max=entry_id,
                count=1,
            )
            assert [row["message_id"] for row in pending] == [entry_id]

    asyncio.run(go())


def test_delivery_cap_dead_letter_retains_carrier_and_trace_settlement(
    make_harness,
    span_recorder,
) -> None:
    async def go() -> None:
        async with make_harness(
            tracer=span_recorder.tracer,
            max_delivery=2,
            reclaim_min_idle_ms=0,
        ) as h:
            consumer = Consumer(
                redis=h.async_redis,
                kernel=h.kernel,
                config=h.config,
                tracer=span_recorder.tracer,
            )
            await consumer.ensure_group()

            async def fail(_qevent: QueuedTurn) -> None:
                raise RuntimeError("permanent injected failure")

            h.kernel.process_event = fail  # type: ignore[method-assign]
            with span_recorder.tracer.start_as_current_span("test.producer") as producer:
                producer_context = producer.get_span_context()
                carrier = inject_trace_context()
            assert carrier is not None
            fields = to_stream_fields(_qevent(event_id="dead-letter-example"))
            fields[TRACE_CONTEXT_FIELD] = carrier
            entry_id = await _deliver_new(consumer, h, fields)

            await _reclaim_and_settle(consumer)  # delivery 2, still pending
            await _reclaim_and_settle(consumer)  # budget spent: XADD then XACK

            rows = await h.async_redis.xrange(h.config.dead_letter_stream_name())
            assert len(rows) == 1
            _dead_id, dead = rows[0]
            assert dead["dl_original_id"] == entry_id
            assert dead[TRACE_CONTEXT_FIELD] == carrier
            pending = await h.async_redis.xpending(
                h.config.stream, h.config.consumer_group
            )
            assert pending["pending"] == 0

            processes = [
                span
                for span in span_recorder.spans(name="process curie:runs")
                if span.context.trace_id == producer_context.trace_id
            ]
            assert len(processes) == 3
            assert all(span.parent is not None for span in processes)
            assert any("messaging.dead_letter" in _event_names(span) for span in processes)
            dead_letter_process = next(
                span
                for span in processes
                if "messaging.dead_letter" in _event_names(span)
            )
            assert dead_letter_process.status.status_code is StatusCode.ERROR
            assert carrier not in _span_payload(dead_letter_process)
            await h.async_redis.delete(h.config.dead_letter_stream_name())

    asyncio.run(go())


def test_kernel_turn_events_preserve_dedupe_and_finish_race_routing(
    make_harness,
    span_recorder,
) -> None:
    async def go() -> None:
        async with make_harness(tracer=span_recorder.tracer) as h:
            h.runner.default_script = [Final(text="first answer", status=DONE)]
            first = _qevent("first", thread="finish-race", event_id="first-example")
            await h.kernel.process_event(first)
            await h.kernel.process_event(first)

            # The existing route is idle.  The steer returns 409 and the sacred
            # kernel must open a new turn on that same sandbox.
            h.runner.default_script = [Final(text="second answer", status=DONE)]
            await h.kernel.process_event(
                _qevent("second", thread="finish-race", event_id="second-example")
            )

            assert h.runner.opened == ["first", "second"]
            assert h.runner.steers == []
            assert h.sink.last_text == "second answer"

            turns = span_recorder.spans(name="worker.turn")
            assert len(turns) == 3
            assert all(span.status.status_code is StatusCode.OK for span in turns)
            assert turns[0].attributes is not None
            assert turns[0].attributes["curie.turn.outcome"] == "delivered"
            assert turns[1].attributes is not None
            assert turns[1].attributes["curie.turn.outcome"] == "duplicate"
            assert turns[2].attributes is not None
            assert turns[2].attributes["curie.turn.outcome"] == "delivered"
            names = [_event_names(span) for span in turns]
            assert {
                "worker.dedupe.checked",
                "worker.lock.wait",
                "worker.lock.acquired",
                "worker.route.start",
                "worker.reply.final",
                "worker.completion.settled",
                "worker.terminal",
            } <= names[0]
            assert "worker.dedupe.skip" in names[1]
            assert {"worker.route.finish_race", "worker.route.start"} <= names[2]

    asyncio.run(go())


def test_kernel_live_followup_emits_steer_without_forking_a_turn(
    make_harness,
    span_recorder,
) -> None:
    async def go() -> None:
        async with make_harness(tracer=span_recorder.tracer) as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="working")]
            h.runner.tail = [Final(text="done", status=DONE)]

            owner = asyncio.create_task(
                h.kernel.process_event(
                    _qevent("first", thread="live-steer", event_id="owner-example")
                )
            )
            await _wait_until(lambda: h.runner.turn_active)
            await h.kernel.process_event(
                _qevent("second", thread="live-steer", event_id="steer-example")
            )

            assert h.runner.opened == ["first"]
            assert h.runner.steers == ["second"]
            steered = [
                span
                for span in span_recorder.spans(name="worker.turn")
                if "worker.route.steer" in _event_names(span)
            ]
            assert len(steered) == 1
            assert steered[0].status.status_code is StatusCode.OK
            assert steered[0].attributes is not None
            assert steered[0].attributes["curie.turn.outcome"] == "steered"
            hold.set()
            await owner

    asyncio.run(go())


def test_classified_failure_marks_worker_turn_error_without_changing_retry_policy(
    make_harness,
    span_recorder,
) -> None:
    async def go() -> None:
        async with make_harness(tracer=span_recorder.tracer, max_attempts=3) as h:
            h.runner.default_script = [Final(text="safe failure", status=FAILED)]
            await h.kernel.process_event(
                _qevent("run", thread="failed-turn", event_id="failed-example")
            )

            assert h.runner.opened == ["run", "run", "run"]
            assert h.sink.last_text is not None
            assert "human" in h.sink.last_text.lower()
            turn = span_recorder.one("worker.turn")
            assert turn.status.status_code is StatusCode.ERROR
            assert turn.attributes is not None
            assert turn.attributes["curie.turn.outcome"] == "escalated"
            assert {
                "worker.terminal",
                "worker.completion.settled",
                "worker.retry.scheduled",
                "worker.retry.stopped",
            } <= _event_names(turn)
            assert [event.name for event in turn.events].count(
                "worker.retry.scheduled"
            ) == 2

    asyncio.run(go())


class _ResolvedBinding:
    def __init__(self, agent_id: uuid.UUID, token: str) -> None:
        self.agent_id = agent_id
        self.token = token
        self.endpoint: str | None = None
        self.adapter: str | None = None

    async def resolve(self, _kind: str, _channel: str) -> _ResolvedBinding:
        return self

    def boot_env(self, _resolved: object, _thread: str) -> dict[str, str]:
        return {"CURIE_RUNNER_TOKEN": self.token}

    def packs_for(self, _resolved: object) -> BehaviorPacks:
        return BehaviorPacks()


class _SecondCheckExplodes:
    def __init__(self) -> None:
        self.calls = 0

    async def is_killed(self, _agent_id: uuid.UUID) -> bool:
        self.calls += 1
        if self.calls == 1:
            return False
        raise RuntimeError("injected killswitch recheck failure")


def test_killswitch_recheck_failure_closes_started_http_stream_before_consumption(
    make_harness,
    span_recorder,
) -> None:
    """The response is open before the post-registration kill recheck.

    If that recheck raises, the kernel never enters ``async with turn``.  The
    manually-owned CLIENT span and response must still be closed in the caller's
    route/start failure cleanup rather than leaking until process teardown.
    """

    async def go() -> None:
        auth = "runner-auth-PLACEHOLDER-preconsume"
        binding = _ResolvedBinding(uuid.uuid4(), auth)
        async with make_harness(
            tracer=span_recorder.tracer,
            binding=binding,
        ) as h:
            h.runner.default_script = [Final(text="unconsumed", status=DONE)]
            killswitch = _SecondCheckExplodes()
            h.kernel.attach_killswitch(killswitch)  # type: ignore[arg-type]
            event = _qevent(
                "preconsume",
                thread="killswitch-preconsume",
                event_id="killswitch-preconsume-example",
            )

            with pytest.raises(RuntimeError, match="killswitch recheck"):
                await h.kernel.process_event(event)

            assert killswitch.calls == 2
            assert h.runner.opened == ["preconsume"]
            # Preserve the pre-telemetry timing contract: the placeholder is
            # updated before route/start, even when the post-start kill recheck
            # later fails. The open response still closes below.
            assert h.sink.last_text == h.config.booting_text
            assert not await h.async_redis.exists(h.config.done_key(event.event_id))
            clients = span_recorder.spans(kind=SpanKind.CLIENT)
            assert len(clients) == 2  # steer 409 followed by the accepted event stream
            assert all(span.end_time is not None for span in clients)
            assert any(span.status.status_code is StatusCode.ERROR for span in clients)
            assert all(auth not in _span_payload(span) for span in clients)
            turn = span_recorder.one("worker.turn")
            assert turn.status.status_code is StatusCode.ERROR

    asyncio.run(go())
