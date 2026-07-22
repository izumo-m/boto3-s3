# Copyright 2014 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
#
# This file has been modified and ported for boto3-s3.

"""Port of aws-cli's functional ls tests to ``boto3-s3 ls``.

Provenance: aws-cli's ``tests/functional/s3/test_ls_command.py``
(aws-cli 2.36.1). Test names, canned responses, and expected stdout/rc are
kept verbatim where possible so the file stays diffable against the aws-cli
original when aws-cli is updated.

The behaviour under test is aws-cli's s3 command implementation in
``vendor/aws-cli/awscli/customizations/s3/`` - ``subcommands.py`` (``ListCommand``).

A test carrying no ``# aws-cli:`` comment ports the aws-cli test of the same
class and method name. A ``# aws-cli:`` comment names a divergent origin
instead: above a test for a per-test difference (a rename, a parametrized
merge of several aws-cli tests, a method from a different aws-cli class or
file, or ``none`` for a boto3-s3 addition), or above a class when a whole
block was carved out of one aws-cli class under the same method names.

Adaptation rules (aws-cli harness -> ``tests.utils.recorder``):

- ``self.parsed_responses`` / ``self.operations_called`` become
  ``make_recording_client(parsed_responses)`` + the returned call list. The
  recorder intercepts ``_make_api_call``, so responses bypass botocore's
  output parser: ``LastModified`` is a ``datetime`` instead of the aws-cli's
  ISO string, and recorded params predate botocore's parameter-build step -
  the auto-injected ``EncodingType: "url"`` the aws-cli asserts (botocore
  ``set_list_objects_encoding_type_url``, applied to aws-cli and boto3-s3
  alike, decoded symmetrically) never shows up here and is dropped from
  expected params.
- The aws-cli's ``assertNotIn('delimiter', ...)`` (lowercase, vacuously true)
  is tightened to the real ``'Delimiter'`` key.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from boto3_s3_cli.commands.base import Context
from tests.utils.harness import CliResult, run_cli_in_process
from tests.utils.recorder import ApiCall, make_recording_client

_TIME_UTC = dt.datetime(2014, 1, 9, 20, 45, 49, tzinfo=dt.timezone.utc)
# The aws-cli tests shift the stored UTC time to the local timezone because
# that is what the ls output displays; doing the same keeps this file
# timezone-independent.
_TIME_FMT = _TIME_UTC.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _run_cmd(
    parsed_responses: list[dict[str, Any]], argv: list[str]
) -> tuple[CliResult, list[ApiCall]]:
    """The port's ``self.run_cmd``: in-process main() with a recording client."""
    client, calls = make_recording_client(parsed_responses)
    ctx = Context(client_factory=lambda _args: client)
    return run_cli_in_process(argv, ctx=ctx), calls


def _obj(key: str, size: int = 100) -> dict[str, Any]:
    return {"Key": key, "Size": size, "LastModified": _TIME_UTC}


