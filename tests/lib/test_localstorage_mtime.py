"""`boto3_s3.localstorage._size_mtime`: which local mtimes count as representable.

The mtime is read in UTC: only a value outside `datetime`'s range (or the
platform's `gmtime`) falls back to the epoch. aws-cli renders it in the local
zone and falls back wherever that fails - also within the zone's offset of
either end of the range. Such a value is kept here
(docs/cli/aws-differences.md), so the cases set a local zone to show it plays
no part.

The stat results are synthesized so the cases hold on any filesystem (a real
one clamps or refuses these mtimes).
"""

from __future__ import annotations

import contextlib
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pytest

from boto3_s3.localstorage import _size_mtime

if TYPE_CHECKING:
    from collections.abc import Generator

# Every case needs the process's local zone set, which needs tzset (POSIX).
pytestmark = pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="setting the local zone needs time.tzset"
)

_UTC_MAX_SECOND = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
_UTC_MIN = datetime(1, 1, 1, tzinfo=timezone.utc)


@contextlib.contextmanager
def _local_zone(name: str) -> Generator[None, None, None]:
    """Run the body with the process's local zone set to `name`.

    Not `monkeypatch.setenv`: restoring `TZ` only takes effect once `tzset` has
    re-read it, and a fixture's teardown runs before monkeypatch's undo.
    """
    before = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = before
        time.tzset()


def _stat(mtime: float, size: int = 7) -> os.stat_result:
    return os.stat_result((0o100644, 1, 1, 1, 0, 0, size, 0, 0, 0), {"st_mtime": mtime})


# (zone, st_mtime, expected mtime or None).
_CASES = [
    ("UTC", 253402300799, _UTC_MAX_SECOND),
    ("UTC", 253402300800, None),
    ("UTC", -62135596800, _UTC_MIN),
    ("UTC", -62135596801, None),
    # aws-cli falls back to the epoch for these (measured on aws-cli 2.36.40): the
    # local wall clock leaves datetime's range, UTC does not.
    ("Asia/Tokyo", 253402300799, _UTC_MAX_SECOND),
    ("America/New_York", -62135596800, _UTC_MIN),
    ("Asia/Tokyo", 1700000000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)),
]


@pytest.mark.parametrize(("zone", "mtime", "expected"), _CASES)
def test_the_mtime_is_read_in_utc(zone: str, mtime: int, expected: datetime | None) -> None:
    with _local_zone(zone):
        assert _size_mtime(_stat(mtime)) == (7, expected)


def test_a_representable_mtime_keeps_its_fraction() -> None:
    with _local_zone("Asia/Tokyo"):
        assert _size_mtime(_stat(1700000000.5)) == (
            7,
            datetime(2023, 11, 14, 22, 13, 20, 500000, tzinfo=timezone.utc),
        )
