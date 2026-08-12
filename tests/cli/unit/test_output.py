"""Unit tests for boto3_s3_cli.output (``aws s3 ls`` formatting, ``uni_write``).

Every timestamp expectation here is a line the pinned aws-cli actually printed
for the same response and the same ``TZ`` (captured against a fake S3 that can
serve a chosen ``LastModified`` / ``CreationDate``, which no real endpoint
allows), so the assertions are aws bytes rather than a second implementation of
the same formatting.
"""

from __future__ import annotations

import datetime as dt
import io
import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from boto3_s3 import FileKind, S3FileInfo
from boto3_s3_cli import output

if TYPE_CHECKING:
    from collections.abc import Generator

_MTIME = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)

# The listing timestamp is rendered in the *local* zone, so pinning aws's bytes
# means pinning the zone. Windows has no ``time.tzset``, and without it a
# changed ``TZ`` never reaches the C library the conversion reads.
needs_tzset = pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="setting TZ mid-process requires time.tzset (POSIX only)"
)


@contextmanager
def local_zone(zone: str) -> Generator[None, None, None]:
    """Run the block with ``TZ`` set to ``zone`` (skipping if the host lacks it)."""
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        # A zone the host has no tzdata entry for silently degrades to UTC,
        # which would compare our output against the wrong aws bytes.
        if zone != "UTC" and time.tzname[0] in {"UTC", "GMT"}:
            pytest.skip(f"host has no tzdata entry for {zone}")
        yield
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


def _at(iso: str) -> dt.datetime:
    """The instant a fake S3 served as ``LastModified`` / ``CreationDate``."""
    return dt.datetime.fromisoformat(iso).replace(tzinfo=dt.timezone.utc)


class _StrictAsciiStream(io.StringIO):
    """StringIO that raises like an ascii-configured console on non-ASCII writes."""

    encoding = "ascii"

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0

    def write(self, text: str) -> int:
        text.encode("ascii")  # raises UnicodeEncodeError on non-ASCII
        return super().write(text)

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


class TestUniWrite:
    def test_unencodable_text_falls_back_to_replace_and_flushes(self) -> None:
        # aws-cli's uni_print: a stream whose encoding cannot represent the
        # text (a non-ASCII key on an ascii console/pipe) is re-tried with
        # errors='replace' instead of raising - one unencodable key must
        # never abort a listing or delete run mid-way.
        stream = _StrictAsciiStream()
        output.uni_write(stream, "delete: s3://b/名前.txt\n")
        assert stream.getvalue() == "delete: s3://b/??.txt\n"
        assert stream.flushes == 1

    def test_encodable_text_is_written_verbatim(self) -> None:
        stream = _StrictAsciiStream()
        output.uni_write(stream, "delete: s3://b/plain.txt\n")
        assert stream.getvalue() == "delete: s3://b/plain.txt\n"
        assert stream.flushes == 1


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

    @needs_tzset
    def test_bucket_renders_date_and_name_only(self) -> None:
        info = S3FileInfo(key="mexico-dst", kind=FileKind.BUCKET, mtime=_at("2022-07-01T12:00:00"))
        with local_zone("UTC"):
            line = output.format_entry(info, recursive=False, human_readable=False)
        assert line == "2022-07-01 12:00:00 mexico-dst"  # no size column


