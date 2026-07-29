"""Boot-time platform-API wiring gate (#442, AC2).

The dispatcher resolves Slack approval clicks by calling the platform API. When
it is wired to the wrong place the only symptom today is a warning at click time
and a dead-ended button (``ApprovalResolveClient.resolve`` catches the
``httpx.HTTPError`` and returns ``ResolveOutcome(status_code=0)``). This gate
turns that silent misconfiguration into a loud boot failure naming the URL it
could not reach.

The gate is bounded-retry-then-fail, not fail-immediately: a single probe at t=0
races the API's own startup, and in k8s pod start order is not ordered at all, so
fail-immediately would crash-loop a healthy stack. Test 3 is the guard on that
decision.

Only the API is faked, at the ``client=`` seam, with ``httpx.MockTransport`` --
the same "fake the platform API, drive the real code" discipline as
test_approval_actions.py's scripted resolver. The retry loop, the deadline, and
the URL construction are all real. Tests stay fast by configuring tiny backoff
values (real settings, not a patched clock), so the poll loop under test is the
one that ships.
"""

from __future__ import annotations

import logging
import time

import httpx
import pytest
from curie_dispatcher.config import DispatcherConfig
from curie_dispatcher.preflight import ApiUnreachableError, check_api_reachable

from .conftest import _black_hole_api

API_URL = "http://curie-api:8000"


def _config(**overrides: object) -> DispatcherConfig:
    """A config whose poll loop runs to its deadline in a fraction of a second.

    The backoff knobs are the real ``config.backoff_*`` settings the preflight
    reuses; shrinking them keeps the suite fast without patching time, so the
    loop exercised here is the shipped one.
    """
    defaults: dict[str, object] = {
        "api_base_url": API_URL,
        "api_preflight_timeout_s": 0.2,
        "backoff_initial_seconds": 0.01,
        "backoff_max_seconds": 0.02,
        "backoff_multiplier": 2.0,
    }
    defaults.update(overrides)
    return DispatcherConfig(**defaults)  # type: ignore[arg-type]


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _refusing(request: httpx.Request) -> httpx.Response:
    """A fake handler that always refuses the connection."""
    raise httpx.ConnectError("connection refused", request=request)


def _ok(request: httpx.Request) -> httpx.Response:
    """A fake handler that always answers a healthy 200."""
    return httpx.Response(200, json={"status": "ok"})


def test_healthy_api_returns_and_logs_the_url(caplog: pytest.LogCaptureFixture) -> None:
    """A reachable API returns cleanly and records where it looked.

    The happy path. A gutted implementation (a bare `return`) also passes this
    one, which is why it is not the contract -- tests 2 and 3 are.
    """
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={"status": "ok"})

    logger = logging.getLogger("test-preflight-healthy")
    with caplog.at_level(logging.INFO, logger="test-preflight-healthy"):
        check_api_reachable(_config(), logger=logger, client=_client(handler))

    assert requested == [f"{API_URL}/health"]
    assert any(API_URL in record.getMessage() for record in caplog.records), (
        "the preflight logged nothing naming the API URL it reached; the "
        "resolved wiring must be visible in the boot logs"
    )


def test_unreachable_api_raises_naming_the_url() -> None:
    """AC2: an unreachable API fails loudly, naming the URL that was tried.

    This is the whole point of the gate. Deleting the implementation fails this.
    Naming the URL is the load-bearing part: "cannot reach the API" without the
    resolved address does not tell an operator that it is pointed at itself.
    """

    with pytest.raises(ApiUnreachableError) as excinfo:
        check_api_reachable(
            _config(api_preflight_timeout_s=0.05),
            logger=logging.getLogger("test-preflight"),
            client=_client(_refusing),
        )

    assert API_URL in str(excinfo.value), (
        f"the error must name the configured api_base_url; got {str(excinfo.value)!r}"
    )


