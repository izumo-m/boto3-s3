"""Unit tests for ``boto3_s3.checksumcompare``: the native-checksum PairFilter.

Pins ``ChecksumComparison``'s construction (the ``check_size`` /
``pure_max_size`` knobs), the GetObjectAttributes read + parse
(FULL_OBJECT whole-file and COMPOSITE part-boundary reconstruction, pagination),
the s3->s3 stored-checksum comparison, the size pre-check, the indeterminate ->
copy rules (no checksum / unknown algo / ClientError), and the local checksum
backends (zlib / hashlib / awscrt, and the pure-Python slicing-by-8 fallback with
its ``pure_max_size`` gate).

Canned attribute shapes are deliberately miniature: part sizes far below S3's
5 MiB floor, digest strings that are not real base64. The code under test does
arithmetic and string comparison over the returned values - the live service's
size and format rules do not change its paths - and miniature shapes keep the
fixtures readable and the suite fast. (Tests that feed a part listing outside
S3's API - a truncated one without its marker - say so in their own
comments.)

CRC goldens come from ``awscrt`` (the same C implementation S3 uses); tests that
need them skip when it is absent. The pure-Python CRC is additionally pinned to
its awscrt-independent canonical check values, so its correctness is verified
even without awscrt installed.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import io
import types
import zlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, BinaryIO, ClassVar, Literal, cast

import pytest
from typing_extensions import override

import boto3_s3.checksumcompare as cf
from boto3_s3.checksumcompare import (
    ChecksumComparison,
    _can_compute,  # pyright: ignore[reportPrivateUsage]
    _composite_b64,  # pyright: ignore[reportPrivateUsage]
    _pure_crc32c,  # pyright: ignore[reportPrivateUsage]
    _pure_crc64nvme,  # pyright: ignore[reportPrivateUsage]
    _whole_b64,  # pyright: ignore[reportPrivateUsage]
)
from boto3_s3.comparator import SyncPair
from boto3_s3.etagcompare import EtagComparison
from boto3_s3.exceptions import Boto3S3Error, ConfigurationError, NotFoundError, TransportError
from boto3_s3.localstorage import LocalStorage
from boto3_s3.storage import Storage
from boto3_s3.types import FileInfo, S3FileInfo, TransferType
from tests.utils.pairbuilders import local_info, make_pair, native_key, write_file

try:
    from awscrt import checksums as _crt
except ImportError:  # pragma: no cover - exercised only on a no-awscrt host
    _crt = None
if _crt is not None and not hasattr(_crt, "crc64nvme"):
    # An old awscrt (the SDK floor's own [crt] pin, 0.16.x) predates crc64nvme;
    # the library falls back to its pure CRC there (checksumcompare._crc_backend
    # getattr-guards), but these goldens need the real thing.
    _crt = None

_needs_crt = pytest.mark.skipif(_crt is None, reason="awscrt with crc64nvme not installed")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# A fixed 1 KiB payload and its goldens (independent of the module under test).
_DATA = b"0123456789abcdef" * 64
_GOLDEN: dict[str, str] = {
    "crc32": _b64(zlib.crc32(_DATA).to_bytes(4, "big")),
    "md5": _b64(hashlib.md5(_DATA).digest()),
    "sha1": _b64(hashlib.sha1(_DATA).digest()),
    "sha256": _b64(hashlib.sha256(_DATA).digest()),
    "sha512": _b64(hashlib.sha512(_DATA).digest()),
}
_KEY_OF = {
    "crc32": "ChecksumCRC32",
    "crc32c": "ChecksumCRC32C",
    "crc64nvme": "ChecksumCRC64NVME",
    "sha1": "ChecksumSHA1",
    "sha256": "ChecksumSHA256",
    "sha512": "ChecksumSHA512",
    "md5": "ChecksumMD5",
    "xxhash64": "ChecksumXXHASH64",
    "xxhash3": "ChecksumXXHASH3",
    "xxhash128": "ChecksumXXHASH128",
}
if _crt is not None:
    _GOLDEN["crc32c"] = _b64(_crt.crc32c(_DATA).to_bytes(4, "big"))
    _GOLDEN["crc64nvme"] = _b64(_crt.crc64nvme(_DATA).to_bytes(8, "big"))

# awscrt's one-shot XXHASH functions (its XXHash class, 0.31.2 and later); the
# XXHASH goldens and the tests that need them skip without it.
_XXHASH = getattr(_crt, "XXHash", None)
_XXHASH_ONE_SHOT: dict[str, Callable[[bytes], bytes]] = (
    {}
    if _XXHASH is None
    else {
        "xxhash64": _XXHASH.compute_xxhash64,
        "xxhash3": _XXHASH.compute_xxhash3_64,
        "xxhash128": _XXHASH.compute_xxhash3_128,
    }
)
for _name, _fn in _XXHASH_ONE_SHOT.items():
    _GOLDEN[_name] = _b64(_fn(_DATA))
_needs_xxhash = pytest.mark.skipif(_XXHASH is None, reason="awscrt with XXHash not installed")


# -- fakes ---------------------------------------------------------------------


class _FakeClient:
    """Stands in for a boto3 S3 client; serves canned GetObjectAttributes."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get_object_attributes(self, **kw: Any) -> Any:
        self.calls.append(kw)
        resp = self.responses[kw["Key"]]
        if callable(resp):
            resp = resp(kw)
        if isinstance(resp, Exception):
            raise resp
        return resp


