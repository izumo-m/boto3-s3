"""Unit tests for ``S3.website`` request shaping and error model.

aws-cli parity facts asserted here (aws-cli WebsiteCommand,
``awscli/customizations/s3/subcommands.py:977-1025``):

- ``--index-document`` -> ``IndexDocument.Suffix``, ``--error-document`` ->
  ``ErrorDocument.Key``; unset ones are omitted, and with neither set an
  **empty** configuration is sent (the server rejects it, not the client);
- a string target names the bucket the way aws-cli's ``_get_bucket_name``
  reads its argument - ``s3://`` and one trailing ``/`` off, the unsupported
  ARN families refused, the rest the bucket name whole - so botocore takes
  or refuses the name as on aws-cli; an ``S3Storage`` target names its bucket;
- no local catch: a single-call operation raising category errors directly,
  never BatchError - NoSuchBucket maps to NotFoundError with the
  ClientError kept as ``__cause__``.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ParamValidationError

from boto3_s3 import (
    S3,
    BatchError,
    Boto3S3Error,
    InvalidConfigError,
    NotFoundError,
    S3Storage,
    ValidationError,
)
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
        # aws-cli sends the empty dict and lets the server reject it.
        client = _FakeWebsiteClient()
        S3().website(_storage("s3://bucket", client))
        assert client.calls == [{"Bucket": "bucket", "WebsiteConfiguration": {}}]

    def test_an_s3storage_target_names_its_bucket(self) -> None:
        # An S3Storage is used as given: its bucket, whatever key it carries.
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
        assert isinstance(excinfo.value.__cause__, ParamValidationError)


class TestStringTarget:
    """A string names the bucket as aws-cli's website argument does."""

    @staticmethod
    def _s3() -> tuple[S3, list[str]]:
        # A real botocore client, answered locally: botocore's own checks run,
        # and the URL of a request it sends is recorded.
        import io

        import boto3
        from botocore.awsrequest import AWSResponse

        session = boto3.session.Session(
            aws_access_key_id="x", aws_secret_access_key="y", region_name="us-east-1"
        )
        urls: list[str] = []

        def answer(request: Any, **_kwargs: Any) -> AWSResponse:
            urls.append(request.url)
            response = AWSResponse(request.url, 200, {}, None)

            class _Raw(io.BytesIO):
                def stream(self, *_args: Any, **_kwargs: Any) -> Any:
                    yield b""

            response.raw = _Raw(b"")
            return response

        # On the session: every client S3 builds from it carries the handler.
        session.events.register("before-send.s3", answer)
        return S3(session=session), urls

    @pytest.mark.parametrize(
        ("target", "name"),
        [
            ("s3://bucket/some/key", "bucket/some/key"),
            ("s3://bucket//", "bucket/"),
            ("s3:///k", "/k"),
            ("s3://", ""),
            ("bad!name", "bad!name"),
        ],
    )
    def test_the_whole_rest_is_the_bucket_name(self, target: str, name: str) -> None:
        # Never split into a bucket and a key: botocore refuses the whole name
        # before any request, in its own words (measured on the pinned aws).
        s3, urls = self._s3()
        with pytest.raises(ValidationError) as excinfo:
            s3.website(target, index_document="i.html")
        assert str(excinfo.value).startswith(
            f'Parameter validation failed:\nInvalid bucket name "{name}": Bucket name must match'
        )
        assert isinstance(excinfo.value.__cause__, ParamValidationError)
        assert excinfo.value.operation == "website"
        assert urls == []

    @pytest.mark.parametrize(
        ("target", "message"),
        [
            (
                "s3://arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/ol/x",
                "s3 commands do not support S3 Object Lambda resources",
            ),
            (
                "arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-01234567890123456/bucket/bk",
                "s3 commands do not support Outpost Bucket ARNs",
            ),
        ],
    )
    def test_the_unsupported_arn_families_are_refused(self, target: str, message: str) -> None:
        s3, urls = self._s3()
        with pytest.raises(ValidationError, match=message) as excinfo:
            s3.website(target, index_document="i.html")
        assert excinfo.value.operation == "website"
        assert urls == []

    def test_a_malformed_arn_is_botocores_to_refuse(self) -> None:
        # Outside the unsupported families' patterns (no region), so it reaches
        # botocore, which refuses it as aws-cli's run shows.
        s3, urls = self._s3()
        with pytest.raises(ValidationError, match="Invalid ARN: bucket ARN is missing a region"):
            s3.website("s3://arn:aws:s3-object-lambda::123456789012:accesspoint/ol")
        assert urls == []

    @pytest.mark.parametrize(
        ("target", "host"),
        [
            ("s3://bucket/", "bucket.s3.amazonaws.com"),
            ("bucket", "bucket.s3.amazonaws.com"),
            (
                "arn:aws:s3:us-east-1:123456789012:accesspoint/ap",
                "ap-123456789012.s3-accesspoint.us-east-1.amazonaws.com",
            ),
        ],
    )
    def test_a_name_botocore_takes_is_sent(self, target: str, host: str) -> None:
        s3, urls = self._s3()
        s3.website(target, index_document="i.html")
        assert len(urls) == 1
        assert f"//{host}/" in urls[0]

    def test_an_empty_aws_ca_bundle_is_refused_beside_an_overridden_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A client an overridden `client()` returns is vetted as one handed to
        # an S3Storage is: an empty AWS_CA_BUNDLE is refused before the request.
        fake = _FakeWebsiteClient()

        class _S3(S3):
            def client(self) -> Any:
                return fake

        monkeypatch.setenv("AWS_CA_BUNDLE", "")
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle"):
            _S3().website("s3://bucket", index_document="i.html")
        assert fake.calls == []
