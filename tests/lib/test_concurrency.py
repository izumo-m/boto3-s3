"""Unit tests for boto3_s3.concurrency.prefetch.

The worker is a background thread; tests drive it through in-memory page lists
so timing stays deterministic. Where teardown is asserted, the named worker
thread must be gone once the context manager exits (it joins on exit).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest

from boto3_s3.concurrency import prefetch

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