def test_transient_failure_then_success_does_not_raise() -> None:
    """Bounded RETRY, not fail-immediately: a slow-starting API is not a failure.

    The crash-loop guard, and the test that pins the design decision. A naive
    single-probe implementation fails here: in compose and k8s the API is
    routinely not yet accepting connections when the dispatcher boots.
    """
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"status": "ok"})

    check_api_reachable(
        _config(), logger=logging.getLogger("test-preflight"), client=_client(handler)
    )

    assert len(attempts) == 3, (
        f"expected the preflight to keep polling until the API answered "
        f"(3 attempts); it made {len(attempts)}"
    )


def test_non_200_health_is_treated_as_unreachable() -> None:
    """A responding-but-unhealthy endpoint is not "reachable".

    Guards against ignoring status_code. A 404 here is the realistic case: the
    URL points at some other service that answers HTTP but is not the API.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(ApiUnreachableError):
        check_api_reachable(
            _config(api_preflight_timeout_s=0.05),
            logger=logging.getLogger("test-preflight"),
            client=_client(handler),
        )


def test_health_url_tolerates_a_trailing_slash() -> None:
    """A base URL with a trailing slash must not yield `//health`.

    ``ApprovalResolveClient`` already rstrips its base; the preflight builds its
    own URL and must do the same or a perfectly reasonable
    `http://curie-api:8000/` fails the gate on a correctly wired stack.
    """
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, json={"status": "ok"})

    check_api_reachable(
        _config(api_base_url=f"{API_URL}/"),
        logger=logging.getLogger("test-preflight"),
        client=_client(handler),
    )

    assert requested == ["/health"]


def test_the_loop_spends_its_whole_budget_with_production_backoff_ratios() -> None:
    """The gate must poll until its deadline, and report what it really spent.

    The other cases run `backoff_max << timeout`, where the growing delay never
    approaches the deadline. The shipped defaults are the opposite ratio
    (`backoff_max` 30.0 against a 30.0s deadline), so the delays 1, 2, 4, 8, 16
    reach t=15s and the next 16s delay overshoots. An implementation that breaks
    on the overshoot instead of clamping to the remaining budget abandons half
    its deadline and then reports the configured 30.0s it never spent. This
    config is those defaults scaled by 100.
    """

    config = _config(
        api_preflight_timeout_s=0.3,
        backoff_initial_seconds=0.01,
        backoff_max_seconds=0.3,
        backoff_multiplier=2.0,
    )
    start = time.monotonic()
    with pytest.raises(ApiUnreachableError) as excinfo:
        check_api_reachable(
            config, logger=logging.getLogger("test-preflight"), client=_client(_refusing)
        )
    elapsed = time.monotonic() - start

    assert elapsed >= 0.27, (
        f"the preflight gave up after {elapsed:.3f}s of its 0.3s budget; the "
        f"delay must be clamped to the remaining time, not abandoned when it "
        f"would overshoot"
    )
    # 0.15s is the pre-fix elapsed: the message must never claim time it did not spend.
    assert "after 0.3s" in str(excinfo.value), (
        f"the error must report the time actually elapsed, not the configured "
        f"deadline; got {str(excinfo.value)!r}"
    )


def test_the_loop_does_not_probe_past_its_deadline() -> None:
    """The upper bound: the gate must spend its budget and then stop.

    The sibling test above pins the lower bound (never abandon the budget early).
    This pins the other side, which that fix left open: the loop could still
    enter a probe with little or no budget left, and the probe's own timeout then
    ran past the deadline -- measured at 0.249s elapsed against a 0.2s deadline,
    with the error still claiming "after 0.2s". A boot gate that overshoots its
    configured deadline delays the CrashLoopBackOff signal it exists to produce.

    The injected client carries the 5.0s default the preflight builds for itself,
    so an unbounded probe against a black hole burns 5s of a 0.3s budget.
    """
    with _black_hole_api() as url:
        config = _config(api_base_url=url, api_preflight_timeout_s=0.3)
        start = time.monotonic()
        with pytest.raises(ApiUnreachableError) as excinfo:
            check_api_reachable(
                config,
                logger=logging.getLogger("test-preflight"),
                client=httpx.Client(timeout=5.0),
            )
        elapsed = time.monotonic() - start

    assert elapsed < 0.6, (
        f"the preflight took {elapsed:.3f}s against a 0.3s deadline; each probe "
        f"must be bounded by the time remaining, not by the probe timeout"
    )
    assert elapsed >= 0.27, (
        f"the preflight gave up after {elapsed:.3f}s of its 0.3s budget; bounding "
        f"the probe must not turn into abandoning the deadline early"
    )
    assert "after 0.3s" in str(excinfo.value), (
        f"the error must report the time actually elapsed; got {str(excinfo.value)!r}"
    )


def test_userinfo_in_the_base_url_is_kept_out_of_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A credential-bearing base URL must not write its password to the logs.

    AC2 requires naming the resolved URL, so stripping userinfo beats dropping
    the URL. `httpx` accepts `http://user:pass@host`, so a BYO `apiBaseUrl` would
    otherwise put `pass` in the pod logs and every shipper downstream.
    """

    logger = logging.getLogger("test-preflight-userinfo")
    with caplog.at_level(logging.INFO, logger="test-preflight-userinfo"):
        check_api_reachable(
            _config(api_base_url="http://user:hunter2@curie-api:8000"),
            logger=logger,
            client=_client(_ok),
        )

    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "hunter2" not in logged and "user" not in logged, (
        f"the preflight logged the URL's userinfo: {logged!r}"
    )
    assert "curie-api:8000" in logged, (
        f"AC2 still requires the log to name the resolved URL; got {logged!r}"
    )


