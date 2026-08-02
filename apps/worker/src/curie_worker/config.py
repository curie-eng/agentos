"""Typed configuration for the worker kernel, read from the environment.

The kernel needs a Valkey connection (the stream it consumes plus its locks and
markers), a Slack bot token (to edit the placeholder in place), the stream and
consumer-group identity, and the tunables for retry, per-thread locking, and
crash-recovery reclaim. Substrate wiring (namespace, warm pool, runner port)
lives in ``SubstrateConfig`` and is assembled separately by the entrypoint.

``WorkerConfig`` is a ``pydantic_settings.BaseSettings`` (the house pattern, see
``apps/api``): construct it with no arguments and it reads the environment on
init, falling back to the defaults below for anything absent. The CURIE_-prefixed
knobs map through per-field ``validation_alias``; the rest read the uppercased
field name (VALKEY_HOST, DATABASE_URL, S3_ENDPOINT_URL, LANGFUSE_HOST, ...).
"""

from __future__ import annotations

import os
import socket
from typing import Annotated

from aci_protocol.service_config import (
    API_KEY_ENV,
    DEAD_LETTER_STREAM_ENV,
    EVAL_CONSUMER_GROUP_DEFAULT,
    EVAL_STREAM_DEFAULT,
    HEARTBEAT_FILE_ENV,
    HEARTBEAT_INTERVAL_ENV,
    RUNS_STREAM_DEFAULT,
    SHIMMER_ENV,
    STREAM_ENV,
    WORKER_GROUP_DEFAULT,
    AliasOnlyEnvSource,
    api_url_validation_alias,
    derive_dead_letter_stream_name,
    warn_if_deprecated_api_url_env,
)
from pydantic import BeforeValidator, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import (
    PydanticBaseSettingsSource,
)