class TestLSCommand:
    def test_operations_used_in_recursive_list(self) -> None:
        responses = [{"CommonPrefixes": [], "Contents": [_obj("foo/bar.txt")]}]
        result, calls = _run_cmd(responses, ["ls", "s3://bucket/", "--recursive"])
        assert result.rc == 0
        call_args = calls[0].params
        # No delimiter: a recursive listing.
        assert call_args["Prefix"] == ""
        assert call_args["Bucket"] == "bucket"
        assert "Delimiter" not in call_args
        assert result.stdout == f"{_TIME_FMT}        100 foo/bar.txt\n"

    def test_errors_out_with_extra_arguments(self) -> None:
        result, _ = _run_cmd([], ["ls", "--extra-argument-foo"])
        assert result.rc == 252
        assert "Unknown options" in result.stderr
        assert "--extra-argument-foo" in result.stderr

    def test_list_buckets_use_page_size(self) -> None:
        result, calls = _run_cmd([{"Buckets": []}], ["ls", "--page-size", "8"])
        assert result.rc == 0
        # The page size gets translated to ``MaxBuckets`` in the s3 model.
        assert calls[0].params["MaxBuckets"] == 8

    def test_operations_use_page_size(self) -> None:
        responses = [{"CommonPrefixes": [], "Contents": [_obj("foo/bar.txt")]}]
        result, calls = _run_cmd(responses, ["ls", "s3://bucket/", "--page-size", "8"])
        assert result.rc == 0
        call_args = calls[0].params
        assert call_args["Prefix"] == ""
        assert call_args["Bucket"] == "bucket"
        # The page size gets translated to ``MaxKeys`` in the s3 model.
        assert call_args["MaxKeys"] == 8

    def test_operations_use_page_size_recursive(self) -> None:
        responses = [{"CommonPrefixes": [], "Contents": [_obj("foo/bar.txt")]}]
        result, calls = _run_cmd(
            responses, ["ls", "s3://bucket/", "--page-size", "8", "--recursive"]
        )
        assert result.rc == 0
        call_args = calls[0].params
        assert call_args["Prefix"] == ""
        assert call_args["Bucket"] == "bucket"
        assert call_args["MaxKeys"] == 8
        assert "Delimiter" not in call_args

    def test_success_rc_has_prefixes_and_objects(self) -> None:
        responses = [{"CommonPrefixes": [{"Prefix": "foo/"}], "Contents": [_obj("foo/bar.txt")]}]
        result, _ = _run_cmd(responses, ["ls", "s3://bucket/foo"])
        assert result.rc == 0

    def test_success_rc_has_only_prefixes(self) -> None:
        responses: list[dict[str, Any]] = [{"CommonPrefixes": [{"Prefix": "foo/"}]}]
        result, _ = _run_cmd(responses, ["ls", "s3://bucket/foo"])
        assert result.rc == 0

    def test_success_rc_has_only_objects(self) -> None:
        responses = [{"Contents": [_obj("foo/bar.txt")]}]
        result, _ = _run_cmd(responses, ["ls", "s3://bucket/foo"])
        assert result.rc == 0

    def test_success_rc_with_pagination(self) -> None:
        # Pagination should not affect a successful return code of zero, even
        # if there are no results on the second page because there were
        # results in previous pages. (aws-cli-verbatim responses: page 1 is not
        # truncated, so the paginator never requests the empty second page -
        # the leftover canned response is allowed, as in the aws-cli harness.)
        responses: list[dict[str, Any]] = [
            {"CommonPrefixes": [{"Prefix": "foo/"}], "Contents": [_obj("foo/bar.txt")]},
            {},
        ]
        result, _ = _run_cmd(responses, ["ls", "s3://bucket/foo"])
        assert result.rc == 0

    def test_success_rc_empty_bucket_no_key_given(self) -> None:
        # If no key has been provided and the bucket is empty, it should
        # still return an rc of 0 since the user is not looking for an actual
        # object.
        result, _ = _run_cmd([{}], ["ls", "s3://bucket"])
        assert result.rc == 0

    def test_fail_rc_no_objects_nor_prefixes(self) -> None:
        result, _ = _run_cmd([{}], ["ls", "s3://bucket/foo"])
        assert result.rc == 1

    def test_human_readable_file_size(self) -> None:
        responses = [
            {
                "CommonPrefixes": [],
                "Contents": [
                    _obj("onebyte.txt", 1),
                    _obj("onekilobyte.txt", 1024),
                    _obj("onemegabyte.txt", 1024**2),
                    _obj("onegigabyte.txt", 1024**3),
                    _obj("oneterabyte.txt", 1024**4),
                    _obj("onepetabyte.txt", 1024**5),
                ],
            }
        ]
        result, _ = _run_cmd(responses, ["ls", "s3://bucket/", "--human-readable"])
        assert result.rc == 0
        assert f"{_TIME_FMT}     1 Byte onebyte.txt\n" in result.stdout
        assert f"{_TIME_FMT}    1.0 KiB onekilobyte.txt\n" in result.stdout
        assert f"{_TIME_FMT}    1.0 MiB onemegabyte.txt\n" in result.stdout
        assert f"{_TIME_FMT}    1.0 GiB onegigabyte.txt\n" in result.stdout
        assert f"{_TIME_FMT}    1.0 TiB oneterabyte.txt\n" in result.stdout
        assert f"{_TIME_FMT}    1.0 PiB onepetabyte.txt\n" in result.stdout

    def test_summarize(self) -> None:
        responses = [
            {
                "CommonPrefixes": [],
                "Contents": [
                    _obj("onebyte.txt", 1),
                    _obj("onekilobyte.txt", 1024),
                    _obj("onemegabyte.txt", 1024**2),
                    _obj("onegigabyte.txt", 1024**3),
                    _obj("oneterabyte.txt", 1024**4),
                    _obj("onepetabyte.txt", 1024**5),
                ],
            }
        ]
        result, _ = _run_cmd(responses, ["ls", "s3://bucket/", "--summarize"])
        assert result.rc == 0
        assert "Total Objects: 6\n" in result.stdout
        assert "Total Size: 1127000493261825\n" in result.stdout

    def test_summarize_with_human_readable(self) -> None:
        responses = [
            {
                "CommonPrefixes": [],
                "Contents": [
                    _obj("onebyte.txt", 1),
                    _obj("onekilobyte.txt", 1024),
                    _obj("onemegabyte.txt", 1024**2),
                    _obj("onegigabyte.txt", 1024**3),
                    _obj("oneterabyte.txt", 1024**4),
                    _obj("onepetabyte.txt", 1024**5),
                ],
            }
        ]
        result, _ = _run_cmd(responses, ["ls", "s3://bucket/", "--human-readable", "--summarize"])
        assert result.rc == 0
        assert "Total Objects: 6\n" in result.stdout
        assert "Total Size: 1.0 PiB\n" in result.stdout

    def test_requester_pays(self) -> None:
        responses = [{"CommonPrefixes": [], "Contents": [_obj("onebyte.txt", 1)]}]
        result, calls = _run_cmd(
            responses, ["ls", "s3://mybucket/foo/", "--request-payer", "requester"]
        )
        assert result.rc == 0
        # aws-cli expectation minus ``EncodingType: "url"`` (botocore injects
        # it after the recording point; see module docstring).
        assert calls[0].params == {
            "Bucket": "mybucket",
            "Delimiter": "/",
            "RequestPayer": "requester",
            "Prefix": "foo/",
        }

    def test_requester_pays_with_no_args(self) -> None:
        responses = [{"CommonPrefixes": [], "Contents": [_obj("onebyte.txt", 1)]}]
        result, calls = _run_cmd(responses, ["ls", "s3://mybucket/foo/", "--request-payer"])
        assert result.rc == 0
        assert calls[0].params == {
            "Bucket": "mybucket",
            "Delimiter": "/",
            "RequestPayer": "requester",
            "Prefix": "foo/",
        }

    def test_accesspoint_arn(self) -> None:
        responses = [{"Contents": [_obj("bar.txt")]}]
        arn = "arn:aws:s3:us-west-2:123456789012:accesspoint/endpoint"
        result, calls = _run_cmd(responses, ["ls", f"s3://{arn}"])
        assert result.rc == 0
        assert calls[0].params["Bucket"] == arn

    def test_list_buckets_uses_bucket_name_prefix(self) -> None:
        result, calls = _run_cmd([{"Buckets": []}], ["ls", "--bucket-name-prefix", "myprefix"])
        assert result.rc == 0
        assert calls[0].params["Prefix"] == "myprefix"

    def test_list_buckets_uses_bucket_region(self) -> None:
        result, calls = _run_cmd([{"Buckets": []}], ["ls", "--bucket-region", "us-west-1"])
        assert result.rc == 0
        assert calls[0].params["BucketRegion"] == "us-west-1"

    def test_list_objects_ignores_bucket_name_prefix(self) -> None:
        result, calls = _run_cmd([{}], ["ls", "s3://mybucket", "--bucket-name-prefix", "myprefix"])
        assert result.rc == 0
        assert calls[0].params["Prefix"] == ""

    def test_list_objects_ignores_bucket_region(self) -> None:
        result, calls = _run_cmd([{}], ["ls", "s3://mybucket", "--bucket-region", "us-west-1"])
        assert result.rc == 0
        assert "BucketRegion" not in calls[0].params
