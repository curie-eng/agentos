"""Recording what a turn did to the world (ADR-0117).

The worker has no database of its own -- it persists an approval by POSTing to
the platform API, and it records an action the same way. So the two ACI frames of
one side-effecting call become two calls here: ``record`` when the call was made,
``complete`` when its result came back.

The connector's reply is the only declaration of reversibility (decision 1), and
this module is where that convention is read: ``prior`` and ``target`` out of the
tool's structured reply. Nothing else can supply them -- no function of
``scale_deployment(replicas=10)``'s arguments produces the replica count from
before the call, which is why snapshot-restore is not the safer mechanism but the
only one that knows the answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from aci_protocol import SideEffectFlag

logger = logging.getLogger(__name__)

# The keys a reporting connector answers with. Named here rather than inline
# because this pair IS the contract a connector author writes to, and a silent
# rename would turn every reversible tool in the fleet irreversible with nothing
# failing.
PRIOR_KEY = "prior"
TARGET_KEY = "target"


class ActionBackendError(RuntimeError):
    """The ledger could not be written."""


@dataclass(frozen=True)
class RecordedAction:
    id: str
    status: str


class ActionRecorder(Protocol):
    """The kernel's whole view of the ledger: open a record, then close it."""

    async def record(
        self,
        frame: SideEffectFlag,
        *,
        event_id: str,
        conversation_id: str,
        agent_id: str | None,
    ) -> RecordedAction: ...

    async def complete(self, action_id: str, frame: SideEffectFlag) -> None: ...


def _snapshot(frame: SideEffectFlag) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The prior state and target a restore replays, out of the tool's reply.

    A connector that answered in prose carries no structured result and lands
    here as ``(None, None)``, which downstream means not undoable. So does one
    that returned JSON without reporting what it overwrote. Neither is an error
    and neither is inferred: an inferred prior state is a guess a restore would
    act on.
    """

    result = frame.result
    if not isinstance(result, dict):
        return None, None
    prior = result.get(PRIOR_KEY)
    target = result.get(TARGET_KEY)
    return (
        prior if isinstance(prior, dict) else None,
        target if isinstance(target, dict) else None,
    )


class ActionClient:
    """HTTP implementation against the platform API's /actions endpoint."""

    def __init__(
        self,
        *,
        api_base_url: str,
        api_key: str,
        client: httpx.AsyncClient,
    ) -> None:
        self._url = f"{api_base_url.rstrip('/')}/actions"
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._client = client

    async def record(
        self,
        frame: SideEffectFlag,
        *,
        event_id: str,
        conversation_id: str,
        agent_id: str | None,
    ) -> RecordedAction:
        """Open the record for a call that was just made.

        ``dedupe_key`` is the event id AND the call id. The event id alone would
        collapse a turn that called the same tool twice into one record; the call
        id alone would not survive a redelivery of that turn.
        """

        body = {
            "agent_id": agent_id,
            "conversation_id": conversation_id,
            "call_id": frame.call_id,
            "tool": frame.tool or "unknown",
            "arguments": frame.arguments,
            "detail": frame.detail,
            "dedupe_key": f"{event_id}:{frame.call_id}",
        }
        payload = await self._post(self._url, body, "action record")
        return RecordedAction(id=str(payload["id"]), status=str(payload["status"]))

    async def complete(self, action_id: str, frame: SideEffectFlag) -> None:
        """Close the record with what came back."""

        prior, target = _snapshot(frame)
        await self._post(
            f"{self._url}/{action_id}/complete",
            {
                "failed": bool(frame.failed),
                "result": frame.result,
                "prior_state": prior,
                "target": target,
                "detail": frame.detail,
            },
            "action complete",
        )

    async def _post(self, url: str, body: dict[str, Any], what: str) -> dict[str, Any]:
        try:
            response = await self._client.post(url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ActionBackendError(f"{what} failed: {exc}") from exc
        # 201 is a fresh record; 200 is the idempotent replay of either call.
        if response.status_code not in (200, 201):
            raise ActionBackendError(
                f"{what} failed: HTTP {response.status_code}: {response.text}"
            )
        payload: dict[str, Any] = response.json()
        return payload
