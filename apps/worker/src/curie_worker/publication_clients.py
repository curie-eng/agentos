"""Worker-authenticated API and GitHub clients for publication recovery."""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit

import httpx

from .publication_loop import PublicationCredential, PublicationReconcileError


class PublicationCredentialClient:
    """Redeem write auth only for the approved server-derived publication."""

    def __init__(
        self,
        *,
        api_base_url: str,
        worker_token: str,
        client: httpx.AsyncClient,
    ) -> None:
        if not worker_token:
            raise ValueError("publication credentials require internal worker auth")
        self._base = api_base_url.rstrip("/")
        self._headers = {"X-Curie-Worker-Token": worker_token}
        self._client = client

    async def redeem(self, publication_id: uuid.UUID) -> PublicationCredential:
        try:
            response = await self._client.post(
                f"{self._base}/v1/internal/publications/{publication_id}/credential",
                headers=self._headers,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise PublicationReconcileError(
                "publication credential endpoint is unreachable"
            ) from exc
        if response.status_code != 200:
            raise PublicationReconcileError(
                f"publication credential redemption returned HTTP {response.status_code}"
            )
        if "no-store" not in response.headers.get("Cache-Control", "").lower():
            raise PublicationReconcileError(
                "publication credential response omitted Cache-Control: no-store"
            )
        try:
            body = response.json()
            repo = str(body["repo_full_name"])
            clone_url = str(body["clone_url"])
            authorization = str(body["authorization_header"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PublicationReconcileError("publication credential response was unusable") from exc
        parsed = urlsplit(clone_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or clone_url != f"https://github.com/{repo}.git"
        ):
            raise PublicationReconcileError(
                "publication credential response carried a non-canonical clone URL"
            )
        if not authorization or any(char in authorization for char in ("\r", "\n", "\0")):
            raise PublicationReconcileError(
                "publication credential response carried invalid authorization"
            )
        return PublicationCredential(
            clean_clone_url=clone_url,
            authorization_header=authorization,
        )


class GitHubPublicationLookup:
    """Adopt an existing pull request by its deterministic head branch."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def find_pr_by_head(self, repo_full_name: str, branch: str) -> str | None:
        # Public fallback for test doubles and public repositories. Production
        # reconciliation uses the authenticated sibling below.
        return await self._find(repo_full_name, branch, authorization_header=None)

    async def find_authenticated_pr_by_head(
        self,
        repo_full_name: str,
        branch: str,
        authorization_header: str,
    ) -> str | None:
        return await self._find(
            repo_full_name,
            branch,
            authorization_header=authorization_header,
        )

    async def _find(
        self,
        repo_full_name: str,
        branch: str,
        *,
        authorization_header: str | None,
    ) -> str | None:
        owner = repo_full_name.split("/", 1)[0]
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "curie-publication-worker",
        }
        if authorization_header:
            headers["Authorization"] = authorization_header
        response = await self._client.get(
            f"https://api.github.com/repos/{repo_full_name}/pulls",
            params={"state": "open", "head": f"{owner}:{branch}"},
            headers=headers,
            follow_redirects=False,
        )
        if response.status_code != 200:
            raise PublicationReconcileError(
                f"GitHub deterministic-head lookup returned HTTP {response.status_code}"
            )
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            return None
        url = rows[0].get("html_url") if isinstance(rows[0], dict) else None
        if not isinstance(url, str) or not url.startswith(
            f"https://github.com/{repo_full_name}/pull/"
        ):
            raise PublicationReconcileError("GitHub returned an invalid pull request URL")
        return url
