"""``sync --delete``: the local destination walk runs in consumption order.

The delete lane removes orphans through ``LocalStorage.delete`` while the same
walk is still listing the destination tree, so that walk must not read pages
ahead of it: ``aws s3 sync --delete`` lists its destination lazily and
interleaves the deletes, so a file reached through one path and deleted is
never listed again under another. A symlinked directory that aliases its target
is the reproducible case - two paths, one file - and a read-ahead walk hands out
the second path after the file is gone (an ENOENT delete failure, rc 1, or the
walk's own "File does not exist." warning, rc 2).

The knob is ``ScanOptions.read_ahead``; these tests pin its ``Storage.scan``
contract, the lane it is applied to (a *local destination* of a *deleting* run,
nothing else), and the end-to-end behavior it buys.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from boto3.s3.transfer import TransferConfig
from typing_extensions import override

from boto3_s3.localstorage import LocalFileInfo, LocalStorage
from boto3_s3.producers import walk_source_scan_options
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.storage import Storage, StorageCapability
from boto3_s3.types import (
    CancelToken,
    FileInfo,
    LocalScanOptions,
    OpOutcome,
    OpResult,
    ScanOptions,
    TransferType,
)
from tests.utils.fakes3 import listing
from tests.utils.recorder import make_recording_client

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

_SERIAL = TransferConfig(use_threads=False)
_PREFETCH_WORKER = "boto3-s3-prefetch"


def _write(root: Path, rel: str, body: bytes) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    return target


class _PageSpy(Storage):
    """A listing backend recording when each page was pulled, and by which thread.

    ``pulled`` gains one ``(page index, thread name)`` entry per page the
    consumer's demand (or the prefetch worker's read-ahead) actually produced,
    which is what separates the two ``read_ahead`` modes.
    """

    scheme = "spy"
    capabilities = StorageCapability.SCAN

    def __init__(self, pages: Sequence[Sequence[FileInfo]]) -> None:
        self._pages = pages
        self.pulled: list[tuple[int, str]] = []

    @override
    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
        for index, page in enumerate(self._pages):
            self.pulled.append((index, threading.current_thread().name))
            yield page

    @override
    def as_text(self) -> str:
        return "spy://pages"


def _entry(name: str) -> FileInfo:
    return FileInfo(key=name, size=1, compare_key=name)


def _spy(*names: str) -> _PageSpy:
    """A spy whose pages hold one entry each, so a page boundary is every pull."""
    return _PageSpy([[_entry(name)] for name in names])


class _ScanRecorder(LocalStorage):
    """A ``LocalStorage`` recording the ``read_ahead`` of every scan it serves."""

    def __init__(self, path: Path, seen: list[bool]) -> None:
        super().__init__(path)
        self._seen = seen

    @override
    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[LocalFileInfo]]:
        self._seen.append(options.read_ahead)
        return super().scan_pages(options)


class _NeverReadAhead(LocalStorage):
    """A backend declaring in its source-config that it is never read ahead."""

    @override
    def default_scan_options(self) -> LocalScanOptions:
        return replace(super().default_scan_options(), read_ahead=False)


class TestScanReadAhead:
    """``Storage.scan``'s two page-pull modes (the ``ScanOptions.read_ahead`` knob)."""

    def test_lazy_mode_pulls_each_page_when_the_consumer_reaches_it(self) -> None:
        spy = _spy("a", "b", "c")
        stream = spy.scan(ScanOptions(read_ahead=False))
        assert spy.pulled == []  # a generator: nothing is produced before the first pull
        assert next(stream).key == "a"
        assert [index for index, _thread in spy.pulled] == [0]
        assert next(stream).key == "b"
        assert [index for index, _thread in spy.pulled] == [0, 1]
        stream.close()
        assert [index for index, _thread in spy.pulled] == [0, 1]  # the close pulled nothing

    def test_lazy_mode_produces_on_the_consuming_thread(self) -> None:
        spy = _spy("a", "b")
        assert [info.key for info in spy.scan(ScanOptions(read_ahead=False))] == ["a", "b"]
        assert {thread for _index, thread in spy.pulled} == {threading.current_thread().name}

    def test_default_reads_ahead_on_the_prefetch_worker(self) -> None:
        spy = _spy("a", "b", "c")
        stream = spy.scan(ScanOptions())
        try:
            assert next(stream).key == "a"
            # The worker fills the queue regardless of the consumer's progress.
            deadline = time.monotonic() + 5
            while len(spy.pulled) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert [index for index, _thread in spy.pulled] == [0, 1, 2]
            assert {thread for _index, thread in spy.pulled} == {_PREFETCH_WORKER}
        finally:
            stream.close()

    def test_lazy_mode_honors_a_cancel_token_before_the_next_page(self) -> None:
        spy = _spy("a", "b")
        token = CancelToken()
        stream = spy.scan(ScanOptions(read_ahead=False), cancel_token=token)
        assert next(stream).key == "a"
        token.cancel()
        assert list(stream) == []  # entries already yielded stand; no further page pull
        assert [index for index, _thread in spy.pulled] == [0]


