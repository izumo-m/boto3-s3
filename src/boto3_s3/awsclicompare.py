"""``boto3_s3.awsclicompare``: the aws-cli size + last-modified comparison for ``S3.sync``.

``S3.sync``'s copy decision is a ``PairFilter`` (``True``
copies the source). ``AwsCliComparison`` is the aws-cli judgment and the
**explicit form of ``update_filter=None``** - ``update_filter=None`` is equivalent to
``AwsCliComparison()``. It decides by size + last-modified, reading the transfer
direction from each ``SyncPair``. ``S3.sync`` hands it
only both-sides pairs (a source-only pair is ``create_filter``'s lane):

- a source-only pair (no destination) always copies - a defensive fallback for a
  standalone caller, matching aws-cli's ``MissingFileSync``;
- a pair present on both sides copies when the sizes differ, or when the
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
block imported by submodule path
(``from boto3_s3.awsclicompare import AwsCliComparison``), is **not** part of the
package's lazy root re-export, and imports no AWS SDK module - so
``import boto3_s3.awsclicompare`` stays SDK-free. Pass it via ``update_filter=`` to tune
the default, e.g. ``s3.sync(src, dest, update_filter=AwsCliComparison(size_only=True))``;
wrap it in ``ParallelFilter`` to decide on a caller's thread pool.
"""

from __future__ import annotations

from boto3_s3.comparator import SyncPair, compare_size_time


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
        return compare_size_time(
            pair, size_only=self.size_only, exact_timestamps=self.exact_timestamps
        )


__all__ = ["AwsCliComparison"]
