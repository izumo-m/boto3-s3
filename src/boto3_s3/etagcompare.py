"""``boto3_s3.etagcompare``: an ETag content-comparison strategy for ``S3.sync``.

``S3.sync``'s copy decision is a :data:`~boto3_s3.comparator.PairFilter` (``True``
copies the source). The default ``compare=None`` decides by size + last-modified,
aws-cli style; :class:`EtagComparison` decides by **content**,
comparing S3's ETag against the ETag the source would carry:

- an s3-to-s3 (COPY) pair compares the two listings' ETags directly - both are
  already known, so no bytes are read;
- an upload / download pair reconstructs the **local** file's S3-style ETag and
  compares it to the S3 side. S3's ETag is the hex MD5 of the object for a
  single-part PUT, and ``MD5(concat of each part's binary MD5) + "-<n>"`` for a
  multipart upload - so the reconstruction needs the multipart part size
  (the ``part_size`` argument, default :data:`DEFAULT_PART_SIZE`).

This is a standalone, opt-in building block: it lives in its own module, is
imported by submodule path (``from boto3_s3.etagcompare import EtagComparison``), and
is **not** part of the package's lazy root re-export. Like
:mod:`~boto3_s3.comparator` it imports no AWS SDK module at import time; the one
SDK touch - mirroring s3transfer's ``ChunksizeAdjuster`` so the reconstructed
part size matches what an actual upload would chunk - is deferred into the
compute path, so ``import boto3_s3.etagcompare`` stays SDK-free.

Two caveats are inherent to ETag comparison and are the caller's to manage:

- **the part size must match the upload.** The strategy holds a single
  ``part_size`` fixed at construction, so an object uploaded with a non-default
  ``multipart_chunksize`` is recognized only when the same value is supplied;
  otherwise every multipart object reads as differing and is re-copied (the same
  constraint rclone documents for its chunk size). ``EtagComparison(s3)`` is the
  convenience for the common case - it reads ``part_size`` from that ``s3``'s
  profile - but the value is still fixed at construction, not this sync's live
  transfer config. The *effective* part size has a 5 MiB floor and 5 GiB ceiling
  and auto-grows past S3's 10000-part limit (s3transfer's ``ChunksizeAdjuster``),
  so a requested ``part_size`` below 5 MiB is clamped up.
- **the object must be unencrypted or SSE-S3.** SSE-KMS / SSE-C / DSSE objects
  carry an opaque, non-MD5 ETag that cannot be reconstructed, and a listing does
  not reveal an object's encryption - so against such a bucket this strategy
  treats every object as differing and re-copies it each run. Use the default
  ``compare=None`` there instead.

Because the copy decision runs on ``sync``'s main thread, an upload / download
pair blocks that thread on the local read + hash: the strategy trades wall-clock
for byte-exact comparison (the size check skips that read when the two sides'
sizes already differ).
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING

from boto3_s3.comparator import SyncPair
from boto3_s3.localstorage import to_native_path
from boto3_s3.types import LocalFileInfo, S3FileInfo, TransferType

if TYPE_CHECKING:
    from boto3_s3.s3 import S3

DEFAULT_PART_SIZE = 8 * 1024 * 1024
"""Default multipart part size - boto3 ``TransferConfig.multipart_chunksize``."""

_READ_CHUNK = 1024 * 1024
"""Streaming read granularity; bounds memory regardless of file size."""


class EtagComparison:
    """A content-comparison :data:`~boto3_s3.comparator.PairFilter` (``True`` = copy).

    Copies a pair when the destination's S3
    ETag does not match the source's content. A source-only pair (no destination)
    always copies; an s3-to-s3 pair compares the listings' ETags directly; an
    upload / download reconstructs the local file's single- or multipart ETag (at
    ``part_size``) and compares. A missing / non-MD5 ETag is treated as differing
    (copy), so it never skips on an indeterminate comparison. It is a replacement
    ``compare=`` strategy - selected instead of the size+time default, not
    composed with it.

    The multipart part size is fixed at construction (``part_size``) and must
    equal the ``multipart_chunksize`` the object was uploaded with (see the module
    docstring). Supply it one of three ways:

    - ``EtagComparison(s3)`` reads it from that ``s3``'s active profile
      (``[s3] multipart_chunksize``, falling back to :data:`DEFAULT_PART_SIZE`).
      The read is tied to the passed ``s3`` - an explicit injection, not an
      ambient / default-session read - and happens only when no ``part_size`` is
      given.
    - ``EtagComparison(part_size=...)`` pins an explicit value. It overrides the
      ``s3``-derived default, so ``EtagComparison(s3, part_size=...)`` uses the
      explicit value and does not consult ``s3``.
    - ``EtagComparison()`` uses :data:`DEFAULT_PART_SIZE` (boto3's 8 MiB).

    When ``check_size`` is true (the default) a pair whose two sides have
    known, differing sizes is treated as differing (copy) before any ETag work.
    For an s3-to-s3 pair this is a correctness safeguard, not just a shortcut:
    ETag equality alone can falsely skip a copy because MD5 is collision-prone
    (distinct contents can share an ETag), and a size mismatch is independent
    evidence. For an upload / download it additionally avoids the local read +
    hash. ``check_size=False`` restores pure-ETag semantics.

    An upload / download comparison reads the local file, so it raises ``OSError``
    if that file is unreadable (e.g. removed between the listing and the compare);
    the s3-to-s3 path reads nothing.
    """

    __slots__ = ("check_size", "part_size")

    def __init__(
        self,
        s3: S3 | None = None,
        *,
        part_size: int | None = None,
        check_size: bool = True,
    ) -> None:
        if part_size is None and s3 is not None:
            part_size = s3.aws_config().get_size("s3.multipart_chunksize", DEFAULT_PART_SIZE)
        self.part_size = DEFAULT_PART_SIZE if part_size is None else part_size
        self.check_size = check_size

    def __call__(self, pair: SyncPair) -> bool:
        src, dst = pair.src, pair.dst
        if src is None:
            raise ValueError(f"copy decision consulted without a source entry: {pair.key!r}")
        if dst is None:
            return True
        if (
            self.check_size
            and src.size is not None
            and dst.size is not None
            and src.size != dst.size
        ):
            # Differing sizes mean differing content - copy without trusting an
            # ETag (MD5 can collide) or reading the local file.
            return True
        if pair.transfer_type is TransferType.COPY:
            # Both sides are S3 listings: the stored ETags are directly comparable.
            return _etag_differs(_s3_etag(src), _s3_etag(dst))
        if pair.transfer_type is TransferType.UPLOAD:
            local, remote = src, dst
        elif pair.transfer_type is TransferType.DOWNLOAD:
            local, remote = dst, src
        else:  # MOVE never reaches a copy decision; guard defensively.
            raise ValueError(
                f"etag comparison cannot judge a {pair.transfer_type.value!r} pair: {pair.key!r}"
            )
        if not isinstance(local, LocalFileInfo):
            raise ValueError(
                f"etag comparison: no local side for pair {pair.key!r} "
                f"(transfer_type={pair.transfer_type.value})"
            )
        remote_etag = _s3_etag(remote)
        if not remote_etag:
            return True
        path = to_native_path(local.key)
        if "-" in remote_etag:
            size = os.path.getsize(path)
            chunk = _effective_part_size(self.part_size, size)
            computed = _multipart_etag_at(path, chunk_size=chunk)
        else:
            computed = _file_md5_hex(path)
        return remote_etag != computed


def _s3_etag(info: object) -> str | None:
    """The dequoted ETag of an S3 listing entry, or ``None`` for any other side."""
    return info.etag if isinstance(info, S3FileInfo) else None


def _etag_differs(a: str | None, b: str | None) -> bool:
    """Whether two ETags disagree; an unknown ETag on either side counts as differing."""
    return a is None or b is None or a != b


def _file_md5_hex(path: str) -> str:
    """Single-part S3 ETag: the streamed hex MD5 of the whole file.

    Raises ``OSError`` if the file cannot be read (e.g. removed between the
    listing and this call).
    """
    hasher = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_READ_CHUNK), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _multipart_etag_at(path: str, *, chunk_size: int) -> str:
    """Multipart S3 ETag at an exact ``chunk_size`` (no part-size adjustment).

    ``MD5(concat of each part's binary MD5).hex() + "-" + n``. A 0-byte file
    yields ``...-0``, a value no real (always single-part) empty object carries -
    callers reach this branch only for an ETag that already bears a ``-n``
    suffix, so a real empty object never lands here. Raises ``OSError`` on a read
    failure.
    """
    part_digests = bytearray()
    parts = 0
    with open(path, "rb") as fh:
        while True:
            part = hashlib.md5()
            remaining = chunk_size
            read_any = False
            while remaining > 0:
                block = fh.read(min(_READ_CHUNK, remaining))
                if not block:
                    break
                read_any = True
                part.update(block)
                remaining -= len(block)
            if not read_any:
                break
            part_digests += part.digest()
            parts += 1
    return hashlib.md5(bytes(part_digests)).hexdigest() + f"-{parts}"


def _effective_part_size(part_size: int, file_size: int) -> int:
    """The part size an upload would actually use, via s3transfer's adjuster.

    Mirrors s3transfer's ``ChunksizeAdjuster``: grow the part size until the file
    fits in S3's 10000-part limit, then clamp to S3's [5 MiB, 5 GiB] part bounds.
    This is the module's only AWS SDK dependency, imported here lazily so that
    importing the module stays SDK-free.
    """
    from s3transfer.utils import ChunksizeAdjuster

    return ChunksizeAdjuster().adjust_chunksize(part_size, file_size)


__all__ = ["DEFAULT_PART_SIZE", "EtagComparison"]
