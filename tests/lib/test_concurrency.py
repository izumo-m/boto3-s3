"""Unit tests for boto3_s3.concurrency: prefetch and Wake.

The worker is a background thread; tests drive it through in-memory page lists
so timing stays deterministic. Where teardown is asserted, the named worker
thread must be gone once the context manager exits (it joins on exit).
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator

import pytest

from boto3_s3 import concurrency
from boto3_s3.concurrency import Wake, prefetch
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import S3FileInfo, ScanOptions
from tests.utils.interrupts import interrupt_main_in, skip_without_pthread_kill

_WORKER_NAME = "boto3-s3-prefetch"


def _worker_alive() -> bool:
    return any(t.name == _WORKER_NAME and t.is_alive() for t in threading.enumerate())


def test_flattens_chunks_in_order() -> None:
    with prefetch([[1, 2, 3], [4, 5], [6]]) as it:
        assert list(it) == [1, 2, 3, 4, 5, 6]


def test_empty_source_yields_nothing() -> None:
    with prefetch([]) as it:
        assert list(it) == []


def test_all_chunks_within_queue_capacity_are_delivered() -> None:
    # The producer finishes with the queue full (every chunk fits); the end
    # marker must still reach the consumer rather than being dropped.
    with prefetch([[1], [2], [3], [4]], queue_size=4) as it:
        assert list(it) == [1, 2, 3, 4]


def test_none_items_are_not_confused_with_end() -> None:
    with prefetch([[None, 1], [None]]) as it:
        assert list(it) == [None, 1, None]


@pytest.mark.parametrize("queue_size", [0, -1])
def test_non_positive_queue_size_is_rejected(queue_size: int) -> None:
    with pytest.raises(ValueError, match="queue_size must be positive"):
        with prefetch([[1]], queue_size=queue_size):
            pass


def test_worker_exception_surfaces_after_buffered_items() -> None:
    def boom() -> Iterator[list[int]]:
        yield [1, 2]
        raise ValueError("kaboom")

    collected: list[int] = []
    with pytest.raises(ValueError, match="kaboom"):
        with prefetch(boom()) as it:
            collected.extend(it)
    assert collected == [1, 2]


def test_worker_joined_on_normal_exit() -> None:
    with prefetch([[1], [2]]) as it:
        assert list(it) == [1, 2]
    assert not _worker_alive()


def test_early_break_tears_down_worker() -> None:
    # A large lazy source the consumer abandons after one item; on context exit
    # the worker must observe the stop flag and join.
    pages = ([i] for i in range(100_000))
    with prefetch(pages, queue_size=2) as it:
        assert next(it) == 0
    assert not _worker_alive()


def _drain_worker(release: threading.Event) -> None:
    """Let an abandoned worker finish and wait for it to die (test hygiene)."""
    release.set()
    deadline = time.monotonic() + 5
    while _worker_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _worker_alive()


def test_interrupt_still_joins_by_default() -> None:
    # A KeyboardInterrupt unwind keeps the no-surviving-worker contract unless
    # the app opted out with reusable_after_interrupt=False. The producer far exceeds
    # the queue, so the worker is genuinely blocked mid-run - it cannot have
    # finished naturally, and only the context exit's join reclaims it.
    pages = ([i] for i in range(100_000))
    with pytest.raises(KeyboardInterrupt):
        with prefetch(pages, queue_size=2) as it:
            assert next(it) == 0
            raise KeyboardInterrupt
    assert not _worker_alive()


def test_interrupt_can_abandon_a_stuck_page_pull() -> None:
    # reusable_after_interrupt=False: a KeyboardInterrupt unwind must not wait for a
    # page pull in flight (a network fetch can block for a full timeout); the
    # daemon worker is abandoned to die with the process.
    pull_started = threading.Event()
    release = threading.Event()

    def pages() -> Iterator[list[int]]:
        yield [1]
        pull_started.set()
        release.wait(10)  # the slow in-flight pull
        yield [2]

    try:
        with pytest.raises(KeyboardInterrupt):
            with prefetch(pages(), queue_size=1, reusable_after_interrupt=False) as it:
                assert next(it) == 1
                assert pull_started.wait(5)
                raise KeyboardInterrupt
        # Returning promptly is the point: the exit skipped the join while the
        # worker is still inside the 10s pull.
        assert _worker_alive()
    finally:
        _drain_worker(release)


@pytest.mark.parametrize("exit_with", [None, SystemExit, ValueError])
def test_no_early_exit_waits_for_a_pull_under_reusable_after_interrupt_false(
    exit_with: type[BaseException] | None,
) -> None:
    # The opt-out covers every early exit, not only an interrupt: the worker is
    # told to stop and ends by itself after the pull in flight.
    pull_started = threading.Event()
    release = threading.Event()

    def pages() -> Iterator[list[int]]:
        yield [1]
        pull_started.set()
        release.wait(10)
        yield [2]

    try:
        with contextlib.suppress(SystemExit, ValueError):
            with prefetch(pages(), queue_size=1, reusable_after_interrupt=False) as it:
                assert next(it) == 1
                assert pull_started.wait(5)
                if exit_with is not None:
                    raise exit_with
        assert _worker_alive()
    finally:
        _drain_worker(release)


def test_storage_scan_forwards_the_interrupt_policy() -> None:
    # Storage.scan wires ScanOptions.reusable_after_interrupt into prefetch: with
    # False, a KeyboardInterrupt unwind of the scan returns without waiting
    # for the in-flight page pull.
    from typing import BinaryIO, Literal

    from boto3_s3 import Storage
    from boto3_s3.types import FileInfo

    pull_started = threading.Event()
    release = threading.Event()

    class _Blocking(Storage):
        def scan_pages(self, options: ScanOptions) -> Iterator[list[FileInfo]]:
            yield [FileInfo(key="a")]
            pull_started.set()
            release.wait(10)
            yield [FileInfo(key="b")]

        def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
            raise NotImplementedError

        def delete(self, info: FileInfo) -> None:
            raise NotImplementedError

        def get_fileinfo(self, key: str = "", *, on_warning: object = None) -> FileInfo | None:
            return None

        def as_text(self) -> str:
            return "blocking-stub"

    try:
        scan = _Blocking().scan(ScanOptions(reusable_after_interrupt=False))
        assert next(scan).key == "a"
        assert pull_started.wait(5)
        with pytest.raises(KeyboardInterrupt):
            scan.throw(KeyboardInterrupt)
        assert _worker_alive()  # abandoned mid-pull, not joined
    finally:
        _drain_worker(release)


class _BlockingScanStorage(S3Storage):
    """An S3Storage whose second page pull blocks until released."""

    def __init__(self, *args: object, block: float = 10.0, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        self.release = threading.Event()
        self.block = block

    def scan_pages(self, options: ScanOptions) -> Iterator[list[S3FileInfo]]:
        yield [
            S3FileInfo(key="prefix/a", size=1, compare_key="a"),
            S3FileInfo(key="prefix/b", size=1, compare_key="b"),
        ]
        self.release.wait(self.block)  # the slow in-flight pull
        yield [S3FileInfo(key="prefix/c", size=1, compare_key="c")]


def _interrupt_on_second(counter: dict[str, int]) -> None:
    counter["n"] += 1
    if counter["n"] == 2:
        raise KeyboardInterrupt


def test_ls_honors_the_posture_when_the_interrupt_lands_in_on_entry() -> None:
    # An interrupt raised from `on_entry` never unwinds through the prefetch
    # context: the scan is merely closed, which must not join the 10s pull.
    from boto3_s3 import S3

    storage = _BlockingScanStorage("s3://bkt/prefix/")
    counter = {"n": 0}
    start = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            S3(reusable_after_interrupt=False).ls(
                storage, on_entry=lambda info: _interrupt_on_second(counter)
            )
        assert time.monotonic() - start < 5
        assert _worker_alive()
    finally:
        _drain_worker(storage.release)


def test_rm_honors_the_posture_when_the_interrupt_lands_in_on_result() -> None:
    # Same window on rm's loop.
    from boto3_s3 import S3

    storage = _BlockingScanStorage("s3://bkt/prefix/")
    counter = {"n": 0}
    start = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            S3(reusable_after_interrupt=False).rm(
                storage,
                recursive=True,
                dryrun=True,
                on_result=lambda result: _interrupt_on_second(counter),
            )
        assert time.monotonic() - start < 5
        assert _worker_alive()
    finally:
        _drain_worker(storage.release)


def test_transfer_teardown_does_not_wait_for_a_pull() -> None:
    # Neither closing the producer in the unwind nor finalizing what is left of
    # it once the traceback is released may join the pull.
    import gc
    import tempfile

    from boto3_s3 import S3

    storage = _BlockingScanStorage("s3://bkt/prefix/")
    counter = {"n": 0}
    start = time.monotonic()
    try:
        with tempfile.TemporaryDirectory() as dest:
            with pytest.raises(KeyboardInterrupt):
                S3(reusable_after_interrupt=False).cp(
                    storage,
                    dest,
                    recursive=True,
                    dryrun=True,
                    on_result=lambda result: _interrupt_on_second(counter),
                )
            # The pytest.raises exit dropped the exception state; collect so
            # the assertion below cannot depend on refcount timing.
            gc.collect()
            assert time.monotonic() - start < 5
            assert _worker_alive()
    finally:
        _drain_worker(storage.release)


def test_an_interrupted_scan_stops_with_the_traceback_still_held() -> None:
    # The operation closes the scan in the unwind: its worker, with pages left to
    # pull for ever, must not wait for the traceback's release to learn to stop.
    from boto3_s3 import S3

    class _Endless(_BlockingScanStorage):
        def scan_pages(self, options: ScanOptions) -> Iterator[list[S3FileInfo]]:
            while True:
                yield [
                    S3FileInfo(key="prefix/a", size=1, compare_key="a"),
                    S3FileInfo(key="prefix/b", size=1, compare_key="b"),
                ]

    storage = _Endless("s3://bkt/prefix/")
    counter = {"n": 0}
    with pytest.raises(KeyboardInterrupt) as held:
        S3(reusable_after_interrupt=False).ls(
            storage, on_entry=lambda info: _interrupt_on_second(counter)
        )
    _drain_worker(storage.release)
    assert held.traceback is not None


def test_sync_teardown_does_not_wait_for_a_pull() -> None:
    # sync closes its two side scans on its ExitStack: a site of its own.
    import tempfile

    from boto3_s3 import S3

    storage = _BlockingScanStorage("s3://bkt/prefix/")
    counter = {"n": 0}
    start = time.monotonic()
    try:
        with tempfile.TemporaryDirectory() as dest:
            with pytest.raises(KeyboardInterrupt):
                S3(reusable_after_interrupt=False).sync(
                    storage,
                    dest,
                    dryrun=True,
                    on_result=lambda result: _interrupt_on_second(counter),
                )
            gc_import = __import__("gc")
            gc_import.collect()
            assert time.monotonic() - start < 5
            assert _worker_alive()
    finally:
        _drain_worker(storage.release)


def test_rm_live_route_honors_the_posture_when_the_interrupt_lands_in_submit() -> None:
    # The non-dryrun rm consumes its scan through the S3Deleter loop - a
    # separate site from the dryrun loop the other rm test drives.
    from boto3_s3 import S3
    from boto3_s3.deleter import S3Deleter

    storage = _BlockingScanStorage("s3://bkt/prefix/")
    original = S3Deleter.submit

    def submit(self: S3Deleter, info: object) -> None:
        raise KeyboardInterrupt

    S3Deleter.submit = submit  # pyright: ignore[reportAttributeAccessIssue]
    start = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            S3(reusable_after_interrupt=False).rm(storage, recursive=True)
        assert time.monotonic() - start < 5
        assert _worker_alive()
    finally:
        S3Deleter.submit = original  # pyright: ignore[reportAttributeAccessIssue]
        _drain_worker(storage.release)


@pytest.mark.parametrize("exc_type", [SystemExit, ValueError])
def test_ls_joins_on_every_exit_by_default(exc_type: type[BaseException]) -> None:
    # The default posture keeps the full reclamation: the worker is joined.
    from boto3_s3 import S3

    storage = _BlockingScanStorage("s3://bkt/prefix/", block=1.0)
    counter = {"n": 0}

    def on_entry(_info: object) -> None:
        counter["n"] += 1
        if counter["n"] == 2:
            raise exc_type()

    try:
        with pytest.raises(exc_type):
            S3().ls(storage, on_entry=on_entry)
        assert not _worker_alive()
    finally:
        storage.release.set()


class TestWake:
    """The wait the calling thread does on other threads (`concurrency.Wake`)."""

    @staticmethod
    def _send_soon(wake: Wake, flag: list[bool]) -> None:
        def send() -> None:
            time.sleep(0.05)
            flag[0] = True
            wake.send()

        threading.Thread(target=send).start()

    def test_a_send_ends_the_wait_at_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Woken by the send, not by the look the slice takes.
        monkeypatch.setattr(concurrency, "_WAKE_SLICE", 30.0)
        wake, flag = Wake(), [False]
        self._send_soon(wake, flag)
        started = time.monotonic()
        wake.wait_until(lambda: flag[0])
        assert time.monotonic() - started < 5

    def test_a_condition_that_holds_is_not_waited_for(self) -> None:
        Wake().wait_until(lambda: True)

    def test_sends_with_nobody_waiting_do_not_pile_up(self) -> None:
        wake = Wake()
        for _ in range(1000):
            wake.send()
        pending = wake._pending  # pyright: ignore[reportPrivateUsage]
        assert pending.acquire(blocking=False)  # one wake-up pending
        assert not pending.acquire(blocking=False)  # and no more

    @skip_without_pthread_kill
    def test_a_ctrl_c_in_the_wait_leaves_it_usable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(concurrency, "_WAKE_SLICE", 30.0)
        wake, flag = Wake(), [False]
        threading.Thread(
            target=interrupt_main_in,
            args=("test_a_ctrl_c_in_the_wait_leaves_it_usable",),
            kwargs={"waiting_in": "wait_until"},
        ).start()
        with pytest.raises(KeyboardInterrupt):
            wake.wait_until(lambda: flag[0])
        self._send_soon(wake, flag)
        started = time.monotonic()
        wake.wait_until(lambda: flag[0])
        assert time.monotonic() - started < 5
