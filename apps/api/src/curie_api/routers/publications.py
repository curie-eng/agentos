"""Durable approval-gated publication control plane.

The API stores private patch state and resolves credentials. Kubernetes and
GitHub side effects belong to the trusted worker publication reconciler.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from starlette.concurrency import run_in_threadpool

from .. import crud
from ..auth import (
    require_api_key,
    require_internal_worker_token,
)
from ..config import get_settings
from ..deps import SessionDep
from ..repository_auth import resolve_repository_credential
from ..schemas import PublicationCreate, PublicationOut, RepositoryCredentialOut
from ..workspace_policy import credential_mode, repository_is_allowed

router = APIRouter(
    prefix="/publications",
    tags=["publications"],
    dependencies=[Depends(require_api_key)],
)
internal_router = APIRouter(
    prefix="/v1/internal/publications", tags=["internal-publications"]
)


@internal_router.post(
    "",
    response_model=PublicationOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_internal_worker_token)],
)
async def create_publication(
    data: PublicationCreate,
    session: SessionDep,
    response: Response,
) -> PublicationOut:
    try:
        patch = data.decoded_patch()
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    patch_limit_bytes = get_settings().publication_patch_max_bytes
    if len(patch) > patch_limit_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"publication patch exceeds the {patch_limit_bytes}-byte limit",
        )
    try:
        publication, created = await crud.create_publication(session, data, patch=patch)
    except crud.PublicationReplayConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except Exception:
        await session.rollback()
        raise
    if not created:
        response.status_code = status.HTTP_200_OK
    return PublicationOut.model_validate(publication)


@router.get("", response_model=list[PublicationOut])
async def list_publications(session: SessionDep, limit: int = 100) -> list[PublicationOut]:
    rows = await crud.list_publications(session, limit=min(max(limit, 1), 200))
    return [PublicationOut.model_validate(row) for row in rows]


@router.get("/{publication_id}", response_model=PublicationOut)
async def get_publication(
    publication_id: uuid.UUID, session: SessionDep
) -> PublicationOut:
    publication = await crud.get_publication(session, publication_id)
    if publication is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "publication not found")
    return PublicationOut.model_validate(publication)


@internal_router.post(
    "/{publication_id}/credential",
    response_model=RepositoryCredentialOut,
    dependencies=[Depends(require_internal_worker_token)],
)
async def redeem_publication_credential(
    publication_id: uuid.UUID,
    session: SessionDep,
    response: Response,
) -> RepositoryCredentialOut:
    response.headers["Cache-Control"] = "no-store"
    publication = await crud.get_publication(session, publication_id)
    repo = publication.repo_full_name if publication is not None else None
    deployment_id = publication.deployment_id if publication is not None else None

    async def refused(code: int, detail: str) -> None:
        await crud.append_credential_redemption_audit(
            session,
            purpose="publication_push",
            outcome="refused",
            deployment_id=deployment_id,
            publication_id=publication.id if publication is not None else None,
            repo_full_name=repo,
            detail=detail,
        )
        raise HTTPException(code, detail, headers={"Cache-Control": "no-store"})

    if publication is None:
        await refused(status.HTTP_404_NOT_FOUND, "publication not found")
    assert publication is not None and repo is not None
    if publication.status not in ("approved", "launching", "running"):
        await refused(
            status.HTTP_409_CONFLICT,
            "publication must be approved before a write credential can be redeemed",
        )
    deployment = await crud.get_deployment(session, publication.deployment_id)
    approval = await crud.get_approval(session, publication.approval_id)
    if deployment is None or approval is None:
        await refused(status.HTTP_409_CONFLICT, "publication workspace binding is absent")
    assert deployment is not None and approval is not None
    selected = await crud.get_thread_workspace(
        session,
        agent_id=deployment.agent_id,
        conversation_id=approval.conversation_id,
    )
    settings = get_settings()
    if (
        selected is None
        or selected.repo_full_name.casefold() != repo.casefold()
        or not repository_is_allowed(repo, settings.github_repo_allowlist)
    ):
        await refused(
            status.HTTP_403_FORBIDDEN,
            "publication repository is no longer authorized for this thread",
        )
    try:
        clone_url, authorization_header = await run_in_threadpool(
            resolve_repository_credential, repo, settings
        )
    except Exception as exc:
        await crud.append_credential_redemption_audit(
            session,
            purpose="publication_push",
            outcome="refused",
            deployment_id=publication.deployment_id,
            publication_id=publication.id,
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
        purpose="publication_push",
        outcome="issued",
        deployment_id=publication.deployment_id,
        publication_id=publication.id,
        repo_full_name=repo,
        detail=(
            "server-derived repository credential issued via "
            + credential_mode(
                app_id=settings.github_app_id,
                app_private_key=settings.github_app_private_key,
                token=settings.github_token,
            )
        ),
    )
    return RepositoryCredentialOut(
        repo_full_name=repo,
        clone_url=clone_url,
        authorization_header=authorization_header,
    )
