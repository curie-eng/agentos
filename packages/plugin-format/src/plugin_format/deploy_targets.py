"""Declared deploy targets: where a bundle gets sent (ADR-0089).

``connectors.yaml`` says what a bundle needs wherever it runs. ``deploy.yaml``
says where THIS repository sends it. Two files because they are different kinds
of fact with different lifetimes: a connector changes when the agent's
capabilities change, a target changes when the deployment topology does.

    # deploy.yaml
    targets:
      dev:
        agent: acme-dev
        env: dev
        slack_channel: C0EXAMPLE2
      prod:
        agent: acme-bot
        env: prod
        slack_channel: C0EXAMPLE1

    curie cluster deploy --target prod

Before this, routing lived in whatever invoked the command. For acme-bot that
was two GitHub Actions workflows describing a dev/prod split that did not
exist: both resolved to the same agent, overwrote each other's active version,
and contended for the one channel that agent can bind. Nothing reported it,
because ``--env`` defaults to ``dev`` and a prod workflow that omits it deploys
to dev silently.

The bundle is IDENTICAL across targets. Only the binding differs -- which is
what lets prod promote the exact artifact dev validated, and why rewriting
``plugin.json``'s name per environment was rejected.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Curie's two deployment environments. Not open-ended: the worker's binding
# query ranks prod over dev explicitly, so a third value would silently never
# be selected.
_ENVS = ("dev", "prod")

# A target name is a label a human types after `--target`; an agent name becomes
# a platform identity. They are held to the same shape for the same reason
# connector names are -- predictable, no surprises at the boundary.
_NAME_RE = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
_NAME_MAX = 40

# Slack conversation ids are uppercase alphanumeric starting with C (channel),
# G (private group) or D (DM). Checked because a mistyped id binds the agent to
# a channel nobody is watching, and the deploy reports success.
_SLACK_RE = re.compile(r"[CGD][A-Z0-9]{6,}")


class DeployTarget(BaseModel):
    """One named destination for this bundle."""

    model_config = ConfigDict(extra="forbid")

    # Which agent this target binds. Defaults to the manifest name, so a
    # single-agent bundle need not repeat itself.
    agent: str | None = None
    env: str = "dev"
    slack_channel: str | None = None


class DeployTargetsFile(BaseModel):
    """The parsed ``deploy.yaml``."""

    model_config = ConfigDict(extra="forbid")

    targets: dict[str, DeployTarget] = Field(default_factory=dict)


def _is_valid_name(name: str) -> bool:
    return len(name) <= _NAME_MAX and bool(_NAME_RE.fullmatch(name))


def validate_deploy_targets(data: Any) -> tuple[DeployTargetsFile | None, list[tuple[str, str]]]:
    """Validate parsed ``deploy.yaml`` content.

    Returns ``(parsed, errors)`` where each error is ``(code, message)``. Every
    check here exists because the failure it prevents is SILENT: a bad target
    otherwise deploys successfully to the wrong place and says so cheerfully.
    """

    errors: list[tuple[str, str]] = []
    if data is None:
        return DeployTargetsFile(), errors
    if not isinstance(data, dict):
        return None, [("deploy.not_object", "deploy.yaml must be a mapping")]

    try:
        parsed = DeployTargetsFile.model_validate(data)
    except Exception as exc:  # pydantic ValidationError -- surface it verbatim
        return None, [("deploy.invalid", str(exc)[:400])]

    for name, target in parsed.targets.items():
        where = f"targets.{name}"
        if not _is_valid_name(name):
            errors.append(
                (
                    "deploy.bad_target_name",
                    f"{where}: a target name is typed after `--target`, so it must be "
                    "lowercase alphanumeric or dashes, start and end alphanumeric, and be "
                    f"at most {_NAME_MAX} characters",
                )
            )
        if target.env not in _ENVS:
            errors.append(
                (
                    "deploy.bad_env",
                    f"{where}: env must be one of {', '.join(_ENVS)} (got {target.env!r}). "
                    "The worker ranks prod over dev explicitly, so any other value would "
                    "never be selected.",
                )
            )
        if target.agent is not None and not _is_valid_name(target.agent):
            errors.append(
                (
                    "deploy.bad_agent_name",
                    f"{where}: `{target.agent}` is not a valid agent name. A typo here does "
                    "not fail -- it MINTS A NEW AGENT and the deploy reports success, so the "
                    "name is checked before anything is created (ADR-0089).",
                )
            )
        if target.slack_channel is not None and not _SLACK_RE.fullmatch(target.slack_channel):
            errors.append(
                (
                    "deploy.bad_slack_channel",
                    f"{where}: `{target.slack_channel}` is not a Slack conversation id. Use "
                    "the id (starts with C, G, or D), not the #name -- a mistyped id binds "
                    "the agent to a channel nobody is watching and the deploy still succeeds.",
                )
            )

    return (parsed if not errors else None), errors
