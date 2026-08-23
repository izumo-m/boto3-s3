"""``boto3_s3.etagcompare``: an ETag content-comparison strategy for ``S3.sync``.

``S3.sync``'s update copy decision is a ``PairFilter`` (``True``
copies the source). The default ``update_filter=None`` decides by size + last-modified,
aws-cli style; ``EtagComparison`` decides by **content**,
comparing S3's ETag against the ETag the readable (non-S3) side's bytes
would carry:

- an s3-to-s3 (COPY) pair compares the two listings' ETags directly - both are
  already known, so no bytes are read;
- an upload / download pair reconstructs the **readable** (non-S3) side's
  S3-style ETag - reading its bytes through ``Storage.open``, so any backend
  works, not just a local file - and compares it to the S3 side. S3's ETag is the
  hex MD5 of the object for a
  single-part PUT, and ``MD5(concat of each part's binary MD5) + "-<n>"`` for a
  multipart upload - so the reconstruction needs the multipart part size
  (the ``part_size`` argument, default ``DEFAULT_PART_SIZE``).

The same judgment is reachable for a **single** source through
``EtagComparison.content_differs``: one local path or open stream against an ETag
the caller already holds - from a listing, a ``HeadObject``, or a ``PutObject``
response - so verifying one object (a post-upload check, an artifact check, an
inventory reconciliation) needs neither a hand-built ``SyncPair`` nor an S3 call
of its own.

This is a standalone, opt-in building block: it lives in its own module and is
reachable either from the package root (``from boto3_s3 import EtagComparison``) or
by submodule path (``from boto3_s3.etagcompare import EtagComparison``). Like
``comparator`` it imports no AWS SDK module at import time; the one
SDK touch - mirroring s3transfer's ``ChunksizeAdjuster`` so the reconstructed
part size matches what an actual upload would chunk - is deferred into the
compute path, so ``import boto3_s3.etagcompare`` stays SDK-free.

Two caveats are inherent to ETag comparison and are the caller's to manage:

- **the part size must reproduce the upload's part boundaries.** The strategy
  holds a single ``part_size`` fixed at construction, so a multipart object
  uploaded with a non-default ``multipart_chunksize`` is recognized only when
  the supplied value yields the same effective boundaries (supplying the same
  chunksize is the reliable way; a single-part object compares regardless of
  the setting); otherwise it reads as differing and is re-copied (the same
  constraint rclone documents for its chunk size). ``EtagComparison(s3)`` is the
  convenience for the common case - it reads ``part_size`` from that ``s3``'s
  profile - but the value is still fixed at construction, not this sync's live
  transfer config. The *effective* part size has a 5 MiB floor and 5 GiB ceiling
  and auto-grows past S3's 10000-part limit (s3transfer's ``ChunksizeAdjuster``),
  so a requested ``part_size`` below 5 MiB is clamped up.
- **the object must be unencrypted or SSE-S3.** SSE-KMS / SSE-C / DSSE objects
  carry an opaque, non-MD5 ETag that cannot be reconstructed, and a listing does
  not reveal an object's encryption - so an upload / download against such a
  bucket treats every object as differing and re-copies it each run (an
  s3-to-s3 pair compares the opaque strings directly, so identical strings -
  rare, since re-encryption changes them - do skip). Use the default
  ``update_filter=None`` there instead.

The copy decision runs on whatever thread drives the ``update_filter`` lane -
``sync``'s calling thread by default, or a pool worker under
``ParallelFilter`` - and an upload / download pair
costs that thread the local read + hash: the strategy trades wall-clock for
byte-exact comparison (the size check skips the read when the two sides' sizes
already differ; ``ParallelFilter`` overlaps the reads instead).
"""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING

from boto3_s3.comparator import READ_CHUNK, ContentComparison
from boto3_s3.types import S3FileInfo

if TYPE_CHECKING:
    from typing import BinaryIO

    from boto3_s3.exceptions import Boto3S3Error
    from boto3_s3.s3 import S3
    from boto3_s3.storage import Storage
    from boto3_s3.types import FileInfo, TransferType

# Default multipart part size: boto3 `TransferConfig.multipart_chunksize`.
DEFAULT_PART_SIZE = 8 * 1024 * 1024


