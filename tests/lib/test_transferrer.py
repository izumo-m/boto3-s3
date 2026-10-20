"""``boto3_s3.transfer``: the s3transfer-backed engine, against a recording client.

The recorder replaces ``client._make_api_call`` (the same seam aws-cli's own
functional tests stub at the HTTP layer), so the genuine s3transfer
submission paths run - single-part and multipart - while every API call is
recorded. ``TransferConfig(use_threads=False)`` selects the
NonThreadedExecutor exactly like boto3 does, making multipart call order
deterministic.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import errno
import io
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    EndpointConnectionError,
    IncompleteReadError,
    ReadTimeoutError,
)
from s3transfer import copies as s3transfer_copies
from s3transfer.copies import CopySubmissionTask

from boto3_s3 import transfer
from boto3_s3.exceptions import (
    AccessDeniedError,
    Boto3S3Error,
    CancelledError,
    ConfigurationError,
    InvalidConfigError,
    MalformedResponseError,
    NotFoundError,
    TransportError,
    ValidationError,
)
from boto3_s3.localstorage import LocalStorage
from boto3_s3.transfer import (
    TransferItem,
    Transferrer,
    annotations_copy_unsupported_reason,
    conditional_write_unsupported_reason,
)
from boto3_s3.transferconfig import TransferConfig as LibraryTransferConfig
from boto3_s3.types import (
    AnnotationCopyMode,
    CancelMode,
    CancelToken,
    CopyPropsMode,
    FileInfo,
    OpOutcome,
    OpResult,
    TransferOptions,
    TransferProgress,
    TransferType,
)
from tests.utils.fakemodel import model_client, model_only_client
from tests.utils.fakes3 import MTIME, client_error
from tests.utils.recorder import ApiCall, make_recording_client, ops

_SYNC_CONFIG = TransferConfig(use_threads=False)
_MIB = 1024 * 1024


class _Pipe:
    """A reader without ``seek`` / ``tell``: what s3transfer calls non-seekable."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def close(self) -> None:
        self._buffer.close()


def _consume_bodies(client: Any) -> None:
    """Have the recording client read each request ``Body`` like a real one.

    s3transfer fires its progress callbacks as the body is read, so a stream
    upload's byte counter (``transfer._CountBytes``) only advances when the
    stub consumes what botocore would have sent. The chunk's callbacks are
    enabled by s3transfer's ``request-created`` hook, which the stub never
    reaches, so the stub enables them the way that hook would.
    """
    original = client._make_api_call

    def call(operation: str, params: dict[str, Any]) -> Any:
        body = params.get("Body")
        if body is not None and hasattr(body, "read"):
            enable = getattr(body, "enable_callback", None)
            if enable is not None:
                enable()
            body.read()
        return original(operation, params)

    client._make_api_call = call


class _OpenSink(io.BytesIO):
    """A download sink whose ``close`` is suppressed.

    The open route has the transfer ``close`` every dest fileobj it is handed
    (``transfer._CloseFileobj`` - a custom backend's writer commits that way); a
    real ``IOStorage`` hands it a close-suppressing view, never the caller's raw
    stream. These lower-level tests build the ``TransferItem`` directly, so this
    stand-in mirrors that view: ``getvalue()`` stays readable after the transfer
    closes it.
    """

    def close(self) -> None:
        pass


def _run(
    kind: TransferType,
    items: list[TransferItem],
    responses: list[dict[str, Any] | BaseException],
    *,
    source_responses: list[dict[str, Any] | BaseException] | None = None,
    options: TransferOptions | None = None,
    config: TransferConfig = _SYNC_CONFIG,
    is_move: bool = False,
    dest_storage: LocalStorage | None = None,
) -> tuple[list[ApiCall], list[ApiCall], list[OpResult], Transferrer]:
    client, calls = make_recording_client(responses)
    source_client = None
    source_calls: list[ApiCall] = []
    if source_responses is not None:
        source_client, source_calls = make_recording_client(source_responses)
    # Mirror the orchestration: an mv upload deletes its source through the
    # source storage's Storage.delete(info), so wire both like production does.
    src_storage: LocalStorage | None = None
    if is_move and kind is TransferType.UPLOAD:
        src_storage = LocalStorage(".")
        for it in items:
            if it.src_info is None and it.src_path is not None:
                it.src_info = FileInfo(key=it.src_path.replace(os.sep, "/"))
    results: list[OpResult] = []
    transferrer = Transferrer(
        kind,
        client,
        source_client=source_client,
        src_storage=src_storage,
        dest_storage=dest_storage,
        transfer_config=config,
        options=options,
        is_move=is_move,
        on_result=results.append,
    )
    with transferrer:
        for item in items:
            transferrer.submit(item)
    return calls, source_calls, results, transferrer


