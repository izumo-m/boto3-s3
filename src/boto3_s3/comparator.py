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
- A :data:`PairFilter` is a copy judgment: a predicate over a pair where
  ``True`` copies the source. It is what ``S3.sync(compare=...)`` selects -
  the size+time default, a content strategy (``EtagComparison`` / ``ChecksumComparison``),
  or a caller's own. (``S3.sync``'s delete lane instead narrows with a
  ``FileFilter`` over the destination-only orphan.)
- :func:`compare_size_time` is that size+time default (aws-cli's stock
  judgment, with the ``size_only`` / ``exact_timestamps`` tuners). It is not a
  re-exported building block (kept out of ``__all__``); it is the judgment
  behind :class:`~boto3_s3.awsclicompare.AwsCliComparison`, the form ``S3.sync``
  selects for ``compare=None``. The direction is read from
  ``pair.transfer_type``.
- :func:`all_of` / :func:`any_of` compose same-signature predicates - chiefly
  the ``filter=`` visibility predicates over :class:`~boto3_s3.types.FileInfo`
  (a copy strategy is *chosen*, not composed).

Everything here is pure logic over :class:`~boto3_s3.types.FileInfo`; no
AWS SDK module is imported.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TypeVar

from boto3_s3.types import FileInfo, TransferType

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
    """

    key: str
    transfer_type: TransferType
    src: FileInfo | None = None
    dest: FileInfo | None = None


PairFilter = Callable[[SyncPair], bool]
"""A pair predicate: ``True`` performs the action the pair stands for."""


@dataclass(frozen=True, slots=True)
class ParallelCompare:
    """Run a content ``compare=`` strategy on a thread pool (``S3.sync`` only).

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
    stamped onto every emitted pair - context, not a judgment.
    """

    transfer_type: TransferType

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
        src_iter = iter(src_entries)
        dest_iter = iter(dest_entries)
        src = next(src_iter, None)
        dest = next(dest_iter, None)
        while src is not None and dest is not None:
            if src[0] < dest[0]:
                yield SyncPair(key=src[0], transfer_type=transfer_type, src=src[1])
                src = next(src_iter, None)
            elif src[0] > dest[0]:
                yield SyncPair(key=dest[0], transfer_type=transfer_type, dest=dest[1])
                dest = next(dest_iter, None)
            else:
                yield SyncPair(key=src[0], transfer_type=transfer_type, src=src[1], dest=dest[1])
                src = next(src_iter, None)
                dest = next(dest_iter, None)
        while src is not None:
            yield SyncPair(key=src[0], transfer_type=transfer_type, src=src[1])
            src = next(src_iter, None)
        while dest is not None:
            yield SyncPair(key=dest[0], transfer_type=transfer_type, dest=dest[1])
            dest = next(dest_iter, None)


def compare_size_time(
    pair: SyncPair, *, size_only: bool = False, exact_timestamps: bool = False
) -> bool:
    """``S3.sync``'s internal default judgment (aws-cli size + last-modified).

    Not a public building block: it implements
    :class:`~boto3_s3.awsclicompare.AwsCliComparison`, which ``S3.sync`` selects
    for ``compare=None``.
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
    loop, not here, so it composes with any ``compare=`` strategy. Comparisons
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
