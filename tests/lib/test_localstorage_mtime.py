"""`boto3_s3.localstorage._size_mtime`: which local mtimes count as representable.

aws-cli renders a file's mtime with dateutil's `tzlocal()` and treats a failure
as an invalid timestamp (warn, and compare the file as if it were from the
epoch), so the representable range is the *local* calendar's - UTC's, shifted by
the zone's offset, and one DST step wider still at the low end because dateutil
probes for an ambiguous wall clock. Near either end of `datetime`'s range that
verdict decides an rc (2 vs 0) and which way sync transfers, so it is pinned per
zone here.

The expected verdicts are measured, not derived: every (zone, mtime) row below
is one cell of probes/fixA/p1-mtime-boundary-matrix.sh, which runs
`cp <file> s3://bucket/key --dryrun` on aws 2.36.40 and reads back its rc and
warning. The UTC instants are `date -u -d @<mtime>`.

The stat results are synthesized so the cases hold on any filesystem (a real
one clamps or refuses these mtimes).
"""

from __future__ import annotations

import contextlib
import os
import sys
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


# (zone, st_mtime, expected mtime or None) - one row per measured probe cell.
_HIGH_WALL = [
    # datetime.max is 9999-12-31T23:59:59.999999 of the *local* wall clock.
    ("UTC", 253402300799, _UTC_MAX_SECOND),
    ("UTC", 253402300800, None),
    ("Asia/Tokyo", 253402300799, None),
    ("Asia/Tokyo", 253402268399, datetime(9999, 12, 31, 14, 59, 59, tzinfo=timezone.utc)),
    ("Asia/Tokyo", 253402268400, None),
    ("Pacific/Kiritimati", 253402300799, None),
    ("Pacific/Kiritimati", 253402250399, datetime(9999, 12, 31, 9, 59, 59, tzinfo=timezone.utc)),
    ("Pacific/Kiritimati", 253402250400, None),
    # West of UTC the wall moves the other way: still representable here.
    ("Pacific/Niue", 253402300799, _UTC_MAX_SECOND),
]

_LOW_WALL = [
    ("UTC", -62135596800, _UTC_MIN),
    ("UTC", -62135596801, None),
    ("America/New_York", -62135596800, None),
    # Local 0001-01-01T00:00:00 exactly, and one second below it: both invalid
    # in a DST zone, because dateutil subtracts the DST step from the wall clock
    # to test for an ambiguous time and that underflows.
    ("America/New_York", -62135578800, None),
    ("America/New_York", -62135578801, None),
    ("Pacific/Kiritimati", -62135596800, _UTC_MIN),
]

_QUIET_MIDDLE = [
    ("UTC", 1700000000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)),
    ("Asia/Tokyo", 1700000000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)),
    ("America/New_York", 0, datetime(1970, 1, 1, tzinfo=timezone.utc)),
    ("Asia/Tokyo", -1, datetime(1969, 12, 31, 23, 59, 59, tzinfo=timezone.utc)),
]


@pytest.mark.parametrize(("zone", "mtime", "expected"), _HIGH_WALL + _LOW_WALL + _QUIET_MIDDLE)
def test_representability_follows_the_local_zone(
    zone: str, mtime: int, expected: datetime | None
) -> None:
    with _local_zone(zone):
        assert _size_mtime(_stat(mtime)) == (7, expected)


def test_a_representable_mtime_keeps_its_fraction() -> None:
    with _local_zone("Asia/Tokyo"):
        assert _size_mtime(_stat(1700000000.5)) == (
            7,
            datetime(2023, 11, 14, 22, 13, 20, 500000, tzinfo=timezone.utc),
        )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows has no check-free range")
def test_an_ordinary_mtime_costs_no_local_zone_work(monkeypatch: pytest.MonkeyPatch) -> None:
    # The derivation runs once per walked file, so away from the walls it must
    # not touch dateutil at all (where the two zones cannot disagree anyway).
    def _forbidden(_ts: float) -> bool:
        raise AssertionError("the local zone was consulted for an ordinary mtime")

    monkeypatch.setattr("boto3_s3.localstorage._local_zone_representable", _forbidden)
    with _local_zone("Asia/Tokyo"):
        assert _size_mtime(_stat(1700000000)) == (
            7,
            datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc),
        )
