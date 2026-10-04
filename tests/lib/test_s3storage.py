"""Unit tests for ``boto3_s3.s3storage.S3Storage``: listing, the path grammar,
the single-key methods, and the single-request object transfer.

Mostly hand-rolled fake clients (a fake paginator recording the kwargs passed to
``paginate``, so delimiter / page-size / request-payer wiring can be asserted)
plus the canned-response recording client. One round-trip case runs against moto
instead, where what botocore itself does with the arguments is the point
(``TestSingleRequestTransferRoundTrip``).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import os
import stat
import sys
import threading
import time
from collections.abc import Collection, Generator, Iterator
from datetime import datetime, timedelta, timezone, tzinfo
from importlib.metadata import version
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import (
    ClientError,
    IncompleteReadError,
    ProfileNotFound,
    ResponseStreamingError,
)
from moto import mock_aws

from boto3_s3 import (
    S3,
    AccessDeniedError,
    Boto3S3Error,
    ConfigurationError,
    FileInfo,
    FileKind,
    InvalidConfigError,
    LocalStorage,
    MalformedResponseError,
    NotFoundError,
    OpResult,
    S3FileInfo,
    S3ScanOptions,
    S3Storage,
    ScanOptions,
    StorageCapability,
    TransportError,
    ValidationError,
    s3storage,
)
from boto3_s3.storage import sieve_pages
from tests.utils.fakemodel import model_meta
from tests.utils.fakes3 import MTIME, client_error, listing
from tests.utils.recorder import ApiCall, make_recording_client, ops


class _FakePaginator:
    def __init__(
        self, pages: list[dict[str, Any]], error: Exception | None, calls: list[dict[str, Any]]
    ) -> None:
        self._pages = pages
        self._error = error
        self._calls = calls

    def paginate(self, **kwargs: Any) -> Any:
        self._calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return iter(self._pages)


# What a current botocore's ListBuckets models. The fakes below carry it unless
# a test asks for an older model: a subset, or None for no input shape at all.
_LIST_BUCKETS_MEMBERS = frozenset({"Prefix", "BucketRegion"})


class _FakeS3Client:
    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        head_response: dict[str, Any] | None = None,
        head_error: Exception | None = None,
        list_buckets_members: Collection[str] | None = _LIST_BUCKETS_MEMBERS,
    ) -> None:
        self._pages = pages or []
        self._error = error
        self._head_response = head_response
        self._head_error = head_error
        self.calls: list[dict[str, Any]] = []
        self.paginator_names: list[str] = []
        self.head_calls: list[dict[str, Any]] = []
        # The ListBuckets filter gate reads the model, so the fake carries one.
        self.meta = model_meta({"ListBuckets": list_buckets_members})

    def can_paginate(self, name: str) -> bool:
        return True

    def get_paginator(self, name: str) -> _FakePaginator:
        self.paginator_names.append(name)
        return _FakePaginator(self._pages, self._error, self.calls)

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.head_calls.append(kwargs)
        if self._head_error is not None:
            raise self._head_error
        return self._head_response or {}


def _storage(
    pages: list[dict[str, Any]] | None = None,
    *,
    error: Exception | None = None,
    head_response: dict[str, Any] | None = None,
    head_error: Exception | None = None,
    url: str = "s3://bucket/prefix/",
    page_size: int = 1000,
    fetch_owner: bool = False,
    list_buckets_members: Collection[str] | None = _LIST_BUCKETS_MEMBERS,
) -> tuple[S3Storage, _FakeS3Client]:
    client = _FakeS3Client(
        pages=pages,
        error=error,
        head_response=head_response,
        head_error=head_error,
        list_buckets_members=list_buckets_members,
    )
    return S3Storage(url, client=client, page_size=page_size, fetch_owner=fetch_owner), client


def _obj(
    key: str,
    size: int = 1,
    *,
    etag: str | None = None,
    storage_class: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    obj: dict[str, Any] = {"Key": key, "Size": size, "LastModified": MTIME}
    if etag is not None:
        obj["ETag"] = etag
    if storage_class is not None:
        obj["StorageClass"] = storage_class
    if owner is not None:
        obj["Owner"] = {"ID": owner}
    return obj


class TestScanNonRecursive:
    def test_yields_common_prefixes_then_objects(self) -> None:
        pages = [
            {
                "CommonPrefixes": [{"Prefix": "prefix/sub/"}],
                "Contents": [
                    _obj("prefix/a.txt", 10, etag='"abc"', storage_class="STANDARD", owner="me")
                ],
            }
        ]
        storage, client = _storage(pages)
        results = list(storage.scan())

        assert client.paginator_names == ["list_objects_v2"]
        assert client.calls[0]["Delimiter"] == "/"
        assert client.calls[0]["Bucket"] == "bucket"
        assert client.calls[0]["Prefix"] == "prefix/"
        # compare_key is the Prefix-relative key, and the listing backend is
        # stamped as storage - both by scan, before any filter.
        assert results[0] == S3FileInfo(
            key="prefix/sub/", kind=FileKind.DIRECTORY, compare_key="sub/", storage=storage
        )
        info = results[1]
        assert isinstance(info, S3FileInfo)
        assert info.kind is FileKind.FILE
        assert info.key == "prefix/a.txt"
        assert info.compare_key == "a.txt"
        assert info.size == 10
        assert info.mtime == MTIME
        assert info.etag == '"abc"'  # as the response carried it, quotes included
        assert info.storage_class == "STANDARD"
        assert info.owner == "me"
        assert info.storage is storage


class TestEtagIsKeptAsReceived:
    """``S3FileInfo.etag`` is the response's own text; nothing is stripped.

    The transfer engine hands it to s3transfer as the ``If-Match`` of a ranged
    download (and a multipart copy's ``CopySourceIfMatch``), and aws-cli sends
    the response's text there untouched. Stripping the quotes and putting them
    back is only an identity for the usual quoted form: measured against the
    pinned aws (2.36.40) through a 127.0.0.1 fake, an ETag served as ``abc``
    goes back as ``abc`` and one served as ``W/"abc"`` as ``W/"abc"``, where a
    strip-and-requote sent ``"abc"`` and ``"W/"abc"``.
    """

    @pytest.mark.parametrize("etag", ['"abc"', '"abc-2"', "abc", 'W/"abc"'])
    def test_a_listing_entry_keeps_the_etag_text(self, etag: str) -> None:
        storage, _ = _storage([{"Contents": [_obj("prefix/a.txt", 10, etag=etag)]}])
        (info,) = storage.scan(S3ScanOptions(recursive=True))
        assert isinstance(info, S3FileInfo)
        assert info.etag == etag

    @pytest.mark.parametrize("etag", ['"abc"', "abc", 'W/"abc"'])
    def test_a_head_keeps_the_etag_text(self, etag: str) -> None:
        head = {"ContentLength": 7, "LastModified": MTIME, "ETag": etag}
        storage, _ = _storage(url="s3://bucket/prefix/obj.txt", head_response=head)
        info = storage.get_fileinfo()
        assert info is not None and info.etag == etag

    def test_only_a_missing_etag_is_none(self) -> None:
        # An empty ETag is still what the response carried: aws-cli provides
        # it to s3transfer like any other (so no HeadObject probe goes out
        # for that object - measured through a fake serving `<ETag></ETag>`),
        # and only an entry with no ETag element at all has none.
        pages = [{"Contents": [_obj("prefix/a.txt"), _obj("prefix/b.txt", etag="")]}]
        storage, _ = _storage(pages)
        infos = list(storage.scan(S3ScanOptions(recursive=True)))
        assert [info.etag for info in infos if isinstance(info, S3FileInfo)] == [None, ""]


class TestScanRecursive:
    def test_no_delimiter_and_objects_across_pages(self) -> None:
        pages = [
            {"Contents": [_obj("prefix/a.txt"), _obj("prefix/sub/b.txt")]},
            {"Contents": [_obj("prefix/c.txt")]},
        ]
        storage, client = _storage(pages)
        results = list(storage.scan(S3ScanOptions(recursive=True)))

        assert "Delimiter" not in client.calls[0]
        assert all(isinstance(r, S3FileInfo) for r in results)
        assert [r.key for r in results] == ["prefix/a.txt", "prefix/sub/b.txt", "prefix/c.txt"]
        # scan stamps the Prefix-relative compare_key on every entry.
        assert [r.compare_key for r in results] == ["a.txt", "sub/b.txt", "c.txt"]

    def test_filter_matches_scan_stamped_compare_key(self) -> None:
        # A custom ScanOptions.filter can match the Prefix-relative compare_key
        # directly: scan stamps it, so the predicate neither strips the prefix
        # nor trips over a None compare_key.
        pages = [{"Contents": [_obj("prefix/keep/a.txt"), _obj("prefix/drop/b.txt")]}]
        storage, _ = _storage(pages)
        options = S3ScanOptions(
            recursive=True, filter=lambda info: (info.compare_key or "").startswith("keep/")
        )
        results = list(storage.scan(options))
        assert [r.key for r in results] == ["prefix/keep/a.txt"]
        assert results[0].compare_key == "keep/a.txt"


class TestScanRecursiveCommonPrefixes:
    """``S3ScanOptions.include_common_prefixes``: the listing view of a recursive scan.

    A recursive listing sends no ``Delimiter``, so a conforming service returns
    no ``CommonPrefixes`` at all; a service that returns them anyway is what
    ``aws s3 ls --recursive`` still prints as ``PRE`` lines. The oracle for what
    a page then yields, and in which order, is the pinned aws-cli listing such a
    page (2026-08-12 stamps, ``PRE`` right-justified to column 30):

        $ aws s3 ls s3://bkt/deep/ --recursive
                                   PRE one/
                                   PRE sub/
                                   PRE /
        2026-08-12 00:00:00          1 deep/a.txt
        2026-08-12 00:00:00          2 deep/two/sub/b.txt

    for a page carrying the prefixes ``deep/one/``, ``deep/two/sub/``, ``deep//``
    and those two objects - every prefix of the page ahead of the page's
    objects, in the response's own order, whatever their depth. A transfer must
    not see those entries (they would enter its item stream and break ``sync``'s
    ``compare_key`` byte order), so the default drops them.
    """

    def test_default_recursive_scan_drops_them(self) -> None:
        pages = [
            {"CommonPrefixes": [{"Prefix": "prefix/sub/"}], "Contents": [_obj("prefix/a.txt")]}
        ]
        storage, client = _storage(pages)
        results = list(storage.scan(S3ScanOptions(recursive=True)))
        assert [r.key for r in results] == ["prefix/a.txt"]
        assert "Delimiter" not in client.calls[0]

    def test_storage_config_never_seeds_the_listing_view(self) -> None:
        # The knob is operation-set (ls), not storage config: every scan a
        # transfer builds from default_scan_options keeps the transfer view.
        storage, _ = _storage([])
        assert storage.default_scan_options().include_common_prefixes is False

    def test_flag_emits_each_page_directories_ahead_of_its_objects(self) -> None:
        pages = [
            {"Contents": [_obj("prefix/a.txt")], "CommonPrefixes": [{"Prefix": "prefix/zdir/"}]},
            {
                "CommonPrefixes": [{"Prefix": "prefix/adir/"}, {"Prefix": "prefix/two/sub/"}],
                "Contents": [_obj("prefix/z.txt")],
            },
        ]
        storage, client = _storage(pages)
        results = list(storage.scan(S3ScanOptions(recursive=True, include_common_prefixes=True)))
        assert [(r.kind, r.key) for r in results] == [
            (FileKind.DIRECTORY, "prefix/zdir/"),
            (FileKind.FILE, "prefix/a.txt"),
            (FileKind.DIRECTORY, "prefix/adir/"),
            (FileKind.DIRECTORY, "prefix/two/sub/"),
            (FileKind.FILE, "prefix/z.txt"),
        ]
        # Still a recursive listing: the flag widens what a page yields, it does
        # not ask the service for prefixes.
        assert "Delimiter" not in client.calls[0]

    def test_widened_directory_entries_carry_the_scan_stamps(self) -> None:
        pages = [{"CommonPrefixes": [{"Prefix": "prefix/two/sub/"}]}]
        storage, _ = _storage(pages)
        results = list(storage.scan(S3ScanOptions(recursive=True, include_common_prefixes=True)))
        assert results == [
            S3FileInfo(
                key="prefix/two/sub/",
                kind=FileKind.DIRECTORY,
                compare_key="two/sub/",
                storage=storage,
            )
        ]

    def test_filter_prunes_a_widened_entry(self) -> None:
        # The caller that widens the enumeration owns the filtering, as on
        # LocalScanOptions.enumerate_all_entries.
        pages = [
            {"CommonPrefixes": [{"Prefix": "prefix/sub/"}], "Contents": [_obj("prefix/a.txt")]}
        ]
        storage, _ = _storage(pages)
        options = S3ScanOptions(
            recursive=True,
            include_common_prefixes=True,
            filter=lambda info: info.kind is not FileKind.DIRECTORY,
        )
        assert [r.key for r in storage.scan(options)] == ["prefix/a.txt"]

    @pytest.mark.parametrize("include", [False, True])
    def test_non_recursive_listing_emits_them_either_way(self, include: bool) -> None:
        # The Delimiter a non-recursive listing sends is what asks for the
        # prefixes, so that path ignores the flag entirely.
        pages = [
            {"CommonPrefixes": [{"Prefix": "prefix/sub/"}], "Contents": [_obj("prefix/a.txt")]}
        ]
        storage, client = _storage(pages)
        results = list(storage.scan(S3ScanOptions(include_common_prefixes=include)))
        assert [(r.kind, r.key) for r in results] == [
            (FileKind.DIRECTORY, "prefix/sub/"),
            (FileKind.FILE, "prefix/a.txt"),
        ]
        assert client.calls[0]["Delimiter"] == "/"


class TestScanOptionForwarding:
    def test_page_size_and_request_payer_forwarded(self) -> None:
        storage, client = _storage([])
        list(storage.scan(S3ScanOptions(page_size=42, request_payer="requester")))
        assert client.calls[0]["PaginationConfig"] == {"PageSize": 42}
        assert client.calls[0]["RequestPayer"] == "requester"

    def test_request_payer_omitted_by_default(self) -> None:
        storage, client = _storage([])
        list(storage.scan())
        assert "RequestPayer" not in client.calls[0]

    def test_fetch_owner_forwarded(self) -> None:
        storage, client = _storage([])
        list(storage.scan(S3ScanOptions(fetch_owner=True)))
        assert client.calls[0]["FetchOwner"] is True

    def test_fetch_owner_omitted_by_default(self) -> None:
        storage, client = _storage([])
        list(storage.scan())
        assert "FetchOwner" not in client.calls[0]

    def test_storage_page_size_config_seeds_scan(self) -> None:
        # page_size given to the constructor flows into an arg-less scan()
        # (via default_scan_options), so an app tunes the listing on the storage.
        storage, client = _storage([], page_size=3)
        list(storage.scan())
        assert client.calls[0]["PaginationConfig"] == {"PageSize": 3}

    def test_storage_fetch_owner_config_seeds_scan(self) -> None:
        storage, client = _storage([], fetch_owner=True)
        list(storage.scan())
        assert client.calls[0]["FetchOwner"] is True


class TestScanPages:
    def test_yields_one_list_per_page(self) -> None:
        pages = [
            {"Contents": [_obj("a.txt"), _obj("b.txt")]},
            {"Contents": [_obj("c.txt")]},
        ]
        storage, _ = _storage(pages)
        result = list(storage.scan_pages(S3ScanOptions(recursive=True)))
        assert [[fi.key for fi in page] for page in result] == [["a.txt", "b.txt"], ["c.txt"]]

    def test_override_filters_entries_through_scan(self) -> None:
        # A subclass that drops dotfiles by overriding scan_pages still gets
        # scan()'s flattening + prefetch, and carries the one ScanOptions value
        # through without re-implementing either method.
        class NoDotfiles(S3Storage):
            def scan_pages(self, options: ScanOptions) -> Any:
                for page in super().scan_pages(options):
                    yield [fi for fi in page if not fi.key.rsplit("/", 1)[-1].startswith(".")]

        client = _FakeS3Client(pages=[{"Contents": [_obj("d/.hidden"), _obj("d/seen.txt")]}])
        storage = NoDotfiles("s3://bucket/d/", client=client)
        assert [fi.key for fi in storage.scan(S3ScanOptions(recursive=True))] == ["d/seen.txt"]


class TestScanPrefixOverride:
    """``ScanOptions.prefix`` re-anchors the listing on the storage instance itself.

    A transfer whose normalized listing prefix differs from the raw source key
    lists via ``storage.scan(prefix=...)`` instead of rebuilding a bare
    ``S3Storage`` - so a custom subclass (and its ``scan_pages`` override) survives.
    """

    def test_prefix_overrides_key_as_listing_anchor(self) -> None:
        pages = [{"Contents": [_obj("data/a.txt"), _obj("data/sub/b.txt")]}]
        storage, client = _storage(pages, url="s3://bucket/data")  # key == "data"
        results = list(storage.scan(S3ScanOptions(recursive=True, prefix="data/")))
        # Listed under the prefix, not the storage's own key.
        assert client.calls[0]["Prefix"] == "data/"
        # compare_key is relative to the prefix ("data/"), not "data".
        assert [r.compare_key for r in results] == ["a.txt", "sub/b.txt"]

    def test_prefix_none_uses_the_storage_key(self) -> None:
        storage, client = _storage([], url="s3://bucket/data")
        list(storage.scan(S3ScanOptions(recursive=True)))
        assert client.calls[0]["Prefix"] == "data"

    def test_subclass_scan_pages_override_survives_a_prefix_reanchor(self) -> None:
        # The transfer re-anchor fix: scanning the instance with a prefix (not
        # rebuilding a plain S3Storage) keeps the subclass's scan_pages override.
        class Tagged(S3Storage):
            def scan_pages(self, options: ScanOptions) -> Any:
                for page in super().scan_pages(options):
                    for fi in page:
                        fi.compare_key = "TAG/" + (fi.compare_key or "")
                    yield page

        client = _FakeS3Client(pages=[{"Contents": [_obj("data/a.txt")]}])
        storage = Tagged("s3://bucket/data", client=client)  # key == "data"
        results = list(storage.scan(S3ScanOptions(recursive=True, prefix="data/")))
        assert client.calls[0]["Prefix"] == "data/"  # re-anchored on the instance
        assert results[0].compare_key == "TAG/a.txt"  # the override ran


class TestScanOptionsType:
    def test_scan_rejects_a_foreign_scan_options(self) -> None:
        # S3Storage.scan requires its own S3ScanOptions; a bare ScanOptions is
        # rejected rather than silently listing with S3 defaults.
        storage, _ = _storage([])
        with pytest.raises(TypeError, match="S3ScanOptions"):
            list(storage.scan(ScanOptions(recursive=True)))

    def test_default_scan_options_is_s3(self) -> None:
        storage, _ = _storage([])
        assert isinstance(storage.default_scan_options(), S3ScanOptions)

    def test_default_scan_options_seeds_constructor_config(self) -> None:
        storage, _ = _storage([], page_size=5, fetch_owner=True)
        opts = storage.default_scan_options()
        assert opts.page_size == 5
        assert opts.fetch_owner is True


class TestScanFilter:
    """``ScanOptions.filter`` is applied by ``scan_pages`` (which returns filtered
    pages), on the prefetch worker that drives the producer."""

    def test_keeps_only_included_entries(self) -> None:
        pages = [
            {"Contents": [_obj("prefix/a.txt"), _obj("prefix/b.log")]},
            {"Contents": [_obj("prefix/c.txt")]},
        ]
        storage, _ = _storage(pages)
        options = S3ScanOptions(recursive=True, filter=lambda info: info.key.endswith(".txt"))
        assert [fi.key for fi in storage.scan(options)] == ["prefix/a.txt", "prefix/c.txt"]

    def test_none_filter_keeps_everything(self) -> None:
        pages = [{"Contents": [_obj("prefix/a"), _obj("prefix/b")]}]
        storage, _ = _storage(pages)
        assert [fi.key for fi in storage.scan(S3ScanOptions(recursive=True))] == [
            "prefix/a",
            "prefix/b",
        ]

    def test_scan_pages_returns_filtered(self) -> None:
        # The producer applies options.filter itself (returns filtered pages);
        # a page emptied by the filter is dropped, not yielded empty.
        pages = [{"Contents": [_obj("prefix/a.txt"), _obj("prefix/b.log")]}]
        storage, _ = _storage(pages)
        options = S3ScanOptions(recursive=True, filter=lambda info: info.key.endswith(".txt"))
        result = list(storage.scan_pages(options))
        assert [[fi.key for fi in page] for page in result] == [["prefix/a.txt"]]
        # a filter excluding everything yields no pages at all
        storage2, _ = _storage(pages)
        assert (
            list(storage2.scan_pages(S3ScanOptions(recursive=True, filter=lambda _i: False))) == []
        )

    def test_sieve_drops_emptied_pages(self) -> None:
        # sieve_pages (the helper a producer wraps its raw pages with): a page
        # whose every entry is excluded is dropped, not yielded empty, so it
        # never occupies a prefetch queue slot.
        pages = iter([[FileInfo(key="a.log")], [FileInfo(key="b.txt")]])
        out = list(sieve_pages(pages, lambda info: info.key.endswith(".txt")))
        assert [[fi.key for fi in page] for page in out] == [["b.txt"]]

    def test_predicate_runs_on_the_prefetch_worker(self) -> None:
        threads: set[str] = set()

        def keep(_info: FileInfo) -> bool:
            threads.add(threading.current_thread().name)
            return True

        pages = [{"Contents": [_obj("prefix/a"), _obj("prefix/b")]}]
        storage, _ = _storage(pages)
        list(storage.scan(S3ScanOptions(recursive=True, filter=keep)))
        assert threads == {"boto3-s3-prefetch"}

    def test_predicate_error_surfaces_on_the_consumer_pull(self) -> None:
        def boom(_info: FileInfo) -> bool:
            raise RuntimeError("predicate failed")

        pages = [{"Contents": [_obj("prefix/a")]}]
        storage, _ = _storage(pages)
        with pytest.raises(RuntimeError, match="predicate failed"):
            list(storage.scan(S3ScanOptions(recursive=True, filter=boom)))


class TestGetFileinfo:
    """``S3Storage.get_fileinfo`` - a generic HeadObject: present / 404->None / raise."""

    def test_present_returns_fileinfo(self) -> None:
        # A realistic HeadObject: StorageClass is present only for non-STANDARD
        # classes (the API omits the header for STANDARD objects).
        head = {
            "ContentLength": 7,
            "LastModified": MTIME,
            "ETag": '"abc"',
            "StorageClass": "GLACIER",
        }
        storage, client = _storage(url="s3://bucket/prefix/obj.txt", head_response=head)
        info = storage.get_fileinfo()
        assert isinstance(info, S3FileInfo)
        assert info.key == "prefix/obj.txt"
        assert info.compare_key == "obj.txt"  # basename
        assert info.size == 7
        assert info.etag == '"abc"'  # as the response carried it, quotes included
        assert info.head is head  # the HeadObject payload is cached
        assert client.head_calls == [{"Bucket": "bucket", "Key": "prefix/obj.txt"}]

    def test_404_returns_none(self) -> None:
        storage, _ = _storage(
            url="s3://bucket/missing", head_error=client_error("404", 404, "HeadObject")
        )
        assert storage.get_fileinfo() is None

    def test_other_error_raises(self) -> None:
        storage, _ = _storage(
            url="s3://bucket/denied", head_error=client_error("403", 403, "HeadObject")
        )
        with pytest.raises(AccessDeniedError):
            storage.get_fileinfo()

    def test_child_key_joins_under_the_prefix(self) -> None:
        head = {"ContentLength": 1, "LastModified": MTIME}
        storage, client = _storage(url="s3://bucket/prefix/", head_response=head)
        info = storage.get_fileinfo("sub/f.txt")
        assert info is not None
        assert info.key == "prefix/sub/f.txt"
        assert info.compare_key == "f.txt"
        assert client.head_calls == [{"Bucket": "bucket", "Key": "prefix/sub/f.txt"}]

    def test_child_key_joins_under_a_slashless_prefix(self) -> None:
        # The "/" boundary is inserted even when the prefix lacks one, so a child
        # key is an entry beneath the location (not a bare-concat "prefixsub/...").
        head = {"ContentLength": 1, "LastModified": MTIME}
        storage, client = _storage(url="s3://bucket/prefix", head_response=head)
        info = storage.get_fileinfo("sub/f.txt")
        assert info is not None
        assert info.key == "prefix/sub/f.txt"
        assert client.head_calls == [{"Bucket": "bucket", "Key": "prefix/sub/f.txt"}]

    def test_child_key_under_a_keyless_location_has_no_leading_slash(self) -> None:
        head = {"ContentLength": 1, "LastModified": MTIME}
        storage, client = _storage(url="s3://bucket", head_response=head)
        info = storage.get_fileinfo("a.txt")
        assert info is not None
        assert info.key == "a.txt"
        assert client.head_calls == [{"Bucket": "bucket", "Key": "a.txt"}]


def _bucket_entry(name: str) -> dict[str, Any]:
    return {"Name": name, "CreationDate": MTIME}


class _NoPaginatorClient:
    """A botocore < 1.34.162 client: no ListBuckets paginator, and a
    ListBuckets model with no input shape at all."""

    def __init__(self) -> None:
        self.list_buckets_calls = 0
        self.meta = model_meta({"ListBuckets": None})

    def can_paginate(self, name: str) -> bool:
        return False

    def list_buckets(self, **kwargs: Any) -> dict[str, Any]:
        self.list_buckets_calls += 1
        return {"Buckets": [_bucket_entry("alpha")]}


class TestListBuckets:
    """The S3 service root is a separate operation - ``list_buckets`` (``ListBuckets``),
    not ``scan`` (which is object listing / openable entities)."""

    def test_lists_buckets_as_bucket_entries(self) -> None:
        pages = [{"Buckets": [_bucket_entry("alpha"), _bucket_entry("beta")]}]
        storage, client = _storage(pages, url="s3://")
        results = list(storage.list_buckets())

        assert client.paginator_names == ["list_buckets"]
        assert all(isinstance(r, S3FileInfo) for r in results)
        assert [(r.key, r.kind) for r in results] == [
            ("alpha", FileKind.BUCKET),
            ("beta", FileKind.BUCKET),
        ]
        assert results[0].mtime == MTIME  # CreationDate
        assert results[0].size is None

    def test_filters_forwarded(self) -> None:
        # page_size is the storage's own config now (shared with the object listing).
        storage, client = _storage([], url="s3://", page_size=7)
        list(storage.list_buckets(name_prefix="al", region="us-west-2"))
        assert client.calls[0] == {
            "PaginationConfig": {"PageSize": 7},
            "Prefix": "al",
            "BucketRegion": "us-west-2",
        }

    def test_filters_omitted_by_default(self) -> None:
        storage, client = _storage([], url="s3://")
        list(storage.list_buckets())
        assert client.calls[0] == {"PaginationConfig": {"PageSize": 1000}}

    def test_scan_at_root_is_object_listing_not_buckets(self) -> None:
        # scan is object listing only: at a service root it uses list_objects_v2
        # (with an empty Bucket, which real botocore rejects as an Invalid bucket
        # name - matching aws s3 cp/rm/sync s3://), never ListBuckets.
        storage, client = _storage([], url="s3://")
        list(storage.scan())
        assert client.paginator_names == ["list_objects_v2"]

    def test_falls_back_to_unpaginated_list_buckets_below_the_floor(self) -> None:
        # botocore < 1.34.162 has no ListBuckets paginator; an unfiltered listing
        # must fall back to a single list_buckets() call rather than crash with
        # OperationNotPageableError.
        client = _NoPaginatorClient()
        storage = S3Storage("s3://", client=client)  # type: ignore[arg-type]
        results = list(storage.list_buckets())
        assert client.list_buckets_calls == 1
        assert [(r.key, r.kind) for r in results] == [("alpha", FileKind.BUCKET)]

    @pytest.mark.parametrize("filters", [{"name_prefix": "al"}, {"region": "us-west-2"}])
    def test_filters_below_the_paginator_floor_are_refused(self, filters: dict[str, str]) -> None:
        # That old a botocore models no ListBuckets input shape at all, so the
        # fallback call cannot carry a filter: a filtered listing must fail
        # rather than answer with the account's whole bucket list.
        client = _NoPaginatorClient()
        storage = S3Storage("s3://", client=client)  # type: ignore[arg-type]
        with pytest.raises(ConfigurationError):
            list(storage.list_buckets(**filters))
        assert client.list_buckets_calls == 0

    @pytest.mark.parametrize("filters", [{"name_prefix": ""}, {"region": ""}])
    def test_empty_filters_are_not_a_request(self, filters: dict[str, str]) -> None:
        # "" is not a filter (aws-cli's truthiness check): nothing is sent, so
        # no input member is needed and the sub-floor fallback still lists.
        client = _NoPaginatorClient()
        storage = S3Storage("s3://", client=client)  # type: ignore[arg-type]
        results = list(storage.list_buckets(**filters))
        assert client.list_buckets_calls == 1
        assert [(r.key, r.kind) for r in results] == [("alpha", FileKind.BUCKET)]

    @pytest.mark.parametrize("filters", [{"name_prefix": "al"}, {"region": "us-west-2"}])
    def test_filters_the_model_cannot_carry_are_refused(self, filters: dict[str, str]) -> None:
        # botocore 1.34.162 through 1.35.41 paginates ListBuckets but models
        # neither filter input; sending one there fails botocore's own param
        # validation, so the request is refused first - as a ConfigurationError
        # (the SDK floor lacks the capability), not a ValidationError.
        storage, client = _storage([], url="s3://", list_buckets_members=set())
        with pytest.raises(ConfigurationError) as exc_info:
            list(storage.list_buckets(**filters))
        message = str(exc_info.value)
        assert "1.35.42" in message
        assert version("botocore") in message
        # A storage-level call names no operation; `S3.ls` stamps its own.
        assert exc_info.value.operation is None
        assert client.calls == []

    def test_prefix_only_model_carries_name_prefix_and_refuses_region(self) -> None:
        # Each filter rides its own input member, so a model with one of the two
        # serves that filter and refuses the other.
        storage, client = _storage([], url="s3://", list_buckets_members={"Prefix"})
        list(storage.list_buckets(name_prefix="al"))
        assert client.calls[0]["Prefix"] == "al"
        with pytest.raises(ConfigurationError):
            list(storage.list_buckets(region="us-west-2"))
        assert len(client.calls) == 1

    def test_bucket_region_only_model_carries_region_and_refuses_name_prefix(self) -> None:
        storage, client = _storage([], url="s3://", list_buckets_members={"BucketRegion"})
        list(storage.list_buckets(region="us-west-2"))
        assert client.calls[0]["BucketRegion"] == "us-west-2"
        with pytest.raises(ConfigurationError):
            list(storage.list_buckets(name_prefix="al"))
        assert len(client.calls) == 1


def _keys_until_raise(entries: Iterator[FileInfo], expected: type[Exception]) -> list[str]:
    """Drain `entries` until it raises `expected`, returning the keys it got out first.

    What was delivered before the failure is the parity-relevant half of these
    cases: aws-cli emits every entry ahead of the one it chokes on, so a listing
    that raises with nothing delivered - or one that delivers everything and
    then raises - is a different observable run.
    """
    keys: list[str] = []
    with pytest.raises(expected):
        for info in entries:
            keys.append(info.key)
    return keys


class TestMalformedListingEntries:
    """An entry missing a required element stops the listing where aws-cli stops it.

    aws-cli reads a ``Contents`` entry's ``Key`` / ``LastModified`` / ``Size``
    (and a common prefix's ``Prefix``, a bucket's ``CreationDate`` / ``Name``) by
    subscript, so a response that omits one dies with ``KeyError`` naming the
    element, right at that entry; here that is ``MalformedResponseError`` with
    the KeyError's text as its message and the KeyError on ``__cause__``.
    Measured against the pinned aws through a
    127.0.0.1 fake: ``ls`` prints the entries ahead of it and exits 255 with
    ``[ERROR]: 'LastModified'``, a transfer prints its own and exits 1 with
    ``fatal error: 'LastModified'``. Dropping the entry instead - what this used
    to do - silently shortened the run at rc 0.
    """

    @pytest.mark.parametrize(
        ("entry", "missing"),
        [
            ({"Size": 1, "LastModified": MTIME}, "Key"),
            ({"Key": "prefix/bad.txt", "Size": 1}, "LastModified"),
            ({"Key": "prefix/bad.txt", "LastModified": MTIME}, "Size"),
            ({"ETag": '"x"'}, "Key"),
        ],
    )
    def test_a_missing_element_is_a_malformed_response_naming_it(
        self, entry: dict[str, Any], missing: str
    ) -> None:
        # The last row pins the read order too: aws-cli's BucketLister takes Key
        # first, then LastModified, and its consumer Size afterwards, so an entry
        # missing several is reported by the first of them.
        storage, _ = _storage([{"Contents": [entry]}])
        with pytest.raises(MalformedResponseError) as excinfo:
            list(storage.scan(S3ScanOptions(recursive=True)))
        assert str(excinfo.value) == f"'{missing}'"  # str(KeyError): the CLI's line
        cause = excinfo.value.__cause__
        assert isinstance(cause, KeyError) and cause.args[0] == missing
        assert excinfo.value.bucket == storage.bucket
        assert excinfo.value.key == (None if missing == "Key" else entry["Key"])

    def test_entries_ahead_of_the_bad_one_are_still_yielded(self) -> None:
        bad = {"Key": "prefix/bad.txt", "Size": 1}
        pages = [{"Contents": [_obj("prefix/a.txt"), bad, _obj("prefix/z.txt")]}]
        storage, _ = _storage(pages)
        keys = _keys_until_raise(
            storage.scan(S3ScanOptions(recursive=True)), MalformedResponseError
        )
        assert keys == ["prefix/a.txt"]

    def test_an_earlier_page_survives_a_later_bad_entry(self) -> None:
        pages = [
            {"Contents": [_obj("prefix/a.txt")]},
            {"Contents": [_obj("prefix/b.txt"), {"Key": "prefix/bad.txt", "Size": 1}]},
        ]
        storage, _ = _storage(pages)
        keys = _keys_until_raise(
            storage.scan(S3ScanOptions(recursive=True)), MalformedResponseError
        )
        assert keys == ["prefix/a.txt", "prefix/b.txt"]

    def test_a_common_prefix_without_prefix_raises_before_the_objects(self) -> None:
        # aws-cli renders a page's common prefixes ahead of its objects, so a bad
        # one is reached before any object of that page is emitted.
        pages = [
            {
                "CommonPrefixes": [{"Prefix": "prefix/good/"}, {}],
                "Contents": [_obj("prefix/a.txt")],
            }
        ]
        storage, _ = _storage(pages)
        keys = _keys_until_raise(
            storage.scan(S3ScanOptions(recursive=False)), MalformedResponseError
        )
        assert keys == ["prefix/good/"]

    @pytest.mark.parametrize(
        ("head", "missing"),
        [
            ({"ContentLength": 7, "ETag": '"abc"'}, "LastModified"),
            ({"LastModified": MTIME, "ETag": '"abc"'}, "ContentLength"),
            ({"ETag": '"abc"'}, "ContentLength"),
        ],
    )
    def test_a_head_missing_an_element_is_a_malformed_response_naming_it(
        self, head: dict[str, Any], missing: str
    ) -> None:
        # The single-object HEAD follows the same rule in aws-cli's
        # `_list_single_object` order - ContentLength first, then LastModified;
        # ETag is read with a default there - so a doubly incomplete response
        # is blamed on ContentLength (the transfer engine's `head_single` reads
        # it the same way; the CLI measurement is in that route's suite).
        storage, _ = _storage(url="s3://bucket/prefix/obj.txt", head_response=head)
        with pytest.raises(MalformedResponseError) as excinfo:
            storage.get_fileinfo()
        assert str(excinfo.value) == f"'{missing}'"
        assert isinstance(excinfo.value.__cause__, KeyError)
        # A storage-level call: the operation stays unset, the entry is named.
        assert (excinfo.value.operation, excinfo.value.bucket, excinfo.value.key) == (
            None,
            "bucket",
            "prefix/obj.txt",
        )

    @pytest.mark.parametrize(
        ("entry", "missing"),
        [({"Name": "zzz"}, "CreationDate"), ({"CreationDate": MTIME}, "Name")],
    )
    def test_a_bucket_missing_an_element_raises_after_the_earlier_buckets(
        self, entry: dict[str, Any], missing: str
    ) -> None:
        # aws-cli's bucket listing renders the creation date and then appends the
        # name, so the date is the one reported when both are gone.
        storage, _ = _storage([{"Buckets": [_bucket_entry("aaa"), entry]}], url="s3://")
        keys: list[str] = []
        with pytest.raises(MalformedResponseError) as excinfo:
            for info in storage.list_buckets():
                keys.append(info.key)
        assert keys == ["aaa"]
        assert str(excinfo.value) == f"'{missing}'"


_FAR_FUTURE = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
_FAR_PAST = datetime(1, 1, 1, tzinfo=timezone.utc)

# Every case needs the process's local zone set, which needs tzset (POSIX).
_needs_tzset = pytest.mark.skipif(
    not hasattr(time, "tzset"), reason="setting TZ mid-process requires time.tzset (POSIX only)"
)


@contextlib.contextmanager
def _local_zone(name: str) -> Generator[None, None, None]:
    """Run the block with the process's local zone set to `name`.

    Not `monkeypatch.setenv`: restoring `TZ` only takes effect once `tzset` has
    re-read it, and a fixture's teardown runs before monkeypatch's undo.
    """
    before = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        # A zone the host has no tzdata entry for silently degrades to UTC,
        # which would not exercise the offset this case turns on.
        if name != "UTC" and time.tzname[0] in {"UTC", "GMT"}:
            pytest.skip(f"host has no tzdata entry for {name}")
        yield
    finally:
        if before is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = before
        time.tzset()


def _stamped(key: str, mtime: datetime) -> dict[str, Any]:
    return {"Key": key, "Size": 1, "LastModified": mtime}


@_needs_tzset
class TestListingTimestampRepresentability:
    """A stamp the local zone cannot hold kills the run, the way it does in aws-cli.

    aws-cli puts every timestamp an S3 response carries through
    ``parse(...).astimezone(tzlocal())`` as it reads the response, so a stamp
    within the zone's offset of the end of ``datetime``'s range raises
    ``date value out of range`` there rather than being listed or transferred -
    measured against the pinned aws through a 127.0.0.1 fake: ``fatal error: date
    value out of range`` at rc 1 for cp / sync, ``[ERROR]`` at rc 255 for ``ls``.
    Here it is ``MalformedResponseError`` carrying that text and the
    ``OverflowError`` on ``__cause__``.
    Which stamps qualify depends on the zone, so each case pins one; the value
    kept on the entry stays UTC per the ``FileInfo.mtime`` contract.
    """

    def test_a_far_future_stamp_is_rejected_east_of_utc(self) -> None:
        pages = [{"Contents": [_obj("prefix/a.txt"), _stamped("prefix/far.txt", _FAR_FUTURE)]}]
        with _local_zone("Asia/Tokyo"):
            storage, _ = _storage(pages)
            keys = _keys_until_raise(
                storage.scan(S3ScanOptions(recursive=True)), MalformedResponseError
            )
        assert keys == ["prefix/a.txt"]

    def test_the_same_stamp_is_kept_where_the_zone_can_hold_it(self) -> None:
        pages = [{"Contents": [_stamped("prefix/far.txt", _FAR_FUTURE)]}]
        with _local_zone("UTC"):
            storage, _ = _storage(pages)
            results = list(storage.scan(S3ScanOptions(recursive=True)))
        assert [r.mtime for r in results] == [_FAR_FUTURE]  # unchanged, and still UTC

    def test_a_far_past_stamp_is_rejected_west_of_utc(self) -> None:
        pages = [{"Contents": [_stamped("prefix/old.txt", _FAR_PAST)]}]
        with _local_zone("America/New_York"):
            storage, _ = _storage(pages)
            with pytest.raises(MalformedResponseError, match="date value out of range") as excinfo:
                list(storage.scan(S3ScanOptions(recursive=True)))
        assert isinstance(excinfo.value.__cause__, OverflowError)
        assert (excinfo.value.bucket, excinfo.value.key) == (storage.bucket, "prefix/old.txt")

    def test_the_far_past_stamp_is_kept_east_of_utc(self) -> None:
        pages = [{"Contents": [_stamped("prefix/old.txt", _FAR_PAST)]}]
        with _local_zone("Asia/Tokyo"):
            storage, _ = _storage(pages)
            results = list(storage.scan(S3ScanOptions(recursive=True)))
        assert [r.mtime for r in results] == [_FAR_PAST]

    def test_a_head_response_carries_the_same_rejection(self) -> None:
        # aws-cli converts the single-object HeadObject stamp the same way
        # (filegenerator's `_list_single_object`).
        head = {"ContentLength": 5, "LastModified": _FAR_FUTURE}
        with _local_zone("Asia/Tokyo"):
            storage, _ = _storage([], head_response=head)
            with pytest.raises(MalformedResponseError, match="date value out of range"):
                storage.get_fileinfo("a.txt")

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows has no check-free range")
    def test_an_ordinary_stamp_costs_no_local_zone_work(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # This runs once per listed object, so away from the ends of the range -
        # where the zone cannot change the verdict - it must not touch dateutil.
        def _forbidden() -> None:
            raise AssertionError("the local zone was consulted for an ordinary stamp")

        monkeypatch.setattr("dateutil.tz.tzlocal", _forbidden)
        with _local_zone("Asia/Tokyo"):
            storage, _ = _storage([{"Contents": [_obj("prefix/a.txt")]}])
            assert [r.key for r in storage.scan(S3ScanOptions(recursive=True))] == ["prefix/a.txt"]

    def test_the_windows_shape_of_the_failure_is_translated_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On Windows the conversion goes through time.localtime, which fails
        # with OSError [Errno 22] - not OverflowError - for a stamp below the
        # epoch or past its own limit. Only the POSIX pair was caught, so the
        # OSError left the library bare (measured on a Windows host; aws.exe
        # dies on the same error with the same line).
        class _WindowsLocalZone(tzinfo):
            def utcoffset(self, dt: datetime | None) -> timedelta:
                raise OSError(errno.EINVAL, "Invalid argument")

        monkeypatch.setattr("dateutil.tz.tzlocal", _WindowsLocalZone)
        monkeypatch.setattr(s3storage, "_ALWAYS_CHECK_LOCAL_ZONE", True)
        with pytest.raises(MalformedResponseError) as exc_info:
            s3storage.reject_unrepresentable_stamp(
                datetime(1960, 1, 1, tzinfo=timezone.utc), bucket="b", key="k"
            )
        error = exc_info.value
        assert str(error) == "[Errno 22] Invalid argument"
        assert isinstance(error.__cause__, OSError)
        assert (error.bucket, error.key) == ("b", "k")

    @pytest.mark.skipif(sys.platform != "win32", reason="the Windows localtime limits")
    def test_a_stamp_below_the_epoch_is_malformed_on_windows(self) -> None:
        with pytest.raises(MalformedResponseError):
            s3storage.reject_unrepresentable_stamp(datetime(1960, 1, 1, tzinfo=timezone.utc))


class TestZonelessStamp:
    """A timestamp with no zone is read as local time, the way aws-cli reads it.

    Every form S3 sends carries a zone. An endpoint that leaves it off
    (``2025-01-01T00:00:00``) makes botocore hand back a naive ``datetime``,
    which aws-cli's ``parse(...).astimezone(tzlocal())`` takes as local time
    and compares, stamps and prints from there (measured against the pinned
    aws through a 127.0.0.1 fake, ``TZ=UTC`` and ``TZ=Asia/Tokyo``). Kept
    naive, the value could not be set against the local side's aware one: a
    sync with one key on both sides ended on ``TypeError``.
    """

    _NAIVE = datetime(2025, 1, 1, 0, 0, 0)

    def test_a_listing_entry_gets_the_local_reading_as_aware_utc(self) -> None:
        storage, _ = _storage([{"Contents": [_stamped("prefix/a.txt", self._NAIVE)]}])
        [entry] = storage.scan(S3ScanOptions(recursive=True))
        assert entry.mtime is not None
        assert entry.mtime.utcoffset() == timedelta(0)
        # `timestamp()` of a naive value is the stdlib's own local-time reading.
        assert entry.mtime.timestamp() == self._NAIVE.timestamp()

    @_needs_tzset
    def test_the_reading_follows_the_local_zone(self) -> None:
        with _local_zone("Asia/Tokyo"):
            storage, _ = _storage([{"Contents": [_stamped("prefix/a.txt", self._NAIVE)]}])
            [entry] = storage.scan(S3ScanOptions(recursive=True))
        assert entry.mtime == datetime(2024, 12, 31, 15, tzinfo=timezone.utc)

    def test_a_stamp_that_carries_a_zone_is_kept_as_it_is(self) -> None:
        east = datetime(2025, 1, 1, 9, tzinfo=timezone(timedelta(hours=9)))
        storage, _ = _storage([{"Contents": [_stamped("prefix/a.txt", east)]}])
        [entry] = storage.scan(S3ScanOptions(recursive=True))
        assert entry.mtime is east

    def test_a_head_gets_the_same_reading(self) -> None:
        head = {"ContentLength": 7, "LastModified": self._NAIVE}
        storage, _ = _storage(url="s3://bucket/prefix/obj.txt", head_response=head)
        info = storage.get_fileinfo()
        assert info is not None and info.mtime is not None
        assert info.mtime.utcoffset() == timedelta(0)
        assert info.mtime.timestamp() == self._NAIVE.timestamp()

    @_needs_tzset
    def test_a_wall_clock_time_the_zone_skips_is_read_as_aws_cli_reads_it(self) -> None:
        # 02:30 on the day New York's clocks jump from 02:00 to 03:00 does not
        # exist. aws-cli, on Python 3.14, places it at 07:30Z (measured: a
        # download is stamped 1741505400); `astimezone()` on a naive value
        # says 06:30Z before Python 3.12, which is why the instant is taken
        # through `timestamp()`.
        gap = datetime(2025, 3, 9, 2, 30)
        with _local_zone("America/New_York"):
            storage, _ = _storage([{"Contents": [_stamped("prefix/a.txt", gap)]}])
            [entry] = storage.scan(S3ScanOptions(recursive=True))
        assert entry.mtime == datetime(2025, 3, 9, 7, 30, tzinfo=timezone.utc)

    def test_the_microseconds_survive_the_reading(self) -> None:
        precise = datetime(2025, 1, 1, 12, 0, 0, 123457)
        storage, _ = _storage([{"Contents": [_stamped("prefix/a.txt", precise)]}])
        [entry] = storage.scan(S3ScanOptions(recursive=True))
        assert entry.mtime is not None
        assert entry.mtime.microsecond == 123457
        assert entry.mtime.replace(microsecond=0).timestamp() == (
            precise.replace(microsecond=0).timestamp()
        )

    def test_the_last_day_of_the_range_is_malformed(self) -> None:
        # aws-cli's Python places a naive value by solving for it on both
        # sides of a possible fold, and the second solution reads the local
        # calendar a day ahead - so a zone-less stamp from 9999-12-31T00:00:00
        # on ends its run (measured under UTC, Asia/Tokyo and
        # America/New_York: `ls` at rc 255 from that second on, rc 0 one
        # microsecond before).
        pages = [{"Contents": [_stamped("prefix/far.txt", datetime(9999, 12, 31, 0, 0, 0))]}]
        storage, _ = _storage(pages)
        with pytest.raises(MalformedResponseError):
            list(storage.scan(S3ScanOptions(recursive=True)))

    @_needs_tzset
    @pytest.mark.parametrize("zone", ["UTC", "Asia/Tokyo", "America/New_York"])
    def test_the_last_day_is_malformed_whatever_the_zone(self, zone: str) -> None:
        # East of UTC and at UTC the stamp's own instant is in range; only the
        # day-ahead step leaves it. The error is the one that step raises, the
        # year running out, as it is for aws-cli.
        pages = [{"Contents": [_stamped("prefix/far.txt", datetime(9999, 12, 31, 0, 0, 0))]}]
        with _local_zone(zone):
            storage, _ = _storage(pages)
            with pytest.raises(MalformedResponseError) as exc_info:
                list(storage.scan(S3ScanOptions(recursive=True)))
        if sys.platform == "linux":
            # The step's own error (glibc reads the calendar a day ahead and
            # CPython then refuses the year); another libc may fail the read
            # itself, so only the kind of outcome is held elsewhere.
            assert isinstance(exc_info.value.__cause__, ValueError)
            assert "10000" in str(exc_info.value)

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows' localtime ends at the year 3000")
    def test_the_microsecond_before_the_last_day_is_kept(self) -> None:
        stamp = datetime(9999, 12, 30, 23, 59, 59, 999999)
        storage, _ = _storage([{"Contents": [_stamped("prefix/far.txt", stamp)]}])
        with _local_zone("UTC"):
            [entry] = storage.scan(S3ScanOptions(recursive=True))
        assert entry.mtime == stamp.replace(tzinfo=timezone.utc)

    def test_a_bucket_creation_date_gets_the_same_reading(self) -> None:
        pages = [{"Buckets": [{"Name": "alpha", "CreationDate": self._NAIVE}]}]
        storage, _ = _storage(pages, url="s3://")
        [bucket] = storage.list_buckets()
        assert bucket.mtime is not None
        assert bucket.mtime.utcoffset() == timedelta(0)
        assert bucket.mtime.timestamp() == self._NAIVE.timestamp()

    @_needs_tzset
    def test_a_zoneless_stamp_the_conversion_cannot_hold_is_malformed(self) -> None:
        # The reading itself can fail at the edge of datetime's range; aws-cli
        # dies on the same conversion.
        pages = [{"Contents": [_stamped("prefix/far.txt", datetime(1, 1, 1))]}]
        with _local_zone("Asia/Tokyo"):
            storage, _ = _storage(pages)
            with pytest.raises(MalformedResponseError) as exc_info:
                list(storage.scan(S3ScanOptions(recursive=True)))
        assert (exc_info.value.bucket, exc_info.value.key) == ("bucket", "prefix/far.txt")
        assert isinstance(exc_info.value.__cause__, (OverflowError, ValueError, OSError))


class TestScanErrorMapping:
    def test_client_error_404_maps_to_not_found(self) -> None:
        error = ClientError(
            {
                "Error": {"Code": "NoSuchBucket", "Message": "The specified bucket does not exist"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "ListObjectsV2",
        )
        storage, _ = _storage(error=error)
        with pytest.raises(NotFoundError) as exc_info:
            list(storage.scan())
        assert isinstance(exc_info.value.__cause__, ClientError)

    def test_lazy_client_build_failure_maps_to_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A lazily-built default client whose construction fails - e.g.
        # AWS_PROFILE naming a missing profile - surfaces as the documented
        # InvalidConfigError refinement (a set-but-unusable configuration,
        # design/exceptions.md section 3), not the raw botocore error.
        import boto3

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise ProfileNotFound(profile="missing-profile")

        monkeypatch.setattr(boto3, "client", boom)
        with pytest.raises(InvalidConfigError) as exc_info:
            list(S3Storage("s3://bucket/prefix/").scan())
        assert isinstance(exc_info.value.__cause__, ProfileNotFound)

    def test_malformed_env_endpoint_maps_to_invalid_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The lazy default build has the same plain-ValueError hole as
        # S3.client(): botocore rejects a malformed AWS_ENDPOINT_URL with a
        # ValueError that is not a BotoCoreError. It must surface as the
        # translated refinement (rc 255 lane), never raw.
        monkeypatch.setenv("AWS_ENDPOINT_URL", "not-a-url")
        with pytest.raises(InvalidConfigError) as exc_info:
            S3Storage("s3://bucket/prefix/").get_client()
        assert type(exc_info.value) is InvalidConfigError
        assert isinstance(exc_info.value.__cause__, ValueError)

    def test_unresolvable_credentials_stay_plain_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # NoCredentials/NoRegion keep the PLAIN ConfigurationError - the CLI
        # maps that to aws's dedicated rc 253, while the InvalidConfigError
        # refinement maps to the general 255 (design/exceptions.md section 3).
        import boto3
        from botocore.exceptions import NoCredentialsError

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise NoCredentialsError()

        monkeypatch.setattr(boto3, "client", boom)
        with pytest.raises(ConfigurationError) as exc_info:
            list(S3Storage("s3://bucket/prefix/").scan())
        assert type(exc_info.value) is ConfigurationError

    @pytest.mark.parametrize(
        ("status", "category"),
        [
            (403, AccessDeniedError),
            (404, NotFoundError),
            (400, ValidationError),
            (500, TransportError),
            (None, Boto3S3Error),
        ],
    )
    def test_unknown_code_widens_on_http_status(
        self, status: int | None, category: type[Boto3S3Error]
    ) -> None:
        # design/exceptions.md section 3: a code not in the table falls back to
        # HTTP-status widening - 5xx -> TransportError, no usable status ->
        # the base Boto3S3Error.
        meta: dict[str, Any] = {"HTTPStatusCode": status} if status is not None else {}
        error = ClientError(
            {"Error": {"Code": "SomethingNovel", "Message": "boom"}, "ResponseMetadata": meta},
            "ListObjectsV2",
        )
        storage, _ = _storage(error=error)
        with pytest.raises(Boto3S3Error) as exc_info:
            list(storage.scan())
        assert type(exc_info.value) is category

    def test_keyboard_interrupt_passes_through_untranslated(self) -> None:
        # design/exceptions.md section 2: KeyboardInterrupt is never wrapped.
        from boto3_s3.s3storage import s3_errors

        with pytest.raises(KeyboardInterrupt):
            with s3_errors(operation="ls"):
                raise KeyboardInterrupt

    def test_missing_dependency_stays_plain_configuration_error(self) -> None:
        # A missing optional dependency (awscrt absent where SigV4a signing
        # needs it - an MRAP presign, say) is the crt-absence family: the
        # PLAIN ConfigurationError the CLI maps to rc 253, cause preserved.
        # The engine-selection pass-through (crtsupport) never crosses this
        # seam and stays unwrapped.
        from botocore.exceptions import MissingDependencyException

        from boto3_s3.s3storage import s3_errors

        with pytest.raises(ConfigurationError) as exc_info:
            with s3_errors(operation="presign", bucket="b", key="k"):
                raise MissingDependencyException(msg="Missing Dependency: install awscrt")
        assert type(exc_info.value) is ConfigurationError
        assert isinstance(exc_info.value.__cause__, MissingDependencyException)

    def test_object_scan_error_carries_no_fixed_operation(self) -> None:
        # The recursive object listing backs ls, rm, cp, mv, and sync source
        # enumeration alike, so the calling subcommand is unknown here: a scan
        # failure carries operation=None rather than mislabelling the others "ls".
        error = ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "denied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "ListObjectsV2",
        )
        storage, _ = _storage(error=error)
        with pytest.raises(AccessDeniedError) as exc_info:
            list(storage.scan())
        assert exc_info.value.operation is None

    @pytest.mark.parametrize(
        ("run", "operation"),
        [
            ("ls", "ls"),
            ("rm", "rm"),
            ("rm --dryrun", "rm"),
            ("cp", "cp"),
            ("mv", "mv"),
            ("sync", "sync"),
        ],
    )
    def test_the_operation_reading_the_listing_names_its_failure(
        self, tmp_path: Path, run: str, operation: str
    ) -> None:
        # The listing cannot know who reads it, so it raises unnamed and the
        # operation fills its own name in - the rule open / delete / validate
        # failures follow. One listing failure, five names.
        denied = client_error("AccessDenied", 403, "ListObjectsV2")
        client, _calls = make_recording_client([denied, denied])
        source = S3Storage("s3://bucket/prefix/", client=client)
        s3 = S3()
        runs = {
            "ls": lambda: s3.ls(source, on_entry=lambda info: None, recursive=True),
            "rm": lambda: s3.rm(source, recursive=True),
            "rm --dryrun": lambda: s3.rm(source, recursive=True, dryrun=True),
            "cp": lambda: s3.cp(source, str(tmp_path), recursive=True),
            "mv": lambda: s3.mv(source, str(tmp_path), recursive=True),
            "sync": lambda: s3.sync(source, str(tmp_path)),
        }
        with pytest.raises(AccessDeniedError) as exc_info:
            runs[run]()
        error = exc_info.value
        assert (error.operation, error.bucket, error.key) == (operation, "bucket", None)
        assert error.__cause__ is denied

    @pytest.mark.parametrize("run", ["ls", "rm --dryrun"])
    def test_what_the_callers_callback_raises_is_not_the_listings(self, run: str) -> None:
        # Only fetching an entry is the listing's. A storage call the callback
        # makes itself - the documented way to read an entry's object - raises
        # unnamed, and stays unnamed: the operation's name was being written
        # over the whole delivery loop.
        denied = client_error("AccessDenied", 403, "HeadObject")
        client, _calls = make_recording_client([listing(("prefix/a", 1)), denied])
        source = S3Storage("s3://bucket/prefix/", client=client)

        def read_it(_record: object) -> None:
            source.get_fileinfo("prefix/a")

        s3 = S3()
        runs = {
            "ls": lambda: s3.ls(source, on_entry=read_it, recursive=True),
            "rm --dryrun": lambda: s3.rm(source, recursive=True, dryrun=True, on_result=read_it),
        }
        with pytest.raises(AccessDeniedError) as exc_info:
            runs[run]()
        assert exc_info.value.operation is None

    @pytest.mark.parametrize(
        "run", ["ls", "rb", "website", "rm", "rm --recursive", "rm --recursive --dryrun"]
    )
    def test_failing_to_build_the_client_names_no_operation(self, run: str) -> None:
        # A storage built without a client builds one on first use. That
        # failing is no operation's failure (exceptions.md), and the transfer
        # routes, mb and presign reported it so; these routes built the client
        # inside their request's or listing's attribution and named
        # themselves - the single-key rm as a FAILED record and a BatchError.
        class _Unbuildable(S3Storage):
            def get_client(self) -> Any:
                raise InvalidConfigError("The config profile (nope) could not be found")

        results: list[OpResult] = []
        s3 = S3()
        key = _Unbuildable("s3://bucket/k")
        prefix = _Unbuildable("s3://bucket/prefix/")
        runs = {
            "ls": lambda: s3.ls(prefix, on_entry=lambda info: None),
            "rb": lambda: s3.rb(_Unbuildable("s3://bucket")),
            "website": lambda: s3.website(_Unbuildable("s3://bucket"), index_document="i.html"),
            "rm": lambda: s3.rm(key, on_result=results.append),
            "rm --recursive": lambda: s3.rm(prefix, recursive=True, on_result=results.append),
            "rm --recursive --dryrun": lambda: s3.rm(
                prefix, recursive=True, dryrun=True, on_result=results.append
            ),
        }
        with pytest.raises(InvalidConfigError) as exc_info:
            runs[run]()
        error = exc_info.value
        assert (error.operation, error.bucket, error.key) == (None, None, None)
        assert results == []

    def test_a_bucket_listing_failure_is_named_by_ls_alone(self) -> None:
        denied = client_error("AccessDenied", 403, "ListBuckets")
        client, _calls = make_recording_client([denied, denied])
        storage = S3Storage("s3://", client=client)
        with pytest.raises(AccessDeniedError) as direct:
            list(storage.list_buckets())
        assert direct.value.operation is None
        with pytest.raises(AccessDeniedError) as through_ls:
            S3().ls(storage, on_entry=lambda info: None)
        assert through_ls.value.operation == "ls"

    def test_a_direct_head_failure_names_no_operation(self) -> None:
        # get_fileinfo is a storage-level call like open and delete: only an
        # operation that reaches it could name the failure.
        storage, _ = _storage(head_error=client_error("AccessDenied", 403, "HeadObject"))
        with pytest.raises(AccessDeniedError) as exc_info:
            storage.get_fileinfo()
        error = exc_info.value
        assert (error.operation, error.bucket, error.key) == (None, "bucket", "prefix/")


class _DeleteRecordingClient:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return {}


def _request_scan(storage: S3Storage, tmp_path: Path) -> object:
    return list(storage.scan(S3ScanOptions(recursive=True)))


def _request_list_buckets(storage: S3Storage, tmp_path: Path) -> object:
    return list(storage.list_buckets())


def _request_get_fileinfo(storage: S3Storage, tmp_path: Path) -> object:
    return storage.get_fileinfo("k")


def _request_open(storage: S3Storage, tmp_path: Path) -> object:
    return storage.open("prefix/k", "rb")


def _request_delete(storage: S3Storage, tmp_path: Path) -> object:
    return storage.delete(FileInfo(key="prefix/k"))


def _request_get_file(storage: S3Storage, tmp_path: Path) -> object:
    return storage.get_file(tmp_path / "out.bin", key="k")


def _request_put_file(storage: S3Storage, tmp_path: Path) -> object:
    source = tmp_path / "in.bin"
    source.write_bytes(b"x")
    return storage.put_file(source, key="k")


# Each request the backend issues itself, with the attribution its failure
# carries: (operation, bucket, key).
_REQUEST_POINTS: list[tuple[Any, tuple[str | None, str | None, str | None]]] = [
    (_request_scan, (None, "bucket", None)),
    (_request_list_buckets, (None, None, None)),
    (_request_get_fileinfo, (None, "bucket", "prefix/k")),
    (_request_open, (None, "bucket", "prefix/k")),
    (_request_delete, (None, "bucket", "prefix/k")),
    (_request_get_file, ("get_file", "bucket", "prefix/k")),
    (_request_put_file, ("put_file", "bucket", "prefix/k")),
]


class TestRequestRaisingOutsideTheBotoFamily:
    """Whatever a request raises from inside botocore is that request failing.

    botocore can die reading a response with an exception that is none of its
    own: an S3 Express ``CreateSession`` reply without ``Credentials``
    (``KeyError``), a region-less 301 looping its redirector into
    ``RecursionError``, a value its parser cannot convert (``ValueError``).
    aws-cli ends the run with that exception's text - ``fatal error:
    'Credentials'`` at rc 1 for a transfer, ``[ERROR]: 'Credentials'`` at rc 255
    for ``ls``, measured against the pinned aws (2.36.40) through a 127.0.0.1
    fake - and every request the backend issues reports it as the base
    ``Boto3S3Error`` carrying the original, so ``except Boto3S3Error`` catches
    a failed request whatever botocore died of (the deleter and the
    single-call operations already did).
    """

    @pytest.mark.parametrize(("request_point", "attribution"), _REQUEST_POINTS)
    @pytest.mark.parametrize(
        "boom",
        [
            KeyError("Credentials"),
            RecursionError("maximum recursion depth exceeded"),
            ValueError('Invalid timestamp "garbage": Unknown string format: garbage'),
        ],
        ids=["KeyError", "RecursionError", "ValueError"],
    )
    def test_it_becomes_the_base_error_carrying_the_original(
        self,
        tmp_path: Path,
        request_point: Any,
        attribution: tuple[str | None, str | None, str | None],
        boom: Exception,
    ) -> None:
        client, _calls = make_recording_client([boom])
        storage = S3Storage("s3://bucket/prefix/", client=client)
        with pytest.raises(Boto3S3Error) as excinfo:
            request_point(storage, tmp_path)
        error = excinfo.value
        assert type(error) is Boto3S3Error
        assert error.__cause__ is boom
        assert str(error) == str(boom)  # the text aws-cli's line carries
        assert (error.operation, error.bucket, error.key) == attribution

    @pytest.mark.parametrize(("request_point", "attribution"), _REQUEST_POINTS)
    def test_an_assertion_passes_through(
        self,
        tmp_path: Path,
        request_point: Any,
        attribution: tuple[str | None, str | None, str | None],
    ) -> None:
        # An invariant or a test double's unexpected-call guard is never a
        # request outcome: the carve-out the deleter's capture makes too.
        client, _calls = make_recording_client([AssertionError("unexpected call")])
        storage = S3Storage("s3://bucket/prefix/", client=client)
        with pytest.raises(AssertionError, match="unexpected call"):
            request_point(storage, tmp_path)

    def test_a_later_page_failing_keeps_the_pages_already_listed(self) -> None:
        boom = RecursionError("maximum recursion depth exceeded")
        first = {
            "Contents": [_obj("prefix/a")],
            "IsTruncated": True,
            "NextContinuationToken": "t",
        }
        client, _calls = make_recording_client([first, boom])
        storage = S3Storage("s3://bucket/prefix/", client=client)
        keys = _keys_until_raise(storage.scan(S3ScanOptions(recursive=True)), Boto3S3Error)
        assert keys == ["prefix/a"]

    def test_a_body_read_raising_it_leaves_no_file_behind(self, tmp_path: Path) -> None:
        # The streamed body is part of the GetObject: what reading it raises
        # is the request failing, not the local filesystem.
        boom = RecursionError("maximum recursion depth exceeded")

        class _Body:
            def read(self, amt: int | None = None) -> bytes:
                raise boom

            def close(self) -> None:
                pass

        client, _calls = make_recording_client([{"Body": _Body(), "ContentLength": 9}])
        storage = S3Storage("s3://bucket/prefix/", client=client)
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"old")
        with pytest.raises(Boto3S3Error) as excinfo:
            storage.get_file(dest, key="k")
        assert type(excinfo.value) is Boto3S3Error
        assert excinfo.value.__cause__ is boom
        assert (excinfo.value.bucket, excinfo.value.key) == ("bucket", "prefix/k")
        assert dest.read_bytes() == b"old"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["out.bin"]

    def test_the_conversion_of_a_fetched_page_is_not_a_request(self) -> None:
        # Only the request sits inside the capture: what this module's own
        # reading of the page raises keeps its type (a programming error
        # stays loud instead of being reported as the service failing).
        entry = {**_obj("prefix/a"), "Key": 7}  # no parser ever yields a non-str Key
        client, _calls = make_recording_client([{"Contents": [entry]}])
        storage = S3Storage("s3://bucket/prefix/", client=client)
        with pytest.raises(TypeError):
            list(storage.scan(S3ScanOptions(recursive=True)))


class TestListingEntryBotocoreCannotDecode:
    """An entry missing ``Key`` is a malformed response even when botocore
    meets it first.

    botocore asks every ``ListObjectsV2`` for ``EncodingType=url`` and, when
    the response echoes it (S3 and MinIO do), URL-decodes each entry's ``Key``
    and each common prefix's ``Prefix`` by subscript as it parses the page -
    ahead of the backend's own required-element read. aws-cli dies in that
    same handler (``fatal error: 'Key'`` at rc 1 for ``rm --recursive``,
    ``[ERROR]: 'Key'`` at rc 255 for ``ls --recursive``, measured against the
    pinned aws (2.36.40) through a 127.0.0.1 fake echoing ``EncodingType``),
    so the report is the one `TestMalformedListingEntries` pins for a response
    that does not echo it. The recording client bypasses botocore's parser, so
    the handler's ``KeyError`` is raised in its place.
    """

    @pytest.mark.parametrize("element", ["Key", "Prefix"])
    def test_the_decoding_key_error_is_a_malformed_response(self, element: str) -> None:
        boom = KeyError(element)
        client, _calls = make_recording_client([boom])
        storage = S3Storage("s3://bucket/prefix/", client=client)
        with pytest.raises(MalformedResponseError) as excinfo:
            list(storage.scan(S3ScanOptions(recursive=True)))
        error = excinfo.value
        assert str(error) == f"'{element}'"  # str(KeyError): the CLI's line
        assert error.__cause__ is boom
        assert (error.operation, error.bucket, error.key) == (None, "bucket", None)

    def test_another_missing_element_stays_the_base_error(self) -> None:
        # Not an entry of this listing: an element botocore reads elsewhere in
        # the request (an S3 Express session reply) is the request failing.
        client, _calls = make_recording_client([KeyError("Credentials")])
        storage = S3Storage("s3://bucket/prefix/", client=client)
        with pytest.raises(Boto3S3Error) as excinfo:
            list(storage.scan(S3ScanOptions(recursive=True)))
        assert type(excinfo.value) is Boto3S3Error


class TestDelete:
    def test_blind_delete_object_call(self) -> None:
        client = _DeleteRecordingClient()
        S3Storage("s3://bucket/any", client=client).delete(S3FileInfo(key="data/a.txt"))
        assert client.calls == [{"Bucket": "bucket", "Key": "data/a.txt"}]

    def test_request_payer_forwarded(self) -> None:
        client = _DeleteRecordingClient()
        S3Storage("s3://bucket", client=client).delete(
            S3FileInfo(key="k"), request_payer="requester"
        )
        assert client.calls == [{"Bucket": "bucket", "Key": "k", "RequestPayer": "requester"}]

    def test_client_error_translates_with_key_context(self) -> None:
        error = ClientError(
            {
                "Error": {"Code": "NoSuchBucket", "Message": "gone"},
                "ResponseMetadata": {"HTTPStatusCode": 404},
            },
            "DeleteObject",
        )
        client = _DeleteRecordingClient(error=error)
        with pytest.raises(NotFoundError) as exc_info:
            S3Storage("s3://bucket", client=client).delete(S3FileInfo(key="k"))
        assert exc_info.value.bucket == "bucket"
        assert exc_info.value.key == "k"
        assert isinstance(exc_info.value.__cause__, ClientError)


class TestConstructor:
    def test_s3_scheme_is_optional(self) -> None:
        storage = S3Storage("bucket/some/prefix")
        assert (storage.bucket, storage.key) == ("bucket", "some/prefix")

    def test_explicit_s3_scheme_is_equivalent(self) -> None:
        bare = S3Storage("bucket/some/prefix")
        explicit = S3Storage("s3://bucket/some/prefix")
        assert (bare.bucket, bare.key) == (explicit.bucket, explicit.key)

    def test_uri_is_canonicalized_with_scheme(self) -> None:
        assert S3Storage("bucket/key").uri == "s3://bucket/key"

    def test_empty_bucket_is_the_service_root(self) -> None:
        for url in ("s3://", ""):
            storage = S3Storage(url)
            assert (storage.bucket, storage.key) == ("", "")
            assert storage.uri == "s3://"

    def test_key_without_bucket_is_rejected(self) -> None:
        # Construction is permissive (non-raising); validate() does the rejection.
        with pytest.raises(ValidationError):
            S3Storage("s3:///key").validate()

    def test_key_without_bucket_rejection_carries_the_key(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            S3Storage("s3:///key").validate()
        assert exc_info.value.key == "key"
        assert exc_info.value.bucket is None

    def test_direct_validate_leaves_the_operation_unset(self) -> None:
        # The storage does not know which operation is about to use it; the
        # operation layer stamps the name (see test_s3.py). A caller invoking
        # validate() directly keeps the documented operation=None.
        with pytest.raises(ValidationError) as exc_info:
            S3Storage("s3:///key").validate()
        assert exc_info.value.operation is None


class TestArnBuckets:
    """ARN-shaped bucket parts split like aws-cli's ``find_bucket_key``.

    The whole access-point ARN - slash-separated name included - is the
    bucket; only what follows it is the key. Object Lambda and Outposts
    *bucket* ARNs are rejected by ``S3Storage.validate`` (deferred from the
    permissive construction) the way ``aws s3`` rejects them at parse time
    (ParamValidation -> rc 252, verified against aws-cli 2.34).

    The client needs no ARN-derived region: botocore resolves the endpoint
    and signing region from the ARN at request time (``use_arn_region``
    defaults to true), in aws-cli's vendored botocore and ours alike.
    """

    _ACCESSPOINT = "arn:aws:s3:us-west-2:123456789012:accesspoint/endpoint"
    _OUTPOST_ACCESSPOINT = (
        "arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-01234567890123456/accesspoint/my-ap"
    )

    def test_accesspoint_arn_is_the_bucket(self) -> None:
        storage = S3Storage(f"s3://{self._ACCESSPOINT}")
        assert (storage.bucket, storage.key) == (self._ACCESSPOINT, "")

    def test_accesspoint_arn_with_key(self) -> None:
        storage = S3Storage(f"s3://{self._ACCESSPOINT}/some/prefix")
        assert (storage.bucket, storage.key) == (self._ACCESSPOINT, "some/prefix")

    def test_accesspoint_arn_with_colon_name_separator(self) -> None:
        arn = "arn:aws:s3:us-west-2:123456789012:accesspoint:endpoint"
        storage = S3Storage(f"s3://{arn}/key")
        assert (storage.bucket, storage.key) == (arn, "key")

    def test_outpost_accesspoint_arn_is_the_bucket(self) -> None:
        storage = S3Storage(f"s3://{self._OUTPOST_ACCESSPOINT}/some/prefix")
        assert (storage.bucket, storage.key) == (self._OUTPOST_ACCESSPOINT, "some/prefix")

    def test_object_lambda_arn_is_rejected(self) -> None:
        arn = "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-olap"
        with pytest.raises(ValidationError, match="S3 Object Lambda"):
            S3Storage(f"s3://{arn}").validate()

    def test_outpost_bucket_arn_is_rejected(self) -> None:
        arn = (
            "arn:aws:s3-outposts:us-west-2:123456789012:"
            "outpost/op-01234567890123456/bucket/my-bucket"
        )
        with pytest.raises(ValidationError, match="Outpost Bucket"):
            S3Storage(f"s3://{arn}").validate()

    @pytest.mark.parametrize(
        "arn",
        [
            "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-olap",
            "arn:aws:s3-outposts:us-west-2:123456789012:"
            "outpost/op-01234567890123456/bucket/my-bucket",
        ],
    )
    def test_rejections_carry_the_whole_arn_as_the_bucket(self, arn: str) -> None:
        # The whole ARN identifies what was rejected, whatever the constructor's
        # splitters made of it.
        with pytest.raises(ValidationError) as exc_info:
            S3Storage(f"s3://{arn}/some/key").validate()
        assert exc_info.value.bucket == arn
        assert exc_info.value.key is None


class TestResolveRouting:
    """``S3.resolve`` routes strictly: only ``s3://`` is S3, everything else local.

    The constructor stays lenient (a bare ``"bucket/key"`` is claimed by S3 via
    explicit construction), but ``resolve`` - what ``cp`` / ``mv`` / ``sync`` use
    to interpret a ``Location`` - keeps the local / S3 distinction aws-cli relies
    on. A ``Storage`` instance passed in is returned verbatim.
    """

    def test_resolve_routes_bare_path_to_local(self) -> None:
        assert isinstance(S3().resolve("bucket/key"), LocalStorage)

    def test_resolve_routes_s3_url_to_s3(self) -> None:
        assert isinstance(S3().resolve("s3://bucket/key"), S3Storage)

    def test_resolve_claims_service_root(self) -> None:
        assert isinstance(S3().resolve("s3://"), S3Storage)

    def test_resolve_returns_storage_verbatim(self) -> None:
        storage = LocalStorage("some/path")
        assert S3().resolve(storage) is storage


class TestOpen:
    """``S3Storage.open`` - a ``GetObject`` read convenience (``"rb"`` only);
    ``"wb"`` stays unimplemented (S3 writes on the transfer lanes ride
    s3transfer, and ``put_file`` writes a whole file rather than a stream)."""

    def test_rb_reads_object_bytes_by_full_key(self) -> None:
        # The key is the object's *full* bucket key, used verbatim as the S3 Key
        # (no prefix join) - so info.storage.open(info.key, "rb") reads it directly.
        client, calls = make_recording_client([{"Body": io.BytesIO(b"hello-content")}])
        storage = S3Storage("s3://bucket/data", client=client)
        with storage.open("data/x.txt", "rb") as fh:
            assert fh.read() == b"hello-content"
        assert calls == [ApiCall("GetObject", {"Bucket": "bucket", "Key": "data/x.txt"})]

    def test_rb_uses_the_key_verbatim_regardless_of_storage_prefix(self) -> None:
        # No relativization against the storage's own key/prefix: the passed key
        # is the GetObject Key as-is.
        client, calls = make_recording_client([{"Body": io.BytesIO(b"")}])
        storage = S3Storage("s3://bucket/some/prefix", client=client)
        storage.open("other/deep/key.bin", "rb").close()
        assert calls == [ApiCall("GetObject", {"Bucket": "bucket", "Key": "other/deep/key.bin"})]

    def test_wb_raises_not_implemented(self) -> None:
        client, calls = make_recording_client([])
        storage = S3Storage("s3://bucket/data", client=client)
        with pytest.raises(NotImplementedError, match="mode='wb'"):
            storage.open("data/x.txt", "wb")
        assert calls == []  # no API call for the unimplemented write

    def test_rb_error_translates_to_taxonomy(self) -> None:
        # A GetObject 404 surfaces as NotFoundError (s3_errors taxonomy), like
        # the rest of the S3 call sites - not a raw botocore ClientError.
        client, _calls = make_recording_client([client_error("NoSuchKey", 404, "GetObject")])
        storage = S3Storage("s3://bucket/data", client=client)
        with pytest.raises(NotFoundError):
            storage.open("data/gone.txt", "rb")

    def test_capability_declares_open_read_not_write(self) -> None:
        caps = S3Storage.capabilities
        assert StorageCapability.OPEN_READ in caps
        assert StorageCapability.OPEN_WRITE not in caps
        storage = S3Storage("s3://bucket/data")
        assert storage.supports(StorageCapability.OPEN_READ)
        assert not storage.supports(StorageCapability.OPEN_WRITE)


def _get_response(body: bytes, **extra: Any) -> dict[str, Any]:
    """A canned GetObject response streaming `body`; `extra` overlays it."""
    return {
        "Body": io.BytesIO(body),
        "ContentLength": len(body),
        "LastModified": MTIME,
        "ETag": '"abc"',
        **extra,
    }


class _FailingBody:
    """A GetObject body that yields one chunk and then breaks mid-stream.

    ``ResponseStreamingError`` is what botocore raises when the response stream
    dies part-way through (its ``StreamingBody.read`` wraps urllib3's protocol
    error), so it is the shape a truncated download really arrives in.
    """

    def __init__(self, chunk: bytes) -> None:
        self._chunk = chunk
        self.closes = 0

    def read(self, amt: int | None = None) -> bytes:
        if self._chunk:
            chunk, self._chunk = self._chunk, b""
            return chunk
        raise ResponseStreamingError(error="connection reset")

    def close(self) -> None:
        self.closes += 1


class TestGetFile:
    """``S3Storage.get_file`` - one ``GetObject`` streamed onto a local path.

    The single-request lane: no ``HeadObject`` probe, no transfer engine, and an
    atomic local write (sibling temp file + ``os.replace``), the safety property
    s3transfer's download lane has.
    """

    def test_downloads_the_object_and_returns_its_fileinfo(self, tmp_path: Path) -> None:
        response = _get_response(b"payload-bytes", StorageClass="STANDARD_IA")
        client, calls = make_recording_client([dict(response)])
        storage = S3Storage("s3://bucket/prefix/", client=client)
        dest = tmp_path / "manifest.json"

        info = storage.get_file(dest, key="manifest.json")

        # Exactly one request, and it is the GetObject: no pre-transfer
        # HeadObject, which is what makes a cp download cost two.
        assert calls == [ApiCall("GetObject", {"Bucket": "bucket", "Key": "prefix/manifest.json"})]
        assert dest.read_bytes() == b"payload-bytes"
        # The temp file the download finished into is gone, not left beside it.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["manifest.json"]
        assert isinstance(info, S3FileInfo)
        assert info.key == "prefix/manifest.json"
        assert info.compare_key == "manifest.json"  # basename, as get_fileinfo stamps
        assert info.size == 13
        assert info.mtime == MTIME
        assert info.etag == '"abc"'  # as the response carried it, quotes included
        assert info.storage_class == "STANDARD_IA"
        assert info.storage is storage
        # head is the parsed response minus the transport metadata and the body.
        assert info.head == {
            "ContentLength": 13,
            "LastModified": MTIME,
            "ETag": '"abc"',
            "StorageClass": "STANDARD_IA",
        }

    def test_a_zoneless_stamp_comes_back_aware(self, tmp_path: Path) -> None:
        # The listing's reading of a stamp with no zone (TestZonelessStamp):
        # local time, kept as aware UTC.
        naive = datetime(2025, 1, 1, 12, 0, 0)
        client, _ = make_recording_client([dict(_get_response(b"x", LastModified=naive))])
        info = S3Storage("s3://bucket/prefix/", client=client).get_file(tmp_path / "f", key="f")
        assert info.mtime is not None
        assert info.mtime.utcoffset() == timedelta(0)
        assert info.mtime.timestamp() == naive.timestamp()

    def test_a_stamp_that_cannot_be_read_fails_before_the_file_is_touched(
        self, tmp_path: Path
    ) -> None:
        # The stamp is judged before a byte is written: the call fails with the
        # destination as it was, and names its operation like every other
        # get_file failure.
        dest = tmp_path / "f"
        dest.write_bytes(b"what was there")
        unreadable = datetime(9999, 12, 31, 12, 0, 0)  # zone-less, in the range's last day
        client, _ = make_recording_client([dict(_get_response(b"new", LastModified=unreadable))])
        with pytest.raises(MalformedResponseError) as exc_info:
            S3Storage("s3://bucket/prefix/", client=client).get_file(dest, key="f")
        error = exc_info.value
        assert (error.operation, error.bucket, error.key) == ("get_file", "bucket", "prefix/f")
        assert dest.read_bytes() == b"what was there"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]  # no temp file left

    @pytest.mark.parametrize(
        ("url", "key", "expected"),
        [
            # "" is the storage's own location, exactly as in get_fileinfo.
            ("s3://bucket/prefix/obj.txt", "", "prefix/obj.txt"),
            ("s3://bucket/prefix/", "sub/f.txt", "prefix/sub/f.txt"),
            # The "/" boundary is inserted even when the prefix lacks one.
            ("s3://bucket/prefix", "sub/f.txt", "prefix/sub/f.txt"),
            # ... and never invented under a keyless location.
            ("s3://bucket", "a.txt", "a.txt"),
        ],
    )
    def test_key_joins_like_get_fileinfo(
        self, tmp_path: Path, url: str, key: str, expected: str
    ) -> None:
        client, calls = make_recording_client([_get_response(b"x")])
        storage = S3Storage(url, client=client)
        info = storage.get_file(tmp_path / "out.bin", key=key)
        assert calls == [ApiCall("GetObject", {"Bucket": "bucket", "Key": expected})]
        assert info.key == expected

    def test_missing_object_raises_not_found(self, tmp_path: Path) -> None:
        # Unlike get_fileinfo's existence check, a download of an absent object
        # is a failure: the 404 surfaces as the taxonomy's NotFoundError.
        client, _calls = make_recording_client([client_error("NoSuchKey", 404, "GetObject")])
        storage = S3Storage("s3://bucket/data", client=client)
        dest = tmp_path / "gone.txt"
        with pytest.raises(NotFoundError) as exc_info:
            storage.get_file(dest, key="gone.txt")
        assert exc_info.value.bucket == "bucket"
        assert exc_info.value.key == "data/gone.txt"
        assert not dest.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_an_existing_destination_keeps_its_permission_bits(self, tmp_path: Path) -> None:
        # The atomic replace lands a NEW inode, so without the mode copy the
        # download would silently re-mode the file it replaced.
        dest = tmp_path / "state.json"
        dest.write_bytes(b"old")
        dest.chmod(0o640)
        client, _calls = make_recording_client([_get_response(b"new")])
        S3Storage("s3://bucket/state.json", client=client).get_file(dest)
        assert dest.read_bytes() == b"new"
        assert stat.S_IMODE(dest.stat().st_mode) == 0o640

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_a_new_destination_takes_the_bits_a_plain_write_would(self, tmp_path: Path) -> None:
        # Nothing to preserve, so the umask decides - the temp file's own 0600
        # must not leak into the destination as a side effect of the mechanism.
        reference = tmp_path / "reference.bin"
        reference.write_bytes(b"")
        client, _calls = make_recording_client([_get_response(b"new")])
        dest = tmp_path / "fresh.bin"
        S3Storage("s3://bucket/fresh.bin", client=client).get_file(dest)
        assert stat.S_IMODE(dest.stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)

    def test_a_body_failing_mid_read_leaves_the_destination_intact(self, tmp_path: Path) -> None:
        dest = tmp_path / "state.json"
        dest.write_bytes(b"previous-contents")
        body = _FailingBody(b"partial")
        client, _calls = make_recording_client([{"Body": body, "ContentLength": 99}])
        storage = S3Storage("s3://bucket/state.json", client=client)

        with pytest.raises(TransportError) as exc_info:
            storage.get_file(dest)

        # The transport failure is attributed to the S3 side, not the local one.
        assert exc_info.value.bucket == "bucket"
        assert exc_info.value.key == "state.json"
        assert isinstance(exc_info.value.__cause__, ResponseStreamingError)
        assert dest.read_bytes() == b"previous-contents"  # byte-for-byte
        assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]  # no temp-file litter
        assert body.closes == 1  # the connection is released either way

    def test_a_body_ending_short_is_a_transport_error_on_either_urllib3(
        self, tmp_path: Path
    ) -> None:
        # urllib3 2 enforces Content-Length itself and the cut arrives as a
        # broken stream (the case above); under urllib3 1.x botocore's own
        # length check raises IncompleteReadError, which botocore files
        # outside its transport tree. One event, one category.
        short = IncompleteReadError(actual_bytes=5, expected_bytes=16)

        class _ShortBody:
            def read(self, amt: int | None = None) -> bytes:
                raise short

            def close(self) -> None:
                pass

        dest = tmp_path / "state.json"
        dest.write_bytes(b"previous-contents")
        client, _calls = make_recording_client([{"Body": _ShortBody(), "ContentLength": 16}])
        with pytest.raises(TransportError) as exc_info:
            S3Storage("s3://bucket/state.json", client=client).get_file(dest)
        assert exc_info.value.__cause__ is short
        assert (exc_info.value.bucket, exc_info.value.key) == ("bucket", "state.json")
        assert dest.read_bytes() == b"previous-contents"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]

    def test_a_local_write_failure_is_attributed_locally_and_cleans_up(
        self, tmp_path: Path
    ) -> None:
        # A directory at the destination path: the temp file is written, and the
        # replace onto it fails. The local family names the local path in `key`
        # and leaves `bucket` unset (design/exceptions.md).
        dest = tmp_path / "occupied"
        dest.mkdir()
        client, _calls = make_recording_client([_get_response(b"payload")])
        with pytest.raises(Boto3S3Error) as exc_info:
            S3Storage("s3://bucket/occupied", client=client).get_file(dest)
        assert exc_info.value.key == str(dest)
        assert exc_info.value.bucket is None
        assert isinstance(exc_info.value.__cause__, OSError)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["occupied"]  # temp file removed

    @pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
    def test_a_symlink_destination_is_replaced_not_followed(self, tmp_path: Path) -> None:
        reference = tmp_path / "reference.bin"
        reference.write_bytes(b"")
        target = tmp_path / "target.txt"
        target.write_bytes(b"target-contents")
        target.chmod(0o600)  # distinctive, so following the link would show up
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        client, _calls = make_recording_client([_get_response(b"downloaded")])

        S3Storage("s3://bucket/obj", client=client).get_file(link)

        assert not link.is_symlink()  # the link itself was replaced
        assert link.read_bytes() == b"downloaded"
        assert target.read_bytes() == b"target-contents"  # its target is untouched
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        # The mode is not followed either: the destination is stat'ed without
        # dereferencing, so a symlink has no bits to preserve and the
        # replacement takes the umask's, not the link target's 0o600.
        assert stat.S_IMODE(link.stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)

    def test_parent_directories_are_created(self, tmp_path: Path) -> None:
        client, _calls = make_recording_client([_get_response(b"deep")])
        dest = tmp_path / "a" / "b" / "c.bin"
        S3Storage("s3://bucket/obj", client=client).get_file(dest)
        assert dest.read_bytes() == b"deep"

    def test_an_empty_object_lands_as_an_empty_file(self, tmp_path: Path) -> None:
        client, calls = make_recording_client([_get_response(b"")])
        dest = tmp_path / "empty.bin"
        info = S3Storage("s3://bucket/empty.bin", client=client).get_file(dest)
        assert dest.read_bytes() == b""
        assert info.size == 0
        assert ops(calls) == ["GetObject"]


class _PutRecordingClient:
    """A client that records each ``PutObject`` and drains the body handed to it.

    The body is read *during* the call, which is the only moment it is open:
    ``put_file`` owns the handle and closes it as it returns.
    """

    def __init__(
        self, response: dict[str, Any] | None = None, error: Exception | None = None
    ) -> None:
        self._response = {"ETag": '"abc"'} if response is None else response
        self._error = error
        self.calls: list[dict[str, Any]] = []
        self.bodies: list[bytes] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        body = kwargs.pop("Body")
        self.calls.append(kwargs)
        self.bodies.append(body.read())
        if self._error is not None:
            raise self._error
        return self._response


class TestGetFileTempName:
    """The sibling temp file's name fits the filesystem's per-name limit in bytes."""

    # "a" is 1 byte and the hiragana 3 in UTF-8, the filesystem encoding the
    # POSIX hosts use; the suffix is the 9-byte ".xxxxxxxx".
    _SUFFIX = ".0a1b2c3d"
    _WIDE = chr(0x3042)

    def test_a_short_name_is_kept_whole(self) -> None:
        from boto3_s3.s3storage import _download_temp_name

        assert _download_temp_name("state.json", self._SUFFIX) == "state.json.0a1b2c3d"

    def test_a_long_ascii_name_is_cut_to_the_limit(self) -> None:
        from boto3_s3.s3storage import _download_temp_name

        name = _download_temp_name("a" * 255, self._SUFFIX)
        assert name == "a" * 246 + self._SUFFIX
        assert len(os.fsencode(name)) == 255

    @pytest.mark.skipif(sys.platform == "win32", reason="the byte width assumed is UTF-8's")
    def test_a_multibyte_name_is_cut_by_bytes_at_a_character_boundary(self) -> None:
        from boto3_s3.s3storage import _download_temp_name

        # 85 characters, 255 bytes: a valid name that a cut by characters
        # leaves whole, making the suffixed name 264 bytes.
        name = _download_temp_name(self._WIDE * 85, self._SUFFIX)
        assert name == self._WIDE * 82 + self._SUFFIX  # 246 bytes of name
        assert len(os.fsencode(name)) == 255
        # One byte over a boundary drops the whole character, not a part of it.
        mixed = _download_temp_name("a" + self._WIDE * 85, self._SUFFIX)
        assert mixed == "a" + self._WIDE * 81 + self._SUFFIX
        assert len(os.fsencode(mixed)) == 253

    def test_downloads_onto_a_valid_name_of_255_multibyte_bytes(self, tmp_path: Path) -> None:
        dest = tmp_path / (self._WIDE * 85)
        try:
            dest.write_bytes(b"old")
        except OSError:
            pytest.skip("this filesystem cannot hold the name at all")
        client, _calls = make_recording_client([_get_response(b"new")])
        S3Storage("s3://bucket/obj", client=client).get_file(dest)
        assert dest.read_bytes() == b"new"
        assert [p.name for p in tmp_path.iterdir()] == [dest.name]


class TestPutFile:
    """``S3Storage.put_file`` - one ``PutObject`` carrying a local file."""

    def test_uploads_the_file_bytes_in_one_call(self, tmp_path: Path) -> None:
        source = tmp_path / "manifest.json"
        source.write_bytes(b"payload-bytes")
        client = _PutRecordingClient()
        storage = S3Storage("s3://bucket/prefix/", client=client)  # type: ignore[arg-type]

        info = storage.put_file(source, key="manifest.json")

        # One request, and nothing shapes the object: no ContentType, so no MIME
        # guessing (that is the transfer lanes' aws parity behavior, not this one's).
        assert client.calls == [{"Bucket": "bucket", "Key": "prefix/manifest.json"}]
        assert client.bodies == [b"payload-bytes"]
        assert isinstance(info, S3FileInfo)
        assert info.key == "prefix/manifest.json"
        assert info.compare_key == "manifest.json"
        assert info.size == 13  # the local file's size
        assert info.etag == '"abc"'  # as the response carried it, quotes included
        assert info.storage is storage
        assert info.head == {"ETag": '"abc"'}

    def test_response_metadata_is_stripped_from_head(self, tmp_path: Path) -> None:
        source = tmp_path / "a.bin"
        source.write_bytes(b"x")
        client = _PutRecordingClient(
            {"ETag": '"e"', "VersionId": "v1", "ResponseMetadata": {"HTTPStatusCode": 200}}
        )
        info = S3Storage("s3://bucket/a.bin", client=client).put_file(source)  # type: ignore[arg-type]
        assert info.head == {"ETag": '"e"', "VersionId": "v1"}

    def test_key_joins_like_get_fileinfo(self, tmp_path: Path) -> None:
        source = tmp_path / "a.bin"
        source.write_bytes(b"x")
        client = _PutRecordingClient()
        # The "/" boundary is inserted under a slashless prefix, and "" stays
        # the storage's own key.
        S3Storage("s3://bucket/prefix", client=client).put_file(source, key="sub/f.txt")  # type: ignore[arg-type]
        S3Storage("s3://bucket/prefix/obj.txt", client=client).put_file(source)  # type: ignore[arg-type]
        assert [call["Key"] for call in client.calls] == ["prefix/sub/f.txt", "prefix/obj.txt"]

    def test_an_empty_file_uploads_as_an_empty_object(self, tmp_path: Path) -> None:
        source = tmp_path / "empty.bin"
        source.write_bytes(b"")
        client = _PutRecordingClient()
        info = S3Storage("s3://bucket/empty.bin", client=client).put_file(source)  # type: ignore[arg-type]
        assert client.bodies == [b""]
        assert info.size == 0

    def test_a_missing_source_raises_not_found_without_a_request(self, tmp_path: Path) -> None:
        client = _PutRecordingClient()
        source = tmp_path / "gone.bin"
        storage = S3Storage("s3://bucket/gone.bin", client=client)  # type: ignore[arg-type]
        with pytest.raises(NotFoundError) as exc_info:
            storage.put_file(source)
        # A locally-originating error names the local path in `key`, with no bucket.
        assert exc_info.value.key == str(source)
        assert exc_info.value.bucket is None
        assert isinstance(exc_info.value.__cause__, FileNotFoundError)
        assert client.calls == []

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
    def test_a_fifo_source_is_refused_without_opening_it(self, tmp_path: Path) -> None:
        # Opening a FIFO blocks until a writer appears, and with one botocore
        # dies sizing the body (`tell()` -> [Errno 29] Illegal seek). Refused
        # before the open, so the call returns at once and sends nothing.
        source = tmp_path / "pipe"
        os.mkfifo(source)
        client = _PutRecordingClient()
        storage = S3Storage("s3://bucket/prefix/", client=client)  # type: ignore[arg-type]
        with pytest.raises(ValidationError, match="FIFO, or socket") as exc_info:
            storage.put_file(source, key="k")
        assert client.calls == []
        error = exc_info.value
        assert (error.operation, error.bucket, error.key) == ("put_file", None, str(source))

    def test_a_put_failure_translates_to_the_taxonomy(self, tmp_path: Path) -> None:
        source = tmp_path / "a.bin"
        source.write_bytes(b"x")
        client = _PutRecordingClient(error=client_error("AccessDenied", 403, "PutObject"))
        storage = S3Storage("s3://bucket/a.bin", client=client)  # type: ignore[arg-type]
        with pytest.raises(AccessDeniedError) as exc_info:
            storage.put_file(source)
        assert exc_info.value.bucket == "bucket"
        assert exc_info.value.key == "a.bin"
        assert isinstance(exc_info.value.__cause__, ClientError)


class TestSingleRequestTransferRoundTrip:
    """``put_file`` then ``get_file`` through the real SDK stack (moto).

    The fakes above stub ``_make_api_call``, so nothing there exercises what
    botocore does with the arguments: that a file handle passed as ``Body`` is
    streamed (and length-computed) rather than rejected, and that the download
    loop reads botocore's real ``StreamingBody``. This runs both against a moto
    backend, over enough bytes to need several read chunks.
    """

    def test_a_file_survives_the_round_trip(self, tmp_path: Path) -> None:
        payload = b'{"seen": 42}\n' * 50_000  # ~650 KB: several 256 KB chunks
        source = tmp_path / "state.json"
        source.write_bytes(payload)
        with mock_aws():
            client = boto3.session.Session().client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="round-trip")
            storage = S3Storage("s3://round-trip/app/", client=client)

            uploaded = storage.put_file(source, key="state.json")
            assert uploaded.key == "app/state.json"
            assert uploaded.size == len(payload)
            # A single-part object's ETag is the MD5 of its bytes, in S3's quotes.
            digest = hashlib.md5(payload, usedforsecurity=False).hexdigest()
            assert uploaded.etag == f'"{digest}"'

            back = tmp_path / "downloaded.json"
            downloaded = storage.get_file(back, key="state.json")

        assert back.read_bytes() == payload
        assert downloaded.size == len(payload)
        assert downloaded.etag == uploaded.etag
        assert downloaded.mtime is not None
        assert sorted(p.name for p in tmp_path.iterdir()) == ["downloaded.json", "state.json"]
