"""``boto3_s3.transfer``: the s3transfer-backed engine, against a recording client.

The recorder replaces ``client._make_api_call`` (the same seam aws-cli's own
functional tests stub at the HTTP layer), so the genuine s3transfer
submission paths run - single-part and multipart - while every API call is
recorded. ``TransferConfig(use_threads=False)`` selects the
NonThreadedExecutor exactly like boto3 does, making multipart call order
deterministic.
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from boto3_s3 import transfer
from boto3_s3.exceptions import ConfigurationError, NotFoundError, ValidationError
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
    CopyPropsMode,
    FileInfo,
    OpOutcome,
    OpResult,
    TransferOptions,
    TransferProgress,
    TransferType,
)
from tests.utils.fakemodel import model_only_client
from tests.utils.recorder import ApiCall, make_recording_client

_SYNC_CONFIG = TransferConfig(use_threads=False)
_MIB = 1024 * 1024


def _client_error(code: str, status: int, operation: str) -> ClientError:
    response: Any = {
        "Error": {"Code": code, "Message": "stub"},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return ClientError(response, operation)


def _ops(calls: list[ApiCall]) -> list[str]:
    return [call.operation for call in calls]


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
    responses: list[dict[str, Any] | Exception],
    *,
    source_responses: list[dict[str, Any] | Exception] | None = None,
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
        assert _ops(calls) == ["PutObject"]
        assert calls[0].params["Bucket"] == "bucket"
        assert calls[0].params["Key"] == "up/a.bin"
        assert (transferrer.succeeded, transferrer.failed) == (1, 0)
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert results[0].src == str(src)
        assert results[0].dest == "s3://bucket/up/a.bin"
        assert results[0].bytes_transferred == 7

    def test_result_has_no_extra_info(self, tmp_path: Path) -> None:
        # s3transfer discards the PutObject response, so an upload surfaces no
        # result ETag (docs/transfer.md).
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        _, _, results, _ = _run(TransferType.UPLOAD, [item], [{}])
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
        assert _ops(calls) == [
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
            TransferType.UPLOAD, [item], [_client_error("NoSuchBucket", 404, "PutObject")]
        )
        assert _ops(calls) == ["PutObject"]
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert isinstance(transferrer.first_error, NotFoundError)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert str(results[0].error).startswith("An error occurred (NoSuchBucket)")

    def test_invalid_grants_raise_at_submit_time(self, tmp_path: Path) -> None:
        # aws maps request params per item inside its pipeline, so a bad
        # --grants surfaces in-flight (fatal error, rc 1) - never upfront.
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
            etag="abc123",
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
        assert _ops(calls) == ["GetObject"]
        target = tmp_path / "out" / "a.bin"
        assert target.read_bytes() == b"payload"
        assert item.mtime is not None
        assert os.stat(target).st_mtime == item.mtime.timestamp()
        assert transferrer.succeeded == 1
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]

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
        assert "but was unable to update the last modified time." in str(warned.error)
        assert str(warned.error).startswith(f"Skipping file {item.dest_path}.")
        # EPERM gets aws's re-wording (aws-cli set_file_utime).
        assert "attempting to modify the utime of the file failed" in str(warned.error)

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
        assert _ops(calls) == ["HeadObject", "GetObject"]
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
            etag="abc123",
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
        assert _ops(calls) == ["CopyObject"]
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
        assert _ops(calls) == [
            "CreateMultipartUpload",
            "UploadPartCopy",
            "UploadPartCopy",
            "CompleteMultipartUpload",
        ]
        assert source_calls == []

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
        # Where s3transfer still forwards inline Tagging to the create call
        # (< upstream 0.19) the small set rides the header; where it is
        # blacklisted the engine routes it through the post-copy
        # PutObjectTagging instead (transfer._mpu_inline_tagging_supported).
        inline = transfer._mpu_inline_tagging_supported()
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {},
        ]
        if not inline:
            responses.append({})  # PutObjectTagging
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
        assert _ops(source_calls) == ["HeadObject", "GetObjectTagging"]
        assert source_calls[0].params == {"Bucket": "src-b", "Key": "d/a.bin"}
        create = calls[0]
        assert create.params["ContentType"] == "text/css"
        if inline:
            assert create.params["Tagging"] == "team=a%26b"
        else:
            assert "Tagging" not in create.params
            assert _ops(calls)[-1] == "PutObjectTagging"
            assert calls[-1].params["Tagging"] == {"TagSet": [{"Key": "team", "Value": "a&b"}]}
        assert transferrer.succeeded == 1

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

    def test_all_multipart_default_preloads_then_uses_native_write_path(self) -> None:
        # Reads are completed before submission. s3transfer >= 0.19 still owns
        # the post-complete writes and pins ObjectIfMatch to the new ETag.
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
            {"Annotations": [{"AnnotationName": "ann1"}]},
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
        assert _ops(source_calls) == [
            "HeadObject",
            "GetObjectTagging",
            "ListObjectAnnotations",
            "GetObjectAnnotation",
        ]
        assert source_calls[2].params == {"Bucket": "src-b", "Key": "d/a.bin"}
        assert _ops(calls)[-1] == "PutObjectAnnotation"
        assert calls[-1].params == {
            "Bucket": "dest-b",
            "Key": "cp/a.bin",
            "AnnotationName": "ann1",
            "AnnotationPayload": b"payload",
            "ObjectIfMatch": '"dest-etag"',
            "VersionId": "dest-v1",
        }
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
                {},  # native-path failure cleanup AbortMultipartUpload
            ],
            source_responses=[
                {},
                {"TagSet": []},
                _client_error("AccessDenied", 403, "ListObjectAnnotations"),
            ],
            options=TransferOptions(
                copy_props=CopyPropsMode.ALL,
                annotation_copy_mode=AnnotationCopyMode.DEFERRED,
            ),
        )

        assert _ops(source_calls)[-1] == "ListObjectAnnotations"
        assert _ops(calls) == [
            "CreateMultipartUpload",
            "UploadPartCopy",
            "UploadPartCopy",
            "CompleteMultipartUpload",
            "AbortMultipartUpload",
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
            _client_error("AccessDenied", 403, "ListObjectAnnotations"),
        ]

        calls, source_calls, results, transferrer = _run(
            TransferType.COPY,
            [self._item(size=9 * _MIB)],
            responses,
            source_responses=source_responses,
            options=TransferOptions(copy_props=CopyPropsMode.ALL),
        )

        assert _ops(source_calls) == [
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
                "Annotations": [{"AnnotationName": "ann1"}],
                "NextContinuationToken": "next",
            },
            {"Annotations": [{"AnnotationName": "ann2"}]},
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

        assert _ops(source_calls) == [
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
                        {"AnnotationName": "ann1"},
                        {"AnnotationName": "ann2"},
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

        assert _ops(source_calls) == ["HeadObject", "GetObjectTagging"]
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
                {"Annotations": [{"AnnotationName": "ann1"}]},
                _client_error("AccessDenied", 403, "GetObjectAnnotation"),
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
        assert _ops(calls)[-1] == "PutObjectTagging"
        assert calls[-1].params["Tagging"] == {"TagSet": [{"Key": "k", "Value": big}]}
        assert "Tagging" not in calls[0].params
        assert transferrer.succeeded == 1
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]

    def test_post_copy_tagging_failure_rolls_back_the_destination(self) -> None:
        big = "v" * 3000
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {},
            _client_error("AccessDenied", 403, "PutObjectTagging"),
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
        assert _ops(calls)[-2:] == ["PutObjectTagging", "DeleteObject"]
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
            [_client_error("PreconditionFailed", 412, "PutObject")],
            options=TransferOptions(no_overwrite=True),
        )
        assert _ops(calls) == ["PutObject"]
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
        assert _ops(calls) == [
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
            etag="abc123",
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
        assert calls[3].params["IfNoneMatch"] == "*"


class TestMove:
    """``is_move``: delete-the-source semantics + the MOVE reporting kind."""

    def _download_item(self, tmp_path: Path) -> TransferItem:
        return TransferItem(
            compare_key="a.bin",
            size=7,
            etag="abc123",
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
        assert _ops(calls) == ["PutObject"]
        assert not src.exists()
        assert transferrer.succeeded == 1
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert all(result.transfer_type is TransferType.MOVE for result in results)

    def test_download_move_deletes_on_the_managers_client(self, tmp_path: Path) -> None:
        item = self._download_item(tmp_path)
        calls, _, results, transferrer = _run(
            TransferType.DOWNLOAD, [item], [self._get_object_response(), {}], is_move=True
        )
        assert _ops(calls) == ["GetObject", "DeleteObject"]
        assert calls[1].params == {"Bucket": "bucket", "Key": "d/a.bin"}
        target = tmp_path / "out" / "a.bin"
        assert target.read_bytes() == b"payload"
        # The mtime stamp still lands (after the delete in our ordering).
        assert item.mtime is not None
        assert os.stat(target).st_mtime == item.mtime.timestamp()
        assert transferrer.succeeded == 1
        assert [result.transfer_type for result in results] == [TransferType.MOVE]

    def test_copy_move_deletes_on_the_source_client(self) -> None:
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag="abc123",
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )
        calls, source_calls, _, transferrer = _run(
            TransferType.COPY, [item], [{}], source_responses=[{}], is_move=True
        )
        assert _ops(calls) == ["CopyObject"]
        assert _ops(source_calls) == ["DeleteObject"]
        assert source_calls[0].params == {"Bucket": "src-b", "Key": "d/a.bin"}
        assert transferrer.succeeded == 1

    def test_copy_move_delete_failure_attributes_to_the_source(self) -> None:
        # Copy lands, the source DeleteObject fails: the structured error must
        # name the *source* object (the one that failed to delete), not the copy
        # destination that succeeded.
        item = TransferItem(
            compare_key="a.bin",
            size=7,
            etag="abc123",
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )
        _, source_calls, results, transferrer = _run(
            TransferType.COPY,
            [item],
            [{}],
            source_responses=[_client_error("AccessDenied", 403, "DeleteObject")],
            is_move=True,
        )
        assert _ops(source_calls) == ["DeleteObject"]
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        error = results[0].error
        assert error is not None
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
            [self._get_object_response(), _client_error("AccessDenied", 403, "DeleteObject")],
            is_move=True,
        )
        assert _ops(calls) == ["GetObject", "DeleteObject"]
        # The bytes are on disk (aws ditto), but the move reports failed.
        assert (tmp_path / "out" / "a.bin").read_bytes() == b"payload"
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert transferrer.first_error is not None
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert str(results[0].error).startswith("An error occurred (AccessDenied)")

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
        # aws prints the OS's own wording after "move failed: ..." - compare via
        # str(OSError(...)): that rendering quotes the filename with repr, so the
        # expectation stays exact where the path itself contains backslashes.
        assert str(results[0].error) == str(OSError(13, "Permission denied", str(src)))

    def test_transfer_failure_leaves_the_source(self, tmp_path: Path) -> None:
        src = tmp_path / "a.bin"
        src.write_bytes(b"x")
        item = TransferItem(
            compare_key="a.bin", size=1, src_path=str(src), dest_bucket="b", dest_key="k"
        )
        calls, _, results, transferrer = _run(
            TransferType.UPLOAD,
            [item],
            [_client_error("NoSuchBucket", 404, "PutObject")],
            is_move=True,
        )
        assert _ops(calls) == ["PutObject"]
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
            [_client_error("PreconditionFailed", 412, "PutObject")],
            options=TransferOptions(no_overwrite=True),
            is_move=True,
        )
        assert _ops(calls) == ["PutObject"]
        assert src.exists()
        assert (transferrer.failed, transferrer.skipped) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.SKIPPED]

    def test_post_copy_tagging_failure_keeps_the_source(self) -> None:
        # _SetTags settles (rolls back the destination, flips the future)
        # before _DeleteSource looks at it: the source object survives.
        big = "v" * 3000
        item = TransferItem(
            compare_key="a.bin",
            size=9 * _MIB,
            etag="abc123",
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {},
            _client_error("AccessDenied", 403, "PutObjectTagging"),
            {},  # rollback DeleteObject (destination side)
        ]
        source_responses: list[dict[str, Any] | Exception] = [
            {},
            {"TagSet": [{"Key": "k", "Value": big}]},
        ]
        calls, source_calls, results, transferrer = _run(
            TransferType.COPY, [item], responses, source_responses=source_responses, is_move=True
        )
        assert _ops(calls)[-2:] == ["PutObjectTagging", "DeleteObject"]
        assert calls[-1].params == {"Bucket": "dest-b", "Key": "cp/a.bin"}
        # No source-side delete: the only source calls are the props reads.
        assert _ops(source_calls) == ["HeadObject", "GetObjectTagging"]
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]

    def test_post_copy_tagging_and_rollback_both_fail_succeeds(self) -> None:
        # aws parity: when the post-copy PutObjectTagging fails and the rollback
        # DeleteObject also fails, aws-cli lets the delete error escape the done
        # callback (swallowed) without flipping the future - the transfer is a
        # success. We mirror that: the mv's source delete then proceeds.
        big = "v" * 3000
        item = TransferItem(
            compare_key="a.bin",
            size=9 * _MIB,
            etag="abc123",
            src_bucket="src-b",
            src_key="d/a.bin",
            dest_bucket="dest-b",
            dest_key="cp/a.bin",
        )
        responses: list[dict[str, Any] | Exception] = [
            {"UploadId": "u"},
            {"CopyPartResult": {"ETag": '"p1"'}},
            {"CopyPartResult": {"ETag": '"p2"'}},
            {},
            _client_error("AccessDenied", 403, "PutObjectTagging"),
            _client_error("AccessDenied", 403, "DeleteObject"),  # rollback also fails
        ]
        source_responses: list[dict[str, Any] | Exception] = [
            {},
            {"TagSet": [{"Key": "k", "Value": big}]},
            {},  # mv source-side DeleteObject
        ]
        calls, source_calls, results, transferrer = _run(
            TransferType.COPY, [item], responses, source_responses=source_responses, is_move=True
        )
        assert _ops(calls)[-2:] == ["PutObjectTagging", "DeleteObject"]
        # The source delete proceeds - the transfer is recorded as a success.
        assert _ops(source_calls) == ["HeadObject", "GetObjectTagging", "DeleteObject"]
        assert source_calls[-1].params == {"Bucket": "src-b", "Key": "d/a.bin"}
        assert (transferrer.succeeded, transferrer.failed) == (1, 0)
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]

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
            etag="abc123",
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
        assert _ops(calls) == ["GetObject", "DeleteObject"]
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
        assert _ops(calls) == ["GetObject"]
        assert (tmp_path / "out" / "a.bin").read_bytes() == b"payload"
        assert (transferrer.succeeded, transferrer.failed) == (0, 1)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert "Input/output error" in str(results[0].error)
        assert str(results[0].error).startswith("Failed to persist ")

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
        assert _ops(calls) == ["GetObject", "DeleteObject"]
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
        assert _ops(calls) == ["GetObject"]
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
            etag="abc123",
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
        assert _ops(calls) == ["PutObject"]
        assert calls[0].params["Key"] == "streaming.txt"
        # No path means no mimetypes guess: ContentType stays unset.
        assert "ContentType" not in calls[0].params
        assert transferrer.succeeded == 1
        assert results[-1].src == "-"

    def test_stream_upload_with_expected_size(self) -> None:
        item = TransferItem(
            compare_key="streaming.txt",
            size=4,
            src_fileobj=io.BytesIO(b"foo\n"),
            dest_bucket="bucket",
            dest_key="streaming.txt",
        )
        calls, _, results, transferrer = _run(TransferType.UPLOAD, [item], [{}])
        assert _ops(calls) == ["PutObject"]
        assert transferrer.succeeded == 1
        # The provided size reaches the future (aws ProvideSizeSubscriber) and
        # SUCCEEDED reports it, unlike the size-less streaming upload above.
        assert results[0].bytes_transferred == 4

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
        assert _ops(calls) == ["HeadObject", "GetObject"]
        assert sink.getvalue() == b"foo\n"
        assert transferrer.succeeded == 1


class TestEngineSelection:
    """``preferred_transfer_client`` resolution at the manager seam (docs/crt.md)."""

    def _transferrer(self, kind: TransferType, config: Any) -> Transferrer:
        client, _ = make_recording_client([])
        return Transferrer(kind, client, transfer_config=config)

    def test_default_auto_resolves_classic_on_a_non_optimized_host(self) -> None:
        from s3transfer.manager import TransferManager

        # conftest pins is_optimized_for_system to False; boto3's 'auto'
        # answer there is the classic manager.
        manager = self._transferrer(TransferType.UPLOAD, None)._get_manager()
        assert isinstance(manager, TransferManager)

    def test_explicit_crt_delegates_to_crtsupport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from boto3_s3 import crtsupport

        sentinel = object()
        seen: list[Any] = []

        def fake_create(
            client: Any, config: Any, *, endpoint: str | None = None, session: Any | None = None
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
            client: Any, config: Any, *, endpoint: str | None = None, session: Any | None = None
        ) -> Any:
            seen.append(endpoint)
            return object()

        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", fake_create)
        vpce = "https://bucket.vpce-0abc.s3.us-east-1.vpce.amazonaws.com"
        client, _ = make_recording_client([])
        transferrer = Transferrer(
            TransferType.UPLOAD,
            client,
            transfer_config=TransferConfig(preferred_transfer_client="crt"),
            crt_endpoint=vpce,
        )
        transferrer._get_manager()
        assert seen == [vpce]

    def test_session_is_threaded_to_crtsupport(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from boto3_s3 import crtsupport

        seen: list[Any] = []

        def fake_create(
            client: Any, config: Any, *, endpoint: str | None = None, session: Any | None = None
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

    def test_copy_kind_is_unconditionally_classic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from s3transfer.manager import TransferManager

        from boto3_s3 import crtsupport

        def boom(
            client: Any, config: Any, *, endpoint: str | None = None, session: Any | None = None
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
        monkeypatch.setattr(
            crtsupport,
            "create_crt_transfer_manager",
            lambda c, cfg, *, endpoint=None, session=None: None,
        )
        config = TransferConfig(preferred_transfer_client="crt")
        manager = self._transferrer(TransferType.UPLOAD, config)._get_manager()
        assert isinstance(manager, TransferManager)


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


class TestPublicSurface:
    def test_all_matches_the_documented_surface(self) -> None:
        # The module is a documented submodule-path surface (docs/transfer.md):
        # the engine pair plus the SDK-floor probes (--no-overwrite, section 7;
        # copy_props=ALL, section 4). A symbol added or dropped must be a
        # deliberate __all__ / docs decision.
        assert set(transfer.__all__) == {
            "TransferItem",
            "Transferrer",
            "annotations_copy_unsupported_reason",
            "conditional_write_unsupported_reason",
        }
        for name in transfer.__all__:
            assert hasattr(transfer, name), name


class TestConditionalWriteSupport:
    """The --no-overwrite (IfNoneMatch) old-botocore gate, library side.

    IfNoneMatch reached the S3 write ops only in later botocore (PutObject in
    1.35.16, CopyObject in 1.41.0); below that --no-overwrite must be rejected
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
        assert "1.35.16" in reason and "PutObject" in reason

    def test_reason_names_min_botocore_for_copy(self) -> None:
        # PutObject present but CopyObject not: copy needs the later 1.41.0.
        reason = conditional_write_unsupported_reason(
            model_only_client({"PutObject"}), is_copy=True
        )
        assert reason is not None
        assert "1.41.0" in reason and "CopyObject" in reason

    def test_upload_refused_without_s3transfer_create_blocklist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # s3transfer < 0.11 hands the full extra_args to CreateMultipartUpload
        # (no CREATE_MULTIPART_BLOCKLIST), so a botocore that models
        # IfNoneMatch - the real boto3 1.35.16+ / s3transfer 0.10.x pairing -
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
        with pytest.raises(ConfigurationError, match=r"1\.35\.16"):
            Transferrer(
                TransferType.UPLOAD, model_only_client(set()), options={"no_overwrite": True}
            )

    def test_transferrer_rejects_no_overwrite_copy_on_old_botocore(self) -> None:
        client = model_only_client({"PutObject"})  # upload ok, copy not yet
        with pytest.raises(ConfigurationError, match=r"1\.41\.0"):
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


