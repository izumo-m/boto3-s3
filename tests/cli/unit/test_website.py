"""Unit tests for the ``boto3-s3 website`` subcommand (dispatch + exit codes).

The rc shape (docs/cli.md section 6): **no local catch** - unlike mb/rb,
server rejections keep their ClientError cause and exit 254 through main;
botocore's client-side parameter validation and aws's
fold-the-key-into-the-bucket-name rejection are 252; success prints
nothing at all.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context


class _FakeWebsiteClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def put_bucket_website(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {}


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutBucketWebsite",
    )


def _ctx(client: Any) -> Context:
    return Context(client_factory=lambda _args: client)  # pyright: ignore[reportArgumentType]


def _real_client_ctx() -> Context:
    # A real botocore client (no HTTP happens on these paths) so client-side
    # parameter validation fires like it does for aws.
    import boto3

    return Context(
        client_factory=lambda _args: boto3.session.Session().client("s3", region_name="us-east-1")
    )


class TestWebsite:
    def test_index_document_succeeds_silently(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = _FakeWebsiteClient()
        rc = cli.main(
            ["website", "s3://bucket", "--index-document", "index.html"], ctx=_ctx(client)
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == ""  # aws prints nothing on success
        assert client.calls == [
            {
                "Bucket": "bucket",
                "WebsiteConfiguration": {"IndexDocument": {"Suffix": "index.html"}},
            }
        ]

    def test_error_document(self) -> None:
        client = _FakeWebsiteClient()
        rc = cli.main(
            ["website", "s3://bucket", "--error-document", "error.html"], ctx=_ctx(client)
        )
        assert rc == 0
        assert client.calls[0]["WebsiteConfiguration"] == {"ErrorDocument": {"Key": "error.html"}}

    def test_both_documents(self) -> None:
        client = _FakeWebsiteClient()
        rc = cli.main(
            [
                "website",
                "s3://bucket",
                "--index-document",
                "index.html",
                "--error-document",
                "error.html",
            ],
            ctx=_ctx(client),
        )
        assert rc == 0
        assert client.calls[0]["WebsiteConfiguration"] == {
            "IndexDocument": {"Suffix": "index.html"},
            "ErrorDocument": {"Key": "error.html"},
        }

    def test_no_options_sends_empty_configuration(self) -> None:
        # aws sends the empty dict; the server, not the client, rejects it.
        client = _FakeWebsiteClient()
        rc = cli.main(["website", "s3://bucket"], ctx=_ctx(client))
        assert rc == 0
        assert client.calls[0]["WebsiteConfiguration"] == {}

    def test_bare_bucket_name_accepted(self) -> None:
        client = _FakeWebsiteClient()
        assert cli.main(["website", "bucket", "--index-document", "i.html"], ctx=_ctx(client)) == 0
        assert client.calls[0]["Bucket"] == "bucket"

    def test_trailing_slash_stripped(self) -> None:
        client = _FakeWebsiteClient()
        assert (
            cli.main(["website", "s3://bucket/", "--index-document", "i.html"], ctx=_ctx(client))
            == 0
        )
        assert client.calls[0]["Bucket"] == "bucket"

    def test_accesspoint_arn_passes_as_bucket(self) -> None:
        # aws sends the whole ARN as the Bucket (block_unsupported_resources
        # only rejects Object Lambda / Outposts bucket ARNs).
        arn = "arn:aws:s3:us-west-2:123456789012:accesspoint/endpoint"
        client = _FakeWebsiteClient()
        assert (
            cli.main(["website", f"s3://{arn}", "--index-document", "i.html"], ctx=_ctx(client))
            == 0
        )
        assert client.calls[0]["Bucket"] == arn


class TestWebsiteExitCodeShape:
    def test_key_part_is_param_validation_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        # aws folds "b/some/key" into the Bucket parameter and botocore's
        # name regex rejects it -> 252; we reject the same shapes ourselves.
        client = _FakeWebsiteClient()
        rc = cli.main(
            ["website", "s3://bucket/some/key", "--index-document", "i.html"], ctx=_ctx(client)
        )
        assert rc == 252
        assert "Parameter validation failed" in capsys.readouterr().err
        assert client.calls == []

    def test_double_trailing_slash_is_252(self) -> None:
        # "s3://b//" strips one slash -> Bucket "b/" fails aws's regex; the
        # leftover slash must not be silently dropped on our side.
        client = _FakeWebsiteClient()
        assert (
            cli.main(["website", "s3://bucket//", "--index-document", "i.html"], ctx=_ctx(client))
            == 252
        )
        assert client.calls == []

    def test_empty_uri_is_param_validation_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["website", "s3://", "--index-document", "i.html"], ctx=_real_client_ctx())
        assert rc == 252
        assert "Invalid bucket name" in capsys.readouterr().err

    def test_extra_positional_is_unknown_options_252(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def factory(_args: Any) -> Any:
            raise AssertionError("client factory must not be called")

        rc = cli.main(["website", "s3://bucket", "extra-arg"], ctx=Context(client_factory=factory))
        assert rc == 252
        assert "Unknown options: extra-arg" in capsys.readouterr().err

    def test_object_lambda_arn_stays_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        arn = "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-olap"
        rc = cli.main(["website", f"s3://{arn}"], ctx=_ctx(object()))
        assert rc == 252
        assert "S3 Object Lambda" in capsys.readouterr().err

    def test_no_such_bucket_is_254_not_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The defining contrast with mb/rb: website has no local catch, so a
        # server rejection keeps its ClientError cause and exits 254.
        client = _FakeWebsiteClient(error=_client_error("NoSuchBucket", 404))
        rc = cli.main(["website", "s3://no-such", "--index-document", "i.html"], ctx=_ctx(client))
        assert rc == 254
        assert "NoSuchBucket" in capsys.readouterr().err
