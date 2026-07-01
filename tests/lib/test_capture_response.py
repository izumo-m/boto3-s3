"""``capture_response``: surfacing the S3 write response on ``OpResult.extra_info``.

Capture rides botocore's client event stream (``before-parameter-build`` +
``after-call``), so these run against a real moto-backed client - the recording
client the other transfer suites use stubs ``_make_api_call`` and would never
emit those events. Stage 1 covers the ``write`` slot (upload / copy) and the
ETag promotion; ``delete`` / ``read`` land in later stages.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any, cast

import boto3
import pytest
from moto import mock_aws

from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import OpResult

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

BUCKET = "capture-bucket"


@pytest.fixture
def s3_client() -> Iterator[Any]:
    """A moto-backed S3 client with ``BUCKET`` created (process-wide patch).

    ``S3()`` builds its own client under the same ``mock_aws`` patch, so it
    reaches this seeding client's in-memory backend.
    """
    with mock_aws():
        client = boto3.session.Session().client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _sink() -> tuple[list[OpResult], Callable[[OpResult], None]]:
    out: list[OpResult] = []
    return out, out.append


def _etag(data: bytes) -> str:
    return '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'  # S3 ETag form


class TestWriteSlot:
    def test_single_upload_captures_put_response(self, s3_client: Any, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"hello")
        out, cb = _sink()
        S3().cp(str(src), f"s3://{BUCKET}/a.txt", capture_response=True, on_result=cb)
        info = out[0].extra_info
        assert info is not None
        assert "write" in info
        # ResponseMetadata (HTTP headers, request id) never leaks to the surface.
        assert "ResponseMetadata" not in info["write"]
        # ETag is promoted from the write response.
        assert info["ETag"] == info["write"]["ETag"] == _etag(b"hello")

    def test_multipart_upload_captures_complete_response(
        self, s3_client: Any, tmp_path: Path
    ) -> None:
        src = tmp_path / "big.bin"
        src.write_bytes(b"x" * (9 * 1024 * 1024))  # over the 8 MiB multipart default
        out, cb = _sink()
        S3().cp(str(src), f"s3://{BUCKET}/big.bin", capture_response=True, on_result=cb)
        write = cast("dict[str, Any]", out[0].extra_info)["write"]
        # CompleteMultipartUpload shape (Bucket/Key/Location) - not an UploadPart
        # or PutObject response; the intermediate multipart calls are not observed.
        assert {"Bucket", "Key", "ETag"} <= set(write)
        assert cast("dict[str, Any]", out[0].extra_info)["ETag"] == write["ETag"]

    def test_copy_captures_copyobject_and_unwraps_etag(
        self, s3_client: Any, tmp_path: Path
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"hello")
        S3().cp(str(src), f"s3://{BUCKET}/a.txt")  # seed the source object
        out, cb = _sink()
        S3().cp(
            f"s3://{BUCKET}/a.txt",
            f"s3://{BUCKET}/b.txt",
            capture_response=True,
            on_result=cb,
        )
        info = cast("dict[str, Any]", out[0].extra_info)
        # CopyObject nests the ETag under CopyObjectResult; promotion unwraps it.
        assert "CopyObjectResult" in info["write"]
        assert info["ETag"] == info["write"]["CopyObjectResult"]["ETag"]

    def test_recursive_upload_has_no_crosstalk(self, s3_client: Any, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        contents = {f"f{i}.txt": f"content-{i}".encode() for i in range(5)}
        for name, data in contents.items():
            (src / name).write_bytes(data)
        out, cb = _sink()
        S3().cp(
            str(src),
            f"s3://{BUCKET}/dir",
            recursive=True,
            capture_response=True,
            on_result=cb,
        )
        assert len(out) == len(contents)
        # Each result carries its own object's response (keyed by Bucket/Key).
        for r in out:
            info = cast("dict[str, Any]", r.extra_info)
            assert info["write"]["ETag"] == _etag(contents[r.key]), r.key


class TestBackwardCompatWhenOff:
    def test_upload_without_flag_has_no_extra_info(self, s3_client: Any, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"hello")
        out, cb = _sink()
        S3().cp(str(src), f"s3://{BUCKET}/a.txt", on_result=cb)
        assert out[0].extra_info is None

    def test_copy_without_flag_keeps_etag_only(self, s3_client: Any, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"hello")
        S3().cp(str(src), f"s3://{BUCKET}/a.txt")
        out, cb = _sink()
        S3().cp(f"s3://{BUCKET}/a.txt", f"s3://{BUCKET}/b.txt", on_result=cb)
        info = out[0].extra_info
        assert info is not None
        assert "write" not in info
        assert "ETag" in info


class TestClientHygiene:
    def test_handlers_removed_after_op_on_a_shared_client(
        self, s3_client: Any, tmp_path: Path
    ) -> None:
        # One shared client across two ops. If the capture handlers leaked past
        # the first op, the second (no-flag) upload would still capture a write
        # response; instead its extra_info is None (historical upload shape).
        src = tmp_path / "a.txt"
        src.write_bytes(b"hello")
        out1, cb1 = _sink()
        S3().cp(
            str(src),
            S3Storage(f"s3://{BUCKET}/a.txt", client=s3_client),
            capture_response=True,
            on_result=cb1,
        )
        assert out1[0].extra_info is not None and "write" in out1[0].extra_info
        out2, cb2 = _sink()
        S3().cp(str(src), S3Storage(f"s3://{BUCKET}/b.txt", client=s3_client), on_result=cb2)
        assert out2[0].extra_info is None


def test_capture_response_forces_classic_engine(
    s3_client: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # capture rides the botocore client events the CRT data plane bypasses, so a
    # capture upload skips the CRT manager and takes the classic engine - logged
    # as a breadcrumb on the --debug channel next to "transfer engine: ...".
    src = tmp_path / "a.txt"
    src.write_bytes(b"hi")
    with caplog.at_level(logging.DEBUG, logger="boto3_s3.transfer"):
        S3().cp(str(src), f"s3://{BUCKET}/a.txt", capture_response=True)
    assert any("classic forced by capture_response" in r.getMessage() for r in caplog.records)
