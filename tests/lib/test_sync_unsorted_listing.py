"""An unsorted side of a sync: merged the way aws-cli merges it.

``Comparator.compare`` merge-joins two streams that are expected to ascend by
compare key. A side that does not - an S3-compatible endpoint whose
``ListObjectsV2`` does not sort (real S3 and MinIO both do), or a custom
backend that declares ``SORTABLE_SCAN`` and breaks the promise - is merged all
the same, one comparison of the two entries in hand per step, exactly as
aws-cli's ``Comparator.call`` merges it. The mis-pairing that follows is
aws-cli's own: with ``--delete`` it deletes from the destination a key the
source has too and copies it again (measured against a listing mutated in
flight).

The pins here: the pairing equals a step-for-step port of aws-cli's merge on
arbitrary unordered input, and ``S3.sync`` then acts on every pair - both
loss shapes, from either side.
"""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3.comparator import Comparator, DestOnlyPair, MergedPair, SrcOnlyPair, SyncPair
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import FileInfo, TransferType
from tests.utils.fakes3 import MTIME, get_response, listing
from tests.utils.recorder import make_recording_client, ops

_SERIAL = TransferConfig(use_threads=False)
_KIND = TransferType.UPLOAD  # pairing is direction-agnostic
_TIME = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _entries(*keys: str) -> list[tuple[str, FileInfo]]:
    return [(key, FileInfo(key=key, size=10, mtime=_TIME)) for key in keys]


def _shapes(pairs: list[MergedPair]) -> list[tuple[str, str]]:
    names = {SrcOnlyPair: "src", SyncPair: "both", DestOnlyPair: "dest"}
    return [(names[type(pair)], pair.compare_key) for pair in pairs]


def _aws_merge(src: list[str], dest: list[str]) -> list[tuple[str, str]]:
    """aws-cli's ``Comparator.call`` state machine, step for step.

    Each step takes a fresh entry from a side only when the previous step
    consumed that side's entry, compares the two in hand, and reports one
    shape; once a side runs dry the other drains.
    """
    out: list[tuple[str, str]] = []
    src_iter, dest_iter = iter(src), iter(dest)
    src_done = dest_done = False
    src_take = dest_take = True
    src_key = dest_key = ""
    while True:
        if not src_done and src_take:
            try:
                src_key = next(src_iter)
            except StopIteration:
                src_done = True
        if not dest_done and dest_take:
            try:
                dest_key = next(dest_iter)
            except StopIteration:
                dest_done = True
        if not src_done and not dest_done:
            src_take = dest_take = True
            if src_key == dest_key:
                out.append(("both", src_key))
            elif src_key < dest_key:
                dest_take = False
                out.append(("src", src_key))
            else:
                src_take = False
                out.append(("dest", dest_key))
        elif not src_done:
            src_take = True
            out.append(("src", src_key))
        elif not dest_done:
            dest_take = True
            out.append(("dest", dest_key))
        else:
            return out


def _write(root: Path, rel: str, body: bytes, *, mtime: datetime) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    os.utime(target, (mtime.timestamp(), mtime.timestamp()))


class TestMergeMatchesAwsCli:
    def test_a_descending_source_is_merged_not_refused(self) -> None:
        pairs = list(Comparator(_KIND).compare(iter(_entries("b", "a")), iter(_entries("a", "b"))))
        # The source's "a" arrives after the merge has passed it on the
        # destination side, so it is both destination-only and source-only.
        assert _shapes(pairs) == [("dest", "a"), ("both", "b"), ("src", "a")]

    @pytest.mark.parametrize("seed", range(200))
    def test_unordered_input_pairs_as_aws_clis_merge_pairs_it(self, seed: int) -> None:
        rng = random.Random(seed)
        alphabet = "abcdef"
        src = [rng.choice(alphabet) for _ in range(rng.randint(0, 7))]
        dest = [rng.choice(alphabet) for _ in range(rng.randint(0, 7))]
        pairs = list(Comparator(_KIND).compare(iter(_entries(*src)), iter(_entries(*dest))))
        assert _shapes(pairs) == _aws_merge(src, dest)


class TestUnsortedListingSync:
    """``S3.sync`` acts on every pair an unsorted listing produces."""

    def test_unsorted_s3_source_deletes_then_downloads_again(self, tmp_path: Path) -> None:
        # The measured shape (a proxy reversing ListObjectsV2 Contents): the
        # reversed source makes the local k1 / k3 look destination-only, so
        # they are deleted, and the rest of the source then downloads them
        # back alongside the others; the genuine orphan goes last.
        out = tmp_path / "out"
        for name in ("k1.txt", "k3.txt"):
            _write(out, name, b"xx", mtime=MTIME - timedelta(hours=1))
        _write(out, "zold.txt", b"stale", mtime=MTIME - timedelta(hours=1))
        page = listing(
            ("d/k5.txt", 7), ("d/k4.txt", 7), ("d/k3.txt", 7), ("d/k2.txt", 7), ("d/k1.txt", 7)
        )
        client, calls = make_recording_client([page] + [get_response() for _ in range(5)])
        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            str(out),
            delete_filter=True,
            transfer_config=_SERIAL,
        )
        assert ops(calls) == ["ListObjectsV2"] + ["GetObject"] * 5
        assert [call.params["Key"] for call in calls[1:]] == [
            "d/k5.txt",
            "d/k4.txt",
            "d/k3.txt",
            "d/k2.txt",
            "d/k1.txt",
        ]
        assert sorted(path.name for path in out.iterdir()) == [
            "k1.txt",
            "k2.txt",
            "k3.txt",
            "k4.txt",
            "k5.txt",
        ]

    def test_unsorted_s3_destination_deletes_a_key_the_source_has(self, tmp_path: Path) -> None:
        # aws's loss shape from the destination side: the merge uploads a.txt,
        # then drains the destination as orphans and deletes a.txt as well -
        # a key the source still holds.
        src = tmp_path / "src"
        _write(src, "a.txt", b"xx", mtime=MTIME - timedelta(hours=1))
        page = listing(("d/m.txt", 2), ("d/a.txt", 2))
        client, calls = make_recording_client([page, {}, {}])
        S3().sync(
            str(src),
            S3Storage("s3://bucket/d", client=client),
            delete_filter=True,
            transfer_config=_SERIAL,
        )
        assert ops(calls) == ["ListObjectsV2", "PutObject", "DeleteObjects"]
        assert calls[1].params["Key"] == "d/a.txt"
        assert [entry["Key"] for entry in calls[2].params["Delete"]["Objects"]] == [
            "d/m.txt",
            "d/a.txt",
        ]