def test_userinfo_is_stripped_from_the_error_too() -> None:
    """The failure path names the URL as well, so it needs the same scrub."""

    with pytest.raises(ApiUnreachableError) as excinfo:
        check_api_reachable(
            _config(
                api_base_url="http://user:hunter2@curie-api:8000",
                api_preflight_timeout_s=0.05,
            ),
            logger=logging.getLogger("test-preflight"),
            client=_client(_refusing),
        )

    assert "hunter2" not in str(excinfo.value)
    assert "curie-api:8000" in str(excinfo.value)


def test_run_main_gates_before_connecting_slack(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordering contract: the wiring gate precedes any Slack wiring.

    Driven through the real ``run.main()`` so it survives an internal rename of
    ``check_api_reachable``. ``build_supervisor`` -- the boundary that builds the
    Valkey client, the Slack Web client, and the Socket Mode connection factory --
    is replaced with a recorder; reaching it at all means the gate did not fire
    first. The API is a genuinely dead port, so nothing is stubbed on the path
    under test.
    """
    from curie_dispatcher import run

    reached_slack: list[str] = []

    def _recording_build_supervisor(*args: object, **kwargs: object) -> object:
        reached_slack.append("build_supervisor")
        raise AssertionError(
            "build_supervisor was called with an unreachable API: the dispatcher "
            "wired up Slack before (or instead of) gating on the platform API"
        )

    monkeypatch.setattr(run, "build_supervisor", _recording_build_supervisor)
    for name, field in DispatcherConfig.model_fields.items():
        alias = field.validation_alias
        monkeypatch.delenv(alias if isinstance(alias, str) else name.upper(), raising=False)
    # Port 1 is reserved and never listening: a real, immediate connection refusal.
    monkeypatch.setenv("CURIE_API_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CURIE_API_PREFLIGHT_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("CURIE_BACKOFF_INITIAL_SECONDS", "0.01")
    monkeypatch.setenv("CURIE_BACKOFF_MAX_SECONDS", "0.02")

    with pytest.raises(SystemExit) as excinfo:
        run.main()

    assert excinfo.value.code not in (0, None), (
        f"the dispatcher must exit non-zero on an unreachable API so a "
        f"CrashLoopBackOff surfaces it; exited {excinfo.value.code!r}"
    )
    assert reached_slack == []
