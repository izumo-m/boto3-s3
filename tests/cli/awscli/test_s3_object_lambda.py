# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
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

"""Port of aws-cli's functional S3 Object Lambda rejection tests.

Provenance: aws-cli's ``tests/functional/s3/test_s3_object_lambda.py``
(aws-cli 2.35.18). The two ARN spellings, optional key, nine command shapes,
exit code, and stable error token are kept verbatim. The aws-cli harness's
``run_cmd`` becomes the in-process CLI runner plus a recording client; the
empty call list proves rejection happens before any service request.
"""

from __future__ import annotations

from boto3_s3_cli.commands.base import Context
from tests.utils.harness import run_cli_in_process
from tests.utils.recorder import make_recording_client


class TestObjectLambdaHandling:
    prefixes: tuple[tuple[str, ...], ...] = (
        ("ls",),
        ("cp", "."),
        ("mv", "."),
        ("sync", "."),
        ("rm",),
        ("mb",),
        ("presign",),
        ("rb",),
        ("website",),
    )

    def _assert_rejected(self, object_lambda_arn: str) -> None:
        """Assert all command shapes reject the ARN both with and without a key."""
        for suffix in ("", "/my-key"):
            uri = f"s3://{object_lambda_arn}{suffix}"
            for prefix in self.prefixes:
                client, calls = make_recording_client([])
                argv = [prefix[0], uri, *prefix[1:]]
                result = run_cli_in_process(
                    argv,
                    ctx=Context(client_factory=lambda _args, client=client: client),
                )
                assert result.rc == 252, (argv, result)
                assert "s3 commands do not support" in result.stderr, (argv, result)
                assert calls == [], (argv, calls)

    def test_object_lambda_arn_with_colon_raises_exception(self) -> None:
        self._assert_rejected(
            "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint:my-accesspoint"
        )

    def test_object_lambda_arn_with_slash_raises_exception(self) -> None:
        self._assert_rejected(
            "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-accesspoint"
        )
