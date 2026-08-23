"""The worker's approval-record client (#244, ADR-0010).

When a run ends ``awaiting-approval`` the kernel persists a durable ``Approval``
record before suspending the session, so the pending human decision survives
every component restarting. The record lives server-side with the API (the
authorizer of #246 is enforced there, where it cannot be spoofed from inside a
sandbox); this module is the thin write client, mirroring the eval lane's
``EvalReporter`` (same base URL + shared API key).

Creation is idempotent: ``dedupe_key`` carries the triggering event id, so a
reclaimed/redelivered turn that re-requests the same approval adopts the
existing record (the API answers 200 instead of 201) rather than forking a
second pending record for one human decision.
"""

from __future__ import annotations

import base64
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from aci_protocol import ApprovalRequest

# Re-exported so this module stays the kernel-facing seam for the approval
# payload: ``ApprovalRequest`` is now the shared wire model (#492), not a
# lane-local mirror of the API's schema.
logger = logging.getLogger(__name__)

__all__ = [
    "ApprovalBackendError",
    "ApprovalClient",
    "ApprovalCreator",
    "ApprovalReader",
    "ApprovalRequest",
    "CreatedApproval",
    "SettledApproval",
    "CreatedPublication",
    "PublicationCreateRequest",
    "PublicationCreator",
]


@dataclass(frozen=True)
class CreatedApproval:
    """What the kernel needs back: the record's identity and its status."""

    id: str
    status: str


@dataclass(frozen=True)
class PublicationCreateRequest:
    """API-local atomic Approval+Publication write request."""

    deployment_id: uuid.UUID
    conversation_id: str
    author: str
    summary: str
    reply_kind: str
    reply_channel: str
    reply_placeholder: str | None
    reply_endpoint: str | None
    reply_adapter: str | None
    dedupe_key: str
    base_sha: str
    patch: bytes
    changed_paths: tuple[str, ...]
    expires_in_seconds: int

    def to_json(self) -> dict[str, Any]:
        if len(self.patch) > 900_000:
            raise ApprovalBackendError("publication patch exceeds 900000 raw bytes")
        return {
            "deployment_id": str(self.deployment_id),
            "conversation_id": self.conversation_id,
            "author": self.author,
            "summary": self.summary,
            "reply_kind": self.reply_kind,
            "reply_channel": self.reply_channel,
            "reply_placeholder": self.reply_placeholder,
            "reply_endpoint": self.reply_endpoint,
            "reply_adapter": self.reply_adapter,
            "dedupe_key": self.dedupe_key,
            "base_sha": self.base_sha,
            "patch_b64": base64.b64encode(self.patch).decode("ascii"),
            "changed_paths": list(self.changed_paths),
            "expires_in_seconds": self.expires_in_seconds,
        }


@dataclass(frozen=True)
class CreatedPublication:
    id: str
    approval_id: str
    status: str


class ApprovalBackendError(Exception):
    """The approval record could not be created; the kernel escalates rather
    than suspending a session no resolution could ever wake."""


@dataclass(frozen=True)
class SettledApproval:
    """A resolved record's outcome, for stamping its card (#1084).

    Read from the record rather than parsed out of the platform-authored resume
    turn. That turn does carry all three facts in prose, and the kernel already
    keys off its ``[approval expired]`` marker, but a marker is a stable literal
    while "was approved by X. Note: Y." is a sentence -- reconstructing a
    decision by regex over it would make the card's correctness depend on the
    wording of a string built for a language model to read.
    """

    status: str
    resolved_by: str | None
    resolution_note: str | None


class ApprovalCreator(Protocol):
    """The kernel-facing seam; tests supply a recording fake."""

    async def create(self, request: ApprovalRequest) -> CreatedApproval: ...


class PublicationCreator(Protocol):
    """Atomic trusted write seam used only for exact publish provenance."""

    async def create_publication(self, request: PublicationCreateRequest) -> CreatedPublication: ...


class ApprovalReader(Protocol):
    """Read one settled record back. Separate from ``ApprovalCreator`` because
    the kernel's pause path needs only the create half, and a fake for it should
    not have to grow a method it never calls."""

    async def get(self, approval_id: str) -> SettledApproval | None: ...


class ApprovalClient:
    """HTTP implementation against the platform API's /approvals endpoint."""

    def __init__(
        self,
        *,
        api_base_url: str,
        api_key: str,
        client: httpx.AsyncClient,
        worker_token: str = "",
    ) -> None:
        self._url = f"{api_base_url.rstrip('/')}/approvals"
        self._publication_url = f"{api_base_url.rstrip('/')}/v1/internal/publications"
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._worker_headers = {"X-Curie-Worker-Token": worker_token} if worker_token else {}
        self._client = client

    async def create(self, request: ApprovalRequest) -> CreatedApproval:
        try:
            response = await self._client.post(
                self._url,
                content=request.model_dump_json(),
                headers={**self._headers, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ApprovalBackendError(f"approval create failed: {exc}") from exc
        # 201 is a fresh record; 200 is the idempotent dedupe_key replay.
        if response.status_code not in (200, 201):
            raise ApprovalBackendError(
                f"approval create failed: HTTP {response.status_code}: {response.text}"
            )
        body = response.json()
        return CreatedApproval(id=str(body["id"]), status=str(body["status"]))

    async def get(self, approval_id: str) -> SettledApproval | None:
        """The record's settled outcome, or None when it cannot be read (#1084).

        Never raises. Its only caller is best-effort card teardown on a resume
        turn: the resolution already happened and the session is already waking,
        so a failed read costs a stamped card and nothing else. Raising here
        would turn a cosmetic gap into a dead-lettered resume.
        """

        try:
            response = await self._client.get(f"{self._url}/{approval_id}", headers=self._headers)
        except httpx.HTTPError as exc:
            logger.warning("approval read failed for %s: %s", approval_id, exc)
            return None
        if response.status_code != 200:
            logger.warning(
                "approval read failed for %s: HTTP %s", approval_id, response.status_code
            )
            return None
        try:
            body = response.json()
            return SettledApproval(
                status=str(body["status"]),
                resolved_by=body.get("resolved_by"),
                resolution_note=body.get("resolution_note"),
            )
        except (ValueError, KeyError) as exc:
            logger.warning("approval read returned an unusable body for %s: %s", approval_id, exc)
            return None

    async def create_publication(self, request: PublicationCreateRequest) -> CreatedPublication:
        """Atomically persist the approval and its private patch.

        The ordinary platform API key is intentionally not accepted on this
        route.  If the dedicated worker credential is absent, fail before any
        request so a local/non-cluster install cannot create a stranded card.
        """

        if not self._worker_headers:
            raise ApprovalBackendError(
                "repository publication is cluster-only and requires internal worker auth"
            )
        try:
            response = await self._client.post(
                self._publication_url,
                json=request.to_json(),
                headers={**self._worker_headers, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ApprovalBackendError(f"publication create failed: {exc}") from exc
        if response.status_code not in (200, 201):
            raise ApprovalBackendError(
                f"publication create failed: HTTP {response.status_code}: {response.text}"
            )
        try:
            body = response.json()
            return CreatedPublication(
                id=str(body["id"]),
                approval_id=str(body["approval_id"]),
                status=str(body["status"]),
            )
        except (ValueError, KeyError) as exc:
            raise ApprovalBackendError("publication create returned an unusable body") from exc
