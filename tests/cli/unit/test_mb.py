"""Unit tests for the ``mb`` subcommand: output, exit-code shape, request params.

The mb exit-code shape (aws-cli MbCommand): usage errors - a non-s3 path, an
S3 Express (``--x-s3``) bucket, rejected ARN forms - are 252; everything after
the operation starts is rc 1 with one ``make_bucket failed:`` line, never 254
(aws catches every create_bucket exception locally).
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from boto3_s3 import Boto3S3Error
from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context
from tests.utils.harness import run_cli_in_process
from tests.utils.recorder import make_recording_client


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "CreateBucket",
    )


def _ctx(client: Any) -> Context:
    return Context(client_factory=lambda _args: client)  # pyright: ignore[reportArgumentType]


def _unused_factory(_args: Any) -> Any:
    raise AssertionError("client factory must not be called on this path")


def _run_recorded(parsed_responses: list[dict[str, Any]], argv: list[str]) -> Any:
    client, calls = make_recording_client(parsed_responses)
    return run_cli_in_process(argv, ctx=_ctx(client)), calls


class _RaisingCreateClient:
    """Fake whose CreateBucket raises (needs meta for the region lookup)."""

    class _Meta:
        region_name = "us-east-1"

    meta = _Meta()

    def __init__(self, error: ClientError) -> None:
        self._error = error

    def create_bucket(self, **_kwargs: Any) -> dict[str, Any]:
        raise self._error


class TestOutput:
    def test_success_prints_make_bucket_line(self) -> None:
        result, calls = _run_recorded([{}], ["mb", "s3://b"])
        assert (result.rc, result.stdout, result.stderr) == (0, "make_bucket: b\n", "")
        # The recording client sits in us-east-1, so no CreateBucketConfiguration.
        assert [(c.operation, c.params) for c in calls] == [("CreateBucket", {"Bucket": "b"})]

    def test_key_part_is_silently_dropped(self) -> None:
        # aws mb keeps only the bucket of the path (split_s3_bucket_key).
        result, calls = _run_recorded([{}], ["mb", "s3://b/some/key"])
        assert (result.rc, result.stdout) == (0, "make_bucket: b\n")
        assert calls[0].params == {"Bucket": "b"}

    def test_tags_pairs_keep_order_and_duplicates(self) -> None:
        result, calls = _run_recorded(
            [{}], ["mb", "s3://b", "--tags", "K", "1", "--tags", "K", "2"]
        )
        assert result.rc == 0
        assert calls[0].params == {
            "Bucket": "b",
            "CreateBucketConfiguration": {
                "Tags": [{"Key": "K", "Value": "1"}, {"Key": "K", "Value": "2"}]
            },
        }


class TestUsageErrors:
    # mb builds the client before the path checks (aws order), so these paths
    # consult the factory; a benign one (returns None, never used before the
    # rejection) keeps the assertion on the path usage error (252).
    def test_no_scheme_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["mb", "bucket"], ctx=_ctx(None))
        assert rc == 252
        assert "Invalid argument type" in capsys.readouterr().err

    def test_bare_bucket_key_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["mb", "bucket/key"], ctx=_ctx(None))
        assert rc == 252
        assert "Invalid argument type" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "path", ["s3://bucket--usw2-az1--x-s3", "s3://bucket--usw2-az1--x-s3/"]
    )
    def test_express_bucket_is_252(self, path: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["mb", path], ctx=_ctx(None))
        assert rc == 252
        assert "Cannot use mb command with a directory bucket." in capsys.readouterr().err

    def test_client_construction_error_beats_path_usage_error(self) -> None:
        # aws builds the client before the path check, so a construction error
        # (bad --profile / unresolved creds) wins over the path 252.
        def boom(_args: Any) -> Any:
            raise Boto3S3Error("Unable to locate credentials")

        rc = cli.main(["mb", "bucket"], ctx=Context(client_factory=boom))
        assert rc == 255  # the construction error, not the scheme 252

    def test_object_lambda_arn_is_252_not_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The S3Storage.validate() rejection must stay outside mb's rc-1 catch.
        arn = "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-ap"
        rc = cli.main(["mb", f"s3://{arn}"], ctx=_ctx(None))
        assert rc == 252
        assert "make_bucket failed" not in capsys.readouterr().err

    def test_tags_missing_value_is_252(self) -> None:
        result = run_cli_in_process(
            ["mb", "s3://b", "--tags", "K"], ctx=Context(client_factory=_unused_factory)
        )
        assert result.rc == 252

    def test_tags_extra_token_is_unknown_options(self) -> None:
        result = run_cli_in_process(
            ["mb", "s3://b", "--tags", "K", "V", "Extra"],
            ctx=Context(client_factory=_unused_factory),
        )
        assert result.rc == 252
        assert "Unknown options: Extra" in result.stderr


class TestPostStartErrors:
    @pytest.mark.parametrize("path", ["s3://", "s3:///k"])
    def test_empty_bucket_is_rc_1(self, path: str) -> None:
        # aws sends Bucket="" and botocore's client-side validation fails
        # inside mb's local catch -> rc 1 (the empty-bucket check short-circuits
        # before the client is used, so a benign factory suffices).
        result = run_cli_in_process(["mb", path], ctx=_ctx(None))
        assert result.rc == 1
        # botocore's ParamValidationError str uses a colon + newline (aws wording).
        assert result.stderr == (
            f'make_bucket failed: {path} Parameter validation failed:\nInvalid bucket name ""\n'
        )

    def test_create_failure_is_rc_1_not_254(self) -> None:
        client = _RaisingCreateClient(_client_error("BucketAlreadyOwnedByYou", 409))
        result = run_cli_in_process(["mb", "s3://b"], ctx=_ctx(client))
        assert result.rc == 1
        assert result.stdout == ""
        assert result.stderr.startswith("make_bucket failed: s3://b ")
        assert "BucketAlreadyOwnedByYou" in result.stderr
