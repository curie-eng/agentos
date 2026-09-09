"""Subprocess driver that runs the REAL Discord adapter entrypoint.

This is not a test module; pytest must not collect it. `test_telemetry.py`
spawns it as ``[sys.executable, str(DRIVER_PATH)]`` because "the process's logs
are JSON, redacted, and exported" is a property of a *process*, and the only
honest way to observe it is to start one and read what it actually wrote.

**What is real.** `curie_discord_adapter.main.main()` itself — including the
telemetry bootstrap this ticket adds and its ``finally`` — plus `DiscordConfig`,
`DiscordState`, `DiscordAdapter`, `DiscordReplyService`, `create_reply_app` and
`run()`'s own ``asyncio.gather`` / ``finally`` teardown. Nothing inside the
package is patched, and in particular nothing about logging is touched here: if
a test in this suite passes, it passes because the shipped entrypoint behaves.

**What is faked, and why only this.** Exactly two coroutines, both of which are
*external network peers* rather than code under test:

- ``discord.Client.start`` — Discord's Gateway is an unauthorized external
  service for this run. Logging into it would require a real bot token and would
  put traffic on a third party's servers, which this repo's tests never do.
- ``uvicorn.Server.serve`` — it otherwise blocks forever, so ``asyncio.gather``
  in `run()` could never return and the process could never reach the
  ``finally`` whose flush is the very thing under test. Binding a real port adds
  nothing: no test here speaks HTTP to the adapter.

Faking a peer is not faking the code under test. The substitution is at the
socket-owning boundary, one call deep, and every statement whose behavior any
assertion depends on still executes for real.

The scenario is chosen by ``CURIE_DISCORD_TEST_SCENARIO``; the planted synthetic
credential is supplied by ``CURIE_DISCORD_TEST_PLANTED_SECRET`` so the test file
owns the constant and this driver never hardcodes anything secret-shaped.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import discord
import uvicorn
from curie_discord_adapter.main import main

SCENARIO_ENV = "CURIE_DISCORD_TEST_SCENARIO"
PLANTED_SECRET_ENV = "CURIE_DISCORD_TEST_PLANTED_SECRET"

# The carrier text of the planted record. It contains no secret, so a test can
# assert it survived into the output as a negative control *before* asserting
# that the secret did not — otherwise "the secret is absent" is also satisfied by
# output that never carried the record at all.
PLANTED_CARRIER = "planted telemetry probe record for the discord adapter"

# A logger for a module that does not exist. The point is precisely that it does
# not: `configure_service_logging` is handed the *package* logger, so every
# descendant is covered by `Logger.callHandlers` walking up — including modules
# nobody has written yet. Bootstrapping module loggers one at a time would leave
# this one on the root handler, and the `descendant` scenario would expose it.
FUTURE_MODULE_LOGGER = "curie_discord_adapter.a_module_added_later"


def _emit_planted_record(logger_name: str) -> None:
    """Log the planted credential as a ``%s`` ARG, never baked into the template.

    Two different code paths must both scrub it, and only an arg exercises both:
    `RedactingLogFilter` sees it via ``record.getMessage()`` on the stderr side,
    while `_otlp_body` walks ``record.args`` against the percent-format template
    on the OTLP side. A secret pre-interpolated into the template would silently
    skip the second one and the OTLP assertions would prove less than they claim.
    """
    logging.getLogger(logger_name).info(
        "%s: credential=%s", PLANTED_CARRIER, os.environ[PLANTED_SECRET_ENV]
    )


def _install_fake_peers(scenario: str) -> None:
    async def fake_serve(self: uvicorn.Server, *args: Any, **kwargs: Any) -> None:
        # Return promptly rather than immediately: `run()` gathers this with the
        # Gateway coroutine, and yielding to the loop keeps the ordering closer
        # to the real one without making any test depend on the delay.
        await asyncio.sleep(0.01)

    async def fake_start(self: discord.Client, token: str, *, reconnect: bool = True) -> None:
        if scenario == "quiet":
            return
        if scenario == "planted_secret":
            _emit_planted_record("curie_discord_adapter.main")
            return
        if scenario == "third_party":
            # A CHILD of `discord`, not `discord` itself. The adapter runs under
            # third-party loggers it does not own, and `main()` must hand the
            # bootstrap the parent names — so proving coverage means proving
            # propagation from a child, the same way the package logger covers
            # `curie_discord_adapter.*`. Hardcoding `discord` would pass against
            # an implementation that configured exactly one logger by name and
            # still left `discord.client`, `discord.gateway` and the rest on
            # `lastResort`.
            logging.getLogger("discord.client").warning(
                "%s: credential=%s", PLANTED_CARRIER, os.environ[PLANTED_SECRET_ENV]
            )
            return
        if scenario == "descendant":
            _emit_planted_record("curie_discord_adapter.main")
            logging.getLogger(FUTURE_MODULE_LOGGER).info(
                "a module added after the bootstrap was written also logs"
            )
            return
        if scenario == "error_exit":
            _emit_planted_record("curie_discord_adapter.main")
            # The message carries NO secret on purpose. Python prints an
            # uncaught traceback itself, outside the logging package and
            # therefore outside the redaction filter, so a secret in the
            # exception text would leak legitimately — and a test asserting its
            # absence would then be asserting something false about the design.
            raise RuntimeError("synthetic gateway failure with no credential in its message")
        raise SystemExit(f"unknown scenario {scenario!r}")

    uvicorn.Server.serve = fake_serve  # type: ignore[method-assign]
    discord.Client.start = fake_start  # type: ignore[method-assign]


if __name__ == "__main__":
    _install_fake_peers(os.environ[SCENARIO_ENV])
    main()
