# 93. Local-model assets are pre-provisioned, never implicitly downloaded

Date: 2026-08-03

Status: Accepted

Realized by `preflight_local_model` in `cli/src/docker.rs`, which both tiers call
from `cli/src/local.rs` before anything is brought up, and by the opt-in fetch
flag that keeps the old behaviour available for anyone who wants it.

## Context

Issue #1183 reported that `curie local up --local-model` hangs. It does not. It
downloads **~11.4 GB** and says nothing while it does.

Two separate fetches, at two different layers, neither of them announced:

| what | size | who fetches it | where it lands |
|---|---|---|---|
| `ollama/ollama:0.24.0` image | **8.93 GB** | the host docker daemon, as a `compose up` side effect | the image cache |
| `qwen3:4b` weights (the `--local-model` default) | **2.5 GB** | the `ollama-pull` one-shot container | the `ollama_data` volume |

Measured end to end on macOS arm64 over a ~30 MB/s link:

| state | wall clock | bytes curie printed |
|---|---|---|
| image cold, model cold | **232s** | **0** |
| image warm, model cold | 137s | 0 |
| both warm | **17.9s** | normal |

Extrapolated to a 50 Mbit/s home link the cold case is roughly half an hour. The
only thing on screen for its entire duration is a spinner reading
`starting dev stack`, because `run_step` captures the subprocess with
`.output()` and replays it afterwards through `ui.plumbing`, a no-op without
`--debug`. There is also no upper bound: `--wait-timeout` was measured at 15s
against the 8.93 GB pull and the command still ran **135s and exited 0**,
because that flag covers only compose's readiness wait, not the pull that
precedes it.

The reporter's "relevant output or logs" block is empty. That is not an omission
on their part — there was nothing to paste. A user cannot distinguish this from
a genuine wedge, and during the investigation a real wedge (a hung
`docker-credential-desktop`) produced a byte-for-byte identical screen while
never returning at all.

The parity ladder is inconsistent about this today. `skill up --local-model`
bounds its readiness wait at 120s and errors clearly, but its `docker run` and
`pull_model` fetch the same two assets just as implicitly and just as silently.
`cluster up` never faces the question: it does not run Ollama.

So the shape of the problem is not "the wait needs a progress bar." It is that a
single short command **acquires 11.4 GB of infrastructure as a side effect of
being run**, and the same command therefore takes 18 seconds on one machine and
half an hour on the next. For a project whose entire proposition is that the
same bundle behaves the same way across three tiers, an operator verb whose cost
varies by two orders of magnitude with hidden machine state is the wrong
default, independent of how prettily the wait is rendered.

## Decision

**`--local-model` requires its assets to already be present. It never downloads
them implicitly, and it says exactly what is missing when they are not.**

Before the stack is brought up, both tiers preflight:

1. the Ollama image is present locally, and
2. the requested model is present in the tier's Ollama data volume.

If either is missing the command **fails before bringing anything up**, naming
each missing asset, its download size, and the one command that fetches it.
Fetching is available but must be asked for explicitly, by a flag on the same
verb. When both are present the command proceeds exactly as it does today — the
18-second path is unchanged.

The check lives in the CLI, ahead of the `docker` / `docker compose`
invocation, and is strictly offline. It escalates in cost only as far as it has
to: `docker image inspect` (measured at 32ms) settles the image, `docker volume
inspect` settles a data volume that does not exist yet, and only a machine that
has both pays for the one probe that has to read the volume's contents — a
throwaway container off the Ollama image just established to be present, ~2s. No
step reaches the network, and the first failing step short-circuits the rest.

That last probe is a container, so "before the stack is brought up" is meant
literally rather than "before any container runs at all". The distinction is
deliberate: the property being bought is that nothing is **downloaded** and no
service is **started** before the operator has consented, not that the check is
free.

**Both tiers answer the same way.** `skill up --local-model` and `local up
--local-model` preflight identically and fail identically, per
[ADR 0041](0041-every-verb-answers-at-every-tier.md). `cluster` is unaffected;
it has no Ollama to provision.

## Consequences

**`--local-model` stops being a one-command first run.** A new user on a clean
machine now gets an error and has to issue a second command. This is the cost,
and it is accepted deliberately: an error that names an 8.9 GB download is
strictly better information than a spinner that does not, and the second command
is the point at which the user consents to the download rather than discovering
it.

**The failure is the feature.** The preflight message is the only place the
11.4 GB is ever disclosed before it is spent, so its wording is load-bearing, not
decoration. It must name each missing asset separately (image and model fail
independently), state each size, and give a runnable command.

**The remaining implicit downloads are out of scope and still silent.** A first
`curie local up` with no `--local-model` still pulls roughly nine other images
with no progress and no bound, and any of them can wedge forever. This ADR
narrows the problem to its dominant case; it does not close the class. Bounding
operator verbs in wall clock, and streaming their subprocess output, are
separate decisions.

**One entry point is preserved.** The remediation the error prints is a `curie`
command, never a raw `docker pull`, per the repository's single-surface rule.

**Prior behavior is recoverable, not removed.** The explicit fetch flag keeps
the old outcome available for anyone who wants it, including CI that provisions
a fresh machine on purpose.

## Alternatives considered

**Stream the download progress and keep the implicit fetch.** Show the pull
percentage on the spinner and let the 11.4 GB proceed. This makes the wait
legible but leaves the actual defect: the command still costs two orders of
magnitude more on one machine than another, still with no consent. Progress
reporting is worth doing on its own merits — every operator verb needs it — but
as the answer to #1183 it treats the symptom.

**Pass `--wait-timeout` and fail on expiry.** Measured and rejected on evidence:
`--wait-timeout 15` against the cold image pull still ran 135s and exited 0,
because compose applies that budget to the readiness wait only. It cannot bound
the phase where the time actually goes, so as a fix it is inert.

**Warn up front, then download anyway.** Print "this will fetch ~11.4 GB" and
continue. Better than silence, but a warning nobody can act on is just a slower
way to commit the user's disk and bandwidth, and it still leaves the same command
meaning two very different things on two machines.

**Ship a smaller default model.** Reduces the 2.5 GB but not the 8.93 GB image,
which is the larger half and the one whose phase has no observable container at
all. It also trades away model quality to work around a disclosure problem.

**Target a host-installed Ollama instead of containerizing it.** Would skip both
downloads for the many operators who already run Ollama, and is genuinely
attractive — but it is a feature with its own egress, reproducibility and
ladder-asymmetry questions, tracked separately as #1233. It changes where the
assets come from; it does not change whether a command may spend 11.4 GB without
asking. The two are independent and this one lands first.
