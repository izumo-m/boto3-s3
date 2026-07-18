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

"""Port of aws-cli's functional mv tests to ``boto3-s3 mv``.

Provenance: aws-cli's ``tests/functional/s3/test_mv_command.py``
(aws-cli 2.36.1). Test names, canned responses, and expected operations are
kept verbatim where possible so the file stays diffable against the aws-cli
original when aws-cli is updated.

The behaviour under test is aws-cli's s3 command implementation in
``vendor/aws-cli/awscli/customizations/s3/`` - ``subcommands.py`` (``MvCommand``)
on the same ``s3handler.py`` / ``filegenerator.py`` pipeline as cp.

A test carrying no ``# aws-cli:`` comment ports the aws-cli test of the same
class and method name. A ``# aws-cli:`` comment names a divergent origin
instead: above a test for a per-test difference (a rename, a parametrized
merge of several aws-cli tests, a method from a different aws-cli class or
file, or ``none`` for a boto3-s3 addition), or above a class when a whole
block was carved out of one aws-cli class under the same method names.

Adaptation rules (on top of the cp port's - see its module docstring):

- The aws-cli harness feeds every service's responses through one stubbed
  HTTP list, so its ``operations_called`` interleaves ``GetAccessPoint`` /
  ``GetCallerIdentity`` with the S3 calls. Here each service gets its own
  recording client through ``Context.service_client_factory``, so the
  validation tests assert the s3control/sts call lists separately from the
  S3 one (same calls, same order within each service).
- The aws-cli's exact ``aws: [ERROR]: ...`` stderr equality for the
  MRAP-not-found case becomes a message-token assertion (our error prefix
  is ``boto3-s3: [ERROR]:``; the message body is the aws-cli's verbatim).
- ``ChecksumMode: 'ENABLED'`` is kept on the single-source HeadObject - we
  setdefault it like aws's filegenerator when the client resolves
  ``response_checksum_validation`` to ``when_supported`` (cp port rule).

Not ported, with reasons:

- ``TestMvWithCRTClient`` (3 tests): the CRT data plane bypasses the botocore
  client, so the recording client cannot drive it; CRT parity is enforced by
  the e2e CRT lane instead (docs/crt.md, docs/testing.md).
- ``TestMvRecursiveCaseConflict.test_warn_with_case_conflicts_in_s3`` is an
  aws-cli ``pass`` (their threaded get/delete order is nondeterministic);
  ported here as a real test - the injected NonThreadedExecutor makes the
  per-item get-then-delete order deterministic.
- ``TestMvRecursiveCaseConflict.test_ignore_with_case_conflicts_in_s3`` is the
  other aws-cli ``pass`` (it pins no behavior); dropped rather than upgraded.
"""

from __future__ import annotations

import datetime as dt
import io
import os
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3_cli.commands.base import Context
from tests.utils.harness import CliResult, run_cli_in_process
from tests.utils.recorder import ApiCall, make_recording_client

MB = 1024**2
_TIME_UTC = dt.datetime(2014, 1, 9, 20, 45, 49, tzinfo=dt.timezone.utc)
_SYNC_CONFIG = TransferConfig(use_threads=False)
# See test_cp_command._CASE_CONFLICT_CONFIG: the "two S3 twins" gate detects a
# conflict only while the first twin is still in flight, which needs a threaded
# (non-blocking) submit running ahead of completions - aws-cli runs its whole
# functional s3 harness with max_concurrent_requests = 1. That single worker also
# keeps mv's per-item get-then-delete order deterministic.
_CASE_CONFLICT_CONFIG = TransferConfig(max_concurrency=1)

_AP_ARN = "arn:aws:s3:us-west-2:123456789012:accesspoint/myaccesspoint"
_OUTPOST_ARN = (
    "arn:aws:s3-outposts:us-east-1:123456789012:outpost/op-foobar/accesspoint/myaccesspoint"
)
_MRAP_ARN = "arn:aws:s3::123456789012:accesspoint/foobar.mrap"


def _run_cmd(
    parsed_responses: list[dict[str, Any] | Exception],
    argv: list[str],
    expected_rc: int = 0,
    transfer_config: TransferConfig = _SYNC_CONFIG,
) -> tuple[CliResult, list[ApiCall]]:
    """The port's ``self.run_cmd``: in-process main() with a recording client."""
    client, calls = make_recording_client(parsed_responses)
    ctx = Context(client_factory=lambda _args: client, transfer_config=transfer_config)
    result = run_cli_in_process(argv, ctx=ctx)
    assert result.rc == expected_rc, (result.rc, result.stdout, result.stderr, calls)
    return result, calls


