"""Threading helpers for overlapping I/O with consumption.

The Storage ``scan`` path is a lazy generator, but each page fetch
(``ListObjectsV2`` for S3, one ``os.scandir`` per directory for local) is I/O the
consumer would otherwise block on. ``prefetch`` runs the page iterable on a
worker thread and hands back a flattened iterator, so the next page is in flight
while the consumer processes the current one.

Backpressure is bounded by a ``queue.Queue``: the worker blocks when the
queue is full, so a slow consumer throttles a fast producer instead of buffering
without limit. A worker-side exception surfaces on the consumer's next pull.
Cleanup is cooperative -- the worker checks a stop flag between puts. The
owner must exit the `prefetch` context when it stops consuming; context exit
drops buffered pages and waits for a page pull already in progress before
returning, so no worker survives the operation.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from boto3_s3.types import CancelToken

T = TypeVar("T")

# End-of-stream sentinel. A unique object (not None) so a legitimate None item
# -- should the source yield one -- is never mistaken for termination.
_END: object = object()

# How long the worker blocks on a put before re-checking the stop flag. Short
# enough that an early-break consumer tears down promptly; long enough that an
# idle worker does not spin.
_STOP_POLL_SECONDS = 0.1


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
    the consumer's progress, buffering at most ``queue_size`` chunks
    (~ ``queue_size`` x items-per-chunk in memory).

    Items are flattened from the chunks. A worker-side exception is re-raised on
    the consumer's next pull, after any items already queued. On context exit the
    worker is signalled to stop and joined; unread chunks are dropped. A
    `cancel_token` stops the producer before its next page pull; a pull already
    in progress finishes, but its returned page is discarded.
    """
    q: queue.Queue[Sequence[T] | object] = queue.Queue(maxsize=queue_size)
    stop = threading.Event()
    error: list[BaseException] = []

    def _cancelled() -> bool:
        return cancel_token is not None and cancel_token.cancelled

    def _stopping() -> bool:
        return stop.is_set() or _cancelled()

    def _put(item: Sequence[T] | object, *, finish: bool = False) -> None:
        # Block on a full queue, but wake periodically to honour an early-break
        # consumer rather than deadlocking on the put.
        while not stop.is_set() and (finish or not _cancelled()):
            try:
                q.put(item, timeout=_STOP_POLL_SECONDS)
                return
            except queue.Full:
                continue

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
                _put(chunk)
        except BaseException as exc:  # re-raised on the consumer side
            error.append(exc)
        finally:
            # Always deliver the end marker via the same stop-poll loop: if every
            # chunk fit in the queue the producer finishes with the queue full,
            # and a single drop-on-timeout here would leave the consumer's untimed
            # get() blocked forever after it drains.
            _put(_END, finish=True)

    worker = threading.Thread(target=_produce, name="boto3-s3-prefetch", daemon=True)
    worker.start()

    def _consume() -> Iterator[T]:
        while True:
            item = q.get()
            if item is _END:
                if error:
                    raise error[0]
                return
            chunk: Sequence[T] = item  # pyright: ignore[reportAssignmentType]
            yield from chunk

    try:
        yield _consume()
    finally:
        stop.set()
        # Free a producer blocked on put() by making room.
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass
        # A page pull already in progress is accepted work: wait for it to
        # finish rather than returning with a live worker. Botocore's request
        # timeouts bound a stuck S3 fetch; local/custom backends must likewise
        # make their page producer eventually return.
        worker.join()


__all__ = ["prefetch"]
