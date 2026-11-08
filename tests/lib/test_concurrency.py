"""Unit tests for boto3_s3.concurrency: prefetch, Wake and WakeAll.

The worker is a background thread; tests drive it through in-memory page lists
so timing stays deterministic. Where teardown is asserted, the named worker
thread must be gone once the context manager exits (it joins on exit).
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from boto3_s3 import concurrency
from boto3_s3.concurrency import Wake, WakeAll, prefetch
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import CancelMode, CancelToken, S3FileInfo, ScanOptions
from tests.utils.interrupts import interrupt_main_in, main_waits_in, skip_without_pthread_kill

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
    # the worker must observe the stop flag and end by itself.
    pages = ([i] for i in range(100_000))
    with prefetch(pages, queue_size=2) as it:
        assert next(it) == 0
    _drain_worker(threading.Event())


def _drain_worker(release: threading.Event) -> None:
    """Let an abandoned worker finish and wait for it to die (test hygiene)."""
    release.set()
    deadline = time.monotonic() + 5
    while _worker_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _worker_alive()


def test_an_exit_run_on_the_worker_does_not_wait_for_itself() -> None:
    # The garbage collector closes a scan left unclosed in a reference cycle on
    # whichever thread it runs, the worker's included.
    entered = threading.Event()
    exited = threading.Event()
    contexts: list[contextlib.AbstractContextManager[Iterator[int]]] = []

    def pages() -> Iterator[list[int]]:
        yield [1]
        assert entered.wait(5)
        contexts[0].__exit__(None, None, None)
        exited.set()
        yield [2]

    contexts.append(prefetch(pages()))
    contexts[0].__enter__()
    entered.set()
    assert exited.wait(5)


def test_an_interrupt_leaves_the_worker_to_end_by_itself() -> None:
    # A KeyboardInterrupt unwind does not join the worker, but tells it to stop:
    # blocked for room with pages left to pull for ever, it ends by itself.
    pages = ([i] for i in range(100_000))
    with pytest.raises(KeyboardInterrupt):
        with prefetch(pages, queue_size=2) as it:
            assert next(it) == 0
            raise KeyboardInterrupt
    deadline = time.monotonic() + 5
    while _worker_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _worker_alive()


def test_interrupt_can_abandon_a_stuck_page_pull() -> None:
    # A KeyboardInterrupt unwind must not wait for a page pull in flight (a
    # network fetch can block for a full timeout); the daemon worker is left to
    # end by itself.
    pull_started = threading.Event()
    release = threading.Event()

    def pages() -> Iterator[list[int]]:
        yield [1]
        pull_started.set()
        release.wait(10)  # the slow in-flight pull
        yield [2]

    try:
        with pytest.raises(KeyboardInterrupt):
            with prefetch(pages(), queue_size=1) as it:
                assert next(it) == 1
                assert pull_started.wait(5)
                raise KeyboardInterrupt
        # Returning promptly is the point: the exit skipped the join while the
        # worker is still inside the 10s pull.
        assert _worker_alive()
    finally:
        _drain_worker(release)


@pytest.mark.parametrize("exit_with", [None, SystemExit, ValueError])
def test_no_early_exit_waits_for_a_pull(exit_with: type[BaseException] | None) -> None:
    # Every early exit, not only an interrupt: the worker is told to stop and
    # ends by itself after the pull in flight.
    pull_started = threading.Event()
    release = threading.Event()

    def pages() -> Iterator[list[int]]:
        yield [1]
        pull_started.set()
        release.wait(10)
        yield [2]

    try:
        with contextlib.suppress(SystemExit, ValueError):
            with prefetch(pages(), queue_size=1) as it:
                assert next(it) == 1
                assert pull_started.wait(5)
                if exit_with is not None:
                    raise exit_with
        assert _worker_alive()
    finally:
        _drain_worker(release)


@pytest.mark.parametrize("mode", [CancelMode.GRACEFUL, CancelMode.IMMEDIATE])
def test_a_cancel_ends_the_items_without_waiting_for_a_pull(mode: CancelMode) -> None:
    # The consumer waiting for the next page wakes on the cancel and ends, the
    # pull in flight left to the worker, which drops its page.
    pull_started = threading.Event()
    release = threading.Event()
    token = CancelToken()

    def pages() -> Iterator[list[int]]:
        yield [1]
        pull_started.set()
        release.wait(10)
        yield [2]

    try:
        with prefetch(pages(), queue_size=1, cancel_token=token) as it:
            assert next(it) == 1
            assert pull_started.wait(5)
            threading.Timer(0.1, token.cancel, kwargs={"mode": mode}).start()
            started = time.monotonic()
            assert list(it) == []
            assert time.monotonic() - started < 5
        assert _worker_alive()
    finally:
        _drain_worker(release)


def test_storage_scan_does_not_wait_for_a_pull_on_an_interrupt() -> None:
    # A KeyboardInterrupt thrown into the scan returns without waiting for the
    # in-flight page pull.
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
        scan = _Blocking().scan(ScanOptions())
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
        self.pulling = threading.Event()
        self.block = block

    def scan_pages(self, options: ScanOptions) -> Iterator[list[S3FileInfo]]:
        yield [
            S3FileInfo(key="prefix/a", size=1, compare_key="a"),
            S3FileInfo(key="prefix/b", size=1, compare_key="b"),
        ]
        self.pulling.set()
        self.release.wait(self.block)  # the slow in-flight pull
        yield [S3FileInfo(key="prefix/c", size=1, compare_key="c")]


def _interrupt_on_second(counter: dict[str, int]) -> None:
    counter["n"] += 1
    if counter["n"] == 2:
        raise KeyboardInterrupt


def test_ls_does_not_wait_when_the_interrupt_lands_in_on_entry() -> None:
    # An interrupt raised from `on_entry` never unwinds through the prefetch
    # context: the scan is closed while it is handled, which must not join the
    # 10s pull.
    from boto3_s3 import S3

    storage = _BlockingScanStorage("s3://bkt/prefix/")
    counter = {"n": 0}
    start = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            S3().ls(storage, on_entry=lambda info: _interrupt_on_second(counter))
        assert time.monotonic() - start < 5
        assert _worker_alive()
    finally:
        _drain_worker(storage.release)


def test_rm_does_not_wait_when_the_interrupt_lands_in_on_result() -> None:
    # Same window on rm's loop.
    from boto3_s3 import S3

    storage = _BlockingScanStorage("s3://bkt/prefix/")
    counter = {"n": 0}
    start = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            S3().rm(
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
                S3().cp(
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
        S3().ls(storage, on_entry=lambda info: _interrupt_on_second(counter))
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
                S3().sync(
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


def test_rm_live_route_does_not_wait_when_the_interrupt_lands_in_submit() -> None:
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
            S3().rm(storage, recursive=True)
        assert time.monotonic() - start < 5
        assert _worker_alive()
    finally:
        S3Deleter.submit = original  # pyright: ignore[reportAttributeAccessIssue]
        _drain_worker(storage.release)


@pytest.mark.parametrize("exc_type", [SystemExit, ValueError])
def test_ls_does_not_wait_for_a_pull_on_any_exit(exc_type: type[BaseException]) -> None:
    # Not only a Ctrl-C: no early end waits for the page pull in flight.
    from boto3_s3 import S3

    storage = _BlockingScanStorage("s3://bkt/prefix/")
    counter = {"n": 0}

    def on_entry(_info: object) -> None:
        counter["n"] += 1
        if counter["n"] == 2:
            raise exc_type()

    start = time.monotonic()
    try:
        with pytest.raises(exc_type):
            S3().ls(storage, on_entry=on_entry)
        assert time.monotonic() - start < 5
        assert _worker_alive()
    finally:
        _drain_worker(storage.release)


def test_a_cancel_ends_the_chunk_in_hand() -> None:
    token = CancelToken()
    with prefetch([[1, 2, 3]], cancel_token=token) as it:
        assert next(it) == 1
        token.cancel()
        assert list(it) == []


def test_a_graceful_cancel_ending_the_walk_reports_nothing_after_it(tmp_path: Path) -> None:
    # A graceful cancel can end cp's source walk with no exception while the
    # walk still warns about the page in flight: nothing reaches on_result
    # once the CancelledError has reached the caller (overview section 3).
    from boto3_s3 import S3, CancelledError, LocalStorage
    from boto3_s3.types import OpResult

    pulling = threading.Event()
    release = threading.Event()
    warned = threading.Event()

    class _Slow(LocalStorage):
        def scan_pages(self, options: Any) -> Any:
            pages = super().scan_pages(options)
            yield next(pages)
            pulling.set()
            release.wait(10)
            if options.on_warning is not None:
                options.on_warning("from the page in flight")
            warned.set()
            yield from pages

    src = tmp_path / "src"
    src.mkdir()
    for name in ("a", "b", "c"):
        (src / name).write_text("x")
    token = CancelToken()

    def cancel_once_waiting() -> None:
        assert pulling.wait(5)
        main_waits_in("_consume", waiting_in="wait_until")
        token.cancel()

    threading.Thread(target=cancel_once_waiting, daemon=True).start()
    raised = [False]
    after: list[OpResult] = []

    def on_result(result: OpResult) -> None:
        if raised[0]:
            after.append(result)

    try:
        with pytest.raises(CancelledError):
            S3().cp(
                _Slow(str(src)),
                "s3://bkt/p/",
                recursive=True,
                dryrun=True,
                on_result=on_result,
                cancel_token=token,
            )
        raised[0] = True
    finally:
        _drain_worker(release)
    assert warned.is_set()
    assert after == []


@pytest.mark.parametrize("operation", ["cp", "sync"])
def test_a_transfer_run_does_not_wait_for_a_pull_on_a_cancel(
    operation: str, tmp_path: Path
) -> None:
    # A cancel ends cp's and sync's source listing at once, as it ends ls's:
    # the page pull in flight is left to the worker (overview section 3).
    from boto3_s3 import S3, CancelledError

    storage = _BlockingScanStorage("s3://bkt/prefix/")
    token = CancelToken()

    def cancel_once_waiting() -> None:
        # Once the run waits for the page being pulled, not before.
        assert storage.pulling.wait(5)
        main_waits_in("_consume", waiting_in="wait_until")
        token.cancel(mode=CancelMode.IMMEDIATE)

    threading.Thread(target=cancel_once_waiting, daemon=True).start()
    start = time.monotonic()
    try:
        with pytest.raises(CancelledError):
            if operation == "cp":
                S3().cp(storage, str(tmp_path), recursive=True, dryrun=True, cancel_token=token)
            else:
                S3().sync(storage, str(tmp_path), dryrun=True, cancel_token=token)
        assert time.monotonic() - start < 5
    finally:
        _drain_worker(storage.release)


class TestWake:
    """How one thread waits for another (`concurrency.Wake`, `concurrency.WakeAll`).

    A wait that should end runs on a thread whose first look at the condition
    comes before the other side acts, so the wait can end only by being woken,
    and that is joined with a deadline; or on the main thread with a late send
    as a backstop. A wait that never ends fails the test instead of hanging it.
    """

    @staticmethod
    def _soon(act: Callable[[], None]) -> None:
        def later() -> None:
            time.sleep(0.05)
            act()

        threading.Thread(target=later, daemon=True).start()

    def _send_soon(self, wake: Wake, flag: list[bool]) -> None:
        def send() -> None:
            flag[0] = True
            wake.send()

        self._soon(send)

    class _Waiting:
        """``wait(ready)`` on a thread, with its first look and its end observable."""

        def __init__(
            self, wait: Callable[[Callable[[], bool]], None], holds: Callable[[], bool]
        ) -> None:
            self.looks = 0
            self.looked = threading.Event()
            self.ended = threading.Event()

            def ready() -> bool:
                self.looks += 1
                result = holds()
                self.looked.set()
                return result

            def run() -> None:
                wait(ready)
                self.ended.set()

            threading.Thread(target=run, daemon=True).start()
            assert self.looked.wait(5)
            assert not self.ended.is_set()

    def _woken_by(
        self,
        act: Callable[[], None],
        wait: Callable[[Callable[[], bool]], None],
        holds: Callable[[], bool],
    ) -> None:
        """*act* after the wait's first look; assert the wait then ends."""
        waiting = self._Waiting(wait, holds)
        act()
        assert waiting.ended.wait(5), "the wait never ended"

    @staticmethod
    def _backstop(wake: Wake, flag: list[bool]) -> threading.Timer:
        """For a wait on the main thread: end it in 10 s should nothing else."""

        def send() -> None:
            flag[0] = True
            wake.send()

        timer = threading.Timer(10, send)
        timer.daemon = True
        timer.start()
        return timer

    def test_a_send_ends_the_wait_at_once(self) -> None:
        wake, flag = Wake(), [False]

        def send() -> None:
            flag[0] = True
            wake.send()

        self._woken_by(send, wake.wait_until, lambda: flag[0])

    def test_the_wait_looks_only_when_woken(self) -> None:
        # No look of its own: a condition that comes true without a send is
        # seen only at the next send.
        wake, flag = Wake(), [False]
        waiting = self._Waiting(wake.wait_until, lambda: flag[0])
        flag[0] = True
        assert not waiting.ended.wait(0.3)
        wake.send()
        assert waiting.ended.wait(5)
        assert waiting.looks == 2

    def test_a_condition_that_holds_is_not_waited_for(self) -> None:
        Wake().wait_until(lambda: True)

    def test_sends_with_nobody_waiting_do_not_pile_up(self) -> None:
        wake = Wake()
        for _ in range(1000):
            wake.send()
        pending = wake._pending  # pyright: ignore[reportPrivateUsage]
        assert pending.acquire(blocking=False)  # one wake-up pending
        assert not pending.acquire(blocking=False)  # and no more

    def test_a_cancel_wakes_a_wait_given_the_token(self) -> None:
        token, wake = CancelToken(), Wake()
        self._woken_by(
            token.cancel,
            lambda ready: wake.wait_until(ready, cancel_token=token),
            lambda: token.cancelled,
        )
        assert not token._waiters._wakes  # pyright: ignore[reportPrivateUsage]

    def test_an_escalation_wakes_the_wait_again(self) -> None:
        token, wake = CancelToken(), Wake()
        token.cancel()
        self._woken_by(
            lambda: token.cancel(mode=CancelMode.IMMEDIATE),
            lambda ready: wake.wait_until(ready, cancel_token=token),
            lambda: token.mode is CancelMode.IMMEDIATE,
        )

    def test_send_all_wakes_every_thread_waiting(self) -> None:
        wake_all, flag = WakeAll(), [False]

        def wait(ready: Callable[[], bool]) -> None:
            wake = Wake()
            wake_all.add(wake)
            try:
                wake.wait_until(ready)
            finally:
                wake_all.discard(wake)

        waitings = [self._Waiting(wait, lambda: flag[0]) for _ in range(3)]
        flag[0] = True
        wake_all.send()
        for waiting in waitings:
            assert waiting.ended.wait(5)

    def test_the_main_thread_looks_by_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The main thread's wait wakes by itself to let a Ctrl-C through, and so
        # sees a condition nothing sent for.
        monkeypatch.setattr(concurrency, "_CTRL_C_SLICE", 0.05)
        wake, flag = Wake(), [False]
        backstop = self._backstop(wake, flag)
        self._soon(lambda: flag.__setitem__(0, True))
        started = time.monotonic()
        wake.wait_until(lambda: flag[0])
        backstop.cancel()
        assert time.monotonic() - started < 5

    @skip_without_pthread_kill
    def test_a_ctrl_c_in_the_wait_leaves_it_usable(self) -> None:
        wake, flag = Wake(), [False]
        backstop = self._backstop(wake, flag)
        threading.Thread(
            target=interrupt_main_in,
            args=("test_a_ctrl_c_in_the_wait_leaves_it_usable",),
            kwargs={"waiting_in": "wait_until"},
        ).start()
        with pytest.raises(KeyboardInterrupt):
            wake.wait_until(lambda: flag[0])
        backstop.cancel()
        backstop = self._backstop(wake, flag)
        self._send_soon(wake, flag)
        started = time.monotonic()
        wake.wait_until(lambda: flag[0])
        backstop.cancel()
        assert time.monotonic() - started < 5
