"""Secret redaction shared by console, log, and span export boundaries."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RedactionRule:
    """One secret class and its stable replacement marker."""

    name: str
    pattern: re.Pattern[str]
    placeholder: str


def _placeholder(name: str) -> str:
    return f"[REDACTED:{name}]"


# Registry order is load bearing. More specific URL and JWT cases run before
# their generic bearer/assignment siblings so every match has one stable class.
REDACTION_RULES: tuple[RedactionRule, ...] = (
    RedactionRule(
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        _placeholder("pem_private_key"),
    ),
    RedactionRule(
        "url_secret_param",
        re.compile(
            r"[?&](?:token|secret|password|passwd|pwd|api_key|apikey|access_token|key|sig"
            r"|signature)=[^&\s]+",
            re.IGNORECASE,
        ),
        _placeholder("url_secret_param"),
    ),
    RedactionRule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
        _placeholder("jwt"),
    ),
    RedactionRule(
        "bearer_token",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+"),
        _placeholder("bearer_token"),
    ),
    RedactionRule(
        "api_key",
        re.compile(r"\b(?:sk|xai)[-_][A-Za-z0-9_-]{16,}"),
        _placeholder("api_key"),
    ),
    RedactionRule(
        "aws_access_key_id",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16,}"),
        _placeholder("aws_access_key_id"),
    ),
    RedactionRule(
        "github_pat",
        re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})"),
        _placeholder("github_pat"),
    ),
    RedactionRule(
        "gitlab_token",
        re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),
        _placeholder("gitlab_token"),
    ),
    RedactionRule(
        "slack_token",
        re.compile(r"\bxox[baps]-[A-Za-z0-9-]{10,}"),
        _placeholder("slack_token"),
    ),
    RedactionRule(
        "google_api_key",
        re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),
        _placeholder("google_api_key"),
    ),
    RedactionRule(
        "secret_assignment",
        re.compile(
            r"\b(?:secret|password|passwd|pwd|api_key|apikey|access_token|token)=\S+",
            re.IGNORECASE,
        ),
        _placeholder("secret_assignment"),
    ),
    RedactionRule(
        "home_path",
        re.compile(r"/(?:home|Users)/[^/\s]+"),
        _placeholder("home_path"),
    ),
)

REDACTION_BOUNDARIES: tuple[str, ...] = (
    "stderr",
    "otel_log",
    "otel_span",
)


def redact_text(text: str) -> str:
    """Apply every registered rule to one string."""

    for rule in REDACTION_RULES:
        text = rule.pattern.sub(rule.placeholder, text)
    return text


def redact_value(value: object) -> object:
    """Recursively scrub strings while preserving container and scalar types."""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, Mapping):
        return {key: redact_value(item) for key, item in value.items()}
    return value


# Compatibility name retained for runner call sites while redaction moves into
# the shared package.
redact_span_attribute = redact_value


def still_leaks(value: object) -> bool:
    """Return true when any nested string still matches a secret rule."""

    if isinstance(value, str):
        return redact_text(value) != value
    if isinstance(value, Mapping):
        return any(still_leaks(key) or still_leaks(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(still_leaks(item) for item in value)
    return False


class RedactingLogFilter(logging.Filter):
    """Scrub a record after args-style formatting and before any handler emits."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        if record.exc_info:
            rendered = logging.Formatter().formatException(record.exc_info)
            record.exc_text = redact_text(rendered)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        # Extra attributes can be translated independently by the OTEL handler.
        for name, value in tuple(vars(record).items()):
            scrubbed = redact_value(value)
            if scrubbed != value:
                setattr(record, name, scrubbed)
        return True


def _ensure_filter(handler: logging.Handler) -> None:
    if not any(isinstance(item, RedactingLogFilter) for item in handler.filters):
        handler.addFilter(RedactingLogFilter())


def install_logging_redaction(logger: logging.Logger) -> None:
    """Install redaction on this logger's effective console path, idempotently."""

    current: logging.Logger | None = logger
    while current is not None:
        for handler in current.handlers:
            _ensure_filter(handler)
        if not current.propagate:
            break
        current = current.parent


def redact_mapping(values: Mapping[str, Any]) -> dict[str, object]:
    """Typed convenience for callers sanitizing arbitrary attribute maps."""

    return {key: redact_value(value) for key, value in values.items()}
