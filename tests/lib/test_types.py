"""Unit tests for boto3_s3.types covering the core-types spec."""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import boto3_s3
import boto3_s3.types as t

DT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

TYPE_NAMES = [
    "CancelMode",
    "CancelToken",
    "FileInfo",
    "FileKind",
    "LocalFileInfo",
    "LocalScanOptions",
    "ListingCallback",
    "S3FileInfo",
    "S3ScanOptions",
    "ScanOptions",
]


class TestCancelToken:
    def test_cancel_defaults_to_graceful(self) -> None:
        token = t.CancelToken()

        assert token.cancelled is False
        assert token.mode is None

        token.cancel()

        assert token.cancelled is True
        assert token.mode is t.CancelMode.GRACEFUL

    def test_cancel_can_request_immediate_shutdown(self) -> None:
        token = t.CancelToken()

        token.cancel(mode=t.CancelMode.IMMEDIATE)

        assert token.cancelled is True
        assert token.mode is t.CancelMode.IMMEDIATE

    def test_immediate_request_escalates_but_never_downgrades(self) -> None:
        token = t.CancelToken()

        token.cancel()
        token.cancel(mode=t.CancelMode.IMMEDIATE)
        token.cancel(mode=t.CancelMode.GRACEFUL)

        assert token.mode is t.CancelMode.IMMEDIATE

    def test_cancel_reenters_while_the_same_thread_holds_the_lock(self) -> None:
        # cancel() is signal-handler-safe by contract, and CPython delivers
        # signal handlers on the main thread: a handler's cancel() must not
        # deadlock when the interrupted frame is inside mode/cancel holding
        # the token lock. Holding it here reproduces that interrupted state.
        token = t.CancelToken()

        with token._lock:  # pyright: ignore[reportPrivateUsage]
            token.cancel(mode=t.CancelMode.IMMEDIATE)

        assert token.mode is t.CancelMode.IMMEDIATE


class TestFileInfo:
    def test_construct_with_all_required_fields(self) -> None:
        fi = t.FileInfo(key="dir/file.txt", size=1024, mtime=DT)
        assert fi.key == "dir/file.txt"
        assert fi.size == 1024
        assert fi.mtime == DT

    def test_fields_are_keyword_only(self) -> None:
        with pytest.raises(TypeError):
            t.FileInfo("k", 1, DT)  # pyright: ignore[reportCallIssue]

    def test_key_is_required(self) -> None:
        with pytest.raises(TypeError):
            t.FileInfo()  # pyright: ignore[reportCallIssue]

    def test_defaults_to_a_file_with_no_size_or_mtime(self) -> None:
        fi = t.FileInfo(key="k")
        assert fi.kind is t.FileKind.FILE
        assert fi.size is None
        assert fi.mtime is None

    def test_directory_kind_entry(self) -> None:
        fi = t.FileInfo(key="dir/", kind=t.FileKind.DIRECTORY)
        assert fi.kind is t.FileKind.DIRECTORY
        assert fi.size is None

    def test_instance_is_mutable(self) -> None:
        fi = t.FileInfo(key="k", size=1, mtime=DT)
        fi.size = 2
        assert fi.size == 2


class TestLocalFileInfo:
    def test_construct_with_stat_result_and_symlink_flag(self, tmp_path: Path) -> None:
        st = os.stat(tmp_path)
        lfi = t.LocalFileInfo(key="d/f", size=10, mtime=DT, stat_result=st, is_symlink=True)
        assert lfi.stat_result is st
        assert lfi.is_symlink is True
        assert lfi.key == "d/f"
        assert lfi.size == 10
        assert lfi.mtime == DT

    def test_defaults_leave_stat_result_none_and_not_symlink(self) -> None:
        lfi = t.LocalFileInfo(key="d/f", size=10, mtime=DT)
        assert lfi.stat_result is None
        assert lfi.is_symlink is False

    def test_is_a_file_info(self) -> None:
        lfi = t.LocalFileInfo(key="d/f", size=10, mtime=DT)
        assert isinstance(lfi, t.FileInfo)


