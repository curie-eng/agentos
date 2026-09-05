"""The chart carries a generated copy of the API's authoritative schema graph."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from alembic.script import ScriptDirectory

REPO = Path(__file__).resolve().parents[3]
ARTIFACT = Path("charts/curie/files/schema-compat.json")


def test_packaged_schema_metadata_matches_api_authority() -> None:
    metadata = json.loads((REPO / ARTIFACT).read_text())
    api = REPO / "apps/api/src/curie_api"
    window = json.loads((api / "schema_compat.json").read_text())
    kinds = json.loads((api / "revision_kinds.json").read_text())
    assert metadata["schema_min"] == window["schema_min"]
    assert metadata["schema_head"] == window["schema_head"]
    graph = ScriptDirectory(str(REPO / "apps/api/alembic"))
    revisions = {entry["revision"]: entry for entry in metadata["revisions"]}
    assert set(revisions) == set(kinds)
    for revision in graph.walk_revisions():
        parent = revision.down_revision
        parents = list(parent) if isinstance(parent, tuple) else [parent] if parent else []
        assert revisions[revision.revision] == {
            "revision": revision.revision,
            "parents": parents,
            "kind": kinds[revision.revision],
        }


def test_revision_gate_rejects_stale_packaged_metadata(tmp_path: Path) -> None:
    for source in [
        "scripts/check-alembic-revisions.py",
        "apps/api/src/curie_api/schema_compat.json",
        "apps/api/src/curie_api/revision_kinds.json",
        str(ARTIFACT),
    ]:
        target = tmp_path / source
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / source, target)
    shutil.copytree(REPO / "apps/api/alembic", tmp_path / "apps/api/alembic")
    command = [sys.executable, str(tmp_path / "scripts/check-alembic-revisions.py")]
    healthy = subprocess.run(command, capture_output=True, text=True, check=False)
    assert healthy.returncode == 0, healthy.stderr
    artifact = tmp_path / ARTIFACT
    metadata = json.loads(artifact.read_text())
    metadata["revisions"][0]["kind"] = "contract"
    artifact.write_text(json.dumps(metadata))
    stale = subprocess.run(command, capture_output=True, text=True, check=False)
    assert stale.returncode == 1
    assert "schema compatibility metadata" in stale.stderr.lower()


def test_chart_metadata_is_available_without_creating_a_cluster_resource() -> None:
    rendered = subprocess.run(
        [
            "helm",
            "template",
            "acme-bot",
            str(REPO / "charts/curie"),
            "--show-only",
            "templates/schema-compat.yaml",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    import yaml

    resource = yaml.safe_load(rendered.stdout)
    metadata = json.loads(resource["data"]["compatibility.json"])
    assert metadata == json.loads((REPO / ARTIFACT).read_text())


def test_metadata_write_cannot_claim_to_generate_from_a_non_authoritative_tree(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "alembic"
    shutil.copytree(REPO / "apps/api/alembic", tree)
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts/check-alembic-revisions.py"),
            "--script-location",
            str(tree),
            "--write-upgrade-metadata",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "authoritative" in result.stderr
