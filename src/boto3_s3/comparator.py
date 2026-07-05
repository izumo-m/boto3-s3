"""Sync pairing and pair-decision material (the engine room of ``S3.sync``).

``aws s3 sync`` is a two-layer pipeline. Layer one is *visibility*: each
side's listing is pruned independently (``--exclude`` / ``--include``,
folder markers) before the sides ever meet - that pruning lives in the
scan path, not here. Layer two is *pair decisions*: the surviving streams
are merge-joined by compare key, then per-pair decisions select what to
copy or delete. This module is layer two's material:

- :class:`Comparator` pairs two key-ordered streams into :class:`SyncPair`
  records. It is a **pure pairer** - every key on either side comes out and
  no copy/delete judgment happens here (unlike aws-cli's comparator, which
  buries its strategy calls in the merge loop; splitting them keeps each
  side replaceable). It only stamps the run's direction (``transfer_type``) onto each
  pair, as context for the filters.
- A :data:`PairFilter` is the **update** judgment: a predicate over a
  both-sides pair where ``True`` re-copies the source over the destination. It
  is what ``S3.sync(update_filter=...)`` selects - the size+time default, a
  content strategy (``EtagComparison`` / ``ChecksumComparison``), or a caller's
  own. ``S3.sync`` only ever hands it an update pair (both sides present), so it
  never faces a ``None`` side; the new (source-only) lane is governed by
  ``create_filter`` and the delete lane by ``delete_filter``, each a ``FileFilter``
  over the one side it has.
- :func:`compare_size_time` is that size+time default (aws-cli's stock
  judgment, with the ``size_only`` / ``exact_timestamps`` tuners). It is not a
  re-exported building block (kept out of ``__all__``); it is the judgment
  behind :class:`~boto3_s3.awsclicompare.AwsCliComparison`, the form ``S3.sync``
  selects for ``update_filter=None``. The direction is read from
  ``pair.transfer_type``.
- :class:`ContentComparison` is the shared template of the content
  strategies (``EtagComparison`` / ``ChecksumComparison``): the decision
  skeleton every content comparison runs before its leaf digest work - the
  guards, the ``check_size`` safeguard, and the direction dispatch - lives
  here once, so the two strategies cannot drift apart on it.
- :func:`all_of` / :func:`any_of` compose same-signature predicates - chiefly
  the ``filter=`` visibility predicates over :class:`~boto3_s3.types.FileInfo`
  (a copy strategy is *chosen*, not composed).

Everything here is pure logic over :class:`~boto3_s3.types.FileInfo`; no
AWS SDK module is imported.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, TypeVar

from boto3_s3.types import FileInfo, S3FileInfo, TransferType

if TYPE_CHECKING:
    from boto3_s3.storage import Storage

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True, kw_only=True)
class SyncPair:
    """One compare key's pairing across the two sides of a sync.

    ``key`` is the compare key - the entry's path relative to its side's
    sync root, ``/``-separated on every platform - so name-based filters
    need not care where either root lives. ``transfer_type`` is the sync's
    transfer direction (UPLOAD / DOWNLOAD / COPY), stamped on every pair so a
    pair filter can apply the direction-asymmetric rules without being told the
    route. ``src`` / ``dest`` are the sides' listing entries; exactly one may
    be ``None``:

    - both set: the key exists on both sides (copy is an *update*),
    - ``dest`` is ``None``: source-only (copy is a *new* transfer),
    - ``src`` is ``None``: destination-only (the delete candidate).

    ``src_storage`` / ``dest_storage`` are the backends the two sides were listed
    from (constant across a sync, stamped by :class:`Comparator`). A content
    ``update_filter=`` strategy reads the non-S3 side's bytes through its ``Storage.open``
    - so content comparison works for any backend, not just a local filesystem.
    """

    key: str
    transfer_type: TransferType
    src: FileInfo | None = None
    dest: FileInfo | None = None
    src_storage: Storage | None = None
    dest_storage: Storage | None = None


PairFilter = Callable[[SyncPair], bool]
"""A pair predicate: ``True`` performs the action the pair stands for."""


@dataclass(frozen=True, slots=True)
class ParallelCompare:
    """Run a content ``update_filter=`` strategy on a thread pool (``S3.sync`` only).

    A value container, **not** a callable: ``S3.sync`` recognizes it and runs
    the wrapped ``compare`` concurrently on up to ``workers`` threads, deciding
    the both-sides (update) pairs - the ones where a content strategy does its
    I/O - in parallel. Passing it is observationally identical to passing
    ``compare`` bare: the same pairs are copied and the exit is the same, only
    faster. The wrapped ``compare`` must be thread-safe;
    :class:`~boto3_s3.checksumcompare.ChecksumComparison` and
    :class:`~boto3_s3.etagcompare.EtagComparison` are. New (destination-missing)
    pairs and the ``--case-conflict`` check stay on the calling thread in
    compare-key order, so they remain deterministic.

    ``workers`` defaults to the sync's ``transfer_config.max_concurrency``.
    """

    compare: PairFilter
    workers: int | None = None

    def __post_init__(self) -> None:
        if self.workers is not None and self.workers < 1:
            raise ValueError(f"ParallelCompare workers must be >= 1, got {self.workers}")


def _byte_ordered(
    entries: Iterable[tuple[str, FileInfo]], side: str
) -> Iterator[tuple[str, FileInfo]]:
    """Dev-only pass-through that asserts a side ascends by ``compare_key``.

    :meth:`Comparator.compare`'s merge-join assumes both sides arrive in UTF-8
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
    """Merge-join two key-ordered listing streams into :class:`SyncPair`s.

    The classic sorted-merge (aws-cli ``Comparator.call``): advance whichever
    side holds the smaller key, pair equal keys, and flush the survivor once
    one side is exhausted.
    Inputs must be ascending by compare key - that is the ``Storage.scan``
    ordering contract (S3 byte order; the local walk sorts to match) - and
    the merge itself never compares sizes or times: feed the resulting pairs
    to a :data:`PairFilter` for that. ``transfer_type`` (the run's direction) is
    stamped onto every emitted pair - context, not a judgment. ``src_storage`` /
    ``dest_storage`` are stamped alongside it (the two sides' backends), so a
    content ``update_filter=`` strategy can open the non-S3 side of any pair.
    """

    transfer_type: TransferType
    src_storage: Storage | None = None
    dest_storage: Storage | None = None

    def compare(
        self,
        src_entries: Iterable[tuple[str, FileInfo]],
        dest_entries: Iterable[tuple[str, FileInfo]],
    ) -> Iterator[SyncPair]:
        """Yield every key on either side as a :class:`SyncPair`.

        ``src_entries`` / ``dest_entries`` are ``(compare_key, info)``
        streams, lazily consumed - pairing streams page-by-page listings
        without materializing either side.
        """
        transfer_type = self.transfer_type
        sstore, dstore = self.src_storage, self.dest_storage
        if __debug__:  # dev guard: catch an unsorted SORTABLE_SCAN side (compiled out under -O)
            src_entries = _byte_ordered(src_entries, "source")
            dest_entries = _byte_ordered(dest_entries, "destination")
        src_iter = iter(src_entries)
        dest_iter = iter(dest_entries)
        src = next(src_iter, None)
        dest = next(dest_iter, None)
        while src is not None and dest is not None:
            if src[0] < dest[0]:
                yield SyncPair(
                    key=src[0],
                    transfer_type=transfer_type,
                    src=src[1],
                    src_storage=sstore,
                    dest_storage=dstore,
                )
                src = next(src_iter, None)
            elif src[0] > dest[0]:
                yield SyncPair(
                    key=dest[0],
                    transfer_type=transfer_type,
                    dest=dest[1],
                    src_storage=sstore,
                    dest_storage=dstore,
                )
                dest = next(dest_iter, None)
            else:
                yield SyncPair(
                    key=src[0],
                    transfer_type=transfer_type,
                    src=src[1],
                    dest=dest[1],
                    src_storage=sstore,
                    dest_storage=dstore,
                )
                src = next(src_iter, None)
                dest = next(dest_iter, None)
        while src is not None:
            yield SyncPair(
                key=src[0],
                transfer_type=transfer_type,
                src=src[1],
                src_storage=sstore,
                dest_storage=dstore,
            )
            src = next(src_iter, None)
        while dest is not None:
            yield SyncPair(
                key=dest[0],
                transfer_type=transfer_type,
                dest=dest[1],
                src_storage=sstore,
                dest_storage=dstore,
            )
            dest = next(dest_iter, None)


