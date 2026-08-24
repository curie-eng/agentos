"""Operator authorization policy for runtime repository workspaces."""

from __future__ import annotations

import re

REPOSITORY_FULL_NAME_PATTERN = (
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9_-])?$"
)
_REPO_FULL_NAME = re.compile(REPOSITORY_FULL_NAME_PATTERN)
_OWNER_WILDCARD = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/\*$")


def valid_repository_name(repo_full_name: str) -> bool:
    """Accept exactly one canonical GitHub owner/repository selector."""

    return bool(_REPO_FULL_NAME.fullmatch(repo_full_name))


def valid_allowlist_entry(entry: str) -> bool:
    return bool(valid_repository_name(entry) or _OWNER_WILDCARD.fullmatch(entry))


def repository_is_allowed(repo_full_name: str, allowlist: tuple[str, ...]) -> bool:
    """Match exact owner/repository or owner-wide owner/* entries."""

    repo = repo_full_name.casefold()
    owner = repo.split("/", 1)[0]
    return any(
        entry.casefold() == repo or entry.casefold() == f"{owner}/*"
        for entry in allowlist
    )


def credential_mode(*, app_id: str, app_private_key: str, token: str) -> str:
    if app_id and app_private_key:
        return "github_app"
    if token:
        return "raw_token_fallback"
    return "anonymous"
