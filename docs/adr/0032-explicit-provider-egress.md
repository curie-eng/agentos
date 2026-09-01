# 32. Explicit named-provider egress, resolved at install

Date: 2026-07-13
Status: Accepted

**Superseded in part by [ADR 0114](0114-cluster-up-infers-detected-install-facts.md).**
The first Decision clause and rejected Alternative 1 no longer require an
explicit provider when the effective credential prefix selects one provider.
Every other egress rule remains unchanged.

**Superseded in part by [ADR 0075](0075-the-agent-proxy-credential-and-egress-boundary.md).**
The install-time CIDR resolve is the interim FQDN mechanism this ADR deferred;
ADR-0075 is that durable proxy. The CIDR path remains the fallback where the
proxy is not deployed.

Implements [#362](https://github.com/curie-eng/curie/issues/362).

## Context

`curie cluster up` used to open the runner's fail-closed egress to the model
provider automatically whenever a model credential was present. It did this by
pushing a single hardcoded Anthropic CIDR (`160.79.104.0/23`) as an egress
carve-out at index `[0]` of the NetworkPolicy's `allowedEgress` array, plus a
note claiming "egress opened to the model provider."

That is wrong for any BYO provider. A credential for OpenRouter (or OpenAI, or
Gemini) still opened the Anthropic range and still printed the misleading note,
while the provider's real API host stayed NetworkPolicy-blocked -- so the runner
could authenticate nowhere and the operator was told the opposite. The carve-out
was also a bare CIDR baked into the binary, and provider/CDN IP ranges rotate, so
even the Anthropic-only case drifted stale over time.

The underlying containment is CIDR-based: the runner NetworkPolicy allows egress
only to explicitly declared IP ranges on TCP 443 (metadata endpoint excepted).
There is no FQDN-aware egress in the stack today, so opening a provider means
opening IPs, and IPs for a hosted API are not stable.

## Decision

Replace the automatic hardcoded carve-out with an explicit, per-provider opt-in
resolved at install time:

- **A model credential alone opens no egress.** The sandbox stays fail-closed /
  sealed; the model is unreachable until its provider egress is opened explicitly.
- **New repeatable flag `--allow-egress-host <provider>`** accepting only the
  named providers `anthropic` and `openrouter`. Each maps to its API hostname(s)
  (`anthropic` -> `api.anthropic.com`, `openrouter` -> `openrouter.ai`), which are
  resolved via a DNS lookup in the CLI to narrow `/32` + `/128` host-route CIDRs at
  install time. Only hostnames are baked into the binary -- never provider IPs --
  because provider/CDN IPs rotate.
- **The named-provider set is intentionally scoped to the two providers the
  runner can drive end-to-end today** (`anthropic` via `sk-ant-` keys,
  `openrouter` via `sk-or-` keys). Opening egress to a host the runner cannot
  actually talk to gives false confidence, so a provider is only offered here once
  the runner has runtime support for it. Adding a new provider to this egress list
  must be done together with the runner-side credential/base-URL support for it, in
  the same change, so egress never advertises an undrivable provider. OpenAI,
  Gemini, and the base-URL-override providers (zhipu/moonshot/deepseek) are
  candidates to layer in once the runner supports them.
- **An unknown `--allow-egress-host` value is a usage error (exit 2)** that lists
  the accepted providers and points at `--allow-web-egress <CIDR>` for arbitrary
  destinations.
- **The note is fixed.** It names the provider(s) whose egress was opened, plus a
  "resolved IPs can rotate; re-run if calls start failing" caveat. When a
  credential is present but nothing was opened, `up` warns that the sandbox is
  sealed and the model is unreachable, naming both `--allow-egress-host` and
  `--allow-web-egress`.
- **`--allow-web-egress <CIDR>` is unchanged**: the raw escape hatch for arbitrary
  CIDR destinations on TCP 443, still metadata-excepted by the chart.

## Alternatives considered and rejected

1. **Auto-detect the provider from the credential prefix and resolve it.** Infer
   Anthropic vs OpenRouter vs OpenAI from the API-key shape, then DNS-resolve that
   provider's host. Rejected: auto-detection is exactly what shipped the wrong
   provider in the first place (a BYO key that does not match the guessed prefix
   opens the wrong host, or none), and it still bets on resolved CDN IPs that
   rotate. Making the provider an explicit operator choice removes the guess.

2. **Bake a curated provider -> CIDR table into the binary.** Ship a maintained
   map of each provider's published API ranges. Rejected: those are insider CIDRs
   that go stale on every provider/CDN renumbering, and a released binary cannot
   be re-cut fast enough to track them. Resolving hostnames at install keeps the
   binary carrying only stable names.

3. **A durable FQDN-based egress mechanism now** (Cilium `toFQDNs` or an egress
   proxy that re-resolves on rotation). Deferred to a separate future ADR: it is a
   larger, CNI-dependent mechanism, not a chart/CLI change. This ticket
   resolves-at-install as the interim; it does **not** solve IP rotation.

## Consequences

- **Behavior change operators must know about:** a model credential alone no
  longer reaches the provider. An install that previously "just worked" now
  installs sealed until the operator passes `--allow-egress-host <provider>` (or a
  `--allow-web-egress` CIDR). The credential-present-but-sealed warning names both
  flags so the fix is discoverable.
- Resolved host routes are a point-in-time snapshot. When a provider rotates the
  IPs behind its API host, the opened `/32`s go stale and calls start failing;
  the remedy is to re-run `cluster up`, and the note says so. Durable
  rotation-survival is out of scope here.
- Host resolution happens from the operator's network where `cluster up` runs,
  which can diverge from the runner's in-cluster resolution (GeoDNS, split-horizon
  DNS, or a CDN returning different IPs to the two networks). The operator-resolved
  `/32`s may then not match what the runner resolves, so calls fail closed even
  though egress "was opened"; the remedy is the same (re-run `cluster up`, or the
  durable FQDN mechanism), which reinforces why FQDN-aware egress is the real fix.
- The durable FQDN-based mechanism is tracked separately as a future ADR; this ADR
  is the interim decision it will eventually supersede.
- Containment is not weakened for the BYO case: only the named provider's host
  routes are opened, so an OpenRouter credential no longer opens the Anthropic
  range (or vice versa).
