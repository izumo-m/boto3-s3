"""``S3.sync`` over the open route: a custom ``Storage`` backend paired with S3.

sync merge-joins two byte-ordered listings, so a custom side must declare
``SORTED_SCAN`` (gated up front - an unsorted side would manufacture phantom
new/delete pairs and, with ``--delete``, corrupt the destination). ``opens3``
syncs a custom source up to S3 (uploads via ``open("rb")``; orphan S3 keys
deleted with ``DeleteObjects``); ``s3open`` syncs an S3 source down into a custom
destination (downloads via ``open("wb")``; orphan custom keys deleted via
``Storage.delete``). The in-memory ``_MemStorage`` family is reused from the
cp/mv open-route tests; the S3 side rides the recording client.

The transfer tests pin ``compare=True`` (copy every source) so the routing -
not the comparator (covered in ``test_s3_sync.py``) - is what is under test.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3.exceptions import ValidationError
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import FileInfo, OpOutcome, OpResult
from tests.lib.test_s3_cp_open import _MemStorage, _NoDeleteMem, _ReadOnlyMem
from tests.utils.recorder import ApiCall, make_recording_client

_SERIAL = TransferConfig(use_threads=False)
_MTIME = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ops(calls: list[ApiCall]) -> list[str]:
    return [call.operation for call in calls]


def _listing(*entries: tuple[str, int]) -> dict[str, Any]:
    return {
        "Contents": [
            {"Key": key, "Size": size, "LastModified": _MTIME, "ETag": '"e"'}
            for key, size in entries
        ]
    }


def _get_response(body: bytes = b"payload") -> dict[str, Any]:
    return {"Body": io.BytesIO(body), "ContentLength": len(body), "ETag": '"abc"'}


class TestSyncOpens3Upload:
    """opens3: a custom source synced up to an S3 destination."""

    def test_uploads_each_source_entry(self) -> None:
        src = _MemStorage({"a.txt": b"x", "sub/b.txt": b"yy"}, location="mem://data/")
        client, calls = make_recording_client([_listing(), {}, {}])  # empty dest, 2 PutObject
        S3().sync(
            src, S3Storage("s3://b/dest/", client=client), compare=True, transfer_config=_SERIAL
        )
        assert _ops(calls) == ["ListObjectsV2", "PutObject", "PutObject"]
        assert calls[0].params["Prefix"] == "dest/"
        assert [call.params["Key"] for call in calls[1:]] == ["dest/a.txt", "dest/sub/b.txt"]

    def test_delete_removes_orphan_s3_keys(self) -> None:
        # The destination is S3, so orphans are deleted with DeleteObjects (the
        # custom source side is never deleted).
        src = _MemStorage({"a.txt": b"x"}, location="mem://data/")
        client, calls = make_recording_client(
            [_listing(("dest/a.txt", 1), ("dest/orphan.txt", 2)), {}, {}]
        )
        S3().sync(
            src,
            S3Storage("s3://b/dest/", client=client),
            compare=True,
            delete=True,
            transfer_config=_SERIAL,
        )
        assert _ops(calls) == ["ListObjectsV2", "PutObject", "DeleteObjects"]
        keys = [entry["Key"] for entry in calls[2].params["Delete"]["Objects"]]
        assert keys == ["dest/orphan.txt"]

    def test_unsorted_source_is_rejected(self) -> None:
        # No SORTED_SCAN -> the merge-join cannot trust the order, so reject up
        # front (an unsorted --delete sync could otherwise corrupt the dest).
        src = _ReadOnlyMem({"a.txt": b"x"}, location="mem://data/")
        client, calls = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().sync(src, S3Storage("s3://b/dest/", client=client), transfer_config=_SERIAL)
        assert "SORTED_SCAN" in str(excinfo.value)
        assert calls == []


class TestSyncS3openDownload:
    """s3open: an S3 source synced down into a custom destination."""

    def test_downloads_each_source_entry(self) -> None:
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/")
        client, calls = make_recording_client(
            [
                _listing(("src/a.txt", 3), ("src/sub/b.txt", 3)),
                _get_response(b"AAA"),
                _get_response(b"BBB"),
            ]
        )
        S3().sync(
            S3Storage("s3://b/src/", client=client), dest, compare=True, transfer_config=_SERIAL
        )
        assert _ops(calls) == ["ListObjectsV2", "GetObject", "GetObject"]
        assert store == {"a.txt": b"AAA", "sub/b.txt": b"BBB"}

    def test_delete_removes_orphan_custom_keys(self) -> None:
        # The destination is custom, so orphans are removed through its own
        # Storage.delete (not os.remove / DeleteObject).
        store = {"a.txt": b"old", "orphan.txt": b"gone"}
        dest = _MemStorage(store, location="mem://data/")
        client, calls = make_recording_client([_listing(("src/a.txt", 3)), _get_response(b"AAA")])
        S3().sync(
            S3Storage("s3://b/src/", client=client),
            dest,
            compare=True,
            delete=True,
            transfer_config=_SERIAL,
        )
        assert _ops(calls) == ["ListObjectsV2", "GetObject"]
        assert dest.deletes == ["orphan.txt"]
        assert store == {"a.txt": b"AAA"}

    def test_delete_capture_surfaces_custom_backend_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A custom destination's Storage.delete response is surfaced under
        # extra_info["delete"] when capture_response=True (ResponseMetadata stripped).
        store = {"orphan.txt": b"gone"}
        dest = _MemStorage(store, location="mem://data/")

        def _delete_with_response(info: FileInfo) -> Mapping[str, Any]:
            del store[info.key]
            return {"VersionId": "v1", "ResponseMetadata": {"HTTPStatusCode": 204}}

        monkeypatch.setattr(dest, "delete", _delete_with_response)
        # empty source -> nothing to transfer, only the orphan removal
        client, _calls = make_recording_client([_listing()])
        results: list[OpResult] = []
        S3().sync(
            S3Storage("s3://b/src/", client=client),
            dest,
            delete=True,
            capture_response=True,
            on_result=results.append,
            transfer_config=_SERIAL,
        )
        assert [r.extra_info for r in results] == [{"delete": {"VersionId": "v1"}}]

    def test_delete_without_delete_capability_is_rejected(self) -> None:
        dest = _NoDeleteMem({}, location="mem://data/")
        client, calls = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().sync(
                S3Storage("s3://b/src/", client=client),
                dest,
                delete=True,
                transfer_config=_SERIAL,
            )
        assert "DELETE" in str(excinfo.value)
        assert calls == []

    def test_no_delete_does_not_require_delete_capability(self) -> None:
        # Without --delete the custom destination needs no DELETE; the sync runs.
        store: dict[str, bytes] = {}
        dest = _NoDeleteMem(store, location="mem://data/")
        client, calls = make_recording_client([_listing(("src/a.txt", 3)), _get_response(b"AAA")])
        S3().sync(
            S3Storage("s3://b/src/", client=client), dest, compare=True, transfer_config=_SERIAL
        )
        assert _ops(calls) == ["ListObjectsV2", "GetObject"]
        assert store == {"a.txt": b"AAA"}

    def test_dryrun_lists_but_never_opens_the_backend(self) -> None:
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/")
        client, calls = make_recording_client([_listing(("src/a.txt", 3))])
        results: list[OpResult] = []
        S3().sync(
            S3Storage("s3://b/src/", client=client),
            dest,
            compare=True,
            dryrun=True,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert _ops(calls) == ["ListObjectsV2"]
        assert store == {}
        assert dest.opens == []
        assert [r.outcome for r in results] == [OpOutcome.DRYRUN]
