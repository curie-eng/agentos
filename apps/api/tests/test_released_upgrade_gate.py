"""The released-upgrade gate must prove the migrated database is READABLE (#2098).

`scripts/check-released-upgrade.py` proves migrations RUN against a released
database. It cannot prove the result loads: #1914 is a row migration 0021
backfilled verbatim out of `agents.slack_channel`, which then failed
`ChannelBinding`'s address rule and made `GET /agents` return 500 for every
agent, not just the one holding it.

Two phases close that gap -- a `seed` before the candidate upgrade and a
`read-back` after it -- and this module is their contract. It is the frozen API
surface the gate and its read-back runner are built to:

* `SelfTestDirection` / `SELF_TEST_DIRECTIONS` -- the pinned direction table,
  which must keep #1706's negative control (a must-FAIL direction) while adding
  a must-PASS one, because a self test that only knows how to fail is inert.
* `SeedAgent` / `SEED_FIXTURE` -- the seeded rows, asserted here against the
  LIVE `_validate_channel_binding`, so a fixture softened to make the gate green
  goes red instead.
* `ColumnInfo` / `_plan_seed_statements` / `_detect_approval_route_era` -- the
  schema-adaptive planner, which must never silently seed nothing and must pick
  the 0021 channel era and the 0034 approval-route era INDEPENDENTLY (#2098).
* `scripts/released_upgrade_readback.py::_collect_failures` -- the pure
  assertion helper, which must not pass vacuously and must name the DATA before
  it names Pydantic.

Everything here is a pure unit test with stubs. No test creates a git worktree,
runs `uv sync`, runs alembic, or touches Postgres; the live gate is exercised by
running it, not by a pytest that costs four minutes.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from curie_api.schemas import _validate_channel_binding

REPO_ROOT = Path(__file__).resolve().parents[3]
GATE_SCRIPT = REPO_ROOT / "scripts" / "check-released-upgrade.py"
READBACK_SCRIPT = REPO_ROOT / "scripts" / "released_upgrade_readback.py"

# Direction 1, retained verbatim from #1706. The markers are the defense against
# a MissingGreenlet (or any other incidental error) masquerading as the expected
# revision collision, so they are quoted here exactly, not paraphrased.
COLLISION_MARKERS = (
    "asyncpg.exceptions.UndefinedTableError",
    'relation "curie.agent_channels" does not exist',
    "0022_approvals_reply_kind.py",
)


def _load_script(module_name: str, path: Path) -> ModuleType:
    """Load a `scripts/` file by path.

    `check-released-upgrade.py` has a hyphen and is not importable by name, so
    both modules load the same way, following `test_alembic_revision_gate.py`'s
    `REPO_ROOT` anchor. A missing file fails this one test loudly instead of
    taking the whole collection down with an import error.
    """

    if not path.is_file():
        pytest.fail(f"expected {path} to exist")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        pytest.fail(f"could not build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    return _load_script("check_released_upgrade", GATE_SCRIPT)


@pytest.fixture(scope="module")
def readback() -> ModuleType:
    return _load_script("released_upgrade_readback", READBACK_SCRIPT)


def _direction(gate: ModuleType, candidate_ref: str) -> Any:
    matching = [
        direction
        for direction in gate.SELF_TEST_DIRECTIONS
        if direction.candidate_ref == candidate_ref
    ]
    assert len(matching) == 1, (
        f"expected exactly one direction with candidate ref {candidate_ref}, "
        f"got {len(matching)}"
    )
    return matching[0]


# --------------------------------------------------------------------------
# T1 -- the direction table
# --------------------------------------------------------------------------


def test_direction_is_a_frozen_record_of_refs_expectation_and_markers(
    gate: ModuleType,
) -> None:
    """The row shape: two refs, an expected failure phase, and pinned markers."""
    field_names = {field.name for field in dataclasses.fields(gate.SelfTestDirection)}

    assert field_names == {
        "released_ref",
        "candidate_ref",
        "expect_failure_phase",
        "markers",
    }
    assert isinstance(gate.SELF_TEST_DIRECTIONS, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        gate.SELF_TEST_DIRECTIONS[0].released_ref = "v0.0.0"


def test_exactly_one_direction_pins_a_read_back_failure(gate: ModuleType) -> None:
    """#1914's reproduction: v0.6.2 upgrades cleanly to v0.7.3 and still fails."""
    read_back = [
        direction
        for direction in gate.SELF_TEST_DIRECTIONS
        if direction.expect_failure_phase == "read-back"
    ]

    assert len(read_back) == 1
    assert read_back[0].released_ref == "v0.6.2"
    assert read_back[0].candidate_ref == "v0.7.3"


