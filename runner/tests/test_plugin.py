"""Plugin bundle loading and validation against the frozen plugin-format."""

from pathlib import Path

import pytest
from curie_runner import PluginBundleError, load_plugins

_FIXTURES = Path(__file__).resolve().parents[2] / "packages/plugin-format/tests/fixtures"


def test_no_plugin_dir_is_empty() -> None:
    assert load_plugins(None) == []
    assert load_plugins("") == []


def test_valid_bundle_becomes_local_plugin_config() -> None:
    bundle = _FIXTURES / "valid_bundle"
    plugins = load_plugins(str(bundle))
    assert plugins == [{"type": "local", "path": str(bundle)}]


def test_invalid_bundle_raises() -> None:
    bundle = _FIXTURES / "bad_manifest_name"
    with pytest.raises(PluginBundleError):
        load_plugins(str(bundle))


def test_bundle_system_prompt_read_from_manifest(tmp_path: Path) -> None:
    """The manifest ``systemPrompt`` is read from the bundle (epic #30, #271)."""
    from curie_runner import load_bundle_system_prompt

    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "demo", "systemPrompt": "Be terse and cite the CRM."}',
        encoding="utf-8",
    )
    assert load_bundle_system_prompt(str(tmp_path)) == "Be terse and cite the CRM."


def test_bundle_system_prompt_absent_is_none(tmp_path: Path) -> None:
    from curie_runner import load_bundle_system_prompt

    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "demo"}', encoding="utf-8"
    )
    assert load_bundle_system_prompt(str(tmp_path)) is None
    # No plugin dir, and a dir with no manifest, both resolve to None.
    assert load_bundle_system_prompt(None) is None
    assert load_bundle_system_prompt("") is None


def test_bundle_system_prompt_bad_manifest_is_none(tmp_path: Path) -> None:
    """A malformed manifest is non-fatal here (load_plugins is the real gate)."""
    from curie_runner import load_bundle_system_prompt

    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "plugin.json").write_text("{ not json", encoding="utf-8")
    assert load_bundle_system_prompt(str(tmp_path)) is None


def test_bundle_web_search_defaults_on_and_accepts_explicit_boolean(tmp_path: Path) -> None:
    from curie_runner import load_bundle_web_search_enabled

    assert load_bundle_web_search_enabled(None) is True
    assert load_bundle_web_search_enabled(str(tmp_path)) is True

    config = tmp_path / "curie.bundle.json"
    config.write_text('{"webSearch": true}', encoding="utf-8")
    assert load_bundle_web_search_enabled(str(tmp_path)) is True

    config.write_text('{"webSearch": false}', encoding="utf-8")
    assert load_bundle_web_search_enabled(str(tmp_path)) is False


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("not json", "expected JSON object"),
        ("[]", "root must be a JSON object"),
        ('{"websearch": false}', "unknown key"),
        ('{"webSearch": "false"}', "must be a JSON boolean"),
    ],
)
def test_bundle_web_search_invalid_config_fails_closed(
    tmp_path: Path, body: str, message: str
) -> None:
    from curie_runner import load_bundle_web_search_enabled

    (tmp_path / "curie.bundle.json").write_text(body, encoding="utf-8")
    with pytest.raises(PluginBundleError, match=message):
        load_bundle_web_search_enabled(str(tmp_path))
