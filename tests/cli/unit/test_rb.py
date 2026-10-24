"""Unit tests for the ``rb`` subcommand: output, exit-code shape, --force flow.

The rb exit-code shape (aws-cli RbCommand): usage errors - a non-s3 path, a
key part, rejected ARN forms - are 252; a ``--force`` whose inner ``rm
--recursive`` fails is rc **255** (aws-cli raises RuntimeError into the general
handler) without attempting the bucket delete; everything after delete_bucket
starts is rc 1 with one ``remove_bucket failed:`` line. The wrapping is by
return code only: an exception out of the inner rm - its own rc-255 report -
escapes unwrapped, exactly as aws-cli's does.
"""

from __future__ import annotations

import datetime as dt
import errno
import io
import sys
from typing import Any

import pytest
from botocore.exceptions import ClientError

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context
from tests.utils.fakes3 import client_error
from tests.utils.harness import (
    client_ctx,
    run_cli_in_process,
    run_recorded,
    unused_ctx,
)
from tests.utils.recorder import make_recording_client

_MTIME = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)


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
                def pages() -> Any:
                    raise error
                    yield  # pragma: no cover - marks this function as a generator

                return pages()

        return _Paginator()

    def delete_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_bucket_calls.append(kwargs)
        return {}


class TestOutput:
    def test_success_prints_remove_bucket_line(self) -> None:
        result, calls = run_recorded([{}], ["rb", "s3://b"])
        assert (result.rc, result.stdout, result.stderr) == (0, "remove_bucket: b\n", "")
        assert [(c.operation, c.params) for c in calls] == [("DeleteBucket", {"Bucket": "b"})]

    def test_trailing_slash_only_is_accepted(self) -> None:
        # "s3://b/" has an empty key, which passes aws-cli's key check too.
        result, calls = run_recorded([{}], ["rb", "s3://b/"])
        assert (result.rc, result.stdout) == (0, "remove_bucket: b\n")
        assert calls[0].params == {"Bucket": "b"}


