"""Port of aws-cli's functional cp tests to ``boto3-s3 cp``.

Provenance: aws-cli's ``tests/functional/s3/test_cp_command.py``
(aws-cli 2.34.x). Test names, canned responses, and expected operations are
kept verbatim where possible so the file stays diffable against the aws-cli
original when aws-cli is updated.

Adaptation rules (on top of the ls/rm ports' - see their module docstrings):

- ``Context.transfer_config`` injects ``TransferConfig(use_threads=False)``
  (the NonThreadedExecutor, exactly what boto3 selects for that flag) so
  multipart call order is deterministic against the positional canned list;
  thresholds stay at the aws default 8 MiB.
- The aws-cli's expected ``ChecksumAlgorithm: 'CRC64NVME'`` becomes
  ``'CRC32'`` on upload-path operations (PutObject / CreateMultipartUpload /
  UploadPart): both engines inject a default integrity checksum - aws via its
  bundled botocore, ours via pip s3transfer - they just pick different
  algorithms. ``ChecksumMode: 'ENABLED'`` on the single-source HeadObject is
  kept verbatim: like aws's filegenerator we setdefault it when the client
  resolves ``response_checksum_validation`` to ``when_supported`` (the botocore
  default), so the recorded HEAD matches aws.
- ``LastModified`` strings become ``datetime`` objects and string
  ``ContentLength``/sizes become ints (no output parser runs, recorder rule).
- ``ListObjectsV2`` expectations gain ``MaxKeys: 1000`` (our explicit
  page-size default; rm port rule) and never show ``EncodingType``.
- ``mock.patch`` targets translate: ``mimetypes.guess_type`` and ``os.utime``
  are identical seams; the aws-cli's ``filegenerator.get_file_stat`` becomes
  ``boto3_s3.localstorage.get_file_stat``; the streaming tests swap ``sys.stdin``
  / ``sys.stdout`` for shims exposing a ``buffer`` instead of patching the
  aws-cli's ``BufferedBytesIO`` onto ``sys.stdin``.
- The case-conflict ``*_with_existing_file`` variants keep the aws-cli's
  skip-on-case-sensitive-filesystem guard (``os.path.exists`` cannot see a
  case variant on Linux), via the ``case_insensitive_workdir`` fixture
  (tests/conftest.py): it runs them on a case-insensitive dir from
  ``BOTO3_S3_PYTEST_CASE_INSENSITIVE_DIR`` (or a natively case-insensitive
  tmp dir on macOS / Windows) and skips otherwise.

Not ported, with reasons:

- ``TestCpWithCRTClient``: the CRT engine is charter exception 2.
- ``TestAccesspointCPCommand``: an ARN endpoint-resolution harness; ARN parsing
  is covered by the unit tier (``TestS3ExpressCpRecursive`` *is* ported below).
- ``TestCpSourceRegion``: a different aws-cli harness (``BaseS3CLIRunnerTest``);
  the client wiring is covered by ``tests/cli/unit/test_cp.py``.
- The s3s3 SSE-C matrix (4 tests with large canned sequences): the parameter
  mapping they pin, including the copy-source HEAD, is covered by
  ``tests/lib/test_requestparams.py``.
- The two s3s3 SSE-KMS *copy* tests (``test_cp_copy_with_sse_kms_and_key_id`` /
  ``..._large_file_...``): ``map_copy_object_params`` shares
  ``_set_sse_request_params`` with the upload path, so the mapping is pinned by
  ``tests/lib/test_requestparams.py`` (``test_sse_and_kms_key`` on both
  ``TestPutObjectParams`` and ``TestCopyObjectParams``); the upload SSE-KMS pair
  is ported here.
- Storage-class variants other than STANDARD_IA / DEEP_ARCHIVE and the
  duplicate recursive-mp prop-override cases: mechanically identical
  siblings of ported tests.
"""

from __future__ import annotations

import datetime as dt
import io
from typing import Any
from unittest import mock

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3.localstorage import LocalStorage
from boto3_s3_cli.commands.base import Context
from tests.utils.harness import CliResult, run_cli_in_process
from tests.utils.recorder import ApiCall, make_recording_client

MB = 1024**2
_TIME_UTC = dt.datetime(2014, 1, 9, 20, 45, 49, tzinfo=dt.timezone.utc)
_SYNC_CONFIG = TransferConfig(use_threads=False)
# The case-conflict "two S3 twins in one listing" tests detect the conflict via
# the gate's in-flight set, which only holds while the first twin's download is
# still running. That needs a non-blocking (threaded) submit so the gate runs
# ahead of completions - aws-cli's own functional tests use a single worker
# (max_concurrent_requests = 1) for exactly this. NonThreadedExecutor would
# complete each twin before the next is judged, emptying the set.
_CASE_CONFLICT_CONFIG = TransferConfig(max_concurrency=1)

SOURCE_BUCKET = "source-bucket"
SOURCE_KEY = "source-key"
TARGET_BUCKET = "target-bucket"
TARGET_KEY = "target-key"
MULTIPART_THRESHOLD = 8 * MB


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


def _operations(calls: list[ApiCall]) -> list[str]:
    return [call.operation for call in calls]


def _assert_in_operations_called(
    calls: list[ApiCall], operation: str, params: dict[str, Any]
) -> None:
    assert (operation, params) in [(c.operation, c.params) for c in calls], calls


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


def list_objects_response(keys: list[str], **override_kwargs: Any) -> dict[str, Any]:
    contents: list[dict[str, Any]] = []
    for key in keys:
        content: dict[str, Any] = {
            "Key": key,
            "LastModified": _TIME_UTC,
            "Size": 100,
            "ETag": '"foo-1"',
        }
        content.update(override_kwargs)
        contents.append(content)
    return {"Contents": contents, "CommonPrefixes": []}


def get_object_response() -> dict[str, Any]:
    return {"ETag": '"foo-1"', "Body": io.BytesIO(b"foo")}


def create_mpu_response(upload_id: str) -> dict[str, Any]:
    return {"UploadId": upload_id}


def upload_part_copy_response() -> dict[str, Any]:
    return {"CopyPartResult": {"ETag": '"etag"'}}


