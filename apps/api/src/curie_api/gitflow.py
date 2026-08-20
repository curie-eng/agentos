"""Git-flow engine (J1): turn a pushed commit into a deploy or a promote.

A push to the dev branch builds the plugin bundle at that commit and deploys it
under the dev bot identity; a push to the prod branch promotes the same commit
(reusing the already-built bundle when present) under the prod bot identity. The
bundle is produced by archiving the pushed sha from the repo, so the flow needs
only git-protocol access to the remote (local bare repos in tests) and never the
GitHub API.
"""

import base64
import hashlib
import hmac
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aci_protocol import EvalJob
from plugin_format.deploy_targets import DeployTargetsFile
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from . import bundles, crud, deploy
from .config import Settings
from .evalqueue import EvalQueue, now_iso
from .github_app import credentials_for
from .models import Agent, AgentVersion, Environment
from .repo_full_name import InvalidRepoFullName, repo_url_path
from .schemas import WebhookResult
from .storage import ObjectStore

logger = logging.getLogger(__name__)

_ZERO_SHA = "0" * 40
# A full lowercase-hex git object id: SHA-1 (40) or SHA-256 (64).
# Unanchored on purpose; paired with fullmatch below so a trailing newline
# (which `$` would tolerate) is rejected.
_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _is_valid_sha(sha: str) -> bool:
    """True only for a full lowercase-hex SHA-1 or SHA-256 object id."""

    return bool(_SHA_RE.fullmatch(sha))


class GitFlowError(Exception):
    """The repo could not be fetched or archived at the requested commit."""


class CloneOriginMismatch(GitFlowError):
    """The payload's clone URL is not the registered repository's origin."""


class CommitNotOnBranch(GitFlowError):
    """The pushed commit is not reachable from the payload's deploy branch."""


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of GitHub's X-Hub-Signature-256 over the raw body."""

    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def environment_for_ref(ref: str | None, settings: Settings) -> Environment | None:
    """Map a git ref to an environment, or None if it is not a deploy branch.

    The full ref is matched (refs/heads/<branch>), never just the last path
    segment, so a tag named `main` or a branch `feature/dev` cannot masquerade
    as a deploy branch.
    """

    if not ref:
        return None
    if ref == f"refs/heads/{settings.dev_branch}":
        return Environment.dev
    if ref == f"refs/heads/{settings.prod_branch}":
        return Environment.prod
    return None


_DEFAULT_PORTS = {"https": 443, "http": 80}


def trusted_clone_url(repo_full_name: str, settings: Settings) -> str:
    """The one origin this installation is allowed to clone for that repo.

    Derived from configuration plus the repository binding stored on the agent
    row, never from the webhook payload, so the platform GitHub credential can
    only ever travel to the configured host (#1122).
    """

    return f"{settings.github_clone_base.rstrip('/')}/{repo_url_path(repo_full_name)}.git"


def _origin_key(url: str) -> tuple[str, str, str, str, int | None, str, str, str]:
    """A normalized comparison key for a clone URL.

    Covers every dimension that decides where a request actually goes: scheme,
    user information, host, port (with the scheme's default applied so an
    explicit ``:443`` matches), path modulo a trailing ``.git`` and trailing
    slashes, query, and fragment. User information is carried IN the key rather
    than rejected separately, so any credential in the payload URL mismatches
    automatically against a derived URL that never carries one.

    Keys are compared as WHOLE TUPLES for equality. No element may ever be
    compared with ``startswith`` or a substring test: that is exactly what would
    let ``owner/repo-evil`` pass against a registered ``owner/repo``.
    """

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port
    if port is None:
        port = _DEFAULT_PORTS.get(scheme)
    path = parts.path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    path = path.rstrip("/")
    return (
        scheme,
        parts.username or "",
        parts.password or "",
        parts.hostname or "",
        port,
        path,
        parts.query,
        parts.fragment,
    )


