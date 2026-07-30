"""Cross-language drift gate (#491): the plugin-format name + MCP rules assert
against the SAME shared corpus the Rust CLI checks use, so a rule change on one
side without the other fails a corpus test here or there.
"""

import json
from pathlib import Path

from plugin_format import validate_bundle
from plugin_format.validate import _NAME_RE

_CORPUS = json.loads(
    (Path(__file__).parents[1] / "schema" / "name-mcp.fixture.json").read_text(encoding="utf-8")
)


def test_name_rule_matches_the_shared_corpus() -> None:
    for name in _CORPUS["valid_names"]:
        assert _NAME_RE.match(name), f"corpus valid name rejected: {name!r}"
    for name in _CORPUS["invalid_names"]:
        assert not _NAME_RE.match(name), f"corpus invalid name accepted: {name!r}"


def _mcp_codes(tmp_path: Path, connector: object) -> set[str]:
    """Validate a bundle whose sole connector is ``connector``; return the mcp.*
    error codes (empty = the connector object is accepted)."""
    manifest = json.dumps({"name": "demo", "mcpServers": {"conn": connector}})
    (tmp_path / ".claude-plugin").mkdir(parents=True)
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
    return {
        issue.code for issue in validate_bundle(tmp_path).errors if issue.code.startswith("mcp.")
    }


def test_mcp_rule_matches_the_shared_corpus(tmp_path: Path) -> None:
    for i, obj in enumerate(_CORPUS["valid_mcp"]):
        codes = _mcp_codes(tmp_path / f"ok{i}", obj)
        assert codes == set(), f"corpus valid mcp rejected {obj!r}: {codes}"
    for i, obj in enumerate(_CORPUS["invalid_mcp"]):
        codes = _mcp_codes(tmp_path / f"bad{i}", obj)
        assert codes, f"corpus invalid mcp accepted: {obj!r}"
