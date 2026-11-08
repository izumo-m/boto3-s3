"""Threading helpers for overlapping I/O with consumption.

The Storage ``scan`` path is a lazy generator, but each page fetch
(``ListObjectsV2`` for S3, one ``os.scandir`` per directory for local) is I/O the
consumer would otherwise block on. ``prefetch`` runs the page iterable on a
worker thread and hands back a flattened iterator, so the next page is in flight
while the consumer processes the current one.

Backpressure is bounded: the worker waits while ``queue_size`` pages are
unread, so a slow consumer throttles a fast producer instead of buffering
without limit. A worker-side exception surfaces on the consumer's pull once
the items already queued have drained.
Cleanup is cooperative -- the worker checks a stop flag between pages. The
owner must exit the `prefetch` context when it stops consuming; context exit
drops buffered pages and tells the worker to stop, without waiting for a page
pull already in progress (see `prefetch`).

``Wake`` is how a thread, the calling thread where a Ctrl-C lands among them,
waits for another thread to do something, and ``WakeAll`` how one event wakes
every thread waiting for it.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from boto3_s3.types import CancelToken

T = TypeVar("T")

# End-of-stream sentinel. A unique object (not None) so a legitimate None item
# -- should the source yield one -- is never mistaken for termination.
_END: object = object()

# How long a `Wake` wait on the main thread sleeps before it looks again by
# itself, to let a Ctrl-C through (design/overview.md section 3, "A wait does
# not poll"). Python acts on a Ctrl-C only once the main thread is back in
# Python code, and the lock wait does not end for one that came just as the
# wait began, nor on Windows before Python 3.14 for any
# (python/cpython#125541). A wait on another thread wakes only by a send.
_CTRL_C_SLICE = 1.0


class Wake:
    """Wake the thread that waits for a condition, safely under Ctrl-C.

    One thread waits at a time: it sleeps in `wait_until` until the condition
    holds, and any thread that may have made it hold calls `send`. The wait
    looks at the condition only when woken, so whatever can make it hold must
    send. Built on a ``threading.Lock`` used as a one-place signal - held means
    no wake-up is pending - whose ``acquire`` and ``release`` are C: a signal
    cannot leave them half-done, ``release`` never blocks (so a signal handler
    may send), and on POSIX a Ctrl-C during the ``acquire`` ends it (measured
    on Python 3.10 to 3.14). One that comes just as the wait begins does not,
    nor any on Windows before Python 3.14, so a wait on the main thread also
    looks again every `_CTRL_C_SLICE` by itself. A signal sent by
    ``_thread.interrupt_main`` does not end a blocking call either.
    ``threading.Event``, ``Condition`` and ``queue.Queue`` are Python code: a
    Ctrl-C landing inside can leave their lock held or released, and the
    thread that signals them then blocks for good or fails (measured under a
    storm of interrupts on Python 3.10 to 3.14).
    """

    def __init__(self) -> None:
        self._pending = threading.Lock()
        self._pending.acquire()

    def send(self) -> None:
        """Wake the waiting thread, if any; never blocks."""
        try:
            self._pending.release()
        except RuntimeError:
            pass  # a wake-up is pending already

    def wait_until(
        self, ready: Callable[[], bool], *, cancel_token: CancelToken | None = None
    ) -> None:
        """Sleep until ``ready()`` holds, looking again on each wake-up.

        A ``cancel_token`` wakes the wait when it is cancelled, for a
        condition that looks at it.
        """
        timeout = -1.0
        if threading.current_thread() is threading.main_thread():
            timeout = _CTRL_C_SLICE
        if cancel_token is None:
            while not ready():
                self._pending.acquire(timeout=timeout)
            return
        # Joined before the first look: a cancel after it sends, and one
        # before it is what that look sees.
        waiters = cancel_token._waiters  # pyright: ignore[reportPrivateUsage]
        try:
            waiters.add(self)
            while not ready():
                self._pending.acquire(timeout=timeout)
        finally:
            waiters.discard(self)


class WakeAll:
    """Wake every thread waiting for one event, each sleeping on its own `Wake`.

    A waiting thread adds its `Wake` before it first looks at the condition
    and discards it once done; `send` sends each one added at the time. No
    lock: the set's add and discard each change it in one step, and its copy
    reads it in one step, none of which a thread switch, a signal handler or
    the garbage collector can cut into, so `send` never blocks and a signal
    handler may call it.
    """

    def __init__(self) -> None:
        self._wakes: set[Wake] = set()

    def add(self, wake: Wake) -> None:
        self._wakes.add(wake)

    def discard(self, wake: Wake) -> None:
        self._wakes.discard(wake)

    def send(self) -> None:
        """Wake every thread waiting."""
        for wake in self._wakes.copy():
            wake.send()


@contextmanager
def prefetch(
    pages: Iterable[Sequence[T]],
    *,
    queue_size: int = 4,
    cancel_token: CancelToken | None = None,
) -> Generator[Iterator[T]]:
    """Run ``pages`` on a worker thread; yield a flattened iterator.

    ``pages`` is an iterable of chunks where pulling the next chunk is slow I/O
    (e.g. an S3 ``ListObjectsV2`` page). The worker overlaps that latency with
    the consumer's progress, buffering at most ``queue_size`` chunks in the
    queue plus the one it holds in hand while it waits for room
    (~ ``(queue_size + 1)`` x items-per-chunk in memory).

    Items are flattened from the chunks. A worker-side exception is re-raised on
    the consumer's next pull, after any items already queued. On context exit the
    worker is told to stop and unread chunks are dropped. The exit does not wait
    for a page pull already in flight, which can block for a full network
    timeout: the worker, a daemon thread, drops the page it brings back and ends
    by itself, starting no further pull, so whatever ``pages`` runs for that page
    (a filter) can still run after the exit (design/overview.md section 3). A
    `cancel_token`'s cancel, of either mode, ends the items at once, chunks
    still queued included, and stops the producer before its next page pull; a
    pull already in progress finishes behind the consumer, and its page is
    discarded. The consumer then sees the items end, so it must look at the
    token before it takes them for the whole listing. An empty chunk is
    not queued, so a source that yields one per filtered-out page lets the
    worker stop between those pages at no queue cost.

    `queue_size` must be positive: with none, the worker could never queue a
    page. The pages wait on a ``queue.SimpleQueue`` taken from without
    waiting; the consumer and the worker each sleep on a `Wake` the other
    sends (`Wake` says why).
    """
    if queue_size <= 0:
        raise ValueError(f"queue_size must be positive (got {queue_size!r})")
    ready: queue.SimpleQueue[Sequence[T] | object] = queue.SimpleQueue()
    # Sent by the worker for each page it queues and when it finishes.
    arrived = Wake()
    # Sent by the consumer for each page it takes and when it stops.
    room = Wake()
    stop = False
    error: list[BaseException] = []

    def _cancelled() -> bool:
        return cancel_token is not None and cancel_token.cancelled

    def _stopping() -> bool:
        return stop or _cancelled()

    def _produce() -> None:
        try:
            source = iter(pages)
            while not _stopping():
                try:
                    chunk = next(source)
                except StopIteration:
                    break
                if _stopping():
                    break
                # An empty chunk (a page a filter emptied) still came back
                # from its own pull, so the stop check above ran; it carries
                # nothing for the consumer.
                if chunk:
                    room.wait_until(
                        lambda: ready.qsize() < queue_size or _stopping(),
                        cancel_token=cancel_token,
                    )
                    if _stopping():
                        break
                    ready.put(chunk)
                    arrived.send()
        except BaseException as exc:  # re-raised on the consumer side
            error.append(exc)
        finally:
            ready.put(_END)
            arrived.send()

    worker = threading.Thread(target=_produce, name="boto3-s3-prefetch", daemon=True)
    worker.start()

    def _consume() -> Iterator[T]:
        while True:
            # A cancel ends the items at once: the worker drops whatever it
            # still brings back, so nothing is worth waiting for.
            arrived.wait_until(lambda: not ready.empty() or _cancelled(), cancel_token=cancel_token)
            if _cancelled():
                return
            item = ready.get_nowait()
            room.send()
            if item is _END:
                if error:
                    raise error[0]
                return
            chunk: Sequence[T] = item  # pyright: ignore[reportAssignmentType]
            for entry in chunk:
                if _cancelled():  # the rest of the chunk in hand as well
                    return
                yield entry

    try:
        yield _consume()
    finally:
        stop = True
        room.send()
        # Drop the buffered pages.
        try:
            while True:
                ready.get_nowait()
        except queue.Empty:
            pass


__all__ = ["Wake", "WakeAll", "prefetch"]
