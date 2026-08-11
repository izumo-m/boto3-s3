"""Unit tests for boto3_s3.concurrency.prefetch.

The worker is a background thread; tests drive it through in-memory page lists
so timing stays deterministic. Where teardown is asserted, the named worker
thread must be gone once the context manager exits (it joins on exit).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from boto3_s3 import concurrency
from boto3_s3.concurrency import prefetch
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import S3FileInfo, ScanOptions

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


def test_early_break_still_joins_with_reusable_after_interrupt_false() -> None:
    # The opt-out is interrupt-scoped: GeneratorExit / normal exits keep the
    # full teardown even when reusable_after_interrupt is False.
    pages = ([i] for i in range(100_000))
    with prefetch(pages, queue_size=2, reusable_after_interrupt=False) as it:
        assert next(it) == 0
    assert not _worker_alive()


def test_systemexit_still_joins_with_reusable_after_interrupt_false() -> None:
    # The opt-out scopes to KeyboardInterrupt alone. sys.exit() requests an
    # orderly termination, so a SystemExit unwind reclaims the worker like any
    # ordinary exception even when reusable_after_interrupt is False.
    pages = ([i] for i in range(100_000))
    with pytest.raises(SystemExit):
        with prefetch(pages, queue_size=2, reusable_after_interrupt=False) as it:
            assert next(it) == 0
            raise SystemExit(3)
    assert not _worker_alive()


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


def test_marked_interrupt_abandons_a_generator_exit_teardown() -> None:
    # The interrupt does not always unwind through the prefetch context: when
    # it lands in the consumer's loop body (ls's on_entry, rm's submission
    # wait) the scan is merely closed - a GeneratorExit. The operation marks
    # the unwind (`mark_interrupt_unwinding`) and a posture-False teardown
    # then abandons the in-flight pull instead of joining it.
    pull_started = threading.Event()
    release = threading.Event()

    def pages() -> Iterator[list[int]]:
        yield [1]
        pull_started.set()
        release.wait(10)  # the slow in-flight pull
        yield [2]

    try:
        with prefetch(pages(), queue_size=1, reusable_after_interrupt=False) as it:
            assert next(it) == 1
            assert pull_started.wait(5)
            concurrency.mark_interrupt_unwinding()
        # The exit was a plain (non-interrupt) teardown, but the marker made
        # it abandon: the worker is still inside the 10s pull.
        assert _worker_alive()
    finally:
        _drain_worker(release)


def test_the_marker_leaves_a_default_posture_teardown_joining() -> None:
    # The marker relaxes posture-False contexts alone: full reclamation stays
    # the default posture's contract even mid-fatal-unwind.
    pages = ([i] for i in range(100_000))
    concurrency.mark_interrupt_unwinding()
    with prefetch(pages, queue_size=2) as it:
        assert next(it) == 0
    assert not _worker_alive()


class _BlockingScanStorage(S3Storage):
    """An S3Storage whose second page pull blocks until released."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        self.release = threading.Event()

    def scan_pages(self, options: ScanOptions) -> Iterator[list[S3FileInfo]]:
        yield [
            S3FileInfo(key="prefix/a", size=1, compare_key="a"),
            S3FileInfo(key="prefix/b", size=1, compare_key="b"),
        ]
        self.release.wait(10)  # the slow in-flight pull
        yield [S3FileInfo(key="prefix/c", size=1, compare_key="c")]


def _interrupt_on_second(counter: dict[str, int]) -> None:
    counter["n"] += 1
    if counter["n"] == 2:
        raise KeyboardInterrupt


def test_ls_honors_the_posture_when_the_interrupt_lands_in_on_entry() -> None:
    # The consumer-side windows: an interrupt raised from `on_entry` never
    # unwinds through the prefetch context, so before the operations marked
    # the unwind, ls's close joined the 10s pull and held the interrupt back
    # for its full length (measured pre-fix).
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
    # Same window on rm's loop - which additionally iterated its scan as a
    # bare temporary, so the unwind finalized (and joined) it even before an
    # `except` could have marked anything (measured pre-fix).
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


def test_transfer_teardown_abandons_after_the_interrupt_is_marked() -> None:
    # cp/mv/sync already delivered the interrupt promptly (their guard skips
    # the close), but releasing the traceback finalized the producer and its
    # teardown joined the pull inside the caller's own interrupt handler
    # (measured pre-fix). With the unwind marked, the deferred finalization
    # abandons the worker too.
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
