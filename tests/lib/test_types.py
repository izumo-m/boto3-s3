"""Unit tests for boto3_s3.types covering the core-types spec."""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import boto3_s3
import boto3_s3.types as t

DT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

TYPE_NAMES = ["FileInfo", "FileKind", "LocalFileInfo", "S3FileInfo", "ScanOptions"]


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
    def test_defaults(self) -> None:
        opts = t.ScanOptions()
        assert opts.recursive is False
        assert opts.page_size == 1000
        assert opts.request_payer is None
        assert opts.fetch_owner is False
        assert opts.filter is None

    def test_filter_carries_a_predicate(self) -> None:
        def keep(info: t.FileInfo) -> bool:
            return info.key.endswith(".txt")

        assert t.ScanOptions(filter=keep).filter is keep

    def test_carries_all_values(self) -> None:
        opts = t.ScanOptions(
            recursive=True, page_size=50, request_payer="requester", fetch_owner=True
        )
        assert (opts.recursive, opts.page_size, opts.request_payer, opts.fetch_owner) == (
            True,
            50,
            "requester",
            True,
        )

    def test_is_frozen(self) -> None:
        opts = t.ScanOptions()
        with pytest.raises(AttributeError):
            opts.recursive = True  # pyright: ignore[reportAttributeAccessIssue]

    def test_page_size_passes_through_unvalidated(self) -> None:
        # aws-cli parity (exit-code charter): out-of-range values reach the
        # server, which decides (0 lists nothing, negative -> InvalidArgument).
        assert t.ScanOptions(page_size=0).page_size == 0
        assert t.ScanOptions(page_size=-1).page_size == -1


class TestPublicReExport:
    @pytest.mark.parametrize("name", TYPE_NAMES)
    def test_top_level_import_succeeds(self, name: str) -> None:
        assert hasattr(boto3_s3, name)

    @pytest.mark.parametrize("name", TYPE_NAMES)
    def test_top_level_identity_matches_module(self, name: str) -> None:
        assert getattr(boto3_s3, name) is getattr(t, name)

    def test_all_enumerates_public_types(self) -> None:
        assert set(TYPE_NAMES) <= set(boto3_s3.__all__)
        assert set(TYPE_NAMES) <= set(t.__all__)
