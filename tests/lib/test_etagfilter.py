"""Unit tests for ``boto3_s3.etagfilter``: the ETag content-comparison PairFilter.

Pins ``by_etag``'s construction (the ``s3`` / ``part_size`` / ``check_size``
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
from boto3_s3.etagfilter import (
    DEFAULT_PART_SIZE,
    _effective_part_size,  # pyright: ignore[reportPrivateUsage]
    _EtagComparison,  # pyright: ignore[reportPrivateUsage]
    _file_md5_hex,  # pyright: ignore[reportPrivateUsage]
    _multipart_etag_at,  # pyright: ignore[reportPrivateUsage]
    by_etag,
)
from boto3_s3.types import FileInfo, LocalFileInfo, OpKind, S3FileInfo

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
    return LocalFileInfo(key=key, size=size)


def _s3(*, etag: str | None = None, size: int | None = None, key: str = "k") -> S3FileInfo:
    return S3FileInfo(key=key, etag=etag, size=size)


def _pair(kind: OpKind, *, src: FileInfo | None = None, dst: FileInfo | None = None) -> SyncPair:
    return SyncPair(key="k", kind=kind, src=src, dst=dst)


def _independent_single(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _independent_multipart(data: bytes, part: int) -> str:
    # Deliberately a different shape than the module's streaming reader: slice the
    # whole bytes, hash each slice, hash the concatenation of the digests.
    chunks = [data[i : i + part] for i in range(0, len(data), part)] if data else []
    concat = b"".join(hashlib.md5(c).digest() for c in chunks)
    return hashlib.md5(concat).hexdigest() + f"-{len(chunks)}"


class _FakeS3:
    """Minimal stand-in exposing only what ``by_etag`` calls on an ``S3``."""

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


def _etag(s3: Any = None, **kw: Any) -> _EtagComparison:
    """Build via the factory and narrow to the concrete type for attribute checks."""
    f = by_etag(s3, **kw)
    assert isinstance(f, _EtagComparison)
    return f


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
    """``by_etag(s3)`` against a real boto3 session over a temp config file."""

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
        pair = _pair(OpKind.COPY, src=_s3(etag="abc"), dst=_s3(etag="abc"))
        assert by_etag()(pair) is False

    def test_differing_etags_copy(self) -> None:
        pair = _pair(OpKind.COPY, src=_s3(etag="abc"), dst=_s3(etag="xyz"))
        assert by_etag()(pair) is True

    def test_missing_etag_either_side_copies(self) -> None:
        assert by_etag()(_pair(OpKind.COPY, src=_s3(etag=None), dst=_s3(etag="abc"))) is True
        assert by_etag()(_pair(OpKind.COPY, src=_s3(etag="abc"), dst=_s3(etag=None))) is True

    def test_non_s3_side_counts_as_differ(self) -> None:
        # A side that is not an S3FileInfo has no comparable etag -> differ.
        pair = _pair(OpKind.COPY, src=FileInfo(key="k"), dst=_s3(etag="abc"))
        assert by_etag()(pair) is True

    def test_reads_no_bytes(self) -> None:
        # Keys point at nothing on disk; the equal-etag skip still holds.
        pair = _pair(OpKind.COPY, src=_s3(etag="abc", key="nope"), dst=_s3(etag="abc", key="nope"))
        assert by_etag()(pair) is False

    def test_size_collision_guard(self) -> None:
        # Equal etag but different size: check_size forces a copy (MD5 collisions
        # exist), while pure-etag mode trusts the equality and skips.
        pair = _pair(OpKind.COPY, src=_s3(etag="abc", size=10), dst=_s3(etag="abc", size=20))
        assert by_etag(check_size=True)(pair) is True
        assert by_etag(check_size=False)(pair) is False


class TestNewAndMissingSides:
    @pytest.mark.parametrize("kind", list(OpKind))
    def test_dst_none_always_copies(self, kind: OpKind) -> None:
        # The dst-None short-circuit precedes the kind switch, so even MOVE /
        # DELETE return True here.
        assert by_etag()(_pair(kind, src=_s3(etag="abc"))) is True

    @pytest.mark.parametrize("kind", list(OpKind))
    def test_src_none_raises(self, kind: OpKind) -> None:
        with pytest.raises(ValueError, match="without a source entry"):
            by_etag()(_pair(kind, dst=_s3(etag="abc")))


class TestUnsupportedKindsAndSides:
    def test_move_kind_raises(self) -> None:
        pair = _pair(OpKind.MOVE, src=_s3(etag="a", size=10), dst=_s3(etag="b", size=10))
        with pytest.raises(ValueError, match="cannot judge a 'move' pair"):
            by_etag()(pair)

    def test_delete_kind_raises(self) -> None:
        pair = _pair(OpKind.DELETE, src=_s3(etag="a", size=10), dst=_s3(etag="b", size=10))
        with pytest.raises(ValueError, match="cannot judge a 'delete' pair"):
            by_etag()(pair)

    def test_upload_non_local_local_side_raises(self) -> None:
        # UPLOAD: local = src; an S3 src has no local file to hash.
        pair = _pair(OpKind.UPLOAD, src=_s3(etag="a", size=10), dst=_s3(etag="b", size=10))
        with pytest.raises(ValueError, match="no local side"):
            by_etag()(pair)

    def test_download_non_local_local_side_raises(self) -> None:
        # DOWNLOAD: local = dst; an S3 dst has no local file to hash.
        pair = _pair(OpKind.DOWNLOAD, src=_s3(etag="a", size=10), dst=_s3(etag="b", size=10))
        with pytest.raises(ValueError, match="no local side"):
            by_etag()(pair)


class TestSinglePartReconstruction:
    def test_upload_matching_md5_skips(self, tmp_path: Path) -> None:
        p = _write(tmp_path, _TEN)
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dst=_s3(etag=_TEN_SINGLE, size=len(_TEN)),
        )
        assert by_etag()(pair) is False

    def test_upload_mismatch_copies(self, tmp_path: Path) -> None:
        p = _write(tmp_path, _TEN)
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dst=_s3(etag="0" * 32, size=len(_TEN)),
        )
        assert by_etag()(pair) is True

    def test_download_swaps_local_and_remote(self, tmp_path: Path) -> None:
        # DOWNLOAD: local = dst, remote = src; a matching reconstruction skips.
        p = _write(tmp_path, _TEN)
        pair = _pair(
            OpKind.DOWNLOAD,
            src=_s3(etag=_TEN_SINGLE, size=len(_TEN)),
            dst=_local(_key(p), size=len(_TEN)),
        )
        assert by_etag()(pair) is False

    def test_missing_remote_etag_copies_before_read(self, tmp_path: Path) -> None:
        # No remote etag -> copy, without touching the (nonexistent) local file.
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(tmp_path / "nope"), size=10),
            dst=_s3(etag=None, size=10),
        )
        assert by_etag()(pair) is True


class TestMultipartReconstruction:
    def test_single_part_via_clamp_dash_one(self, tmp_path: Path) -> None:
        # A "-1" remote etag: the effective part (>= 5 MiB) makes the 10-byte file
        # exactly one part, exercising the multipart branch end-to-end on a tiny
        # file (no >5 MiB write needed).
        p = _write(tmp_path, _TEN)
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dst=_s3(etag=_TEN_MP10, size=len(_TEN)),
        )
        assert by_etag()(pair) is False

    def test_real_multipart_match_skips(self, tmp_path: Path) -> None:
        # The one genuine multi-part __call__ case: 6 MiB at a 5 MiB part -> 2 parts.
        p = _write(tmp_path, _CONTENT_6MIB)
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(p), size=len(_CONTENT_6MIB)),
            dst=_s3(etag=_CONTENT_6MIB_MP5, size=len(_CONTENT_6MIB)),
        )
        assert by_etag(part_size=5 * _MIB)(pair) is False

    def test_download_multipart_match_skips(self, tmp_path: Path) -> None:
        # DOWNLOAD swaps local/remote (local = dst): the S3 src etag is compared
        # against the reconstructed local dst hash.
        p = _write(tmp_path, _CONTENT_6MIB)
        pair = _pair(
            OpKind.DOWNLOAD,
            src=_s3(etag=_CONTENT_6MIB_MP5, size=len(_CONTENT_6MIB)),
            dst=_local(_key(p), size=len(_CONTENT_6MIB)),
        )
        assert by_etag(part_size=5 * _MIB)(pair) is False

    def test_real_multipart_wrong_hash_copies(self, tmp_path: Path) -> None:
        p = _write(tmp_path, _CONTENT_6MIB)
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(p), size=len(_CONTENT_6MIB)),
            dst=_s3(etag="0" * 32 + "-2", size=len(_CONTENT_6MIB)),
        )
        assert by_etag(part_size=5 * _MIB)(pair) is True

    def test_real_multipart_wrong_count_copies(self, tmp_path: Path) -> None:
        # Correct hex digits, wrong part count -> the count participates -> differ.
        p = _write(tmp_path, _CONTENT_6MIB)
        wrong = _CONTENT_6MIB_MP5.split("-")[0] + "-3"
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(p), size=len(_CONTENT_6MIB)),
            dst=_s3(etag=wrong, size=len(_CONTENT_6MIB)),
        )
        assert by_etag(part_size=5 * _MIB)(pair) is True

    def test_part_size_fragility_at_helper(self, tmp_path: Path) -> None:
        # An object hashed at a different part size will not match (the rclone
        # chunk-size constraint). Pinned at the helper layer because __call__'s
        # effective part size is clamped to >= 5 MiB.
        p = str(_write(tmp_path, _TEN))
        assert _multipart_etag_at(p, chunk_size=4) != _multipart_etag_at(p, chunk_size=5)


class TestEmptyFile:
    def test_empty_object_uses_single_part_branch(self, tmp_path: Path) -> None:
        # A real empty object's etag has no "-" suffix -> single-part branch ->
        # matches md5("") and skips. The "-0" multipart trap is never reached.
        p = _write(tmp_path, b"")
        pair = _pair(
            OpKind.UPLOAD, src=_local(_key(p), size=0), dst=_s3(etag=_EMPTY_SINGLE, size=0)
        )
        assert by_etag()(pair) is False

    def test_helper_empty_multipart_is_dash_zero(self, tmp_path: Path) -> None:
        # The trap the branch avoids: the multipart helper on an empty file yields
        # "...-0" (proving there is no `or [b""]` fallback).
        assert _multipart_etag_at(str(_write(tmp_path, b"")), chunk_size=5) == _EMPTY_MP


class TestOpaqueEtag:
    """SSE-KMS / SSE-C / DSSE objects carry an opaque etag -> always differ."""

    def test_kms_style_multipart_etag_copies(self, tmp_path: Path) -> None:
        p = _write(tmp_path, _TEN)
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dst=_s3(etag="ffffffffffffffffffffffffffffffff-7", size=len(_TEN)),
        )
        assert by_etag()(pair) is True

    def test_opaque_single_etag_copies(self, tmp_path: Path) -> None:
        # A single-part SSE etag is a 32-hex that is not the plaintext MD5.
        p = _write(tmp_path, _TEN)
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dst=_s3(etag="deadbeef" * 4, size=len(_TEN)),
        )
        assert by_etag()(pair) is True


class TestSizeCheckToggle:
    """The size check on the upload / download path (collision guard + perf)."""

    def test_size_mismatch_short_circuits_before_read(self, tmp_path: Path) -> None:
        # check_size on: a size mismatch returns True without opening the file, so
        # a nonexistent local path raises nothing.
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(tmp_path / "nope"), size=10),
            dst=_s3(etag="abc", size=20),
        )
        assert by_etag(check_size=True)(pair) is True

    def test_download_size_mismatch_short_circuits(self, tmp_path: Path) -> None:
        # The check is uniform across directions: a DOWNLOAD size mismatch also
        # short-circuits before touching the (nonexistent) local dst.
        pair = _pair(
            OpKind.DOWNLOAD,
            src=_s3(etag=_TEN_SINGLE, size=10),
            dst=_local(_key(tmp_path / "nope"), size=20),
        )
        assert by_etag(check_size=True)(pair) is True

    def test_size_check_off_reads_and_raises_on_missing(self, tmp_path: Path) -> None:
        # check_size off: no short-circuit, so the missing file is opened -> OSError.
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(tmp_path / "nope"), size=10),
            dst=_s3(etag="abc", size=20),
        )
        with pytest.raises(OSError):
            by_etag(check_size=False)(pair)

    def test_equal_size_still_hashes(self, tmp_path: Path) -> None:
        # Same size, different content: the size check passes through to the hash.
        p = _write(tmp_path, _TEN)
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(p), size=len(_TEN)),
            dst=_s3(etag="0" * 32, size=len(_TEN)),
        )
        assert by_etag(check_size=True)(pair) is True

    def test_unknown_size_does_not_short_circuit(self, tmp_path: Path) -> None:
        # A None size on a side disables the pre-check; the hash decides (skip).
        p = _write(tmp_path, _TEN)
        pair = _pair(
            OpKind.UPLOAD,
            src=_local(_key(p), size=None),
            dst=_s3(etag=_TEN_SINGLE, size=10),
        )
        assert by_etag(check_size=True)(pair) is False


class TestHelperUnits:
    def test_file_md5_hex(self, tmp_path: Path) -> None:
        assert _file_md5_hex(str(_write(tmp_path, _TEN))) == _TEN_SINGLE
        assert _file_md5_hex(str(_write(tmp_path, b"", "e"))) == _EMPTY_SINGLE

    @pytest.mark.parametrize(("chunk", "expected"), [(4, _TEN_MP4), (5, _TEN_MP5), (10, _TEN_MP10)])
    def test_multipart_etag_at(self, tmp_path: Path, chunk: int, expected: str) -> None:
        assert _multipart_etag_at(str(_write(tmp_path, _TEN)), chunk_size=chunk) == expected

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
