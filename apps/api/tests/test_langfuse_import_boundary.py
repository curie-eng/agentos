"""Import boundary for the Langfuse observability reader."""

import json
import subprocess
import sys
from pathlib import Path


def test_importing_langfuse_does_not_load_the_runner_or_harness_sdk() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, sys; import curie_api.langfuse; print(json.dumps(sorted(sys.modules)))",
        ],
        check=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[3],
        text=True,
    )

    imported_modules = json.loads(result.stdout)
    forbidden_prefixes = ("claude_agent_sdk", "curie_runner")
    leaked_modules = [
        module
        for module in imported_modules
        if module == forbidden_prefixes[0]
        or module.startswith(f"{forbidden_prefixes[0]}.")
        or module == forbidden_prefixes[1]
        or module.startswith(f"{forbidden_prefixes[1]}.")
    ]

    assert not leaked_modules, (
        "curie_api.langfuse must not load runner or harness modules: "
        f"{leaked_modules}"
    )
