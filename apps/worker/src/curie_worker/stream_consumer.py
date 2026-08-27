"""Shared Valkey consumer-group read-loop mechanics.

The runs consumer (``consumer.py``) and the eval consumer (``eval/stream.py``)
both drive the same Valkey ``XREADGROUP`` loop: ensure the group exists, do a
blocking group read, skip an empty/timeout response, survive a transient
transport fault (a blocking-read ``TimeoutError`` is the routine idle case ->
DEBUG; a ``ConnectionError`` is a real fault -> WARNING), back off and retry, and
ack a handled entry. That shared plumbing lives here once; each consumer keeps
its own stream/group/consumer names, backoff constant, log-message prefixes, and
per-message business logic and passes them in.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    ResponseError,
)
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)

from .broker import StreamBroker
from .consumer_liveness import ConsumerLivenessStore

# One stream entry as redis returns it with decode_responses=True.
StreamEntry = tuple[str, dict[str, str]]

# An async per-message handler: (entry_id, fields) -> None.
EntryHandler = Callable[[str, dict[str, str]], Awaitable[None]]


@dataclass(frozen=True)
class ReadLoopSpec:
    """The per-consumer knobs the shared read loop needs. Everything here is
    deliberately passed in (not hard-coded) so each consumer's stream, group,
    backoff, and log output stay exactly what they were before the extraction."""

    stream: str
    group: str
    consumer: str
    count: int
    block_ms: int
    backoff_s: float
    # Log messages kept per-consumer so log output is byte-identical; each takes
    # a single ``%s`` for the exception. The logger is the owning module's logger
    # so records carry that module's name (tests assert on it).
    timeout_msg: str
    connection_msg: str
    logger: logging.Logger


@dataclass(frozen=True)
class DeliverySpec:
    """The per-consumer knobs the shared reclaim/dead-letter machinery needs;
    passed in so each lane stays byte-identical after the extraction."""

    stream: str
    group: str
    consumer: str
    dead_letter_target: str
    over_cap_reason: str
    max_delivery: int
    dead_letter_maxlen: int
    reclaim_min_idle_ms: int
    dead_consumer_idle_ms: int
    heartbeat_ttl_ms: int
    capability_ttl_ms: int
    read_count: int
    cap_scan_page: int
    handler: EntryHandler
    logger: logging.Logger
    dead_letter_log: str
    dead_letter_fail_log: str


class ConsumerLivenessExpired(RuntimeError):
    """The local consumer could no longer renew its alive lease safely."""


class StreamConsumer:
    """Base for a Valkey consumer-group reader.

    Owns the transport-level plumbing (group create, blocking read loop with the
    Timeout->DEBUG / Connection->WARNING split and backoff, ack, stop-aware
    sleep). Subclasses supply their stream config via a :class:`ReadLoopSpec`,
    their per-message handler, and their own maintenance/reclaim loops and
    business logic.
    """

    def __init__(
        self,
        redis: StreamBroker,
        delivery: DeliverySpec | None = None,
        *,
        liveness_store: ConsumerLivenessStore | None = None,
    ) -> None:
        # The stream broker behind the port (#284). ``redis.asyncio.Redis`` is the
        # one backing today and structurally satisfies ``StreamBroker``; a second
        # broker is a drop-in. Named ``_redis`` still so the sacred consumer.py
        # subclass (which reads ``self._redis`` for XAUTOCLAIM) is untouched.
        self._redis: StreamBroker = redis
        # Process stop is permanent (SIGTERM/operator shutdown). Generation stop
        # is replaced for every supervised ``run()`` so a terminal lease failure
        # can tear one generation down and restart cleanly without making the
        # process look gracefully stopped.
        self._stop = asyncio.Event()
        self._generation_stop = asyncio.Event()
        # Entry ids currently being handled by THIS consumer. XAUTOCLAIM would
        # otherwise reclaim our own long-running (still-pending) entries and
        # re-dispatch a duplicate handler that steers the same prompt into its
        # own live turn; skipping these ids prevents that self-reclaim.
        self._inflight_ids: set[str] = set()
        # The reclaim/dead-letter knobs, or None for a base-only reader that
        # exercises just ``_consume`` (no reclaim machinery).
        self._delivery = delivery
        self._liveness_store = liveness_store
        # The first absent observation for each capable peer. It is scoped to a
        # run generation and cleared on restoration/disappearance/restart.
        self._peer_absent_since: dict[str, float] = {}
        # Prompt and 15-minute recovery both select PEL ownership through this
        # lock. It never covers handler execution or a capacity-semaphore wait.
        self._reclaim_lock = asyncio.Lock()
        self._last_liveness_renewal: float | None = None

    @property
    def _spec(self) -> DeliverySpec:
        assert self._delivery is not None, (
            "reclaim/dead-letter machinery used without a DeliverySpec"
        )
        return self._delivery

    def request_stop(self) -> None:
        self._stop.set()

    def _should_stop(self) -> bool:
        return self._stop.is_set() or self._generation_stop.is_set()

    async def _ensure_group(self, stream: str, group: str, *, start_id: str) -> None:
        """Create the consumer group (and the stream) if it does not exist.

        ``start_id`` is the group's read start position and is per-consumer (see
        each subclass's ``ensure_group`` for why it picks ``$`` vs ``0``). An
        existing group is left untouched.
        """
        try:
            await self._redis.xgroup_create(stream, group, id=start_id, mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _consume(self, spec: ReadLoopSpec, handler: EntryHandler) -> None:
        """Blocking-read loop: read the group, dispatch each entry to ``handler``,
        and survive transient transport faults until stop is requested."""
        while not self._should_stop():
            try:
                resp = await self._redis.xreadgroup(
                    spec.group,
                    spec.consumer,
                    {spec.stream: ">"},
                    count=spec.count,
                    block=spec.block_ms,
                )
            except RedisTimeoutError as exc:
                # A blocking-read timeout is the routine idle case (no entries
                # arrived within block_ms plus the socket timeout margin), not a
                # fault -- log at DEBUG so an idle worker doesn't flood WARNING.
                # Still back off + retry rather than letting it kill the loop.
                spec.logger.debug(spec.timeout_msg, exc)
                await self._sleep_or_stop(spec.backoff_s)
                continue
            except RedisConnectionError as exc:
                # A real connection fault (a Valkey failover, pod-to-pod blip):
                # transient but worth a WARNING. Back off and retry; redis-py
                # reconnects on the next attempt.
                spec.logger.warning(spec.connection_msg, exc)
                await self._sleep_or_stop(spec.backoff_s)
                continue
            if not resp:
                continue
            streams = cast("list[tuple[str, list[StreamEntry]]]", resp)
            for _stream, entries in streams:
                for entry_id, fields in entries:
                    try:
                        await handler(entry_id, fields)
                    except Exception:
                        # A handler-internal error must not escape this loop. The
                        # realistic trigger is a transient transport fault (a Valkey
                        # failover/blip) hit while a poison-pill entry is being
                        # dead-lettered via XADD to ``<stream>:dead`` (#585 widened
                        # this with a second eval dead-letter site). This loop shares
                        # one event loop with the other consumers (runs, evals,
                        # killswitch, heartbeat) under the top-level gather, so an
                        # escaping exception would tear its siblings down (#673).
                        # Log and continue: the entry is left un-acked in the PEL, so
                        # the reclaim loop re-delivers it (and dead-letters it once the
                        # delivery cap is hit) rather than being lost. ``CancelledError``
                        # is a ``BaseException`` and still propagates, so cooperative
                        # shutdown is unaffected.
                        spec.logger.exception(
                            "handler failed for entry %s on stream %s; "
                            "left pending for reclaim",
                            entry_id,
                            spec.stream,
                        )

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep until the process/generation stops or the timeout elapses."""

        process_stop = asyncio.create_task(self._stop.wait())
        generation_stop = asyncio.create_task(self._generation_stop.wait())
        try:
            await asyncio.wait(
                {process_stop, generation_stop},
                timeout=seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (process_stop, generation_stop):
                if not task.done():
                    task.cancel()
            await asyncio.gather(process_stop, generation_stop, return_exceptions=True)

    async def _sleep_generation(self, seconds: float) -> None:
        """Sleep for a lease cadence, ignoring process stop during drain."""

        try:
            await asyncio.wait_for(self._generation_stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    # -- consumer liveness + structured run generations ---------------------

    def _generation_inflight_tasks(self) -> set[asyncio.Task[None]]:
        """Tasks whose handlers own PEL entries in this generation."""

        return set()

    def _reset_generation_resources(self) -> None:
        """Subclass hook for semaphore/accounting reset after forced teardown."""

    def _reset_generation(self) -> None:
        self._generation_stop = asyncio.Event()
        self._peer_absent_since.clear()
        self._inflight_ids.clear()
        self._last_liveness_renewal = None

    def _liveness_timeout_s(self) -> float:
        # One renewal attempt may consume at most one sixth of the lease, so a
        # timed-out attempt plus a retry still fit before the pre-expiry guard.
        # The
        # 1ms floor keeps deliberately tiny integration-test leases usable.
        return max(0.001, self._spec.heartbeat_ttl_ms / 6000)

    async def _publish_liveness(self) -> None:
        if self._liveness_store is None:
            raise RuntimeError("consumer generation has no liveness store")
        async with asyncio.timeout(self._liveness_timeout_s()):
            await self._liveness_store.publish(
                stream=self._spec.stream,
                group=self._spec.group,
                consumer=self._spec.consumer,
                heartbeat_ttl_ms=self._spec.heartbeat_ttl_ms,
                capability_ttl_ms=self._spec.capability_ttl_ms,
            )
        self._last_liveness_renewal = time.monotonic()

    async def _liveness_refresh_loop(self) -> None:
        """Renew alive/capability until this run generation ends.

        One transient failure is not terminal. What matters is monotonic time
        since the last confirmed renewal: before the alive lease can silently
        expire, this task raises and structured teardown leaves owned entries in
        the PEL for a replacement generation/process.
        """

        assert self._liveness_store is not None
        ttl_s = self._spec.heartbeat_ttl_ms / 1000
        refresh_s = ttl_s / 3
        expiry_guard_s = max(0.001, ttl_s - refresh_s)
        retry_s = max(0.001, min(refresh_s / 4, 0.25))

        await self._sleep_generation(refresh_s)
        while not self._generation_stop.is_set():
            try:
                async with asyncio.timeout(self._liveness_timeout_s()):
                    await self._liveness_store.renew(
                        stream=self._spec.stream,
                        group=self._spec.group,
                        consumer=self._spec.consumer,
                        heartbeat_ttl_ms=self._spec.heartbeat_ttl_ms,
                        capability_ttl_ms=self._spec.capability_ttl_ms,
                    )
            except Exception as exc:
                # ``CancelledError`` remains a BaseException and propagates.
                last = self._last_liveness_renewal
                elapsed = float("inf") if last is None else time.monotonic() - last
                if elapsed >= expiry_guard_s:
                    raise ConsumerLivenessExpired(
                        "consumer liveness renewal could not be confirmed before "
                        f"lease expiry for {self._spec.consumer}"
                    ) from exc
                self._spec.logger.warning(
                    "consumer liveness renewal failed transiently for %s; retrying",
                    self._spec.consumer,
                    exc_info=True,
                )
                await self._sleep_generation(retry_s)
                continue
            self._last_liveness_renewal = time.monotonic()
            await self._sleep_generation(refresh_s)

    async def _cleanup_alive(self) -> None:
        if self._liveness_store is None:
            return
        try:
            await self._liveness_store.cleanup_alive(
                stream=self._spec.stream,
                group=self._spec.group,
                consumer=self._spec.consumer,
                timeout_s=self._liveness_timeout_s(),
            )
        except Exception:
            # Cleanup is best effort. The short TTL remains the authoritative
            # bound if Valkey is unavailable while this generation terminates.
            self._spec.logger.warning(
                "consumer alive-lease cleanup failed for %s; awaiting TTL",
                self._spec.consumer,
                exc_info=True,
            )

    async def _cancel_and_join(self, tasks: set[asyncio.Task[None]]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_consumer_generation(
        self,
        factories: dict[str, Callable[[], Coroutine[Any, Any, None]]],
        *,
        may_complete: frozenset[str] = frozenset(),
    ) -> None:
        """Run one supervised consumer generation with structured teardown.

        ``request_stop`` is graceful: stop/cancel new-work loops, drain existing
        handlers while the lease refresher stays alive, then remove the alive
        lease. A child/refresher failure is terminal: cancel and join every loop
        and handler, reset generation-local ownership/accounting, and re-raise so
        the process supervisor starts exactly one clean generation.
        """

        self._reset_generation()
        reserved = factories.keys() & {"bootstrap", "liveness"}
        if reserved:
            raise ValueError(f"consumer generation factories use reserved names: {reserved}")
        await self._publish_liveness()  # ordered publication precedes every read

        # A prior generation in this same pod can have been canceled after its
        # alive lease became unverifiable. Recover rows still owned by this
        # stable consumer name before reading new work. The refresher is already
        # running while eval's inline handler drains this bootstrap.
        tasks: dict[str, asyncio.Task[None]] = {}
        tasks["liveness"] = asyncio.create_task(
            self._liveness_refresh_loop(), name="consumer:liveness"
        )
        tasks["bootstrap"] = asyncio.create_task(
            self._recover_local_pending_once(), name="consumer:bootstrap"
        )
        stop_waiter = asyncio.create_task(self._stop.wait(), name="consumer:process-stop")

        failure: BaseException | None = None
        graceful = False
        try:
            while tasks:
                done, _pending = await asyncio.wait(
                    {*tasks.values(), stop_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_waiter in done:
                    graceful = True
                    break
                for name, task in list(tasks.items()):
                    if task not in done:
                        continue
                    del tasks[name]
                    try:
                        task.result()
                    except BaseException as exc:
                        failure = exc
                        break
                    if name == "bootstrap":
                        tasks.update(
                            {
                                child_name: asyncio.create_task(
                                    factory(), name=f"consumer:{child_name}"
                                )
                                for child_name, factory in factories.items()
                            }
                        )
                        continue
                    if name not in may_complete:
                        failure = RuntimeError(
                            f"consumer generation task {name!r} exited unexpectedly"
                        )
                        break
                if failure is not None:
                    break

            if graceful:
                # No new ownership after this point. Eval dispatch shields its
                # inline handler; runs handlers already live in the in-flight set.
                background = {
                    task for name, task in tasks.items() if name != "liveness"
                }
                await self._cancel_and_join(background)

                # Snapshot after producer loops are joined, so no new handler can
                # appear while graceful drain is in progress.
                inflight = self._generation_inflight_tasks()
                if inflight:
                    await asyncio.gather(*inflight, return_exceptions=True)

                self._generation_stop.set()
                liveness = tasks.get("liveness")
                if liveness is not None:
                    await asyncio.gather(liveness, return_exceptions=True)
                return

            # A terminal liveness or child failure must leave no zombie loop or
            # handler that could race the replacement generation.
            self._generation_stop.set()
            await self._cancel_and_join(set(tasks.values()))
            await self._cancel_and_join(self._generation_inflight_tasks())
            self._reset_generation_resources()
            # All old-generation work is joined. Replace the event and clear
            # ownership/absence state before the supervisor's restart backoff so
            # the next ``run()`` cannot inherit a terminal generation.
            self._reset_generation()
            if failure is None:
                failure = RuntimeError("consumer generation exited without a terminal cause")
            raise failure
        finally:
            if not stop_waiter.done():
                stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)
            await self._cleanup_alive()

    async def _xack(self, stream: str, group: str, entry_id: str) -> None:
        await self._redis.xack(stream, group, entry_id)

    async def _ack(self, entry_id: str) -> None:
        await self._redis.xack(self._spec.stream, self._spec.group, entry_id)

    async def _entry_fields(self, entry_id: str) -> dict[str, str] | None:
        """The original entry's fields, or None if it was already trimmed off the
        stream (then a metadata-only graveyard row is written)."""
        rows = await self._redis.xrange(self._spec.stream, min=entry_id, max=entry_id)
        return dict(rows[0][1]) if rows else None

    async def _dead_letter(
        self,
        entry_id: str,
        fields: dict[str, str] | None,
        *,
        reason: str,
        delivery_count: int,
    ) -> None:
        """Move an entry to the graveyard and ack it off the main group.

        ``fields`` are the original entry's fields, kept verbatim so a human or a
        replay tool can inspect them; ``None`` writes a metadata-only row (the
        entry was pending but its message had been trimmed off the stream).

        The ``dl_`` prefix is a CONVENTION, not a guarantee: the unparseable path
        accepts arbitrary malformed field maps, so an entry may already carry its
        own ``dl_original_id``. A plain copy-then-update would let the payload
        forge (or be silently clobbered by) the graveyard's own metadata. Original
        keys already starting with ``dl_`` are therefore escaped by doubling the
        prefix (``dl_reason`` -> ``dl_dl_reason``) before the metadata is written
        last. The escape is injective -- escaped keys always start with ``dl_dl_``
        and so can never equal a metadata key, and un-escaping strips exactly one
        ``dl_`` -- so the metadata always wins AND the original is fully
        recoverable.

        XADD before XACK, deliberately: a crash between the two leaves the entry
        pending, so it is re-reclaimed and re-dead-lettered -- a duplicate
        graveyard row, which is strictly safer than the XACK-first ordering's
        failure mode (a lost entry). Two replicas racing the same over-cap entry
        produce the same acceptable duplicate.

        The XADD is bounded by an approximate ``dead_letter_maxlen``, so
        graveyard rows are BEST-EFFORT: under a flood the oldest rows are evicted
        and those failures are lost. That loss is deliberate. The unparseable
        path dead-letters per inbound entry, so a wire-format drift would grow an
        unbounded graveyard at full ingest rate on the same Valkey that holds the
        kernel's locks and side-effect markers -- bounded record loss is traded
        against a platform-wide OOM. ``approximate=True`` lets Valkey trim on
        node boundaries, so the stream is bounded at *at least* the configured
        length, not exactly it.
        """
        target = self._spec.dead_letter_target
        # Escape the original's own ``dl_*`` keys (see above) so the metadata
        # written last always wins and the original stays recoverable.
        payload: dict[str, str] = {
            (f"dl_{k}" if k.startswith("dl_") else k): v for k, v in (fields or {}).items()
        }
        payload.update(
            {
                "dl_original_id": entry_id,
                "dl_delivery_count": str(delivery_count),
                "dl_reason": reason,
                "dl_dead_lettered_at": datetime.now(UTC).isoformat(),
            }
        )
        await self._redis.xadd(
            target,
            payload,
            maxlen=self._spec.dead_letter_maxlen,
            approximate=True,
        )
        self._spec.logger.error(
            self._spec.dead_letter_log,
            entry_id,
            delivery_count,
            reason,
            target,
        )
        await self._ack(entry_id)

    async def _dead_letter_over_cap(self) -> set[str]:
        """Dead-letter pending entries that have exhausted their delivery budget.

        Returns every over-cap id seen this pass -- including ones whose
        dead-letter failed -- so the caller never re-dispatches them.

        Read the delivery count with XPENDING *before* XAUTOCLAIM rather than
        after, because XAUTOCLAIM increments the counter as it claims: the
        pre-claim value is the number of deliveries ALREADY made, so an entry at
        ``>= max_delivery`` has had its full budget and must not be claimed
        again. Reading post-claim would fold in the current claim's own bump and
        kill the entry one delivery early.

        The scan PAGES THROUGH THE WHOLE pending list (``min`` advanced past the
        last id seen), because XAUTOCLAIM below pages through all of it too: a
        single ``COUNT cap_scan_page`` page would cap-check only the head of the
        list while XAUTOCLAIM happily claimed and dispatched the over-cap tail,
        so the bound would silently not hold at backlog scale.

        The IDLE filter matches XAUTOCLAIM's ``min_idle_time`` so both see the
        same candidate set and an entry that is not yet reclaim-eligible is never
        prematurely dead-lettered.

        A failure to dead-letter ONE entry is logged and isolated: it must not
        stop the other entries being cap-checked, nor XAUTOCLAIM, nor
        ``reap_orphans``. This is the first await of the maintenance tick, and an
        unguarded raise here would kill crash recovery for the whole group on
        every tick -- #505's own stall class.
        """
        over_cap: set[str] = set()
        page_size = self._spec.cap_scan_page
        cursor = "-"
        while True:
            pending = await self._redis.xpending_range(
                self._spec.stream,
                self._spec.group,
                min=cursor,
                max="+",
                count=page_size,
                idle=self._spec.reclaim_min_idle_ms,
            )
            if not pending:
                break
            for row in pending:
                entry_id = str(row["message_id"])
                # An entry in flight on THIS consumer is not an orphan: it is
                # being worked right now, and its count must not be judged. This
                # guard stays ahead of the cap check.
                if entry_id in self._inflight_ids:
                    continue
                delivered = int(row["times_delivered"])
                if delivered < self._spec.max_delivery:
                    continue
                over_cap.add(entry_id)
                try:
                    fields = await self._entry_fields(entry_id)
                    await self._dead_letter(
                        entry_id,
                        fields,
                        reason=self._spec.over_cap_reason,
                        delivery_count=delivered,
                    )
                except Exception:
                    self._spec.logger.exception(
                        self._spec.dead_letter_fail_log,
                        entry_id,
                    )
            if len(pending) < page_size:
                break
            # Exclusive lower bound: resume after the last id of this page.
            cursor = f"({pending[-1]['message_id']}"
        return over_cap

    async def _select_dead_consumer_entries(
        self, over_cap: set[str]
    ) -> list[StreamEntry]:
        """Select/transfer entries only after sustained lease absence.

        Caller holds ``_reclaim_lock``. Consumer idle is merely a cheap
        candidate threshold. A peer is proven dead only when it advertised the
        liveness protocol and its alive lease is absent on two observations at
        least one full heartbeat TTL apart. Unknown/pre-marker peers remain on
        the unchanged XAUTOCLAIM backstop.
        """

        if self._liveness_store is None:
            return []
        try:
            consumers = await self._redis.xinfo_consumers(
                self._spec.stream, self._spec.group
            )
        except ResponseError:
            self._peer_absent_since.clear()
            return []

        current_names = {str(info["name"]) for info in consumers}
        for missing in self._peer_absent_since.keys() - current_names:
            self._peer_absent_since.pop(missing, None)

        now = time.monotonic()
        required_absence_s = self._spec.heartbeat_ttl_ms / 1000
        selected: list[StreamEntry] = []
        for info in consumers:
            if self._should_stop():
                break
            name = str(info["name"])
            if (
                name == self._spec.consumer
                or int(info.get("pending") or 0) <= 0
                or int(info.get("idle") or 0) < self._spec.dead_consumer_idle_ms
            ):
                self._peer_absent_since.pop(name, None)
                continue

            try:
                capable = await self._liveness_store.is_capable(
                    stream=self._spec.stream,
                    group=self._spec.group,
                    consumer=name,
                )
                if not capable:
                    self._peer_absent_since.pop(name, None)
                    continue
                alive = await self._liveness_store.is_alive(
                    stream=self._spec.stream,
                    group=self._spec.group,
                    consumer=name,
                )
            except Exception:
                # An uncertain observation cannot contribute to proof of death.
                self._peer_absent_since.pop(name, None)
                self._spec.logger.warning(
                    "consumer liveness observation failed for %s; prompt reclaim skipped",
                    name,
                    exc_info=True,
                )
                continue

            if alive:
                self._peer_absent_since.pop(name, None)
                continue
            first_absent = self._peer_absent_since.setdefault(name, now)
            if now - first_absent < required_absence_s:
                continue
            token: str | None = None
            try:
                # Every surviving replica reaches this proof point at roughly
                # the same time. A Valkey NX lease chooses one transfer owner so
                # racing XCLAIM calls cannot burn several delivery attempts.
                token = await self._liveness_store.try_acquire_reclaim(
                    stream=self._spec.stream,
                    group=self._spec.group,
                    consumer=name,
                    ttl_ms=max(self._spec.heartbeat_ttl_ms * 2, 60_000),
                )
                if token is None:
                    continue
                # The alive lease can be republished between the observation
                # above and winning arbitration. Re-check inside the lease so a
                # stale absence never transfers work from a restarted owner.
                if await self._liveness_store.is_alive(
                    stream=self._spec.stream,
                    group=self._spec.group,
                    consumer=name,
                ):
                    self._peer_absent_since.pop(name, None)
                    continue
                selected.extend(
                    await self._claim_consumer_pending_locked(name, over_cap)
                )
            except Exception:
                self._spec.logger.exception(
                    "dead-consumer reclaim failed for %s on stream %s; left pending",
                    name,
                    self._spec.stream,
                )
            finally:
                if token is not None:
                    try:
                        await self._liveness_store.release_reclaim(
                            stream=self._spec.stream,
                            group=self._spec.group,
                            consumer=name,
                            token=token,
                        )
                    except Exception:
                        # The lease TTL is authoritative. Never discard entries
                        # already transferred merely because release failed.
                        self._spec.logger.warning(
                            "dead-consumer reclaim lease release failed for %s",
                            name,
                            exc_info=True,
                        )
        return selected

    async def _recover_local_pending_once(self) -> None:
        """Recover rows canceled by an earlier generation with this same name."""

        if self._liveness_store is None:
            raise RuntimeError("consumer liveness store is required")
        retry_s = min(max(0.001, self._spec.heartbeat_ttl_ms / 3000), 1.0)
        while not self._should_stop():
            token: str | None = None
            entries: list[StreamEntry] = []
            async with self._reclaim_lock:
                try:
                    # A peer can begin transferring this stable consumer name
                    # while the local supervisor is between generations.
                    # Bootstrap must contend on the same distributed lease or
                    # both XCLAIM calls can dispatch the row and spend two
                    # delivery attempts.
                    token = await self._liveness_store.try_acquire_reclaim(
                        stream=self._spec.stream,
                        group=self._spec.group,
                        consumer=self._spec.consumer,
                        ttl_ms=max(self._spec.heartbeat_ttl_ms * 2, 60_000),
                    )
                    if token is not None:
                        entries = await self._claim_consumer_pending_locked(
                            self._spec.consumer, set()
                        )
                finally:
                    if token is not None:
                        try:
                            await self._liveness_store.release_reclaim(
                                stream=self._spec.stream,
                                group=self._spec.group,
                                consumer=self._spec.consumer,
                                token=token,
                            )
                        except Exception:
                            self._spec.logger.warning(
                                "local pending recovery lease release failed for %s",
                                self._spec.consumer,
                                exc_info=True,
                            )
            if token is not None:
                await self._dispatch_reclaimed(entries)
                return
            await self._sleep_generation(retry_s)

    async def _claim_consumer_pending_locked(
        self, name: str, over_cap: set[str]
    ) -> list[StreamEntry]:
        """Transfer one proven-dead peer's PEL, enforcing cap before XCLAIM.

        Caller holds ``_reclaim_lock``. At/over-cap entries are dead-lettered
        directly without XCLAIM's delivery-count increment. Handler dispatch is
        returned to the caller and always occurs after the lock is released.
        """

        selected: list[StreamEntry] = []
        cursor = "-"
        page_size = self._spec.read_count
        while not self._should_stop():
            rows = await self._redis.xpending_range(
                self._spec.stream,
                self._spec.group,
                min=cursor,
                max="+",
                count=page_size,
                consumername=name,
            )
            if not rows:
                break

            claim_ids: list[str] = []
            for row in rows:
                entry_id = str(row["message_id"])
                if entry_id in self._inflight_ids or entry_id in over_cap:
                    continue
                delivered = int(row["times_delivered"])
                if delivered >= self._spec.max_delivery:
                    over_cap.add(entry_id)
                    try:
                        await self._dead_letter(
                            entry_id,
                            await self._entry_fields(entry_id),
                            reason=self._spec.over_cap_reason,
                            delivery_count=delivered,
                        )
                    except Exception:
                        self._spec.logger.exception(
                            self._spec.dead_letter_fail_log,
                            entry_id,
                        )
                    continue
                claim_ids.append(entry_id)

            if claim_ids:
                claimed = await self._redis.xclaim(
                    self._spec.stream,
                    self._spec.group,
                    self._spec.consumer,
                    0,
                    claim_ids,
                )
                for entry_id, fields in cast("list[StreamEntry]", claimed or []):
                    if entry_id in self._inflight_ids or entry_id in over_cap:
                        continue
                    selected.append((entry_id, dict(fields)))
            if len(rows) < page_size:
                break
            cursor = f"({rows[-1]['message_id']}"
        return selected

    async def _dispatch_reclaimed(self, entries: list[StreamEntry]) -> int:
        dispatched = 0
        for entry_id, fields in entries:
            if self._should_stop():
                break
            if entry_id in self._inflight_ids:
                continue
            await self._spec.handler(entry_id, fields)
            dispatched += 1
        return dispatched

    async def _reclaim_dead_consumers(self) -> int:
        """Prompt-only compatible-peer reclaim, with dispatch outside lock."""

        async with self._reclaim_lock:
            entries = await self._select_dead_consumer_entries(set())
        return await self._dispatch_reclaimed(entries)

    async def _prompt_reclaim_once(self) -> int:
        """Observe capable peers and promptly recover only proven-dead owners."""

        return await self._reclaim_dead_consumers()

    async def _prompt_reclaim_loop(self) -> None:
        """Dedicated lease-observation cadence, isolated from heavy maintenance."""

        interval_s = self._spec.heartbeat_ttl_ms / 3000
        while not self._should_stop():
            try:
                await self._prompt_reclaim_once()
            except Exception:
                self._spec.logger.exception(
                    "prompt consumer reclaim tick failed on stream %s",
                    self._spec.stream,
                )
            await self._sleep_or_stop(interval_s)

    async def _reclaim_once(self) -> int:
        """Reclaim entries pending too long from any (dead) consumer and retry.

        Entries that have already exhausted their delivery budget are
        dead-lettered first, so they are never claimed or re-dispatched again.
        XAUTOCLAIM still claims an over-cap entry whose dead-letter failed (it is
        still pending), so the ids it reports are skipped rather than dispatched:
        the cap binds even when the graveyard is unwritable.

        The prompt capable-peer path shares this selection lock, while unknown
        peers retain this 15-minute compatibility backstop unchanged.
        """
        selected: list[StreamEntry] = []
        async with self._reclaim_lock:
            over_cap = await self._dead_letter_over_cap()
            selected.extend(await self._select_dead_consumer_entries(over_cap))
            cursor: str = "0-0"
            while not self._should_stop():
                raw = await self._redis.xautoclaim(
                    self._spec.stream,
                    self._spec.group,
                    self._spec.consumer,
                    min_idle_time=self._spec.reclaim_min_idle_ms,
                    start_id=cursor,
                    count=self._spec.read_count,
                )
                cursor = str(raw[0])
                entries = cast("list[StreamEntry]", raw[1])
                for entry_id, fields in entries:
                    if entry_id in self._inflight_ids:
                        continue  # still being processed here; not an orphan
                    if entry_id in over_cap:
                        continue  # budget spent; never dispatch it again
                    selected.append((entry_id, fields))
                if cursor in ("0-0", "0"):
                    break
        return await self._dispatch_reclaimed(selected)
