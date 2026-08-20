"""Contract tests for publishing the verified release chart through a Helm index."""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "chart-index.yaml"
CONFIG_PATH = REPO_ROOT / "cr.yaml"


def load_yaml(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def workflow_steps(workflow: dict) -> list[dict]:
    jobs = workflow["jobs"]
    assert len(jobs) == 1
    return next(iter(jobs.values()))["steps"]


def run_script(step: dict) -> str:
    return step.get("run", "")


class TestChartIndexWorkflowContract:
    def test_only_published_releases_and_the_narrow_next_bootstrap_trigger_it(self):
        workflow = load_yaml(WORKFLOW_PATH)
        trigger = workflow["on"]

        assert set(trigger) == {"release", "push"}
        assert trigger["release"] == {"types": ["published"]}
        assert trigger["push"]["branches"] == ["next"]
        assert len(trigger["push"]["paths"]) == 2
        assert set(trigger["push"]["paths"]) == {
            ".github/workflows/chart-index.yaml",
            "cr.yaml",
        }

    def test_write_permission_is_job_scoped_and_checkout_cannot_reuse_it(self):
        workflow = load_yaml(WORKFLOW_PATH)
        assert (workflow.get("permissions") or {}).get("contents") != "write"

        jobs = workflow["jobs"]
        writers = [
            job
            for job in jobs.values()
            if (job.get("permissions") or {}).get("contents") == "write"
        ]
        assert len(jobs) == len(writers) == 1
        assert writers[0]["permissions"] == {
            "contents": "write",
            "attestations": "read",
        }
        assert {
            name
            for name, access in writers[0]["permissions"].items()
            if access == "write"
        } == {"contents"}

        checkouts = [
            step
            for step in workflow_steps(workflow)
            if step.get("uses", "").startswith("actions/checkout@")
        ]
        assert checkouts
        assert all(
            step.get("with", {}).get("persist-credentials") == "false"
            for step in checkouts
        )

    def test_every_remote_action_is_pinned_to_a_full_commit_sha(self):
        workflow = load_yaml(WORKFLOW_PATH)
        actions = [step["uses"] for step in workflow_steps(workflow) if "uses" in step]

        assert actions
        assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", action) for action in actions)

    def test_release_assets_are_fully_verified_before_pages_can_change(self):
        workflow = load_yaml(WORKFLOW_PATH)
        steps = workflow_steps(workflow)

        def index_of(pattern: str) -> int:
            matches = [
                index
                for index, step in enumerate(steps)
                if re.search(pattern, run_script(step), re.IGNORECASE)
            ]
            assert matches, pattern
            return matches[0]

        download = index_of(r"\bgh\s+release\s+download\b")
        closed_world = index_of(r"release/integrity\.py\s+verify\b")
        signature = index_of(r"\bcosign\s+verify-blob\b")
        provenance = index_of(r"\bgh\s+attestation\s+verify\b")

        signature_script = run_script(steps[signature])
        assert "checksums.txt.sigstore.json" in signature_script
        assert "--certificate-identity" in signature_script
        assert "--certificate-oidc-issuer" in signature_script
        assert re.search(r"\bchecksums\.txt\b", signature_script)

        provenance_script = run_script(steps[provenance])
        assert '--repo "$GITHUB_REPOSITORY"' in provenance_script
        assert "--signer-workflow" in provenance_script
        assert ".github/workflows/release.yaml" in provenance_script
        assert "--source-ref" in provenance_script
        assert "--source-digest" in provenance_script

        mutations = [
            index
            for index, step in enumerate(steps)
            if re.search(
                r"\bgit\s+push\b|\bgit\s+(?:checkout|switch)\s+--orphan\b|"
                r"\bcr\s+index\b[^\n]*\s--push\b",
                run_script(step),
            )
        ]
        assert mutations
        assert download < closed_world < signature < provenance < min(mutations)

        download_script = run_script(steps[download])
        assert "--pattern" not in download_script
        assert "--archive" not in download_script

    def test_only_an_absent_pages_branch_can_be_initialized(self):
        workflow = load_yaml(WORKFLOW_PATH)
        scripts = "\n".join(run_script(step) for step in workflow_steps(workflow))

        assert re.search(
            r"git\s+ls-remote\b[^\n]*--heads\b[^\n]*(?:refs/heads/)?gh-pages\b",
            scripts,
        )
        assert re.search(r"git\s+(?:checkout|switch)\s+--orphan\s+gh-pages\b", scripts)
        assert re.search(r"(?:mktemp\s+-d|\$RUNNER_TEMP)", scripts)

        ownership_guard = next(
            run_script(step)
            for step in workflow_steps(workflow)
            if "gh-pages" in run_script(step)
            and re.search(r"\b(?:exit|return)\s+1\b", run_script(step))
        )
        assert "index.yaml" in ownership_guard
        assert re.search(r"\b(?:find|ls|git\s+ls-files)\b", ownership_guard)

    def test_chart_releaser_cli_is_checksum_verified_before_it_can_run(self):
        workflow = load_yaml(WORKFLOW_PATH)
        steps = workflow_steps(workflow)
        scripts = [run_script(step) for step in steps]
        all_runs = "\n".join(scripts)

        assert "helm/chart-releaser-action" not in WORKFLOW_PATH.read_text(
            encoding="utf-8"
        )
        assert re.search(
            r"https://github\.com/helm/chart-releaser/releases/download/v1\.7\.0/"
            r"chart-releaser_1\.7\.0_linux_amd64\.tar\.gz",
            all_runs,
        )
        assert re.search(
            r"https://github\.com/helm/chart-releaser/releases/download/v1\.7\.0/"
            r"checksums\.txt",
            all_runs,
        )

        def command_position(pattern: str) -> tuple[int, int]:
            for index, script in enumerate(scripts):
                match = re.search(pattern, script)
                if match is not None:
                    return index, match.start()
            raise AssertionError(pattern)

        checksum = command_position(r"\bsha256sum\s+(?:--check\b|-c\b)")
        extract = command_position(r"\btar\b[^\n]*\s(?:-x|-\w*x\w*)")
        execute = command_position(r"\bcr\s+index\b")
        assert checksum < extract < execute

        checksum_script = scripts[checksum[0]]
        assert "checksums.txt" in checksum_script
        assert "chart-releaser_1.7.0_linux_amd64.tar.gz" in checksum_script

        index_line = next(
            line
            for script in scripts
            for line in script.splitlines()
            if re.search(r"\bcr\s+index\b", line)
        )
        assert re.search(r"(?:--config|-c)\s+['\"]?cr\.yaml['\"]?", index_line)
        assert re.search(r"\s--push(?:\s|$)", index_line)
        assert len(re.findall(r"\bcr\s+index\b", all_runs)) == 1
        assert not re.search(r"\bcr\s+(?:package|upload)\b", all_runs)

    def test_exact_verified_chart_is_the_only_input_to_indexing(self):
        workflow = load_yaml(WORKFLOW_PATH)
        steps = workflow_steps(workflow)
        scripts = [run_script(step) for step in steps]

        integrity_index = next(
            index
            for index, script in enumerate(scripts)
            if re.search(r"release/integrity\.py\s+verify\b", script)
        )
        index_command = next(
            index
            for index, script in enumerate(scripts)
            if re.search(r"\bcr\s+index\b", script)
        )

        staging = [
            script
            for script in scripts[integrity_index + 1 : index_command]
            if "checksums.txt" in script
            and re.search(r"curie-.+(?:\\)?\.tgz", script)
            and ".cr-release-packages" in script
        ]
        assert len(staging) == 1
        chart_selection = re.search(
            r"(?P<variable>[A-Za-z_][A-Za-z0-9_]*)=[\"']?\$\("
            r"(?P<selector>.*?checksums\.txt.*?)\)[\"']?",
            staging[0],
            re.DOTALL,
        )
        assert chart_selection is not None
        assert re.search(
            r"curie-.+(?:\\)?\.tgz", chart_selection.group("selector")
        )
        variable = re.escape(chart_selection.group("variable"))
        assert re.search(
            rf"\bcp\s+[\"']?dist/\$\{{?{variable}\}}?[\"']?\s+"
            r"[\"']?\.cr-release-packages(?:/|[\"'])",
            staging[0],
        )

        all_runs = "\n".join(scripts)
        assert not re.search(r"\bhelm\s+package\b", all_runs)
        assert not re.search(r"\bcr\s+(?:package|upload)\b", all_runs)
        assert not re.search(r"\bgh\s+release\s+(?:create|upload)\b", all_runs)
        assert all(
            "softprops/action-gh-release" not in step.get("uses", "")
            for step in steps
        )


class TestChartReleaserConfigContract:
    def test_index_resolves_the_existing_curie_release_asset(self):
        config = load_yaml(CONFIG_PATH)

        assert config["owner"] == "curie-eng"
        assert config["git-repo"] == "curie"
        assert config["pages-branch"] == "gh-pages"
        assert config["charts-repo"] == (
            "https://raw.githubusercontent.com/curie-eng/curie/gh-pages"
        )
        assert config["release-name-template"] == "v{{ .Version }}"
