"""Shared access to the `curie init` scaffold's literal bytes for the tests.

Reading the Rust const's raw-string literal rather than running the CLI lets a
Python test pin the SHIPPED scaffold with no cargo build. There is one such
helper so the API's unit and integration suites cannot answer "what does `curie
init` write" two different ways, one of which quietly goes stale.
"""

from __future__ import annotations

import re
from pathlib import Path

# packages/test-support/src/curie_test_support/<this file> -- root is five up.
REPO_ROOT = Path(__file__).parents[4]

SCAFFOLD_RS = REPO_ROOT / "cli" / "src" / "scaffold.rs"

_DEPLOY_YAML = re.compile(r'const DEPLOY_YAML: &str = r#"(.*?)"#;', re.S)


def scaffolded_deploy_yaml() -> str:
    """The literal `deploy.yaml` bytes `curie init` writes.

    Raises if `cli/src/scaffold.rs` is missing, moved, or reshaped (a bump to
    `r##"`, say). That is the point: the pin must break loudly, naming the file
    to re-point it at, never quietly stop pinning anything.
    """

    source = SCAFFOLD_RS.read_text(encoding="utf-8")
    match = _DEPLOY_YAML.search(source)
    assert match is not None, (
        f"{SCAFFOLD_RS} no longer declares `const DEPLOY_YAML: &str = r#\"...\"#;`; "
        "re-point this pin at it"
    )
    return match.group(1)
