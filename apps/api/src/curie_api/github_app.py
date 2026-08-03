"""The platform's own GitHub identity (ADR-0092).

Answers one question: *what credential may this installation use to read
repository X?* Everything about how that credential is then applied — the
`x-access-token` basic header, keeping it out of `argv` and out of
`.git/config` — stays in `gitflow._clone_credential_env`, unchanged.

Two paths, in order:

1. **A GitHub App.** The platform holds a private key and mints a token scoped
   to ONE repository, valid for an hour. Nothing to rotate, no human owner, and
   a leak is useless against any other repository.
2. **A personal access token.** The fallback, and deliberately not deprecated:
   a GitHub Enterprise or air-gapped install may have no App, and a first-run
   operator should be able to prove the flow before registering one.

Neither configured is not an error. A public repository clones with no
credential at all, and failing at startup would make the platform refuse to boot
for installations that never deploy from a private repo.

The installation is DISCOVERED, not configured. An operator who has to look up
an opaque numeric id to onboard a repository has not been given the "tick a box
in GitHub" that ADR-0092 is buying.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

from .config import Settings

logger = logging.getLogger(__name__)

# GitHub rejects an App JWT with an `iat` in its future, and small clock skew
# between us and GitHub is normal. Backdating by a minute is the documented
# remedy. The `exp` stays well inside GitHub's 10-minute ceiling.
_JWT_BACKDATE_SECONDS = 60
_JWT_LIFETIME_SECONDS = 480

# Re-mint this long before a token actually expires, so a clone that starts just
# under the wire does not have its credential expire mid-transfer.
_TOKEN_REFRESH_MARGIN_SECONDS = 300


# Resolvers are shared per credential configuration, because the token cache
# lives on the resolver. Constructing one per webhook -- the obvious thing --
# would mean two extra GitHub calls on every single push, and the cache would
# never once be hit. Keyed on the fields that determine identity, so a test
# passing its own Settings gets its own resolver rather than a poisoned one.
_RESOLVERS: dict[tuple[str, str, str, str], GitHubCredentials] = {}


def credentials_for(settings: Settings) -> GitHubCredentials:
    """The shared resolver for this credential configuration."""

    key = (
        settings.github_app_id,
        settings.github_app_private_key,
        settings.github_token,
        settings.github_api_url,
    )
    existing = _RESOLVERS.get(key)
    if existing is None:
        existing = GitHubCredentials(settings=settings)
        _RESOLVERS[key] = existing
    return existing


class GitHubAppError(RuntimeError):
    """The App is configured but could not produce a token."""


@dataclass
class _CachedToken:
    token: str
    expires_at: float

    def usable(self, now: float) -> bool:
        return bool(self.token) and now < self.expires_at - _TOKEN_REFRESH_MARGIN_SECONDS


@dataclass
class GitHubCredentials:
    """Resolves a read credential for one repository.

    Holds a token cache keyed by repository, so a burst of pushes to one repo
    costs one token exchange rather than one per push.
    """

    settings: Settings
    _tokens: dict[str, _CachedToken] = field(default_factory=dict)
    _installations: dict[str, int] = field(default_factory=dict)

    @property
    def app_configured(self) -> bool:
        return bool(self.settings.github_app_id and self.settings.github_app_private_key)

    def describe(self) -> str:
        """Which path is in use, for one startup log line. Names no secret."""

        if self.app_configured:
            return f"github app (app_id={self.settings.github_app_id})"
        if self.settings.github_token:
            return "personal access token"
        return "none (public repositories only)"

    def token_for(self, repo_full_name: str) -> str:
        """A credential able to read ``owner/repo``, or "" if none is configured.

        Returning "" rather than raising is deliberate: a public repository
        needs no credential, and `_clone_credential_env` already treats an empty
        token as "send no Authorization header".
        """

        if not self.app_configured:
            return self.settings.github_token

        now = time.time()
        cached = self._tokens.get(repo_full_name)
        if cached is not None and cached.usable(now):
            return cached.token

        token, expires_at = self._mint_installation_token(repo_full_name)
        self._tokens[repo_full_name] = _CachedToken(token=token, expires_at=expires_at)
        return token

    # -- GitHub App plumbing -------------------------------------------------

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - _JWT_BACKDATE_SECONDS,
            "exp": now + _JWT_LIFETIME_SECONDS,
            "iss": self.settings.github_app_id,
        }
        try:
            return jwt.encode(payload, self.settings.github_app_private_key, algorithm="RS256")
        except Exception as exc:  # a malformed key is an operator error, not a runtime one
            raise GitHubAppError(
                "could not sign a GitHub App JWT; check that github_app_private_key "
                f"is the App's full PEM private key: {type(exc).__name__}"
            ) from exc

    def _installation_id(self, repo_full_name: str) -> int:
        """The App's installation on this repository, discovered and cached."""

        known = self._installations.get(repo_full_name)
        if known is not None:
            return known

        url = f"{self.settings.github_api_url.rstrip('/')}/repos/{repo_full_name}/installation"
        data = self._get(url)
        installation_id = data.get("id")
        if not isinstance(installation_id, int):
            raise GitHubAppError(f"GitHub did not report an installation id for {repo_full_name!r}")
        self._installations[repo_full_name] = installation_id
        return installation_id

    def _mint_installation_token(self, repo_full_name: str) -> tuple[str, float]:
        installation_id = self._installation_id(repo_full_name)
        url = (
            f"{self.settings.github_api_url.rstrip('/')}"
            f"/app/installations/{installation_id}/access_tokens"
        )
        # Scoped to this one repository even though the installation may cover
        # more. A credential that leaks is then useless against the others --
        # the narrowing ADR-0092 buys over an org-wide PAT.
        owner, _, repo = repo_full_name.partition("/")
        data = self._post(url, {"repositories": [repo]} if repo else None)

        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise GitHubAppError(f"GitHub returned no installation token for {repo_full_name!r}")
        return token, self._expiry_seconds(data.get("expires_at"))

    @staticmethod
    def _expiry_seconds(raw: Any) -> float:
        """When the token dies, as a monotonic-ish epoch second.

        GitHub states an ISO timestamp. If it is missing or unparseable, assume
        the documented one hour rather than treating the token as immortal --
        caching a dead token would fail every clone until restart.
        """

        from datetime import datetime

        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                logger.warning("unparseable GitHub token expiry; assuming one hour")
        return time.time() + 3600

    # -- HTTP ----------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._app_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, url: str) -> dict[str, Any]:
        return self._request("GET", url, None)

    def _post(self, url: str, body: dict[str, Any] | None) -> dict[str, Any]:
        return self._request("POST", url, body)

    def _request(self, method: str, url: str, body: dict[str, Any] | None) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.settings.github_app_timeout_seconds) as client:
                response = client.request(method, url, json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise GitHubAppError(f"could not reach GitHub at {url}: {exc}") from exc

        if response.status_code == 404:
            # The single most likely operator mistake, and the one whose default
            # message ("Not Found") explains nothing.
            raise GitHubAppError(
                f"GitHub returned 404 for {url}. Usually this means the App is not "
                "installed on that repository -- install it, or add the repository "
                "to the existing installation."
            )
        if response.status_code >= 400:
            raise GitHubAppError(f"GitHub returned {response.status_code} for {url}")

        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubAppError(f"GitHub returned an unexpected body for {url}")
        return payload
