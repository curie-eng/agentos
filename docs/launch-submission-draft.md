# Draft launch submission

IDEA ONLY: This draft takes inspiration from the research described in issue #1619. It copies no wording or creative expression. Brian alone decides whether and where to publish it.

## Submission title

Show HN: The missing piece between a coding agent working locally and an agent running in production

## Submission body

I can ask a coding agent to build a skill that works on my laptop. The hard part begins when that same bundle needs to run as a production agent.

Curie is a self hosted harness for that handoff. It runs the same immutable plugin bundle and the bundle's own eval suite through three targets: a fast skill loop, the full platform locally, and Kubernetes. The aim is environment parity, not a promise that production behavior will match every test case.

The local target exercises the platform path through its queue, worker, sandbox, and reply flow. The cluster target runs that platform on Kubernetes. Git flow can store and deploy a versioned bundle, with a merge promoting the already built version rather than rebuilding it.

The public verification posture is deliberately imperfect. Pull request parity jobs use a fake model to prove plumbing. A separate nightly workflow runs the live model ladder. From July 25, 2026 through August 19, 2026, scheduled nightly runs recorded 11 successes and 15 failures out of 26, a 42.3 percent workflow pass rate. That is a signal to improve, not evidence that every production agent is safe.

## How is this different from X

It is not a coding agent. A coding agent can author a bundle, but Curie is the harness that runs that bundle through local and cluster targets with the same bundle and eval cases.

It is not a behavior guarantee or a substitute for production ownership. Parity can expose environment drift, while real traffic, credentials, integrations, and incomplete eval cases can still produce failures.

It is not only an observability product or a generic orchestration framework. Its narrow job is carrying an agent bundle from a local development loop through a versioned deployment path, while providing traces, evals, budgets, and isolated sandboxes as parts of that path.

## Claim ledger

1. Same immutable bundle and evals across skill, local, and cluster: `README.md`, `docs/agents.md`, and `docs/adr/0021-agentos-is-a-harness-for-coding-agents.md`.
2. Environment parity is not a behavior guarantee: `README.md`.
3. Local queue, worker, sandbox, and reply path: `README.md` and `ARCHITECTURE.md`.
4. Self hosted Kubernetes target and git flow deployment: `README.md` and `docs/vision.md`.
5. Traces, evals, budgets, and isolated sandboxes: `README.md` and `docs/vision.md`.
6. Fake model pull request plumbing gates: `.github/workflows/ci.yaml`, jobs `e2e-ladder`, `e2e-ladder-release`, and `e2e-ladder-cluster`.
7. Live nightly grading and its purpose: `.github/workflows/nightly-graded-ladder.yaml` and `docs/adr/0081-nightly-graded-parity-ladder.md`.
8. Nightly pass rate: GitHub Actions workflow `Nightly graded parity ladder`, scheduled run history from July 25, 2026 through August 19, 2026, 11 successful and 15 failed runs of 26 total.

## Submission plan

1. Brian decides whether to publish. Until then, take no external action.
2. With Brian approval, Hacker News is the primary surface for one second submission. This is not a second launch. An unchanged artifact does not prevent resubmission when the framing and evidence are honest.
3. Before every post, Brian approves the exact title and body. After a Hacker News post, monitor the thread for 48 hours and answer only with evidence in the claim ledger.
4. After seven days, review comments and referral quality. Do not repost unchanged copy without Brian approval.
5. If the Hacker News conversation is thin, wait at least six weeks before considering another Hacker News submission. Refresh the framing and current proof, do not call it a launch, and require Brian approval.
6. Relevant self hosted or agent developer communities are conditional secondary surfaces. Consider one original post only after a material, independently verifiable product improvement, at least 30 days after the Hacker News submission, and Brian approval for that post.
7. Keep the GitHub issue as the internal record of decisions, results, and any later copy revision.
