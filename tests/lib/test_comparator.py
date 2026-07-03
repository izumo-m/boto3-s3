"""``boto3_s3.comparator``: the sync pairing and pair-decision material.

Ports the aws-cli semantics into the split design (pure pairing +
``PairFilter`` judgments): aws-cli ``tests/unit/customizations/s3/
test_comparator.py`` becomes the ``Comparator`` pairing matrix (their
strategy-call assertions become filter-application cases), and the
``syncstrategy`` unit tests (``test_base`` / ``test_sizeonly`` /
``test_exacttimestamps``) become the ``compare_size_time`` matrix -
including the aws-cli's direction asymmetry: a same-size download syncs
only when the *local* side is newer, which ``exact_timestamps`` tightens
to exact equality. (``no_overwrite`` is an orthogonal ``S3.sync``
write-guard, exercised in ``test_s3_sync``.)
"""

from __future__ import annotations

import functools
from datetime import datetime, timedelta, timezone

import pytest

from boto3_s3.comparator import (
    Comparator,
    SyncPair,
    all_of,
    any_of,
    compare_size_time,
)
from boto3_s3.types import FileInfo, TransferType

_TIME = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_KIND = TransferType.UPLOAD  # pairing is direction-agnostic; any transfer kind stamps the pairs


def _info(key: str = "k", *, size: int | None = 10, mtime: datetime | None = _TIME) -> FileInfo:
    return FileInfo(key=key, size=size, mtime=mtime)


def _entries(*keys: str) -> list[tuple[str, FileInfo]]:
    return [(key, _info(key)) for key in keys]


def _pairs(src: list[tuple[str, FileInfo]], dest: list[tuple[str, FileInfo]]) -> list[SyncPair]:
    return list(Comparator(_KIND).compare(iter(src), iter(dest)))


class TestComparatorPairing:
    """The pure merge: every key on either side surfaces exactly once."""

    def test_equal_keys_pair_both_sides(self) -> None:
        src = _entries("a.txt")
        dest = _entries("a.txt")
        pairs = _pairs(src, dest)
        assert pairs == [SyncPair(key="a.txt", transfer_type=_KIND, src=src[0][1], dest=dest[0][1])]

    def test_source_only_key_yields_src_pair(self) -> None:
        # aws-cli's test_compare_key_less: 'b...' sorts before 'c...', so the
        # source-only entry surfaces first, then the dest-only one.
        src = _entries("bomparator_test.py")
        dest = _entries("comparator_test.py")
        pairs = _pairs(src, dest)
        assert pairs == [
            SyncPair(key="bomparator_test.py", transfer_type=_KIND, src=src[0][1]),
            SyncPair(key="comparator_test.py", transfer_type=_KIND, dest=dest[0][1]),
        ]

    def test_dest_only_key_yields_dest_pair(self) -> None:
        # aws-cli's test_compare_key_greater: the dest-only entry sorts first.
        src = _entries("domparator_test.py")
        dest = _entries("comparator_test.py")
        pairs = _pairs(src, dest)
        assert pairs == [
            SyncPair(key="comparator_test.py", transfer_type=_KIND, dest=dest[0][1]),
            SyncPair(key="domparator_test.py", transfer_type=_KIND, src=src[0][1]),
        ]

    def test_empty_src_flushes_dest(self) -> None:
        dest = _entries("a", "b")
        assert _pairs([], dest) == [
            SyncPair(key="a", transfer_type=_KIND, dest=dest[0][1]),
            SyncPair(key="b", transfer_type=_KIND, dest=dest[1][1]),
        ]

    def test_empty_dest_flushes_src(self) -> None:
        src = _entries("a", "b")
        assert _pairs(src, []) == [
            SyncPair(key="a", transfer_type=_KIND, src=src[0][1]),
            SyncPair(key="b", transfer_type=_KIND, src=src[1][1]),
        ]

    def test_both_empty_yields_nothing(self) -> None:
        assert _pairs([], []) == []

    def test_interleaved_streams_merge_in_key_order(self) -> None:
        src = _entries("a", "c", "d", "f")
        dest = _entries("b", "c", "e", "f")
        pairs = _pairs(src, dest)
        assert [(p.key, p.src is not None, p.dest is not None) for p in pairs] == [
            ("a", True, False),
            ("b", False, True),
            ("c", True, True),
            ("d", True, False),
            ("e", False, True),
            ("f", True, True),
        ]

    def test_stamps_the_run_kind_on_every_pair(self) -> None:
        # The direction rides each pair so a filter applies the asymmetric
        # rules without being told the route.
        pairs = list(
            Comparator(TransferType.DOWNLOAD).compare(iter(_entries("a")), iter(_entries("b")))
        )
        assert {p.transfer_type for p in pairs} == {TransferType.DOWNLOAD}

    def test_consumes_streams_lazily(self) -> None:
        # Pairing must stream: the first pair comes out before either side
        # is fully consumed (both sides are paged listings in practice).
        src = iter(_entries("a", "b", "c"))
        dest = iter(_entries("a", "b", "c"))
        stream = Comparator(_KIND).compare(src, dest)
        assert next(stream).key == "a"
        assert next(src, None) is not None, "source stream was drained eagerly"