class TestAnnotationsCopySupport:
    """The copy_props=ALL SDK gate (docs/transfer.md section 4).

    Annotations need botocore's S3 model (CopyObject.AnnotationDirective,
    1.43.31) and s3transfer's own multipart handling (the directive in
    CopySubmissionTask.CREATE_MULTIPART_ARGS_BLACKLIST, 0.19); below either,
    ALL must be refused with a clear message. The installed dev SDK satisfies
    both, so the s3transfer side is exercised by trimming the blacklist.
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

    def test_reason_names_min_s3transfer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from s3transfer.copies import CopySubmissionTask

        trimmed = [
            arg
            for arg in CopySubmissionTask.CREATE_MULTIPART_ARGS_BLACKLIST
            if arg != "AnnotationDirective"
        ]
        monkeypatch.setattr(CopySubmissionTask, "CREATE_MULTIPART_ARGS_BLACKLIST", trimmed)
        reason = annotations_copy_unsupported_reason(self._capable_client())
        assert reason is not None
        assert "0.19" in reason and "s3transfer" in reason

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
        # as unspecified (the falsy convention the other options follow),
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
        # (docs/exceptions.md). The CLI never reaches here (choices-validated).
        client = self._capable_client()
        with pytest.raises(ValidationError, match="copy_props"):
            Transferrer(
                TransferType.COPY,
                client,
                source_client=client,
                options=cast(TransferOptions, {"copy_props": "al"}),
            )

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
