"""Declared connectors: what a bundle needs running, not how to run it (ADR-0086).

``.mcp.json`` says an MCP server exists and where to reach it. It has never said
who *runs* it, so every bundle author writing a non-stdio connector had to
hand-author a Deployment, a Service, a Secret reference, container hardening,
and a NetworkPolicy -- roughly 180 lines of Kubernetes to stand up one server,
per bundle, each copy independently right or wrong.

``connectors.yaml`` closes that. A bundle declares intent; the platform derives
the objects. Two forms:

    connectors:
      grafana:                                  # hosted: Curie runs the image
        image: grafana/mcp-grafana:0.17.2
        args: [-t, streamable-http, -disable-write]
        env: {GRAFANA_URL: "https://grafana.example.com"}
        secrets: [GRAFANA_SERVICE_ACCOUNT_TOKEN]

      internal:                                 # remote: already running
        url: https://mcp.internal/mcp
        secrets: [INTERNAL_TOKEN]

Secrets are NAMES only. Values arrive at deploy time through the existing
mechanism (ADR-0009), so this introduces no new way to carry a credential and
a bundle stays safe to commit.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# A connector name becomes a Kubernetes object name and a DNS label, so it is
# held to the stricter of those: lowercase alphanumeric and dashes, starting and
# ending alphanumeric. Rejecting here beats a confusing apply-time failure.
_NAME_MAX = 40


class ConnectorSpec(BaseModel):
    """One declared connector. Exactly one of ``image`` or ``url``.

    Strict, unlike the rest of this package. The leniency mandate exists because
    real Claude Code plugin bundles carry keys this MVP does not model, and
    rejecting them would reject valid bundles. ``connectors.yaml`` is not a
    Claude Code artifact -- it is Curie's own file with no external producer, so
    an unrecognised key is a typo, and silently ignoring it would mean a
    connector that renders subtly wrong with no diagnostic.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # -- hosted form --
    image: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    port: int = 8000

    # -- remote form --
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    # -- both --
    secrets: list[str] = Field(default_factory=list)

    @property
    def is_hosted(self) -> bool:
        return self.image is not None


class ConnectorsFile(BaseModel):
    """The parsed ``connectors.yaml``."""

    model_config = ConfigDict(extra="forbid")

    connectors: dict[str, ConnectorSpec] = Field(default_factory=dict)


_NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")


def _is_valid_name(name: str) -> bool:
    """RFC 1123 label, capped: the name becomes a k8s object and a DNS label."""

    return len(name) <= _NAME_MAX and bool(_NAME_RE.fullmatch(name))


def validate_connectors(data: Any) -> tuple[ConnectorsFile | None, list[tuple[str, str]]]:
    """Validate parsed ``connectors.yaml`` content.

    Returns ``(parsed, errors)`` where each error is ``(code, message)``. A
    non-empty error list means the file must be rejected -- these are all
    conditions that would otherwise surface as an opaque Kubernetes apply
    failure long after the author has stopped looking.
    """

    errors: list[tuple[str, str]] = []
    if data is None:
        return ConnectorsFile(), errors
    if not isinstance(data, dict):
        return None, [("connectors.not_object", "connectors.yaml must be a mapping")]

    try:
        parsed = ConnectorsFile.model_validate(data)
    except Exception as exc:  # pydantic ValidationError -- surface it verbatim
        return None, [("connectors.invalid", str(exc)[:400])]

    for name, spec in parsed.connectors.items():
        where = f"connectors.{name}"
        if not _is_valid_name(name):
            errors.append(
                (
                    "connectors.bad_name",
                    f"{where}: a connector name becomes a Kubernetes object and DNS "
                    "label, so it must be lowercase alphanumeric or dashes, start and "
                    f"end alphanumeric, and be at most {_NAME_MAX} characters",
                )
            )
        if spec.image and spec.url:
            errors.append(
                (
                    "connectors.ambiguous",
                    f"{where}: set either `image` (Curie runs it) or `url` (already "
                    "running), not both -- otherwise it is unclear who owns the process",
                )
            )
        if not spec.image and not spec.url:
            errors.append(
                (
                    "connectors.underspecified",
                    f"{where}: set `image` for Curie to run it, or `url` to point at "
                    "something already running",
                )
            )
        if spec.url and (spec.args or spec.env):
            errors.append(
                (
                    "connectors.remote_has_runtime",
                    f"{where}: `args`/`env` configure a process Curie starts; a `url` "
                    "connector is already running, so they would be silently ignored",
                )
            )
        if spec.image and spec.headers:
            errors.append(
                (
                    "connectors.hosted_has_headers",
                    f"{where}: `headers` apply to a remote endpoint; configure a hosted "
                    "connector with `env` and `args` instead",
                )
            )
        if not (1 <= spec.port <= 65535):
            errors.append(("connectors.bad_port", f"{where}: port {spec.port} is out of range"))
        for key in spec.env:
            if not key.replace("_", "").isalnum() or key[:1].isdigit():
                errors.append(
                    ("connectors.bad_env_name", f"{where}: `{key}` is not a valid env var name")
                )

    return (parsed if not errors else None), errors
