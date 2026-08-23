"""``S3.sync(pair_filter=...)``: one callback for every merged pair.

``pair_filter`` replaces the three lane filters with a single
``MergedPairFilter``, so an application that needs one view of everything a run
decides - a journal, a confirmation flow, statistics, any state shared across
lanes - writes one function instead of wiring three. These tests pin what that
one view promises: every pair shape arrives, serially, on the calling thread, in
ascending compare-key order; ``True`` takes the pair's default action and
``False`` takes none (the update lane's own default judgment is *not* consulted);
the delete machinery is live even for a callback that deletes nothing (so
returning ``False`` everywhere is a supported observe-only mode); and the
combinations that would contradict "every pair reaches the callback" are refused
up front.

The three lane filters keep their own coverage in ``test_s3_sync.py``, and the
walk knob this hook switches on is specified in
``test_sync_delete_lazy_walk.py``.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3 import GlobFilter
from boto3_s3.comparator import (
    DestOnlyPair,
    MergedPair,
    ParallelFilter,
    SrcOnlyPair,
    SyncPair,
)
from boto3_s3.exceptions import ValidationError
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import OpOutcome, OpResult, TransferType
from tests.lib.test_s3_cp_open import _MemStorage, _NoDeleteMem
from tests.lib.test_sync_delete_lazy_walk import _ScanRecorder
from tests.utils.fakes3 import MTIME, get_response, listing
from tests.utils.recorder import make_recording_client, ops

if TYPE_CHECKING:
    from datetime import datetime

_SERIAL = TransferConfig(use_threads=False)
_OLDER = MTIME - timedelta(hours=1)
_NEWER = MTIME + timedelta(hours=1)


def _write(root: Path, rel: str, body: bytes, *, mtime: datetime | None = None) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    if mtime is not None:
        os.utime(target, (mtime.timestamp(), mtime.timestamp()))
    return target


class _Journal:
    """A pair_filter that records every pair and answers from a key set.

    ``seen`` is the whole decision stream as ``(pair type, compare key)``, in
    call order; ``threads`` collects the thread each call ran on. ``accept`` is
    the set of compare keys answered ``True`` - ``None`` accepts everything.
    """

    def __init__(self, accept: set[str] | None = None) -> None:
        self.seen: list[tuple[type[MergedPair], str]] = []
        self.threads: set[int] = set()
        self._accept = accept

    def __call__(self, pair: MergedPair) -> bool:
        self.threads.add(threading.get_ident())
        self.seen.append((type(pair), pair.compare_key))
        return self._accept is None or pair.compare_key in self._accept

    @property
    def keys(self) -> list[str]:
        return [key for _shape, key in self.seen]


class TestOnePairStream:
    """Every merged pair reaches the one callback, in one ordered serial stream."""

    def test_all_three_shapes_arrive_in_key_order_on_the_calling_thread(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "src"
        _write(src, "changed.txt", b"xxx", mtime=_NEWER)  # at both sides -> SyncPair
        _write(src, "new.txt", b"xx")  # source only -> SrcOnlyPair
        page = listing(("p/changed.txt", 2), ("p/orphan.txt", 2))  # orphan -> DestOnlyPair
        client, calls = make_recording_client([page, {}, {}, {}])
        journal = _Journal()

        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            pair_filter=journal,
            transfer_config=_SERIAL,
        )

        assert journal.seen == [
            (SyncPair, "changed.txt"),
            (SrcOnlyPair, "new.txt"),
            (DestOnlyPair, "orphan.txt"),
        ]
        assert journal.keys == sorted(journal.keys)  # one ascending stream, all lanes
        assert journal.threads == {threading.get_ident()}  # serial, on the calling thread
        # True took each pair's default action: copy, copy, delete.
        assert ops(calls) == ["ListObjectsV2", "PutObject", "PutObject", "DeleteObjects"]
        assert [call.params["Key"] for call in calls[1:3]] == ["p/changed.txt", "p/new.txt"]
        deleted = [entry["Key"] for entry in calls[3].params["Delete"]["Objects"]]
        assert deleted == ["p/orphan.txt"]

    def test_the_pairs_carry_both_sides_as_their_shape_promises(self, tmp_path: Path) -> None:
        # The callback reads each shape's own fields: a SyncPair has both sides,
        # a SrcOnlyPair only src, a DestOnlyPair only dest.
        src = tmp_path / "src"
        _write(src, "changed.txt", b"xxx")
        _write(src, "new.txt", b"xx")
        page = listing(("p/changed.txt", 2), ("p/orphan.txt", 2))
        client, _calls = make_recording_client([page])
        sides: dict[str, tuple[int | None, int | None]] = {}

        def decide(pair: MergedPair) -> bool:
            src_size = pair.src.size if not isinstance(pair, DestOnlyPair) else None
            dest_size = pair.dest.size if not isinstance(pair, SrcOnlyPair) else None
            sides[pair.compare_key] = (src_size, dest_size)
            assert pair.transfer_type is TransferType.UPLOAD  # stamped on every shape
            return False

        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            pair_filter=decide,
            transfer_config=_SERIAL,
        )
        assert sides == {"changed.txt": (3, 2), "new.txt": (2, None), "orphan.txt": (None, 2)}

    def test_visibility_filter_prunes_before_the_callback(self, tmp_path: Path) -> None:
        # The two layers still compose: a key the visibility filter drops is
        # invisible on both sides, so it never becomes a pair at all.
        src = tmp_path / "src"
        _write(src, "keep.txt", b"xx")
        _write(src, "skip.log", b"xx")
        page = listing(("p/orphan.log", 2), ("p/orphan.txt", 2))
        client, calls = make_recording_client([page])
        journal = _Journal(accept=set())

        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            filter=GlobFilter().exclude("*.log").compile(),
            pair_filter=journal,
            transfer_config=_SERIAL,
        )
        assert journal.keys == ["keep.txt", "orphan.txt"]
        assert ops(calls) == ["ListObjectsV2"]

    def test_a_raising_callback_aborts_the_sync(self, tmp_path: Path) -> None:
        # Like a lane filter that raises: the exception is not translated into a
        # per-item failure, it propagates out of sync.
        src = tmp_path / "src"
        _write(src, "a.txt", b"xx")
        client, _calls = make_recording_client([listing()])

        def boom(_pair: MergedPair) -> bool:
            raise ValueError("decide blew up")

        with pytest.raises(ValueError, match="decide blew up"):
            S3().sync(
                str(src),
                S3Storage("s3://bucket/p", client=client),
                pair_filter=boom,
                transfer_config=_SERIAL,
            )


class TestDecisionsDriveTheActions:
    """``True`` takes the pair's default action, ``False`` takes none."""

    def test_create_lane_copies_only_the_accepted_new_entries(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src, "copy.txt", b"xx")
        _write(src, "skip.txt", b"xx")
        client, calls = make_recording_client([listing(), {}])
        journal = _Journal(accept={"copy.txt"})

        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            pair_filter=journal,
            transfer_config=_SERIAL,
        )
        assert journal.seen == [(SrcOnlyPair, "copy.txt"), (SrcOnlyPair, "skip.txt")]
        assert ops(calls) == ["ListObjectsV2", "PutObject"]
        assert calls[1].params["Key"] == "p/copy.txt"

    def test_update_lane_replaces_the_default_judgment_in_both_directions(
        self, tmp_path: Path
    ) -> None:
        # The default (size + last-modified) is NOT consulted: the accepted pair
        # is one the default would skip, and the refused one is a pair the
        # default would copy.
        src = tmp_path / "src"
        _write(src, "same.txt", b"xx", mtime=_OLDER)  # same size, dest newer -> default skips
        _write(src, "stale.txt", b"xxx", mtime=_NEWER)  # size differs -> default copies
        page = listing(("p/same.txt", 2), ("p/stale.txt", 2))
        client, calls = make_recording_client([page, {}])
        journal = _Journal(accept={"same.txt"})

        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            pair_filter=journal,
            transfer_config=_SERIAL,
        )
        assert journal.seen == [(SyncPair, "same.txt"), (SyncPair, "stale.txt")]
        assert ops(calls) == ["ListObjectsV2", "PutObject"]
        assert calls[1].params["Key"] == "p/same.txt"

    def test_delete_lane_batches_the_accepted_orphans_of_an_s3_destination(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client([listing(("p/gone.txt", 2), ("p/kept.txt", 2)), {}])
        results: list[OpResult] = []
        journal = _Journal(accept={"gone.txt"})

        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            pair_filter=journal,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert journal.seen == [(DestOnlyPair, "gone.txt"), (DestOnlyPair, "kept.txt")]
        assert ops(calls) == ["ListObjectsV2", "DeleteObjects"]
        keys = [entry["Key"] for entry in calls[1].params["Delete"]["Objects"]]
        assert keys == ["p/gone.txt"]
        assert [(r.transfer_type, r.compare_key, r.outcome) for r in results] == [
            (TransferType.DELETE, "gone.txt", OpOutcome.SUCCEEDED)
        ]

    def test_delete_lane_removes_the_accepted_orphans_of_a_local_destination(
        self, tmp_path: Path
    ) -> None:
        out = tmp_path / "out"
        gone = _write(out, "gone.txt", b"xx")
        kept = _write(out, "kept.txt", b"xx")
        client, calls = make_recording_client([listing()])
        results: list[OpResult] = []
        journal = _Journal(accept={"gone.txt"})

        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            str(out),
            pair_filter=journal,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert journal.seen == [(DestOnlyPair, "gone.txt"), (DestOnlyPair, "kept.txt")]
        assert ops(calls) == ["ListObjectsV2"]  # a local delete is synchronous, not an API call
        assert not gone.exists() and kept.exists()
        assert [(r.transfer_type, r.src, r.outcome) for r in results] == [
            (TransferType.DELETE, str(gone), OpOutcome.SUCCEEDED)
        ]

    def test_observe_only_sees_every_pair_and_mutates_nothing(self, tmp_path: Path) -> None:
        # The supported audit mode: False everywhere. Without this hook the same
        # view needed a delete lane switched on with a keep-everything callable.
        out = tmp_path / "out"
        paired = _write(out, "paired.txt", b"xx", mtime=_NEWER)  # default would download
        orphan = _write(out, "orphan.txt", b"xx")
        page = listing(("d/new.txt", 7), ("d/paired.txt", 2))
        client, calls = make_recording_client([page])
        results: list[OpResult] = []
        journal = _Journal(accept=set())

        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            str(out),
            pair_filter=journal,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert journal.seen == [
            (SrcOnlyPair, "new.txt"),
            (DestOnlyPair, "orphan.txt"),
            (SyncPair, "paired.txt"),
        ]
        assert ops(calls) == ["ListObjectsV2"]  # nothing was fetched
        assert results == []
        assert paired.exists() and orphan.exists()
        assert not (out / "new.txt").exists()

    def test_dryrun_still_consults_the_callback_and_records_what_it_accepted(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "src"
        _write(src, "new.txt", b"xx")
        client, calls = make_recording_client([listing(("p/orphan.txt", 2))])
        results: list[OpResult] = []
        journal = _Journal()

        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            pair_filter=journal,
            dryrun=True,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert journal.seen == [(SrcOnlyPair, "new.txt"), (DestOnlyPair, "orphan.txt")]
        assert ops(calls) == ["ListObjectsV2"]
        assert {(r.transfer_type, r.compare_key): r.outcome for r in results} == {
            (TransferType.UPLOAD, "new.txt"): OpOutcome.DRYRUN,
            (TransferType.DELETE, "orphan.txt"): OpOutcome.DRYRUN,
        }


class TestDeleteMachineryIsLive:
    """The delete lane is prepared as if ``delete_filter`` were a callable."""

    def test_a_walked_local_destination_drops_its_read_ahead(self, tmp_path: Path) -> None:
        # The callback MAY delete, so the destination walk must see its own
        # deletions - the same reasoning (and the same knob) as an ordinary
        # delete lane's, pinned in test_sync_delete_lazy_walk.py.
        out = tmp_path / "out"
        _write(out, "orphan.txt", b"xx")
        seen: list[bool] = []
        client, _calls = make_recording_client([listing()])

        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            _ScanRecorder(out, seen),
            pair_filter=lambda _pair: False,
            transfer_config=_SERIAL,
        )
        assert seen == [False]

    def test_a_custom_destination_must_declare_delete(self) -> None:
        # The open-route capability gate is checked with delete=True: a backend
        # that cannot delete is refused up front, before any listing, even
        # though this callback would have deleted nothing.
        dest = _NoDeleteMem({}, location="mem://data/")
        client, calls = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().sync(
                S3Storage("s3://b/src/", client=client),
                dest,
                pair_filter=lambda _pair: False,
                transfer_config=_SERIAL,
            )
        assert "DELETE" in str(excinfo.value)
        assert calls == []

    def test_a_custom_destination_orphan_is_removed_through_its_own_delete(self) -> None:
        store = {"a.txt": b"old", "orphan.txt": b"gone"}
        dest = _MemStorage(store, location="mem://data/")
        client, calls = make_recording_client([listing(("src/a.txt", 3)), get_response(b"AAA")])

        S3().sync(
            S3Storage("s3://b/src/", client=client),
            dest,
            pair_filter=lambda pair: isinstance(pair, (DestOnlyPair, SyncPair)),
            transfer_config=_SERIAL,
        )
        assert ops(calls) == ["ListObjectsV2", "GetObject"]
        assert dest.deletes == ["orphan.txt"]
        assert store == {"a.txt": b"AAA"}


class TestExclusivity:
    """``pair_filter`` replaces the lanes, so the ambiguous combinations raise."""

    @pytest.mark.parametrize(
        ("lane", "value"),
        [("create_filter", False), ("update_filter", True), ("delete_filter", True)],
    )
    def test_a_non_default_lane_filter_is_refused(
        self, tmp_path: Path, lane: str, value: bool
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client([])
        kwargs: dict[str, Any] = {lane: value}
        with pytest.raises(ValidationError) as excinfo:
            S3().sync(
                str(src),
                S3Storage("s3://bucket/p", client=client),
                pair_filter=lambda _pair: True,
                transfer_config=_SERIAL,
                **kwargs,
            )
        message = str(excinfo.value)
        assert "pair_filter" in message and lane in message
        assert excinfo.value.operation == "sync"
        assert calls == []

    def test_the_defaults_of_the_three_lanes_are_accepted(self, tmp_path: Path) -> None:
        # Passing the documented default explicitly is not a conflict - only a
        # lane that would actually have decided something is.
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client([listing()])
        S3().sync(
            str(src),
            S3Storage("s3://bucket/p", client=client),
            create_filter=True,
            update_filter=None,
            delete_filter=False,
            pair_filter=lambda _pair: True,
            transfer_config=_SERIAL,
        )
        assert ops(calls) == ["ListObjectsV2"]

    def test_no_overwrite_is_refused(self, tmp_path: Path) -> None:
        # no_overwrite drops the whole update lane, which would silently hide
        # every both-sides pair from a callback promised all of them.
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().sync(
                str(src),
                S3Storage("s3://bucket/p", client=client),
                pair_filter=lambda _pair: True,
                no_overwrite=True,
                transfer_config=_SERIAL,
            )
        message = str(excinfo.value)
        assert "pair_filter" in message and "no_overwrite" in message
        assert calls == []

    def test_a_parallel_filter_is_refused_eagerly(self, tmp_path: Path) -> None:
        # ParallelFilter is a value container, not a callable: refusing it here
        # names the conflict instead of failing as a TypeError mid-run.
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client([])
        with ThreadPoolExecutor(1) as pool, pytest.raises(ValidationError) as excinfo:
            S3().sync(
                str(src),
                S3Storage("s3://bucket/p", client=client),
                pair_filter=ParallelFilter(lambda _pair: True, executor=pool),
                transfer_config=_SERIAL,
            )
        message = str(excinfo.value)
        assert "pair_filter" in message and "ParallelFilter" in message
        assert calls == []

    def test_the_refusal_happens_before_any_side_effect(self, tmp_path: Path) -> None:
        # The check is over the arguments alone, so it lands before the
        # destination directory a download would otherwise pre-create.
        out = tmp_path / "fresh" / "nested"
        client, calls = make_recording_client([])
        with pytest.raises(ValidationError):
            S3().sync(
                S3Storage("s3://bucket/d", client=client),
                str(out),
                delete_filter=True,
                pair_filter=lambda _pair: True,
                transfer_config=_SERIAL,
            )
        assert not out.exists()
        assert calls == []
