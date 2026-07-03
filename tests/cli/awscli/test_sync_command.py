"""Port of aws-cli's functional sync tests to ``boto3-s3 sync``.

Provenance: aws-cli's ``tests/functional/s3/test_sync_command.py``
(aws-cli 2.34.x). Test names, canned responses, and expected operations are
kept verbatim where possible so the file stays diffable against the aws-cli
original when aws-cli is updated.

Adaptation rules (on top of the cp/mv ports' - see their module docstrings):

- Sync deletions: the aws-cli issues one ``DeleteObject`` per key; ours
  batch through ``DeleteObjects`` (accepted wire-level deviation,
  docs/deleter.md section 4 - the rm port rule), so per-key expectations become
  one ``DeleteObjects`` carrying the keys with ``Quiet: True``.
- ``ListObjectsV2`` expectations gain our explicit ``MaxKeys: 1000``
  page-size default (rm port rule).
- The per-algorithm checksum matrices are parametrized instead of being
  nine near-identical methods (cp port rule).

Not ported, with reasons:

- ``TestSyncWithCRTClient`` (5 tests): the CRT data plane bypasses the botocore
  client, so the recording client cannot drive it; CRT parity is enforced by
  the e2e CRT lane instead (docs/crt.md, docs/testing.md).
- ``TestSyncSourceRegion``: a different aws-cli harness
  (``BaseS3CLIRunnerTest``, endpoint-level assertions); the
  ``--source-region`` client wiring is covered by the cp/mv unit tier
  (cp port precedent).
- The two s3s3 SSE-C *multipart* variants: cp port precedent (the
  parameter mapping is covered by ``tests/lib/test_requestparams.py``);
  the single-part pair is ported below.
"""

from __future__ import annotations

import datetime as dt
import io
import os
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3.localstorage import LocalStorage
from boto3_s3_cli.commands.base import Context
from tests.utils.harness import CliResult, run_cli_in_process
from tests.utils.recorder import ApiCall, make_recording_client

MB = 1024**2
_TIME_UTC = dt.datetime(2014, 1, 9, 20, 45, 49, tzinfo=dt.timezone.utc)
_SYNC_CONFIG = TransferConfig(use_threads=False)
# See test_cp_command._CASE_CONFLICT_CONFIG: the "two S3 twins" gate detects a
# conflict only while the first twin is still in flight, so it needs a threaded
# (non-blocking) submit running ahead of completions - aws-cli's own tests use a
# single worker (max_concurrent_requests = 1) here.
_CASE_CONFLICT_CONFIG = TransferConfig(max_concurrency=1)

_LIST_BASE = {"MaxKeys": 1000}


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


