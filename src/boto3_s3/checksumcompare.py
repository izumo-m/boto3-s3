"""``boto3_s3.checksumcompare``: a native-checksum content-comparison strategy for ``S3.sync``.

``S3.sync``'s copy decision is a :data:`~boto3_s3.comparator.PairFilter` (``True``
copies the source). The default ``update_filter=None`` decides by size + last-modified,
aws-cli style; :class:`ChecksumComparison` decides by **content**,
comparing S3's stored native checksum against the checksum the local file would
carry:

- it reads the S3 object's checksum with **GetObjectAttributes** - the
  ``Checksum`` (algorithm + value) and, for a multipart object, the exact
  ``ObjectParts`` boundaries;
- it recomputes that same algorithm over the local file and compares.

Unlike :class:`~boto3_s3.etagcompare.EtagComparison` this needs **no write side** (the
checksum is one S3 already stores), works against objects any tool uploaded with
a checksum, is exact for multipart objects (the part sizes come back from
GetObjectAttributes, so there is no part-size to guess), and works for SSE
objects (a checksum is independent of encryption, unlike an ETag). The cost is
one ``GetObjectAttributes`` round-trip per object compared.

This is a standalone, opt-in building block: it lives in its own module, is
imported by submodule path (``from boto3_s3.checksumcompare import
ChecksumComparison``), and is **not** part of the package's lazy root re-export. It
imports no AWS SDK module at import time; the SDK touches - the boto3 client (via
``s3.resolve``), ``botocore``'s ``ClientError``, and the optional ``awscrt`` fast
checksums - are all deferred into the construct / compute paths.

It is a replacement ``update_filter=`` strategy, not composed with the default:
``S3.sync(update_filter=ChecksumComparison(s3, src, dest))`` decides every pair by content.
That catches what the size + mtime default misses (notably the download
asymmetry: a same-size object updated only on the S3 source is never pulled
down by the default), at the price of a GetObjectAttributes + local hash on
every both-sides pair (the size pre-check still skips differing sizes for free).

Checksum computation. ``crc32`` (zlib) and ``sha1`` / ``sha256`` (hashlib) are
always available. ``crc32c`` / ``crc64nvme`` use ``awscrt`` when it is installed
(a C path ~1000x faster than Python and the same one S3 uses) and fall back to a
bundled pure-Python slicing-by-8 implementation otherwise (~15-20 MiB/s). Because
that fallback is slow, the ``pure_max_size`` arg can cap it: above the cap, with no
``awscrt``, a ``crc32c`` / ``crc64nvme`` object is treated as indeterminate
(copied) rather than hashed.

Caveats. An object with **no** native checksum, or one whose algorithm cannot be
computed locally (``crc32c`` / ``crc64nvme`` without ``awscrt``, or past
``pure_max_size``), is treated as differing (copied) - the strategy never skips
on an indeterminate comparison. An upload / download comparison reads the
**readable** (non-S3) side through its ``Storage.open`` on whatever thread
drives the ``update_filter`` lane (``sync``'s calling thread, or a pool worker
under :class:`~boto3_s3.comparator.ParallelFilter`) - any backend, not just a
local file - so a read failure surfaces as that backend's error (a
``Boto3S3Error`` for a local file). SSE-C objects need the customer key to
``GetObjectAttributes`` and are not supported (they read as indeterminate ->
copy).
"""

from __future__ import annotations

import base64
import functools
import hashlib
import sys
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from boto3_s3.comparator import READ_CHUNK, ContentComparison
from boto3_s3.types import TransferType

if TYPE_CHECKING:
    from typing import BinaryIO

    from boto3_s3.s3 import S3
    from boto3_s3.storage import Location, Storage
    from boto3_s3.types import FileInfo

# GetObjectAttributes ``Checksum.<key>`` -> our algorithm name. An object carries
# at most one; the first present key wins (priority is irrelevant - only one is set).
_ALGO_BY_KEY = {
    "ChecksumCRC32": "crc32",
    "ChecksumCRC32C": "crc32c",
    "ChecksumCRC64NVME": "crc64nvme",
    "ChecksumSHA1": "sha1",
    "ChecksumSHA256": "sha256",
}

_MASK32 = 0xFFFFFFFF
_MASK64 = 0xFFFFFFFFFFFFFFFF
_POLY_CRC32C = 0x82F63B78  # CRC32C (Castagnoli), reflected
_POLY_CRC64NVME = 0x9A6C9329AC4BC9B5  # CRC-64/NVME, reflected
_LITTLE = sys.byteorder == "little"


