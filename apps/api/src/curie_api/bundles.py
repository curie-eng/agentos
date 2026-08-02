"""Plugin bundle intake: detect archive format, extract safely, validate.

The upload path is: bytes -> detect zip/tar(.gz) -> extract into a temp dir
(guarding against path traversal) -> locate the bundle root -> validate via the
frozen ``plugin_format.validate_bundle``. Storage and DB wiring live in the
router; this module is pure intake logic.
"""

import io
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import yaml
from plugin_format import (
    DEFAULT_MAX_COMPRESSION_RATIO,
    DEFAULT_MAX_MEMBERS,
    DEFAULT_MAX_UNCOMPRESSED_BYTES,
    MANIFEST_LOCATIONS,
    UnsupportedArchive,
    ValidationResult,
    bundle_root,
    connector_render,
    safe_extract,
    validate_bundle,
)
from plugin_format.connectors import ConnectorsFile, validate_connectors
from plugin_format.deploy_targets import DeployTargetsFile, validate_deploy_targets
from plugin_format.validate import CONNECTORS_FILE, DEPLOY_FILE

# Re-exported so existing catchers (gitflow.py, routers/bundles.py, tests) keep
# resolving ``bundles.UnsupportedArchive`` after the extraction logic moved to
# plugin_format; safe_extract raises this single error for unsafe/unrecognized
# archives.
__all__ = [
    "UnsupportedArchive",
    "bundle_root",
    "detect_format",
    "extract_and_validate",
    "read_bundle_text_files",
]

# Detected format -> (stored key extension, content type). Detection sniffs the
# bytes rather than trusting the upload filename.
_ZIP = (".zip", "application/zip")
_TAR_GZ = (".tar.gz", "application/gzip")
_TAR = (".tar", "application/x-tar")


def detect_format(data: bytes) -> tuple[str, str]:
    """Return (extension, content_type) for the archive, sniffing the bytes."""

    if zipfile.is_zipfile(io.BytesIO(data)):
        return _ZIP
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz"):
            return _TAR_GZ
    except tarfile.TarError:
        pass
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:"):
            return _TAR
    except tarfile.TarError:
        pass
    raise UnsupportedArchive("upload is not a zip or tar(.gz) archive")


