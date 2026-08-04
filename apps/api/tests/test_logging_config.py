"""The service's own INFO records actually reach a handler (#1270).

`uvicorn curie_api.main:app` -- the Dockerfile's literal command -- configures
only the three `uvicorn*` loggers and no root entry. So `curie_api.*` had an
effective level of WARNING and no handler, and every INFO record in the service
was discarded by Python's last-resort handler. Not a local artifact: that is
the production path.

The lane that suffered most is the one with no fallback. A webhook delivery can
be inspected in GitHub's UI; a polled deploy has nothing but these records.
"""

from __future__ import annotations

import logging

from curie_api.main import configure_logging, create_app


def test_a_curie_api_info_record_reaches_a_handler(caplog) -> None:
    # The acceptance criterion, and the thing that was actually broken.
    create_app()
    with caplog.at_level(logging.INFO, logger="curie_api"):
        logging.getLogger("curie_api.commitpoller").info("commit poller started")
    assert any("commit poller started" in r.getMessage() for r in caplog.records)


def test_the_logger_has_a_handler_and_an_info_level_after_construction() -> None:
    # caplog installs its own handler, so the test above would pass even if
    # nothing were configured. This asserts the configuration itself.
    create_app()
    logger = logging.getLogger("curie_api")
    assert logger.level == logging.INFO
    assert logger.handlers, "curie_api has no handler; INFO goes to the last-resort handler"
    assert logger.isEnabledFor(logging.INFO)


def test_constructing_many_apps_does_not_multiply_handlers() -> None:
    # Tests build dozens of apps in one process. A handler added per
    # construction would print every line dozens of times, which reads as a
    # duplicate-work bug rather than a logging one.
    create_app()
    first = len(logging.getLogger("curie_api").handlers)
    for _ in range(5):
        create_app()
    assert len(logging.getLogger("curie_api").handlers) == first


def test_it_does_not_configure_the_root_logger() -> None:
    # Root belongs to uvicorn's dictConfig and to whatever the OTel wiring
    # attaches. Taking it over would fight both.
    before = list(logging.getLogger().handlers)
    create_app()
    assert logging.getLogger().handlers == before


def test_records_do_not_propagate_to_root() -> None:
    # With our own handler AND propagation, a root handler configured later
    # emits every record a second time.
    create_app()
    assert logging.getLogger("curie_api").propagate is False


def test_the_level_is_configurable() -> None:
    configure_logging("WARNING")
    logger = logging.getLogger("curie_api")
    assert not logger.isEnabledFor(logging.INFO)
    configure_logging("INFO")
    assert logger.isEnabledFor(logging.INFO)
