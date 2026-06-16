"""Port of aws-cli's functional website tests to ``boto3-s3 website``.

Provenance: aws-cli's ``tests/functional/s3/test_website_command.py``
(aws-cli 2.34.x snapshot). Test names, argv, and expected params are
kept verbatim so the file stays diffable against the aws-cli original; the
aws-cli harness's ``assert_params_for_cmd`` becomes the recording client
(``tests/utils/recorder.py``) plus an explicit rc/params assert, like the
other ports.
"""

from __future__ import annotations

from typing import Any

from boto3_s3_cli.commands.base import Context
from tests.utils.harness import CliResult, run_cli_in_process
from tests.utils.recorder import ApiCall, make_recording_client


def _run_cmd(argv: list[str]) -> tuple[CliResult, list[ApiCall]]:
    client, calls = make_recording_client([{}])
    result = run_cli_in_process(argv, ctx=Context(client_factory=lambda _args: client))
    return result, calls


def _assert_params(calls: list[ApiCall], expected_params: dict[str, Any]) -> None:
    """The port's ``assert_params_for_cmd`` (single PutBucketWebsite expected)."""
    assert [(c.operation, c.params) for c in calls] == [("PutBucketWebsite", expected_params)]


class TestWebsiteCommand:
    def test_index_document(self) -> None:
        result, calls = _run_cmd(["website", "s3://mybucket", "--index-document", "index.html"])
        assert result.rc == 0
        _assert_params(
            calls,
            {
                "WebsiteConfiguration": {"IndexDocument": {"Suffix": "index.html"}},
                "Bucket": "mybucket",
            },
        )

    def test_error_document(self) -> None:
        result, calls = _run_cmd(["website", "s3://mybucket", "--error-document", "mykey"])
        assert result.rc == 0
        _assert_params(
            calls,
            {
                "WebsiteConfiguration": {"ErrorDocument": {"Key": "mykey"}},
                "Bucket": "mybucket",
            },
        )