class _BodyRaising:
    """A streaming body whose ``read`` raises (a reply cut short inside botocore)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def read(self, *_: Any) -> bytes:
        raise self._exc


class TestUpload:
    def test_single_put_object(self, tmp_path: Path) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"payload")
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            src_path=str(src),
            dest_bucket="bucket",
            dest_key="up/a.bin",
            src_display=str(src),
            dest_display="s3://bucket/up/a.bin",
        )
        calls, _, results, transferrer = _run(TransferType.UPLOAD, [item], [{}])
        assert ops(calls) == ["PutObject"]
        assert calls[0].params["Bucket"] == "bucket"
        assert calls[0].params["Key"] == "up/a.bin"
        assert (transferrer.succeeded, transferrer.failed) == (1, 0)
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert results[0].src == str(src)
        assert results[0].dest == "s3://bucket/up/a.bin"
        assert results[0].bytes_transferred == 7

    def test_result_has_no_extra_info(self, tmp_path: Path) -> None:
        # s3transfer discards the PutObject response, so an upload surfaces no
        # result ETag (design/transfer.md).
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        # A realistic PutObject response carries an ETag; extra_info must stay
        # None anyway (the response is discarded, not mined for the ETag).
        _, _, results, _ = _run(TransferType.UPLOAD, [item], [{"ETag": '"abc123"'}])
        assert results[0].extra_info is None

    def test_multipart_upload_sequence(self, tmp_path: Path) -> None:
        src = tmp_path / "big.bin"
        src.write_bytes(b"x" * (9 * _MIB))
        item = TransferItem(
            compare_key="big.bin", size=9 * _MIB, src_path=str(src), dest_bucket="b", dest_key="big"
        )
        calls, _, _, transferrer = _run(
            TransferType.UPLOAD,
            [item],
            [{"UploadId": "upload-id"}, {"ETag": '"p1"'}, {"ETag": '"p2"'}, {}],
        )
        assert ops(calls) == [
            "CreateMultipartUpload",
            "UploadPart",
            "UploadPart",
            "CompleteMultipartUpload",
        ]
        assert calls[1].params["UploadId"] == "upload-id"
        assert transferrer.succeeded == 1

    def test_content_type_guessed_for_uploads(self, tmp_path: Path) -> None:
        src = tmp_path / "data.json"
        src.write_bytes(b"{}")
        item = TransferItem(
            compare_key="data.json", size=2, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, _, _ = _run(TransferType.UPLOAD, [item], [{}])
        assert calls[0].params["ContentType"] == "application/json"

    def test_explicit_content_type_wins_over_the_guess(self, tmp_path: Path) -> None:
        src = tmp_path / "data.json"
        src.write_bytes(b"{}")
        item = TransferItem(
            compare_key="data.json", size=2, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, _, _ = _run(
            TransferType.UPLOAD, [item], [{}], options=TransferOptions(content_type="text/x-probe")
        )
        assert calls[0].params["ContentType"] == "text/x-probe"

    def test_no_guess_leaves_content_type_unset(self, tmp_path: Path) -> None:
        src = tmp_path / "data.json"
        src.write_bytes(b"{}")
        item = TransferItem(
            compare_key="data.json", size=2, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, _, _ = _run(
            TransferType.UPLOAD, [item], [{}], options=TransferOptions(guess_mime_type=False)
        )
        assert "ContentType" not in calls[0].params

    def _upload_on_crt(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        options: TransferOptions | None = None,
    ) -> dict[str, Any]:
        """Submit one upload through a stand-in CRT manager; its extra_args."""
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        manager = _CrtCapturingManager()
        monkeypatch.setattr(Transferrer, "_create_crt_manager", lambda _self: manager)
        client, _ = make_recording_client([])
        with Transferrer(TransferType.UPLOAD, client, options=options) as transferrer:
            transferrer.submit(item)
        return manager.uploads[0]["extra_args"]

    def test_the_library_names_no_checksum_of_its_own_on_the_crt_lane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # boto3-faithful: the default is s3transfer's (CRC32 in its CRT module).
        # aws's CRC64NVME default is the CLI's to name (boto3_s3_cli.checksumdefault,
        # design/crt.md section 1) - and it matters there, since aws-checksums
        # computes CRC32 in software (benchmarks/RESULTS.md 2026-09-06).
        extra_args = self._upload_on_crt(tmp_path, monkeypatch)
        assert "ChecksumAlgorithm" not in extra_args

    def test_an_explicit_checksum_reaches_the_crt_lane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        extra_args = self._upload_on_crt(
            tmp_path, monkeypatch, options=TransferOptions(checksum_algorithm="CRC64NVME")
        )
        assert extra_args["ChecksumAlgorithm"] == "CRC64NVME"

    def test_classic_lane_keeps_s3transfers_default(self, tmp_path: Path) -> None:
        # pip s3transfer stamps botocore's default (CRC32) on a classic upload
        # that names none; the library leaves it (design/transfer.md section 10).
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, _, _ = _run(TransferType.UPLOAD, [item], [{}])
        assert calls[0].params["ChecksumAlgorithm"] == "CRC32"

    def test_request_params_flow_into_put_object(self, tmp_path: Path) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, _, _ = _run(
            TransferType.UPLOAD,
            [item],
            [{}],
            options=TransferOptions(
                storage_class="STANDARD_IA", metadata={"k1": "v1"}, sse="AES256"
            ),
        )
        params = calls[0].params
        assert params["StorageClass"] == "STANDARD_IA"
        assert params["Metadata"] == {"k1": "v1"}
        assert params["ServerSideEncryption"] == "AES256"

    def test_failure_translates_and_aggregates(self, tmp_path: Path) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, results, transferrer = _run(
            TransferType.UPLOAD, [item], [client_error("NoSuchBucket", 404, "PutObject")]
        )
        assert ops(calls) == ["PutObject"]
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert isinstance(transferrer.first_error, NotFoundError)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        # The attribute contract (exceptions.md section 2.1): an
        # exception-backed per-item failure links its original ClientError on
        # the record error's __cause__, matching the raise-from paths.
        error = results[0].error
        assert error is not None
        assert isinstance(error.__cause__, ClientError)

    def test_invalid_grants_raise_at_submit_time(self, tmp_path: Path) -> None:
        # The operation layer refuses a bad grants before the run; driven
        # directly, the Transferrer's own per-item mapping still refuses it at
        # submit, before any request.
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        client, calls = make_recording_client([{}])
        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            options=TransferOptions(grants=["bogus"]),
            transfer_config=_SYNC_CONFIG,
        )
        with pytest.raises(ValidationError), transferrer:
            transferrer.submit(item)
        assert calls == []

    def test_submit_failure_closes_an_already_open_fileobj(self) -> None:
        # The open routes / _cp_stream hand submit an already-open stream; a
        # submit-time failure (here: the grants ValidationError from param
        # mapping) must release it - no future exists, so _CloseFileobj never
        # runs.
        fileobj = io.BytesIO(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_fileobj=fileobj, dest_bucket="b", dest_key="k"
        )
        client, calls = make_recording_client([{}])
        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            options=TransferOptions(grants=["bogus"]),
            transfer_config=_SYNC_CONFIG,
        )
        with pytest.raises(ValidationError), transferrer:
            transferrer.submit(item)
        assert calls == []
        assert fileobj.closed


class TestDownload:
    def _item(self, tmp_path: Path, dest: str = "out/a.bin") -> TransferItem:
        return TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            mtime=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            src_bucket="bucket",
            src_key="d/a.bin",
            dest_path=str(tmp_path / dest),
            src_display="s3://bucket/d/a.bin",
            dest_display=dest,
        )

    def _get_object_response(self) -> dict[str, Any]:
        return {"Body": io.BytesIO(b"payload"), "ContentLength": 7, "ETag": '"abc123"'}

    def test_single_download_writes_file_and_stamps_mtime(self, tmp_path: Path) -> None:
        item = self._item(tmp_path)
        calls, _, results, transferrer = _run(
            TransferType.DOWNLOAD, [item], [self._get_object_response()]
        )
        # Size + etag were both provided, so no HeadObject probe happened.
        assert ops(calls) == ["GetObject"]
        target = tmp_path / "out" / "a.bin"
        assert target.read_bytes() == b"payload"
        assert item.mtime is not None
        assert os.stat(target).st_mtime == item.mtime.timestamp()
        assert transferrer.succeeded == 1
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]

    def test_an_empty_etag_is_provided_too(self, tmp_path: Path) -> None:
        # Provided means "is not None": with size and an (empty) etag in hand
        # s3transfer sends no HeadObject probe, exactly as when aws-cli hands
        # it the empty ETag an endpoint served.
        item = self._item(tmp_path)
        item.etag = ""
        calls, _, results, _ = _run(TransferType.DOWNLOAD, [item], [self._get_object_response()])
        assert ops(calls) == ["GetObject"]
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]

    def test_a_missing_etag_lets_s3transfer_probe(self, tmp_path: Path) -> None:
        item = self._item(tmp_path)
        item.etag = None
        head = {"ContentLength": 7, "ETag": '"abc123"'}
        calls, _, _, _ = _run(TransferType.DOWNLOAD, [item], [head, self._get_object_response()])
        assert ops(calls) == ["HeadObject", "GetObject"]

    def test_result_carries_source_etag_as_extra_info(self, tmp_path: Path) -> None:
        # s3transfer records the object's ETag on the future; it rides through to
        # OpResult.extra_info as the result's S3 response metadata.
        item = self._item(tmp_path)
        _, _, results, _ = _run(TransferType.DOWNLOAD, [item], [self._get_object_response()])
        assert results[0].extra_info == {"ETag": '"abc123"'}

    def test_parent_directories_are_created(self, tmp_path: Path) -> None:
        item = self._item(tmp_path, dest="deep/er/tree/a.bin")
        _run(TransferType.DOWNLOAD, [item], [self._get_object_response()])
        assert (tmp_path / "deep" / "er" / "tree" / "a.bin").exists()

    def test_utime_failure_warns_but_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item = self._item(tmp_path)

        def _boom(path: object, times: object) -> None:
            raise OSError(1, "Operation not permitted")

        monkeypatch.setattr(os, "utime", _boom)
        _, _, results, transferrer = _run(
            TransferType.DOWNLOAD, [item], [self._get_object_response()]
        )
        assert (transferrer.succeeded, transferrer.failed, transferrer.warned) == (1, 0, 1)
        outcomes = [result.outcome for result in results]
        assert outcomes == [OpOutcome.WARNED, OpOutcome.SUCCEEDED]
        warned = results[0]
        # The warning names the file whose time could not be set.
        assert item.dest_path is not None and item.dest_path in str(warned.error)

    def test_download_request_params(self, tmp_path: Path) -> None:
        item = self._item(tmp_path)
        calls, _, _, _ = _run(
            TransferType.DOWNLOAD,
            [item],
            [self._get_object_response()],
            options=TransferOptions(request_payer="requester"),
        )
        assert calls[0].params["RequestPayer"] == "requester"

    def test_streaming_download_reports_resolved_bytes(self) -> None:
        # No size/etag (a streaming download): s3transfer probes the object via
        # HeadObject, so SUCCEEDED must report the resolved byte count, not 0.
        sink = _OpenSink()
        item = TransferItem(
            compare_key="a.bin",
            src_bucket="bucket",
            src_key="d/a.bin",
            dest_fileobj=sink,
            src_display="s3://bucket/d/a.bin",
            dest_display="-",
        )
        head = {"ContentLength": 7, "ETag": '"abc123"'}
        calls, _, results, _ = _run(
            TransferType.DOWNLOAD, [item], [head, self._get_object_response()]
        )
        assert ops(calls) == ["HeadObject", "GetObject"]
        assert sink.getvalue() == b"payload"
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]
        assert results[0].bytes_transferred == 7


class TestCopy:
    def _item(self, *, size: int = 7, head: dict[str, Any] | None = None) -> TransferItem:
        # An etag is always known for an S3 source (listing and HeadObject
        # both carry it); providing it alongside the size is what keeps
        # s3transfer 0.17's copy path from probing the source with its own
        # HeadObject (it heads when size OR etag is missing).
        return TransferItem(
            compare_key="a.bin",
            size=size,
            etag='"abc123"',
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
            head=head,
        )

    def test_single_part_copy_props_default_is_native(self) -> None:
        # Below the threshold S3's CopyObject carries metadata and tags by
        # itself: no directives are sent and no extra reads happen.
        calls, source_calls, _, transferrer = _run(
            TransferType.COPY, [self._item()], [{}], source_responses=[]
        )
        assert ops(calls) == ["CopyObject"]
        assert calls[0].params["CopySource"] == {"Bucket": "src-b", "Key": "d/a.bin"}
        assert "MetadataDirective" not in calls[0].params
        assert "TaggingDirective" not in calls[0].params
        assert source_calls == []
        assert transferrer.succeeded == 1

    def test_copy_props_none_replaces_both_directives(self) -> None:
        calls, _, _, _ = _run(
            TransferType.COPY,
            [self._item()],
            [{}],
            source_responses=[],
            options=TransferOptions(copy_props=CopyPropsMode.NONE),
        )
        assert calls[0].params["MetadataDirective"] == "REPLACE"
        assert calls[0].params["TaggingDirective"] == "REPLACE"

    def test_explicit_metadata_directive_disables_copy_props(self) -> None:
        calls, source_calls, _, _ = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            [
                {"UploadId": "u"},
                {"CopyPartResult": {"ETag": '"p1"'}},
                {"CopyPartResult": {"ETag": '"p2"'}},
                {},
            ],
            source_responses=[],
            options=TransferOptions(metadata_directive="REPLACE"),
        )
        assert ops(calls) == [
            "CreateMultipartUpload",
            "UploadPartCopy",
            "UploadPartCopy",
            "CompleteMultipartUpload",
        ]
        assert source_calls == []

    def test_explicit_metadata_directive_copy_keeps_the_caller_properties(self) -> None:
        # With the chain disabled nothing sets MetadataDirective=REPLACE, which
        # is upstream 0.19's condition for keeping the seven preserved fields on
        # CreateMultipartUpload - it would otherwise strip the caller's own
        # --content-type / --metadata and substitute the (never fetched) head
        # response's. aws-cli's bundled s3transfer has no such table and passes
        # them straight through; _submit_copy flips the multipart-bound
        # directive to REPLACE (blocklisted from the create call, so no wire
        # request changes) to match.
        calls, source_calls, _, _ = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            [
                {"UploadId": "u"},
                {"CopyPartResult": {"ETag": '"p1"'}},
                {"CopyPartResult": {"ETag": '"p2"'}},
                {},
            ],
            source_responses=[],
            options=TransferOptions(
                metadata_directive="COPY",
                content_type="text/plain",
                cache_control="max-age=1",
                metadata={"k": "v"},
            ),
        )
        create = calls[0]
        assert create.operation == "CreateMultipartUpload"
        assert create.params["ContentType"] == "text/plain"
        assert create.params["CacheControl"] == "max-age=1"
        assert create.params["Metadata"] == {"k": "v"}
        # The directive itself stays off the create call (both tables blacklist it).
        assert "MetadataDirective" not in create.params
        assert source_calls == []

    def test_explicit_copy_directive_stays_on_a_single_part_copy(self) -> None:
        # Below the threshold the directive is a real CopyObject parameter -
        # S3 itself copies or replaces the metadata - so the multipart flip
        # must not touch it: aws sends COPY verbatim.
        calls, source_calls, _, _ = _run(
            TransferType.COPY,
            [self._item()],
            [{}],
            source_responses=[],
            options=TransferOptions(metadata_directive="COPY", content_type="text/plain"),
        )
        assert ops(calls) == ["CopyObject"]
        assert calls[0].params["MetadataDirective"] == "COPY"
        assert calls[0].params["ContentType"] == "text/plain"
        assert source_calls == []

    def test_upstream_copy_tables_keep_their_defaults_for_third_parties(self) -> None:
        # The alignment patches are process-shared, so they are bounded to
        # changes that stay inert for a plain s3transfer caller in the same
        # process: the preserved-metadata table - whose emptying would flip
        # such a caller's default multipart-copy behavior - stays upstream's
        # own (the explicit-COPY case is handled per request by _submit_copy
        # instead).
        _run(TransferType.COPY, [self._item()], [{}], source_responses=[])
        preserved = getattr(CopySubmissionTask, "PRESERVED_METADATA_FIELDS", None)
        if preserved is not None:  # absent on the s3transfer floor
            assert "ContentType" in preserved
        # The annotation writes are this library's own now, so upstream's
        # annotation table and write path are left exactly as shipped.
        put_args = getattr(CopySubmissionTask, "PUT_OBJECT_ANNOTATION_ARGS", None)
        if put_args is not None:
            assert "ChecksumAlgorithm" in put_args
        task_cls: Any = getattr(s3transfer_copies, "CopyCompleteMultipartUploadTask", None)
        apply_annotations = getattr(task_cls, "_apply_annotations", None)
        if apply_annotations is not None:
            assert apply_annotations.__module__ == "s3transfer.copies"

    def test_multipart_metadata_injected_from_cached_head(self) -> None:
        head = {"ContentType": "text/html", "Metadata": {"a": "b"}}
        calls, source_calls, _, _ = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB, head=head)],
            [
                {"UploadId": "u"},
                {"CopyPartResult": {"ETag": '"p1"'}},
                {"CopyPartResult": {"ETag": '"p2"'}},
                {},
            ],
            source_responses=[{"TagSet": []}],
            options=TransferOptions(copy_props=CopyPropsMode.METADATA_DIRECTIVE),
        )
        # The cached single-source HeadObject is reused: no second HEAD.
        assert source_calls == []
        create = calls[0]
        assert create.operation == "CreateMultipartUpload"
        assert create.params["ContentType"] == "text/html"
        assert create.params["Metadata"] == {"a": "b"}

    def test_multipart_default_heads_source_and_copies_small_tags(self) -> None:
        # The small set rides the Tagging header on the create call, aws-cli's
        # wire shape: its bundled s3transfer keeps the header off the create
        # blacklist, and _allow_inline_mpu_tagging realigns upstream 0.19's
        # table at manager build - uniform across the supported range (older
        # s3transfer never blacklisted it).
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {},
        ]
        source_responses: list[dict[str, Any] | Exception] = [
            {"ContentType": "text/css", "Metadata": {}},
            {"TagSet": [{"Key": "team", "Value": "a&b"}]},
        ]
        calls, source_calls, _, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            responses,
            source_responses=source_responses,
        )
        assert ops(source_calls) == ["HeadObject", "GetObjectTagging"]
        assert source_calls[0].params == {"Bucket": "src-b", "Key": "d/a.bin"}
        create = calls[0]
        assert create.params["ContentType"] == "text/css"
        assert create.params["Tagging"] == "team=a%26b"
        assert "PutObjectTagging" not in ops(calls)
        assert transferrer.succeeded == 1

    def test_multipart_tagging_read_failure_names_the_source(self) -> None:
        # _SetTags._get_source_tags pre-translates its GetObjectTagging read
        # carrying the *source* bucket/key; a raw ClientError would otherwise
        # be re-tagged with the copy destination by _record_failure.
        source_responses: list[dict[str, Any] | Exception] = [
            {},  # HeadObject
            client_error("AccessDenied", 403, "GetObjectTagging"),
        ]
        calls, source_calls, results, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            [],
            source_responses=source_responses,
        )
        assert ops(source_calls) == ["HeadObject", "GetObjectTagging"]
        assert calls == []  # the on_queued failure precedes CreateMultipartUpload
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        error = results[0].error
        assert type(error) is AccessDeniedError
        assert (error.bucket, error.key) == ("src-b", "d/a.bin")

    def test_a_tagging_read_raising_outside_botocore_names_the_source(self) -> None:
        # What botocore dies of inside the request (an S3 Express CreateSession
        # reply without Credentials) is the request failing all the same.
        source_responses: list[dict[str, Any] | Exception] = [{}, KeyError("Credentials")]
        _, _, results, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            [],
            source_responses=source_responses,
        )
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        error = results[0].error
        assert type(error) is Boto3S3Error
        assert isinstance(error.__cause__, KeyError)
        assert (error.bucket, error.key) == ("src-b", "d/a.bin")

    @pytest.mark.parametrize(
        ("options", "source_responses", "error_type", "text"),
        [
            pytest.param(
                TransferOptions(copy_props=CopyPropsMode.METADATA_DIRECTIVE),
                [KeyError("Credentials")],
                Boto3S3Error,
                "Credentials",
                id="head-raises",
            ),
            pytest.param(
                TransferOptions(), [{}, {}], MalformedResponseError, "TagSet", id="no-tagset"
            ),
            pytest.param(
                TransferOptions(),
                [{}, {"TagSet": [{"Value": "v"}]}],
                MalformedResponseError,
                "Key",
                id="tag-without-key",
            ),
            pytest.param(
                TransferOptions(),
                [{}, {"TagSet": [{"Key": "k"}]}],
                MalformedResponseError,
                "Value",
                id="tag-without-value",
            ),
            pytest.param(
                TransferOptions(copy_props=CopyPropsMode.ALL),
                [{}, {"TagSet": []}, KeyError("Credentials")],
                Boto3S3Error,
                "Credentials",
                id="listing-raises",
            ),
            pytest.param(
                TransferOptions(copy_props=CopyPropsMode.ALL),
                [{}, {"TagSet": []}, {"Annotations": [{"Size": 1}]}],
                MalformedResponseError,
                "AnnotationName",
                id="entry-without-name",
            ),
            pytest.param(
                TransferOptions(copy_props=CopyPropsMode.ALL),
                [
                    {},
                    {"TagSet": []},
                    {"Annotations": [{"AnnotationName": "n"}]},
                    KeyError("Credentials"),
                ],
                Boto3S3Error,
                "Credentials",
                id="annotation-raises",
            ),
            pytest.param(
                TransferOptions(copy_props=CopyPropsMode.ALL),
                [{}, {"TagSet": []}, {"Annotations": [{"AnnotationName": "n"}]}, {}],
                MalformedResponseError,
                "AnnotationPayload",
                id="reply-without-payload",
            ),
            pytest.param(
                TransferOptions(copy_props=CopyPropsMode.ALL),
                [
                    {},
                    {"TagSet": []},
                    {"Annotations": [{"AnnotationName": "n"}]},
                    {"AnnotationPayload": _BodyRaising(KeyError("Credentials"))},
                ],
                Boto3S3Error,
                "Credentials",
                id="body-read-raises",
            ),
        ],
    )
    def test_a_source_read_failure_names_the_source(
        self,
        options: TransferOptions,
        source_responses: list[dict[str, Any] | Exception],
        error_type: type[Boto3S3Error],
        text: str,
    ) -> None:
        # Whatever a source-side read raises - botocore's family or not - or
        # a reply missing an element aws-cli reads by subscript fails the item
        # against the *source*, not the copy destination _record_failure would
        # otherwise stamp on it; the message names the missing element or the
        # cause.
        _, _, results, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            [],
            source_responses=source_responses,
            options=options,
        )
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        error = results[0].error
        assert error is not None
        assert type(error) is error_type
        assert text in str(error)
        assert isinstance(error.__cause__, KeyError)
        assert (error.bucket, error.key) == ("src-b", "d/a.bin")

    def test_single_part_copy_excludes_annotations(self) -> None:
        # Every copy-props mode short of ALL sends AnnotationDirective=EXCLUDE
        # on the single-part CopyObject (the server default COPY would carry
        # annotations over otherwise).
        calls, _, _, _ = _run(
            TransferType.COPY,
            [self._item(size=7, head={})],
            [{}],
            source_responses=[{"TagSet": []}],
        )
        assert calls[0].operation == "CopyObject"
        assert calls[0].params["AnnotationDirective"] == "EXCLUDE"

    def test_all_single_part_copy_sends_no_annotation_directive(self) -> None:
        # copy_props=ALL rides the server-side COPY default: nothing on the wire.
        calls, _, _, _ = _run(
            TransferType.COPY,
            [self._item(size=7, head={})],
            [{}],
            source_responses=[{"TagSet": []}],
            options=TransferOptions(copy_props=CopyPropsMode.ALL),
        )
        assert calls[0].operation == "CopyObject"
        assert "AnnotationDirective" not in calls[0].params

    def test_all_multipart_default_preloads_then_writes_after_the_copy(self) -> None:
        # Reads are completed before submission; the writes follow the copy and
        # pin ObjectIfMatch / VersionId to the new object, as aws-cli's do.
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {"ETag": '"dest-etag"', "VersionId": "dest-v1"},
            {},  # PutObjectAnnotation
        ]
        source_responses: list[dict[str, Any] | Exception] = [
            {},  # HeadObject
            {"TagSet": []},
            {"Annotations": [{"AnnotationName": "ann1", "LastModified": MTIME, "Size": 7}]},
            {"AnnotationPayload": io.BytesIO(b"payload")},
        ]
        calls, source_calls, _, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            responses,
            source_responses=source_responses,
            options=TransferOptions(copy_props=CopyPropsMode.ALL),
        )
        assert "AnnotationDirective" not in calls[0].params
        assert ops(source_calls) == [
            "HeadObject",
            "GetObjectTagging",
            "ListObjectAnnotations",
            "GetObjectAnnotation",
        ]
        assert source_calls[2].params == {"Bucket": "src-b", "Key": "d/a.bin"}
        assert ops(calls)[-1] == "PutObjectAnnotation"
        assert calls[-1].params == {
            "Bucket": "dest-b",
            "Key": "cp/a.bin",
            "AnnotationName": "ann1",
            "AnnotationPayload": b"payload",
            "ObjectIfMatch": '"dest-etag"',
            "VersionId": "dest-v1",
        }
        assert transferrer.succeeded == 1

    def test_annotation_write_drops_the_copy_checksum_algorithm(self) -> None:
        # --checksum-algorithm rides the copy calls, but aws writes
        # annotations from its own subscriber and maps only RequestPayer onto
        # PutObjectAnnotation; so does `_SetAnnotations` (this pins the wire).
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {"ETag": '"dest-etag"', "VersionId": "dest-v1"},
            {},  # PutObjectAnnotation
        ]
        source_responses: list[dict[str, Any] | Exception] = [
            {},  # HeadObject
            {"TagSet": []},
            {"Annotations": [{"AnnotationName": "ann1", "LastModified": MTIME, "Size": 7}]},
            {"AnnotationPayload": io.BytesIO(b"payload")},
        ]
        calls, _, _, _ = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            responses,
            source_responses=source_responses,
            options=TransferOptions(copy_props=CopyPropsMode.ALL, checksum_algorithm="CRC32"),
        )
        assert calls[0].operation == "CreateMultipartUpload"
        assert calls[0].params["ChecksumAlgorithm"] == "CRC32"
        annotation_put = calls[-1]
        assert annotation_put.operation == "PutObjectAnnotation"
        assert "ChecksumAlgorithm" not in annotation_put.params

    def test_all_multipart_deferred_lists_every_annotation_page(self) -> None:
        # s3transfer's own post-complete read calls list_object_annotations
        # once and never follows NextContinuationToken; the deferred-mode
        # adapter paginates underneath so a multi-page source keeps its tail
        # (aws-cli collects the names with a paginator). Payload reads stay
        # lazy per-name pass-throughs.
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {"ETag": '"dest-etag"', "VersionId": "dest-v1"},
            {},  # PutObjectAnnotation ann1
            {},  # PutObjectAnnotation ann2
        ]
        source_responses: list[dict[str, Any] | Exception] = [
            {},  # HeadObject
            {"TagSet": []},
            {
                "Annotations": [{"AnnotationName": "ann1", "LastModified": MTIME, "Size": 7}],
                "NextContinuationToken": "next",
            },
            {"Annotations": [{"AnnotationName": "ann2", "LastModified": MTIME, "Size": 7}]},
            {"AnnotationPayload": io.BytesIO(b"one")},
            {"AnnotationPayload": io.BytesIO(b"two")},
        ]

        calls, source_calls, _, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            responses,
            source_responses=source_responses,
            options=TransferOptions(
                copy_props=CopyPropsMode.ALL,
                annotation_copy_mode=AnnotationCopyMode.DEFERRED,
            ),
        )

        assert ops(source_calls) == [
            "HeadObject",
            "GetObjectTagging",
            "ListObjectAnnotations",
            "ListObjectAnnotations",
            "GetObjectAnnotation",
            "GetObjectAnnotation",
        ]
        assert source_calls[3].params["ContinuationToken"] == "next"
        assert [call.params["AnnotationName"] for call in calls[-2:]] == ["ann1", "ann2"]
        assert transferrer.succeeded == 1

    def test_all_multipart_deferred_read_failure_happens_after_destination(self) -> None:
        calls, source_calls, results, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            [
                {"UploadId": "u"},
                {"CopyPartResult": {"ETag": '"p1"'}},
                {"CopyPartResult": {"ETag": '"p2"'}},
                {"ETag": '"dest-etag"'},
            ],
            source_responses=[
                {},
                {"TagSet": []},
                client_error("AccessDenied", 403, "ListObjectAnnotations"),
            ],
            options=TransferOptions(
                copy_props=CopyPropsMode.ALL,
                annotation_copy_mode=AnnotationCopyMode.DEFERRED,
            ),
        )

        assert ops(source_calls)[-1] == "ListObjectAnnotations"
        # The read runs after the copy completed, outside s3transfer's tasks:
        # the destination stays and nothing aborts the finished upload.
        assert ops(calls) == [
            "CreateMultipartUpload",
            "UploadPartCopy",
            "UploadPartCopy",
            "CompleteMultipartUpload",
        ]
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]

    def test_all_multipart_default_read_failure_leaves_no_destination(self) -> None:
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {"ETag": '"dest-etag"'},
            {},  # native-path failure cleanup AbortMultipartUpload
        ]
        source_responses: list[dict[str, Any] | Exception] = [
            {},  # HeadObject
            {"TagSet": []},
            client_error("AccessDenied", 403, "ListObjectAnnotations"),
        ]

        calls, source_calls, results, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            responses,
            source_responses=source_responses,
            options=TransferOptions(copy_props=CopyPropsMode.ALL),
        )

        assert ops(source_calls) == [
            "HeadObject",
            "GetObjectTagging",
            "ListObjectAnnotations",
        ]
        assert calls == []
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]

    def test_all_multipart_preload_lists_all_pages_before_reading_payloads(self) -> None:
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {"ETag": '"dest-etag"'},
            {},
            {},
        ]
        source_responses: list[dict[str, Any] | Exception] = [
            {"TagSet": []},
            {
                "Annotations": [{"AnnotationName": "ann1", "LastModified": MTIME, "Size": 7}],
                "NextContinuationToken": "next",
            },
            {"Annotations": [{"AnnotationName": "ann2", "LastModified": MTIME, "Size": 7}]},
            {"AnnotationPayload": io.BytesIO(b"one")},
            {"AnnotationPayload": io.BytesIO(b"two")},
        ]

        calls, source_calls, _, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB, head={"VersionId": "src-v1"})],
            responses,
            source_responses=source_responses,
            options=TransferOptions(
                copy_props=CopyPropsMode.ALL,
                request_payer="requester",
            ),
        )

        assert ops(source_calls) == [
            "GetObjectTagging",
            "ListObjectAnnotations",
            "ListObjectAnnotations",
            "GetObjectAnnotation",
            "GetObjectAnnotation",
        ]
        assert source_calls[1].params == {
            "Bucket": "src-b",
            "Key": "d/a.bin",
            "RequestPayer": "requester",
            "VersionId": "src-v1",
        }
        assert source_calls[2].params["ContinuationToken"] == "next"
        assert source_calls[3].params["AnnotationName"] == "ann1"
        assert source_calls[4].params["AnnotationName"] == "ann2"
        assert [call.params["AnnotationPayload"] for call in calls[-2:]] == [b"one", b"two"]
        assert transferrer.succeeded == 1

    def test_all_multipart_tempfile_mode_cleans_its_configured_directory(
        self, tmp_path: Path
    ) -> None:
        calls, _, _, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB, head={})],
            [
                {"UploadId": "u"},
                {"CopyPartResult": {"ETag": '"p1"'}},
                {"CopyPartResult": {"ETag": '"p2"'}},
                {"ETag": '"dest-etag"'},
                {},
                {},
            ],
            source_responses=[
                {"TagSet": []},
                {
                    "Annotations": [
                        {"AnnotationName": "ann1", "LastModified": MTIME, "Size": 7},
                        {"AnnotationName": "ann2", "LastModified": MTIME, "Size": 7},
                    ]
                },
                {"AnnotationPayload": io.BytesIO(b"first")},
                {"AnnotationPayload": io.BytesIO(b"second-longer")},
            ],
            options=TransferOptions(
                copy_props=CopyPropsMode.ALL,
                annotation_copy_mode=AnnotationCopyMode.PRELOAD_TEMPFILE,
            ),
            config=LibraryTransferConfig(use_threads=False, annotation_temp_dir=tmp_path),
        )

        assert [call.params["AnnotationPayload"] for call in calls[-2:]] == [
            b"first",
            b"second-longer",
        ]
        assert list(tmp_path.iterdir()) == []
        assert transferrer.succeeded == 1

    def test_all_multipart_tempfile_creation_failure_leaves_no_destination(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "missing"
        calls, source_calls, results, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            [],
            source_responses=[{}, {"TagSet": []}],
            options=TransferOptions(
                copy_props=CopyPropsMode.ALL,
                annotation_copy_mode=AnnotationCopyMode.PRELOAD_TEMPFILE,
            ),
            config=LibraryTransferConfig(use_threads=False, annotation_temp_dir=missing),
        )

        assert ops(source_calls) == ["HeadObject", "GetObjectTagging"]
        assert calls == []
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]

    def test_all_multipart_tempfile_read_failure_closes_the_file(self, tmp_path: Path) -> None:
        calls, _, results, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB, head={})],
            [],
            source_responses=[
                {"TagSet": []},
                {"Annotations": [{"AnnotationName": "ann1", "LastModified": MTIME, "Size": 7}]},
                client_error("AccessDenied", 403, "GetObjectAnnotation"),
            ],
            options=TransferOptions(
                copy_props=CopyPropsMode.ALL,
                annotation_copy_mode=AnnotationCopyMode.PRELOAD_TEMPFILE,
            ),
            config=LibraryTransferConfig(use_threads=False, annotation_temp_dir=tmp_path),
        )

        assert calls == []
        assert list(tmp_path.iterdir()) == []
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]

    def test_oversized_tags_apply_after_the_copy(self) -> None:
        big = "v" * 3000
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {},
            {},  # PutObjectTagging
        ]
        source_responses: list[dict[str, Any] | Exception] = [
            {},
            {"TagSet": [{"Key": "k", "Value": big}]},
        ]
        calls, _, results, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            responses,
            source_responses=source_responses,
        )
        assert ops(calls)[-1] == "PutObjectTagging"
        assert calls[-1].params["Tagging"] == {"TagSet": [{"Key": "k", "Value": big}]}
        assert "Tagging" not in calls[0].params
        assert transferrer.succeeded == 1
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]

    def _all_with_oversized_tags(
        self, dest_tail: list[dict[str, Any] | BaseException]
    ) -> tuple[list[ApiCall], list[OpResult], Transferrer]:
        """A `copy_props=ALL` multipart copy whose tags do not fit the create
        header and which carries two annotations; ``dest_tail`` is what the
        destination answers after CompleteMultipartUpload.

        aws-cli registers its tags subscriber ahead of its annotations one, so
        after the copy the tagging write goes out first and the annotation
        writes follow - unless the tagging failure has already settled the
        future.
        """
        big = "v" * 3000
        responses: list[dict[str, Any] | BaseException] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {"ETag": '"dest-etag"'},
            *dest_tail,
        ]
        source_responses: list[dict[str, Any] | BaseException] = [
            {},
            {"TagSet": [{"Key": "k", "Value": big}]},
            {
                "Annotations": [
                    {"AnnotationName": "ann1", "LastModified": MTIME, "Size": 1},
                    {"AnnotationName": "ann2", "LastModified": MTIME, "Size": 1},
                ]
            },
            {"AnnotationPayload": io.BytesIO(b"1")},
            {"AnnotationPayload": io.BytesIO(b"2")},
        ]
        calls, _, results, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            responses,
            source_responses=source_responses,
            options=TransferOptions(copy_props=CopyPropsMode.ALL),
        )
        return calls, results, transferrer

    def test_the_completion_response_is_kept_only_while_annotations_need_it(self) -> None:
        # Taken by the annotations subscriber on the item's terminal; a run
        # whose metadata_directive disables the chain keeps none at all.
        _, _, transferrer = self._all_with_oversized_tags([{}, {}, {}])
        completions = transferrer._completions  # pyright: ignore[reportPrivateUsage]
        assert completions is not None
        assert completions._responses == {}  # pyright: ignore[reportPrivateUsage]
        client, _ = make_recording_client([])
        bypassed = Transferrer(
            TransferType.COPY,
            client,
            options=TransferOptions(copy_props=CopyPropsMode.ALL, metadata_directive="REPLACE"),
        )
        assert bypassed._completions is None  # pyright: ignore[reportPrivateUsage]

    def test_the_tagging_write_goes_out_before_the_annotation_writes(self) -> None:
        calls, results, transferrer = self._all_with_oversized_tags([{}, {}, {}])
        assert ops(calls)[-4:] == [
            "CompleteMultipartUpload",
            "PutObjectTagging",
            "PutObjectAnnotation",
            "PutObjectAnnotation",
        ]
        assert transferrer.succeeded == 1
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]

    def test_a_partial_annotation_failure_keeps_the_tagged_object(self) -> None:
        # The tags are already on; ann2's write is denied: the object is kept
        # (no rollback, no abort of the completed upload) and the failure names
        # the annotation that was not written.
        calls, results, transferrer = self._all_with_oversized_tags(
            [{}, {}, client_error("AccessDenied", 403, "PutObjectAnnotation")]
        )
        assert ops(calls)[-3:] == ["PutObjectTagging", "PutObjectAnnotation", "PutObjectAnnotation"]
        assert "DeleteObject" not in ops(calls)
        assert "AbortMultipartUpload" not in ops(calls)
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert "ann2" in str(results[0].error)

    @pytest.mark.parametrize(
        "rollback",
        [{}, client_error("AccessDenied", 403, "DeleteObject")],
        ids=["rollback-deletes", "rollback-fails"],
    )
    def test_a_tagging_failure_fails_before_any_annotation_is_written(
        self, rollback: dict[str, Any] | BaseException
    ) -> None:
        # A rollback delete that fails too still leaves the item failed with
        # the tagging error and no annotation written (aws-cli's future stays
        # a success there, and it writes the annotations and exits 0).
        calls, results, _ = self._all_with_oversized_tags(
            [client_error("AccessDenied", 403, "PutObjectTagging"), rollback, {}, {}]
        )
        assert ops(calls)[-3:] == ["CompleteMultipartUpload", "PutObjectTagging", "DeleteObject"]
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert isinstance(results[0].error, AccessDeniedError)
        assert "PutObjectTagging" in str(results[0].error)

    def test_post_copy_tagging_failure_rolls_back_the_destination(self) -> None:
        big = "v" * 3000
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {},
            client_error("AccessDenied", 403, "PutObjectTagging"),
            {},  # rollback DeleteObject
        ]
        source_responses: list[dict[str, Any] | Exception] = [
            {},
            {"TagSet": [{"Key": "k", "Value": big}]},
        ]
        calls, _, results, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            responses,
            source_responses=source_responses,
        )
        assert ops(calls)[-2:] == ["PutObjectTagging", "DeleteObject"]
        assert calls[-1].params == {"Bucket": "dest-b", "Key": "cp/a.bin"}
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]


class TestNonTransferOutcomes:
    def test_dryrun_emits_without_building_a_manager(self) -> None:
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        item = TransferItem(compare_key="a", src_display="a", dest_display="s3://b/a", size=1)
        with Transferrer(TransferType.UPLOAD, client, on_result=results.append) as transferrer:
            transferrer.dryrun(item)
        assert calls == []
        assert transferrer._manager is None  # pyright: ignore[reportPrivateUsage]
        assert [result.outcome for result in results] == [OpOutcome.DRYRUN]
        assert (results[0].src, results[0].dest) == ("a", "s3://b/a")

    def test_warn_and_skip_count_into_the_rollup(self) -> None:
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        with Transferrer(TransferType.DOWNLOAD, client, on_result=results.append) as transferrer:
            transferrer.warn("Skipping file s3://b/k. Object is of storage class GLACIER.", key="k")
            transferrer.skip(TransferItem(compare_key="k2"))
        assert (transferrer.warned, transferrer.skipped) == (1, 1)
        assert [result.outcome for result in results] == [OpOutcome.WARNED, OpOutcome.SKIPPED]
        assert results[0].transfer_type is TransferType.DOWNLOAD

    def test_warner_sink_is_exposed_and_counts_into_the_rollup(self) -> None:
        # A backend walk's ScanOptions.on_warning targets transferrer.warner.warn
        # (the shared sink) directly, not a Transferrer method; it still emits a
        # WARNED record and counts into the rollup exposed on the Transferrer.
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        with Transferrer(TransferType.UPLOAD, client, on_result=results.append) as transferrer:
            transferrer.warner.warn("Skipping file /tmp/x. File does not exist.", key="x")
        assert transferrer.warned == 1
        assert [result.outcome for result in results] == [OpOutcome.WARNED]
        assert results[0].transfer_type is TransferType.UPLOAD

    def test_progress_events_accumulate(self, tmp_path: Path) -> None:
        # Driven through a download: the canned GetObject Body is genuinely
        # read by s3transfer's I/O loop, which is what fires progress deltas
        # (an upload under the recorder never reads its Body).
        progress: list[TransferProgress] = []
        client, _ = make_recording_client(
            [{"Body": io.BytesIO(b"x" * 1000), "ContentLength": 1000, "ETag": '"e"'}]
        )
        item = TransferItem(
            compare_key="a.bin",
            size=1000,
            etag="e",
            src_bucket="b",
            src_key="k",
            dest_path=str(tmp_path / "a.bin"),
        )
        with Transferrer(
            TransferType.DOWNLOAD, client, transfer_config=_SYNC_CONFIG, on_progress=progress.append
        ) as transferrer:
            transferrer.submit(item)
        assert progress[0].bytes_done == 0  # queued notification
        assert progress[-1].bytes_done == 1000
        assert all(p.bytes_total == 1000 for p in progress)


class TestCancellationTypeOnAFuture:
    """`transfer._is_cancellation`: the two cancellation types mean "revoked"
    only when somebody revoked. The classic coordinator says so itself; where
    there is no such word (the CRT engine, a submit-time failure) only this
    run's cancel token having ordered a cancel does."""

    @pytest.mark.parametrize(
        "exc",
        [
            concurrent.futures.CancelledError("thrown"),
            CancelledError("thrown"),
        ],
        ids=["engine-type", "library-type"],
    )
    @pytest.mark.parametrize(
        ("revoked", "cancel_initiated", "expected"),
        [
            (True, False, True),
            (False, False, False),
            (False, True, False),
            (None, False, False),
            (None, True, True),
        ],
    )
    def test_the_type_alone_never_decides(
        self, exc: BaseException, revoked: bool | None, cancel_initiated: bool, expected: bool
    ) -> None:
        judged = transfer._is_cancellation(  # pyright: ignore[reportPrivateUsage]
            exc, cancel_initiated=cancel_initiated, revoked=revoked
        )
        assert judged is expected


