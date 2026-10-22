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
- A ``MergedPairFilter`` is the same judgment for *all three* shapes at once -
  what ``S3.sync(pair_filter=...)`` takes in place of the three lane filters,
  so an application needing one view of every decision (a journal, a
  confirmation flow, statistics) writes one function rather than wiring three.
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
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from boto3_s3.exceptions import Boto3S3Error
from boto3_s3.types import FileInfo, S3FileInfo, TransferType

if TYPE_CHECKING:
    from concurrent.futures import Executor
    from typing import BinaryIO

    from boto3_s3.storage import Storage

_T = TypeVar("_T")
# What a content strategy's digest of the readable side returns.
_R = TypeVar("_R")


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncPair:
    """A compare key present on **both** sides of a sync - the update pair.

    The shape ``S3.sync``'s ``update_filter`` judges (aws-cli's
    ``file_at_src_and_dest`` strategy slot). ``src`` / ``dest`` are the two
    sides' listing entries, both always present - a filter reads
    ``pair.src.size`` / ``pair.dest.mtime`` directly, with no ``None``
    handling. ``compare_key`` is the entry's path relative to its
    side's listing (directory-relative for a local side, ``Prefix``-relative
    for an S3 side), ``/``-separated on every platform - so name-based filters
    need not care where either side lives. ``transfer_type`` is whatever the
    ``Comparator`` was built with, copied onto every pair verbatim -
    ``S3.sync`` supplies the run's direction (UPLOAD / DOWNLOAD / COPY) there,
    so a pair filter can apply the direction-asymmetric rules without being
    told the route.

    The backend each side was listed from rides on that side's entry
    (``pair.src.storage`` / ``pair.dest.storage``, stamped by the producing
    ``Storage.scan``): a content ``update_filter=`` strategy reads the non-S3
    side's bytes through its ``Storage.open`` - so content comparison works for
    any backend, not just a local filesystem.
    """

    compare_key: str
    transfer_type: TransferType
    src: FileInfo
    dest: FileInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class SrcOnlyPair:
    """A compare key present only on the source side - a new entry.

    ``S3.sync`` routes it to the create lane (``create_filter``; aws-cli's
    ``file_not_at_dest`` slot, where copying is hard-coded). ``compare_key`` and
    ``transfer_type`` are as on ``SyncPair``; ``src`` is the source listing
    entry, and there is no ``dest`` attribute at all.
    """

    compare_key: str
    transfer_type: TransferType
    src: FileInfo


@dataclass(frozen=True, slots=True, kw_only=True)
class DestOnlyPair:
    """A compare key present only on the destination side - an orphan.

    The delete candidate: ``S3.sync`` routes it to the delete lane
    (``delete_filter``; aws-cli's ``file_not_at_src`` slot). ``compare_key`` and
    ``transfer_type`` are as on ``SyncPair``; ``dest`` is the destination
    listing entry, and there is no ``src`` attribute at all.
    """

    compare_key: str
    transfer_type: TransferType
    dest: FileInfo


# What `Comparator.compare` yields: each merge-joined entry as exactly one of
# the three pair shapes, telling by type which side(s) hold it. Per entry -
# one per key because a listing names each compare key once (the storage
# protocol).
MergedPair = SrcOnlyPair | SyncPair | DestOnlyPair

# The update judgment: a predicate over the both-sides pair, `True` = copy.
PairFilter = Callable[[SyncPair], bool]

# One judgment for every merged pair, whatever its shape: `True` = take that
# pair's default action (copy a new or an updated entry, delete an orphan).
# What `S3.sync(pair_filter=...)` takes - one callable in place of the three
# lane filters, so a caller that must see the whole decision stream (a journal,
# a confirmation flow, statistics) writes one function instead of wiring three.
MergedPairFilter = Callable[[MergedPair], bool]