class _FakeStorage:
    def __init__(self, bucket: str, client: _FakeClient) -> None:
        self._bucket = bucket
        self._client = client

    @property
    def bucket(self) -> str:
        return self._bucket

    def get_client(self) -> _FakeClient:
        return self._client


def _full(algorithm: str, value: str) -> dict[str, Any]:
    return {"Checksum": {_KEY_OF[algorithm]: value, "ChecksumType": "FULL_OBJECT"}}


def _composite_resp(
    algorithm: str, value: str, sizes: list[int], *, truncated_at: int | None = None
) -> Any:
    """A COMPOSITE response; with ``truncated_at`` it paginates ObjectParts."""
    parts = [{"PartNumber": i + 1, "Size": s} for i, s in enumerate(sizes)]
    head = {"Checksum": {_KEY_OF[algorithm]: value, "ChecksumType": "COMPOSITE"}}
    if truncated_at is None:
        return {**head, "ObjectParts": {"Parts": parts, "IsTruncated": False}}

    def respond(kw: dict[str, Any]) -> dict[str, Any]:
        marker = kw.get("PartNumberMarker")
        if not marker:
            return {
                **head,
                "ObjectParts": {
                    "Parts": parts[:truncated_at],
                    "IsTruncated": True,
                    "NextPartNumberMarker": truncated_at,
                },
            }
        return {"ObjectParts": {"Parts": parts[truncated_at:], "IsTruncated": False}}

    return respond


def _deciding_on(
    compare: ChecksumComparison,
    *,
    src: _FakeClient | None = None,
    dest: _FakeClient | None = None,
) -> Callable[[SyncPair], bool]:
    """``compare`` over pairs whose S3 entries reach these clients.

    The strategy takes each S3 side's client from the entry's ``storage``,
    which a listing stamps; the entries here are built bare, so it is set here.
    """

    def decide(pair: SyncPair) -> bool:
        for info, client in ((pair.src, src), (pair.dest, dest)):
            if client is not None:
                info.storage = cast("Any", _FakeStorage("b", client))
        return compare(pair)

    return decide


def _upload_filter(client: _FakeClient, **kw: Any) -> Callable[[SyncPair], bool]:
    return _deciding_on(ChecksumComparison(**kw), dest=client)


def _download_filter(client: _FakeClient, **kw: Any) -> Callable[[SyncPair], bool]:
    return _deciding_on(ChecksumComparison(**kw), src=client)


def _write(tmp_path: Path, data: bytes = _DATA, name: str = "f") -> Path:
    return write_file(tmp_path, data, name)


def _s3(key: str = "obj", *, size: int | None = None) -> S3FileInfo:
    return S3FileInfo(key=key, size=size)


def _pair(transfer_type: TransferType, *, src: FileInfo, dest: FileInfo) -> SyncPair:
    return make_pair(transfer_type, src=src, dest=dest, compare_key="obj")


# -- construction --------------------------------------------------------------


class TestConstruction:
    def test_defaults(self) -> None:
        f = ChecksumComparison()
        assert f.check_size is True
        assert f.pure_max_size is None

    def test_knobs(self) -> None:
        f = ChecksumComparison(check_size=False, pure_max_size=1024)
        assert f.check_size is False
        assert f.pure_max_size == 1024


# -- size pre-check ------------------------------------------------------------


class TestSizePreCheck:
    def test_size_mismatch_copies_without_a_call(self, tmp_path: Path) -> None:
        client = _FakeClient({})  # never consulted
        f = _upload_filter(client)
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(tmp_path / "nope"), size=10),
            dest=_s3(size=20),
        )
        assert f(pair) is True
        assert client.calls == []

    def test_check_size_off_does_not_short_circuit(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _GOLDEN["sha256"])})
        f = _upload_filter(client, check_size=False)
        # Sizes differ but check_size is off -> it reads + hashes -> matches -> skip.
        pair = _pair(TransferType.UPLOAD, src=local_info(native_key(p), size=1), dest=_s3(size=999))
        assert f(pair) is False
        assert client.calls  # the GOA happened


# -- whole-object (FULL_OBJECT) upload / download ------------------------------


