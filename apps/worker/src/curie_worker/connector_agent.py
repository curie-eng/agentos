"""Reconcile one agent's connectors (ADR-0090, #1184).

Joins the three halves that already exist: the API renders, `connector_reconcile`
decides, `connector_apply` carries it out. What lives here is the part none of
them can know on its own -- which objects are this agent's to manage, and when
reconciling would do more harm than leaving it alone.

**The credential hazard, which is the reason this module is not three lines.**
The API's render emits a Service, a Deployment and a NetworkPolicy per
connector. It never emits the Secret. For a connector using the owned form
(`secrets: [NAME]`) that Secret is minted by `curie cluster deploy`, from values
only the operator's environment holds -- the cluster deliberately does not have
them (ADR-0086), so the reconciler cannot render it and never will.

That Secret carries `curie.dev/connector-owner`, because the CLI stamps the same
label the reconciler prunes on. So the naive loop -- list what we own, delete
what we no longer declare -- **deletes the credential the CLI provisioned**, and
every connector pod fails on its next restart with a missing secretKeyRef. The
object is owned, it is genuinely undeclared, and pruning it is exactly what the
plan is supposed to do. Nothing downstream can catch this; it has to be handled
here.

So an agent's owned Secret is protected: never applied (we have no values) and
never pruned (someone else owns its lifecycle). And if it is protected but
absent, the agent is skipped entirely rather than reconciled -- applying a
Deployment whose secretKeyRef points at a Secret that does not exist produces
pods that never start, which is worse than not acting.

Connectors using the reference form (`secrets: [{from_secret: ...}]`, #1163)
have no owned keys at all, so they reconcile with no exception. That is the
path ADR-0090 calls the prerequisite, and it is why this restriction shrinks as
bundles adopt it rather than being permanent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .connector_apply import ApplyReport, ConnectorClient, execute
from .connector_reconcile import OWNER_LABEL, ReconcilePlan, identity, plan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderedConnectors:
    """What the API returns for one version's `connectors.yaml`."""

    manifests: list[dict[str, Any]] = field(default_factory=list)
    # The Secret Curie owns for this agent, and the keys whose VALUES a caller
    # must supply. Non-empty means the CLI owns that Secret's lifecycle.
    owned_secret_name: str = ""
    owned_secret_keys: list[str] = field(default_factory=list)

    @property
    def needs_operator_credentials(self) -> bool:
        return bool(self.owned_secret_keys)


class ManifestSource(Protocol):
    """Where rendered connector objects come from.

    A seam, so the reconcile step is testable without an API. Rendering stays
    in the API by ADR-0090 -- duplicating it here would recreate exactly the
    drift `connectors.yaml` exists to prevent.
    """

    def rendered(self, *, agent_id: str, version_id: str) -> RenderedConnectors: ...


@dataclass(frozen=True)
class AgentOutcome:
    """What reconciling one agent did, or why it did nothing."""

    agent: str
    skipped: str | None = None
    plan: ReconcilePlan | None = None
    report: ApplyReport | None = None

    @property
    def ok(self) -> bool:
        return self.report is None or self.report.ok


def own(obj: dict[str, Any], agent: str) -> dict[str, Any]:
    """Stamp the ownership label the whole prune path keys on.

    The renderer does not set it -- it is a deployment fact, not a bundle fact,
    and the CLI applier stamps it at the same point. An object applied without
    it is invisible to every future prune, so the applier refuses to write one;
    this is where the reconciler earns the right to.
    """

    stamped = dict(obj)
    metadata = dict(stamped.get("metadata") or {})
    metadata["labels"] = {**(metadata.get("labels") or {}), OWNER_LABEL: agent}
    stamped["metadata"] = metadata
    return stamped


def reconcile_agent(
    source: ManifestSource,
    client: ConnectorClient,
    *,
    agent: str,
    agent_id: str,
    version_id: str,
    namespace: str,
) -> AgentOutcome:
    """Converge one agent's connectors toward its in-force version."""

    rendered = source.rendered(agent_id=agent_id, version_id=version_id)
    desired = [own(obj, agent) for obj in rendered.manifests]
    live = client.list_owned(namespace, agent)

    protected: set[tuple[str, str]] = set()
    if rendered.needs_operator_credentials and rendered.owned_secret_name:
        protected.add(("Secret", rendered.owned_secret_name))
        if not any(identity(obj) in protected for obj in live):
            # Reconciling now would create Deployments referencing a Secret that
            # does not exist. Pods would stay stuck rather than fail loudly, and
            # the cause would be several layers from the symptom.
            reason = (
                f"connector credentials are operator-supplied and "
                f"{rendered.owned_secret_name} is not provisioned; "
                f"deploy once with the CLI (keys: {', '.join(rendered.owned_secret_keys)})"
            )
            logger.warning("connector reconcile skipped agent=%s reason=%s", agent, reason)
            return AgentOutcome(agent=agent, skipped=reason)

        logger.debug(
            "connector reconcile agent=%s protecting operator-supplied secret %s",
            agent,
            rendered.owned_secret_name,
        )

    # Hidden from the plan rather than filtered out of its result: the plan's job
    # is "own it and no longer declare it -> delete it", and that judgement is
    # correct. What is wrong is calling this object ours to begin with.
    manageable = [obj for obj in live if identity(obj) not in protected]

    computed = plan(desired, manageable, agent=agent)
    if computed.is_noop:
        return AgentOutcome(agent=agent, plan=computed, report=ApplyReport())

    report = execute(client, computed, namespace=namespace, agent=agent)
    if not report.ok:
        logger.warning(
            "connector reconcile agent=%s had %d failure(s): %s",
            agent,
            len(report.failures),
            "; ".join(f"{kind}/{name}: {err}" for kind, name, err in report.failures),
        )
    return AgentOutcome(agent=agent, plan=computed, report=report)