def get_object_tagging_response(tags: dict[str, str]) -> dict[str, Any]:
    return {"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]}


def mp_copy_responses() -> list[dict[str, Any] | Exception]:
    return [create_mpu_response("upload_id"), upload_part_copy_response(), {}]


def all_metadata_directive_props() -> dict[str, Any]:
    return {
        "CacheControl": "cache-control",
        "ContentDisposition": "content-disposition",
        "ContentEncoding": "content-encoding",
        "ContentLanguage": "content-language",
        "ContentType": "content-type",
        "Expires": "Tue, 07 Jan 2020 20:40:03 GMT",
        "Metadata": {"key": "value"},
    }


def copy_command(copy_props: str | None = None, extra: str = "") -> list[str]:
    argv = ["cp", f"s3://{SOURCE_BUCKET}/{SOURCE_KEY}", f"s3://{TARGET_BUCKET}/{TARGET_KEY}"]
    if copy_props:
        argv += ["--copy-props", copy_props]
    if extra:
        argv += extra.split()
    return argv


def recursive_copy_command(copy_props: str | None = None, extra: str = "") -> list[str]:
    argv = ["cp", f"s3://{SOURCE_BUCKET}/", f"s3://{TARGET_BUCKET}/", "--recursive"]
    if copy_props:
        argv += ["--copy-props", copy_props]
    if extra:
        argv += extra.split()
    return argv


def copy_object_request(**override_kwargs: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "Bucket": TARGET_BUCKET,
        "Key": TARGET_KEY,
        "CopySource": {"Bucket": SOURCE_BUCKET, "Key": SOURCE_KEY},
    }
    params.update(override_kwargs)
    return params


def create_mpu_request(key: str = TARGET_KEY, **override_kwargs: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"Bucket": TARGET_BUCKET, "Key": key}
    params.update(override_kwargs)
    return params


class TestCPCommand:
    def test_operations_used_in_upload(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            ["cp", full_path, "s3://bucket/key.txt"],
        )
        # The only operation we should have called is PutObject.
        assert len(calls) == 1, calls
        assert calls[0].operation == "PutObject"

    def test_key_name_added_when_only_bucket_provided(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}], ["cp", full_path, "s3://bucket/"]
        )
        assert len(calls) == 1, calls
        assert calls[0].operation == "PutObject"
        assert calls[0].params["Key"] == "foo.txt"
        assert calls[0].params["Bucket"] == "bucket"

    def test_trailing_slash_appended(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        # Here we're saying s3://bucket instead of s3://bucket/: this should
        # still work the same as if we added the trailing slash.
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}], ["cp", full_path, "s3://bucket"]
        )
        assert len(calls) == 1, calls
        assert calls[0].operation == "PutObject"
        assert calls[0].params["Key"] == "foo.txt"
        assert calls[0].params["Bucket"] == "bucket"

    def test_dryrun_upload(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        result, calls = _run_cmd([], ["cp", full_path, "s3://bucket/key.txt", "--dryrun"])
        assert calls == []
        assert (
            f"(dryrun) upload: {LocalStorage.relative_path(full_path)} to s3://bucket/key.txt"
            in (result.stdout)
        )

    def test_error_on_same_line_as_status(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        result, _ = _run_cmd(
            [_client_error("BucketNotExists", 400, "PutObject")],
            ["cp", full_path, "s3://bucket-not-exist/key.txt"],
            expected_rc=1,
        )
        rendered = LocalStorage.relative_path(full_path)
        assert (
            f"upload failed: {rendered} to s3://bucket-not-exist/key.txt An error"
        ) in result.stderr

    def test_upload_grants(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            [
                "cp",
                full_path,
                "s3://bucket/key.txt",
                "--grants",
                "read=id=foo",
                "full=id=bar",
                "readacl=id=biz",
                "writeacl=id=baz",
            ],
        )
        assert len(calls) == 1, calls
        assert calls[0].operation == "PutObject"
        assert calls[0].params == {
            "Key": "key.txt",
            "Bucket": "bucket",
            "ChecksumAlgorithm": "CRC32",
            "GrantRead": "id=foo",
            "GrantFullControl": "id=bar",
            "GrantReadACP": "id=biz",
            "GrantWriteACP": "id=baz",
            "ContentType": "text/plain",
            "Body": mock.ANY,
        }

    def test_upload_expires(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            ["cp", full_path, "s3://bucket/key.txt", "--expires", "90"],
        )
        assert len(calls) == 1, calls
        assert calls[0].operation == "PutObject"
        assert calls[0].params["Key"] == "key.txt"
        assert calls[0].params["Bucket"] == "bucket"
        assert calls[0].params["Expires"] == "90"

    @pytest.mark.parametrize("storage_class", ["STANDARD_IA", "DEEP_ARCHIVE"])
    def test_upload_storage_class(self, tmp_path: Any, storage_class: str) -> None:
        # aws-cli test_upload_standard_ia / test_upload_deep_archive (the
        # other storage-class variants are mechanically identical).
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            ["cp", full_path, "s3://bucket/key.txt", "--storage-class", storage_class],
        )
        assert len(calls) == 1, calls
        args = calls[0].params
        assert args["Key"] == "key.txt"
        assert args["Bucket"] == "bucket"
        assert args["StorageClass"] == storage_class

    def test_operations_used_in_download_file(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [
                {"ContentLength": 100, "LastModified": _TIME_UTC, "ETag": '"foo-1"'},
                {"ETag": '"foo-1"', "Body": io.BytesIO(b"foo")},
            ],
            ["cp", "s3://bucket/key.txt", str(tmp_path)],
        )
        # The only operations we should have called are HeadObject/GetObject.
        assert _operations(calls) == ["HeadObject", "GetObject"]

    def test_operations_used_in_recursive_download(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [{"ETag": '"foo-1"', "Contents": [], "CommonPrefixes": []}],
            ["cp", "s3://bucket/key.txt", str(tmp_path), "--recursive"],
        )
        # We called ListObjectsV2 but had no objects to download.
        assert _operations(calls) == ["ListObjectsV2"]

    def test_dryrun_download(self, tmp_path: Any) -> None:
        target = str(tmp_path / "file.txt")
        result, calls = _run_cmd(
            [head_object_response()], ["cp", "s3://bucket/key.txt", target, "--dryrun"]
        )
        assert [(c.operation, c.params) for c in calls] == [
            ("HeadObject", {"Bucket": "bucket", "Key": "key.txt", "ChecksumMode": "ENABLED"})
        ]
        assert (
            f"(dryrun) download: s3://bucket/key.txt to {LocalStorage.relative_path(target)}"
            in (result.stdout)
        )

    def test_website_redirect_ignore_paramfile(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            ["cp", full_path, "s3://bucket/key.txt", "--website-redirect", "http://someserver"],
        )
        # The specified web address is used as opposed to its contents.
        assert calls[0].params["WebsiteRedirectLocation"] == "http://someserver"

    def test_dryrun_copy(self, tmp_path: Any) -> None:
        result, calls = _run_cmd(
            [head_object_response()],
            ["cp", "s3://bucket/key.txt", "s3://bucket/key2.txt", "--dryrun"],
        )
        assert [(c.operation, c.params) for c in calls] == [
            ("HeadObject", {"Bucket": "bucket", "Key": "key.txt", "ChecksumMode": "ENABLED"})
        ]
        assert "(dryrun) copy: s3://bucket/key.txt to s3://bucket/key2.txt" in result.stdout

    def test_metadata_copy(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(), {"ETag": '"foo-1"'}],
            ["cp", "s3://bucket/key.txt", "s3://bucket/key2.txt", "--metadata", "KeyName=Value"],
        )
        assert _operations(calls) == ["HeadObject", "CopyObject"]
        assert calls[1].params["Metadata"] == {"KeyName": "Value"}

    def test_metadata_copy_with_put_object(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [{"ETag": '"foo-1"'}],
            ["cp", full_path, "s3://bucket/key2.txt", "--metadata", "KeyName=Value"],
        )
        assert _operations(calls) == ["PutObject"]
        assert calls[0].params["Metadata"] == {"KeyName": "Value"}

    def test_metadata_copy_with_multipart_upload(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_bytes(b"a" * 10 * MB)
        _, calls = _run_cmd(
            [{"UploadId": "foo"}, {"ETag": '"foo-1"'}, {"ETag": '"foo-2"'}, {}],
            ["cp", full_path, "s3://bucket/key2.txt", "--metadata", "KeyName=Value"],
        )
        assert len(calls) == 4, calls
        assert calls[0].operation == "CreateMultipartUpload"
        assert calls[0].params["Metadata"] == {"KeyName": "Value"}

    def test_metadata_directive_copy(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(), {"ETag": '"foo-1"'}],
            [
                "cp",
                "s3://bucket/key.txt",
                "s3://bucket/key2.txt",
                "--metadata-directive",
                "REPLACE",
            ],
        )
        assert _operations(calls) == ["HeadObject", "CopyObject"]
        assert calls[1].params["MetadataDirective"] == "REPLACE"

    def test_no_metadata_directive_for_non_copy(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            ["cp", full_path, "s3://bucket", "--metadata-directive", "REPLACE"],
        )
        assert _operations(calls) == ["PutObject"]
        assert "MetadataDirective" not in calls[0].params

    def test_cp_succeeds_with_mimetype_errors(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        with mock.patch("mimetypes.guess_type") as mock_guess_type:
            # This should throw a UnicodeDecodeError.
            mock_guess_type.side_effect = lambda x: b"\xe2".decode("ascii")
            _, calls = _run_cmd(
                [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
                ["cp", full_path, "s3://bucket/key.txt"],
            )
        # Because of the decoding error the command should have succeeded,
        # just without a content type added.
        assert "ContentType" not in calls[0].params

    def test_cp_fails_with_utime_errors_but_continues(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("")
        with mock.patch("os.utime") as mock_utime:
            mock_utime.side_effect = OSError(1, "")
            result, _ = _run_cmd(
                [
                    {"ContentLength": 100, "LastModified": _TIME_UTC, "ETag": '"foo-1"'},
                    {"ETag": '"foo-1"', "Body": io.BytesIO(b"foo")},
                ],
                ["cp", "s3://bucket/key.txt", full_path],
                expected_rc=2,
            )
        assert "attempting to modify the utime" in result.stderr

    def test_recursive_glacier_download_with_force_glacier(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [
                {
                    "Contents": [
                        {
                            "Key": "foo/bar.txt",
                            "ContentLength": 100,
                            "LastModified": _TIME_UTC,
                            "StorageClass": "GLACIER",
                            "Size": 100,
                            "ETag": '"foo-1"',
                        }
                    ],
                    "CommonPrefixes": [],
                },
                {"ETag": '"foo-1"', "Body": io.BytesIO(b"foo")},
            ],
            ["cp", "s3://bucket/foo", str(tmp_path), "--recursive", "--force-glacier-transfer"],
        )
        assert _operations(calls) == ["ListObjectsV2", "GetObject"]

    def test_recursive_glacier_download_without_force_glacier(self, tmp_path: Any) -> None:
        result, calls = _run_cmd(
            [
                {
                    "Contents": [
                        {
                            "Key": "foo/bar.txt",
                            "ContentLength": 100,
                            "LastModified": _TIME_UTC,
                            "StorageClass": "GLACIER",
                            "Size": 100,
                        }
                    ],
                    "CommonPrefixes": [],
                }
            ],
            ["cp", "s3://bucket/foo", str(tmp_path), "--recursive"],
            expected_rc=2,
        )
        assert _operations(calls) == ["ListObjectsV2"]
        assert "GLACIER" in result.stderr

    @pytest.mark.parametrize(
        ("storage_class", "content_length"),
        [
            ("GLACIER", 100),
            ("DEEP_ARCHIVE", 100),
            ("GLACIER", 20 * MB),
            ("DEEP_ARCHIVE", 20 * MB),
        ],
    )
    def test_warns_on_glacier_incompatible_operation(
        self, tmp_path: Any, storage_class: str, content_length: int
    ) -> None:
        # aws-cli test_warns_on_{glacier,deep_arhive}_incompatible_operation
        # (+ the _for_multipart_file variants).
        result, calls = _run_cmd(
            [
                {
                    "ContentLength": content_length,
                    "LastModified": _TIME_UTC,
                    "StorageClass": storage_class,
                }
            ],
            ["cp", "s3://bucket/key.txt", "."],
            expected_rc=2,
        )
        assert _operations(calls) == ["HeadObject"]
        assert "GLACIER" in result.stderr

    @pytest.mark.parametrize("storage_class", ["GLACIER", "DEEP_ARCHIVE"])
    def test_turn_off_glacier_warnings(self, tmp_path: Any, storage_class: str) -> None:
        result, calls = _run_cmd(
            [
                {
                    "ContentLength": 20 * MB,
                    "LastModified": _TIME_UTC,
                    "StorageClass": storage_class,
                }
            ],
            ["cp", "s3://bucket/key.txt", ".", "--ignore-glacier-warnings"],
        )
        assert _operations(calls) == ["HeadObject"]
        assert result.stderr == ""

    def test_cp_with_sse_flag(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("contents")
        _, calls = _run_cmd([{}], ["cp", full_path, "s3://bucket/key.txt", "--sse"])
        assert len(calls) == 1
        assert calls[0].operation == "PutObject"
        assert calls[0].params == {
            "Key": "key.txt",
            "Bucket": "bucket",
            "ChecksumAlgorithm": "CRC32",
            "ContentType": "text/plain",
            "Body": mock.ANY,
            "ServerSideEncryption": "AES256",
        }

    def test_cp_with_sse_c_flag(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("contents")
        _, calls = _run_cmd(
            [{}], ["cp", full_path, "s3://bucket/key.txt", "--sse-c", "--sse-c-key", "foo"]
        )
        assert len(calls) == 1
        assert calls[0].operation == "PutObject"
        assert calls[0].params == {
            "Key": "key.txt",
            "Bucket": "bucket",
            "ChecksumAlgorithm": "CRC32",
            "ContentType": "text/plain",
            "Body": mock.ANY,
            "SSECustomerAlgorithm": "AES256",
            "SSECustomerKey": "foo",
        }

    def test_cp_with_sse_c_fileb(self, tmp_path: Any) -> None:
        file_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("contents")
        key_path = str(tmp_path / "foo.key")
        key_contents = (
            b"K\xc9G\xe1\xf9&\xee\xd1\x03\xf3\xd4\x10\x18o9E\xc2\xaeD"
            b"\x89(\x18\xea\xda\xf6\x81\xc3\xd2\x9d\\\xa8\xe6"
        )
        (tmp_path / "foo.key").write_bytes(key_contents)
        _, calls = _run_cmd(
            [{}],
            [
                "cp",
                file_path,
                "s3://bucket/key.txt",
                "--sse-c",
                "--sse-c-key",
                f"fileb://{key_path}",
            ],
        )
        assert len(calls) == 1
        assert calls[0].operation == "PutObject"
        assert calls[0].params == {
            "Key": "key.txt",
            "Bucket": "bucket",
            "ChecksumAlgorithm": "CRC32",
            "ContentType": "text/plain",
            "Body": mock.ANY,
            "SSECustomerAlgorithm": "AES256",
            "SSECustomerKey": key_contents,
        }

    def test_cp_upload_with_sse_kms_and_key_id(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("contents")
        _, calls = _run_cmd(
            [{}],
            ["cp", full_path, "s3://bucket/key.txt", "--sse", "aws:kms", "--sse-kms-key-id", "foo"],
        )
        assert len(calls) == 1
        assert calls[0].operation == "PutObject"
        assert calls[0].params == {
            "Key": "key.txt",
            "Bucket": "bucket",
            "ChecksumAlgorithm": "CRC32",
            "ContentType": "text/plain",
            "Body": mock.ANY,
            "SSEKMSKeyId": "foo",
            "ServerSideEncryption": "aws:kms",
        }

    def test_cp_upload_large_file_with_sse_kms_and_key_id(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_bytes(b"a" * 10 * MB)
        _, calls = _run_cmd(
            [{"UploadId": "foo"}, {"ETag": '"foo"'}, {"ETag": '"foo"'}, {}],
            [
                "cp",
                full_path,
                "s3://bucket/key.txt",
                "--copy-props",
                "none",
                "--sse",
                "aws:kms",
                "--sse-kms-key-id",
                "foo",
            ],
        )
        assert len(calls) == 4
        # We are only really concerned that the CreateMultipartUpload used
        # the KMS key id.
        assert calls[0].operation == "CreateMultipartUpload"
        assert calls[0].params == {
            "Key": "key.txt",
            "Bucket": "bucket",
            "ChecksumAlgorithm": "CRC32",
            "ContentType": "text/plain",
            "SSEKMSKeyId": "foo",
            "ServerSideEncryption": "aws:kms",
        }

    def test_upload_unicode_path(self, tmp_path: Any) -> None:
        result, _ = _run_cmd(
            [
                {"ContentLength": 10, "LastModified": _TIME_UTC, "ETag": '"foo"'},
                {"ETag": '"foo"'},
            ],
            ["cp", "s3://bucket/\u2603", "s3://bucket/\u2713"],
        )
        assert "copy: s3://bucket/\u2603 to s3://bucket/\u2713" in result.stdout
        assert "Completed 10 Bytes" in result.stdout

    def test_cp_with_error_and_warning_permissions(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("bar")
        # Patch the stat helper to report an invalid timestamp (impossible to
        # produce portably on a real filesystem; aws-cli patches its
        # get_file_stat the same way).
        with mock.patch("boto3_s3.localstorage.get_file_stat", return_value=(None, None)):
            result, _ = _run_cmd(
                [_client_error("NoSuchBucket", 404, "PutObject")],
                ["cp", full_path, "s3://bucket/foo.txt"],
                expected_rc=1,
            )
        assert "upload failed" in result.stderr
        assert "warning: File has an invalid timestamp." in result.stderr


class TestCpCommandWithRequesterPayer:
    def test_single_upload(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "myfile")
        (tmp_path / "myfile").write_text("mycontent")
        _, calls = _run_cmd([{}], ["cp", full_path, "s3://mybucket/mykey", "--request-payer"])
        assert [(c.operation, c.params) for c in calls] == [
            (
                "PutObject",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "ChecksumAlgorithm": "CRC32",
                    "RequestPayer": "requester",
                    "Body": mock.ANY,
                },
            )
        ]

    def test_multipart_upload(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "myfile")
        (tmp_path / "myfile").write_bytes(b"a" * 10 * MB)
        _, calls = _run_cmd(
            [{"UploadId": "myid"}, {"ETag": '"myetag"'}, {"ETag": '"myetag"'}, {}],
            ["cp", full_path, "s3://mybucket/mykey", "--request-payer"],
        )
        assert [(c.operation, c.params) for c in calls] == [
            (
                "CreateMultipartUpload",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "ChecksumAlgorithm": "CRC32",
                    "RequestPayer": "requester",
                },
            ),
            (
                "UploadPart",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "ChecksumAlgorithm": "CRC32",
                    "RequestPayer": "requester",
                    "UploadId": "myid",
                    "PartNumber": mock.ANY,
                    "Body": mock.ANY,
                },
            ),
            (
                "UploadPart",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "ChecksumAlgorithm": "CRC32",
                    "RequestPayer": "requester",
                    "UploadId": "myid",
                    "PartNumber": mock.ANY,
                    "Body": mock.ANY,
                },
            ),
            (
                "CompleteMultipartUpload",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "RequestPayer": "requester",
                    "UploadId": "myid",
                    "MultipartUpload": {
                        "Parts": [
                            {"ETag": '"myetag"', "PartNumber": 1},
                            {"ETag": '"myetag"', "PartNumber": 2},
                        ]
                    },
                },
            ),
        ]

    def test_recursive_upload(self, tmp_path: Any) -> None:
        (tmp_path / "myfile").write_text("mycontent")
        _, calls = _run_cmd(
            [{}], ["cp", str(tmp_path), "s3://mybucket/", "--request-payer", "--recursive"]
        )
        assert [(c.operation, c.params) for c in calls] == [
            (
                "PutObject",
                {
                    "Bucket": "mybucket",
                    "Key": "myfile",
                    "ChecksumAlgorithm": "CRC32",
                    "RequestPayer": "requester",
                    "Body": mock.ANY,
                },
            )
        ]

    def test_single_download(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(), get_object_response()],
            ["cp", "s3://mybucket/mykey", str(tmp_path), "--request-payer"],
        )
        assert [(c.operation, c.params) for c in calls] == [
            (
                "HeadObject",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "RequestPayer": "requester",
                    "ChecksumMode": "ENABLED",
                },
            ),
            ("GetObject", {"Bucket": "mybucket", "Key": "mykey", "RequestPayer": "requester"}),
        ]

    def test_ranged_download(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(ContentLength=10 * MB),
                get_object_response(),
                get_object_response(),
            ],
            ["cp", "s3://mybucket/mykey", str(tmp_path), "--request-payer"],
        )
        assert [(c.operation, c.params) for c in calls] == [
            (
                "HeadObject",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "RequestPayer": "requester",
                    "ChecksumMode": "ENABLED",
                },
            ),
            (
                "GetObject",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "Range": mock.ANY,
                    "RequestPayer": "requester",
                    "IfMatch": '"foo-1"',
                },
            ),
            (
                "GetObject",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "Range": mock.ANY,
                    "RequestPayer": "requester",
                    "IfMatch": '"foo-1"',
                },
            ),
        ]

    def test_recursive_download(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [list_objects_response(["mykey"]), get_object_response()],
            ["cp", "s3://mybucket/", str(tmp_path), "--request-payer", "--recursive"],
        )
        assert [(c.operation, c.params) for c in calls] == [
            (
                "ListObjectsV2",
                {
                    "Bucket": "mybucket",
                    "Prefix": "",
                    "RequestPayer": "requester",
                    "MaxKeys": 1000,
                },
            ),
            ("GetObject", {"Bucket": "mybucket", "Key": "mykey", "RequestPayer": "requester"}),
        ]

    def test_single_copy(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(), {}],
            ["cp", "s3://sourcebucket/sourcekey", "s3://mybucket/mykey", "--request-payer"],
        )
        assert [(c.operation, c.params) for c in calls] == [
            (
                "HeadObject",
                {
                    "Bucket": "sourcebucket",
                    "Key": "sourcekey",
                    "RequestPayer": "requester",
                    "ChecksumMode": "ENABLED",
                },
            ),
            (
                "CopyObject",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "CopySource": {"Bucket": "sourcebucket", "Key": "sourcekey"},
                    "RequestPayer": "requester",
                },
            ),
        ]

    def test_multipart_copy(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(ContentLength=10 * MB),
                create_mpu_response("id"),
                upload_part_copy_response(),
                upload_part_copy_response(),
                {},
            ],
            [
                "cp",
                "s3://sourcebucket/sourcekey",
                "s3://mybucket/mykey",
                "--copy-props",
                "none",
                "--request-payer",
            ],
        )
        expected_part = {
            "Bucket": "mybucket",
            "Key": "mykey",
            "CopySource": {"Bucket": "sourcebucket", "Key": "sourcekey"},
            "UploadId": "id",
            "PartNumber": mock.ANY,
            "RequestPayer": "requester",
            "CopySourceRange": mock.ANY,
            "CopySourceIfMatch": '"foo-1"',
        }
        assert [(c.operation, c.params) for c in calls] == [
            (
                "HeadObject",
                {
                    "Bucket": "sourcebucket",
                    "Key": "sourcekey",
                    "RequestPayer": "requester",
                    "ChecksumMode": "ENABLED",
                },
            ),
            (
                "CreateMultipartUpload",
                {"Bucket": "mybucket", "Key": "mykey", "RequestPayer": "requester"},
            ),
            ("UploadPartCopy", expected_part),
            ("UploadPartCopy", expected_part),
            (
                "CompleteMultipartUpload",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "UploadId": "id",
                    "RequestPayer": "requester",
                    "MultipartUpload": {
                        "Parts": [
                            {"ETag": '"etag"', "PartNumber": 1},
                            {"ETag": '"etag"', "PartNumber": 2},
                        ]
                    },
                },
            ),
        ]

    def test_recursive_copy(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [list_objects_response(["mykey"]), {}],
            ["cp", "s3://sourcebucket/", "s3://mybucket/", "--request-payer", "--recursive"],
        )
        assert [(c.operation, c.params) for c in calls] == [
            (
                "ListObjectsV2",
                {
                    "Bucket": "sourcebucket",
                    "Prefix": "",
                    "RequestPayer": "requester",
                    "MaxKeys": 1000,
                },
            ),
            (
                "CopyObject",
                {
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "CopySource": {"Bucket": "sourcebucket", "Key": "mykey"},
                    "RequestPayer": "requester",
                },
            ),
        ]

    def test_mp_copy_object(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(ContentLength=MULTIPART_THRESHOLD),
                get_object_tagging_response({}),
                *mp_copy_responses(),
            ],
            ["cp", "s3://sourcebucket/mykey", "s3://mybucket/mykey", "--request-payer"],
        )
        _assert_in_operations_called(
            calls,
            "CreateMultipartUpload",
            {"Bucket": "mybucket", "Key": "mykey", "RequestPayer": "requester"},
        )
        _assert_in_operations_called(
            calls,
            "GetObjectTagging",
            {"Bucket": "sourcebucket", "Key": "mykey", "RequestPayer": "requester"},
        )

    def test_mp_copy_object_with_tags_exceed_2k(self, tmp_path: Any) -> None:
        big_tags = {"tag-key": "value" * (2 * 1024)}
        _, calls = _run_cmd(
            [
                head_object_response(ContentLength=MULTIPART_THRESHOLD),
                get_object_tagging_response(big_tags),
                *mp_copy_responses(),
                {},  # PutObjectTagging
            ],
            ["cp", "s3://sourcebucket/mykey", "s3://mybucket/mykey", "--request-payer"],
        )
        _assert_in_operations_called(
            calls,
            "CreateMultipartUpload",
            {"Bucket": "mybucket", "Key": "mykey", "RequestPayer": "requester"},
        )
        _assert_in_operations_called(
            calls,
            "PutObjectTagging",
            {
                "Bucket": "mybucket",
                "Key": "mykey",
                "Tagging": {"TagSet": [{"Key": "tag-key", "Value": "value" * (2 * 1024)}]},
                "RequestPayer": "requester",
            },
        )