class TestListingTimestamp:
    """The 19-column stamp: aws's zone conversion and its field-built padding.

    aws converts with dateutil's ``tzlocal``, which snapshots the offsets in
    effect *now* (``time.timezone`` / ``time.altzone``) and looks the DST flag up
    per instant. So in a zone whose rules changed, an object older than the
    change is rendered with today's offset - while a zone that still observes DST
    keeps switching, historically. The expectations below are the lines the
    pinned aws-cli printed for these very instants, and they discriminate: a
    conversion using the historical rule (plain ``astimezone``) gets Almaty and
    Nuuk 2013 wrong, and one pinned to today's standard offset gets the two
    summer lines wrong.
    """

    # (TZ, LastModified served, aws's line) for a 1-byte object listed
    # non-recursively, so the name column is the key's basename.
    @needs_tzset
    @pytest.mark.parametrize(
        ("zone", "iso", "expected"),
        [
            # Mexico abolished DST in 2022-10: everything renders at -6.
            ("America/Mexico_City", "2022-07-01T12:00:00", "2022-07-01 06:00:00          1 stamp"),
            ("America/Mexico_City", "2013-07-15T12:00:00", "2013-07-15 06:00:00          1 stamp"),
            # Kazakhstan moved +6 -> +5 in 2024-03; Brazil dropped DST in 2019.
            ("Asia/Almaty", "2013-01-15T12:00:00", "2013-01-15 17:00:00          1 stamp"),
            ("America/Sao_Paulo", "2018-12-20T12:00:00", "2018-12-20 09:00:00          1 stamp"),
            # Greenland moved -3/-2 to -2/-1 in 2023 but kept DST, so the flag
            # is still per-instant while the offsets are today's.
            ("America/Nuuk", "2013-01-15T12:00:00", "2013-01-15 10:00:00          1 stamp"),
            ("America/Nuuk", "2013-07-15T12:00:00", "2013-07-15 11:00:00          1 stamp"),
            # Controls: zones whose rules never changed render historically too.
            ("Europe/Paris", "2013-01-15T12:00:00", "2013-01-15 13:00:00          1 stamp"),
            ("Europe/Paris", "2013-07-15T12:00:00", "2013-07-15 14:00:00          1 stamp"),
            ("UTC", "2013-01-15T12:00:00", "2013-01-15 12:00:00          1 stamp"),
        ],
    )
    def test_zone_conversion_matches_aws(self, zone: str, iso: str, expected: str) -> None:
        info = S3FileInfo(key="stamps/stamp", size=1, mtime=_at(iso))
        with local_zone(zone):
            line = output.format_entry(info, recursive=False, human_readable=False)
        assert line == expected

    # (key, size, LastModified served, aws's line), TZ=UTC. A year below 1000
    # is left-justified into 19 columns so the size column never moves.
    @needs_tzset
    @pytest.mark.parametrize(
        ("key", "size", "iso", "expected"),
        [
            ("y0001", 1, "0001-06-15T12:00:00", "1-06-15 12:00:00             1 y0001"),
            ("y0042", 22, "0042-06-15T12:00:00", "42-06-15 12:00:00           22 y0042"),
            ("y0999", 333, "0999-06-15T12:00:00", "999-06-15 12:00:00         333 y0999"),
            ("y1000", 4444, "1000-06-15T12:00:00", "1000-06-15 12:00:00       4444 y1000"),
            ("y2026", 55555, "2026-08-12T00:00:00", "2026-08-12 00:00:00      55555 y2026"),
        ],
    )
    def test_short_year_keeps_the_size_column_in_place(
        self, key: str, size: int, iso: str, expected: str
    ) -> None:
        info = S3FileInfo(key=f"shortyear/{key}", size=size, mtime=_at(iso))
        with local_zone("UTC"):
            line = output.format_entry(info, recursive=False, human_readable=False)
        assert line == expected

    @needs_tzset
    @pytest.mark.parametrize(
        ("key", "size", "iso", "expected"),
        [
            ("y0001", 1, "0001-06-15T12:00:00", "1-06-15 12:00:00        1 Byte y0001"),
            ("y0999", 333, "0999-06-15T12:00:00", "999-06-15 12:00:00   333 Bytes y0999"),
            ("y1000", 4444, "1000-06-15T12:00:00", "1000-06-15 12:00:00    4.3 KiB y1000"),
        ],
    )
    def test_short_year_with_human_readable_sizes(
        self, key: str, size: int, iso: str, expected: str
    ) -> None:
        info = S3FileInfo(key=f"shortyear/{key}", size=size, mtime=_at(iso))
        with local_zone("UTC"):
            line = output.format_entry(info, recursive=False, human_readable=True)
        assert line == expected

    # Bucket lines carry CreationDate through the same aws helper, so both the
    # zone conversion and the padding show up in the all-buckets listing.
    @needs_tzset
    @pytest.mark.parametrize(
        ("zone", "name", "iso", "expected"),
        [
            ("UTC", "y0999", "0999-06-15T12:00:00", "999-06-15 12:00:00  y0999"),
            ("UTC", "y1000", "1000-06-15T12:00:00", "1000-06-15 12:00:00 y1000"),
            (
                "America/Nuuk",
                "nuuk-summer",
                "2013-07-15T12:00:00",
                "2013-07-15 11:00:00 nuuk-summer",
            ),
            (
                "America/Mexico_City",
                "mexico-dst",
                "2022-07-01T12:00:00",
                "2022-07-01 06:00:00 mexico-dst",
            ),
        ],
    )
    def test_bucket_creation_date_matches_aws(
        self, zone: str, name: str, iso: str, expected: str
    ) -> None:
        info = S3FileInfo(key=name, kind=FileKind.BUCKET, mtime=_at(iso))
        with local_zone(zone):
            line = output.format_entry(info, recursive=False, human_readable=False)
        assert line == expected

    @needs_tzset
    def test_an_absent_timestamp_still_occupies_the_stamp_columns(self) -> None:
        # No S3 listing omits LastModified, but the blank-stamp branch must hold
        # the same 19 columns as a rendered one or the size column would shift.
        stamped = S3FileInfo(key="p/f", size=7, mtime=_at("2026-08-12T00:00:00"))
        blank = S3FileInfo(key="p/f", size=7)
        with local_zone("UTC"):
            with_stamp = output.format_entry(stamped, recursive=False, human_readable=False)
            without = output.format_entry(blank, recursive=False, human_readable=False)
        assert with_stamp == "2026-08-12 00:00:00          7 f"
        assert without == f"{' ' * 19}          7 f"


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
