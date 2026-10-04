"""Unit tests for ``boto3_s3.checksumcompare``: the native-checksum PairFilter.

Pins ``ChecksumComparison``'s construction (the resolved-endpoint injection, the
``check_size`` / ``pure_max_size`` knobs), the GetObjectAttributes read + parse
(FULL_OBJECT whole-file and COMPOSITE part-boundary reconstruction, pagination),
the s3->s3 stored-checksum comparison, the size pre-check, the indeterminate ->
copy rules (no checksum / unknown algo / ClientError), and the local checksum
backends (zlib / hashlib / awscrt, and the pure-Python slicing-by-8 fallback with
its ``pure_max_size`` gate).

Canned attribute shapes are deliberately miniature: part sizes far below S3's
5 MiB floor, digest strings that are not real base64. The code under test does
arithmetic and string comparison over the returned values - the live service's
size and format rules do not change its paths - and miniature shapes keep the
fixtures readable and the suite fast. (Tests that exercise *malformed* inputs
- a wrong part count, a truncated listing without its marker - say so in
their own comments.)

CRC goldens come from ``awscrt`` (the same C implementation S3 uses); tests that
need them skip when it is absent. The pure-Python CRC is additionally pinned to
its awscrt-independent canonical check values, so its correctness is verified
even without awscrt installed.
"""

from __future__ import annotations

import base64
import hashlib
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

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
from boto3_s3.exceptions import Boto3S3Error, ConfigurationError, TransportError
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
    "sha1": _b64(hashlib.sha1(_DATA).digest()),
    "sha256": _b64(hashlib.sha256(_DATA).digest()),
}
_KEY_OF = {
    "crc32": "ChecksumCRC32",
    "crc32c": "ChecksumCRC32C",
    "crc64nvme": "ChecksumCRC64NVME",
    "sha1": "ChecksumSHA1",
    "sha256": "ChecksumSHA256",
}
if _crt is not None:
    _GOLDEN["crc32c"] = _b64(_crt.crc32c(_DATA).to_bytes(4, "big"))
    _GOLDEN["crc64nvme"] = _b64(_crt.crc64nvme(_DATA).to_bytes(8, "big"))


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


class _FakeS3:
    def __init__(self, mapping: dict[str, Any]) -> None:
        self.mapping = mapping

    def resolve(self, loc: str) -> Any:
        return self.mapping[loc]


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


def _upload_filter(client: _FakeClient, *, key: str = "obj", **kw: Any) -> ChecksumComparison:
    s3 = _FakeS3({"local": "LOCAL", f"s3://b/{key}": _FakeStorage("b", client)})
    return ChecksumComparison(s3, "local", f"s3://b/{key}", **kw)  # pyright: ignore[reportArgumentType]


def _download_filter(client: _FakeClient, *, key: str = "obj", **kw: Any) -> ChecksumComparison:
    s3 = _FakeS3({f"s3://b/{key}": _FakeStorage("b", client), "local": "LOCAL"})
    return ChecksumComparison(s3, f"s3://b/{key}", "local", **kw)  # pyright: ignore[reportArgumentType]


def _write(tmp_path: Path, data: bytes = _DATA, name: str = "f") -> Path:
    return write_file(tmp_path, data, name)


def _s3(key: str = "obj", *, size: int | None = None) -> S3FileInfo:
    return S3FileInfo(key=key, size=size)


def _pair(transfer_type: TransferType, *, src: FileInfo, dest: FileInfo) -> SyncPair:
    return make_pair(transfer_type, src=src, dest=dest, compare_key="obj")


# -- construction --------------------------------------------------------------


class TestConstruction:
    def test_defaults(self) -> None:
        f = _upload_filter(_FakeClient({}))
        assert f.check_size is True
        assert f.pure_max_size is None

    def test_knobs(self) -> None:
        f = _upload_filter(_FakeClient({}), check_size=False, pure_max_size=1024)
        assert f.check_size is False
        assert f.pure_max_size == 1024

    def test_resolves_both_sides(self) -> None:
        # Both locations are resolved once at construction (the endpoint injection).
        resolved: list[str] = []

        class _S3:
            def resolve(self, loc: str) -> Any:
                resolved.append(loc)
                return "LOCAL" if loc == "local" else _FakeStorage("b", _FakeClient({}))

        ChecksumComparison(_S3(), "local", "s3://b/obj")  # pyright: ignore[reportArgumentType]
        assert resolved == ["local", "s3://b/obj"]

    def test_s3_side_clients_built_at_construction(self) -> None:
        # The decides may run on a ParallelFilter pool, where a lazy first
        # get_client() would race boto3's non-thread-safe client construction:
        # both S3 sides' clients are built (memoized) on the constructing thread.
        from boto3_s3.s3storage import S3Storage

        calls: list[str] = []

        class _Counting(S3Storage):
            def get_client(self) -> Any:
                calls.append(self.bucket)
                return _FakeClient({})

        class _S3:
            def resolve(self, loc: Any) -> Any:
                return loc

        ChecksumComparison(
            _S3(),  # pyright: ignore[reportArgumentType]
            _Counting("s3://a/p"),
            _Counting("s3://b/q"),
        )
        assert calls == ["a", "b"]


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

    @pytest.mark.parametrize("algorithm", ["crc32", "crc32c", "crc64nvme", "sha1", "sha256"])
    def test_each_algorithm_matches(self, tmp_path: Path, algorithm: str) -> None:
        if algorithm not in _GOLDEN:
            pytest.skip("awscrt not installed")
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
    fn = {"crc32c": _crt.crc32c, "crc64nvme": _crt.crc64nvme}[algorithm]  # type: ignore[union-attr]
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

    def test_wrong_count_copies(self, tmp_path: Path) -> None:
        # Right digits, wrong "-N": the part count participates -> differ.
        p = _write(tmp_path)
        value = _composite_golden(_DATA, _PARTS, "crc32c").rsplit("-", 1)[0] + "-9"
        client = _FakeClient({"obj": _composite_resp("crc32c", value, _PARTS)})
        pair = _pair(
            TransferType.UPLOAD,
            src=local_info(native_key(p), size=len(_DATA)),
            dest=_s3(size=len(_DATA)),
        )
        assert _upload_filter(client)(pair) is True

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

    ``HeadObject`` appends the part count; MinIO's ``GetObjectAttributes``
    returns the digest bare and says COMPOSITE in ``ChecksumType`` alone
    (measured). Read as a FULL_OBJECT value, the bare form never matches the
    whole-file hash, so an unchanged multipart object would be copied on every
    run.
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

    def test_a_part_count_that_disagrees_with_the_listing_copies(self, tmp_path: Path) -> None:
        resp = _composite_resp("sha256", f"{_sha256_composite(_DATA, _PARTS)}-9", _PARTS)
        assert self._upload(tmp_path, resp) is True

    def test_a_part_count_that_is_no_number_copies(self, tmp_path: Path) -> None:
        resp = _composite_resp("sha256", f"{_sha256_composite(_DATA, _PARTS)}-x", _PARTS)
        assert self._upload(tmp_path, resp) is True


