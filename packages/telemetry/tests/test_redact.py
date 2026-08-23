"""Every shared telemetry value boundary uses the same recursive scrub."""

from __future__ import annotations

import io
import logging

from curie_telemetry.redact import RedactingLogFilter, redact_text, redact_value

FAKE_API_KEY = "sk-" + "FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"
FAKE_BEARER = "Bearer " + "abc0000FAKEFAKEFAKEFAKEFAKEFAKE"
FAKE_TOOL_CONTENT = f"tool input token={FAKE_API_KEY}"


def test_args_style_logging_is_redacted_after_formatting() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingLogFilter())
    logger = logging.getLogger("curie-telemetry-redact-args")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("authorization=%s", FAKE_BEARER)

    output = stream.getvalue()
    assert FAKE_BEARER not in output
    assert "[REDACTED:" in output


def test_recursive_value_redaction_preserves_container_and_scalar_types() -> None:
    value = {
        "scalar": FAKE_API_KEY,
        "list": ["safe", FAKE_TOOL_CONTENT],
        "tuple": (FAKE_BEARER, 3, True),
        "nested": {"prompt": FAKE_API_KEY},
    }

    scrubbed = redact_value(value)

    assert isinstance(scrubbed, dict)
    assert isinstance(scrubbed["list"], list)
    assert isinstance(scrubbed["tuple"], tuple)
    assert scrubbed["tuple"][1:] == (3, True)
    rendered = repr(scrubbed)
    assert FAKE_API_KEY not in rendered
    assert FAKE_BEARER not in rendered
    assert "[REDACTED:" in rendered


def test_redaction_keeps_safe_correlation_and_exact_non_string_types() -> None:
    safe = "trace_id=11111111111111111111111111111111 outcome=success"
    assert redact_text(safe) == safe
    for value in (0, 42, True, False, 1.5, None):
        redacted = redact_value(value)
        assert redacted == value
        assert type(redacted) is type(value)
