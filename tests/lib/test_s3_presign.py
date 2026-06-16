"""Unit tests for ``S3.presign`` request shaping and error model.

aws-cli parity facts asserted here (aws-cli PresignCommand,
``awscli/customizations/s3/subcommands.py:1022-1057``):

- pure client-side computation: ``generate_presigned_url`` is the only call;
- the ``s3://`` scheme is optional, and ``expires_in`` passes through with no
  range validation (0 / negative / over S3's 604800 maximum all sign);
- an empty bucket or key fails botocore's client-side parameter validation,
  surfaced as ``ValidationError`` (the ``s3_errors`` ParamValidationError
  branch) - what the CLI's rc-252 parity rests on;
- ``method="put_object"`` is the library's permissive superset (aws-cli only
  signs ``get_object``).
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ParamValidationError

from boto3_s3 import S3, BatchError, S3Storage, ValidationError


class _FakePresignClient:
    """Fake covering the presign wire surface (generate_presigned_url)."""

    def __init__(self, *, url: str = "https://signed.example/x", error: Exception | None = None):
        self.url = url
        self.error = error
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    def generate_presigned_url(self, method: str, **kwargs: Any) -> str:
        self.calls.append((method, kwargs["Params"], kwargs["ExpiresIn"]))
        if self.error is not None:
            raise self.error
        return self.url


def _storage(target: str, client: Any) -> S3Storage:
    return S3Storage(target, client=client)


class TestPresign:
    def test_signs_get_object_with_defaults(self) -> None:
        client = _FakePresignClient()
        url = S3().presign(_storage("s3://bucket/some/key.txt", client))
        assert url == client.url
        assert client.calls == [("get_object", {"Bucket": "bucket", "Key": "some/key.txt"}, 3600)]

    def test_scheme_is_optional(self) -> None:
        client = _FakePresignClient()
        S3().presign(_storage("bucket/key", client))
        assert client.calls[0][1] == {"Bucket": "bucket", "Key": "key"}

    def test_expires_in_passes_through_unvalidated(self) -> None:
        client = _FakePresignClient()
        s3 = S3()
        for value in (0, -1, 604801):
            s3.presign(_storage("s3://b/k", client), expires_in=value)
        assert [call[2] for call in client.calls] == [0, -1, 604801]

    def test_put_object_method_is_the_library_superset(self) -> None:
        client = _FakePresignClient()
        S3().presign(_storage("s3://b/k", client), method="put_object")
        assert client.calls[0][0] == "put_object"

    def test_url_returned_unchanged(self) -> None:
        client = _FakePresignClient(url="https://x.example/y?sig=1")
        assert S3().presign(_storage("s3://b/k", client)) == "https://x.example/y?sig=1"

    def test_invalid_target_type_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            S3().presign(123)  # pyright: ignore[reportArgumentType]


class TestPresignErrors:
    def test_param_validation_error_becomes_validation_error(self) -> None:
        cause = ParamValidationError(report='Invalid bucket name ""')
        client = _FakePresignClient(error=cause)
        with pytest.raises(ValidationError) as excinfo:
            S3().presign(_storage("s3://b/k", client))
        assert excinfo.value.__cause__ is cause
        assert not isinstance(excinfo.value, BatchError)
        assert "Invalid bucket name" in str(excinfo.value)
        assert excinfo.value.operation == "presign"

    def test_empty_key_fails_real_botocore_validation(self) -> None:
        # Real botocore (no fake): presign validates the request shape
        # client-side with no HTTP, so this runs offline. The CLI's rc-252
        # parity for `presign s3://bucket` rests on exactly this error.
        import boto3

        client = boto3.session.Session().client("s3", region_name="us-east-1")
        with pytest.raises(ValidationError) as excinfo:
            S3().presign(S3Storage("s3://bucket-only", client=client))
        assert "Invalid length for parameter Key" in str(excinfo.value)

    def test_empty_bucket_fails_real_botocore_validation(self) -> None:
        import boto3

        client = boto3.session.Session().client("s3", region_name="us-east-1")
        with pytest.raises(ValidationError) as excinfo:
            S3().presign(S3Storage("s3://", client=client))
        assert "Invalid bucket name" in str(excinfo.value)
