"""Unit tests for ``boto3_s3.etagcompare``: the ETag content-comparison PairFilter.

Pins ``EtagComparison``'s construction (the ``s3`` / ``part_size`` / ``check_size``
knobs), the s3->s3 direct-ETag comparison and its size collision-guard, the
upload / download single- and multipart reconstruction at ``part_size``, the
missing / non-MD5 -> differ rule, and the documented caveats (part-size
fragility, SSE opaque etag, the empty-object "-0" avoidance).

Multipart ETag expectations are anchored to offline-computed goldens (recipe at
the bottom of this module) and cross-checked against an independently shaped
in-test computation (``TestGoldenCrossCheck``), so a bug shared by both copies
of the algorithm cannot pass. The single-part goldens are the published MD5s of
their bytes (e.g. ``printf '0123456789' | md5sum``).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import boto3
import pytest

from boto3_s3 import S3
from boto3_s3.comparator import SyncPair
from boto3_s3.etagcompare import (
    DEFAULT_PART_SIZE,
    EtagComparison,
    _effective_part_size,  # pyright: ignore[reportPrivateUsage]
    _file_md5_hex,  # pyright: ignore[reportPrivateUsage]
    _multipart_etag_at,  # pyright: ignore[reportPrivateUsage]
)
from boto3_s3.exceptions import Boto3S3Error
from boto3_s3.localstorage import LocalStorage, to_native_path
from boto3_s3.storage import Storage
from boto3_s3.types import FileInfo, LocalFileInfo, S3FileInfo, TransferType

_MIB = 1024 * 1024

# Goldens - generated offline; see the recipe at the bottom of this module.
_TEN = b"0123456789"
_TEN_SINGLE = "781e5e245d69b566979b86e28d23f2c7"
_TEN_MP4 = "61e3716e3a7767581863b67c4e785584-3"
_TEN_MP5 = "9a6dbec798b1bfe66cc7659d2bb41720-2"
_TEN_MP10 = "8e938564cd1410f0ec1c1781466a6738-1"
_EMPTY_SINGLE = "d41d8cd98f00b204e9800998ecf8427e"
_EMPTY_MP = "d41d8cd98f00b204e9800998ecf8427e-0"
_CONTENT_6MIB = b"0123456789abcdef" * ((6 * _MIB) // 16)
_CONTENT_6MIB_MP5 = "6976d829a1a06b80396a19f0a92087e6-2"


def _write(tmp_path: Path, data: bytes, name: str = "f") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _key(p: Path) -> str:
    """The ``/``-separated FileInfo key for a real path (round-trips via to_native_path)."""
    return str(p).replace(os.sep, "/")


def _local(key: str, *, size: int | None = None) -> LocalFileInfo:
    # compare_key is the basename: the content strategy opens it against a
    # LocalStorage rooted at the file's parent (see _storage_for).
    return LocalFileInfo(key=key, size=size, compare_key=key.rsplit("/", 1)[-1])


def _s3(*, etag: str | None = None, size: int | None = None, key: str = "k") -> S3FileInfo:
    return S3FileInfo(key=key, etag=etag, size=size)


def _storage_for(info: FileInfo) -> Storage | None:
    """A LocalStorage rooted at a local side's parent dir, so ``open(compare_key)``
    reaches the real file (the readable side the strategy hashes)."""
    if isinstance(info, LocalFileInfo):
        return LocalStorage(os.path.dirname(to_native_path(info.key)))
    return None


def _pair(transfer_type: TransferType, *, src: FileInfo, dest: FileInfo) -> SyncPair:
    # The backend rides on each side's FileInfo; the strategy reads pair.src.storage
    # / pair.dest.storage to open the readable (local) side.
    src.storage = _storage_for(src)
    dest.storage = _storage_for(dest)
    return SyncPair(compare_key="k", transfer_type=transfer_type, src=src, dest=dest)


def _md5_hex_of(path: Path) -> str:
    """The single-part helper over a file (opens the stream the helper now takes)."""
    with open(path, "rb") as fh:
        return _file_md5_hex(fh)


def _mp_etag_of(path: Path, chunk: int) -> str:
    """The multipart helper over a file at ``chunk`` size."""
    with open(path, "rb") as fh:
        return _multipart_etag_at(fh, chunk_size=chunk)


def _independent_single(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _independent_multipart(data: bytes, part: int) -> str:
    # Deliberately a different shape than the module's streaming reader: slice the
    # whole bytes, hash each slice, hash the concatenation of the digests.
    chunks = [data[i : i + part] for i in range(0, len(data), part)] if data else []
    concat = b"".join(hashlib.md5(c).digest() for c in chunks)
    return hashlib.md5(concat).hexdigest() + f"-{len(chunks)}"


class _FakeS3:
    """Minimal stand-in exposing only what ``EtagComparison`` calls on an ``S3``."""

    def __init__(self, chunksize: int) -> None:
        self.chunksize = chunksize
        self.aws_config_calls = 0
        self.get_size_calls: list[tuple[str, int]] = []

    def aws_config(self) -> _FakeS3:
        self.aws_config_calls += 1
        return self

    def get_size(self, key: str, default: int) -> int:
        self.get_size_calls.append((key, default))
        return self.chunksize


def _etag(s3: Any = None, **kw: Any) -> EtagComparison:
    """Construct the comparison; centralizes the attribute-check tests' build."""
    return EtagComparison(s3, **kw)


