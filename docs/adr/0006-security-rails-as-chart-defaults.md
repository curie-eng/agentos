# 6. Security rails are chart defaults, not hardening backlog

Date: 2026-07-04
Status: Accepted

**Superseded in part by [ADR 0114](0114-cluster-up-infers-detected-install-facts.md).**
The final Consequences clause no longer requires gVisor absence to fail review
when `curie cluster up` receives the exact admission rejection that the
`gvisor` RuntimeClass is absent. The Decision still keeps gVisor enabled by
default, and every other security rail remains unchanged.

## Context

The input channel (Slack) is prompt-injectable by anyone in the channel, and the runner holds customer credentials. An enterprise leave-behind will be security-reviewed. Retrofitting isolation costs more than defaulting it, and a boundary that is *believed* to hold but doesn't (e.g. an egress policy on a non-enforcing CNI) ships an incident. So the security posture had to be proven, not assumed.

## Decision

The umbrella Helm chart ships security rails **on by default**: per-agent K8s ServiceAccounts, NetworkPolicy isolating runner pods (egress = model API + declared MCP endpoints + DNS only), per-plugin secret scoping (a runner mounts only its own agent's credentials), non-root / read-only-rootfs runner containers, gVisor (runsc) RuntimeClass for kernel isolation, and the interrupt kill path always available. Every customer install runs a CNI-enforcement preflight before trusting the egress policy.

## Evidence (live, scratch cluster, 2026-07-04)

- **Egress lockdown holds, with a control:** baseline pod reached `example.com` (HTTP 200); after default-deny + DNS-only, `example.com` and the cloud metadata endpoint `169.254.169.254` both blocked (curl exit 7) while DNS still resolved. The before→after flip proves the CNI genuinely enforces.
- **Cross-agent secret isolation:** an RBAC Role scoped to one secret let SA `agent-a` get its own creds, not agent-b's, and not list.
- **Container hardening:** pod ran uid=1000 with read-only rootfs; write to `/` failed.
- **gVisor:** a `runtimeClassName: gvisor` pod ran under the gVisor sentry (`uname` = `4.19.0-gvisor`, distinct from host), even inside an unprivileged LXC (runsc systrap platform). **Kata is infeasible** where there is no `/dev/kvm`.

## Consequences

- **A customer CNI must enforce NetworkPolicy or the egress lockdown is a silent false-pass.** The install preflight (the same before/after probe) is mandatory, not optional.
- gVisor is the isolation runtime; Kata is only viable where a hypervisor (`/dev/kvm`) is present.
- These rails are load-bearing product behavior with their own tasks; their absence fails review.