class TestCopyPropsNoneCpCommand:
    def test_copy_object(self, tmp_path: Any) -> None:
        _, calls = _run_cmd([head_object_response(), {}], copy_command(copy_props="none"))
        _assert_in_operations_called(
            calls,
            "CopyObject",
            copy_object_request(MetadataDirective="REPLACE", TaggingDirective="REPLACE"),
        )

    def test_mp_copy_object(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(ContentLength=MULTIPART_THRESHOLD), *mp_copy_responses()],
            copy_command(copy_props="none"),
        )
        # The CreateMultipartUpload is where additional parameters are
        # typically added; it should have only the replace directives the
        # engine seeds (s3transfer keeps directives off the create call).
        _assert_in_operations_called(calls, "CreateMultipartUpload", create_mpu_request())

    def test_metadata_directive_disables_copy_props(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(), {}],
            copy_command(copy_props="none", extra="--metadata-directive COPY"),
        )
        _assert_in_operations_called(
            calls, "CopyObject", copy_object_request(MetadataDirective="COPY")
        )


class TestCopyPropsMetadataDirectiveCpCommand:
    def test_copy_object(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(), {}], copy_command(copy_props="metadata-directive")
        )
        _assert_in_operations_called(
            calls, "CopyObject", copy_object_request(TaggingDirective="REPLACE")
        )

    def test_copy_object_overrides_with_cmdline_props(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(**all_metadata_directive_props()), {}],
            copy_command(
                copy_props="metadata-directive",
                extra="--content-type content-type-from-cmdline",
            ),
        )
        expected = all_metadata_directive_props()
        expected["MetadataDirective"] = "REPLACE"
        expected["TaggingDirective"] = "REPLACE"
        expected["ContentType"] = "content-type-from-cmdline"
        _assert_in_operations_called(calls, "CopyObject", copy_object_request(**expected))

    def test_recursive_copy_object(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [list_objects_response(keys=[SOURCE_KEY]), {}, {}],
            recursive_copy_command(copy_props="metadata-directive"),
        )
        _assert_in_operations_called(
            calls,
            "CopyObject",
            {
                "Bucket": TARGET_BUCKET,
                "Key": SOURCE_KEY,
                "CopySource": {"Bucket": SOURCE_BUCKET, "Key": SOURCE_KEY},
                "TaggingDirective": "REPLACE",
            },
        )

    def test_recursive_copy_object_overrides_with_cmdline_props(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [
                list_objects_response(keys=[SOURCE_KEY]),
                head_object_response(**all_metadata_directive_props()),
                {},
                {},
            ],
            recursive_copy_command(
                copy_props="metadata-directive", extra="--metadata key=val-from-cmdline"
            ),
        )
        expected = all_metadata_directive_props()
        expected["MetadataDirective"] = "REPLACE"
        expected["TaggingDirective"] = "REPLACE"
        expected["Metadata"] = {"key": "val-from-cmdline"}
        _assert_in_operations_called(
            calls,
            "CopyObject",
            {
                "Bucket": TARGET_BUCKET,
                "Key": SOURCE_KEY,
                "CopySource": {"Bucket": SOURCE_BUCKET, "Key": SOURCE_KEY},
                **expected,
            },
        )

    def test_recursive_copy_maps_additional_head_object_headers(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [
                list_objects_response(keys=[SOURCE_KEY]),
                head_object_response(),
                {},
            ],
            recursive_copy_command(
                copy_props="metadata-directive",
                extra="--metadata key=val-from-cmdline --request-payer requester",
            ),
        )
        _assert_in_operations_called(
            calls,
            "HeadObject",
            {"Bucket": SOURCE_BUCKET, "Key": SOURCE_KEY, "RequestPayer": "requester"},
        )

    def test_mp_copy_object(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(
                    ContentLength=MULTIPART_THRESHOLD, **all_metadata_directive_props()
                ),
                *mp_copy_responses(),
            ],
            copy_command(copy_props="metadata-directive"),
        )
        _assert_in_operations_called(
            calls, "CreateMultipartUpload", create_mpu_request(**all_metadata_directive_props())
        )

    def test_recursive_mp_copy(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [
                list_objects_response(keys=[SOURCE_KEY], Size=MULTIPART_THRESHOLD),
                head_object_response(**all_metadata_directive_props()),
                *mp_copy_responses(),
            ],
            recursive_copy_command(copy_props="metadata-directive"),
        )
        _assert_in_operations_called(
            calls,
            "CreateMultipartUpload",
            create_mpu_request(key=SOURCE_KEY, **all_metadata_directive_props()),
        )

    def test_fails_when_head_object_fails(self, tmp_path: Any) -> None:
        result, _ = _run_cmd(
            [
                list_objects_response(keys=[SOURCE_KEY], Size=MULTIPART_THRESHOLD),
                _client_error("NoSuchKey", 404, "HeadObject"),
            ],
            recursive_copy_command(copy_props="metadata-directive"),
            expected_rc=1,
        )
        assert "NoSuchKey" in result.stderr

    def test_metadata_directive_disables_copy_props(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(), {}],
            copy_command(copy_props="metadata-directive", extra="--metadata-directive REPLACE"),
        )
        _assert_in_operations_called(
            calls, "CopyObject", copy_object_request(MetadataDirective="REPLACE")
        )


