"""Closed, value-free application events for correlated OTEL logs."""

from __future__ import annotations

import logging

from .attributes import log_event_names_for

_EVENT_MARKER = "_curie_otel_application_event"
_SERVICE_MARKER = "_curie_otel_service"
_SERVICE_ROOTS = {
    "curie-api": "curie_api",
    "curie-dispatcher": "curie_dispatcher",
    "curie-worker": "curie_worker",
    "curie-runner": "curie_runner",
}
_STANDARD_RECORD_FIELDS = frozenset(
    vars(
        logging.LogRecord(
            name="",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="",
            args=(),
            exc_info=None,
        )
    )
) | {"asctime", "message"}


def _logger_service(logger_name: str) -> str | None:
    normalized = logger_name.replace("-", "_")
    for service_name, root in _SERVICE_ROOTS.items():
        if normalized == root or normalized.startswith(f"{root}."):
            return service_name
    return None


def emit_log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
) -> None:
    """Emit one audited, value-free event through the existing logger path.

    The event body is the entire record: callers cannot attach arbitrary
    messages, arguments, exceptions, or attributes. Normal diagnostics keep
    flowing to stderr, but the OTEL handler accepts only records created here.
    """

    service_name = _logger_service(logger.name)
    if service_name is None or event not in log_event_names_for(service_name):
        raise ValueError(f"unsupported telemetry log event {event!r}")
    logger.log(
        level,
        event,
        extra={
            _EVENT_MARKER: event,
            _SERVICE_MARKER: service_name,
        },
    )


class CuratedLogEventFilter(logging.Filter):
    """Admit only marked events in this handler's closed service vocabulary."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name
        self._events = log_event_names_for(service_name)

    def filter(self, record: logging.LogRecord) -> bool:
        event = getattr(record, _EVENT_MARKER, None)
        extras = set(vars(record)) - _STANDARD_RECORD_FIELDS - {
            _EVENT_MARKER,
            _SERVICE_MARKER,
        }
        return bool(
            getattr(record, _SERVICE_MARKER, None) == self._service_name
            and isinstance(event, str)
            and event in self._events
            and record.msg == event
            and not record.args
            and record.exc_info is None
            and record.stack_info is None
            and not extras
        )
