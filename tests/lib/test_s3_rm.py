"""Unit tests for ``S3.rm`` orchestration and ``rm_filter_root``.

The fake client covers the three wire surfaces rm uses: ListObjectsV2
pagination (enumerating paths), a single blind DeleteObject (the
non-recursive key path), and batched DeleteObjects (via S3Deleter).
aws-cli parity facts asserted here:

- a recursive prefix is normalized to end with "/" before listing, so
  ``rm s3://b/data --recursive`` never touches ``data-sibling.txt``;
- a keyless non-recursive rm sweeps only zero-byte "folder marker"
  objects (any depth);
- the single-key path is blind: no listing, no existence check;
- item failures aggregate into BatchError, pre-item failures (the
  listing itself) propagate as their category error.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from botocore.exceptions import ClientError

from boto3_s3 import (
    S3,
    BatchError,
    FileInfo,
    GlobFilter,
    NotFoundError,
    OpOutcome,
    OpResult,
    S3Storage,
    ValidationError,
    rm_filter_root,
)

_MTIME = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)


def _client_error(code: str, operation: str, status: int = 404) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "x"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        operation,
    )


class _FakePaginator:
    def __init__(self, owner: _FakeS3Client) -> None:
        self._owner = owner

    def paginate(self, **kwargs: Any) -> Any:
        self._owner.list_calls.append(kwargs)
        if self._owner.list_error is not None:
            raise self._owner.list_error
        return iter(self._owner.pages)


class _FakeS3Client:
    """Fake covering the rm wire surface (list / delete_object / delete_objects)."""

    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        *,
        list_error: Exception | None = None,
        delete_object_error: Exception | None = None,
        delete_objects_errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.pages = pages or []
        self.list_error = list_error
        self.delete_object_error = delete_object_error
        self.delete_objects_errors = delete_objects_errors or []
        self.list_calls: list[dict[str, Any]] = []
        self.delete_object_calls: list[dict[str, Any]] = []
        self.delete_objects_calls: list[dict[str, Any]] = []

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator(self)

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_object_calls.append(kwargs)
        if self.delete_object_error is not None:
            raise self.delete_object_error
        return {}

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_objects_calls.append(kwargs)
        return {"Errors": self.delete_objects_errors}


def _obj(key: str, size: int = 1) -> dict[str, Any]:
    return {"Key": key, "Size": size, "LastModified": _MTIME}


def _rm(
    url: str,
    client: _FakeS3Client,
    **kwargs: Any,
) -> list[OpResult]:
    """Run S3().rm with a result collector and return the OpResults."""
    results: list[OpResult] = []
    storage = S3Storage(url, client=client)
    S3().rm(storage, on_result=results.append, **kwargs)
    return results


class TestRmFilterRoot:
    @pytest.mark.parametrize(
        ("key", "recursive", "expected"),
        [
            ("", False, ""),  # bucket-root marker sweep
            ("", True, ""),  # full-bucket recursive
            ("a.txt", False, ""),  # top-level key roots at bucket
            ("data/a.txt", False, "data/"),  # parent "directory"
            ("data/", False, "data/"),  # explicit marker target roots at itself
            ("data", True, "data/"),  # recursive normalizes to "/"
            ("data/", True, "data/"),
            ("data/sub", True, "data/sub/"),
        ],
    )
    def test_root(self, key: str, recursive: bool, expected: str) -> None:
        assert rm_filter_root(key, recursive=recursive) == expected


class TestRmSingleKey:
    def test_blind_delete_no_listing(self) -> None:
        client = _FakeS3Client()
        results = _rm("s3://b/data/a.txt", client)
        assert client.list_calls == []
        assert client.delete_object_calls == [{"Bucket": "b", "Key": "data/a.txt"}]
        assert [(r.key, r.outcome) for r in results] == [("data/a.txt", OpOutcome.SUCCEEDED)]

    def test_result_carries_src_info_and_storage(self) -> None:
        # The deleted object rides through on src / src_info / src_storage (aws's
        # delete result shape is src=path, dest=None), so an app can identify and
        # re-reach exactly what was removed.
        client = _FakeS3Client()
        results = _rm("s3://b/data/a.txt", client)
        assert results[0].src == "s3://b/data/a.txt"
        assert results[0].src_info is not None and results[0].src_info.key == "data/a.txt"
        assert isinstance(results[0].src_storage, S3Storage)
        assert results[0].dest_info is None

    def test_request_payer_forwarded(self) -> None:
        client = _FakeS3Client()
        _rm("s3://b/k", client, request_payer="requester")
        assert client.delete_object_calls == [
            {"Bucket": "b", "Key": "k", "RequestPayer": "requester"}
        ]

    def test_trailing_slash_key_is_a_blind_marker_delete(self) -> None:
        # aws: "rm s3://b/dir/" short-circuits to one DeleteObject of "dir/".
        client = _FakeS3Client()
        _rm("s3://b/dir/", client)
        assert client.list_calls == []
        assert client.delete_object_calls == [{"Bucket": "b", "Key": "dir/"}]

    def test_dryrun_makes_no_api_calls(self) -> None:
        client = _FakeS3Client()
        results = _rm("s3://b/no-such", client, dryrun=True)
        assert client.delete_object_calls == []
        assert client.list_calls == []
        assert [(r.key, r.outcome) for r in results] == [("no-such", OpOutcome.DRYRUN)]

    def test_failure_emits_failed_and_raises_batch_error(self) -> None:
        client = _FakeS3Client(delete_object_error=_client_error("NoSuchBucket", "DeleteObject"))
        results: list[OpResult] = []
        storage = S3Storage("s3://b-no-such/k", client=client)
        with pytest.raises(BatchError) as exc_info:
            S3().rm(storage, on_result=results.append)
        assert (exc_info.value.succeeded, exc_info.value.failed) == (0, 1)
        assert isinstance(exc_info.value.__cause__, NotFoundError)
        assert isinstance(exc_info.value.__cause__.__cause__, ClientError)
        assert [(r.key, r.outcome) for r in results] == [("k", OpOutcome.FAILED)]
        assert isinstance(results[0].error, NotFoundError)

    def test_excluded_by_matcher_is_silent(self) -> None:
        client = _FakeS3Client()
        keep = GlobFilter().exclude("*").compile()
        results = _rm("s3://b/data/a.txt", client, filter=keep)
        assert client.delete_object_calls == []
        assert results == []

    def test_matcher_sees_parent_relative_key(self) -> None:
        # Root of "data/a.txt" is "data/": the pattern matches the basename.
        client = _FakeS3Client()
        keep = GlobFilter().exclude("a.*").compile()
        assert _rm("s3://b/data/a.txt", client, filter=keep) == []
        assert client.delete_object_calls == []


class TestRmRecursive:
    def test_deletes_all_keys_in_batches(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("data/a"), _obj("data/b")]}])
        results = _rm("s3://b/data/", client, recursive=True)
        assert client.delete_objects_calls == [
            {
                "Bucket": "b",
                "Delete": {
                    "Objects": [{"Key": "data/a"}, {"Key": "data/b"}],
                    "Quiet": True,
                },
            }
        ]
        assert [(r.key, r.outcome) for r in results] == [
            ("data/a", OpOutcome.SUCCEEDED),
            ("data/b", OpOutcome.SUCCEEDED),
        ]

    def test_prefix_without_slash_is_normalized(self) -> None:
        # aws-cli appends "/" for recursive targets (FileFormat.s3_format):
        # "data" must list under "data/", never matching "data-sibling.txt".
        client = _FakeS3Client([{"Contents": [_obj("data/a")]}])
        _rm("s3://b/data", client, recursive=True)
        assert client.list_calls[0]["Prefix"] == "data/"

    def test_listing_knobs_forwarded(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/a")]}])
        _rm("s3://b/p/", client, recursive=True, page_size=5, request_payer="requester")
        call = client.list_calls[0]
        assert call["PaginationConfig"] == {"PageSize": 5}
        assert call["RequestPayer"] == "requester"
        assert "Delimiter" not in call
        assert client.delete_objects_calls[0]["RequestPayer"] == "requester"

    def test_markers_are_deleted_too(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/m/", 0), _obj("p/a")]}])
        results = _rm("s3://b/p/", client, recursive=True)
        assert [r.key for r in results] == ["p/m/", "p/a"]

    def test_matcher_sees_prefix_relative_key(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("data/a.txt"), _obj("data/sub/x")]}])
        keep = GlobFilter().exclude("sub/*").compile()
        results = _rm("s3://b/data/", client, recursive=True, filter=keep)
        assert [r.key for r in results] == ["data/a.txt"]

    def test_interleaved_ordering_is_last_match_wins(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/a.txt"), _obj("p/b.bin")]}])
        keep = GlobFilter().include("*.txt").exclude("*").compile()
        assert _rm("s3://b/p/", client, recursive=True, filter=keep) == []
        keep = GlobFilter().exclude("*").include("*.txt").compile()
        results = _rm("s3://b/p/", client, recursive=True, filter=keep)
        assert [r.key for r in results] == ["p/a.txt"]

    def test_callable_filter_receives_fileinfo(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/big", 10), _obj("p/empty", 0)]}])
        results = _rm(
            "s3://b/p/",
            client,
            recursive=True,
            filter=lambda info: info.size == 0,
        )
        assert [r.key for r in results] == ["p/empty"]

    def test_dryrun_lists_but_never_deletes(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/a"), _obj("p/b")]}])
        results = _rm("s3://b/p/", client, recursive=True, dryrun=True)
        assert len(client.list_calls) == 1
        assert client.delete_objects_calls == []
        assert [(r.key, r.outcome) for r in results] == [
            ("p/a", OpOutcome.DRYRUN),
            ("p/b", OpOutcome.DRYRUN),
        ]

    def test_zero_matches_is_a_silent_success(self) -> None:
        client = _FakeS3Client([{}])
        results = _rm("s3://b/none/", client, recursive=True)
        assert results == []
        assert client.delete_objects_calls == []

    def test_listing_failure_propagates_as_category_error(self) -> None:
        # Pre-item failures are "fatal" (aws rc 1): the category error passes
        # through untouched, not wrapped in BatchError.
        client = _FakeS3Client(list_error=_client_error("NoSuchBucket", "ListObjectsV2"))
        with pytest.raises(NotFoundError):
            _rm("s3://b-no-such/", client, recursive=True)
        assert client.delete_objects_calls == []

    def test_item_failures_aggregate_into_batch_error(self) -> None:
        client = _FakeS3Client(
            [{"Contents": [_obj("p/a"), _obj("p/b")]}],
            delete_objects_errors=[{"Key": "p/a", "Code": "AccessDenied", "Message": "denied"}],
        )
        results: list[OpResult] = []
        storage = S3Storage("s3://b/p/", client=client)
        with pytest.raises(BatchError) as exc_info:
            S3().rm(storage, recursive=True, on_result=results.append)
        assert (exc_info.value.succeeded, exc_info.value.failed) == (1, 1)
        outcomes = {r.key: r.outcome for r in results}
        assert outcomes == {"p/a": OpOutcome.FAILED, "p/b": OpOutcome.SUCCEEDED}


class TestRmBucketRootSweep:
    def test_only_folder_markers_are_deleted(self) -> None:
        # aws parity: keyless non-recursive rm lists everything but deletes
        # only zero-byte "/"-terminated markers, at any depth.
        client = _FakeS3Client(
            [{"Contents": [_obj("m1/", 0), _obj("m2/sub/", 0), _obj("keep/x", 3), _obj("z", 0)]}]
        )
        results = _rm("s3://b", client)
        assert client.list_calls[0]["Prefix"] == ""
        assert [r.key for r in results] == ["m1/", "m2/sub/"]
        assert client.delete_objects_calls[0]["Delete"]["Objects"] == [
            {"Key": "m1/"},
            {"Key": "m2/sub/"},
        ]

    def test_trailing_slash_form_is_equivalent(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("m/", 0), _obj("keep/x", 3)]}])
        results = _rm("s3://b/", client)
        assert [r.key for r in results] == ["m/"]


class TestRmTargetValidation:
    def test_service_root_is_rejected(self) -> None:
        # aws reaches the API with Bucket="" and fails botocore validation
        # (rc 1 fatal); the library rejects with the same token up front
        # instead of listing buckets.
        client = _FakeS3Client()
        storage = S3Storage("s3://", client=client)
        with pytest.raises(ValidationError, match="Invalid bucket name"):
            S3().rm(storage)
        assert client.list_calls == []

    def test_non_s3_location_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="rm accepts"):
            S3().rm(123)  # type: ignore[arg-type]

    def test_filter_none_deletes_everything_listed(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/a")]}])
        results = _rm("s3://b/p/", client, recursive=True, filter=None)
        assert [r.key for r in results] == ["p/a"]


class TestRmFileInfoOnBlindPath:
    def test_callable_filter_gets_key_only_fileinfo(self) -> None:
        # The blind path never lists, so the FileInfo handed to a callable
        # filter carries the key alone (size/mtime None).
        seen: list[FileInfo] = []

        def keep(info: FileInfo) -> bool:
            seen.append(info)
            return False

        client = _FakeS3Client()
        _rm("s3://b/data/a.txt", client, filter=keep)
        assert [(i.key, i.size, i.mtime) for i in seen] == [("data/a.txt", None, None)]
