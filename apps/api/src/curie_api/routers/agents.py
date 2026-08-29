"""Agents and their versions."""

import functools
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from plugin_format.connector_render import AmbiguousObjectName
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from .. import bundles, crud
from ..auth import require_api_key
from ..config import get_settings
from ..deps import SessionDep, StoreDep
from ..schemas import (
    AgentCreate,
    AgentOut,
    AgentUpdate,
    BundleFile,
    BundleFiles,
    ConnectorManifests,
    VersionCreate,
    VersionOut,
    enforce_behavior_packs_size,
)

router = APIRouter(prefix="/agents", tags=["agents"], dependencies=[Depends(require_api_key)])

# Postgres SQLSTATE for a unique_violation. asyncpg exposes it (and the
# violated constraint's name) as plain attributes on the wrapped driver
# exception -- there is no psycopg-style `.diag` namespace.
_UNIQUE_VIOLATION = "23505"

# The real unique constraints an agent write can violate -- on `agents` and on
# its `agent_channels` binding (from the alembic migrations) -- mapped to the
# human message for each. Any other unique violation falls back to a generic
# message.
_UNIQUE_CONSTRAINT_MESSAGES = {
    "agents_name_key": "an agent with that name already exists",
    # ix_agents_repo_full_name stopped being unique in 0018 (ADR-0091): one
    # repository builds many agents now. The mapping is kept because a
    # pre-0018 database still has the unique index, and an operator hitting it
    # deserves the actionable message rather than the generic fallback. It
    # names the fix, since the constraint is no longer intended behaviour.
    "ix_agents_repo_full_name": (
        "an agent for that repository already exists. One repository may build "
        "several agents (ADR-0091) -- run `alembic upgrade head` to apply "
        "migration 0018, which drops this constraint"
    ),
    # #38: one agent per channel ROUTE, carried onto `agent_channels` by
    # migration 0021 and widened from the address alone to the `(kind, address)`
    # pair by 0023, once the worker's resolver started routing on the pair too
    # (ADR-0096 phase 2). Without this the create succeeded and the second agent
    # was silently shadowed by the resolver at runtime. Stated without the word
    # "Slack" since ADR-0096: the invariant, and the shadowing it prevents,
    # belong to every channel kind.
    "agent_channels_kind_address_key": (
        "another agent is already bound to that channel kind and address; one "
        "agent per route (move or delete the other agent, or pick another "
        "address)"
    ),
    # The reverse direction, which the old scalar column got for free and a
    # child table would silently discard (ADR-0089: "one agent still binds one
    # channel"). A different operator mistake from the one above, so a different
    # message: sharing one message would misdiagnose both.
    "agent_channels_agent_id_key": (
        "that agent already has a channel binding; one agent binds one channel "
        "(ADR-0089), so PATCH the agent's channel to move it rather than adding "
        "a second binding"
    ),
}


def _driver_diag(exc: IntegrityError, attr: str) -> str | None:
    """Read an asyncpg diagnostic field, walking the `__cause__` chain.

    asyncpg surfaces `sqlstate` on SQLAlchemy's DBAPI wrapper (`exc.orig`) but
    exposes `constraint_name` only on the underlying `asyncpg` error one link
    down the `__cause__` chain. Walk both so either shape resolves; guard
    against a cyclic chain.
    """
    obj = getattr(exc, "orig", None)
    seen: set[int] = set()
    while obj is not None and id(obj) not in seen:
        seen.add(id(obj))
        value = getattr(obj, attr, None)
        if value is not None:
            return str(value)
        obj = getattr(obj, "__cause__", None)
    return None


def classify_integrity_error(exc: IntegrityError) -> tuple[int, str] | None:
    """Map a real unique-constraint violation to a `(409, message)` conflict.

    Only a genuine unique_violation (SQLSTATE 23505) is a caller conflict; a
    NOT NULL or FK violation is a server fault and must surface as a 500, so
    this returns `None` for those (the caller re-raises). The human message is
    chosen by the violated constraint's name from asyncpg's structured fields,
    not by substring-matching the stringified driver error.
    """
    if _driver_diag(exc, "sqlstate") != _UNIQUE_VIOLATION:
        return None
    constraint_name = _driver_diag(exc, "constraint_name")
    message = "agent violates a uniqueness constraint"
    if constraint_name is not None:
        message = _UNIQUE_CONSTRAINT_MESSAGES.get(constraint_name, message)
    return status.HTTP_409_CONFLICT, message


