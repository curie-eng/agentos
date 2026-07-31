"""Resolve a named deploy target from a bundle's ``deploy.yaml`` (ADR-0089).

Its own router because it belongs to no agent and no version: it is a pure
function of the text posted to it, with no database, no bundle store, and no
cluster. Hanging it off the bundle router would have given it the prefix
``/agents/{agent_id}/versions/{version_id}/bundle/...``, which is exactly
backwards -- the caller uses this BEFORE an agent exists, to find out which
agent to create.
"""

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from plugin_format.deploy_targets import validate_deploy_targets

from ..auth import require_api_key
from ..schemas import ResolvedTarget, ResolveTargetRequest

router = APIRouter(tags=["deploy-targets"], dependencies=[Depends(require_api_key)])


@router.post("/deploy-targets/resolve", response_model=ResolvedTarget)
async def resolve_deploy_target(body: ResolveTargetRequest) -> ResolvedTarget:
    """Resolve a named target from a bundle's ``deploy.yaml`` (ADR-0089).

    Pure function of the supplied text: no database, no store, no cluster. It
    lives here rather than in the CLI so there is exactly ONE parser for this
    format. A second implementation in Rust could disagree with this one about
    the same file, and the file's entire purpose is to be unambiguous about
    where a deploy lands -- a disagreement would route a deploy somewhere the
    author did not intend and report success.

    Validation errors are returned rather than swallowed, because every one of
    them describes a deploy that would otherwise succeed against the wrong
    agent, environment, or channel.
    """

    try:
        data = yaml.safe_load(body.content)
    except yaml.YAMLError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"deploy.yaml is unparseable: {exc}"
        ) from exc

    parsed, errors = validate_deploy_targets(data)
    if errors:
        detail = "; ".join(f"{code}: {message}" for code, message in errors)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail)
    assert parsed is not None

    target = parsed.targets.get(body.target)
    if target is None:
        known = ", ".join(sorted(parsed.targets)) or "(none declared)"
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no target named {body.target!r} in deploy.yaml. Declared: {known}",
        )
    return ResolvedTarget(agent=target.agent, env=target.env, slack_channel=target.slack_channel)
