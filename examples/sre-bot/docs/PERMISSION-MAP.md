# SRE bot permission map

Every capability this bot can request is listed here. Curie's tool policy,
human approval, and Kubernetes authorization answer different questions:

- `toolPolicy` decides whether the runner allows, pauses, or refuses a tool.
- approval authorizes one paused call; it does not grant Kubernetes authority.
- RBAC is the capability ceiling enforced by the Kubernetes API server.

The sandbox receives only connector URLs. Kubeconfig files stay mounted in the
connector containers, outside the bundle and runner environment.

## Vanilla Kubernetes connector

The pinned upstream connector starts with only the core toolset, multi-cluster
disabled, stateless operation, and one file-mounted kubeconfig. Config tools and
multi-cluster tools are not advertised. An upstream tool added later is
unclassified and denied by `curie/mcp-tool-policy@1`.

### Reads allowed without approval

| Canonical tool |
|---|
| `kubernetes/events_list` |
| `kubernetes/namespaces_list` |
| `kubernetes/projects_list` |
| `kubernetes/nodes_log` |
| `kubernetes/nodes_stats_summary` |
| `kubernetes/nodes_top` |
| `kubernetes/pods_list` |
| `kubernetes/pods_list_in_namespace` |
| `kubernetes/pods_get` |
| `kubernetes/pods_top` |
| `kubernetes/pods_log` |
| `kubernetes/resources_list` |
| `kubernetes/resources_get` |

### Mutations requiring approval

| Canonical tool | Live tool |
|---|---|
| `kubernetes/pods_delete` | `mcp__kubernetes__pods_delete` |
| `kubernetes/pods_exec` | `mcp__kubernetes__pods_exec` |
| `kubernetes/pods_run` | `mcp__kubernetes__pods_run` |
| `kubernetes/resources_create_or_update` | `mcp__kubernetes__resources_create_or_update` |
| `kubernetes/resources_delete` | `mcp__kubernetes__resources_delete` |
| `kubernetes/resources_scale` | `mcp__kubernetes__resources_scale` |

These six entries are exact, not a wildcard. Each approval is one-shot and
tool-name-scoped. Rejection leaves the call upstream of the MCP server.

The single `sre-bot-kubernetes` credential has cluster read access only for
non-secret operational resources and an additive Role only in `sre-demo`. It
may create, update, patch, scale, or delete workload resources there. It cannot
reach Secrets, ServiceAccounts, RBAC, CRDs, admission webhooks, namespace or
node mutation, another namespace, or cluster-scoped mutation.

Raw manifest updates can change images, commands, and environment inside that
ceiling. Kubernetes RBAC cannot restrict a patch to a friendly field, and there
is no generic rollback for those changes. Approval controls the agent, not the
credential: if the credential leaks, only RBAC remains.

## `upgrade_self()`

Tool: `mcp__self-upgrade__upgrade_self()` with zero arguments.

This remains a separate `approvalPolicy` gate. Its connector starts a Job from
the named suspended self-upgrade CronJob. The bot cannot select a repository,
branch, image, command, version, or target. Starting the Job is not proof the
upgrade finished; the bot must verify the resulting Job and deployment state.

The connector identity can `get` the two named CronJobs and has namespace-wide
`create` and `list` on Jobs. Kubernetes cannot apply `resourceNames` to create,
so the no-argument connector surface is defense in depth rather than an RBAC
ceiling.

## `upgrade_platform()`

Tool: `mcp__self-upgrade__upgrade_platform()` with zero arguments.

This is a second explicit gate and a separate Job-trigger path. The Job's
short-lived projected `curie-platform-upgrader` identity can rewrite the
platform release objects. The general Kubernetes connector never receives that
identity or Role. A Helm rollback does not undo database migrations, so recovery
may require restoring a backup rather than another tool call.

### Service-account escalation disclosure

When `manifests/upgrade-role.yaml` is installed beside
`manifests/platform-upgrade-role.yaml`, a leaked `sre-bot-upgrader` token can
create a Job with `spec.serviceAccountName: curie-platform-upgrader`. Kubernetes
RBAC authorizes the Job create but does not restrict which ServiceAccount its
Pod uses, so that Job inherits the platform upgrader's namespace-wide powers.

The connector cannot construct that request: its zero-argument tool posts an
operator-written CronJob template verbatim. A token holder can bypass the
connector. The hole is open. Draft
[ADR-0141](../../../docs/adr/0141-admission-pins-jobs-a-connector-token-may-create.md)
proposes in-tree admission that pins a Job from this identity to the live
CronJob template (ServiceAccount, image, command, env, and volumes together).
Constraining ServiceAccount choice alone is not enough: allowing
`curie-platform-upgrader` preserves the escalation, and denying it breaks the
legitimate Job.

`manifests/upgrade-job-admission.yaml` is that sketch. It is not applied by
`curie example sre-bot install --platform-upgrade`, and applying it is not
authorized while the ADR is Draft. If the proposed control is not acceptable,
do not install the upgrade connector.

## Retention and evidence

Kubernetes Events and pod logs are short-lived. Approval records show a call
was authorized, not that the API accepted it or the workload became healthy.
Every mutation report therefore pairs the approval result with the Kubernetes
API result and a follow-up read of resulting state. Job history is bounded by
the CronJob retention settings; external logs and backups are operator-owned.