class TestCopyPropsDefaultCpCommand:
    def test_copy_object(self, tmp_path: Any) -> None:
        _, calls = _run_cmd([head_object_response(), {}], copy_command(copy_props="default"))
        # The CopyObject should have no additional parameters other than
        # copy source, bucket, and key.
        _assert_in_operations_called(calls, "CopyObject", copy_object_request())

    def test_is_default_value(self, tmp_path: Any) -> None:
        _, calls = _run_cmd([head_object_response(), {}], copy_command(copy_props=None))
        _assert_in_operations_called(calls, "CopyObject", copy_object_request())

    def test_copy_object_with_prop_overrides(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(**all_metadata_directive_props()), {}],
            copy_command(
                copy_props="default", extra="--content-language content-lang-from-cmdline"
            ),
        )
        expected = all_metadata_directive_props()
        expected["ContentLanguage"] = "content-lang-from-cmdline"
        expected["MetadataDirective"] = "REPLACE"
        _assert_in_operations_called(calls, "CopyObject", copy_object_request(**expected))

    def test_recursive_copy_object(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [
                list_objects_response(keys=[SOURCE_KEY]),
                head_object_response(**all_metadata_directive_props()),
                {},
            ],
            recursive_copy_command(copy_props="default"),
        )
        _assert_in_operations_called(
            calls,
            "CopyObject",
            {
                "Bucket": TARGET_BUCKET,
                "Key": SOURCE_KEY,
                "CopySource": {"Bucket": SOURCE_BUCKET, "Key": SOURCE_KEY},
            },
        )

    def test_mp_copy_object(self, tmp_path: Any) -> None:
        tags = {"tag-key": "tag-value", "tag-key2": "tag-value2"}
        _, calls = _run_cmd(
            [
                head_object_response(
                    ContentLength=MULTIPART_THRESHOLD, **all_metadata_directive_props()
                ),
                get_object_tagging_response(tags),
                *mp_copy_responses(),
            ],
            copy_command(copy_props="default"),
        )
        expected = all_metadata_directive_props()
        expected["Tagging"] = "tag-key=tag-value&tag-key2=tag-value2"
        _assert_in_operations_called(calls, "CreateMultipartUpload", create_mpu_request(**expected))

    def test_mp_copy_object_no_tags(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(ContentLength=MULTIPART_THRESHOLD),
                get_object_tagging_response({}),
                *mp_copy_responses(),
            ],
            copy_command(copy_props="default"),
        )
        _assert_in_operations_called(calls, "CreateMultipartUpload", create_mpu_request())

    def test_mp_copy_object_tags_exceed_2k(self, tmp_path: Any) -> None:
        big_tags = {"tag-key": "value" * (2 * 1024)}
        _, calls = _run_cmd(
            [
                head_object_response(ContentLength=MULTIPART_THRESHOLD),
                get_object_tagging_response(big_tags),
                *mp_copy_responses(),
                {},  # PutObjectTagging
            ],
            copy_command(copy_props="default"),
        )
        _assert_in_operations_called(calls, "CreateMultipartUpload", create_mpu_request())
        _assert_in_operations_called(
            calls,
            "PutObjectTagging",
            {
                "Bucket": TARGET_BUCKET,
                "Key": TARGET_KEY,
                "Tagging": {"TagSet": [{"Key": "tag-key", "Value": "value" * (2 * 1024)}]},
            },
        )

    def test_recursive_mp_copy_object(self, tmp_path: Any) -> None:
        tags = {"tag-key": "tag-value", "tag-key2": "tag-value2"}
        _, calls = _run_cmd(
            [
                list_objects_response(keys=[SOURCE_KEY], Size=MULTIPART_THRESHOLD),
                head_object_response(**all_metadata_directive_props()),
                get_object_tagging_response(tags),
                *mp_copy_responses(),
            ],
            recursive_copy_command(copy_props="default"),
        )
        expected = all_metadata_directive_props()
        expected["Tagging"] = "tag-key=tag-value&tag-key2=tag-value2"
        _assert_in_operations_called(
            calls, "CreateMultipartUpload", create_mpu_request(key=SOURCE_KEY, **expected)
        )

    def test_recursive_mp_copy_tags_exceed_2k(self, tmp_path: Any) -> None:
        big_tags = {"tag-key": "value" * (2 * 1024)}
        _, calls = _run_cmd(
            [
                list_objects_response(keys=[SOURCE_KEY], Size=MULTIPART_THRESHOLD),
                head_object_response(),
                get_object_tagging_response(big_tags),
                *mp_copy_responses(),
                {},  # PutObjectTagging
            ],
            recursive_copy_command(copy_props="default"),
        )
        _assert_in_operations_called(
            calls, "CreateMultipartUpload", create_mpu_request(key=SOURCE_KEY)
        )
        _assert_in_operations_called(
            calls,
            "PutObjectTagging",
            {
                "Bucket": TARGET_BUCKET,
                "Key": SOURCE_KEY,
                "Tagging": {"TagSet": [{"Key": "tag-key", "Value": "value" * (2 * 1024)}]},
            },
        )

    def test_fails_when_head_object_fails(self, tmp_path: Any) -> None:
        result, _ = _run_cmd(
            [
                list_objects_response(keys=[SOURCE_KEY], Size=MULTIPART_THRESHOLD),
                _client_error("NoSuchKey", 404, "HeadObject"),
            ],
            recursive_copy_command(copy_props="default"),
            expected_rc=1,
        )
        assert "NoSuchKey" in result.stderr

    def test_fails_when_get_tagging_object_fails(self, tmp_path: Any) -> None:
        result, _ = _run_cmd(
            [
                head_object_response(ContentLength=MULTIPART_THRESHOLD),
                _client_error("AccessDenied", 403, "GetObjectTagging"),
            ],
            copy_command(copy_props="default"),
            expected_rc=1,
        )
        assert "AccessDenied" in result.stderr

    def test_fails_and_cleans_up_when_put_tagging_object_fails(self, tmp_path: Any) -> None:
        big_tags = {"tag-key": "value" * (2 * 1024)}
        result, calls = _run_cmd(
            [
                head_object_response(ContentLength=MULTIPART_THRESHOLD),
                get_object_tagging_response(big_tags),
                *mp_copy_responses(),
                _client_error("AccessDenied", 403, "PutObjectTagging"),
                {},  # DeleteObject
            ],
            copy_command(copy_props="default"),
            expected_rc=1,
        )
        assert "AccessDenied" in result.stderr
        _assert_in_operations_called(
            calls, "DeleteObject", {"Bucket": TARGET_BUCKET, "Key": TARGET_KEY}
        )

    def test_clean_up_uses_requester_payer(self, tmp_path: Any) -> None:
        big_tags = {"tag-key": "value" * (2 * 1024)}
        result, calls = _run_cmd(
            [
                head_object_response(ContentLength=MULTIPART_THRESHOLD),
                get_object_tagging_response(big_tags),
                *mp_copy_responses(),
                _client_error("AccessDenied", 403, "PutObjectTagging"),
                {},  # DeleteObject
            ],
            copy_command(copy_props="default", extra="--request-payer requester"),
            expected_rc=1,
        )
        assert "AccessDenied" in result.stderr
        _assert_in_operations_called(
            calls,
            "DeleteObject",
            {"Bucket": TARGET_BUCKET, "Key": TARGET_KEY, "RequestPayer": "requester"},
        )

    def test_metadata_directive_disables_copy_props(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(), {}],
            copy_command(copy_props="default", extra="--metadata-directive REPLACE"),
        )
        _assert_in_operations_called(
            calls, "CopyObject", copy_object_request(MetadataDirective="REPLACE")
        )


