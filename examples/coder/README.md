# Built-in coding tools consumer

This deliberately skill-less bundle demonstrates Curie's built-in coding tools.
Every session receives the Claude Code file tools and
`mcp__curie__publish_changes`; the bundle does not carry a coder skill, GitHub
credentials, clone scripts, approval policy, or publication orchestration.

## Five-minute cluster path

Install Curie with Slack and the operator-owned GitHub credential, then deploy
this bundle. The retained `--workspace` and `--no-workspace` flags are deprecated
compatibility no-ops and are not needed:

```bash
export CURIE_GITHUB_TOKEN=<operator-token>
export SLACK_APP_TOKEN=xapp-...
export SLACK_BOT_TOKEN=xoxb-...

curie cluster up --set 'api.githubRepoAllowlist[0]=acme-corp/acme-bot'
curie cluster comms --slack
curie cluster deploy --plugin-dir examples/coder \
  --agent acme-dev --env dev --slack-channel C0EXAMPLE1
```

The chart default `api.githubRepoAllowlist: []` denies every runtime selection.
`curie cluster deploy --workspace` warns when that list is empty. The retained
`--workspace` flag is a deprecated compatibility no-op; the allowlist is the
real control. `owner/*` allows every repository under that owner.

Invite the bot to the channel. In the opening message, include the single
allowed root repository URL, for example
`https://github.com/acme-corp/acme-bot`, together with a focused change. Curie
acquires that repository when it claims the sandbox and mounts the
credential-free checkout at `/workspace`. A message without a usable root URL
runs without a managed checkout; adding one after that sandbox is already
running does not remount it.

When the change is ready, ask the agent to publish. The built-in publication
tool posts an approval card in the same thread, ends the turn while approval is
pending, and never pushes from the sandbox. The requester may approve the card;
the platform publishes from outside the sandbox and posts the pull-request URL
back to the thread. The sandbox never receives the operator GitHub credential,
and publication does not depend on a synchronous Slack reply.

## Evals

`evals/cases.json` grades this skill-less bundle the same way at every tier.
Coding tools are a built-in session surface (ruling #2154), so the cases exercise
that runtime: the publication tool is named, git must not push, publication
without a managed workspace refuses, and file tools inspect /workspace.
They do not treat a coder skill or an SRE-bot merge as the capability.

With a live-model runner up from this directory, run:

```bash
curie skill eval
```

The cases are written to be falsifiable: a null agent and an input-parrot both
go red, and no expected token is present in its case's input. The
no-mount publication case grades a generic session; a cluster turn that already
acquired an allowlisted checkout takes the approval-card path instead.
