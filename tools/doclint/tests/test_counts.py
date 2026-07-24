"""The seam-catalog count gate (#938).

The counts a seam doc states about the tree ("nine runner modules import
``claude_agent_sdk``", "32 committed schemas") were prose nothing recomputed,
so the drift class fired twice -- #858, then #920's residual -- each time fixed
by hand. These drive the real ``CLAIMS`` (real patterns, real counters) over
miniature trees, so a pattern that stops matching the house phrasing fails here
rather than in six months' review.

Two failure modes matter equally and both are asserted: a count that disagrees
with the tree, and an anchor phrase that vanished so nothing is checked at all.
The second is the vacuity guard -- a silently-unchecked claim is the defect one
level up from the one #938 reports.
"""

from __future__ import annotations

from pathlib import Path

from curie_doclint.counts import check_counts, parse_count

from .conftest import Regenerate, RunLint, write

_HARNESS_DOC = "docs/interfaces/harness-modelsession/INTERFACE.md"
_CLI_OUTPUT_DOC = "docs/interfaces/cli-output/INTERFACE.md"


def _sdk_module(name: str) -> tuple[str, str]:
    """A runner module that imports the harness SDK at import level."""
    return f"runner/src/curie_runner/{name}.py", "from claude_agent_sdk import Thing\n"


def _harness_prose(count: str) -> str:
    """The harness seam's sentence, in the house phrasing, stating ``count``."""
    return (
        "CLEAN, but the SDK is not yet confined to one module: "
        f"{count} runner modules\nstill import it today.\n"
    )


def _cli_output_prose(schemas: str, tests: str) -> str:
    """The cli-output seam's two sentences, in the house phrasing.

    Both are written every time: the doc carries two claims, so omitting one
    would (rightly) trip its vacuity guard and bury the claim under test.
    """
    return (
        f"There are {schemas} committed schemas under `cli/schema/` with an index.\n"
        f"All are validated against real `to_json()` output across {tests} tests in\n"
        "`cli/tests/json_contract.rs`.\n"
        "An agent is coupled to shapes enforced by committed schemas and a drift gate.\n"
    )


# --- the count disagrees with the tree: the named drift class --------------


def test_matching_count_passes(tmp_path: Path) -> None:
    # Positive control. Without this the suite could pass by reporting
    # everything, which is no gate either.
    write(tmp_path, *_sdk_module("a"))
    write(tmp_path, *_sdk_module("b"))
    write(tmp_path, _HARNESS_DOC, _harness_prose("two"))
    assert check_counts(tmp_path) == []


def test_drifted_count_is_reported(tmp_path: Path) -> None:
    # The #858/#920 recurrence itself: a third importing module lands and the
    # prose still says two. Before this gate, doc-lint stayed green here.
    write(tmp_path, *_sdk_module("a"))
    write(tmp_path, *_sdk_module("b"))
    write(tmp_path, *_sdk_module("c"))
    write(tmp_path, _HARNESS_DOC, _harness_prose("two"))
    findings = check_counts(tmp_path)
    assert len(findings) == 1
    assert "'two'" in findings[0].reason
    assert "3" in findings[0].reason


def test_digits_and_number_words_are_both_accepted(tmp_path: Path) -> None:
    # The house style spells small counts as words and larger ones as digits,
    # so the same value must pass written either way.
    write(tmp_path, *_sdk_module("a"))
    write(tmp_path, *_sdk_module("b"))
    write(tmp_path, _HARNESS_DOC, _harness_prose("2"))
    assert check_counts(tmp_path) == []
    assert parse_count("nine") == 9
    assert parse_count("32") == 32
    assert parse_count("many") is None


def test_a_repeated_count_must_agree_in_every_place(tmp_path: Path) -> None:
    # The real harness doc states its module count twice, in two different
    # sentences. Checking only the first would let the second rot, which is
    # how a "fixed" doc stays half wrong.
    write(tmp_path, *_sdk_module("a"))
    doc = _harness_prose("one") + "\nElsewhere: imported across seven runner modules.\n"
    write(tmp_path, _HARNESS_DOC, doc)
    findings = check_counts(tmp_path)
    assert len(findings) == 1
    assert "'seven'" in findings[0].reason