class _StdinShim:
    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


class _StdoutShim:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()
        self._text: list[str] = []

    def write(self, text: str) -> int:
        self._text.append(text)
        return len(text)

    def flush(self) -> None:
        pass


class TestCPCommandNoOverwrite:
    """The aws-cli no-overwrite block of TestCPCommand."""

    def test_no_overwrite_flag_when_object_not_exists_on_target(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            ["cp", full_path, "s3://bucket", "--no-overwrite"],
        )
        assert len(calls) == 1
        assert calls[0].operation == "PutObject"
        assert calls[0].params["IfNoneMatch"] == "*"

    def test_no_overwrite_flag_when_object_exists_on_target(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [_client_error("PreconditionFailed", 412, "PutObject")],
            ["cp", full_path, "s3://bucket", "--no-overwrite"],
        )
        assert len(calls) == 1
        assert calls[0].operation == "PutObject"
        assert calls[0].params["IfNoneMatch"] == "*"

    def test_no_overwrite_flag_multipart_upload_when_object_not_exists_on_target(
        self, tmp_path: Any
    ) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_bytes(b"a" * 10 * MB)
        _, calls = _run_cmd(
            [{"UploadId": "foo"}, {"ETag": '"foo-1"'}, {"ETag": '"foo-2"'}, {}],
            ["cp", full_path, "s3://bucket", "--no-overwrite"],
        )
        assert _operations(calls) == [
            "CreateMultipartUpload",
            "UploadPart",
            "UploadPart",
            "CompleteMultipartUpload",
        ]
        assert calls[3].params["IfNoneMatch"] == "*"

    def test_no_overwrite_flag_multipart_upload_when_object_exists_on_target(
        self, tmp_path: Any
    ) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_bytes(b"a" * 10 * MB)
        # Checking for success as the file is skipped.
        _, calls = _run_cmd(
            [
                {"UploadId": "foo"},
                {"ETag": '"foo-1"'},
                {"ETag": '"foo-2"'},
                _client_error("PreconditionFailed", 412, "CompleteMultipartUpload"),
                {},  # AbortMultipartUpload
            ],
            ["cp", full_path, "s3://bucket", "--no-overwrite"],
        )
        assert _operations(calls) == [
            "CreateMultipartUpload",
            "UploadPart",
            "UploadPart",
            "CompleteMultipartUpload",
            "AbortMultipartUpload",
        ]
        assert calls[3].params["IfNoneMatch"] == "*"

    def test_no_overwrite_flag_on_copy_when_small_object_does_not_exist_on_target(
        self, tmp_path: Any
    ) -> None:
        _, calls = _run_cmd(
            [head_object_response(ContentLength=5), {}],
            ["cp", "s3://bucket1/key.txt", "s3://bucket/key.txt", "--no-overwrite"],
        )
        assert _operations(calls) == ["HeadObject", "CopyObject"]
        assert calls[1].params["IfNoneMatch"] == "*"

    def test_no_overwrite_flag_on_copy_when_small_object_exists_on_target(
        self, tmp_path: Any
    ) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(ContentLength=5),
                _client_error("PreconditionFailed", 412, "CopyObject"),
            ],
            ["cp", "s3://bucket1/key.txt", "s3://bucket/key.txt", "--no-overwrite"],
        )
        assert _operations(calls) == ["HeadObject", "CopyObject"]
        assert calls[1].params["IfNoneMatch"] == "*"

    def test_no_overwrite_flag_on_copy_when_large_object_exists_on_target(
        self, tmp_path: Any
    ) -> None:
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
            ["cp", "s3://bucket1/key.txt", "s3://bucket/key.txt", "--no-overwrite"],
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

    def test_no_overwrite_flag_on_copy_when_large_object_does_not_exist_on_target(
        self, tmp_path: Any
    ) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(ContentLength=10 * MB),
                get_object_tagging_response({}),
                create_mpu_response("foo"),
                upload_part_copy_response(),
                upload_part_copy_response(),
                {},
            ],
            ["cp", "s3://bucket1/key.txt", "s3://bucket/key1.txt", "--no-overwrite"],
        )
        assert _operations(calls) == [
            "HeadObject",
            "GetObjectTagging",
            "CreateMultipartUpload",
            "UploadPartCopy",
            "UploadPartCopy",
            "CompleteMultipartUpload",
        ]
        assert calls[5].params["IfNoneMatch"] == "*"

    def test_no_overwrite_flag_on_download_when_single_object_already_exists_at_target(
        self, tmp_path: Any
    ) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("existing content")
        _, calls = _run_cmd(
            [head_object_response()],
            ["cp", "s3://bucket/key.txt", full_path, "--no-overwrite"],
        )
        assert _operations(calls) == ["HeadObject"]
        assert (tmp_path / "foo.txt").read_text() == "existing content"

    def test_no_overwrite_flag_on_download_when_single_object_does_not_exist_at_target(
        self, tmp_path: Any
    ) -> None:
        full_path = str(tmp_path / "foo.txt")
        _, calls = _run_cmd(
            [head_object_response(), get_object_response()],
            ["cp", "s3://bucket/key.txt", full_path, "--no-overwrite"],
        )
        assert _operations(calls) == ["HeadObject", "GetObject"]


