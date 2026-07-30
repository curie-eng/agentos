"""The wire-lock gate: a version gate that actually gates.

The committed compat test pins artifact *sync* -- it fails if the models drift
from the generated files. It cannot tell "the wire changed" from "someone bumped
the version", because both move the generated schema together. This module adds
the missing half: a fingerprint of the wire shape that is *invariant* under a
version bump, and a gate that fails a wire change which was not accompanied by a
bump -- with a message saying which to do.

The fingerprint is built from the *shipped* wire shape, not the raw pydantic
output: the same ``schema_export._require_wire_mandatory_props`` postprocess that
the exported schema goes through is applied first, so an exporter-only change
(e.g. that helper no longer marking ``version`` required) moves the hash too.
The version *values* are then normalized out (decision 4 of the plan) -- the
``protocolVersion`` key that embeds ``PROTOCOL_VERSION`` and any
``const``/``default`` on the version field are placeholdered -- and every
``title``/``description`` key is stripped, because documentation is not
wire-compatibility surface (a doc-only edit must not force a version bump). Field
names, types, ``required``, enums, and structure remain, so the fingerprint still
moves when a field is added or the version field is retyped -- only a pure
version-number bump or a pure doc edit leaves it unchanged.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import models_json_schema

from . import schema_export
from .version import PROTOCOL_VERSION, WIRE_VERSION_FIELD

_VERSION_PLACEHOLDER = "<version>"

_LOCK_NAME = "wire.lock"


class WireLockError(Exception):
    """Raised when the wire changed but PROTOCOL_VERSION was not bumped."""


def _placeholder_version_values(node: Any) -> None:
    """Replace version const/default *values* in place, leaving shape intact.

    Normalizes only the version-number values so a pure version bump does not
    move the fingerprint. The version field's presence, type, and pattern are
    deliberately untouched -- retyping the field must still change the hash.
    """

    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            version_prop = props.get(WIRE_VERSION_FIELD)
            if isinstance(version_prop, dict):
                for key in ("const", "default"):
                    if key in version_prop:
                        version_prop[key] = _VERSION_PLACEHOLDER
        for value in node.values():
            _placeholder_version_values(value)
    elif isinstance(node, list):
        for item in node:
            _placeholder_version_values(item)


def _strip_doc_keys(node: Any) -> None:
    """Remove every ``title``/``description`` key in place.

    Documentation is not wire-compatibility surface: a class docstring or a new
    ``Field(description=...)`` renders into the schema as ``title``/``description``
    text but changes nothing a consumer decodes off the wire. Stripping them keeps
    a doc-only edit from moving the fingerprint (the change-class table's
    "Doc/description only -> none"), while field names, types, ``required``,
    enums, and structure -- the real wire surface -- remain and still move it.
    """

    if isinstance(node, dict):
        node.pop("title", None)
        node.pop("description", None)
        for value in node.values():
            _strip_doc_keys(value)
    elif isinstance(node, list):
        for item in node:
            _strip_doc_keys(item)


def wire_fingerprint(models: tuple[type[BaseModel], ...] | None = None) -> str:
    """Fingerprint the shipped wire shape of ``models`` (defaults to the set).

    The same ``_require_wire_mandatory_props`` postprocess the exported schema
    goes through is applied first (so an exporter-only change to the shipped wire
    moves the hash), version values are normalized out (invariant under a version
    bump), and ``title``/``description`` are stripped (invariant under a doc-only
    edit). The hash still changes when a field is added, removed, retyped, or
    renamed.
    """

    if models is None:
        models = schema_export._MODELS
    _, top = models_json_schema(
        [(model, "validation") for model in models],
        ref_template="#/$defs/{model}",
    )
    # ``top`` is a fresh models_json_schema result owned by this call, so it is
    # normalized in place rather than copied.
    normalized = top
    if isinstance(normalized.get("$defs"), dict):
        schema_export._require_wire_mandatory_props(normalized["$defs"])
    normalized.pop("protocolVersion", None)
    _strip_doc_keys(normalized)
    _placeholder_version_values(normalized)
    canonical = json.dumps(normalized, indent=2, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_wire_lock(*, fingerprint: str, version: str, lock: dict[str, str]) -> None:
    """Fail an unbumped wire change; pass a version-only bump or an unchanged wire.

    Truth table (plan decision 4):
    - fingerprint == lock hash -> pass (a gratuitous version-only bump is legal).
    - fingerprint != lock hash and version == lock version -> fail (the escape
      hatch): the wire moved without a bump.
    - fingerprint != lock hash and version != lock version -> pass; the caller
      must regenerate and commit the lock.
    """

    if fingerprint == lock["wire_sha256"]:
        return
    if version != lock["protocol_version"]:
        return
    raise WireLockError(
        "The ACI wire changed but PROTOCOL_VERSION was not bumped "
        f"(still {version!r}). Bump PROTOCOL_VERSION to the class the change "
        "earns per the semver table in packages/CLAUDE.md, then regenerate the "
        "lock with scripts/check-contracts.sh and commit it."
    )


def check_against_base(base_lock: dict[str, str] | None) -> None:
    """Fail an unbumped wire change measured against the base branch's lock.

    ``test_committed_lock_matches_the_current_wire`` only proves the lock matches
    the *current* wire -- it passes even if an author regenerated or hand-forged
    the lock without bumping. This compares the current wire against the BASE
    (origin/main) lock instead, so a wire change at the same version as base
    raises :class:`WireLockError`. A ``None`` base lock (the base predates the
    lock, true for this PR and all pre-lock history) is not a failure and returns
    quietly.
    """

    if base_lock is None:
        return
    check_wire_lock(fingerprint=wire_fingerprint(), version=PROTOCOL_VERSION, lock=base_lock)


def _lock_path() -> Path:
    return schema_export.schema_path().parent / _LOCK_NAME


def write_lock(models: tuple[type[BaseModel], ...] | None = None) -> Path:
    """Rewrite the committed wire.lock, refusing an unbumped wire change.

    Enforces the same rule as :func:`check_wire_lock` before touching disk, so an
    author cannot silently rewrite the lock without a bump. The refusal fires
    *before* the file is written, keeping the tree clean.
    """

    fingerprint = wire_fingerprint(models)
    version = PROTOCOL_VERSION
    path = _lock_path()
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        check_wire_lock(fingerprint=fingerprint, version=version, lock=existing)
    payload = {"protocol_version": version, "wire_sha256": fingerprint}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "--check-base":
        # CI base gate: compare the current wire against the base branch's lock.
        # A missing or empty base lock (no lock on base yet) is treated as None
        # and skips cleanly -- only a genuine unbumped wire change vs an existing
        # base lock exits non-zero.
        base_path = Path(sys.argv[2])
        base_lock: dict[str, str] | None = None
        if base_path.exists():
            raw = base_path.read_text(encoding="utf-8").strip()
            if raw:
                base_lock = json.loads(raw)
        try:
            check_against_base(base_lock)
        except WireLockError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
        print("wire-lock base gate: ok")
    else:
        written = write_lock()
        print(f"wrote {written}")