def test_exactly_one_direction_must_pass_cleanly(gate: ModuleType) -> None:
    """The must-PASS direction. Without it the self test only knows how to fail."""
    passing = [
        direction
        for direction in gate.SELF_TEST_DIRECTIONS
        if direction.expect_failure_phase is None
    ]

    assert len(passing) == 1
    assert passing[0].released_ref == "v0.6.2"
    assert passing[0].candidate_ref == "v0.8.0"


def test_the_1706_collision_direction_is_retained_with_all_three_markers(
    gate: ModuleType,
) -> None:
    """#1706's coverage is EXTENDED here, never replaced."""
    direction = _direction(gate, "v0.7.0-rc.1")

    assert direction.released_ref == "v0.6.2"
    assert direction.expect_failure_phase == "candidate upgrade"
    for marker in COLLISION_MARKERS:
        assert marker in direction.markers, f"lost collision marker {marker!r}"


# --------------------------------------------------------------------------
# T2 -- `_run_self_test` fails loudly on either mismatch shape
# --------------------------------------------------------------------------


def _matching_result(gate: ModuleType, direction: Any) -> Any:
    """The `PairResult` a direction declares it expects."""
    if direction.expect_failure_phase is None:
        return gate.PairResult("read-back", 0, "")
    return gate.PairResult(
        direction.expect_failure_phase, 1, "\n".join(direction.markers)
    )


def _stub_pair_walk(
    gate: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
) -> None:
    """Every direction returns what it declared, except the named overrides.

    Keyed on candidate ref, since the released ref is `v0.6.2` for all three.
    """

    results = {
        direction.candidate_ref: _matching_result(gate, direction)
        for direction in gate.SELF_TEST_DIRECTIONS
    }
    results.update(overrides)

    def _fake_resolve_ref(ref: str) -> str:
        return "0" * 40

    def _fake_run_pair(**kwargs: Any) -> Any:
        return results[kwargs["candidate_ref"]]

    monkeypatch.setattr(gate, "_resolve_ref", _fake_resolve_ref)
    monkeypatch.setattr(gate, "_run_pair", _fake_run_pair)


