"""Port of aws-cli's functional rb tests to ``boto3-s3 rb``.

Provenance: aws-cli's ``tests/functional/s3/test_rb_command.py``
(aws-cli 2.34.x snapshot). Test names, canned responses, and expected
ops/rc are kept verbatim where possible so the file stays diffable against
the aws-cli original when aws-cli is updated.

Adaptation rules (on top of the rm port's - see
``tests/cli/awscli/test_rm_command.py``):

- ``test_rb_force_non_empty_bucket`` expects a per-key ``DeleteObject`` in
  the aws-cli original; the inner rm deletes in batched ``DeleteObjects``
  (accepted wire-level deviation, docs/deleter.md section 4). ``LastModified``
  becomes a real ``datetime`` (recorder bypasses the output parser).
- The aws-cli ``http_response.status_code = 500`` cases port as a canned
  ``ClientError`` raised by the recorder (Exception passthrough).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from botocore.exceptions import ClientError

from boto3_s3_cli.commands.base import Context
from tests.utils.harness import CliResult, run_cli_in_process
from tests.utils.recorder import ApiCall, make_recording_client

_TIME_UTC = dt.datetime(2016, 3, 1, 23, 50, 13, tzinfo=dt.timezone.utc)


def _run_cmd(
    parsed_responses: list[dict[str, Any] | Exception], argv: list[str]
) -> tuple[CliResult, list[ApiCall]]:
    """The port's ``self.run_cmd``: one shared client (rb --force builds two
    namespaces but both factory calls must replay the same canned list)."""
    client, calls = make_recording_client(parsed_responses)
    ctx = Context(client_factory=lambda _args: client)
    return run_cli_in_process(argv, ctx=ctx), calls


def _http_500() -> ClientError:
    """The aws-cli ``self.http_response.status_code = 500`` equivalent."""
    return ClientError(
        {
            "Error": {"Code": "InternalError", "Message": "x"},
            "ResponseMetadata": {"HTTPStatusCode": 500},
        },
        "Operation",
    )


class TestRb:
    def test_rb(self) -> None:
        result, calls = _run_cmd([{}], ["rb", "s3://bucket"])
        assert result.rc == 0
        assert len(calls) == 1, calls
        assert calls[0].operation == "DeleteBucket"

    def test_rb_force_empty_bucket(self) -> None:
        result, calls = _run_cmd([{}, {}], ["rb", "s3://bucket", "--force"])
        assert result.rc == 0
        assert [c.operation for c in calls] == ["ListObjectsV2", "DeleteBucket"]

    def test_rb_force_non_empty_bucket(self) -> None:
        responses: list[dict[str, Any] | Exception] = [
            {"Contents": [{"Key": "foo", "Size": 100, "LastModified": _TIME_UTC}]},
            {},
            {},
        ]
        result, calls = _run_cmd(responses, ["rb", "s3://bucket", "--force"])
        assert result.rc == 0
        # aws-cli expectation adapted: DeleteObjects instead of a per-key
        # DeleteObject (module docstring).
        assert [c.operation for c in calls] == ["ListObjectsV2", "DeleteObjects", "DeleteBucket"]

    def test_rb_failed_rc(self) -> None:
        result, calls = _run_cmd([_http_500()], ["rb", "s3://bucket"])
        assert result.rc == 1
        assert "remove_bucket failed:" in result.stderr
        assert [c.operation for c in calls] == ["DeleteBucket"]

    def test_rb_force_with_failed_rm(self) -> None:
        result, calls = _run_cmd([_http_500()], ["rb", "s3://bucket", "--force"])
        assert result.rc == 255
        assert "remove_bucket failed:" in result.stderr
        assert len(calls) == 1
        assert calls[0].operation == "ListObjectsV2"

    def test_nonzero_exit_if_uri_scheme_not_provided(self) -> None:
        result, calls = _run_cmd([{}], ["rb", "bucket"])
        assert result.rc == 252
        assert calls == []

    def test_nonzero_exit_if_key_provided(self) -> None:
        result, calls = _run_cmd([{}], ["rb", "s3://bucket/key", "--force"])
        assert result.rc == 252
        assert calls == []

        result, calls = _run_cmd([{}], ["rb", "s3://bucket/key"])
        assert result.rc == 252
        assert calls == []
