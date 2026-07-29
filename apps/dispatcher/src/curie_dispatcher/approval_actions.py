"""The click-to-resolve flow for approval cards (#246, ADR-0010).

The worker posts a Block Kit approval card whose Approve/Reject buttons carry
these action ids; a click arrives here over the authenticated Socket Mode
websocket and is forwarded to the platform API's resolve endpoint, where the
authorizer decides server-side whether this actor may resolve (channel
membership, self-approval block). The dispatcher never decides authorization
itself -- it relays who clicked and from which channel, and renders the API's
verdict back into Slack:

- the winner's card is edited in place (buttons removed, verdict stamped);
- a non-approver gets the ephemeral "you are not an approver" rejection;
- a loser of the claim race gets the ephemeral "already resolved by X";
- an expired record gets the ephemeral expiry notice.

The action-id constants live here (not in the worker, which renders the card)
because the worker already depends on this package for the queue seam; the
card renderer imports them so the two sides cannot drift.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from slack_sdk.web import WebClient

from .config import DispatcherConfig

logger = logging.getLogger(__name__)

# Block Kit action ids for the approval card's buttons. The button ``value``
# carries the approval record id. process_action's catch-all must skip these
# (Bolt runs every matching listener, so without the skip a click would ALSO
# be normalized into an ordinary turn).
APPROVE_ACTION_ID = "curie-approval-approve"
REJECT_ACTION_ID = "curie-approval-reject"

# The note-collecting variants (#1053). A card rendered from a ``ConfirmIntent``
# with ``allow_free_text`` carries these instead, and a click on one opens a
# dialog for an optional note before resolving. They are a SECOND action-id pair
# rather than a flag inside the button ``value`` so the behavior is visible in
# the interaction payload itself and so the renderer and the handler keep sharing
# one set of literals -- the worker's ``blocks.approval_card`` imports these, the
# same anti-drift arrangement the original pair already uses.
APPROVE_NOTE_ACTION_ID = "curie-approval-approve-note"
REJECT_NOTE_ACTION_ID = "curie-approval-reject-note"

# The dialog's ``callback_id`` and the block/action ids of its one input. The
# approval id, the card's channel and ts, and the decision ride in
# ``private_metadata`` because a view submission carries no channel or message of
# its own.
NOTE_MODAL_CALLBACK_ID = "curie-approval-note"
_NOTE_BLOCK_ID = "note"
_NOTE_ACTION_ID = "note-input"

# The human-facing note limit, declared on the modal's input so Slack shows a
# counter and blocks submit (#1077). Slack would otherwise accept up to 3000, and
# the verdict line concatenates the note onto an attribution prefix, so an
# unbounded note produces a text object over the cap and ``chat_update`` raises.
_NOTE_MAX_LENGTH = 2000

# The structural backstop on the stamped context line, applied in
# ``_verdict_line`` so a non-modal caller is guarded too. It mirrors the value
# the codebase already settled on for the same class of problem: the worker's
# ``_APPROVAL_SUMMARY_MAX`` and ``_CHUNK_TARGET``, both in
# ``curie_worker.blocks``, are the same 2900 (copied rather than imported: the
# dispatcher must not depend on apps/worker). The three share a derivation from
# Slack's text-object limit, not a coupling to each other -- two of them guard
# different block types in a different service, and nothing gates any of the
# three against the others; this comment naming the other two is the only
# cross-reference there is.
#
# Assumption, stated because it is inferred rather than cited: Slack documents
# 3000 as the general maximum for a text object's ``text``, but documents no
# per-element cap for the elements of a CONTEXT block specifically. 2900 is
# therefore a deliberate margin under an inferred limit, not a documented one.
_VERDICT_LINE_MAX = 2900

_APPROVAL_ACTION_IDS = frozenset(
    {
        APPROVE_ACTION_ID,
        REJECT_ACTION_ID,
        APPROVE_NOTE_ACTION_ID,
        REJECT_NOTE_ACTION_ID,
    }
)

# Which decision each action id resolves to, so the two pairs cannot drift on it.
_DECISION_BY_ACTION_ID = {
    APPROVE_ACTION_ID: "approved",
    APPROVE_NOTE_ACTION_ID: "approved",
    REJECT_ACTION_ID: "rejected",
    REJECT_NOTE_ACTION_ID: "rejected",
}


def is_approval_action(action_id: str) -> bool:
    """True when a Block Kit action id belongs to the approval card."""

    return action_id in _APPROVAL_ACTION_IDS


def decision_for_action(action_id: str) -> str | None:
    """The decision an approval action id resolves to, or None if it is not one."""

    return _DECISION_BY_ACTION_ID.get(action_id)


@dataclass(frozen=True)
class ResolveOutcome:
    """The API's verdict on one resolution attempt, normalized for rendering."""

    status_code: int
    detail: str = ""
    resolved_by: str | None = None
    decision: str | None = None