class _CrtCancelError(Exception):
    """awscrt's cancellation outcome: matched by ``name``, awscrt never imported."""

    name = "AWS_ERROR_S3_CANCELED"


class _FakeCrtMeta:
    size = None
    etag = None

    def __init__(self) -> None:
        self.user_context: dict[str, Any] = {}


class _FakeCrtFuture:
    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.meta = _FakeCrtMeta()

    def done(self) -> bool:
        return True

    def result(self) -> None:
        raise self._error

    def cancel(self) -> None:
        pass


class _CrtDrainManager:
    """The CRT manager's drain surface: every accepted transfer settles with
    awscrt's cancellation outcome and no interrupt ever escapes - the shape
    pip s3transfer's ``CRTTransferManager`` presents when a Ctrl-C lands in
    its drain (its coordinator converts the interrupt into ``cancel()`` and
    its ``_shutdown`` discards the replacement error too)."""

    def __init__(self) -> None:
        self._accepted: list[tuple[_FakeCrtFuture, list[Any]]] = []

    def upload(self, **kwargs: Any) -> _FakeCrtFuture:
        future = _FakeCrtFuture(
            _CrtCancelError("AWS_ERROR_S3_CANCELED: Request successfully cancelled")
        )
        self._accepted.append((future, list(kwargs["subscribers"])))
        return future

    def _drain(self) -> None:
        for future, subscribers in self._accepted:
            for subscriber in subscribers:
                on_done = getattr(subscriber, "on_done", None)
                if on_done is not None:
                    on_done(future)

    def shutdown(self) -> None:
        self._drain()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._drain()


class _FakeCrtSuccessFuture(_FakeCrtFuture):
    def result(self) -> None:
        return None


class _CrtCapturingManager(_CrtDrainManager):
    """A CRT stand-in that records each upload's kwargs and settles it as a success."""

    def __init__(self) -> None:
        super().__init__()
        self.uploads: list[dict[str, Any]] = []

    def upload(self, **kwargs: Any) -> _FakeCrtFuture:
        self.uploads.append(kwargs)
        future = _FakeCrtSuccessFuture(RuntimeError("unused"))
        self._accepted.append((future, list(kwargs["subscribers"])))
        return future


class TestCrtCancelClassification:
    """A CRT cancellation this run ordered - its cancel token, a fatal or a
    Ctrl-C folding the manager - revokes the item as CANCELLED, as on the
    classic engine (design/crt.md section 6); aws-cli prints a failed line per
    such item instead. A CRT cancellation nobody ordered stays a failure.

    Driven with a stand-in manager because the real CRT stack needs awscrt's
    event loop; the stand-in reproduces exactly the seam under test - futures
    settling with the ``AWS_ERROR_S3_CANCELED`` outcome during the drain.
    """

    def _submit_one(self, transferrer: Transferrer, tmp_path: Path) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        transferrer.submit(
            TransferItem(
                compare_key="a.bin",
                size=1,
                src_path=str(src),
                dest_bucket="b",
                dest_key="a.bin",
            )
        )

    def test_uninitiated_cancellations_are_failures(self, tmp_path: Path) -> None:
        # Nobody here ordered a cancel, yet the drain settled the item
        # cancelled: a failure, so a run cut short by something the library
        # cannot see is never reported as a success.
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        with Transferrer(TransferType.UPLOAD, client, on_result=results.append) as transferrer:
            transferrer._manager = _CrtDrainManager()  # pyright: ignore[reportPrivateUsage]
            self._submit_one(transferrer, tmp_path)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert "AWS_ERROR_S3_CANCELED" in str(results[0].error)
        assert (transferrer.failed, transferrer.cancelled) == (1, 0)

    def test_fatal_ordered_cancellations_are_cancelled(self, tmp_path: Path) -> None:
        # The fatal path orders the cancel (Transferrer.__exit__ under the
        # exception): each folded in-flight transfer is revoked, as on the
        # classic engine. aws-cli prints "upload failed: ...
        # AWS_ERROR_S3_CANCELED" per item there (measured), not copied.
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        transferrer = Transferrer(TransferType.UPLOAD, client, on_result=results.append)
        with pytest.raises(RuntimeError, match="fatal elsewhere"):
            with transferrer:
                transferrer._manager = _CrtDrainManager()  # pyright: ignore[reportPrivateUsage]
                self._submit_one(transferrer, tmp_path)
                raise RuntimeError("fatal elsewhere")
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert "AWS_ERROR_S3_CANCELED" in str(results[0].error)
        assert (transferrer.failed, transferrer.cancelled) == (0, 1)
        assert transferrer.first_error is None

    def test_token_ordered_cancellations_stay_cancelled(self, tmp_path: Path) -> None:
        # The cancel-token escalation (`_cancel_futures`, as the drain-time
        # watcher runs it): an ordered revocation, CANCELLED, outside the
        # failure counts - the on_result contract (design/opresult.md).
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        with Transferrer(TransferType.UPLOAD, client, on_result=results.append) as transferrer:
            transferrer._manager = _CrtDrainManager()  # pyright: ignore[reportPrivateUsage]
            self._submit_one(transferrer, tmp_path)
            transferrer._cancel_futures()  # pyright: ignore[reportPrivateUsage]
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert "AWS_ERROR_S3_CANCELED" in str(results[0].error)
        assert (transferrer.failed, transferrer.cancelled) == (0, 1)

    def test_token_cancel_caught_mid_submission_stays_cancelled(self, tmp_path: Path) -> None:
        # An immediate token cancel surfacing in the submission loop reaches
        # __exit__ as the CancelledError that folds the manager
        # (s3._raise_if_cancelled): the in-flight item revokes CANCELLED
        # exactly like the drain-time escalation above.
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        token = CancelToken()
        transferrer = Transferrer(
            TransferType.UPLOAD, client, on_result=results.append, cancel_token=token
        )
        with pytest.raises(CancelledError):
            with transferrer:
                transferrer._manager = _CrtDrainManager()  # pyright: ignore[reportPrivateUsage]
                self._submit_one(transferrer, tmp_path)
                token.cancel(mode=CancelMode.IMMEDIATE)
                raise CancelledError("cp was cancelled", operation="cp")
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert "AWS_ERROR_S3_CANCELED" in str(results[0].error)
        assert (transferrer.failed, transferrer.cancelled) == (0, 1)

    def test_interrupt_folded_cancellations_are_cancelled(self, tmp_path: Path) -> None:
        # A Ctrl-C forcing the fold revokes the in-flight items like a fatal;
        # the interrupt itself propagates.
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        transferrer = Transferrer(TransferType.UPLOAD, client, on_result=results.append)
        with pytest.raises(KeyboardInterrupt):
            with transferrer:
                transferrer._manager = _CrtDrainManager()  # pyright: ignore[reportPrivateUsage]
                self._submit_one(transferrer, tmp_path)
                raise KeyboardInterrupt
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert "AWS_ERROR_S3_CANCELED" in str(results[0].error)
        assert (transferrer.failed, transferrer.cancelled) == (0, 1)


class _CrtWait:
    """One request's awscrt ``finished_future``, as the drain waits on it.

    ``state`` is ``running`` until the request finishes ``ok`` or
    ``cancelled``, which runs the done callbacks the way the CRT client does
    (``settle``). A wait marked ``interrupt`` is where the Ctrl-C lands.
    """

    def __init__(self, *, interrupt: bool) -> None:
        self.interrupt = interrupt
        self.state = "running"
        self.settle: Callable[[], None] = lambda: None

    def done(self) -> bool:
        return self.state != "running"

    def result(self, timeout: float | None = None) -> None:
        if self.state == "cancelled":
            raise _CrtCancelError("AWS_ERROR_S3_CANCELED: Request successfully cancelled")
        if self.state == "ok":
            return
        if self.interrupt:
            self.interrupt = False
            raise KeyboardInterrupt
        raise AssertionError("the drain waited on a request that never finishes")

    def finish(self, state: str) -> None:
        self.state = state
        self.settle()


class _CrtRequest:
    """An awscrt request whose cancel settles it - cancelled, or ``ok`` when
    it had already completed (``completes``) by the time the cancel arrived."""

    def __init__(self, wait: _CrtWait, *, completes: bool) -> None:
        self.finished_future = wait
        self._completes = completes

    def cancel(self) -> None:
        if self.finished_future.state == "running":
            self.finished_future.finish("ok" if self._completes else "cancelled")


def _swallowing_crt_manager(*requests: dict[str, bool]) -> Any:
    """pip s3transfer's own CRT manager and coordinators over fake requests.

    The drain (``shutdown`` / ``_shutdown``) and the coordinator's
    ``result`` / ``cancel`` are s3transfer's, so a Ctrl-C landing in the
    drain's wait is swallowed exactly as pip s3transfer swallows it; only the
    awscrt requests underneath are stand-ins. Each upload takes the next of
    ``requests``: ``interrupt`` (the drain's wait on it is interrupted),
    ``completes`` (a cancel finds it already complete), ``done`` (finished
    before the drain).
    """
    pytest.importorskip("awscrt")
    from s3transfer.crt import CRTTransferCoordinator, CRTTransferFuture, CRTTransferManager

    specs = list(requests)

    class Manager(CRTTransferManager):
        def upload(
            self,
            fileobj: Any,
            bucket: Any,
            key: Any,
            extra_args: Any = None,
            subscribers: Any = None,
        ) -> Any:
            spec = specs.pop(0)
            coordinator = CRTTransferCoordinator(transfer_id=len(self._future_coordinators))
            future = CRTTransferFuture(meta=cast(Any, _FakeCrtMeta()), coordinator=coordinator)
            wait = _CrtWait(interrupt=spec.get("interrupt", False))

            def settle() -> None:
                for subscriber in subscribers or ():
                    on_done = getattr(subscriber, "on_done", None)
                    if on_done is not None:
                        on_done(future)
                coordinator.set_done_callbacks_complete()

            wait.settle = settle
            request = _CrtRequest(wait, completes=spec.get("completes", False))
            coordinator.set_s3_request(cast(Any, request))
            self._future_coordinators.append(coordinator)
            if spec.get("done", False):
                wait.finish("ok")
            return future

    manager = object.__new__(Manager)
    manager._future_coordinators = []  # pyright: ignore[reportAttributeAccessIssue]
    return manager


class TestCrtSwallowedInterrupt:
    """A Ctrl-C landing in pip s3transfer's CRT drain is swallowed there - the
    coordinator turns it into its own ``cancel()`` and the manager's shutdown
    drops what follows. Here what it cancelled is CANCELLED, as on the
    classic engine, and the interrupt reaches the caller once the drain is
    over (design/crt.md section 6)."""

    def _run(self, tmp_path: Path, manager: Any) -> list[OpResult]:
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        transferrer = Transferrer(TransferType.UPLOAD, client, on_result=results.append)
        with pytest.raises(KeyboardInterrupt):
            with transferrer:
                transferrer._manager = manager  # pyright: ignore[reportPrivateUsage]
                for name in ("a.bin", "b.bin"):
                    src = tmp_path / name
                    src.write_bytes(b"x")
                    transferrer.submit(
                        TransferItem(
                            compare_key=name,
                            size=1,
                            src_path=str(src),
                            dest_bucket="b",
                            dest_key=name,
                        )
                    )
        assert transferrer.failed == 0
        return results

    def test_the_cancelled_transfer_is_cancelled_and_the_interrupt_raised(
        self, tmp_path: Path
    ) -> None:
        # The coordinator cancels the transfer it was waiting on, and its
        # cancellation error replaces the interrupt; the manager drops that.
        manager = _swallowing_crt_manager({"interrupt": True}, {"done": True})
        results = self._run(tmp_path, manager)
        assert sorted((r.compare_key, r.outcome) for r in results) == [
            ("a.bin", OpOutcome.CANCELLED),
            ("b.bin", OpOutcome.SUCCEEDED),
        ]

    def test_a_transfer_complete_before_the_cancel_keeps_its_outcome(self, tmp_path: Path) -> None:
        # The waited-on transfer had already completed, so the coordinator
        # re-raises the interrupt and the manager's shutdown catches it,
        # cancels every transfer still running, and drops it there instead.
        manager = _swallowing_crt_manager({"interrupt": True, "completes": True}, {"done": True})
        results = self._run(tmp_path, manager)
        assert sorted((r.compare_key, r.outcome) for r in results) == [
            ("a.bin", OpOutcome.SUCCEEDED),
            ("b.bin", OpOutcome.SUCCEEDED),
        ]

    def test_a_cancel_without_an_interrupt_passes_through(self, tmp_path: Path) -> None:
        # The guard reads the interrupt being handled; a cancel the run's
        # token issues has none, and nothing is kept to raise.
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        with Transferrer(TransferType.UPLOAD, client, on_result=results.append) as transferrer:
            transferrer._manager = _swallowing_crt_manager({})  # pyright: ignore[reportPrivateUsage]
            src = tmp_path / "a.bin"
            src.write_bytes(b"x")
            transferrer.submit(
                TransferItem(
                    compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="a"
                )
            )
            transferrer._cancel_futures()  # pyright: ignore[reportPrivateUsage]
        assert [(r.compare_key, r.outcome) for r in results] == [("a.bin", OpOutcome.CANCELLED)]


class TestFatalCancellation:
    """A fatal escaping the submission loop cancels the accepted transfers
    (aws's manager-context behavior, measured live: a mid-listing fatal
    leaves every queued transfer unrun), and on the classic lane each revoked
    item reports one CANCELLED record naming the fatal (design/opresult.md;
    the CRT lane's in TestCrtCancelClassification)."""

    @pytest.mark.parametrize(
        ("raise_exc", "raises", "match", "revoked_text"),
        [
            pytest.param(
                lambda: NotFoundError("listing died mid-enumeration"),
                NotFoundError,
                "listing died",
                "listing died mid-enumeration",
                id="fatal",
            ),
            pytest.param(
                # An interrupt escaping the submission loop folds the manager
                # the same way: the classic manager revokes the queued work
                # with CancelledError (str(KI) is empty, so s3transfer's repr
                # fallback names it) and the interrupt propagates - CANCELLED
                # records, as on the CRT lane for the same window.
                KeyboardInterrupt,
                KeyboardInterrupt,
                None,
                "KeyboardInterrupt()",
                id="interrupt",
            ),
        ],
    )
    def test_fatal_mid_enumeration_cancels_queued_transfers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        raise_exc: Any,
        raises: type[BaseException],
        match: str | None,
        revoked_text: str,
    ) -> None:
        from s3transfer.manager import TransferCoordinatorController

        names = ("a.bin", "b.bin", "c.bin")
        for name in names:
            (tmp_path / name).write_bytes(b"x")
        items = [
            TransferItem(
                compare_key=name,
                size=1,
                src_path=str(tmp_path / name),
                dest_bucket="b",
                dest_key=f"p/{name}",
                src_display=str(tmp_path / name),
                dest_display=f"s3://b/p/{name}",
            )
            for name in names
        ]
        client, _ = make_recording_client([])
        api_calls: list[str] = []
        first_started = threading.Event()
        release_first = threading.Event()

        def api_call(operation: str, params: dict[str, Any]) -> dict[str, Any]:
            api_calls.append(f"{operation}:{params['Key']}")
            if len(api_calls) == 1:
                first_started.set()
                assert release_first.wait(5.0)
            return {}

        client._make_api_call = api_call  # type: ignore[method-assign]

        # Release the blocked in-flight request only once the shutdown's
        # cancel has revoked every pending coordinator, so the "queued items
        # never reach the API" assertion cannot race the worker. The in-flight
        # one still finishes - a running request cannot be interrupted - and
        # reports SUCCEEDED: s3transfer lets a completing task overwrite the
        # mid-flight cancel mark (the bytes really landed; aws's engine does
        # the same, its printer just never shows it).
        original_cancel = TransferCoordinatorController.cancel

        def cancel(self: Any, *args: Any, **kwargs: Any) -> None:
            original_cancel(self, *args, **kwargs)
            release_first.set()

        monkeypatch.setattr(TransferCoordinatorController, "cancel", cancel)

        results: list[OpResult] = []
        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            transfer_config=TransferConfig(max_concurrency=1),
            on_result=results.append,
        )
        with pytest.raises(raises, match=match):
            with transferrer:
                for item in items:
                    transferrer.submit(item)
                # Let the first request actually start before the fatal: the
                # shape under test is "in-flight finishes, queued revoked" -
                # without this the fatal can also beat the worker and revoke
                # all three (a legal but different outcome).
                assert first_started.wait(5.0)
                raise raise_exc()

        # Only the already-running first transfer reached the API; the queued
        # two were revoked before ever becoming requests (aws's transfer set:
        # its uploads in flight at the fatal land on S3 the same way). Record
        # order is not part of the contract - a revoked item's record fires
        # from the cancelling thread or the executor's skip path, whichever
        # its submission had reached.
        assert len(api_calls) == 1
        by_key = {result.compare_key: result for result in results}
        assert len(results) == 3 and set(by_key) == {"a.bin", "b.bin", "c.bin"}
        assert by_key["a.bin"].outcome is OpOutcome.SUCCEEDED
        for key in ("b.bin", "c.bin"):
            result = by_key[key]
            assert result.outcome is OpOutcome.CANCELLED
            assert isinstance(result.error, CancelledError)
            assert revoked_text in str(result.error)
        assert (transferrer.succeeded, transferrer.failed, transferrer.cancelled) == (1, 0, 2)
        # first_error stays reserved for real failures (BatchError's sample).
        assert transferrer.first_error is None


