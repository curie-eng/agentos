# plugin-format

The plugin bundle format: the Claude Code plugin shape
verbatim, plus its validator. Compatibility with the Claude Code plugin format
is the distribution wedge, so this package does not invent format extensions.

## Stability

This is a frozen contract compiled against in three languages, for the same
reason as `aci-protocol`: every deploy path (the CLI scaffold, the bundle
pipeline, the runner's bundle loader) calls the single `validate_bundle`, so it
never changes from a dependent lane and a needed change lands as its own
reviewed change (see the frozen-interface rule below). Unlike `aci-protocol` it
carries no `PROTOCOL_VERSION`: the format is the Claude Code plugin shape
verbatim, and the models are lenient by design (`extra="allow"`) so real and
future Claude Code bundles that carry keys this MVP does not model still
validate. It is v0.x, so breaking changes to the validator remain possible, but
they must stay backward-compatible with existing valid bundles and land as their
own reviewed change; the schema-compat gate (`tests/test_schema_compat.py`)
fails on any drift between the models and the committed schema.

## Format surface

Pydantic models mirroring the Claude Code shapes:

- `PluginManifest` (`.claude-plugin/plugin.json`): `name` required; optional
  `version`, `description`, `author` (string or `{name, email?, url?}`),
  `homepage`, `repository`, `license`, `keywords`, `commands`, `agents`,
  `hooks`, `mcpServers`. Unknown keys are accepted and preserved.
- `SkillFrontmatter` (`skills/**/SKILL.md` YAML frontmatter): `name` and
  `description` required; `allowed-tools` optional.
- `McpServer` / `McpConfig` (`.mcp.json`): `mcpServers` maps a name to a server
  that is either stdio (`command`, `args?`, `env?`) or remote (`type`, `url`,
  `headers?`).
- `HookMatcherConfig` / `HookDefinition` (the manifest `hooks` field): `hooks`
  may be an inline object or a path to a hooks JSON file, shaped
  `{event: [{matcher?, hooks: [{type, command}]}]}` (the Claude Code hooks
  structure). `matcher` is a tool-name pattern (`"Bash"`, `"Write|Edit"`; absent
  = all tools); each action carries a `type` (today only `"command"`, which
  requires a non-empty `command`). **Deploy-time validation** rejects a missing
  hooks file, invalid JSON, a malformed shape, or a `command` hook with no
  command. **Runner consumption** (`runner`): the manifest's `PreToolUse`
  command hooks are translated into SDK `HookMatcher` callbacks and run before a
  matching tool call — exit 0 allows, exit 2 denies (stderr = reason), any other
  non-zero is a non-blocking hook error. Only `PreToolUse` is consumed today;
  other events validate but are not yet wired.
- `TriggerDeclaration` (the manifest `triggers` field, a Curie extension for
  triggers beyond chat, #273/#270): a list of `{type, ...}`. `type` is `cron`
  (requires a non-empty `schedule` cron expression) or `webhook` (requires a
  non-empty `path`). Declaring triggers in the bundle keeps an agent's full
  wake-up behavior in one reviewable artifact. **Deploy-time validation** rejects
  an unknown type, a cron trigger without a schedule, or a webhook trigger
  without a path. Runtime consumption (kernel cron scheduling / webhook ingress)
  is a separate not-yet-built seam (see `docs/interfaces/triggers/INTERFACE.md`),
  so this is validation only today.
- `ApprovalPolicy` / `ApprovalGate` (the manifest `approvalPolicy` field, #273):
  `{gates: [{gate, route}]}` — each gate names a pause point and the approval
  route that decides it; both are required. **Deploy-time validation** rejects a
  malformed policy or a gate missing `gate`/`route`. It also rejects a gate whose
  (whitespace-stripped) name starts with `mcp__` but is not a live,
  fully-namespaced tool name for a server the bundle declares
  (`mcp__plugin_<bundle>_<server>__<tool>`, non-empty tool suffix) — the runner
  matches gates by exact string equality, so a mis-namespaced `mcp__` gate
  previously validated green but silently never armed (#453). Built-in gates
  (no `mcp__` prefix, e.g. `Bash`) are unaffected. The error message names the
  expected form; to arm a live tool name the bundle does not declare, use the
  per-agent `CURIE_APPROVAL_REQUIRED_TOOLS` env knob instead. Runtime approval
  routing is a separate not-yet-built seam, so this is validation only today.
- `scripts/` is a directory convention (no manifest schema of its own).

`validate_bundle(path) -> ValidationResult` is the entry point the bundle pipeline calls. It
returns actionable, path-qualified issues instead of raising:

```python
from plugin_format import validate_bundle

result = validate_bundle("path/to/bundle")
if not result.valid:
    for issue in result.errors:
        print(issue.code, issue.location, issue.message)
```

Error codes include `bundle.missing`, `manifest.missing`,
`manifest.invalid_json`, `manifest.invalid`, `manifest.name_invalid`,
`skill.frontmatter_missing`, `skill.frontmatter_invalid`,
`skill.tools_confusable`, `mcp.invalid_json`, `mcp.server_incomplete`,
`mcp.declared_pointer`, `hooks.declared_missing`, `hooks.invalid_json`,
`hooks.invalid`, `hooks.command_missing`, `triggers.invalid`,
`triggers.unknown_type`, `triggers.cron_missing_schedule`,
`triggers.webhook_missing_path`, `approval_policy.invalid`,
`approval_policy.incomplete`, `approval_policy.gate_not_namespaced`,
`scripts.not_a_directory`.

## Frozen-interface rule

This package is a **frozen interface** for the same reasons as `aci-protocol`:
compatibility is the wedge. Do not change it unilaterally; a needed change stops
the task and escalates to the maintainers. Any change must regenerate the
committed schema with `scripts/check-contracts.sh` (which runs
`python -m plugin_format.schema_export`) and commit it. The compat gate
(`tests/test_schema_compat.py`) fails on drift.

## Decisions made under ambiguity

- **`allowed-tools`, not `tools`.** The task shorthand said SKILL.md frontmatter
  carries `name, description, tools`. The verbatim Claude Code / Agent Skills
  field is `allowed-tools`; using the real field name is the compatibility
  choice (the wedge), so the model exposes `allowed_tools` aliased to
  `allowed-tools`. A bundle written for Claude Code validates unchanged.
- **Lenient models (`extra="allow"`).** Real bundles and future Claude Code
  versions carry manifest and frontmatter keys this MVP does not model. Rejecting
  them would reject valid bundles, so the models accept and preserve unknown
  keys rather than forbidding them. (This is the opposite of `aci-protocol`,
  whose wire contract is strict.)
- **Manifest location.** The canonical location is `.claude-plugin/plugin.json`;
  a bare `plugin.json` at the bundle root is accepted as a fallback.
- **`McpServer` is one permissive model.** Rather than a strict stdio-vs-remote
  union, a single model with all fields optional stays forward compatible; the
  validator enforces that each server defines either `command` or `url`.