def _run_validate_cmd(
    argv: list[str],
    *,
    expected_rc: int = 0,
    s3_responses: list[dict[str, Any] | Exception] | None = None,
    s3control_responses: list[dict[str, Any] | Exception] | None = None,
    sts_responses: list[dict[str, Any] | Exception] | None = None,
) -> tuple[CliResult, list[ApiCall], list[ApiCall], list[ApiCall]]:
    """run_cmd with per-service recording clients for the validation flow."""
    client, calls = make_recording_client(s3_responses or [])
    s3control, s3control_calls = make_recording_client(
        s3control_responses or [], service="s3control"
    )
    sts, sts_calls = make_recording_client(sts_responses or [], service="sts")

    def service_factory(service: str, _args: Any, *, region: Any = None) -> Any:
        return {"s3control": s3control, "sts": sts}[service]

    ctx = Context(
        client_factory=lambda _args: client,
        service_client_factory=service_factory,
        transfer_config=_SYNC_CONFIG,
    )
    result = run_cli_in_process(argv, ctx=ctx)
    assert result.rc == expected_rc, (result.rc, result.stdout, result.stderr)
    return result, calls, s3control_calls, sts_calls


def _operations(calls: list[ApiCall]) -> list[str]:
    return [call.operation for call in calls]


def _client_error(code: str, status: int, operation: str) -> Exception:
    from botocore.exceptions import ClientError

    response: Any = {
        "Error": {"Code": code, "Message": "stub"},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return ClientError(response, operation)


def head_object_response(**override_kwargs: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "ContentLength": 100,
        "LastModified": _TIME_UTC,
        "ETag": '"foo-1"',
    }
    response.update(override_kwargs)
    return response


def list_objects_response(keys: list[str]) -> dict[str, Any]:
    return {
        "Contents": [
            {"Key": key, "LastModified": _TIME_UTC, "Size": 100, "ETag": '"foo-1"'} for key in keys
        ],
        "CommonPrefixes": [],
    }


def get_object_response() -> dict[str, Any]:
    return {"ETag": '"foo-1"', "Body": io.BytesIO(b"foo")}


def get_object_tagging_response(tags: dict[str, str]) -> dict[str, Any]:
    return {"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]}


def create_mpu_response(upload_id: str) -> dict[str, Any]:
    return {"UploadId": upload_id}


def upload_part_copy_response() -> dict[str, Any]:
    return {"CopyPartResult": {"ETag": '"etag"'}}


class TestMvCommand:
    def test_cant_mv_object_onto_itself(self) -> None:
        result, _ = _run_cmd([], ["mv", "s3://bucket/key", "s3://bucket/key"], expected_rc=252)
        assert "Cannot mv a file onto itself" in result.stderr

    def test_cant_mv_object_with_implied_name(self) -> None:
        # The "key" key name is implied in the dest argument.
        result, _ = _run_cmd([], ["mv", "s3://bucket/key", "s3://bucket/"], expected_rc=252)
        assert "Cannot mv a file onto itself" in result.stderr

    def test_dryrun_move(self) -> None:
        result, calls = _run_cmd(
            [head_object_response()],
            ["mv", "s3://bucket/key.txt", "s3://bucket/key2.txt", "--dryrun"],
        )
        assert calls == [
            ApiCall("HeadObject", {"Bucket": "bucket", "Key": "key.txt", "ChecksumMode": "ENABLED"})
        ]
        assert "(dryrun) move: s3://bucket/key.txt to s3://bucket/key2.txt" in result.stdout

    def test_website_redirect_ignore_paramfile(self, tmp_path: Path) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            ["mv", full_path, "s3://bucket/key.txt", "--website-redirect", "http://someserver"],
        )
        assert calls[0].operation == "PutObject"
        # Make sure that the specified web address is used as opposed to the
        # contents of the web address.
        assert calls[0].params["WebsiteRedirectLocation"] == "http://someserver"
        assert not os.path.exists(full_path)

    def test_metadata_directive_copy(self) -> None:
        _, calls = _run_cmd(
            [head_object_response(), {"ETag": '"foo-1"'}, {}],
            [
                "mv",
                "s3://bucket/key.txt",
                "s3://bucket/key2.txt",
                "--metadata-directive",
                "REPLACE",
            ],
        )
        assert _operations(calls) == ["HeadObject", "CopyObject", "DeleteObject"]
        assert calls[1].params["MetadataDirective"] == "REPLACE"
        assert calls[2].params == {"Bucket": "bucket", "Key": "key.txt"}

    def test_no_metadata_directive_for_non_copy(self, tmp_path: Path) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            ["mv", full_path, "s3://bucket", "--metadata-directive", "REPLACE"],
        )
        assert len(calls) == 1
        assert calls[0].operation == "PutObject"
        assert "MetadataDirective" not in calls[0].params

    def test_download_move_with_request_payer(self, tmp_path: Path) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(),
                get_object_response(),
                {},
            ],
            ["mv", "s3://mybucket/mykey", str(tmp_path), "--request-payer"],
        )
        assert calls == [
            ApiCall(
                "HeadObject",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "RequestPayer": "requester",
                    "ChecksumMode": "ENABLED",
                },
            ),
            ApiCall(
                "GetObject",
                {"Bucket": "mybucket", "Key": "mykey", "RequestPayer": "requester"},
            ),
            ApiCall(
                "DeleteObject",
                {"Bucket": "mybucket", "Key": "mykey", "RequestPayer": "requester"},
            ),
        ]

    def test_copy_move_with_request_payer(self) -> None:
        _, calls = _run_cmd(
            [head_object_response(), {"ETag": '"foo-1"'}, {}],
            ["mv", "s3://sourcebucket/sourcekey", "s3://mybucket/mykey", "--request-payer"],
        )
        assert _operations(calls) == ["HeadObject", "CopyObject", "DeleteObject"]
        assert calls[0].params == {
            "Bucket": "sourcebucket",
            "Key": "sourcekey",
            "RequestPayer": "requester",
            "ChecksumMode": "ENABLED",
        }
        assert calls[1].params["CopySource"] == {"Bucket": "sourcebucket", "Key": "sourcekey"}
        assert calls[1].params["RequestPayer"] == "requester"
        assert calls[2].params == {
            "Bucket": "sourcebucket",
            "Key": "sourcekey",
            "RequestPayer": "requester",
        }

    def test_with_copy_props(self) -> None:
        upload_id = "upload_id"
        large_tag_set = {"tag-key": "val" * 3000}
        metadata = {"tag-key": "tag-value"}
        _, calls = _run_cmd(
            [
                head_object_response(Metadata=metadata, ContentLength=8 * MB),
                get_object_tagging_response(large_tag_set),
                create_mpu_response(upload_id),
                upload_part_copy_response(),
                {},  # CompleteMultipartUpload
                {},  # PutObjectTagging
                {},  # DeleteObject
            ],
            ["mv", "s3://sourcebucket/sourcekey", "s3://bucket/key", "--copy-props", "default"],
        )
        assert _operations(calls) == [
            "HeadObject",
            "GetObjectTagging",
            "CreateMultipartUpload",
            "UploadPartCopy",
            "CompleteMultipartUpload",
            "PutObjectTagging",
            "DeleteObject",
        ]
        assert calls[2].params["Metadata"] == metadata
        assert calls[3].params["CopySourceIfMatch"] == '"foo-1"'
        assert calls[5].params["Tagging"] == {"TagSet": [{"Key": "tag-key", "Value": "val" * 3000}]}
        # The source object goes last (the move's deletion).
        assert calls[6].params == {"Bucket": "sourcebucket", "Key": "sourcekey"}

    def test_mv_does_not_delete_source_on_failed_put_tagging(self) -> None:
        upload_id = "upload_id"
        large_tag_set = {"tag-key": "val" * 3000}
        metadata = {"tag-key": "tag-value"}
        _, calls = _run_cmd(
            [
                head_object_response(Metadata=metadata, ContentLength=8 * MB),
                get_object_tagging_response(large_tag_set),
                create_mpu_response(upload_id),
                upload_part_copy_response(),
                {},  # CompleteMultipartUpload
                _client_error("AccessDenied", 403, "PutObjectTagging"),
                {},  # DeleteObject (destination rollback)
            ],
            ["mv", "s3://sourcebucket/sourcekey", "s3://bucket/key", "--copy-props", "default"],
            expected_rc=1,
        )
        assert _operations(calls) == [
            "HeadObject",
            "GetObjectTagging",
            "CreateMultipartUpload",
            "UploadPartCopy",
            "CompleteMultipartUpload",
            "PutObjectTagging",
            "DeleteObject",
        ]
        # The deletion is the destination rollback - the source survives.
        assert calls[6].params == {"Bucket": "bucket", "Key": "key"}

    def test_upload_with_checksum_algorithm_crc32(self, tmp_path: Path) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("contents")
        _, calls = _run_cmd(
            [{"ETag": '"foo"'}],
            ["mv", full_path, "s3://bucket/key.txt", "--checksum-algorithm", "CRC32"],
        )
        assert calls[0].operation == "PutObject"
        assert calls[0].params["ChecksumAlgorithm"] == "CRC32"

    def test_download_with_checksum_mode_crc32(self, tmp_path: Path) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(),
                {"ETag": "foo-1", "ChecksumCRC32": "checksum", "Body": io.BytesIO(b"foo")},
                {},
            ],
            ["mv", "s3://bucket/foo", str(tmp_path), "--checksum-mode", "ENABLED"],
        )
        assert calls[1].operation == "GetObject"
        assert calls[1].params["ChecksumMode"] == "ENABLED"