def get_object_tagging_response(tags: dict[str, str]) -> dict[str, Any]:
    return {"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]}


def create_mpu_response(upload_id: str) -> dict[str, Any]:
    return {"UploadId": upload_id}


def upload_part_copy_response() -> dict[str, Any]:
    return {"CopyPartResult": {"ETag": '"etag"'}}


class TestSyncCommand:
    def test_website_redirect_ignore_paramfile(self, tmp_path: Path) -> None:
        (tmp_path / "foo.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [
                {"CommonPrefixes": [], "Contents": []},
                {"ETag": '"c8afdb36c52cf4727836669019e69222"'},
            ],
            [
                "sync",
                str(tmp_path),
                "s3://bucket/key.txt",
                "--website-redirect",
                "http://someserver",
            ],
        )
        # The only operations we should have called are ListObjectsV2/PutObject.
        assert _operations(calls) == ["ListObjectsV2", "PutObject"]
        # Make sure that the specified web address is used as opposed to the
        # contents of the web address when uploading the object.
        assert calls[1].params["WebsiteRedirectLocation"] == "http://someserver"

    def test_no_recursive_option(self) -> None:
        # Return code will be 252 for invalid parameter ``--recursive``.
        _run_cmd([], ["sync", ".", "s3://mybucket", "--recursive"], expected_rc=252)

    def test_sync_from_non_existant_directory(self, tmp_path: Path) -> None:
        non_existant_directory = str(tmp_path / "fakedir")
        result, calls = _run_cmd(
            [{"CommonPrefixes": [], "Contents": []}],
            ["sync", non_existant_directory, "s3://bucket/"],
            expected_rc=255,
        )
        assert "does not exist" in result.stderr
        assert calls == []

    def test_sync_to_non_existant_directory(self, tmp_path: Path) -> None:
        key = "foo.txt"
        non_existant_directory = str(tmp_path / "fakedir")
        _run_cmd(
            [
                {
                    "CommonPrefixes": [],
                    "Contents": [
                        {
                            "Key": key,
                            "Size": 3,
                            "LastModified": _TIME_UTC,
                            "ETag": '"c8afdb36c52cf4727836669019e69222-"',
                        }
                    ],
                },
                {
                    "ETag": '"c8afdb36c52cf4727836669019e69222-"',
                    "Body": io.BytesIO(b"foo"),
                },
            ],
            ["sync", "s3://bucket/", non_existant_directory],
        )
        # Make sure the file now exists.
        assert os.path.exists(os.path.join(non_existant_directory, key))

    def test_dryrun_sync(self, tmp_path: Path) -> None:
        full_path = tmp_path / "file.txt"
        full_path.write_text("mycontent")
        result, calls = _run_cmd(
            [list_objects_response([])],
            ["sync", str(tmp_path), "s3://bucket/", "--dryrun"],
        )
        assert [(c.operation, c.params) for c in calls] == [
            ("ListObjectsV2", {"Bucket": "bucket", "Prefix": "", **_LIST_BASE}),
        ]
        assert (
            f"(dryrun) upload: {LocalStorage.relative_path(str(full_path))} to s3://bucket/file.txt"
            in (result.stdout)
        )

    def test_glacier_sync_with_force_glacier(self, tmp_path: Path) -> None:
        _, calls = _run_cmd(
            [
                {
                    "Contents": [
                        {
                            "Key": "foo/bar.txt",
                            "ContentLength": "100",
                            "LastModified": _TIME_UTC,
                            "StorageClass": "GLACIER",
                            "Size": 100,
                            "ETag": '"foo-1"',
                        },
                    ],
                    "CommonPrefixes": [],
                },
                {"ETag": '"foo-1"', "Body": io.BytesIO(b"foo")},
            ],
            ["sync", "s3://bucket/foo", str(tmp_path), "--force-glacier-transfer"],
        )
        assert _operations(calls) == ["ListObjectsV2", "GetObject"]

    def test_handles_glacier_incompatible_operations(self, tmp_path: Path) -> None:
        result, calls = _run_cmd(
            [
                {
                    "Contents": [
                        {
                            "Key": "foo",
                            "Size": 100,
                            "LastModified": _TIME_UTC,
                            "StorageClass": "GLACIER",
                        },
                        {
                            "Key": "bar",
                            "Size": 100,
                            "LastModified": _TIME_UTC,
                            "StorageClass": "DEEP_ARCHIVE",
                        },
                    ]
                }
            ],
            ["sync", "s3://bucket/", str(tmp_path)],
            expected_rc=2,
        )
        # There should not have been a download attempted because the
        # operation was skipped because it is glacier and glacier
        # deep archive incompatible.
        assert _operations(calls) == ["ListObjectsV2"]
        assert "GLACIER" in result.stderr
        assert "s3://bucket/foo" in result.stderr
        assert "s3://bucket/bar" in result.stderr

    def test_turn_off_glacier_warnings(self, tmp_path: Path) -> None:
        result, calls = _run_cmd(
            [
                {
                    "Contents": [
                        {
                            "Key": "foo",
                            "Size": 100,
                            "LastModified": _TIME_UTC,
                            "StorageClass": "GLACIER",
                        },
                        {
                            "Key": "bar",
                            "Size": 100,
                            "LastModified": _TIME_UTC,
                            "StorageClass": "DEEP_ARCHIVE",
                        },
                    ]
                }
            ],
            ["sync", "s3://bucket/", str(tmp_path), "--ignore-glacier-warnings"],
        )
        assert _operations(calls) == ["ListObjectsV2"]
        assert result.stderr == ""

    def test_warning_on_invalid_timestamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "foo.txt").write_text("mycontent")

        # Patch the stat reader to return a value indicating that an invalid
        # timestamp was loaded (impossible to set on all OSes; aws-cli patches
        # get_file_stat the same way).
        def invalid_stat(_path: str) -> tuple[None, None]:
            return (None, None)

        monkeypatch.setattr("boto3_s3.localstorage._file_stat", invalid_stat)
        _, calls = _run_cmd(
            [
                {"CommonPrefixes": [], "Contents": []},
                {"ETag": '"c8afdb36c52cf4727836669019e69222"'},
            ],
            ["sync", str(tmp_path), "s3://bucket/key.txt"],
            expected_rc=2,
        )
        # We should still have put the object.
        assert _operations(calls) == ["ListObjectsV2", "PutObject"]

    def test_sync_with_delete_on_downloads(self, tmp_path: Path) -> None:
        full_path = tmp_path / "foo.txt"
        full_path.write_text("mycontent")
        _, calls = _run_cmd(
            [
                {"CommonPrefixes": [], "Contents": []},
                {"ETag": '"c8afdb36c52cf4727836669019e69222"'},
            ],
            ["sync", "s3://bucket", str(tmp_path), "--delete"],
        )
        # The only operations we should have called are ListObjectsV2 (the
        # delete is a local os.remove).
        assert _operations(calls) == ["ListObjectsV2"]
        assert not full_path.exists()

    # When a file has been deleted after listing, the stat may raise either
    # an OSError or a ValueError depending on the environment; both skip the
    # file with a warning.
    @pytest.mark.parametrize("error", [ValueError, OSError])
    def test_sync_skips_over_files_deleted_between_listing_and_transfer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
    ) -> None:
        full_path = tmp_path / "foo.txt"
        full_path.write_text("mycontent")

        def side_effect(_path: str) -> tuple[int, Any]:
            os.remove(full_path)
            raise error()

        monkeypatch.setattr("boto3_s3.localstorage._file_stat", side_effect)
        _, calls = _run_cmd(
            [{"CommonPrefixes": [], "Contents": []}],
            ["sync", str(tmp_path), "s3://bucket/"],
            expected_rc=2,
        )
        # We should not call PutObject because the file was deleted before we
        # could transfer it.
        assert _operations(calls) == ["ListObjectsV2"]

    def test_request_payer(self) -> None:
        _, calls = _run_cmd(
            [
                # Response for ListObjects on source bucket.
                list_objects_response(["mykey"]),
                # Response for ListObjects on destination bucket.
                list_objects_response([]),
                {},
            ],
            ["sync", "s3://sourcebucket/", "s3://mybucket", "--request-payer"],
        )
        assert [(c.operation, c.params) for c in calls] == [
            (
                "ListObjectsV2",
                {"Bucket": "sourcebucket", "Prefix": "", "RequestPayer": "requester", **_LIST_BASE},
            ),
            (
                "ListObjectsV2",
                {"Bucket": "mybucket", "Prefix": "", "RequestPayer": "requester", **_LIST_BASE},
            ),
            (
                "CopyObject",
                {
                    "CopySource": {"Bucket": "sourcebucket", "Key": "mykey"},
                    "Bucket": "mybucket",
                    "Key": "mykey",
                    "RequestPayer": "requester",
                },
            ),
        ]

    def test_s3s3_sync_with_destination_sse_c(self) -> None:
        _, calls = _run_cmd(
            [list_objects_response(["mykey"]), list_objects_response([]), {}],
            [
                "sync",
                "s3://sourcebucket/",
                "s3://mybucket",
                "--sse-c",
                "AES256",
                "--sse-c-key",
                "destination-key",
            ],
        )
        assert _operations(calls) == ["ListObjectsV2", "ListObjectsV2", "CopyObject"]
        assert calls[2].params == {
            "CopySource": {"Bucket": "sourcebucket", "Key": "mykey"},
            "Bucket": "mybucket",
            "Key": "mykey",
            "SSECustomerAlgorithm": "AES256",
            "SSECustomerKey": "destination-key",
        }

    def test_s3s3_sync_with_different_sse_c_keys(self) -> None:
        _, calls = _run_cmd(
            [list_objects_response(["mykey"]), list_objects_response([]), {}],
            [
                "sync",
                "s3://sourcebucket/",
                "s3://mybucket",
                "--sse-c-copy-source",
                "AES256",
                "--sse-c-copy-source-key",
                "source-key",
                "--sse-c",
                "AES256",
                "--sse-c-key",
                "destination-key",
            ],
        )
        assert _operations(calls) == ["ListObjectsV2", "ListObjectsV2", "CopyObject"]
        assert calls[2].params == {
            "CopySource": {"Bucket": "sourcebucket", "Key": "mykey"},
            "Bucket": "mybucket",
            "Key": "mykey",
            "SSECustomerAlgorithm": "AES256",
            "SSECustomerKey": "destination-key",
            "CopySourceSSECustomerAlgorithm": "AES256",
            "CopySourceSSECustomerKey": "source-key",
        }

    def test_request_payer_with_deletes(self) -> None:
        _, calls = _run_cmd(
            [
                # Response for ListObjects on source bucket.
                list_objects_response([]),
                # Response for ListObjects on destination bucket.
                list_objects_response(["key-to-delete"]),
                {},
            ],
            ["sync", "s3://sourcebucket/", "s3://mybucket", "--request-payer", "--delete"],
        )
        # aws-cli expectation adapted: one batched DeleteObjects instead of a
        # per-key DeleteObject (module docstring).
        assert [(c.operation, c.params) for c in calls] == [
            (
                "ListObjectsV2",
                {"Bucket": "sourcebucket", "Prefix": "", "RequestPayer": "requester", **_LIST_BASE},
            ),
            (
                "ListObjectsV2",
                {"Bucket": "mybucket", "Prefix": "", "RequestPayer": "requester", **_LIST_BASE},
            ),
            (
                "DeleteObjects",
                {
                    "Bucket": "mybucket",
                    "Delete": {"Objects": [{"Key": "key-to-delete"}], "Quiet": True},
                    "RequestPayer": "requester",
                },
            ),
        ]

    def test_absolute_exclude_does_not_protect_the_destination_from_delete(
        self, tmp_path: Path
    ) -> None:
        # An absolute --exclude anchors at the local source, so it hides the
        # source's keep/a.txt but NOT the anchorless S3 destination key. The dest
        # key is therefore a dest-only orphan and --delete removes it - matching
        # aws-cli, which roots the pattern per side (src_rootdir vs dst_rootdir).
        src = tmp_path / "src"
        (src / "keep").mkdir(parents=True)
        (src / "keep" / "a.txt").write_text("x")
        _, calls = _run_cmd(
            [list_objects_response(["keep/a.txt"]), {}],
            ["sync", str(src), "s3://bucket", "--delete", "--exclude", f"{src}/keep/*"],
        )
        assert _operations(calls) == ["ListObjectsV2", "DeleteObjects"]  # source not uploaded
        delete = next(c for c in calls if c.operation == "DeleteObjects")
        assert delete.params["Delete"]["Objects"] == [{"Key": "keep/a.txt"}]

    def test_relative_exclude_protects_the_destination_from_delete(self, tmp_path: Path) -> None:
        # A relative --exclude matches each side's compare_key, so it hides
        # keep/a.txt on BOTH sides; the dest is invisible and --delete leaves it
        # (aws-cli "files excluded by filters are excluded from deletion").
        src = tmp_path / "src"
        (src / "keep").mkdir(parents=True)
        (src / "keep" / "a.txt").write_text("x")
        _, calls = _run_cmd(
            [list_objects_response(["keep/a.txt"])],
            ["sync", str(src), "s3://bucket", "--delete", "--exclude", "keep/*"],
        )
        assert _operations(calls) == ["ListObjectsV2"]  # no DeleteObjects

    def test_with_accesspoint_arn(self, tmp_path: Path) -> None:
        accesspoint_arn = "arn:aws:s3:us-west-2:123456789012:accesspoint/endpoint"
        _, calls = _run_cmd(
            [list_objects_response(["mykey"]), get_object_response()],
            ["sync", f"s3://{accesspoint_arn}", str(tmp_path)],
        )
        assert _operations(calls) == ["ListObjectsV2", "GetObject"]
        assert calls[0].params["Bucket"] == accesspoint_arn
        assert calls[1].params == {"Bucket": accesspoint_arn, "Key": "mykey"}

    def test_with_copy_props(self) -> None:
        upload_id = "upload_id"
        large_tag_set = {"tag-key": "val" * 3000}
        metadata = {"tag-key": "tag-value"}
        _, calls = _run_cmd(
            [
                list_objects_response(["key"], Size=8 * MB),
                list_objects_response([]),
                head_object_response(Metadata=metadata, ContentLength=8 * MB),
                get_object_tagging_response(large_tag_set),
                create_mpu_response(upload_id),
                upload_part_copy_response(),
                {},  # CompleteMultipartUpload
                {},  # PutObjectTagging
            ],
            ["sync", "s3://sourcebucket/", "s3://bucket/", "--copy-props", "default"],
        )
        assert _operations(calls) == [
            "ListObjectsV2",
            "ListObjectsV2",
            "HeadObject",
            "GetObjectTagging",
            "CreateMultipartUpload",
            "UploadPartCopy",
            "CompleteMultipartUpload",
            "PutObjectTagging",
        ]
        assert calls[2].params == {"Bucket": "sourcebucket", "Key": "key"}
        assert calls[4].params["Metadata"] == metadata
        assert calls[5].params["CopySourceIfMatch"] == '"foo-1"'
        assert calls[5].params["PartNumber"] == 1
        assert calls[7].params["Tagging"] == {"TagSet": [{"Key": "tag-key", "Value": "val" * 3000}]}

    @pytest.mark.parametrize(
        "algorithm",
        [
            "SHA1",
            "SHA256",
            "SHA512",
            "CRC32",
            "CRC32C",
            "CRC64NVME",
            "XXHASH3",
            "XXHASH64",
            "XXHASH128",
        ],
    )
    def test_upload_with_checksum_algorithm(self, tmp_path: Path, algorithm: str) -> None:
        (tmp_path / "foo.txt").write_text("contents")
        _, calls = _run_cmd(
            [list_objects_response([]), {}],
            ["sync", str(tmp_path), "s3://bucket/", "--checksum-algorithm", algorithm],
        )
        assert calls[1].operation == "PutObject"
        assert calls[1].params["ChecksumAlgorithm"] == algorithm

    def test_copy_with_checksum_algorithm_update_sha1(self) -> None:
        _, calls = _run_cmd(
            [
                # Response for ListObjects on source bucket.
                {
                    "Contents": [
                        {
                            "Key": "mykey",
                            "LastModified": _TIME_UTC,
                            "Size": 100,
                            "ChecksumAlgorithm": "SHA1",
                            "ETag": "myetag",
                        }
                    ],
                    "CommonPrefixes": [],
                },
                # Response for ListObjects on destination bucket.
                list_objects_response([]),
                # Response for CopyObject.
                {"ChecksumSHA1": "sha1-checksum"},
            ],
            ["sync", "s3://src-bucket/", "s3://dest-bucket/", "--checksum-algorithm", "SHA1"],
        )
        assert _operations(calls) == ["ListObjectsV2", "ListObjectsV2", "CopyObject"]
        assert calls[2].params == {
            "CopySource": {"Bucket": "src-bucket", "Key": "mykey"},
            "Bucket": "dest-bucket",
            "Key": "mykey",
            "ChecksumAlgorithm": "SHA1",
        }

    @pytest.mark.parametrize(
        "checksum_field",
        [
            "ChecksumSHA1",
            "ChecksumSHA256",
            "ChecksumSHA512",
            "ChecksumCRC32",
            "ChecksumCRC32C",
            "ChecksumCRC64NVME",
            "ChecksumXXHASH3",
            "ChecksumXXHASH64",
            "ChecksumXXHASH128",
        ],
    )
    def test_download_with_checksum_mode(self, tmp_path: Path, checksum_field: str) -> None:
        _, calls = _run_cmd(
            [
                list_objects_response(["bucket"]),
                # Mocked GetObject response with a checksum algorithm specified.
                {"ETag": "foo-1", checksum_field: "checksum", "Body": io.BytesIO(b"foo")},
            ],
            ["sync", "s3://bucket/foo", str(tmp_path), "--checksum-mode", "ENABLED"],
        )
        assert _operations(calls) == ["ListObjectsV2", "GetObject"]
        assert calls[1].params["ChecksumMode"] == "ENABLED"

    def test_sync_upload_with_no_overwrite_when_file_does_not_exist_at_destination(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "new_file.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [
                list_objects_response(["file.txt"]),
                {"ETag": '"c8afdb36c52cf4727836669019e69222"'},
            ],
            ["sync", str(tmp_path), "s3://bucket", "--no-overwrite"],
        )
        assert _operations(calls) == ["ListObjectsV2", "PutObject"]
        assert calls[1].params["Key"] == "new_file.txt"
        # Sync's --no-overwrite lives wholly in the pair judgment (aws-cli
        # pops the handler param): no conditional-write header on the wire.
        assert "IfNoneMatch" not in calls[1].params

    def test_sync_upload_with_no_overwrite_when_file_exists_at_destination(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "new_file.txt").write_text("mycontent")
        _, calls = _run_cmd(
            [list_objects_response(["new_file.txt"])],
            ["sync", str(tmp_path), "s3://bucket", "--no-overwrite"],
        )
        assert _operations(calls) == ["ListObjectsV2"]

    def test_sync_download_with_no_overwrite_file_not_exists_at_destination(
        self, tmp_path: Path
    ) -> None:
        _, calls = _run_cmd(
            [list_objects_response(["new_file.txt"]), get_object_response()],
            ["sync", "s3://bucket/", str(tmp_path), "--no-overwrite"],
        )
        assert _operations(calls) == ["ListObjectsV2", "GetObject"]
        assert os.path.exists(os.path.join(str(tmp_path), "new_file.txt"))

    def test_sync_download_with_no_overwrite_file_exists_at_destination(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "file.txt").write_text("My content")
        _, calls = _run_cmd(
            [list_objects_response(["file.txt"])],
            ["sync", "s3://bucket/", str(tmp_path), "--no-overwrite"],
        )
        assert _operations(calls) == ["ListObjectsV2"]

    def test_sync_copy_with_no_overwrite_file_not_exists_at_destination(self) -> None:
        _, calls = _run_cmd(
            [
                list_objects_response(["new_file.txt"]),
                list_objects_response(["file1.txt"]),
                {},
            ],
            ["sync", "s3://bucket/", "s3://bucket2/", "--no-overwrite"],
        )
        assert _operations(calls) == ["ListObjectsV2", "ListObjectsV2", "CopyObject"]
        assert calls[2].params["Key"] == "new_file.txt"

    def test_sync_copy_with_no_overwrite_file_exists_at_destination(self) -> None:
        _, calls = _run_cmd(
            [
                list_objects_response(["new_file.txt"]),
                list_objects_response(["new_file.txt", "file1.txt"]),
            ],
            ["sync", "s3://bucket/", "s3://bucket2/", "--no-overwrite"],
        )
        assert _operations(calls) == ["ListObjectsV2", "ListObjectsV2"]