# -- s3 -> s3 (COPY) -----------------------------------------------------------


class TestCopyDirect:
    def _copy(self, src_resp: Any, dest_resp: Any, **kw: Any) -> Callable[[SyncPair], bool]:
        s3 = _FakeS3(
            {
                "s3://b/src": _FakeStorage("b", _FakeClient({"src": src_resp})),
                "s3://b/dest": _FakeStorage("b", _FakeClient({"dest": dest_resp})),
            }
        )
        return ChecksumComparison(s3, "s3://b/src", "s3://b/dest", **kw)  # pyright: ignore[reportArgumentType]

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
        s3 = _FakeS3({"s3://b/src": _FakeStorage("b", src), "s3://b/dest": _FakeStorage("b", dest)})
        f = ChecksumComparison(s3, "s3://b/src", "s3://b/dest")  # pyright: ignore[reportArgumentType]
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
    """A part listing that cannot be relied on reads as indeterminate (copy).

    The part split has to be known exactly to reproduce a COMPOSITE checksum;
    a listing short of that is neither followed forever nor allowed to stop
    the run.
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

    @pytest.mark.parametrize("marker", ["x", 0, -1])
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

    def test_a_part_without_a_size_copies(self, tmp_path: Path) -> None:
        page = {
            "Checksum": {"ChecksumSHA256": "ABC-2", "ChecksumType": "COMPOSITE"},
            "ObjectParts": {"Parts": [{"PartNumber": 1}, {"PartNumber": 2, "Size": 524}]},
        }
        assert self._upload(tmp_path, _FakeClient({"obj": page})) is True

    def test_a_later_page_part_without_a_size_copies(self, tmp_path: Path) -> None:
        def respond(kw: dict[str, Any]) -> Any:
            if kw.get("PartNumberMarker"):
                return {"ObjectParts": {"Parts": [{"PartNumber": 2}], "IsTruncated": False}}
            return {
                "Checksum": {"ChecksumSHA256": "ABC-2", "ChecksumType": "COMPOSITE"},
                "ObjectParts": {
                    "Parts": [{"PartNumber": 1, "Size": 500}],
                    "IsTruncated": True,
                    "NextPartNumberMarker": 1,
                },
            }

        client = _FakeClient({"obj": respond})
        assert self._upload(tmp_path, client) is True
        assert len(client.calls) == 2


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
        # botocore's parser raises a bare ValueError on an attribute it
        # cannot convert (a LastModified that is no timestamp). Every other
        # request point reports that as the base error; this one let the
        # ValueError out of S3.sync, past `except Boto3S3Error`.
        err = ValueError('Invalid timestamp "garbage": Unknown string format: garbage')
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
        err = ValueError("invalid literal for int() with base 10: 'abc'")

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
        for algo in ("crc32", "sha1", "sha256"):
            assert _can_compute(algo, 10**12, pure_max_size=1) is True

    def test_can_compute_unknown_algorithm(self) -> None:
        assert _can_compute("xxhash64", None, None) is False

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
        assert _pure_crc32c(data) == _crt.crc32c(data)  # type: ignore[union-attr]
        assert _pure_crc64nvme(data) == _crt.crc64nvme(data)  # type: ignore[union-attr]

    def test_chunked_chaining_matches_one_shot(self) -> None:
        data = bytes((i * 91 + 3) & 0xFF for i in range(5000))
        p32 = p64 = 0
        i = 0
        for step in (5, 1, 4096, 17, 5000):  # last step grabs the remainder
            p32 = _pure_crc32c(data[i : i + step], p32)
            p64 = _pure_crc64nvme(data[i : i + step], p64)
            i += step
        assert p32 == _crt.crc32c(data)  # type: ignore[union-attr]
        assert p64 == _crt.crc64nvme(data)  # type: ignore[union-attr]
