"""The ObjectStore port: an in-memory backing conforms, and BundleStore does too.

These are pure unit tests -- no RustFS/S3 required. They pin the extracted port
(#282, ADR-0026): a second, non-S3 implementation is a drop-in as long as it
satisfies the Protocol, and the existing BundleStore already does.
"""

import anyio
import pytest
from curie_api.config import Settings
from curie_api.storage import BundleStore, ObjectStore, build_s3_client


class InMemoryObjectStore:
    """A non-S3 backing that satisfies ObjectStore -- the "drop-in" this port
    exists to enable. Enforces the write-once discipline the port promotes into
    its contract."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def ensure_bucket(self) -> None:
        return None

    async def exists(self, key: str) -> bool:
        return key in self._objects

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        if key in self._objects:
            raise ValueError(f"write-once violation: {key} already stored")
        self._objects[key] = data

    async def get(self, key: str) -> bytes:
        return self._objects[key]


def test_in_memory_store_satisfies_port() -> None:
    store = InMemoryObjectStore()
    assert isinstance(store, ObjectStore)


def test_in_memory_round_trip_and_write_once() -> None:
    async def go() -> None:
        store: ObjectStore = InMemoryObjectStore()
        await store.ensure_bucket()
        assert await store.exists("k") is False
        await store.put("k", b"bundle-bytes", "application/zip")
        assert await store.exists("k") is True
        assert await store.get("k") == b"bundle-bytes"
        # Write-once: a second put to the same key is a violation, not an update.
        with pytest.raises(ValueError):
            await store.put("k", b"other", "application/zip")

    anyio.run(go)


def _settings() -> Settings:
    return Settings(
        s3_endpoint_url="http://localhost:29000",
        s3_access_key="rustfs",
        s3_secret_key="rustfssecret",
        s3_region="us-east-1",
        bundle_bucket="bundles",
    )


def test_bundle_store_satisfies_port() -> None:
    # boto3.client construction is offline; no network call is made here.
    store = BundleStore(_settings())
    assert isinstance(store, ObjectStore)


def test_build_s3_client_is_path_style() -> None:
    client = build_s3_client(_settings())
    # Path-style addressing is the alignment RustFS requires; assert the shared
    # factory pins it so a second S3-backed site cannot silently drift.
    assert client.meta.config.s3["addressing_style"] == "path"