class TestStreamingCPCommand:
    def test_streaming_upload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", _StdinShim(b"foo\n"))
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            ["cp", "-", "s3://bucket/streaming.txt"],
        )
        assert len(calls) == 1
        assert calls[0].operation == "PutObject"
        assert calls[0].params == {
            "Bucket": "bucket",
            "Key": "streaming.txt",
            "ChecksumAlgorithm": "CRC32",
            "Body": mock.ANY,
        }

    def test_streaming_upload_with_expected_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", _StdinShim(b"foo\n"))
        _, calls = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            ["cp", "-", "s3://bucket/streaming.txt", "--expected-size", "4"],
        )
        assert len(calls) == 1
        assert calls[0].operation == "PutObject"
        assert calls[0].params == {
            "Bucket": "bucket",
            "Key": "streaming.txt",
            "ChecksumAlgorithm": "CRC32",
            "Body": mock.ANY,
        }

    def test_streaming_upload_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", _StdinShim(b"foo\n"))
        result, _ = _run_cmd(
            [_client_error("NoSuchBucket", 404, "PutObject")],
            ["cp", "-", "s3://bucket/streaming.txt"],
            expected_rc=1,
        )
        assert "An error occurred (NoSuchBucket) when calling the PutObject operation" in (
            result.stderr
        )

    def test_streaming_upload_when_stdin_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", None)
        result, _ = _run_cmd(
            [{"ETag": '"c8afdb36c52cf4727836669019e69222"'}],
            ["cp", "-", "s3://bucket/streaming.txt"],
            expected_rc=1,
        )
        assert "stdin is required for this operation, but is not available" in result.stderr

    def test_streaming_download(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from boto3_s3_cli import cli
        from tests.utils.recorder import make_recording_client

        client, calls = make_recording_client(
            [
                {"ContentLength": 4, "ETag": '"d3b07384d113edec49eaa6238ad5ff00"'},
                {
                    "ContentLength": 4,
                    "ETag": '"d3b07384d113edec49eaa6238ad5ff00"',
                    "Body": io.BytesIO(b"foo\n"),
                },
            ]
        )
        shim = _StdoutShim()
        monkeypatch.setattr("sys.stdout", shim)
        ctx = Context(client_factory=lambda _args: client, transfer_config=_SYNC_CONFIG)
        rc = cli.main(["cp", "s3://bucket/streaming.txt", "-"], ctx=ctx)
        assert rc == 0
        assert shim.buffer.getvalue() == b"foo\n"
        # Ensures no extra operations were called.
        assert _operations(calls) == ["HeadObject", "GetObject"]

    def test_streaming_download_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result, _ = _run_cmd(
            [_client_error("NoSuchBucket", 404, "HeadObject")],
            ["cp", "s3://bucket/streaming.txt", "-"],
            expected_rc=1,
        )
        assert "An error occurred (NoSuchBucket) when calling the HeadObject operation" in (
            result.stderr
        )

    def test_no_overwrite_cannot_be_used_with_streaming_download(self) -> None:
        result, _ = _run_cmd(
            [], ["cp", "s3://bucket/streaming.txt", "-", "--no-overwrite"], expected_rc=252
        )
        assert "--no-overwrite parameter is not supported for streaming downloads" in (
            result.stderr
        )


class TestCPCommandChecksums:
    @pytest.mark.parametrize(
        "algorithm",
        [
            "CRC32",
            "SHA1",
            "SHA256",
            "SHA512",
            "CRC32C",
            "CRC64NVME",
            "XXHASH3",
            "XXHASH64",
            "XXHASH128",
        ],
    )
    def test_upload_with_checksum_algorithm(self, tmp_path: Any, algorithm: str) -> None:
        # aws-cli test_upload_with_checksum_algorithm_* (the recorder sits
        # above botocore's checksum *calculation*, so even algorithms the
        # stock botocore cannot compute record their parameter here).
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_text("contents")
        _, calls = _run_cmd(
            [{}], ["cp", full_path, "s3://bucket/key.txt", "--checksum-algorithm", algorithm]
        )
        assert calls[0].operation == "PutObject"
        assert calls[0].params["ChecksumAlgorithm"] == algorithm

    def test_multipart_upload_with_checksum_algorithm_crc32(self, tmp_path: Any) -> None:
        full_path = str(tmp_path / "foo.txt")
        (tmp_path / "foo.txt").write_bytes(b"a" * 10 * MB)
        _, calls = _run_cmd(
            [
                {"UploadId": "foo"},
                {"ETag": "foo-e1", "ChecksumCRC32": "foo-1"},
                {"ETag": "foo-e2", "ChecksumCRC32": "foo-2"},
                {},
            ],
            ["cp", full_path, "s3://bucket/key2.txt", "--checksum-algorithm", "CRC32"],
        )
        assert len(calls) == 4, calls
        assert calls[0].operation == "CreateMultipartUpload"
        assert calls[0].params["ChecksumAlgorithm"] == "CRC32"
        assert calls[1].operation == "UploadPart"
        assert calls[1].params["ChecksumAlgorithm"] == "CRC32"
        assert calls[3].operation == "CompleteMultipartUpload"
        parts = calls[3].params["MultipartUpload"]["Parts"]
        assert {"ETag": "foo-e1", "ChecksumCRC32": "foo-1", "PartNumber": mock.ANY} in parts
        assert {"ETag": "foo-e2", "ChecksumCRC32": "foo-2", "PartNumber": mock.ANY} in parts

    def test_copy_with_checksum_algorithm_crc32(self, tmp_path: Any) -> None:
        _, calls = _run_cmd(
            [head_object_response(), {"ETag": "foo-1", "ChecksumCRC32": "Tq0H4g=="}],
            [
                "cp",
                "s3://bucket1/key.txt",
                "s3://bucket2/key.txt",
                "--checksum-algorithm",
                "CRC32",
            ],
        )
        assert calls[1].operation == "CopyObject"
        assert calls[1].params["ChecksumAlgorithm"] == "CRC32"

    @pytest.mark.parametrize("checksum_field", ["ChecksumCRC32", "ChecksumCRC32C"])
    def test_download_with_checksum_mode(self, tmp_path: Any, checksum_field: str) -> None:
        _, calls = _run_cmd(
            [
                head_object_response(),
                {"ETag": "foo-1", checksum_field: "Tq0H4g==", "Body": io.BytesIO(b"foo")},
            ],
            ["cp", "s3://bucket/foo", str(tmp_path), "--checksum-mode", "ENABLED"],
        )
        assert calls[0].operation == "HeadObject"
        assert calls[0].params["ChecksumMode"] == "ENABLED"
        assert calls[1].operation == "GetObject"
        assert calls[1].params["ChecksumMode"] == "ENABLED"


class TestCpRecursiveCaseConflict:
    """aws-cli TestCpRecursiveCaseConflict (inheriting TestSyncCaseConflict)."""

    LOWER_KEY = "a.txt"
    UPPER_KEY = "A.txt"

    def _cmd(self, tmp_path: Any, case_conflict: str | None = None) -> list[str]:
        argv = ["cp", "--recursive", "s3://bucket", str(tmp_path)]
        if case_conflict is not None:
            argv += ["--case-conflict", case_conflict]
        return argv

    def test_ignore_by_default(self, tmp_path: Any) -> None:
        (tmp_path / self.LOWER_KEY).write_text("mycontent")
        # Note there's no --case-conflict param.
        result, _ = _run_cmd(
            [list_objects_response([self.UPPER_KEY]), get_object_response()],
            self._cmd(tmp_path),
        )
        # Expect success (not error mode) and no warnings (not warn or skip).
        assert not result.stderr

    def test_error_with_existing_file(self, case_insensitive_workdir: Any) -> None:
        (case_insensitive_workdir / self.LOWER_KEY).write_text("mycontent")
        result, _ = _run_cmd(
            [list_objects_response([self.UPPER_KEY])],
            self._cmd(case_insensitive_workdir, "error"),
            expected_rc=1,
        )
        assert f"Failed to download bucket/{self.UPPER_KEY}" in result.stderr

    def test_error_with_case_conflicts_in_s3(self, tmp_path: Any) -> None:
        # The first (admitted) key still downloads; only the conflicting
        # second key trips the error gate - so one GetObject is scripted.
        result, _ = _run_cmd(
            [list_objects_response([self.UPPER_KEY, self.LOWER_KEY]), get_object_response()],
            self._cmd(tmp_path, "error"),
            expected_rc=1,
            transfer_config=_CASE_CONFLICT_CONFIG,
        )
        assert f"Failed to download bucket/{self.LOWER_KEY}" in result.stderr

    def test_warn_with_existing_file(self, case_insensitive_workdir: Any) -> None:
        (case_insensitive_workdir / self.LOWER_KEY).write_text("mycontent")
        result, _ = _run_cmd(
            [list_objects_response([self.UPPER_KEY]), get_object_response()],
            self._cmd(case_insensitive_workdir, "warn"),
        )
        assert f"warning: Downloading bucket/{self.UPPER_KEY}" in result.stderr

    def test_warn_with_case_conflicts_in_s3(self, tmp_path: Any) -> None:
        result, _ = _run_cmd(
            [
                list_objects_response([self.UPPER_KEY, self.LOWER_KEY]),
                get_object_response(),
                get_object_response(),
            ],
            self._cmd(tmp_path, "warn"),
            transfer_config=_CASE_CONFLICT_CONFIG,
        )
        assert f"warning: Downloading bucket/{self.LOWER_KEY}" in result.stderr

    def test_skip_with_existing_file(self, case_insensitive_workdir: Any) -> None:
        (case_insensitive_workdir / self.LOWER_KEY).write_text("mycontent")
        result, _ = _run_cmd(
            [list_objects_response([self.UPPER_KEY])], self._cmd(case_insensitive_workdir, "skip")
        )
        assert f"warning: Skipping bucket/{self.UPPER_KEY}" in result.stderr

    def test_skip_with_case_conflicts_in_s3(self, tmp_path: Any) -> None:
        result, _ = _run_cmd(
            [
                list_objects_response([self.UPPER_KEY, self.LOWER_KEY]),
                get_object_response(),
            ],
            self._cmd(tmp_path, "skip"),
            transfer_config=_CASE_CONFLICT_CONFIG,
        )
        assert f"warning: Skipping bucket/{self.LOWER_KEY}" in result.stderr

    def test_ignore_with_existing_file(self, tmp_path: Any) -> None:
        (tmp_path / self.LOWER_KEY).write_text("mycontent")
        _run_cmd(
            [list_objects_response([self.UPPER_KEY]), get_object_response()],
            self._cmd(tmp_path, "ignore"),
        )

    def test_ignore_with_case_conflicts_in_s3(self, tmp_path: Any) -> None:
        _run_cmd(
            [
                list_objects_response([self.UPPER_KEY, self.LOWER_KEY]),
                get_object_response(),
                get_object_response(),
            ],
            self._cmd(tmp_path, "ignore"),
        )


class TestS3ExpressCpRecursive:
    def test_s3_express_error_raises_exception(self, tmp_path: Any) -> None:
        result, _ = _run_cmd(
            [],
            [
                "cp",
                "--recursive",
                "s3://bucket--usw2-az1--x-s3",
                str(tmp_path),
                "--case-conflict",
                "error",
            ],
            expected_rc=252,
        )
        assert "`error` is not a valid value" in result.stderr

    def test_s3_express_skip_raises_exception(self, tmp_path: Any) -> None:
        result, _ = _run_cmd(
            [],
            [
                "cp",
                "--recursive",
                "s3://bucket--usw2-az1--x-s3",
                str(tmp_path),
                "--case-conflict",
                "skip",
            ],
            expected_rc=252,
        )
        assert "`skip` is not a valid value" in result.stderr

    def test_s3_express_warn_emits_warning(self, tmp_path: Any) -> None:
        result, _ = _run_cmd(
            [
                list_objects_response(["a.txt", "A.txt"]),
                get_object_response(),
                get_object_response(),
            ],
            [
                "cp",
                "--recursive",
                "s3://bucket--usw2-az1--x-s3",
                str(tmp_path),
                "--case-conflict",
                "warn",
            ],
        )
        assert "warning: Recursive copies/moves" in result.stderr
