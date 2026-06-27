"""``S3.cp``: route dispatch, naming, gates, and aggregation (recording client).

Behavioral parity pins (aws-cli refs in the implementation): the pre-batch
missing-source error uses aws's wording
and the *base* category (their bare RuntimeError -> rc 255), the single S3
source resolves via HeadObject whose 404 is rewritten to ``Key "..." does
not exist`` (in-pipeline -> rc 1), folder markers and parent-directory
escapes are dropped from downloads, the glacier gate warns/skips/forces, and
item failures aggregate into ``BatchError``.
"""

from __future__ import annotations

import gzip
import io
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from boto3_s3 import GlobFilter
from boto3_s3.exceptions import (
    BatchError,
    Boto3S3Error,
    CancelledError,
    NotFoundError,
    ValidationError,
)
from boto3_s3.iostorage import IOStorage
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import (
    CancelToken,
    CaseConflictMode,
    FileInfo,
    OpOutcome,
    OpResult,
    TransferOptions,
)
from tests.utils.recorder import ApiCall, make_recording_client

_SYNC = TransferConfig(use_threads=False)
_MTIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _client_error(code: str, status: int, operation: str) -> ClientError:
    response: Any = {
        "Error": {"Code": code, "Message": "stub"},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return ClientError(response, operation)


def _ops(calls: list[ApiCall]) -> list[str]:
    return [call.operation for call in calls]


def _head_response(**extra: Any) -> dict[str, Any]:
    return {"ContentLength": 7, "LastModified": _MTIME, "ETag": '"abc"', **extra}


def _get_response(body: bytes = b"payload") -> dict[str, Any]:
    import io

    return {"Body": io.BytesIO(body), "ContentLength": len(body), "ETag": '"abc"'}


class TestUploadRoute:
    def test_single_upload_names_the_key_from_the_prefix(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x" * 7)
        client, calls = make_recording_client([{}])
        results: list[OpResult] = []
        S3().cp(
            str(src),
            S3Storage("s3://bucket/up/", client=client),
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert _ops(calls) == ["PutObject"]
        assert calls[0].params["Bucket"] == "bucket"
        assert calls[0].params["Key"] == "up/a.txt"
        assert results[0].outcome is OpOutcome.SUCCEEDED
        assert results[0].src == str(src)
        assert results[0].dest == "s3://bucket/up/a.txt"

    def test_recursive_upload_walks_in_byte_order(self, tmp_path: Path) -> None:
        for name in ("a/inner.txt", "a.txt"):
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"x")
        client, calls = make_recording_client([{}, {}])
        S3().cp(
            str(tmp_path),
            S3Storage("s3://b/tree", client=client),
            recursive=True,
            transfer_config=_SYNC,
        )
        assert [call.params["Key"] for call in calls] == ["tree/a.txt", "tree/a/inner.txt"]

    def test_detect_symlink_loops_reaches_the_local_walk(self, tmp_path: Path) -> None:
        # The opt-in cycle guard (default off = aws parity) flows from cp's
        # detect_symlink_loops flag through to the local recursive walk.
        (tmp_path / "a.txt").write_bytes(b"x")
        (tmp_path / "loop").symlink_to(tmp_path)  # a directory cycle
        client, calls = make_recording_client([{}])  # one PutObject for a.txt
        results: list[OpResult] = []
        S3().cp(
            str(tmp_path),
            S3Storage("s3://b/t", client=client),
            recursive=True,
            detect_symlink_loops=True,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert [call.params["Key"] for call in calls] == ["t/a.txt"]  # loop skipped, no crash
        assert any(
            r.outcome is OpOutcome.WARNED and "Symbolic link loop detected" in str(r.error)
            for r in results
        )

    def test_missing_source_raises_the_base_category_up_front(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "nope.txt")
        client, calls = make_recording_client([])
        with pytest.raises(Boto3S3Error) as excinfo:
            S3().cp(missing, S3Storage("s3://b/k", client=client))
        # The aws-cli raises a bare RuntimeError here (rc 255), so the exact
        # base category matters: NotFoundError would map to a different rc.
        assert type(excinfo.value) is Boto3S3Error
        assert str(excinfo.value) == f"The user-provided path {missing} does not exist."
        assert calls == []

    def test_unreadable_single_source_warns_without_failing(self, tmp_path: Path) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root reads anything")
        src = tmp_path / "secret.txt"
        src.write_bytes(b"x")
        src.chmod(0)
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        try:
            S3().cp(
                str(src),
                S3Storage("s3://b/k", client=client),
                transfer_config=_SYNC,
                on_result=results.append,
            )
        finally:
            src.chmod(0o644)
        assert calls == []
        assert [result.outcome for result in results] == [OpOutcome.WARNED]
        assert f"Skipping file {src}. File/Directory is not readable." == str(results[0].error)

    def test_directory_single_source_fails_is_a_directory(self, tmp_path: Path) -> None:
        # A non-recursive cp of a directory fails like aws-cli with [Errno 21]
        # Is a directory, before any PutObject - the engine detects the directory
        # rather than letting botocore's default checksum wrapper mask the read
        # failure as an opaque rewind error.
        src = tmp_path / "adir"
        src.mkdir()
        (src / "a.txt").write_bytes(b"x")
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        with pytest.raises(BatchError) as excinfo:
            S3().cp(
                str(src),
                S3Storage("s3://b/k", client=client),
                transfer_config=_SYNC,
                on_result=results.append,
            )
        assert calls == []
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert "Is a directory" in str(results[0].error)
        assert (excinfo.value.succeeded, excinfo.value.failed) == (0, 1)

    def test_local_to_local_is_rejected(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        with pytest.raises(ValidationError):
            S3().cp(str(src), str(tmp_path / "b.txt"))

    def test_item_failures_aggregate_into_batch_error(self, tmp_path: Path) -> None:
        for name in ("a.txt", "b.txt"):
            (tmp_path / name).write_bytes(b"x")
        client, _ = make_recording_client([_client_error("NoSuchBucket", 404, "PutObject"), {}])
        with pytest.raises(BatchError) as excinfo:
            S3().cp(
                str(tmp_path),
                S3Storage("s3://b-missing/t", client=client),
                recursive=True,
                transfer_config=_SYNC,
            )
        error = excinfo.value
        assert str(error) == "1 of 2 transfers failed"
        assert (error.succeeded, error.failed) == (1, 1)
        assert isinstance(error.__cause__, NotFoundError)

    def test_cancel_token_stops_before_submission(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        token = CancelToken()
        token.cancel()
        client, calls = make_recording_client([])
        with pytest.raises(CancelledError):
            S3().cp(str(src), S3Storage("s3://b/k", client=client), cancel_token=token)
        assert calls == []


class TestFilters:
    def test_glob_filter_is_fed_compare_keys(self, tmp_path: Path) -> None:
        for name in ("keep.txt", "drop.bin"):
            (tmp_path / name).write_bytes(b"x")
        keep = GlobFilter().exclude("*").include("*.txt").compile()
        client, calls = make_recording_client([{}])
        S3().cp(
            str(tmp_path),
            S3Storage("s3://b/t", client=client),
            recursive=True,
            filter=keep,
            transfer_config=_SYNC,
        )
        assert [call.params["Key"] for call in calls] == ["t/keep.txt"]

    def test_predicate_is_fed_file_infos(self, tmp_path: Path) -> None:
        (tmp_path / "small.txt").write_bytes(b"x")
        (tmp_path / "large.txt").write_bytes(b"x" * 10)

        def keep(info: FileInfo) -> bool:
            return (info.size or 0) > 5

        client, calls = make_recording_client([{}])
        S3().cp(
            str(tmp_path),
            S3Storage("s3://b/t", client=client),
            recursive=True,
            filter=keep,
            transfer_config=_SYNC,
        )
        assert [call.params["Key"] for call in calls] == ["t/large.txt"]


class TestDownloadRoute:
    def test_single_download_heads_then_gets(self, tmp_path: Path) -> None:
        client, calls = make_recording_client([_head_response(), _get_response()])
        dest = tmp_path / "out.bin"
        S3().cp(S3Storage("s3://b/d/a.txt", client=client), str(dest), transfer_config=_SYNC)
        assert _ops(calls) == ["HeadObject", "GetObject"]
        assert calls[0].params == {"Bucket": "b", "Key": "d/a.txt"}
        assert dest.read_bytes() == b"payload"
        assert os.stat(dest).st_mtime == _MTIME.timestamp()

    def test_missing_single_source_uses_the_rewritten_404_message(self, tmp_path: Path) -> None:
        client, _ = make_recording_client([_client_error("404", 404, "HeadObject")])
        with pytest.raises(NotFoundError) as excinfo:
            S3().cp(S3Storage("s3://b/no-such", client=client), str(tmp_path / "x"))
        assert str(excinfo.value) == (
            "An error occurred (404) when calling the HeadObject operation: "
            'Key "no-such" does not exist'
        )

    def test_dryrun_heads_but_never_gets(self, tmp_path: Path) -> None:
        client, calls = make_recording_client([_head_response()])
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/d/a.txt", client=client),
            str(tmp_path / "x"),
            dryrun=True,
            on_result=results.append,
        )
        assert _ops(calls) == ["HeadObject"]
        assert [result.outcome for result in results] == [OpOutcome.DRYRUN]

    def test_keyless_non_recursive_source_transfers_nothing(self, tmp_path: Path) -> None:
        # `cp s3://bucket .`: aws lists the bucket and exact-matches nothing
        # (rc 0, silent); same zero-item outcome here.
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        S3().cp(S3Storage("s3://bucket", client=client), str(tmp_path), on_result=results.append)
        assert calls == []
        assert results == []

    def test_recursive_download_drops_markers_and_parent_escapes(self, tmp_path: Path) -> None:
        listing = {
            "Contents": [
                {"Key": "pre/../evil", "Size": 1, "LastModified": _MTIME, "ETag": '"e"'},
                {"Key": "pre/a.txt", "Size": 7, "LastModified": _MTIME, "ETag": '"abc"'},
                {"Key": "pre/marker/", "Size": 0, "LastModified": _MTIME, "ETag": '"m"'},
            ]
        }
        client, calls = make_recording_client([listing, _get_response()])
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/pre", client=client),
            str(tmp_path / "out"),
            recursive=True,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert _ops(calls) == ["ListObjectsV2", "GetObject"]
        assert calls[0].params["Prefix"] == "pre/"
        assert (tmp_path / "out" / "a.txt").read_bytes() == b"payload"
        outcomes = {result.key: result.outcome for result in results}
        assert outcomes["../evil"] is OpOutcome.WARNED
        warned = next(r for r in results if r.outcome is OpOutcome.WARNED)
        assert str(warned.error) == ("Skipping file ../evil. File references a parent directory.")

    def test_recursive_download_skips_a_leading_slash_parent_escape(self, tmp_path: Path) -> None:
        # An S3 key with a double slash after the prefix relativizes to a
        # leading-slash compare_key ("/../secret"); the parent-ref gate must
        # still skip it (aws-cli anchors with "./" before normpath - a bare
        # normpath("/../secret") == "/secret" would slip the ".." and write
        # outside the target directory).
        listing = {
            "Contents": [
                {"Key": "pre//../secret", "Size": 1, "LastModified": _MTIME, "ETag": '"e"'},
            ]
        }
        client, calls = make_recording_client([listing])
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/pre", client=client),
            str(tmp_path / "out"),
            recursive=True,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert _ops(calls) == ["ListObjectsV2"]  # skipped, never fetched
        assert [r.outcome for r in results] == [OpOutcome.WARNED]
        assert "references a parent directory" in str(results[0].error)
        assert not (tmp_path / "secret").exists()

    def test_recursive_download_to_existing_file_dest_empty_source(self, tmp_path: Path) -> None:
        # aws skips the dest makedirs when the path already exists, so a
        # recursive download to an existing FILE with an empty source listing
        # transfers nothing and leaves the file intact - not a FileExistsError
        # crash up front (which would mismatch aws's rc 0).
        dest = tmp_path / "out"
        dest.write_bytes(b"keep")
        client, calls = make_recording_client([{}])  # empty ListObjectsV2 page
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/pre", client=client),
            str(dest),
            recursive=True,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert _ops(calls) == ["ListObjectsV2"]
        assert dest.read_bytes() == b"keep"
        assert results == []


class TestGlacierGate:
    def _download(
        self,
        tmp_path: Path,
        responses: list[dict[str, Any] | Exception],
        **cp_options: Any,
    ) -> tuple[list[ApiCall], list[OpResult]]:
        client, calls = make_recording_client(responses)
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/cold", client=client),
            str(tmp_path / "out.bin"),
            transfer_config=_SYNC,
            on_result=results.append,
            **cp_options,
        )
        return calls, results

    def test_glacier_object_warns_and_skips(self, tmp_path: Path) -> None:
        calls, results = self._download(tmp_path, [_head_response(StorageClass="GLACIER")])
        assert _ops(calls) == ["HeadObject"]
        assert [result.outcome for result in results] == [OpOutcome.WARNED]
        message = str(results[0].error)
        assert message.startswith("Skipping file s3://b/cold. Object is of storage class GLACIER.")
        assert "Unable to perform download operations on GLACIER objects." in message

    def test_deep_archive_blocks_too(self, tmp_path: Path) -> None:
        _, results = self._download(tmp_path, [_head_response(StorageClass="DEEP_ARCHIVE")])
        assert [result.outcome for result in results] == [OpOutcome.WARNED]

    def test_restored_object_transfers(self, tmp_path: Path) -> None:
        head = _head_response(
            StorageClass="GLACIER", Restore='ongoing-request="false", expiry-date="..."'
        )
        calls, results = self._download(tmp_path, [head, _get_response()])
        assert _ops(calls) == ["HeadObject", "GetObject"]
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]

    def test_force_glacier_transfer(self, tmp_path: Path) -> None:
        calls, _ = self._download(
            tmp_path,
            [_head_response(StorageClass="GLACIER"), _get_response()],
            force_glacier_transfer=True,
        )
        assert _ops(calls) == ["HeadObject", "GetObject"]

    def test_ignore_glacier_warnings_skips_silently(self, tmp_path: Path) -> None:
        calls, results = self._download(
            tmp_path,
            [_head_response(StorageClass="GLACIER")],
            ignore_glacier_warnings=True,
        )
        assert _ops(calls) == ["HeadObject"]
        assert [result.outcome for result in results] == [OpOutcome.SKIPPED]


class TestCopyRoute:
    def test_single_copy_heads_the_source_client(self, tmp_path: Path) -> None:
        src_client, src_calls = make_recording_client([_head_response()])
        dst_client, dst_calls = make_recording_client([{}])
        S3().cp(
            S3Storage("s3://src-b/d/a.txt", client=src_client),
            S3Storage("s3://dst-b/cp/", client=dst_client),
            transfer_config=_SYNC,
        )
        assert _ops(src_calls) == ["HeadObject"]
        assert _ops(dst_calls) == ["CopyObject"]
        params = dst_calls[0].params
        assert params["CopySource"] == {"Bucket": "src-b", "Key": "d/a.txt"}
        assert params["Bucket"] == "dst-b"
        assert params["Key"] == "cp/a.txt"

    def test_copy_options_flow_through(self, tmp_path: Path) -> None:
        src_client, _ = make_recording_client([_head_response()])
        dst_client, dst_calls = make_recording_client([{}])
        S3().cp(
            S3Storage("s3://src-b/d/a.txt", client=src_client),
            S3Storage("s3://dst-b/cp/", client=dst_client),
            transfer_config=_SYNC,
            **TransferOptions(metadata={"k": "v"}),
        )
        params = dst_calls[0].params
        assert params["Metadata"] == {"k": "v"}
        assert params["MetadataDirective"] == "REPLACE"


class TestStreamRoutes:
    def test_stream_upload_uses_the_key_verbatim(self) -> None:
        client, calls = make_recording_client([{}])
        results: list[OpResult] = []
        S3().cp(
            IOStorage(io.BytesIO(b"foo\n")),
            S3Storage("s3://bucket/streaming.txt", client=client),
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert [call.operation for call in calls] == ["PutObject"]
        assert calls[0].params["Key"] == "streaming.txt"
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert (results[0].src, results[0].dest) == ("-", "s3://bucket/streaming.txt")

    def test_stream_download_probes_then_streams(self) -> None:
        sink = io.BytesIO()
        client, calls = make_recording_client(
            [
                {"ContentLength": 4, "ETag": '"foo"'},
                {"Body": io.BytesIO(b"foo\n"), "ContentLength": 4, "ETag": '"foo"'},
            ]
        )
        S3().cp(
            S3Storage("s3://bucket/streaming.txt", client=client),
            IOStorage(sink),
            transfer_config=_SYNC,
        )
        assert [call.operation for call in calls] == ["HeadObject", "GetObject"]
        assert sink.getvalue() == b"foo\n"

    def test_stream_download_to_a_text_storage_decodes(self) -> None:
        sink = io.StringIO()
        client, _ = make_recording_client(
            [
                {"ContentLength": 6, "ETag": '"x"'},
                {"Body": io.BytesIO("héllo".encode()), "ContentLength": 6, "ETag": '"x"'},
            ]
        )
        S3().cp(
            S3Storage("s3://bucket/t.txt", client=client), IOStorage(sink), transfer_config=_SYNC
        )
        assert sink.getvalue() == "héllo"

    def test_stream_download_into_a_gzip_writer(self, tmp_path: Path) -> None:
        # A non-seekable binary write stream (gzip's compressor) is a valid
        # download sink: IOStorage writes the object's bytes through it and never
        # closes it, so the ``with`` block finalizes the .gz file on disk.
        out = tmp_path / "out.gz"
        client, calls = make_recording_client(
            [
                {"ContentLength": 4, "ETag": '"foo"'},
                {"Body": io.BytesIO(b"foo\n"), "ContentLength": 4, "ETag": '"foo"'},
            ]
        )
        with gzip.open(out, "wb") as f:
            S3().cp(
                S3Storage("s3://bucket/streaming.txt", client=client),
                IOStorage(f),
                transfer_config=_SYNC,
            )
        assert [call.operation for call in calls] == ["HeadObject", "GetObject"]
        with gzip.open(out, "rb") as g:
            assert g.read() == b"foo\n"

    def test_stream_dryrun_makes_no_calls(self) -> None:
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        S3().cp(
            IOStorage(io.BytesIO(b"x")),
            S3Storage("s3://bucket/k", client=client),
            dryrun=True,
            on_result=results.append,
        )
        assert calls == []
        assert [result.outcome for result in results] == [OpOutcome.DRYRUN]

    def test_stream_with_recursive_is_rejected(self) -> None:
        client, _ = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().cp(
                IOStorage(io.BytesIO(b"x")),
                S3Storage("s3://bucket/k", client=client),
                recursive=True,
            )
        assert "only compatible with non-recursive cp commands" in str(excinfo.value)

    def test_stream_to_stream_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            S3().cp(IOStorage(io.BytesIO(b"x")), IOStorage(io.BytesIO()))

    def test_stream_download_with_no_overwrite_is_rejected(self) -> None:
        # A streaming download has no existing destination to guard, so
        # no_overwrite is meaningless and rejected (aws-cli rejects it too).
        # An upload stream keeps no_overwrite (IfNoneMatch), so dst_stream gates it.
        client, _ = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().cp(
                S3Storage("s3://bucket/k", client=client),
                IOStorage(io.BytesIO()),
                no_overwrite=True,
            )
        assert "no_overwrite is not supported for streaming downloads" in str(excinfo.value)


class TestNoOverwriteDownload:
    def test_existing_destination_is_a_silent_skip(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"already here")
        client, calls = make_recording_client([_head_response()])
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/d/a.txt", client=client),
            str(dest),
            transfer_config=_SYNC,
            on_result=results.append,
            **TransferOptions(no_overwrite=True),
        )
        # The single-path HeadObject runs, but no GetObject follows.
        assert [call.operation for call in calls] == ["HeadObject"]
        assert [result.outcome for result in results] == [OpOutcome.SKIPPED]
        assert dest.read_bytes() == b"already here"

    def test_missing_destination_downloads(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.bin"
        client, calls = make_recording_client([_head_response(), _get_response()])
        S3().cp(
            S3Storage("s3://b/d/a.txt", client=client),
            str(dest),
            transfer_config=_SYNC,
            **TransferOptions(no_overwrite=True),
        )
        assert [call.operation for call in calls] == ["HeadObject", "GetObject"]
        assert dest.read_bytes() == b"payload"


def _cc_listing() -> dict[str, Any]:
    return {
        "Contents": [
            {"Key": "cc/A.txt", "Size": 5, "LastModified": _MTIME, "ETag": '"u"'},
            {"Key": "cc/a.txt", "Size": 5, "LastModified": _MTIME, "ETag": '"l"'},
        ]
    }


class TestCaseConflictGate:
    def _run(
        self,
        tmp_path: Path,
        mode: CaseConflictMode,
        responses: list[dict[str, Any] | Exception],
    ) -> tuple[list[Any], list[OpResult], Path]:
        out = tmp_path / "out"
        client, calls = make_recording_client(responses)
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/cc/", client=client),
            str(out),
            recursive=True,
            transfer_config=_SYNC,
            on_result=results.append,
            **TransferOptions(case_conflict=mode),
        )
        return calls, results, out

    def test_skip_blocks_the_case_twin(self, tmp_path: Path) -> None:
        calls, results, out = self._run(
            tmp_path, CaseConflictMode.SKIP, [_cc_listing(), _get_response()]
        )
        assert [call.operation for call in calls] == ["ListObjectsV2", "GetObject"]
        notices = [r for r in results if r.outcome is OpOutcome.NOTICE]
        assert len(notices) == 1
        assert str(notices[0].error).startswith("warning: Skipping b/cc/a.txt -> ")
        assert "differs only by case" in str(notices[0].error)
        assert (out / "A.txt").exists() and not (out / "a.txt").exists()

    def test_warn_downloads_both_with_a_notice(self, tmp_path: Path) -> None:
        calls, results, out = self._run(
            tmp_path, CaseConflictMode.WARN, [_cc_listing(), _get_response(), _get_response()]
        )
        assert [call.operation for call in calls] == ["ListObjectsV2", "GetObject", "GetObject"]
        notices = [r for r in results if r.outcome is OpOutcome.NOTICE]
        assert len(notices) == 1
        assert str(notices[0].error).startswith("warning: Downloading b/cc/a.txt -> ")
        assert (out / "A.txt").exists() and (out / "a.txt").exists()

    def test_error_raises_the_awscli_failure(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        client, _ = make_recording_client([_cc_listing(), _get_response()])
        with pytest.raises(Boto3S3Error) as excinfo:
            S3().cp(
                S3Storage("s3://b/cc/", client=client),
                str(out),
                recursive=True,
                transfer_config=_SYNC,
                **TransferOptions(case_conflict=CaseConflictMode.ERROR),
            )
        assert str(excinfo.value).startswith("Failed to download b/cc/a.txt -> ")

    def test_exact_case_match_at_dest_always_copies(self, tmp_path: Path) -> None:
        # The AlwaysSync arm: an exact-name local file never
        # conflicts, transfers unconditionally, and is NOT added to the
        # submitted set - so its case twin slips through without a notice.
        out = tmp_path / "out"
        out.mkdir()
        (out / "A.txt").write_bytes(b"old")
        calls, results, _ = self._run(
            tmp_path, CaseConflictMode.SKIP, [_cc_listing(), _get_response(), _get_response()]
        )
        assert [call.operation for call in calls] == ["ListObjectsV2", "GetObject", "GetObject"]
        assert [r for r in results if r.outcome is OpOutcome.NOTICE] == []

    def test_ignore_mode_builds_no_gate(self, tmp_path: Path) -> None:
        calls, results, _out = self._run(
            tmp_path, CaseConflictMode.IGNORE, [_cc_listing(), _get_response(), _get_response()]
        )
        assert [call.operation for call in calls] == ["ListObjectsV2", "GetObject", "GetObject"]
        assert [r for r in results if r.outcome is OpOutcome.NOTICE] == []