class ChecksumComparison(ContentComparison):
    """A native-checksum content :data:`~boto3_s3.comparator.PairFilter` (``True`` = copy).

    Copies a pair when the destination's
    stored S3 checksum does not match the source's content. ``S3.sync`` hands it
    only both-sides pairs (source-only is ``create_filter``'s lane; a standalone
    source-only pair copies as a defensive fallback). An upload / download reads
    the remote
    object's checksum via ``GetObjectAttributes`` and recomputes it over the
    **readable** (non-S3) side's bytes (through its ``Storage.open`` - any
    backend), whole-file for a ``FULL_OBJECT`` checksum or part-by-part at the
    returned ``ObjectParts`` sizes for a ``COMPOSITE`` one. An s3-to-s3 (COPY)
    pair compares the two objects' stored checksums directly (both via
    ``GetObjectAttributes``; no bytes read). A missing checksum, a mismatched
    algorithm, or an algorithm that cannot be computed locally is treated as
    differing (copy), so it never skips on an indeterminate comparison. It is a
    replacement ``update_filter=`` strategy, selected instead of the size+time default.

    The S3 client and bucket for each S3 side are taken by resolving ``src`` /
    ``dest`` against ``s3`` (``s3.resolve`` - the same values passed to
    ``s3.sync``); pass ``S3Storage`` instances for a cross-account s3-to-s3 sync,
    exactly as ``sync`` does. ``bucket`` is not on a ``FileInfo``, which is why
    the endpoint is injected explicitly here rather than read from the pair.

    When ``check_size`` is true (the default) a pair whose two sides have
    known, differing sizes is treated as differing (copy) before any
    ``GetObjectAttributes`` or hashing - a shortcut on upload / download and, for
    an s3-to-s3 pair, a guard against a CRC collision (a 32-bit CRC can collide).

    ``pure_max_size`` bounds the pure-Python ``crc32c`` / ``crc64nvme``
    fallback (used only when ``awscrt`` is absent, ~15-20 MiB/s): a larger object
    of that algorithm is treated as indeterminate (copy) instead of being hashed.
    ``None`` (the default) never caps - the comparison is always exact, just slow
    without ``awscrt``.
    """

    __slots__ = ("_dest_storage", "_request_payer", "_src_storage", "check_size", "pure_max_size")

    _strategy_name = "checksum comparison"

    check_size: bool
    pure_max_size: int | None
    # The SDK boundary: ``s3.resolve`` returns a ``Storage`` whose S3 side carries
    # ``.bucket`` / ``.get_client()``. Held as ``Any`` so this module needs no
    # ``S3Storage`` import (which would drag ``botocore`` at import time); the
    # route (``pair.transfer_type``) tells which side is the S3 one.
    _src_storage: Any
    _dest_storage: Any
    _request_payer: str | None

    def __init__(
        self,
        s3: S3,
        src: Location,
        dest: Location,
        *,
        check_size: bool = True,
        pure_max_size: int | None = None,
        request_payer: str | None = None,
    ) -> None:
        # Construct-path SDK touch (the module import stays SDK-free).
        from boto3_s3.s3storage import S3Storage

        self._src_storage = s3.resolve(src)
        self._dest_storage = s3.resolve(dest)
        # Build each S3 side's client now (get_client memoizes): the decides
        # may run on a ParallelFilter pool, and a lazy first build there would
        # race boto3's non-thread-safe client construction (docs/s3.md). An
        # eager build here is what keeps the strategy's documented
        # thread-safety true for S3Storage sides passed without a client.
        for side in (self._src_storage, self._dest_storage):
            if isinstance(side, S3Storage):
                side.get_client()
        self.check_size = check_size
        self.pure_max_size = pure_max_size
        self._request_payer = request_payer

    def _readable_remote_differ(
        self,
        storage: Storage | None,
        readable: FileInfo,
        remote: FileInfo,
        transfer_type: TransferType,
    ) -> bool:
        # The remote (S3) entry belongs to the endpoint the direction names; its
        # storage carries the client/bucket GetObjectAttributes needs.
        s3_storage = (
            self._dest_storage if transfer_type is TransferType.UPLOAD else self._src_storage
        )
        remote_checksum = self._remote_checksum(s3_storage, remote.key)
        if remote_checksum is None:
            return True
        if not _can_compute(remote_checksum.algorithm, readable.size, self.pure_max_size):
            return True
        key = readable.compare_key
        if storage is None or key is None:
            return True  # cannot open the readable side -> treat as differing (copy)
        with storage.open(key, "rb") as fh:
            if remote_checksum.part_sizes is not None:
                # None = the readable side outruns the parts sum (an appended
                # tail): definitely different content, whatever the digests say.
                readable_value = _composite_b64(
                    fh, remote_checksum.algorithm, remote_checksum.part_sizes
                )
            else:
                readable_value = _whole_b64(fh, remote_checksum.algorithm)
        return readable_value != remote_checksum.value

    def _copy_differs(self, src: FileInfo, dest: FileInfo) -> bool:
        """s3-to-s3: compare the two objects' stored checksums (no bytes read).

        The stored value strings are compared directly, so the COMPOSITE part
        sizes are not needed (``need_parts=False`` skips that pagination).
        """
        a = self._remote_checksum(self._src_storage, src.key, need_parts=False)
        b = self._remote_checksum(self._dest_storage, dest.key, need_parts=False)
        if a is None or b is None or a.algorithm != b.algorithm:
            return True
        return a.value != b.value

    def _remote_checksum(
        self, storage: Any, key: str, *, need_parts: bool = True
    ) -> _RemoteChecksum | None:
        return _fetch_remote(
            storage.get_client(),
            storage.bucket,
            key,
            request_payer=self._request_payer,
            need_parts=need_parts,
        )