# --- the anchor vanished: the vacuity guard --------------------------------


def test_reworded_sentence_fails_rather_than_going_vacuous(tmp_path: Path) -> None:
    # THE most important test here. A reworded sentence must not silently stop
    # being checked -- a green gate over unverified prose is the failure mode
    # #938 exists to close, and a skip would rebuild it one level up.
    write(tmp_path, *_sdk_module("a"))
    write(tmp_path, _HARNESS_DOC, "The SDK leaks into several modules today.\n")
    findings = check_counts(tmp_path)
    assert len(findings) == 1
    assert "no longer verified" in findings[0].reason


def test_absent_seam_doc_is_skipped(tmp_path: Path) -> None:
    # A repo root that carries no seam docs at all (the miniature fixture tree)
    # has no claims to check. Whether a seam doc should exist is the catalog's
    # own concern, reported there, not duplicated as a count finding.
    write(tmp_path, *_sdk_module("a"))
    assert check_counts(tmp_path) == []


# --- counter semantics -----------------------------------------------------


def test_only_import_level_uses_count_as_importing_modules(tmp_path: Path) -> None:
    # The prose claims modules that IMPORT the SDK. A module that merely names
    # it in a comment is not one, or the count inflates on documentation edits.
    write(tmp_path, *_sdk_module("real"))
    write(
        tmp_path,
        "runner/src/curie_runner/mentions.py",
        "# claude_agent_sdk is discussed here but never imported.\n",
    )
    write(tmp_path, _HARNESS_DOC, _harness_prose("one"))
    assert check_counts(tmp_path) == []


def test_schema_count_excludes_the_index_and_the_prose_mention(tmp_path: Path) -> None:
    # Two traps in one. `index.json` lists the schemas rather than being one of
    # them, and the same doc later says "enforced by committed schemas and a
    # drift gate" -- prose ABOUT the schemas, not a count of them, which an
    # under-anchored pattern would read as the count and choke on.
    for name in ("kill", "resume", "budget"):
        write(tmp_path, f"cli/schema/{name}.json", "{}\n")
    write(tmp_path, "cli/schema/index.json", "{}\n")
    write(tmp_path, "cli/tests/json_contract.rs", "#[test]\nfn a() {}\n")
    write(tmp_path, _CLI_OUTPUT_DOC, _cli_output_prose(schemas="3", tests="one"))
    assert check_counts(tmp_path) == []


def test_json_contract_test_count_is_read_from_the_test_file(tmp_path: Path) -> None:
    write(tmp_path, "cli/schema/kill.json", "{}\n")
    write(
        tmp_path,
        "cli/tests/json_contract.rs",
        "#[test]\nfn a() {}\n#[test]\nfn b() {}\n",
    )
    write(tmp_path, _CLI_OUTPUT_DOC, _cli_output_prose(schemas="1", tests="5"))
    findings = check_counts(tmp_path)
    assert len(findings) == 1
    assert "'5'" in findings[0].reason
    assert "2" in findings[0].reason


# --- wired into the linter, not dead code ----------------------------------


def test_count_drift_fails_through_the_cli(
    clean_repo: Path, run_lint: RunLint, regenerate: Regenerate
) -> None:
    # The integration proof: the check runs as part of `curie dev docs-lint`,
    # so a drifted count fails the real gate rather than only a unit test.
    write(clean_repo, *_sdk_module("importer"))
    write(
        clean_repo,
        _HARNESS_DOC,
        "---\n"
        "seam: Harness in-proc / ModelSession\n"
        "kind: CLEAN\n"
        "impls: 1 + fake\n"
        "grade: not separately graded\n"
        "epics:\n"
        '  - "#25"\n'
        "order: 99\n"
        "---\n"
        "\n# Harness\n\n"
        "<!-- BEGIN GENERATED: header (curie dev docs-lint) -->\n"
        "<!-- END GENERATED: header -->\n\n" + _harness_prose("six"),
    )
    # Regenerate first: a new seam doc legitimately changes the index and this
    # doc's header, and that drift is a different finding than the one under
    # test here.
    regenerate(clean_repo)
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "runner modules importing claude_agent_sdk" in out
    assert "'six'" in out