def _both(transfer_type: TransferType, src: FileInfo, dest: FileInfo, **flags: bool) -> bool:
    return compare_size_time(
        SyncPair(key=src.key, transfer_type=transfer_type, src=src, dest=dest), **flags
    )


class TestCompareSizeTime:
    """aws-cli syncstrategy matrix, by direction and variant flag."""

    def test_source_only_pair_always_copies(self) -> None:
        # aws-cli's MissingFileSync: no destination entry -> transfer.
        for transfer_type in (TransferType.UPLOAD, TransferType.DOWNLOAD, TransferType.COPY):
            assert (
                compare_size_time(SyncPair(key="k", transfer_type=transfer_type, src=_info()))
                is True
            )

    def test_rejects_pair_without_source(self) -> None:
        with pytest.raises(ValueError, match="without a source entry"):
            compare_size_time(SyncPair(key="k", transfer_type=TransferType.UPLOAD, dest=_info()))

    # -- size + last-modified (the default judgment) -----------------------

    def test_size_difference_copies_regardless_of_time(self) -> None:
        src = _info(size=10)
        dest = _info(size=11, mtime=_TIME + timedelta(days=1))
        assert _both(TransferType.UPLOAD, src, dest) is True
        assert _both(TransferType.DOWNLOAD, src, dest) is True

    def test_upload_skips_when_dest_is_newer_or_equal(self) -> None:
        # aws-cli's compare_time upload/copy: delta = dest - src >= 0 -> skip.
        src = _info()
        assert _both(TransferType.UPLOAD, src, _info(mtime=_TIME + timedelta(seconds=1))) is False
        assert _both(TransferType.UPLOAD, src, _info()) is False
        assert _both(TransferType.UPLOAD, src, _info(mtime=_TIME - timedelta(seconds=1))) is True

    def test_copy_uses_the_upload_rule(self) -> None:
        src = _info()
        assert _both(TransferType.COPY, src, _info(mtime=_TIME + timedelta(seconds=1))) is False
        assert _both(TransferType.COPY, src, _info(mtime=_TIME - timedelta(seconds=1))) is True

    def test_download_skips_when_dest_is_older_or_equal(self) -> None:
        # The aws-cli asymmetry: a same-size download syncs only when the
        # LOCAL (destination) side is newer - an S3-side update with the
        # same size is deliberately ignored (--exact-timestamps exists for
        # exactly this).
        src = _info()
        assert _both(TransferType.DOWNLOAD, src, _info(mtime=_TIME - timedelta(seconds=1))) is False
        assert _both(TransferType.DOWNLOAD, src, _info()) is False
        assert _both(TransferType.DOWNLOAD, src, _info(mtime=_TIME + timedelta(seconds=1))) is True

    def test_comparison_keeps_sub_second_precision(self) -> None:
        # aws-cli's total_seconds() is a full-precision float, not whole seconds.
        src = _info()
        newer = _info(mtime=_TIME + timedelta(microseconds=1))
        assert _both(TransferType.UPLOAD, src, newer) is False
        assert _both(TransferType.DOWNLOAD, src, newer) is True

    def test_missing_mtime_counts_as_difference(self) -> None:
        assert _both(TransferType.UPLOAD, _info(mtime=None), _info()) is True
        assert _both(TransferType.UPLOAD, _info(), _info(mtime=None)) is True

    def test_missing_size_counts_as_difference(self) -> None:
        assert _both(TransferType.UPLOAD, _info(size=None), _info(size=None)) is True

    # -- size_only ----------------------------------------------------------

    def test_size_only_ignores_time(self) -> None:
        # aws-cli's SizeOnlySync: equal sizes -> skip even with wildly
        # different update times; differing sizes -> copy.
        src = _info()
        older = _info(mtime=_TIME - timedelta(days=1))
        assert _both(TransferType.UPLOAD, src, older, size_only=True) is False
        assert (
            _both(
                TransferType.DOWNLOAD, src, _info(mtime=_TIME + timedelta(days=1)), size_only=True
            )
            is False
        )
        assert _both(TransferType.UPLOAD, src, _info(size=11), size_only=True) is True

    # -- exact_timestamps ----------------------------------------------------

    def test_exact_timestamps_tightens_downloads_to_equality(self) -> None:
        # aws-cli's ExactTimestampsSync: same size syncs unless times are
        # exactly equal - in either direction of skew.
        src = _info()
        flags = {"exact_timestamps": True}
        assert _both(TransferType.DOWNLOAD, src, _info(), **flags) is False
        assert (
            _both(TransferType.DOWNLOAD, src, _info(mtime=_TIME - timedelta(seconds=1)), **flags)
            is True
        )
        assert (
            _both(TransferType.DOWNLOAD, src, _info(mtime=_TIME + timedelta(seconds=1)), **flags)
            is True
        )

    def test_exact_timestamps_wins_over_size_only(self) -> None:
        # Both flags fill the same aws-cli strategy slot and the override
        # order makes --exact-timestamps win (aws 2.34.53 downloads
        # a same-size, different-time file when both flags are given).
        src = _info()
        stale = _info(mtime=_TIME - timedelta(days=1))
        flags = {"size_only": True, "exact_timestamps": True}
        assert _both(TransferType.DOWNLOAD, src, stale, **flags) is True
        assert (
            _both(TransferType.UPLOAD, src, _info(mtime=_TIME + timedelta(days=1)), **flags)
            is False
        )

    def test_exact_timestamps_leaves_uploads_on_the_default_rule(self) -> None:
        # aws-cli's test_compare_exact_timestamps_diff_age_not_download.
        src = _info()
        newer = _info(mtime=_TIME + timedelta(seconds=1))
        assert _both(TransferType.UPLOAD, src, newer, exact_timestamps=True) is False
        assert _both(TransferType.COPY, src, newer, exact_timestamps=True) is False