class TestSyncCommandWithS3Express:
    def test_incompatible_with_sync_upload(self, tmp_path: Path) -> None:
        result, _ = _run_cmd(
            [],
            ["sync", str(tmp_path), "s3://testdirectorybucket--usw2-az1--x-s3/"],
            expected_rc=252,
        )
        assert "Cannot use sync command with a directory bucket." in result.stderr

    def test_incompatible_with_sync_download(self, tmp_path: Path) -> None:
        result, _ = _run_cmd(
            [],
            ["sync", "s3://testdirectorybucket--usw2-az1--x-s3/", str(tmp_path)],
            expected_rc=252,
        )
        assert "Cannot use sync command with a directory bucket." in result.stderr

    def test_incompatible_with_sync_copy(self) -> None:
        result, _ = _run_cmd(
            [],
            ["sync", "s3://bucket/", "s3://testdirectorybucket--usw2-az1--x-s3/"],
            expected_rc=252,
        )
        assert "Cannot use sync command with a directory bucket." in result.stderr

    def test_incompatible_with_sync_with_delete(self) -> None:
        result, _ = _run_cmd(
            [],
            ["sync", "s3://bucket/", "s3://testdirectorybucket--usw2-az1--x-s3/", "--delete"],
            expected_rc=252,
        )
        assert "Cannot use sync command with a directory bucket." in result.stderr

    def test_compatible_with_sync_with_local_directory_like_directory_bucket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        _, calls = _run_cmd(
            [{"Contents": []}],
            ["sync", "s3://bucket/", "testdirectorybucket--usw2-az1--x-s3/"],
        )
        # Just asserting that the command validated and made an API call.
        assert _operations(calls) == ["ListObjectsV2"]


