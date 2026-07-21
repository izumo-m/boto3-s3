"""Unit tests for ``S3.mb`` / ``S3.rb`` request shaping and error model.

aws-cli parity facts asserted here (aws-cli MbCommand / RbCommand,
``awscli/customizations/s3/subcommands.py:1240-1355``):

- ``LocationConstraint`` is the client's region, omitted for us-east-1;
- a bucket name ending in ``-an`` selects the account-regional namespace;
- ``tags`` become ``CreateBucketConfiguration.Tags`` with order and
  duplicate keys preserved (the server rejects duplicates, not the library);
- both operations use only the bucket of the target - a key part is ignored
  (the library is permissive; the CLI layer owns aws's strict path checks);
- single-call operations raise category errors directly, never BatchError.
"""

from __future__ import annotations

from typing import Any

import pytest

from boto3_s3 import (
    S3,
    BatchError,
    Boto3S3Error,
    NotFoundError,
    S3Storage,
    ValidationError,
)
from tests.utils.fakes3 import client_error


class _FakeMeta:
    def __init__(self, region_name: str) -> None:
        self.region_name = region_name


class _FakeBucketClient:
    """Fake covering the mb/rb wire surface (create_bucket / delete_bucket)."""

    def __init__(
        self,
        *,
        region: str = "us-east-1",
        create_error: Exception | None = None,
        delete_error: Exception | None = None,
    ) -> None:
        self.meta = _FakeMeta(region)
        self.create_error = create_error
        self.delete_error = delete_error
        self.create_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls.append(kwargs)
        if self.create_error is not None:
            raise self.create_error
        return {}

    def delete_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_calls.append(kwargs)
        if self.delete_error is not None:
            raise self.delete_error
        return {}


class TestMb:
    def test_us_east_1_sends_bare_params(self) -> None:
        client = _FakeBucketClient(region="us-east-1")
        S3().mb(S3Storage("s3://bucket", client=client))
        assert client.create_calls == [{"Bucket": "bucket"}]

    def test_other_region_adds_location_constraint(self) -> None:
        client = _FakeBucketClient(region="us-west-2")
        S3().mb(S3Storage("s3://bucket", client=client))
        assert client.create_calls == [
            {
                "Bucket": "bucket",
                "CreateBucketConfiguration": {"LocationConstraint": "us-west-2"},
            }
        ]

    @pytest.mark.parametrize("bucket", ["amzn-s3-demo-bucket-111122223333-us-east-1-an", "xyz-an"])
    def test_an_suffix_selects_account_regional_namespace(self, bucket: str) -> None:
        client = _FakeBucketClient(region="us-east-1")
        S3().mb(S3Storage(f"s3://{bucket}", client=client))
        assert client.create_calls == [{"Bucket": bucket, "BucketNamespace": "account-regional"}]

    def test_an_suffix_combines_with_location_constraint(self) -> None:
        client = _FakeBucketClient(region="us-west-2")
        S3().mb(S3Storage("s3://demo-an", client=client))
        assert client.create_calls == [
            {
                "Bucket": "demo-an",
                "BucketNamespace": "account-regional",
                "CreateBucketConfiguration": {"LocationConstraint": "us-west-2"},
            }
        ]

    def test_tags_preserve_order_and_duplicate_keys(self) -> None:
        client = _FakeBucketClient(region="us-east-1")
        S3().mb(
            S3Storage("s3://bucket", client=client),
            tags=[("K", "1"), ("A", "2"), ("K", "3")],
        )
        assert client.create_calls == [
            {
                "Bucket": "bucket",
                "CreateBucketConfiguration": {
                    "Tags": [
                        {"Key": "K", "Value": "1"},
                        {"Key": "A", "Value": "2"},
                        {"Key": "K", "Value": "3"},
                    ]
                },
            }
        ]

    def test_tags_mapping_accepted(self) -> None:
        client = _FakeBucketClient(region="us-east-1")
        S3().mb(S3Storage("s3://bucket", client=client), tags={"K1": "V1", "K2": "V2"})
        assert client.create_calls == [
            {
                "Bucket": "bucket",
                "CreateBucketConfiguration": {
                    "Tags": [
                        {"Key": "K1", "Value": "V1"},
                        {"Key": "K2", "Value": "V2"},
                    ]
                },
            }
        ]

    @pytest.mark.parametrize("tags", [None, [], {}])
    def test_empty_tags_send_no_configuration(self, tags: Any) -> None:
        client = _FakeBucketClient(region="us-east-1")
        S3().mb(S3Storage("s3://bucket", client=client), tags=tags)
        assert client.create_calls == [{"Bucket": "bucket"}]

    def test_key_part_is_ignored(self) -> None:
        # Same as aws mb: split_s3_bucket_key keeps the bucket, drops the key.
        client = _FakeBucketClient()
        S3().mb(S3Storage("s3://bucket/some/key", client=client))
        assert client.create_calls == [{"Bucket": "bucket"}]

    def test_scheme_is_optional(self) -> None:
        client = _FakeBucketClient()
        S3().mb(S3Storage("bucket", client=client))
        assert client.create_calls == [{"Bucket": "bucket"}]

    def test_empty_bucket_raises_eagerly(self) -> None:
        client = _FakeBucketClient()
        with pytest.raises(ValidationError):
            S3().mb(S3Storage("s3://", client=client))
        assert client.create_calls == []

    def test_non_location_target_raises(self) -> None:
        with pytest.raises(ValidationError):
            S3().mb(123)  # type: ignore[arg-type]

    def test_client_error_raises_category_with_cause(self) -> None:
        error = client_error("BucketAlreadyOwnedByYou", 409, "CreateBucket")
        client = _FakeBucketClient(create_error=error)
        with pytest.raises(Boto3S3Error) as excinfo:
            S3().mb(S3Storage("s3://bucket", client=client))
        # 409 falls back to the 4xx category; single-op => never BatchError.
        assert isinstance(excinfo.value, ValidationError)
        assert not isinstance(excinfo.value, BatchError)
        assert excinfo.value.__cause__ is error


