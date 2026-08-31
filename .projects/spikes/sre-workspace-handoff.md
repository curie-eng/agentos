# Safe-boundary late workspace handoff spike

Date: 2026-08-31

Decision: **GO for P2 implementation review.**

The authority for this spike is [ADR 0136: A late workspace handoff replaces
the sandbox at a fenced turn boundary](../../docs/adr/0136-a-late-workspace-handoff-replaces-the-sandbox-at-a-fenced-turn-boundary.md),
which is `Accepted` under ADR 0102's explicit-maintainer-approval path and
merged into `next` by this repository's PR #2153. This spike does not recreate
PR #1850's start-of-session workspace or publication work; it exercises only
acquisition after a generic runner already owns the logical thread.

## What was proved

One workspace-capable deployment can execute an ordinary first turn with no
repository and no workspace preparation. A later repository-bearing delivery:

1. establishes the server-side sticky repository selection using the ingress
   principal and deployment-derived agent;
2. refuses to steer, prepare, or acknowledge while the generic runner is
   active, awaiting approval, missing status, or missing a successful
   structured-history append;
3. prepares and verifies the sanitized workspace in the trusted worker;
4. cold-creates a candidate claim while the generic route remains authoritative;
5. preserves the logical session id and history reference on the candidate;
6. atomically swaps affinity only if the old claim and generation still match;
7. starts the repository-bearing delivery on the replacement; and
8. reuses that workspace route for later messages even when they omit the
   repository URL.

The affinity record contains the canonical repository and a fencing generation,
not a credential. No ACI or plugin-format field changed.

## Executable disposable proof

Run from the repository root with the local test backing services available:

```bash
set -euo pipefail
proof_log="$(mktemp /tmp/curie-workspace-handoff-proof.XXXXXX)"
trap 'test ! -e "$proof_log" || unlink "$proof_log"' EXIT

uv run pytest -q \
  apps/api/tests/test_workspace_control_plane.py \
  apps/worker/tests/test_workspace.py \
  apps/worker/tests/sandbox/test_affinity.py \
  apps/worker/tests/sandbox/test_substrate.py \
  apps/worker/tests/kernel/test_kernel.py \
  apps/worker/tests/kernel/test_runner_client.py \
  runner/tests/test_server.py \
  runner/tests/test_history.py \
  runner/tests/test_session.py \
  --disable-warnings --maxfail=1 2>&1 | tee "$proof_log"

# The synthetic clone credential is deliberately known only to the trusted
# preparation fixture. It must not be emitted by any worker/runner test log.
if rg -n --fixed-strings 'redeemed-credential-value' "$proof_log"; then
  echo 'credential marker reached proof output' >&2
  exit 1
fi
```

Observed on the implementation worktree before this report was written:

```text
269 passed, 12 warnings in 39.30s
```

The added proofs are outcome assertions through the real API router, kernel,
runner HTTP status endpoint, sandbox substrate, and a real per-test Valkey
prefix. Kubernetes is the external service replaced by the existing
in-memory Agent Sandbox client in the focused substrate suite. No shared
cluster was mutated.

The central positive test is
`test_late_workspace_selection_replaces_generic_sandbox_and_stays_sticky`.
It observes two different claims, the same session id, generation `0 -> 1`,
the canonical workspace repository only on the successor route, three turns
on one logical thread, and exactly one handoff. The atomic-store proof is
`test_replace_if_generation_is_an_atomic_claim_and_generation_fence`.
`test_accepted_steer_is_persisted_in_ordered_structured_history` accepts a live
steer and finds it exactly once in the durable structured turn record replayed
by a cold successor.

## Negative repository paths

- No repository is not an error. The API returns
  `{"repo_full_name": null}`, creates no selection row, and the kernel stays on
  the generic path without a workspace claim.
- Two root repository URLs are ambiguous and are rejected by
  `test_runtime_repo_parser_accepts_one_root_url_and_rejects_ambiguous` before
  selection, preparation, or runner execution.
- Credentialed, non-root, query-bearing, fragment-bearing, alternate-host, and
  alternate-port URLs are ignored by
  `test_runtime_repo_parser_rejects_non_root_or_credentialed_urls`.
- An unauthorized repository is refused by the API allowlist before credential
  redemption. A different repository after selection returns
  `workspace.selection_conflict`; the database still contains exactly the first
  `(agent_id, conversation_id, repo_full_name, selected_by)` row.
- An active turn, a failed history append, and an approval suspension each
  raise the existing pending-delivery deferral. The negative test asserts zero
  steer calls, zero candidate claims, and the unchanged generic route.

## Credential isolation search

The synthetic marker `redeemed-credential-value` is introduced at the trusted
credential client. The proof
`test_clone_credential_is_absent_from_argv_archive_config_and_claim_env`
searches all of these surfaces and finds no marker:

- Git subprocess argv;
- the complete workspace archive and its materialized `.git/config`;
- replacement claim environment values;
- preparation event names; and
- captured worker logs.

It also proves the only workspace values crossing to the sandbox are
`CURIE_WORKSPACE_REF` and `CURIE_WORKSPACE_SHA256`. The sibling proof
`test_workspace_claim_env_never_carries_worker_auth_object_store_or_git_credentials`
checks the internal worker token, object-store keys, AWS aliases, GitHub token,
and redeemed authorization header. The late-handoff kernel proof separately
searches the replacement claim environment for authorization, password, token,
or secret material. The executable log search above is an independent final
check over runner events and worker/runner output.

## Failure recovery and cleanup

Before the CAS, a losing claim+generation comparison deletes the candidate and
leaves the generic route untouched. The coordinator restores the previous
workspace ownership ledger and deletes the unexposed candidate archive.

After the CAS, the new route is authoritative. The test
`test_handoff_route_survives_old_claim_delete_failure_and_reaper_finishes_cleanup`
injects failure while deleting the old claim, proves the route still names the
replacement, ages the now-unrouted claim, runs the production orphan-reaper
path, and observes the old claim removed. A second reap is already proved
idempotent by the substrate suite.

All Valkey keys use per-test random prefixes and fixture teardown deletes them.
The fake Agent Sandbox client is process-local. Workspace scratch directories
and archives are temporary and removed by their owners. This run started no
Compose stack and no local or shared Kubernetes resources; it therefore had no
containers, namespaces, claims, or volumes to tear down. Existing containers
on the host belonged to another checkout and were left untouched.

## Verification record

| Check | Result |
| --- | --- |
| Focused API/worker/runner/substrate suite | 269 passed |
| Runner and worker first slice | 66 passed |
| Real-Valkey affinity and substrate slice | 36 passed |
| Workspace kernel paths | 10 passed |
| `uv run ruff check .` | passed |
| `uv run mypy` | passed, 243 source files |
| `git diff --check` | passed |
| OpenAPI regeneration | `WorkspaceSelectionOut.repo_full_name` is nullable |

Tier classification:

- `skill`: not applicable; no plugin packaging, skill runner loop, or ACI event
  shape changed.
- `local`: required. Component/integration coverage exercised API, worker,
  bearer-authenticated runner status, and Docker/Kubernetes-neutral sandbox
  routing. The hands-on local ladder remains a merge gate because this host's
  fixed Compose ports are occupied by another checkout; this run did not take
  over or stop that owner's stack.
- `local-release`: not applicable; no released image identity, install path,
  version pin, or release Compose file changed.
- `cluster`: required for final merge because production claims are Kubernetes
  resources. The focused spike exercises the exact substrate algorithm with a
  fake external Kubernetes control plane and real Valkey, but deliberately did
  not mutate `k8scratch` or another shared cluster. A disposable-cluster run of
  the stacked PR remains the merge gate.
- `live provider`: not applicable; model routing, provider authentication,
  token accounting, and model credentials are unchanged. The fake model is the
  deliberate credential-free control.
- `external integration`: not applicable; the logical Slack route is carried by
  queued-turn fixtures and no Slack API shape changed.

## Frozen contracts and P2 implementation scope

No frozen-contract blocker was found. The implementation changes neither
`packages/aci-protocol` nor `packages/plugin-format`. Discovering a need for a
new field in either remains an immediate stop condition.

The P2 production scope is 266 added and 48 removed production/generated
lines, plus 416 added and 12 removed test lines before this report:

- `apps/api/src/curie_api/routers/workspaces.py`, `schemas.py`, and
  `openapi.json`: nullable no-selection response with no durable row.
- `apps/worker/src/curie_worker/workspace.py`: optional selection and
  prepare-before-handoff coordination.
- `apps/worker/src/curie_worker/kernel.py` and `runner_client.py`: pre-steer
  sticky selection, bearer-authenticated safe-boundary read, deferral, and
  replacement routing.
- `apps/worker/src/curie_worker/sandbox/affinity.py`, `substrate.py`, and
  `types.py`: workspace-marked generations, atomic claim+generation CAS,
  cold candidate creation, and post-fence cleanup.
- `runner/src/curie_runner/session.py` and `server.py`: accepted-steer capture,
  authenticated internal `history_durable` status derived from the actual
  transcript append outcome, and a separate open probe status.
- API, worker, runner, and substrate tests: positive handoff, sticky reuse,
  ambiguity/conflict, every safe-boundary negative, credential search, CAS
  loss, post-CAS deletion failure, and cleanup.

The implementation PR targets `next` after merged PR #2153 and must not merge
until the hands-on local ladder can run without borrowing another checkout's
stack and the disposable cluster tier passes.
