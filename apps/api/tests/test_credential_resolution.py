"""The API's key-free BYO object-store path (#1559), end to end from env to signer.

Documented behavior: leave ``S3_ACCESS_KEY``/``S3_SECRET_KEY`` unset and the API
picks up ambient cloud credentials (IRSA, an instance role, an ambient profile).
That only works if the config default is empty AND the empty value reaches boto3
as ``None``, so this asserts the resolved signer rather than the config string:
a config default of "" alone leaves the path just as inert.

Asserted through the real ``BundleStore`` because that is what production
constructs; a ``BundleStore`` that later grew its own credential handling would
slip past a test of the bare builder. ``BundleStore.__init__`` only constructs a
boto3 client and does no network I/O, and botocore's web-identity provider
returns deferred credentials without calling STS, so this is fully offline.
"""

import os
from pathlib import Path

import pytest
from curie_api.config import Settings
from curie_api.storage import BundleStore


@pytest.fixture
def web_identity_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Only an IRSA-style web identity is available to the provider chain.

    Every ``AWS_*``/``S3_*`` var is scrubbed first so an ambient developer
    profile or a CI runner's own credentials cannot decide the result. The token
    file's contents are never read (the provider defers the STS exchange), so a
    placeholder is enough.
    """
    for name in list(os.environ):
        if name.startswith(("AWS_", "S3_")):
            monkeypatch.delenv(name, raising=False)
    token_file = tmp_path / "token"
    token_file.write_text("placeholder-web-identity-token")
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::000000000000:role/curie-bundles")
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("S3_ENDPOINT_URL", "https://s3.example.com:443")


def test_omitted_credential_env_reaches_web_identity(web_identity_env: None) -> None:
    # _env_file=None is mandatory: Settings declares env_file=".env", so without
    # it a stray developer .env in the checkout silently decides the result.
    settings = Settings(_env_file=None)
    store = BundleStore(settings)
    credentials = store._client._request_signer._credentials
    assert credentials.method == "assume-role-with-web-identity"


def test_configured_credential_env_signs_explicitly(
    web_identity_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The guard against "fixing" the chain by ignoring configured credentials: a
    # RustFS/MinIO deployment supplies a static key pair via env and it must be
    # the one that signs, even with an ambient web identity right next to it.
    monkeypatch.setenv("S3_ACCESS_KEY", "static-ak")
    monkeypatch.setenv("S3_SECRET_KEY", "static-sk")
    settings = Settings(_env_file=None)
    store = BundleStore(settings)
    credentials = store._client._request_signer._credentials
    assert credentials.method == "explicit"
    assert credentials.access_key == "static-ak"