@router.post("", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(data: AgentCreate, session: SessionDep) -> AgentOut:
    # Reject oversized behavior packs (#936) before we touch the DB.
    if data.behavior_packs is not None:
        enforce_behavior_packs_size(data.behavior_packs)
    # name and repo_full_name are unique. A collision is a caller conflict (409),
    # not a server fault: catch the DB IntegrityError and map it, rather than
    # letting it bubble as an opaque 500. A non-unique violation (NOT NULL, FK)
    # is a genuine server fault -- re-raise it so it surfaces as a 500.
    try:
        agent = await crud.create_agent(session, data)
    except IntegrityError as exc:
        await session.rollback()
        classified = classify_integrity_error(exc)
        if classified is None:
            raise
        status_code, message = classified
        raise HTTPException(status_code, message) from exc
    return AgentOut.model_validate(agent)


@router.get("", response_model=list[AgentOut])
async def list_agents(session: SessionDep) -> list[AgentOut]:
    agents = await crud.list_agents(session)
    return [AgentOut.model_validate(a) for a in agents]


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: uuid.UUID, session: SessionDep) -> AgentOut:
    agent = await crud.get_agent(session, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    return AgentOut.model_validate(agent)


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(agent_id: uuid.UUID, data: AgentUpdate, session: SessionDep) -> AgentOut:
    # Lets a redeploy move an existing agent's channel (the CLI only sends this
    # when a channel was passed explicitly). An omitted field is a no-op so the
    # agent's current binding is preserved; an explicit null is refused by the
    # schema rather than clearing it (there is no default binding to fall back
    # to), so `is not None` here means exactly "the client sent a binding".
    agent = await crud.get_agent(session, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    if data.channel is not None:
        # The binding's address is unique (one agent per address, #38), so this is
        # the one PATCHable field that can collide with another agent. Classify it
        # the same way create does -- a caller conflict is a 409, not an opaque 500
        # -- so the constraint cannot be sidestepped by creating on a free address
        # and then moving onto a taken one. (Before #38 no PATCHable field was
        # unique, which is why this path had no IntegrityError handler.)
        try:
            agent = await crud.update_agent_binding(session, agent, data.channel)
        except IntegrityError as exc:
            await session.rollback()
            classified = classify_integrity_error(exc)
            if classified is None:
                raise
            status_code, message = classified
            raise HTTPException(status_code, message) from exc
    # Presence, not truthiness (#1310). `is not None` conflates "the client did
    # not mention this field" with "the client explicitly sent null", so setting
    # either override used to be a one-way door: nothing could put it back to the
    # platform default. `model_fields_set` carries exactly the keys the request
    # actually contained, which is the distinction the API's own semantics rest
    # on. Both nullable overrides get it -- they are the same seam, and fixing
    # one beside the other would leave the sibling broken on an adjacent line.
    sent = data.model_fields_set
    if "model" in sent:
        agent = await crud.update_agent_model(session, agent, data.model)
    if "thinking" in sent:
        agent = await crud.update_agent_thinking(session, agent, data.thinking)
    if data.approval_required_tools is not None:
        # Omitted leaves the gates unchanged; an explicit [] clears them (#245).
        agent = await crud.update_agent_approval_tools(session, agent, data.approval_required_tools)
    if data.approval_routes is not None:
        # Omitted leaves the bindings unchanged; an explicit {} clears them (#247).
        agent = await crud.update_agent_approval_routes(
            session,
            agent,
            {name: b.model_dump() for name, b in data.approval_routes.items()},
        )
    if data.repo_full_name is not None:
        # Binds this agent to a repository so git-flow can route pushes to it
        # (ADR-0091). Several agents may share one, so this cannot collide.
        agent = await crud.update_agent_repo(session, agent, data.repo_full_name)
    if data.secrets is not None:
        # Omitted leaves the secrets unchanged; an explicit {} clears them (#429).
        agent = await crud.update_agent_secrets(session, agent, data.secrets)
    return AgentOut.model_validate(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: uuid.UUID, session: SessionDep) -> None:
    # Deleting an agent cascades its versions and deployments rows (bundle
    # objects in RustFS are left as-is, out of scope). Refuse while a deployment
    # is still active so a live agent cannot be pulled out from under Slack
    # traffic; the caller must stop it (kill/undeploy) first.
    agent = await crud.get_agent(session, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    if await crud.agent_has_active_deployment(session, agent_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "agent has an active deployment; stop it before deleting",
        )
    await crud.delete_agent(session, agent_id)


@router.post(
    "/{agent_id}/versions",
    response_model=VersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    agent_id: uuid.UUID, data: VersionCreate, session: SessionDep
) -> VersionOut:
    if await crud.get_agent(session, agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    version = await crud.create_version(session, agent_id, data)
    return VersionOut.model_validate(version)


@router.get("/{agent_id}/versions", response_model=list[VersionOut])
async def list_versions(agent_id: uuid.UUID, session: SessionDep) -> list[VersionOut]:
    if await crud.get_agent(session, agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    versions = await crud.list_versions(session, agent_id)
    return [VersionOut.model_validate(v) for v in versions]


@router.get("/{agent_id}/versions/{version_id}/connectors", response_model=ConnectorManifests)
async def read_version_connectors(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    session: SessionDep,
    store: StoreDep,
    release: str,
    namespace: str,
    app_name: str,
) -> ConnectorManifests:
    """Render this version's declared connectors into Kubernetes objects.

    Read-only and side-effect free: the API computes the manifests and returns
    them; the CALLER applies them with its own cluster credentials. That split
    is deliberate -- rendering is a pure function, so the API needs no cluster
    access for it, and this service (which receives internet webhooks) keeps the
    read-only `pods: list` + `pods/log: get` RBAC it has today (ADR-0086).

    `release`, `namespace`, and `app_name` are supplied by the caller because
    they are install-time facts the API does not know: the Helm release name and
    nameOverride live with whoever ran `cluster up`, not in the bundle.
    """

    version = await crud.get_version(session, version_id)
    if version is None or version.agent_id != agent_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found")
    if version.bundle_ref is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no bundle stored for this version")
    # Object names are scoped to the agent, not just the release (#1116). Curie
    # runs many agents per release, so a release-scoped name lets two agents
    # that each declare `grafana` overwrite one another's Deployment, Service,
    # and credential with no error. The agent NAME (not the id) is used so the
    # objects stay recognisable in `kubectl get`.
    agent = await crud.get_agent(session, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    data = await store.get(version.bundle_ref)
    settings = get_settings()
    agent_name = agent.name

    def _render() -> ConnectorManifests:
        with tempfile.TemporaryDirectory() as tmp:
            bundles.extract_and_validate(
                data,
                Path(tmp),
                max_uncompressed_bytes=settings.bundle_max_uncompressed_bytes,
                max_compression_ratio=settings.bundle_max_compression_ratio,
                max_members=settings.bundle_max_members,
            )
            declared = bundles.read_connectors(Path(tmp))
            # Per-agent too: a release-scoped Secret means deploying the prod
            # agent overwrites the dev agent's token in place (#1116).
            secret_name = f"{release}-{agent_name}-connector-secrets"
            return ConnectorManifests(
                manifests=bundles.render_connector_manifests(
                    declared,
                    release=release,
                    agent=agent_name,
                    namespace=namespace,
                    app_name=app_name,
                    secret_name=secret_name,
                ),
                mcp_entries=bundles.connector_mcp_entries(
                    declared, release=release, agent=agent_name, namespace=namespace
                ),
                # Which keys the CALLER must resolve a value for. Stated rather
                # than inferable: a referenced Secret's key renders an identical
                # secretKeyRef, and resolving it would defeat the point (#1163).
                owned_secret_name=secret_name,
                owned_secret_keys=bundles.owned_secret_keys(declared),
            )

    # `object_name` fails closed on an agent name that forges its `-mcp-` join
    # (#1446), and that raise surfaces HERE: every derivation the renderer
    # touches goes through it. The create path refuses such a name now, so the
    # only rows still holding one predate that validator -- which is exactly the
    # case this read-only endpoint has to survive.
    #
    # 422 rather than letting it fall through as a 500. The request is
    # well-formed -- real ids, a stored bundle, valid install-time params -- and
    # nothing about it can be corrected; what cannot be satisfied is the STORED
    # agent name. A 500 tells an operator running `cluster deploy` only that the
    # server broke, with no name in the body and nothing to act on, and the name
    # is not necessarily one they are holding: the bundle's deploy.yaml chose
    # which agent this renders for.
    #
    # "Recreate", not "rename": `AgentUpdate` carries no `name` field, so there
    # is no in-place rename to point them at. Saying "rename" would send them
    # looking for an API that does not exist.
    try:
        return await run_in_threadpool(_render)
    except AmbiguousObjectName as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"the stored name of agent {agent_name!r} cannot produce an "
            f"unambiguous connector object name ({exc}). Connector objects are named "
            "'{release}-{agent}-mcp-{connector}', so this name makes two "
            "different agents render the same Kubernetes objects and share one "
            "connector's credential (#1446). Recreate the agent under a name "
            "that neither contains '-mcp-' nor ends in '-mcp'; an agent's name "
            "cannot be changed in place.",
        ) from exc


@router.get("/{agent_id}/versions/{version_id}/files", response_model=BundleFiles)
async def read_version_files(
    agent_id: uuid.UUID,
    version_id: uuid.UUID,
    session: SessionDep,
    store: StoreDep,
) -> BundleFiles:
    # The UI reads a version's authored text (skills, manifest, eval cases) to
    # render the bundle without pulling the raw archive. 404 covers a missing
    # agent, a version that is not this agent's, and a version with no bundle
    # stored yet -- there is nothing to read in any of those cases.
    version = await crud.get_version(session, version_id)
    if version is None or version.agent_id != agent_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "version not found")
    if version.bundle_ref is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no bundle stored for this version")
    data = await store.get(version.bundle_ref)
    settings = get_settings()
    read = functools.partial(
        bundles.read_bundle_text_files,
        max_uncompressed_bytes=settings.bundle_max_uncompressed_bytes,
        max_compression_ratio=settings.bundle_max_compression_ratio,
        max_members=settings.bundle_max_members,
    )
    files = await run_in_threadpool(read, data)
    return BundleFiles(files=[BundleFile(path=p, content=c) for p, c in files])