def _default_consumer_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _parse_bool(value: object) -> bool:
    """Parse the truthy env-string set the worker has always accepted.

    A real bool passes through (so kwarg construction in tests is unchanged); any
    other string is truthy only when it is one of the accepted tokens, matching
    the previous hand-rolled ``_b`` (note: the worker does not treat "on" as
    truthy, unlike the dispatcher).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


Bool = Annotated[bool, BeforeValidator(_parse_bool)]


class WorkerConfig(BaseSettings):
    """Everything the kernel needs, in one typed object."""

    model_config = SettingsConfigDict(frozen=True, populate_by_name=True, extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Swap the env source so aliased fields read only their alias."""
        # Surface the CURIE_API_BASE_URL -> CURIE_API_URL rename (#496) at the
        # single point every WorkerConfig load passes through.
        warn_if_deprecated_api_url_env()
        return (
            init_settings,
            AliasOnlyEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    # Valkey
    valkey_host: str = "localhost"
    valkey_port: int = 6379
    valkey_password: str = ""
    valkey_db: int = 0

    # Slack
    slack_bot_token: str = ""
    # The worker's DEFAULT Slack Web API base URL: the endpoint used to finalize a
    # turn whose reply handle carries no per-turn endpoint (issue #19). Unset = the
    # real Slack API. A turn that carries its own reply endpoint (e.g. a CLI stub)
    # overrides this per turn, so a real workspace and a no-Slack CLI stub can
    # coexist on one worker instead of contending for this single setting.
    slack_api_base_url: str = ""

    # Postgres (read-only): resolve channel -> agent -> deployment -> version.
    # Matches the API's DATABASE_URL / DB_SCHEMA so the worker reads the same DB.
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:25432/postgres"
    db_schema: str = "curie"

    # Deployment-to-runtime binding. The plugin dir is the local path the runner
    # reads; sandbox provisioning fetches CURIE_BUNDLE_REF into it. Platform
    # default budget applies when an agent's budget columns are NULL.
    bundle_plugin_dir: str = Field(default="/bundles/current", validation_alias="CURIE_PLUGIN_DIR")
    default_max_usd_per_day: float = 10.0
    default_max_output_tokens_per_run: int = 100000

    # Runner model + credentials passthrough (injected per-claim into every boot
    # env). fake_model runs the runner's canned FakeModelSession (no Anthropic
    # call, no credential needed) -- the middle-mode default on a laptop.
    # credentials is the opaque CURIE_CREDENTIALS the runner forwards to the
    # model call; it is never logged and never required when fake_model is on.
    fake_model: Bool = Field(default=False, validation_alias="CURIE_FAKE_MODEL")
    credentials: str = Field(default="", validation_alias="CURIE_CREDENTIALS")
    # Local model demo path: the worker can point the runner at an
    # Anthropic-compatible local endpoint without changing the fake-model default.
    model_base_url: str = Field(default="", validation_alias="CURIE_MODEL_BASE_URL")
    # The endpoint's wire protocol and the env var(s) carrying the credential
    # (#514), both declared rather than inferred. Operator scope like
    # model_base_url: they select which env var a credential is read from and
    # which wire protocol is dialed, so an agent author must never set them.
    model_api_backend: str = Field(default="", validation_alias="CURIE_MODEL_API_BACKEND")
    model_env_key: str = Field(default="", validation_alias="CURIE_MODEL_ENV_KEY")
    model: str = Field(default="", validation_alias="CURIE_MODEL")

    # Opt-in false-completion check (#517, #669), operator scope like
    # model_api_backend/model_env_key: forwarded verbatim into every boot env as
    # CURIE_FALSE_COMPLETION_CHECK, never derived from the agent row. The
    # runner reads it as a direct env var outside the frozen BootEnv contract
    # (runner/src/curie_runner/config.py) since it is authority-free and
    # observe-only; this field is the missing operator-facing producer that
    # closes the loop -- without it the runner-side read is unreachable in any
    # deployed sandbox (#669). Default off preserves current behavior.
    false_completion_check: Bool = Field(
        default=False, validation_alias="CURIE_FALSE_COMPLETION_CHECK"
    )

    # Where this release's hosted connectors live (ADR-0086, #1118). The runner
    # derives each declared connector's MCP URL from the Service Curie created,
    # `<release>-<agent>-mcp-<connector>` in `<namespace>`, and neither value is
    # knowable inside the sandbox: the Helm release name lives with whoever ran
    # `cluster up`. Both default empty, and the boot env carries the connector
    # scope only when the whole set resolves -- so a worker that predates the
    # chart plumbing simply mounts no hosted connector rather than emitting a
    # scope that names a Service which cannot exist.
    connector_release: str = Field(default="", validation_alias="CURIE_RELEASE")
    connector_namespace: str = Field(default="", validation_alias="CURIE_NAMESPACE")

    # When true, clear the Slack assistant-thread status (the "shimmer" the
    # dispatcher set) once a turn ends -- editing the placeholder does not
    # auto-clear it. Off by default; pairs with the dispatcher's CURIE_SHIMMER.
    shimmer: Bool = Field(default=False, validation_alias=SHIMMER_ENV)

    # When true, suppress intermediate placeholder edits while streaming so the
    # placeholder gets exactly one chat.update (the final) -- rate-limit friendly
    # and flicker-free; pair with the shimmer for liveness. Default false
    # preserves live-edit streaming.
    slack_no_edit_streaming: Bool = Field(
        default=False, validation_alias="CURIE_SLACK_NO_EDIT_STREAMING"
    )

    # Shown by editing the dispatcher's placeholder to a "booting" state before the
    # sandbox claim, so the cold-boot wait is not silent. Best-effort; overridable.
    # Kept free of internal implementation vocabulary ("runner", "sandbox") by
    # default (#717) -- an end user talking to an agent should never see curie's
    # own architecture terms in a status line.
    booting_text: str = Field(
        default="Working on it...",
        validation_alias="CURIE_BOOTING_TEXT",
    )

    # Stream / consumer group (must match the dispatcher's CURIE_STREAM). The
    # defaults are the shared declarations (#492) so a rename cannot drift this
    # lane out of sync with the dispatcher/API/CLI; the validation_alias keeps
    # #496's env-override path intact.
    stream: str = Field(default=RUNS_STREAM_DEFAULT, validation_alias=STREAM_ENV)
    consumer_group: str = Field(
        default=WORKER_GROUP_DEFAULT, validation_alias="CURIE_CONSUMER_GROUP"
    )
    consumer_name: str = Field(
        default_factory=_default_consumer_name,
        validation_alias="CURIE_CONSUMER_NAME",
    )

    # Delivery cap + dead-letter graveyard (#505). ``max_delivery`` is the maximum
    # number of times a stream entry may be DELIVERED to a handler before it is
    # moved to the dead-letter stream and acked off the group. It is NOT
    # ``max_attempts`` below: that governs the kernel's flag-clean per-turn retry
    # *classification* (a completely different mechanism operating inside a single
    # delivery). Conflating the two silently changes kernel retry behavior.
    #
    # The count is read from Valkey's pending-entries list, so it is durable: a
    # restarted worker sees the accumulated count and still caps. The floor is 2
    # because ``max_delivery=1`` would dead-letter every ordinary worker crash on
    # its first reclaim, and values below 3 undermine ADR-0013 crash recovery
    # (which relies on a reclaim actually retrying the entry).
    #
    # Leave headroom above what a HEALTHY turn can legitimately burn. A single
    # delivery may span up to ``max_attempts * runner_total_timeout_s`` (~1800s
    # at defaults, see the retry knobs below), which exceeds
    # ``reclaim_min_idle_ms`` (900s), so another replica can reclaim a turn that
    # is still working and bump its delivery count. A healthy long turn can
    # therefore accrue roughly
    # ``(max_attempts * runner_total_timeout_s) / reclaim_min_idle_ms`` (~2 at
    # defaults) deliveries on its own. ``max_delivery`` must stay comfortably
    # above that: at the ``ge=2`` floor a slow but healthy turn could be
    # dead-lettered while it is still making progress.
    max_delivery: int = Field(default=5, ge=2, validation_alias="CURIE_MAX_DELIVERY")
    # Empty means "derive ``<stream>:dead``" at the use site; a static Field
    # default cannot reference ``self.stream``. An explicit override equal to
    # ``stream`` is rejected outright -- see ``_reject_self_targeting_graveyard``.
    dead_letter_stream: str = Field(default="", validation_alias=DEAD_LETTER_STREAM_ENV)
    # The graveyard is capped with an approximate MAXLEN on every XADD. The
    # unparseable-poison path dead-letters per INBOUND entry, so a wire-format
    # drift that makes entries unparseable en masse would otherwise grow the
    # graveyard at full ingest rate -- on the same Valkey that holds the kernel's
    # per-thread locks and side-effect markers, i.e. a platform-wide OOM. The
    # trade is deliberate and lossy: under a flood the oldest dead-letter rows are
    # evicted, so graveyard records are best-effort, not a durable audit log.
    dead_letter_maxlen: int = Field(
        default=10000, ge=1, validation_alias="CURIE_DEAD_LETTER_MAXLEN"
    )

    @model_validator(mode="after")
    def _reject_self_targeting_graveyard(self) -> WorkerConfig:
        """Fail at construction if the graveyard points back at the source stream.

        ``_dead_letter`` XADDs the original payload to the dead-letter stream and
        only then XACKs it. If that target IS the source stream, the payload is
        re-queued to the very stream it was consumed from: a valid failure gets
        re-consumed under a fresh entry id, and an unparseable one forms a hot
        loop that re-creates the permanent stall the delivery cap exists to
        prevent. Rejecting at config/startup means an operator learns at boot
        rather than during an incident; the derived ``<stream>:dead`` default can
        never collide, so only an explicit override trips this.
        """
        if self.dead_letter_stream and self.dead_letter_stream == self.stream:
            raise ValueError(
                "CURIE_DEAD_LETTER_STREAM must not equal CURIE_STREAM "
                f"({self.stream!r}): dead-lettering onto the source stream "
                "re-queues failures forever"
            )
        return self

    @model_validator(mode="after")
    def _connector_reconciler_knows_where_it_is(self) -> WorkerConfig:
        """Fail at construction if the reconciler is on but unaddressed.

        Connector object names are built from the release name and the app name
        (`<release>-<agent>-mcp-<connector>`). With either missing, every name
        the reconciler renders differs from the names that actually exist: it
        would find nothing of its own in the namespace, create a parallel set
        under wrong names, and leave the real connectors unmanaged. That is
        silent and looks like it is working, so it is rejected at boot instead.
        """

        if not self.connector_reconcile_enabled:
            return self
        missing = [
            env
            for env, value in (
                ("CURIE_NAMESPACE", self.connector_namespace),
                ("CURIE_RELEASE", self.connector_release),
                ("CURIE_CONNECTOR_APP_NAME", self.connector_app_name),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(
                f"CURIE_CONNECTOR_RECONCILE is on but {', '.join(missing)} "
                "is unset; connector object names are derived from these, so "
                "the reconciler would manage a parallel set under wrong names"
            )
        return self

    # Read loop
    read_count: int = 16
    read_block_ms: int = 5000

    # Per-thread lock (serializes the routing decision + turn opening across
    # workers so a thread never has two live sessions). The TTL must exceed the
    # worst-case critical section (a cold claim can take up to the substrate's
    # claim_timeout, default 90s, now bounded end-to-end across the claim's bind
    # and serviceFQDN phases by a single shared deadline in
    # SandboxSubstrate._claim_fresh) so the lock never lapses mid-section and lets
    # a second worker open a concurrent turn. 90s claim + slack/route overhead
    # stays safely under this 120s TTL; if you raise claim_timeout keep it below
    # this.
    lock_ttl_ms: int = 120000
    lock_acquire_timeout_s: float = 45.0
    lock_poll_interval_s: float = 0.02

    # Retry (flag-clean failures only; see the no-retry-after-side-effects rule)
    max_attempts: int = Field(default=3, validation_alias="CURIE_MAX_ATTEMPTS")
    retry_backoff_base_s: float = Field(default=1.0, gt=0)
    retry_backoff_max_s: float = Field(default=20.0, gt=0)

    # Markers
    idempotency_ttl_s: int = 86400

    # Crash recovery: reclaim stream entries pending longer than this, and run
    # the orphan-claim reaper, on this cadence.
    #
    # This window does NOT cover the longest legitimate in-flight time. One
    # delivery is not one runner call: the kernel may retry a flag-clean failure
    # up to max_attempts (3) times WITHIN a single delivery, each bounded by
    # runner_total_timeout_s (600s), so a healthy delivery can legitimately span
    # up to ~max_attempts * runner_total_timeout_s = ~1800s -- twice this 900s
    # idle threshold. A long healthy turn can therefore be reclaimed by another
    # replica and accrue delivery count (see max_delivery's headroom note above).
    # The consumer skips its OWN in-flight entry ids, so this only bites across
    # replicas; that cross-replica dup-dispatch is pre-existing and tracked
    # separately. Raising this threshold past
    # max_attempts * runner_total_timeout_s would close it at the cost of slower
    # crash recovery.
    reclaim_min_idle_ms: int = 900000
    reclaim_interval_s: float = 30.0

    # Slack placeholder edits are throttled to avoid rate limits while streaming.
    slack_edit_min_interval_s: float = 0.7

    # Runner HTTP timeouts
    runner_connect_timeout_s: float = 10.0
    runner_total_timeout_s: float = 600.0

    # Eval stream (F3): a separate consumer group on curie:evals runs eval
    # suites and reports results to the platform API and Langfuse.
    eval_stream: str = Field(default=EVAL_STREAM_DEFAULT, validation_alias="CURIE_EVAL_STREAM")
    eval_consumer_group: str = Field(
        default=EVAL_CONSUMER_GROUP_DEFAULT,
        validation_alias="CURIE_EVAL_CONSUMER_GROUP",
    )
    eval_consumer_name: str = Field(default_factory=_default_consumer_name)
    # Upper bound on eval SandboxClaims being created/bound CONCURRENTLY in this
    # worker (#709). Eval jobs are handled by two coroutines racing under one
    # gather -- the blocking read loop and the crash-recovery reclaim loop -- and
    # each provisions its own sandbox, so without a bound they can create claims
    # faster than a small cluster binds them. On a single-node k3s cluster that
    # storm is the observed failure: claims never bind within claim_timeout and
    # claim reads return 504. The default of 1 is single-node-safe (a second claim
    # is not created until the first has bound), giving sequential-with-backpressure
    # claim creation; a multi-node cluster raises this to admit real parallelism.
    # The bound covers only the create/bind phase (the flood source), not the whole
    # suite run: once a claim binds the slot frees so the next claim can begin while
    # the bound sandbox runs its cases. Floor 1 -- 0 would create no claims at all.
    eval_max_concurrent_claims: int = Field(
        default=1, ge=1, validation_alias="CURIE_EVAL_MAX_CONCURRENT_CLAIMS"
    )
    # On first creation the eval group starts at (now - this window) rather than
    # the stream head, so a backlog of ancient entries is not replayed en masse
    # on boot. Recent entries (younger than the window) are still delivered, so a
    # short outage never drops a live eval; only long-dead entries are skipped.
    eval_stream_max_age_hours: int = Field(
        default=24, validation_alias="CURIE_EVAL_STREAM_MAX_AGE_HOURS"
    )
    # MinIO / S3 for plugin bundles (mirrors the API's env names). The consumer
    # fetches a version's bundle by bundle_ref and loads its evals/ suite.
    s3_endpoint_url: str = "http://localhost:29000"
    s3_access_key: str = "minio"
    s3_secret_key: str = "miniosecret"
    s3_region: str = "us-east-1"
    bundle_bucket: str = "curie-bundles"
    # Bundle extraction bounds (ADR-0059 decision 3), applied by the Docker
    # substrate's claim-time bundle-fetch (`sandbox/docker.py`'s
    # `_prepare_bundle` -> `bundle_store.extract_bundle`) and the eval-stream
    # suite loader (`eval/stream.py`'s `load_suite_from_bundle`), both of which
    # route through `plugin_format.safe_extract`. Mirrors the API's `Settings`
    # field names/defaults (apps/api/src/curie_api/config.py) -- the same
    # stored bytes get the same caps regardless of which lane fetches them, so
    # keep the two in sync (a parity seam per AGENTS.md). The Kubernetes
    # substrate fetches/extracts via shell init containers, not this Python
    # path, so this does not reach it; see ADR-0059 decision 3's note that the
    # API-side bound is substrate-neutral, while extraction itself is not on
    # every substrate.
    bundle_max_uncompressed_bytes: int = 1024 * 1024 * 1024  # 1 GiB
    bundle_max_compression_ratio: float = 100.0
    # Member-count cap, enforced incrementally during the pre-scan (#815) so a
    # many-member archive is refused mid-walk. Mirrors the API's Settings field.
    bundle_max_members: int = 10_000
    # Platform API for POST /evals/report. Defaults match the API's dev stack
    # (README serves it on :8000; its shared dev key is curie-dev-key).
    api_base_url: str = Field(
        default="http://localhost:8000", validation_alias=api_url_validation_alias()
    )
    # The API base a SPAWNED RUNNER dials, distinct from api_base_url above, which
    # is the worker's OWN self-dial URL (its /evals/report, binding resolve). The
    # two diverge whenever the worker and the runner sit on different networks:
    # in the docker substrate the worker runs host-net and reaches the API at the
    # published localhost port, but the runner container it spawns joins the
    # bridge runner network and can only reach the API by its in-network service
    # name (compose: http://curie-api:8000). CURIE_MEMORY_REF/CURIE_HISTORY_REF
    # are minted for the runner, so they must be built from THIS base, not the
    # worker's localhost self-dial (#678). Empty means "not split out": the ref
    # falls back to api_base_url, byte-identical to the pre-#678 behavior. That
    # fallback is correct wherever the worker's own api_base_url is already
    # runner-reachable; it does NOT by itself make an unreachable api_base_url
    # reachable. The k8s substrate is a separate case -- a default chart install
    # wires neither CURIE_API_URL nor this on the worker, so its runner state
    # refs are the same localhost gap in the opposite substrate, out of #678's
    # docker scope -- see runner_facing_api_base_url.
    runner_api_base_url: str = Field(default="", validation_alias="CURIE_RUNNER_API_URL")
    api_key: str = Field(default="curie-dev-key", validation_alias=API_KEY_ENV)
    # ---------------------------------------------------------------------
    # Connector reconciler (ADR-0090, #1184). Off by default: enabling it is
    # what makes the worker's Role grant create/patch/delete on Deployments,
    # Services, Secrets and NetworkPolicies, and the chart gates that grant on
    # the same flag. A worker that is not reconciling should not hold it.
    # ---------------------------------------------------------------------
    connector_reconcile_enabled: bool = Field(
        default=False, validation_alias="CURIE_CONNECTOR_RECONCILE"
    )
    connector_reconcile_interval_s: float = Field(
        default=60.0, gt=0, validation_alias="CURIE_CONNECTOR_RECONCILE_INTERVAL_S"
    )
    # The reconciler reuses `connector_release` / `connector_namespace` above --
    # deliberately the same two values the runner's connector scope is built
    # from. They must agree: the runner dials a Service by the name those
    # produce, and the reconciler creates the Service by the same name. Two
    # separate settings could disagree, and the symptom would be a connector
    # that exists and an agent that cannot reach it.
    #
    # `app_name` is the one thing neither of them already needs: it is the
    # chart's nameOverride, and it appears only in the pod selector the
    # NetworkPolicy matches on.
    connector_app_name: str = Field(default="", validation_alias="CURIE_CONNECTOR_APP_NAME")
    report_max_attempts: int = 3
    report_backoff_base_s: float = Field(default=0.5, gt=0)
    # Langfuse for recording eval scores (the matrix reads them back by version).
    langfuse_host: str = "http://localhost:23000"
    langfuse_public_key: str = "pk-lf-curie-dev"
    langfuse_secret_key: str = "sk-lf-curie-dev"

    # The worker's asyncio loop touches heartbeat_file every heartbeat_interval_s
    # so an exec liveness probe can restart a pod whose event loop has wedged.
    heartbeat_file: str = Field(
        default="/tmp/curie-worker.heartbeat",
        validation_alias=HEARTBEAT_FILE_ENV,
    )
    heartbeat_interval_s: float = Field(default=10.0, validation_alias=HEARTBEAT_INTERVAL_ENV)

    key_prefix: str = "curie:worker"

    @property
    def runner_facing_api_base_url(self) -> str:
        """The API base a spawned runner dials, falling back to the self-dial URL.

        ``runner_api_base_url`` overrides only when the runner sits on a different
        network than the worker (the docker substrate: host-net worker, bridge-net
        runner). When it is unset this falls back to api_base_url, byte-identical
        to the pre-#678 behavior -- correct wherever the worker's own api_base_url
        is already runner-reachable, but not itself a fix for a substrate where it
        is not (see the field comment on the k8s gap). Callers minting
        runner-facing refs (CURIE_MEMORY_REF / CURIE_HISTORY_REF) read this,
        never api_base_url directly (#678).
        """
        return self.runner_api_base_url or self.api_base_url

    @property
    def valkey_socket_timeout_s(self) -> float:
        """Socket read timeout for the Valkey clients, kept above the block interval.

        An idle blocking XREADGROUP blocks server-side for ``read_block_ms`` and
        then returns an empty reply, but redis-py enforces the client
        ``socket_timeout`` on that same read (its default is 5s). If the socket
        timeout is not longer than the block, the socket read deadline fires at
        the exact moment the block would return empty, so every idle cycle raises
        a read timeout instead of returning empty and floods the logs. The extra
        headroom past ``read_block_ms`` covers pod-to-pod RTT and Valkey
        processing after the block elapses. Genuine connection blips still raise
        and are logged as real transport errors.
        """
        return self.read_block_ms / 1000 + 5.0

    def done_key(self, event_id: str) -> str:
        return f"{self.key_prefix}:done:{event_id}"

    def side_effect_key(self, event_id: str) -> str:
        return f"{self.key_prefix}:sidefx:{event_id}"

    def lock_key(self, thread_key: str) -> str:
        return f"{self.key_prefix}:lock:{thread_key}"

    def approval_card_key(self, thread_key: str) -> str:
        # Where a suspended thread's posted approval card lives, so an expiry can
        # disable it (#419). Keyed by thread: one pending approval per suspended
        # thread at a time.
        return f"{self.key_prefix}:approval-card:{thread_key}"

    def dead_letter_stream_name(self) -> str:
        """The graveyard stream: the explicit override, else derived ``<stream>:dead``.

        ``dead_letter_stream``'s Field default cannot reference ``self.stream``,
        so the derivation lives here rather than at the use site -- next to the
        other derived names, and next to ``_reject_self_targeting_graveyard``,
        which reasons about the same name. The derivation itself now lives in the
        shared ``derive_dead_letter_stream_name`` (#668) so the API's watcher and
        this writer can never drift on the name.
        """
        return derive_dead_letter_stream_name(self.stream, self.dead_letter_stream)

    def eval_dead_letter_stream_name(self) -> str:
        """The eval lane's graveyard: ``<eval_stream>:dead`` (#535).

        Derived from ``eval_stream``, NOT ``dead_letter_stream_name()`` (which is
        keyed to the runs ``stream``): the eval lane runs its own delivery cap, so
        a permanently-failing eval must dead-letter to its own graveyard rather
        than the runs graveyard. Always derived, so it can never collide with
        ``eval_stream`` and needs no self-targeting validator.
        """
        return f"{self.eval_stream}:dead"
