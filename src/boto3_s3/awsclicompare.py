"""``boto3_s3.awsclicompare``: the aws-cli size + last-modified comparison for ``S3.sync``.

``S3.sync``'s update copy decision is a ``PairFilter`` (``True``
copies the source). ``AwsCliComparison`` is the aws-cli judgment and the
**explicit form of ``update_filter=None``** - ``update_filter=None`` is equivalent to
``AwsCliComparison()``. It decides by size + last-modified, reading the transfer
direction from each ``SyncPair`` (the both-sides pair; a new, source-only entry
is ``create_filter``'s lane): a pair copies when the sizes differ, or when the
last-modified rule does not rule the copy out - an upload / copy is redundant
when the destination is at least as new as the source, a download when the
destination is at least as old (aws-cli's direction-asymmetric rule).

The two flags mirror ``aws s3 sync``'s ``--size-only`` / ``--exact-timestamps``:

- ``size_only`` decides purely on size, ignoring time (aws-cli's ``SizeOnlySync``).
- ``exact_timestamps`` tightens the **download** rule to require exactly equal
  times (aws-cli's ``ExactTimestampsSync``; uploads / copies are unaffected).
- with both set, ``exact_timestamps`` wins (aws-cli's strategy override order).
  The two flags fill a single aws-cli strategy slot, which is why they are
  constructor arguments of one comparison object rather than two independent
  ``sync`` options - a content ``update_filter=`` replaces the whole judgment, so there
  is nothing for them to tune there, and the combination is simply unrepresentable.

Like its peers ``EtagComparison`` /
``ChecksumComparison``, it is a standalone building
block - re-exported at the package root and equally reachable by submodule path
(``from boto3_s3.awsclicompare import AwsCliComparison``) - and it imports no AWS
SDK module, so ``import boto3_s3.awsclicompare`` stays SDK-free. Pass it via
``update_filter=`` to tune the default, e.g.
``s3.sync(src, dest, update_filter=AwsCliComparison(size_only=True))``;
wrap it in ``ParallelFilter`` to decide on a caller's thread pool.
"""

from __future__ import annotations

from boto3_s3.comparator import SyncPair
from boto3_s3.types import FileInfo, TransferType


class AwsCliComparison:
    """The aws-cli size + last-modified ``PairFilter`` (``True`` = copy).

    The explicit form of ``S3.sync``'s ``update_filter=None`` default: ``update_filter=None``
    is equivalent to ``AwsCliComparison()``. ``size_only`` / ``exact_timestamps``
    mirror ``aws s3 sync``'s ``--size-only`` / ``--exact-timestamps`` (with both
    set, ``exact_timestamps`` wins). See the module docstring for the decision rule
    and the direction asymmetry.
    """

    __slots__ = ("exact_timestamps", "size_only")

    def __init__(self, *, size_only: bool = False, exact_timestamps: bool = False) -> None:
        self.size_only = size_only
        self.exact_timestamps = exact_timestamps

    def __call__(self, pair: SyncPair) -> bool:
        """Whether ``pair`` copies, by the rule in the module docstring.

        The direction comes from ``pair.transfer_type`` (the time rule is
        direction-asymmetric). Comparisons run at full ``timedelta`` precision
        (aws-cli's ``total_seconds``). A missing ``size`` or ``mtime`` on either
        side counts as a difference - aws-cli never faces one (its listings
        always carry both), so leaning toward copying is the permissive
        reading. The ``no_overwrite`` write-guard is orthogonal and lives in
        the ``S3.sync`` loop, not here, so it composes with any
        ``update_filter=`` strategy.
        """
        src, dest = pair.src, pair.dest
        same_size = src.size is not None and src.size == dest.size
        if self.size_only and not self.exact_timestamps:
            # aws-cli's SizeOnlySync.
            return not same_size
        # aws-cli's SizeAndLastModifiedSync, or ExactTimestampsSync under
        # exact_timestamps.
        return not same_size or not _times_match(
            src, dest, pair.transfer_type, self.exact_timestamps
        )


def _times_match(src: FileInfo, dest: FileInfo, transfer_type: TransferType, exact: bool) -> bool:
    """aws-cli's ``compare_time``: True when last-modified makes the copy redundant."""
    if src.mtime is None or dest.mtime is None:
        return False
    delta = (dest.mtime - src.mtime).total_seconds()
    if transfer_type is TransferType.DOWNLOAD:
        return delta == 0 if exact else delta <= 0
    return delta >= 0


__all__ = ["AwsCliComparison"]
