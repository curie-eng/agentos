"""Reverse-diff pin for the dispatcher Slack capability boot gate (#2205)."""

from __future__ import annotations

import logging

import pytest

MISSING_SCOPE_MESSAGE = (
    "Slack channel capability preflight failed: bot token is missing required "
    "scope channels:read. Add channels:read under OAuth & Permissions > Bot "
    "Token Scopes, then reinstall the app to the workspace."
)


class _TestTelemetry:
    def shutdown(self) -> None:
        pass


def test_missing_scope_refuses_boot_before_slack_wiring(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real process entrypoint must stop on the actionable scope refusal.

    Imports stay at the consumer module boundary so the reverse-diff verifier
    attributes a missing gate to this selected test instead of failing during
    collection when the new preflight symbols have been removed.
    """
    from curie_dispatcher import run

    assert hasattr(run, "check_slack_channel_capabilities"), (
        "dispatcher startup has no Slack channel capability preflight"
    )
    assert hasattr(run, "SlackChannelPreflightError"), (
        "dispatcher startup cannot handle Slack channel preflight refusals"
    )

    events: list[str] = []

    def check_api(*args: object, **kwargs: object) -> None:
        events.append("api")

    def refuse_slack(*args: object, **kwargs: object) -> None:
        events.append("slack")
        error_type = run.SlackChannelPreflightError
        raise error_type(MISSING_SCOPE_MESSAGE)

    def unexpected_supervisor(*args: object, **kwargs: object) -> object:
        events.append("supervisor")
        raise AssertionError("missing scope reached Slack wiring")

    monkeypatch.setenv(
        "CURIE_APPROVAL_CHAT_ATTESTER_SECRET", "dispatcher-attester-test-secret"
    )
    monkeypatch.setattr(
        run, "bootstrap_service_telemetry", lambda *a, **k: _TestTelemetry()
    )
    monkeypatch.setattr(run, "check_api_reachable", check_api)
    monkeypatch.setattr(run, "check_slack_channel_capabilities", refuse_slack)
    monkeypatch.setattr(run, "build_supervisor", unexpected_supervisor)

    with caplog.at_level(logging.ERROR, logger="curie_dispatcher"):
        with pytest.raises(SystemExit) as excinfo:
            run.main()

    assert excinfo.value.code == 1
    assert events == ["api", "slack"]
    assert [record.getMessage() for record in caplog.records] == [
        MISSING_SCOPE_MESSAGE
    ]