def _json_only(pair: SyncPair) -> bool:
    return pair.key.endswith(".json")


class TestCombinators:
    def test_all_of_requires_every_predicate(self) -> None:
        copy_all = functools.partial(compare_size_time, size_only=False, exact_timestamps=False)
        combined = all_of(_json_only, copy_all)
        assert combined(SyncPair(key="a.json", transfer_type=_KIND, src=_info("a.json"))) is True
        assert combined(SyncPair(key="a.txt", transfer_type=_KIND, src=_info("a.txt"))) is False

    def test_any_of_passes_on_first_hit(self) -> None:
        # A visibility-style override composed with the size+time default:
        # one extra rule forces certain keys through.
        base = functools.partial(compare_size_time, size_only=False, exact_timestamps=False)
        combined = any_of(_json_only, base)
        up_to_date = SyncPair(
            key="a.json", transfer_type=_KIND, src=_info("a.json"), dest=_info("a.json")
        )
        assert base(up_to_date) is False
        assert combined(up_to_date) is True
        skipped = SyncPair(
            key="a.txt", transfer_type=_KIND, src=_info("a.txt"), dest=_info("a.txt")
        )
        assert combined(skipped) is False

    def test_empty_combinators_follow_all_any_semantics(self) -> None:
        pair = SyncPair(key="k", transfer_type=_KIND, src=_info())
        assert all_of()(pair) is True
        assert any_of()(pair) is False

    def test_combinators_are_generic_over_the_predicate_argument(self) -> None:
        # The same helpers compose visibility predicates over FileInfo.
        def small(info: FileInfo) -> bool:
            return (info.size or 0) < 100

        def not_tmp(info: FileInfo) -> bool:
            return not info.key.endswith(".tmp")

        keep = all_of(small, not_tmp)
        assert keep(_info("a.txt", size=10)) is True
        assert keep(_info("a.tmp", size=10)) is False
        assert keep(_info("big.txt", size=1000)) is False