class TestWholeObject:
    def test_upload_match_skips(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _GOLDEN["sha256"])})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client)(pair) is False

    def test_upload_mismatch_copies(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _b64(b"\x00" * 32))})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client)(pair) is True

    def test_download_swaps_local_and_remote(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _GOLDEN["sha256"])})
        pair = _pair(
            TransferType.DOWNLOAD,
            src=_s3(size=len(_DATA)),
            dest=local_info(native_key(p), size=len(_DATA)),
        )
        assert _download_filter(client)(pair) is False

    def test_no_checksum_copies(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": {"Checksum": {}}})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client)(pair) is True

    @pytest.mark.parametrize("algorithm", list(_KEY_OF))
    def test_each_algorithm_matches(self, tmp_path: Path, algorithm: str) -> None:
        if algorithm not in _GOLDEN:
            pytest.skip("awscrt (with this algorithm) not installed")
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full(algorithm, _GOLDEN[algorithm])})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client)(pair) is False


# -- composite (multipart) reconstruction --------------------------------------


def _composite_golden(data: bytes, sizes: list[int], algorithm: str) -> str:
    fn = {"crc32c": _crt.crc32c, "crc64nvme": _crt.crc64nvme}[algorithm]  # pyright: ignore[reportOptionalMemberAccess]
    width = {"crc32c": 4, "crc64nvme": 8}[algorithm]
    combined = bytearray()
    off = 0
    for s in sizes:
        combined += fn(data[off : off + s]).to_bytes(width, "big")
        off += s
    return _b64(fn(bytes(combined)).to_bytes(width, "big")) + f"-{len(sizes)}"


_PARTS = [400, 400, 224]  # sums to 1024 = len(_DATA)


@_needs_crt
class TestComposite:
    def test_match_skips(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        value = _composite_golden(_DATA, _PARTS, "crc32c")
        client = _FakeClient({"obj": _composite_resp("crc32c", value, _PARTS)})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client)(pair) is False

    def test_paginated_parts(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        value = _composite_golden(_DATA, _PARTS, "crc32c")
        client = _FakeClient({"obj": _composite_resp("crc32c", value, _PARTS, truncated_at=2)})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client)(pair) is False
        assert len(client.calls) == 2  # the second page was fetched

    def test_local_tail_beyond_the_parts_sum_differs(self, tmp_path: Path) -> None:
        # A local file that starts as the multipart object and was appended to
        # afterwards: the per-part digests never see the tail, so the composite
        # values would collide - the comparison must still say "differs", even
        # with the size gate opted out (check_size=False).
        p = _write(tmp_path, _DATA + b"appended tail")
        value = _composite_golden(_DATA, _PARTS, "crc32c")
        client = _FakeClient({"obj": _composite_resp("crc32c", value, _PARTS)})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA) + 13),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client, check_size=False)(pair) is True

    def test_local_truncation_differs(self, tmp_path: Path) -> None:
        # The short direction: the final part hashes short -> a different
        # composite -> differs (pinned so both directions stay covered).
        p = _write(tmp_path, _DATA[:-8])
        value = _composite_golden(_DATA, _PARTS, "crc32c")
        client = _FakeClient({"obj": _composite_resp("crc32c", value, _PARTS)})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA) - 8),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client, check_size=False)(pair) is True


def _sha256_composite(data: bytes, sizes: list[int]) -> str:
    """The bare COMPOSITE sha256 digest (no part count), computed independently."""
    combined = b""
    off = 0
    for s in sizes:
        combined += hashlib.sha256(data[off : off + s]).digest()
        off += s
    return _b64(hashlib.sha256(combined).digest())


class TestCompositeForms:
    """The two spellings of a COMPOSITE value: ``<digest>-<n>`` and the bare digest.

    ``HeadObject`` appends the part count; ``GetObjectAttributes`` on a real S3
    general purpose bucket (and on MinIO) returns the digest bare and says
    COMPOSITE in ``ChecksumType`` alone (measured). Read as a FULL_OBJECT
    value, the bare form never matches the whole-file hash, so an unchanged
    multipart object would be copied on every run.
    """

    def _upload(self, tmp_path: Path, resp: Any, data: bytes = _DATA) -> bool:
        p = _write(tmp_path, data)
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(data)),
            dest=_s3(size=len(data)),
        )
        return _upload_filter(_FakeClient({"obj": resp}), check_size=False)(pair)

    def test_a_bare_value_marked_composite_matches(self, tmp_path: Path) -> None:
        resp = _composite_resp("sha256", _sha256_composite(_DATA, _PARTS), _PARTS)
        assert self._upload(tmp_path, resp) is False

    def test_a_bare_composite_value_still_tells_changed_content(self, tmp_path: Path) -> None:
        resp = _composite_resp("sha256", _sha256_composite(_DATA, _PARTS), _PARTS)
        changed = b"X" + _DATA[1:]
        assert self._upload(tmp_path, resp, changed) is True

    def test_a_part_count_alone_marks_a_composite(self, tmp_path: Path) -> None:
        # An endpoint that predates ChecksumType: the suffix is the only sign.
        value = f"{_sha256_composite(_DATA, _PARTS)}-{len(_PARTS)}"
        resp = _composite_resp("sha256", value, _PARTS)
        del resp["Checksum"]["ChecksumType"]
        assert self._upload(tmp_path, resp) is False


# -- SHA512 / MD5 / the XXHASH family -------------------------------------------