class TestWalkScanOptions:
    """``walk_source_scan_options`` may only *narrow* a backend's read-ahead."""

    def _options(self, storage: LocalStorage, *, read_ahead: bool) -> ScanOptions:
        return walk_source_scan_options(
            storage,
            recursive=True,
            on_warning=None,
            item_filter=None,
            reusable_after_interrupt=True,
            read_ahead=read_ahead,
        )

    def test_a_run_can_turn_read_ahead_off(self, tmp_path: Path) -> None:
        storage = LocalStorage(tmp_path)
        assert self._options(storage, read_ahead=True).read_ahead is True
        assert self._options(storage, read_ahead=False).read_ahead is False

    def test_a_backends_own_optout_survives_a_run_asking_for_read_ahead(
        self, tmp_path: Path
    ) -> None:
        storage = _NeverReadAhead(tmp_path)
        assert self._options(storage, read_ahead=True).read_ahead is False


class TestDeleteLaneScope:
    """Only the walked destination of a deleting run drops its read-ahead."""

    def test_local_destination_of_a_delete_run_drops_read_ahead(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _write(out, "stale.txt", b"xx")
        seen: list[bool] = []
        client, _calls = make_recording_client([listing()])
        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            _ScanRecorder(out, seen),
            delete_filter=True,
            transfer_config=_SERIAL,
        )
        assert seen == [False]

    def test_local_destination_keeps_read_ahead_without_delete(self, tmp_path: Path) -> None:
        out = tmp_path / "out"
        _write(out, "stale.txt", b"xx")
        seen: list[bool] = []
        client, _calls = make_recording_client([listing()])
        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            _ScanRecorder(out, seen),
            transfer_config=_SERIAL,
        )
        assert seen == [True]

    def test_local_source_keeps_read_ahead_in_a_delete_run(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        _write(src, "new.txt", b"xx")
        seen: list[bool] = []
        client, _calls = make_recording_client([listing(), {}])
        S3().sync(
            _ScanRecorder(src, seen),
            S3Storage("s3://bucket/d", client=client),
            delete_filter=True,
            transfer_config=_SERIAL,
        )
        assert seen == [True]


class TestAliasedLocalDestination:
    """The parity case: one destination file reachable under two paths."""

    @pytest.mark.parametrize("alias", ["alias", "zalias"])
    def test_alias_of_a_deleted_orphan_is_never_listed(self, tmp_path: Path, alias: str) -> None:
        # `alias` sorts before "real", `zalias` after it, so each run covers one
        # of the two orders in which the walk can reach the shared file.
        out = tmp_path / "out"
        _write(out, "real/f.txt", b"xx")
        (out / alias).symlink_to(out / "real", target_is_directory=True)
        client, _calls = make_recording_client([listing()])
        results: list[OpResult] = []
        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            str(out),
            delete_filter=True,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        # aws deletes the file under whichever path the walk reaches first and
        # never lists the other: one record, no failure, no walk warning.
        first = min(alias, "real")
        assert [(r.transfer_type, r.outcome) for r in results] == [
            (TransferType.DELETE, OpOutcome.SUCCEEDED)
        ]
        assert [r.src for r in results] == [str(out / first / "f.txt")]
        assert not (out / "real" / "f.txt").exists()

    def test_the_walk_still_reaches_entries_the_alias_does_not_hide(self, tmp_path: Path) -> None:
        # The lazy walk must not stop at the vanished directory: entries sorting
        # after it are still listed (and deleted) from the same run.
        out = tmp_path / "out"
        _write(out, "real/f.txt", b"xx")
        _write(out, "zzz.txt", b"xx")
        (out / "zalias").symlink_to(out / "real", target_is_directory=True)
        client, _calls = make_recording_client([listing()])
        results: list[OpResult] = []
        S3().sync(
            S3Storage("s3://bucket/d", client=client),
            str(out),
            delete_filter=True,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert {r.outcome for r in results} == {OpOutcome.SUCCEEDED}
        assert sorted(str(r.src) for r in results) == sorted(
            [str(out / "real" / "f.txt"), str(out / "zzz.txt")]
        )
        assert os.listdir(out / "real") == []
