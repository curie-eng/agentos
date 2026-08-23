"""Worker-only repository credential redemption for managed workspaces."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Response, status

from .. import crud
from ..auth import verify_internal_worker_token
from ..config import get_settings
from ..deps import SessionDep
from ..repository_credentials import resolve_repository_credential
from ..schemas import RepositoryCredentialOut

router = APIRouter(prefix="/v1/internal/workspaces", tags=["internal-workspaces"])


@router.post("/{deployment_id}/credential", response_model=RepositoryCredentialOut)
async def redeem_workspace_credential(
    deployment_id: uuid.UUID,
    session: SessionDep,
    response: Response,
    x_curie_worker_token: Annotated[
        str | None, Header(alias="X-Curie-Worker-Token")
    ] = None,
) -> RepositoryCredentialOut:
    response.headers["Cache-Control"] = "no-store"
    deployment = await crud.get_deployment(session, deployment_id)
    repo = deployment.workspace_repo if deployment is not None else None
    if not verify_internal_worker_token(x_curie_worker_token):
        await crud.append_credential_redemption_audit(
            session,
            purpose="workspace_clone",
            outcome="refused",
            deployment_id=deployment.id if deployment is not None else None,
            publication_id=None,
            repo_full_name=repo,
            detail="internal worker authentication refused",
        )
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "missing or invalid internal worker token",
            headers={"Cache-Control": "no-store"},
        )
    if deployment is None:
        await crud.append_credential_redemption_audit(
            session,
            purpose="workspace_clone",
            outcome="refused",
            deployment_id=None,
            publication_id=None,
            repo_full_name=None,
            detail="deployment not found",
        )
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "deployment not found",
            headers={"Cache-Control": "no-store"},
        )
    if repo is None:
        await crud.append_credential_redemption_audit(
            session,
            purpose="workspace_clone",
            outcome="refused",
            deployment_id=deployment.id,
            publication_id=None,
            repo_full_name=None,
            detail="deployment has no managed workspace",
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "deployment does not declare a repository workspace",
            headers={"Cache-Control": "no-store"},
        )
    try:
        clone_url, authorization_header = resolve_repository_credential(
            repo, get_settings()
        )
    except Exception as exc:
        await crud.append_credential_redemption_audit(
            session,
            purpose="workspace_clone",
            outcome="refused",
            deployment_id=deployment.id,
            publication_id=None,
            repo_full_name=repo,
            detail="operator credential resolution failed",
        )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "operator repository credential could not be resolved",
            headers={"Cache-Control": "no-store"},
        ) from exc
    await crud.append_credential_redemption_audit(
        session,
        purpose="workspace_clone",
        outcome="issued",
        deployment_id=deployment.id,
        publication_id=None,
        repo_full_name=repo,
        detail="server-derived repository credential issued",
    )
    return RepositoryCredentialOut(
        repo_full_name=repo,
        clone_url=clone_url,
        authorization_header=authorization_header,
    )
