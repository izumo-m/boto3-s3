"""Unit tests for the ``boto3-s3 presign`` subcommand (dispatch + exit codes).

The rc shape is unlike every other subcommand (docs/cli.md section 6): pure
client-side computation means 1 and 254 cannot happen - 0 on success, 252 for
botocore's client-side parameter validation (empty bucket/key) and usage
errors, 255 for a non-integer ``--expires-in`` (aws's bare ``int()``
conversion escapes to its general handler).
"""

from __future__ import annotations

from typing import Any

import pytest

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context
from tests.utils.harness import client_ctx, unused_ctx


class _FakePresignClient:
    def __init__(self, url: str = "https://example.test/presigned?X-Amz-Expires=3600") -> None:
        self.url = url
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    def generate_presigned_url(self, method: str, **kwargs: Any) -> str:
        self.calls.append((method, kwargs["Params"], kwargs["ExpiresIn"]))
        return self.url


def _real_client_ctx() -> Context:
    # A real botocore client (still offline: presign never sends a request)
    # so the client-side parameter validation the 252 paths rest on fires.
    import boto3

    return Context(
        client_factory=lambda _args: boto3.session.Session().client("s3", region_name="us-east-1")
    )


class TestPresign:
    def test_prints_url_and_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        client = _FakePresignClient()
        rc = cli.main(["presign", "s3://bucket/key.txt"], ctx=client_ctx(client))
        assert rc == 0
        assert capsys.readouterr().out == client.url + "\n"
        assert client.calls == [("get_object", {"Bucket": "bucket", "Key": "key.txt"}, 3600)]

    def test_scheme_is_optional(self) -> None:
        # aws-cli only strips a *present* s3:// prefix; unlike mb/rb/rm a
        # bare bucket/key path is accepted.
        client = _FakePresignClient()
        assert cli.main(["presign", "bucket/key"], ctx=client_ctx(client)) == 0
        assert client.calls[0][1] == {"Bucket": "bucket", "Key": "key"}

    def test_expires_in_converted_to_int(self) -> None:
        client = _FakePresignClient()
        assert cli.main(["presign", "s3://b/k", "--expires-in", "120"], ctx=client_ctx(client)) == 0
        assert client.calls[0][2] == 120

    def test_negative_expires_in_passes_through(self) -> None:
        # No range validation anywhere: aws signs -1 and S3 rejects at use.
        client = _FakePresignClient()
        assert cli.main(["presign", "s3://b/k", "--expires-in", "-1"], ctx=client_ctx(client)) == 0
        assert client.calls[0][2] == -1


class TestPresignExitCodeShape:
    def test_non_integer_expires_in_exits_255(self, capsys: pytest.CaptureFixture[str]) -> None:
        # aws converts integer options with a bare int(); the ValueError hits
        # its *general* handler -> 255, before any client.
        def factory(_args: Any) -> Any:
            raise AssertionError("client factory must not be called")

        rc = cli.main(
            ["presign", "s3://b/k", "--expires-in", "abc"], ctx=Context(client_factory=factory)
        )
        assert rc == 255
        assert "invalid literal for int()" in capsys.readouterr().err

    def test_bucket_only_is_param_validation_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["presign", "s3://bucket-only"], ctx=_real_client_ctx())
        err = capsys.readouterr().err
        assert rc == 252
        assert "Invalid length for parameter Key" in err

    def test_trailing_slash_is_param_validation_252(self) -> None:
        assert cli.main(["presign", "s3://bucket-only/"], ctx=_real_client_ctx()) == 252

    def test_empty_uri_is_param_validation_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["presign", "s3://"], ctx=_real_client_ctx())
        assert rc == 252
        assert "Invalid bucket name" in capsys.readouterr().err

    def test_extra_positional_is_unknown_options_252(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def factory(_args: Any) -> Any:
            raise AssertionError("client factory must not be called")

        rc = cli.main(["presign", "s3://b/k", "extra-arg"], ctx=Context(client_factory=factory))
        assert rc == 252
        assert "Unknown options: extra-arg" in capsys.readouterr().err

    def test_object_lambda_arn_stays_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        # S3Storage.validate() ARN rejections must reach main's 252 mapping.
        arn = "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-olap"
        rc = cli.main(["presign", f"s3://{arn}"], ctx=client_ctx(object()))
        assert rc == 252
        assert "S3 Object Lambda" in capsys.readouterr().err


class TestPresignParamfileAndQuery:
    """aws expands the positional path file:// at parse time and compiles
    --query there; a bad reference / expression is its 252 before any signing."""

    def test_positional_file_reference_is_expanded(self, tmp_path: Any) -> None:
        ref = tmp_path / "u.txt"
        ref.write_text("s3://b/k")
        client = _FakePresignClient()
        assert cli.main(["presign", f"file://{ref}"], ctx=client_ctx(client)) == 0
        assert client.calls[0][1] == {"Bucket": "b", "Key": "k"}

    def test_positional_missing_paramfile_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["presign", "file:///no/x"], ctx=unused_ctx())
        assert rc == 252
        assert "Error parsing parameter 'path': Unable to load paramfile" in capsys.readouterr().err

    def test_invalid_query_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["presign", "s3://b/k", "--query", "]["], ctx=unused_ctx())
        assert rc == 252
        assert "Bad value for --query ][" in capsys.readouterr().err
