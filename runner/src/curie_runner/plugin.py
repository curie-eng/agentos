"""Load and validate the mounted plugin bundle for the SDK.

``CURIE_PLUGIN_DIR`` points at a Claude Code plugin bundle (skills/, .mcp.json,
scripts/, plugin.json). The runner validates it with the frozen
``plugin_format.validate_bundle`` before handing it to the SDK, and translates a
valid bundle into the ``ClaudeAgentOptions.plugins`` shape (a local plugin
config). An invalid bundle is a hard configuration error surfaced at startup, not
a silent skip: a runner that booted with a broken bundle would answer with the
wrong (empty) capability set.
"""

import json
from pathlib import Path

from claude_agent_sdk import SdkPluginConfig
from plugin_format import (
    TOOL_POLICY_ENFORCEMENT,
    PluginManifest,
    resolve_manifest,
    validate_bundle,
)

BUNDLE_CONFIG_NAME = "curie.bundle.json"


class PluginBundleError(RuntimeError):
    """Raised when the mounted plugin bundle fails validation."""


def load_bundle_web_search_enabled(plugin_dir: str | None) -> bool:
    """Return the bundle's provider-side web-search choice (ADR-0138).

    ``curie.bundle.json`` is a Curie-owned sidecar beside, not inside, the
    frozen Claude plugin-format contract. An absent sidecar defaults on. A
    present document is strict because a misspelled opt-out would otherwise
    widen the model's capability while appearing to disable it.
    """

    if not plugin_dir:
        return True
    config_path = Path(plugin_dir) / BUNDLE_CONFIG_NAME
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise PluginBundleError(f"cannot read {config_path}: {exc}") from exc

    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PluginBundleError(
            f"invalid {config_path}: expected JSON object ({exc.msg})"
        ) from exc
    if not isinstance(loaded, dict):
        raise PluginBundleError(f"invalid {config_path}: root must be a JSON object")
    unknown = sorted(set(loaded) - {"webSearch"})
    if unknown:
        raise PluginBundleError(
            f"invalid {config_path}: unknown key(s): {', '.join(unknown)}"
        )
    enabled = loaded.get("webSearch", True)
    if not isinstance(enabled, bool):
        raise PluginBundleError(
            f"invalid {config_path}: webSearch must be a JSON boolean"
        )
    return enabled


def load_bundle_system_prompt(plugin_dir: str | None) -> str | None:
    """Return the ``systemPrompt`` declared in the bundle manifest, if any.

    The system prompt travels in the bundle (manifest field, epic #30) so it is
    versioned with the agent, and this is its sole surface: the out-of-band env
    override was removed in #488, so the bundle always wins. Returns ``None``
    when there is no plugin dir, no
    manifest, or no ``systemPrompt`` field. Best-effort and non-fatal: a bundle
    that fails to parse here is caught by ``load_plugins`` at startup, which is
    the authoritative validation gate, so this reader stays quiet.
    """

    if not plugin_dir:
        return None
    manifest_path = resolve_manifest(plugin_dir)
    if manifest_path is None:
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = PluginManifest.model_validate(data)
    except (json.JSONDecodeError, ValueError, OSError):
        return None
    return manifest.systemPrompt


def load_plugins(plugin_dir: str | None) -> list[SdkPluginConfig]:
    """Validate the bundle at ``plugin_dir`` and return the SDK plugin config.

    Returns an empty list when no plugin dir is configured. Raises
    ``PluginBundleError`` with the aggregated validation issues when the bundle
    exists but is malformed.
    """

    if not plugin_dir:
        return []

    root = Path(plugin_dir)
    # Naming the enforcement contract is a statement that this build applies a
    # declared toolPolicy. Older runners omit it and safely refuse policy-bearing
    # bundles rather than starting them unfenced.
    result = validate_bundle(root, enforces_tool_policy=TOOL_POLICY_ENFORCEMENT)
    if not result.valid:
        detail = "; ".join(f"[{i.code}] {i.location}: {i.message}" for i in result.errors)
        raise PluginBundleError(f"invalid plugin bundle at {root}: {detail}")

    return [SdkPluginConfig(type="local", path=str(root))]
