"""``capture_response``: surfacing S3 responses on ``OpResult.extra_info``.

The write / read slots ride botocore's client event stream
(``before-parameter-build`` + ``after-call``), so these run against a real
moto-backed client - the recording client the other transfer suites use stubs
``_make_api_call`` and would never emit those events. Covered: the ``write`` slot
(upload / copy, ETag promotion), the ``read`` slot (download), and the ``delete``
slot (an ``mv`` source, and ``rm`` / ``sync --delete``, batched or blind
single-key).
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import TYPE_CHECKING, Any, cast

import boto3
import pytest
from moto import mock_aws

from boto3_s3.iostorage import IOStorage
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.transfer import TransferItem, Transferrer
from boto3_s3.types import OpOutcome, OpResult, TransferOptions, TransferType

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

    def test_filter_read_never_satisfies_the_write_slot(
        self, s3_client: Any, tmp_path: Path
    ) -> None:
        # A sync content filter reading the pre-overwrite destination through
        # the run's own client (pair.dest.storage.open(..., "rb")) records a
        # GetObject for the very key the upload then writes. Reads and writes
        # are stored separately, so the write slot still surfaces the
        # PutObject response - the OLD object's payload must not win.
        src = tmp_path / "src"
        src.mkdir()
        (src / "k.txt").write_bytes(b"new-bytes!")
        seed = tmp_path / "seed.txt"
        seed.write_bytes(b"old")
        S3().cp(str(seed), f"s3://{BUCKET}/pre/k.txt")

        read_back: list[bytes] = []

        def reads_dest(pair: Any) -> bool:
            dest = pair.dest
            assert dest is not None and dest.storage is not None
            with dest.storage.open(dest.key, "rb") as fh:
                read_back.append(fh.read())
            return True

        out, cb = _sink()
        S3().sync(
            str(src),
            f"s3://{BUCKET}/pre/",
            update_filter=reads_dest,
            capture_response=True,
            on_result=cb,
        )
        assert read_back == [b"old"]  # the filter really read the old object
        copied = [r for r in out if r.outcome is OpOutcome.SUCCEEDED]
        info = cast("dict[str, Any]", copied[0].extra_info)
        assert info["ETag"] == info["write"]["ETag"] == _etag(b"new-bytes!")


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

    def test_failed_upload_drains_the_captured_error_response(
        self, s3_client: Any, tmp_path: Path
    ) -> None:
        # after-call fires before botocore raises for a >=300 status, so the
        # failed PutObject stores an error payload under the dest key; the
        # FAILED path must drain it - surfacing no extra_info and leaving
        # nothing behind in the capture store for the rest of the run.
        src = tmp_path / "f.txt"
        src.write_bytes(b"x")
        client = boto3.session.Session().client("s3", region_name="us-east-1")
        results, cb = _sink()
        item = TransferItem(
            compare_key="f.txt",
            size=1,
            src_path=str(src),
            dest_bucket="no-such-bucket-anywhere",
            dest_key="f.txt",
            src_display=str(src),
            dest_display="s3://no-such-bucket-anywhere/f.txt",
        )
        transferrer = Transferrer(TransferType.UPLOAD, client, capture_response=True, on_result=cb)
        with transferrer:
            transferrer.submit(item)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert results[0].extra_info is None
        capture = transferrer._capture  # pyright: ignore[reportPrivateUsage]
        assert capture is not None
        assert capture._writes == {}  # pyright: ignore[reportPrivateUsage]
        assert capture._reads == {}  # pyright: ignore[reportPrivateUsage]

    def test_no_overwrite_skip_drains_the_captured_412(
        self, s3_client: Any, tmp_path: Path
    ) -> None:
        # --no-overwrite's IfNoneMatch rejection is a silent SKIPPED, and
        # after-call already stored the 412 error payload under the dest key;
        # the skip branch must drain it like the failure branch.
        s3_client.put_object(Bucket=BUCKET, Key="exists.txt", Body=b"old")
        src = tmp_path / "exists.txt"
        src.write_bytes(b"new")
        client = boto3.session.Session().client("s3", region_name="us-east-1")
        results, cb = _sink()
        item = TransferItem(
            compare_key="exists.txt",
            size=3,
            src_path=str(src),
            dest_bucket=BUCKET,
            dest_key="exists.txt",
        )
        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            options=TransferOptions(no_overwrite=True),
            capture_response=True,
            on_result=cb,
        )
        with transferrer:
            transferrer.submit(item)
        assert [result.outcome for result in results] == [OpOutcome.SKIPPED]
        assert results[0].extra_info is None
        capture = transferrer._capture  # pyright: ignore[reportPrivateUsage]
        assert capture is not None
        assert capture._writes == {}  # pyright: ignore[reportPrivateUsage]
        assert capture._reads == {}  # pyright: ignore[reportPrivateUsage]

    def test_exit_unregisters_capture_even_when_shutdown_raises(
        self, s3_client: Any, tmp_path: Path
    ) -> None:
        # Transferrer.__exit__ unregisters the capture handlers in a finally:
        # even a manager shutdown that raises must not leave them feeding the
        # store on a longer-lived client.
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        client = boto3.session.Session().client("s3", region_name="us-east-1")
        transferrer = Transferrer(TransferType.UPLOAD, client, capture_response=True)
        item = TransferItem(
            compare_key="a.txt", size=1, src_path=str(src), dest_bucket=BUCKET, dest_key="a.txt"
        )
        real_shutdown: list[Any] = []

        def _boom(**kwargs: Any) -> None:
            raise RuntimeError("shutdown boom")

        with pytest.raises(RuntimeError, match="shutdown boom"):
            with transferrer:
                transferrer.submit(item)
                manager = transferrer._manager  # pyright: ignore[reportPrivateUsage]
                assert manager is not None
                real_shutdown.append(manager.shutdown)
                manager.shutdown = _boom
        real_shutdown[0]()  # drain the real manager (test hygiene)
        capture = transferrer._capture  # pyright: ignore[reportPrivateUsage]
        assert capture is not None
        # Handlers left the client despite the raise: a later PutObject on the
        # same client must not feed the old capture store.
        client.put_object(Bucket=BUCKET, Key="later.txt", Body=b"y")
        assert capture._writes == {}  # pyright: ignore[reportPrivateUsage]
        assert capture._reads == {}  # pyright: ignore[reportPrivateUsage]


class TestStreamRoutes:
    def test_stream_download_carries_read_slot_and_lists_nothing(self, s3_client: Any) -> None:
        s3_client.put_object(Bucket=BUCKET, Key="a.txt", Body=b"hello")
        buf = io.BytesIO()
        out, cb = _sink()
        S3().cp(f"s3://{BUCKET}/a.txt", IOStorage(buf), capture_response=True, on_result=cb)
        info = out[0].extra_info
        assert info is not None and "read" in info
        assert "Body" not in info["read"]
        assert info["ETag"] == info["read"]["ETag"] == _etag(b"hello")
        assert buf.getvalue() == b"hello"
        # opresult.md: a stream cp lists nothing - both listing entries are None.
        assert out[0].src_info is None and out[0].dest_info is None

    def test_stream_upload_carries_write_slot(self, s3_client: Any) -> None:
        out, cb = _sink()
        S3().cp(
            IOStorage(io.BytesIO(b"hello")),
            f"s3://{BUCKET}/up.txt",
            capture_response=True,
            on_result=cb,
        )
        info = out[0].extra_info
        assert info is not None and "write" in info
        assert info["ETag"] == info["write"]["ETag"] == _etag(b"hello")

    def test_mv_to_stream_carries_read_and_delete(self, s3_client: Any) -> None:
        s3_client.put_object(Bucket=BUCKET, Key="gone.txt", Body=b"hello")
        buf = io.BytesIO()
        out, cb = _sink()
        S3().mv(f"s3://{BUCKET}/gone.txt", IOStorage(buf), capture_response=True, on_result=cb)
        info = out[0].extra_info
        assert info is not None
        assert "read" in info and "delete" in info
        assert buf.getvalue() == b"hello"
        assert s3_client.list_objects_v2(Bucket=BUCKET).get("KeyCount") == 0


class TestMvDeleteSlot:
    def test_mv_s3_to_s3_carries_write_and_delete(self, s3_client: Any, tmp_path: Path) -> None:
        s3_client.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )
        src = tmp_path / "a.txt"
        src.write_bytes(b"hello")
        S3().cp(str(src), f"s3://{BUCKET}/a.txt")  # seed the source object
        out, cb = _sink()
        S3().mv(f"s3://{BUCKET}/a.txt", f"s3://{BUCKET}/b.txt", capture_response=True, on_result=cb)
        info = cast("dict[str, Any]", out[0].extra_info)
        assert "write" in info  # the CopyObject that created the destination
        assert "delete" in info  # the source's DeleteObject
        assert info["delete"].get("DeleteMarker") is True
        assert info["ETag"] == info["write"]["CopyObjectResult"]["ETag"]

    def test_mv_without_flag_has_etag_only(self, s3_client: Any, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"hello")
        S3().cp(str(src), f"s3://{BUCKET}/a.txt")
        out, cb = _sink()
        S3().mv(f"s3://{BUCKET}/a.txt", f"s3://{BUCKET}/b.txt", on_result=cb)
        info = out[0].extra_info
        assert info is not None
        assert "write" not in info
        assert "delete" not in info
        assert "ETag" in info


class TestRmDeleteSlot:
    def test_rm_recursive_batch_reconstructs_delete_slot(self, s3_client: Any) -> None:
        s3_client.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )
        for i in range(3):
            s3_client.put_object(Bucket=BUCKET, Key=f"d/f{i}.txt", Body=b"x")
        out, cb = _sink()
        S3().rm(f"s3://{BUCKET}/d", recursive=True, capture_response=True, on_result=cb)
        assert len(out) == 3
        for r in out:
            info = cast("dict[str, Any]", r.extra_info)
            # the batch DeleteObjects entry, reconstructed to a DeleteObject shape
            assert "delete" in info
            assert info["delete"].get("DeleteMarker") is True

    def test_rm_single_key_blind_carries_delete_slot(self, s3_client: Any) -> None:
        s3_client.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )
        s3_client.put_object(Bucket=BUCKET, Key="solo.txt", Body=b"x")
        out, cb = _sink()
        # non-recursive exact key: the blind Storage.delete path, not the batcher
        S3().rm(f"s3://{BUCKET}/solo.txt", capture_response=True, on_result=cb)
        info = cast("dict[str, Any]", out[0].extra_info)
        assert "delete" in info
        assert info["delete"].get("DeleteMarker") is True

    def test_rm_recursive_without_flag_has_no_extra_info(self, s3_client: Any) -> None:
        for i in range(2):
            s3_client.put_object(Bucket=BUCKET, Key=f"d/f{i}.txt", Body=b"x")
        out, cb = _sink()
        S3().rm(f"s3://{BUCKET}/d", recursive=True, on_result=cb)
        assert all(r.extra_info is None for r in out)


class TestReadSlot:
    def test_single_download_captures_get_response(self, s3_client: Any, tmp_path: Path) -> None:
        s3_client.put_object(Bucket=BUCKET, Key="a.txt", Body=b"hello")
        out, cb = _sink()
        S3().cp(
            f"s3://{BUCKET}/a.txt", str(tmp_path / "a.txt"), capture_response=True, on_result=cb
        )
        info = cast("dict[str, Any]", out[0].extra_info)
        assert "read" in info
        assert "Body" not in info["read"]  # the streaming body is stripped
        assert "ResponseMetadata" not in info["read"]
        assert info["read"]["ETag"] == info["ETag"] == _etag(b"hello")

    def test_multipart_download_read_is_whole_object_shaped(
        self, s3_client: Any, tmp_path: Path
    ) -> None:
        big = b"x" * (9 * 1024 * 1024)  # over the 8 MiB threshold -> ranged GetObjects
        s3_client.put_object(Bucket=BUCKET, Key="big.bin", Body=big)
        out, cb = _sink()
        S3().cp(
            f"s3://{BUCKET}/big.bin", str(tmp_path / "big.bin"), capture_response=True, on_result=cb
        )
        info = cast("dict[str, Any]", out[0].extra_info)
        assert "read" in info
        # the range-specific fields of a partial GET are dropped
        assert "ContentRange" not in info["read"]
        assert info["read"]["ETag"] == _etag(big)

    def test_download_without_flag_keeps_etag_only(self, s3_client: Any, tmp_path: Path) -> None:
        s3_client.put_object(Bucket=BUCKET, Key="a.txt", Body=b"hello")
        out, cb = _sink()
        S3().cp(f"s3://{BUCKET}/a.txt", str(tmp_path / "a.txt"), on_result=cb)
        info = out[0].extra_info
        assert info is not None
        assert "read" not in info
        assert "ETag" in info

    def test_filter_source_read_never_wins_the_read_slot(
        self, s3_client: Any, tmp_path: Path
    ) -> None:
        # An s3->local sync whose content update_filter reads the SOURCE through
        # the run's own client (pair.src.storage.open(..., "rb")) records a
        # GetObject for the very key about to be downloaded. The read store is
        # first-stored-wins, so without the submit-time clear that stale read
        # would beat the transfer's own GetObject to the read slot. The filter
        # overwrites the source after reading, so the two GetObjects carry
        # different ETags - the read slot must reflect the bytes actually
        # downloaded, not the filter's pre-change response.
        s3_client.put_object(Bucket=BUCKET, Key="pre/k.txt", Body=b"AAAA")
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "k.txt").write_bytes(b"zzzz")  # pre-existing dest -> the update lane

        def reads_then_bumps_source(pair: Any) -> bool:
            src = pair.src
            assert src is not None and src.storage is not None
            with src.storage.open(src.key, "rb") as fh:
                assert fh.read() == b"AAAA"  # the filter really reads the old source
            # Mutate the source after its GetObject is captured; the transfer's
            # own GetObject then reads (and must store) the new bytes.
            s3_client.put_object(Bucket=BUCKET, Key="pre/k.txt", Body=b"BBBBBBB")
            return True

        out, cb = _sink()
        S3().sync(
            f"s3://{BUCKET}/pre/",
            str(dst),
            update_filter=reads_then_bumps_source,
            capture_response=True,
            on_result=cb,
        )
        copied = [r for r in out if r.outcome is OpOutcome.SUCCEEDED]
        assert len(copied) == 1
        info = cast("dict[str, Any]", copied[0].extra_info)
        assert (dst / "k.txt").read_bytes() == b"BBBBBBB"  # the transfer wrote new bytes
        assert info["read"]["ETag"] == info["ETag"] == _etag(b"BBBBBBB")


class TestSyncDelete:
    def test_sync_delete_s3_dest_carries_delete_slot(self, s3_client: Any, tmp_path: Path) -> None:
        s3_client.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )
        # an orphan in the S3 destination, absent from the (empty) local source
        s3_client.put_object(Bucket=BUCKET, Key="dst/orphan.txt", Body=b"x")
        src = tmp_path / "src"
        src.mkdir()
        out, cb = _sink()
        S3().sync(
            str(src), f"s3://{BUCKET}/dst", delete_filter=True, capture_response=True, on_result=cb
        )
        assert len(out) == 1  # just the orphan removal (nothing to transfer)
        info = cast("dict[str, Any]", out[0].extra_info)
        assert "delete" in info  # the batched DeleteObjects entry, reconstructed
        assert info["delete"].get("DeleteMarker") is True


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
