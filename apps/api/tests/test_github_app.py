"""The platform's GitHub identity (ADR-0092).

The happy path is two HTTP calls. What is worth asserting is the selection
between credential paths, the scoping that makes an App token safer than a PAT,
and the caching -- because a cache bug here means either a token exchange on
every push or a dead token served until restart.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from curie_api.config import Settings
from curie_api.github_app import GitHubAppError, GitHubCredentials

REPO = "octo/agent-bot"

# Captured before any patching. `curie_api.github_app.httpx` is the SAME module
# object as this module's `httpx`, so patching `.Client` there patches it here
# too -- a replacement that calls `httpx.Client(...)` would call itself.
_REAL_CLIENT = httpx.Client


def serve(handler: Any) -> Any:
    """A replacement `httpx.Client` that answers from `handler`."""

    return lambda *a, **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler))


@pytest.fixture(scope="module")
def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def app_settings(private_key: str, **over: Any) -> Settings:
    return Settings(github_app_id="12345", github_app_private_key=private_key, **over)


class Recorder:
    """Stands in for GitHub. Records what we sent so scoping can be asserted."""

    def __init__(self, expires_at: str = "2999-01-01T00:00:00Z"):
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.minted = 0
        self._expires_at = expires_at

    def handle(self, request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, str(request.url), body))
        if request.url.path.endswith("/installation"):
            return httpx.Response(200, json={"id": 4242})
        if request.url.path.endswith("/access_tokens"):
            self.minted += 1
            return httpx.Response(
                201, json={"token": f"ghs_minted_{self.minted}", "expires_at": self._expires_at}
            )
        return httpx.Response(404, json={"message": "Not Found"})


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr("curie_api.github_app.httpx.Client", serve(recorder.handle))
    return recorder


# --------------------------------------------------------------------------- #
# Which credential
# --------------------------------------------------------------------------- #
def test_the_app_is_preferred_over_a_token(private_key: str, github: Recorder) -> None:
    creds = GitHubCredentials(settings=app_settings(private_key, github_token="ghp_the_pat"))
    assert creds.token_for(REPO) == "ghs_minted_1"


def test_the_token_is_used_when_no_app_is_configured(github: Recorder) -> None:
    creds = GitHubCredentials(settings=Settings(github_token="ghp_the_pat"))
    assert creds.token_for(REPO) == "ghp_the_pat"
    assert github.calls == [], "no App configured means GitHub is never called"


def test_no_credential_configured_is_not_an_error() -> None:
    # A public repository clones with no header at all, and refusing to boot
    # would break every installation that never deploys from a private repo.
    creds = GitHubCredentials(settings=Settings(github_token=""))
    assert creds.token_for(REPO) == ""


def test_a_half_configured_app_falls_back_rather_than_breaking(github: Recorder) -> None:
    # An id with no key cannot sign anything. Falling back beats failing every
    # clone with a signing error.
    creds = GitHubCredentials(
        settings=Settings(github_app_id="12345", github_app_private_key="", github_token="ghp_x")
    )
    assert creds.token_for(REPO) == "ghp_x"


@pytest.mark.parametrize(
    ("settings_kwargs", "expected"),
    [
        ({"github_token": "ghp_x"}, "personal access token"),
        ({}, "none (public repositories only)"),
    ],
)
def test_describe_names_the_path_and_never_a_secret(
    settings_kwargs: dict[str, Any], expected: str
) -> None:
    creds = GitHubCredentials(settings=Settings(**settings_kwargs))
    assert creds.describe() == expected


def test_describe_does_not_leak_the_private_key(private_key: str) -> None:
    described = GitHubCredentials(settings=app_settings(private_key)).describe()
    assert "PRIVATE KEY" not in described
    assert private_key[:40] not in described


# --------------------------------------------------------------------------- #
# Scoping -- the reason an App beats an org-wide PAT
# --------------------------------------------------------------------------- #
def test_the_minted_token_is_scoped_to_the_one_repository(
    private_key: str, github: Recorder
) -> None:
    # An installation may cover the whole org. Narrowing to the repository being
    # cloned is what makes a leaked token useless against the others.
    GitHubCredentials(settings=app_settings(private_key)).token_for(REPO)
    mint = [c for c in github.calls if c[1].endswith("/access_tokens")][0]
    assert mint[2] == {"repositories": ["agent-bot"]}


def test_the_installation_is_discovered_not_configured(private_key: str, github: Recorder) -> None:
    # No installation id in settings anywhere: onboarding a repo is a checkbox
    # in GitHub, not a values change.
    GitHubCredentials(settings=app_settings(private_key)).token_for(REPO)
    assert any(c[1].endswith(f"/repos/{REPO}/installation") for c in github.calls)
    assert "github_app_installation_id" not in Settings.model_fields


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #
def test_a_second_push_reuses_the_cached_token(private_key: str, github: Recorder) -> None:
    creds = GitHubCredentials(settings=app_settings(private_key))
    first, second = creds.token_for(REPO), creds.token_for(REPO)
    assert first == second
    assert github.minted == 1, "a burst of pushes must not cost a token exchange each"


def test_a_near_expiry_token_is_reminted(private_key: str, monkeypatch) -> None:
    """A token inside the refresh margin is re-minted before it can expire.

    This used `1971-01-01`, already long past, so it proved only that an
    EXPIRED token is replaced -- deleting the 300s margin entirely left it
    green (#1263). The margin exists for a token that is still valid now and
    will not be by the time a clone finishes, so the expiry has to sit in the
    future and inside the margin for the assertion to mean anything.
    """

    from curie_api.github_app import _TOKEN_REFRESH_MARGIN_SECONDS

    soon = datetime.now(UTC) + timedelta(seconds=_TOKEN_REFRESH_MARGIN_SECONDS / 2)
    recorder = Recorder(expires_at=soon.strftime("%Y-%m-%dT%H:%M:%SZ"))
    monkeypatch.setattr("curie_api.github_app.httpx.Client", serve(recorder.handle))
    creds = GitHubCredentials(settings=app_settings(private_key))
    creds.token_for(REPO)
    creds.token_for(REPO)
    assert recorder.minted == 2


def test_a_token_comfortably_outside_the_margin_is_reused(private_key: str, monkeypatch) -> None:
    """The other side of the margin: still-fresh tokens are NOT re-minted.

    Without this, "re-mint whenever asked" also passes the test above, and the
    cache stops being a cache.
    """

    from curie_api.github_app import _TOKEN_REFRESH_MARGIN_SECONDS

    later = datetime.now(UTC) + timedelta(seconds=_TOKEN_REFRESH_MARGIN_SECONDS * 4)
    recorder = Recorder(expires_at=later.strftime("%Y-%m-%dT%H:%M:%SZ"))
    monkeypatch.setattr("curie_api.github_app.httpx.Client", serve(recorder.handle))
    creds = GitHubCredentials(settings=app_settings(private_key))
    creds.token_for(REPO)
    creds.token_for(REPO)
    assert recorder.minted == 1


def test_an_unparseable_expiry_is_not_treated_as_immortal(private_key: str, monkeypatch) -> None:
    """A garbled expiry must fall back to an hour, not to forever.

    Caching a token as never-expiring fails every clone after it dies, until
    someone restarts the pod -- and the log says "authentication", not
    "expired". Treating it as immortal survived the suite (#1263).
    """

    recorder = Recorder(expires_at="not-a-timestamp")
    monkeypatch.setattr("curie_api.github_app.httpx.Client", serve(recorder.handle))
    creds = GitHubCredentials(settings=app_settings(private_key))
    creds.token_for(REPO)
    cached = creds._tokens[REPO]
    assumed = cached.expires_at - time.time()
    assert 3000 < assumed < 4200, f"expected the documented ~1h fallback, got {assumed:.0f}s"


# --------------------------------------------------------------------------- #
# The JWT itself -- GitHub rejects a malformed one with a bare 401
# --------------------------------------------------------------------------- #
def _decode(token: str, private_key: str) -> dict[str, Any]:
    """Verify and decode, the way GitHub does."""
    from cryptography.hazmat.primitives import serialization as ser

    public = ser.load_pem_private_key(private_key.encode(), password=None).public_key()
    return jwt.decode(token, public, algorithms=["RS256"], options={"verify_aud": False})


def test_the_jwt_names_this_app_as_the_issuer(private_key: str) -> None:
    # `iss` is how GitHub knows which App is calling. Replacing it with "0"
    # survived every test (#1263); the symptom is a 401 naming nothing.
    creds = GitHubCredentials(settings=app_settings(private_key))
    assert _decode(creds._app_jwt(), private_key)["iss"] == "12345"


def test_the_jwt_is_backdated_and_not_yet_expired(private_key: str) -> None:
    """`iat` in the past, `exp` in the future, both within GitHub's bounds.

    GitHub rejects a JWT whose `iat` is in its future -- ordinary clock skew
    does it -- and one whose `exp` is more than 10 minutes out. Pushing `iat`
    600s forward, or setting `exp` already-past, both survived (#1263), and
    both produce a 401 that says nothing about time.
    """

    creds = GitHubCredentials(settings=app_settings(private_key))
    now = time.time()
    claims = _decode(creds._app_jwt(), private_key)
    assert claims["iat"] < now, "iat must be backdated for clock skew"
    assert claims["exp"] > now, "exp must be in the future"
    assert claims["exp"] - claims["iat"] <= 600, "exp must stay inside GitHub's 10-minute cap"


# --------------------------------------------------------------------------- #
# The shared-resolver cache key
# --------------------------------------------------------------------------- #
def test_rotating_the_private_key_gets_a_new_resolver(private_key: str) -> None:
    """The cache key includes the key, so a rotation is not served stale.

    Dropping the private key from the key survived (#1263): after a rotation
    the old resolver -- and its cached tokens minted with the retired key --
    would be handed back until the process restarted.
    """

    from curie_api.github_app import credentials_for

    first = credentials_for(app_settings(private_key))
    assert credentials_for(app_settings(private_key)) is first, "same config, same resolver"

    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rotated = other.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    assert credentials_for(app_settings(rotated)) is not first


def test_two_repositories_do_not_share_a_token(private_key: str, github: Recorder) -> None:
    creds = GitHubCredentials(settings=app_settings(private_key))
    assert creds.token_for(REPO) != creds.token_for("octo/other-bot")


# --------------------------------------------------------------------------- #
# Failure modes an operator has to be able to act on
# --------------------------------------------------------------------------- #
def test_a_repository_the_app_cannot_see_says_so(private_key: str, monkeypatch) -> None:
    # The most likely setup mistake, and GitHub's own "Not Found" explains
    # nothing about how to fix it.
    monkeypatch.setattr(
        "curie_api.github_app.httpx.Client", serve(lambda r: httpx.Response(404, json={}))
    )
    creds = GitHubCredentials(settings=app_settings(private_key))
    with pytest.raises(GitHubAppError, match="not installed on that repository"):
        creds.token_for(REPO)


def test_a_malformed_private_key_is_reported_as_a_config_error() -> None:
    creds = GitHubCredentials(
        settings=Settings(github_app_id="1", github_app_private_key="not-a-pem")
    )
    with pytest.raises(GitHubAppError, match="full PEM private key"):
        creds.token_for(REPO)


def test_an_unreachable_github_is_not_silently_a_missing_credential(
    private_key: str, monkeypatch
) -> None:
    # Returning "" here would downgrade an outage into "clone a private repo
    # anonymously", whose error names authentication and misleads the operator.
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("curie_api.github_app.httpx.Client", serve(boom))
    creds = GitHubCredentials(settings=app_settings(private_key))
    with pytest.raises(GitHubAppError, match="could not reach GitHub"):
        creds.token_for(REPO)


# --------------------------------------------------------------------------- #
# The resolver has to outlive one push, or the cache above is decorative
# --------------------------------------------------------------------------- #
def test_the_resolver_is_shared_across_pushes(private_key: str) -> None:
    # A fresh resolver per webhook means two extra GitHub calls on every push
    # and a cache that is never once hit.
    from curie_api.github_app import credentials_for

    settings = app_settings(private_key)
    assert credentials_for(settings) is credentials_for(app_settings(private_key))


def test_a_different_configuration_gets_a_different_resolver(private_key: str) -> None:
    from curie_api.github_app import credentials_for

    assert credentials_for(app_settings(private_key)) is not credentials_for(
        Settings(github_token="ghp_other")
    )


# --------------------------------------------------------------------------- #
# Saying which credential is live (#1262)
# --------------------------------------------------------------------------- #
def test_the_startup_line_names_the_credential_path(caplog) -> None:
    """AC1/AC3. ADR-0092 promised this line and it was never emitted.

    `describe()` had two tests and no caller, so an operator could not tell an
    App install from a PAT install from one that 404s on every private clone.
    Removing the call from create_app must turn this red.
    """

    from curie_api.main import create_app

    with caplog.at_level(logging.INFO, logger="curie_api"):
        create_app()
    assert any("github credential path" in r.getMessage() for r in caplog.records), (
        "no startup line names the credential path"
    )


def test_a_half_configured_app_warns_rather_than_looking_unconfigured(caplog) -> None:
    """AC2. ID set, key empty -- the DEFAULT shape of a partly-applied Secret.

    `app_configured` needs both, and the chart always renders the key from a
    secretKeyRef defaulting to empty. So a sync still in flight, or a typo'd
    key name, silently yields a PAT-or-nothing platform and a 404 on the
    private clone -- with nothing in the log distinguishing it from an install
    that never configured an App at all.
    """

    from curie_api.github_app import log_credential_path

    settings = Settings(github_app_id="1234567", github_app_private_key="", github_token="ghp_x")
    with caplog.at_level(logging.INFO, logger="curie_api"):
        log_credential_path(settings)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a half-configured App must not look like an unconfigured one"
    assert "1234567" in warnings[0].getMessage()
    assert "EMPTY" in warnings[0].getMessage()


def test_a_fully_configured_app_does_not_warn(private_key: str, caplog) -> None:
    # Otherwise "always warn" satisfies the test above and the line becomes noise.
    from curie_api.github_app import log_credential_path

    with caplog.at_level(logging.INFO, logger="curie_api"):
        log_credential_path(app_settings(private_key))
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_the_startup_line_carries_no_secret(private_key: str, caplog) -> None:
    from curie_api.github_app import log_credential_path

    with caplog.at_level(logging.INFO, logger="curie_api"):
        log_credential_path(app_settings(private_key, github_token="ghp_supersecret"))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "PRIVATE KEY" not in text
    assert "ghp_supersecret" not in text