class TestUsageErrors:
    # A benign factory keeps the assertion on the path error for the checks
    # made once the client is built.
    def test_no_scheme_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["rb", "bucket"], ctx=client_ctx(None))
        assert rc == 252
        err = capsys.readouterr().err
        assert "<S3Uri>\nError: Invalid argument type" in err
        assert "usage:" not in err  # rb raises aws-cli's bare form, like mb

    @pytest.mark.parametrize("argv", [["rb", "s3://b/key"], ["rb", "s3://b/key", "--force"]])
    def test_key_part_is_252(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(argv, ctx=client_ctx(None))
        assert rc == 252
        assert "valid bucket name only" in capsys.readouterr().err

    def test_positional_missing_paramfile_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        # aws-cli expands the positional file:// at parse time. Names it 'path'.
        rc = cli.main(["rb", "file:///no/x"], ctx=unused_ctx())
        assert rc == 252
        assert "Error parsing parameter 'path': Unable to load paramfile" in capsys.readouterr().err

    def test_object_lambda_arn_is_252_not_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        arn = "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-ap"
        rc = cli.main(["rb", f"s3://{arn}"], ctx=client_ctx(None))
        assert rc == 252
        assert "remove_bucket failed" not in capsys.readouterr().err


class TestPostStartErrors:
    def test_delete_failure_is_rc_1(self) -> None:
        client = _RaisingDeleteBucketClient(client_error("NoSuchBucket", 404, "DeleteBucket"))
        result = run_cli_in_process(["rb", "s3://b"], ctx=client_ctx(client))
        assert result.rc == 1
        assert result.stdout == ""
        assert result.stderr.startswith("remove_bucket failed: s3://b ")
        assert "NoSuchBucket" in result.stderr

    @pytest.mark.parametrize("stderr", ["closed", "full"])
    def test_a_failure_line_stderr_cannot_take_keeps_rc_1(
        self, stderr: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `2>&-` (no stderr at all) or `2>/dev/full`: the lost failure line
        # changes no code - not 255 through the dispatcher's backstop.
        class _FullStderr:
            encoding = "utf-8"

            def write(self, _text: str) -> int:
                raise OSError(errno.ENOSPC, "No space left on device")

            def flush(self) -> None:
                raise OSError(errno.ENOSPC, "No space left on device")

        client = _RaisingDeleteBucketClient(client_error("NoSuchBucket", 404, "DeleteBucket"))
        monkeypatch.setattr(sys, "stderr", None if stderr == "closed" else _FullStderr())
        assert cli.main(["rb", "s3://b"], ctx=client_ctx(client)) == 1

    def test_bucket_not_empty_is_rc_1(self) -> None:
        client = _RaisingDeleteBucketClient(client_error("BucketNotEmpty", 409, "DeleteBucket"))
        result = run_cli_in_process(["rb", "s3://b"], ctx=client_ctx(client))
        assert result.rc == 1
        assert "BucketNotEmpty" in result.stderr

    def test_a_success_line_stdout_cannot_take_is_lost_output(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The bucket was removed, so it is not reported as an rb failure (aws-cli's
        # catch spans the line and does: rc 1, `remove_bucket failed:`); the
        # lost line is reported as lost output, 255.
        class _FullStdout(io.StringIO):
            def write(self, text: str) -> int:
                raise OSError(28, "No space left on device")

        client, calls = make_recording_client([{}])
        monkeypatch.setattr(sys, "stdout", _FullStdout())
        rc = cli.main(["rb", "s3://b"], ctx=client_ctx(client))
        assert rc == 255
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: [Errno 28] No space left on device\n"
        assert [c.operation for c in calls] == ["DeleteBucket"]

    def test_a_run_without_a_stdout_keeps_its_code(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `1>&-` leaves sys.stdout None: nothing to write the line to, and the
        # code is the work's.
        client, calls = make_recording_client([{}])
        monkeypatch.setattr(sys, "stdout", None)
        rc = cli.main(["rb", "s3://b"], ctx=client_ctx(client))
        assert rc == 0
        assert capsys.readouterr().err == ""
        assert [c.operation for c in calls] == ["DeleteBucket"]

    @pytest.mark.parametrize("uri", ["s3://", "s3:///k"])
    @pytest.mark.parametrize("force", [[], ["--force"]], ids=["plain", "force"])
    def test_a_path_with_no_bucket_is_a_usage_error(self, uri: str, force: list[str]) -> None:
        # Refused before the client is built (252), --force or not: aws-cli sends
        # Bucket="" and reports an rb failure at rc 1, or with --force runs
        # the inner rm first and ends at 255.
        result = run_cli_in_process(["rb", uri, *force], ctx=unused_ctx())
        assert result.rc == 252
        assert 'Invalid bucket name ""' in result.stderr


class TestForce:
    def test_force_empty_bucket_deletes_bucket_only(self) -> None:
        # Listing page with no contents, then DeleteBucket.
        result, calls = run_recorded([{}, {}], ["rb", "s3://b", "--force"])
        assert (result.rc, result.stdout) == (0, "remove_bucket: b\n")
        assert [c.operation for c in calls] == ["ListObjectsV2", "DeleteBucket"]

    def test_force_non_empty_deletes_objects_then_bucket(self) -> None:
        page = {"Contents": [{"Key": "foo", "Size": 100, "LastModified": _MTIME}]}
        result, calls = run_recorded([page, {}, {}], ["rb", "s3://b", "--force"])
        assert result.rc == 0
        assert [c.operation for c in calls] == ["ListObjectsV2", "DeleteObjects", "DeleteBucket"]
        # The inner rm streams its delete lines before the remove_bucket line.
        assert "delete: s3://b/foo\n" in result.stdout
        assert result.stdout.endswith("remove_bucket: b\n")

    def test_force_failure_is_255_and_skips_delete_bucket(self) -> None:
        client = _ForceFailClient(client_error("NoSuchBucket", 404, "ListObjectsV2"))
        result = run_cli_in_process(["rb", "s3://b", "--force"], ctx=client_ctx(client))
        assert result.rc == 255
        # The inner rm printed its own fatal line, then rb's own failure.
        assert "fatal error:" in result.stderr
        assert "remove_bucket failed:" in result.stderr
        assert client.delete_bucket_calls == []

    def test_an_inner_rm_255_escapes_unwrapped(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli's RbCommand._force wraps a nonzero *return code* in its
        # RuntimeError; an exception out of the inner rm - its own rc-255
        # report - propagates to the outer handler untouched, so
        # `remove_bucket failed:` never appears. Measured on the pinned aws-cli
        # with an invalid [s3] value, which the inner rm now reads like aws-cli's.
        config = tmp_path / "config"
        config.write_text(
            "[default]\nregion = us-east-1\ns3 =\n  preferred_transfer_client = bogus\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        client, calls = make_recording_client([])
        result = run_cli_in_process(["rb", "s3://b", "--force"], ctx=client_ctx(client))
        assert result.rc == 255
        assert "preferred_transfer_client" in result.stderr
        assert "remove_bucket failed" not in result.stderr
        assert calls == []

    def test_force_shares_rb_session_and_keeps_rm_defaults(self) -> None:
        # The inner rm runs on rb's own S3 (Context.with_s3) exactly as aws-cli
        # hands its one CLI session to RmCommand(self._session): every client
        # the force flow builds comes from rb's namespace - a second session
        # would resolve credentials again (credential_process / MFA
        # re-prompting). The rm-only defaults still come from rm's own parser,
        # observable on the wire: recursive listing with no MaxKeys (unset
        # --page-size), no dryrun, then the bucket delete.
        captured: list[Any] = []
        client, calls = make_recording_client([{}, {}])

        def factory(args: Any) -> Any:
            captured.append(args)
            return client

        result = run_cli_in_process(
            ["--region", "ap-northeast-1", "rb", "s3://b", "--force"],
            ctx=Context(client_factory=factory),  # pyright: ignore[reportArgumentType]
        )
        assert result.rc == 0
        # rb's eager build plus the inner rm's build - both served through the
        # shared S3, so both see rb's namespace (the same object).
        assert len(captured) == 2
        assert captured[0] is captured[1]
        assert captured[0].region == "ap-northeast-1"
        assert not hasattr(captured[0], "recursive")  # rb's namespace, not rm's
        assert [c.operation for c in calls] == ["ListObjectsV2", "DeleteBucket"]
        assert "MaxKeys" not in calls[0].params  # rm's own unset --page-size default
