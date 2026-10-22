"""Unit tests for the ``mb`` subcommand: output, exit-code shape, request params.

The mb exit-code shape (aws-cli MbCommand): usage errors - a non-s3 path, an
S3 Express (``--x-s3``) bucket, rejected ARN forms - are 252; everything after
the operation starts is rc 1 with one ``make_bucket failed:`` line, never 254
(aws catches every create_bucket exception locally).
"""

from __future__ import annotations

import io
import sys
from typing import Any

import pytest
from botocore.exceptions import ClientError

from boto3_s3_cli import cli
from tests.utils.fakes3 import client_error
from tests.utils.harness import (
    client_ctx,
    run_cli_in_process,
    run_recorded,
    unused_ctx,
)
from tests.utils.recorder import make_recording_client


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
        result, calls = run_recorded([{}], ["mb", "s3://b"])
        assert (result.rc, result.stdout, result.stderr) == (0, "make_bucket: b\n", "")
        # The recording client sits in us-east-1, so no CreateBucketConfiguration.
        assert [(c.operation, c.params) for c in calls] == [("CreateBucket", {"Bucket": "b"})]

    def test_key_part_is_silently_dropped(self) -> None:
        # aws mb keeps only the bucket of the path (split_s3_bucket_key).
        result, calls = run_recorded([{}], ["mb", "s3://b/some/key"])
        assert (result.rc, result.stdout) == (0, "make_bucket: b\n")
        assert calls[0].params == {"Bucket": "b"}

    def test_dash_led_tag_tokens_are_taken_as_the_pair(self) -> None:
        # aws declares --tags with a two-token count, so both tokens are the
        # pair however they look and the bucket is created (measured: `mb
        # --tags -k -v s3://a` reaches CreateBucket on aws, where our pre-3.12
        # argparse used to report a missing value instead).
        result, calls = run_recorded([{}], ["mb", "s3://b", "--tags", "-k", "-v"])
        assert result.rc == 0
        assert calls[0].params == {
            "Bucket": "b",
            "CreateBucketConfiguration": {"Tags": [{"Key": "-k", "Value": "-v"}]},
        }

    def test_a_dash_led_pair_parses_with_the_paths_intact(self) -> None:
        # Through the parser the dispatch really parses with: the swallowed
        # tokens are the pair, not positionals, so the path still arrives.
        parser = cli._build_command_parse_parser("mb", cli._load_command("mb")())
        args = parser.parse_args(["--tags", "-k", "-v", "--tags", "K", "V", "s3://b"])
        assert args.tags == [["-k", "-v"], ["K", "V"]]
        assert args.path == "s3://b"

    def test_tags_pairs_keep_order_and_duplicates(self) -> None:
        result, calls = run_recorded([{}], ["mb", "s3://b", "--tags", "K", "1", "--tags", "K", "2"])
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
        rc = cli.main(["mb", "bucket"], ctx=client_ctx(None))
        assert rc == 252
        err = capsys.readouterr().err
        assert "<S3Uri>\nError: Invalid argument type" in err
        # aws's MbCommand raises the bare form - only rm's CommandParameters
        # path gets the "usage: aws s3 <cmd> ..." prefix (measured on the pinned aws-cli).
        assert "usage:" not in err

    def test_bare_bucket_key_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["mb", "bucket/key"], ctx=client_ctx(None))
        assert rc == 252
        assert "Invalid argument type" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "path", ["s3://bucket--usw2-az1--x-s3", "s3://bucket--usw2-az1--x-s3/"]
    )
    def test_express_bucket_is_252(self, path: str, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["mb", path], ctx=client_ctx(None))
        assert rc == 252
        assert "directory bucket" in capsys.readouterr().err

    def test_object_lambda_arn_is_252_not_1(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The S3Storage.validate() rejection must stay outside mb's rc-1 catch.
        arn = "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-ap"
        rc = cli.main(["mb", f"s3://{arn}"], ctx=client_ctx(None))
        assert rc == 252
        assert "make_bucket failed" not in capsys.readouterr().err

    def test_slash_form_express_arn_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        # A slash-form accesspoint ARN whose name ends --x-s3: aws's ARN-aware
        # split makes the whole ARN the bucket, so is_s3express_bucket sees the
        # suffix -> 252 (a naive partition on the first "/" would miss it).
        arn = "arn:aws:s3:us-east-1:123456789012:accesspoint/foo--x-s3"
        rc = cli.main(["mb", f"s3://{arn}"], ctx=client_ctx(None))
        assert rc == 252
        assert "directory bucket" in capsys.readouterr().err

    def test_positional_missing_paramfile_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        # aws expands the positional file:// at parse time. Names it 'path'.
        rc = cli.main(["mb", "file:///no/x"], ctx=unused_ctx())
        assert rc == 252
        assert "Error parsing parameter 'path': Unable to load paramfile" in capsys.readouterr().err

    def test_tags_missing_value_is_252(self) -> None:
        result = run_cli_in_process(["mb", "s3://b", "--tags", "K"], ctx=unused_ctx())
        assert result.rc == 252

    def test_tags_extra_token_is_unknown_options(self) -> None:
        result = run_cli_in_process(
            ["mb", "s3://b", "--tags", "K", "V", "Extra"],
            ctx=unused_ctx(),
        )
        assert result.rc == 252
        assert "Unknown options: Extra" in result.stderr


class TestPostStartErrors:
    @pytest.mark.parametrize("uri", ["s3://", "s3:///k"])
    def test_a_path_with_no_bucket_is_a_usage_error(self, uri: str) -> None:
        # Refused before the client is built (252); aws sends Bucket="" and
        # reports an mb failure at rc 1. mb drops the key, so "s3:///k" is
        # the same missing bucket.
        result = run_cli_in_process(["mb", uri], ctx=unused_ctx())
        assert result.rc == 252
        assert 'Invalid bucket name ""' in result.stderr

    def test_create_failure_is_rc_1_not_254(self) -> None:
        client = _RaisingCreateClient(client_error("BucketAlreadyOwnedByYou", 409, "CreateBucket"))
        result = run_cli_in_process(["mb", "s3://b"], ctx=client_ctx(client))
        assert result.rc == 1
        assert result.stdout == ""
        assert result.stderr.startswith("make_bucket failed: s3://b ")
        assert "BucketAlreadyOwnedByYou" in result.stderr

    def test_a_success_line_stdout_cannot_take_is_lost_output(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The bucket was created, so it is not reported as an mb failure (aws's
        # catch spans the line and does: rc 1, `make_bucket failed:`); the
        # lost line is reported as lost output, 255.
        class _FullStdout(io.StringIO):
            def write(self, text: str) -> int:
                raise OSError(28, "No space left on device")

        client, calls = make_recording_client([{}])
        monkeypatch.setattr(sys, "stdout", _FullStdout())
        rc = cli.main(["mb", "s3://b"], ctx=client_ctx(client))
        assert rc == 255
        assert capsys.readouterr().err == "boto3-s3: [ERROR]: [Errno 28] No space left on device\n"
        assert [c.operation for c in calls] == ["CreateBucket"]

    def test_a_run_without_a_stdout_keeps_its_code(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `1>&-` leaves sys.stdout None: nothing to write the line to, and the
        # code is the work's.
        client, calls = make_recording_client([{}])
        monkeypatch.setattr(sys, "stdout", None)
        rc = cli.main(["mb", "s3://b"], ctx=client_ctx(client))
        assert rc == 0
        assert capsys.readouterr().err == ""
        assert [c.operation for c in calls] == ["CreateBucket"]