@dataclass(frozen=True)
class NoteSubmission:
    """A note dialog that has already been resolved, carried across the ack.

    Everything the render half needs travels in here so that no Slack call has
    to precede the ack (#1077): ``resolve_note_submission`` fills it in and
    ``render_note_submission`` consumes it, with ``ack()`` in between.
    ``response_action`` is the body Bolt must ack the view with -- None closes
    the dialog, and an ``errors`` response keeps it open with the reason
    attached to the note field.
    """

    approval_id: str
    decision: str
    user: str
    channel: str
    card_ts: str
    note: str | None
    outcome: ResolveOutcome
    response_action: dict[str, Any] | None


# The resolve POST is the only network round trip left inside the
# ``view_submission`` ack budget (#1077), so it must give up well inside Slack's
# three seconds. All four phases are named explicitly because httpx timeouts are
# PER PHASE and the phases are sequential: any phase left at its default silently
# joins the sum. A single-argument ``httpx.Timeout(1.0, connect=0.5)`` reads like
# 1.5s and is actually 3.5s, over the deadline this constant exists to fit inside.
#
# Be precise about what the four numbers bound: each caps INACTIVITY inside its own
# I/O operation, not elapsed wall clock. pool 0.1 + connect 0.4 + write 0.3 + read
# 1.4 = 2.2s is the worst case for a response that arrives in the normal way, which
# leaves real margin inside the three seconds for websocket transit and Bolt's own
# dispatch. It is NOT a guaranteed ceiling. A slowly-streaming response resets the
# read clock on every chunk, so it can run past the ack budget without ever
# tripping a timeout, and httpx cannot express a total-request deadline at all.
# What makes that acceptable is the peer rather than the setting: this talks to the
# platform API, which answers with one small JSON body that lands in a single read,
# so there is no trickle for the read clock to keep forgiving.
#
# The shape of the split is deliberate. Read gets the largest share because it is
# the phase that legitimately waits on the authorizer doing work: a group-bound
# approval whose membership cache has expired makes the API perform a live Slack
# lookup inside this request. Connect is tighter because a platform API that has
# not accepted the socket in 0.4s is down, not busy. Pool is tiny because pool
# exhaustion is near-impossible here (five Bolt listener workers against httpx's
# default pool of 100), so failing fast is the right answer if it ever happens.
#
# Timing out yields ``ResolveOutcome(status_code=0)``. Be clear about what that
# does and does not mean: for anything past the connect phase the request WAS
# delivered, so the outcome is UNKNOWN to the dispatcher and the server may well
# have committed the resolution. What keeps a retry safe is the server-side
# compare-and-set, not the record being untouched -- a retry of a decision that
# did land comes back 409 instead of resolving it a second time.
_RESOLVE_TIMEOUT = httpx.Timeout(connect=0.4, read=1.4, write=0.3, pool=0.1)


class ApprovalResolveClient:
    """Thin client for POST /approvals/{id}/resolve (shared API key auth)."""

    def __init__(
        self, *, api_base_url: str, api_key: str, client: httpx.Client | None = None
    ) -> None:
        self._base = api_base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._client = client or httpx.Client(timeout=_RESOLVE_TIMEOUT)

    def resolve(
        self,
        approval_id: str,
        *,
        decision: str,
        resolved_by: str,
        actor_channel: str,
        note: str | None = None,
    ) -> ResolveOutcome:
        body: dict[str, Any] = {
            "decision": decision,
            "resolved_by": resolved_by,
            "actor_channel": actor_channel,
        }
        # Only send the key when the approver typed something. The field is
        # optional on ``ApprovalResolve`` and an empty string is not the same
        # statement as leaving the note blank: it would persist as a
        # resolution_note the resume turn then interpolates as ``Note: .``.
        if note:
            body["note"] = note
        try:
            response = self._client.post(
                f"{self._base}/approvals/{approval_id}/resolve",
                json=body,
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            logger.warning("approval resolve call failed for %s: %s", approval_id, exc)
            return ResolveOutcome(status_code=0, detail=str(exc))
        detail = ""
        resolved = None
        decided = None
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("detail", ""))
                resolved = body.get("resolved_by")
                decided = body.get("status")
        except ValueError:
            # A non-JSON body (e.g. from an ingress/proxy/WAF intermediary)
            # may carry an internal hostname or request id. Do not surface
            # that raw text to the Slack clicker; fall back to empty so the
            # caller's class-neutral fallback applies instead (#453 LOW-1).
            detail = ""
        return ResolveOutcome(
            status_code=response.status_code,
            detail=detail,
            resolved_by=str(resolved) if resolved else None,
            decision=str(decided) if decided else None,
        )


