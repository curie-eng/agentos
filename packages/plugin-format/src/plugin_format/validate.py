"""Validate a plugin bundle directory against the Claude Code plugin shape.

``validate_bundle(path)`` is the entry point task B2 calls before versioning and
storing a bundle. It returns a ValidationResult with actionable, path-qualified
errors instead of raising, so the caller can surface every problem at once.
"""

import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, TypeAdapter, ValidationError

from . import connector_lock
from .approval_policy import (
    connector_server_names,
    connector_tool_prefix,
    declared_mcp_server_names,
    effective_tool_prefix,
    grantable_routes,
)
from .connector_lock import (
    CONNECTOR_LOCK_FILE,
    ConnectorLockFile,
    resolve_context,
    source_digest_of,
    validate_connector_lock,
)
from .connectors import CONNECTORS_FILE, ConnectorsFile, validate_connectors
from .deploy_targets import validate_deploy_targets
from .manifest import resolve_manifest
from .models import (
    _TRIGGER_TYPES,
    ApprovalPolicy,
    HookMatcherConfig,
    McpConfig,
    PluginManifest,
    SkillFrontmatter,
    ToolPolicy,
    TriggerDeclaration,
)
from .reserved_env import SECRET_NAME_RE, is_reserved_boot_env_name
from .tool_policy import (
    TOOL_POLICY_ENFORCEMENT,
    check_policy_patterns,
    literal_server_segment,
    policy_patterns,
)
from .yaml_loader import DuplicateKeyError, safe_load_unique

# The hooks field is a mapping of event name -> list of matcher entries. Reused
# to validate both the inline object and a declared hooks file.
_HOOKS_ADAPTER = TypeAdapter(dict[str, list[HookMatcherConfig]])
_TRIGGERS_ADAPTER = TypeAdapter(list[TriggerDeclaration])

# Claude Code plugin names are kebab-case: lowercase alphanumerics and hyphens.
_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class ValidationIssue(BaseModel):
    code: str
    message: str
    location: str


class ValidationResult(BaseModel):
    valid: bool
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []


class _Collector:
    def __init__(self) -> None:
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []

    def error(self, code: str, message: str, location: str) -> None:
        self.errors.append(ValidationIssue(code=code, message=message, location=location))

    def warn(self, code: str, message: str, location: str) -> None:
        self.warnings.append(ValidationIssue(code=code, message=message, location=location))

    def result(self) -> ValidationResult:
        return ValidationResult(valid=not self.errors, errors=self.errors, warnings=self.warnings)


def validate_bundle(
    path: str | Path, *, enforces_tool_policy: str | None = None
) -> ValidationResult:
    """Validate the plugin bundle at ``path`` and return a ValidationResult.

    ``enforces_tool_policy`` is the CALLER's statement of which vanilla MCP
    tool-policy contract it enforces at runtime. It is the enforcement handshake,
    and it is the point of the ``toolPolicy`` extension: a bundle that declares a
    ``toolPolicy`` is REJECTED (``tool_policy.unenforced``) unless the caller
    passes ``tool_policy.TOOL_POLICY_ENFORCEMENT``, which it may only do once it
    actually applies the policy.

    Without that rule the field would be worse than absent. It is tempting to
    argue that a non-enforcing runtime "cannot obtain a parsed policy" because
    ``load_tool_policy`` raises -- but nothing forces a consumer to call that
    function. ``apps/api``'s bundle intake and the runner's plugin loader both
    call ``validate_bundle(root)`` and neither reads ``toolPolicy`` at all, so
    without this check both would accept a policy-bearing bundle and apply
    nothing: a bundle that looks fenced and runs unfenced. With it, both REFUSE
    such a bundle until the runtime lane exists to pass the id.

    The keyword-only ``None`` default keeps every existing call site
    source-compatible, and a bundle that declares no ``toolPolicy`` is entirely
    unaffected -- it yields the same ``valid``/``errors``/``warnings`` as before.
    """

    root = Path(path)
    c = _Collector()

    if not root.is_dir():
        c.error("bundle.missing", f"bundle path is not a directory: {root}", str(root))
        return c.result()

    manifest = _validate_manifest(root, c)
    if manifest is not None:
        _validate_skills(root, c)
        mcp_servers = _validate_mcp(root, manifest, c)
        _validate_hooks(root, manifest, c)
        _validate_triggers(manifest, c)
        # One derivation of the connectors.yaml server names, shared by both
        # cross-checks below. Computing it twice would re-read and re-parse the
        # file and let the two checks assert against DIFFERENT sets on a racing
        # or partially-written bundle -- the drift shape #453 is about.
        connector_servers = connector_server_names(root)
        _validate_approval_policy(manifest, mcp_servers, connector_servers, c)
        _validate_tool_policy(manifest, mcp_servers, connector_servers, enforces_tool_policy, c)
        _validate_secrets(manifest, c)
        _validate_scripts(root, c)
        _validate_connectors(root, c)
        _validate_connector_lock(root, c)
        _validate_deploy_targets(root, c)

    return c.result()


DEPLOY_FILE = "deploy.yaml"