class TestConstruction:
    def test_default_part_size_and_check_size(self) -> None:
        f = _etag()
        assert f.part_size == DEFAULT_PART_SIZE
        assert f.check_size is True

    def test_explicit_part_size(self) -> None:
        assert _etag(part_size=16 * _MIB).part_size == 16 * _MIB

    def test_s3_derives_part_size_from_profile(self) -> None:
        fake = _FakeS3(32 * _MIB)
        f = _etag(fake)
        assert f.part_size == 32 * _MIB
        assert fake.get_size_calls == [("s3.multipart_chunksize", DEFAULT_PART_SIZE)]

    def test_explicit_part_size_overrides_s3_unconsulted(self) -> None:
        # The permissive layer: passing both is not an error - the explicit value
        # wins and s3 is never consulted.
        fake = _FakeS3(32 * _MIB)
        f = _etag(fake, part_size=16 * _MIB)
        assert f.part_size == 16 * _MIB
        assert fake.aws_config_calls == 0

    def test_check_size_flag(self) -> None:
        assert _etag(check_size=False).check_size is False


class TestS3Integration:
    """``EtagComparison(s3)`` against a real boto3 session over a temp config file."""

    def test_part_size_from_config_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "config"
        cfg.write_text("[default]\ns3 =\n    multipart_chunksize = 16MB\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
        s3 = S3(session=boto3.Session())
        assert _etag(s3).part_size == 16 * _MIB

    def test_absent_key_falls_back_to_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = tmp_path / "config"
        cfg.write_text("[default]\nregion = us-east-1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
        s3 = S3(session=boto3.Session())
        assert _etag(s3).part_size == DEFAULT_PART_SIZE


class TestCopyDirectEtag:
    """s3->s3 compares the listings' ETags directly; no bytes read."""

    def test_equal_etags_skip(self) -> None:
        pair = _pair(TransferType.COPY, src=_s3(etag="abc"), dest=_s3(etag="abc"))
        assert EtagComparison()(pair) is False

    def test_differing_etags_copy(self) -> None:
        pair = _pair(TransferType.COPY, src=_s3(etag="abc"), dest=_s3(etag="xyz"))
        assert EtagComparison()(pair) is True

    def test_missing_etag_either_side_copies(self) -> None:
        assert (
            EtagComparison()(_pair(TransferType.COPY, src=_s3(etag=None), dest=_s3(etag="abc")))
            is True
        )
        assert (
            EtagComparison()(_pair(TransferType.COPY, src=_s3(etag="abc"), dest=_s3(etag=None)))
            is True
        )

    def test_non_s3_side_counts_as_differ(self) -> None:
        # A side that is not an S3FileInfo has no comparable etag -> differ.
        pair = _pair(TransferType.COPY, src=FileInfo(key="k"), dest=_s3(etag="abc"))
        assert EtagComparison()(pair) is True

    def test_size_collision_guard(self) -> None:
        # Equal etag but different size: check_size forces a copy (MD5 collisions
        # exist), while pure-etag mode trusts the equality and skips.
        pair = _pair(TransferType.COPY, src=_s3(etag="abc", size=10), dest=_s3(etag="abc", size=20))
        assert EtagComparison(check_size=True)(pair) is True
        assert EtagComparison(check_size=False)(pair) is False


class TestTypeMatchedSides:
    """Which side is the S3 object is a *type* decision, not a direction one."""

    @pytest.mark.parametrize("transfer_type", list(TransferType))
    def test_two_s3_sides_compare_by_etag_any_direction(self, transfer_type: TransferType) -> None:
        # Two S3FileInfo sides -> the s3-to-s3 ETag compare, whatever the (nominal)
        # transfer_type says (MOVE / DELETE included) - no local side is assumed.
        differ = _pair(transfer_type, src=_s3(etag="a", size=10), dest=_s3(etag="b", size=10))
        assert EtagComparison()(differ) is True
        same = _pair(transfer_type, src=_s3(etag="a", size=10), dest=_s3(etag="a", size=10))
        assert EtagComparison()(same) is False

    def test_neither_side_s3_copies(self) -> None:
        # No S3 object on either side -> no stored digest to compare against -> copy.
        pair = _pair(
            TransferType.UPLOAD, src=FileInfo(key="k", size=10), dest=FileInfo(key="k", size=10)
        )
        assert EtagComparison()(pair) is True


class TestSinglePartReconstruction:
    def test_upload_matching_md5_skips(self, tmp_path: Path) -> None:
        p = _write(tmp_path, _TEN)
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dest=_s3(etag=_TEN_SINGLE, size=len(_TEN)),
        )
        assert EtagComparison()(pair) is False

    def test_upload_mismatch_copies(self, tmp_path: Path) -> None:
        p = _write(tmp_path, _TEN)
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dest=_s3(etag="0" * 32, size=len(_TEN)),
        )
        assert EtagComparison()(pair) is True

    def test_download_swaps_local_and_remote(self, tmp_path: Path) -> None:
        # DOWNLOAD: local = dest, remote = src; a matching reconstruction skips.
        p = _write(tmp_path, _TEN)
        pair = _pair(
            TransferType.DOWNLOAD,
            src=_s3(etag=_TEN_SINGLE, size=len(_TEN)),
            dest=_local(_key(p), size=len(_TEN)),
        )
        assert EtagComparison()(pair) is False

    def test_missing_remote_etag_copies_before_read(self, tmp_path: Path) -> None:
        # No remote etag -> copy, without touching the (nonexistent) local file.
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(tmp_path / "nope"), size=10),
            dest=_s3(etag=None, size=10),
        )
        assert EtagComparison()(pair) is True


