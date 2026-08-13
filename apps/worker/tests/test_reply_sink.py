"""The reply-sink router and the two adapters below the seam.

Stream B, ADR-0096 phase 2. Covers T-B4 (a non-Slack kind reaches the HTTP
adapter), T-B5 (egress fails closed), T-B6 (#530's transport fallback stays on
the Slack path only), T-B7 (#708's best-effort swallow survives on the HTTP
path), T-B10 (the Slack bot token cannot leave its configured origin) and the
worker-side half of T-B13 (per-adapter credentials, cross-adapter containment).

Nothing internal is mocked. Both adapters talk HTTP to an in-process aiohttp
capture server -- the same shape ``tests/kernel/conftest.py`` uses for the fake
runner -- so "which secret reached which URL" is asserted from the bytes that
actually left the process, not from a patched method. That is what makes T-B10's
"the token provably never leaves" a real claim: a monkeypatched client would
prove nothing about the wire.

Async tests run inside ``asyncio.run`` bodies; the repo has no pytest-asyncio
plugin (see the kernel conftest docstring).
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from channel_protocol import OutboundMessage
from channel_protocol.reply import (
    REPLY_WIRE_VERSION,
    ReplyPost,
    ReplyTarget,
    ReplyUpdate,
    TurnCompleted,
)
from curie_worker.config import WorkerConfig
from curie_worker.reply_sink import (
    MAX_ACK_BODY_BYTES,
    HttpReplyAdapter,
    MissingAdapterCredentialError,
    OversizedAdapterResponseError,
    RedirectedAdapterEndpointError,
    RejectedAdapterResponseError,
    TargetRoute,
    build_reply_sink,
)
from curie_worker.slack_sink import UntrustedSlackEndpointError

# EB-B4: the per-adapter egress credential travels in this header, and only this
# header. Pinned as a constant so a rename shows up as one failure, not thirty.
SECRET_HEADER = "X-Curie-Adapter-Secret"

ADAPTER_A = "agentmail-sandbox"
ADAPTER_B = "other-adapter"
SECRET_A = "secret-for-agentmail-sandbox"
SECRET_B = "secret-for-other-adapter"
SLACK_TOKEN = "xoxb-test-bot-token"


class _Capture:
    """Records every request that reaches it, whatever the route.

    ``/a`` and ``/b`` are two distinct adapter endpoints; ``/slack/api/`` is the
    configured Slack origin and ``/slack/dead/`` is a SAME-ORIGIN path whose
    connection drops, which is the only shape that can still exercise #530's
    transport fallback once D4.4 refuses cross-origin endpoints.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.app = web.Application()
        self.app.add_routes(
            [
                web.post("/a", self._record_ok),
                web.post("/b", self._record_ok),
                web.post("/slack/api/{method}", self._record_slack),
                web.post("/slack/dead/{method}", self._drop),
                web.post("/redirect", self._redirect_to_b),
            ]
        )

    async def _capture(self, request: web.Request) -> None:
        self.requests.append(
            {
                "path": request.path,
                "headers": dict(request.headers),
                "body": await request.text(),
            }
        )

    async def _record_ok(self, request: web.Request) -> web.Response:
        await self._capture(request)
        # ``ref`` is the adapter-minted handle a ``reply.post`` ack carries back
        # (EB-B1's ``ReplyAck``); an adapter with nothing to mint omits it.
        return web.json_response({"ref": "msg_minted"})

    async def _record_slack(self, request: web.Request) -> web.Response:
        await self._capture(request)
        return web.json_response({"ok": True, "ts": "1720000000.000200"})

    async def _drop(self, request: web.Request) -> web.Response:
        # Record it (so a test can prove the call was ATTEMPTED here) and then
        # kill the connection, which the client sees as an aiohttp.ClientError --
        # the "unreachable" class #530's fallback keys on, as distinct from a
        # SlackApiError, which means the endpoint answered.
        await self._capture(request)
        transport = request.transport
        assert transport is not None
        transport.close()
        return web.Response()

    async def _redirect_to_b(self, request: web.Request) -> web.Response:
        # A redirecting adapter endpoint: record the attempt, then point the
        # client at ANOTHER path. aiohttp's default would replay the POST there
        # with the egress secret still attached, so ``/b`` receiving anything is
        # the leak this shape exists to catch.
        await self._capture(request)
        return web.Response(status=307, headers={"Location": "/b"})

    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]

    def secrets(self) -> list[str | None]:
        return [r["headers"].get(SECRET_HEADER) for r in self.requests]

    def bodies_mentioning(self, needle: str) -> list[dict[str, Any]]:
        return [
            r
            for r in self.requests
            if needle in r["body"] or needle in json.dumps(r["headers"])
        ]


