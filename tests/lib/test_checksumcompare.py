"""Unit tests for ``boto3_s3.checksumcompare``: the native-checksum PairFilter.

Pins ``ChecksumComparison``'s construction (the resolved-endpoint injection, the
``check_size`` / ``pure_max_size`` knobs), the GetObjectAttributes read + parse
(FULL_OBJECT whole-file and COMPOSITE part-boundary reconstruction, pagination),
the s3->s3 stored-checksum comparison, the size pre-check, the indeterminate ->
copy rules (no checksum / unknown algo / ClientError), and the local checksum
backends (zlib / hashlib / awscrt, and the pure-Python slicing-by-8 fallback with
its ``pure_max_size`` gate).

CRC goldens come from ``awscrt`` (the same C implementation S3 uses); tests that
need them skip when it is absent. The pure-Python CRC is additionally pinned to
its awscrt-independent canonical check values, so its correctness is verified
even without awscrt installed.
"""

from __future__ import annotations

import base64
import hashlib
import os
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
from boto3_s3.types import FileInfo, LocalFileInfo, S3FileInfo, TransferType

try:
    from awscrt import checksums as _crt
except ImportError:  # pragma: no cover - exercised only on a no-awscrt host
    _crt = None

_needs_crt = pytest.mark.skipif(_crt is None, reason="awscrt not installed")


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
    p = tmp_path / name
    p.write_bytes(data)
    return p


def _key(p: Path) -> str:
    return str(p).replace(os.sep, "/")


def _local(key: str, *, size: int | None = None) -> LocalFileInfo:
    return LocalFileInfo(key=key, size=size)


def _s3(key: str = "obj", *, size: int | None = None) -> S3FileInfo:
    return S3FileInfo(key=key, size=size)


def _pair(
    transfer_type: TransferType, *, src: FileInfo | None = None, dst: FileInfo | None = None
) -> SyncPair:
    return SyncPair(key="obj", transfer_type=transfer_type, src=src, dst=dst)


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


# -- new / missing / unsupported sides ----------------------------------------


class TestNewAndMissingSides:
    @pytest.mark.parametrize("transfer_type", list(TransferType))
    def test_dst_none_always_copies(self, transfer_type: TransferType) -> None:
        f = _upload_filter(_FakeClient({}))
        assert f(_pair(transfer_type, src=_local("k", size=1))) is True

    @pytest.mark.parametrize("transfer_type", list(TransferType))
    def test_src_none_raises(self, transfer_type: TransferType) -> None:
        f = _upload_filter(_FakeClient({}))
        with pytest.raises(ValueError, match="without a source entry"):
            f(_pair(transfer_type, dst=_s3()))

    def test_move_kind_raises(self) -> None:
        f = _upload_filter(_FakeClient({}))
        with pytest.raises(ValueError, match="cannot judge a 'move' pair"):
            f(_pair(TransferType.MOVE, src=_local("k", size=1), dst=_s3(size=1)))

    def test_upload_non_local_side_raises(self) -> None:
        f = _upload_filter(_FakeClient({}))
        with pytest.raises(ValueError, match="no local side"):
            f(_pair(TransferType.UPLOAD, src=_s3(size=1), dst=_s3(size=1)))


# -- size pre-check ------------------------------------------------------------


class TestSizePreCheck:
    def test_size_mismatch_copies_without_a_call(self, tmp_path: Path) -> None:
        client = _FakeClient({})  # never consulted
        f = _upload_filter(client)
        pair = _pair(
            TransferType.UPLOAD, src=_local(_key(tmp_path / "nope"), size=10), dst=_s3(size=20)
        )
        assert f(pair) is True
        assert client.calls == []

    def test_check_size_off_does_not_short_circuit(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _GOLDEN["sha256"])})
        f = _upload_filter(client, check_size=False)
        # Sizes differ but check_size is off -> it reads + hashes -> matches -> skip.
        pair = _pair(TransferType.UPLOAD, src=_local(_key(p), size=1), dst=_s3(size=999))
        assert f(pair) is False
        assert client.calls  # the GOA happened


# -- whole-object (FULL_OBJECT) upload / download ------------------------------