def _validate_deploy_targets(root: Path, c: _Collector) -> None:
    """Validate ``deploy.yaml`` if present (ADR-0089).

    Optional: a bundle that passes routing as flags simply omits it. When
    present it must parse, because every error it can raise is one that would
    otherwise deploy SUCCESSFULLY to the wrong place -- a mistyped agent mints a
    new agent, a mistyped channel binds a channel nobody watches, and neither
    reports anything.
    """

    path = root / DEPLOY_FILE
    if not path.is_file():
        return
    try:
        data = safe_load_unique(path.read_text(encoding="utf-8"))
    except DuplicateKeyError as exc:
        c.error(
            "deploy.duplicate_target",
            f"{DEPLOY_FILE}: duplicate target key {exc.key!r}",
            DEPLOY_FILE,
        )
        return
    except (OSError, yaml.YAMLError) as exc:
        c.error("deploy.unreadable", f"{DEPLOY_FILE}: {exc}", DEPLOY_FILE)
        return
    for code, message in validate_deploy_targets(data)[1]:
        c.error(code, message, DEPLOY_FILE)


def _validate_connectors(root: Path, c: _Collector) -> None:
    """Validate ``connectors.yaml`` if present (ADR-0086).

    Optional: a bundle with no hosted connectors simply omits it. When present
    it must parse, because every error it can raise would otherwise surface as
    an opaque Kubernetes apply failure long after the author stopped looking.
    """

    path = root / CONNECTORS_FILE
    if not path.is_file():
        return
    try:
        data = safe_load_unique(path.read_text(encoding="utf-8"))
    except DuplicateKeyError as exc:
        c.error(
            "connectors.duplicate_connector",
            f"{CONNECTORS_FILE}: duplicate connector key {exc.key!r}",
            CONNECTORS_FILE,
        )
        return
    except (OSError, yaml.YAMLError) as exc:
        c.error("connectors.unreadable", f"{CONNECTORS_FILE}: {exc}", CONNECTORS_FILE)
        return
    parsed, errors = validate_connectors(data)
    for code, message in errors:
        c.error(code, message, CONNECTORS_FILE)
    if parsed is not None:
        _reject_connector_name_collisions(root, parsed, c)


def _read_connectors(root: Path) -> ConnectorsFile | None:
    """The bundle's parsed ``connectors.yaml``, or None when it is absent or bad.

    ``_validate_connectors`` has already reported whatever made it bad, so the
    lock arm stays silent about a declaration it cannot trust rather than
    reporting a second, confusing error about the same file.
    """

    path = root / CONNECTORS_FILE
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    parsed, errors = validate_connectors(data)
    return None if errors else parsed


def _validate_connector_lock(root: Path, c: _Collector) -> None:
    """Validate ``connectors.lock.yaml`` and the bundle's builds against it (ADR 0113).

    ``validate_bundle`` is the ONE gate a bundle passes through whatever entry
    point it arrives by -- the CLI upload, the console's create-agent modal, and
    the git push path all route through it -- so these rules live here and cover
    all three with one implementation.

    Delivery is deliberately NOT checked. ``curie local deploy`` legitimately
    uploads a bundle carrying a ``local-daemon`` lock; the registry-only rule
    belongs to the cluster deploy preflight, the only path that needs an
    artifact a Kubernetes node can pull.
    """

    lock: ConnectorLockFile | None = None
    path = root / CONNECTOR_LOCK_FILE
    if path.is_file():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            c.error(
                "connectors.lock_unreadable",
                f"{CONNECTOR_LOCK_FILE}: {exc}",
                CONNECTOR_LOCK_FILE,
            )
            return
        lock, errors = validate_connector_lock(data)
        for code, message in errors:
            c.error(code, message, CONNECTOR_LOCK_FILE)
        if lock is None:
            return

    declared = _read_connectors(root)
    if declared is None:
        return

    complete = True
    for name, spec in sorted(declared.connectors.items()):
        if spec.build is None:
            continue
        try:
            # Resolved, not merely joined: `source_digest_of` hashes whatever
            # tree this names, so a symlinked context would let a bundle pin
            # bytes it does not contain -- and the CLI (`resolve_context` in
            # cli/src/connector_build.rs) already refuses that before it builds.
            # Intake accepting what the builder refuses is the seam this closes.
            context = resolve_context(root, spec.build.context)
        except ValueError as exc:
            complete = False
            c.error(
                "connectors.build_context_escapes",
                f"connectors.{name}: {exc}",
                CONNECTORS_FILE,
            )
            continue
        if not context.is_dir():
            # Refused here rather than skipped: a bundle whose declared build
            # input is not in it can never be built, and letting it through
            # means the version is created, the deployment goes active, and the
            # failure surfaces at render time far from its cause.
            complete = False
            c.error(
                "connectors.build_context_missing",
                f"connectors.{name}: `build.context` is {spec.build.context!r}, which this "
                "bundle does not contain, so there is nothing to build or to hash. Add the "
                "build context to the bundle or correct the path.",
                CONNECTORS_FILE,
            )
            continue
        entry = lock.connectors.get(name) if lock is not None else None
        if entry is None:
            complete = False
            c.error(
                "connectors.lock_missing",
                f"connectors.{name}: declares `build:` but {CONNECTOR_LOCK_FILE} has no entry "
                "for it, so nothing pins what would be deployed. Run `curie build --plugin-dir "
                "<dir>` and commit the lock it writes.",
                CONNECTOR_LOCK_FILE,
            )
            continue
        # Pure hashing over the already-extracted tree: no docker, no registry,
        # no network, so the API stays a pure renderer under ADR-0087. Without
        # it a git push after a source or `platforms` edit activates the
        # PREVIOUS digest and the deployed connector silently stops matching the
        # reviewed source.
        if source_digest_of(context, spec.build) != entry.source_digest:
            complete = False
            c.error(
                "connectors.lock_stale",
                f"connectors.{name}: {CONNECTOR_LOCK_FILE} records a source digest that no "
                "longer matches this bundle's build input, so the recorded image was built "
                "from something else. Rebuild it with `curie build --plugin-dir <dir>`.",
                CONNECTOR_LOCK_FILE,
            )

    if complete and lock is not None:
        # The last rule the model cannot express: an image that is not a digest
        # of its delivery's shape. `apply_lock` owns that refusal, so intake
        # asks it rather than carrying a second copy -- a hand-edited lock
        # reaches here exactly as a generated one does, and a version whose
        # connector can never render must not be stored.
        try:
            connector_lock.apply_lock(declared, lock, portable=False)
        except ValueError as exc:
            c.error("connectors.lock_invalid", f"{CONNECTOR_LOCK_FILE}: {exc}", CONNECTOR_LOCK_FILE)


