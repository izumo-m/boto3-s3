"""Unit tests for the ``rb`` subcommand: output, exit-code shape, --force flow.

The rb exit-code shape (aws-cli RbCommand): usage errors - a non-s3 path, a
key part, rejected ARN forms - are 252; a ``--force`` whose inner ``rm
--recursive`` fails is rc **255** (aws raises RuntimeError into the general
handler) without attempting the bucket delete; everything after delete_bucket
starts is rc 1 with one ``remove_bucket failed:`` line.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from botocore.exceptions import ClientError

from boto3_s3 import Boto3S3Error
from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context
from tests.utils.harness import CliResult, run_cli_in_process
from tests.utils.recorder import ApiCall, make_recording_client

_MTIME = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "DeleteBucket",
    )


def _ctx(client: Any) -> Context:
    return Context(client_factory=lambda _args: client)  # pyright: ignore[reportArgumentType]


def _unused_factory(_args: Any) -> Any:
    raise AssertionError("client factory must not be called")


def _run_recorded(
    parsed_responses: list[dict[str, Any]], argv: list[str]
) -> tuple[CliResult, list[ApiCall]]:
    client, calls = make_recording_client(parsed_responses)
    return run_cli_in_process(argv, ctx=_ctx(client)), calls


class _RaisingDeleteBucketClient:
    """Fake whose DeleteBucket raises."""

    def __init__(self, error: ClientError) -> None:
        self._error = error

    def delete_bucket(self, **_kwargs: Any) -> dict[str, Any]:
        raise self._error


class _ForceFailClient:
    """Fake whose listing raises (kills the inner rm) and records DeleteBucket."""

    def __init__(self, error: ClientError) -> None:
        self._error = error
        self.delete_bucket_calls: list[dict[str, Any]] = []

    def get_paginator(self, _name: str) -> Any:
        error = self._error

        class _Paginator:
            def paginate(self, **_kwargs: Any) -> Any:
                raise error

        return _Paginator()

    def delete_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_bucket_calls.append(kwargs)
        return {}


class TestOutput:
    def test_success_prints_remove_bucket_line(self) -> None:
        result, calls = _run_recorded([{}], ["rb", "s3://b"])
        assert (result.rc, result.stdout, result.stderr) == (0, "remove_bucket: b\n", "")
        assert [(c.operation, c.params) for c in calls] == [("DeleteBucket", {"Bucket": "b"})]

    def test_trailing_slash_only_is_accepted(self) -> None:
        # "s3://b/" has an empty key, which passes aws's key check too.
        result, calls = _run_recorded([{}], ["rb", "s3://b/"])
        assert (result.rc, result.stdout) == (0, "remove_bucket: b\n")
        assert calls[0].params == {"Bucket": "b"}


class TestUsageErrors:
    # rb builds the client before the path checks (aws order), so these paths
    # consult the factory; a benign one keeps the assertion on the path error.
    def test_no_scheme_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["rb", "bucket"], ctx=_ctx(None))
        assert rc == 252
        err = capsys.readouterr().err
        assert "<S3Uri>\nError: Invalid argument type" in err
        assert "usage:" not in err  # rb raises aws's bare form, like mb

    def test_client_construction_error_beats_path_usage_error(self) -> None:
        # aws builds the client before the path check; a construction error wins.
        def boom(_args: Any) -> Any:
            raise Boto3S3Error("Unable to locate credentials")

        assert cli.main(["rb", "bucket"], ctx=Context(client_factory=boom)) == 255

    @pytest.mark.parametrize("argv", [["rb", "s3://b/key"], ["rb", "s3://b/key", "--force"]])
    def test_key_part_is_252(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(argv, ctx=_ctx(None))
        assert rc == 252
        assert "Please specify a valid bucket name only. E.g. s3://b" in capsys.readouterr().err

    def test_empty_bucket_with_key_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        # aws splits the path first: an empty bucket carrying a key is the key
        # usage error (252), ending in a bare "s3://" - not the empty-bucket rc 1.
        rc = cli.main(["rb", "s3:///k"], ctx=_ctx(None))
        assert rc == 252
        assert "Please specify a valid bucket name only. E.g. s3://\n" in capsys.readouterr().err

    def test_positional_missing_paramfile_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        # aws expands the positional file:// at parse time, before the client
        # build (so a bad reference beats a bad --profile). Names it 'path'.
        rc = cli.main(["rb", "file:///no/x"], ctx=Context(client_factory=_unused_factory))
        assert rc == 252
        assert "Error parsing parameter 'path': Unable to load paramfile" in capsys.readouterr().err

    def test_invalid_query_beats_client_construction(self) -> None:
        def boom(_args: Any) -> Any:
            raise Boto3S3Error("Unable to locate credentials")

        assert cli.main(["rb", "s3://b", "--query", "]["], ctx=Context(client_factory=boom)) == 252

    def test_object_lambda_arn_is_252_not_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        arn = "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-ap"
        rc = cli.main(["rb", f"s3://{arn}"], ctx=_ctx(None))
        assert rc == 252
        assert "remove_bucket failed" not in capsys.readouterr().err


class TestPostStartErrors:
    def test_delete_failure_is_rc_1(self) -> None:
        client = _RaisingDeleteBucketClient(_client_error("NoSuchBucket", 404))
        result = run_cli_in_process(["rb", "s3://b"], ctx=_ctx(client))
        assert result.rc == 1
        assert result.stdout == ""
        assert result.stderr.startswith("remove_bucket failed: s3://b ")
        assert "NoSuchBucket" in result.stderr

    def test_bucket_not_empty_is_rc_1(self) -> None:
        client = _RaisingDeleteBucketClient(_client_error("BucketNotEmpty", 409))
        result = run_cli_in_process(["rb", "s3://b"], ctx=_ctx(client))
        assert result.rc == 1
        assert "BucketNotEmpty" in result.stderr

    def test_empty_uri_is_rc_1(self) -> None:
        # aws sends Bucket="" to DeleteBucket and fails client-side inside the
        # local catch -> rc 1 with the botocore-shaped line (same form as
        # mb/rm), handled before construction so the client is never used.
        client, calls = make_recording_client([])
        result = run_cli_in_process(["rb", "s3://"], ctx=_ctx(client))
        assert result.rc == 1
        assert result.stderr == (
            'remove_bucket failed: s3:// Parameter validation failed:\nInvalid bucket name ""\n'
        )
        assert calls == []


class TestForce:
    def test_force_empty_bucket_deletes_bucket_only(self) -> None:
        # Listing page with no contents, then DeleteBucket.
        result, calls = _run_recorded([{}, {}], ["rb", "s3://b", "--force"])
        assert (result.rc, result.stdout) == (0, "remove_bucket: b\n")
        assert [c.operation for c in calls] == ["ListObjectsV2", "DeleteBucket"]

    def test_force_non_empty_deletes_objects_then_bucket(self) -> None:
        page = {"Contents": [{"Key": "foo", "Size": 100, "LastModified": _MTIME}]}
        result, calls = _run_recorded([page, {}, {}], ["rb", "s3://b", "--force"])
        assert result.rc == 0
        assert [c.operation for c in calls] == ["ListObjectsV2", "DeleteObjects", "DeleteBucket"]
        # The inner rm streams its delete lines before the remove_bucket line.
        assert "delete: s3://b/foo\n" in result.stdout
        assert result.stdout.endswith("remove_bucket: b\n")

    def test_force_failure_is_255_and_skips_delete_bucket(self) -> None:
        client = _ForceFailClient(_client_error("NoSuchBucket", 404))
        result = run_cli_in_process(["rb", "s3://b", "--force"], ctx=_ctx(client))
        assert result.rc == 255
        # The inner rm printed its own fatal line, then the fixed sentence goes
        # through main's general handler with its 'boto3-s3: [ERROR]:' prefix
        # (aws routes its RuntimeError the same way, 'aws: [ERROR]:').
        assert "fatal error:" in result.stderr
        assert (
            "boto3-s3: [ERROR]: remove_bucket failed: Unable to delete all objects in the "
            "bucket, bucket will not be deleted." in result.stderr
        )
        assert client.delete_bucket_calls == []

    def test_force_carries_globals_and_rm_defaults(self) -> None:
        # Pins the namespace synthesis: the factory runs once for rb and once
        # for the inner rm; globals reach both, rm defaults come from its own
        # parser (not from rb's namespace).
        captured: list[Any] = []
        client, _calls = make_recording_client([{}, {}])

        def factory(args: Any) -> Any:
            captured.append(args)
            return client

        result = run_cli_in_process(
            ["--region", "ap-northeast-1", "rb", "s3://b", "--force"],
            ctx=Context(client_factory=factory),  # pyright: ignore[reportArgumentType]
        )
        assert result.rc == 0
        assert len(captured) == 2
        rb_args, rm_args = captured
        assert rb_args.region == "ap-northeast-1"
        assert rm_args.region == "ap-northeast-1"
        assert not hasattr(rb_args, "recursive")
        assert rm_args.paths == "s3://b"
        assert rm_args.recursive is True
        assert rm_args.dryrun is False
        assert rm_args.page_size == 1000
