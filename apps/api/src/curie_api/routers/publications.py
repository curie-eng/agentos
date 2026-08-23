"""Durable approval-gated publication control plane.

The API stores private patch state and resolves credentials. Kubernetes and
GitHub side effects belong to the trusted worker publication reconciler.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status

from .. import crud
from ..auth import (
    require_api_key,
    require_internal_worker_token,
    verify_internal_worker_token,
)
from ..config import get_settings
from ..deps import SessionDep
from ..repository_credentials import resolve_repository_credential
from ..schemas import PublicationCreate, PublicationOut, RepositoryCredentialOut

router = APIRouter(
    prefix="/publications",
    tags=["publications"],
    dependencies=[Depends(require_api_key)],
)
internal_router = APIRouter(
    prefix="/v1/internal/publications", tags=["internal-publications"]
)

PATCH_LIMIT_BYTES = 900_000


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
    if len(patch) > PATCH_LIMIT_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"publication patch exceeds the {PATCH_LIMIT_BYTES}-byte limit",
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


async def _require_publication_credential_worker(
    publication_id: uuid.UUID,
    session: SessionDep,
    x_curie_worker_token: Annotated[
        str | None, Header(alias="X-Curie-Worker-Token")
    ] = None,
) -> None:
    if verify_internal_worker_token(x_curie_worker_token):
        return
    publication = await crud.get_publication(session, publication_id)
    repo = publication.repo_full_name if publication is not None else None
    await crud.append_credential_redemption_audit(
        session,
        purpose="publication_push",
        outcome="refused",
        deployment_id=publication.deployment_id if publication is not None else None,
        publication_id=publication.id if publication is not None else None,
        repo_full_name=repo,
        detail="missing or invalid internal worker token",
    )
    await require_internal_worker_token(x_curie_worker_token)


@internal_router.post(
    "/{publication_id}/credential",
    response_model=RepositoryCredentialOut,
    dependencies=[Depends(_require_publication_credential_worker)],
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
    try:
        clone_url, authorization_header = resolve_repository_credential(
            repo, get_settings()
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
        detail="server-derived repository credential issued",
    )
    return RepositoryCredentialOut(
        repo_full_name=repo,
        clone_url=clone_url,
        authorization_header=authorization_header,
    )