def _one_shot(algorithm: str) -> Callable[[bytes], bytes]:
    if algorithm in _XXHASH_ONE_SHOT:
        return _XXHASH_ONE_SHOT[algorithm]
    return lambda data: hashlib.new(algorithm, data).digest()


def _bare_composite(algorithm: str, data: bytes, sizes: list[int]) -> str:
    """The bare COMPOSITE digest - ALGO over the parts' raw ALGO digests."""
    digest = _one_shot(algorithm)
    combined = b""
    off = 0
    for s in sizes:
        combined += digest(data[off : off + s])
        off += s
    return _b64(digest(combined))


class TestNewerAlgorithms:
    """The algorithms S3 stores only as COMPOSITE on a multipart object.

    A multipart object carrying SHA512, MD5 or an XXHASH checksum has it as a
    COMPOSITE one (S3 offers no FULL_OBJECT form for them), so each is pinned
    on that path as well as the whole-file one above.
    """

    @pytest.fixture
    def _no_awscrt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cf, "_awscrt_checksums", lambda: None)

    @pytest.fixture
    def _fresh_hashlib_probe(self) -> Iterator[None]:
        # _hashlib_builds caches per algorithm; a test that makes hashlib
        # refuse one must not leave its verdict behind.
        cf._hashlib_builds.cache_clear()  # pyright: ignore[reportPrivateUsage]
        yield
        cf._hashlib_builds.cache_clear()  # pyright: ignore[reportPrivateUsage]

    def _upload(self, tmp_path: Path, resp: Any, data: bytes = _DATA) -> bool:
        p = _write(tmp_path, data)
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(data)),
            dest=_s3(size=len(data)),
        )
        return _upload_filter(_FakeClient({"obj": resp}), check_size=False)(pair)

    @pytest.mark.parametrize("algorithm", ["sha512", "md5", "xxhash64", "xxhash3", "xxhash128"])
    def test_composite_matches_and_tells_changed_content(
        self, tmp_path: Path, algorithm: str
    ) -> None:
        if algorithm.startswith("xxhash") and _XXHASH is None:
            pytest.skip("awscrt with XXHash not installed")
        resp = _composite_resp(algorithm, _bare_composite(algorithm, _DATA, _PARTS), _PARTS)
        assert self._upload(tmp_path, resp) is False
        assert self._upload(tmp_path, resp, b"X" + _DATA[1:]) is True

    @pytest.mark.parametrize("algorithm", ["xxhash64", "xxhash3", "xxhash128"])
    def test_xxhash_without_awscrt_copies_without_reading(
        self, tmp_path: Path, algorithm: str, _no_awscrt: None
    ) -> None:
        # No pure-Python fallback: indeterminate -> copy. The local path does
        # not exist, proving no read happened.
        client = _FakeClient({"obj": _full(algorithm, "irrelevant")})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(tmp_path / "nope"), size=10),
            dest=_s3(size=10),
        )
        assert _upload_filter(client)(pair) is True
        assert _can_compute(algorithm, 10, pure_max_size=None) is False

    @pytest.mark.parametrize("algorithm", ["xxhash64", "xxhash3", "xxhash128"])
    def test_xxhash_on_an_awscrt_without_xxhash_copies(
        self, monkeypatch: pytest.MonkeyPatch, algorithm: str
    ) -> None:
        # awscrt 0.23 - 0.31.1: a checksums module with the CRCs, no XXHash.
        old = types.SimpleNamespace(crc32c=lambda *_: 0, crc64nvme=lambda *_: 0)
        monkeypatch.setattr(cf, "_awscrt_checksums", lambda: old)
        assert _can_compute(algorithm, 10, pure_max_size=None) is False

    def test_md5_refused_by_hashlib_copies_without_reading(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _fresh_hashlib_probe: None
    ) -> None:
        # A FIPS build that refuses md5 even for a non-security use.
        def refuse(algorithm: str) -> Any:
            raise ValueError(f"unsupported hash type {algorithm}")

        monkeypatch.setattr(cf, "_hashlib_new", refuse)
        client = _FakeClient({"obj": _full("md5", _GOLDEN["md5"])})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(tmp_path / "nope"), size=10),
            dest=_s3(size=10),
        )
        assert _upload_filter(client)(pair) is True

    @_needs_xxhash
    @pytest.mark.parametrize(
        ("algorithm", "empty_digest"),
        [
            # The XXH64 / XXH3-64 / XXH3-128 values of empty input, seed 0, in
            # the canonical (big-endian) form xxHash's own spec gives them.
            ("xxhash64", "ef46db3751d8e999"),
            ("xxhash3", "2d06800538d394c2"),
            ("xxhash128", "99aa06d3014798d86001c324468d497f"),
        ],
    )
    def test_xxhash_digest_is_the_canonical_form(self, algorithm: str, empty_digest: str) -> None:
        assert _whole_b64(io.BytesIO(b""), algorithm) == _b64(bytes.fromhex(empty_digest))


# -- s3 -> s3 (COPY) -----------------------------------------------------------