# aws-cli: TestMvCommand (the --no-overwrite block, carved into its own class; same method names)
class TestMvCommandNoOverwrite:
    """The aws-cli no-overwrite block of TestMvCommand (source-survival pins)."""

    def test_mv_no_overwrite_flag_when_object_not_exists_on_target(self, tmp_path: Path) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("contents")
        _, calls = _run_cmd([{"ETag": '"foo"'}], ["mv", full_path, "s3://bucket", "--no-overwrite"])
        assert len(calls) == 1
        assert calls[0].operation == "PutObject"
        assert calls[0].params["IfNoneMatch"] == "*"
        # Verify source file was deleted (move operation).
        assert not os.path.exists(full_path)

    def test_mv_no_overwrite_flag_when_object_exists_on_target(self, tmp_path: Path) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [_client_error("PreconditionFailed", 412, "PutObject")],
            ["mv", full_path, "s3://bucket/foo.txt", "--no-overwrite"],
        )
        assert len(calls) == 1
        assert calls[0].operation == "PutObject"
        assert calls[0].params["IfNoneMatch"] == "*"
        # Verify source file was not deleted.
        assert os.path.exists(full_path)

    def test_mv_no_overwrite_flag_multipart_upload_when_object_not_exists_on_target(
        self, tmp_path: Path
    ) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_bytes(b"a" * 10 * MB)
        _, calls = _run_cmd(
            [{"UploadId": "foo"}, {"ETag": '"foo-1"'}, {"ETag": '"foo-2"'}, {}],
            ["mv", full_path, "s3://bucket", "--no-overwrite"],
        )
        assert _operations(calls) == [
            "CreateMultipartUpload",
            "UploadPart",
            "UploadPart",
            "CompleteMultipartUpload",
        ]
        assert calls[3].params["IfNoneMatch"] == "*"
        assert not os.path.exists(full_path)

    def test_mv_no_overwrite_flag_multipart_upload_when_object_exists_on_target(
        self, tmp_path: Path
    ) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_bytes(b"a" * 10 * MB)
        _, calls = _run_cmd(
            [
                {"UploadId": "foo"},
                {"ETag": '"foo-1"'},
                {"ETag": '"foo-2"'},
                _client_error("PreconditionFailed", 412, "CompleteMultipartUpload"),
                {},  # AbortMultipartUpload
            ],
            ["mv", full_path, "s3://bucket", "--no-overwrite"],
        )
        assert _operations(calls) == [
            "CreateMultipartUpload",
            "UploadPart",
            "UploadPart",
            "CompleteMultipartUpload",
            "AbortMultipartUpload",
        ]
        assert calls[3].params["IfNoneMatch"] == "*"
        # Source not deleted (failed move due to PreconditionFailed).
        assert os.path.exists(full_path)

    def test_mv_no_overwrite_flag_on_copy_when_small_object_does_not_exist_on_target(
        self,
    ) -> None:
        _, calls = _run_cmd(
            [head_object_response(), {"ETag": '"foo-1"'}, {}],
            ["mv", "s3://bucket1/key.txt", "s3://bucket2/key1.txt", "--no-overwrite"],
        )
        assert _operations(calls) == ["HeadObject", "CopyObject", "DeleteObject"]
        assert calls[1].params["IfNoneMatch"] == "*"
        assert calls[2].params == {"Bucket": "bucket1", "Key": "key.txt"}

    def test_mv_no_overwrite_flag_on_copy_when_small_object_exists_on_target(self) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(),
                _client_error("PreconditionFailed", 412, "CopyObject"),
            ],
            ["mv", "s3://bucket1/key.txt", "s3://bucket2/key.txt", "--no-overwrite"],
        )
        assert _operations(calls) == ["HeadObject", "CopyObject"]
        assert calls[1].params["IfNoneMatch"] == "*"

    def test_mv_no_overwrite_flag_when_large_object_exists_on_target(self) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(ContentLength=10 * MB),
                get_object_tagging_response({}),
                create_mpu_response("foo"),
                upload_part_copy_response(),
                upload_part_copy_response(),
                _client_error("PreconditionFailed", 412, "CompleteMultipartUpload"),
                {},  # AbortMultipartUpload
            ],
            ["mv", "s3://bucket1/key1.txt", "s3://bucket/key1.txt", "--no-overwrite"],
        )
        assert _operations(calls) == [
            "HeadObject",
            "GetObjectTagging",
            "CreateMultipartUpload",
            "UploadPartCopy",
            "UploadPartCopy",
            "CompleteMultipartUpload",
            "AbortMultipartUpload",
        ]
        assert calls[5].params["IfNoneMatch"] == "*"

    def test_mv_no_overwrite_flag_when_large_object_does_not_exist_on_target(self) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(ContentLength=10 * MB),
                get_object_tagging_response({}),
                create_mpu_response("foo"),
                upload_part_copy_response(),
                upload_part_copy_response(),
                {},  # CompleteMultipartUpload
                {},  # DeleteObject (for move operation)
            ],
            ["mv", "s3://bucket1/key1.txt", "s3://bucket/key.txt", "--no-overwrite"],
        )
        assert _operations(calls) == [
            "HeadObject",
            "GetObjectTagging",
            "CreateMultipartUpload",
            "UploadPartCopy",
            "UploadPartCopy",
            "CompleteMultipartUpload",
            "DeleteObject",
        ]
        assert calls[6].params == {"Bucket": "bucket1", "Key": "key1.txt"}

    def test_no_overwrite_flag_on_mv_download_when_single_object_exists_at_target(
        self, tmp_path: Path
    ) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("existing content")
        _, calls = _run_cmd(
            [head_object_response()],
            ["mv", "s3://bucket/foo.txt", full_path, "--no-overwrite"],
        )
        assert _operations(calls) == ["HeadObject"]
        assert (tmp_path / "foo.txt").read_text() == "existing content"

    def test_no_overwrite_flag_on_mv_download_when_single_object_does_not_exist_at_target(
        self, tmp_path: Path
    ) -> None:
        full_path = str(tmp_path / "foo.txt")
        _, calls = _run_cmd(
            [head_object_response(), get_object_response(), {}],
            ["mv", "s3://bucket/foo.txt", full_path, "--no-overwrite"],
        )
        assert _operations(calls) == ["HeadObject", "GetObject", "DeleteObject"]
        assert (tmp_path / "foo.txt").read_text() == "foo"


