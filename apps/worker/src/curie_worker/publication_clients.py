"""Worker-authenticated API and GitHub clients for publication recovery."""

from __future__ import annotations

import re
import uuid
from urllib.parse import quote, urlsplit

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
            raise PublicationReconcileError(
                "publication credential response was unusable"
            ) from exc
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

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_base_url: str = "https://api.github.com",
    ) -> None:
        self._client = client
        self._api_base = api_base_url.rstrip("/")

    async def recover_pr_by_head(
        self,
        repo_full_name: str,
        branch: str,
        title: str,
        body: str,
        authorization_header: str,
    ) -> str | None:
        """Adopt a PR, or create it only when its deterministic branch exists."""

        if not authorization_header:
            raise PublicationReconcileError(
                "GitHub deterministic-head recovery requires authorization"
            )
        default_branch = await self._default_branch(
            repo_full_name,
            authorization_header=authorization_header,
        )
        existing = await self._find(
            repo_full_name,
            branch,
            title=title,
            body=body,
            base=default_branch,
            authorization_header=authorization_header,
        )
        if existing is not None:
            return existing

        headers = self._headers(authorization_header)
        repo_api = f"{self._api_base}/repos/{repo_full_name}"
        ref_url = f"{repo_api}/git/ref/heads/{quote(branch, safe='')}"
        try:
            ref_response = await self._client.get(
                ref_url,
                headers=headers,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise PublicationReconcileError(
                "GitHub deterministic branch lookup was unreachable"
            ) from exc
        if ref_response.status_code == 404:
            return None
        if ref_response.status_code != 200:
            raise PublicationReconcileError(
                "GitHub deterministic branch lookup returned HTTP "
                f"{ref_response.status_code}"
            )

        pulls_url = f"{repo_api}/pulls"
        try:
            created = await self._client.post(
                pulls_url,
                headers=headers,
                json={
                    "title": title,
                    "head": branch,
                    "base": default_branch,
                    "body": body,
                },
                follow_redirects=False,
            )
        except httpx.HTTPError:
            created = None
        if created is not None and created.status_code == 201:
            return self._pull_url(
                created,
                repo_full_name,
                branch=branch,
                title=title,
                body=body,
                base=default_branch,
            )

        # A lost POST response or a concurrent reconciler is ambiguous. Query
        # the deterministic head once more before surfacing an error.
        recovered = await self._find(
            repo_full_name,
            branch,
            title=title,
            body=body,
            base=default_branch,
            authorization_header=authorization_header,
        )
        if recovered is not None:
            return recovered
        status = "unreachable" if created is None else f"HTTP {created.status_code}"
        raise PublicationReconcileError(f"GitHub pull request creation returned {status}")

    @staticmethod
    def _headers(authorization_header: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": authorization_header,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "curie-publication-worker",
        }

    async def _default_branch(
        self,
        repo_full_name: str,
        *,
        authorization_header: str,
    ) -> str:
        try:
            response = await self._client.get(
                f"{self._api_base}/repos/{repo_full_name}",
                headers=self._headers(authorization_header),
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise PublicationReconcileError("GitHub repository lookup was unreachable") from exc
        if response.status_code != 200:
            raise PublicationReconcileError(
                f"GitHub repository lookup returned HTTP {response.status_code}"
            )
        try:
            default_branch = response.json()["default_branch"]
        except (KeyError, TypeError, ValueError) as exc:
            raise PublicationReconcileError(
                "GitHub repository response omitted its default branch"
            ) from exc
        if not isinstance(default_branch, str) or not default_branch:
            raise PublicationReconcileError(
                "GitHub repository response carried an invalid default branch"
            )
        return default_branch

    @staticmethod
    def _pull_url(
        response: httpx.Response,
        repo_full_name: str,
        *,
        branch: str,
        title: str,
        body: str,
        base: str,
    ) -> str:
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicationReconcileError("GitHub returned an invalid pull request") from exc
        if not isinstance(payload, dict):
            raise PublicationReconcileError("GitHub returned an invalid pull request")
        url = payload.get("html_url")
        head = payload.get("head")
        base_payload = payload.get("base")
        expected = {
            "title": title,
            "body": body,
            "head_ref": branch,
            "head_repo": repo_full_name,
            "base_ref": base,
            "base_repo": repo_full_name,
        }
        actual = {
            "title": payload.get("title"),
            "body": payload.get("body"),
            "head_ref": head.get("ref") if isinstance(head, dict) else None,
            "head_repo": (
                (head.get("repo") or {}).get("full_name")
                if isinstance(head, dict) and isinstance(head.get("repo"), dict)
                else None
            ),
            "base_ref": (
                base_payload.get("ref") if isinstance(base_payload, dict) else None
            ),
            "base_repo": (
                (base_payload.get("repo") or {}).get("full_name")
                if isinstance(base_payload, dict)
                and isinstance(base_payload.get("repo"), dict)
                else None
            ),
        }
        if actual != expected:
            raise PublicationReconcileError(
                "GitHub pull request does not match the approved publication contract"
            )
        if not isinstance(url, str) or re.fullmatch(
            rf"https://github\.com/{re.escape(repo_full_name)}/pull/[1-9][0-9]*", url
        ) is None:
            raise PublicationReconcileError("GitHub returned an invalid pull request URL")
        return url

    async def _find(
        self,
        repo_full_name: str,
        branch: str,
        *,
        title: str,
        body: str,
        base: str,
        authorization_header: str,
    ) -> str | None:
        owner = repo_full_name.split("/", 1)[0]
        try:
            response = await self._client.get(
                f"{self._api_base}/repos/{repo_full_name}/pulls",
                params={"state": "open", "head": f"{owner}:{branch}"},
                headers=self._headers(authorization_header),
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise PublicationReconcileError(
                "GitHub deterministic-head lookup was unreachable"
            ) from exc
        if response.status_code != 200:
            raise PublicationReconcileError(
                f"GitHub deterministic-head lookup returned HTTP {response.status_code}"
            )
        try:
            rows = response.json()
        except ValueError as exc:
            raise PublicationReconcileError(
                "GitHub deterministic-head lookup returned invalid JSON"
            ) from exc
        if not isinstance(rows, list) or not rows:
            return None
        return self._pull_url(
            httpx.Response(200, json=rows[0]),
            repo_full_name,
            branch=branch,
            title=title,
            body=body,
            base=base,
        )