@dataclass(frozen=True, slots=True)
class ParallelFilter(Generic[_T]):
    """Run one ``S3.sync`` lane's decision on a caller-supplied thread pool.

    A value container, **not** a callable: ``S3.sync`` recognizes it in any of
    ``create_filter`` / ``update_filter`` / ``delete_filter`` and runs the wrapped
    ``decide`` on ``executor`` instead of on the calling thread, so a lane whose
    predicate does per-entry I/O decides many entries concurrently - a content
    ``update_filter=`` strategy (``EtagComparison`` / ``ChecksumComparison``), or a
    ``create`` / ``delete`` filter that reads bytes / object tags / attributes.
    For a pure (stateless) predicate the decisions are the same as passing
    ``decide`` bare, only
    faster (a stateful one can observe the concurrency); what changes is
    ordering: pooled decisions emit their results in
    completion order rather than compare-key order (design/sync.md section 10), and
    parallelizing ``create_filter`` makes the
    ``--case-conflict`` "first key wins" order non-deterministic (a library-only
    knob, so no ``aws s3`` parity is at stake). The result set and the exit are
    otherwise unchanged, with one exception: a wrapped ``delete_filter`` on a
    local (or custom) destination, where a delete is a synchronous removal. The
    pair loop moves on while delete decisions are still outstanding, so a later
    pair can no longer count on an earlier orphan being gone - a self-aliasing
    tree lists a file under its second name and fails that delete, a new
    entry that needs an orphan file's name as a directory (``a`` to delete,
    ``a/b`` to create) is transferred first and fails, and on a
    case-insensitive filesystem - where an orphan and a new entry differing
    only in case are one file, whose survival is a matter of timing on the
    plain lane too when the new entry sorts first, as it is for aws-cli - the
    order in which the orphan sorts first (``A.txt`` to delete, ``a.txt`` to
    create) becomes a race as well: a decision that outlasts the transfer
    removes the new file with nothing recorded as failed.

    ``executor`` is **required** and owned by the caller: ``S3.sync`` neither
    creates nor shuts it down. Reuse across ``sync`` calls, and sharing one pool
    across lanes (pass the same object to each ``ParallelFilter``) or giving each
    lane its own, are the caller's to arrange - but the ``sync`` call itself must
    **not** run as a task on that same pool: the lane blocks its thread waiting
    for decide futures that need a free worker, so a bounded pool driving both
    can deadlock once no worker is left free. The wrapped ``decide`` runs on that
    pool's threads, so it must be thread-safe;
    ``ChecksumComparison`` and
    ``EtagComparison`` are (read-only over their
    fields; ``ChecksumComparison``'s S3-side clients are built at construction,
    and a botocore client is safe to share for concurrent calls). A
    ``ProcessPoolExecutor`` will not work (the wrapped predicate and the
    pair's storage-backed entries close over live handles - S3 clients,
    ``FileInfo.storage`` - that do not pickle) - the pool must be thread-based.

    ``_T`` is the wrapped predicate's argument: ``SyncPair`` for a
    ``update_filter`` (a ``PairFilter``), ``FileInfo``
    for ``create_filter`` / ``delete_filter`` (a
    ``FileFilter``).
    """

    decide: Callable[[_T], bool]
    executor: Executor