def _origins_match(requested: str, trusted: str) -> bool:
    """Whole-key equality between a requested clone URL and the trusted origin.

    An unparseable URL reads as a mismatch rather than crashing inside the
    threadpool: ``urlsplit().port`` raises ``ValueError`` on a non-numeric port
    and ``urlsplit`` itself raises on a malformed IPv6 literal.
    """

    try:
        return _origin_key(requested) == _origin_key(trusted)
    except ValueError:
        return False


def _clone_credential_env(
    trusted_url: str, settings: Settings, *, repo_full_name: str, credentials: Any = None
) -> dict[str, str]:
    """Git config env that authenticates the clone, or empty if not applicable.

    Private repositories are the norm for a bundle -- it names internal hosts and
    services -- so an unauthenticated clone makes git-flow unusable for most real
    agents (#1058).

    The credential travels as git config supplied through ``GIT_CONFIG_*``
    environment variables rather than embedded in the URL. That keeps it out of
    ``argv`` (so it cannot be read from ``ps`` or leak through a subprocess error
    that echoes the command) and out of the cloned repo's ``.git/config``, which
    URL-embedded credentials are persisted into.

    The URL reaching this function is the origin derived from the stored
    repository binding (``trusted_clone_url``), never the webhook payload's
    ``clone_url``, so the header is scoped to the one host this installation
    deploys from (#1122).
    """

    # Which credential, per ADR-0092: a GitHub App installation token scoped to
    # this one repository when an App is configured, otherwise the PAT. Only the
    # SOURCE of the token varies -- `x-access-token` below is the username both
    # kinds authenticate with, so nothing downstream changes.
    resolver = credentials if credentials is not None else credentials_for(settings)
    token = resolver.token_for(repo_full_name)
    if not token or not trusted_url.startswith("https://"):
        return {}
    host = urlsplit(trusted_url).netloc
    if not host:
        return {}
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def _git_failure_detail(
    exc: BaseException | subprocess.CompletedProcess[bytes],
) -> str:
    """git's stderr, which says *why* -- 'Repository not found' vs a timeout.

    The old message interpolated the exception, whose repr is the argv and an
    exit code: 'returned non-zero exit status 128' told an operator nothing, and
    the actual reason was discarded despite being captured. argv is credential-
    free by construction here (see ``_clone_credential_env``), but the tail is
    bounded anyway so a hostile remote cannot flood the response.
    """

    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    if isinstance(stderr, str) and stderr.strip():
        return stderr.strip()[-400:]
    return str(exc)[:200]


