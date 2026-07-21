"""``S3.mv``: the cp pipeline plus the onto-itself guard and source deletion.

Everything route-shaped is cp's (test_s3_cp.py pins it); these tests pin
what mv adds - the always-on same-path guard with aws's exact message
(rc 252 even with ``recursive``),
MOVE-kind reporting, and the per-item source deletion with its gates (no
delete on dryrun / filter / skip / warn / failure).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3 import GlobFilter
from boto3_s3.exceptions import BatchError, ValidationError
from boto3_s3.iostorage import IOStorage
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import OpOutcome, OpResult, TransferType
from tests.utils.fakes3 import MTIME, client_error, get_response, head_response
from tests.utils.recorder import make_recording_client, ops

_SYNC = TransferConfig(use_threads=False)


class TestStreams:
    """A stream is a valid single-object move destination, never a source.

    S3 -> stream rides the s3open route (write to the stream, then delete the
    S3 source). A stream source cannot satisfy the move contract (a move
    deletes its source), and a recursive stream destination would concatenate
    every object into one stream - both are rejected up front. The CLI rejects
    ``-`` for mv on either side outright (aws parity, tests/cli/unit/test_mv.py).
    """

    def test_stream_destination_downloads_then_deletes_source(self) -> None:

        buf = io.BytesIO()
        client, calls = make_recording_client([head_response(), get_response(), {}])
        results: list[OpResult] = []
        S3().mv(
            S3Storage("s3://b/d/a.txt", client=client),
            IOStorage(buf),
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert ops(calls) == ["HeadObject", "GetObject", "DeleteObject"]
        assert calls[2].params == {"Bucket": "b", "Key": "d/a.txt"}
        assert buf.getvalue() == b"payload"
        assert [(r.transfer_type, r.outcome) for r in results] == [
            (TransferType.MOVE, OpOutcome.SUCCEEDED)
        ]

    def test_stream_destination_dryrun_moves_nothing(self) -> None:

        buf = io.BytesIO()
        client, calls = make_recording_client([head_response()])
        results: list[OpResult] = []
        S3().mv(
            S3Storage("s3://b/d/a.txt", client=client),
            IOStorage(buf),
            dryrun=True,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        # dryrun still resolves the single source (HeadObject) but neither
        # reads bytes nor deletes the source, and never touches the stream.
        assert ops(calls) == ["HeadObject"]
        assert buf.getvalue() == b""
        assert [(r.transfer_type, r.outcome) for r in results] == [
            (TransferType.MOVE, OpOutcome.DRYRUN)
        ]

    def test_stream_source_rejected(self) -> None:
        with pytest.raises(ValidationError, match="mv does not support a stream source"):
            S3().mv(IOStorage(io.BytesIO(b"x")), S3Storage("s3://bucket/k"))

    def test_recursive_stream_destination_rejected(self) -> None:
        with pytest.raises(ValidationError, match="only for a single object"):
            S3().mv(S3Storage("s3://bucket/pre/"), IOStorage(io.BytesIO()), recursive=True)

    def test_stream_destination_no_overwrite_rejected(self) -> None:
        # cp's stream gate, ported: a streaming download has no existing
        # destination to guard, so no_overwrite must fail loud instead of
        # being silently ignored (and the source deleted anyway).
        client, calls = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().mv(
                S3Storage("s3://b/d/a.txt", client=client),
                IOStorage(io.BytesIO()),
                no_overwrite=True,
            )
        assert type(excinfo.value) is ValidationError
        assert str(excinfo.value) == "no_overwrite is not supported for streaming downloads"
        assert calls == []


class TestSamePathGuard:
    """aws-cli's ``Cannot mv a file onto itself`` - before any client work."""

    def _assert_guard(self, src: str, dest: str, message: str, **kwargs: Any) -> None:

        client, calls = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().mv(S3Storage(src, client=client), S3Storage(dest, client=client), **kwargs)
        assert str(excinfo.value) == message
        assert calls == []

    def test_exact_same_uri(self) -> None:
        self._assert_guard(
            "s3://b/k.txt",
            "s3://b/k.txt",
            "Cannot mv a file onto itself: s3://b/k.txt - s3://b/k.txt",
        )

    def test_implied_basename(self) -> None:
        self._assert_guard(
            "s3://b/d/a.txt",
            "s3://b/d/",
            "Cannot mv a file onto itself: s3://b/d/a.txt - s3://b/d/",
        )

    def test_keyless_destination_is_normalized_in_the_message(self) -> None:
        self._assert_guard(
            "s3://b/k.txt",
            "s3://b",
            "Cannot mv a file onto itself: s3://b/k.txt - s3://b/",
        )

    def test_recursive_is_not_exempt(self) -> None:
        # The aws-cli's faithful false positive: no key would map onto
        # itself, but aws rejects it anyway (rc 252).
        self._assert_guard(
            "s3://b/d",
            "s3://b/",
            "Cannot mv a file onto itself: s3://b/d - s3://b/",
            recursive=True,
        )

    def test_different_buckets_pass(self) -> None:

        client, calls = make_recording_client([head_response(), {}, {}])
        S3().mv(
            S3Storage("s3://b1/k.txt", client=client),
            S3Storage("s3://b2/k.txt", client=client),
            transfer_config=_SYNC,
        )
        assert ops(calls) == ["HeadObject", "CopyObject", "DeleteObject"]
        assert calls[2].params == {"Bucket": "b1", "Key": "k.txt"}


