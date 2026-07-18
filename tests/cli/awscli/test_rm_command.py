# Copyright 2017 Amazon.com, Inc. or its affiliates. All Rights Reserved.
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

"""Port of aws-cli's functional rm tests to ``boto3-s3 rm``.

Provenance: aws-cli's ``tests/functional/s3/test_rm_command.py``
(aws-cli 2.36.1). Test names, canned responses, and expected stdout/rc are
kept verbatim where possible so the file stays diffable against the aws-cli
original when aws-cli is updated.

The behaviour under test is aws-cli's s3 command implementation in
``vendor/aws-cli/awscli/customizations/s3/`` - ``subcommands.py`` (``RmCommand``)
driving ``s3handler.py``.

A test carrying no ``# aws-cli:`` comment ports the aws-cli test of the same
class and method name. A ``# aws-cli:`` comment names a divergent origin
instead: above a test for a per-test difference (a rename, a parametrized
merge of several aws-cli tests, a method from a different aws-cli class or
file, or ``none`` for a boto3-s3 addition), or above a class when a whole
block was carved out of one aws-cli class under the same method names.

Adaptation rules (on top of the ls port's - see
``tests/cli/awscli/test_ls_command.py``):

- ``test_recursive_delete_with_requests`` expects one per-key ``DeleteObject``
  in the aws-cli original; boto3-s3 deletes recursively in batched
  ``DeleteObjects`` calls (accepted wire-level deviation, docs/deleter.md section 4),
  so the expectation becomes one ``DeleteObjects`` carrying the key with
  ``Quiet: True``. ``RequestPayer`` must still appear on both operations.
- The aws-cli ``TestRmWithCRTClient`` class (2 tests) cannot use the botocore
  recording client because the aws-cli CRT data plane bypasses it. The single
  and recursive delete shapes are covered by the e2e CRT parity lane instead.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from boto3_s3_cli.commands.base import Context
from tests.utils.harness import CliResult, run_cli_in_process
from tests.utils.recorder import ApiCall, make_recording_client

_TIME_UTC = dt.datetime(2014, 1, 9, 20, 45, 49, tzinfo=dt.timezone.utc)


def _run_cmd(
    parsed_responses: list[dict[str, Any]], argv: list[str]
) -> tuple[CliResult, list[ApiCall]]:
    """The port's ``self.run_cmd``: in-process main() with a recording client."""
    client, calls = make_recording_client(parsed_responses)
    ctx = Context(client_factory=lambda _args: client)
    return run_cli_in_process(argv, ctx=ctx), calls


def _head_object_response() -> dict[str, Any]:
    return {"ContentLength": 100, "LastModified": _TIME_UTC, "ETag": '"foo-1"'}


def _list_objects_response(keys: list[str]) -> dict[str, Any]:
    contents = [
        {"Key": key, "LastModified": _TIME_UTC, "Size": 100, "ETag": '"foo-1"'} for key in keys
    ]
    return {"Contents": contents, "CommonPrefixes": []}


def _empty_response() -> dict[str, Any]:
    return {}


class TestRmCommand:
    def test_operations_used(self) -> None:
        result, calls = _run_cmd([_empty_response()], ["rm", "s3://bucket/key.txt"])
        assert result.rc == 0
        # The only operation we should have called is DeleteObject.
        assert len(calls) == 1, calls
        assert calls[0].operation == "DeleteObject"

    def test_dryrun_delete(self) -> None:
        responses = [_head_object_response()]
        result, calls = _run_cmd(responses, ["rm", "s3://bucket/key.txt", "--dryrun"])
        assert result.rc == 0
        assert calls == []
        assert "(dryrun) delete: s3://bucket/key.txt" in result.stdout

    def test_delete_with_request_payer(self) -> None:
        result, calls = _run_cmd(
            [_empty_response()], ["rm", "s3://mybucket/mykey", "--request-payer"]
        )
        assert result.rc == 0
        assert [(c.operation, c.params) for c in calls] == [
            (
                "DeleteObject",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "RequestPayer": "requester",
                },
            )
        ]

    def test_recursive_delete_with_requests(self) -> None:
        responses = [
            _list_objects_response(["mykey"]),
            _empty_response(),
        ]
        result, calls = _run_cmd(
            responses, ["rm", "s3://mybucket/", "--recursive", "--request-payer"]
        )
        assert result.rc == 0
        # aws-cli expectation adapted: one batched DeleteObjects instead of a
        # per-key DeleteObject (module docstring).
        assert [(c.operation, c.params) for c in calls] == [
            (
                "ListObjectsV2",
                {
                    "Bucket": "mybucket",
                    "Prefix": "",
                    "RequestPayer": "requester",
                },
            ),
            (
                "DeleteObjects",
                {
                    "Bucket": "mybucket",
                    "Delete": {"Objects": [{"Key": "mykey"}], "Quiet": True},
                    "RequestPayer": "requester",
                },
            ),
        ]
