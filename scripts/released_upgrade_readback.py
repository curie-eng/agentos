"""Read the migrated database back through the CANDIDATE tree's read models.

`check-released-upgrade.py` proves the migrations RUN. It cannot prove the
result LOADS, which is exactly what #1914 was: migration 0021 backfilled
`curie.agent_channels` verbatim out of `agents.slack_channel`, and the rows it
copied then failed `ChannelBinding`'s address rule, so `GET /agents` returned
500 for every agent -- not only the one holding the offending address.

This file is the second half of the gate (#2098). It is committed at HEAD and
never changes across directions, but it is EXECUTED inside the candidate tree's
environment:

    uv run --frozen --project <candidate_tree> \
        --directory <candidate_tree>/apps/api \
        python <REPO_ROOT>/scripts/released_upgrade_readback.py \
        --expect-agent ... --expect-address ...

so `import curie_api` resolves to the CANDIDATE's package. One fixed assertion
harness, pointed at whichever version of the read models is under judgement.

Three constraints follow from that inversion, and all three are load-bearing:

1. **Only stdlib, sqlalchemy and `curie_api` may be imported.** `sys.path[0]` is
   HEAD's `scripts/` while the installed package is the candidate's, so an
   import of a sibling module in `scripts/` would silently mix two versions.
   In particular this file does NOT import `check-released-upgrade.py`; the
   fixture arrives entirely through argv.
2. **No field name may be hard-coded.** `AgentOut` carries a singular
   `channel: ChannelBinding` at v0.7.3 and a plural
   `channels: list[ChannelBindingOut]` on main, so naming either attribute
   would make the runner work on exactly one side of the fix it is judging.
   ORM rows are loaded and handed whole to `AgentOut.model_validate`, and the
   address expectations are checked by searching the SERIALIZED dump rather
   than by reading a named field.
3. **It cannot pass vacuously.** Zero agents is a FAILURE. A seed that silently
   wrote nothing, or a migration that silently dropped the seeded rows, is the
   precise failure mode this gate exists to close, so "the read path raised
   nothing" is not sufficient evidence on its own.
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

from curie_api import models
from curie_api.schemas import AgentOut
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

# JSON keys whose string values are treated as channel addresses when scraping
# an agent's `approval_routes` for the error message. BOTH eras are listed on
# purpose: pre-0034 storage is `{"deploy": {"channel": X}}` and post-0034 is
# `{"deploy": {"resolution": {"kind": ..., "address": X}}}`, and the runner has
# to name the offending address on either side of that rewrite.
_ADDRESS_KEYS = frozenset({"address", "channel"})


@dataclass(frozen=True)
class AgentDump:
    """One agent as the candidate read models saw it.

    `dump` is the serialized payload when `AgentOut.model_validate` succeeded and
    `None` when it raised; `error` is the mirror of that. `addresses` is scraped
    from the ORM row itself rather than from `dump`, because the case that
    matters most -- a validation failure -- has no dump to read the address out
    of, and #1914's complaint was precisely that the failure named Pydantic
    instead of the data.
    """

    name: str
    addresses: tuple[str, ...]
    dump: dict[str, Any] | None
    error: str | None


def _json_strings_under(value: Any, keys: frozenset[str]) -> list[str]:
    """Collect every string stored under one of `keys`, at any depth."""

    found: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in keys and isinstance(nested, str):
                found.append(nested)
            found.extend(_json_strings_under(nested, keys))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_json_strings_under(nested, keys))
    return found


def _related_addresses(agent: Any) -> tuple[str, ...]:
    """Every channel address reachable from an ORM agent row.

    Walks the mapper's relationships and reads an `address` attribute off
    whatever they yield, rather than naming the relationship. That is the same
    version-agnostic requirement as constraint 2 in the module docstring: the
    binding relationship is `channel` (singular) at v0.7.3 and `channels`
    (plural) on main, and this runner must work identically on both.

    The walk touches unrelated relationships (`versions`, `deployments`) too and
    will load them. That is deliberate and cheap -- the seeded database holds
    three agents and nothing else -- and it keeps the traversal free of any
    per-version knowledge.
    """

    addresses: list[str] = []
    mapper = sqlalchemy_inspect(type(agent))
    for relationship in mapper.relationships:
        try:
            value = getattr(agent, relationship.key)
        except Exception:  # noqa: BLE001 - a relationship we cannot load is not a verdict
            continue
        related_objects = value if isinstance(value, (list, set, tuple)) else [value]
        for related in related_objects:
            address = getattr(related, "address", None)
            if isinstance(address, str):
                addresses.append(address)
    addresses.extend(
        _json_strings_under(getattr(agent, "approval_routes", None), _ADDRESS_KEYS)
    )
    # Order-preserving dedupe: the same address legitimately appears as both a
    # binding and an approval-route target in the fixture, and naming it twice
    # in a failure line reads like two separate problems.
    return tuple(dict.fromkeys(addresses))


def _dump_agents(session: Session) -> tuple[AgentDump, ...]:
    """Serialize every agent through `AgentOut`, capturing failures per row.

    Runs on a SYNC session (via `AsyncSession.run_sync`) so lazy relationship
    loading works regardless of the candidate's `lazy=` strategy. Both tags we
    pin happen to use `lazy="selectin"`, but depending on that would make the
    gate's correctness a property of the version under test.

    Per-row capture rather than a single try/except around the loop: the whole
    point of #1914 is that ONE bad row took down the response for all 19 agents,
    so the gate has to be able to say which rows loaded and which did not.
    """

    agents = (
        session.execute(select(models.Agent).order_by(models.Agent.name))
        .scalars()
        .all()
    )
    dumps: list[AgentDump] = []
    for agent in agents:
        addresses = _related_addresses(agent)
        try:
            payload = AgentOut.model_validate(agent).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - any read failure is the verdict
            dumps.append(
                AgentDump(
                    name=agent.name, addresses=addresses, dump=None, error=str(exc)
                )
            )
        else:
            dumps.append(
                AgentDump(
                    name=agent.name, addresses=addresses, dump=payload, error=None
                )
            )
    return tuple(dumps)


async def _load_dumps(database_url: str) -> tuple[AgentDump, ...]:
    engine = create_async_engine(database_url)
    try:
        async with AsyncSession(engine) as session:
            return await session.run_sync(_dump_agents)
    finally:
        await engine.dispose()


def _rendered_addresses(addresses: tuple[str, ...]) -> str:
    if not addresses:
        return "no address recorded"
    return ", ".join(repr(address) for address in addresses)


def _collect_failures(
    dumps: tuple[AgentDump, ...],
    *,
    expected_agents: tuple[str, ...],
    expected_addresses: tuple[str, ...],
) -> tuple[str, ...]:
    """Judge a set of dumps against the seeded expectations. Empty tuple == pass.

    Pure by construction -- no engine, no session, no I/O -- so the gate's
    assertions are unit-testable without a four-minute pair walk.
    """

    if not dumps:
        # The vacuous-pass guard. Without it a seed that wrote nothing, or a
        # migration that dropped the seeded rows, reads as "nothing failed to
        # serialize" -- which is exactly the class of green-but-inert gate
        # #1706 built its negative control against.
        return (
            "read-back found no agents in the migrated database: the seed wrote "
            "nothing or the migration dropped it, so the read path was never "
            "exercised",
        )

    failures: list[str] = []

    # Serialization failures first, and the DATA before the Pydantic text. #1914
    # was reported as "an error naming Pydantic rather than the data", so an
    # operator reading the CI log must see WHICH agent and WHICH address before
    # the validation traceback starts explaining itself. The ordering is the
    # deliverable here, not merely the content.
    for dump in dumps:
        if dump.error is None:
            continue
        failures.append(
            f"agent {dump.name!r} (address {_rendered_addresses(dump.addresses)}) "
            "did not serialize through the candidate read models:\n"
            f"{dump.error}"
        )

    seen_names = {dump.name for dump in dumps}
    for name in expected_agents:
        if name not in seen_names:
            failures.append(
                f"expected agent {name!r} was not returned by the migrated "
                "database: the seeded row did not survive the upgrade"
            )

    # Address presence is checked against the SERIALIZED payload, not against a
    # named field, so the same assertion covers the channel binding and the
    # approval-route target under either era's typing (`dict[str, Any]` at
    # v0.7.3, `dict[str, ApprovalRouteBindingOut]` on main).
    serialized = "\n".join(
        json.dumps(dump.dump, sort_keys=True)
        for dump in dumps
        if dump.dump is not None
    )
    for address in expected_addresses:
        if address not in serialized:
            failures.append(
                f"expected address {address!r} is absent from every serialized "
                "agent: the seeded binding was dropped, rewritten, or never read "
                "back"
            )

    return tuple(failures)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Serialize a migrated Curie database through this tree's read "
            "models and require the seeded rows to load."
        )
    )
    # The expectations arrive as arguments so this runner carries no fixture
    # knowledge: `check-released-upgrade.py` owns `SEED_FIXTURE`, and importing
    # it here would violate the sys.path constraint in the module docstring.
    parser.add_argument(
        "--expect-agent",
        action="append",
        default=[],
        metavar="NAME",
        help="An agent name that must be present in the migrated database.",
    )
    parser.add_argument(
        "--expect-address",
        action="append",
        default=[],
        metavar="ADDRESS",
        help="A channel address that must survive into the serialized agent.",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("read-back requires DATABASE_URL to be set", file=sys.stderr)
        return 1
    if not args.expect_agent or not args.expect_address:
        # Refusing an empty expectation set is part of the vacuous-pass guard: a
        # caller that forgot to pass the fixture would otherwise get a green
        # read-back that asserted nothing at all.
        print(
            "read-back requires at least one --expect-agent and one "
            "--expect-address",
            file=sys.stderr,
        )
        return 1

    dumps = asyncio.run(_load_dumps(database_url))
    for dump in dumps:
        status = "failed" if dump.error is not None else "loaded"
        print(
            f"read-back: agent {dump.name!r} ({_rendered_addresses(dump.addresses)}) "
            f"{status}"
        )

    failures = _collect_failures(
        dumps,
        expected_agents=tuple(args.expect_agent),
        expected_addresses=tuple(args.expect_address),
    )
    if failures:
        print("Released upgrade read-back failed:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(
        f"Released upgrade read-back passed: {len(dumps)} agents serialized "
        "through this tree's read models."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