def _resolved_card_blocks(original: dict[str, Any], verdict: str) -> list[dict[str, Any]]:
    """The clicked card with its buttons replaced by the verdict line.

    Every non-actions block of the original message is kept (the summary stays
    readable in place); the actions block is swapped for a context line naming
    the decision and the resolver, so the card cannot be clicked twice.
    """

    blocks = [
        b for b in original.get("blocks", []) if b.get("type") != "actions"
    ]
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": verdict}],
        }
    )
    return blocks


def _verdict_line(decision: str, user: str, note: str | None) -> str:
    """The context line stamped onto a settled card.

    The note is shown HERE as well as on the requester's thread: the approver
    channel is where the next person looks to understand a decision, and a
    reason that only reached the requester leaves that channel with a bare
    verdict.
    """

    # Build the whole line, then cut only if it does not fit. The cut takes from
    # the tail, which is the note, so the attribution survives in preference to
    # it: who decided is the part of this line that must survive. The cut is
    # marked so a reader can tell the card is showing an excerpt; the durable
    # record still holds the whole note, and the requester's resume turn
    # interpolates that one. The marker is the same single ellipsis character
    # the worker's ``_truncate`` in ``curie_worker.blocks`` uses, so a truncated
    # note ends the same way whichever service stamped the card.
    line = f"{decision.capitalize()} by <@{user}>"
    if note:
        line = f"{line}\nNote: {note}"
    if len(line) <= _VERDICT_LINE_MAX:
        return line
    return line[: _VERDICT_LINE_MAX - 1] + "…"


def build_note_modal(
    *, approval_id: str, channel: str, card_ts: str, decision: str
) -> dict[str, Any]:
    """The dialog that collects an OPTIONAL note before resolving (#1053).

    A view submission carries no channel and no message of its own, so
    everything the submit handler needs to finish the job rides in
    ``private_metadata``. The input block is ``optional``: the note is an
    enrichment, and requiring one would turn a decision into a form.
    """

    verb = "Approve" if decision == "approved" else "Reject"
    label = "Note (optional)" if decision == "approved" else "Reason (optional)"
    return {
        "type": "modal",
        "callback_id": NOTE_MODAL_CALLBACK_ID,
        "private_metadata": json.dumps(
            {
                "approval_id": approval_id,
                "channel": channel,
                "card_ts": card_ts,
                "decision": decision,
            }
        ),
        "title": {"type": "plain_text", "text": f"{verb} request"},
        "submit": {"type": "plain_text", "text": verb},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": _NOTE_BLOCK_ID,
                "optional": True,
                "label": {"type": "plain_text", "text": label},
                "element": {
                    "type": "plain_text_input",
                    "action_id": _NOTE_ACTION_ID,
                    "multiline": True,
                    "max_length": _NOTE_MAX_LENGTH,
                },
            }
        ],
    }