class TestMvCommandWithValidateSameS3Paths:
    def _assert_validates_cannot_mv_onto_itself(
        self,
        argv: list[str],
        *,
        s3control_responses: list[dict[str, Any] | Exception] | None = None,
        sts_responses: list[dict[str, Any] | Exception] | None = None,
    ) -> CliResult:
        result, calls, _, _ = _run_validate_cmd(
            argv,
            expected_rc=252,
            s3control_responses=s3control_responses,
            sts_responses=sts_responses,
        )
        assert "Cannot mv a file onto itself" in result.stderr
        assert calls == []
        return result

    def _assert_runs_mv_without_validation(self, argv: list[str]) -> None:
        _, calls, s3control_calls, sts_calls = _run_validate_cmd(
            argv,
            s3_responses=[head_object_response(), {"ETag": '"foo-1"'}, {}],
        )
        assert _operations(calls) == ["HeadObject", "CopyObject", "DeleteObject"]
        assert s3control_calls == []
        assert sts_calls == []

    def _assert_raises_warning(self, argv: list[str]) -> None:
        result, calls, s3control_calls, _ = _run_validate_cmd(
            argv,
            s3_responses=[head_object_response(), {"ETag": '"foo-1"'}, {}],
        )
        assert "warning: Provided s3 paths may resolve" in result.stderr
        assert _operations(calls) == ["HeadObject", "CopyObject", "DeleteObject"]
        assert s3control_calls == []

    def test_cant_mv_object_onto_itself_access_point_arn(self) -> None:
        self._assert_validates_cannot_mv_onto_itself(
            ["mv", "s3://bucket/key", f"s3://{_AP_ARN}/key", "--validate-same-s3-paths"],
            s3control_responses=[{"Bucket": "bucket"}],
        )

    def test_cant_mv_object_onto_itself_access_point_arn_as_source(self) -> None:
        self._assert_validates_cannot_mv_onto_itself(
            ["mv", f"s3://{_AP_ARN}/key", "s3://bucket/key", "--validate-same-s3-paths"],
            s3control_responses=[{"Bucket": "bucket"}],
        )

    def test_cant_mv_object_onto_itself_access_point_arn_with_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS", "true")
        self._assert_validates_cannot_mv_onto_itself(
            ["mv", "s3://bucket/key", f"s3://{_AP_ARN}/key"],
            s3control_responses=[{"Bucket": "bucket"}],
        )

    def test_cant_mv_object_onto_itself_access_point_arn_base_key(self) -> None:
        self._assert_validates_cannot_mv_onto_itself(
            ["mv", "s3://bucket/key", f"s3://{_AP_ARN}/", "--validate-same-s3-paths"],
            s3control_responses=[{"Bucket": "bucket"}],
        )

    def test_cant_mv_object_onto_itself_access_point_arn_base_prefix(self) -> None:
        self._assert_validates_cannot_mv_onto_itself(
            ["mv", "s3://bucket/prefix/key", f"s3://{_AP_ARN}/prefix/", "--validate-same-s3-paths"],
            s3control_responses=[{"Bucket": "bucket"}],
        )

    def test_cant_mv_object_onto_itself_access_point_alias(self) -> None:
        self._assert_validates_cannot_mv_onto_itself(
            [
                "mv",
                "s3://bucket/key",
                "s3://myaccesspoint-foobar-s3alias/key",
                "--validate-same-s3-paths",
            ],
            s3control_responses=[{"Bucket": "bucket"}],
            sts_responses=[{"Account": "123456789012"}],
        )

    def test_cant_mv_object_onto_itself_outpost_access_point_arn(self) -> None:
        self._assert_validates_cannot_mv_onto_itself(
            ["mv", "s3://bucket/key", f"s3://{_OUTPOST_ARN}/key", "--validate-same-s3-paths"],
            s3control_responses=[{"Bucket": "bucket"}],
        )

    def test_outpost_access_point_alias_raises_error(self) -> None:
        result, _, s3control_calls, _ = _run_validate_cmd(
            [
                "mv",
                "s3://bucket/key",
                "s3://myaccesspoint-foobar--op-s3/key",
                "--validate-same-s3-paths",
            ],
            expected_rc=252,
        )
        assert "Can't resolve underlying bucket name" in result.stderr
        assert s3control_calls == []

    def test_cant_mv_object_onto_itself_mrap_arn(self) -> None:
        self._assert_validates_cannot_mv_onto_itself(
            ["mv", "s3://bucket/key", f"s3://{_MRAP_ARN}/key", "--validate-same-s3-paths"],
            s3control_responses=[
                {
                    "AccessPoints": [
                        {
                            "Alias": "foobar.mrap",
                            "Regions": [{"Bucket": "differentbucket"}, {"Bucket": "bucket"}],
                        }
                    ]
                }
            ],
        )

    def test_get_mrap_buckets_raises_if_alias_not_found(self) -> None:
        result, _, _, _ = _run_validate_cmd(
            ["mv", "s3://bucket/key", f"s3://{_MRAP_ARN}/key", "--validate-same-s3-paths"],
            expected_rc=252,
            s3control_responses=[
                {
                    "AccessPoints": [
                        {
                            "Alias": "baz.mrap",
                            "Regions": [{"Bucket": "differentbucket"}, {"Bucket": "bucket"}],
                        }
                    ]
                }
            ],
        )
        assert (
            "Couldn't find multi-region access point with alias foobar.mrap "
            "in account 123456789012" in result.stderr
        )

    def test_mv_works_if_access_point_arn_resolves_to_different_bucket(self) -> None:
        _, calls, s3control_calls, _ = _run_validate_cmd(
            ["mv", "s3://bucket/key", f"s3://{_AP_ARN}/key", "--validate-same-s3-paths"],
            s3_responses=[head_object_response(), {"ETag": '"foo-1"'}, {}],
            s3control_responses=[{"Bucket": "differentbucket"}],
        )
        assert _operations(s3control_calls) == ["GetAccessPoint"]
        assert _operations(calls) == ["HeadObject", "CopyObject", "DeleteObject"]

    def test_mv_works_if_access_point_alias_resolves_to_different_bucket(self) -> None:
        _, calls, s3control_calls, sts_calls = _run_validate_cmd(
            [
                "mv",
                "s3://bucket/key",
                "s3://myaccesspoint-foobar-s3alias/key",
                "--validate-same-s3-paths",
            ],
            s3_responses=[head_object_response(), {"ETag": '"foo-1"'}, {}],
            s3control_responses=[{"Bucket": "differentbucket"}],
            sts_responses=[{"Account": "123456789012"}],
        )
        assert _operations(sts_calls) == ["GetCallerIdentity"]
        assert _operations(s3control_calls) == ["GetAccessPoint"]
        assert _operations(calls) == ["HeadObject", "CopyObject", "DeleteObject"]

    def test_mv_works_if_outpost_access_point_arn_resolves_to_different_bucket(self) -> None:
        _, calls, s3control_calls, _ = _run_validate_cmd(
            ["mv", "s3://bucket/key", f"s3://{_OUTPOST_ARN}/key", "--validate-same-s3-paths"],
            s3_responses=[head_object_response(), {"ETag": '"foo-1"'}, {}],
            s3control_responses=[{"Bucket": "differentbucket"}],
        )
        assert _operations(s3control_calls) == ["GetAccessPoint"]
        # The Outposts GetAccessPoint takes the whole ARN as its Name.
        assert s3control_calls[0].params["Name"] == _OUTPOST_ARN
        assert _operations(calls) == ["HeadObject", "CopyObject", "DeleteObject"]

    def test_mv_works_if_mrap_arn_resolves_to_different_bucket(self) -> None:
        # aws-cli marks this @requires_crt (MRAP requests need SigV4a there);
        # the validation flow itself is CRT-free, so it ports unconditionally.
        _, calls, s3control_calls, _ = _run_validate_cmd(
            ["mv", "s3://bucket/key", f"s3://{_MRAP_ARN}/key", "--validate-same-s3-paths"],
            s3_responses=[head_object_response(), {"ETag": '"foo-1"'}, {}],
            s3control_responses=[
                {
                    "AccessPoints": [
                        {"Alias": "foobar.mrap", "Regions": [{"Bucket": "differentbucket"}]}
                    ]
                }
            ],
        )
        assert _operations(s3control_calls) == ["ListMultiRegionAccessPoints"]
        assert _operations(calls) == ["HeadObject", "CopyObject", "DeleteObject"]

    def test_skips_validation_if_keys_are_different_accesspoint_arn(self) -> None:
        self._assert_runs_mv_without_validation(
            ["mv", "s3://bucket/key", f"s3://{_AP_ARN}/key2", "--validate-same-s3-paths"]
        )

    def test_skips_validation_if_prefixes_are_different_accesspoint_arn(self) -> None:
        self._assert_runs_mv_without_validation(
            ["mv", "s3://bucket/key", f"s3://{_AP_ARN}/prefix/", "--validate-same-s3-paths"]
        )

    def test_skips_validation_if_keys_are_different_accesspoint_alias(self) -> None:
        self._assert_runs_mv_without_validation(
            [
                "mv",
                "s3://bucket/key",
                "s3://myaccesspoint-foobar-s3alias/key2",
                "--validate-same-s3-paths",
            ]
        )

    def test_skips_validation_if_keys_are_different_outpost_arn(self) -> None:
        self._assert_runs_mv_without_validation(
            ["mv", "s3://bucket/key", f"s3://{_OUTPOST_ARN}/key2", "--validate-same-s3-paths"]
        )

    def test_skips_validation_if_keys_are_different_outpost_alias(self) -> None:
        self._assert_runs_mv_without_validation(
            [
                "mv",
                "s3://bucket/key",
                "s3://myaccesspoint-foobar--op-s3/key2",
                "--validate-same-s3-paths",
            ]
        )

    def test_skips_validation_if_keys_are_different_mrap_arn(self) -> None:
        self._assert_runs_mv_without_validation(
            ["mv", "s3://bucket/key", f"s3://{_MRAP_ARN}/key2", "--validate-same-s3-paths"]
        )

    def test_raises_warning_if_validation_not_set(self) -> None:
        self._assert_raises_warning(["mv", "s3://bucket/key", f"s3://{_AP_ARN}/key"])

    def test_raises_warning_if_validation_not_set_source(self) -> None:
        self._assert_raises_warning(["mv", f"s3://{_AP_ARN}/key", "s3://bucket/key"])


