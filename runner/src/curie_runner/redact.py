"""Runner compatibility exports for shared telemetry redaction.

The platform owns one redaction registry and implementation in
``curie_telemetry.redact``. Runner callers keep their established import names
while stderr, OTEL logs, and OTEL span attributes all use that implementation.
"""

from __future__ import annotations

import logging

from curie_telemetry.redact import (
    REDACTION_RULES,
    RedactingLogFilter,
    RedactionRule,
    install_logging_redaction,
    redact_span_attribute,
    redact_text,
)

# Compatibility tripwire for the runner's original boundary names. The shared
# package separately freezes its expanded stderr/OTEL log/OTEL span boundary
# set; keeping these labels avoids changing the runner's import contract.
REDACTION_BOUNDARIES: tuple[str, ...] = ("stdout", "gen_ai_span")


def install_stdout_redaction() -> None:
    """Install shared redaction on the existing diagnostic handlers."""

    install_logging_redaction(logging.getLogger())


__all__ = [
    "REDACTION_BOUNDARIES",
    "REDACTION_RULES",
    "RedactingLogFilter",
    "RedactionRule",
    "install_stdout_redaction",
    "redact_span_attribute",
    "redact_text",
]