class _InterruptingBody:
    """A GetObject body whose read past ``after`` chunks is a Ctrl-C landing
    mid-stream on the calling thread (the use_threads=False lane)."""

    def __init__(self, data: bytes, *, after: int) -> None:
        self._buf = io.BytesIO(data)
        self._after = after
        self._reads = 0

    def read(self, amount: int = -1) -> bytes:
        self._reads += 1
        if self._reads > self._after:
            raise KeyboardInterrupt
        return self._buf.read(amount)


class TestSerialExecutorInterrupt:
    """``use_threads=False`` runs every task on the calling thread, so a
    Ctrl-C lands inside the request itself; upstream's serial executor would
    then run the done callbacks on a coordinator nobody settled (result()
    returns None) and swallow the interrupt (`transfer._SerialExecutor`).
    Pinned here: the item records CANCELLED, its source survives, upstream's
    failure cleanups run, and the interrupt propagates out of submit."""

    def _upload_item(self, tmp_path: Path, size: int = 7) -> TransferItem:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x" * size)
        return TransferItem(
            compare_key="a.bin",
            size=size,
            src_path=str(src),
            src_info=FileInfo(key=str(src).replace(os.sep, "/")),
            dest_bucket="b",
            dest_key="k",
        )

    def _run_interrupted(
        self,
        kind: TransferType,
        item: TransferItem,
        responses: list[dict[str, Any] | BaseException],
        *,
        is_move: bool,
        on_result: Callable[[OpResult], None],
    ) -> tuple[list[ApiCall], Transferrer]:
        client, calls = make_recording_client(responses)
        transferrer = Transferrer(
            kind,
            client,
            src_storage=LocalStorage(".") if kind is TransferType.UPLOAD else None,
            transfer_config=_SYNC_CONFIG,
            is_move=is_move,
            on_result=on_result,
        )
        with pytest.raises(KeyboardInterrupt), transferrer:
            transferrer.submit(item)
        return calls, transferrer

    def test_upload_move_keeps_the_source_and_records_cancelled(self, tmp_path: Path) -> None:
        item = self._upload_item(tmp_path)
        results: list[OpResult] = []
        calls, transferrer = self._run_interrupted(
            TransferType.UPLOAD, item, [KeyboardInterrupt()], is_move=True, on_result=results.append
        )
        assert ops(calls) == ["PutObject"]
        assert (tmp_path / "a.bin").exists()
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert (transferrer.succeeded, transferrer.cancelled) == (0, 1)

    def test_multipart_upload_aborts_and_records_cancelled(self, tmp_path: Path) -> None:
        # The interrupt lands in a non-final task (the first UploadPart): the
        # coordinator is cancelled before upstream's teardown, so its failure
        # cleanup aborts the multipart upload and the item still gets its one
        # record instead of leaking the interrupt from the done callbacks.
        item = self._upload_item(tmp_path, size=9 * _MIB)
        results: list[OpResult] = []
        calls, transferrer = self._run_interrupted(
            TransferType.UPLOAD,
            item,
            [{"UploadId": "u"}, KeyboardInterrupt(), {}],
            is_move=True,
            on_result=results.append,
        )
        assert ops(calls) == ["CreateMultipartUpload", "UploadPart", "AbortMultipartUpload"]
        assert (tmp_path / "a.bin").exists()
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert transferrer.cancelled == 1

    def test_download_move_mid_body_leaves_no_file_and_keeps_the_object(
        self, tmp_path: Path
    ) -> None:
        # Upstream registers the final rename as the GetObject task's done
        # callback, so an unsettled coordinator would rename the half-written
        # temp file into place and then delete the S3 source; cancelled first,
        # the rename is skipped, the temp file removed, the source untouched.
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            src_bucket="bucket",
            src_key="d/a.bin",
            dest_path=str(tmp_path / "out" / "a.bin"),
        )
        results: list[OpResult] = []
        calls, transferrer = self._run_interrupted(
            TransferType.DOWNLOAD,
            item,
            [{"Body": _InterruptingBody(b"payload", after=1), "ContentLength": 7}],
            is_move=True,
            on_result=results.append,
        )
        assert ops(calls) == ["GetObject"]  # no DeleteObject
        assert list((tmp_path / "out").iterdir()) == []  # neither the file nor a temp file
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert transferrer.cancelled == 1

    def test_interrupt_inside_the_source_delete_records_cancelled(self, tmp_path: Path) -> None:
        # Past the transfer, inside mv's DeleteObject: whether the delete
        # reached the service is unknown, so the item is not a completed move.
        # It was recorded SUCCEEDED with the source still there - the once
        # guard kept upstream's second announce from sending the delete again.
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            src_bucket="bucket",
            src_key="d/a.bin",
            dest_path=str(tmp_path / "out" / "a.bin"),
        )
        results: list[OpResult] = []
        calls, transferrer = self._run_interrupted(
            TransferType.DOWNLOAD,
            item,
            [{"Body": io.BytesIO(b"payload"), "ContentLength": 7}, KeyboardInterrupt()],
            is_move=True,
            on_result=results.append,
        )
        assert ops(calls) == ["GetObject", "DeleteObject"]  # sent once, not re-sent
        assert (tmp_path / "out" / "a.bin").read_bytes() == b"payload"
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert (transferrer.succeeded, transferrer.cancelled) == (0, 1)

    def _move_with_post_copy_writes(
        self,
        dest_tail: list[dict[str, Any] | BaseException],
        source_tail: list[dict[str, Any] | BaseException],
        *,
        on_result: Callable[[OpResult], None],
        annotation_copy_mode: AnnotationCopyMode | None = None,
    ) -> tuple[list[ApiCall], list[ApiCall], Transferrer]:
        """A ``mv`` multipart copy under ``copy_props=ALL`` whose tag set only
        fits a post-copy write; the tails are what each side answers after
        the copy completes and after the tag read."""
        options = TransferOptions(copy_props=CopyPropsMode.ALL)
        if annotation_copy_mode is not None:
            options["annotation_copy_mode"] = annotation_copy_mode
        client, calls = make_recording_client(
            [
                {"UploadId": "u"},
                {"CopyPartResult": {"ETag": '"p1"'}},
                {"CopyPartResult": {"ETag": '"p2"'}},
                {"ETag": '"dest-etag"'},
                *dest_tail,
            ]
        )
        source_client, source_calls = make_recording_client(
            [{}, {"TagSet": [{"Key": "k", "Value": "v" * 3000}]}, *source_tail]
        )
        transferrer = Transferrer(
            TransferType.COPY,
            client,
            source_client=source_client,
            transfer_config=_SYNC_CONFIG,
            options=options,
            is_move=True,
            on_result=on_result,
        )
        item = TransferItem(
            compare_key="a.bin",
            size=9 * _MIB,
            etag='"abc123"',
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )
        with pytest.raises(KeyboardInterrupt), transferrer:
            transferrer.submit(item)
        return calls, source_calls, transferrer

    @staticmethod
    def _fresh_annotations() -> list[dict[str, Any] | BaseException]:
        """The source's two annotations, read when the copy is queued."""
        return [
            {
                "Annotations": [
                    {"AnnotationName": "ann1", "LastModified": MTIME, "Size": 1},
                    {"AnnotationName": "ann2", "LastModified": MTIME, "Size": 1},
                ]
            },
            {"AnnotationPayload": io.BytesIO(b"1")},
            {"AnnotationPayload": io.BytesIO(b"2")},
        ]

    @pytest.mark.parametrize(
        ("dest_tail", "sent"),
        [
            ([KeyboardInterrupt(), {}, {}, {}], ["PutObjectTagging"]),
            (
                [
                    client_error("AccessDenied", 403, "PutObjectTagging"),
                    KeyboardInterrupt(),
                    {},
                    {},
                ],
                ["PutObjectTagging", "DeleteObject"],
            ),
            (
                [{}, KeyboardInterrupt(), {}, {}, {}],
                ["PutObjectTagging", "PutObjectAnnotation"],
            ),
        ],
        ids=["in-the-tagging-write", "in-the-rollback", "in-an-annotation-write"],
    )
    def test_interrupt_inside_a_post_copy_write_records_cancelled(
        self, dest_tail: list[dict[str, Any] | BaseException], sent: list[str]
    ) -> None:
        # Past the copy, inside copy-props' own requests: stored as the item's
        # outcome, not let escape - escaping, it would have upstream's second
        # announce re-send the writes, record SUCCEEDED and delete the source.
        # No rollback follows a tagging write it cut short.
        results: list[OpResult] = []
        calls, source_calls, transferrer = self._move_with_post_copy_writes(
            dest_tail, [*self._fresh_annotations(), {}, {}, {}], on_result=results.append
        )
        assert ops(calls)[4:] == sent
        assert ops(source_calls) == [
            "HeadObject",
            "GetObjectTagging",
            "ListObjectAnnotations",
            "GetObjectAnnotation",
            "GetObjectAnnotation",
        ]
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert (transferrer.succeeded, transferrer.cancelled) == (0, 1)

    def test_interrupt_inside_the_deferred_annotation_read_records_cancelled(self) -> None:
        # The deferred mode reads the source's annotations only after the
        # copy and its tag write: an interrupt there is stored the same way.
        results: list[OpResult] = []
        calls, source_calls, transferrer = self._move_with_post_copy_writes(
            [{}, {}, {}],
            [KeyboardInterrupt(), {}, {}],
            on_result=results.append,
            annotation_copy_mode=AnnotationCopyMode.DEFERRED,
        )
        assert ops(calls)[4:] == ["PutObjectTagging"]
        assert ops(source_calls) == ["HeadObject", "GetObjectTagging", "ListObjectAnnotations"]
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert (transferrer.succeeded, transferrer.cancelled) == (0, 1)

    @pytest.mark.parametrize(
        ("subscriber", "dest_sent"),
        [
            ("_SetTags", []),
            ("_SetAnnotations", ["PutObjectTagging"]),
            ("_DeleteSource", ["PutObjectTagging", "PutObjectAnnotation", "PutObjectAnnotation"]),
        ],
    )
    def test_interrupt_past_a_subscribers_flag_records_cancelled(
        self, monkeypatch: pytest.MonkeyPatch, subscriber: str, dest_sent: list[str]
    ) -> None:
        # An interrupt that lands past a post-copy subscriber's once-per-item
        # flag but outside its request (here, in its read of the outcome so
        # far) is stored all the same: escaping, it would let a second
        # announce find the flag set and record a success.
        real_outcome = transfer._outcome
        fired: list[bool] = []

        def outcome(future: Any) -> BaseException | None:
            caller = sys._getframe(1).f_locals.get("self")
            if type(caller).__name__ == subscriber and not fired:
                fired.append(True)
                raise KeyboardInterrupt
            return real_outcome(future)

        monkeypatch.setattr(transfer, "_outcome", outcome)
        results: list[OpResult] = []
        calls, source_calls, transferrer = self._move_with_post_copy_writes(
            [{}, {}, {}], [*self._fresh_annotations(), {}], on_result=results.append
        )
        assert ops(calls)[4:] == dest_sent
        assert "DeleteObject" not in ops(source_calls)
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert (transferrer.succeeded, transferrer.cancelled) == (0, 1)

    def test_interrupt_while_a_failed_rollback_is_logged_records_cancelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The tag write and its rollback both fail, and the interrupt lands
        # while the rollback's failure is logged: stored all the same.
        real_debug = transfer.logger.debug

        def debug(msg: object, *args: object, **kwargs: Any) -> None:
            if isinstance(msg, str) and msg.startswith("deleting s3://"):
                raise KeyboardInterrupt
            real_debug(msg, *args, **kwargs)

        monkeypatch.setattr(transfer.logger, "debug", debug)
        results: list[OpResult] = []
        calls, source_calls, transferrer = self._move_with_post_copy_writes(
            [
                client_error("AccessDenied", 403, "PutObjectTagging"),
                client_error("AccessDenied", 403, "DeleteObject"),
                {},
                {},
            ],
            [*self._fresh_annotations(), {}],
            on_result=results.append,
        )
        assert ops(calls)[4:] == ["PutObjectTagging", "DeleteObject"]
        assert "DeleteObject" not in ops(source_calls)
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert (transferrer.succeeded, transferrer.cancelled) == (0, 1)

    def test_an_interrupt_after_the_post_copy_writes_sends_them_once(self) -> None:
        # The interrupt escapes on_result, after every request: upstream
        # re-runs the uncleared callback list, and the post-copy writes must
        # not go out (or the annotations be read) a second time.
        results: list[OpResult] = []

        def on_result(result: OpResult) -> None:
            results.append(result)
            raise KeyboardInterrupt

        calls, source_calls, transferrer = self._move_with_post_copy_writes(
            [{}, {}, {}, {}, {}, {}], [*self._fresh_annotations(), {}, {}, {}], on_result=on_result
        )
        assert ops(calls)[4:] == ["PutObjectTagging", "PutObjectAnnotation", "PutObjectAnnotation"]
        assert ops(source_calls) == [
            "HeadObject",
            "GetObjectTagging",
            "ListObjectAnnotations",
            "GetObjectAnnotation",
            "GetObjectAnnotation",
            "DeleteObject",
        ]
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert (transferrer.succeeded, transferrer.cancelled) == (1, 0)

    def test_interrupt_in_the_submission_stage_records_cancelled(self) -> None:
        # A non-seekable stream is read by upstream's submission task, outside
        # any request task: upstream stores the interrupt as the outcome and
        # swallows it. The item used to end without a record at all.
        class _InterruptingPipe:
            def read(self, amount: int = -1) -> bytes:
                raise KeyboardInterrupt

        item = TransferItem(
            compare_key="-",
            size=None,
            src_fileobj=cast("Any", _InterruptingPipe()),
            src_info=FileInfo(key="-"),
            dest_bucket="b",
            dest_key="k",
        )
        results: list[OpResult] = []
        calls, transferrer = self._run_interrupted(
            TransferType.UPLOAD, item, [], is_move=False, on_result=results.append
        )
        assert calls == []
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert str(results[0].error) == "KeyboardInterrupt()"
        assert (transferrer.succeeded, transferrer.cancelled) == (0, 1)

    def test_interrupt_in_the_unwind_after_the_item_settled_is_not_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The item's final task ran inside the submission call and settled
        # it; the submission task is still unwinding when the interrupt lands.
        # Upstream catches it and tries to store it on a coordinator that is
        # already done - a no-op - so nothing recorded or re-raised it and the
        # run went on to the next item (about 1 real SIGINT in 100).
        from s3transfer.futures import TransferCoordinator

        original = TransferCoordinator.add_associated_future
        fired: list[bool] = []

        def add_associated_future(coordinator: Any, future: Any) -> None:
            if not fired and coordinator.status == "success":
                fired.append(True)
                raise KeyboardInterrupt  # the SIGINT's stand-in
            original(coordinator, future)

        monkeypatch.setattr(TransferCoordinator, "add_associated_future", add_associated_future)
        item = self._upload_item(tmp_path)
        results: list[OpResult] = []
        calls, transferrer = self._run_interrupted(
            TransferType.UPLOAD, item, [{}], is_move=False, on_result=results.append
        )
        assert fired
        assert ops(calls) == ["PutObject"]
        # The transfer itself completed, so its record stands - once.
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert (transferrer.succeeded, transferrer.cancelled) == (1, 0)

    def test_an_interrupt_that_became_the_last_outcome_is_raised_at_the_exit(
        self, tmp_path: Path
    ) -> None:
        # An interrupt can become an item's outcome with no later submit to
        # hand it back - on a worker during the drain, or where the engine's
        # own shutdown discards it (the CRT manager's does). The exit is then
        # the only place left to re-raise it.
        item = self._upload_item(tmp_path)
        client, _calls = make_recording_client([])
        results: list[OpResult] = []
        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            src_storage=LocalStorage("."),
            transfer_config=_SYNC_CONFIG,
            on_result=results.append,
        )
        with pytest.raises(KeyboardInterrupt), transferrer:
            transferrer.prepare()
            transferrer._record_failure(item, KeyboardInterrupt())  # pyright: ignore[reportPrivateUsage]
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]

    def test_interrupt_inside_on_result_reports_the_item_once(self, tmp_path: Path) -> None:
        # Past the request, inside the terminal callback: the transfer itself
        # completed, so the record stands - once, although upstream re-runs the
        # whole (uncleared) callback list on its second announce - and the
        # interrupt still propagates.
        item = self._upload_item(tmp_path)
        results: list[OpResult] = []

        def on_result(result: OpResult) -> None:
            results.append(result)
            raise KeyboardInterrupt

        calls, transferrer = self._run_interrupted(
            TransferType.UPLOAD, item, [{}], is_move=True, on_result=on_result
        )
        assert ops(calls) == ["PutObject"]
        assert not (tmp_path / "a.bin").exists()  # the move completed
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert (transferrer.succeeded, transferrer.cancelled) == (1, 0)


class _WriteOnlySink:
    """A destination that cannot seek: s3transfer's non-seekable download path."""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> int:
        self.data += data
        return len(data)

    def close(self) -> None:
        pass


class _CutBody:
    """A GetObject body that stops with a retryable error after ``cut`` bytes."""

    def __init__(self, data: bytes, *, cut: int) -> None:
        self._data = data[:cut]
        self._cut = cut
        self._total = len(data)
        self._done = False

    def read(self, amount: int = -1) -> bytes:
        if self._done:
            raise IncompleteReadError(actual_bytes=self._cut, expected_bytes=self._total)
        self._done = True
        return self._data


