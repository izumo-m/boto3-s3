"""``S3.sync``: the two-layer pipeline over a recording client.

Behavioral parity pins (aws-cli refs in the implementation): the size+mtime
judgment with its direction asymmetry,
``--delete`` driven purely by the destination-only pairs (folder markers and
filtered entries protected), the visibility layer pruning each side against
its own root, dry runs that list but never act, the missing-source 255 shape,
a source *file* degrading to the aws-cli's walk warning, and ``no_overwrite``
applied as an orthogonal write-guard in the sync loop (no ``IfNoneMatch`` on
the wire).
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3 import GlobFilter
from boto3_s3.awsclicompare import AwsCliComparison
from boto3_s3.comparator import ParallelCompare, SyncPair
from boto3_s3.exceptions import BatchError, Boto3S3Error, CancelledError
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import (
    CancelToken,
    CaseConflictMode,
    OpOutcome,
    OpResult,
    TransferOptions,
    TransferType,
)
from tests.utils.recorder import ApiCall, make_recording_client

_SERIAL = TransferConfig(use_threads=False)
# The case-conflict gate detects a "two S3 twins" conflict via its in-flight set,
# which only holds while the first twin's download is still running - it needs a
# threaded (non-blocking) submit running ahead of completions, as aws-cli's own
# tests do with a single worker (max_concurrent_requests = 1). _SERIAL completes
# each twin before the next is judged, emptying the set.
_CASE_CONFLICT_CONFIG = TransferConfig(max_concurrency=1)
_MTIME = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_OLDER = _MTIME - timedelta(hours=1)
_NEWER = _MTIME + timedelta(hours=1)


def _ops(calls: list[ApiCall]) -> list[str]:
    return [call.operation for call in calls]


def _listing(*entries: tuple[str, int]) -> dict[str, Any]:
    return {
        "Contents": [
            {"Key": key, "Size": size, "LastModified": _MTIME, "ETag": '"e"'}
            for key, size in entries
        ]
    }


def _get_response(body: bytes = b"payload") -> dict[str, Any]:
    import io

    return {"Body": io.BytesIO(body), "ContentLength": len(body), "ETag": '"abc"'}


def _write(root: Path, rel: str, body: bytes, *, mtime: datetime | None = None) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    if mtime is not None:
        os.utime(target, (mtime.timestamp(), mtime.timestamp()))
    return target


def _always_copies(_pair: SyncPair) -> bool:
    return True


class TestSyncUpload:
    def test_judges_each_pair_with_the_awscli_rules(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src, "new.txt", b"xx", mtime=_OLDER)  # not at dest -> upload
        _write(src, "same.txt", b"xx", mtime=_OLDER)  # dest newer -> skip
        _write(src, "stale.txt", b"xx", mtime=_NEWER)  # dest older -> upload
        _write(src, "bigger.txt", b"xxx", mtime=_OLDER)  # size differs -> upload
        listing = _listing(("p/bigger.txt", 2), ("p/same.txt", 2), ("p/stale.txt", 2))
        client, calls = make_recording_client([listing, {}, {}, {}])
        S3().sync(str(src), S3Storage("s3://bucket/p", client=client), transfer_config=_SERIAL)
        assert _ops(calls) == ["ListObjectsV2", "PutObject", "PutObject", "PutObject"]
        assert calls[0].params["Prefix"] == "p/"
        assert [call.params["Key"] for call in calls[1:]] == [
            "p/bigger.txt",
            "p/new.txt",
            "p/stale.txt",
        ]

    def test_detect_symlink_loops_reaches_the_local_walk(self, tmp_path: Path) -> None:
        # The opt-in cycle guard (default off = aws parity) flows through sync's
        # local-side walk too, not just cp/mv's.
        src = tmp_path / "src"
        _write(src, "a.txt", b"x")
        (src / "loop").symlink_to(src)  # a directory cycle
        client, calls = make_recording_client([_listing(), {}])  # empty dest, PutObject a.txt
        results: list[OpResult] = []
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            detect_symlink_loops=True,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert _ops(calls) == ["ListObjectsV2", "PutObject"]
        assert any(
            r.outcome is OpOutcome.WARNED and "Symbolic link loop detected" in str(r.error)
            for r in results
        )

    def test_delete_off_ignores_dest_only_entries(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client([_listing(("p/extra.txt", 2))])
        results: list[OpResult] = []
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert _ops(calls) == ["ListObjectsV2"]
        assert results == []

    def test_delete_batches_dest_only_keys_and_spares_markers(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src, "keep.txt", b"xx", mtime=_OLDER)
        listing: dict[str, Any] = {
            "Contents": [
                {"Key": "p/extra1.txt", "Size": 2, "LastModified": _MTIME, "ETag": '"e"'},
                {"Key": "p/keep.txt", "Size": 2, "LastModified": _NEWER, "ETag": '"e"'},
                # A folder marker never surfaces on either side: not deleted.
                {"Key": "p/marker/", "Size": 0, "LastModified": _MTIME, "ETag": '"m"'},
                {"Key": "p/sub/extra2.txt", "Size": 2, "LastModified": _MTIME, "ETag": '"e"'},
            ]
        }
        client, calls = make_recording_client([listing, {}])
        results: list[OpResult] = []
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            delete=True,
            transfer_config=_SERIAL,
            on_result=results.append,
            request_payer="requester",
        )
        assert _ops(calls) == ["ListObjectsV2", "DeleteObjects"]
        assert calls[0].params["RequestPayer"] == "requester"
        delete_params = calls[1].params
        assert delete_params["RequestPayer"] == "requester"
        keys = [entry["Key"] for entry in delete_params["Delete"]["Objects"]]
        assert keys == ["p/extra1.txt", "p/sub/extra2.txt"]
        deleted = [r for r in results if r.transfer_type is TransferType.DELETE]
        assert {r.outcome for r in deleted} == {OpOutcome.SUCCEEDED}
        assert sorted(r.src for r in deleted if r.src) == [
            "s3://bucket/p/extra1.txt",
            "s3://bucket/p/sub/extra2.txt",
        ]

    def test_delete_result_carries_src_info_and_storage(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        client, _ = make_recording_client([_listing(("p/orphan.txt", 2)), {}])
        results: list[OpResult] = []
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            delete=True,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        deleted = [r for r in results if r.transfer_type is TransferType.DELETE]
        assert len(deleted) == 1
        assert deleted[0].src == "s3://bucket/p/orphan.txt"
        assert deleted[0].src_info is not None and deleted[0].src_info.key == "p/orphan.txt"
        assert isinstance(deleted[0].src_storage, S3Storage)
        assert deleted[0].dest_info is None

    def test_copy_update_result_carries_both_compared_sides(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src, "a.txt", b"new")  # size 3 != the dest's 2 -> an update upload
        client, _ = make_recording_client([_listing(("p/a.txt", 2)), {}])
        results: list[OpResult] = []
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        copied = [r for r in results if r.transfer_type is not TransferType.DELETE]
        assert len(copied) == 1
        assert copied[0].src_info is not None and copied[0].src_info.key.endswith("a.txt")
        assert copied[0].dest_info is not None and copied[0].dest_info.key == "p/a.txt"

    def test_copy_new_result_has_no_dest_info(self, tmp_path: Path) -> None:
        # docs/opresult.md: only an update pairs with a pre-existing
        # destination entry; a new-file record carries dest_info=None.
        src = tmp_path / "src"
        _write(src, "new.txt", b"xx")
        client, _ = make_recording_client([_listing(), {}])
        results: list[OpResult] = []
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        copied = [r for r in results if r.transfer_type is not TransferType.DELETE]
        assert len(copied) == 1
        assert copied[0].src_info is not None
        assert copied[0].dest_info is None

    def test_delete_predicate_narrows_the_lane(self, tmp_path: Path) -> None:
        # A FileFilter predicate (the orphan's FileInfo) narrows which orphans
        # are deleted - the delete lane is rm over the orphans.
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client(
            [_listing(("p/extra.log", 2), ("p/extra.txt", 2)), {}]
        )
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            delete=lambda info: info.key.endswith(".log"),
            transfer_config=_SERIAL,
        )
        keys = [entry["Key"] for entry in calls[1].params["Delete"]["Objects"]]
        assert keys == ["p/extra.log"]

    def test_delete_filter_narrows_by_name(self, tmp_path: Path) -> None:
        # A GlobFilter narrows the delete lane by the orphan's compare key
        # (relative), reusing rm's filter shape on the orphans.
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client(
            [_listing(("p/extra.log", 2), ("p/extra.txt", 2)), {}]
        )
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            delete=GlobFilter().exclude("*").include("*.log").compile(),
            transfer_config=_SERIAL,
        )
        keys = [entry["Key"] for entry in calls[1].params["Delete"]["Objects"]]
        assert keys == ["p/extra.log"]

    def test_excluded_dest_entries_are_protected_from_delete(self, tmp_path: Path) -> None:
        # aws: "files excluded by filters are excluded from deletion" - the
        # visibility layer prunes the destination stream before pairing.
        src = tmp_path / "src"
        src.mkdir()
        keep = GlobFilter().exclude("*.log").compile()
        client, calls = make_recording_client(
            [_listing(("p/extra.log", 2), ("p/extra.txt", 2)), {}]
        )
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            delete=True,
            filter=keep,
            transfer_config=_SERIAL,
        )
        keys = [entry["Key"] for entry in calls[1].params["Delete"]["Objects"]]
        assert keys == ["p/extra.txt"]

    def test_dryrun_lists_but_never_acts(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src, "new.txt", b"xx", mtime=_OLDER)
        client, calls = make_recording_client([_listing(("p/extra.txt", 2))])
        results: list[OpResult] = []
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            delete=True,
            dryrun=True,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert _ops(calls) == ["ListObjectsV2"]
        outcomes = {(r.transfer_type, r.key): r.outcome for r in results}
        assert outcomes == {
            (TransferType.UPLOAD, "new.txt"): OpOutcome.DRYRUN,
            (TransferType.DELETE, "p/extra.txt"): OpOutcome.DRYRUN,
        }

    def test_compare_replaces_the_default_judgment(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src, "same.txt", b"xx", mtime=_OLDER)  # default would skip
        listing = _listing(("p/same.txt", 2))
        client, calls = make_recording_client([listing, {}])
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            compare=lambda pair: True,
            transfer_config=_SERIAL,
        )
        assert _ops(calls) == ["ListObjectsV2", "PutObject"]

    def test_no_overwrite_judges_without_if_none_match(self, tmp_path: Path) -> None:
        # no_overwrite is an orthogonal sync write-guard: the at-both pair is
        # skipped before the strategy, and the new file's upload carries no
        # conditional-write header (sync never sends IfNoneMatch).
        src = tmp_path / "src"
        _write(src, "exists.txt", b"xxx", mtime=_NEWER)  # differs, but never overwritten
        _write(src, "new.txt", b"xx", mtime=_OLDER)
        client, calls = make_recording_client([_listing(("p/exists.txt", 2)), {}])
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            no_overwrite=True,
            transfer_config=_SERIAL,
        )
        assert _ops(calls) == ["ListObjectsV2", "PutObject"]
        assert calls[1].params["Key"] == "p/new.txt"
        assert "IfNoneMatch" not in calls[1].params

    def test_no_overwrite_guards_any_compare_strategy(self, tmp_path: Path) -> None:
        # The write-guard runs before the strategy: even compare=True (cp-like)
        # never overwrites an existing destination, while a source-only pair
        # still uploads.
        src = tmp_path / "src"
        _write(src, "exists.txt", b"xxx", mtime=_OLDER)  # at dest -> guarded
        _write(src, "new.txt", b"xx", mtime=_OLDER)  # source-only -> uploaded
        client, calls = make_recording_client([_listing(("p/exists.txt", 2)), {}])
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            compare=True,
            no_overwrite=True,
            transfer_config=_SERIAL,
        )
        assert _ops(calls) == ["ListObjectsV2", "PutObject"]
        assert calls[1].params["Key"] == "p/new.txt"

    def test_missing_source_directory_raises_the_base_category(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "nope")
        client, calls = make_recording_client([])
        with pytest.raises(Boto3S3Error) as excinfo:
            S3().sync(missing, S3Storage("s3://bucket/p", client=client))
        assert type(excinfo.value) is Boto3S3Error
        assert str(excinfo.value) == f"The user-provided path {missing} does not exist."
        assert calls == []

    def test_source_file_degrades_to_the_walk_warning(self, tmp_path: Path) -> None:
        # `aws s3 sync ./file s3://...` treats the file as a
        # directory root, warns "File does not exist." (trailing separator
        # included) and exits 2 - no hard failure.
        src = _write(tmp_path, "afile.txt", b"xx")
        client, calls = make_recording_client([_listing()])
        results: list[OpResult] = []
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert _ops(calls) == ["ListObjectsV2"]
        assert [r.outcome for r in results] == [OpOutcome.WARNED]
        assert f"Skipping file {src}{os.sep}. File does not exist." in str(results[0].error)

    def test_cancel_token_aborts_between_pairs(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src, "a.txt", b"xx")
        client, _calls = make_recording_client([_listing()])
        token = CancelToken()
        token.cancel()
        with pytest.raises(CancelledError):
            S3().sync(
                str(src),
                S3Storage("s3://bucket/p", client=client),
                transfer_config=_SERIAL,
                cancel_token=token,
            )


class TestSyncDownload:
    def test_downloads_only_when_local_is_newer(self, tmp_path: Path) -> None:
        # The aws-cli asymmetry: same-size pairs download only when the LOCAL
        # side is newer than S3.
        out = tmp_path / "out"
        _write(out, "old.txt", b"xx", mtime=_OLDER)  # local older -> skip
        _write(out, "touched.txt", b"xx", mtime=_NEWER)  # local newer -> download
        listing = _listing(("d/new.txt", 7), ("d/old.txt", 2), ("d/touched.txt", 2))
        client, calls = make_recording_client([listing, _get_response(), _get_response()])
        S3().sync(S3Storage("s3://bucket/d", client=client), str(out), transfer_config=_SERIAL)
        assert _ops(calls) == ["ListObjectsV2", "GetObject", "GetObject"]
        assert [call.params["Key"] for call in calls[1:]] == ["d/new.txt", "d/touched.txt"]
        assert (out / "new.txt").read_bytes() == b"payload"

    def test_exact_timestamps_downloads_on_any_skew(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _write(out, "old.txt", b"xx", mtime=_OLDER)
        client, calls = make_recording_client([_listing(("d/old.txt", 2)), _get_response()])
        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            str(out),
            compare=AwsCliComparison(exact_timestamps=True),
            transfer_config=_SERIAL,
        )
        assert _ops(calls) == ["ListObjectsV2", "GetObject"]

    def test_size_only_ignores_time_differences(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _write(out, "touched.txt", b"xx", mtime=_NEWER)
        client, calls = make_recording_client([_listing(("d/touched.txt", 2))])
        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            str(out),
            compare=AwsCliComparison(size_only=True),
            transfer_config=_SERIAL,
        )
        assert _ops(calls) == ["ListObjectsV2"]

    def test_compare_true_copies_every_source(self, tmp_path: Path) -> None:
        # compare=True forces all source-present pairs through, even an
        # up-to-date one the default would skip (cp-like).
        src = tmp_path / "src"
        _write(src, "same.txt", b"xx", mtime=_OLDER)  # same size, older -> default would skip
        client, calls = make_recording_client([_listing(("p/same.txt", 2)), {}])
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            compare=True,
            transfer_config=_SERIAL,
        )
        assert _ops(calls) == ["ListObjectsV2", "PutObject"]

    def test_compare_false_copies_nothing(self, tmp_path: Path) -> None:
        # compare=False skips every copy, even a brand-new source file.
        src = tmp_path / "src"
        _write(src, "new.txt", b"xx", mtime=_OLDER)
        client, calls = make_recording_client([_listing()])
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            compare=False,
            transfer_config=_SERIAL,
        )
        assert _ops(calls) == ["ListObjectsV2"]

    def test_compare_false_with_delete_is_delete_only(self, tmp_path: Path) -> None:
        # compare=False + delete=True: prune orphans, copy nothing.
        src = tmp_path / "src"
        _write(src, "keep.txt", b"xx", mtime=_OLDER)
        client, calls = make_recording_client([_listing(("p/extra.txt", 2), ("p/keep.txt", 2)), {}])
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            compare=False,
            delete=True,
            transfer_config=_SERIAL,
        )
        assert _ops(calls) == ["ListObjectsV2", "DeleteObjects"]
        keys = [entry["Key"] for entry in calls[1].params["Delete"]["Objects"]]
        assert keys == ["p/extra.txt"]

    def test_destination_directory_is_created_even_when_empty(self, tmp_path: Path) -> None:
        out = tmp_path / "fresh" / "nested"
        client, _calls = make_recording_client([_listing()])
        S3().sync(S3Storage("s3://bucket/d", client=client), str(out), transfer_config=_SERIAL)
        assert out.is_dir()

    def test_delete_removes_local_files_synchronously(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        stale = _write(out, "stale.txt", b"xx")
        nested = _write(out, "sub/stale2.txt", b"xx")
        client, calls = make_recording_client([_listing()])
        results: list[OpResult] = []
        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            str(out),
            delete=True,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert _ops(calls) == ["ListObjectsV2"]
        assert not stale.exists() and not nested.exists()
        assert {(r.transfer_type, r.outcome) for r in results} == {
            (TransferType.DELETE, OpOutcome.SUCCEEDED)
        }
        assert sorted(r.src for r in results if r.src) == [str(stale), str(nested)]

    def test_dryrun_delete_keeps_local_files(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        stale = _write(out, "stale.txt", b"xx")
        client, _calls = make_recording_client([_listing()])
        results: list[OpResult] = []
        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            str(out),
            delete=True,
            dryrun=True,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert stale.exists()
        assert [r.outcome for r in results] == [OpOutcome.DRYRUN]

    def test_local_delete_failure_aggregates_into_batcherror(self, tmp_path: Path) -> None:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root removes anything")
        out = tmp_path / "out"
        stale = _write(out, "locked/stale.txt", b"xx")
        stale.parent.chmod(0o555)
        client, _calls = make_recording_client([_listing()])
        results: list[OpResult] = []
        try:
            with pytest.raises(BatchError) as excinfo:
                S3().sync(
                    S3Storage("s3://bucket/d", client=client),
                    str(out),
                    delete=True,
                    transfer_config=_SERIAL,
                    on_result=results.append,
                )
        finally:
            stale.parent.chmod(0o755)
        assert str(excinfo.value) == "1 of 1 operations failed"
        assert [r.outcome for r in results] == [OpOutcome.FAILED]
        assert "[Errno 13]" in str(results[0].error)

    def test_glacier_source_warns_and_skips(self, tmp_path: Path) -> None:
        listing: dict[str, Any] = {
            "Contents": [
                {
                    "Key": "d/cold.bin",
                    "Size": 2,
                    "LastModified": _MTIME,
                    "ETag": '"e"',
                    "StorageClass": "GLACIER",
                }
            ]
        }
        client, calls = make_recording_client([listing])
        results: list[OpResult] = []
        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            str(tmp_path / "out"),
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert _ops(calls) == ["ListObjectsV2"]
        assert [r.outcome for r in results] == [OpOutcome.WARNED]
        assert "Unable to perform download operations on GLACIER objects" in str(results[0].error)

    def test_case_conflict_gate_guards_only_missing_pairs(self, tmp_path: Path) -> None:
        # Two case twins, both missing at the destination: the first is
        # admitted, the second hits the submitted-set and SKIP emits the
        # aws-cli notice (uncounted; rc untouched).
        out = tmp_path / "out"
        listing = _listing(("d/A.txt", 2), ("d/a.txt", 2))
        client, calls = make_recording_client([listing, _get_response()])
        results: list[OpResult] = []
        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            str(out),
            transfer_config=_CASE_CONFLICT_CONFIG,
            on_result=results.append,
            **TransferOptions(case_conflict=CaseConflictMode.SKIP),
        )
        assert _ops(calls) == ["ListObjectsV2", "GetObject"]
        assert calls[1].params["Key"] == "d/A.txt"
        notices = [r for r in results if r.outcome is OpOutcome.NOTICE]
        assert len(notices) == 1
        assert str(notices[0].error).startswith("warning: Skipping bucket/d/a.txt -> ")


class TestSyncCopy:
    def test_copies_and_deletes_through_the_dest_client(self, tmp_path: Path) -> None:
        src_client, src_calls = make_recording_client([_listing(("s/new.txt", 2))])
        dest_client, dest_calls = make_recording_client([_listing(("t/extra.txt", 2)), {}, {}])
        S3().sync(
            S3Storage("s3://src-b/s", client=src_client),
            S3Storage("s3://dest-b/t", client=dest_client),
            delete=True,
            transfer_config=_SERIAL,
        )
        assert _ops(src_calls) == ["ListObjectsV2"]
        assert _ops(dest_calls) == ["ListObjectsV2", "CopyObject", "DeleteObjects"]
        copy = dest_calls[1].params
        assert copy["CopySource"] == {"Bucket": "src-b", "Key": "s/new.txt"}
        assert copy["Bucket"] == "dest-b"
        assert copy["Key"] == "t/new.txt"
        keys = [entry["Key"] for entry in dest_calls[2].params["Delete"]["Objects"]]
        assert keys == ["t/extra.txt"]

    def test_same_location_sync_is_a_silent_noop(self, tmp_path: Path) -> None:
        # `aws s3 sync s3://b/p s3://b/p` exits 0 silently - every
        # pair is identical, so the judgment skips all of them (no mv-style
        # onto-itself guard exists for sync).
        listing = _listing(("p/a.txt", 2))
        client, calls = make_recording_client([listing, listing])
        storage = S3Storage("s3://bucket/p", client=client)
        results: list[OpResult] = []
        S3().sync(storage, storage, delete=True, transfer_config=_SERIAL, on_result=results.append)
        assert _ops(calls) == ["ListObjectsV2", "ListObjectsV2"]
        assert results == []


class TestParallelCompare:
    """``compare=ParallelCompare(...)`` pools the both-sides (update) decision
    while keeping new pairs and the case-conflict gate on the calling thread, so
    it copies exactly what the bare strategy would - only faster."""

    def test_workers_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="workers must be >= 1"):
            ParallelCompare(_always_copies, workers=0)

    def test_default_workers_inherit_or_fall_back(self) -> None:
        from boto3_s3.s3 import _compare_workers

        assert _compare_workers(None) == 10
        assert _compare_workers(TransferConfig(max_concurrency=7)) == 7

    def test_cancel_token_aborts_the_pooled_path(self, tmp_path: Path) -> None:
        # docs/sync.md: cancel_token is polled between pairs on the pooled
        # (ParallelCompare) dispatch too, not only on the serial loop.
        src = tmp_path / "src"
        _write(src, "a.txt", b"xx")
        client, calls = make_recording_client([_listing()])
        token = CancelToken()
        token.cancel()
        with pytest.raises(CancelledError):
            S3().sync(
                str(src),
                S3Storage("s3://bucket/p", client=client),
                compare=ParallelCompare(_always_copies, workers=2),
                transfer_config=_SERIAL,
                cancel_token=token,
            )
        assert not [call for call in calls if call.operation == "PutObject"]

    def test_matches_the_bare_strategy_decisions(self, tmp_path: Path) -> None:
        # Update pairs (a/b/c already at the destination) are pooled; new.txt is
        # new. The pool only changes WHERE decide runs, not WHAT it decides.
        src = tmp_path / "src"
        for name in ("a.txt", "b.txt", "c.txt", "new.txt"):
            _write(src, name, b"xx")

        def decide(pair: SyncPair) -> bool:
            return pair.dest is None or pair.key in {"a.txt", "c.txt"}

        listing = _listing(("p/a.txt", 2), ("p/b.txt", 2), ("p/c.txt", 2))
        client, calls = make_recording_client([listing, {}, {}, {}])
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            compare=ParallelCompare(decide, workers=4),
            transfer_config=_SERIAL,
        )
        puts = {call.params["Key"] for call in calls if call.operation == "PutObject"}
        assert puts == {"p/a.txt", "p/c.txt", "p/new.txt"}

    def test_new_pairs_decided_on_calling_thread_in_key_order(self, tmp_path: Path) -> None:
        # New (destination-missing) pairs are decided inline, in compare-key
        # order, so the case-conflict gate's "first key wins" stays deterministic
        # even under a parallel compare.
        src = tmp_path / "src"
        for name in ("n1.txt", "n2.txt", "n3.txt"):
            _write(src, name, b"xx")
        main = threading.get_ident()
        seen: list[str] = []

        def decide(pair: SyncPair) -> bool:
            assert pair.dest is None  # an empty listing leaves every pair new
            assert threading.get_ident() == main
            seen.append(pair.key)
            return False  # copy nothing; the test only pins the call order

        client, _calls = make_recording_client([_listing()])
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            compare=ParallelCompare(decide, workers=4),
            transfer_config=_SERIAL,
        )
        assert seen == ["n1.txt", "n2.txt", "n3.txt"]

    def test_inner_exception_aborts_the_sync(self, tmp_path: Path) -> None:
        # A decision that raises aborts the sync (as the serial path does); the
        # exception surfaces when its pooled result is consumed.
        src = tmp_path / "src"
        _write(src, "x.txt", b"xx")

        def boom(_pair: SyncPair) -> bool:
            raise ValueError("decide blew up")

        client, _calls = make_recording_client([_listing(("p/x.txt", 2))])
        with pytest.raises(ValueError, match="decide blew up"):
            S3().sync(
                str(src),
                S3Storage("s3://bucket/p", client=client),
                compare=ParallelCompare(boom, workers=2),
                transfer_config=_SERIAL,
            )