def open_note_dialog(
    *,
    body: dict[str, Any],
    decision: str,
    web_client: WebClient,
    resolver: ApprovalResolveClient,
    logger: logging.Logger | None = None,
) -> ResolveOutcome | None:
    """Open the note dialog for a click, or fall forward and resolve without one.

    A ``trigger_id`` is valid for about three seconds, so ``views.open`` can
    genuinely fail. When it does this resolves anyway, with no note, and says so
    in an ephemeral. The human already expressed the decision by clicking;
    refusing the click because an optional enrichment could not be collected
    would be the worse failure, and it would leave the record pending with no
    feedback.

    Returns None on the normal path (nothing is resolved until the dialog is
    submitted), or the fall-forward resolution's outcome.
    """

    log = logger or logging.getLogger(__name__)

    actions = body.get("actions") or []
    approval_id = str(actions[0].get("value") or "") if actions else ""
    channel = (body.get("channel") or {}).get("id") or ""
    user = (body.get("user") or {}).get("id") or ""
    message = body.get("message") or {}
    card_ts = message.get("ts") or ""
    trigger_id = body.get("trigger_id") or ""
    if not approval_id or not channel or not user or not card_ts:
        log.info("approval action without id/channel/user/message, skipping")
        return None

    try:
        web_client.views_open(
            trigger_id=trigger_id,
            view=build_note_modal(
                approval_id=approval_id,
                channel=channel,
                card_ts=card_ts,
                decision=decision,
            ),
        )
        return None
    except Exception as exc:  # noqa: BLE001 - the decision must still land
        log.warning("note dialog failed to open for %s: %s", approval_id, exc)

    outcome = _resolve_and_render(
        approval_id=approval_id,
        decision=decision,
        user=user,
        channel=channel,
        card_ts=card_ts,
        message=message,
        note=None,
        web_client=web_client,
        resolver=resolver,
        log=log,
    )
    if outcome.status_code == 200:
        _ephemeral(
            web_client,
            channel=channel,
            user=user,
            text=(
                "The note box could not be opened, so this was resolved without a "
                "note. Add one with `curie <tier> approvals <agent> --resolve` if "
                "it matters."
            ),
            log=log,
        )
    return outcome


def resolve_note_submission(
    *,
    body: dict[str, Any],
    resolver: ApprovalResolveClient,
    logger: logging.Logger | None = None,
) -> NoteSubmission | None:
    """Decide a submitted note dialog, making no Slack call at all (#1053, #1077).

    The pre-ack half. It parses ``private_metadata``, extracts the note, and
    resolves; the returned ``NoteSubmission`` carries the body Bolt must ack the
    view with, so the caller can ack immediately and render afterwards. Returns
    None when the payload is unusable, in which case there is nothing to render.

    The resolve genuinely cannot move past the ack: a view ack CARRIES the
    response. That second channel exists because the claim race widened.
    Resolution now happens at submit rather than at click, so two approvers can
    hold open dialogs at once. The compare-and-set still makes exactly one win,
    but the loser is now standing inside a modal, where an ephemeral is
    invisible -- so every refusal is rendered INTO the view rather than posted
    behind it.

    It takes no ``web_client``: that is what makes "nothing talks to Slack before
    the ack" a structural property of the signature rather than a matter of
    ordering discipline inside the body.
    """

    log = logger or logging.getLogger(__name__)

    view = body.get("view") or {}
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except ValueError:
        meta = {}
    approval_id = str(meta.get("approval_id") or "")
    channel = str(meta.get("channel") or "")
    card_ts = str(meta.get("card_ts") or "")
    decision = str(meta.get("decision") or "")
    user = (body.get("user") or {}).get("id") or ""
    if not approval_id or not channel or not user or decision not in ("approved", "rejected"):
        log.info("note submission with unusable private_metadata, skipping")
        return None

    values = (view.get("state") or {}).get("values") or {}
    note = ((values.get(_NOTE_BLOCK_ID) or {}).get(_NOTE_ACTION_ID) or {}).get("value")
    note = (note or "").strip() or None

    outcome = resolver.resolve(
        approval_id,
        decision=decision,
        resolved_by=user,
        actor_channel=channel,
        note=note,
    )
    response_action = (
        None
        if outcome.status_code == 200
        else {
            "response_action": "errors",
            "errors": {_NOTE_BLOCK_ID: _refusal_text(outcome)},
        }
    )
    return NoteSubmission(
        approval_id=approval_id,
        decision=decision,
        user=user,
        channel=channel,
        card_ts=card_ts,
        note=note,
        outcome=outcome,
        response_action=response_action,
    )