class TestWorkerRaise:
    """A threaded run's worker never receives a signal, so a ``BaseException``
    outside ``Exception`` raised there comes from the caller's own code - an
    event handler on the client, a stream - and upstream would let the item
    settle as a success while the pool swallowed the exception
    (`transfer._ThreadedExecutor`). Whatever ``on_result`` raises belongs to
    the caller on every engine (`Transferrer._deliver`)."""

    def _upload_item(self, tmp_path: Path) -> TransferItem:
        src = tmp_path / "a.bin"
        src.write_bytes(b"payload")
        return TransferItem(
            compare_key="a.bin",
            size=7,
            src_path=str(src),
            src_info=FileInfo(key=str(src).replace(os.sep, "/")),
            dest_bucket="b",
            dest_key="k",
        )

    @pytest.mark.parametrize("raised", [SystemExit(3), KeyboardInterrupt()])
    def test_a_handler_exit_on_a_worker_keeps_the_move_source(
        self, tmp_path: Path, raised: BaseException
    ) -> None:
        item = self._upload_item(tmp_path)
        client, calls = make_recording_client([raised])
        results: list[OpResult] = []
        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            src_storage=LocalStorage("."),
            transfer_config=TransferConfig(),
            is_move=True,
            on_result=results.append,
        )
        with pytest.raises(type(raised)), transferrer:
            transferrer.submit(item)
        assert ops(calls) == ["PutObject"]
        assert (tmp_path / "a.bin").exists()
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert (transferrer.succeeded, transferrer.cancelled) == (0, 1)

    def test_a_stream_interrupt_on_the_io_worker_is_not_a_success(self) -> None:
        class _InterruptingSink(_WriteOnlySink):
            def write(self, data: bytes) -> int:
                raise KeyboardInterrupt

        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            src_bucket="bucket",
            src_key="d/a.bin",
            dest_fileobj=cast("Any", _InterruptingSink()),
        )
        client, _calls = make_recording_client(
            [{"Body": io.BytesIO(b"payload"), "ContentLength": 7}]
        )
        results: list[OpResult] = []
        transferrer = Transferrer(
            TransferType.DOWNLOAD,
            client,
            transfer_config=TransferConfig(),
            on_result=results.append,
        )
        with pytest.raises(KeyboardInterrupt), transferrer:
            transferrer.submit(item)
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]

    @pytest.mark.parametrize("config", [TransferConfig(), _SYNC_CONFIG], ids=["threads", "serial"])
    def test_an_on_result_error_reaches_the_caller(
        self, tmp_path: Path, config: TransferConfig
    ) -> None:
        # s3transfer logs and drops an Exception from a done callback, on a
        # worker and on the calling thread alike.
        item = self._upload_item(tmp_path)
        client, _calls = make_recording_client([{}])

        def on_result(result: OpResult) -> None:
            raise ValueError("from on_result")

        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            src_storage=LocalStorage("."),
            transfer_config=config,
            on_result=on_result,
        )
        with pytest.raises(ValueError, match="from on_result"), transferrer:
            transferrer.submit(item)

    @pytest.mark.parametrize("config", [TransferConfig(), _SYNC_CONFIG], ids=["threads", "serial"])
    def test_an_on_progress_error_reaches_the_caller_and_leaves_the_item(
        self, tmp_path: Path, config: TransferConfig
    ) -> None:
        # Progress fires inside the transfer, where raising would fail the
        # item: the error is the caller's, so it ends the run without failing
        # the item. The queued record fires at submit, so a threaded run
        # raises there and cancels the item still waiting for its worker.
        item = self._upload_item(tmp_path)
        client, _calls = make_recording_client([{}])
        results: list[OpResult] = []

        def on_progress(progress: TransferProgress) -> None:
            raise ValueError("from on_progress")

        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            src_storage=LocalStorage("."),
            transfer_config=config,
            on_result=results.append,
            on_progress=on_progress,
        )
        with pytest.raises(ValueError, match="from on_progress"), transferrer:
            transferrer.submit(item)
        assert len(results) == 1
        assert results[0].outcome in (OpOutcome.SUCCEEDED, OpOutcome.CANCELLED)
        assert transferrer.failed == 0

    def test_the_crt_body_hands_an_interrupt_back_instead_of_to_awscrt(self) -> None:
        class _Exiting:
            def read(self, amount: int = -1) -> bytes:
                raise SystemExit(5)

        noted: list[BaseException] = []
        reader: Any = transfer._GuardedReader(  # pyright: ignore[reportPrivateUsage]
            _Exiting(), noted.append, threading.get_ident()
        )
        # The calling thread itself: unchanged.
        with pytest.raises(SystemExit):
            reader.read(1)
        assert noted == []
        # An awscrt thread: noted, and an ordinary exception in its place.
        raised: list[BaseException] = []

        def read_elsewhere() -> None:
            try:
                reader.read(1)
            except BaseException as exc:
                raised.append(exc)

        worker = threading.Thread(target=read_elsewhere)
        worker.start()
        worker.join()
        assert [type(exc) for exc in noted] == [SystemExit]
        assert [type(exc).__name__ for exc in raised] == ["_StreamInterruptedError"]

    def test_a_crt_stream_upload_hands_awscrt_the_guarded_body(self, tmp_path: Path) -> None:
        stream = io.BytesIO(b"payload")
        client, _ = make_recording_client([])
        manager = _CrtCapturingManager()
        with Transferrer(TransferType.UPLOAD, client) as transferrer:
            transferrer._manager = manager  # pyright: ignore[reportPrivateUsage]
            transferrer.submit(
                TransferItem(
                    compare_key="-",
                    size=None,
                    src_fileobj=cast("Any", stream),
                    src_info=FileInfo(key="-"),
                    dest_bucket="b",
                    dest_key="k",
                )
            )
            src = tmp_path / "a.bin"
            src.write_bytes(b"x")
            transferrer.submit(
                TransferItem(
                    compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="a"
                )
            )
        body, path = (upload["fileobj"] for upload in manager.uploads)
        assert isinstance(body, transfer._GuardedReader)  # pyright: ignore[reportPrivateUsage]
        assert body._raw is stream  # pyright: ignore[reportPrivateUsage]
        assert path == str(src)  # a file is sent by path, not read through Python

    def test_a_crt_stream_interrupt_cancels_the_item_and_reaches_the_caller(self) -> None:
        # awscrt reads the body on its own thread and fails the request with
        # its own error in place of what the read raised; the guard kept the
        # interrupt, so the item is CANCELLED like every interrupted item and
        # the interrupt is re-raised to the caller.
        class _Interrupting:
            def read(self, amount: int = -1) -> bytes:
                raise KeyboardInterrupt

        class _ReadingDrain(_CrtDrainManager):
            def upload(self, **kwargs: Any) -> _FakeCrtFuture:
                future = _FakeCrtFuture(_CrtCancelError("AWS_ERROR_UNKNOWN: Unknown error"))
                self._accepted.append((future, list(kwargs["subscribers"])))
                self.body = kwargs["fileobj"]
                return future

            def shutdown(self) -> None:
                def read_then_settle() -> None:
                    with contextlib.suppress(Exception):
                        self.body.read(1)
                    self._drain()

                worker = threading.Thread(target=read_then_settle)
                worker.start()
                worker.join()

        client, _ = make_recording_client([])
        results: list[OpResult] = []
        transferrer = Transferrer(TransferType.UPLOAD, client, on_result=results.append)
        with pytest.raises(KeyboardInterrupt), transferrer:
            transferrer._manager = _ReadingDrain()  # pyright: ignore[reportPrivateUsage]
            transferrer.submit(
                TransferItem(
                    compare_key="-",
                    size=None,
                    src_fileobj=cast("Any", _Interrupting()),
                    src_info=FileInfo(key="-"),
                    dest_bucket="b",
                    dest_key="k",
                )
            )
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert (transferrer.failed, transferrer.cancelled) == (0, 1)

    def test_an_on_result_error_off_the_calling_thread_does_not_stall_the_crt_drain(
        self, tmp_path: Path
    ) -> None:
        class _ThreadedDrain(_CrtCapturingManager):
            # awscrt settles transfers on its own event-loop thread.
            def shutdown(self) -> None:
                worker = threading.Thread(target=self._drain)
                worker.start()
                worker.join()

        def on_result(result: OpResult) -> None:
            raise ValueError("from on_result")

        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        client, _ = make_recording_client([])
        transferrer = Transferrer(TransferType.UPLOAD, client, on_result=on_result)
        with pytest.raises(ValueError, match="from on_result"), transferrer:
            transferrer._manager = _ThreadedDrain()  # pyright: ignore[reportPrivateUsage]
            transferrer.submit(
                TransferItem(
                    compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="a"
                )
            )
        assert transferrer.succeeded == 1

    def test_a_warning_off_the_calling_thread_reaches_a_run_without_transfers(self) -> None:
        # A dry run builds no manager; the walk still warns from its prefetch
        # worker, and what on_result raised there must still end the run.
        def on_result(result: OpResult) -> None:
            raise ValueError("from on_result")

        client, _ = make_recording_client([])
        transferrer = Transferrer(TransferType.UPLOAD, client, on_result=on_result)
        with pytest.raises(ValueError, match="from on_result"), transferrer:
            worker = threading.Thread(target=transferrer.warner.warn, args=("unreadable",))
            worker.start()
            worker.join()
        assert transferrer.warner.warned == 1


class _CrtCoordinator:
    """s3transfer's CRT coordinator: a done transfer ignores a late exception."""

    def __init__(self) -> None:
        self.exception: BaseException | None = None

    def done(self) -> bool:
        return True

    def set_exception(self, exception: BaseException, override: bool = False) -> None:
        if not self.done() or override:
            self.exception = exception


class _CrtRenameFuture(_FakeCrtFuture):
    def __init__(self, fileobj: Any) -> None:
        super().__init__(RuntimeError("unused"))
        self._coordinator = _CrtCoordinator()
        cast("Any", self.meta).call_args = SimpleNamespace(fileobj=fileobj)

    def result(self) -> None:
        if self._coordinator.exception is not None:
            raise self._coordinator.exception

    def set_exception(self, exception: BaseException) -> None:
        self._coordinator.set_exception(exception, override=True)


class _CrtDownloading(_CrtDrainManager):
    """The CRT manager's download that succeeds: the queued subscribers run at
    submission and the done ones at the drain, on a coordinator that takes a
    subscriber's late ``set_exception`` (mv's source delete)."""

    def download(self, **kwargs: Any) -> _FakeCrtFuture:
        future = _CrtRenameFuture(kwargs["fileobj"])
        subscribers = list(kwargs["subscribers"])
        for subscriber in subscribers:
            on_queued = getattr(subscriber, "on_queued", None)
            if on_queued is not None:
                on_queued(future)
        self._accepted.append((future, subscribers))
        return future


class _CrtRenameFailing(_CrtDownloading):
    """The CRT manager's download whose rename into place fails: its done
    handler (``RenameTempFileHandler``) runs before the subscribers and hands
    the error to the already-done coordinator."""

    def __init__(self, error: OSError) -> None:
        super().__init__()
        self._error = error

    def _drain(self) -> None:
        for future, _subscribers in self._accepted:
            future._coordinator.set_exception(self._error)  # the rename handler
        super()._drain()


class TestDirectoryCreator:
    def test_a_directory_it_cannot_create_names_the_file(self, tmp_path: Path) -> None:
        # A local failure carries the file in key and no bucket, as the
        # engine's own record of the item's local file does.
        blocker = tmp_path / "f"
        blocker.write_text("x")
        dest = str(blocker / "sub" / "a.bin")
        future = SimpleNamespace(meta=SimpleNamespace(call_args=SimpleNamespace(fileobj=dest)))
        with pytest.raises(Boto3S3Error) as raised:
            transfer._DirectoryCreator().on_queued(future)  # pyright: ignore[reportPrivateUsage]
        assert (raised.value.bucket, raised.value.key) == (None, dest.replace(os.sep, "/"))


class TestCrtDownloadRenameFailure:
    """A CRT download whose rename into place fails (a directory at the
    destination, a name too long to create) read as a success: aws then
    prints it done, and a mv deletes the source with nothing written
    (measured). The failure now settles the item."""

    @pytest.mark.parametrize("is_move", [False, True])
    def test_the_item_fails_and_the_source_stays(self, tmp_path: Path, is_move: bool) -> None:
        dest = tmp_path / "a.bin"
        dest.mkdir()
        error = IsADirectoryError(errno.EISDIR, "Is a directory", str(dest) + ".tmp", str(dest))
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            mtime=datetime(2025, 1, 1, tzinfo=timezone.utc),
            src_bucket="bucket",
            src_key="d/a.bin",
            dest_path=str(dest),
        )
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        with Transferrer(
            TransferType.DOWNLOAD, client, is_move=is_move, on_result=results.append
        ) as transferrer:
            transferrer._manager = _CrtRenameFailing(error)  # pyright: ignore[reportPrivateUsage]
            transferrer.submit(item)
        assert calls == []  # no DeleteObject for the source
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        failure = results[0].error
        assert failure is not None and failure.__cause__ is error
        # A local file's failure: the file in key, no bucket.
        assert (failure.bucket, failure.key) == (None, str(dest).replace(os.sep, "/"))


class _CrtNamedError(Exception):
    """awscrt's ``AwsCrtError`` shape: ``code`` / ``name``."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name}: text")
        self.code = 43
        self.name = name


class TestLocalFailureClassification:
    """A failure of the item's local file takes the local taxonomy's class
    and names the file, whichever engine raised it (`_local_failure`)."""

    def _record(self, kind: TransferType, item: TransferItem, exc: BaseException) -> OpResult:
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        with Transferrer(kind, client, on_result=results.append) as transferrer:
            transferrer._record_failure(item, exc)  # pyright: ignore[reportPrivateUsage]
        assert len(results) == 1
        return results[0]

    def _download(self, **fields: Any) -> TransferItem:
        return TransferItem(compare_key="a.bin", src_bucket="b", src_key="d/a.bin", **fields)

    @pytest.mark.parametrize(
        ("exc", "category"),
        [
            (PermissionError(errno.EACCES, "Permission denied"), AccessDeniedError),
            (FileNotFoundError(errno.ENOENT, "No such file or directory"), NotFoundError),
            (IsADirectoryError(errno.EISDIR, "Is a directory"), TransportError),
            (_CrtNamedError("AWS_ERROR_NO_PERMISSION"), AccessDeniedError),
            (_CrtNamedError("AWS_ERROR_FILE_INVALID_PATH"), NotFoundError),
            (_CrtNamedError("AWS_ERROR_FILE_WRITE_FAILURE"), TransportError),
        ],
    )
    def test_a_download_s_local_failure_names_the_file(
        self, tmp_path: Path, exc: BaseException, category: type[Boto3S3Error]
    ) -> None:
        dest = str(tmp_path / "a.bin")
        result = self._record(TransferType.DOWNLOAD, self._download(dest_path=dest), exc)
        assert result.outcome is OpOutcome.FAILED
        assert type(result.error) is category
        assert result.error is not None
        assert str(result.error) == str(exc)
        assert (result.error.bucket, result.error.key) == (None, dest.replace(os.sep, "/"))

    def test_an_upload_s_local_failure_names_its_source(self, tmp_path: Path) -> None:
        src = str(tmp_path / "a.bin")
        item = TransferItem(compare_key="a.bin", src_path=src, dest_bucket="b", dest_key="k")
        result = self._record(TransferType.UPLOAD, item, IsADirectoryError(errno.EISDIR, "x"))
        assert type(result.error) is TransportError
        assert result.error is not None and result.error.key == src.replace(os.sep, "/")

    def test_a_stream_item_keeps_the_request_s_attribution(self) -> None:
        # No local path to name: the failure stays the base error on the object.
        item = self._download(dest_fileobj=cast("Any", _WriteOnlySink()))
        result = self._record(TransferType.DOWNLOAD, item, PermissionError(errno.EACCES, "x"))
        assert type(result.error) is Boto3S3Error
        assert result.error is not None
        assert (result.error.bucket, result.error.key) == ("b", "d/a.bin")


class TestOnlyTheItemsOwnFileIsLocal:
    """An ``OSError`` naming some other file in the item's task - a credential
    provider's token file, the host's mime.types - is the request's, not the
    item's local file failing (`transfer._names_item_file`)."""

    def test_an_error_naming_another_file_stays_the_request_s(self, tmp_path: Path) -> None:
        src = str(tmp_path / "a.bin")
        item = TransferItem(compare_key="a.bin", src_path=src, dest_bucket="b", dest_key="k")
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        with Transferrer(TransferType.UPLOAD, client, on_result=results.append) as transferrer:
            transferrer._record_failure(  # pyright: ignore[reportPrivateUsage]
                item, FileNotFoundError(errno.ENOENT, "No such file", str(tmp_path / "token"))
            )
        assert type(results[0].error) is Boto3S3Error
        assert results[0].error is not None
        assert (results[0].error.bucket, results[0].error.key) == ("b", "k")

    def test_the_temporary_file_beside_it_and_no_name_are_the_item_s(self, tmp_path: Path) -> None:
        path = str(tmp_path / "a.bin")
        names_item_file = transfer._names_item_file  # pyright: ignore[reportPrivateUsage]
        assert names_item_file(OSError(errno.EACCES, "x", path + ".AbC123"), path)
        assert names_item_file(OSError(errno.EISDIR, "x", path + ".AbC123", path), path)
        assert names_item_file(OSError(errno.ENOSPC, "No space left on device"), path)
        assert not names_item_file(OSError(errno.ENOENT, "x", path + "x"), path)
        assert not names_item_file(OSError(errno.EACCES, "x", "/etc/mime.types"), path)


class TestStreamDownloadRetry:
    """A single-request download into a destination that cannot seek is
    retried from the object's first byte; each byte must still be written once
    (`transfer._writing_once`) - upstream's ranged path de-duplicates through
    its DeferQueue, the single request did not."""

    @pytest.mark.parametrize("config", [TransferConfig(), _SYNC_CONFIG], ids=["threads", "serial"])
    @pytest.mark.parametrize("cut", [3, 30])
    def test_a_cut_body_retried_writes_the_object_once(
        self, config: TransferConfig, cut: int
    ) -> None:
        payload = bytes(range(50))
        sink = _WriteOnlySink()
        item = TransferItem(
            compare_key="a.bin",
            size=len(payload),
            etag='"abc123"',
            src_bucket="bucket",
            src_key="d/a.bin",
            dest_fileobj=cast("Any", sink),
        )
        calls, _, results, _ = _run(
            TransferType.DOWNLOAD,
            [item],
            [
                {"Body": _CutBody(payload, cut=cut), "ContentLength": len(payload)},
                {"Body": io.BytesIO(payload), "ContentLength": len(payload)},
            ],
            config=config,
        )
        assert ops(calls) == ["GetObject", "GetObject"]
        assert bytes(sink.data) == payload
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]

    def test_the_wrapper_trims_an_overlapping_chunk(self) -> None:
        written: list[tuple[bytes, int]] = []

        def handle_io(manager: Any, fileobj: Any, chunk: bytes, index: int) -> None:
            written.append((chunk, index))

        handle = transfer._writing_once(handle_io)  # pyright: ignore[reportPrivateUsage]
        handle(None, None, b"abcd", 0)
        handle(None, None, b"ab", 0)  # wholly rewritten: dropped
        handle(None, None, b"cdef", 2)  # straddles the high-water mark: trimmed
        assert written == [(b"abcd", 0), (b"ef", 4)]


class TestFailureWithoutAResponse:
    """A per-item failure without any response is FAILED like every other.

    aws-cli's completion handler reads ``exception.response.get(...)`` and
    raises on the ``None`` response botocore's closed-connection and timeout
    errors carry; s3transfer swallows that, so aws prints no line and exits 0
    for the item (measured on cp, mv, recursive cp and rm). A failure is never
    reported as a success (design/overview.md section 3), so that is not
    copied: the item fails as a refused connection does.
    """

    @staticmethod
    def _upload_item(tmp_path: Path) -> TransferItem:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        return TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )

    @pytest.mark.parametrize(
        "raised",
        [
            ConnectionClosedError(endpoint_url="http://h/"),
            ReadTimeoutError(endpoint_url="http://h/"),
            EndpointConnectionError(endpoint_url="http://h/"),
        ],
        ids=["connection-closed", "read-timeout", "refused"],
    )
    def test_an_upload_whose_request_dies_fails(self, tmp_path: Path, raised: Exception) -> None:
        calls, _, results, transferrer = _run(
            TransferType.UPLOAD, [self._upload_item(tmp_path)], [raised]
        )
        assert ops(calls) == ["PutObject"]
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert isinstance(error, TransportError)
        assert error.__cause__ is raised
        assert (transferrer.succeeded, transferrer.failed, transferrer.skipped) == (0, 1, 0)
        assert transferrer.first_error is error

    def test_a_move_whose_source_delete_dies_fails(self) -> None:
        # S3 may have deleted the source before the connection closed; the
        # client cannot tell, so the move is not reported as done.
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )
        dead = ConnectionClosedError(endpoint_url="http://h/")
        calls, source_calls, results, transferrer = _run(
            TransferType.COPY, [item], [{}], source_responses=[dead], is_move=True
        )
        assert ops(calls) == ["CopyObject"]
        assert ops(source_calls) == ["DeleteObject"]
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert isinstance(error, TransportError)
        assert error.__cause__ is dead
        assert (error.bucket, error.key) == ("src-b", "d/a.bin")
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)

    def test_a_crt_move_whose_source_delete_dies_fails(self, tmp_path: Path) -> None:
        # Under the CRT engine the download lands and the source goes through
        # a botocore DeleteObject; aws hangs there (aws-differences.md).
        dest = tmp_path / "a.bin"
        dest.write_bytes(b"payload")
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            mtime=datetime(2025, 1, 1, tzinfo=timezone.utc),
            src_bucket="bucket",
            src_key="d/a.bin",
            dest_path=str(dest),
        )
        dead = ConnectionClosedError(endpoint_url="http://h/")
        client, calls = make_recording_client([dead])
        results: list[OpResult] = []
        with Transferrer(
            TransferType.DOWNLOAD, client, is_move=True, on_result=results.append
        ) as transferrer:
            transferrer._manager = _CrtDownloading()  # pyright: ignore[reportPrivateUsage]
            transferrer.submit(item)
        assert ops(calls) == ["DeleteObject"]
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert isinstance(error, TransportError)
        assert error.__cause__ is dead
        assert (error.bucket, error.key) == ("bucket", "d/a.bin")
        assert transferrer.failed == 1
        assert dest.read_bytes() == b"payload"


