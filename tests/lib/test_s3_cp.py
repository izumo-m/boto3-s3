"""``S3.cp``: route dispatch, naming, gates, and aggregation (recording client).

Behavioral parity pins (aws-cli refs in the implementation): the pre-batch
missing-source error uses aws's wording as a ``NotFoundError`` with no
``ClientError`` cause (their bare RuntimeError -> rc 255, the general rc this
shape maps to), the single S3 source resolves via HeadObject whose 404 is
rewritten to ``Key "..." does not exist`` (in-pipeline -> rc 1), folder
markers and parent-directory escapes are dropped from downloads, the glacier
gate warns/skips/forces, and item failures aggregate into ``BatchError``.
"""

from __future__ import annotations

import gzip
import io
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError, ParamValidationError

from boto3_s3 import GlobFilter, producers, transferplan
from boto3_s3.exceptions import (
    BatchError,
    CancelledError,
    NotFoundError,
    ValidationError,
)
from boto3_s3.iostorage import IOStorage
from boto3_s3.localstorage import LocalStorage
from boto3_s3.producers import CaseConflictGate
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.transfer import TransferItem, Transferrer
from boto3_s3.types import (
    AnnotationCopyMode,
    CancelMode,
    CancelToken,
    CaseConflictMode,
    CopyPropsMode,
    FileFilter,
    FileInfo,
    FileKind,
    OpOutcome,
    OpResult,
    TransferOptions,
    TransferType,
)
from tests.utils.host import is_case_insensitive, skip_if_chmod_is_inert
from tests.utils.recorder import ApiCall, make_recording_client