class TestMultipartReconstruction:
    def test_single_part_via_clamp_dash_one(self, tmp_path: Path) -> None:
        # A "-1" remote etag: the effective part (>= 5 MiB) makes the 10-byte file
        # exactly one part, exercising the multipart branch end-to-end on a tiny
        # file (no >5 MiB write needed).
        p = _write(tmp_path, _TEN)
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dest=_s3(etag=_TEN_MP10, size=len(_TEN)),
        )
        assert EtagComparison()(pair) is False

    def test_real_multipart_match_skips(self, tmp_path: Path) -> None:
        # The one genuine multi-part __call__ case: 6 MiB at a 5 MiB part -> 2 parts.
        p = _write(tmp_path, _CONTENT_6MIB)
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(p), size=len(_CONTENT_6MIB)),
            dest=_s3(etag=_CONTENT_6MIB_MP5, size=len(_CONTENT_6MIB)),
        )
        assert EtagComparison(part_size=5 * _MIB)(pair) is False

    def test_download_multipart_match_skips(self, tmp_path: Path) -> None:
        # DOWNLOAD swaps local/remote (local = dest): the S3 src etag is compared
        # against the reconstructed local dest hash.
        p = _write(tmp_path, _CONTENT_6MIB)
        pair = _pair(
            TransferType.DOWNLOAD,
            src=_s3(etag=_CONTENT_6MIB_MP5, size=len(_CONTENT_6MIB)),
            dest=_local(_key(p), size=len(_CONTENT_6MIB)),
        )
        assert EtagComparison(part_size=5 * _MIB)(pair) is False

    def test_real_multipart_wrong_hash_copies(self, tmp_path: Path) -> None:
        p = _write(tmp_path, _CONTENT_6MIB)
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(p), size=len(_CONTENT_6MIB)),
            dest=_s3(etag="0" * 32 + "-2", size=len(_CONTENT_6MIB)),
        )
        assert EtagComparison(part_size=5 * _MIB)(pair) is True

    def test_real_multipart_wrong_count_copies(self, tmp_path: Path) -> None:
        # Correct hex digits, wrong part count -> the count participates -> differ.
        p = _write(tmp_path, _CONTENT_6MIB)
        wrong = _CONTENT_6MIB_MP5.split("-")[0] + "-3"
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(p), size=len(_CONTENT_6MIB)),
            dest=_s3(etag=wrong, size=len(_CONTENT_6MIB)),
        )
        assert EtagComparison(part_size=5 * _MIB)(pair) is True

    def test_part_size_fragility_at_helper(self, tmp_path: Path) -> None:
        # An object hashed at a different part size will not match (the rclone
        # chunk-size constraint). Pinned at the helper layer because __call__'s
        # effective part size is clamped to >= 5 MiB.
        p = _write(tmp_path, _TEN)
        assert _mp_etag_of(p, 4) != _mp_etag_of(p, 5)