def _reject_connector_name_collisions(root: Path, parsed: ConnectorsFile, c: _Collector) -> None:
    """Reject a server name declared in BOTH ``connectors.yaml`` and MCP config.

    Curie injects the connector's entry into the agent's MCP configuration
    (ADR-0086) alongside whatever the bundle declares. When both name the same
    server, which one the agent ends up talking to is decided downstream, and
    the loser is overridden with no diagnostic -- so the author's committed
    ``.mcp.json`` entry could be silently ignored, or could silently win over
    the objects Curie actually created.

    Neither outcome should be discoverable only at turn time, and a precedence
    rule would just be a thing to remember. One name, one owner: say so here,
    where the fix is a one-line edit.
    """

    declared = declared_mcp_server_names(root)
    if declared is None:
        # A declaration exists but is unreadable. `_validate_mcp` already errors
        # on that; cross-checking a partial set would add a confusing second
        # error about a name we cannot actually confirm.
        return
    for name in sorted(set(parsed.connectors) & declared):
        c.error(
            "connectors.duplicate_server",
            f"connectors.{name}: `{name}` is declared in both {CONNECTORS_FILE} and the "
            "bundle's MCP config. Curie derives this server's URL from the Service it "
            f"creates, so remove the `{name}` entry from the MCP config and let "
            f"{CONNECTORS_FILE} own it.",
            CONNECTORS_FILE,
        )


def _validate_manifest(root: Path, c: _Collector) -> PluginManifest | None:
    manifest_path = resolve_manifest(root)
    if manifest_path is None:
        c.error(
            "manifest.missing",
            "no plugin manifest found at .claude-plugin/plugin.json or plugin.json",
            str(root),
        )
        return None

    rel = str(manifest_path.relative_to(root))
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        c.error("manifest.invalid_json", f"manifest is not valid JSON: {exc}", rel)
        return None

    try:
        manifest = PluginManifest.model_validate(data)
    except ValidationError as exc:
        for issue in _explain(exc):
            c.error("manifest.invalid", issue, rel)
        return None

    if not _NAME_RE.match(manifest.name):
        c.error(
            "manifest.name_invalid",
            f"plugin name {manifest.name!r} must be kebab-case "
            "(lowercase letters, digits, hyphens)",
            rel,
        )
    return manifest


def _validate_skills(root: Path, c: _Collector) -> None:
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return

    skill_files = sorted(skills_dir.rglob("SKILL.md"))
    if not skill_files:
        c.warn("skills.empty", "skills/ exists but contains no SKILL.md files", "skills")
        return

    for skill_file in skill_files:
        rel = str(skill_file.relative_to(root))
        frontmatter = _read_frontmatter(skill_file, rel, c)
        if frontmatter is None:
            continue
        _check_tools_confusable(frontmatter, rel, c)
        try:
            SkillFrontmatter.model_validate(frontmatter)
        except ValidationError as exc:
            for issue in _explain(exc):
                c.error("skill.frontmatter_invalid", issue, rel)


# Keys an author reaches for instead of the verbatim Claude Code ``allowed-tools``.
# ``tools`` is silently dropped by ``extra="allow"``; ``allowed_tools`` populates
# the field via ``populate_by_name`` but will not survive a round-trip through
# real Claude Code. Both are rejected, but only when ``allowed-tools`` is absent:
# an author who already has the right key is not confused, whatever else the
# frontmatter carries.
_CONFUSABLE_TOOLS_KEYS = ("tools", "allowed_tools", "allowedTools")


def _check_tools_confusable(frontmatter: dict[str, Any], rel: str, c: _Collector) -> None:
    if "allowed-tools" in frontmatter:
        return
    for key in _CONFUSABLE_TOOLS_KEYS:
        if key in frontmatter:
            c.error(
                "skill.tools_confusable",
                f"skill frontmatter key {key!r} is not the Claude Code key: use "
                "'allowed-tools'. As written the skill declares no tools.",
                rel,
            )
            return


