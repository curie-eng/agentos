"""The ONE parser for a bundle's ``deploy.yaml`` text (ADR-0089).

Extracted from ``routers/deploy_targets.py`` so a second consumer -- the
git-flow routing check of #1221 -- cannot end up with a private copy. Two
parsers for this file is the exact failure ADR-0089 put parsing in the API to
prevent: the file's whole job is to be unambiguous about where a deploy lands,
and a disagreement routes a deploy somewhere the author did not intend while
reporting success. A copy inside a router is that second parser by the back
door, so it lives here, imported by both.
"""

import yaml
from fastapi import HTTPException, status
from plugin_format.deploy_targets import DeployTargetsFile, validate_deploy_targets
from plugin_format.yaml_loader import DuplicateKeyError, safe_load_unique


def parse_deploy_targets(content: str) -> DeployTargetsFile:
    """Parse and validate ``deploy.yaml``, or raise the operator-facing 400.

    Shared by every endpoint that reads this format so they cannot disagree
    about whether a file is valid -- which would be a second parser by the back
    door, the exact thing ADR-0089 put this in the API to prevent.
    """

    try:
        data = safe_load_unique(content)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"deploy.duplicate_target: deploy.yaml contains duplicate key {exc.key!r}",
        ) from exc
    except yaml.YAMLError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"deploy.yaml is unparseable: {exc}"
        ) from exc

    parsed, errors = validate_deploy_targets(data)
    if errors:
        detail = "; ".join(f"{code}: {message}" for code, message in errors)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail)
    assert parsed is not None
    return parsed