class TestEmptyFile:
    def test_empty_object_uses_single_part_branch(self, tmp_path: Path) -> None:
        # A real empty object's etag has no "-" suffix -> single-part branch ->
        # matches md5("") and skips. The "-0" multipart trap is never reached.
        p = _write(tmp_path, b"")
        pair = _pair(
            TransferType.UPLOAD, src=_local(_key(p), size=0), dest=_s3(etag=_EMPTY_SINGLE, size=0)
        )
        assert EtagComparison()(pair) is False

    def test_helper_empty_multipart_is_dash_zero(self, tmp_path: Path) -> None:
        # The trap the branch avoids: the multipart helper on an empty file yields
        # "...-0" (proving there is no `or [b""]` fallback).
        assert _mp_etag_of(_write(tmp_path, b""), 5) == _EMPTY_MP


class TestOpaqueEtag:
    """SSE-KMS / SSE-C / DSSE objects carry an opaque etag -> always differ."""

    def test_kms_style_multipart_etag_copies(self, tmp_path: Path) -> None:
        p = _write(tmp_path, _TEN)
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dest=_s3(etag="ffffffffffffffffffffffffffffffff-7", size=len(_TEN)),
        )
        assert EtagComparison()(pair) is True

    def test_opaque_single_etag_copies(self, tmp_path: Path) -> None:
        # A single-part SSE etag is a 32-hex that is not the plaintext MD5.
        p = _write(tmp_path, _TEN)
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dest=_s3(etag="deadbeef" * 4, size=len(_TEN)),
        )
        assert EtagComparison()(pair) is True


class TestSizeCheckToggle:
    """The size check on the upload / download path (collision guard + perf)."""

    def test_size_mismatch_short_circuits_before_read(self, tmp_path: Path) -> None:
        # check_size on: a size mismatch returns True without opening the file, so
        # a nonexistent local path raises nothing.
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(tmp_path / "nope"), size=10),
            dest=_s3(etag="abc", size=20),
        )
        assert EtagComparison(check_size=True)(pair) is True

    def test_download_size_mismatch_short_circuits(self, tmp_path: Path) -> None:
        # The check is uniform across directions: a DOWNLOAD size mismatch also
        # short-circuits before touching the (nonexistent) local dest.
        pair = _pair(
            TransferType.DOWNLOAD,
            src=_s3(etag=_TEN_SINGLE, size=10),
            dest=_local(_key(tmp_path / "nope"), size=20),
        )
        assert EtagComparison(check_size=True)(pair) is True

    def test_size_check_off_reads_and_raises_on_missing(self, tmp_path: Path) -> None:
        # check_size off: no short-circuit, so the missing file is opened. The read
        # now goes through Storage.open, whose OSError is translated to a Boto3S3Error.
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(tmp_path / "nope"), size=10),
            dest=_s3(etag="abc", size=20),
        )
        with pytest.raises(Boto3S3Error):
            EtagComparison(check_size=False)(pair)

    def test_equal_size_still_hashes(self, tmp_path: Path) -> None:
        # Same size, different content: the size check passes through to the hash.
        p = _write(tmp_path, _TEN)
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dest=_s3(etag="0" * 32, size=len(_TEN)),
        )
        assert EtagComparison(check_size=True)(pair) is True

    def test_unknown_size_does_not_short_circuit(self, tmp_path: Path) -> None:
        # A None size on a side disables the pre-check; the hash decides (skip).
        p = _write(tmp_path, _TEN)
        pair = _pair(
            TransferType.UPLOAD,
            src=_local(_key(p), size=None),
            dest=_s3(etag=_TEN_SINGLE, size=10),
        )
        assert EtagComparison(check_size=True)(pair) is False


