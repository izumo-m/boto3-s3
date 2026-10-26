"""Unit tests for the ``boto3-s3 website`` subcommand (dispatch + exit codes).

The rc shape (design/cli.md section 6): **no local catch** - unlike mb/rb,
server rejections keep their ClientError cause and exit 254 through main;
botocore's client-side parameter validation and aws-cli's
fold-the-key-into-the-bucket-name rejection are 252; success prints
nothing at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context
from tests.utils.fakes3 import client_error
from tests.utils.harness import client_ctx, unused_ctx


class _FakeWebsiteClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def put_bucket_website(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {}


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
            ["website", "s3://bucket", "--index-document", "index.html"], ctx=client_ctx(client)
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert (captured.out, captured.err) == ("", "")  # `aws` prints nothing on success
        assert client.calls == [
            {
                "Bucket": "bucket",
                "WebsiteConfiguration": {"IndexDocument": {"Suffix": "index.html"}},
            }
        ]

    def test_error_document(self) -> None:
        client = _FakeWebsiteClient()
        rc = cli.main(
            ["website", "s3://bucket", "--error-document", "error.html"], ctx=client_ctx(client)
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
            ctx=client_ctx(client),
        )
        assert rc == 0
        assert client.calls[0]["WebsiteConfiguration"] == {
            "IndexDocument": {"Suffix": "index.html"},
            "ErrorDocument": {"Key": "error.html"},
        }

    def test_no_options_sends_empty_configuration(self) -> None:
        # aws-cli sends the empty dict; the server, not the client, rejects it.
        client = _FakeWebsiteClient()
        rc = cli.main(["website", "s3://bucket"], ctx=client_ctx(client))
        assert rc == 0
        assert client.calls[0]["WebsiteConfiguration"] == {}

    def test_bare_bucket_name_accepted(self) -> None:
        client = _FakeWebsiteClient()
        assert (
            cli.main(["website", "bucket", "--index-document", "i.html"], ctx=client_ctx(client))
            == 0
        )
        assert client.calls[0]["Bucket"] == "bucket"

    def test_trailing_slash_stripped(self) -> None:
        client = _FakeWebsiteClient()
        assert (
            cli.main(
                ["website", "s3://bucket/", "--index-document", "i.html"], ctx=client_ctx(client)
            )
            == 0
        )
        assert client.calls[0]["Bucket"] == "bucket"

    def test_accesspoint_arn_passes_as_bucket(self) -> None:
        # aws-cli sends the whole ARN as the Bucket (block_unsupported_resources
        # only rejects Object Lambda / Outposts bucket ARNs).
        arn = "arn:aws:s3:us-west-2:123456789012:accesspoint/endpoint"
        client = _FakeWebsiteClient()
        assert (
            cli.main(
                ["website", f"s3://{arn}", "--index-document", "i.html"], ctx=client_ctx(client)
            )
            == 0
        )
        assert client.calls[0]["Bucket"] == arn


class TestWebsiteExitCodeShape:
    @pytest.mark.parametrize(
        ("path", "name"),
        [
            ("s3://bucket/some/key", "bucket/some/key"),
            ("s3://bucket//", "bucket/"),
            ("s3:///k", "/k"),
            ("s3://", ""),
            # An Outposts access point closed with a ":" (both separators).
            (
                "s3://arn:aws:s3-outposts:us-west-2:123456789012:outpost/"
                "op-01234567890123456/accesspoint/ap:",
                "arn:aws:s3-outposts:us-west-2:123456789012:outpost/"
                "op-01234567890123456/accesspoint/ap:",
            ),
            (
                "s3://arn:aws:s3-outposts:us-west-2:123456789012:outpost:"
                "op-01234567890123456:accesspoint:ap:",
                "arn:aws:s3-outposts:us-west-2:123456789012:outpost:"
                "op-01234567890123456:accesspoint:ap:",
            ),
        ],
    )
    def test_the_whole_name_is_botocores_to_refuse_252(
        self, path: str, name: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws-cli strips s3:// and one trailing slash and hands the rest to
        # PutBucketWebsite whole; botocore's name check refuses it before any
        # request (252, measured) - the name the report quotes is that rest.
        rc = cli.main(["website", path, "--index-document", "i.html"], ctx=_real_client_ctx())
        assert rc == 252
        assert (
            "An error occurred (ParamValidation): Parameter validation failed:\n"
            f'Invalid bucket name "{name}": Bucket name must match the regex'
        ) in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("name", "message"),
        [
            (
                "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/ol/x",
                "s3 commands do not support S3 Object Lambda resources",
            ),
            (
                "arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-01234567890123456/bucket/bk",
                "s3 commands do not support Outpost Bucket ARNs",
            ),
        ],
    )
    def test_an_unsupported_arn_is_refused_before_the_request(
        self, name: str, message: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws-cli refuses the unsupported resource itself (measured, 252).
        client = _FakeWebsiteClient()
        rc = cli.main(["website", f"s3://{name}", "--index-document", "i"], ctx=client_ctx(client))
        assert rc == 252
        err = capsys.readouterr().err
        assert message in err
        assert "Parameter validation failed" not in err
        assert client.calls == []

    @pytest.mark.parametrize(
        ("name", "message"),
        [
            # No region: outside the unsupported Object Lambda pattern, so
            # botocore resolves it and refuses it (measured on the pinned aws).
            (
                "arn:aws:s3-object-lambda::123456789012:accesspoint/ol",
                "Invalid ARN: bucket ARN is missing a region",
            ),
            (
                "arn:aws:s3:us-west-2:123456789012:accesspoint:ap1/x"
                ":s3-object-lambda::123456789012:accesspoint/ol",
                "Invalid ARN: The ARN may only contain a single resource component",
            ),
        ],
    )
    def test_a_malformed_arn_is_botocores_to_refuse(
        self, name: str, message: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["website", f"s3://{name}", "--index-document", "i"], ctx=_real_client_ctx())
        assert rc == 252
        assert message in capsys.readouterr().err

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
        rc = cli.main(["website", f"s3://{arn}"], ctx=client_ctx(object()))
        assert rc == 252
        assert "S3 Object Lambda" in capsys.readouterr().err

    def test_no_such_bucket_is_254_not_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The defining contrast with mb/rb: website has no local catch, so a
        # server rejection keeps its ClientError cause and exits 254.
        client = _FakeWebsiteClient(error=client_error("NoSuchBucket", 404, "PutBucketWebsite"))
        rc = cli.main(
            ["website", "s3://no-such", "--index-document", "i.html"], ctx=client_ctx(client)
        )
        assert rc == 254
        assert "NoSuchBucket" in capsys.readouterr().err


class TestWebsiteParamfileAndQuery:
    """aws-cli expands both document options via file:// at parse time and
    compiles --query there; a bad reference / expression is its 252 before the
    request is built. A paramfile positional is refused (252)."""

    def test_index_document_file_reference_is_expanded(self, tmp_path: Any) -> None:
        ref = tmp_path / "i.txt"
        ref.write_text("home.html")
        client = _FakeWebsiteClient()
        assert (
            cli.main(
                ["website", "s3://b", "--index-document", f"file://{ref}"], ctx=client_ctx(client)
            )
            == 0
        )
        assert client.calls[0]["WebsiteConfiguration"] == {"IndexDocument": {"Suffix": "home.html"}}

    @pytest.mark.parametrize("prefix", ["file://", "fileb://"])
    def test_positional_paramfile_is_refused(
        self, prefix: str, tmp_path: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws-cli takes the first character of a file:// text as the bucket (a
        # file holding "s3://mybucket" configures bucket "s") and crashes on
        # fileb:// bytes - an aws-cli bug not copied: both are refused unread.
        ref = tmp_path / "u.txt"
        ref.write_text("s3://mybucket")
        rc = cli.main(["website", f"{prefix}{ref}"], ctx=unused_ctx())
        assert rc == 252
        assert "a file:// or fileb:// reference cannot name the bucket" in (capsys.readouterr().err)

    def test_index_document_missing_paramfile_is_252(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(
            ["website", "s3://b", "--index-document", "file:///no/x"],
            ctx=unused_ctx(),
        )
        assert rc == 252
        assert "Error parsing parameter '--index-document'" in capsys.readouterr().err

    def test_error_document_missing_paramfile_is_252(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(
            ["website", "s3://b", "--error-document", "file:///no/x"],
            ctx=unused_ctx(),
        )
        assert rc == 252
        assert "Error parsing parameter '--error-document'" in capsys.readouterr().err

    def test_invalid_query_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["website", "s3://b", "--query", "]["], ctx=unused_ctx())
        assert rc == 252
        assert "Bad value for --query ][" in capsys.readouterr().err