def test_self_test_passes_when_every_direction_matches_its_expectation(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control: without it the mismatch tests could pass vacuously."""
    _stub_pair_walk(gate, monkeypatch, {})

    assert gate._run_self_test() == 0


def test_an_expected_failure_that_passed_names_that_direction(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that silently stopped exercising the read path must not read green."""
    _stub_pair_walk(
        gate, monkeypatch, {"v0.7.3": gate.PairResult("read-back", 0, "")}
    )

    with pytest.raises(gate.GateError) as excinfo:
        gate._run_self_test()

    message = str(excinfo.value)
    assert "v0.7.3" in message
    assert "v0.7.0-rc.1" not in message
    assert "v0.8.0" not in message


def test_an_expected_pass_that_failed_names_the_direction_and_the_phase(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of AC6: both mismatch shapes are loud, not just one."""
    _stub_pair_walk(
        gate,
        monkeypatch,
        {"v0.8.0": gate.PairResult("candidate upgrade", 1, "boom")},
    )

    with pytest.raises(gate.GateError) as excinfo:
        gate._run_self_test()

    message = str(excinfo.value)
    assert "v0.8.0" in message
    assert "candidate upgrade" in message
    assert "v0.7.3" not in message
    assert "v0.7.0-rc.1" not in message


def test_a_failure_in_the_right_phase_missing_a_marker_names_the_marker(
    gate: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker set is the defense against the RIGHT phase failing WRONG.

    A `MissingGreenlet`, or any incidental error, still lands in the declared
    phase. Only the markers separate "the failure we pinned" from "some failure",
    so a direction that loses one is a mismatch, not a pass.
    """

    partial_output = "\n".join(COLLISION_MARKERS[:2])
    _stub_pair_walk(
        gate,
        monkeypatch,
        {"v0.7.0-rc.1": gate.PairResult("candidate upgrade", 1, partial_output)},
    )

    with pytest.raises(gate.GateError) as excinfo:
        gate._run_self_test()

    message = str(excinfo.value)
    assert "0022_approvals_reply_kind.py" in message
    assert "v0.7.3" not in message
    assert "v0.8.0" not in message


# --------------------------------------------------------------------------
# T3 -- the fixture is legacy-shaped, not validator-shaped
# --------------------------------------------------------------------------


def test_the_fixture_carries_both_a_legacy_and_a_valid_neighbour(
    gate: ModuleType,
) -> None:
    """A fixture of only-legacy or only-valid rows cannot prove the property."""
    legacy = [agent for agent in gate.SEED_FIXTURE if agent.legacy]
    valid = [agent for agent in gate.SEED_FIXTURE if not agent.legacy]

    assert len(legacy) >= 2, "both documented legacy families must be seeded"
    assert len(valid) >= 1, "the valid neighbour proves every agent is returned"
    assert len({agent.name for agent in gate.SEED_FIXTURE}) == len(gate.SEED_FIXTURE)
    assert len({agent.address for agent in gate.SEED_FIXTURE}) == len(
        gate.SEED_FIXTURE
    )


def test_every_legacy_fixture_address_is_rejected_by_the_live_validator(
    gate: ModuleType,
) -> None:
    """The AC1 guarantee: shapes the RELEASED code permitted, read from the gate.

    Read from `SEED_FIXTURE` rather than restated as literals on purpose. If
    someone softens an address to make the gate green, the address stops being a
    shape the current write path rejects, and this goes red.
    """

    for agent in gate.SEED_FIXTURE:
        if not agent.legacy:
            continue
        with pytest.raises(ValueError):
            _validate_channel_binding(agent.kind, agent.address)


def test_the_valid_fixture_address_is_accepted_by_the_live_validator(
    gate: ModuleType,
) -> None:
    for agent in gate.SEED_FIXTURE:
        if agent.legacy:
            continue
        assert _validate_channel_binding(agent.kind, agent.address) == agent.address


def test_the_seeded_approval_route_reuses_a_rejected_legacy_address(
    gate: ModuleType,
) -> None:
    """The sibling read projections are only pinned if the route address is legacy."""
    routed = [
        agent
        for agent in gate.SEED_FIXTURE
        if agent.approval_route_address is not None
    ]

    assert len(routed) == 1
    assert routed[0].legacy
    assert routed[0].approval_route_address == routed[0].address


# --------------------------------------------------------------------------
# T4 -- the seed planner branches correctly
# --------------------------------------------------------------------------


def _column(
    gate: ModuleType,
    table_name: str,
    column_name: str,
    *,
    nullable: bool = True,
    default: str | None = None,
) -> Any:
    return gate.ColumnInfo(
        table_name=table_name,
        column_name=column_name,
        is_nullable="YES" if nullable else "NO",
        column_default=default,
    )


def _legacy_columns(gate: ModuleType) -> tuple[Any, ...]:
    """v0.6.2's shape: `agents.slack_channel` NOT NULL, no `agent_channels`."""
    return (
        _column(gate, "agents", "id", nullable=False, default="gen_random_uuid()"),
        _column(gate, "agents", "name", nullable=False),
        _column(gate, "agents", "slack_channel", nullable=False),
        _column(gate, "agents", "created_at", nullable=False, default="now()"),
        _column(gate, "agents", "approval_routes"),
    )


def _agent_channels_columns(gate: ModuleType) -> tuple[Any, ...]:
    """v0.8.0's shape: `slack_channel` gone, `agent_channels` present."""
    return (
        _column(gate, "agents", "id", nullable=False, default="gen_random_uuid()"),
        _column(gate, "agents", "name", nullable=False),
        _column(gate, "agents", "created_at", nullable=False, default="now()"),
        _column(gate, "agents", "approval_routes"),
        _column(
            gate,
            "agent_channels",
            "id",
            nullable=False,
            default="gen_random_uuid()",
        ),
        _column(gate, "agent_channels", "agent_id", nullable=False),
        _column(gate, "agent_channels", "kind", nullable=False),
        _column(gate, "agent_channels", "address", nullable=False),
        _column(gate, "agent_channels", "generation", nullable=False, default="0"),
        _column(gate, "agent_channels", "endpoint"),
        _column(gate, "agent_channels", "adapter"),
    )


def test_legacy_column_branch_writes_every_address_into_curie_agents(
    gate: ModuleType,
) -> None:
    statements = gate._plan_seed_statements(
        _legacy_columns(gate),
        approval_route_era=gate.APPROVAL_ROUTE_ERA_LEGACY,
    )

    rendered = "\n".join(statements)
    assert statements, "the seed must never plan nothing"
    assert "curie.agents" in rendered
    assert "slack_channel" in rendered
    assert "curie.agent_channels" not in rendered
    for agent in gate.SEED_FIXTURE:
        assert agent.name in rendered
        assert agent.address in rendered


def test_agent_channels_branch_writes_agent_id_kind_and_address(
    gate: ModuleType,
) -> None:
    statements = gate._plan_seed_statements(
        _agent_channels_columns(gate),
        approval_route_era=gate.APPROVAL_ROUTE_ERA_SPLIT,
    )

    rendered = "\n".join(statements)
    binding_statements = [
        statement for statement in statements if "curie.agent_channels" in statement
    ]
    assert binding_statements, "the agent_channels branch must write bindings"
    binding_text = "\n".join(binding_statements)
    for column in ("agent_id", "kind", "address"):
        assert column in binding_text
    assert "slack_channel" not in rendered
    for agent in gate.SEED_FIXTURE:
        assert agent.name in rendered
        assert agent.address in binding_text
        assert agent.kind in binding_text


def test_legacy_branch_seeds_the_pre_0034_approval_route_shape(
    gate: ModuleType,
) -> None:
    """Pre-0034 storage: `{"deploy": {"channel": ...}}`, rewritten on the way up."""
    rendered = "\n".join(
        gate._plan_seed_statements(
            _legacy_columns(gate),
            approval_route_era=gate.APPROVAL_ROUTE_ERA_LEGACY,
        )
    )

    routed = next(
        agent
        for agent in gate.SEED_FIXTURE
        if agent.approval_route_address is not None
    )
    assert "approval_routes" in rendered
    assert "channel" in rendered
    assert routed.approval_route_address in rendered


def test_agent_channels_branch_seeds_the_post_0034_approval_route_shape(
    gate: ModuleType,
) -> None:
    """Post-0034 storage: the split `{"resolution": {"kind", "address"}}` shape."""
    rendered = "\n".join(
        gate._plan_seed_statements(
            _agent_channels_columns(gate),
            approval_route_era=gate.APPROVAL_ROUTE_ERA_SPLIT,
        )
    )

    routed = next(
        agent
        for agent in gate.SEED_FIXTURE
        if agent.approval_route_address is not None
    )
    assert "approval_routes" in rendered
    assert "resolution" in rendered
    assert routed.approval_route_address in rendered


def _routed_agent(gate: ModuleType) -> Any:
    """The one fixture row that carries an approval route."""
    return next(
        agent
        for agent in gate.SEED_FIXTURE
        if agent.approval_route_address is not None
    )


def _legacy_route_json(gate: ModuleType) -> str:
    """The pre-0034 route literal, built the way the planner builds it."""
    routed = _routed_agent(gate)
    return json.dumps({"deploy": {"channel": routed.approval_route_address}})


def _split_route_json(gate: ModuleType) -> str:
    """The post-0034 route literal, key order included (#1460)."""
    routed = _routed_agent(gate)
    return json.dumps(
        {
            "deploy": {
                "resolution": {
                    "kind": routed.kind,
                    "address": routed.approval_route_address,
                }
            }
        }
    )


def _fake_released_tree(tmp_path: Path, *, revisions: tuple[str, ...]) -> Path:
    """A worktree-shaped directory holding just the named revision files."""
    versions = tmp_path / "apps" / "api" / "alembic" / "versions"
    versions.mkdir(parents=True)
    for name in revisions:
        (versions / name).write_text("# stub revision\n", encoding="utf-8")
    return tmp_path


def test_agent_channels_branch_with_a_legacy_route_era_seeds_the_channel_shape(
    gate: ModuleType,
) -> None:
    """The between-eras quadrant: post-0021 bindings, pre-0034 route (#2098).

    A released ref in v0.7.0..v0.7.3 binds channels through `agent_channels` and
    STILL stores `{"deploy": {"channel": ...}}`. Reading the route shape off the
    0021 discriminator seeds `resolution` there -- a future-shaped fixture the
    released code could never have produced -- and the candidate's 0034 (#1460)
    then rejects it as an unknown legacy key, so the gate blames the migration
    for its own seed. Revert the era split and this test goes red.
    """

    rendered = "\n".join(
        gate._plan_seed_statements(
            _agent_channels_columns(gate),
            approval_route_era=gate.APPROVAL_ROUTE_ERA_LEGACY,
        )
    )

    assert "curie.agent_channels" in rendered
    assert _legacy_route_json(gate) in rendered
    assert "resolution" not in rendered


def test_agent_channels_branch_with_a_split_route_era_seeds_the_resolution_shape(
    gate: ModuleType,
) -> None:
    """Post-0034 released refs keep the split shape: the era is a choice, not a drop."""

    rendered = "\n".join(
        gate._plan_seed_statements(
            _agent_channels_columns(gate),
            approval_route_era=gate.APPROVAL_ROUTE_ERA_SPLIT,
        )
    )

    assert _split_route_json(gate) in rendered
    assert _legacy_route_json(gate) not in rendered


def test_legacy_column_branch_with_a_legacy_route_era_seeds_the_channel_shape(
    gate: ModuleType,
) -> None:
    """Pre-0021 implies pre-0034, so this quadrant is unchanged by the era split."""

    rendered = "\n".join(
        gate._plan_seed_statements(
            _legacy_columns(gate),
            approval_route_era=gate.APPROVAL_ROUTE_ERA_LEGACY,
        )
    )

    assert "slack_channel" in rendered
    assert _legacy_route_json(gate) in rendered
    assert "resolution" not in rendered


def test_a_released_tree_holding_0034_is_the_split_era(
    gate: ModuleType,
    tmp_path: Path,
) -> None:
    """The released WORKTREE is the authority: 0034 adds no schema to introspect."""

    tree = _fake_released_tree(
        tmp_path,
        revisions=("0021_agent_channels.py", "0034_approval_route_targets.py"),
    )

    assert gate._detect_approval_route_era(tree) == gate.APPROVAL_ROUTE_ERA_SPLIT


def test_a_released_tree_without_0034_is_the_legacy_era(
    gate: ModuleType,
    tmp_path: Path,
) -> None:
    """v0.7.0..v0.7.3's shape: 0021 has run, 0034 has not."""

    tree = _fake_released_tree(
        tmp_path,
        revisions=("0021_agent_channels.py", "0033_action_gate_approval.py"),
    )

    assert gate._detect_approval_route_era(tree) == gate.APPROVAL_ROUTE_ERA_LEGACY


def test_a_released_tree_with_no_versions_directory_raises(
    gate: ModuleType,
    tmp_path: Path,
) -> None:
    """No versions directory means the tree is not what we think it is.

    Guessing an era from a tree we cannot read is how a future-shaped fixture
    gets seeded in the first place, so refuse instead of defaulting.
    """

    with pytest.raises(gate.GateError) as excinfo:
        gate._detect_approval_route_era(tmp_path)

    assert "alembic versions directory" in str(excinfo.value)


def test_an_unrecognized_approval_route_era_raises_naming_the_value(
    gate: ModuleType,
) -> None:
    """No default and no fallthrough: a bad era must never select a shape."""

    with pytest.raises(gate.GateError) as excinfo:
        gate._plan_seed_statements(
            _agent_channels_columns(gate), approval_route_era="0034"
        )

    message = str(excinfo.value)
    assert "0034" in message
    assert gate.APPROVAL_ROUTE_ERA_LEGACY in message
    assert gate.APPROVAL_ROUTE_ERA_SPLIT in message


def test_neither_binding_location_raises_naming_both_candidates(
    gate: ModuleType,
) -> None:
    """A seed that no-ops turns the read-back into a vacuous pass. Fail instead."""
    columns = (
        _column(gate, "agents", "id", nullable=False, default="gen_random_uuid()"),
        _column(gate, "agents", "name", nullable=False),
    )

    with pytest.raises(gate.GateError) as excinfo:
        gate._plan_seed_statements(
            columns, approval_route_era=gate.APPROVAL_ROUTE_ERA_LEGACY
        )

    message = str(excinfo.value)
    assert "agents.slack_channel" in message
    assert "agent_channels" in message


def test_an_unfilled_mandatory_column_raises_naming_that_column(
    gate: ModuleType,
) -> None:
    """The next person to add a NOT NULL column is told what to add.

    Without this they get a bare Postgres not-null error from inside a CI gate
    they did not write.
    """

    columns = (
        *_legacy_columns(gate),
        _column(gate, "agents", "gate_probe_required", nullable=False),
    )

    with pytest.raises(gate.GateError) as excinfo:
        gate._plan_seed_statements(
            columns, approval_route_era=gate.APPROVAL_ROUTE_ERA_LEGACY
        )

    assert "gate_probe_required" in str(excinfo.value)


# --------------------------------------------------------------------------
# T5 -- the read-back runner's pure assertion helper
# --------------------------------------------------------------------------

PYDANTIC_TEXT = (
    "1 validation error for AgentOut\nchannel.address\n  Value error, "
    "slack channel 'C-0a1b2c3d' is not a Slack channel ID"
)


def _expected_names(gate: ModuleType) -> tuple[str, ...]:
    return tuple(agent.name for agent in gate.SEED_FIXTURE)


def _expected_addresses(gate: ModuleType) -> tuple[str, ...]:
    return tuple(agent.address for agent in gate.SEED_FIXTURE)


def _dump(readback: ModuleType, name: str, address: str) -> Any:
    """One agent that serialized cleanly, with its address nested as it really is."""
    return readback.AgentDump(
        name=name,
        addresses=(address,),
        dump={
            "name": name,
            "channels": [{"kind": "slack", "address": address}],
        },
        error=None,
    )


def _all_dumps(gate: ModuleType, readback: ModuleType) -> tuple[Any, ...]:
    return tuple(
        _dump(readback, agent.name, agent.address) for agent in gate.SEED_FIXTURE
    )


def test_an_empty_result_set_is_a_failure_not_a_pass(
    gate: ModuleType, readback: ModuleType
) -> None:
    """The vacuous-pass guard: zero rows means the seed did nothing."""
    failures = readback._collect_failures(
        (),
        expected_agents=_expected_names(gate),
        expected_addresses=_expected_addresses(gate),
    )

    assert failures
    assert any("no agents" in failure.lower() for failure in failures)


def test_all_expectations_met_reports_no_failures(
    gate: ModuleType, readback: ModuleType
) -> None:
    """Positive control for the helper."""
    failures = readback._collect_failures(
        _all_dumps(gate, readback),
        expected_agents=_expected_names(gate),
        expected_addresses=_expected_addresses(gate),
    )

    assert failures == ()


def test_a_dump_missing_an_expected_address_names_that_address(
    gate: ModuleType, readback: ModuleType
) -> None:
    """A migration that silently drops the seeded row must not read green."""
    dropped = gate.SEED_FIXTURE[1]
    dumps = tuple(
        readback.AgentDump(
            name=dropped.name, addresses=(), dump={"name": dropped.name}, error=None
        )
        if agent.name == dropped.name
        else _dump(readback, agent.name, agent.address)
        for agent in gate.SEED_FIXTURE
    )

    failures = readback._collect_failures(
        dumps,
        expected_agents=_expected_names(gate),
        expected_addresses=_expected_addresses(gate),
    )

    assert any(dropped.address in failure for failure in failures)


def test_a_missing_expected_agent_name_is_reported(
    gate: ModuleType, readback: ModuleType
) -> None:
    missing = gate.SEED_FIXTURE[0]
    dumps = tuple(
        _dump(readback, agent.name, agent.address)
        for agent in gate.SEED_FIXTURE
        if agent.name != missing.name
    )

    failures = readback._collect_failures(
        dumps,
        expected_agents=_expected_names(gate),
        expected_addresses=_expected_addresses(gate),
    )

    assert any(missing.name in failure for failure in failures)


def test_a_validation_failure_names_the_data_before_pydantic(
    gate: ModuleType, readback: ModuleType
) -> None:
    """#1914's literal complaint: "an error naming Pydantic rather than the data".

    Ordering, not membership. An operator reading the CI log must see WHICH agent
    and WHICH address before the traceback text explains itself.
    """

    broken = gate.SEED_FIXTURE[1]
    dumps = tuple(
        readback.AgentDump(
            name=broken.name,
            addresses=(broken.address,),
            dump=None,
            error=PYDANTIC_TEXT,
        )
        if agent.name == broken.name
        else _dump(readback, agent.name, agent.address)
        for agent in gate.SEED_FIXTURE
    )

    failures = readback._collect_failures(
        dumps,
        expected_agents=_expected_names(gate),
        expected_addresses=_expected_addresses(gate),
    )

    rendered = next(
        failure
        for failure in failures
        if broken.name in failure and "validation error" in failure
    )
    name_index = rendered.index(broken.name)
    address_index = rendered.index(broken.address)
    pydantic_index = rendered.index("1 validation error for AgentOut")
    assert name_index < pydantic_index
    assert address_index < pydantic_index
