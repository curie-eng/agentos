# Vision: parity ladder

## The guarantee

The parity ladder makes one bounded guarantee: each named core rung packages one canonical source tree independently, requires matching SHA256 digests, including [evals/cases.json](../evals/cases.json), then exercises that artifact through its own `curie` path. It is evidence that the packers, artifact identity, case identity, and selected runtime path agree, not a claim that every environment is equivalent. [Ladder script](../cli/scripts/e2e-ladder.sh)

## Skill

Skill runs the bundle through the existing skill end to end flow and its evaluator. In a live nightly run, it is the content graded rung: the weather evaluation must report one passing case and no failures. [Ladder script](../cli/scripts/e2e-ladder.sh) [Nightly workflow](../.github/workflows/nightly-graded-ladder.yaml)

## Local

Local brings up the development compose stack, deploys the same bundle, sends a message, and tears the stack down. A live green requires a finalized, nonempty reply that is not the fake model sentinel. [Ladder script](../cli/scripts/e2e-ladder.sh) [ADR 0081](adr/0081-nightly-graded-parity-ladder.md)

## Cluster

Cluster deploys the bundle and messages an already installed release. The nightly cluster job uses kind, and a live green has the same finalized reply and fake sentinel checks as local. [Ladder script](../cli/scripts/e2e-ladder.sh) [Nightly workflow](../.github/workflows/nightly-graded-ladder.yaml) [ADR 0081](adr/0081-nightly-graded-parity-ladder.md)

## Approval gate

Skill rung 1 also carries a live-only case, `case_live_approval_gate_denies`
(issue #2094): it gates `Bash` through `CURIE_APPROVAL_REQUIRED_TOOLS`, sends a
prompt that provokes exactly that tool, and asserts a real Claude Agent SDK
dispatch against a real model denies the call and the turn parks
`awaiting-approval` within a bounded time, with the gated command never
running. It is skipped under a fake run: `FakeModelSession` cannot exhibit the
failure modes this case proves absent. A separate PR check,
[SDK approval-gate live workflow](../.github/workflows/sdk-approval-gate.yaml),
runs this same case on any pull request whose `uv.lock` changes the
`claude-agent-sdk` entry, so a dependency bump that could reopen that failure
class is checked before merge rather than only at the next nightly run.

## What runs nightly

GitHub Actions runs the core skill, local, and cluster rungs against a real OpenRouter model at 08:00 UTC. Local release is separate companion coverage of the generated release compose path, not a fourth core rung. [Nightly workflow](../.github/workflows/nightly-graded-ladder.yaml) [Ladder script](../cli/scripts/e2e-ladder.sh)

## Honest costs

The latest twenty nightly workflow runs, checked 2026 08 20, contain ten successes and ten failures. [Workflow history](https://github.com/curie-eng/curie/actions/workflows/nightly-graded-ladder.yaml) A green fake rung proves plumbing, never that a real model given the bundle answers. A green local or cluster live rung proves a real, nonempty, finalized reply rather than its content being correct. [ADR 0081](adr/0081-nightly-graded-parity-ladder.md) The warm pool cannot bind a real model claim because its bundle environment arrives after its init containers have run, so that claim still needs cold recreation. [Issue 1492](https://github.com/curie-eng/curie/issues/1492)
