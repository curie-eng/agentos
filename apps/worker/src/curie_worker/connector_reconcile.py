"""Decide what a connector reconcile should change (ADR-0090).

The pure half. Given what an agent's in-force version DECLARES and what the
cluster currently HAS, this computes a plan: which objects to apply, which to
delete, and which are already correct. It touches no cluster and no database,
so the decision that can delete a running connector is testable without either.

That split is the same one ADR-0087 drew between rendering and applying, for
the same reason: the dangerous step is the decision, not the API call. A prune
that selects one object too many takes down a live agent's tools, and a plan is
something you can assert against exhaustively where a `kubectl delete` is not.

Two properties this must never violate, both learned the hard way on the CLI
path (#1063):

- **Never touch an object Curie did not create.** Ownership is the
  ``curie.dev/connector-owner`` label. sre-bot ran a hand-written connector
  beside a Curie-managed one through its whole migration; an unlabelled object
  is someone else's and is invisible here.
- **Never prune across agents.** Ownership is scoped to the agent name, not to
  "is a connector". Two agents in one release each declare `grafana`, and one
  removing it must not delete the other's (#1116).
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# Set by whoever created the object, naming the agent that declared it. The CLI
# applier stamps the same label, so an object created by one path and
# reconciled by the other is not a special case (ADR-0090).
OWNER_LABEL = "curie.dev/connector-owner"

# A digest of what Curie declared, stamped on every object it applies. Lets a
# later pass ask "is this still what we declared?" without having to
# reconstruct which fields the API server defaulted.
HASH_ANNOTATION = "curie.dev/connector-hash"


def _hash_of(obj: dict[str, Any]) -> str:
    """Digest of a rendered object, excluding the annotation carrying it."""

    stripped = dict(obj)
    metadata = {k: v for k, v in (stripped.get("metadata") or {}).items()}
    annotations = {k: v for k, v in (metadata.get("annotations") or {}).items()}
    annotations.pop(HASH_ANNOTATION, None)
    if annotations:
        metadata["annotations"] = annotations
    else:
        metadata.pop("annotations", None)
    stripped["metadata"] = metadata
    canonical = json.dumps(stripped, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _live_hash(obj: dict[str, Any]) -> str | None:
    annotations = obj.get("metadata", {}).get("annotations") or {}
    value = annotations.get(HASH_ANNOTATION)
    return value if isinstance(value, str) else None


def stamp_hash(obj: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``obj`` carrying its own digest, ready to apply."""

    stamped = dict(obj)
    metadata = dict(stamped.get("metadata") or {})
    metadata["annotations"] = {
        **(metadata.get("annotations") or {}),
        HASH_ANNOTATION: _hash_of(obj),
    }
    stamped["metadata"] = metadata
    return stamped


def owner_of(obj: dict[str, Any]) -> str | None:
    """The agent that declared this object, or None if Curie did not create it."""

    labels = obj.get("metadata", {}).get("labels") or {}
    value = labels.get(OWNER_LABEL)
    return value if isinstance(value, str) and value else None


def identity(obj: dict[str, Any]) -> tuple[str, str]:
    """``(kind, name)`` -- what makes two objects the same object."""

    return (str(obj.get("kind", "")), str(obj.get("metadata", {}).get("name", "")))


@dataclass(frozen=True)
class ReconcilePlan:
    """What converging this agent's connectors requires."""

    # Objects to create or update: everything declared that is missing or
    # drifted. An object that already matches is NOT here -- re-applying it
    # every loop would be a hot loop with extra steps.
    apply: list[dict[str, Any]] = field(default_factory=list)
    # Objects Curie owns for this agent that are no longer declared.
    delete: list[tuple[str, str]] = field(default_factory=list)
    # Live objects that already match. Reported, not acted on -- a reconciler
    # that logs "converged 4 objects" every loop is noise nobody reads.
    unchanged: list[tuple[str, str]] = field(default_factory=list)
    # Live objects whose spec drifted from what is declared. A subset of
    # `apply`, surfaced separately because correcting drift is the one thing
    # here that overrides a human's `kubectl edit` and must be visible.
    drifted: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.apply and not self.delete


