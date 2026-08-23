"""FastAPI application factory and shared-resource lifespan.

The engine, sessionmaker, httpx client, and Langfuse client are created once at
startup and stored on app.state; dependencies (deps.py) read them per request.
"""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import redis.asyncio as redis
from curie_telemetry import configure, extract_http_trace_context
from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from .commitpoller import CommitPoller, GitHubBranchTip
from .config import get_settings
from .db import create_engine, create_sessionmaker
from .evalqueue import EvalQueue
from .github_app import credentials_for, log_credential_path
from .github_checks import GitHubStatusReporter
from .graveyardwatcher import GraveyardWatcher
from .k8s import build_lazy_pod_lister, build_lazy_pod_log_reader
from .killswitch import KillSwitch
from .langfuse import LangfuseClient
from .resumequeue import ResumeQueue
from .resumereconciler import ResumeReconciler
from .routers import (
    agents,
    approvals,
    bundles,
    channels,
    config,
    console,
    control,
    deploy_targets,
    deployments,
    evals,
    github,
    hooks,
    memory,
    observability,
    runs,
    state,
)
from .slack_approvers import SlackApproverSetSelector
from .slack_usergroups import SlackUserGroupClient
from .storage import BundleStore
from .sweeper import run_expiry_sweeper
from .threadreset import ThreadResetRequests


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    engine = create_engine()
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    http_client = httpx.AsyncClient(timeout=10.0)
    app.state.http_client = http_client
    app.state.langfuse = LangfuseClient(settings, http_client)
    store = BundleStore(settings)
    await store.ensure_bucket()
    app.state.bundle_store = store
    # Lazy: resolving the cluster/credentials is deferred to the first pod-log
    # read so an absent/expired credential does not surface as a boot-time ERROR
    # for a proxy most runs never touch.
    app.state.pod_log_reader = build_lazy_pod_log_reader(settings.kube_config_path)
    app.state.pod_lister = build_lazy_pod_lister(settings.kube_config_path)
    valkey: redis.Redis = redis.from_url(settings.valkey_dsn())
    app.state.valkey = valkey
    app.state.kill_switch = KillSwitch(valkey)
    app.state.thread_reset_requests = ThreadResetRequests(valkey)
    app.state.eval_queue = EvalQueue(valkey)
    # resume_dead_letter_stream stays the narrower override that wins when set;
    # its fallback is now the unified graveyard name (which honors
    # CURIE_DEAD_LETTER_STREAM / CURIE_STREAM via the shared derivation, #668)
    # instead of letting ResumeQueue re-derive `<runs_stream>:dead` from the base
    # stream alone -- so the backstop scans the SAME graveyard the worker writes.
    app.state.resume_queue = ResumeQueue(
        valkey,
        stream=settings.runs_stream,
        dead_letter_stream=settings.resume_dead_letter_stream or settings.dead_letter_stream_name(),
        tracer=app.state.telemetry.tracer,
    )
    # The composition root for approvals (#420, ADR-0034): the only place that
    # names Slack to build the approver-set selector, so the authorizer and the
    # resolve endpoint depend on ports rather than on a provider. The usergroup
    # client shares the app's httpx client and is None when no bot token is
    # configured, which is the normal Slack-free deployment -- a route that
    # declares an approvers group then fails closed at resolve time rather than
    # silently widening.
    usergroups = (
        SlackUserGroupClient(
            http_client,
            token=settings.slack_bot_token,
            ttl_s=settings.slack_usergroup_cache_ttl_s,
        )
        if settings.slack_bot_token
        else None
    )
    app.state.approver_sets = SlackApproverSetSelector(usergroups)
    app.state.github_reporter = GitHubStatusReporter(
        http_client,
        api_url=settings.github_api_url,
        token=settings.github_token,
        context=settings.eval_check_context,
    )
    # The resume reconciler (#411) backstops a failed inline resume enqueue by
    # periodically re-enqueuing owed wakes. It enqueues via resume_queue (which
    # uses the valkey client), so it is cancelled BEFORE valkey.aclose() below.
    reconciler = ResumeReconciler(
        app.state.sessionmaker,
        app.state.resume_queue,
        interval_seconds=settings.resume_reconciler_interval_seconds,
        grace_seconds=settings.resume_reconciler_grace_seconds,
        batch_limit=settings.resume_reconciler_batch_limit,
        dead_letter_scan_limit=settings.resume_dead_letter_scan_limit,
    )
    app.state.resume_reconciler = reconciler
    app.state.resume_reconciler_task = (
        asyncio.create_task(reconciler.run_forever())
        if settings.resume_reconciler_enabled
        else None
    )
    # The expiry sweeper (#412) flips lapsed pending approvals and resumes their
    # stranded sessions. It shares this lifecycle's resources (sessionmaker,
    # resume_queue); interval <= 0 disables it (no task started).
    sweeper_stop: asyncio.Event | None = None
    if settings.approval_sweep_interval_s > 0:
        sweeper_stop = asyncio.Event()
        app.state.sweeper_task = asyncio.create_task(
            run_expiry_sweeper(
                app.state.sessionmaker,
                app.state.resume_queue,
                settings.approval_sweep_interval_s,
                sweeper_stop,
            )
        )
    else:
        app.state.sweeper_task = None
    # The dead-letter graveyard watcher (#531): read-only reader on
    # <runs_stream>:dead that alerts on each new dead-letter. Interval <= 0
    # disables it. Read-only, so it needs no ordering vs valkey.aclose() beyond
    # being cancelled before it.
    if settings.dead_letter_watch_interval_s > 0:
        watcher = GraveyardWatcher(
            valkey,
            stream=settings.dead_letter_stream_name(),
            interval_seconds=settings.dead_letter_watch_interval_s,
        )
        app.state.graveyard_watcher = watcher
        app.state.graveyard_watcher_task = asyncio.create_task(watcher.run_forever())
    else:
        app.state.graveyard_watcher_task = None
    # Commit polling (#1239): the deploy path for a cluster that cannot receive
    # a GitHub webhook. Interval <= 0 disables it, which is the default -- an
    # install whose webhook works needs nothing here, and polling would only
    # add GitHub API calls. It reuses the credential resolver and the ordinary
    # process_push, so it cannot disagree with the webhook about what a push
    # means.
    if settings.commit_poll_interval_s > 0:
        poller = CommitPoller(
            session_factory=app.state.sessionmaker,
            store=app.state.bundle_store,
            settings=settings,
            eval_queue=app.state.eval_queue,
            tips=GitHubBranchTip(settings, credentials_for(settings)),
            interval_seconds=settings.commit_poll_interval_s,
        )
        app.state.commit_poller = poller
        app.state.commit_poller_task = asyncio.create_task(poller.run_forever())
    else:
        app.state.commit_poller_task = None
    try:
        yield
    finally:
        # Both background loops enqueue via resume_queue (which uses the valkey
        # client) and read via the sessionmaker, so both are stopped BEFORE
        # valkey.aclose()/engine.dispose() below.
        task = getattr(app.state, "resume_reconciler_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Stop the sweeper BEFORE closing valkey/engine so an in-flight pass does
        # not race the closed clients. The wait-first loop wakes immediately on
        # stop.set(); wait_for already cancels on timeout, so suppressing
        # TimeoutError/CancelledError is the whole teardown.
        if sweeper_stop is not None:
            sweeper_stop.set()
            try:
                await asyncio.wait_for(app.state.sweeper_task, 5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
        # Read-only, so simply cancel it before closing valkey.
        watcher_task = getattr(app.state, "graveyard_watcher_task", None)
        if watcher_task is not None:
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass
        # Cancelled before engine.dispose(): a poll pass mid-deploy holds a
        # session, and disposing the engine underneath it would raise on the
        # way out rather than shutting down cleanly.
        poller_task = getattr(app.state, "commit_poller_task", None)
        if poller_task is not None:
            poller_task.cancel()
            try:
                await poller_task
            except asyncio.CancelledError:
                pass
        try:
            await valkey.aclose()
            await http_client.aclose()
            await engine.dispose()
        finally:
            app.state.telemetry.shutdown(timeout_millis=2000)


def configure_logging(level: str | None = None) -> logging.Logger:
    """Give this service's loggers a level and a handler (#1270).

    `uvicorn curie_api.main:app` configures only the `uvicorn*` loggers and no
    root entry, so `curie_api.*` has an effective level of WARNING and no
    handler -- every INFO record in the service is dropped by the last-resort
    handler. That is production, not a local artifact: the Dockerfile runs bare
    uvicorn.

    Scoped to the `curie_api` logger rather than root, and `propagate` is off:
    configuring root would fight uvicorn's own dictConfig and anything the OTel
    wiring later attaches, and leaving propagation on would double-emit every
    record through whatever root ends up with.

    Idempotent. Tests construct many apps in one process, and a handler added
    per construction would multiply every line.
    """

    resolved = (level or get_settings().log_level).upper()
    logger = logging.getLogger("curie_api")
    logger.setLevel(resolved)
    logger.propagate = False
    if not any(getattr(h, "_curie_api_handler", False) for h in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s: %(name)s: %(message)s"))
        handler._curie_api_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return logger


def create_app() -> FastAPI:
    service_logger = configure_logging()
    telemetry = configure(service_name="curie-api", service_version="0.0.0")
    telemetry.attach_logging(service_logger)
    # Which credential the platform will clone with (ADR-0092, #1262). One
    # line, no secret, and a warning when the App is set up only halfway.
    log_credential_path(get_settings())
    app = FastAPI(title="Curie API", version="0.1.0", lifespan=lifespan)
    app.state.telemetry = telemetry

    @app.middleware("http")
    async def trace_http_request(request: Request, call_next: Any) -> Any:
        """Adopt only W3C context and export a closed, value-free server span."""

        parent = extract_http_trace_context(request.headers, logger=service_logger)
        supplied_context = (
            "traceparent" in request.headers or "tracestate" in request.headers
        )
        server = request.scope.get("server")
        attributes: dict[str, AttributeValue] = {
            "http.request.method": request.method,
        }
        if isinstance(server, (list, tuple)) and len(server) == 2:
            address, port = server
            if isinstance(address, str) and address:
                attributes["server.address"] = address
            if type(port) is int:
                attributes["server.port"] = port

        with telemetry.tracer.start_as_current_span(
            f"{request.method} request",
            context=parent,
            kind=SpanKind.SERVER,
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            parent_is_valid = trace.get_current_span(parent).get_span_context().is_valid
            if supplied_context and not parent_is_valid:
                span.add_event("trace_context.invalid")
            try:
                response = await call_next(request)
            except BaseException as exc:
                error_type = type(exc).__name__
                span.set_attribute("error.type", error_type)
                span.set_attribute("exception.type", error_type)
                span.set_attribute("exception.escaped", True)
                span.set_status(Status(StatusCode.ERROR))
                route = request.scope.get("route")
                route_path = getattr(route, "path", None)
                if isinstance(route_path, str):
                    span.update_name(f"{request.method} {route_path}")
                raise

            route = request.scope.get("route")
            route_path = getattr(route, "path", None)
            if isinstance(route_path, str):
                span.update_name(f"{request.method} {route_path}")
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_attribute("error.type", f"{response.status_code // 100}xx")
                span.set_status(Status(StatusCode.ERROR))
            else:
                span.set_status(Status(StatusCode.OK))
            return response

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(config.router)
    app.include_router(console.router)
    app.include_router(agents.router)
    app.include_router(deployments.router)
    app.include_router(bundles.router)
    app.include_router(deploy_targets.router)
    app.include_router(github.router)
    app.include_router(observability.router)
    app.include_router(control.router)
    app.include_router(evals.router)
    app.include_router(runs.router)
    app.include_router(state.router)
    app.include_router(memory.router)
    app.include_router(approvals.router)
    app.include_router(channels.router)
    app.include_router(hooks.router)
    return app


app = create_app()
