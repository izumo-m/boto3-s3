"""An unsorted side of a sync: the merge-join's order guard.

``Comparator.compare`` merge-joins two streams that must ascend by compare
key. A side that descends - a custom backend that declares ``SORTABLE_SCAN``
and breaks the promise, or an S3-compatible endpoint whose ``ListObjectsV2``
does not sort (real S3 and MinIO both do) - would mis-pair, and the delete
lane would then remove entries that exist on *both* sides. aws-cli keeps
merging on such a stream and does exactly that (measured against a listing
mutated in flight): it deletes a both-sides key right before re-copying it.
That is silent data loss, so this is a deliberate divergence - the guard kills
the run at the first descent with a library ``ValidationError``, which the CLI
renders as one ``fatal error:`` line at rc 1.

The pins here: the error is a real classified exception (not the ``assert``
this used to be, which escaped the CLI as a bare ``AssertionError`` and a raw
traceback - `finish_transfer` re-raises that class by design), it names the
offending side and key pair, it survives ``python -O``, and nothing is
transferred or deleted after the descent is seen.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3.comparator import Comparator, MergedPair
from boto3_s3.exceptions import ValidationError
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


def _pairs(src: list[tuple[str, FileInfo]], dest: list[tuple[str, FileInfo]]) -> list[MergedPair]:
    return list(Comparator(_KIND).compare(iter(src), iter(dest)))


def _write(root: Path, rel: str, body: bytes, *, mtime: datetime) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    os.utime(target, (mtime.timestamp(), mtime.timestamp()))


class TestOrderGuardError:
    """The failure itself: classified, exact, and self-describing."""

    def test_descending_source_key_is_a_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _pairs(_entries("b", "a"), _entries("a", "b"))
        message = str(excinfo.value)
        assert "source sync stream is not byte-ordered by compare_key" in message
        # Both halves of the violating pair are named, in the order seen.
        assert "'b' then 'a'" in message

    def test_descending_destination_key_is_a_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _pairs(_entries("a", "b"), _entries("c", "a"))
        message = str(excinfo.value)
        assert "destination sync stream is not byte-ordered by compare_key" in message
        assert "'c' then 'a'" in message

    def test_error_class_is_exactly_validation_error(self) -> None:
        # pytest.raises accepts subclasses, so the class is pinned by identity:
        # a refinement (InvalidValueError, an rc-255 CLI mapping) would change
        # the exit code this failure reports outside the transfer span.
        with pytest.raises(ValidationError) as excinfo:
            _pairs(_entries("b", "a"), _entries("a"))
        assert type(excinfo.value) is ValidationError

    def test_error_is_not_an_assertion_error(self) -> None:
        # The CLI's transfer seam re-raises AssertionError untouched (an
        # internal-invariant bug crashes loudly), so this failure must not be
        # one: it has to reach the `fatal error:` translation instead.
        with pytest.raises(ValidationError) as excinfo:
            _pairs(_entries("b", "a"), _entries("a"))
        assert not isinstance(excinfo.value, AssertionError)


class TestOrderGuardBoundaries:
    """What the guard must *not* reject, and how far it lets a run get."""

    def test_equal_consecutive_keys_are_not_a_descent(self) -> None:
        # Only a strict descent is a violation: a backend repeating a key stays
        # the documented pair-per-occurrence case, not a failure.
        pairs = _pairs(_entries("a", "a", "b"), _entries("a"))
        assert [pair.compare_key for pair in pairs] == ["a", "a", "b"]

    def test_ordered_streams_pair_untouched(self) -> None:
        pairs = _pairs(_entries("a", "b", "c"), _entries("a", "c"))
        assert [type(pair).__name__ for pair in pairs] == [
            "SyncPair",
            "SrcOnlyPair",
            "SyncPair",
        ]

    def test_pairs_before_the_descent_survive_and_the_rest_is_unread(self) -> None:
        # Detection is as late as the descent itself: pairs already yielded
        # stand (the caller acted on them), the offending entry is never paired,
        # and nothing past it is pulled from the input.
        src = iter(_entries("a", "b", "a", "z"))
        stream = Comparator(_KIND).compare(src, iter(_entries("a", "b")))
        assert [next(stream).compare_key for _ in range(2)] == ["a", "b"]
        with pytest.raises(ValidationError):
            next(stream)
        assert [key for key, _info in src] == ["z"], "the stream was read past the descent"

    def test_guard_is_not_compiled_out_under_python_dash_o(self) -> None:
        # It used to be an `assert` behind `if __debug__`, so `-O` removed it
        # and the mis-pairing went silent. Asserts are gone in the child, so it
        # reports through stdout rather than assert.
        script = textwrap.dedent("""
            from boto3_s3.comparator import Comparator
            from boto3_s3.exceptions import ValidationError
            from boto3_s3.types import FileInfo, TransferType

            src = [(k, FileInfo(key=k)) for k in ("b", "a")]
            dest = [("a", FileInfo(key="a"))]
            try:
                list(Comparator(TransferType.UPLOAD).compare(iter(src), iter(dest)))
            except ValidationError as exc:
                if "not byte-ordered" in str(exc):
                    print("GUARD-ACTIVE")
            """)
        done = subprocess.run(
            [sys.executable, "-O", "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert "GUARD-ACTIVE" in done.stdout


class TestUnsortedListingStopsTheRun:
    """``S3.sync``: an unsorted listing acts on nothing more once seen."""

    def test_unsorted_s3_source_stops_the_delete_lane(self, tmp_path: Path) -> None:
        # The measured shape (a proxy reversing ListObjectsV2 Contents): the
        # reversed source makes the still-present k1/k3 look destination-only,
        # so they are deleted before the second source pull reveals the descent.
        # Everything after that must not happen - no k2/k4 download, and the
        # genuine orphan (zold) is not deleted either.
        out = tmp_path / "out"
        for name in ("k1.txt", "k3.txt"):
            _write(out, name, b"xx", mtime=MTIME - timedelta(hours=1))
        _write(out, "zold.txt", b"stale", mtime=MTIME - timedelta(hours=1))
        page = listing(
            ("d/k5.txt", 7), ("d/k4.txt", 7), ("d/k3.txt", 7), ("d/k2.txt", 7), ("d/k1.txt", 7)
        )
        client, calls = make_recording_client([page, get_response()])
        with pytest.raises(ValidationError) as excinfo:
            S3().sync(
                S3Storage("s3://bucket/d", client=client),
                str(out),
                delete_filter=True,
                transfer_config=_SERIAL,
            )
        assert "source sync stream is not byte-ordered" in str(excinfo.value)
        assert ops(calls) == ["ListObjectsV2", "GetObject"]
        assert calls[1].params["Key"] == "d/k5.txt"
        assert sorted(path.name for path in out.iterdir()) == ["k5.txt", "zold.txt"]

    def test_unsorted_s3_destination_never_deletes_a_both_sides_key(self, tmp_path: Path) -> None:
        # aws's loss shape, from the destination side: with the descent ignored,
        # the merge drains the destination as orphans and deletes `a.txt` - a key
        # the source still holds and just uploaded. The unflushed delete batch is
        # abandoned by the abort, so nothing reaches the wire.
        src = tmp_path / "src"
        _write(src, "a.txt", b"xx", mtime=MTIME - timedelta(hours=1))
        page = listing(("d/m.txt", 2), ("d/a.txt", 2))
        client, calls = make_recording_client([page, {}])
        with pytest.raises(ValidationError) as excinfo:
            S3().sync(
                str(src),
                S3Storage("s3://bucket/d", client=client),
                delete_filter=True,
                transfer_config=_SERIAL,
            )
        assert "destination sync stream is not byte-ordered" in str(excinfo.value)
        assert ops(calls) == ["ListObjectsV2", "PutObject"]