def render_note_submission(
    submission: NoteSubmission,
    *,
    web_client: WebClient,
    logger: logging.Logger | None = None,
) -> None:
    """Stamp the card for an already-decided submission (#1077).

    The post-ack half, and the only place the dialog path talks to Slack. It
    never raises: by the time it runs the view has been acked and there is no
    surface left to report an error on.

    Note the deliberate consequence of running here: the card is now read AFTER
    the resolve rather than before, so a claim-race loser commonly reads a card
    the winner has already stamped and appends a second context line under it.
    That is a redundant extra line, not a lost decision.
    """

    log = logger or logging.getLogger(__name__)

    # The outer net that makes "never raises" structural rather than incidental.
    # Every Slack call below already carries its own best-effort handler, so
    # totality holds by inspection today -- but only by inspection, and the cost
    # of it lapsing is not a logged traceback. Bolt's thread runner, on a raise
    # from a post-ack listener, sets ``ack.response`` back to None while the
    # dispatch thread is still polling it, so a raise inside that window eats the
    # ack entirely and the view never closes: the exact failure #1077 exists to
    # remove. A future edit that adds a call here must not be able to
    # reintroduce it by forgetting a handler.
    try:
        # The card's original blocks are not in a view_submission payload, so
        # re-read the message the card lives on to stamp it in place.
        # Best-effort: a failed read only costs the in-place edit, never the
        # resolution. Skip the read entirely on the outcomes that discard it --
        # only the 200 and 409 branches of ``_render_outcome`` use ``message``,
        # and a fetch nobody reads still costs a Slack round trip and holds one
        # of Bolt's five shared listener workers.
        message: dict[str, Any] = {}
        if submission.outcome.status_code in (200, 409):
            message = _fetch_card_message(
                web_client,
                channel=submission.channel,
                card_ts=submission.card_ts,
                log=log,
            )

        _render_outcome(
            approval_id=submission.approval_id,
            decision=submission.decision,
            user=submission.user,
            channel=submission.channel,
            card_ts=submission.card_ts,
            message=message,
            note=submission.note,
            outcome=submission.outcome,
            web_client=web_client,
            log=log,
        )
    except Exception as exc:  # noqa: BLE001 - a raise past the ack eats the ack
        log.warning(
            "post-ack render failed for approval %s: %s", submission.approval_id, exc
        )


def _fetch_card_message(
    web_client: WebClient, *, channel: str, card_ts: str, log: logging.Logger
) -> dict[str, Any]:
    """The approval card's message, or an empty dict when it cannot be read."""

    try:
        history = web_client.conversations_history(
            channel=channel, latest=card_ts, oldest=card_ts, inclusive=True, limit=1
        )
        messages = history.get("messages") or []
        if messages:
            return dict(messages[0])
    except Exception as exc:  # noqa: BLE001 - the stamp is best-effort
        log.warning("could not read approval card %s in %s: %s", card_ts, channel, exc)
    return {}


def _refusal_text(outcome: ResolveOutcome) -> str:
    """The one wording for a non-200 resolution, shared by both surfaces."""

    if outcome.status_code == 403:
        # The API already words each refusal class distinctly (non-membership,
        # self-approval, could-not-verify), so render its reason verbatim rather
        # than guessing the class. An empty detail stays class-neutral (#453 AC5).
        return (
            outcome.detail.strip()
            or "This click was refused and the platform gave no reason."
        )
    if outcome.status_code == 409:
        return (
            f"Already resolved by {outcome.resolved_by}."
            if outcome.resolved_by
            else (outcome.detail or "This request was already resolved.")
        )
    if outcome.status_code == 410:
        return "This approval expired and can no longer be resolved."
    if outcome.status_code == 404:
        return "This approval no longer exists."
    return "Resolving failed; try again shortly."


def _ephemeral(
    web_client: WebClient, *, channel: str, user: str, text: str, log: logging.Logger
) -> None:
    try:
        web_client.chat_postEphemeral(channel=channel, user=user, text=text)
    except Exception as exc:  # noqa: BLE001 - the verdict stands regardless
        log.warning("ephemeral notice failed in %s: %s", channel, exc)


def _resolve_and_render(
    *,
    approval_id: str,
    decision: str,
    user: str,
    channel: str,
    card_ts: str,
    message: dict[str, Any],
    note: str | None,
    web_client: WebClient,
    resolver: ApprovalResolveClient,
    log: logging.Logger,
) -> ResolveOutcome:
    """Resolve one approval and hand the verdict to ``_render_outcome``.

    The resolve-then-render pair for a caller that is free to talk to Slack
    inline: the immediate-click path, and the note-dialog path's fall-forward
    when the dialog could not be opened. The rendering itself lives in
    ``_render_outcome``, which the post-ack dialog path calls on its own, so
    this is only the ordering of the two halves plus the returned outcome.
    """

    outcome = resolver.resolve(
        approval_id,
        decision=decision,
        resolved_by=user,
        actor_channel=channel,
        note=note,
    )
    _render_outcome(
        approval_id=approval_id,
        decision=decision,
        user=user,
        channel=channel,
        card_ts=card_ts,
        message=message,
        note=note,
        outcome=outcome,
        web_client=web_client,
        log=log,
    )
    return outcome