def plan(
    desired: list[dict[str, Any]],
    live: list[dict[str, Any]],
    *,
    agent: str,
) -> ReconcilePlan:
    """Compute the change set for one agent's connectors.

    ``desired`` is what the API rendered for the in-force version, already
    owner-labelled. ``live`` is every object currently in the namespace that
    might be relevant -- this filters it rather than trusting the caller's
    query, because a mis-scoped list here deletes someone else's connector.
    """

    owned_live = {identity(o): o for o in live if owner_of(o) == agent}
    desired_by_id = {identity(o): o for o in desired}

    to_apply: list[dict[str, Any]] = []
    unchanged: list[tuple[str, str]] = []
    drifted: list[tuple[str, str]] = []

    for obj_id, want in desired_by_id.items():
        have = owned_live.get(obj_id)
        if have is None:
            to_apply.append(stamp_hash(want))
        elif _differs(want, have):
            # Stamped on the way out, not by the caller: an object applied
            # without its digest is one the next pass cannot recognize as
            # converged, which is the hot loop this exists to prevent.
            to_apply.append(stamp_hash(want))
            drifted.append(obj_id)
        else:
            unchanged.append(obj_id)

    # Only ever objects this agent owns AND no longer declares. Both halves
    # matter: the ownership filter keeps another agent's `grafana` safe, and
    # the declaration check is what makes removing a connector from
    # connectors.yaml actually remove it.
    to_delete = sorted(obj_id for obj_id in owned_live if obj_id not in desired_by_id)

    return ReconcilePlan(
        apply=to_apply,
        delete=to_delete,
        unchanged=sorted(unchanged),
        drifted=sorted(drifted),
    )


def _differs(want: dict[str, Any], have: dict[str, Any]) -> bool:
    """Has the live object drifted from what is declared?

    Two independent questions, because they catch different things and neither
    subsumes the other:

    1. **Did our declaration change?** The hash annotation answers it exactly,
       including a field REMOVED from connectors.yaml -- something a
       does-live-contain-declared check cannot see, since a shrunk declaration
       is still contained by the larger live object. An object created by the
       CLI applier carries no hash, reads as drift, and is adopted on the first
       pass.
    2. **Did someone change the object underneath us?** A `kubectl edit` of the
       image leaves our declaration untouched, so the hash still matches. Only
       comparing values catches it.

    The value comparison is containment, not equality. A live object carries
    fields the API server defaulted -- `clusterIP`, `ipFamilies`,
    `progressDeadlineSeconds`, a `protocol: TCP` inserted into a port we
    declared without one -- and treating those as drift makes every pass rewrite
    every object forever. That is not hypothetical: plain equality reports a
    Service and a Deployment as drifted the instant after a successful apply.
    (A NetworkPolicy happens to acquire no defaults, so it compares equal either
    way -- which is exactly why it is the wrong object to test this on.)
    """

    if _hash_of(want) != _live_hash(have):
        return True
    return not _contains(_curie_owned_view(want), _curie_owned_view(have))


def _contains(want: Any, have: Any) -> bool:
    """Is everything ``want`` declares present and equal in ``have``?

    Keys the server added are ignored; keys we declare must match. Lists compare
    element-wise and by length, so dropping one entry from an env list or a
    port list still reads as drift.
    """

    if isinstance(want, dict):
        if not isinstance(have, dict):
            return False
        for key, value in want.items():
            if key not in have:
                # The API server does not persist an empty list or map, so a
                # rendered `args: []` comes back absent. Declaring nothing and
                # having nothing is the same state; calling it drift re-applies
                # the object on every pass forever.
                if value is None or (isinstance(value, list | dict) and not value):
                    continue
                return False
            if not _contains(value, have[key]):
                return False
        return True
    if isinstance(want, list):
        return (
            isinstance(have, list)
            and len(want) == len(have)
            and all(_contains(w, h) for w, h in zip(want, have, strict=True))
        )
    return bool(want == have)


# Metadata the API server owns. Present on a live object, absent on a rendered
# one, and never a reason to re-apply.
_SERVER_METADATA = frozenset(
    {
        "annotations",
        "creationTimestamp",
        "generation",
        "managedFields",
        "namespace",
        "ownerReferences",
        "resourceVersion",
        "selfLink",
        "uid",
    }
)


def _curie_owned_view(obj: dict[str, Any]) -> dict[str, Any]:
    metadata = {k: v for k, v in (obj.get("metadata") or {}).items() if k not in _SERVER_METADATA}
    return {
        "kind": obj.get("kind"),
        "apiVersion": obj.get("apiVersion"),
        "metadata": metadata,
        "spec": obj.get("spec"),
        "secret": _secret_payload(obj),
        "type": obj.get("type"),
    }


def _secret_payload(obj: dict[str, Any]) -> dict[str, str] | None:
    """A Secret's payload in one form, whichever form it arrived in.

    A Secret carries no spec. We render `stringData`; the API server returns
    the same bytes base64-encoded under `data`. Comparing the two field names
    directly means a Secret is drifted on every single pass -- so decode to a
    single representation and compare that. Values are only ever compared here,
    never logged.
    """

    if not (obj.get("data") or obj.get("stringData")):
        return None
    payload = {str(k): str(v) for k, v in (obj.get("stringData") or {}).items()}
    for key, encoded in (obj.get("data") or {}).items():
        if key in payload:
            continue
        try:
            payload[str(key)] = base64.b64decode(str(encoded)).decode()
        except (ValueError, UnicodeDecodeError):
            # A binary or malformed value. Compare the encoded form rather than
            # guessing -- being wrong here re-applies a credential needlessly.
            payload[str(key)] = str(encoded)
    return payload