def extract_and_validate(
    data: bytes,
    dest: Path,
    *,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> tuple[str, str, ValidationResult]:
    """Detect, extract, and validate. Returns (extension, content_type, result).

    Extraction (with the traversal/symlink/special-file, size/ratio, and
    member-count guards) and the single-wrapper-dir unwrap live in
    ``plugin_format``; this only adds the storage-key/content-type detection the
    upload path needs. The caps default to ``plugin_format``'s generous
    fallbacks; ``deploy.py`` passes the operator-configured ``Settings`` values
    instead.
    """

    extension, content_type = detect_format(data)
    safe_extract(
        data,
        dest,
        max_uncompressed_bytes=max_uncompressed_bytes,
        max_compression_ratio=max_compression_ratio,
        max_members=max_members,
    )
    result = validate_bundle(bundle_root(dest))
    return extension, content_type, result


def read_connectors(root: Path) -> ConnectorsFile:
    """Parse a validated bundle's ``connectors.yaml``, or an empty set.

    Safe to call only after ``validate_bundle`` has passed: every malformed
    shape is already rejected there, so this cannot be the place a bad
    declaration first surfaces.
    """

    path = bundle_root(root) / CONNECTORS_FILE
    if not path.is_file():
        return ConnectorsFile()
    parsed, errors = validate_connectors(yaml.safe_load(path.read_text(encoding="utf-8")))
    if errors or parsed is None:  # pragma: no cover -- validate_bundle gates this
        return ConnectorsFile()
    return parsed


def read_deploy_targets(root: Path) -> DeployTargetsFile | None:
    """Parse a validated bundle's ``deploy.yaml``, or None when the file is ABSENT.

    None is not an error. A bundle without ``deploy.yaml`` predates ADR-0089
    and still deploys to the single agent its repository binds -- the caller
    must keep working for it, not reject it. A present file that declares no
    targets is a different case: it returns a non null ``DeployTargetsFile``
    with an empty ``targets`` map, not None.
    """

    path = bundle_root(root) / DEPLOY_FILE
    if not path.is_file():
        return None
    parsed, errors = validate_deploy_targets(yaml.safe_load(path.read_text(encoding="utf-8")))
    if errors or parsed is None:  # pragma: no cover -- validate_bundle gates this
        return None
    return parsed


def render_connector_manifests(
    connectors: ConnectorsFile,
    *,
    release: str,
    agent: str,
    namespace: str,
    app_name: str,
    secret_name: str,
) -> list[dict[str, Any]]:
    """Kubernetes objects for a bundle's hosted connectors (ADR-0086, #1063).

    The API renders but never applies. Rendering is a pure function, so it needs
    no cluster access, and the API's RBAC stays the deliberately read-only
    `pods: list` + `pods/log: get` it has today -- which matters because this is
    the component that receives webhooks from the internet. The CLI applies the
    result with the operator's own kubectl credentials, so cluster-write
    authority stays where it already was.
    """

    objects: list[dict[str, Any]] = []
    for name, spec in sorted(connectors.connectors.items()):
        objects.extend(
            connector_render.render(release, agent, namespace, app_name, name, spec, secret_name)
        )
    return objects


def owned_secret_keys(connectors: ConnectorsFile) -> list[str]:
    """Declared secrets whose VALUE the caller must supply (#1163).

    Excludes any that reference a Secret provisioned out of band: resolving
    those would defeat the purpose, and the caller may well not have access to
    them -- which is exactly why the reference form exists (ADR-0090).
    """

    keys: list[str] = []
    for spec in connectors.connectors.values():
        for name in spec.resolved_secrets():
            if name not in keys:
                keys.append(name)
    return sorted(keys)


def connector_mcp_entries(
    connectors: ConnectorsFile, *, release: str, agent: str, namespace: str
) -> dict[str, Any]:
    """The `.mcp.json` entries for declared connectors, keyed by name.

    Derived from the Service the manifests define, so an author never writes a
    URL that resolves in one tier and not another.
    """

    return {
        name: connector_render.mcp_entry(release, agent, namespace, name, spec)
        for name, spec in sorted(connectors.connectors.items())
    }


def _collect_text_files(root: Path) -> list[tuple[str, str]]:
    """The bundle's known text files as (bundle-relative posix path, content).

    Deliberately an allowlist of the bundle's structured text surfaces -- the
    manifest, the skill docs, and the eval cases -- so binaries (and anything
    else) are skipped, not just filtered by a guessed encoding. Paths are
    relative to the bundle root and posix so the UI reads a stable shape.
    """

    candidates: list[Path] = []
    for fixed in (*MANIFEST_LOCATIONS, Path("evals/cases.json")):
        if (root / fixed).is_file():
            candidates.append(root / fixed)
    candidates.extend(p for p in root.glob("skills/**/SKILL.md") if p.is_file())

    files: list[tuple[str, str]] = []
    for path in candidates:
        files.append((path.relative_to(root).as_posix(), path.read_text("utf-8")))
    return sorted(files, key=lambda item: item[0])


def read_bundle_text_files(
    data: bytes,
    *,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> list[tuple[str, str]]:
    """Extract an archive's bytes and return its known text files.

    Mirrors the upload path (detect -> extract into a temp dir, guarding path
    traversal -> unwrap to the bundle root) but reads the text surfaces instead of
    validating. Returns (path, content) pairs; the caller shapes the response.
    The caps default to ``plugin_format``'s generous fallbacks; the router passes
    the operator-configured ``Settings`` values so this path honors the same
    bounds as ingestion (#815) rather than the library defaults.
    """

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        safe_extract(
            data,
            dest,
            max_uncompressed_bytes=max_uncompressed_bytes,
            max_compression_ratio=max_compression_ratio,
            max_members=max_members,
        )
        return _collect_text_files(bundle_root(dest))