class EtagComparison(ContentComparison):
    """A content-comparison ``PairFilter`` (``True`` = copy).

    Copies a pair when the S3 side's ETag does not match the readable side's
    content - upload: the destination's ETag against the source's bytes;
    download: the source's ETag against the destination's bytes. It judges
    ``SyncPair``s - both
    sides present by construction (a new, source-only entry is ``create_filter``'s
    lane): an s3-to-s3 pair compares the listings' ETags
    directly (as strings, whatever their form); an upload / download
    reconstructs the readable (non-S3) side's
    single- or multipart ETag (at ``part_size``) and compares - there, a
    missing / non-MD5
    ETag is treated as differing (copy), so it never skips on an indeterminate
    comparison. It is a replacement ``update_filter=`` strategy - selected instead
    of the size+time default, not composed with it.

    The multipart part size is fixed at construction (``part_size``) and must
    reproduce the part boundaries the object was uploaded with - supplying
    the upload's ``multipart_chunksize`` is the reliable way (see the module
    docstring). Supply it one of three ways:

    - ``EtagComparison(s3)`` reads it from that ``s3``'s active profile
      (``[s3] multipart_chunksize``, falling back to ``DEFAULT_PART_SIZE``).
      The read is tied to the passed ``s3`` - an explicit injection, not an
      ambient / default-session read - and happens only when no ``part_size`` is
      given.
    - ``EtagComparison(part_size=...)`` pins an explicit value. It overrides the
      ``s3``-derived default, so ``EtagComparison(s3, part_size=...)`` uses the
      explicit value and does not consult ``s3``.
    - ``EtagComparison()`` uses ``DEFAULT_PART_SIZE`` (boto3's 8 MiB).

    When ``check_size`` is true (the default) a pair whose two sides have
    known, differing sizes is treated as differing (copy) before any ETag work.
    For an s3-to-s3 pair this is a correctness safeguard, not just a shortcut:
    ETag equality alone can falsely skip a copy because MD5 is collision-prone
    (distinct contents can share an ETag), and a size mismatch is independent
    evidence. For an upload / download it additionally avoids the local read +
    hash. ``check_size=False`` restores pure-ETag semantics.

    An upload / download comparison reads the **readable** (non-S3) side through
    its ``Storage.open`` - any backend, not just a local file - so a read failure
    surfaces as that backend's error (a ``Boto3S3Error``
    for a local file removed between the listing and the compare); the s3-to-s3
    path reads nothing. A pair with no readable-side backend, or neither side an
    S3 object, is treated as differing (copy).

    ``content_differs`` applies the same judgment to a **single** source - a
    local path or an open binary stream against an ETag the caller supplies -
    outside any sync. Everything above about the part size, ``check_size`` and
    which ETags are reconstructible holds for it; what differs is only that the
    ETag and the two sizes are passed directly instead of read off a
    ``SyncPair``.
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

    def content_differs(
        self,
        source: str | os.PathLike[str] | BinaryIO,
        *,
        etag: str | None,
        size: int | None = None,
        s3_size: int | None = None,
    ) -> bool:
        """Whether one local source's content differs from a known S3 ETag.

        The single-object form of the judgment ``__call__`` makes for a
        ``SyncPair``, for a caller that already holds an object's ETag - a
        listing entry's, a ``HeadObject``'s, a ``PutObject`` response's - and
        wants one file verified against it (a post-upload check, an artifact
        check, an inventory reconciliation). It reaches S3 not at all. ``True``
        means the content differs **or** the comparison is indeterminate: the
        lean of the pair path, which never skips on a value it could not
        verify.

        ``source`` is a filesystem path or an already-open binary stream. A path
        is opened here and closed again, and an open or read failure is
        translated into the library taxonomy (``NotFoundError`` for a path that
        is gone, ``AccessDeniedError`` for a permission failure,
        ``TransportError`` otherwise) carrying ``operation="compare"`` and the
        path - as given, through ``os.fspath`` - as the key. A stream is read
        from its current position to the end and is never closed - it stays the
        caller's, as does whatever it raises, which propagates unchanged.

        ``etag`` is the object's ETag, dequoted (what ``S3FileInfo.etag``
        carries). Missing or empty is indeterminate -> ``True``, with nothing
        opened or read. An ETag bearing a ``-<n>`` suffix is reconstructed as a
        multipart ETag at ``part_size`` (adjusted per file exactly as an upload
        would chunk it), any other as the whole-stream hex MD5 - so the module
        docstring's two caveats hold here too: the part size must reproduce the
        upload's boundaries, and an opaque SSE-KMS / SSE-C / DSSE ETag can never
        match and always reads as differing.

        ``size`` is the source's byte size, needed only to reconstruct the part
        split of a multipart ``etag``. For a path it is taken with
        ``os.path.getsize`` when the comparison needs it and it was not given;
        for a stream, which cannot be sized here, a multipart ``etag`` with no
        ``size`` is indeterminate -> ``True``. A supplied value is trusted, not
        verified against the bytes.

        ``s3_size`` is the object's size when the caller has it. With
        ``check_size`` on (the default) a size known on both sides and differing
        is treated as differing before any bytes are read - the pair path's
        safeguard, and the reason a size is worth passing. ``check_size=False``
        ignores ``s3_size`` entirely.
        """
        if not etag:
            return True  # no ETag to compare against -> indeterminate
        multipart = "-" in etag
        if (
            size is None
            and isinstance(source, (str, os.PathLike))
            and (multipart or (self.check_size and s3_size is not None))
        ):
            try:
                size = os.path.getsize(source)
            except OSError as exc:
                raise _taxonomy_error(exc, operation="compare", key=os.fspath(source)) from exc
        if self.check_size and size is not None and s3_size is not None and size != s3_size:
            # Differing sizes mean differing content: copy without reading a byte
            # and without trusting the ETag (MD5 can collide; the size is
            # independent evidence) - the __call__ path's safeguard.
            return True
        chunk: int | None = None
        if multipart:
            if size is None:
                return True  # an unsized stream: the part split cannot be reconstructed
            chunk = _effective_part_size(self.part_size, size)
        if isinstance(source, (str, os.PathLike)):
            try:
                with open(source, "rb") as fh:
                    computed = self._computed_etag(fh, chunk_size=chunk)
            except OSError as exc:
                raise _taxonomy_error(exc, operation="compare", key=os.fspath(source)) from exc
        else:
            computed = self._computed_etag(source, chunk_size=chunk)
        return etag != computed

    def _copy_differs(self, src: FileInfo, dest: FileInfo) -> bool:
        # Both sides are S3 listings: the stored ETags are directly comparable.
        return _etag_differs(_s3_etag(src), _s3_etag(dest))

    def _readable_remote_differ(
        self,
        storage: Storage | None,
        readable: FileInfo,
        remote: FileInfo,
        transfer_type: TransferType,
    ) -> bool:
        del transfer_type  # the listing ETag travels on the entry; no endpoint needed
        remote_etag = _s3_etag(remote)
        if not remote_etag:
            return True
        key = readable.compare_key
        if storage is None or key is None:
            return True  # cannot open the readable side -> treat as differing (copy)
        chunk: int | None = None
        if "-" in remote_etag:
            if readable.size is None:
                return True  # need the size to reconstruct the multipart part split
            chunk = _effective_part_size(self.part_size, readable.size)
        try:
            with storage.open(key, "rb") as fh:
                computed = self._computed_etag(fh, chunk_size=chunk)
        except OSError as exc:
            # LocalStorage.open translates its own open failure; a fault
            # *mid-read* (or on close) is a raw OSError from the stream. Keep
            # the promise that a local read failure surfaces as a taxonomy
            # error; a custom backend's own exceptions pass through unchanged.
            raise _taxonomy_error(exc, operation="sync", key=key) from exc
        return remote_etag != computed

    def _computed_etag(self, fh: BinaryIO, *, chunk_size: int | None) -> str:
        """The S3-style ETag of an open stream, read to its end.

        The one place either entry point - the ``SyncPair`` filter and
        ``content_differs`` - turns bytes into an ETag, so the two cannot drift
        apart on the reconstruction. ``chunk_size`` is the already-adjusted part
        size for a multipart ETag; ``None`` selects the single-part form (the
        whole-stream hex MD5). ``OSError`` from a read propagates to the caller,
        which decides whether to translate it.
        """
        if chunk_size is None:
            return _file_md5_hex(fh)
        return _multipart_etag_at(fh, chunk_size=chunk_size)


def _taxonomy_error(exc: OSError, *, operation: str, key: str) -> Boto3S3Error:
    """A local read failure as a library error: missing -> ``NotFoundError``, and so on.

    Deferred import, so this module stays SDK-free at import time; ``operation``
    names the caller's context (``"sync"`` for the pair filter, ``"compare"``
    for ``content_differs``).
    """
    from boto3_s3.localstorage import translate_os_error

    return translate_os_error(exc, operation=operation, key=key)


def _s3_etag(info: object) -> str | None:
    """The dequoted ETag of an S3 listing entry, or ``None`` for any other side."""
    return info.etag if isinstance(info, S3FileInfo) else None


def _etag_differs(a: str | None, b: str | None) -> bool:
    """Whether two ETags disagree; an unknown ETag on either side counts as differing."""
    return a is None or b is None or a != b


def _file_md5_hex(fh: BinaryIO) -> str:
    """Single-part S3 ETag: the streamed hex MD5 of the whole stream.

    Reads ``fh`` - a readable side's ``Storage.open`` stream, or a
    ``content_differs`` source - to its end; ``OSError`` from a read propagates
    to the caller, which decides whether to translate it into the taxonomy.
    """
    hasher = hashlib.md5()
    for block in iter(lambda: fh.read(READ_CHUNK), b""):
        hasher.update(block)
    return hasher.hexdigest()


def _multipart_etag_at(fh: BinaryIO, *, chunk_size: int) -> str:
    """Multipart S3 ETag at an exact ``chunk_size`` (no part-size adjustment).

    ``MD5(concat of each part's binary MD5).hex() + "-" + n`` over the stream
    ``fh``. A 0-byte stream yields ``...-0``, a value no real (always single-part)
    empty object carries - callers reach this branch only for an ETag that already
    bears a ``-n`` suffix, so a real empty object never lands here. ``OSError`` on
    a read propagates.
    """
    part_digests = bytearray()
    parts = 0
    while True:
        part = hashlib.md5()
        remaining = chunk_size
        read_any = False
        while remaining > 0:
            block = fh.read(min(READ_CHUNK, remaining))
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
