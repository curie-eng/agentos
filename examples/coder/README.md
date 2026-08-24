# Managed repository coder

This bundle is deliberately small: it consumes the platform's managed workspace
and approval-gated publication features without carrying GitHub credentials,
clone scripts, approval policy, or publication orchestration of its own.

## Five-minute cluster path

Install Curie with Slack and the operator-owned GitHub credential, then deploy
this bundle with a repository workspace:

```bash
export CURIE_GITHUB_TOKEN=<operator-token>
export SLACK_APP_TOKEN=xapp-...
export SLACK_BOT_TOKEN=xoxb-...

curie cluster up --set 'api.githubRepoAllowlist[0]=acme-corp/acme-bot'
curie cluster comms --slack
curie cluster deploy --plugin-dir examples/coder \
  --agent acme-dev --env dev --slack-channel C0EXAMPLE1 \
  --workspace
```

Invite the bot to the channel. Mention it in a thread with a focused change and
the root repository URL, for example
`https://github.com/acme-corp/acme-bot`. Steer in that thread if needed, then
ask it to publish. Curie posts the publication approval card in that same
thread. If no earlier message established a repository, a later message may do
so. The requester may approve this publication card; the platform publishes
from outside the sandbox and posts the pull-request URL back to the thread.

The sandbox receives a credential-free checkout at `/workspace`. It never
receives the operator GitHub credential, and publication does not depend on a
synchronous Slack reply.
