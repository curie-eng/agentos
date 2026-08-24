# 115. Runtime repository selection is sticky authorized thread state

Date: 2026-08-24

Status: Accepted

Explicit maintainer direction on 2026-08-24 authorizes this ADR to land
Accepted with its realizing code paths in the same pull request.

## Context

[ADR 0114](0114-managed-repository-workspaces-and-approval-gated-publication.md)
made repository workspaces and approval-gated publication platform
capabilities, but selected one repository when a deployment was configured.
That is sufficient for one purpose-built deployment and insufficient for the
development surface: a person should be able to start a Slack thread by naming
the repository to change, without creating or reconfiguring an agent first.

The platform identity remains operator-owned. Per-user GitHub OAuth and user
attribution are separate future work. Runtime choice therefore increases the
importance of the credential boundary: message text can choose a repository,
but must never choose an origin, credential, or publication target outside
operator policy.

Slack carries no durable first-message marker. Delivery can be retried and
workers can restart, so the enforceable rule is first-to-establish-wins rather
than first-observed-by-one-process. A later message may establish selection
when no earlier message did. Deployments also roll while Slack threads remain,
so repository state cannot be keyed by deployment.

## Decision

**A deployment declares repository-workspace capability; the first repository
fact established for an agent thread becomes permanent sticky control-plane
state.**

`workspace_enabled` replaces a deployment's concrete repository value.
`curie ... deploy --workspace` enables runtime selection, `--no-workspace`
disables it, and omission carries the previous active deployment setting. The
git-flow `--repo` binding remains independent and does not select a development
workspace.

On every turn for a workspace-enabled deployment, the trusted worker parses an
optional root `https://github.com/owner/repository` URL from the raw message.
Slack link wrappers and an optional `.git` suffix are accepted. Userinfo,
ports, non-HTTPS origins, lookalike hosts, query or fragment components, and
paths below the repository root are not repository facts. Repeated references
to the same canonical repository are one fact; different repository facts in
one message are refused.

The worker sends the optional canonical fact, author, deployment id, and
conversation id to the worker-authenticated API before sandbox claim, adoption,
steering, or model execution. The API derives the agent id from the deployment
and atomically inserts a row unique on `(agent_id, conversation_id)`. A
concurrent winner is re-read. The same repository is reusable; a different
repository is a terminal, user-visible refusal. A thread without a selection
can establish one on any later message because the transport has no stronger
opening-message fact. A selection does not expire with sandbox affinity,
publication, redeployment, or worker restart.

The API authorizes before durable insertion and before touching the GitHub
credential resolver. `api.githubRepoAllowlist` is the sole runtime-selection
policy. It supports exact `owner/repository` entries and explicit owner-wide
`owner/*` entries, compares case-insensitively as whole values, and defaults to
empty deny-all. The policy is rechecked on selection reuse, clone-credential
redemption, publication creation, and publication-credential redemption, so an
operator removal revokes existing threads and approved-but-not-yet-published
work. Credential resolver caches are bounded because their keys now originate
from authorized runtime input.

After selection, workspace preparation retains ADR 0114's credential isolation:
the API prefers a repository-scoped GitHub App installation token and falls
back to `api.githubToken`; the trusted worker supplies it only as an ephemeral
clone header, immediately rewrites and verifies the checkout remote, and sends
no write-scoped GitHub credential into the sandbox. The raw-token fallback is
an accepted v1 tradeoff for plain installs without GitHub App configuration:
its authority may be broader than one repository, so the allowlist controls
where the platform may present it and the audit records which credential path
was used.

The init container verifies the archive in a staging directory it creates and
owns, then moves the verified files into the mounted workspace. It therefore
does not receive process-scoped Git configuration. The runner, which consumes
the transferred checkout under a different process identity, receives only a
protected `safe.directory=/workspace` declaration and no repository credential.

Publication carries the repository observed in the trusted workspace snapshot.
The API compares that fact with the sticky thread selection before atomically
creating the approval and Publication, repeats the comparison on deduplicated
replay, and derives the Job credential only after rechecking both selection and
current allowlist. Denial still creates no Job and redeems no credential.
Publication refuses changes under `.github/workflows/`: pushing such a branch
can execute repository automation before a reviewer sees the pull request, so a
warning on a self-approvable card would not be a sufficient boundary.
The worker first checks the durable thread transcript for a deterministic
publication marker, then uses the transcript store's atomic append endpoint
before reporting the outcome through the routed reply adapter. A recovery read
absorbs a lost append response, and a later retry no-ops when it finds the same
marker. A transient transcript failure does not veto Slack result delivery or
approval-card settlement: the durable result outbox backs off before retrying
the transcript. If durable state capacity permanently refuses the append, the
worker logs the degradation and continues routed delivery rather than hiding a
pull request that was already created.
Publication Jobs run in a dedicated namespace under a tokenless service account.
A default-deny ingress policy and two-rule egress policy permit only cluster DNS
and operator-supplied GitHub HTTPS CIDRs. GitHub address rotation can therefore
fail publication closed until an operator refreshes those install-time CIDRs;
publication pods never inherit the sandbox NetworkPolicy selector.

The realizing paths are `apps/api/src/curie_api/models.py`, `crud.py`,
`schemas.py`, `workspace_policy.py`, `routers/workspaces.py`, and
`routers/publications.py`; `apps/worker/src/curie_worker/workspace.py`,
`publication_validation.py`, `publication_k8s.py`, `publication_clients.py`,
`binding.py`, and `kernel.py`; `cli/src/main.rs` and `cli/src/api.rs`; and
`charts/curie`.

## Consequences

A plain installation can enable the capability once, authorize an operator
repository set, and let Slack threads choose among those repositories. Later
messages need no URL. A different URL in the same thread cannot retarget the
existing sandbox or a later publication.

Repository selection is durable security state. It outlives ephemeral sandbox
routes and can grow with the number of development threads; normal agent or
thread-retention administration must account for those rows. The recorded
author provides attribution for who aimed the operator identity, while Slack
channel membership remains the v1 user-authorization boundary.

An empty allowlist makes `--workspace` inert rather than permissive. Operators
using the PAT fallback must scope that token as narrowly as their GitHub setup
allows. Moving from operator identity to per-user OAuth does not require moving
clone or publication into the sandbox; it changes credential resolution behind
the same control-plane decisions.

## Alternatives considered

### Reconfigure the deployment for every repository

Rejected. It prevents the five-minute Slack-native workflow and couples one
conversation's target to every other thread on the deployment.

### Let the sandbox interpret the URL and clone directly

Rejected. It would place the operator credential or GitHub authentication flow
inside prompt-controlled code and undo ADR 0114's credential isolation.

### Key repository selection by deployment

Rejected. Redeployment appends a new deployment row and would silently detach
an active Slack thread from its repository.

### Permit retargeting an existing thread

Rejected. Retargeting creates ambiguity between the mounted base, diff,
approval card, and publication destination. A new repository requires a new
thread.

### Treat an empty allowlist as unrestricted

Rejected. Runtime text would then be the only boundary controlling where an
operator write credential is presented.

### Require GitHub App credentials for runtime selection

Rejected for v1. App installation tokens remain preferred, but raw-token
fallback is required for plain installs without GitHub App configuration.
Fail-closed allowlisting, immediate remote sanitization, no sandbox credential
delivery, and audited credential-path selection bound that tradeoff.

### Add per-user GitHub OAuth now

Deferred. It improves identity attribution but is not required to make dynamic
repository choice safe under the operator-owned v1 credential model.