_SYNC = TransferConfig(use_threads=False)
# The case-conflict gate detects a "two S3 twins in one listing" conflict via its
# in-flight set, which only holds while the first twin's download is still
# running. That needs a non-blocking (threaded) submit so the gate runs ahead of
# completions - aws-cli's own tests use a single worker (max_concurrent_requests =
# 1) here. A fully synchronous NonThreadedExecutor (_SYNC) completes each twin
# before the next is judged, emptying the set, so the conflict is never seen.
_CASE_CONFLICT_CONFIG = TransferConfig(max_concurrency=1)
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

    def test_oversize_upload_warns_but_still_attempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli's _warn_if_too_large: an over-48.8-TiB source warns (the rc-2
        # family) but is still attempted, so S3's own EntityTooLarge stays
        # visible. A real 48.8 TiB file cannot be materialized, so the
        # threshold constant is lowered; the message renders the separate
        # _MAX_UPLOAD_SIZE_TEXT constant, so aws's wording is asserted intact.
        monkeypatch.setattr("boto3_s3.producers._MAX_UPLOAD_SIZE", 1)
        src = tmp_path / "big.bin"
        src.write_bytes(b"xx")  # 2 bytes > the patched limit
        client, calls = make_recording_client([{}])
        results: list[OpResult] = []
        S3().cp(
            str(src),
            S3Storage("s3://bucket/up/", client=client),
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert _ops(calls) == ["PutObject"]  # warned, never skipped
        outcomes = [r.outcome for r in results]
        assert outcomes.count(OpOutcome.WARNED) == 1
        assert outcomes.count(OpOutcome.SUCCEEDED) == 1
        warned = next(r for r in results if r.outcome is OpOutcome.WARNED)
        assert "exceeds s3 upload limit of 48.8 TiB." in str(warned.error)
        assert "big.bin" in str(warned.error)

    def test_result_carries_listing_entry_and_storages(self, tmp_path: Path) -> None:
        # The completion surfaces the source FileInfo and both side Storages so an
        # app can act on the result directly; cp never lists the destination, so
        # dest_info stays None.
        src = tmp_path / "a.txt"
        src.write_bytes(b"x" * 7)
        client, _ = make_recording_client([{}])
        results: list[OpResult] = []
        S3().cp(
            str(src),
            S3Storage("s3://bucket/up/", client=client),
            transfer_config=_SYNC,
            on_result=results.append,
        )
        r = results[0]
        assert r.src_info is not None and r.src_info.key.endswith("a.txt")
        assert r.src_storage is not None
        assert isinstance(r.dest_storage, S3Storage) and r.dest_storage.bucket == "bucket"
        assert r.dest_info is None

    def test_single_download_stamps_the_producing_backend(self, tmp_path: Path) -> None:
        # The single-object HEAD path (head_single) stamps FileInfo.storage like
        # every listing path, so src_info agrees with src_storage (opresult.md)
        # and a filter can reach the backend (info.storage) on this route too.
        client, _ = make_recording_client([_head_response(), _get_response()])
        src = S3Storage("s3://b/d/a.txt", client=client)
        results: list[OpResult] = []
        S3().cp(src, str(tmp_path / "a.txt"), transfer_config=_SYNC, on_result=results.append)
        r = results[0]
        assert r.src_info is not None and r.src_info.storage is src
        assert r.src_storage is src

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
        # The opt-in cycle guard (default off = aws parity) is configured on the
        # LocalStorage source and reaches the local recursive walk.
        (tmp_path / "a.txt").write_bytes(b"x")
        (tmp_path / "loop").symlink_to(tmp_path)  # a directory cycle
        client, calls = make_recording_client([{}])  # one PutObject for a.txt
        results: list[OpResult] = []
        S3().cp(
            LocalStorage(str(tmp_path), detect_symlink_loops=True),
            S3Storage("s3://b/t", client=client),
            recursive=True,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert [call.params["Key"] for call in calls] == ["t/a.txt"]  # loop skipped, no crash
        assert any(
            r.outcome is OpOutcome.WARNED and "Symbolic link loop detected" in str(r.error)
            for r in results
        )

    def test_complete_source_enumeration_reaches_the_transfer_filter(self, tmp_path: Path) -> None:
        # The constructor's enumeration policy reaches the high-level operation.
        # A caller can inspect every entry and keep only transferable files.
        (tmp_path / "a.txt").write_bytes(b"x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.txt").write_bytes(b"x")
        (tmp_path / "link.txt").symlink_to(tmp_path / "a.txt")  # a symlink leaf
        client, calls = make_recording_client([{}, {}, {}])  # exactly the three files
        seen: list[str] = []

        def exclude_directories(info: FileInfo) -> bool:
            assert info.compare_key is not None
            seen.append(info.compare_key)
            return info.kind is FileKind.FILE

        S3().cp(
            LocalStorage(str(tmp_path), enumerate_all_entries=True),
            S3Storage("s3://b/t", client=client),
            recursive=True,
            filter=exclude_directories,
            transfer_config=_SYNC,
        )
        assert seen == ["", "a.txt", "link.txt", "sub/", "sub/b.txt"]
        assert [call.params["Key"] for call in calls] == ["t/a.txt", "t/link.txt", "t/sub/b.txt"]

    def test_missing_source_raises_not_found_up_front(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "nope.txt")
        client, calls = make_recording_client([])
        with pytest.raises(NotFoundError) as excinfo:
            S3().cp(missing, S3Storage("s3://b/k", client=client))
        # aws-cli raises a bare RuntimeError here (rc 255); NotFoundError with
        # no ClientError cause maps to the same general rc (the class must not
        # be ValidationError / ConfigurationError, whose rc differ).
        assert excinfo.value.__cause__ is None
        assert str(excinfo.value) == f"The user-provided path {missing} does not exist."
        assert calls == []

    @skip_if_chmod_is_inert
    def test_unreadable_single_source_warns_without_failing(self, tmp_path: Path) -> None:
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

    def test_graceful_cancel_from_result_drains_submitted_transfers(self, tmp_path: Path) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            (tmp_path / name).write_bytes(name.encode())
        client, _ = make_recording_client([])
        calls: list[str] = []
        release_first = threading.Event()

        def api_call(operation: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append(f"{operation}:{params['Key']}")
            if len(calls) == 1:
                assert release_first.wait(5.0)
            return {}

        client._make_api_call = api_call  # type: ignore[method-assign]
        token = CancelToken()
        results: list[OpResult] = []

        def cancel_after_first(result: OpResult) -> None:
            results.append(result)
            if len(results) == 1:
                token.cancel()

        threading.Timer(0.2, release_first.set).start()
        with pytest.raises(CancelledError):
            S3().cp(
                str(tmp_path),
                S3Storage("s3://b/p/", client=client),
                recursive=True,
                transfer_config=TransferConfig(max_concurrency=1),
                cancel_token=token,
                on_result=cancel_after_first,
            )

        assert calls == ["PutObject:p/a.txt", "PutObject:p/b.txt", "PutObject:p/c.txt"]
        assert [result.outcome for result in results] == [
            OpOutcome.SUCCEEDED,
            OpOutcome.SUCCEEDED,
            OpOutcome.SUCCEEDED,
        ]

    def test_immediate_escalation_cancels_queued_transfers(self, tmp_path: Path) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            (tmp_path / name).write_bytes(name.encode())
        client, _ = make_recording_client([])
        calls: list[str] = []
        release_first = threading.Event()
        release_running = threading.Event()

        def api_call(operation: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append(f"{operation}:{params['Key']}")
            if len(calls) == 1:
                assert release_first.wait(5.0)
            else:
                assert release_running.wait(5.0)
            return {}

        client._make_api_call = api_call  # type: ignore[method-assign]
        token = CancelToken()

        def cancel_after_first(_result: OpResult) -> None:
            token.cancel()
            threading.Timer(0.05, lambda: token.cancel(mode=CancelMode.IMMEDIATE)).start()
            threading.Timer(0.2, release_running.set).start()

        threading.Timer(0.2, release_first.set).start()
        with pytest.raises(CancelledError):
            S3().cp(
                str(tmp_path),
                S3Storage("s3://b/p/", client=client),
                recursive=True,
                transfer_config=TransferConfig(max_concurrency=1),
                cancel_token=token,
                on_result=cancel_after_first,
            )

        # The request executor may start b before a's completion callback runs;
        # that request is accepted work and cannot be interrupted safely. c is
        # still queued and is cancelled before it becomes an S3 request.
        assert calls in (
            ["PutObject:p/a.txt"],
            ["PutObject:p/a.txt", "PutObject:p/b.txt"],
        )


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

    def test_single_s3_source_is_filtered_too(self, tmp_path: Path) -> None:
        # aws applies --exclude/--include on the single-object routes as well
        # (its filter stage runs regardless of dir_op): the source is still
        # resolved (HeadObject, like aws's file generator) but an excluded
        # object transfers nothing - no GetObject, no local file, rc 0.
        drop = GlobFilter().exclude("*").compile()
        client, calls = make_recording_client([_head_response()])
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/pre/k.txt", client=client),
            str(tmp_path / "k.txt"),
            filter=drop,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert _ops(calls) == ["HeadObject"]
        assert results == []
        assert not (tmp_path / "k.txt").exists()

    def test_single_upload_is_filtered_too(self, tmp_path: Path) -> None:
        src = tmp_path / "k.txt"
        src.write_bytes(b"x")
        drop = GlobFilter().exclude("*").compile()
        client, calls = make_recording_client([])
        S3().cp(str(src), S3Storage("s3://b/k.txt", client=client), filter=drop)
        assert calls == []


class TestDownloadRoute:
    def test_single_download_heads_then_gets(self, tmp_path: Path) -> None:
        client, calls = make_recording_client([_head_response(), _get_response()])
        dest = tmp_path / "out.bin"
        S3().cp(S3Storage("s3://b/d/a.txt", client=client), str(dest), transfer_config=_SYNC)
        assert _ops(calls) == ["HeadObject", "GetObject"]
        assert calls[0].params == {"Bucket": "b", "Key": "d/a.txt", "ChecksumMode": "ENABLED"}
        assert dest.read_bytes() == b"payload"
        assert os.stat(dest).st_mtime == _MTIME.timestamp()

    def test_head_omits_checksum_mode_below_the_knob_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The ChecksumMode=ENABLED injection is gated on the client resolving
        # response_checksum_validation to "when_supported"; a floor botocore
        # predates the knob entirely and the getattr guard reads that as None
        # - patched here, since the current botocore always carries the
        # attribute. The HEAD must go out without ChecksumMode (the
        # era-appropriate wire shape, docs/overview.md section 2), not raise.
        client, calls = make_recording_client([_head_response(), _get_response()])
        monkeypatch.setattr(client.meta.config, "response_checksum_validation", None, raising=False)
        S3().cp(
            S3Storage("s3://b/d/a.txt", client=client),
            str(tmp_path / "out.bin"),
            transfer_config=_SYNC,
        )
        assert _ops(calls) == ["HeadObject", "GetObject"]
        assert calls[0].params == {"Bucket": "b", "Key": "d/a.txt"}

    def test_missing_single_source_uses_the_rewritten_404_message(self, tmp_path: Path) -> None:
        client, _ = make_recording_client([_client_error("404", 404, "HeadObject")])
        with pytest.raises(NotFoundError) as excinfo:
            S3().cp(S3Storage("s3://b/no-such", client=client), str(tmp_path / "x"))
        assert str(excinfo.value) == (
            "An error occurred (404) when calling the HeadObject operation: "
            'Key "no-such" does not exist'
        )

    def test_bucketless_service_root_source_reaches_head_not_silent_zero(
        self, tmp_path: Path
    ) -> None:
        # A bare `s3://` (service root) as a non-recursive source must NOT take the
        # keyless-bucket `cp s3://bucket .` zero-item short-circuit: it reaches
        # HeadObject with Bucket="", which botocore rejects (Invalid bucket name)
        # like `aws s3 cp s3://` (rc 1), not a silent rc-0 no-op.
        err = ParamValidationError(report='Invalid bucket name ""')
        client, calls = make_recording_client([err])
        with pytest.raises(ValidationError, match="Invalid bucket name"):
            S3().cp(S3Storage("s3://", client=client), str(tmp_path / "x"), transfer_config=_SYNC)
        assert _ops(calls) == ["HeadObject"]  # attempted, not short-circuited to zero
        assert calls[0].params["Bucket"] == ""

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
        # (rc 0, silent). The listing itself is observable when ListBucket is
        # denied, so it must not be optimized away.
        client, calls = make_recording_client([{}])
        results: list[OpResult] = []
        S3().cp(S3Storage("s3://bucket", client=client), str(tmp_path), on_result=results.append)
        assert _ops(calls) == ["ListObjectsV2"]
        assert calls[0].params == {
            "Bucket": "bucket",
            "Prefix": "",
            "MaxKeys": 1000,
        }
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
        dest_client, dest_calls = make_recording_client([{}])
        S3().cp(
            S3Storage("s3://src-b/d/a.txt", client=src_client),
            S3Storage("s3://dest-b/cp/", client=dest_client),
            transfer_config=_SYNC,
        )
        assert _ops(src_calls) == ["HeadObject"]
        assert _ops(dest_calls) == ["CopyObject"]
        params = dest_calls[0].params
        assert params["CopySource"] == {"Bucket": "src-b", "Key": "d/a.txt"}
        assert params["Bucket"] == "dest-b"
        assert params["Key"] == "cp/a.txt"

    def test_copy_options_flow_through(self, tmp_path: Path) -> None:
        src_client, _ = make_recording_client([_head_response()])
        dest_client, dest_calls = make_recording_client([{}])
        S3().cp(
            S3Storage("s3://src-b/d/a.txt", client=src_client),
            S3Storage("s3://dest-b/cp/", client=dest_client),
            transfer_config=_SYNC,
            **TransferOptions(metadata={"k": "v"}),
        )
        params = dest_calls[0].params
        assert params["Metadata"] == {"k": "v"}
        assert params["MetadataDirective"] == "REPLACE"

    def test_annotation_read_failure_precedes_destination_creation(self) -> None:
        src_client, src_calls = make_recording_client(
            [
                _head_response(ContentLength=9 * 1024 * 1024),
                {"TagSet": []},
                _client_error("AccessDenied", 403, "ListObjectAnnotations"),
            ]
        )
        dest_client, dest_calls = make_recording_client([])

        with pytest.raises(BatchError):
            S3().cp(
                S3Storage("s3://src-b/d/a.txt", client=src_client),
                S3Storage("s3://dest-b/cp/", client=dest_client),
                transfer_config=_SYNC,
                **TransferOptions(
                    copy_props=CopyPropsMode.ALL,
                    annotation_copy_mode=AnnotationCopyMode.PRELOAD_MEMORY,
                ),
            )

        assert _ops(src_calls) == [
            "HeadObject",
            "GetObjectTagging",
            "ListObjectAnnotations",
        ]
        assert dest_calls == []


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

    def test_stream_dryrun_never_opens_the_stream(self) -> None:
        # The open routes already gate open() behind dryrun; the stream route must
        # match, so a side-effecting custom IOStorage is left untouched on a dry
        # run (the open is skipped, not merely its bytes left unread).
        class _SpyIO(IOStorage):
            def __init__(self, stream: Any) -> None:
                super().__init__(stream)
                self.opens: list[str] = []

            def open(self, key: str, mode: Any, *, size: int | None = None) -> Any:
                self.opens.append(key)
                return super().open(key, mode, size=size)

        up = _SpyIO(io.BytesIO(b"x"))
        client, calls = make_recording_client([])
        S3().cp(up, S3Storage("s3://bucket/k", client=client), dryrun=True)
        assert calls == [] and up.opens == []

        down = _SpyIO(io.BytesIO())
        client, calls = make_recording_client([])
        S3().cp(S3Storage("s3://bucket/k", client=client), down, dryrun=True)
        assert calls == [] and down.opens == []

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
        # An upload stream keeps no_overwrite (IfNoneMatch), so dest_stream gates it.
        client, _ = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().cp(
                S3Storage("s3://bucket/k", client=client),
                IOStorage(io.BytesIO()),
                no_overwrite=True,
            )
        assert "no_overwrite is not supported for streaming downloads" in str(excinfo.value)

    def test_stream_peer_must_be_s3_not_local(self, tmp_path: Path) -> None:
        # A stream's peer must be S3; a local path on the other side is the
        # "stream on one side" error, not the generic "cp accepts an 's3://...'"
        # message (which reads as if cp never takes a local path). Both
        # directions - stream upload and stream download - go through the same
        # peer check.
        with pytest.raises(ValidationError) as up:
            S3().cp(IOStorage(io.BytesIO(b"x")), str(tmp_path / "out.txt"))
        assert "the other must be s3://" in str(up.value)
        with pytest.raises(ValidationError) as down:
            S3().cp(str(tmp_path / "in.txt"), IOStorage(io.BytesIO()))
        assert "the other must be s3://" in str(down.value)

    def test_stream_cancel_token_stops_before_open(self) -> None:
        # The stream route honors cancel_token like the non-stream route: a
        # pre-cancelled token raises before the fileobj is opened or submitted,
        # so nothing transfers and a side-effecting stream stays untouched
        # (cancel_token is a library extension, no aws parity at stake).
        class _SpyIO(IOStorage):
            def __init__(self, stream: Any) -> None:
                super().__init__(stream)
                self.opens: list[str] = []

            def open(self, key: str, mode: Any, *, size: int | None = None) -> Any:
                self.opens.append(key)
                return super().open(key, mode, size=size)

        token = CancelToken()
        token.cancel()
        up = _SpyIO(io.BytesIO(b"x"))
        client, calls = make_recording_client([])
        with pytest.raises(CancelledError):
            S3().cp(up, S3Storage("s3://b/k", client=client), cancel_token=token)
        assert calls == [] and up.opens == []


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


class TestSourceScanConfig:
    """The recursive S3 source lists via ``scan_s3_source``, whose options derive
    from the passed storage's own ``default_scan_options`` - so constructor
    config and a custom ``S3Storage`` subclass survive through the engine."""

    def test_recursive_s3_source_lists_with_the_constructor_config(self, tmp_path: Path) -> None:
        client, calls = make_recording_client([_cc_listing(), _get_response(), _get_response()])
        S3().cp(
            S3Storage("s3://b/cc/", client=client, page_size=5, fetch_owner=True),
            str(tmp_path / "out"),
            recursive=True,
            transfer_config=_SYNC,
        )
        listing = calls[0]
        assert listing.operation == "ListObjectsV2"
        # The paginator translates the storage's page_size into MaxKeys.
        assert listing.params["MaxKeys"] == 5
        assert listing.params["FetchOwner"] is True

    def test_custom_s3_subclass_scan_pages_override_survives(self, tmp_path: Path) -> None:
        class Tagged(S3Storage):
            def scan_pages(self, options: Any) -> Any:
                for page in super().scan_pages(options):
                    for info in page:
                        info.compare_key = f"TAG/{info.compare_key}"
                    yield page

        listing = {
            "Contents": [{"Key": "d/a.txt", "Size": 7, "LastModified": _MTIME, "ETag": '"x"'}]
        }
        client, _ = make_recording_client([listing, _get_response()])
        out = tmp_path / "out"
        S3().cp(Tagged("s3://b/d/", client=client), str(out), recursive=True, transfer_config=_SYNC)
        # The override's compare_key rewrite shaped the download target: the
        # engine consumed the subclass's scan, not a re-built bare S3Storage.
        assert (out / "TAG" / "a.txt").read_bytes() == b"payload"


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
            transfer_config=_CASE_CONFLICT_CONFIG,
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
        # Listed by stored name: exists() cannot tell the twins apart on a
        # case-insensitive destination.
        assert os.listdir(out) == ["A.txt"]

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
        with pytest.raises(ValidationError) as excinfo:
            S3().cp(
                S3Storage("s3://b/cc/", client=client),
                str(out),
                recursive=True,
                transfer_config=_CASE_CONFLICT_CONFIG,
                **TransferOptions(case_conflict=CaseConflictMode.ERROR),
            )
        assert type(excinfo.value) is ValidationError
        assert str(excinfo.value).startswith("Failed to download b/cc/a.txt -> ")

    def test_error_reports_the_operation_the_gate_was_built_for(self, tmp_path: Path) -> None:
        # cp and mv share the transfer path, so the gate must carry the running
        # operation: a mv-driven --case-conflict error reports operation="mv",
        # not the CaseConflictGate default ("cp").
        out = tmp_path / "out"
        out.mkdir()
        plan = transferplan.plan_transfer(
            S3Storage("s3://b/cc/"), LocalStorage(str(out)), recursive=True, operation="mv"
        )
        client, _ = make_recording_client([])
        transferrer = Transferrer(TransferType.DOWNLOAD, client)
        gate = producers.cp_case_gate(
            plan,
            recursive=True,
            options=TransferOptions(case_conflict=CaseConflictMode.ERROR),
            transferrer=transferrer,
            item_filter=None,
            operation="mv",
        )
        assert gate is not None
        first = TransferItem(
            compare_key="cc/A.txt", src_bucket="b", src_key="cc/A.txt", dest_path=str(out / "A.txt")
        )
        assert gate.blocks(first, transferrer) is False  # admitted; no twin yet
        twin = TransferItem(
            compare_key="cc/a.txt", src_bucket="b", src_key="cc/a.txt", dest_path=str(out / "a.txt")
        )
        with pytest.raises(ValidationError) as excinfo:
            gate.blocks(twin, transferrer)
        assert excinfo.value.operation == "mv"
        assert str(excinfo.value).startswith("Failed to download b/cc/a.txt -> ")

    def _build_gate(
        self, dest: LocalStorage, item_filter: FileFilter | None = None
    ) -> tuple[CaseConflictGate | None, Transferrer]:
        plan = transferplan.plan_transfer(
            S3Storage("s3://b/cc/"), dest, recursive=True, operation="cp"
        )
        client, _ = make_recording_client([])
        transferrer = Transferrer(TransferType.DOWNLOAD, client)
        gate = producers.cp_case_gate(
            plan,
            recursive=True,
            options=TransferOptions(case_conflict=CaseConflictMode.SKIP),
            transferrer=transferrer,
            item_filter=item_filter,
            operation="cp",
        )
        return gate, transferrer

    def test_case_gate_scan_reads_the_dest_storage_config(self, tmp_path: Path) -> None:
        # aws builds the case-conflict reverse enumeration with the run's
        # follow_symlinks parameter (rgen_kwargs), so the membership scan honors
        # the destination storage's config: a --no-follow-symlinks destination
        # excludes the symlinked twin that a default destination includes.
        out = tmp_path / "out"
        out.mkdir()
        (out / "real.txt").write_bytes(b"x")
        (out / "A.txt").symlink_to(out / "real.txt")
        gate, _ = self._build_gate(LocalStorage(str(out)))
        assert gate is not None
        assert "A.txt" in gate._dest_keys  # pyright: ignore[reportPrivateUsage]
        gate, _ = self._build_gate(LocalStorage(str(out), follow_symlinks=False))
        assert gate is not None
        assert "A.txt" not in gate._dest_keys  # pyright: ignore[reportPrivateUsage]

    @skip_if_chmod_is_inert
    def test_case_gate_scan_warns_into_the_rollup_and_filters(self, tmp_path: Path) -> None:
        # aws's reverse enumeration shares the result queue (its walk warnings
        # count toward rc 2) and passes the --exclude/--include filters; the
        # membership scan does the same: the unreadable file warns through the
        # transferrer and the filtered-out entry stays out of the set.
        out = tmp_path / "out"
        out.mkdir()
        (out / "A.txt").write_bytes(b"x")
        (out / "secret.txt").write_bytes(b"x")
        (out / "secret.txt").chmod(0)
        try:
            gate, transferrer = self._build_gate(
                LocalStorage(str(out)), item_filter=lambda info: info.compare_key != "A.txt"
            )
        finally:
            (out / "secret.txt").chmod(0o644)
        assert gate is not None
        assert gate._dest_keys == set()  # pyright: ignore[reportPrivateUsage]
        assert transferrer.warned == 1  # the unreadable-file walk warning

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
        notices = [r for r in results if r.outcome is OpOutcome.NOTICE]
        if is_case_insensitive(tmp_path):
            # The exact-match arm still copies A.txt unconditionally, but here
            # the twin IS gated: ``os.path.exists(dest)`` sees A.txt for a.txt
            # (the gate's case-insensitive-filesystem arm).
            assert [call.operation for call in calls] == ["ListObjectsV2", "GetObject"]
            assert len(notices) == 1
        else:
            assert [call.operation for call in calls] == ["ListObjectsV2", "GetObject", "GetObject"]
            assert notices == []

    def test_ignore_mode_builds_no_gate(self, tmp_path: Path) -> None:
        calls, results, _out = self._run(
            tmp_path, CaseConflictMode.IGNORE, [_cc_listing(), _get_response(), _get_response()]
        )
        assert [call.operation for call in calls] == ["ListObjectsV2", "GetObject", "GetObject"]
        assert [r for r in results if r.outcome is OpOutcome.NOTICE] == []
