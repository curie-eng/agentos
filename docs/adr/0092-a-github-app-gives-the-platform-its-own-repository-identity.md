# 92. A GitHub App gives the platform its own repository identity

Date: 2026-08-02

Status: Accepted

## Context

[ADR 0090](0090-a-reconciler-applies-connectors-so-agent-repos-need-no-cli.md)
said an agent repository should contain only agent logic, and
[ADR 0091](0091-git-flow-resolves-deploy-targets-so-one-repo-serves-many-agents.md)
made git-flow able to route one repository's pushes to several agents. Together
they let the first adopting agent repository delete `.curie-version`,
`provision-curie.yml`, `deploy.yml`
and `deploy-dev.yml` — 464 lines of deploy plumbing.

Deleting them exposed a dependency nobody had had to think about, because it had
always been supplied invisibly.

Today the deploy runs inside GitHub Actions. GitHub hands every workflow an
automatic, short-lived token for its own repository, and the workflow already
has the code checked out. Nobody configured that token. It is why the current
setup appears to need no credential at all:

```
now:  push -> Actions (inside GitHub, free auto-token, code already present)
              -> curie cluster deploy -> pushes the bundle to the API
```

Remove the workflow and the free token goes with it. The platform must fetch the
code itself, from outside GitHub, and every agent repository is private:

```
after: push -> webhook (a doorbell: a commit sha, no code, no credential)
              -> the API clones the repository itself
```

`clone_and_archive` already assumes a credential exists — `settings.github_token`
— and on that agent's cluster that setting has always been empty. This was
invisible because git-flow had never once run there: of 12 versions, **zero**
carry a `commit_sha`. Every deploy came from the workflow path.

So the platform needs a repository identity of its own. The question is which
kind.

## Decision

**The platform authenticates to GitHub as a GitHub App, minting a short-lived
installation token per repository, and falls back to a configured
personal-access token when no App is configured.**

A GitHub App is the mechanism GitHub provides for exactly this situation: a
service, not a person, that needs scoped read access to an organization's
repositories. Concretely it gives us four things a token does not:

- **No human owner.** An installation belongs to the organization. It does not
  stop working when someone leaves, and it is not attributable to one person's
  account.
- **Nothing to rotate.** The App holds a private key; each clone mints a token
  valid for one hour. There is no yearly expiry to remember and no window in
  which a leaked credential stays useful.
- **Repository access is administered in GitHub, not in our values file.**
  Adding the fifth agent repository is a checkbox on the installation, not a
  re-issued credential and a `helm upgrade`.
- **Revocation and audit are first-class.** One page lists what the platform can
  reach and revokes it.

The seam this lands on is one function. `_clone_credential_env` already builds

```python
basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
```

and `x-access-token` is precisely the username a GitHub App installation token
authenticates with. The App changes where `token` comes from, not how it is
used, not where it is allowed to travel, and not the trust model below.

**The trust model does not move.** ADR 0091 already established that
`clone_and_archive` derives `trusted_clone_url` from configuration plus the
`repo_full_name` read from the database, never from the webhook payload, because
a valid HMAC proves only that the sender holds the shared secret. That is
unchanged. What the App adds is a second, narrower scope: an installation token
is minted *for one repository*, so a credential that leaks through some future
bug is useless against any other repository — where a PAT with organization-wide
`Contents: Read` would not be.

**The PAT path stays, as a fallback, and is not deprecated.** Three reasons.
An air-gapped or GitHub-Enterprise install may not have an App. A first-run
operator should be able to prove the flow works before registering one. And an
App is useless for a repository outside the installation, where a PAT still
works. Configuration selects: App credentials present -> App; otherwise the
token; otherwise no credential, which is correct for a public repository.

**Installation discovery is automatic and cached.** The App resolves its own
installation for a repository (`GET /repos/{owner}/{repo}/installation`) rather
than having the operator paste an installation id into values. An operator who
must look up an opaque numeric id to add a repository has not been given the
checkbox this ADR is buying. Tokens are cached until shortly before expiry, so a
push does not cost two extra API calls.

## Consequences

Setting up a new Curie installation gains one step — register and install a
GitHub App — and loses one per agent repository forever after. That is the
trade this makes, and it is the right direction: the cost is paid once by the
platform operator, and the saving is paid to every agent author.

The platform now holds a private key, which is a more sensitive secret than a
read-only PAT: it can mint tokens for every repository in the installation.
It is stored in the same Kubernetes Secret as the other platform credentials
and is never logged, never placed in `argv`, and never written into a cloned
repository's `.git/config` — the same three rules `_clone_credential_env`
already enforces for the PAT.

Two credential paths exist, so a misconfiguration can be ambiguous: an operator
who sets both may not know which is in use. The resolver therefore logs which
path it selected, once, at startup, naming neither secret.

This ADR covers **repository identity only** — how the platform reads code. It
does not change how the webhook is authenticated (still an HMAC shared secret),
and it does not change `require_api_key`. A GitHub App can also carry webhook
delivery and replace the platform API key with per-user identity;
`apps/api/CLAUDE.md` anticipates that. Those are separate decisions with a much
larger blast radius, and folding them in here would make a credential change
into an authentication rewrite.

## Alternatives considered

- **A personal access token, organization-scoped.** Rejected as the durable
  answer, kept as the fallback. It works and it is five minutes of setup, but it
  is owned by a person, expires, must be rotated, and grants access to every
  repository it names for as long as it lives. It is the right thing to *start*
  with and the wrong thing to standardize on.
- **A per-repository deploy key over SSH.** Rejected. `git_allowed_schemes`
  admits only `file://`, `https://` and `http://`, so this needs a transport
  change; and key-per-repository is worse than a PAT on exactly the axis that
  matters here — it does not scale to the fifth agent repository without new
  manual work.
- **Keep a small deploy workflow in the agent repo** — say ten lines calling a
  shared reusable workflow — and go on using the free Actions token. Rejected:
  it reintroduces precisely what ADR 0090 exists to eliminate, an agent
  repository that must know how it is deployed. The 464 lines being deleted here
  also began as something small.
- **Make agent repositories public.** Rejected without qualification.
- **Have the operator paste an installation id.** Rejected: see the discovery
  note above. It converts a checkbox back into a configuration change.
