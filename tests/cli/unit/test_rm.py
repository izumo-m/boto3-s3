"""Unit tests for the ``rm`` subcommand: output matrix, exit-code shape, filters.

The rm exit-code shape differs from ls (docs/cli.md section 6): usage errors
(non-s3 path, rejected ARNs) are 252, but every error after the operation
starts is rc 1 - per-key failures as ``delete failed:`` lines, run-killing
errors as one ``fatal error:`` line - never 254.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from botocore.exceptions import ClientError

from boto3_s3 import FileInfo
from boto3_s3.globsieve import GlobPattern, PatternKind
from boto3_s3_cli import cli, filters
from boto3_s3_cli.commands.base import Context
from tests.utils.harness import run_cli_in_process
from tests.utils.recorder import make_recording_client

_MTIME = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)


def _obj(key: str, size: int = 1) -> dict[str, Any]:
    return {"Key": key, "Size": size, "LastModified": _MTIME}


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "message"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Operation",
    )


def _ctx(client: Any) -> Context:
    return Context(client_factory=lambda _args: client)  # pyright: ignore[reportArgumentType]


def _run_recorded(parsed_responses: list[dict[str, Any]], argv: list[str]) -> Any:
    client, calls = make_recording_client(parsed_responses)
    return run_cli_in_process(argv, ctx=_ctx(client)), calls


class _RaisingPaginatorClient:
    """Fake whose ListObjectsV2 paginator raises on iteration."""

    def __init__(self, error: ClientError) -> None:
        self._error = error

    def get_paginator(self, name: str) -> Any:
        error = self._error

        class _Paginator:
            def paginate(self, **_kwargs: Any) -> Any:
                raise error

        return _Paginator()


class _RaisingDeleteClient:
    """Fake whose DeleteObject raises."""

    def __init__(self, error: ClientError) -> None:
        self._error = error

    def delete_object(self, **_kwargs: Any) -> dict[str, Any]:
        raise self._error


class TestOutputMatrix:
    def test_default_prints_delete_line(self) -> None:
        result, _ = _run_recorded([{}], ["rm", "s3://b/k"])
        assert result.rc == 0
        assert result.stdout == "delete: s3://b/k\n"

    def test_quiet_silences_success(self) -> None:
        result, calls = _run_recorded([{}], ["rm", "s3://b/k", "--quiet"])
        assert (result.rc, result.stdout, result.stderr) == (0, "", "")
        assert len(calls) == 1  # the delete still ran

    def test_only_show_errors_silences_success(self) -> None:
        result, _ = _run_recorded([{}], ["rm", "s3://b/k", "--only-show-errors"])
        assert (result.rc, result.stdout) == (0, "")

    def test_only_show_errors_still_prints_dryrun(self) -> None:
        # aws quirk: OnlyShowErrorsResultPrinter does not override
        # the dryrun line.
        result, calls = _run_recorded([], ["rm", "s3://b/k", "--dryrun", "--only-show-errors"])
        assert (result.rc, result.stdout) == (0, "(dryrun) delete: s3://b/k\n")
        assert calls == []

    def test_quiet_silences_dryrun(self) -> None:
        result, _ = _run_recorded([], ["rm", "s3://b/k", "--dryrun", "--quiet"])
        assert (result.rc, result.stdout, result.stderr) == (0, "", "")

    def test_quiet_silences_failure_lines_but_not_rc(self) -> None:
        # aws --quiet builds no result printer at all: even failure/fatal
        # lines vanish while the rc stays 1.
        client = _RaisingDeleteClient(_client_error("NoSuchBucket", 404))
        result = run_cli_in_process(["rm", "s3://b/k", "--quiet"], ctx=_ctx(client))
        assert (result.rc, result.stdout, result.stderr) == (1, "", "")

    def test_quiet_silences_fatal_lines_but_not_rc(self) -> None:
        client = _RaisingPaginatorClient(_client_error("NoSuchBucket", 404))
        result = run_cli_in_process(["rm", "s3://b/", "--recursive", "--quiet"], ctx=_ctx(client))
        assert (result.rc, result.stdout, result.stderr) == (1, "", "")


class TestExitCodeShape:
    def test_local_path_is_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["rm", "/tmp/foo"], ctx=Context(client_factory=lambda _a: None))  # pyright: ignore[reportArgumentType]
        err = capsys.readouterr().err
        assert rc == 252
        assert "Invalid argument type" in err

    def test_bare_relative_path_is_usage_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["rm", "bucket/key"], ctx=Context(client_factory=lambda _a: None))  # pyright: ignore[reportArgumentType]
        assert rc == 252
        assert "Invalid argument type" in capsys.readouterr().err

    def test_per_key_failure_is_rc_1_with_delete_failed(self) -> None:
        client = _RaisingDeleteClient(_client_error("NoSuchBucket", 404))
        result = run_cli_in_process(["rm", "s3://b-no-such/k"], ctx=_ctx(client))
        assert result.rc == 1
        assert result.stderr.startswith("delete failed: s3://b-no-such/k ")
        assert "NoSuchBucket" in result.stderr

    def test_listing_failure_is_rc_1_fatal_not_254(self) -> None:
        # ls maps a server ClientError to 254; rm must report rc 1 with a
        # "fatal error:" line instead (aws transfer-command convention).
        client = _RaisingPaginatorClient(_client_error("NoSuchBucket", 404))
        result = run_cli_in_process(["rm", "s3://b-no-such/", "--recursive"], ctx=_ctx(client))
        assert result.rc == 1
        assert result.stderr.startswith("fatal error: ")
        assert "NoSuchBucket" in result.stderr

    def test_object_lambda_arn_stays_usage_error(self) -> None:
        # S3Storage.validate() ARN rejections must reach main's 252 mapping, not
        # be swallowed into rm's rc-1 fatal path.
        arn = "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-olap"
        result = run_cli_in_process(["rm", f"s3://{arn}"], ctx=_ctx(object()))
        assert result.rc == 252
        assert "S3 Object Lambda" in result.stderr

    def test_empty_bucket_no_key_is_rc_1_fatal(self) -> None:
        result = run_cli_in_process(["rm", "s3://"], ctx=_ctx(object()))
        assert result.rc == 1
        assert result.stderr.startswith("fatal error: ")
        assert "Invalid bucket name" in result.stderr

    def test_non_integer_page_size_is_rc_255(self) -> None:
        # aws converts integer options with a bare int(); the ValueError hits
        # its *general* handler -> 255. It converts at
        # parse time, so 255 wins even over rm's 252 path check, and no
        # client is ever built.
        def factory(_args: Any) -> Any:
            raise AssertionError("client factory must not be called")

        result = run_cli_in_process(
            ["rm", "/local/path", "--page-size", "abc"], ctx=Context(client_factory=factory)
        )
        assert result.rc == 255
        assert "invalid literal for int()" in result.stderr

    def test_empty_bucket_with_key_is_rc_1_delete_failed(self) -> None:
        # aws sends Bucket="" to DeleteObject and botocore fails client-side:
        # shaped as a per-key failure, not a usage error.
        result = run_cli_in_process(["rm", "s3:///key"], ctx=_ctx(object()))
        assert result.rc == 1
        assert result.stderr.startswith("delete failed: s3:///key ")
        assert "Invalid bucket name" in result.stderr


class TestFilterWiring:
    def test_interleaved_order_reaches_the_matcher(self) -> None:
        responses = [
            {"Contents": [_obj("p/a.txt"), _obj("p/b.bin")]},
            {},
        ]
        result, calls = _run_recorded(
            responses,
            ["rm", "s3://b/p/", "--recursive", "--exclude", "*", "--include", "*.txt"],
        )
        assert result.rc == 0
        assert result.stdout == "delete: s3://b/p/a.txt\n"
        deletes = [c for c in calls if c.operation == "DeleteObjects"]
        assert deletes[0].params["Delete"]["Objects"] == [{"Key": "p/a.txt"}]

    def test_reverse_order_excludes_everything(self) -> None:
        responses = [{"Contents": [_obj("p/a.txt")]}]
        result, calls = _run_recorded(
            responses,
            ["rm", "s3://b/p/", "--recursive", "--include", "*.txt", "--exclude", "*"],
        )
        assert (result.rc, result.stdout) == (0, "")
        assert [c.operation for c in calls] == ["ListObjectsV2"]

    def test_exclude_on_single_key_is_silent_success(self) -> None:
        result, calls = _run_recorded([], ["rm", "s3://b/data/a.txt", "--exclude", "*"])
        assert (result.rc, result.stdout, result.stderr) == (0, "", "")
        assert calls == []


class TestAppendFilterAction:
    def test_preserves_interleaved_order(self) -> None:
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--exclude", action=filters.AppendFilterAction, dest="filters")
        parser.add_argument("--include", action=filters.AppendFilterAction, dest="filters")
        args = parser.parse_args(["--include", "a", "--exclude", "b", "--include", "c"])
        assert args.filters == [
            GlobPattern(PatternKind.INCLUDE, "a"),
            GlobPattern(PatternKind.EXCLUDE, "b"),
            GlobPattern(PatternKind.INCLUDE, "c"),
        ]

    def test_compile_filter_none_for_no_patterns(self) -> None:
        assert filters.compile_filter(None) is None
        assert filters.compile_filter([]) is None

    def test_absolute_pattern_is_dead_against_an_s3_key(self) -> None:
        # rm lists S3 keys, which carry no drive / UNC anchor, so an absolute
        # pattern can never match one (os.path.join drops the root onto an
        # anchorless key, fnmatch misses) - exactly aws-cli, whose s3 paths are
        # anchorless. Only the relative '*' bites here.
        keep = filters.compile_filter(
            [GlobPattern.exclude("/elsewhere/*"), GlobPattern.exclude("*")]
        )
        assert keep is not None
        assert keep(FileInfo(key="anything", compare_key="anything")) is False