class TestWholeObject:
    def test_upload_match_skips(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _GOLDEN["sha256"])})
        pair = _pair(
            TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
        )
        assert _upload_filter(client)(pair) is False

    def test_upload_mismatch_copies(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _b64(b"\x00" * 32))})
        pair = _pair(
            TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
        )
        assert _upload_filter(client)(pair) is True

    def test_download_swaps_local_and_remote(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _GOLDEN["sha256"])})
        pair = _pair(
            TransferType.DOWNLOAD, src=_s3(size=len(_DATA)), dst=_local(_key(p), size=len(_DATA))
        )
        assert _download_filter(client)(pair) is False

    def test_no_checksum_copies(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": {"Checksum": {}}})
        pair = _pair(
            TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
        )
        assert _upload_filter(client)(pair) is True

    @pytest.mark.parametrize("algorithm", ["crc32", "crc32c", "crc64nvme", "sha1", "sha256"])
    def test_each_algorithm_matches(self, tmp_path: Path, algorithm: str) -> None:
        if algorithm not in _GOLDEN:
            pytest.skip("awscrt not installed")
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full(algorithm, _GOLDEN[algorithm])})
        pair = _pair(
            TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
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
            TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
        )
        assert _upload_filter(client)(pair) is False

    def test_wrong_count_copies(self, tmp_path: Path) -> None:
        # Right digits, wrong "-N": the part count participates -> differ.
        p = _write(tmp_path)
        value = _composite_golden(_DATA, _PARTS, "crc32c").rsplit("-", 1)[0] + "-9"
        client = _FakeClient({"obj": _composite_resp("crc32c", value, _PARTS)})
        pair = _pair(
            TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
        )
        assert _upload_filter(client)(pair) is True

    def test_paginated_parts(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        value = _composite_golden(_DATA, _PARTS, "crc32c")
        client = _FakeClient({"obj": _composite_resp("crc32c", value, _PARTS, truncated_at=2)})
        pair = _pair(
            TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
        )
        assert _upload_filter(client)(pair) is False
        assert len(client.calls) == 2  # the second page was fetched


# -- s3 -> s3 (COPY) -----------------------------------------------------------


class TestCopyDirect:
    def _copy(self, src_resp: Any, dst_resp: Any, **kw: Any) -> Callable[[SyncPair], bool]:
        s3 = _FakeS3(
            {
                "s3://b/src": _FakeStorage("b", _FakeClient({"src": src_resp})),
                "s3://b/dst": _FakeStorage("b", _FakeClient({"dst": dst_resp})),
            }
        )
        return ChecksumComparison(s3, "s3://b/src", "s3://b/dst", **kw)  # pyright: ignore[reportArgumentType]

    def _pair(self, **kw: Any) -> SyncPair:
        return SyncPair(
            key="k", transfer_type=TransferType.COPY, src=_s3("src", **kw), dst=_s3("dst", **kw)
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
            key="k",
            transfer_type=TransferType.COPY,
            src=_s3("src", size=10),
            dst=_s3("dst", size=20),
        )
        assert f2(pair) is True

    def test_reads_no_bytes(self) -> None:
        # COPY never opens a local file; keys point at nothing on disk.
        f = self._copy(_full("sha256", "ABC"), _full("sha256", "ABC"))
        assert f(self._pair()) is False

    def test_composite_values_skip_pagination(self) -> None:
        # COPY compares the stored value strings directly, so a COMPOSITE object
        # (whose parts would otherwise paginate) is read with one call per side.
        src = _FakeClient({"src": _composite_resp("sha256", "ZZZ-3", [10, 10, 10], truncated_at=2)})
        dst = _FakeClient({"dst": _composite_resp("sha256", "ZZZ-3", [10, 10, 10], truncated_at=2)})
        s3 = _FakeS3({"s3://b/src": _FakeStorage("b", src), "s3://b/dst": _FakeStorage("b", dst)})
        f = ChecksumComparison(s3, "s3://b/src", "s3://b/dst")  # pyright: ignore[reportArgumentType]
        pair = SyncPair(key="k", transfer_type=TransferType.COPY, src=_s3("src"), dst=_s3("dst"))
        assert f(pair) is False
        assert len(src.calls) == 1
        assert len(dst.calls) == 1


# -- indeterminate -> copy on errors -------------------------------------------


class TestClientErrorIndeterminate:
    def test_client_error_copies(self, tmp_path: Path) -> None:
        from botocore.exceptions import ClientError

        err = ClientError({"Error": {"Code": "AccessDenied"}}, "GetObjectAttributes")
        p = _write(tmp_path)
        client = _FakeClient({"obj": err})
        pair = _pair(
            TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
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
            TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
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
            TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
        )
        assert _upload_filter(client)(pair) is True


# -- request payer -------------------------------------------------------------


class TestRequestPayer:
    def test_threaded_into_the_call(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _GOLDEN["sha256"])})
        f = _upload_filter(client, request_payer="requester")
        f(
            _pair(
                TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
            )
        )
        assert client.calls[0]["RequestPayer"] == "requester"

    def test_omitted_by_default(self, tmp_path: Path) -> None:
        p = _write(tmp_path)
        client = _FakeClient({"obj": _full("sha256", _GOLDEN["sha256"])})
        _upload_filter(client)(
            _pair(
                TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
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
            TransferType.UPLOAD, src=_local(_key(p), size=len(_DATA)), dst=_s3(size=len(_DATA))
        )
        assert _upload_filter(client)(pair) is False

    def test_gate_copies_oversize_without_hashing(self, tmp_path: Path, _no_awscrt: None) -> None:
        # pure_max_size below the file -> the slow hash is skipped (indeterminate
        # -> copy). The local path does not exist, proving no read happened.
        client = _FakeClient({"obj": _full("crc64nvme", "irrelevant")})
        f = _upload_filter(client, pure_max_size=0)
        pair = _pair(
            TransferType.UPLOAD, src=_local(_key(tmp_path / "nope"), size=10), dst=_s3(size=10)
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
        p = str(_write(tmp_path))
        assert _whole_b64(p, "crc32") == _GOLDEN["crc32"]
        assert _whole_b64(p, "sha256") == _GOLDEN["sha256"]

    @_needs_crt
    def test_composite_b64_matches_independent(self, tmp_path: Path) -> None:
        p = str(_write(tmp_path))
        sizes = (500, 524)
        assert _composite_b64(p, "crc32c", sizes) == _composite_golden(_DATA, list(sizes), "crc32c")

    def test_can_compute_always_for_stdlib(self) -> None:
        for algo in ("crc32", "sha1", "sha256"):
            assert _can_compute(algo, 10**12, pure_max_size=1) is True

    def test_can_compute_unknown_algorithm(self) -> None:
        assert _can_compute("xxhash64", None, None) is False


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
