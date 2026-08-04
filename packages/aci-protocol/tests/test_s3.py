"""The shared S3 client builder (#501): path-style addressing, offline."""

from aci_protocol.s3 import build_s3_client


def test_build_s3_client_is_path_style() -> None:
    # boto3.client() does no network I/O until a call is made, so this asserts the
    # construction contract offline: path-style addressing (load-bearing for RustFS)
    # and the endpoint the caller passed.
    client = build_s3_client(
        endpoint_url="http://rustfs.test:9000",
        access_key="ak",
        secret_key="sk",
        region="us-east-1",
    )
    assert client.meta.config.s3["addressing_style"] == "path"
    assert client.meta.endpoint_url == "http://rustfs.test:9000"
    assert client.meta.region_name == "us-east-1"