def _validate_mcp(root: Path, manifest: PluginManifest, c: _Collector) -> set[str] | None:
    """Validate every MCP declaration: the manifest field and root .mcp.json.

    The manifest ``mcpServers`` must be an inline object. The path-string form
    parses but the real loader ignores it, so the servers never register; it is
    rejected outright rather than validating a file that never loads (#540).

    Returns the set of declared server names across every declaration it read, so
    the approval-policy check can compare a gate name against them. ``None`` means
    a declaration existed but could not be read (invalid JSON, a missing declared
    path, or a config that failed to validate); it poisons the union, because a
    single unreadable source makes the declared-server set unknowable. An empty
    set is a different fact: a declaration was read and named no servers.

    The declaration objects are still walked here for their error layering (the
    ``_Collector`` messages), but the RETURNED set -- which
    ``_validate_approval_policy`` builds gate prefixes from -- comes from the
    shared ``declared_mcp_server_names`` derivation the runtime loader also uses,
    so the deploy validator and the runner normalize gate names identically by
    construction (#453/#703). Deriving both from one function is the anti-drift
    guarantee; the per-source walks below only produce actionable errors.
    """

    declared = manifest.mcpServers
    if isinstance(declared, dict):
        _validate_mcp_object(declared, "plugin.json (mcpServers)", c)
    elif isinstance(declared, str):
        c.error(
            "mcp.declared_pointer",
            f"manifest mcpServers is the path {declared!r}, a form the loader "
            "ignores: the servers never register. Declare them as an inline "
            'object instead: "mcpServers": {"<name>": {"command": "..."}}.',
            "plugin.json",
        )

    root_mcp = root / ".mcp.json"
    if root_mcp.is_file():
        _validate_mcp_file(root_mcp, ".mcp.json", c)

    return declared_mcp_server_names(root)