@dataclass(frozen=True, slots=True)
class _RemoteChecksum:
    """An object's native checksum as read from GetObjectAttributes."""

    algorithm: str  # crc32 / crc32c / crc64nvme / sha1 / sha256
    value: str  # base64 digest, with a "-N" suffix for a COMPOSITE multipart object
    part_sizes: tuple[int, ...] | None  # part byte sizes for a COMPOSITE object, else None


def _fetch_remote(
    client: Any, bucket: str, key: str, *, request_payer: str | None, need_parts: bool = True
) -> _RemoteChecksum | None:
    """GetObjectAttributes -> the object's checksum, or ``None`` (indeterminate).

    ``None`` means "cannot compare, copy": no checksum on the object, a COMPOSITE
    object whose parts could not be read, or any ``ClientError`` anywhere in the
    read (a 404, a denied ``s3:GetObjectAttributes``, an SSE-C object needing a
    key, a failure mid part-size pagination, ...) - errors are swallowed so a
    filter never aborts the sync. ``need_parts=False`` (an s3-to-s3 compare, which
    uses the value string directly) skips the COMPOSITE part-size pagination.
    """
    from botocore.exceptions import ClientError

    extra: dict[str, Any] = {"RequestPayer": request_payer} if request_payer else {}
    try:
        resp: Any = client.get_object_attributes(
            Bucket=bucket, Key=key, ObjectAttributes=["Checksum", "ObjectParts"], **extra
        )
        checksum: Any = resp.get("Checksum") or {}
        algorithm: str | None = None
        value: str | None = None
        for api_key, name in _ALGO_BY_KEY.items():
            raw = checksum.get(api_key)
            if raw:
                algorithm, value = name, raw
                break
        if algorithm is None or value is None:
            return None
        part_sizes: tuple[int, ...] | None = None
        # A composite (multipart) checksum is reported as "base64-N" (the part
        # count suffix); a full-object checksum has no suffix. The standard base64
        # alphabet plus '=' padding never contains '-', so the substring test
        # reliably tells the two apart on real S3 responses - the model's
        # ChecksumType would be marginally more robust, but S3's aws-cli
        # customizations carry no COMPOSITE handling to mirror.
        if need_parts and "-" in value:
            part_sizes = _part_sizes(client, bucket, key, resp, request_payer)
            if part_sizes is None:
                return None
        return _RemoteChecksum(algorithm=algorithm, value=value, part_sizes=part_sizes)
    except ClientError:
        return None