def _render_outcome(
    *,
    approval_id: str,
    decision: str,
    user: str,
    channel: str,
    card_ts: str,
    message: dict[str, Any],
    note: str | None,
    outcome: ResolveOutcome,
    web_client: WebClient,
    log: logging.Logger,
) -> None:
    """Render an already-decided outcome back into Slack.

    Split out of ``_resolve_and_render`` so the note-dialog path can run it
    AFTER ``ack()`` (#1077): every call in here talks to Slack, and none of it
    feeds the ack body. It must never raise -- past the ack a raise does not just
    go unreported, it can eat the ack: Bolt's thread runner sets ``ack.response``
    back to None on a listener exception while the dispatch thread is still
    polling it, so the view is left open with no response at all. Every Slack
    call therefore keeps its own best-effort handler, and the caller wraps the
    whole of this in an outer net.
    """

    if outcome.status_code == 200:
        verdict = _verdict_line(outcome.decision or decision, user, note)
        # Best-effort: the record is already resolved and the resume turn is
        # enqueued; a failed card edit must not undo either.
        try:
            web_client.chat_update(
                channel=channel,
                ts=card_ts,
                text=verdict,
                blocks=_resolved_card_blocks(message, verdict),
            )
        except Exception as exc:  # noqa: BLE001 - render is best-effort
            log.warning("approval card update failed for %s: %s", approval_id, exc)
        log.info("approval %s %s by %s", approval_id, decision, user)
        return

    if outcome.status_code == 409:
        # Refresh a stale card so it stops offering buttons for a settled
        # record (the winner's edit normally did this; a race can leave it).
        _refresh_settled_card(
            web_client,
            channel=channel,
            card_ts=card_ts,
            message=message,
            detail=_refusal_text(outcome),
            log=log,
        )
    log.info(
        "approval %s click by %s rejected: HTTP %s %s",
        approval_id,
        user,
        outcome.status_code,
        outcome.detail,
    )


def process_approval_action(
    *,
    body: dict[str, Any],
    decision: str,
    web_client: WebClient,
    resolver: ApprovalResolveClient,
    logger: logging.Logger | None = None,
) -> ResolveOutcome | None:
    """Resolve one card click immediately and render the verdict back into Slack.

    The no-dialog path, for a card rendered without ``allow_free_text``. It shares
    ``_render_outcome`` with the dialog path so the two cannot disagree about what
    a settled card looks like; only the surface a refusal is rendered on
    differs, and that difference is real: here nothing is open, so an ephemeral is
    the right place for it.

    Returns the API outcome (for tests/logging), or None when the interaction
    payload is not a resolvable card click (missing value/channel/message).
    """

    log = logger or logging.getLogger(__name__)

    actions = body.get("actions") or []
    approval_id = str(actions[0].get("value") or "") if actions else ""
    channel = (body.get("channel") or {}).get("id") or ""
    user = (body.get("user") or {}).get("id") or ""
    message = body.get("message") or {}
    card_ts = message.get("ts") or ""
    if not approval_id or not channel or not user or not card_ts:
        log.info("approval action without id/channel/user/message, skipping")
        return None

    outcome = _resolve_and_render(
        approval_id=approval_id,
        decision=decision,
        user=user,
        channel=channel,
        card_ts=card_ts,
        message=message,
        note=None,
        web_client=web_client,
        resolver=resolver,
        log=log,
    )
    if outcome.status_code != 200:
        _ephemeral(
            web_client,
            channel=channel,
            user=user,
            text=_refusal_text(outcome),
            log=log,
        )
    return outcome


def _refresh_settled_card(
    web_client: WebClient,
    *,
    channel: str,
    card_ts: str,
    message: dict[str, Any],
    detail: str,
    log: logging.Logger,
) -> None:
    try:
        web_client.chat_update(
            channel=channel,
            ts=card_ts,
            text=detail,
            blocks=_resolved_card_blocks(message, detail),
        )
    except Exception as exc:  # noqa: BLE001 - best-effort refresh
        log.debug("settled-card refresh skipped: %s", exc)


def build_resolver(config: DispatcherConfig) -> ApprovalResolveClient:
    """The production resolver, from the dispatcher's API settings."""

    return ApprovalResolveClient(
        api_base_url=config.api_base_url, api_key=config.api_key
    )