def _validate_mcp_file(path: Path, location: str, c: _Collector) -> set[str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        c.error("mcp.invalid_json", f"{location} is not valid JSON: {exc}", location)
        return None
    return _validate_mcp_object(data, location, c)


def _validate_mcp_object(obj: object, location: str, c: _Collector) -> set[str] | None:
    # Accept both a full config object ({"mcpServers": {...}}) and a bare servers
    # map ({name: server}), which is how the manifest carries an inline value.
    payload = obj if isinstance(obj, dict) and "mcpServers" in obj else {"mcpServers": obj}
    try:
        config = McpConfig.model_validate(payload)
    except ValidationError as exc:
        for issue in _explain(exc):
            c.error("mcp.invalid", issue, location)
        return None

    for name, server in config.mcpServers.items():
        if server.command is None and server.url is None:
            c.error(
                "mcp.server_incomplete",
                f"mcp server {name!r} must define either 'command' (stdio) or 'url' (remote)",
                location,
            )

    return set(config.mcpServers)


def _validate_hooks(root: Path, manifest: PluginManifest, c: _Collector) -> None:
    """Validate the manifest ``hooks`` declaration (deploy-time gate, #272).

    ``hooks`` may be an inline object or a path to a hooks JSON file; either form
    must parse as ``{event: [ {matcher?, hooks: [{type, command}]} ]}``. A
    ``command`` hook must carry a non-empty ``command`` so a malformed guardrail
    is rejected before it ships (the runner enforces PreToolUse at run time).
    """

    declared = manifest.hooks
    if declared is None:
        return

    if isinstance(declared, str):
        hooks_path = root / declared
        if not hooks_path.is_file():
            c.error(
                "hooks.declared_missing",
                f"manifest hooks path {declared!r} was not found",
                "plugin.json",
            )
            return
        location = str(Path(declared))
        try:
            data: object = json.loads(hooks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            c.error("hooks.invalid_json", f"{location} is not valid JSON: {exc}", location)
            return
    elif isinstance(declared, dict):
        location = "plugin.json (hooks)"
        data = declared
    else:
        c.error("hooks.invalid", "hooks must be an object or a path string", "plugin.json")
        return

    try:
        parsed = _HOOKS_ADAPTER.validate_python(data)
    except ValidationError as exc:
        for issue in _explain(exc):
            c.error("hooks.invalid", issue, location)
        return

    # Each command-type hook must actually declare a command.
    for event, matchers in parsed.items():
        for m_i, matcher in enumerate(matchers):
            for h_i, hook in enumerate(matcher.hooks):
                if hook.type == "command" and not (hook.command and hook.command.strip()):
                    c.error(
                        "hooks.command_missing",
                        f"{event}[{m_i}].hooks[{h_i}]: a 'command' hook must define a "
                        "non-empty command",
                        location,
                    )


def _validate_triggers(manifest: PluginManifest, c: _Collector) -> None:
    """Validate the manifest ``triggers`` declarations (deploy-time gate, #273).

    ``triggers`` is a list of ``{type, ...}``. ``type`` must be a known kind
    (``cron``/``webhook``); a ``cron`` trigger requires a non-empty ``schedule``
    and a ``webhook`` trigger a non-empty ``path``. Malformed declarations are
    rejected at deploy so an agent's non-chat wake-ups fail loudly before ship.
    """

    declared = manifest.triggers
    if declared is None:
        return
    if not isinstance(declared, list):
        c.error("triggers.invalid", "triggers must be a list of declarations", "plugin.json")
        return

    try:
        parsed = _TRIGGERS_ADAPTER.validate_python(declared)
    except ValidationError as exc:
        for issue in _explain(exc):
            c.error("triggers.invalid", issue, "plugin.json (triggers)")
        return

    for i, trigger in enumerate(parsed):
        loc = f"plugin.json (triggers[{i}])"
        if trigger.type not in _TRIGGER_TYPES:
            c.error(
                "triggers.unknown_type",
                f"trigger type {trigger.type!r} is not one of {list(_TRIGGER_TYPES)}",
                loc,
            )
            continue
        if trigger.type == "cron" and not (trigger.schedule and trigger.schedule.strip()):
            c.error(
                "triggers.cron_missing_schedule",
                "a 'cron' trigger must define a non-empty 'schedule'",
                loc,
            )
        if trigger.type == "webhook" and not (trigger.path and trigger.path.strip()):
            c.error(
                "triggers.webhook_missing_path",
                "a 'webhook' trigger must define a non-empty 'path'",
                loc,
            )


def _validate_approval_policy(
    manifest: PluginManifest,
    mcp_servers: set[str] | None,
    connector_servers: set[str] | None,
    c: _Collector,
) -> None:
    """Validate the manifest ``approvalPolicy`` declaration (deploy-time, #273).

    Shape ``{gates: [{gate, route}]}``: each gate names a pause point and the
    route that decides. A malformed policy or a gate missing its ``gate``/``route``
    is rejected at deploy.

    ``mcp_servers`` is the set of MCP server names the bundle declares (from
    ``_validate_mcp``). A gate that carries the ``mcp__`` prefix must be a live,
    fully-namespaced tool name (``mcp__plugin_<bundle>_<server>__<tool>``); the
    runner matches a gate by exact string equality, so the natural
    ``mcp__<server>__<tool>`` shape arms nothing and silently never fires. We
    reject it here. ``None`` means the MCP declaration could not be read, so the
    cross-check stays silent rather than stacking a misleading error on top of the
    MCP error that already fired.

    ``connector_servers`` is the ``connectors.yaml`` half of the same tool surface
    (#1495). A connector is mounted on the SDK's ``mcp_servers`` map rather than
    loaded as a plugin, so its live tool name is ``mcp__<connector>__<tool>`` with
    NO ``plugin_<bundle>_`` infix -- a DIFFERENT namespacing rule, which is why the
    two sets stay separate and each builds its own prefix
    (``connector_tool_prefix`` vs ``effective_tool_prefix``). Without this source a
    bundle that declares its whole tool surface through ``connectors.yaml`` has an
    empty accepted set, so every gate it could write is rejected here and no
    connector tool can be gated at all. ``None`` carries the same
    read-it-or-stay-silent meaning as ``mcp_servers``.
    """

    declared = manifest.approvalPolicy
    if declared is None:
        return
    if not isinstance(declared, dict):
        c.error(
            "approval_policy.invalid",
            "approvalPolicy must be an object with a 'gates' list",
            "plugin.json",
        )
        return

    try:
        policy = ApprovalPolicy.model_validate(declared)
    except ValidationError as exc:
        for issue in _explain(exc):
            c.error("approval_policy.invalid", issue, "plugin.json (approvalPolicy)")
        return

    # Construct the valid prefixes from what the bundle declares rather than
    # parsing the gate to extract a server name: a bundle name cannot contain '_'
    # (_NAME_RE) but a server key can, so parsing mcp__plugin_a_b_c__t is
    # ambiguous. Constructing and testing startswith sidesteps that entirely.
    # Each source contributes its OWN prefix form: a plugin-loaded server gets
    # mcp__plugin_<bundle>_<server>__, a connectors.yaml server gets the bare
    # mcp__<connector>__ the SDK produces for a directly-mounted server (#1495).
    # A single unreadable source makes the accepted set unknowable, so either
    # being None suppresses the check rather than asserting against half of it.
    expected_prefixes: set[str] | None = (
        {effective_tool_prefix(manifest.name, s) for s in mcp_servers}
        | {connector_tool_prefix(s) for s in connector_servers}
        if mcp_servers is not None and connector_servers is not None
        else None
    )

    for i, gate in enumerate(policy.gates):
        loc = f"plugin.json (approvalPolicy.gates[{i}])"
        if not (gate.gate and gate.gate.strip()) or not (gate.route and gate.route.strip()):
            c.error(
                "approval_policy.incomplete",
                "an approval gate must define a non-empty 'gate' and 'route'",
                loc,
            )
            continue

        # A gate without the mcp__ prefix names a built-in tool (Bash, Write,
        # PreToolUse); it is armed by raw name and never touched here. Evaluate
        # the STRIPPED value: the runner strips the gate before matching
        # (approval.py load_approval_policy), so a leading-space "mcp__..." that
        # looks built-in on the raw string would arm a mis-namespaced tool at
        # runtime and silently never fire (#453).
        stripped_gate = gate.gate.strip()
        if expected_prefixes is None or not stripped_gate.startswith("mcp__"):
            continue

        # A live tool name needs a non-empty tool suffix after the matched
        # prefix; the bare "mcp__plugin_<bundle>_<server>__" arms nothing (#453).
        if not any(
            stripped_gate.startswith(prefix) and len(stripped_gate) > len(prefix)
            for prefix in expected_prefixes
        ):
            c.error(
                "approval_policy.gate_not_namespaced",
                _gate_not_namespaced_message(
                    stripped_gate,
                    manifest.name,
                    mcp_servers or set(),
                    connector_servers or set(),
                ),
                loc,
            )

    # #558: an operator-opted grantable route claimed by more than one distinct
    # tool would validate green yet arm no grant (the ambiguous route is excluded
    # by the shared normalizer), so reject it here. grantable_routes is the SAME
    # helper the runtime loader uses, so the validator and loader agree on which
    # routes are grantable by construction (#453).
    _, ambiguous = grantable_routes(policy.gates)
    for route in sorted(ambiguous):
        c.error(
            "approval_policy.grant_route_ambiguous",
            f"more than one grantableViaPolicy gate claims route {route!r} with a"
            " different tool; it is ambiguous which tool a policy approval would"
            " grant, so the route would validate but arm no grant. Make the"
            " grantable gates on this route name the same tool, or drop the opt-in.",
            "plugin.json (approvalPolicy)",
        )


def _gate_not_namespaced_message(
    gate: str, bundle: str, mcp_servers: set[str], connector_servers: set[str]
) -> str:
    """Actionable message for a gate whose mcp__ name is not a live tool name."""

    expected = sorted(
        [f"{effective_tool_prefix(bundle, s)}<tool>" for s in mcp_servers]
        + [f"{connector_tool_prefix(s)}<tool>" for s in connector_servers]
    )
    if expected:
        declared = "Expected one of: " + ", ".join(expected)
    else:
        declared = f"This bundle declares no MCP servers and no {CONNECTORS_FILE} connectors"
    return (
        f"approval gate {gate!r} is not a live MCP tool name. A bundle-declared "
        f"MCP tool's live name is mcp__plugin_{bundle}_<server>__<tool>. A "
        f"{CONNECTORS_FILE} connector is mounted directly instead of loaded as a "
        "plugin, so ITS live name is mcp__<connector>__<tool>, with no "
        f"plugin_{bundle}_ infix. "
        f"{declared}. A built-in tool gate (e.g. Bash) carries no mcp__ prefix. "
        "A MANIFEST approvalPolicy gate must be the fully-namespaced live name "
        "(this deploy gate does not normalize it). The per-agent "
        "CURIE_APPROVAL_REQUIRED_TOOLS env knob is more lenient (#703): the runner "
        "normalizes a bare mcp__<server>__<tool> shorthand for a DECLARED server to "
        "its effective name, and also accepts an already-namespaced "
        "mcp__plugin_<bundle>_<server>__<tool> or a built-in name -- but only for a "
        "server the bundle declares; it never arms a name that names no declared "
        "server. Namespace this manifest gate to its live name."
    )


def _validate_tool_policy(
    manifest: PluginManifest,
    mcp_servers: set[str] | None,
    connector_servers: set[str] | None,
    enforces: str | None,
    c: _Collector,
) -> None:
    """Validate the manifest ``toolPolicy`` declaration (deploy-time).

    Shape ``{enforcement, allow, approvalRequired, deny}``: glob collections over
    canonical ``"<server>/<tool>"`` tool names. Every grammar, duplicate and
    conflict rule lives in ``tool_policy`` and is CALLED from here, never
    reimplemented -- ``approval_policy.py`` exists precisely because a deploy
    validator and a runtime loader that normalize separately silently disagree
    and ship a fail-open (#453/#544), and the runtime lane for this contract is
    still to be written.

    ``mcp_servers`` and ``connector_servers`` are the same two sets
    ``_validate_approval_policy`` receives, from the same locals, so the two
    checks provably see an identical view of the bundle. Unlike the approval
    check the union here is of bare server NAMES rather than live tool prefixes,
    because the canonical form is transport-independent and carries the same
    server name for both mount styles. ``None`` on either side means a
    declaration existed but could not be read, so the accepted set is unknowable
    and the cross-check stays silent rather than stacking a misleading second
    error on top of the ``mcp.*`` / ``connectors.*`` error that already fired.

    Nothing here early-returns past work it owes once the policy has parsed: a
    wrong ``enforcement`` id and three bad globs are four errors in one pass, not
    four ``curie build`` round-trips.
    """

    declared = manifest.toolPolicy
    if declared is None:
        # The backward-compatible path every bundle shipped to date takes.
        return

    # The enforcement HANDSHAKE, and the reason this extension is safe to add at
    # all. Declaring a policy that no consumer applies is strictly worse than
    # declaring none: the bundle reads as fenced and runs unfenced. So a
    # policy-bearing bundle is refused outright until its validating caller
    # states which contract it enforces. Reported before the shape checks and
    # WITHOUT returning, so an author sees both this and any real defects.
    if enforces != TOOL_POLICY_ENFORCEMENT:
        c.error(
            "tool_policy.unenforced",
            f"this bundle declares a toolPolicy, but the caller validating it enforces "
            f"{enforces!r} rather than {TOOL_POLICY_ENFORCEMENT!r}. Accepting the bundle "
            "here would apply NO policy at all, leaving the agent's tool surface "
            "completely unfenced while the manifest claims otherwise. Runtime "
            "enforcement of the tool policy is a separate, blocking follow-up; until "
            "it lands and passes enforces_tool_policy="
            f"{TOOL_POLICY_ENFORCEMENT!r}, no bundle may ship a toolPolicy.",
            "plugin.json (toolPolicy)",
        )

    if not isinstance(declared, dict):
        c.error(
            "tool_policy.invalid",
            "toolPolicy must be an object with an 'enforcement' id and the "
            "'allow'/'approvalRequired'/'deny' glob collections",
            "plugin.json",
        )
        return

    try:
        policy = ToolPolicy.model_validate(declared)
    except ValidationError as exc:
        for issue in _tool_policy_invalid_messages(exc):
            c.error("tool_policy.invalid", issue, "plugin.json (toolPolicy)")
        return

    # Compared EXACTLY -- deliberately NOT stripped, matching
    # ``load_tool_policy``. The id is a wire constant a bundle writes verbatim,
    # so " curie/mcp-tool-policy@1 " is not it; refusing it here is fail-CLOSED,
    # where accepting it would silently apply v1 rules to a string that is not
    # the v1 id. This differs on purpose from the approval-gate check above,
    # which strips GATE names so the validator and the runtime loader agree on
    # one tool name: a version discriminator is not a tool name. Do not "fix"
    # this back to a ``.strip()``.
    enforcement = policy.enforcement
    if enforcement != TOOL_POLICY_ENFORCEMENT:
        c.error(
            "tool_policy.enforcement_unsupported",
            f"toolPolicy.enforcement is {enforcement!r}, which this build does not "
            f"implement; it implements {TOOL_POLICY_ENFORCEMENT!r}. The id is a "
            "versioned discriminator: a bundle asking for different semantics is "
            "rejected rather than reinterpreted under this build's rules.",
            "plugin.json (toolPolicy)",
        )

    # The SHARED rule set: this is the same ``check_policy_patterns`` call
    # ``tool_policy.load_tool_policy`` makes, and the ONLY difference between the
    # two paths is the rendering. Here each issue becomes its own
    # ``ValidationIssue`` with a code and a location, so an author fixes every
    # typo in one ``curie build``; the loader has no per-issue surface and
    # collapses them into a single ``ToolPolicyInvalid``. Never inline a rule
    # here -- a validator and a runtime loader that own separate copies of a
    # grammar silently diverge and ship a fail-open (#453/#544).
    for defect in check_policy_patterns(policy):
        c.error(
            f"tool_policy.{defect.code}",
            defect.message,
            f"plugin.json (toolPolicy.{defect.collection}[{defect.index}])",
        )

    # The declared-server cross-check. A pattern whose server segment is a
    # LITERAL name (``literal_server_segment`` returns ``None`` for a wildcarded
    # or malformed one) must name a server the bundle actually declares, in
    # either the MCP map or connectors.yaml -- a typo'd segment is an inert rule
    # the author believes is live. A wildcard segment is the deliberate escape
    # hatch for a bundle whose servers are not statically declared and is never
    # cross-checked; that makes this check advisory, which is accepted, because
    # the property that actually defends the capability is classify_tool's
    # unmatched-is-DENY default, not this check.
    expected_servers: set[str] | None = (
        mcp_servers | connector_servers
        if mcp_servers is not None and connector_servers is not None
        else None
    )
    if expected_servers is not None:
        for collection, i, pattern in policy_patterns(policy):
            server = literal_server_segment(pattern)
            if server is None or server in expected_servers:
                continue
            c.error(
                "tool_policy.unknown_server",
                _unknown_tool_policy_server_message(pattern, server, expected_servers),
                f"plugin.json (toolPolicy.{collection}[{i}])",
            )

    if not policy.deny and not policy.approvalRequired and not policy.allow:
        # Coherent, not vacuous: with three empty collections every tool falls
        # through to the DENY default, so the agent may call no MCP tool at all.
        # A WARNING rather than an error because that is fail-CLOSED and
        # rejecting it would make "fence this agent out entirely" inexpressible
        # -- but it is far more often a half-finished edit.
        c.warn(
            "tool_policy.denies_everything",
            "toolPolicy declares no patterns in 'allow', 'approvalRequired' or "
            "'deny', so every MCP tool falls through to the deny-by-default rule "
            "and the agent can call none of them. That is coherent if it is what "
            "you meant; otherwise the collections are unfinished.",
            "plugin.json (toolPolicy)",
        )


def _tool_policy_invalid_messages(exc: ValidationError) -> list[str]:
    """``_explain`` for a toolPolicy, with a pointed message for an unknown key.

    ``ToolPolicy`` is the one strict model reached from the manifest, so pydantic's
    generic "Extra inputs are not permitted" is the message an author is most
    likely to hit -- and the case where a bare restatement helps least. A
    misspelled collection (``"denny"``) is a typo that would otherwise become
    permission widening, so the message names the offending key and the three
    collections it was probably meant to be.

    Only that one case is special-cased; every other error keeps ``_explain``'s
    generic rendering, from ``_explain`` itself, so toolPolicy's locations cannot
    drift from the rest of the file's.
    """

    def rewrite(err: Mapping[str, Any], loc: str) -> str | None:
        if err["type"] != "extra_forbidden":
            return None
        return (
            f"{loc}: unknown key in toolPolicy. A tool policy is a Curie-owned "
            "authorization object, so unknown keys are rejected rather than "
            "ignored: a misspelled collection would silently drop the rules it "
            "was meant to carry. Valid keys are 'enforcement', 'allow', "
            "'approvalRequired' and 'deny'."
        )

    return _explain(exc, rewrite=rewrite)


def _unknown_tool_policy_server_message(
    pattern: str, server: str, expected_servers: set[str]
) -> str:
    """Actionable message for a pattern naming a server the bundle does not declare."""

    if expected_servers:
        declared = "This bundle declares: " + ", ".join(sorted(expected_servers))
    else:
        declared = f"This bundle declares no MCP servers and no {CONNECTORS_FILE} connectors"
    return (
        f"tool pattern {pattern!r} names server {server!r}, which this bundle does "
        f"not declare. {declared}. A canonical tool name is '<server>/<tool>', where "
        "<server> is the bare server name from the manifest's mcpServers map or "
        f"from {CONNECTORS_FILE} -- NOT the live mcp__... SDK name, whose namespacing "
        "differs between the two mount styles. Fix the server name, declare the "
        "server, or use a wildcard segment (e.g. '*/<tool>') if the server is not "
        "statically declared."
    )


def _validate_secrets(manifest: PluginManifest, c: _Collector) -> None:
    """Validate the manifest ``secrets`` policy (deploy-time gate, ADR-0009 / #429).

    ``secrets`` is a list of the named connector secrets the bundle needs (the
    NAMES only, never values). Each must be an environment-variable-style name
    (``^[A-Z_][A-Z0-9_]*$``) so it can be forwarded into the sandbox env and
    consumed by ``.mcp.json`` ``${VAR}`` expansion; a malformed name is rejected
    at deploy before an agent ships expecting a secret that can never bind.
    """

    declared = manifest.secrets
    if declared is None:
        return
    if not isinstance(declared, list):
        c.error("secrets.invalid", "secrets must be a list of names", "plugin.json")
        return

    for i, name in enumerate(declared):
        loc = f"plugin.json (secrets[{i}])"
        if not isinstance(name, str) or not SECRET_NAME_RE.match(name):
            c.error(
                "secrets.name_invalid",
                f"secret name {name!r} must be an env-var-style name "
                "(uppercase letters, digits, underscore; not starting with a digit)",
                loc,
            )
        elif is_reserved_boot_env_name(name):
            # Reserved sandbox boot-env / model-credential keys (#457, #445):
            # the whole CURIE_* namespace plus the runner's non-prefixed
            # credential keys (ANTHROPIC_BASE_URL etc). A connector secret must
            # not declare one -- it would clobber or be dropped by the worker
            # binding at delivery time, or silently redirect the model session.
            c.error(
                "secrets.name_reserved",
                f"secret name {name!r} is reserved: it is a platform boot-env, "
                "model-credential, or redirect/capture-capable key and cannot be "
                "used for a connector secret",
                loc,
            )


def _validate_scripts(root: Path, c: _Collector) -> None:
    scripts = root / "scripts"
    if scripts.exists() and not scripts.is_dir():
        c.error("scripts.not_a_directory", "scripts must be a directory", "scripts")


def _read_frontmatter(skill_file: Path, rel: str, c: _Collector) -> dict[str, Any] | None:
    text = skill_file.read_text(encoding="utf-8")
    if not text.startswith("---"):
        c.error("skill.frontmatter_missing", "SKILL.md has no YAML frontmatter block", rel)
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        c.error(
            "skill.frontmatter_unterminated",
            "SKILL.md frontmatter is not closed by '---'",
            rel,
        )
        return None

    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError as exc:
        c.error("skill.frontmatter_invalid_yaml", f"frontmatter is not valid YAML: {exc}", rel)
        return None

    if not isinstance(loaded, dict):
        c.error("skill.frontmatter_invalid", "frontmatter must be a YAML mapping", rel)
        return None
    return loaded


def _explain(
    exc: ValidationError,
    *,
    rewrite: Callable[[Mapping[str, Any], str], str | None] | None = None,
) -> list[str]:
    """One ``"<loc>: <message>"`` line per pydantic error, in pydantic's order.

    ``rewrite`` lets a caller replace the line for errors it can explain better,
    returning ``None`` to keep the generic one. It is handed the already-rendered
    ``loc`` so no caller re-derives the location format: a second copy of the
    join-and-``(root)`` rule is how one validator's messages silently start
    pointing at locations differently from every other validator's.
    """

    out: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        replacement = rewrite(err, loc) if rewrite is not None else None
        out.append(replacement if replacement is not None else f"{loc}: {err['msg']}")
    return out
