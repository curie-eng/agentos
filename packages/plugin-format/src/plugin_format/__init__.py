"""Claude Code plugin bundle format (verbatim) and its validators.

The plugin bundle format is the Claude Code plugin shape unchanged: compatibility
is the distribution wedge, so this package does not invent format extensions. It
is a frozen interface (see the package README); a change stops the task and
escalates to the orchestrator.
"""

from .approval_policy import (
    connector_server_names,
    connector_tool_prefix,
    declared_mcp_server_names,
    effective_operator_gate,
    grantable_routes,
)
from .archive import (
    DEFAULT_MAX_COMPRESSION_RATIO,
    DEFAULT_MAX_MEMBERS,
    DEFAULT_MAX_UNCOMPRESSED_BYTES,
    UnsupportedArchive,
    bundle_root,
    check_archive_bounds,
    safe_extract,
)
from .manifest import MANIFEST_LOCATIONS, resolve_manifest
from .models import (
    ApprovalGate,
    ApprovalPolicy,
    Author,
    HookDefinition,
    HookMatcherConfig,
    McpConfig,
    McpServer,
    PluginManifest,
    SkillFrontmatter,
    TriggerDeclaration,
)
from .reserved_env import RESERVED_BOOT_ENV, is_reserved_boot_env_name
from .validate import ValidationIssue, ValidationResult, validate_bundle

__version__ = "0.0.0"

__all__ = [
    "__version__",
    "RESERVED_BOOT_ENV",
    "is_reserved_boot_env_name",
    "MANIFEST_LOCATIONS",
    "resolve_manifest",
    "PluginManifest",
    "Author",
    "SkillFrontmatter",
    "McpServer",
    "McpConfig",
    "HookDefinition",
    "HookMatcherConfig",
    "TriggerDeclaration",
    "ApprovalPolicy",
    "ApprovalGate",
    "grantable_routes",
    "declared_mcp_server_names",
    "connector_server_names",
    "connector_tool_prefix",
    "effective_operator_gate",
    "validate_bundle",
    "ValidationResult",
    "ValidationIssue",
    "safe_extract",
    "check_archive_bounds",
    "DEFAULT_MAX_UNCOMPRESSED_BYTES",
    "DEFAULT_MAX_COMPRESSION_RATIO",
    "DEFAULT_MAX_MEMBERS",
    "bundle_root",
    "UnsupportedArchive",
]
