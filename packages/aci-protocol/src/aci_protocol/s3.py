"""The one path-style S3/RustFS client construction (#501).

The API's write path (`curie_api.storage.BundleStore`) and the worker's read
path (`curie_worker.bundle_store.BundleStore`) must build their boto3 client
identically -- same endpoint, credentials, region, and (load-bearing for RustFS)
path-style addressing. That construction used to be hand-copied in each app (and a
third time in a worker test fixture), a seam the blob-storage INTERFACE flagged as
"hand-aligned client sites". This is the single builder both import so the
alignment cannot drift.

It takes primitives, not either app's config object, so it couples to neither
`curie_api.Settings` nor `curie_worker.WorkerConfig`. `boto3` is imported
lazily inside the function so importing this module (and the rest of
``aci_protocol``) never requires boto3 for consumers that do not touch S3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


def build_s3_client(
    *,
    endpoint_url: str,
    access_key: str,
    secret_key: str,
    region: str,
) -> S3Client:
    """Construct the path-style S3 client shared by every S3-backed store.

    Path-style addressing is required for RustFS and works with AWS S3, so it is
    fixed here. Callers pass their resolved config values as keyword primitives.
    """
    import boto3
    from botocore.client import Config as BotoConfig

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=BotoConfig(s3={"addressing_style": "path"}),
    )