class TestUploadMove:
    def test_single_upload_deletes_the_local_source(self, tmp_path: Path) -> None:

        src = tmp_path / "a.txt"
        src.write_bytes(b"x" * 7)
        client, calls = make_recording_client([{}])
        results: list[OpResult] = []
        S3().mv(
            str(src),
            S3Storage("s3://bucket/up/", client=client),
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert ops(calls) == ["PutObject"]
        assert calls[0].params["Key"] == "up/a.txt"
        assert not src.exists()
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert all(result.transfer_type is TransferType.MOVE for result in results)

    def test_filtered_out_files_are_neither_moved_nor_deleted(self, tmp_path: Path) -> None:

        (tmp_path / "keep.txt").write_bytes(b"k")
        (tmp_path / "skip.log").write_bytes(b"s")
        client, calls = make_recording_client([{}])
        keep = GlobFilter().exclude("*.log").compile()
        S3().mv(
            str(tmp_path),
            S3Storage("s3://b/tree/", client=client),
            recursive=True,
            filter=keep,
            transfer_config=_SYNC,
        )
        assert ops(calls) == ["PutObject"]
        assert calls[0].params["Key"] == "tree/keep.txt"
        assert not (tmp_path / "keep.txt").exists()
        assert (tmp_path / "skip.log").exists()

    def test_dryrun_touches_nothing(self, tmp_path: Path) -> None:

        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        S3().mv(
            str(src),
            S3Storage("s3://b/k", client=client),
            dryrun=True,
            on_result=results.append,
        )
        assert calls == []
        assert src.exists()
        assert [result.outcome for result in results] == [OpOutcome.DRYRUN]
        assert results[0].transfer_type is TransferType.MOVE


class TestDownloadMove:
    def test_single_download_deletes_the_source_object(self, tmp_path: Path) -> None:

        client, calls = make_recording_client([head_response(), get_response(), {}])
        results: list[OpResult] = []
        S3().mv(
            S3Storage("s3://b/d/a.txt", client=client),
            str(tmp_path / "out.txt"),
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert ops(calls) == ["HeadObject", "GetObject", "DeleteObject"]
        assert calls[2].params == {"Bucket": "b", "Key": "d/a.txt"}
        assert (tmp_path / "out.txt").read_bytes() == b"payload"
        assert [result.transfer_type for result in results] == [TransferType.MOVE]

    def test_filtered_single_source_is_neither_moved_nor_deleted(self, tmp_path: Path) -> None:
        # aws filters the single-object routes too: an excluded mv source
        # must not transfer - and, crucially, must not be deleted.

        drop = GlobFilter().exclude("*").compile()
        client, calls = make_recording_client([head_response()])
        S3().mv(
            S3Storage("s3://b/d/a.txt", client=client),
            str(tmp_path / "out.txt"),
            filter=drop,
            transfer_config=_SYNC,
        )
        assert ops(calls) == ["HeadObject"]
        assert not (tmp_path / "out.txt").exists()

    def test_recursive_skips_markers_without_deleting_them(self, tmp_path: Path) -> None:
        # Folder markers never transfer (aws-cli filegenerator), so a move
        # never deletes them either - `aws s3 mv --recursive` leaves `pre/m/`
        # in the source bucket.

        listing = {
            "Contents": [
                {"Key": "pre/a.txt", "Size": 7, "LastModified": MTIME, "ETag": '"abc"'},
                {"Key": "pre/m/", "Size": 0, "LastModified": MTIME, "ETag": '"m"'},
            ]
        }
        client, calls = make_recording_client([listing, get_response(), {}])
        S3().mv(
            S3Storage("s3://b/pre", client=client),
            str(tmp_path / "out"),
            recursive=True,
            transfer_config=_SYNC,
        )
        assert ops(calls) == ["ListObjectsV2", "GetObject", "DeleteObject"]
        assert calls[2].params == {"Bucket": "b", "Key": "pre/a.txt"}

    def test_glacier_warning_keeps_the_route_wording_and_the_object(self, tmp_path: Path) -> None:

        client, calls = make_recording_client([head_response(StorageClass="GLACIER")])
        results: list[OpResult] = []
        S3().mv(
            S3Storage("s3://b/cold", client=client),
            str(tmp_path / "out.bin"),
            transfer_config=_SYNC,
            on_result=results.append,
        )
        # No GetObject, no DeleteObject; the message says "download" (the
        # aws-cli uses operation_name, not "move") but reports kind MOVE.
        assert ops(calls) == ["HeadObject"]
        assert [result.outcome for result in results] == [OpOutcome.WARNED]
        assert results[0].transfer_type is TransferType.MOVE
        assert "Unable to perform download operations on GLACIER objects." in str(results[0].error)

    def test_no_overwrite_existing_destination_skips_without_deleting(self, tmp_path: Path) -> None:

        target = tmp_path / "out.txt"
        target.write_bytes(b"already here")
        client, calls = make_recording_client([head_response()])
        results: list[OpResult] = []
        S3().mv(
            S3Storage("s3://b/k.txt", client=client),
            str(target),
            no_overwrite=True,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert ops(calls) == ["HeadObject"]
        assert target.read_bytes() == b"already here"
        assert [result.outcome for result in results] == [OpOutcome.SKIPPED]

    def test_delete_failure_aggregates_into_batch_error(self, tmp_path: Path) -> None:

        client, calls = make_recording_client(
            [
                head_response(),
                get_response(),
                client_error("AccessDenied", 403, "DeleteObject"),
            ]
        )
        results: list[OpResult] = []
        with pytest.raises(BatchError) as excinfo:
            S3().mv(
                S3Storage("s3://b/k.txt", client=client),
                str(tmp_path / "out.txt"),
                transfer_config=_SYNC,
                on_result=results.append,
            )
        assert ops(calls) == ["HeadObject", "GetObject", "DeleteObject"]
        assert (excinfo.value.succeeded, excinfo.value.failed) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        # The bytes still arrived (aws ditto): only the move failed.
        assert (tmp_path / "out.txt").read_bytes() == b"payload"


class TestCopyMove:
    def test_single_copy_deletes_on_the_source_client(self) -> None:

        dest_client, dest_calls = make_recording_client([{}])
        source_client, source_calls = make_recording_client([head_response(), {}])
        results: list[OpResult] = []
        S3().mv(
            S3Storage("s3://src-b/d/a.txt", client=source_client),
            S3Storage("s3://dest-b/moved/a.txt", client=dest_client),
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert ops(source_calls) == ["HeadObject", "DeleteObject"]
        assert source_calls[1].params == {"Bucket": "src-b", "Key": "d/a.txt"}
        assert ops(dest_calls) == ["CopyObject"]
        assert [result.transfer_type for result in results] == [TransferType.MOVE]

    def test_request_payer_reaches_the_delete(self) -> None:

        dest_client, _ = make_recording_client([{}])
        source_client, source_calls = make_recording_client([head_response(), {}])
        S3().mv(
            S3Storage("s3://src-b/k", client=source_client),
            S3Storage("s3://dest-b/k2", client=dest_client),
            request_payer="requester",
            transfer_config=_SYNC,
        )
        assert source_calls[1].operation == "DeleteObject"
        assert source_calls[1].params["RequestPayer"] == "requester"