class TestCopyDirect:
    def _copy(self, src_resp: Any, dest_resp: Any, **kw: Any) -> Callable[[SyncPair], bool]:
        return _deciding_on(
            ChecksumComparison(**kw),
            src=_FakeClient({"src": src_resp}),
            dest=_FakeClient({"dest": dest_resp}),
        )

    def _pair(self, **kw: Any) -> SyncPair:
        return SyncPair(
            compare_key="k",
            transfer_type=TransferType.COPY,
            src=_s3("src", **kw),
            dest=_s3("dest", **kw),
        )

    def test_equal_checksums_skip(self) -> None:
        f = self._copy(_full("sha256", "ABC"), _full("sha256", "ABC"))
        assert f(self._pair()) is False

    def test_differing_checksums_copy(self) -> None:
        f = self._copy(_full("sha256", "ABC"), _full("sha256", "XYZ"))
        assert f(self._pair()) is True

    def test_missing_either_side_copies(self) -> None:
        assert self._copy({"Checksum": {}}, _full("sha256", "ABC"))(self._pair()) is True
        assert self._copy(_full("sha256", "ABC"), {"Checksum": {}})(self._pair()) is True

    def test_different_algorithm_copies(self) -> None:
        # Equal-looking value but different algorithm -> not comparable -> copy.
        f = self._copy(_full("crc32", "ABC"), _full("sha256", "ABC"))
        assert f(self._pair()) is True

    def test_size_guard(self) -> None:
        # Equal checksum, different size: check_size forces a copy (CRC can collide).
        f = self._copy(_full("crc32", "ABC"), _full("crc32", "ABC"))
        assert f(self._pair(size=10)) is False  # same size -> trust equality
        f2 = self._copy(_full("crc32", "ABC"), _full("crc32", "ABC"))
        pair = SyncPair(
            compare_key="k",
            transfer_type=TransferType.COPY,
            src=_s3("src", size=10),
            dest=_s3("dest", size=20),
        )
        assert f2(pair) is True

    def test_composite_values_skip_pagination(self) -> None:
        # COPY compares the stored value strings directly, so a COMPOSITE object
        # (whose parts would otherwise paginate) is read with one call per side.
        src = _FakeClient({"src": _composite_resp("sha256", "ZZZ-3", [10, 10, 10], truncated_at=2)})
        dest = _FakeClient(
            {"dest": _composite_resp("sha256", "ZZZ-3", [10, 10, 10], truncated_at=2)}
        )
        f = _deciding_on(ChecksumComparison(), src=src, dest=dest)
        pair = SyncPair(
            compare_key="k", transfer_type=TransferType.COPY, src=_s3("src"), dest=_s3("dest")
        )
        assert f(pair) is False
        assert len(src.calls) == 1
        assert len(dest.calls) == 1

    def test_a_part_count_on_one_side_only_still_compares(self) -> None:
        # The same COMPOSITE digest, spelled with the count by one endpoint and
        # bare by the other: the same object.
        f = self._copy(
            _composite_resp("sha256", "ZZZ-3", [10, 10, 10]),
            _composite_resp("sha256", "ZZZ", [10, 10, 10]),
        )
        assert f(self._pair()) is False

    def test_differing_part_counts_copy(self) -> None:
        f = self._copy(
            _composite_resp("sha256", "ZZZ-3", [10, 10, 10]),
            _composite_resp("sha256", "ZZZ-4", [10, 10, 5, 5]),
        )
        assert f(self._pair()) is True

    def test_a_composite_digest_against_a_full_object_one_copies(self) -> None:
        # Equal text, different kinds: a digest of part digests says nothing
        # about a digest of the whole object.
        f = self._copy(_composite_resp("sha256", "ZZZ", [10, 10, 10]), _full("sha256", "ZZZ"))
        assert f(self._pair()) is True


# -- indeterminate -> copy on errors -------------------------------------------


class TestClientErrorIndeterminate:
    def test_client_error_copies(self, tmp_path: Path) -> None:
        from botocore.exceptions import ClientError

        err = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObjectAttributes")
        p = _write(tmp_path)
        client = _FakeClient({"obj": err})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client)(pair) is True

    def test_pagination_error_copies(self, tmp_path: Path) -> None:
        # A ClientError raised mid part-size pagination is swallowed -> copy; it
        # must not propagate and abort the sync.
        from botocore.exceptions import ClientError

        err = ClientError({"Error": {"Code": "InternalError"}}, "GetObjectAttributes")

        def respond(kw: dict[str, Any]) -> Any:
            if kw.get("PartNumberMarker"):
                raise err
            return {
                "Checksum": {"ChecksumSHA256": "ABC-2", "ChecksumType": "COMPOSITE"},
                "ObjectParts": {
                    "Parts": [{"PartNumber": 1, "Size": 500}],
                    "IsTruncated": True,
                    "NextPartNumberMarker": 1,
                },
            }

        p = _write(tmp_path)
        client = _FakeClient({"obj": respond})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client)(pair) is True
        assert len(client.calls) == 2  # first page + the failing second page

    def test_truncated_without_marker_copies(self, tmp_path: Path) -> None:
        # IsTruncated True but no NextPartNumberMarker (a malformed/partial parts
        # listing) -> indeterminate -> copy, never a partial reconstruction.
        resp = {
            "Checksum": {"ChecksumSHA256": "ABC-2", "ChecksumType": "COMPOSITE"},
            "ObjectParts": {"Parts": [{"PartNumber": 1, "Size": 500}], "IsTruncated": True},
        }
        p = _write(tmp_path)
        client = _FakeClient({"obj": resp})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client)(pair) is True


