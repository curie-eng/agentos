"""The worker's key-free BYO object-store path (#1559), end to end from env to signer.

The worker is a separate lane with its own settings class and its own defaults,
and its config comment declares the S3 block a parity seam with the API
("mirrors the API's env names"). A test covering only the API would leave the
worker's default free to drift back to a baked-in dev key, so the worker's read
path gets the same assertion as the API's write path.

Asserted through the real ``BundleStore`` because that is what production
constructs. ``BundleStore.__init__`` only builds a boto3 client and does no
network I/O, and botocore's web-identity provider returns deferred credentials
without calling STS, so this is fully offline.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from curie_worker.bundle_store import BundleStore
from curie_worker.config import WorkerConfig


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
    config = WorkerConfig()
    store = BundleStore(config)
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
    config = WorkerConfig()
    store = BundleStore(config)
    credentials = store._client._request_signer._credentials
    assert credentials.method == "explicit"
    assert credentials.access_key == "static-ak"