@dataclass(frozen=True)
class Comparator:
    """Merge-join two key-ordered listing streams into ``MergedPair``s.

    The classic sorted-merge (aws-cli ``Comparator.call``): advance whichever
    side holds the smaller key, pair equal keys, and flush the survivor once
    one side is exhausted.
    Inputs are expected ascending by compare key - what a ``SORTABLE_SCAN``
    backend's ``scan(sort=True)`` promises (S3 byte order; the local walk sorts
    to match) - but are not checked: an unordered one mis-pairs exactly as
    aws-cli's merge mis-pairs it (``compare``). The merge itself never
    compares sizes or times: that is the lane filters'
    job (a ``PairFilter`` for the ``SyncPair``s, ``create_filter`` /
    ``delete_filter`` for the one-sided shapes). Every input entry becomes one
    pair, which is one pair per key because a listing names each compare key
    once (the storage protocol; not checked here).
    ``transfer_type`` is
    stamped onto every emitted pair verbatim - context, not a judgment, and
    neither validated nor interpreted here; ``S3.sync`` puts the run's
    direction in it. Each side's backend
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
        """Yield every entry on either side as its ``MergedPair`` shape.

        An equal key on both sides comes out as a ``SyncPair``, a key with no
        destination match as a ``SrcOnlyPair``, one with no source match as a
        ``DestOnlyPair``. ``src_entries`` / ``dest_entries`` are
        ``(compare_key, info)`` streams, lazily consumed - pairing streams
        page-by-page listings without materializing either side.

        An input that is not ascending is merged all the same, step for step
        as aws-cli's ``Comparator.call`` merges it: each step compares only the
        two entries in hand, so a key that arrives out of order on one side
        surfaces as a ``SrcOnlyPair`` and a ``DestOnlyPair`` instead of one
        ``SyncPair`` - which ``sync``'s delete lane then acts on, deleting the
        destination's copy of a key the source has too, exactly as aws-cli
        does against an S3-compatible endpoint whose listing does not sort.
        """
        transfer_type = self.transfer_type
        src_iter = iter(src_entries)
        dest_iter = iter(dest_entries)
        src = next(src_iter, None)
        dest = next(dest_iter, None)
        while src is not None and dest is not None:
            if src[0] < dest[0]:
                yield SrcOnlyPair(compare_key=src[0], transfer_type=transfer_type, src=src[1])
                src = next(src_iter, None)
            elif src[0] > dest[0]:
                yield DestOnlyPair(compare_key=dest[0], transfer_type=transfer_type, dest=dest[1])
                dest = next(dest_iter, None)
            else:
                yield SyncPair(
                    compare_key=src[0], transfer_type=transfer_type, src=src[1], dest=dest[1]
                )
                src = next(src_iter, None)
                dest = next(dest_iter, None)
        while src is not None:
            yield SrcOnlyPair(compare_key=src[0], transfer_type=transfer_type, src=src[1])
            src = next(src_iter, None)
        while dest is not None:
            yield DestOnlyPair(compare_key=dest[0], transfer_type=transfer_type, dest=dest[1])
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
            return self._readable_remote_differ(src, dest)
        if src_is_s3 and not dest_is_s3:  # download-shaped: dest is the readable side
            return self._readable_remote_differ(dest, src)
        # Neither side is an S3 object: no stored digest to compare against -> copy.
        return True

    def _copy_differs(self, src: FileInfo, dest: FileInfo) -> bool:
        """s3-to-s3: whether the two S3 sides' stored digests disagree."""
        raise NotImplementedError

    def _readable_remote_differ(self, readable: FileInfo, remote: FileInfo) -> bool:
        """Whether the readable side's content differs from the S3 ``remote`` side.

        The readable side is read through its own backend (`_read_readable`) -
        any backend, not just a local file - and a strategy that queries the S3
        side reaches it through ``remote.storage``.
        """
        raise NotImplementedError

    @staticmethod
    def _read_readable(readable: FileInfo, digest: Callable[[BinaryIO], _R]) -> _R:
        """Open the readable side as ``storage.open(compare_key, "rb")`` and digest it.

        Relies on the entry's ``storage`` and ``compare_key``, which every scan
        stamps (a hand-built pair must set both). A ``LocalStorage`` side is
        the library's own file: its ``open`` translates its own failure, and a
        raw ``OSError`` from the stream (a fault mid-read or on close) takes
        the local taxonomy's category too. Any other backend is the caller's
        code (design/overview.md section 3): a ``Boto3S3Error`` it raises
        passes as it is, and anything else becomes the base error with the
        original as ``__cause__``. Either way the exception leaves the
        strategy, aborting a ``sync`` as any raising predicate does.
        """
        storage = cast("Storage", readable.storage)
        key = cast("str", readable.compare_key)
        try:
            with storage.open(key, "rb") as fh:
                return digest(fh)
        except Boto3S3Error:
            raise
        except Exception as exc:
            # Deferred import: this module stays SDK-free at import time.
            from boto3_s3.localstorage import LocalStorage, translate_os_error

            if not isinstance(storage, LocalStorage):
                raise Boto3S3Error(str(exc), operation="sync", key=key) from exc
            if isinstance(exc, OSError):
                raise translate_os_error(exc, operation="sync", key=key) from exc
            raise


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
    "MergedPairFilter",
    "PairFilter",
    "ParallelFilter",
    "SrcOnlyPair",
    "SyncPair",
    "all_of",
    "any_of",
]
