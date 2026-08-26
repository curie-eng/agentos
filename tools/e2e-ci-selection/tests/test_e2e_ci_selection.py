"""Executable contract for deterministic end to end CI selection."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SELECTOR = REPO_ROOT / "tools" / "e2e-ci-selection" / "select.py"
REGISTRY = REPO_ROOT / ".github" / "e2e-selection.yaml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"

TIERS = ("skill", "local", "local-release", "cluster")
OUTPUT_KEYS = {
    "skill": "skill",
    "local": "local",
    "local-release": "local_release",
    "cluster": "cluster",
}
APPROVED_ROOT_DOCS = (
    "AGENTS.md",
    "ARCHITECTURE.md",
    "CLAUDE.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "QUICKSTART.md",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
    "TRADEMARKS.md",
    "llms.txt",
)


def _invoke_selector(
    tmp_path: Path,
    *paths: str,
    registry: Path = REGISTRY,
    push: bool = False,
    base: str | None = None,
    head: str | None = None,
    cwd: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    output_path = tmp_path / f"github-output-{len(list(tmp_path.glob('github-output-*')))}"
    command = [sys.executable, str(SELECTOR), "--registry", str(registry)]
    if push:
        command.append("--push")
    if base is not None:
        command.extend(("--base", base))
    if head is not None:
        command.extend(("--head", head))
    for path in paths:
        command.extend(("--path", path))

    environment = os.environ.copy()
    environment["GITHUB_OUTPUT"] = str(output_path)
    completed = subprocess.run(
        command,
        cwd=cwd or tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    output = output_path.read_text() if output_path.exists() else ""
    return completed, output


def _expected_output(*selected: str) -> str:
    selected_tiers = set(selected)
    lines = [
        f"{OUTPUT_KEYS[tier]}={'true' if tier in selected_tiers else 'false'}"
        for tier in TIERS
    ]
    skill_local = ",".join(tier for tier in TIERS[:2] if tier in selected_tiers)
    lines.append(f"skill_local_tiers={skill_local}")
    return "\n".join(lines) + "\n"


def _assert_selection(
    tmp_path: Path,
    path: str,
    selected: tuple[str, ...],
) -> None:
    completed, output = _invoke_selector(tmp_path, path)
    assert completed.returncode == 0, completed.stderr
    assert output == _expected_output(*selected)


@pytest.mark.parametrize(
    ("path", "selected"),
    [
        ("runner/example.py", TIERS),
        ("compose.dev.yaml", ("local",)),
        ("compose/generated.py", ("local-release",)),
        ("charts/curie/values.yaml", ("cluster",)),
        ("apps/api/example.py", ("local", "local-release", "cluster")),
        ("apps/worker/example.py", ("local", "local-release", "cluster")),
        ("otel/collector.yaml", ("local", "local-release")),
        ("cli/example.rs", TIERS),
        ("packages/example.py", TIERS),
        ("pyproject.toml", TIERS),
        ("uv.lock", TIERS),
    ],
)
def test_registry_maps_each_known_surface(
    tmp_path: Path,
    path: str,
    selected: tuple[str, ...],
) -> None:
    _assert_selection(tmp_path, path, selected)


@pytest.mark.parametrize(
    "path",
    [
        "examples/weather/evals/cases.json",
        ".github/e2e-selection.yaml",
        ".github/workflows/ci.yaml",
        "tools/e2e-ci-selection/select.py",
    ],
)
def test_weather_and_enforcement_paths_select_every_tier(tmp_path: Path, path: str) -> None:
    _assert_selection(tmp_path, path, TIERS)


@pytest.mark.parametrize(
    "path",
    [*APPROVED_ROOT_DOCS, "docs/example.md", "docs/guides/getting-started.md"],
)
def test_genuine_documentation_only_selects_no_runtime_e2e_tiers(
    tmp_path: Path,
    path: str,
) -> None:
    _assert_selection(tmp_path, path, ())


@pytest.mark.parametrize(
    ("path", "selected"),
    [
        (".github/e2e-selection.yaml", TIERS),
        (".github/workflows/ci.yaml", TIERS),
        (".github/workflows/README.md", TIERS),
        (".github/action.yml", TIERS),
        ("scripts/README.md", TIERS),
        ("scripts/check-docs.sh", TIERS),
        ("apps/api/README.md", ("local", "local-release", "cluster")),
        ("apps/api/runtime-config.yaml", ("local", "local-release", "cluster")),
        ("packages/plugin-format/README.md", TIERS),
        ("packages/plugin-format/plugin.yaml", TIERS),
        ("examples/weather/README.md", TIERS),
        ("examples/weather/skill-config.yaml", TIERS),
        ("tests/README.md", TIERS),
        ("tests/selector-config.yaml", TIERS),
        ("UNAPPROVED.md", TIERS),
    ],
)
def test_non_allowlisted_paths_never_bypass_runtime_e2e_selection(
    tmp_path: Path,
    path: str,
    selected: tuple[str, ...],
) -> None:
    _assert_selection(tmp_path, path, selected)


def test_mixed_root_documentation_and_runtime_diff_selects_runtime_tiers(
    tmp_path: Path,
) -> None:
    completed, output = _invoke_selector(tmp_path, "ARCHITECTURE.md", "apps/api/main.py")
    assert completed.returncode == 0, completed.stderr
    assert output == _expected_output("local", "local-release", "cluster")


def test_unknown_and_union_selection_are_deterministic(tmp_path: Path) -> None:
    unknown, unknown_output = _invoke_selector(tmp_path, "new-surface/module.py")
    assert unknown.returncode == 0, unknown.stderr
    assert unknown_output == _expected_output(*TIERS)

    forward, forward_output = _invoke_selector(
        tmp_path,
        "charts/curie/values.yaml",
        "compose.dev.yaml",
        "otel/collector.yaml",
    )
    reverse, reverse_output = _invoke_selector(
        tmp_path,
        "otel/collector.yaml",
        "compose.dev.yaml",
        "charts/curie/values.yaml",
    )
    assert forward.returncode == 0, forward.stderr
    assert reverse.returncode == 0, reverse.stderr
    assert forward_output == reverse_output == _expected_output(
        "local",
        "local-release",
        "cluster",
    )


def test_push_selects_every_tier_without_a_repository(tmp_path: Path) -> None:
    completed, output = _invoke_selector(tmp_path, push=True)
    assert completed.returncode == 0, completed.stderr
    assert output == _expected_output(*TIERS)


def test_revisions_select_changed_paths_and_unknown_fallback(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch", "main"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repository,
        check=True,
    )

    known_path = repository / "compose.dev.yaml"
    known_path.write_text("version: one\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repository, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    known_path.write_text("version: two\n")
    subprocess.run(["git", "commit", "-am", "known"], cwd=repository, check=True)
    known_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    known, known_output = _invoke_selector(
        tmp_path,
        base=base,
        head=known_head,
        cwd=repository,
    )
    assert known.returncode == 0, known.stderr
    assert known_output == _expected_output("local")

    (repository / "new-surface.txt").write_text("new\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "unknown"], cwd=repository, check=True)
    unknown_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    unknown, unknown_output = _invoke_selector(
        tmp_path,
        base=known_head,
        head=unknown_head,
        cwd=repository,
    )
    assert unknown.returncode == 0, unknown.stderr
    assert unknown_output == _expected_output(*TIERS)


VALID_REGISTRY = """
version: 1
fallback: [skill, local, local-release, cluster]
rules:
  exact:
    compose.dev.yaml: [local]
  prefixes:
    charts: [cluster]
  ignored_prefixes:
    docs: []