def compare_size_time(
    pair: SyncPair, *, size_only: bool = False, exact_timestamps: bool = False
) -> bool:
    """``S3.sync``'s internal default judgment (aws-cli size + last-modified).

    Not a public building block: it implements
    :class:`~boto3_s3.awsclicompare.AwsCliComparison`, which ``S3.sync`` selects
    for ``update_filter=None``.
    The transfer direction comes from ``pair.transfer_type`` (the time rule is
    direction-asymmetric). A source-only pair always copies (aws-cli's
    ``MissingFileSync``). For a pair present on both sides:

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
    if src is None:
        raise ValueError(f"copy decision consulted without a source entry: {pair.key!r}")
    if dest is None:
        return True
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


READ_CHUNK = 1024 * 1024
"""The content strategies' streaming read granularity; bounds memory."""


class ContentComparison:
    """The shared skeleton of the content ``update_filter=`` strategies (``True`` = copy).

    Not itself a strategy: ``EtagComparison`` / ``ChecksumComparison`` extend it
    with their leaf digest work. What lives here is everything the two must
    agree on - the missing-source guard, the source-only always-copy, the
    ``check_size`` size-mismatch decision, and the split into the two hooks - so
    the strategies cannot silently drift apart on the decision shape.

    Which side is the S3 object (the stored digest to compare against) is decided
    by **type** - the ``S3FileInfo`` side - not by the transfer direction, so the
    other ("readable") side may be any backend: its bytes are read through the
    ``Storage.open`` carried on the pair (:attr:`SyncPair.src_storage` /
    ``dest_storage``), not a local filesystem path. Both sides S3 -> the s3-to-s3
    digest compare; neither side S3 -> nothing to compare against, so copy.
    """

    __slots__ = ()

    #: The guard-message name ("etag comparison" / "checksum comparison").
    _strategy_name: ClassVar[str]

    # Storage is the subclass's (each declares its own slot).
    check_size: bool

    def __call__(self, pair: SyncPair) -> bool:
        src, dest = pair.src, pair.dest
        if src is None:
            raise ValueError(f"copy decision consulted without a source entry: {pair.key!r}")
        if dest is None:
            return True
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
            return self._readable_remote_differ(pair.src_storage, src, dest, pair.transfer_type)
        if src_is_s3 and not dest_is_s3:  # download-shaped: dest is the readable side
            return self._readable_remote_differ(pair.dest_storage, dest, src, pair.transfer_type)
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
    (:data:`PairFilter`) and plain ``FileInfo`` visibility predicates
    alike. With no predicates it always passes (like ``all([])``).
    """

    def combined(value: _T) -> bool:
        return all(predicate(value) for predicate in predicates)

    return combined


def any_of(*predicates: Callable[[_T], bool]) -> Callable[[_T], bool]:
    """A predicate passing when at least one given predicate passes.

    The ``or`` counterpart of :func:`all_of`. With no predicates it never
    passes (like ``any([])``).
    """

    def combined(value: _T) -> bool:
        return any(predicate(value) for predicate in predicates)

    return combined


__all__ = [
    "Comparator",
    "PairFilter",
    "ParallelCompare",
    "SyncPair",
    "all_of",
    "any_of",
]