class TestNoOverwrite:
    def test_put_object_carries_if_none_match(self, tmp_path: Path) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, _, _ = _run(
            TransferType.UPLOAD, [item], [{}], options=TransferOptions(no_overwrite=True)
        )
        assert calls[0].params["IfNoneMatch"] == "*"

    def test_precondition_failed_is_a_silent_skip(self, tmp_path: Path) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, results, transferrer = _run(
            TransferType.UPLOAD,
            [item],
            [client_error("PreconditionFailed", 412, "PutObject")],
            options=TransferOptions(no_overwrite=True),
        )
        assert ops(calls) == ["PutObject"]
        assert (transferrer.failed, transferrer.skipped) == (0, 1)
        assert transferrer.first_error is None
        assert [result.outcome for result in results] == [OpOutcome.SKIPPED]

    def test_multipart_upload_applies_it_to_complete_only(self, tmp_path: Path) -> None:
        src = tmp_path / "big.bin"
        src.write_bytes(b"x" * (9 * _MIB))
        item = TransferItem(
            compare_key="big.bin", size=9 * _MIB, src_path=str(src), dest_bucket="b", dest_key="big"
        )
        calls, _, _, _ = _run(
            TransferType.UPLOAD,
            [item],
            [{"UploadId": "u"}, {"ETag": '"p1"'}, {"ETag": '"p2"'}, {}],
            options=TransferOptions(no_overwrite=True),
        )
        assert ops(calls) == [
            "CreateMultipartUpload",
            "UploadPart",
            "UploadPart",
            "CompleteMultipartUpload",
        ]
        assert "IfNoneMatch" not in calls[0].params
        assert "IfNoneMatch" not in calls[1].params
        assert calls[3].params["IfNoneMatch"] == "*"

    def test_multipart_copy_applies_it_to_complete_only(self) -> None:
        item = TransferItem(
            compare_key="a.bin",
            size=9 * _MIB,
            etag='"abc123"',
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )
        calls, _, _, _ = _run(
            TransferType.COPY,
            [item],
            [
                {"UploadId": "u"},
                {"CopyPartResult": {"ETag": '"p1"'}},
                {"CopyPartResult": {"ETag": '"p2"'}},
                {},
            ],
            source_responses=[],
            options=TransferOptions(no_overwrite=True, metadata_directive="REPLACE"),
        )
        assert "IfNoneMatch" not in calls[0].params
        # The conditional write rides only the completing call: neither part
        # copy may carry it (aws puts IfNoneMatch on CompleteMultipartUpload).
        assert "IfNoneMatch" not in calls[1].params
        assert "IfNoneMatch" not in calls[2].params
        assert calls[3].params["IfNoneMatch"] == "*"

    def test_a_multipart_completion_rejected_is_a_silent_skip(self, tmp_path: Path) -> None:
        src = tmp_path / "big.bin"
        src.write_bytes(b"x" * (9 * _MIB))
        item = TransferItem(
            compare_key="big.bin", size=9 * _MIB, src_path=str(src), dest_bucket="b", dest_key="big"
        )
        calls, _, results, transferrer = _run(
            TransferType.UPLOAD,
            [item],
            [
                {"UploadId": "u"},
                {"ETag": '"p1"'},
                {"ETag": '"p2"'},
                client_error("PreconditionFailed", 412, "CompleteMultipartUpload"),
                {},
            ],
            options=TransferOptions(no_overwrite=True),
        )
        assert ops(calls)[-2:] == ["CompleteMultipartUpload", "AbortMultipartUpload"]
        assert (transferrer.failed, transferrer.skipped) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.SKIPPED]


class TestPreconditionFailedElsewhere:
    """A 412 to any request but no_overwrite's own conditional write fails the item.

    aws-cli skips every ``PreconditionFailed`` silently; here only the
    conditional write's rejection is the asked-for skip, and a 412 anywhere
    else - the object changed under a ranged GET or a part copy, a tag write
    refused - is a failure, even with ``no_overwrite`` set.
    """

    def _copy_item(self) -> TransferItem:
        return TransferItem(
            compare_key="a.bin",
            size=9 * _MIB,
            etag='"abc123"',
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )

    def test_a_write_412_without_no_overwrite_fails(self, tmp_path: Path) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        _, _, results, transferrer = _run(
            TransferType.UPLOAD, [item], [client_error("PreconditionFailed", 412, "PutObject")]
        )
        assert (transferrer.failed, transferrer.skipped) == (1, 0)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]

    def test_a_ranged_get_412_fails_the_download(self, tmp_path: Path) -> None:
        item = TransferItem(
            compare_key="a.bin",
            size=9 * _MIB,
            etag='"abc123"',
            src_bucket="bucket",
            src_key="d/a.bin",
            dest_path=str(tmp_path / "a.bin"),
        )
        first_part = {"Body": io.BytesIO(b"x" * (8 * _MIB)), "ETag": '"abc123"'}
        calls, _, results, transferrer = _run(
            TransferType.DOWNLOAD,
            [item],
            [first_part, client_error("PreconditionFailed", 412, "GetObject")],
            options=TransferOptions(no_overwrite=True),
        )
        assert ops(calls) == ["GetObject", "GetObject"]
        assert (transferrer.failed, transferrer.skipped) == (1, 0)
        assert transferrer.first_error is not None
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert not (tmp_path / "a.bin").exists()

    def test_a_part_copy_412_fails_the_copy(self) -> None:
        calls, _, results, transferrer = _run(
            TransferType.COPY,
            [self._copy_item()],
            [
                {"UploadId": "u"},
                client_error("PreconditionFailed", 412, "UploadPartCopy"),
                {},
            ],
            source_responses=[],
            options=TransferOptions(no_overwrite=True, metadata_directive="REPLACE"),
        )
        assert ops(calls) == [
            "CreateMultipartUpload",
            "UploadPartCopy",
            "AbortMultipartUpload",
        ]
        assert (transferrer.failed, transferrer.skipped) == (1, 0)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]

    def test_a_post_copy_tag_write_412_fails_the_copy(self) -> None:
        responses: list[dict[str, Any] | BaseException] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {},
            client_error("PreconditionFailed", 412, "PutObjectTagging"),
            {},  # rollback DeleteObject
        ]
        source_responses: list[dict[str, Any] | BaseException] = [
            {},
            {"TagSet": [{"Key": "k", "Value": "v" * 3000}]},
        ]
        calls, _, results, transferrer = _run(
            TransferType.COPY,
            [self._copy_item()],
            responses,
            source_responses=source_responses,
            options=TransferOptions(no_overwrite=True),
        )
        assert ops(calls)[-2:] == ["PutObjectTagging", "DeleteObject"]
        assert (transferrer.failed, transferrer.skipped) == (1, 0)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]


class TestMove:
    """``is_move``: delete-the-source semantics + the MOVE reporting kind."""

    def _download_item(self, tmp_path: Path) -> TransferItem:
        return TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            mtime=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            src_bucket="bucket",
            src_key="d/a.bin",
            dest_path=str(tmp_path / "out" / "a.bin"),
        )

    def _get_object_response(self) -> dict[str, Any]:
        return {"Body": io.BytesIO(b"payload"), "ContentLength": 7, "ETag": '"abc123"'}

    def test_upload_move_deletes_the_local_source(self, tmp_path: Path) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"payload")
        item = TransferItem(
            compare_key="a.bin", size=7, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, results, transferrer = _run(TransferType.UPLOAD, [item], [{}], is_move=True)
        assert ops(calls) == ["PutObject"]
        assert not src.exists()
        assert transferrer.succeeded == 1
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert all(result.transfer_type is TransferType.MOVE for result in results)

    def test_download_move_deletes_on_the_managers_client(self, tmp_path: Path) -> None:
        item = self._download_item(tmp_path)
        calls, _, results, transferrer = _run(
            TransferType.DOWNLOAD, [item], [self._get_object_response(), {}], is_move=True
        )
        assert ops(calls) == ["GetObject", "DeleteObject"]
        assert calls[1].params == {"Bucket": "bucket", "Key": "d/a.bin"}
        target = tmp_path / "out" / "a.bin"
        assert target.read_bytes() == b"payload"
        # The mtime stamp lands (registered before the delete, aws's order).
        assert item.mtime is not None
        assert os.stat(target).st_mtime == item.mtime.timestamp()
        assert transferrer.succeeded == 1
        assert [result.transfer_type for result in results] == [TransferType.MOVE]

    def test_case_conflict_key_is_released_after_the_source_delete(self, tmp_path: Path) -> None:
        # aws-cli registers CaseConflictCleanupSubscriber after the DeleteSource
        # family, so an admitted key counts as in flight for the whole of its
        # download's teardown - the mtime stamp and, on mv, the DeleteObject.
        # Releasing it earlier would let a name differing only by case slip past
        # the gate while the first download is still tearing down.
        # Driven without _run so the recorded call list is readable *while* the
        # cleanup fires, which is what pins the ordering.
        client, calls = make_recording_client([self._get_object_response(), {}])
        seen_at_release: list[list[str]] = []
        item = self._download_item(tmp_path)
        item.case_conflict_cleanup = lambda: seen_at_release.append(ops(calls))
        transferrer = Transferrer(TransferType.DOWNLOAD, client, is_move=True)
        with transferrer:
            transferrer.submit(item)
        assert ops(calls) == ["GetObject", "DeleteObject"]
        # The source delete had already been issued when the key was released.
        assert seen_at_release == [["GetObject", "DeleteObject"]]
        assert transferrer.succeeded == 1

    def test_copy_move_deletes_on_the_source_client(self) -> None:
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )
        calls, source_calls, _, transferrer = _run(
            TransferType.COPY, [item], [{}], source_responses=[{}], is_move=True
        )
        assert ops(calls) == ["CopyObject"]
        assert ops(source_calls) == ["DeleteObject"]
        assert source_calls[0].params == {"Bucket": "src-b", "Key": "d/a.bin"}
        assert transferrer.succeeded == 1

    def test_copy_move_delete_failure_attributes_to_the_source(self) -> None:
        # Copy lands, the source DeleteObject fails: the structured error must
        # name the *source* object (the one that failed to delete), not the copy
        # destination that succeeded.
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )
        _, source_calls, results, transferrer = _run(
            TransferType.COPY,
            [item],
            [{}],
            source_responses=[client_error("AccessDenied", 403, "DeleteObject")],
            is_move=True,
        )
        assert ops(source_calls) == ["DeleteObject"]
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        error = results[0].error
        assert error is not None
        assert (error.bucket, error.key) == ("src-b", "d/a.bin")

    def test_copy_move_delete_raising_outside_botocore_attributes_to_the_source(self) -> None:
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )
        _, _, results, transferrer = _run(
            TransferType.COPY,
            [item],
            [{}],
            source_responses=[KeyError("Credentials")],
            is_move=True,
        )
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        error = results[0].error
        assert type(error) is Boto3S3Error
        assert isinstance(error.__cause__, KeyError)
        assert (error.bucket, error.key) == ("src-b", "d/a.bin")

    def test_request_payer_flows_to_the_delete(self, tmp_path: Path) -> None:
        item = self._download_item(tmp_path)
        calls, _, _, _ = _run(
            TransferType.DOWNLOAD,
            [item],
            [self._get_object_response(), {}],
            options=TransferOptions(request_payer="requester"),
            is_move=True,
        )
        assert calls[1].operation == "DeleteObject"
        assert calls[1].params["RequestPayer"] == "requester"

    def test_delete_failure_flips_the_move_to_failed(self, tmp_path: Path) -> None:
        item = self._download_item(tmp_path)
        calls, _, results, transferrer = _run(
            TransferType.DOWNLOAD,
            [item],
            [self._get_object_response(), client_error("AccessDenied", 403, "DeleteObject")],
            is_move=True,
        )
        assert ops(calls) == ["GetObject", "DeleteObject"]
        # The bytes are on disk (aws ditto), but the move reports failed.
        assert (tmp_path / "out" / "a.bin").read_bytes() == b"payload"
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert transferrer.first_error is not None
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert error is not None
        assert isinstance(error.__cause__, ClientError)

    def test_os_remove_failure_flips_the_move_to_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )

        def _boom(path: object) -> None:
            raise OSError(13, "Permission denied", str(path))

        monkeypatch.setattr(os, "remove", _boom)
        _, _, results, transferrer = _run(TransferType.UPLOAD, [item], [{}], is_move=True)
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        error = results[0].error
        assert error is not None
        assert isinstance(error.__cause__, OSError)
        assert str(error) == str(error.__cause__)  # the cause shows, its path included

    def test_transfer_failure_leaves_the_source(self, tmp_path: Path) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, results, transferrer = _run(
            TransferType.UPLOAD,
            [item],
            [client_error("NoSuchBucket", 404, "PutObject")],
            is_move=True,
        )
        assert ops(calls) == ["PutObject"]
        assert src.exists()
        assert transferrer.failed == 1
        assert [result.transfer_type for result in results] == [TransferType.MOVE]

    def test_no_overwrite_rejection_keeps_the_source(self, tmp_path: Path) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, results, transferrer = _run(
            TransferType.UPLOAD,
            [item],
            [client_error("PreconditionFailed", 412, "PutObject")],
            options=TransferOptions(no_overwrite=True),
            is_move=True,
        )
        assert ops(calls) == ["PutObject"]
        assert src.exists()
        assert (transferrer.failed, transferrer.skipped) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.SKIPPED]

    @pytest.mark.parametrize(
        "rollback",
        [{}, client_error("AccessDenied", 403, "DeleteObject")],
        ids=["rollback-deletes", "rollback-fails"],
    )
    def test_post_copy_tagging_failure_keeps_the_source(
        self, rollback: dict[str, Any] | BaseException
    ) -> None:
        # _SetTags settles (rolls back the destination, flips the future)
        # before _DeleteSource looks at it: the source object survives -
        # also when the rollback delete fails, where aws-cli's future stays a
        # success and its mv deletes the source.
        big = "v" * 3000
        item = TransferItem(
            compare_key="a.bin",
            size=9 * _MIB,
            etag='"abc123"',
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )
        responses: list[dict[str, Any] | BaseException] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {},
            client_error("AccessDenied", 403, "PutObjectTagging"),
            rollback,  # rollback DeleteObject (destination side)
        ]
        source_responses: list[dict[str, Any] | BaseException] = [
            {},
            {"TagSet": [{"Key": "k", "Value": big}]},
            {},  # a source-side DeleteObject, were one sent
        ]
        calls, source_calls, results, transferrer = _run(
            TransferType.COPY, [item], responses, source_responses=source_responses, is_move=True
        )
        assert ops(calls)[-2:] == ["PutObjectTagging", "DeleteObject"]
        assert calls[-1].params == {"Bucket": "dest-b", "Key": "cp/a.bin"}
        # No source-side delete: the only source calls are the props reads.
        assert ops(source_calls) == ["HeadObject", "GetObjectTagging"]
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert "PutObjectTagging" in str(results[0].error)

    def test_non_transfer_outcomes_report_the_move_kind(self) -> None:
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        item = TransferItem(compare_key="a", src_display="a", dest_display="s3://b/a", size=1)
        with Transferrer(
            TransferType.UPLOAD, client, is_move=True, on_result=results.append
        ) as transferrer:
            transferrer.dryrun(item)
            transferrer.warn("Skipping file a. probe", key="a")
            transferrer.skip(TransferItem(compare_key="a2"))
        assert calls == []
        assert [result.outcome for result in results] == [
            OpOutcome.DRYRUN,
            OpOutcome.WARNED,
            OpOutcome.SKIPPED,
        ]
        assert all(result.transfer_type is TransferType.MOVE for result in results)


class TestDownloadMoveFsync:
    """``LocalStorage(fsync=True)``: the durability barrier before an mv's delete.

    A library-only opt-in (off by default = aws parity): a ``mv`` whose download
    lands on a local destination fsyncs the file (and its parent dir on POSIX)
    before the S3 source is deleted, closing s3transfer's crash window (the source
    is deleted while the bytes may still be only in the page cache).
    """

    def _item(self, tmp_path: Path) -> TransferItem:
        return TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            mtime=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
            src_bucket="bucket",
            src_key="d/a.bin",
            dest_path=str(tmp_path / "out" / "a.bin"),
        )

    def _get_object_response(self) -> dict[str, Any]:
        return {"Body": io.BytesIO(b"payload"), "ContentLength": 7, "ETag": '"abc123"'}

    def _fsync_spy(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        """Record every ``os.fsync`` while still performing the real flush."""
        real_fsync = os.fsync
        fds: list[int] = []

        def spy(fd: int) -> None:
            fds.append(fd)
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", spy)
        return fds

    def test_fsyncs_file_and_dir_then_deletes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fds = self._fsync_spy(monkeypatch)
        item = self._item(tmp_path)
        calls, _, results, transferrer = _run(
            TransferType.DOWNLOAD,
            [item],
            [self._get_object_response(), {}],
            is_move=True,
            dest_storage=LocalStorage(tmp_path, fsync=True),
        )
        # The delete still runs - fsync only precedes it. POSIX fsyncs the file
        # and its parent directory (2); Windows the file alone (1).
        assert ops(calls) == ["GetObject", "DeleteObject"]
        assert len(fds) == (2 if os.name == "posix" else 1)
        assert (tmp_path / "out" / "a.bin").read_bytes() == b"payload"
        assert transferrer.succeeded == 1
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]

    def test_fsync_failure_keeps_the_s3_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(fd: int) -> None:
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(os, "fsync", boom)
        item = self._item(tmp_path)
        # The DeleteObject is never issued (fsync gates it), so no response for it.
        calls, _, results, transferrer = _run(
            TransferType.DOWNLOAD,
            [item],
            [self._get_object_response()],
            is_move=True,
            dest_storage=LocalStorage(tmp_path, fsync=True),
        )
        # The bytes are on disk, but the source survives and the move reports failed.
        assert ops(calls) == ["GetObject"]
        assert (tmp_path / "out" / "a.bin").read_bytes() == b"payload"
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert "Input/output error" in str(results[0].error)
        assert str(results[0].error).startswith("Failed to persist ")
        # A local failure: the file in key, no bucket.
        assert results[0].error is not None
        assert (results[0].error.bucket, results[0].error.key) == (
            None,
            str(tmp_path / "out" / "a.bin").replace(os.sep, "/"),
        )

    def test_default_storage_does_not_fsync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fds = self._fsync_spy(monkeypatch)
        item = self._item(tmp_path)
        calls, _, _, transferrer = _run(
            TransferType.DOWNLOAD,
            [item],
            [self._get_object_response(), {}],
            is_move=True,
            dest_storage=LocalStorage(tmp_path),  # fsync defaults off = aws parity
        )
        assert ops(calls) == ["GetObject", "DeleteObject"]
        assert fds == []
        assert transferrer.succeeded == 1

    def test_windows_branch_opens_for_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Windows' os.fsync is FlushFileBuffers, which needs GENERIC_WRITE on
        # the handle: the off-POSIX branch must open O_RDWR (a read-only fd
        # fails every flush there), while POSIX keeps the read-only open.
        target = tmp_path / "a.bin"
        target.write_bytes(b"payload")
        real_open = os.open
        opens: list[tuple[str, int]] = []

        def spy(path: str, flags: int, *args: Any, **kwargs: Any) -> int:
            opens.append((path, flags))
            return real_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", spy)
        monkeypatch.setattr(os, "name", "nt")
        transfer._FsyncDest(str(target))._fsync()  # pyright: ignore[reportPrivateUsage]
        # One open (the file; no directory fsync off POSIX), write access set.
        assert [path for path, _ in opens] == [str(target)]
        assert opens[0][1] & os.O_RDWR == os.O_RDWR

    def test_cp_download_never_fsyncs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The barrier is scoped to mv; a plain cp deletes no source, so even a
        # fsync=True destination skips it.
        fds = self._fsync_spy(monkeypatch)
        item = self._item(tmp_path)
        calls, _, _, transferrer = _run(
            TransferType.DOWNLOAD,
            [item],
            [self._get_object_response()],
            dest_storage=LocalStorage(tmp_path, fsync=True),
        )
        assert ops(calls) == ["GetObject"]
        assert fds == []
        assert transferrer.succeeded == 1


class TestChecksumOptions:
    def test_explicit_algorithm_overrides_the_engine_default(self, tmp_path: Path) -> None:
        # s3transfer injects ChecksumAlgorithm=CRC32 via setdefault; an
        # explicit --checksum-algorithm must win.
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, _, _ = _run(
            TransferType.UPLOAD, [item], [{}], options=TransferOptions(checksum_algorithm="SHA256")
        )
        assert calls[0].params["ChecksumAlgorithm"] == "SHA256"

    def test_checksum_mode_flows_to_get_object(self, tmp_path: Path) -> None:
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag='"abc123"',
            src_bucket="b",
            src_key="k",
            dest_path=str(tmp_path / "out.bin"),
        )
        calls, _, _, _ = _run(
            TransferType.DOWNLOAD,
            [item],
            [{"Body": io.BytesIO(b"payload"), "ContentLength": 7, "ETag": '"abc123"'}],
            options=TransferOptions(checksum_mode="ENABLED"),
        )
        assert calls[0].params["ChecksumMode"] == "ENABLED"


