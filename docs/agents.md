# agents.md: the verification contract for driving Curie

Curie is a harness that runs the same immutable bundle and the same eval suite
<!-- doclint:ignore-line -->
(the bundle's own `evals/cases.json` and optional `evals/trajectory.json`)
across three tiers, skill, local, and cluster, so an agent that worked locally
does not silently break once deployed.
Here, `skill` names the runner only tier. An authored bundle skill is the
artifact at `skills/<name>/SKILL.md`.
Its CLI's primary user is a coding agent driven by a developer, and this file is
the contract that agent works to.

If you are instead working **on** the Curie repository itself, the file you want
is [`AGENTS.md`](../AGENTS.md) at the repo root. This one is about driving the
harness, from a released binary or someone else's bundle.

## The surfaces you can read

- [`llms.txt`](../llms.txt) at the repo root: the curated machine map of every
  doc here, organized around the parity ladder. Start there when you have a
  checkout.
- `curie guide`: the self-contained primer for driving the harness, carried
  inside the binary, so it needs no checkout. `curie guide --json` for the
  structured form.
- `curie schema`: the machine-readable command manifest. **This is the authority
  on what commands exist.** It is hidden from `--help`, and it is real.
- `curie schema-index`: the committed, versioned JSON Schemas for every `--json`
  result, so you can check that a payload field exists before you trust it.

## Ask the human, or do it yourself

**Do it yourself: all non-secret plumbing.** Scaffolding a bundle, bringing a
tier up, deploying, running evals, wiring connectors, tearing down. Do the work
rather than handing the developer a shell checklist.

**Ask the human for the VALUES of secrets and API keys.** Never type a
credential yourself. If a credential already exists in the environment or in the
bundle's own `.env`, use it rather than asking for it again.

**Ask the human to create external applications in a browser**, a Slack app for
example, so its tokens can be minted. You cannot do that part.

**Ask the human before anything that costs money on a real provider**, and
before promoting past a red eval.

## The verification contract

House rule, and the reason it is written down: a command is checked by CI only
when it sits inside backticks or a fenced block in this file. A command written
in bare prose escapes the gate, so every command below is backticked
deliberately, not decoratively.

**Never report success from textual evidence.** A file existing, a string
appearing in output, or a command not erroring is not evidence. Report success
only when a command from this contract exits 0 **and** the stated fact holds in
its `--json` payload.

**After scaffolding a bundle with `curie init`:** `curie skill check --json`
exits 0 and `verdict` is `"green"`. Every server in `declared` has a non-null
`registered` counterpart in `matches` with `connected` true. This runs offline
and needs no credential. A green `--fake-model` run does not cover this: the
fake model never calls MCP tools.

**After `curie skill up`:** `curie skill status --json` exits 0 and reports both
a `url` and a `session`.

**After a deploy, at the tier you deployed to.** Angle-bracket placeholders are
banned in this contract, so all three tiers are written out in full:

- skill tier: `curie skill status --json` then `curie skill eval --json`
- local tier: `curie local status --json` then `curie local eval --json`
- cluster tier: `curie cluster status --json` then `curie cluster eval --json`

At the skill tier the bundle is the session, so there is no separate deploy
<!-- doclint:ignore-line -->
step. `eval` runs the bundle's OWN `evals/cases.json` at every tier. Default
`skill`, `local`, and `cluster` eval execute that suite without ambient durable
agent memory, so a deployed preference cannot change a committed case. When the
<!-- doclint:ignore-line -->
bundle also carries `evals/trajectory.json`, every case is scored from the
observed tool sequence. Skill reads runner tool frames directly. Local and
cluster trigger the deployed bundle through the platform eval plane and read
the exact triggered matrix stream. Deploy the changed bundle before running
`curie local eval --json` or `curie cluster eval --json`. Those two trajectory
runs refuse `--cases` because a local override cannot alter the deployed
bundle.

The trajectory sidecar maps each `case_id` to an `expected` tool sequence, a
`mode` of `exact`, `in_order`, `any_order`, `precision`, or `recall`, and a
`threshold`. The case file and sidecar are packaged in the same immutable
deployed bundle. The deploy receipt's `bundle_sha256` is the parity identity
for local and cluster. A case with no matching spec fails closed with an
explanatory `detail`; it never passes by omission.

Trajectory records a tool request before that tool executes. A denied or failed
request can therefore satisfy tool identity and order. A trajectory green proves
that the expected request sequence was observed. It does not prove the tool was
reachable or that execution succeeded.

**A parity example case is falsifiable only when it goes RED after the specific
capability under test is removed.** Run the same case after removing that
capability and record the RED result. A null agent run is still a useful
vacuousness control, but it is not sufficient on its own because it removes the
whole agent shape rather than the identity of the capability being proved.
Use an executable control through the real consumer path. One valid example is
to keep a tool declared, route it through an approval gate, supply no approval,
and observe that the tool is denied. Other controls are valid when they make
the named capability unavailable through the surface the case exercises.

Keep capability denial and oracle discrimination as separate observations when
one run cannot prove both. For weather, the live ungranted approval gate made
WebFetch unavailable, but the turn awaited approval and eval failed before
trajectory scoring. A separate focused regression completed a turn with
WebSearch but no WebFetch and observed the committed trajectory oracle go RED.
That regression proves a missing WebFetch request is detected. Do not describe
the approval denial run as a trajectory grade. More generally, a turn that does
not complete can fail before graders run, so one RED result may prove only the
earlier failure and not the named grader.

**Success on an eval is `failed` equal to 0 AND `plumbing_ok` equal to 0.**
`plumbing_ok` counts cases that completed on the fake model and were therefore
never graded (ADR-0055), and `passed + failed + plumbing_ok == total`. Each
case also reports `samples`, `passes`, and `policy` so a one-sample miss is
labeled as one draw, not unexplained tier drift. Default is one sample with
majority aggregation; `curie skill eval --samples 3 --json`,
`curie local eval --samples 3 --json`, and
`curie cluster eval --samples 3 --json` raise N on the in-CLI path. A run
that is all `plumbing_ok` is not a pass, it is an ungraded run.

**When a command is uncertain, `curie schema` is the authority.** Do not invoke
a command you have not seen resolve there.

## Do not assume

Do not assume any capability of Curie, including an HTTP API, authentication, an
OpenAPI description, an MCP surface, or hosted documentation, unless it is
listed in this file or resolves in `curie schema`.