class TestHelperUnits:
    def test_file_md5_hex(self, tmp_path: Path) -> None:
        assert _md5_hex_of(_write(tmp_path, _TEN)) == _TEN_SINGLE
        assert _md5_hex_of(_write(tmp_path, b"", "e")) == _EMPTY_SINGLE

    @pytest.mark.parametrize(("chunk", "expected"), [(4, _TEN_MP4), (5, _TEN_MP5), (10, _TEN_MP10)])
    def test_multipart_etag_at(self, tmp_path: Path, chunk: int, expected: str) -> None:
        assert _mp_etag_of(_write(tmp_path, _TEN), chunk) == expected

    def test_effective_part_size_clamps_below_5mib(self) -> None:
        assert _effective_part_size(1024, 3000) == 5 * _MIB

    def test_effective_part_size_grows_past_part_limit(self) -> None:
        # A file that would exceed 10000 parts at 5 MiB grows the part size - the
        # only case that truly exercises the adjuster (no multi-GiB write needed).
        big = 5 * _MIB * 10001
        grown = _effective_part_size(5 * _MIB, big)
        assert grown > 5 * _MIB
        assert (big + grown - 1) // grown <= 10000

    def test_effective_part_size_passthrough(self) -> None:
        assert _effective_part_size(8 * _MIB, 100) == 8 * _MIB


class TestGoldenCrossCheck:
    """The hardcoded goldens equal an independently shaped recomputation."""

    def test_single_goldens(self) -> None:
        assert _TEN_SINGLE == _independent_single(_TEN)
        assert _EMPTY_SINGLE == _independent_single(b"")

    @pytest.mark.parametrize(
        ("data", "part", "golden"),
        [
            (_TEN, 4, _TEN_MP4),
            (_TEN, 5, _TEN_MP5),
            (_TEN, 10, _TEN_MP10),
            (_CONTENT_6MIB, 5 * _MIB, _CONTENT_6MIB_MP5),
        ],
        # Explicit ids: the bytes params would otherwise become megabyte-long
        # test ids, overflowing Windows' 32767-char cap on PYTEST_CURRENT_TEST.
        ids=["ten-part4", "ten-part5", "ten-part10", "6mib-part5mib"],
    )
    def test_multipart_goldens(self, data: bytes, part: int, golden: str) -> None:
        assert golden == _independent_multipart(data, part)


# Regenerate the goldens offline (the spec-level S3 ETag formula, anchored for the
# single-part values to `printf '0123456789' | md5sum`):
#
#   import hashlib
#   def s3_etag(data, part=None):
#       if part is None:
#           return hashlib.md5(data).hexdigest()
#       chunks = [data[i:i+part] for i in range(0, len(data), part)] if data else []
#       concat = b"".join(hashlib.md5(c).digest() for c in chunks)
#       return hashlib.md5(concat).hexdigest() + f"-{len(chunks)}"
#   s3_etag(b"0123456789")        # _TEN_SINGLE
#   s3_etag(b"0123456789", 4)     # _TEN_MP4   (and 5 -> _TEN_MP5, 10 -> _TEN_MP10)
#   content6 = b"0123456789abcdef" * ((6 * 1024 * 1024) // 16)
#   s3_etag(content6, 5 * 1024 * 1024)   # _CONTENT_6MIB_MP5 (2 parts)