class TestSyncCaseConflict:
    lower_key = "a.txt"
    upper_key = "A.txt"

    def test_error_with_existing_file(self, case_insensitive_workdir: Path) -> None:
        (case_insensitive_workdir / self.lower_key).write_text("mycontent")
        result, _ = _run_cmd(
            [list_objects_response([self.upper_key])],
            ["sync", "s3://bucket", str(case_insensitive_workdir), "--case-conflict", "error"],
            expected_rc=1,
        )
        assert f"Failed to download bucket/{self.upper_key}" in result.stderr

    def test_error_with_case_conflicts_in_s3(self, tmp_path: Path) -> None:
        result, _ = _run_cmd(
            [
                list_objects_response([self.upper_key, self.lower_key]),
                get_object_response(),
            ],
            ["sync", "s3://bucket", str(tmp_path), "--case-conflict", "error"],
            expected_rc=1,
            transfer_config=_CASE_CONFLICT_CONFIG,
        )
        assert f"Failed to download bucket/{self.lower_key}" in result.stderr

    def test_warn_with_existing_file(self, case_insensitive_workdir: Path) -> None:
        (case_insensitive_workdir / self.lower_key).write_text("mycontent")
        result, _ = _run_cmd(
            [list_objects_response([self.upper_key]), get_object_response()],
            ["sync", "s3://bucket", str(case_insensitive_workdir), "--case-conflict", "warn"],
        )
        assert f"warning: Downloading bucket/{self.upper_key}" in result.stderr

    def test_warn_with_case_conflicts_in_s3(self, tmp_path: Path) -> None:
        result, _ = _run_cmd(
            [
                list_objects_response([self.upper_key, self.lower_key]),
                get_object_response(),
                get_object_response(),
            ],
            ["sync", "s3://bucket", str(tmp_path), "--case-conflict", "warn"],
            transfer_config=_CASE_CONFLICT_CONFIG,
        )
        assert f"warning: Downloading bucket/{self.lower_key}" in result.stderr

    def test_skip_with_existing_file(self, case_insensitive_workdir: Path) -> None:
        (case_insensitive_workdir / self.lower_key).write_text("mycontent")
        result, _ = _run_cmd(
            [list_objects_response([self.upper_key])],
            ["sync", "s3://bucket", str(case_insensitive_workdir), "--case-conflict", "skip"],
        )
        assert f"warning: Skipping bucket/{self.upper_key}" in result.stderr

    def test_skip_with_case_conflicts_in_s3(self, tmp_path: Path) -> None:
        result, calls = _run_cmd(
            [
                list_objects_response([self.upper_key, self.lower_key]),
                get_object_response(),
            ],
            ["sync", "s3://bucket", str(tmp_path), "--case-conflict", "skip"],
            transfer_config=_CASE_CONFLICT_CONFIG,
        )
        assert f"warning: Skipping bucket/{self.lower_key}" in result.stderr
        assert _operations(calls) == ["ListObjectsV2", "GetObject"]

    def test_ignore_with_existing_file(self, tmp_path: Path) -> None:
        (tmp_path / self.lower_key).write_text("mycontent")
        _run_cmd(
            [list_objects_response([self.upper_key]), get_object_response()],
            ["sync", "s3://bucket", str(tmp_path), "--case-conflict", "ignore"],
        )

    def test_ignore_with_case_conflicts_in_s3(self, tmp_path: Path) -> None:
        _, calls = _run_cmd(
            [
                list_objects_response([self.upper_key, self.lower_key]),
                get_object_response(),
                get_object_response(),
            ],
            ["sync", "s3://bucket", str(tmp_path), "--case-conflict", "ignore"],
        )
        assert _operations(calls) == ["ListObjectsV2", "GetObject", "GetObject"]
