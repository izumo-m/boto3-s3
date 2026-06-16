"""Unit tests for boto3_s3_cli.output (``aws s3 ls`` formatting)."""

from __future__ import annotations

import datetime as dt

from boto3_s3 import FileKind, S3FileInfo
from boto3_s3_cli import output

_MTIME = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)


class TestHumanReadableSize:
    def test_one_byte(self) -> None:
        assert output.human_readable_size(1) == "1 Byte"

    def test_bytes(self) -> None:
        assert output.human_readable_size(10) == "10 Bytes"

    def test_kib(self) -> None:
        assert output.human_readable_size(1024) == "1.0 KiB"

    def test_mib(self) -> None:
        assert output.human_readable_size(1024 * 1024) == "1.0 MiB"


class TestFormatEntry:
    def test_file_non_recursive_uses_basename(self) -> None:
        info = S3FileInfo(key="prefix/file.txt", size=10, mtime=_MTIME)
        line = output.format_entry(info, recursive=False, human_readable=False)
        assert line.endswith(" file.txt")
        assert "prefix/file.txt" not in line
        assert "        10" in line  # size right-justified to width 10

    def test_file_recursive_uses_full_key(self) -> None:
        info = S3FileInfo(key="prefix/sub/file.txt", size=10, mtime=_MTIME)
        line = output.format_entry(info, recursive=True, human_readable=False)
        assert line.endswith(" prefix/sub/file.txt")

    def test_directory_renders_pre(self) -> None:
        info = S3FileInfo(key="prefix/sub/", kind=FileKind.DIRECTORY)
        line = output.format_entry(info, recursive=False, human_readable=False)
        assert line == f"{'PRE':>30} sub/"

    def test_directory_double_slash_matches_aws(self) -> None:
        # aws renders an empty last component (Prefix.split('/')[-2]); a prefix
        # ending in '//' is "PRE /", not rstrip's "PRE a/".
        assert (
            output.format_entry(
                S3FileInfo(key="a//", kind=FileKind.DIRECTORY),
                recursive=False,
                human_readable=False,
            )
            == f"{'PRE':>30} /"
        )
        assert (
            output.format_entry(
                S3FileInfo(key="a/b//", kind=FileKind.DIRECTORY),
                recursive=False,
                human_readable=False,
            )
            == f"{'PRE':>30} /"
        )

    def test_human_readable_size_in_entry(self) -> None:
        info = S3FileInfo(key="f", size=2048, mtime=_MTIME)
        line = output.format_entry(info, recursive=False, human_readable=True)
        assert "2.0 KiB" in line

    def test_bucket_renders_date_and_name_only(self) -> None:
        info = S3FileInfo(key="my-bucket", kind=FileKind.BUCKET, mtime=_MTIME)
        line = output.format_entry(info, recursive=False, human_readable=False)
        date = _MTIME.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        assert line == f"{date} my-bucket"  # no size column


class TestFormatSummary:
    def test_counts_and_total_size(self) -> None:
        text = output.format_summary(3, 4096, human_readable=False)
        assert "Total Objects: 3" in text
        assert "Total Size: 4096" in text


class TestFormatBucketLines:
    # Success lines carry the bucket name, failure lines the original path
    # argument (aws-cli MbCommand/RbCommand print exactly that split).

    def test_make_bucket_uses_bucket_name(self) -> None:
        assert output.format_make_bucket("b") == "make_bucket: b"

    def test_make_bucket_failed_uses_original_path(self) -> None:
        line = output.format_make_bucket_failed("s3://b/k", "boom")
        assert line == "make_bucket failed: s3://b/k boom"

    def test_remove_bucket_uses_bucket_name(self) -> None:
        assert output.format_remove_bucket("b") == "remove_bucket: b"

    def test_remove_bucket_failed_uses_original_path(self) -> None:
        line = output.format_remove_bucket_failed("s3://b", "boom")
        assert line == "remove_bucket failed: s3://b boom"
