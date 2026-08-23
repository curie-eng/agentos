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

curie cluster up
curie cluster comms --slack
curie cluster deploy --plugin-dir examples/coder \
  --agent acme-dev --env dev --slack-channel C0EXAMPLE1 \
  --repo acme-corp/acme-bot --workspace
```

Invite the bot to the channel, mention it with a focused change, steer in its
thread if needed, then ask it to publish. Curie posts the publication approval
card in that same thread. The requester may approve this publication card; the
platform publishes from outside the sandbox and posts the pull-request URL back
to the thread.

The sandbox receives a credential-free checkout at `/workspace`. It never
receives the operator GitHub credential, and publication does not depend on a
synchronous Slack reply.