class TestS3FileInfo:
    def test_construct_with_required_fields_only(self) -> None:
        sfi = t.S3FileInfo(key="k", size=10, mtime=DT)
        assert (sfi.key, sfi.size, sfi.mtime) == ("k", 10, DT)
        assert sfi.etag is None
        assert sfi.storage_class is None
        assert sfi.owner is None
        assert sfi.head is None

    def test_construct_with_all_s3_fields(self) -> None:
        sfi = t.S3FileInfo(
            key="k",
            size=10,
            mtime=DT,
            etag="abc123",
            storage_class="STANDARD_IA",
            owner="canonical-id",
            head={"ContentType": "text/plain"},
        )
        assert sfi.etag == "abc123"
        assert sfi.storage_class == "STANDARD_IA"
        assert sfi.owner == "canonical-id"
        assert sfi.head == {"ContentType": "text/plain"}

    def test_is_a_file_info(self) -> None:
        assert isinstance(t.S3FileInfo(key="k", size=10, mtime=DT), t.FileInfo)

    def test_head_can_be_set_after_construction(self) -> None:
        sfi = t.S3FileInfo(key="k", size=10, mtime=DT)
        sfi.head = {"ContentType": "text/plain"}
        assert sfi.head == {"ContentType": "text/plain"}


class TestScanOptions:
    """The backend-agnostic base: only the knobs every backend honors."""

    def test_common_defaults(self) -> None:
        opts = t.ScanOptions()
        assert opts.recursive is False
        assert opts.sort is False
        assert opts.filter is None
        assert opts.on_warning is None

    def test_filter_carries_a_predicate(self) -> None:
        def keep(info: t.FileInfo) -> bool:
            return info.key.endswith(".txt")

        assert t.ScanOptions(filter=keep).filter is keep

    def test_is_frozen(self) -> None:
        opts = t.ScanOptions()
        with pytest.raises(AttributeError):
            opts.recursive = True  # pyright: ignore[reportAttributeAccessIssue]


class TestS3ScanOptions:
    def test_defaults_and_inherits_common(self) -> None:
        opts = t.S3ScanOptions()
        assert isinstance(opts, t.ScanOptions)  # a ScanOptions subclass
        assert (opts.page_size, opts.request_payer, opts.fetch_owner, opts.prefix) == (
            1000,
            None,
            False,
            None,
        )
        assert opts.recursive is False  # inherited common field

    def test_carries_all_values(self) -> None:
        opts = t.S3ScanOptions(
            recursive=True,
            page_size=50,
            request_payer="requester",
            fetch_owner=True,
            prefix="p/",
        )
        assert (
            opts.recursive,
            opts.page_size,
            opts.request_payer,
            opts.fetch_owner,
            opts.prefix,
        ) == (True, 50, "requester", True, "p/")

    def test_page_size_passes_through_unvalidated(self) -> None:
        # aws-cli parity (exit-code charter): out-of-range values reach the
        # server, which decides (0 lists nothing, negative -> InvalidArgument).
        assert t.S3ScanOptions(page_size=0).page_size == 0
        assert t.S3ScanOptions(page_size=-1).page_size == -1


class TestLocalScanOptions:
    def test_defaults_and_inherits_common(self) -> None:
        opts = t.LocalScanOptions()
        assert isinstance(opts, t.ScanOptions)
        assert opts.follow_symlinks is True
        assert opts.detect_symlink_loops is False
        assert opts.sort is False  # inherited common field

    def test_carries_all_values(self) -> None:
        opts = t.LocalScanOptions(recursive=True, follow_symlinks=False, detect_symlink_loops=True)
        assert (opts.recursive, opts.follow_symlinks, opts.detect_symlink_loops) == (
            True,
            False,
            True,
        )


class TestPublicReExport:
    @pytest.mark.parametrize("name", TYPE_NAMES)
    def test_top_level_identity_matches_module(self, name: str) -> None:
        # Identity implies existence, so this also covers "re-exported at all".
        assert getattr(boto3_s3, name) is getattr(t, name)

    def test_all_enumerates_public_types(self) -> None:
        assert set(TYPE_NAMES) <= set(boto3_s3.__all__)
        assert set(TYPE_NAMES) <= set(t.__all__)
