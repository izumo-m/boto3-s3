"""``S3.sync`` over the open route: a custom ``Storage`` backend paired with S3.

sync merge-joins two byte-ordered listings, so a custom side must declare
``SORTABLE_SCAN`` (gated up front - an unsorted side would manufacture phantom
new/delete pairs and, with ``--delete``, corrupt the destination). ``opens3``
syncs a custom source up to S3 (uploads via ``open("rb")``; orphan S3 keys
deleted with ``DeleteObjects``); ``s3open`` syncs an S3 source down into a custom
destination (downloads via ``open("wb")``; orphan custom keys deleted via
``Storage.delete``). The in-memory ``_MemStorage`` family is reused from the
cp/mv open-route tests; the S3 side rides the recording client.

The transfer tests pin ``update_filter=True`` (copy every source) so the routing -
not the comparator (covered in ``test_s3_sync.py``) - is what is under test.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3.exceptions import ValidationError
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import FileInfo, OpOutcome, OpResult
from tests.lib.test_s3_cp_open import _MemStorage, _NoDeleteMem, _ReadOnlyMem
from tests.utils.fakes3 import get_response, listing
from tests.utils.recorder import make_recording_client, ops

_SERIAL = TransferConfig(use_threads=False)


class TestSyncOpens3Upload:
    """opens3: a custom source synced up to an S3 destination."""

    def test_uploads_each_source_entry(self) -> None:
        src = _MemStorage({"a.txt": b"x", "sub/b.txt": b"yy"}, location="mem://data/")
        client, calls = make_recording_client([listing(), {}, {}])  # empty dest, 2 PutObject
        S3().sync(
            src,
            S3Storage("s3://b/dest/", client=client),
            update_filter=True,
            transfer_config=_SERIAL,
        )
        assert ops(calls) == ["ListObjectsV2", "PutObject", "PutObject"]
        assert calls[0].params["Prefix"] == "dest/"
        assert [call.params["Key"] for call in calls[1:]] == ["dest/a.txt", "dest/sub/b.txt"]

    def test_delete_removes_orphan_s3_keys(self) -> None:
        # The destination is S3, so orphans are deleted with DeleteObjects (the
        # custom source side is never deleted).
        src = _MemStorage({"a.txt": b"x"}, location="mem://data/")
        client, calls = make_recording_client(
            [listing(("dest/a.txt", 1), ("dest/orphan.txt", 2)), {}, {}]
        )
        S3().sync(
            src,
            S3Storage("s3://b/dest/", client=client),
            update_filter=True,
            delete_filter=True,
            transfer_config=_SERIAL,
        )
        assert ops(calls) == ["ListObjectsV2", "PutObject", "DeleteObjects"]
        keys = [entry["Key"] for entry in calls[2].params["Delete"]["Objects"]]
        assert keys == ["dest/orphan.txt"]

    def test_custom_listing_is_requested_sorted(self) -> None:
        # docs/sync.md: the merge-join depends on byte order, so the custom
        # side's scan_pages must receive ScanOptions(sort=True) - declaring
        # SORTABLE_SCAN alone (the gate) is not the same as being asked to
        # sort this scan.
        class _OptionsRecordingMem(_MemStorage):
            def __init__(self, store: dict[str, bytes], *, location: str = "mem://data/") -> None:
                super().__init__(store, location=location)
                self.scan_options: list[Any] = []

            def scan_pages(self, options: Any) -> Any:
                self.scan_options.append(options)
                return super().scan_pages(options)

        src = _OptionsRecordingMem({"a.txt": b"x"}, location="mem://data/")
        client, calls = make_recording_client([listing(), {}])
        S3().sync(
            src,
            S3Storage("s3://b/dest/", client=client),
            update_filter=True,
            transfer_config=_SERIAL,
        )
        assert ops(calls) == ["ListObjectsV2", "PutObject"]
        assert [options.sort for options in src.scan_options] == [True]

    def test_unsorted_source_is_rejected(self) -> None:
        # No SORTABLE_SCAN -> the merge-join cannot trust the order, so reject up
        # front (an unsorted --delete sync could otherwise corrupt the dest).
        src = _ReadOnlyMem({"a.txt": b"x"}, location="mem://data/")
        client, calls = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().sync(src, S3Storage("s3://b/dest/", client=client), transfer_config=_SERIAL)
        assert "SORTABLE_SCAN" in str(excinfo.value)
        assert calls == []


class TestSyncS3openDownload:
    """s3open: an S3 source synced down into a custom destination."""

    def test_downloads_each_source_entry(self) -> None:
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/")
        client, calls = make_recording_client(
            [
                listing(("src/a.txt", 3), ("src/sub/b.txt", 3)),
                get_response(b"AAA"),
                get_response(b"BBB"),
            ]
        )
        S3().sync(
            S3Storage("s3://b/src/", client=client),
            dest,
            update_filter=True,
            transfer_config=_SERIAL,
        )
        assert ops(calls) == ["ListObjectsV2", "GetObject", "GetObject"]
        assert store == {"a.txt": b"AAA", "sub/b.txt": b"BBB"}

    def test_no_overwrite_skips_existing_pairs(self) -> None:
        # docs/transfer.md: sync + no_overwrite on a custom destination skips
        # any key that already exists there (the pair never reaches the
        # comparator), and only the genuinely new key transfers.
        store = {"a.txt": b"old"}
        dest = _MemStorage(store, location="mem://data/")
        client, calls = make_recording_client(
            [listing(("src/a.txt", 3), ("src/b.txt", 3)), get_response(b"BBB")]
        )
        S3().sync(
            S3Storage("s3://b/src/", client=client),
            dest,
            update_filter=True,
            no_overwrite=True,
            transfer_config=_SERIAL,
        )
        assert ops(calls) == ["ListObjectsV2", "GetObject"]
        assert store == {"a.txt": b"old", "b.txt": b"BBB"}

    def test_delete_removes_orphan_custom_keys(self) -> None:
        # The destination is custom, so orphans are removed through its own
        # Storage.delete (not os.remove / DeleteObject).
        store = {"a.txt": b"old", "orphan.txt": b"gone"}
        dest = _MemStorage(store, location="mem://data/")
        client, calls = make_recording_client([listing(("src/a.txt", 3)), get_response(b"AAA")])
        S3().sync(
            S3Storage("s3://b/src/", client=client),
            dest,
            update_filter=True,
            delete_filter=True,
            transfer_config=_SERIAL,
        )
        assert ops(calls) == ["ListObjectsV2", "GetObject"]
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
        client, _calls = make_recording_client([listing()])
        results: list[OpResult] = []
        S3().sync(
            S3Storage("s3://b/src/", client=client),
            dest,
            delete_filter=True,
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
                delete_filter=True,
                transfer_config=_SERIAL,
            )
        assert "DELETE" in str(excinfo.value)
        assert calls == []

    def test_no_delete_does_not_require_delete_capability(self) -> None:
        # Without --delete the custom destination needs no DELETE; the sync runs.
        store: dict[str, bytes] = {}
        dest = _NoDeleteMem(store, location="mem://data/")
        client, calls = make_recording_client([listing(("src/a.txt", 3)), get_response(b"AAA")])
        S3().sync(
            S3Storage("s3://b/src/", client=client),
            dest,
            update_filter=True,
            transfer_config=_SERIAL,
        )
        assert ops(calls) == ["ListObjectsV2", "GetObject"]
        assert store == {"a.txt": b"AAA"}

    def test_dryrun_lists_but_never_opens_the_backend(self) -> None:
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/")
        client, calls = make_recording_client([listing(("src/a.txt", 3))])
        results: list[OpResult] = []
        S3().sync(
            S3Storage("s3://b/src/", client=client),
            dest,
            update_filter=True,
            dryrun=True,
            transfer_config=_SERIAL,
            on_result=results.append,
        )
        assert ops(calls) == ["ListObjectsV2"]
        assert store == {}
        assert dest.opens == []
        assert [r.outcome for r in results] == [OpOutcome.DRYRUN]