class TestPartListingIndeterminate:
    """A part listing that would not end reads as indeterminate (copy).

    A marker that does not move past the page just read, or that runs past
    S3's last part number, is not followed: a server answering that way could
    otherwise page forever.
    """

    def _upload(self, tmp_path: Path, client: _FakeClient) -> bool:
        p = _write(tmp_path)
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        return _upload_filter(client)(pair)

    def test_a_marker_that_does_not_advance_copies(self, tmp_path: Path) -> None:
        # An endpoint that ignores PartNumberMarker answers every page alike;
        # following its marker would ask for the same page forever.
        page = {
            "Checksum": {"ChecksumSHA256": "ABC-2", "ChecksumType": "COMPOSITE"},
            "ObjectParts": {
                "Parts": [{"PartNumber": 1, "Size": 500}],
                "IsTruncated": True,
                "NextPartNumberMarker": 1,
            },
        }
        client = _FakeClient({"obj": page})
        assert self._upload(tmp_path, client) is True
        assert len(client.calls) == 2  # the first page, and the one that repeated its marker

    @pytest.mark.parametrize("marker", [0, -1, 10_000])
    def test_a_marker_that_cannot_continue_the_listing_copies(
        self, tmp_path: Path, marker: object
    ) -> None:
        page = {
            "Checksum": {"ChecksumSHA256": "ABC-2", "ChecksumType": "COMPOSITE"},
            "ObjectParts": {
                "Parts": [{"PartNumber": 1, "Size": 500}],
                "IsTruncated": True,
                "NextPartNumberMarker": marker,
            },
        }
        client = _FakeClient({"obj": page})
        assert self._upload(tmp_path, client) is True
        assert len(client.calls) == 1


class TestBotoCoreErrorAborts:
    """A ``BotoCoreError`` from the remote read is not per-object indeterminate.

    Swallowing a credential or transport failure like a ``ClientError`` would
    silently copy everything: ``_fetch_remote`` raises instead, translated to
    the library taxonomy with the original at ``__cause__``.
    """

    def _pair_at(self, tmp_path: Path) -> SyncPair:
        p = _write(tmp_path)
        return _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )

    def test_endpoint_connection_error_raises_transport_error(self, tmp_path: Path) -> None:
        from botocore.exceptions import EndpointConnectionError

        err = EndpointConnectionError(endpoint_url="https://x")
        client = _FakeClient({"obj": err})
        with pytest.raises(TransportError) as excinfo:
            _upload_filter(client)(self._pair_at(tmp_path))
        assert type(excinfo.value) is TransportError
        assert excinfo.value.__cause__ is err
        assert (excinfo.value.operation, excinfo.value.bucket, excinfo.value.key) == (
            "sync",
            "b",
            "obj",
        )

    def test_no_credentials_error_raises_configuration_error(self, tmp_path: Path) -> None:
        from botocore.exceptions import NoCredentialsError

        err = NoCredentialsError()
        client = _FakeClient({"obj": err})
        with pytest.raises(ConfigurationError) as excinfo:
            _upload_filter(client)(self._pair_at(tmp_path))
        assert type(excinfo.value) is ConfigurationError
        assert excinfo.value.__cause__ is err

    def test_a_failure_from_outside_the_boto_family_is_the_requests_too(
        self, tmp_path: Path
    ) -> None:
        # A refreshing credential_process whose program is gone raises a bare
        # FileNotFoundError inside the request. Every other request point
        # reports that as the base error; this one let it out of S3.sync,
        # past `except Boto3S3Error`.
        err = FileNotFoundError(errno.ENOENT, "No such file or directory", "/bin/helper")
        client = _FakeClient({"obj": err})
        with pytest.raises(Boto3S3Error) as excinfo:
            _upload_filter(client)(self._pair_at(tmp_path))
        assert type(excinfo.value) is Boto3S3Error
        assert excinfo.value.__cause__ is err
        assert (excinfo.value.operation, excinfo.value.bucket, excinfo.value.key) == (
            "sync",
            "b",
            "obj",
        )

    def test_the_part_listing_request_is_covered_too(self, tmp_path: Path) -> None:
        # The second GetObjectAttributes - the next page of a COMPOSITE
        # object's parts - goes through the same wrapper; reverting that one
        # call left every test green.
        err = FileNotFoundError(errno.ENOENT, "No such file or directory", "/bin/helper")

        def respond(kw: dict[str, Any]) -> Any:
            if kw.get("PartNumberMarker"):
                raise err
            return {
                "Checksum": {"ChecksumSHA256": "ABC-2", "ChecksumType": "COMPOSITE"},
                "ObjectParts": {
                    "Parts": [{"PartNumber": 1, "Size": 500}],
                    "IsTruncated": True,
                    "NextPartNumberMarker": 1,
                },
            }

        client = _FakeClient({"obj": respond})
        with pytest.raises(Boto3S3Error) as excinfo:
            _upload_filter(client)(self._pair_at(tmp_path))
        assert type(excinfo.value) is Boto3S3Error
        assert excinfo.value.__cause__ is err
        assert len(client.calls) == 2

    def test_a_family_error_from_inside_the_call_passes_as_it_is(self, tmp_path: Path) -> None:
        # Raised by a handler registered on the client, say. It was handed to
        # the non-boto wrapping, came back as itself and was raised `from`
        # itself: an exception that is its own __cause__.
        err = TransportError("connection reset by a handler")
        client = _FakeClient({"obj": err})
        with pytest.raises(TransportError) as excinfo:
            _upload_filter(client)(self._pair_at(tmp_path))
        assert excinfo.value is err
        assert err.__cause__ is None


