"""Sync pairing and pair-decision material (the engine room of ``S3.sync``).

``aws s3 sync`` is a two-layer pipeline. Layer one is *visibility*: each
side's listing is pruned independently (``--exclude`` / ``--include``,
folder markers) before the sides ever meet - that pruning lives in the
scan path, not here. Layer two is *pair decisions*: the surviving streams
are merge-joined by compare key, then per-pair decisions select what to
copy or delete. This module is layer two's material:

- ``Comparator`` pairs two key-ordered streams into ``MergedPair``
  records - a ``SyncPair`` (both sides), ``SrcOnlyPair`` (source only), or
  ``DestOnlyPair`` (destination only) per key, the shape telling which
  side(s) hold it. It is a **pure pairer** - every key on either side comes
  out and no copy/delete judgment happens here (unlike aws-cli's comparator,
  which buries its strategy calls in the merge loop; splitting them keeps each
  side replaceable). It only stamps the run's direction (``transfer_type``) onto each
  pair, as context for the filters.
- A ``PairFilter`` is the **update** judgment: a predicate over the
  both-sides ``SyncPair`` where ``True`` re-copies the source over the
  destination. It
  is what ``S3.sync(update_filter=...)`` selects - the size+time default, a
  content strategy (``EtagComparison`` / ``ChecksumComparison``), or a caller's
  own. A ``SyncPair`` carries both sides by construction, so the filter reads
  ``pair.src`` / ``pair.dest`` directly; the new (``SrcOnlyPair``) lane is
  governed by ``create_filter`` and the delete (``DestOnlyPair``) lane by
  ``delete_filter``, each a ``FileFilter`` over the one side it has.
- ``compare_size_time`` is that size+time default (aws-cli's stock
  judgment, with the ``size_only`` / ``exact_timestamps`` tuners). It is not a
  re-exported building block (kept out of ``__all__``); it is the judgment
  behind ``AwsCliComparison``, the form ``S3.sync``
  selects for ``update_filter=None``. The direction is read from
  ``pair.transfer_type``.
- ``ContentComparison`` is the shared template of the content
  strategies (``EtagComparison`` / ``ChecksumComparison``): the decision
  skeleton every content comparison runs before its leaf digest work - the
  guards, the ``check_size`` safeguard, and the direction dispatch - lives
  here once, so the two strategies cannot drift apart on it.
- ``all_of`` / ``any_of`` compose same-signature predicates - chiefly
  the ``filter=`` visibility predicates over ``FileInfo``
  (a copy strategy is *chosen*, not composed).

Everything here is pure logic over ``FileInfo``; no
AWS SDK module is imported.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

from boto3_s3.types import FileInfo, S3FileInfo, TransferType

if TYPE_CHECKING:
    from concurrent.futures import Executor

    from boto3_s3.storage import Storage

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncPair:
    """A compare key present on **both** sides of a sync - the update pair.

    The shape ``S3.sync``'s ``update_filter`` judges (aws-cli's
    ``file_at_src_and_dest`` strategy slot). ``src`` / ``dest`` are the two
    sides' listing entries, both always present - a filter reads
    ``pair.src.size`` / ``pair.dest.mtime`` directly, with no ``None``
    handling. ``key`` is the compare key - the entry's path relative to its
    side's listing (directory-relative for a local side, ``Prefix``-relative
    for an S3 side), ``/``-separated on every platform - so name-based filters
    need not care where either side lives. ``transfer_type`` is the sync's
    transfer direction (UPLOAD / DOWNLOAD / COPY), stamped on every pair so a
    pair filter can apply the direction-asymmetric rules without being told the
    route.

    The backend each side was listed from rides on that side's entry
    (``pair.src.storage`` / ``pair.dest.storage``, stamped by the producing
    ``Storage.scan``): a content ``update_filter=`` strategy reads the non-S3
    side's bytes through its ``Storage.open`` - so content comparison works for
    any backend, not just a local filesystem.
    """

    key: str
    transfer_type: TransferType
    src: FileInfo
    dest: FileInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class SrcOnlyPair:
    """A compare key present only on the source side - a new entry.

    ``S3.sync`` routes it to the create lane (``create_filter``; aws-cli's
    ``file_not_at_dest`` slot, where copying is hard-coded). ``key`` and
    ``transfer_type`` are as on ``SyncPair``; ``src`` is the source listing
    entry, and there is no ``dest`` attribute at all.
    """

    key: str
    transfer_type: TransferType
    src: FileInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class DestOnlyPair:
    """A compare key present only on the destination side - an orphan.

    The delete candidate: ``S3.sync`` routes it to the delete lane
    (``delete_filter``; aws-cli's ``file_not_at_src`` slot). ``key`` and
    ``transfer_type`` are as on ``SyncPair``; ``dest`` is the destination
    listing entry, and there is no ``src`` attribute at all.
    """

    key: str
    transfer_type: TransferType
    dest: FileInfo


# What `Comparator.compare` yields: each merge-joined key as exactly one of the
# three pair shapes, telling by type which side(s) hold it.
MergedPair = SrcOnlyPair | SyncPair | DestOnlyPair

# The update judgment: a predicate over the both-sides pair, `True` = copy.
PairFilter = Callable[[SyncPair], bool]


@dataclass(frozen=True, slots=True)
class ParallelFilter(Generic[_T]):
    """Run one ``S3.sync`` lane's decision on a caller-supplied thread pool.

    A value container, **not** a callable: ``S3.sync`` recognizes it in any of
    ``create_filter`` / ``update_filter`` / ``delete_filter`` and runs the wrapped
    ``decide`` on ``executor`` instead of on the calling thread, so a lane whose
    predicate does per-entry I/O decides many entries concurrently - a content
    ``update_filter=`` strategy (``EtagComparison`` / ``ChecksumComparison``), or a
    ``create`` / ``delete`` filter that reads bytes / object tags / attributes.
    Passing it is observationally identical to passing ``decide`` bare, only
    faster - **except** that parallelizing ``create_filter`` makes the
    ``--case-conflict`` "first key wins" order non-deterministic (a library-only
    knob, so no ``aws s3`` parity is at stake). The result set and the exit are
    otherwise unchanged.

    ``executor`` is **required** and owned by the caller: ``S3.sync`` neither
    creates nor shuts it down. Reuse across ``sync`` calls, and sharing one pool
    across lanes (pass the same object to each ``ParallelFilter``) or giving each
    lane its own, are the caller's to arrange - but the ``sync`` call itself must
    **not** run as a task on that same pool: the lane blocks its thread waiting
    for decide futures that need a free worker, so a bounded pool driving both
    deadlocks. The wrapped ``decide`` runs on that
    pool's threads, so it must be thread-safe;
    ``ChecksumComparison`` and
    ``EtagComparison`` are (read-only over their
    fields; ``ChecksumComparison``'s S3-side clients are built at construction,
    and a botocore client is safe to share for concurrent calls). A
    ``ProcessPoolExecutor`` will not work (the predicate and its S3 client are not
    picklable) - the pool must be thread-based.

    ``_T`` is the wrapped predicate's argument: ``SyncPair`` for a
    ``update_filter`` (a ``PairFilter``), ``FileInfo``
    for ``create_filter`` / ``delete_filter`` (a
    ``FileFilter``).
    """

    decide: Callable[[_T], bool]
    executor: Executor


def _byte_ordered(
    entries: Iterable[tuple[str, FileInfo]], side: str
) -> Iterator[tuple[str, FileInfo]]:
    """Dev-only pass-through that asserts a side ascends by ``compare_key``.

    ``Comparator.compare``'s merge-join assumes both sides arrive in UTF-8
    byte order (what a ``SORTABLE_SCAN`` backend promises; ``str`` order is code-point
    = byte order). A custom backend that declares ``SORTABLE_SCAN`` but yields out of
    order would *silently* mis-pair - phantom src-only / dest-only pairs, and with
    ``--delete`` the deletion of files present on both sides. This trips a loud
    ``AssertionError`` in tests instead. Guarded by ``if __debug__`` at the call
    site, so it is compiled out entirely under ``-O`` (zero production cost).
    """
    prev: str | None = None
    for key, info in entries:
        assert prev is None or key >= prev, (
            f"{side} sync stream is not byte-ordered by compare_key "
            f"({prev!r} then {key!r}); a SORTABLE_SCAN backend must yield ascending keys"
        )
        prev = key
        yield key, info


@dataclass(frozen=True)
class Comparator:
    """Merge-join two key-ordered listing streams into ``MergedPair``s.

    The classic sorted-merge (aws-cli ``Comparator.call``): advance whichever
    side holds the smaller key, pair equal keys, and flush the survivor once
    one side is exhausted.
    Inputs must be ascending by compare key - what a ``SORTABLE_SCAN``
    backend's ``scan(sort=True)`` promises (S3 byte order; the local walk sorts
    to match) - and
    the merge itself never compares sizes or times: that is the lane filters'
    job (a ``PairFilter`` for the ``SyncPair``s, ``create_filter`` /
    ``delete_filter`` for the one-sided shapes). ``transfer_type`` (the run's direction) is
    stamped onto every emitted pair - context, not a judgment. Each side's backend
    already rides on its ``FileInfo`` (``pair.src.storage`` / ``pair.dest.storage``,
    stamped by the producing ``Storage.scan``), so a content ``update_filter=``
    strategy can open the non-S3 side of any pair without the merge threading it.
    """

    transfer_type: TransferType

    def compare(
        self,
        src_entries: Iterable[tuple[str, FileInfo]],
        dest_entries: Iterable[tuple[str, FileInfo]],
    ) -> Iterator[MergedPair]:
        """Yield every key on either side as its ``MergedPair`` shape.

        An equal key on both sides comes out as a ``SyncPair``, a key with no
        destination match as a ``SrcOnlyPair``, one with no source match as a
        ``DestOnlyPair``. ``src_entries`` / ``dest_entries`` are
        ``(compare_key, info)`` streams, lazily consumed - pairing streams
        page-by-page listings without materializing either side.
        """
        transfer_type = self.transfer_type
        if __debug__:  # dev guard: catch an unsorted SORTABLE_SCAN side (compiled out under -O)
            src_entries = _byte_ordered(src_entries, "source")
            dest_entries = _byte_ordered(dest_entries, "destination")
        src_iter = iter(src_entries)
        dest_iter = iter(dest_entries)
        src = next(src_iter, None)
        dest = next(dest_iter, None)
        while src is not None and dest is not None:
            if src[0] < dest[0]:
                yield SrcOnlyPair(key=src[0], transfer_type=transfer_type, src=src[1])
                src = next(src_iter, None)
            elif src[0] > dest[0]:
                yield DestOnlyPair(key=dest[0], transfer_type=transfer_type, dest=dest[1])
                dest = next(dest_iter, None)
            else:
                yield SyncPair(key=src[0], transfer_type=transfer_type, src=src[1], dest=dest[1])
                src = next(src_iter, None)
                dest = next(dest_iter, None)
        while src is not None:
            yield SrcOnlyPair(key=src[0], transfer_type=transfer_type, src=src[1])
            src = next(src_iter, None)
        while dest is not None:
            yield DestOnlyPair(key=dest[0], transfer_type=transfer_type, dest=dest[1])
            dest = next(dest_iter, None)


def compare_size_time(
    pair: SyncPair, *, size_only: bool = False, exact_timestamps: bool = False
) -> bool:
    """``S3.sync``'s internal default judgment (aws-cli size + last-modified).

    Not a public building block: it implements
    ``AwsCliComparison``, which ``S3.sync`` selects
    for ``update_filter=None``.
    The transfer direction comes from ``pair.transfer_type`` (the time rule is
    direction-asymmetric). For each pair:

    - ``size_only``: copy iff the sizes differ (aws-cli's ``SizeOnlySync``).
      Ignored when ``exact_timestamps`` is also set: the flags fill the same
      aws-cli strategy slot and its override order makes ``exact_timestamps``
      win,
    - otherwise copy iff the sizes differ *or* last-modified does not rule
      the copy out (aws-cli's ``SizeAndLastModifiedSync``): an upload/copy is
      redundant when the destination is at least as new as the source, a
      download when the destination is at least as old. ``exact_timestamps``
      tightens the download rule to require exactly equal times (aws-cli's
      ``ExactTimestampsSync``; uploads/copies are unaffected).

    The ``no_overwrite`` write-guard is orthogonal and lives in the ``S3.sync``
    loop, not here, so it composes with any ``update_filter=`` strategy. Comparisons
    run at full ``timedelta`` precision (aws-cli's ``total_seconds``). A missing
    ``size`` or ``mtime`` on either side counts as a difference - aws-cli never
    faces one (its listings always carry both), so leaning toward copying is the
    permissive reading.
    """
    src, dest = pair.src, pair.dest
    same_size = src.size is not None and src.size == dest.size
    if size_only and not exact_timestamps:
        return not same_size
    return not same_size or not _times_match(src, dest, pair.transfer_type, exact_timestamps)


def _times_match(src: FileInfo, dest: FileInfo, transfer_type: TransferType, exact: bool) -> bool:
    """aws-cli's ``compare_time``: True when last-modified makes the copy redundant."""
    if src.mtime is None or dest.mtime is None:
        return False
    delta = (dest.mtime - src.mtime).total_seconds()
    if transfer_type is TransferType.DOWNLOAD:
        return delta == 0 if exact else delta <= 0
    return delta >= 0


# Content strategies' streaming read granularity, bounding memory use.
READ_CHUNK = 1024 * 1024


class ContentComparison:
    """The shared skeleton of the content ``update_filter=`` strategies (``True`` = copy).

    Not itself a strategy: ``EtagComparison`` / ``ChecksumComparison`` extend it
    with their leaf digest work. What lives here is everything the two must
    agree on - the ``check_size`` size-mismatch decision, which side is the S3
    side, and the split into the two hooks - so the strategies cannot silently
    drift apart on the decision shape.

    Which side is the S3 object (the stored digest to compare against) is decided
    by **type** - the ``S3FileInfo`` side - not by the transfer direction, so the
    other ("readable") side may be any backend: its bytes are read through the
    ``Storage.open`` carried on that side's entry (``pair.src.storage`` /
    ``pair.dest.storage``), not a local filesystem path. Both sides S3 -> the
    s3-to-s3 digest compare; neither side S3 -> nothing to compare against, so copy.
    """

    __slots__ = ()

    # No storage slot in the base - each subclass declares its own.
    check_size: bool

    def __call__(self, pair: SyncPair) -> bool:
        src, dest = pair.src, pair.dest
        if (
            self.check_size
            and src.size is not None
            and dest.size is not None
            and src.size != dest.size
        ):
            # Differing sizes mean differing content - copy without trusting the
            # stored digest (MD5 / a fixed-width CRC can collide; the size is
            # independent evidence) and without reading the readable side.
            return True
        src_is_s3 = isinstance(src, S3FileInfo)
        dest_is_s3 = isinstance(dest, S3FileInfo)
        if src_is_s3 and dest_is_s3:
            return self._copy_differs(src, dest)
        if dest_is_s3 and not src_is_s3:  # upload-shaped: src is the readable side
            return self._readable_remote_differ(src.storage, src, dest, pair.transfer_type)
        if src_is_s3 and not dest_is_s3:  # download-shaped: dest is the readable side
            return self._readable_remote_differ(dest.storage, dest, src, pair.transfer_type)
        # Neither side is an S3 object: no stored digest to compare against -> copy.
        return True

    def _copy_differs(self, src: FileInfo, dest: FileInfo) -> bool:
        """s3-to-s3: whether the two S3 sides' stored digests disagree."""
        raise NotImplementedError

    def _readable_remote_differ(
        self,
        storage: Storage | None,
        readable: FileInfo,
        remote: FileInfo,
        transfer_type: TransferType,
    ) -> bool:
        """Whether the readable side's content differs from the S3 ``remote`` side.

        ``storage`` opens the readable side (``storage.open(readable.compare_key,
        "rb")``) - any backend, not just a local file; ``None`` (a pair built
        without a backend) means it cannot be read, so the strategy copies.
        ``transfer_type`` tells which endpoint ``remote`` belongs to (UPLOAD -> the
        destination, DOWNLOAD -> the source) for a strategy that must reach that
        endpoint's client.
        """
        raise NotImplementedError


def all_of(*predicates: Callable[[_T], bool]) -> Callable[[_T], bool]:
    """A predicate passing only when every given predicate passes.

    Composes any same-signature single-argument predicates - pair filters
    (``PairFilter``) and plain ``FileInfo`` visibility predicates
    alike. With no predicates it always passes (like ``all([])``).
    """

    def combined(value: _T) -> bool:
        return all(predicate(value) for predicate in predicates)

    return combined


def any_of(*predicates: Callable[[_T], bool]) -> Callable[[_T], bool]:
    """A predicate passing when at least one given predicate passes.

    The ``or`` counterpart of ``all_of``. With no predicates it never
    passes (like ``any([])``).
    """

    def combined(value: _T) -> bool:
        return any(predicate(value) for predicate in predicates)

    return combined


__all__ = [
    "Comparator",
    "DestOnlyPair",
    "MergedPair",
    "PairFilter",
    "ParallelFilter",
    "SrcOnlyPair",
    "SyncPair",
    "all_of",
    "any_of",
]
