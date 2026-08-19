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

import logging
from dataclasses import dataclass
from typing import Protocol

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
]


@dataclass(frozen=True)
class CreatedApproval:
    """What the kernel needs back: the record's identity and its status."""

    id: str
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
        read_timeout_s: float,
    ) -> None:
        self._url = f"{api_base_url.rstrip('/')}/approvals"
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._client = client
        self._read_timeout_s = read_timeout_s

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
            response = await self._client.get(
                f"{self._url}/{approval_id}",
                headers=self._headers,
                timeout=self._read_timeout_s,
            )
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