class _RaisingStorage(Storage):
    """A custom backend whose ``open`` raises what it was given."""

    scheme: ClassVar[str] = "stub"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    @override
    def as_text(self) -> str:
        return "stub://"

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        raise self._exc


class _MidReadFailingLocal(LocalStorage):
    """A ``LocalStorage`` whose stream fails mid-read with a raw ``OSError``."""

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        class _Failing(io.BytesIO):
            def read(self, size: int | None = -1, /) -> bytes:
                raise FileNotFoundError(errno.ENOENT, "gone")

        return _Failing()


class TestReadableSideFailure:
    """A failure reading the readable side leaves either strategy as a library
    error (`ContentComparison._read_readable`): the caller's backend's as the
    base error with its own on ``__cause__`` (a family error as it is), the
    local file's in the local taxonomy."""

    @staticmethod
    def _strategies() -> list[Any]:
        client = _FakeClient({"obj": _full("sha256", _GOLDEN["sha256"])})
        return [EtagComparison(), _upload_filter(client)]

    @staticmethod
    def _pair(storage: Storage) -> SyncPair:
        src = FileInfo(key="k", compare_key="obj", size=10, storage=storage)
        dest = S3FileInfo(key="obj", etag='"abc"', size=10)
        return SyncPair(compare_key="obj", transfer_type=TransferType.UPLOAD, src=src, dest=dest)

    @pytest.mark.parametrize("strategy", [0, 1], ids=["etag", "checksum"])
    @pytest.mark.parametrize(
        "raised",
        [OSError("broken"), KeyError("k"), NotFoundError("gone")],
        ids=["oserror", "keyerror", "family"],
    )
    def test_a_custom_backend_failure(self, strategy: int, raised: Exception) -> None:
        compare = self._strategies()[strategy]
        with pytest.raises(Boto3S3Error) as excinfo:
            compare(self._pair(_RaisingStorage(raised)))
        if isinstance(raised, Boto3S3Error):
            assert excinfo.value is raised
            return
        assert type(excinfo.value) is Boto3S3Error
        assert excinfo.value.__cause__ is raised
        assert (excinfo.value.operation, excinfo.value.key) == ("sync", "obj")

    @pytest.mark.parametrize("strategy", [0, 1], ids=["etag", "checksum"])
    def test_a_local_read_failure_is_translated(self, strategy: int, tmp_path: Path) -> None:
        compare = self._strategies()[strategy]
        with pytest.raises(NotFoundError) as excinfo:
            compare(self._pair(_MidReadFailingLocal(str(tmp_path))))
        assert isinstance(excinfo.value.__cause__, FileNotFoundError)
        assert (excinfo.value.operation, excinfo.value.key) == ("sync", "obj")


# -- request payer -------------------------------------------------------------


class TestRequestPayer:
    def test_threaded_into_the_call(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _GOLDEN["sha256"])})
        f = _upload_filter(client, request_payer="requester")
        f(
            _pair(
                TransferType.UPLOAD,
                src=local_info(native_key(p), size=len(_DATA)),
                dest=_s3(size=len(_DATA)),
            )
        )
        assert client.calls[0]["RequestPayer"] == "requester"

    def test_omitted_by_default(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _GOLDEN["sha256"])})
        _upload_filter(client)(
            _pair(
                TransferType.UPLOAD,
                src=local_info(native_key(p), size=len(_DATA)),
                dest=_s3(size=len(_DATA)),
            )
        )
        assert "RequestPayer" not in client.calls[0]


# -- pure-Python fallback + the pure_max_size gate -----------------------------