def _closed_port() -> int:
    """A port nothing is listening on, so a connect attempt fails at transport."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _target(kind: str, **overrides: object) -> ReplyTarget:
    base: dict[str, object] = {
        "kind": kind,
        "address": "agent@example.test" if kind != "slack" else "C1",
        "conversation_id": "thread-1",
        "reply_ref": "ref-1",
    }
    base.update(overrides)
    return ReplyTarget(**base)  # type: ignore[arg-type]


def _update(kind: str, text: str = "the answer") -> ReplyUpdate:
    return ReplyUpdate(
        version=REPLY_WIRE_VERSION,
        event="reply.update",
        target=_target(kind),
        text=text,
    )


def _config(capture_port: int | None = None, **overrides: object) -> WorkerConfig:
    base: dict[str, object] = {
        "slack_bot_token": SLACK_TOKEN,
        "adapter_credentials": {ADAPTER_A: SECRET_A, ADAPTER_B: SECRET_B},
    }
    if capture_port is not None:
        base["slack_api_base_url"] = f"http://127.0.0.1:{capture_port}/slack/api/"
    base.update(overrides)
    return WorkerConfig(**base)  # type: ignore[arg-type]


# --- T-B4: a non-Slack kind routes to the HTTP adapter ------------------------


def test_a_non_slack_kind_is_delivered_over_http_and_never_touches_slack() -> None:
    # D3: adapter selection is the ONLY place ``kind`` is switched on, and it
    # lives in the router below the seam. The kernel holds one ``ReplySink``.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(_config(port))
            await sink.emit(
                _update("email"),
                route=TargetRoute(
                    endpoint=f"http://127.0.0.1:{port}/a", adapter=ADAPTER_A
                ),
            )
            assert capture.paths() == ["/a"]
            # The Slack origin was never dialled: no request reached /slack/api/,
            # so no Slack client was used for this turn at all.
            assert not [p for p in capture.paths() if p.startswith("/slack")]
        finally:
            await server.close()

    asyncio.run(go())


def test_the_http_adapter_posts_the_serialized_event_as_json() -> None:
    # EB-B4: the body is ``event.model_dump_json()`` with
    # ``Content-Type: application/json``. The adapter reads the event off the
    # wire, so the event body -- and nothing about where it was sent -- is what
    # crosses.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(_config(port))
            event = TurnCompleted(
                version=REPLY_WIRE_VERSION,
                event="turn.completed",
                target=_target("email"),
                event_id="ev-42",
                outcome="delivered",
            )
            await sink.emit(
                event,
                route=TargetRoute(
                    endpoint=f"http://127.0.0.1:{port}/a", adapter=ADAPTER_A
                ),
            )
            assert len(capture.requests) == 1
            sent = capture.requests[0]
            assert sent["headers"]["Content-Type"].startswith("application/json")
            body = json.loads(sent["body"])
            assert body == json.loads(event.model_dump_json())
            # EB-B2: the route is a kwarg, never a wire field. A published event
            # body must not tell an adapter where the platform sent it, and must
            # never carry the egress credential selector.
            assert "endpoint" not in body
            assert "adapter" not in body
        finally:
            await server.close()

    asyncio.run(go())


def test_a_reply_post_returns_the_adapters_minted_ref() -> None:
    # The ack is how the approval card's handle gets back to the kernel
    # (kernel.py:1313-1319 keeps ``card_ts`` today). An adapter that answers with
    # a ref must have it survive the seam, or the card can never be settled.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(_config())
            ack = await sink.emit(
                ReplyPost(
                    version=REPLY_WIRE_VERSION,
                    event="reply.post",
                    target=_target("email"),
                    message=OutboundMessage(version="1.0", text="Deploy this?"),
                    requested_by="U9",
                ),
                route=TargetRoute(
                    endpoint=f"http://127.0.0.1:{port}/a", adapter=ADAPTER_A
                ),
            )
            assert ack.ref == "msg_minted"
            assert capture.paths() == ["/a"]
        finally:
            await server.close()

    asyncio.run(go())


# --- T-B5: platform egress fails closed ---------------------------------------


@pytest.mark.parametrize(
    ("adapter", "why"),
    [
        (None, "no adapter identity on the route"),
        ("never-configured", "an adapter slug with no configured credential"),
    ],
)
def test_egress_without_a_credential_raises_and_sends_nothing(
    adapter: str | None, why: str
) -> None:
    # D4.3 / finding 9: an unauthenticated platform request lets any reachable
    # pod be impersonated and gives the adapter no way to tell the platform from
    # an attacker. Sending anonymously is a hole, not a mitigation.
    # Mutation: restore the anonymous-send branch and this fails.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(_config(port))
            with pytest.raises(MissingAdapterCredentialError):
                await sink.emit(
                    _update("email"),
                    route=TargetRoute(
                        endpoint=f"http://127.0.0.1:{port}/a", adapter=adapter
                    ),
                )
            assert capture.requests == [], f"egress must send nothing with {why}"
        finally:
            await server.close()

    asyncio.run(go())


def test_egress_without_an_endpoint_raises_and_sends_nothing() -> None:
    # E17: a binding row whose kind is not ``slack`` and whose ``endpoint`` is
    # NULL is an operator error. The worker fails closed and the turn escalates,
    # rather than silently delivering nowhere.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            sink = build_reply_sink(_config())
            with pytest.raises(MissingAdapterCredentialError):
                await sink.emit(
                    _update("email"),
                    route=TargetRoute(endpoint=None, adapter=ADAPTER_A),
                )
            assert capture.requests == []
        finally:
            await server.close()

    asyncio.run(go())


# --- T-B13: per-adapter credentials, and cross-adapter containment ------------


def test_each_adapters_secret_reaches_only_its_own_endpoint() -> None:
    # D4.2 / findings 7 and 9: the secret is per-ADAPTER, not per-kind, so
    # compromising one adapter yields one secret and yields it only for its own
    # binding.
    # Mutation: key ``adapter_credentials`` by ``kind`` instead of ``adapter``
    # and both halves of this fail -- both bindings would present one secret,
    # which is revision 1's rejected design.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(_config(port))

            await sink.emit(
                _update("email", "reply for A"),
                route=TargetRoute(
                    endpoint=f"http://127.0.0.1:{port}/a", adapter=ADAPTER_A
                ),
            )
            assert capture.paths() == ["/a"]
            assert capture.secrets() == [SECRET_A]
            assert capture.bodies_mentioning(SECRET_B) == [], (
                "adapter B's secret must appear in no request at all"
            )

            await sink.emit(
                _update("email", "reply for B"),
                route=TargetRoute(
                    endpoint=f"http://127.0.0.1:{port}/b", adapter=ADAPTER_B
                ),
            )
            assert capture.paths() == ["/a", "/b"]
            assert capture.secrets() == [SECRET_A, SECRET_B]
            # The containment property, stated as one assertion per direction:
            # neither request carries the other adapter's secret anywhere.
            a_request, b_request = capture.requests
            assert SECRET_B not in json.dumps(a_request)
            assert SECRET_A not in json.dumps(b_request)
        finally:
            await server.close()

    asyncio.run(go())


def test_the_slack_bot_token_is_never_sent_to_a_non_slack_adapter() -> None:
    # The sibling of the containment property above, across the kind seam: the
    # platform's Slack credential must not ride an HTTP adapter's request, which
    # is what a single shared ``self._token`` (slack_sink.py:152-157 today) would
    # do if the HTTP path reused it.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(_config(port))
            await sink.emit(
                _update("email"),
                route=TargetRoute(
                    endpoint=f"http://127.0.0.1:{port}/a", adapter=ADAPTER_A
                ),
            )
            assert capture.bodies_mentioning(SLACK_TOKEN) == []
        finally:
            await server.close()

    asyncio.run(go())


# --- T-B10: the Slack bot token cannot leave its configured origin ------------


def test_a_slack_endpoint_at_another_origin_is_refused_and_nothing_is_sent() -> None:
    # D4.4 / finding 8, the one live exploitable-today hole: today
    # ``slack_sink.py:159-175`` builds ``AsyncWebClient(token=self._token,
    # base_url=endpoint)`` for whatever URL the turn named, so a turn carrying an
    # attacker's endpoint captures the platform bot token.
    # Mutation: restore that unconditional client and this fails.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            other = _Capture()
            other_server = TestServer(other.app)
            await other_server.start_server()
            other_port = other_server.port
            assert other_port is not None
            try:
                sink = build_reply_sink(_config(port))
                with pytest.raises(UntrustedSlackEndpointError):
                    await sink.emit(
                        _update("slack"),
                        route=TargetRoute(
                            endpoint=f"http://127.0.0.1:{other_port}/slack/api/",
                            adapter=None,
                        ),
                    )
                # The proof: no request left the process at all, so the token
                # provably never reached the untrusted origin.
                assert other.requests == []
                assert capture.requests == []
            finally:
                await other_server.close()
        finally:
            await server.close()

    asyncio.run(go())


def test_a_slack_endpoint_at_the_configured_origin_is_accepted() -> None:
    # The dev loop must keep working: ``SLACK_API_BASE_URL`` is an explicitly
    # CONFIGURED dev origin (compose.dev.yaml:517), not a wire-supplied one, so a
    # per-turn endpoint that matches it is trusted and delivered.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(_config(port))
            await sink.emit(
                _update("slack"),
                route=TargetRoute(
                    endpoint=f"http://127.0.0.1:{port}/slack/api/", adapter=None
                ),
            )
            assert capture.paths() == ["/slack/api/chat.update"]
            assert capture.bodies_mentioning(SLACK_TOKEN), (
                "the bot token authenticates the call to its OWN origin"
            )
        finally:
            await server.close()

    asyncio.run(go())


def test_a_configured_trusted_dev_origin_is_accepted() -> None:
    # The dev/CLI stub is reached on a host and port the worker's single
    # ``slack_api_base_url`` cannot also name (`local message` advertises
    # ``host.docker.internal``, `cluster message` a routable host), so the origin
    # pin alone would refuse the local loop. ``CURIE_SLACK_TRUSTED_ORIGINS`` is
    # the explicitly CONFIGURED override; it is operator config, never a
    # wire-supplied origin, which is what keeps the test above true.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            stub = _Capture()
            stub_server = TestServer(stub.app)
            await stub_server.start_server()
            stub_port = stub_server.port
            assert stub_port is not None
            try:
                sink = build_reply_sink(
                    _config(
                        port,
                        slack_trusted_origins=f"http://127.0.0.1:{stub_port}",
                    )
                )
                await sink.emit(
                    _update("slack"),
                    route=TargetRoute(
                        endpoint=f"http://127.0.0.1:{stub_port}/slack/api/",
                        adapter=None,
                    ),
                )
                # Delivered at the configured dev origin, and nothing was sent to
                # the worker's own default transport.
                assert stub.paths() == ["/slack/api/chat.update"]
                assert capture.requests == []
            finally:
                await stub_server.close()
        finally:
            await server.close()

    asyncio.run(go())


def test_a_portless_trusted_entry_accepts_any_port_on_that_host() -> None:
    # The dev affordance that makes the override usable at all: `curie chat` and
    # `cluster message --listen-port 0` bind an EPHEMERAL stub port, so no fixed
    # origin can be configured ahead of time. A portless entry
    # (``http://127.0.0.1``) trusts any port on that scheme+host.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            stub = _Capture()
            stub_server = TestServer(stub.app)
            await stub_server.start_server()
            stub_port = stub_server.port
            assert stub_port is not None
            try:
                sink = build_reply_sink(
                    _config(port, slack_trusted_origins="http://127.0.0.1")
                )
                await sink.emit(
                    _update("slack"),
                    route=TargetRoute(
                        endpoint=f"http://127.0.0.1:{stub_port}/slack/api/",
                        adapter=None,
                    ),
                )
                assert stub.paths() == ["/slack/api/chat.update"]
                assert capture.requests == []
            finally:
                await stub_server.close()
        finally:
            await server.close()

    asyncio.run(go())


def test_a_portless_trusted_entry_still_refuses_another_host() -> None:
    # Portless widens the PORT, never the host: an endpoint on any other host is
    # refused exactly as before, with nothing sent.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(
                _config(port, slack_trusted_origins="http://127.0.0.1")
            )
            with pytest.raises(UntrustedSlackEndpointError):
                await sink.emit(
                    _update("slack"),
                    route=TargetRoute(
                        endpoint="http://attacker.invalid:9999/slack/api/",
                        adapter=None,
                    ),
                )
            assert capture.requests == []
        finally:
            await server.close()

    asyncio.run(go())


def test_an_exact_trusted_entry_still_refuses_a_port_mismatch() -> None:
    # An entry that NAMES a port keeps exact matching, so the portless form is an
    # opt-in widening rather than a blanket one: same host, other port, refused.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            stub = _Capture()
            stub_server = TestServer(stub.app)
            await stub_server.start_server()
            stub_port = stub_server.port
            assert stub_port is not None
            try:
                sink = build_reply_sink(
                    _config(port, slack_trusted_origins=f"http://127.0.0.1:{stub_port}")
                )
                with pytest.raises(UntrustedSlackEndpointError):
                    await sink.emit(
                        _update("slack"),
                        route=TargetRoute(
                            endpoint=f"http://127.0.0.1:{_closed_port()}/slack/api/",
                            adapter=None,
                        ),
                    )
                assert stub.requests == []
                assert capture.requests == []
            finally:
                await stub_server.close()
        finally:
            await server.close()

    asyncio.run(go())


def test_a_slack_turn_with_no_per_turn_endpoint_uses_the_configured_origin() -> None:
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(_config(port))
            await sink.emit(
                _update("slack"), route=TargetRoute(endpoint=None, adapter=None)
            )
            assert capture.paths() == ["/slack/api/chat.update"]
        finally:
            await server.close()

    asyncio.run(go())


# --- T-B6: #530's transport fallback is Slack-only ---------------------------


def test_an_unreachable_slack_endpoint_falls_back_to_the_configured_default() -> None:
    # #530 preserved. D4.4 narrows which endpoints can reach this at all: a
    # cross-origin endpoint is refused BEFORE the fallback can fire (the test
    # above), so the surviving #530 shape is a trusted same-origin target whose
    # connection drops -- a stub whose path moved or died. The fallback target is
    # the worker's configured default transport, never a wire-named one.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(_config(port))
            await sink.emit(
                _update("slack"),
                route=TargetRoute(
                    endpoint=f"http://127.0.0.1:{port}/slack/dead/", adapter=None
                ),
            )
            # Attempted on the dead path, then retried on the configured default.
            assert capture.paths() == [
                "/slack/dead/chat.update",
                "/slack/api/chat.update",
            ]
        finally:
            await server.close()

    asyncio.run(go())


def test_a_redirecting_adapter_endpoint_is_refused_and_the_secret_never_moves() -> None:
    # aiohttp follows redirects by default and KEEPS the custom
    # ``X-Curie-Adapter-Secret`` header across the hop, so a redirecting adapter
    # endpoint is a credential-exfil primitive: it names any Location and the
    # platform re-sends its egress secret there. A redirect is a delivery
    # FAILURE, not a hop. Mutation: drop ``allow_redirects=False`` and the secret
    # lands on ``/b``.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(_config(port))
            with pytest.raises(RedirectedAdapterEndpointError):
                await sink.emit(
                    _update("email"),
                    route=TargetRoute(
                        endpoint=f"http://127.0.0.1:{port}/redirect",
                        adapter=ADAPTER_A,
                    ),
                )
            assert capture.paths() == ["/redirect"], (
                "the redirect target must never receive the request"
            )
            assert capture.secrets() == [SECRET_A]
        finally:
            await server.close()

    asyncio.run(go())


def test_a_redirect_is_not_swallowed_by_best_effort_delivery() -> None:
    # #708's swallow covers an UNREACHABLE target only. A redirect is a live
    # answer from a reachable endpoint, so it must stay loud even on a
    # best-effort resume turn -- otherwise a redirecting adapter both keeps the
    # secret-bounce attempt and reads as a delivered reply.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            sink = build_reply_sink(_config(port))
            with pytest.raises(RedirectedAdapterEndpointError):
                await sink.emit(
                    _update("email"),
                    route=TargetRoute(
                        endpoint=f"http://127.0.0.1:{port}/redirect",
                        adapter=ADAPTER_A,
                    ),
                    best_effort_unreachable=True,
                )
            assert capture.paths() == ["/redirect"]
        finally:
            await server.close()

    asyncio.run(go())


def test_an_unreachable_email_endpoint_raises_and_never_falls_back_to_slack() -> None:
    # E7, the sibling half and the sharper one: ``_with_transport_fallback``
    # falls back to the worker's DEFAULT SLACK transport, so a fallback on a
    # non-Slack kind would post an email reply into a Slack workspace -- the
    # exact hazard #530's issue text flagged. ``HttpReplyAdapter`` therefore has
    # NO transport fallback.
    async def go() -> None:
        capture = _Capture()
        server = TestServer(capture.app)
        await server.start_server()
        try:
            port = server.port
            assert port is not None
            dead = _closed_port()
            sink = build_reply_sink(_config(port))
            with pytest.raises(aiohttp.ClientError):
                await sink.emit(
                    _update("email"),
                    route=TargetRoute(
                        endpoint=f"http://127.0.0.1:{dead}/a", adapter=ADAPTER_A
                    ),
                )
            assert capture.requests == [], (
                "an unreachable email adapter must never reach the Slack transport"
            )
        finally:
            await server.close()

    asyncio.run(go())


# --- T-B7: #708's best-effort swallow survives on the HTTP path ---------------


def test_a_best_effort_email_reply_to_a_dead_endpoint_completes() -> None:
    # #708: an approval-resume turn whose reply endpoint is dead must ACK rather
    # than dead-letter -- the granted tool already ran in the runner, so
    # re-running it is the harm. Losing this on the HTTP path re-opens #708 for
    # a new channel. ``HttpReplyAdapter`` honors the flag by logging and
    # returning, with no fallback target.
    async def go() -> None:
        dead = _closed_port()
        sink = build_reply_sink(_config())
        ack = await sink.emit(
            _update("email"),
            route=TargetRoute(endpoint=f"http://127.0.0.1:{dead}/a", adapter=ADAPTER_A),
            best_effort_unreachable=True,
        )
        # It returned instead of raising; there is no ref because nothing was
        # delivered.
        assert ack.ref is None

    asyncio.run(go())


def test_the_same_dead_email_endpoint_still_raises_without_the_flag() -> None:
    # The mirror that makes the test above meaningful: it is the FLAG, not luck
    # or an accidentally-swallowed error, that converts a dead-letter into a
    # terminal ack. A non-resume turn stays loud and reclaims (ADR-0039).
    async def go() -> None:
        dead = _closed_port()
        sink = build_reply_sink(_config())
        with pytest.raises(aiohttp.ClientError):
            await sink.emit(
                _update("email"),
                route=TargetRoute(
                    endpoint=f"http://127.0.0.1:{dead}/a", adapter=ADAPTER_A
                ),
            )

    asyncio.run(go())


def test_a_missing_credential_is_not_swallowed_by_best_effort() -> None:
    # Fail-closed outranks best-effort: ``best_effort_unreachable`` covers an
    # UNREACHABLE target, never a missing credential. Swallowing the latter would
    # turn D4.3's loud fail-closed into a silent no-delivery.
    async def go() -> None:
        sink = build_reply_sink(_config())
        with pytest.raises(MissingAdapterCredentialError):
            await sink.emit(
                _update("email"),
                route=TargetRoute(endpoint="http://127.0.0.1:1/a", adapter=None),
                best_effort_unreachable=True,
            )

    asyncio.run(go())


# --- The acknowledgement body is bounded, and errors carry no full URL --------


async def _serve(handler, path: str = "/ack"):  # noqa: ANN001, ANN202
    """A one-route server, for the ack-body shapes ``_Capture`` cannot express."""
    app = web.Application()
    app.add_routes([web.post(path, handler)])
    server = TestServer(app)
    await server.start_server()
    return server


_SECRET_IN_PATH = "/ack?token=super-secret-token"


def test_a_chunked_oversize_ack_is_refused_even_with_a_small_first_chunk() -> None:
    # The cap must survive a body that arrives in pieces. ``StreamReader.read(n)``
    # returns whatever has arrived SO FAR, so a chunked adapter that writes a
    # 16-byte first chunk and then megabytes behind it walks straight past a
    # single sized read. Mutation: replace the accumulating loop with one
    # ``read(MAX_ACK_BODY_BYTES + 1)`` and this test passes an unbounded body.
    async def go() -> None:
        async def handler(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b'{"ref": "x"}')  # a small first chunk
            # The pause is the point: it lets the client's first read complete
            # with ONLY that chunk in the buffer, which is exactly the state a
            # single sized read mistakes for the whole body.
            await asyncio.sleep(0.05)
            for _ in range(4):
                await response.write(b"y" * (MAX_ACK_BODY_BYTES // 2))
                await asyncio.sleep(0.01)
            await response.write_eof()
            return response

        server = await _serve(handler)
        try:
            adapter = HttpReplyAdapter({ADAPTER_A: SECRET_A})
            with pytest.raises(OversizedAdapterResponseError):
                await adapter.emit(
                    _update("email"),
                    route=TargetRoute(
                        endpoint=f"http://127.0.0.1:{server.port}/ack",
                        adapter=ADAPTER_A,
                    ),
                )
        finally:
            await server.close()

    asyncio.run(go())


def test_an_ack_at_the_cap_is_still_read_and_its_ref_survives() -> None:
    # The other side of the bound: the cap is a refusal of MORE than the cap, so
    # an honest adapter answering exactly at it is delivered normally.
    async def go() -> None:
        async def handler(request: web.Request) -> web.StreamResponse:
            head = b'{"ref": "msg_minted", "pad": "' + b"z" * 32 + b'"}'
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(head)
            await response.write(b" " * (MAX_ACK_BODY_BYTES - len(head)))
            await response.write_eof()
            return response

        server = await _serve(handler)
        try:
            adapter = HttpReplyAdapter({ADAPTER_A: SECRET_A})
            ack = await adapter.emit(
                _update("email"),
                route=TargetRoute(
                    endpoint=f"http://127.0.0.1:{server.port}/ack", adapter=ADAPTER_A
                ),
            )
            assert ack.ref == "msg_minted"
        finally:
            await server.close()

    asyncio.run(go())


@pytest.mark.parametrize("status", [401, 500])
def test_a_rejected_delivery_never_puts_the_endpoint_url_in_the_error(
    status: int,
) -> None:
    # ``raise_for_status()`` raises aiohttp's ``ClientResponseError``, whose
    # ``str()`` prints ``request_info.real_url`` -- so every upstream logger that
    # formats the exception re-leaks an endpoint whose path or query can carry a
    # token. The delivery failure is raised by this module instead, redacted to
    # scheme://host:port. Mutation: restore ``raise_for_status()`` and the query
    # string is back in the message.
    async def go() -> None:
        async def handler(request: web.Request) -> web.Response:
            return web.Response(status=status, text="nope")

        server = await _serve(handler)
        try:
            adapter = HttpReplyAdapter({ADAPTER_A: SECRET_A})
            endpoint = f"http://127.0.0.1:{server.port}{_SECRET_IN_PATH}"
            with pytest.raises(RejectedAdapterResponseError) as caught:
                await adapter.emit(
                    _update("email"),
                    route=TargetRoute(endpoint=endpoint, adapter=ADAPTER_A),
                )
            message = str(caught.value)
            assert "super-secret-token" not in message
            assert f"http://127.0.0.1:{server.port}" in message
            assert str(status) in message
            # And nothing aiohttp built is chained in behind it.
            assert caught.value.__cause__ is None
            assert caught.value.__context__ is None
        finally:
            await server.close()

    asyncio.run(go())


def test_a_missing_adapter_identity_names_only_the_endpoint_origin() -> None:
    # The same redaction on the fail-closed path (finding 7's worker half): the
    # message an operator reads must identify the route without reprinting a
    # credential-bearing path or query.
    async def go() -> None:
        adapter = HttpReplyAdapter({ADAPTER_A: SECRET_A})
        with pytest.raises(MissingAdapterCredentialError) as caught:
            await adapter.emit(
                _update("email"),
                route=TargetRoute(
                    endpoint=f"http://adapter.example{_SECRET_IN_PATH}", adapter=None
                ),
            )
        assert "super-secret-token" not in str(caught.value)
        assert "http://adapter.example" in str(caught.value)

    asyncio.run(go())
