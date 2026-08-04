# Vision

This is the north star for Curie: what the project is, who it is for, and what
it could become. It is deliberately not a feature list or a roadmap (those live
in [GitHub issues](https://github.com/curie-eng/curie/issues)). When we weigh
a new feature, this is the document we hold it against. If a change does not
serve the vision below, it needs a very good reason to exist.

## What Curie is

The open source platform for building, running, and operating production AI
agents. A developer authors an agent locally, tests it in a loop identical to
production, and ships it through git flow: a pull request opens a dev agent, a
merge promotes it to prod. Agents answer in Slack today — the channel is a
swappable seam, with email and Teams next — run in isolated sandboxes, and
get observability and evals out of the box. One command brings the whole
thing up, locally or on any cluster.

If Supabase is the box that tells a developer they need auth, storage, and a
realtime database before they know they need them, Curie is the box that tells
them they need evals, traces, and CI/CD for their agent before it breaks
silently in production.

## Why it exists

Building a production agent today means assembling a channel, a runtime, skills,
connectors, observability, and a deploy pipeline from scratch, and most people
skip the last three. The result is a familiar failure: the agent works on the
author's laptop, drifts when someone else runs it, and degrades in production
with no one watching. The industry data says the same thing (most GenAI pilots
show no measurable impact; a large share of agentic projects get canceled), and
the diagnosis is missing engineering discipline, not missing model capability.

Curie ships that discipline as the default path, not an advanced option.

## Who it is for

Developers building production agents. Not business users, not operators.

Cost reduction is the boss's pain. The local dev loop, evals, and CI/CD are the
developer's pain, and the developer is who adopts open source. Most developers
think building an agent means writing a `skill.md`; they do not yet know they
need evals, traces, and promotion gates. Curie gives them those before they
learn the hard way, and it does so as a tool they can build on, own, and run on
their own infrastructure rather than a black box they rent.

Increasingly the developer builds agents by pointing a coding agent (Claude Code,
Codex, Cursor) at the work rather than hand-writing every file. So the *interface*
to Curie is drifting to the developer's coding agent: they point it at Curie
and it authors the skills, wires the connectors, writes the evals, and ships the
bundle. The human stays in the loop through the agent, rarely typing `curie`
directly. This does not change who adopts Curie or who owns the stack; it
changes who operates it. Curie is a harness a coding agent can drive to build
agents properly, because the one thing a coding agent cannot guarantee on its own
is that a skill working locally will behave identically deployed and on a cluster.
That guarantee is what the harness supplies.

## What makes it Curie

Six commitments that should hold across every feature we build:

- **The local loop is the production loop.** What a developer tests locally is
  what runs in prod, down to the sandbox and the harness. Local-first is a
  load-bearing promise, not a convenience.
- **Git flow is the deploy model.** Branches map to bot environments; a PR is a
  dev agent, a merge is a promotion. Shipping an agent should feel like shipping
  code, because it is.
- **Opinionated core, swappable jobs.** One curated default for every job a
  production agent platform must do (harness, channel, observability, evals,
  blob storage, relational store), each replaceable behind a clean seam. The
  defaults are the draw; the seams are why you stay. You own your stack and your
  model choice.
- **The whole box.** Build, test, deploy, observe, and evaluate in one platform.
  The full loop is the product, not any single piece of it.
- **Open, self-hostable, model-agnostic.** Runs on your infrastructure, with any
  model, with nothing locked away. The same eval suite across models is how a
  developer learns which model each job actually needs.
- **Agent-operable by default.** The CLI's primary user is a coding agent, not a
  human at a prompt. Commands are structured, non-interactive, idempotent, and
  self-describing, and the harness tells the agent exactly how to use it, so a
  coding agent can drive the whole loop and a human rarely needs to.

## What it could become

Near term, Curie is how we deliver agent work faster: a repeatable delivery
artifact where each project costs a fraction of the last, and the platform is
the boundary that keeps bespoke agent development from becoming
"Accenture for agents."

If it proves out internally, the longer arc is a developer-first open source
platform that grows the way Supabase and PostHog did: adoption from developers
who want to own their agent stack instead of renting an expensive black box, a
comparison the market makes on its own once the box is good enough. We do not
lead with "the cheap one." We lead with "build agents as code, test them with
evals, ship them through git flow, run them on your infrastructure with any
model," and let owning your stack be the reason people arrive.

The test for any feature is simple: does it make that developer's loop tighter,
that box more complete, or that ownership more real? If yes, it belongs. If no,
it is someone else's product.