class TestPureFallback:
    @pytest.fixture
    def _no_awscrt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cf, "_awscrt_checksums", lambda: None)

    @_needs_crt
    @pytest.mark.parametrize("algorithm", ["crc32c", "crc64nvme"])
    def test_pure_path_matches(self, tmp_path: Path, algorithm: str, _no_awscrt: None) -> None:
        # With awscrt forced absent, the bundled slicing-by-8 path reproduces the
        # awscrt golden -> a matching object still skips.
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full(algorithm, _GOLDEN[algorithm])})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client)(pair) is False

    def test_gate_copies_oversize_without_hashing(self, tmp_path: Path, _no_awscrt: None) -> None:
        # pure_max_size below the file -> the slow hash is skipped (indeterminate
        # -> copy). The local path does not exist, proving no read happened.
        client = _FakeClient({"obj": _full("crc64nvme", "irrelevant")})
        f = _upload_filter(client, pure_max_size=0)
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(tmp_path / "nope"), size=10),
            dest=_s3(size=10),
        )
        assert f(pair) is True

    def test_gate_does_not_apply_with_awscrt(self) -> None:
        # awscrt present -> any size is computable; the gate is moot.
        if _crt is None:
            pytest.skip("awscrt not installed")
        assert _can_compute("crc64nvme", 10**12, pure_max_size=1) is True


# -- local checksum helpers ----------------------------------------------------


class TestHelperUnits:
    def test_whole_b64_crc32_and_sha256(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        with open(p, "rb") as fh:
            assert _whole_b64(fh, "crc32") == _GOLDEN["crc32"]
        with open(p, "rb") as fh:
            assert _whole_b64(fh, "sha256") == _GOLDEN["sha256"]

    @_needs_crt
    def test_composite_b64_matches_independent(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        sizes = (500, 524)
        with open(p, "rb") as fh:
            got = _composite_b64(fh, "crc32c", sizes)
        # The digest alone: the "-N" a response may append is the caller's.
        assert got == _composite_golden(_DATA, list(sizes), "crc32c").rsplit("-", 1)[0]

    def test_can_compute_always_for_stdlib(self) -> None:
        for algo in ("crc32", "md5", "sha1", "sha256", "sha512"):
            assert _can_compute(algo, 10**12, pure_max_size=1) is True

    @_needs_xxhash
    def test_can_compute_xxhash_with_awscrt_whatever_the_cap(self) -> None:
        for algo in ("xxhash64", "xxhash3", "xxhash128"):
            assert _can_compute(algo, 10**12, pure_max_size=1) is True

    def test_can_compute_unknown_algorithm(self) -> None:
        assert _can_compute("blake3", None, None) is False

    def test_can_compute_unknown_size_counts_as_above_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The cap is a hard bound on the slow pure-Python fallback: a size it
        # cannot check (a custom backend's listing without sizes) reads as
        # above it - indeterminate -> copy - never an unbounded hash. Without
        # a cap, unknown sizes still compute.
        monkeypatch.setattr("boto3_s3.checksumcompare._crc_has_awscrt", lambda _algo: False)
        assert _can_compute("crc64nvme", None, pure_max_size=1024) is False
        assert _can_compute("crc64nvme", None, pure_max_size=None) is True


class TestPureCrcCanonical:
    """awscrt-independent: the CRC check value for the standard string."""

    def test_crc32c_check_value(self) -> None:
        assert _pure_crc32c(b"123456789") == 0xE3069283

    def test_crc64nvme_check_value(self) -> None:
        assert _pure_crc64nvme(b"123456789") == 0xAE8B14860A799888

    def test_empty_is_identity_for_chaining(self) -> None:
        # An empty update leaves the running value unchanged (streaming invariant).
        assert _pure_crc32c(b"", 0x1234) == 0x1234
        assert _pure_crc64nvme(b"", 0xDEAD) == 0xDEAD


@_needs_crt
class TestPureCrcCrossCheck:
    """The pure path equals awscrt across sizes, including incremental chaining."""

    @pytest.mark.parametrize("n", [0, 1, 7, 8, 9, 100, 1000, 4096])
    def test_pure_equals_awscrt(self, n: int) -> None:
        data = bytes((i * 37 + 11) & 0xFF for i in range(n))
        assert _pure_crc32c(data) == _crt.crc32c(data)  # pyright: ignore[reportOptionalMemberAccess]
        assert _pure_crc64nvme(data) == _crt.crc64nvme(data)  # pyright: ignore[reportOptionalMemberAccess]

    def test_chunked_chaining_matches_one_shot(self) -> None:
        data = bytes((i * 91 + 3) & 0xFF for i in range(5000))
        p32 = p64 = 0
        i = 0
        for step in (5, 1, 4096, 17, 5000):  # last step grabs the remainder
            p32 = _pure_crc32c(data[i : i + step], p32)
            p64 = _pure_crc64nvme(data[i : i + step], p64)
            i += step
        assert p32 == _crt.crc32c(data)  # pyright: ignore[reportOptionalMemberAccess]
        assert p64 == _crt.crc64nvme(data)  # pyright: ignore[reportOptionalMemberAccess]