def _part_sizes(
    client: Any, bucket: str, key: str, first: Any, request_payer: str | None
) -> tuple[int, ...] | None:
    """The COMPOSITE object's part byte sizes, in order (paginated past 1000)."""
    parts_info: Any = first.get("ObjectParts")
    if not parts_info:
        return None
    extra: dict[str, Any] = {"RequestPayer": request_payer} if request_payer else {}
    sizes: list[int] = [int(p["Size"]) for p in parts_info.get("Parts", [])]
    truncated: Any = parts_info.get("IsTruncated", False)
    marker: Any = parts_info.get("NextPartNumberMarker")
    while truncated:
        if not marker:
            return None  # truncated but no marker (malformed) -> indeterminate -> copy
        resp: Any = client.get_object_attributes(
            Bucket=bucket,
            Key=key,
            ObjectAttributes=["ObjectParts"],
            PartNumberMarker=int(marker),
            **extra,
        )
        parts_info = resp.get("ObjectParts") or {}
        sizes.extend(int(p["Size"]) for p in parts_info.get("Parts", []))
        truncated = parts_info.get("IsTruncated", False)
        marker = parts_info.get("NextPartNumberMarker")
    # An empty part list is degenerate (S3 multipart objects have >= 1 part);
    # treat it as indeterminate rather than hashing a "-0" that cannot match.
    return tuple(sizes) if sizes else None


def _can_compute(algorithm: str, size: int | None, pure_max_size: int | None) -> bool:
    """Whether we can compute ``algorithm`` locally for an object of ``size``.

    ``crc32`` / ``sha1`` / ``sha256`` always. ``crc32c`` / ``crc64nvme`` always
    with ``awscrt``; without it the bundled pure-Python path can, but
    ``pure_max_size`` may cap it (a larger object reads as indeterminate -> copy).
    Any other (future) algorithm is uncomputable.
    """
    if algorithm in ("crc32", "sha1", "sha256"):
        return True
    if algorithm not in ("crc32c", "crc64nvme"):
        return False
    if _crc_has_awscrt(algorithm):
        return True
    return pure_max_size is None or size is None or size <= pure_max_size


# -- local checksum computation ------------------------------------------------


def _whole_b64(fh: BinaryIO, algorithm: str) -> str:
    """The base64 ``FULL_OBJECT`` checksum of the whole stream ``fh``."""
    hasher = _new_hasher(algorithm)
    assert hasher is not None  # callers gate on _can_compute
    for block in iter(lambda: fh.read(READ_CHUNK), b""):
        hasher.update(block)
    return base64.b64encode(hasher.digest()).decode("ascii")


def _composite_b64(fh: BinaryIO, algorithm: str, part_sizes: tuple[int, ...]) -> str | None:
    """The base64 ``COMPOSITE`` checksum + ``"-N"`` at the exact part boundaries.

    ``base64(ALGO(concat of each part's raw ALGO digest)) + "-<n>"`` - the
    checksum-of-checksums S3 forms for a multipart upload, with the part split
    taken from GetObjectAttributes' ``ObjectParts`` (so it is exact, not guessed),
    read from the stream ``fh``. Both length mismatches read as differing: a
    readable side shorter than the parts sum hashes short (a truncated final
    part), and one *longer* returns ``None`` - the remote object is exactly the
    parts sum long, so an appended tail means different content even though the
    per-part digests (which never see the tail) would collide.
    """
    combined = bytearray()
    for size in part_sizes:
        part = _new_hasher(algorithm)
        assert part is not None
        remaining = size
        while remaining > 0:
            block = fh.read(min(READ_CHUNK, remaining))
            if not block:
                break
            part.update(block)
            remaining -= len(block)
        combined += part.digest()
    if fh.read(1):
        return None
    top = _new_hasher(algorithm)
    assert top is not None
    top.update(bytes(combined))
    return base64.b64encode(top.digest()).decode("ascii") + f"-{len(part_sizes)}"


class _IntCrc:
    """A hashlib-shaped wrapper over an int-returning CRC (``fn(chunk, prev)``)."""

    __slots__ = ("_fn", "_v", "_width")

    def __init__(self, fn: Callable[[bytes, int], int], width: int) -> None:
        self._fn = fn
        self._width = width
        self._v = 0

    def update(self, chunk: bytes) -> None:
        self._v = self._fn(chunk, self._v)

    def digest(self) -> bytes:
        return self._v.to_bytes(self._width, "big")


def _new_hasher(algorithm: str) -> Any | None:
    """A fresh incremental hasher (``.update(bytes)`` / ``.digest() -> bytes``)."""
    if algorithm == "crc32":
        return _IntCrc(zlib.crc32, 4)
    if algorithm in ("sha1", "sha256"):
        return hashlib.new(algorithm)
    if algorithm in ("crc32c", "crc64nvme"):
        return _IntCrc(*_crc_backend(algorithm))
    return None