class TestStreams:
    def test_stream_upload_uses_the_fileobj(self) -> None:
        item = TransferItem(
            compare_key="streaming.txt",
            src_fileobj=io.BytesIO(b"foo\n"),
            dest_bucket="bucket",
            dest_key="streaming.txt",
            src_display="-",
            dest_display="s3://bucket/streaming.txt",
        )
        calls, _, results, transferrer = _run(TransferType.UPLOAD, [item], [{}])
        assert ops(calls) == ["PutObject"]
        assert calls[0].params["Key"] == "streaming.txt"
        # No path means no mimetypes guess: ContentType stays unset.
        assert "ContentType" not in calls[0].params
        assert transferrer.succeeded == 1
        assert results[-1].src == "-"

    def test_stream_upload_without_a_resolved_size_reports_the_bytes_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A size-unknown item gets a byte counter (`_CountBytes`): the CRT
        # manager's meta carries no size, and classic resolves none for a
        # non-seekable upload, so SUCCEEDED reports the progress total instead
        # of 0 (measured on both engines against MinIO before the counter).
        manager = _CrtCapturingManager()
        monkeypatch.setattr(Transferrer, "_create_crt_manager", lambda _self: manager)
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        item = TransferItem(
            compare_key="-", src_fileobj=io.BytesIO(b"x" * 10), dest_bucket="b", dest_key="k"
        )
        with Transferrer(TransferType.UPLOAD, client, on_result=results.append) as transferrer:
            transferrer.submit(item)
            for subscriber in manager.uploads[0]["subscribers"]:
                on_progress = getattr(subscriber, "on_progress", None)
                if on_progress is not None:
                    on_progress(None, bytes_transferred=6)
                    on_progress(None, bytes_transferred=4)
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert results[0].bytes_transferred == 10

    def test_stream_upload_with_expected_size(self) -> None:
        item = TransferItem(
            compare_key="streaming.txt",
            size=4,
            src_fileobj=io.BytesIO(b"foo\n"),
            dest_bucket="bucket",
            dest_key="streaming.txt",
        )
        calls, _, results, transferrer = _run(TransferType.UPLOAD, [item], [{}])
        assert ops(calls) == ["PutObject"]
        assert transferrer.succeeded == 1
        # SUCCEEDED reports the item's size, unlike the size-less streaming
        # upload above. (This asserts the record, not _ProvideSize's delivery:
        # the probe-skip that delivery buys is pinned by the cp download tests'
        # call sequences - GetObject with no HeadObject when size+etag ride.)
        assert results[0].bytes_transferred == 4

    def test_a_size_hint_never_shortens_a_seekable_stream(self) -> None:
        # expected_size is a hint for a stream the engine cannot size. A
        # seekable one (a file opened "rb", the streams.md example) is sized by
        # s3transfer itself, and a provided size would become the byte count it
        # reads: an understated hint truncated the object silently, SUCCEEDED
        # (measured on MinIO: 6 MiB of a 12 MiB file, 100 bytes on a hint of 100).
        client, calls = make_recording_client([{}])
        _consume_bodies(client)
        results: list[OpResult] = []
        item = TransferItem(
            compare_key="streaming.txt",
            size_hint=2,
            src_fileobj=io.BytesIO(b"foo\n"),
            dest_bucket="bucket",
            dest_key="streaming.txt",
        )
        with Transferrer(
            TransferType.UPLOAD, client, transfer_config=_SYNC_CONFIG, on_result=results.append
        ) as transferrer:
            transferrer.submit(item)
        assert ops(calls) == ["PutObject"]
        # The body s3transfer handed the request is the whole stream.
        assert len(calls[0].params["Body"]) == 4
        assert transferrer.succeeded == 1
        assert results[0].bytes_transferred == 4

    def test_a_size_hint_sizes_a_non_seekable_stream_and_the_record_counts(self) -> None:
        # A reader without seek/tell is the hint's one target: s3transfer plans
        # the parts from the provided size and reads to EOF regardless (aws's
        # --expected-size, applied to its non-seekable stdin alone), so the
        # whole stream is sent and SUCCEEDED reports the bytes counted, not
        # the hint the engine was handed.
        client, calls = make_recording_client([{}])
        _consume_bodies(client)
        results: list[OpResult] = []
        item = TransferItem(
            compare_key="-", size_hint=2, src_fileobj=_Pipe(b"foo\n"), dest_bucket="b", dest_key="k"
        )
        with Transferrer(
            TransferType.UPLOAD, client, transfer_config=_SYNC_CONFIG, on_result=results.append
        ) as transferrer:
            transferrer.submit(item)
        assert ops(calls) == ["PutObject"]
        assert len(calls[0].params["Body"]) == 4
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert results[0].bytes_transferred == 4

    def test_a_stream_that_cannot_say_whether_it_seeks_fails_as_an_item(self) -> None:
        # The hint asks the stream whether it is seekable on the submitting
        # thread, and a closed file answers by raising ValueError - which left
        # the operation bare, with no record. Without a hint the same stream
        # is the engine's to touch and fails the item; with one it does too.
        closed = io.BytesIO(b"foo\n")
        closed.close()
        client, calls = make_recording_client([{}])
        results: list[OpResult] = []
        item = TransferItem(
            compare_key="-", size_hint=2, src_fileobj=closed, dest_bucket="b", dest_key="k"
        )
        with Transferrer(
            TransferType.UPLOAD, client, transfer_config=_SYNC_CONFIG, on_result=results.append
        ) as transferrer:
            transferrer.submit(item)
        assert calls == []
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert transferrer.failed == 1

    def test_progress_totals_carry_the_hint_only_where_it_sizes_the_stream(self) -> None:
        # bytes_total is the size known up front. For a non-seekable stream
        # that is the caller's hint (results.md); a seekable one ignores the
        # hint, so it has none to report.
        totals: dict[str, set[int | None]] = {}
        for label, stream in (("pipe", _Pipe(b"foo\n")), ("seekable", io.BytesIO(b"foo\n"))):
            client, _calls = make_recording_client([{}])
            _consume_bodies(client)
            progress: list[TransferProgress] = []
            item = TransferItem(
                compare_key="-", size_hint=9, src_fileobj=stream, dest_bucket="b", dest_key="k"
            )
            with Transferrer(
                TransferType.UPLOAD,
                client,
                transfer_config=_SYNC_CONFIG,
                on_progress=progress.append,
            ) as transferrer:
                transferrer.submit(item)
            totals[label] = {event.bytes_total for event in progress}
        assert totals == {"pipe": {9}, "seekable": {None}}

    def test_the_hint_is_provided_to_the_engine_only_for_a_non_seekable_stream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _CrtCapturingManager()
        monkeypatch.setattr(Transferrer, "_create_crt_manager", lambda _self: manager)
        client, _ = make_recording_client([])
        with Transferrer(TransferType.UPLOAD, client) as transferrer:
            transferrer.submit(
                TransferItem(compare_key="a", size_hint=2, src_fileobj=io.BytesIO(b"foo\n"))
            )
            transferrer.submit(TransferItem(compare_key="b", size_hint=2, src_fileobj=_Pipe(b"x")))
        provided = [
            [s._size for s in upload["subscribers"] if type(s).__name__ == "_ProvideSize"]
            for upload in manager.uploads
        ]
        assert provided == [[], [2]]

    def test_stream_download_probes_then_writes(self) -> None:
        sink = _OpenSink()
        item = TransferItem(
            compare_key="streaming.txt",
            src_bucket="bucket",
            src_key="streaming.txt",
            dest_fileobj=sink,
            src_display="s3://bucket/streaming.txt",
            dest_display="-",
        )
        # Without size+etag s3transfer probes the object itself (the aws
        # stream wire shape: HeadObject then GetObject).
        calls, _, _, transferrer = _run(
            TransferType.DOWNLOAD,
            [item],
            [
                {"ContentLength": 4, "ETag": '"foo"'},
                {"Body": io.BytesIO(b"foo\n"), "ContentLength": 4, "ETag": '"foo"'},
            ],
        )
        assert ops(calls) == ["HeadObject", "GetObject"]
        assert sink.getvalue() == b"foo\n"
        assert transferrer.succeeded == 1


class TestEngineSelection:
    """``preferred_transfer_client`` resolution at the manager seam (design/crt.md)."""

    def _transferrer(self, kind: TransferType, config: Any) -> Transferrer:
        client, _ = make_recording_client([])
        return Transferrer(kind, client, transfer_config=config)

    def test_default_auto_resolves_classic_on_a_non_optimized_host(self) -> None:
        from s3transfer.manager import TransferManager

        # conftest pins is_optimized_for_system to False; boto3's 'auto'
        # answer there is the classic manager.
        manager = self._transferrer(TransferType.UPLOAD, None)._get_manager()
        assert isinstance(manager, TransferManager)

    def test_omitted_config_carries_the_same_defaults_as_an_explicit_one(self) -> None:
        # Regression: the omitted branch built s3transfer's bare TransferConfig,
        # whose download IO queue depth is not the one boto3's class carries, so
        # passing a default-constructed TransferConfig silently changed the
        # transfer's buffering. Both branches must land on the same values.
        omitted = self._transferrer(TransferType.DOWNLOAD, None)._get_manager().config
        explicit = (
            self._transferrer(TransferType.DOWNLOAD, LibraryTransferConfig())._get_manager().config
        )
        for field in (
            "max_io_queue_size",
            "max_request_concurrency",
            "max_request_queue_size",
            "multipart_threshold",
            "multipart_chunksize",
            "io_chunksize",
            "num_download_attempts",
            "max_in_memory_download_chunks",
            "max_in_memory_upload_chunks",
            "max_bandwidth",
        ):
            assert getattr(omitted, field) == getattr(explicit, field), field
        assert omitted.max_io_queue_size == 1000

    def test_explicit_crt_delegates_to_crtsupport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from boto3_s3 import crtsupport

        sentinel = object()
        seen: list[Any] = []

        def fake_create(
            client: Any,
            config: Any,
            *,
            endpoint: str | None = None,
            session: Any | None = None,
            allow_lockless: bool = False,
            sign_requests: bool | None = None,
        ) -> Any:
            seen.append((client, config, endpoint, session))
            return sentinel

        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", fake_create)
        config = TransferConfig(preferred_transfer_client="crt")
        transferrer = self._transferrer(TransferType.UPLOAD, config)
        assert transferrer._get_manager() is sentinel
        assert seen and seen[0][1] is config
        assert seen[0][2] is None  # no explicit endpoint threaded here
        assert seen[0][3] is None  # no session threaded here

    def test_crt_endpoint_is_threaded_to_crtsupport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from boto3_s3 import crtsupport

        seen: list[Any] = []

        def fake_create(
            client: Any,
            config: Any,
            *,
            endpoint: str | None = None,
            session: Any | None = None,
            allow_lockless: bool = False,
            sign_requests: bool | None = None,
        ) -> Any:
            seen.append(endpoint)
            return object()

        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", fake_create)
        vpce = "https://bucket.vpce-0abc.s3.us-east-1.vpce.amazonaws.com"
        # The run's client was built with the explicit endpoint (the CLI flow,
        # and S3.resolve() with S3(endpoint_url=...)), so the pin applies.
        client, _ = make_recording_client([], endpoint_url=vpce)
        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            transfer_config=TransferConfig(preferred_transfer_client="crt"),
            crt_endpoint=vpce,
        )
        transferrer._get_manager()
        assert seen == [vpce]

    def test_crt_endpoint_is_dropped_for_a_storage_supplied_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The explicit endpoint pin belongs to the client built from it. A
        # storage-supplied client with its own endpoint must not be pinned to
        # the S3-level one - the CRT would dial one endpoint with another's
        # credentials (the classic lane just uses the client) - so the manager
        # falls back to the host heuristic on the client's own endpoint.
        from boto3_s3 import crtsupport

        seen: list[Any] = []

        def fake_create(
            client: Any,
            config: Any,
            *,
            endpoint: str | None = None,
            session: Any | None = None,
            allow_lockless: bool = False,
            sign_requests: bool | None = None,
        ) -> Any:
            seen.append(endpoint)
            return object()

        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", fake_create)
        client, _ = make_recording_client([])  # default AWS endpoint, not the S3-level one
        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            transfer_config=TransferConfig(preferred_transfer_client="crt"),
            crt_endpoint="https://endpoint-a.example.com",
        )
        transferrer._get_manager()
        assert seen == [None]

    def test_session_is_threaded_to_crtsupport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from boto3_s3 import crtsupport

        seen: list[Any] = []

        def fake_create(
            client: Any,
            config: Any,
            *,
            endpoint: str | None = None,
            session: Any | None = None,
            allow_lockless: bool = False,
            sign_requests: bool | None = None,
        ) -> Any:
            seen.append(session)
            return object()

        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", fake_create)
        caller_session = object()
        client, _ = make_recording_client([])
        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            transfer_config=TransferConfig(preferred_transfer_client="crt"),
            session=caller_session,  # pyright: ignore[reportArgumentType]
        )
        transferrer._get_manager()
        assert seen == [caller_session]

    @pytest.mark.parametrize(
        "declaration", [None, False, True], ids=["library-default", "cli-unsigned", "cli-signed"]
    )
    def test_sign_requests_posture_is_threaded_to_crtsupport(
        self, monkeypatch: pytest.MonkeyPatch, declaration: bool | None
    ) -> None:
        # None keeps boto3's rule (derive the mode from the client); a
        # declaration is aws-cli's `sign_request`, which is what keeps
        # `--no-sign-request --sse aws:kms` anonymous on the CRT lane.
        from boto3_s3 import crtsupport

        seen: list[bool | None] = []

        def fake_create(
            client: Any,
            config: Any,
            *,
            endpoint: str | None = None,
            session: Any | None = None,
            allow_lockless: bool = False,
            sign_requests: bool | None = None,
        ) -> Any:
            seen.append(sign_requests)
            return object()

        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", fake_create)
        client, _ = make_recording_client([])
        kwargs: dict[str, Any] = {} if declaration is None else {"crt_sign_requests": declaration}
        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            transfer_config=TransferConfig(preferred_transfer_client="crt"),
            **kwargs,
        )
        transferrer._get_manager()
        assert seen == [declaration]

    def test_copy_kind_is_unconditionally_classic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from s3transfer.manager import TransferManager

        from boto3_s3 import crtsupport

        def boom(
            client: Any,
            config: Any,
            *,
            endpoint: str | None = None,
            session: Any | None = None,
            allow_lockless: bool = False,
            sign_requests: bool | None = None,
        ) -> Any:
            raise AssertionError("copy reached the CRT path")  # must not run

        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", boom)
        config = TransferConfig(preferred_transfer_client="crt")
        manager = self._transferrer(TransferType.COPY, config)._get_manager()
        assert isinstance(manager, TransferManager)

    def test_crt_unavailable_falls_back_to_classic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from s3transfer.manager import TransferManager

        from boto3_s3 import crtsupport

        # boto3 semantics: a None from the CRT factory (lock held elsewhere,
        # incompatible singleton) silently selects classic.
        def fake_create(_client: Any, _config: Any, **_kwargs: Any) -> Any:
            return None

        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", fake_create)
        config = TransferConfig(preferred_transfer_client="crt")
        manager = self._transferrer(TransferType.UPLOAD, config)._get_manager()
        assert isinstance(manager, TransferManager)


class TestCrtEngineErrors:
    """What building the CRT engine raises is classified at the seam.

    The engine is built by third-party code (boto3's config validation,
    awscrt's client factory, a credential resolution), and
    `transfer.crt_engine_errors` gives each failure the category
    design/exceptions.md names for its cause - an `AssertionError` (an
    invariant or a test double's guard) alone passes through.
    """

    @staticmethod
    def _transferrer_whose_engine_raises(
        monkeypatch: pytest.MonkeyPatch, exc: BaseException
    ) -> Transferrer:
        from boto3_s3 import crtsupport

        def fake_create(_client: Any, _config: Any, **_kwargs: Any) -> Any:
            raise exc

        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", fake_create)
        client, _ = make_recording_client([])
        config = TransferConfig(preferred_transfer_client="crt")
        return Transferrer(TransferType.UPLOAD, client, transfer_config=config, operation="sync")

    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (FileNotFoundError(2, "No such file or directory", "/ca.pem"), InvalidConfigError),
            (IsADirectoryError(21, "Is a directory", "/ca"), InvalidConfigError),
            (PermissionError(13, "Permission denied", "/ca.pem"), InvalidConfigError),
            (
                RuntimeError("34 (AWS_ERROR_INVALID_ARGUMENT): An invalid argument."),
                InvalidConfigError,
            ),
            (
                ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "AssumeRole"),
                AccessDeniedError,
            ),
            # The serializer's client build reading a malformed configuration
            # of the session it is built from (`s3 = foo`), as `S3.client()`
            # classifies the same build (`client_build_error`).
            (AttributeError("'str' object has no attribute 'get'"), InvalidConfigError),
            (ValueError("Invalid endpoint: 1http://127.0.0.1:9"), InvalidConfigError),
        ],
        ids=[
            "missing-ca",
            "directory-ca",
            "unreadable-ca",
            "unparsable-ca",
            "refused-assume-role",
            "malformed-s3-section",
            "malformed-endpoint",
        ],
    )
    def test_a_construction_failure_keeps_its_category(
        self, monkeypatch: pytest.MonkeyPatch, raised: Exception, expected: type[Any]
    ) -> None:
        transferrer = self._transferrer_whose_engine_raises(monkeypatch, raised)
        with pytest.raises(expected) as info:
            transferrer._get_manager()
        assert str(info.value) == str(raised)
        assert info.value.__cause__ is raised
        assert info.value.operation == "sync"

    @staticmethod
    def _transport_errors() -> list[Exception]:
        from botocore.exceptions import (
            ConnectTimeoutError,
            EndpointConnectionError,
            ProxyConnectionError,
            ReadTimeoutError,
            SSLError,
        )

        errors: list[Exception] = []
        errors.append(EndpointConnectionError(endpoint_url="http://127.0.0.1:9/"))
        # These four derive from botocore's vendored requests' IOError:
        # OSErrors, which the seam must not read as a wrong setting.
        errors.append(SSLError(endpoint_url="https://127.0.0.1:9/", error="handshake"))
        errors.append(ConnectTimeoutError(endpoint_url="https://sts/"))
        errors.append(ReadTimeoutError(endpoint_url="https://sts/"))
        errors.append(ProxyConnectionError(proxy_url="http://127.0.0.1:9"))
        return errors

    @pytest.mark.parametrize("index", range(5))
    def test_an_unreachable_sts_is_a_transport_error(
        self, monkeypatch: pytest.MonkeyPatch, index: int
    ) -> None:
        raised = self._transport_errors()[index]
        if index > 0:
            assert isinstance(raised, OSError)
        transferrer = self._transferrer_whose_engine_raises(monkeypatch, raised)
        with pytest.raises(TransportError) as info:
            transferrer._get_manager()
        assert info.value.__cause__ is raised

    def test_boto3s_crt_config_rejection_is_a_validation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from boto3.exceptions import InvalidCrtTransferConfigError

        raised = InvalidCrtTransferConfigError("The following transfer config options are invalid")
        transferrer = self._transferrer_whose_engine_raises(monkeypatch, raised)
        with pytest.raises(ValidationError) as info:
            transferrer._get_manager()
        assert info.value.__cause__ is raised

    def test_the_seam_needs_no_crt_only_boto3_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # boto3 1.34.6 to 1.41 run the CRT engine without InvalidCrtTransferConfigError
        # (it arrived in 1.42.0); an unconditional import failed every CRT
        # transfer there with ImportError before the first byte moved.
        import boto3.exceptions

        from boto3_s3 import crtsupport

        monkeypatch.delattr(boto3.exceptions, "InvalidCrtTransferConfigError", raising=False)
        sentinel = object()
        monkeypatch.setattr(
            crtsupport, "create_crt_transfer_manager", lambda _c, _cfg, **_k: sentinel
        )
        client, _ = make_recording_client([])
        config = TransferConfig(preferred_transfer_client="crt")
        assert (
            Transferrer(TransferType.UPLOAD, client, transfer_config=config)._get_manager()
            is sentinel
        )
        # And a failure there is still classified, with nothing to look up.
        transferrer = self._transferrer_whose_engine_raises(
            monkeypatch, FileNotFoundError(2, "No such file or directory", "/ca.pem")
        )
        with pytest.raises(InvalidConfigError):
            transferrer._get_manager()

    def test_an_assertion_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        transferrer = self._transferrer_whose_engine_raises(monkeypatch, AssertionError())
        with pytest.raises(AssertionError):
            transferrer._get_manager()

    def test_a_redirect_loop_inside_a_resolution_is_the_base_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # RecursionError is a RuntimeError, but not a configuration problem:
        # the request failing, as request_failure reports it everywhere else.
        from boto3_s3.exceptions import Boto3S3Error

        transferrer = self._transferrer_whose_engine_raises(monkeypatch, RecursionError("maximum"))
        with pytest.raises(Boto3S3Error) as info:
            transferrer._get_manager()
        assert type(info.value) is Boto3S3Error


class TestCrtSubscriberCompat:
    """The provide-size/etag subscribers guard for the CRT meta (aws-cli shape)."""

    class _CrtLikeMeta:
        pass  # no provide_transfer_size / provide_object_etag, like CRTTransferMeta

    def test_provide_size_is_harmless_without_the_hook(self) -> None:
        from boto3_s3.transfer import _ProvideSize

        future = type("F", (), {"meta": self._CrtLikeMeta()})()
        _ProvideSize(123).on_queued(future)  # must not raise

    def test_provide_etag_is_harmless_without_the_hook(self) -> None:
        from boto3_s3.transfer import _ProvideETag

        future = type("F", (), {"meta": self._CrtLikeMeta()})()
        _ProvideETag("abc").on_queued(future)  # must not raise


