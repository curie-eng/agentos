"""The shipped worker image owns workspace cloning and must contain git."""

from pathlib import Path


def test_worker_image_installs_git_for_workspace_preflight() -> None:
    dockerfile = Path(__file__).parents[1] / "Dockerfile"
    instructions = "\n".join(
        line for line in dockerfile.read_text().splitlines() if not line.lstrip().startswith("#")
    )

    assert "apt-get" in instructions
    assert "install" in instructions
    assert "git" in instructions
    assert instructions.index("git") < instructions.index("USER 1000")