"""


@pytest.mark.parametrize(
    "registry_text",
    [
        VALID_REGISTRY.replace("charts: [cluster]", "charts: [unknown]"),
        VALID_REGISTRY.replace("charts: [cluster]", "charts: [cluster, cluster]"),
        VALID_REGISTRY.replace("compose.dev.yaml: [local]", "compose.dev.yaml: []"),
        VALID_REGISTRY.replace("charts: [cluster]", "charts: []"),
        VALID_REGISTRY.replace("version: 1", "version: true"),
        VALID_REGISTRY.replace(
            "    charts: [cluster]",
            "    charts: [cluster]\n    charts: [local]",
        ),
        VALID_REGISTRY.replace("  exact:\n    compose.dev.yaml: [local]", "  exact: []"),
        VALID_REGISTRY.replace(
            "fallback: [skill, local, local-release, cluster]",
            "fallback: [skill, local]",
        ),
        VALID_REGISTRY.replace("    docs: []", "    charts: []"),
    ],
    ids=(
        "unknown_tier",
        "duplicate_tier",
        "empty_exact_tiers",
        "empty_prefix_tiers",
        "boolean_version",
        "duplicate_rule",
        "malformed_entry",
        "invalid_fallback",
        "ignored_overlap",
    ),
)
def test_selector_rejects_invalid_registries(
    tmp_path: Path,
    registry_text: str,
) -> None:
    registry = tmp_path / "registry.yaml"
    registry.write_text(registry_text)
    completed, _output = _invoke_selector(tmp_path, "charts/example.yaml", registry=registry)
    assert completed.returncode != 0


AGGREGATE_EXPRESSIONS = {
    "changes_result": "${{ needs.changes.result }}",
    "skill_selected": "${{ needs.changes.outputs.skill }}",
    "local_selected": "${{ needs.changes.outputs.local }}",
    "local_release_selected": "${{ needs.changes.outputs.local_release }}",
    "cluster_selected": "${{ needs.changes.outputs.cluster }}",
    "skill_local_result": "${{ needs.e2e-ladder.result }}",
    "local_release_result": "${{ needs.e2e-ladder-release.result }}",
    "cluster_result": "${{ needs.e2e-ladder-cluster.result }}",
}


def test_workflow_consumes_each_selection_output_exactly() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    jobs = workflow["jobs"]
    assert jobs["changes"]["outputs"] == {
        "skill": "${{ steps.filter.outputs.skill }}",
        "local": "${{ steps.filter.outputs.local }}",
        "local_release": "${{ steps.filter.outputs.local_release }}",
        "cluster": "${{ steps.filter.outputs.cluster }}",
        "skill_local_tiers": "${{ steps.filter.outputs.skill_local_tiers }}",
    }

    skill_local = jobs["e2e-ladder"]
    assert skill_local["if"] == (
        "${{ needs.changes.outputs.skill == 'true' || "
        "needs.changes.outputs.local == 'true' }}"
    )
    ladder_steps = [
        step
        for step in skill_local["steps"]
        if step.get("run") == "bash cli/scripts/e2e-ladder.sh"
    ]
    assert len(ladder_steps) == 1
    assert ladder_steps[0]["env"]["CURIE_E2E_TIERS"] == (
        "${{ needs.changes.outputs.skill_local_tiers }}"
    )

    assert jobs["e2e-ladder-release"]["if"] == (
        "${{ needs.changes.outputs.local_release == 'true' }}"
    )
    assert jobs["e2e-ladder-cluster"]["if"] == (
        "${{ needs.changes.outputs.cluster == 'true' }}"
    )


def _aggregate_contract() -> tuple[str, dict[str, str]]:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["e2e-required"]
    assert job["name"] == "E2E required"
    assert set(job["needs"]) == {
        "changes",
        "e2e-ladder",
        "e2e-ladder-release",
        "e2e-ladder-cluster",
    }
    assert job["if"] == "${{ !cancelled() }}"

    candidates = [
        step
        for step in job["steps"]
        if isinstance(step, dict)
        and isinstance(step.get("run"), str)
        and isinstance(step.get("env"), dict)
        and AGGREGATE_EXPRESSIONS["changes_result"] in step["env"].values()
    ]
    assert len(candidates) == 1
    step = candidates[0]
    bindings: dict[str, str] = {}
    for semantic_name, expression in AGGREGATE_EXPRESSIONS.items():
        environment_names = [
            name for name, value in step["env"].items() if value == expression
        ]
        assert len(environment_names) == 1, expression
        bindings[semantic_name] = environment_names[0]
    return step["run"], bindings


def _run_aggregate(
    *,
    script_transform: Callable[[str], str] | None = None,
    **overrides: str,
) -> subprocess.CompletedProcess[str]:
    script, bindings = _aggregate_contract()
    if script_transform is not None:
        script = script_transform(script)
    state = {
        "changes_result": "success",
        "skill_selected": "false",
        "local_selected": "false",
        "local_release_selected": "false",
        "cluster_selected": "false",
        "skill_local_result": "skipped",
        "local_release_result": "skipped",
        "cluster_result": "skipped",
    }
    state.update(overrides)
    environment = os.environ.copy()
    environment.update({bindings[name]: value for name, value in state.items()})
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_e2e_required_validates_docs_only_ladder_skips(tmp_path: Path) -> None:
    selected, output = _invoke_selector(tmp_path, "ARCHITECTURE.md")
    assert selected.returncode == 0, selected.stderr
    assert output == _expected_output()
    outputs = dict(line.split("=", maxsplit=1) for line in output.splitlines())

    skipped = _run_aggregate(
        skill_selected=outputs["skill"],
        local_selected=outputs["local"],
        local_release_selected=outputs["local_release"],
        cluster_selected=outputs["cluster"],
    )
    assert skipped.returncode == 0, skipped.stdout + skipped.stderr

    unexpected_result = _run_aggregate(
        skill_selected=outputs["skill"],
        local_selected=outputs["local"],
        local_release_selected=outputs["local_release"],
        cluster_selected=outputs["cluster"],
        skill_local_result="success",
    )
    assert unexpected_result.returncode != 0


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"skill_selected": "true", "skill_local_result": "success"},
        {"local_selected": "true", "skill_local_result": "success"},
        {
            "skill_selected": "true",
            "local_selected": "true",
            "local_release_selected": "true",
            "cluster_selected": "true",
            "skill_local_result": "success",
            "local_release_result": "success",
            "cluster_result": "success",
        },
    ],
)
def test_aggregate_accepts_exact_selected_outcomes(state: dict[str, str]) -> None:
    completed = _run_aggregate(**state)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.parametrize(
    "state",
    [
        {"changes_result": "failure"},
        {"skill_selected": "true", "skill_local_result": "skipped"},
        {"local_selected": "true", "skill_local_result": "cancelled"},
        {"local_release_selected": "true", "local_release_result": "failure"},
        {"cluster_selected": "true", "cluster_result": "skipped"},
        {"cluster_selected": "true", "cluster_result": "cancelled"},
        {"skill_local_result": "success"},
        {"local_release_result": "success"},
        {"cluster_result": "success"},
    ],
)
def test_aggregate_rejects_inconsistent_outcomes(state: dict[str, str]) -> None:
    completed = _run_aggregate(**state)
    assert completed.returncode != 0


def test_aggregate_negative_control_runs_before_real_results() -> None:
    completed = _run_aggregate()
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    negative_control = "Selected and skipped negative control passed"
    real_result = "E2E required passed"
    assert negative_control in output
    assert real_result in output
    assert output.index(negative_control) < output.index(real_result)


def test_negative_control_rejects_selected_skipped_outcome_when_validator_mutates() -> None:
    unmutated = _run_aggregate(
        skill_selected="true",
        skill_local_result="success",
    )
    assert unmutated.returncode == 0, unmutated.stdout + unmutated.stderr

    skill_result_check = '"$SKILL_LOCAL_RESULT" != "$skill_local_expected" ||'

    def accept_selected_skipped(script: str) -> str:
        assert script.count(skill_result_check) == 1
        return script.replace(skill_result_check, "", 1)

    completed = _run_aggregate(
        script_transform=accept_selected_skipped,
        skill_selected="true",
        skill_local_result="skipped",
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output
    assert "Selected and skipped negative control failed" in output