_CRC_PARAMS = {
    "crc32c": (_POLY_CRC32C, _MASK32, 4),
    "crc64nvme": (_POLY_CRC64NVME, _MASK64, 8),
}


def _crc_backend(name: str) -> tuple[Callable[[bytes, int], int], int]:
    """The (update, width) for ``crc32c`` / ``crc64nvme``: awscrt if present, else pure."""
    width = _CRC_PARAMS[name][2]
    crt = _awscrt_checksums()
    if crt is not None:
        fn = getattr(crt, name, None)
        if fn is not None:
            return fn, width
    return _PURE_CRC[name], width


@functools.lru_cache(maxsize=1)
def _awscrt_checksums() -> Any | None:
    """``awscrt.checksums`` if installed, else ``None`` (the SDK touch, deferred)."""
    try:
        from awscrt import checksums
    except ImportError:
        return None
    return checksums


def _crc_has_awscrt(name: str) -> bool:
    crt = _awscrt_checksums()
    return crt is not None and getattr(crt, name, None) is not None


@functools.cache
def _slice8_tables(poly: int, mask: int) -> tuple[list[int], ...]:
    """The eight slicing-by-8 tables for a reflected CRC (``T0`` plus ``T1..T7``)."""
    base: list[int] = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ (poly if c & 1 else 0)
        base.append(c & mask)
    tables: list[list[int]] = [base]
    for _k in range(1, 8):
        prev = tables[-1]
        tables.append([(prev[i] >> 8) ^ base[prev[i] & 0xFF] for i in range(256)])
    return tuple(tables)


def _pure_crc32c(chunk: bytes, prev: int = 0) -> int:
    """Pure-Python CRC32C (slicing-by-8), awscrt/zlib seed convention (0 = fresh)."""
    t0, t1, t2, t3, t4, t5, t6, t7 = _slice8_tables(_POLY_CRC32C, _MASK32)
    c = prev ^ _MASK32
    mv = memoryview(chunk)
    tail = chunk
    if _LITTLE:
        nw = len(chunk) // 8
        for q in mv[: nw * 8].cast("Q"):  # 8 bytes as one little-endian uint64
            one = c ^ (q & _MASK32)  # low 4 bytes XORed into the 32-bit register
            hi = q >> 32
            c = (
                t7[one & 0xFF]
                ^ t6[(one >> 8) & 0xFF]
                ^ t5[(one >> 16) & 0xFF]
                ^ t4[(one >> 24) & 0xFF]
                ^ t3[hi & 0xFF]
                ^ t2[(hi >> 8) & 0xFF]
                ^ t1[(hi >> 16) & 0xFF]
                ^ t0[(hi >> 24) & 0xFF]
            )
        tail = bytes(mv[nw * 8 :])
    for b in tail:
        c = (c >> 8) ^ t0[(c ^ b) & 0xFF]
    return c ^ _MASK32


def _pure_crc64nvme(chunk: bytes, prev: int = 0) -> int:
    """Pure-Python CRC-64/NVME (slicing-by-8), seed convention (0 = fresh)."""
    t0, t1, t2, t3, t4, t5, t6, t7 = _slice8_tables(_POLY_CRC64NVME, _MASK64)
    c = prev ^ _MASK64
    mv = memoryview(chunk)
    tail = chunk
    if _LITTLE:
        nw = len(chunk) // 8
        for q in mv[: nw * 8].cast("Q"):
            c ^= q
            c = (
                t7[c & 0xFF]
                ^ t6[(c >> 8) & 0xFF]
                ^ t5[(c >> 16) & 0xFF]
                ^ t4[(c >> 24) & 0xFF]
                ^ t3[(c >> 32) & 0xFF]
                ^ t2[(c >> 40) & 0xFF]
                ^ t1[(c >> 48) & 0xFF]
                ^ t0[(c >> 56) & 0xFF]
            )
        tail = bytes(mv[nw * 8 :])
    for b in tail:
        c = (c >> 8) ^ t0[(c ^ b) & 0xFF]
    return c ^ _MASK64


_PURE_CRC: dict[str, Callable[[bytes, int], int]] = {
    "crc32c": _pure_crc32c,
    "crc64nvme": _pure_crc64nvme,
}


__all__ = ["ChecksumComparison"]