def clone_and_archive(
    clone_url: str,
    sha: str,
    settings: Settings,
    *,
    repo_full_name: str,
    ref: str,
    credentials: Any = None,
) -> bytes:
    """Mirror-clone the repo and return a tar of the tree at ``sha``.

    Refuses clone URLs outside the configured scheme allowlist and restricts git
    to safe transports, so a webhook cannot coerce an arbitrary git command.

    ``clone_url`` is the untrusted webhook payload value: it is compared against
    the origin derived from ``repo_full_name`` (which the caller must have read
    from the agent row) and then discarded. Git is handed the derived origin,
    for both the clone argv and the credential header, so a signed-but-forged
    payload cannot choose where the platform GitHub token travels (#1122).
    """

    if not clone_url.startswith(settings.git_allowed_schemes):
        raise GitFlowError(f"clone url scheme not allowed: {clone_url}")
    if not _is_valid_sha(sha):
        raise GitFlowError(f"invalid commit sha: {sha!r}")

    trusted_url = trusted_clone_url(repo_full_name, settings)
    if not trusted_url.startswith(settings.git_allowed_schemes):
        # The derived URL, not the payload, is what git actually clones. The
        # comparison below is not a substitute for this check: it is safe
        # today only by transitivity, and a misconfigured `github_clone_base`
        # must fail as a deployment configuration error, not be reported as a
        # forged push. Never interpolate the credential here; this URL never
        # carries one (see `_clone_credential_env`), but keep it that way.
        raise GitFlowError(
            f"configured github_clone_base produces a clone url outside the "
            f"allowed schemes {settings.git_allowed_schemes!r}: {trusted_url!r}"
        )
    if not _origins_match(clone_url, trusted_url):
        # Truncated: this lands in the webhook response body and in the warning
        # log, and a forged payload must not be able to flood either.
        raise CloneOriginMismatch(
            f"clone url does not match the registered repository "
            f"{repo_full_name!r}: {clone_url[:200]!r}"
        )

    tmp = tempfile.mkdtemp(prefix="gitflow-")
    env = {
        **os.environ,
        "GIT_ALLOW_PROTOCOL": "file:https:http",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        **_clone_credential_env(
            trusted_url, settings, repo_full_name=repo_full_name, credentials=credentials
        ),
    }
    try:
        try:
            # `git help config` (git 2.43.0): "http.followRedirects: Whether git
            # should follow HTTP redirects. ... If set to false, git will treat
            # all redirects as errors. If set to initial, git will follow
            # redirects only for the initial request to a remote, but not for
            # subsequent follow-up HTTP requests. ... The default is initial."
            # The initial request is precisely the one carrying the extraheader,
            # so the default would let a redirect target receive the credential.
            # `-c` must precede the `clone` subcommand to take effect.
            subprocess.run(
                [
                    "git",
                    "-c",
                    "http.followRedirects=false",
                    "clone",
                    "--quiet",
                    "--mirror",
                    "--",
                    trusted_url,
                    tmp,
                ],
                check=True,
                capture_output=True,
                env=env,
                timeout=120,
            )
            ancestry = subprocess.run(
                ["git", "-C", tmp, "merge-base", "--is-ancestor", sha, ref],
                check=False,
                capture_output=True,
                env=env,
                timeout=120,
            )
            if ancestry.returncode == 1:
                raise CommitNotOnBranch(
                    f"commit {sha[:12]} is not reachable from {ref}"
                )
            if ancestry.returncode != 0:
                raise GitFlowError(
                    f"could not verify commit {sha[:12]} against {ref}: "
                    f"{_git_failure_detail(ancestry)}"
                )
            archived = subprocess.run(
                ["git", "-C", tmp, "archive", "--format=tar", "--", sha],
                check=True,
                capture_output=True,
                env=env,
                timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise GitFlowError(f"could not archive {sha[:12]}: {_git_failure_detail(exc)}") from exc
        return archived.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def log_push_outcome(result: WebhookResult, payload: dict[str, object], *, source: str) -> None:
    """Log a rejected push loudly, whichever lane delivered it (#1066, #1268).

    A rejected push is still acknowledged with 200, because GitHub retries and
    redelivers on a non-2xx and the push is not going to succeed on a retry. The
    cost is that every dashboard reports success: GitHub shows a green delivery,
    the access log shows "POST /github/webhook 200 OK", and no agent, version, or
    deployment appears. The reason exists only in the response body, which
    nothing surfaces.

    That combination made a broken deploy indistinguishable from a working one
    until someone thought to open the delivery payload in GitHub's UI. Logging
    the rejection is what makes it findable from the platform side (#1066).

    It lives here, not in the webhook router, because the POLLING lane needs it
    more and had it less (#1268). A polled push has no HTTP response body to
    carry the errors and no GitHub delivery UI to fall back on -- polling exists
    precisely for clusters GitHub cannot reach. Its only output named the
    outcome "deployed" at INFO and discarded `result.errors` entirely, which is
    #1066 again on the one path with no fallback. Shared rather than copied so
    the two lanes cannot drift.
    """

    if result.status != "rejected":
        return
    repo = payload.get("repository")
    full_name = repo.get("full_name") if isinstance(repo, dict) else None
    codes = [e.get("code", "?") for e in (result.errors or [])]
    logger.warning(
        "%s rejected push: repo=%s ref=%s sha=%s codes=%s errors=%s",
        source,
        full_name,
        payload.get("ref"),
        str(payload.get("after"))[:12],
        ",".join(codes) or "none",
        result.errors,
    )


async def process_push(
    session: AsyncSession,
    store: ObjectStore,
    settings: Settings,
    eval_queue: EvalQueue,
    payload: dict[str, object],
) -> WebhookResult:
    """Deploy (dev) or promote (prod) the pushed commit; ignore other refs."""

    ref = payload.get("ref")
    after = payload.get("after")
    repo = payload.get("repository")
    if not isinstance(ref, str):
        return WebhookResult(status="ignored")
    environment = environment_for_ref(ref, settings)
    if environment is None:
        return WebhookResult(status="ignored")
    if not isinstance(after, str) or after == _ZERO_SHA or not _is_valid_sha(after):
        return WebhookResult(status="ignored")
    if not isinstance(repo, dict):
        return WebhookResult(status="ignored")

    full_name = repo.get("full_name")
    clone_url = repo.get("clone_url") or repo.get("url")
    if not isinstance(full_name, str) or not isinstance(clone_url, str):
        return WebhookResult(status="ignored")

    # Every agent built from this repository (ADR-0091). A repository binds
    # several on purpose: a dev bot and a prod bot are the same bundle on two
    # channels. Which one THIS push deploys to comes from the bundle's
    # deploy.yaml, so the bundle has to be fetched before the agent is known.
    repo_agents = await crud.get_agents_by_repo(session, full_name)
    # The second clause is unreachable in practice (the lookup's predicate is
    # `Agent.repo_full_name == full_name` with a non-None argument); it exists
    # solely to narrow `str | None` to `str` for the origin derivation below.
    repo_agents = [a for a in repo_agents if a.repo_full_name is not None]
    if not repo_agents:
        return WebhookResult(status="ignored")

    # The trust model does not move (ADR-0091). The origin is still derived from
    # what the DATABASE holds, never the payload -- and with several agents it
    # stays unambiguous, because every agent bound to a repository carries the
    # same repo_full_name, so the derived origin is identical whichever row is
    # read. The clone is authorized by the repository binding; the target only
    # decides which agent receives the resulting Version.
    trusted_repo_full_name = str(repo_agents[0].repo_full_name)

    try:
        archive = await run_in_threadpool(
            clone_and_archive,
            clone_url,
            after,
            settings,
            repo_full_name=trusted_repo_full_name,
            ref=ref,
        )
        extension, content_type = deploy.validate_archive(archive, settings)
    except InvalidRepoFullName as exc:
        return WebhookResult(
            status="rejected", errors=[{"code": "git.invalid_repository", "message": str(exc)}]
        )
    except CloneOriginMismatch as exc:
        return WebhookResult(
            status="rejected", errors=[{"code": "git.origin_mismatch", "message": str(exc)}]
        )
    except CommitNotOnBranch as exc:
        return WebhookResult(
            status="rejected",
            errors=[{"code": "git.commit_not_on_branch", "message": str(exc)}],
        )
    except GitFlowError as exc:
        return WebhookResult(
            status="rejected", errors=[{"code": "git.archive_failed", "message": str(exc)}]
        )
    except bundles.UnsupportedArchive as exc:
        return WebhookResult(
            status="rejected", errors=[{"code": "bundle.unsupported", "message": str(exc)}]
        )
    except deploy.BundleInvalid as exc:
        return WebhookResult(status="rejected", errors=exc.errors)

    targets = await run_in_threadpool(_read_targets, archive, settings)
    named = _target_agent_name(targets, environment)
    named_elsewhere = (
        await crud.get_agent_by_name(session, named)
        if named and not any(a.name == named for a in repo_agents)
        else None
    )
    try:
        agent = resolve_target_agent(targets, environment, repo_agents, named_elsewhere)
    except TargetUnresolved as exc:
        return WebhookResult(status="rejected", errors=[{"code": exc.code, "message": str(exc)}])
    if agent is None:
        # A branch this bundle declares no target for. Silently doing nothing is
        # correct and is what an unmatched branch already does.
        return WebhookResult(status="ignored")

    version = await crud.get_version_by_commit(session, agent.id, after)
    # Only a version whose bundle is actually stored may be reused for promote.
    # A row with bundle_ref still None is the residue of a prior attempt that
    # failed after the row committed; rebuild and store into it rather than
    # deploying a bundleless version. This same "new-or-repaired" condition
    # gates the eval fan-out below: a redelivered push for an already-bundled
    # version must not enqueue a second job for the same version.
    bundle_built = version is None or version.bundle_ref is None
    if bundle_built:
        if version is None:
            version = await crud.create_version_row(
                session,
                agent.id,
                version_label=after[:12],
                created_by="git-flow",
                commit_sha=after,
            )
        # Bundle-once, bind-many (ADR-0091). A sibling agent in this repository
        # may already hold this exact commit -- the dev push that ran minutes
        # ago. Reuse its stored object rather than uploading the same bytes
        # again: prod then promotes not merely an identical artifact but the
        # SAME one, which is what makes "promote what you validated" a property
        # of the schema rather than of discipline.
        sibling = await _sibling_bundle(session, repo_agents, agent.id, after)
        if sibling is not None:
            version = await crud.attach_bundle(
                session, version, str(sibling.bundle_ref), str(sibling.bundle_sha256)
            )
        else:
            await deploy.store_bundle(
                store, session, agent.id, version, archive, extension, content_type
            )

    # Either the version pre-existed with a bundle, or the block above created
    # or repaired it; it is non-None from here on.
    assert version is not None

    # A prod promote (bundle_built is False) reuses a bundle that may have been
    # stored before the current size/ratio caps existed, or under looser ones --
    # revalidate it here (ADR-0059 decision 3's backward-compat commitment). The
    # bundle_built branch above already ran these bytes through
    # `deploy.validate_archive` under the current caps moments ago, so re-fetching
    # and rechecking the identical bytes here would be redundant.
    if not bundle_built:
        try:
            await deploy.revalidate_stored_bundle(store, version, settings)
        except deploy.BundleTooLarge as exc:
            return WebhookResult(
                status="rejected",
                errors=[{"code": "bundle.too_large", "message": str(exc)}],
            )

    deployment = await crud.create_deployment_row(
        session,
        agent.id,
        version.id,
        environment,
        commit_sha=after,
    )

    # Fan out the eval run for a dev deploy (eval-as-CI); prod promote does not.
    # Only when this delivery actually built the bundle, so a redelivered push
    # for an already-bundled version does not spawn a duplicate eval job.
    if environment is Environment.dev and bundle_built:
        await eval_queue.enqueue(
            EvalJob(
                agent_id=agent.id,
                version_id=version.id,
                sha=after,
                suite=settings.eval_default_suite,
                bundle_ref=version.bundle_ref,
                requested_at=now_iso(),
            )
        )

    return WebhookResult(
        status="promoted" if environment is Environment.prod else "deployed",
        environment=environment,
        agent_id=agent.id,
        version_id=version.id,
        deployment_id=deployment.id,
        commit_sha=after,
    )


class TargetUnresolved(Exception):
    """A push cannot be routed to an agent, with an operator-facing reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_target_agent(
    targets: "DeployTargetsFile | None",
    environment: Environment,
    repo_agents: "Sequence[Agent]",
    named_elsewhere: "Agent | None",
) -> "Agent | None":
    """Which agent this push deploys to (ADR-0091).

    Returns None when the push should be ignored -- a branch with no matching
    target, exactly as an unmatched branch is ignored today. Raises
    ``TargetUnresolved`` when it should be REJECTED, which is a different thing:
    the author declared a target and it cannot be honoured, so silence would
    look like a deploy that worked.

    ``named_elsewhere`` is the agent the target names IF it exists but is bound
    to a different repository. That case is the sharpest edge in ADR-0091 and
    the reason this function takes an argument for it: without the check, one
    repository's push deploys over another repository's agent. It is refused.

    What decides the fallback is whether any target is DECLARED, not whether a
    ``deploy.yaml`` exists: a bundle with no such file and a bundle whose
    ``targets:`` map is empty say exactly the same thing about routing (#1210).
    """

    if targets is None or not targets.targets:
        # An empty map is what `curie init` scaffolds, so it is the ordinary
        # case rather than the legacy one (#1210). With several agents bound,
        # nothing says which, and guessing deploys to the wrong bot silently.
        if len(repo_agents) == 1:
            return repo_agents[0]
        missing = (
            "the bundle has no deploy.yaml"
            if targets is None
            else "the bundle's deploy.yaml declares an empty `targets:` map"
        )
        raise TargetUnresolved(
            "deploy.no_targets",
            f"{len(repo_agents)} agents are built from this repository but "
            f"{missing}, so there is nothing to say which one this branch "
            "deploys to. Declare a target (ADR-0089).",
        )

    matching = [
        (key, target)
        for key, target in targets.targets.items()
        if target.env == environment.value
    ]
    agent_names: list[str] = []
    for key, target in matching:
        if target.agent is None:
            raise TargetUnresolved(
                "deploy.missing_agent",
                f"targets.{key}: agent is required for every declared target",
            )
        agent_names.append(target.agent)
    if not matching:
        # Not an error: a repository may deploy only prod from main and leave
        # dev to the CLI. Ignoring matches how an unmatched branch behaves.
        return None
    if len(matching) > 1:
        raise TargetUnresolved(
            "deploy.ambiguous_env",
            f"deploy.yaml declares {len(matching)} targets with env "
            f"{environment.value!r} ({', '.join(sorted(agent_names))}); "
            "one branch cannot deploy to two agents in one push.",
        )

    wanted = agent_names[0]
    for agent in repo_agents:
        if agent.name == wanted:
            return agent

    if named_elsewhere is not None:
        # One repository's push would otherwise deploy over another's agent.
        raise TargetUnresolved(
            "deploy.agent_bound_elsewhere",
            f"deploy.yaml names agent {wanted!r}, which is built from a "
            "different repository. Refusing: a push must not deploy over an "
            "agent another repository owns.",
        )
    raise TargetUnresolved(
        "deploy.unknown_agent",
        f"deploy.yaml names agent {wanted!r}, which does not exist. Create it "
        "once (`curie cluster deploy`, or the console) so the push has "
        "something to deploy to; a webhook does not mint agents.",
    )


def _read_targets(archive: bytes, settings: Settings) -> DeployTargetsFile | None:
    """Read ``deploy.yaml`` out of an already-validated archive.

    Extracted to a temp dir because that is the only way to read one file out
    of the bundle, and run in a threadpool by the caller since it is blocking.
    """

    with tempfile.TemporaryDirectory() as tmp:
        bundles.extract_and_validate(
            archive,
            Path(tmp),
            max_uncompressed_bytes=settings.bundle_max_uncompressed_bytes,
            max_compression_ratio=settings.bundle_max_compression_ratio,
            max_members=settings.bundle_max_members,
        )
        return bundles.read_deploy_targets(Path(tmp))


def _target_agent_name(targets: DeployTargetsFile | None, environment: Environment) -> str | None:
    """The agent name this environment's target names, if exactly one does."""

    if targets is None:
        return None
    matching = [t for t in targets.targets.values() if t.env == environment.value]
    return matching[0].agent if len(matching) == 1 else None


async def _sibling_bundle(
    session: AsyncSession,
    repo_agents: "Sequence[Agent]",
    agent_id: uuid.UUID,
    commit_sha: str,
) -> "AgentVersion | None":
    """A version another agent of this repo already stored for this commit."""

    for sibling in repo_agents:
        if sibling.id == agent_id:
            continue
        existing = await crud.get_version_by_commit(session, sibling.id, commit_sha)
        if existing is not None and existing.bundle_ref:
            return existing
    return None