class TestMvRecursiveCaseConflict:
    """aws-cli TestMvRecursiveCaseConflict (inheriting TestSyncCaseConflict)."""

    LOWER_KEY = "a.txt"
    UPPER_KEY = "A.txt"

    def _cmd(self, tmp_path: Path, case_conflict: str) -> list[str]:
        return [
            "mv",
            "--recursive",
            "s3://bucket",
            str(tmp_path),
            "--case-conflict",
            case_conflict,
        ]

    # aws-cli: TestSyncCaseConflict.test_error_with_existing_file (test_sync_command.py)
    def test_error_with_existing_file(self, case_insensitive_workdir: Path) -> None:
        (case_insensitive_workdir / self.LOWER_KEY).write_text("mycontent")
        result, _ = _run_cmd(
            [list_objects_response([self.UPPER_KEY])],
            self._cmd(case_insensitive_workdir, "error"),
            expected_rc=1,
        )
        assert f"Failed to download bucket/{self.UPPER_KEY}" in result.stderr

    # aws-cli: TestSyncCaseConflict.test_error_with_case_conflicts_in_s3 (test_sync_command.py)
    def test_error_with_case_conflicts_in_s3(self, tmp_path: Path) -> None:
        # The gate's fatal cancels the already-admitted first twin (the
        # engine shuts down with cancel=True on a fatal, like aws's manager
        # context), so its download - and with it mv's source delete -
        # normally never becomes an API call. That cancellation races the
        # worker by nature (aws's own sync original scripts only the listing
        # and asserts no operations), so the admitted twin's get-then-delete
        # pair is scripted defensively and the assertions accept either
        # outcome; the conflicting key must never reach the API, and its
        # source must never be deleted.
        result, calls = _run_cmd(
            [
                list_objects_response([self.UPPER_KEY, self.LOWER_KEY]),
                get_object_response(),
                {},
            ],
            self._cmd(tmp_path, "error"),
            expected_rc=1,
            transfer_config=_CASE_CONFLICT_CONFIG,
        )
        assert f"Failed to download bucket/{self.LOWER_KEY}" in result.stderr
        operations = _operations(calls)
        assert operations[0] == "ListObjectsV2"
        assert operations in (
            ["ListObjectsV2"],  # the admitted twin cancelled (the usual case)
            ["ListObjectsV2", "GetObject", "DeleteObject"],  # it out-raced the cancel
        )
        assert all(call.params.get("Key") != self.LOWER_KEY for call in calls[1:])

    def test_warn_with_existing_file(self, case_insensitive_workdir: Path) -> None:
        (case_insensitive_workdir / self.LOWER_KEY).write_text("mycontent")
        result, _ = _run_cmd(
            [list_objects_response([self.UPPER_KEY]), get_object_response(), {}],
            self._cmd(case_insensitive_workdir, "warn"),
        )
        assert f"warning: Downloading bucket/{self.UPPER_KEY}" in result.stderr

    def test_warn_with_case_conflicts_in_s3(self, tmp_path: Path) -> None:
        # An aws-cli `pass` (their threaded get/delete order is flaky); a single
        # worker (_CASE_CONFLICT_CONFIG) makes ours deterministic - the conflict
        # is detected (first twin still in flight) and the order is get, delete
        # per item.
        result, calls = _run_cmd(
            [
                list_objects_response([self.UPPER_KEY, self.LOWER_KEY]),
                get_object_response(),
                {},
                get_object_response(),
                {},
            ],
            self._cmd(tmp_path, "warn"),
            transfer_config=_CASE_CONFLICT_CONFIG,
        )
        assert f"warning: Downloading bucket/{self.LOWER_KEY}" in result.stderr
        assert _operations(calls) == [
            "ListObjectsV2",
            "GetObject",
            "DeleteObject",
            "GetObject",
            "DeleteObject",
        ]

    # aws-cli: TestSyncCaseConflict.test_skip_with_existing_file (test_sync_command.py)
    def test_skip_with_existing_file(self, case_insensitive_workdir: Path) -> None:
        (case_insensitive_workdir / self.LOWER_KEY).write_text("mycontent")
        result, _ = _run_cmd(
            [list_objects_response([self.UPPER_KEY])],
            self._cmd(case_insensitive_workdir, "skip"),
        )
        assert f"warning: Skipping bucket/{self.UPPER_KEY}" in result.stderr

    def test_skip_with_case_conflicts_in_s3(self, tmp_path: Path) -> None:
        result, calls = _run_cmd(
            [
                list_objects_response([self.UPPER_KEY, self.LOWER_KEY]),
                get_object_response(),
                {},
            ],
            self._cmd(tmp_path, "skip"),
            transfer_config=_CASE_CONFLICT_CONFIG,
        )
        assert f"warning: Skipping bucket/{self.LOWER_KEY}" in result.stderr
        # The skipped conflict is neither downloaded nor deleted.
        assert _operations(calls) == ["ListObjectsV2", "GetObject", "DeleteObject"]
        assert calls[2].params == {"Bucket": "bucket", "Key": self.UPPER_KEY}

    def test_ignore_with_existing_file(self, tmp_path: Path) -> None:
        (tmp_path / self.LOWER_KEY).write_text("mycontent")
        _run_cmd(
            [list_objects_response([self.UPPER_KEY]), get_object_response(), {}],
            self._cmd(tmp_path, "ignore"),
        )


class TestS3ExpressMvRecursive:
    def test_s3_express_error_raises_exception(self, tmp_path: Path) -> None:
        result, _ = _run_cmd(
            [],
            [
                "mv",
                "--recursive",
                "s3://bucket--usw2-az1--x-s3",
                str(tmp_path),
                "--case-conflict",
                "error",
            ],
            expected_rc=252,
        )
        assert "`error` is not a valid value" in result.stderr

    def test_s3_express_skip_raises_exception(self, tmp_path: Path) -> None:
        result, _ = _run_cmd(
            [],
            [
                "mv",
                "--recursive",
                "s3://bucket--usw2-az1--x-s3",
                str(tmp_path),
                "--case-conflict",
                "skip",
            ],
            expected_rc=252,
        )
        assert "`skip` is not a valid value" in result.stderr
