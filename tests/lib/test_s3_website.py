"""Unit tests for ``S3.website`` request shaping and error model.

aws-cli parity facts asserted here (aws-cli WebsiteCommand,
``awscli/customizations/s3/subcommands.py:977-1025``):

- ``--index-document`` -> ``IndexDocument.Suffix``, ``--error-document`` ->
  ``ErrorDocument.Key``; unset ones are omitted, and with neither set an
  **empty** configuration is sent (the server rejects it, not the client);
- the library uses only the bucket of the target (permissive; aws's
  pass-the-key-as-bucket-name 252 is the CLI layer's job);
- no local catch: a single-call operation raising category errors directly,
  never BatchError - NoSuchBucket maps to NotFoundError with the
  ClientError kept as ``__cause__``.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ParamValidationError

from boto3_s3 import S3, BatchError, Boto3S3Error, NotFoundError, S3Storage, ValidationError
from tests.utils.fakes3 import client_error


class _FakeWebsiteClient:
    """Fake covering the website wire surface (put_bucket_website)."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def put_bucket_website(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {}


def _storage(target: str, client: Any) -> S3Storage:
    return S3Storage(target, client=client)


class TestWebsite:
    def test_index_document_shapes_suffix(self) -> None:
        client = _FakeWebsiteClient()
        S3().website(_storage("s3://bucket", client), index_document="index.html")
        assert client.calls == [
            {
                "Bucket": "bucket",
                "WebsiteConfiguration": {"IndexDocument": {"Suffix": "index.html"}},
            }
        ]

    def test_error_document_shapes_key(self) -> None:
        client = _FakeWebsiteClient()
        S3().website(_storage("s3://bucket", client), error_document="error.html")
        assert client.calls == [
            {
                "Bucket": "bucket",
                "WebsiteConfiguration": {"ErrorDocument": {"Key": "error.html"}},
            }
        ]

    def test_both_documents(self) -> None:
        client = _FakeWebsiteClient()
        S3().website(
            _storage("s3://bucket", client),
            index_document="index.html",
            error_document="error.html",
        )
        assert client.calls[0]["WebsiteConfiguration"] == {
            "IndexDocument": {"Suffix": "index.html"},
            "ErrorDocument": {"Key": "error.html"},
        }

    def test_neither_sends_empty_configuration(self) -> None:
        # aws sends the empty dict and lets the server reject it.
        client = _FakeWebsiteClient()
        S3().website(_storage("s3://bucket", client))
        assert client.calls == [{"Bucket": "bucket", "WebsiteConfiguration": {}}]

    def test_key_part_is_ignored(self) -> None:
        # Permissive library: only the bucket is used; aws's strict path
        # handling (key folded into the bucket name -> 252) is CLI-layer.
        client = _FakeWebsiteClient()
        S3().website(_storage("s3://bucket/some/key", client), index_document="i.html")
        assert client.calls[0]["Bucket"] == "bucket"

    def test_scheme_is_optional(self) -> None:
        client = _FakeWebsiteClient()
        S3().website(_storage("bucket", client), index_document="i.html")
        assert client.calls[0]["Bucket"] == "bucket"

    def test_invalid_target_type_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            S3().website(123)  # pyright: ignore[reportArgumentType]


class TestWebsiteErrors:
    def test_no_such_bucket_becomes_not_found(self) -> None:
        cause = client_error("NoSuchBucket", 404, "PutBucketWebsite")
        client = _FakeWebsiteClient(error=cause)
        with pytest.raises(NotFoundError) as excinfo:
            S3().website(_storage("s3://no-such", client), index_document="i.html")
        assert excinfo.value.__cause__ is cause
        assert not isinstance(excinfo.value, BatchError)
        assert excinfo.value.operation == "website"

    def test_param_validation_error_becomes_validation_error(self) -> None:
        cause = ParamValidationError(report='Invalid bucket name ""')
        client = _FakeWebsiteClient(error=cause)
        with pytest.raises(ValidationError) as excinfo:
            S3().website(_storage("s3://b", client))
        assert excinfo.value.__cause__ is cause

    def test_a_non_botocore_exception_is_wrapped_with_its_cause(self) -> None:
        # A redirect loop inside botocore ends in RecursionError; the request
        # failed all the same, so it is the base error like a per-item failure.
        cause = RecursionError("maximum recursion depth exceeded")
        client = _FakeWebsiteClient(error=cause)
        with pytest.raises(Boto3S3Error) as excinfo:
            S3().website(_storage("s3://b", client), index_document="i.html")
        assert type(excinfo.value) is Boto3S3Error
        assert excinfo.value.__cause__ is cause
        assert (excinfo.value.operation, excinfo.value.bucket) == ("website", "b")

    def test_an_assertion_error_passes_through(self) -> None:
        client = _FakeWebsiteClient(error=AssertionError("unexpected call"))
        with pytest.raises(AssertionError, match="unexpected call"):
            S3().website(_storage("s3://b", client))

    def test_empty_bucket_fails_real_botocore_validation(self) -> None:
        # Real botocore rejects Bucket="" client-side before any HTTP, so this
        # runs offline. The CLI's rc-252 parity for `website s3://` rests on
        # exactly this error.
        import boto3

        client = boto3.session.Session().client("s3", region_name="us-east-1")
        with pytest.raises(ValidationError) as excinfo:
            S3().website(S3Storage("s3://", client=client))
        assert "Invalid bucket name" in str(excinfo.value)