class TestProvidedEtagIsTheResponseText:
    """The engine hands s3transfer the source ETag exactly as the response
    carried it - the ``If-Match`` aws-cli sends.

    s3transfer puts the provided value on every ranged GET of a multipart
    download and on a multipart copy's ``CopySourceIfMatch``. Measured against
    the pinned aws (2.36.40) through a 127.0.0.1 fake: an ETag served without
    quotes goes back without them, a weak ``W/"..."`` one as it came, and the
    usual quoted one quoted.
    """

    @pytest.mark.parametrize("etag", ['"abc123"', '"abc123-2"', "abc123", 'W/"abc123"'])
    def test_the_value_is_provided_untouched(self, etag: str) -> None:
        from boto3_s3.transfer import _ProvideETag

        provided: list[str] = []
        meta = type("Meta", (), {"provide_object_etag": staticmethod(provided.append)})()
        future = type("F", (), {"meta": meta})()
        _ProvideETag(etag).on_queued(future)
        assert provided == [etag]


class TestResponseCaptureExpectGate:
    """``transfer._ResponseCapture``: only keys admitted via ``expect()`` record.

    A same-client GetObject issued outside the engine - a content filter
    reading a pair the run then rejects - must not grow the read store for the
    rest of the run, since no item would ever drain it.
    """

    def test_unexpected_key_is_not_stored(self) -> None:
        from boto3_s3.transfer import _ResponseCapture

        capture = _ResponseCapture()
        capture.expect("b", "k")
        parsed = {"ETag": '"e"', "ResponseMetadata": {"HTTPStatusCode": 200}}
        capture._record_read(parsed, {"boto3_s3_capture_key": ("b", "k")})
        capture._record_read(parsed, {"boto3_s3_capture_key": ("b", "other")})
        # The expected key surfaces (transport internals stripped); the
        # never-expected key stored nothing.
        assert capture.pop_read("b", "k") == {"ETag": '"e"'}
        assert capture.pop_read("b", "other") is None


class TestPublicSurface:
    def test_all_matches_the_documented_surface(self) -> None:
        # The module is a documented submodule-path surface (design/transfer.md):
        # the engine pair, the Warner protocol its `warner` seam returns, and
        # the SDK-floor probes (--no-overwrite, section 7; copy_props=ALL,
        # section 4). A symbol added or dropped must be a deliberate
        # __all__ / docs decision.
        assert set(transfer.__all__) == {
            "TransferItem",
            "Transferrer",
            "Warner",
            "annotations_copy_unsupported_reason",
            "conditional_write_unsupported_reason",
        }
        for name in transfer.__all__:
            assert hasattr(transfer, name), name


class TestConditionalWriteSupport:
    """The --no-overwrite (IfNoneMatch) old-botocore gate, library side.

    IfNoneMatch reached the S3 write ops only in later botocore (PutObject in
    1.35.2, CopyObject in 1.40.61); below that --no-overwrite must be rejected
    with a clear message instead of an opaque botocore ParamValidationError.
    """

    def test_reason_none_when_upload_supported(self) -> None:
        client = model_only_client({"PutObject", "CompleteMultipartUpload"})
        assert conditional_write_unsupported_reason(client, is_copy=False) is None

    def test_reason_none_when_copy_supported(self) -> None:
        client = model_only_client({"CopyObject"})
        assert conditional_write_unsupported_reason(client, is_copy=True) is None

    def test_reason_names_min_botocore_for_upload(self) -> None:
        reason = conditional_write_unsupported_reason(model_only_client(set()), is_copy=False)
        assert reason is not None
        assert "1.35.2" in reason and "PutObject" in reason

    def test_reason_names_min_botocore_for_copy(self) -> None:
        # PutObject present but CopyObject not: copy needs the later 1.40.61.
        reason = conditional_write_unsupported_reason(
            model_only_client({"PutObject"}), is_copy=True
        )
        assert reason is not None
        assert "1.40.61" in reason and "CopyObject" in reason

    def test_upload_refused_without_s3transfer_create_blocklist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # s3transfer < 0.11 hands the full extra_args to CreateMultipartUpload
        # (no CREATE_MULTIPART_BLOCKLIST), so a botocore that models
        # IfNoneMatch - the real boto3 1.35.2+ / s3transfer 0.10.x pairing -
        # would fail every multipart --no-overwrite upload deep in botocore.
        # The probe refuses uploads up front instead.
        monkeypatch.delattr("s3transfer.upload.UploadSubmissionTask.CREATE_MULTIPART_BLOCKLIST")
        client = model_only_client({"PutObject", "CompleteMultipartUpload"})
        reason = conditional_write_unsupported_reason(client, is_copy=False)
        assert reason is not None
        assert "0.11.0" in reason and "CreateMultipartUpload" in reason

    def test_copy_unaffected_by_missing_upload_blocklist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The copy-side blacklist exists across the whole supported s3transfer
        # range; only the upload path needs the 0.11 refusal.
        monkeypatch.delattr("s3transfer.upload.UploadSubmissionTask.CREATE_MULTIPART_BLOCKLIST")
        client = model_only_client({"CopyObject"})
        assert conditional_write_unsupported_reason(client, is_copy=True) is None

    def test_transferrer_rejects_no_overwrite_upload_on_old_botocore(self) -> None:
        # ConfigurationError: the environment (SDK floor) lacks the
        # capability, not the caller's arguments (exceptions.md section 3).
        with pytest.raises(ConfigurationError, match=r"1\.35\.2"):
            Transferrer(
                TransferType.UPLOAD, model_only_client(set()), options={"no_overwrite": True}
            )

    def test_transferrer_rejects_no_overwrite_copy_on_old_botocore(self) -> None:
        client = model_only_client({"PutObject"})  # upload ok, copy not yet
        with pytest.raises(ConfigurationError, match=r"1\.40\.61"):
            Transferrer(
                TransferType.COPY, client, source_client=client, options={"no_overwrite": True}
            )

    def test_transferrer_allows_no_overwrite_download_on_old_botocore(self) -> None:
        # Downloads never send IfNoneMatch, so an old model must not block them.
        Transferrer(TransferType.DOWNLOAD, model_only_client(set()), options={"no_overwrite": True})

    def test_transferrer_allows_no_overwrite_when_supported(self) -> None:
        Transferrer(
            TransferType.UPLOAD, model_only_client({"PutObject"}), options={"no_overwrite": True}
        )


class TestChecksumAlgorithmSupport:
    """The checksum-algorithm SDK gate (docs/compatibility.md).

    The S3 model gains each newer algorithm in one release, with every member
    that carries it (CRC64NVME 1.36.0, SHA512 and XXHASH 1.42.94); below it a
    single upload fails and a multipart upload or copy cannot carry its parts'
    checksums, so uploads and copies are refused up front, naming the version.
    """

    # PutObject's checksum members on botocore 1.31 (the floor).
    _FLOOR = frozenset({"ChecksumCRC32", "ChecksumCRC32C", "ChecksumSHA1", "ChecksumSHA256"})

    @pytest.mark.parametrize(
        ("algorithm", "minimum"),
        [
            ("CRC64NVME", "1.36.0"),
            ("SHA512", "1.42.94"),
            ("XXHASH64", "1.42.94"),
            ("XXHASH3", "1.42.94"),
            ("XXHASH128", "1.42.94"),
        ],
    )
    def test_upload_and_copy_refused_below_the_algorithm(
        self, algorithm: str, minimum: str
    ) -> None:
        client = model_client({"PutObject": self._FLOOR})
        for transfer_type in (TransferType.UPLOAD, TransferType.COPY):
            with pytest.raises(ConfigurationError, match=re.escape(minimum)) as exc_info:
                Transferrer(
                    transfer_type,
                    client,
                    source_client=client,
                    options={"checksum_algorithm": algorithm},
                )
            assert f"checksum_algorithm={algorithm}" in str(exc_info.value)

    @pytest.mark.parametrize("transfer_type", [TransferType.UPLOAD, TransferType.COPY])
    @pytest.mark.parametrize("algorithm", ["SHA512", "sha512", "Sha512"])
    def test_a_model_with_the_member_runs(
        self, transfer_type: TransferType, algorithm: str
    ) -> None:
        # 1.42.94 onward: the member is there, whatever botocore computes it
        # with - for a copy as for an upload, and whatever the name's case.
        client = model_client({"PutObject": self._FLOOR | {"ChecksumSHA512"}})
        Transferrer(
            transfer_type, client, source_client=client, options={"checksum_algorithm": algorithm}
        )

    def test_lowercase_name_is_probed_like_botocore_reads_it(self) -> None:
        # botocore uppercases nothing for the caller; the probe folds the case.
        client = model_client({"PutObject": self._FLOOR})
        with pytest.raises(ConfigurationError, match=r"SHA512"):
            Transferrer(TransferType.UPLOAD, client, options={"checksum_algorithm": "sha512"})

    @pytest.mark.parametrize("algorithm", ["CRC32", "CRC32C", "SHA1", "SHA256", "MD5", "BOGUS"])
    def test_floor_algorithms_and_unknown_names_are_left_to_botocore(self, algorithm: str) -> None:
        # The floor's own algorithms pass; MD5 (modelled from 1.42.94, never
        # computable) and an unknown name keep botocore's per-item report.
        client = model_client({"PutObject": self._FLOOR})
        Transferrer(TransferType.UPLOAD, client, options={"checksum_algorithm": algorithm})

    def test_download_is_not_gated(self) -> None:
        # A download never sends ChecksumAlgorithm.
        client = model_client({"PutObject": self._FLOOR})
        Transferrer(TransferType.DOWNLOAD, client, options={"checksum_algorithm": "SHA512"})

    def test_current_botocore_models_every_gated_algorithm(self) -> None:
        # The dev SDK is above every entry, so a real client is refused nothing.
        client = boto3.client("s3", region_name="us-east-1")
        for algorithm in transfer._CHECKSUM_MIN_BOTOCORE:  # pyright: ignore[reportPrivateUsage]
            Transferrer(
                TransferType.UPLOAD, client, options={"checksum_algorithm": algorithm.upper()}
            )


class TestAnnotationsCopySupport:
    """The copy_props=ALL SDK gate (design/transfer.md section 4).

    Annotations need botocore's S3 model (CopyObject.AnnotationDirective,
    1.43.31); below it ALL must be refused with a clear message. The
    multipart carryover is this library's own subscriber, so s3transfer's
    version does not enter into it.
    """

    def _capable_client(self) -> Any:
        return model_only_client({"CopyObject"}, member="AnnotationDirective")

    def test_reason_none_on_current_sdk(self) -> None:
        assert annotations_copy_unsupported_reason(self._capable_client()) is None

    def test_reason_names_min_botocore(self) -> None:
        reason = annotations_copy_unsupported_reason(
            model_only_client(set(), member="AnnotationDirective")
        )
        assert reason is not None
        assert "1.43.31" in reason and "botocore" in reason

    def test_an_s3transfer_without_annotation_handling_is_enough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from s3transfer.copies import CopySubmissionTask

        trimmed = [
            arg
            for arg in CopySubmissionTask.CREATE_MULTIPART_ARGS_BLACKLIST
            if arg != "AnnotationDirective"
        ]
        monkeypatch.setattr(CopySubmissionTask, "CREATE_MULTIPART_ARGS_BLACKLIST", trimmed)
        assert annotations_copy_unsupported_reason(self._capable_client()) is None

    def test_transferrer_rejects_all_on_old_botocore(self) -> None:
        client = model_only_client(set(), member="AnnotationDirective")
        with pytest.raises(ConfigurationError, match=r"1\.43\.31"):
            Transferrer(
                TransferType.COPY,
                client,
                source_client=client,
                options={"copy_props": CopyPropsMode.ALL},
            )

    def test_transferrer_allows_all_on_current_sdk(self) -> None:
        client = self._capable_client()
        Transferrer(
            TransferType.COPY,
            client,
            source_client=client,
            options={"copy_props": CopyPropsMode.ALL},
        )

    def test_metadata_directive_disables_the_all_gate_on_old_botocore(self) -> None:
        # An explicit metadata_directive disables the copy-props chain entirely
        # (aws-cli), so ALL never reaches the annotations path - the gate must
        # not refuse the combination for a feature that won't run. aws-cli
        # accepts `cp ... --metadata-directive REPLACE --copy-props all` here.
        client = model_only_client(set(), member="AnnotationDirective")
        Transferrer(
            TransferType.COPY,
            client,
            source_client=client,
            options={"copy_props": CopyPropsMode.ALL, "metadata_directive": "REPLACE"},
        )

    def test_other_modes_stay_usable_on_old_botocore(self) -> None:
        # EXCLUDE degrades silently below the annotations model; construction
        # must not gate the default mode.
        client = model_only_client(set(), member="AnnotationDirective")
        Transferrer(TransferType.COPY, client, source_client=client)

    def test_copy_props_none_value_means_default(self) -> None:
        # The constructor interprets the mode once; a None copy_props reads
        # as unspecified (an is-None check: None only, not any falsy value),
        # not a ValueError - so a permissive caller's dryrun still constructs.
        client = model_only_client(set(), member="AnnotationDirective")
        transferrer = Transferrer(
            TransferType.COPY,
            client,
            source_client=client,
            options=cast(TransferOptions, {"copy_props": None}),
        )
        assert transferrer._copy_props is CopyPropsMode.DEFAULT

    def test_invalid_copy_props_value_raises_validation_error(self) -> None:
        # A bad copy_props string fails the enum conversion; the constructor
        # translates it to a Boto3S3Error-family ValidationError rather than
        # leaking the enum's raw ValueError past the public API
        # (design/exceptions.md). The CLI never reaches here (choices-validated).
        client = self._capable_client()
        with pytest.raises(ValidationError, match="copy_props"):
            Transferrer(
                TransferType.COPY,
                client,
                source_client=client,
                options=cast(TransferOptions, {"copy_props": "al"}),
            )

    def test_empty_string_copy_props_raises_validation_error(self) -> None:
        # "" is present but empty (an untyped caller's value): the is-None
        # unspecified check must not swallow it into DEFAULT - it fails the
        # enum conversion like any other bad string.
        client = self._capable_client()
        with pytest.raises(ValidationError) as excinfo:
            Transferrer(
                TransferType.COPY,
                client,
                source_client=client,
                options=cast(TransferOptions, {"copy_props": ""}),
            )
        assert type(excinfo.value) is ValidationError
        assert str(excinfo.value) == "Invalid copy_props value: ''"

    def test_empty_string_annotation_copy_mode_raises_validation_error(self) -> None:
        client = self._capable_client()
        with pytest.raises(ValidationError) as excinfo:
            Transferrer(
                TransferType.COPY,
                client,
                source_client=client,
                options=cast(TransferOptions, {"annotation_copy_mode": ""}),
            )
        assert type(excinfo.value) is ValidationError
        assert str(excinfo.value) == "Invalid annotation_copy_mode value: ''"

    @pytest.mark.parametrize("mode", list(AnnotationCopyMode))
    def test_annotation_copy_mode_accepts_every_public_mode(self, mode: AnnotationCopyMode) -> None:
        client = self._capable_client()
        Transferrer(
            TransferType.COPY,
            client,
            source_client=client,
            options={"copy_props": CopyPropsMode.ALL, "annotation_copy_mode": mode},
        )

    def test_invalid_annotation_copy_mode_raises_validation_error(self) -> None:
        client = self._capable_client()
        with pytest.raises(ValidationError, match="annotation_copy_mode"):
            Transferrer(
                TransferType.COPY,
                client,
                source_client=client,
                options=cast(
                    TransferOptions,
                    {"copy_props": CopyPropsMode.ALL, "annotation_copy_mode": "preload"},
                ),
            )


class _BackpressureFuture(_FakeCrtFuture):
    """A pending transfer that settles on ``cancel()`` or at shutdown."""

    def __init__(self, key: str, subscribers: list[Any], manager: _BackpressureManager) -> None:
        super().__init__(RuntimeError("unused"))
        self._key = key
        self._subscribers = subscribers
        self._manager = manager
        self._settled = threading.Event()
        self._outcome: BaseException | None = None

    def done(self) -> bool:
        return self._settled.is_set()

    def result(self) -> None:
        if self._outcome is not None:
            raise self._outcome
        return None

    def cancel(self) -> None:
        self._outcome = _CrtCancelError("AWS_ERROR_S3_CANCELED: Request successfully cancelled")
        self._manager.cancelled.append(self._key)
        self.settle()

    def settle(self) -> None:
        if self._settled.is_set():
            return
        self._settled.set()
        for subscriber in self._subscribers:
            on_done = getattr(subscriber, "on_done", None)
            if on_done is not None:
                on_done(self)
        self._manager.slot.release()


class _BackpressureManager:
    """A stand-in with one in-flight slot: the next hand-over blocks until the
    previous transfer settles, as the CRT manager's transfer semaphore does."""

    def __init__(self) -> None:
        self.slot = threading.Semaphore(1)
        self.cancelled: list[str] = []
        self._accepted: list[_BackpressureFuture] = []

    def upload(self, **kwargs: Any) -> _BackpressureFuture:
        assert self.slot.acquire(timeout=10), "the in-flight slot was never released"
        future = _BackpressureFuture(kwargs["key"], list(kwargs["subscribers"]), self)
        self._accepted.append(future)
        return future

    def shutdown(self) -> None:
        for future in list(self._accepted):
            future.settle()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.shutdown()


class TestImmediateCancelWatcher:
    """The immediate-cancel watcher runs from the manager's construction, not
    only during the drain: an IMMEDIATE order must reach in-flight work while
    the submitting thread waits on the engine's backpressure (measured on the
    CRT engine against a server stalling every PUT 25 s: 140 files took the
    whole 25 s to honor a cancel ordered at 3 s, 20 files - the loop done
    submitting, the drain's watcher up - returned at 3 s)."""

    @staticmethod
    def _watchers() -> list[threading.Thread]:
        return [thread for thread in threading.enumerate() if thread.name == "boto3-s3-cancel"]

    def test_the_watcher_lives_from_the_manager_build_to_the_exit(self) -> None:
        client, _ = make_recording_client([])
        transferrer = Transferrer(
            TransferType.UPLOAD, client, transfer_config=_SYNC_CONFIG, cancel_token=CancelToken()
        )
        assert self._watchers() == []
        with transferrer:
            transferrer.prepare()
            assert len(self._watchers()) == 1
        assert self._watchers() == []

    def test_no_token_means_no_watcher(self) -> None:
        client, _ = make_recording_client([])
        with Transferrer(TransferType.UPLOAD, client, transfer_config=_SYNC_CONFIG) as transferrer:
            transferrer.prepare()
            assert self._watchers() == []

    def test_an_escalation_frees_a_submission_waiting_on_backpressure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _BackpressureManager()
        monkeypatch.setattr(Transferrer, "_create_crt_manager", lambda _self: manager)
        client, _ = make_recording_client([])
        token = CancelToken()
        results: list[OpResult] = []
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")

        def item(key: str) -> TransferItem:
            return TransferItem(
                compare_key=key, size=1, src_path=str(src), dest_bucket="b", dest_key=key
            )

        with Transferrer(
            TransferType.UPLOAD, client, cancel_token=token, on_result=results.append
        ) as transferrer:
            transferrer.submit(item("a"))
            threading.Timer(0.3, lambda: token.cancel(mode=CancelMode.IMMEDIATE)).start()
            started = time.monotonic()
            # Blocked on the slot "a" holds until the watcher cancels "a".
            transferrer.submit(item("b"))
            assert time.monotonic() - started < 5.0
        assert manager.cancelled == ["a"]
        assert {result.dest_info or result.dest: result.outcome for result in results} == {
            "s3://b/a": OpOutcome.CANCELLED,
            "s3://b/b": OpOutcome.SUCCEEDED,
        } or [result.outcome for result in results] == [OpOutcome.CANCELLED, OpOutcome.SUCCEEDED]


class TestHandOverRefusal:
    def test_the_managers_synchronous_refusal_is_a_validationerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The CRT manager refuses a checksum algorithm awscrt cannot compute
        # with a bare ValueError before accepting the work (the classic engine's
        # botocore refusal arrives per item instead, FAILED). A caller-argument
        # problem stays inside the taxonomy, like InvalidCrtTransferConfigError
        # at the manager build; measured raw out of S3.cp against MinIO.
        refusal = ValueError(
            "ChecksumAlgorithm: MD5 not supported. Supported algorithms are: "
            "['CRC32C', 'CRC32', 'SHA1', 'SHA256', 'CRC64NVME']"
        )

        class _Refusing(_CrtDrainManager):
            def upload(self, **kwargs: Any) -> _FakeCrtFuture:
                raise refusal

        monkeypatch.setattr(Transferrer, "_create_crt_manager", lambda _self: _Refusing())
        client, _ = make_recording_client([])
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        results: list[OpResult] = []
        transferrer = Transferrer(TransferType.UPLOAD, client, on_result=results.append)
        with pytest.raises(ValidationError) as excinfo, transferrer:
            transferrer.submit(
                TransferItem(
                    compare_key="a.bin",
                    size=1,
                    src_path=str(src),
                    dest_bucket="b",
                    dest_key="a.bin",
                )
            )
        assert excinfo.value.__cause__ is refusal
        assert excinfo.value.operation == "cp"
        assert str(excinfo.value) == str(refusal)
        assert results == []
