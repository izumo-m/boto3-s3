"""Unit tests for boto3_s3.s3storage.S3Storage.scan (ListObjectsV2 + error mapping).

Uses a hand-rolled fake S3 client/paginator (no moto dependency); the fake
records the kwargs passed to ``paginate`` so delimiter / page-size / request-payer
wiring can be asserted.
"""

from __future__ import annotations

import io
import threading
from typing import Any

import pytest
from botocore.exceptions import ClientError, ProfileNotFound

from boto3_s3 import (
    S3,
    AccessDeniedError,
    Boto3S3Error,
    ConfigurationError,
    FileInfo,
    FileKind,
    InvalidConfigError,
    LocalStorage,
    NotFoundError,
    S3FileInfo,
    S3ScanOptions,
    S3Storage,
    ScanOptions,
    StorageCapability,
    TransportError,
    ValidationError,
)
from boto3_s3.storage import sieve_pages
from tests.utils.fakes3 import MTIME, client_error
from tests.utils.recorder import ApiCall, make_recording_client


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


class _FakeS3Client:
    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        head_response: dict[str, Any] | None = None,
        head_error: Exception | None = None,
    ) -> None:
        self._pages = pages or []
        self._error = error
        self._head_response = head_response
        self._head_error = head_error
        self.calls: list[dict[str, Any]] = []
        self.paginator_names: list[str] = []
        self.head_calls: list[dict[str, Any]] = []

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
) -> tuple[S3Storage, _FakeS3Client]:
    client = _FakeS3Client(
        pages=pages, error=error, head_response=head_response, head_error=head_error
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
        assert info.etag == "abc"  # surrounding quotes stripped
        assert info.storage_class == "STANDARD"
        assert info.owner == "me"
        assert info.storage is storage


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
        assert info.etag == "abc"  # surrounding quotes stripped
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
        # botocore < 1.34.162 has no ListBuckets paginator; list_buckets must fall
        # back to a single list_buckets() call (filters inert) rather than crash
        # with OperationNotPageableError.
        class _NoPaginatorClient:
            def __init__(self) -> None:
                self.list_buckets_calls = 0

            def can_paginate(self, name: str) -> bool:
                return False

            def list_buckets(self, **kwargs: Any) -> dict[str, Any]:
                self.list_buckets_calls += 1
                return {"Buckets": [_bucket_entry("alpha")]}

        client = _NoPaginatorClient()
        storage = S3Storage("s3://", client=client)  # type: ignore[arg-type]
        results = list(storage.list_buckets(name_prefix="al"))
        assert client.list_buckets_calls == 1
        assert [(r.key, r.kind) for r in results] == [("alpha", FileKind.BUCKET)]


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
        # docs/exceptions.md section 3), not the raw botocore error.
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
        # refinement maps to the general 255 (docs/exceptions.md section 3).
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
        # docs/exceptions.md section 3: a code not in the table falls back to
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
        # docs/exceptions.md section 2: KeyboardInterrupt is never wrapped.
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


class _DeleteRecordingClient:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return {}


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
    ``"wb"`` stays unimplemented (S3 writes ride s3transfer)."""

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
