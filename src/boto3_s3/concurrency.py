"""Threading helpers for overlapping I/O with consumption.

The Storage ``scan`` path is a lazy generator, but each page fetch
(``ListObjectsV2`` for S3, one ``os.scandir`` per directory for local) is I/O the
consumer would otherwise block on. ``prefetch`` runs the page iterable on a
worker thread and hands back a flattened iterator, so the next page is in flight
while the consumer processes the current one.

Backpressure is bounded by a ``queue.Queue``: the worker blocks when the
queue is full, so a slow consumer throttles a fast producer instead of buffering
without limit. A worker-side exception surfaces on the consumer's next pull.
Cleanup is cooperative -- the worker checks a stop flag between puts, so a
consumer that abandons the iterator early (``break``) tears the worker down
rather than leaking it.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Generator, Iterable, Iterator, Sequence
from contextlib import contextmanager
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# End-of-stream sentinel. A unique object (not None) so a legitimate None item
# -- should the source yield one -- is never mistaken for termination.
_END: object = object()

# How long the worker blocks on a put before re-checking the stop flag. Short
# enough that an early-break consumer tears down promptly; long enough that an
# idle worker does not spin.
_STOP_POLL_SECONDS = 0.1

# How long context exit waits for the worker to join before giving up. The
# worker is a daemon thread, so the process reaps it regardless; this only
# bounds the cleanup wait when a page fetch is still blocked in I/O.
_JOIN_TIMEOUT_SECONDS = 2.0


@contextmanager
def prefetch(pages: Iterable[Sequence[T]], *, queue_size: int = 4) -> Generator[Iterator[T]]:
    """Run ``pages`` on a worker thread; yield a flattened iterator.

    ``pages`` is an iterable of chunks where pulling the next chunk is slow I/O
    (e.g. an S3 ``ListObjectsV2`` page). The worker overlaps that latency with
    the consumer's progress, buffering at most ``queue_size`` chunks
    (~ ``queue_size`` x items-per-chunk in memory).

    Items are flattened from the chunks. A worker-side exception is re-raised on
    the consumer's next pull, after any items already queued. On context exit the
    worker is signalled to stop and joined; unread chunks are dropped.
    """
    q: queue.Queue[Sequence[T] | object] = queue.Queue(maxsize=queue_size)
    stop = threading.Event()
    error: list[BaseException] = []

    def _put(item: Sequence[T] | object) -> None:
        # Block on a full queue, but wake periodically to honour an early-break
        # consumer rather than deadlocking on the put.
        while not stop.is_set():
            try:
                q.put(item, timeout=_STOP_POLL_SECONDS)
                return
            except queue.Full:
                continue

    def _produce() -> None:
        try:
            for chunk in pages:
                if stop.is_set():
                    break
                _put(chunk)
        except BaseException as exc:  # re-raised on the consumer side
            error.append(exc)
        finally:
            # Always deliver the end marker via the same stop-poll loop: if every
            # chunk fit in the queue the producer finishes with the queue full,
            # and a single drop-on-timeout here would leave the consumer's untimed
            # get() blocked forever after it drains.
            _put(_END)

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
        worker.join(timeout=_JOIN_TIMEOUT_SECONDS)
        if worker.is_alive():
            logger.warning(
                "prefetch worker %r did not stop within %.1fs; thread leaked "
                "(a page fetch is likely still blocked in I/O).",
                worker.name,
                _JOIN_TIMEOUT_SECONDS,
            )


__all__ = ["prefetch"]