class TestRb:
    def test_deletes_the_bucket(self) -> None:
        client = _FakeBucketClient()
        S3().rb(S3Storage("s3://bucket", client=client))
        assert client.delete_calls == [{"Bucket": "bucket"}]

    def test_scheme_is_optional(self) -> None:
        client = _FakeBucketClient()
        S3().rb(S3Storage("bucket", client=client))
        assert client.delete_calls == [{"Bucket": "bucket"}]

    def test_key_part_is_ignored(self) -> None:
        # Library leniency (the CLI rejects a key with rc 252, aws parity).
        client = _FakeBucketClient()
        S3().rb(S3Storage("s3://bucket/key", client=client))
        assert client.delete_calls == [{"Bucket": "bucket"}]

    def test_empty_bucket_raises_eagerly(self) -> None:
        client = _FakeBucketClient()
        with pytest.raises(ValidationError):
            S3().rb(S3Storage("s3://", client=client))
        assert client.delete_calls == []

    def test_no_such_bucket_raises_not_found(self) -> None:
        error = client_error("NoSuchBucket", 404, "DeleteBucket")
        client = _FakeBucketClient(delete_error=error)
        with pytest.raises(NotFoundError) as excinfo:
            S3().rb(S3Storage("s3://bucket", client=client))
        assert excinfo.value.__cause__ is error

    def test_bucket_not_empty_raises_validation_category(self) -> None:
        error = client_error("BucketNotEmpty", 409, "DeleteBucket")
        client = _FakeBucketClient(delete_error=error)
        with pytest.raises(Boto3S3Error) as excinfo:
            S3().rb(S3Storage("s3://bucket", client=client))
        assert isinstance(excinfo.value, ValidationError)
        assert not isinstance(excinfo.value, BatchError)
        assert excinfo.value.__cause__ is error
