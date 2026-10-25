"""Unit tests for boto3_s3.S3: ls routing, and the client() / resolve() seams."""

from __future__ import annotations

import datetime as dt
import io
import os
import re
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError, ProfileNotFound

from boto3_s3 import (
    S3,
    AccessDeniedError,
    BatchError,
    Boto3S3Error,
    CancelledError,
    CancelToken,
    CrtClientOptions,
    Exclude,
    FileInfo,
    FileKind,
    Include,
    InvalidConfigError,
    IOStorage,
    LocalStorage,
    NotFoundError,
    OpOutcome,
    OpResult,
    S3Storage,
    SyncPair,
    TransferConfig,
    ValidationError,
    globsieve,
)
from tests.utils.fakes3 import client_error, listing
from tests.utils.recorder import make_recording_client

_MTIME = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


class _FakePaginator:
    def __init__(self, pages: list[dict[str, Any]], calls: list[dict[str, Any]]) -> None:
        self._pages = pages
        self._calls = calls

    def paginate(self, **kwargs: Any) -> Any:
        self._calls.append(kwargs)
        return iter(self._pages)


class _FakeS3Client:
    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, Any]] = []

    def can_paginate(self, _name: str) -> bool:
        return True

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator(self._pages, self.calls)


class TestLsRouting:
    def test_pre_cancelled_listing_makes_no_request(self) -> None:
        client = _FakeS3Client([])
        token = CancelToken()
        token.cancel()

        with pytest.raises(CancelledError):
            S3().ls(
                S3Storage("s3://b/p/", client=client),
                on_entry=lambda _info: None,
                cancel_token=token,
            )

        assert client.calls == []

    def test_cancel_from_on_entry_stops_delivery_and_reclaims_prefetch(self) -> None:
        pages = [
            {"Contents": [{"Key": f"p/{i}", "Size": 1, "LastModified": _MTIME}]} for i in range(20)
        ]
        storage = S3Storage("s3://b/p/", client=_FakeS3Client(pages))
        token = CancelToken()
        keys: list[str] = []

        def stop_after_first(info: Any) -> None:
            keys.append(info.key)
            token.cancel()

        with pytest.raises(CancelledError):
            S3().ls(storage, on_entry=stop_after_first, cancel_token=token)

        assert keys == ["p/0"]
        assert not any(
            thread.name == "boto3-s3-prefetch" and thread.is_alive()
            for thread in threading.enumerate()
        )

    def test_cancel_stops_prefetch_before_callback_returns(self) -> None:
        second_started = threading.Event()
        release_second = threading.Event()
        third_started = threading.Event()

        class _ControlledPaginator:
            def paginate(self, **_kwargs: Any) -> Any:
                def pages() -> Any:
                    yield {"Contents": [{"Key": "p/0", "Size": 1, "LastModified": _MTIME}]}
                    second_started.set()
                    assert release_second.wait(5.0)
                    yield {"Contents": [{"Key": "p/1", "Size": 1, "LastModified": _MTIME}]}
                    third_started.set()
                    yield {"Contents": [{"Key": "p/2", "Size": 1, "LastModified": _MTIME}]}

                return pages()

        class _ControlledClient(_FakeS3Client):
            def get_paginator(self, _name: str) -> Any:
                return _ControlledPaginator()

        token = CancelToken()

        def cancel_while_page_is_inflight(_info: Any) -> None:
            assert second_started.wait(5.0)
            token.cancel()
            release_second.set()
            assert not third_started.wait(0.2)

        with pytest.raises(CancelledError):
            S3().ls(
                S3Storage("s3://b/p/", client=_ControlledClient([])),
                on_entry=cancel_while_page_is_inflight,
                cancel_token=token,
            )

    def test_s3storage_target_delegates_to_scan(self) -> None:
        client = _FakeS3Client([{"Contents": [{"Key": "p/a", "Size": 1, "LastModified": _MTIME}]}])
        storage = S3Storage("s3://b/p/", client=client)
        infos: list[Any] = []
        S3().ls(storage, on_entry=infos.append)
        assert [info.key for info in infos] == ["p/a"]

    def test_scheme_optional_storage_delegates_to_scan(self) -> None:
        # S3Storage accepts an s3://-less "bucket/key"; ls drives its scan the same.
        client = _FakeS3Client([{"Contents": [{"Key": "p/a", "Size": 1, "LastModified": _MTIME}]}])
        storage = S3Storage("b/p/", client=client)  # no s3:// scheme
        infos: list[Any] = []
        S3().ls(storage, on_entry=infos.append)
        assert [info.key for info in infos] == ["p/a"]

    def test_non_s3_location_raises_eagerly(self) -> None:
        # ls() is a regular method (not a generator): a non-S3 target raises on
        # the call itself, before any iteration.
        with pytest.raises(ValidationError):
            S3().ls(LocalStorage("/tmp/x"), on_entry=lambda _info: None)

    def test_service_root_storage_lists_buckets(self) -> None:
        client = _FakeS3Client([{"Buckets": [{"Name": "alpha", "CreationDate": _MTIME}]}])
        storage = S3Storage("s3://", client=client)
        infos: list[Any] = []
        S3().ls(storage, on_entry=infos.append)
        assert [(i.key, i.kind) for i in infos] == [("alpha", FileKind.BUCKET)]

    def test_bucket_filters_reach_the_scan(self) -> None:
        client = _FakeS3Client([])
        storage = S3Storage("s3://", client=client)
        S3().ls(
            storage,
            on_entry=lambda _info: None,
            bucket_name_prefix="al",
            bucket_region="us-east-1",
        )
        assert client.calls[0]["Prefix"] == "al"
        assert client.calls[0]["BucketRegion"] == "us-east-1"

    def test_key_without_bucket_raises_eagerly(self) -> None:
        with pytest.raises(ValidationError):
            S3().ls("s3:///key", on_entry=lambda _info: None)


def _object(key: str) -> dict[str, Any]:
    return {"Key": key, "Size": 1, "LastModified": _MTIME}


def _bucket(name: str) -> dict[str, Any]:
    return {"Name": name, "CreationDate": _MTIME}


class TestLsFilter:
    """``filter`` is offered every entry ``on_entry`` would receive, on both listings."""

    def test_recursive_listing_keeps_what_the_glob_keeps(self) -> None:
        client = _FakeS3Client(
            [{"Contents": [_object("p/a.log"), _object("p/b.txt"), _object("p/sub/c.log")]}]
        )
        keep = globsieve.compile([Exclude("*"), Include("*.log")])
        infos: list[FileInfo] = []
        S3().ls(
            S3Storage("s3://b/p/", client=client),
            recursive=True,
            filter=keep,
            on_entry=infos.append,
        )
        assert [info.key for info in infos] == ["p/a.log", "p/sub/c.log"]

    def test_directory_entries_are_offered_too(self) -> None:
        # "tmp/*" also matches the common prefix "tmp/" ('*' matches nothing).
        client = _FakeS3Client(
            [
                {
                    "CommonPrefixes": [{"Prefix": "p/keep/"}, {"Prefix": "p/tmp/"}],
                    "Contents": [_object("p/tmp.txt")],
                }
            ]
        )
        infos: list[FileInfo] = []
        S3().ls(
            S3Storage("s3://b/p/", client=client),
            filter=globsieve.compile([Exclude("tmp/*")]),
            on_entry=infos.append,
        )
        assert [(info.key, info.kind) for info in infos] == [
            ("p/keep/", FileKind.DIRECTORY),
            ("p/tmp.txt", FileKind.FILE),
        ]

    def test_the_filter_sees_the_compare_key_on_entry_gets(self) -> None:
        # ls takes its key verbatim as the Prefix (no "/" appended, unlike a
        # recursive rm), and the filter is shown that listing's own entries.
        client = _FakeS3Client([{"Contents": [_object("p/a"), _object("p-x")]}])
        offered: list[str | None] = []

        def record(info: FileInfo) -> bool:
            offered.append(info.compare_key)
            return True

        delivered: list[FileInfo] = []
        S3().ls(
            S3Storage("s3://b/p", client=client),
            recursive=True,
            filter=record,
            on_entry=delivered.append,
        )
        assert offered == ["/a", "-x"]
        assert [info.compare_key for info in delivered] == offered

    def test_bucket_listing_is_filtered_by_name(self) -> None:
        client = _FakeS3Client([{"Buckets": [_bucket("prod-a"), _bucket("dev-b")]}])
        infos: list[FileInfo] = []
        S3().ls(
            S3Storage("s3://", client=client),
            filter=globsieve.compile([Exclude("*"), Include("prod-*")]),
            on_entry=infos.append,
        )
        assert [(info.key, info.kind) for info in infos] == [("prod-a", FileKind.BUCKET)]

    @pytest.mark.parametrize(
        ("target", "page", "on_caller"),
        [
            ("s3://b/p/", {"Contents": [_object("p/a")]}, False),
            ("s3://", {"Buckets": [_bucket("alpha")]}, True),
        ],
        ids=["objects-on-prefetch-worker", "buckets-on-calling-thread"],
    )
    def test_where_the_filter_runs(
        self, target: str, page: dict[str, Any], on_caller: bool
    ) -> None:
        threads: list[threading.Thread] = []

        def record(_info: FileInfo) -> bool:
            threads.append(threading.current_thread())
            return True

        S3().ls(
            S3Storage(target, client=_FakeS3Client([page])),
            filter=record,
            on_entry=lambda _info: None,
        )
        assert len(threads) == 1
        assert (threads[0] is threading.current_thread()) is on_caller

    @pytest.mark.parametrize(
        ("target", "page"),
        [
            ("s3://b/p/", {"Contents": [_object("p/a")]}),
            ("s3://", {"Buckets": [_bucket("alpha")]}),
        ],
        ids=["objects", "buckets"],
    )
    def test_an_unnamed_error_from_the_filter_is_attributed_to_ls(
        self, target: str, page: dict[str, Any]
    ) -> None:
        error = NotFoundError("the predicate's own lookup failed")

        def refuse(_info: FileInfo) -> bool:
            raise error

        delivered: list[FileInfo] = []
        with pytest.raises(NotFoundError) as exc_info:
            S3().ls(
                S3Storage(target, client=_FakeS3Client([page])),
                filter=refuse,
                on_entry=delivered.append,
            )
        assert exc_info.value is error
        assert error.operation == "ls"
        assert delivered == []

    @pytest.mark.parametrize(
        ("target", "page"),
        [
            ("s3://b/p/", {"Contents": [_object("p/a")]}),
            ("s3://", {"Buckets": [_bucket("alpha")]}),
        ],
        ids=["objects", "buckets"],
    )
    def test_a_foreign_error_from_the_filter_passes_unchanged(
        self, target: str, page: dict[str, Any]
    ) -> None:
        error = ValueError("not the library's")

        def refuse(_info: FileInfo) -> bool:
            raise error

        with pytest.raises(ValueError) as exc_info:
            S3().ls(
                S3Storage(target, client=_FakeS3Client([page])),
                filter=refuse,
                on_entry=lambda _info: None,
            )
        assert exc_info.value is error

    def test_an_absolute_pattern_never_matches_a_bucket(self) -> None:
        # A bucket entry has no bucket/key path for a full-path pattern to match.
        client = _FakeS3Client([{"Buckets": [_bucket("alpha"), _bucket("beta")]}])
        infos: list[FileInfo] = []
        S3().ls(
            S3Storage("s3://", client=client),
            filter=globsieve.compile([Exclude("/*")]),
            on_entry=infos.append,
        )
        assert [info.key for info in infos] == ["alpha", "beta"]

    def test_a_cancel_from_on_entry_stops_a_filtered_object_listing(self) -> None:
        # The filter rejects every page after the first; the listing must
        # still stop at a page boundary rather than run to its end.
        released = threading.Event()
        pulled: list[int] = []

        class _Paginator:
            def paginate(self, **_kwargs: Any) -> Any:
                def pages() -> Any:
                    for i in range(200):
                        pulled.append(i)
                        if i == 2:
                            assert released.wait(5.0)
                        yield {"Contents": [_object(f"p/{i:03}")]}

                return pages()

        class _Client(_FakeS3Client):
            def get_paginator(self, _name: str) -> Any:
                return _Paginator()

        token = CancelToken()

        def stop(_info: FileInfo) -> None:
            token.cancel()
            released.set()

        with pytest.raises(CancelledError):
            S3().ls(
                S3Storage("s3://b/p/", client=_Client([])),
                recursive=True,
                filter=lambda info: info.key == "p/000",
                on_entry=stop,
                cancel_token=token,
            )
        assert len(pulled) <= 3

    def test_a_cancel_stops_a_filtered_bucket_listing_between_entries(self) -> None:
        pulled: list[int] = []

        def pages() -> Any:
            for i in range(200):
                pulled.append(i)
                yield {"Buckets": [_bucket(f"b{i:03}")]}

        token = CancelToken()

        def reject_and_cancel(info: FileInfo) -> bool:
            if info.key == "b001":
                token.cancel()
            return False

        with pytest.raises(CancelledError):
            S3().ls(
                S3Storage("s3://", client=_FakeS3Client(cast("list[dict[str, Any]]", pages()))),
                filter=reject_and_cancel,
                on_entry=lambda _info: None,
                cancel_token=token,
            )
        assert len(pulled) == 2


class TestUnknownTransferOption:
    """cp / mv / sync reject a typo'd ``**options`` key eagerly (pre-pipeline).

    Unpack[TransferOptions] covers type-checked callers only; without the
    runtime check a dry_run (for dryrun) typo would be silently ignored -
    and on mv still delete the source.
    """

    @pytest.mark.parametrize("op", ["cp", "mv", "sync"])
    def test_unknown_key_raises_before_any_work(self, op: str) -> None:
        # "Before any work" observed directly: resolving either path would
        # build a client, so an S3 whose client() explodes proves the option
        # validation really runs first.
        class NoClientS3(S3):
            def client(self) -> Any:
                raise AssertionError("client built before option validation")

        method = getattr(NoClientS3(), op)
        with pytest.raises(ValidationError, match=r"Unknown transfer option\(s\): dry_run") as ei:
            method("src-path", "dest-path", dry_run=True)  # pyright: ignore[reportCallIssue]
        assert type(ei.value) is ValidationError


class TestMalformedGrants:
    """cp / mv / sync refuse a malformed ``grants`` before any work.

    aws-cli finds it only as it maps the first upload or copy, after a sync's
    deletes may have gone out, or never when nothing needs transferring; here
    it is a bad argument, refused like one on every route (a download's
    included, which maps no grants).
    """

    @pytest.mark.parametrize("op", ["cp", "mv", "sync"])
    @pytest.mark.parametrize(
        ("grant", "message"),
        [
            ("foo", "grants should be of the form permission=principal"),
            ("bogus=id=x", "permission must be one of: read|readacl|writeacl|full"),
        ],
    )
    @pytest.mark.parametrize(
        ("src", "dest"), [("local-dir", "s3://b/p/"), ("s3://b/p/", "local-dir")]
    )
    def test_raises_before_any_work(
        self, op: str, grant: str, message: str, src: str, dest: str
    ) -> None:
        class NoClientS3(S3):
            def client(self) -> Any:
                raise AssertionError("client built before option validation")

        method = getattr(NoClientS3(), op)
        recursive = {} if op == "sync" else {"recursive": True}
        with pytest.raises(ValidationError, match=re.escape(message)) as ei:
            method(src, dest, grants=["read=id=ok", grant], **recursive)
        assert type(ei.value) is ValidationError
        assert ei.value.operation == op

    def test_well_formed_grants_pass(self, tmp_path: Any) -> None:
        # The check itself refuses nothing well formed: a dry run of a local
        # upload with every permission spelled goes through to its record.
        (tmp_path / "a.txt").write_bytes(b"x")
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        S3().cp(
            str(tmp_path / "a.txt"),
            S3Storage("s3://b/a.txt", client=client),
            dryrun=True,
            grants=["read=id=a", "full=id=b", "readacl=id=c", "writeacl=id=d"],
            on_result=results.append,
        )
        assert calls == []
        assert [result.outcome for result in results] == [OpOutcome.DRYRUN]


class TestValidationOperationAttribution:
    """A location the storage rejects names the operation that ran the check.

    ``Storage.validate`` sees the location but not its caller, so the operation
    layer stamps its own name; ``operation=None`` stays reserved for a check the
    caller ran itself (docs/reference/exceptions.md).
    """

    @pytest.mark.parametrize(
        ("operation", "call"),
        [
            ("ls", lambda s3, _dest: s3.ls("s3:///key", on_entry=lambda _info: None)),
            ("rm", lambda s3, _dest: s3.rm("s3:///key")),
            ("cp", lambda s3, dest: s3.cp("s3:///key", dest)),
            ("mv", lambda s3, dest: s3.mv("s3:///key", dest)),
            ("sync", lambda s3, dest: s3.sync("s3:///key", dest)),
        ],
    )
    def test_each_entry_path_stamps_its_own_name(
        self, operation: str, call: Callable[[S3, str], None], tmp_path: Any
    ) -> None:
        # One case per entry path: the single-target resolver (ls / rm), the
        # shared cp/mv pipeline, and sync. "s3:///key" is the cheapest reject.
        with pytest.raises(ValidationError) as exc_info:
            call(S3(), str(tmp_path / "dest"))
        assert exc_info.value.operation == operation
        assert exc_info.value.key == "key"

    @pytest.mark.parametrize(
        ("operation", "call"),
        [
            ("cp", lambda s3, src: s3.cp(src, "s3:///key")),
            ("mv", lambda s3, src: s3.mv(src, "s3:///key")),
            ("sync", lambda s3, src: s3.sync(src, "s3:///key")),
        ],
    )
    def test_the_destination_side_is_stamped_too(
        self, operation: str, call: Callable[[S3, str], None], tmp_path: Any
    ) -> None:
        # Both sides of a transfer are attributed, not just the source: a local
        # source validates as a no-op, so only the destination's check can raise
        # here.
        src = tmp_path / "src"
        src.mkdir()
        with pytest.raises(ValidationError) as exc_info:
            call(S3(), str(src))
        assert exc_info.value.operation == operation
        assert exc_info.value.key == "key"

    def test_a_non_validation_failure_is_stamped_too(self) -> None:
        # A custom backend's validate() may raise any family member, so the
        # attribution is not ValidationError-specific.
        class _Failing(S3Storage):
            def validate(self) -> None:
                raise NotFoundError("the backend location is gone")

        with pytest.raises(NotFoundError) as exc_info:
            S3().ls(_Failing("s3://b/k"), on_entry=lambda _info: None)
        assert exc_info.value.operation == "ls"

    def test_an_operation_the_backend_named_is_kept(self) -> None:
        class _Failing(S3Storage):
            def validate(self) -> None:
                raise ValidationError("the backend said no", operation="backend-op")

        with pytest.raises(ValidationError) as exc_info:
            S3().ls(_Failing("s3://b/k"), on_entry=lambda _info: None)
        assert exc_info.value.operation == "backend-op"

    def test_the_original_error_and_its_cause_survive(self) -> None:
        # The stamping re-raises the same object, so a backend's own chaining
        # is not flattened into a new exception.
        cause = RuntimeError("underneath")
        error = ValidationError("the backend said no")

        class _Failing(S3Storage):
            def validate(self) -> None:
                raise error from cause

        with pytest.raises(ValidationError) as exc_info:
            S3().ls(_Failing("s3://b/k"), on_entry=lambda _info: None)
        assert exc_info.value is error
        assert exc_info.value.__cause__ is cause


class TestStorageMethodAttribution:
    """A storage's ``open`` / ``delete`` raise unnamed; the operation names them.

    The storage cannot know which operation is using it (design/exceptions.md),
    so the same delete failure reads the operation's name whichever path
    reached it, and ``None`` on a direct call - the rule ``validate`` already
    follows above.
    """

    def test_a_direct_delete_leaves_operation_unset(self, tmp_path: Any) -> None:
        client, _calls = make_recording_client([client_error("AccessDenied", 403, "DeleteObject")])
        with pytest.raises(Boto3S3Error) as exc_info:
            S3Storage("s3://b/p/k", client=client).delete(FileInfo(key="p/k"))
        assert (exc_info.value.operation, exc_info.value.bucket, exc_info.value.key) == (
            None,
            "b",
            "p/k",
        )
        with pytest.raises(NotFoundError) as local_info:
            LocalStorage(str(tmp_path)).delete(
                FileInfo(key=f"{tmp_path}/missing".replace(os.sep, "/"))
            )
        assert local_info.value.operation is None

    def test_rms_single_key_path_names_rm(self) -> None:
        # The batched path (S3Deleter) already stamps "rm"; the blind
        # single-key path goes through S3Storage.delete and must agree.
        client, _calls = make_recording_client([client_error("AccessDenied", 403, "DeleteObject")])
        results: list[OpResult] = []
        with pytest.raises(BatchError) as exc_info:
            S3().rm(S3Storage("s3://b/p/k", client=client), on_result=results.append)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert error is not None
        assert (error.operation, error.bucket, error.key) == ("rm", "b", "p/k")
        assert exc_info.value.__cause__ is error

    def test_syncs_local_orphan_delete_names_sync(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "orphan.txt").write_bytes(b"o")

        def refuse(path: Any, *args: Any, **kwargs: Any) -> None:
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(os, "remove", refuse)
        client, _calls = make_recording_client([listing(("p/a.txt", 1))])
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            S3().sync(
                S3Storage("s3://b/p/", client=client),
                str(dest),
                create_filter=False,
                delete_filter=True,
                on_result=results.append,
            )
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert error is not None
        assert (error.operation, error.bucket, error.key) == (
            "sync",
            None,
            str(dest / "orphan.txt").replace(os.sep, "/"),
        )

    def test_a_decision_raising_unnamed_is_attributed_to_sync(self, tmp_path: Any) -> None:
        # A content strategy opens one side through Storage.open, which raises
        # unnamed; the failure aborts the run attributed to sync.
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "a.txt").write_bytes(b"a")
        error = Boto3S3Error("content compare failed", key="p/a.txt")

        def compare(_pair: SyncPair) -> bool:
            raise error

        client, _calls = make_recording_client([listing(("p/a.txt", 1))])
        with pytest.raises(Boto3S3Error) as exc_info:
            S3().sync(S3Storage("s3://b/p/", client=client), str(dest), update_filter=compare)
        assert exc_info.value is error
        assert error.operation == "sync"


def _verifying(verify: Any) -> SimpleNamespace:
    """A client stand-in carrying *verify* where botocore keeps it."""
    return SimpleNamespace(_endpoint=SimpleNamespace(http_session=SimpleNamespace(_verify=verify)))


class TestClientSeam:
    """``S3.client`` builds a fresh client from session / endpoint_url / config."""

    def test_endpoint_url_applied(self) -> None:
        client = S3(endpoint_url="https://minio.example:9000").client()
        assert client.meta.endpoint_url == "https://minio.example:9000"

    def test_config_region_applied(self) -> None:
        client = S3(config=Config(region_name="eu-west-1")).client()
        assert client.meta.region_name == "eu-west-1"

    def test_session_used(self) -> None:
        session = boto3.Session(region_name="ap-northeast-1")
        assert S3(session=session).client().meta.region_name == "ap-northeast-1"

    def test_session_is_the_instance_default(self) -> None:
        session = boto3.Session(region_name="ap-northeast-1")
        assert S3(session=session).session is session

    def test_fresh_client_each_call(self) -> None:
        # Documented contract: a fresh client per call, owned by the caller.
        s3 = S3()
        assert s3.client() is not s3.client()

    def test_build_failure_maps_to_invalid_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A build failure - e.g. AWS_PROFILE naming a missing profile - raises
        # the translated error (design/exceptions.md section 3), not the raw
        # botocore one. ProfileNotFound refines to InvalidConfigError, not the
        # plain ConfigurationError base; pin the exact type because the CLI
        # maps this refinement to rc 255 (vs 253 for the base).
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise ProfileNotFound(profile="missing-profile")

        monkeypatch.setattr(boto3, "client", boom)
        with pytest.raises(InvalidConfigError) as exc_info:
            S3().client()
        assert type(exc_info.value) is InvalidConfigError
        assert isinstance(exc_info.value.__cause__, ProfileNotFound)

    def test_session_build_failure_maps_to_invalid_config_error(self) -> None:
        # The session branch of client() is wrapped like the default-session
        # branch: a session whose client build fails raises the same translated
        # InvalidConfigError refinement, not the raw botocore error.
        class _BrokenSession:
            def client(self, *args: Any, **kwargs: Any) -> Any:
                raise ProfileNotFound(profile="missing-profile")

        with pytest.raises(InvalidConfigError) as exc_info:
            S3(session=cast("Any", _BrokenSession())).client()
        assert type(exc_info.value) is InvalidConfigError
        assert isinstance(exc_info.value.__cause__, ProfileNotFound)

    @pytest.mark.parametrize("bundle", ["", "   "], ids=["empty", "whitespace"])
    def test_an_empty_ca_bundle_is_refused(
        self, bundle: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An older botocore reads an empty CA bundle as "verification off" and
        # builds the client; 1.43.54 refuses it while building. Either way
        # the library answers botocore's own refusal, as the InvalidConfigError
        # refinement, from both build paths.
        from botocore.exceptions import InvalidConfigError as BotocoreInvalidConfigError

        monkeypatch.setenv("AWS_CA_BUNDLE", bundle)
        for build in (lambda: S3().client(), lambda: S3Storage("s3://b/").get_client()):
            with pytest.raises(InvalidConfigError, match="Invalid CA bundle") as exc_info:
                build()
            assert type(exc_info.value) is InvalidConfigError
            assert isinstance(exc_info.value.__cause__, BotocoreInvalidConfigError)

    @pytest.mark.parametrize("bundle", ["", "   "], ids=["empty", "whitespace"])
    def test_a_supplied_client_with_an_empty_ca_bundle_is_refused(self, bundle: str) -> None:
        # A client an older botocore built with an empty CA bundle never
        # carries a request: every operation takes its client from here.
        client = SimpleNamespace(
            _endpoint=SimpleNamespace(http_session=SimpleNamespace(_verify=bundle))
        )
        storage = S3Storage("s3://b/", client=cast("Any", client))
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle"):
            storage.get_client()

    @pytest.mark.parametrize("bundle", ["", "   "], ids=["empty", "whitespace"])
    def test_an_empty_aws_ca_bundle_is_refused_beside_a_handed_over_client(
        self, bundle: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The client carries a real path, but the clients botocore builds
        # behind it for an assumed role or SSO resolve AWS_CA_BUNDLE first.
        monkeypatch.setenv("AWS_CA_BUNDLE", bundle)
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle"):
            S3Storage("s3://b/", client=cast("Any", _verifying("/etc/ca.pem"))).get_client()

    def test_an_empty_requests_ca_bundle_is_not_guessed_at(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Whether the credential clients read REQUESTS_CA_BUNDLE depends on the
        # session's profile, which a handed-over client does not name.
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "")
        client = _verifying("/etc/ca.pem")
        assert S3Storage("s3://b/", client=cast("Any", client)).get_client() is client

    def test_a_client_the_cli_vetted_skips_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # boto3-s3-cli checks every client its sessions build where aws-cli does;
        # a second look at the environment would refuse runs aws-cli completes.
        from boto3_s3.s3storage import CA_BUNDLE_ENVIRONMENT_VETTED

        monkeypatch.setenv("AWS_CA_BUNDLE", "")
        client = _verifying("/etc/ca.pem")
        setattr(client, CA_BUNDLE_ENVIRONMENT_VETTED, True)
        assert S3Storage("s3://b/", client=cast("Any", client)).get_client() is client

    def test_a_client_s3_built_is_not_second_guessed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A session whose ca_bundle was set in code outranks AWS_CA_BUNDLE for
        # the S3 client and its credential clients alike, so a string-URI
        # operation handing S3.client()'s client to S3Storage must not refuse
        # it over the (unread) empty variable.
        import certifi

        session = boto3.Session(region_name="us-east-1")
        session._session.set_config_variable("ca_bundle", certifi.where())  # pyright: ignore[reportPrivateUsage]
        monkeypatch.setenv("AWS_CA_BUNDLE", "")
        storage = S3(session=session).resolve("s3://b/")
        assert isinstance(storage, S3Storage)
        storage.get_client()

    @pytest.mark.parametrize("verify", [True, False, "/etc/ca.pem"], ids=["default", "off", "path"])
    def test_a_usable_verify_setting_passes(self, verify: Any) -> None:
        client = SimpleNamespace(
            _endpoint=SimpleNamespace(http_session=SimpleNamespace(_verify=verify))
        )
        assert S3Storage("s3://b/", client=cast("Any", client)).get_client() is client

    def test_malformed_endpoint_maps_to_invalid_config_error(self) -> None:
        # botocore rejects a malformed endpoint_url with a plain ValueError,
        # which is not a BotoCoreError and used to leak raw through the
        # translator - a break of client()'s "never the raw botocore error"
        # contract. Pin the exact refinement type (rc 255 lane).
        with pytest.raises(InvalidConfigError) as exc_info:
            S3(endpoint_url="not-a-url").client()
        assert type(exc_info.value) is InvalidConfigError
        assert isinstance(exc_info.value.__cause__, ValueError)

    def test_a_config_value_of_the_wrong_shape_maps_to_invalid_config_error(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `s3 = foo` is a scalar where botocore reads a section: its
        # `.get(...)` on the string raises AttributeError mid-build.
        config = tmp_path / "config"
        config.write_text("[default]\nregion = us-east-1\ns3 = foo\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        for build in (lambda: S3().client(), lambda: S3Storage("s3://b/").get_client()):
            with pytest.raises(InvalidConfigError) as exc_info:
                build()
            assert isinstance(exc_info.value.__cause__, AttributeError)

    def test_a_credential_process_that_cannot_run_maps_to_invalid_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # botocore runs credential_process while it builds the client, and a
        # missing executable surfaces as subprocess's own FileNotFoundError.
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError(2, "No such file or directory", "/no/such-helper")

        monkeypatch.setattr(boto3, "client", boom)
        for build in (lambda: S3().client(), lambda: S3Storage("s3://b/").get_client()):
            with pytest.raises(InvalidConfigError) as exc_info:
                build()
            assert isinstance(exc_info.value.__cause__, FileNotFoundError)

    @pytest.mark.parametrize(
        ("raised", "category"),
        [
            (OSError(8, "Exec format error"), "InvalidConfigError"),
            (IsADirectoryError(21, "Is a directory", "/helper"), "InvalidConfigError"),
            (OSError(24, "Too many open files", "/botocore/data/endpoints.json"), "TransportError"),
        ],
    )
    def test_a_build_time_os_error_is_configuration_only_for_a_configured_path(
        self, monkeypatch: pytest.MonkeyPatch, raised: OSError, category: str
    ) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise raised

        monkeypatch.setattr(boto3, "client", boom)
        with pytest.raises(Boto3S3Error) as exc_info:
            S3().client()
        assert type(exc_info.value).__name__ == category
        assert exc_info.value.__cause__ is raised

    def test_a_caller_s_wrong_typed_argument_passes_through(self) -> None:
        # A programming error in the arguments is not the configuration's
        # (design/exceptions.md section 2): it propagates as is.
        with pytest.raises(AttributeError):
            S3(session=cast("Any", "x")).client()
        with pytest.raises(AttributeError):
            S3(config=cast("Any", "x")).client()


class TestResolveSeam:
    """``S3.resolve`` injects ``client()`` into bare ``s3://`` strings and is overridable."""

    def test_s3_string_carries_instance_client(self) -> None:
        sentinel = _FakeS3Client([])

        class _SentinelS3(S3):
            def client(self) -> Any:
                return sentinel

        storage = _SentinelS3().resolve("s3://bucket/key")
        assert isinstance(storage, S3Storage)
        assert storage.get_client() is sentinel

    def test_override_adds_scheme_and_defers_to_super(self) -> None:
        class _SchemeS3(S3):
            def resolve(self, loc: Any) -> Any:
                if isinstance(loc, str) and loc.startswith("mem://"):
                    return LocalStorage(loc.removeprefix("mem://"))
                return super().resolve(loc)

        s3 = _SchemeS3()
        assert isinstance(s3.resolve("mem://x"), LocalStorage)  # custom scheme
        assert isinstance(s3.resolve("s3://b/k"), S3Storage)  # deferred to super()

    def test_s3_only_ops_accept_pathlike_targets(self) -> None:
        # ls/rm/mb/rb/presign/website type their target as Location (which
        # includes os.PathLike); _resolve_s3_target must fspath a PathLike,
        # mirroring resolve(). A non-S3 Storage (no __fspath__) still raises.
        sentinel = _FakeS3Client([])

        class _SentinelS3(S3):
            def client(self) -> Any:
                return sentinel

        class _P(os.PathLike):  # type: ignore[type-arg]
            def __fspath__(self) -> str:
                return "s3://my-bucket/key"

        storage = _SentinelS3()._resolve_s3_target(_P(), operation="ls")
        assert isinstance(storage, S3Storage)
        assert (storage.bucket, storage.key) == ("my-bucket", "key")
        with pytest.raises(ValidationError):
            _SentinelS3()._resolve_s3_target(LocalStorage("/tmp/x"), operation="ls")


class _StopTransferError(Exception):
    """Raised by the Transferrer spy to short-circuit before any transfer runs."""


@pytest.fixture
def _captured_transferrer_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the keyword arguments a transfer would build its Transferrer with.

    Patches ``Transferrer.__init__`` to record them and raise, so the test
    stops at construction - before the plan submits anything to S3.
    """
    import boto3_s3.s3 as s3mod

    captured: dict[str, Any] = {}

    def spy(_self: Any, _kind: Any, _client: Any, **kwargs: Any) -> None:
        captured.clear()
        captured.update(kwargs)
        raise _StopTransferError

    monkeypatch.setattr(s3mod.Transferrer, "__init__", spy)
    return captured


class TestTransferConfigDefault:
    """cp / mv / sync fall back to the instance ``transfer_config``; a per-call value wins.

    The transferrer receives the resolved config (`S3.resolve_transfer_config`),
    which carries the given settings over.
    """

    def test_instance_default_reaches_the_transferrer(
        self, _captured_transferrer_kwargs: dict[str, Any], tmp_path: Any
    ) -> None:
        src = tmp_path / "x.txt"
        src.write_text("hi")
        marker = TransferConfig(max_concurrency=3)
        with pytest.raises(_StopTransferError):
            S3(transfer_config=marker).cp(str(src), "s3://bucket/key")
        assert _captured_transferrer_kwargs["transfer_config"].max_concurrency == 3

    def test_per_call_overrides_instance_default(
        self, _captured_transferrer_kwargs: dict[str, Any], tmp_path: Any
    ) -> None:
        src = tmp_path / "x.txt"
        src.write_text("hi")
        instance_tc, call_tc = TransferConfig(max_concurrency=3), TransferConfig(max_concurrency=5)
        with pytest.raises(_StopTransferError):
            S3(transfer_config=instance_tc).cp(str(src), "s3://bucket/key", transfer_config=call_tc)
        assert _captured_transferrer_kwargs["transfer_config"].max_concurrency == 5


class TestCrtClientOptionsPosture:
    """``S3(crt=...)`` reaches every route that builds a Transferrer.

    The options are what keep ``boto3-s3-cli``'s ``--no-sign-request``
    anonymous on the CRT lane even where ``--sse aws:kms`` restores signing on
    the client (design/crt.md section 4), and its CRT lane on aws-cli's trust
    source; a route that forgot to thread them would sign that route's
    transfers where ``aws s3`` sends them unsigned.
    """

    def _run(self, s3: S3, tmp_path: Any, route: str) -> None:
        src = tmp_path / "x.txt"
        src.write_text("hi")
        if route == "cp":
            s3.cp(str(src), "s3://bucket/key")
        elif route == "stream":
            s3.cp(IOStorage(io.BytesIO(b"hi")), "s3://bucket/key")
        else:
            s3.sync(str(tmp_path), "s3://bucket/pfx")

    @pytest.mark.parametrize("route", ["cp", "stream", "sync"])
    @pytest.mark.parametrize(
        "declared",
        [None, CrtClientOptions(verify="/ca.pem")],
        ids=["default", "declared"],
    )
    def test_posture_reaches_every_route(
        self,
        _captured_transferrer_kwargs: dict[str, Any],
        tmp_path: Any,
        route: str,
        declared: CrtClientOptions | None,
    ) -> None:
        s3 = S3(crt=declared)
        with pytest.raises(_StopTransferError):
            self._run(s3, tmp_path, route)
        expected = declared if declared is not None else CrtClientOptions()
        assert _captured_transferrer_kwargs["crt"].crt == expected


class TestMaterializeCrtEngine:
    """`S3.materialize_crt_engine` hands the instance's postures to crtsupport.

    The seam ``rm`` uses to pay ``aws s3``'s transfer-manager construction
    without transferring; the postures it forwards are what make that
    construction fail where aws-cli's does.
    """

    @pytest.fixture(autouse=True)
    def _usable_crt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An explicit 'crt' resolves only with a usable stack, and claims the
        # process's CRT slot - kept out of the test process.
        from boto3_s3 import crtsupport

        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: True)
        monkeypatch.setattr(crtsupport, "has_crt_s3transfer", lambda: True)
        monkeypatch.setattr(crtsupport, "acquire_process_lock", lambda: True)

    def test_the_instance_postures_are_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from boto3_s3 import crtsupport

        seen: list[dict[str, Any]] = []

        def fake(client: Any, config: Any, **kwargs: Any) -> None:
            seen.append({"client": client, "config": config, **kwargs})

        monkeypatch.setattr(crtsupport, "materialize_crt_engine", fake)
        endpoint = "https://minio.example.com"
        client = SimpleNamespace(meta=SimpleNamespace(endpoint_url=endpoint))
        config = TransferConfig(preferred_transfer_client="crt")
        session = boto3.Session()
        s3 = S3(
            session=session,
            endpoint_url=endpoint,
            crt=CrtClientOptions(allow_lockless=True),
        )
        s3.materialize_crt_engine(client, transfer_config=config)  # pyright: ignore[reportArgumentType]
        assert [entry.pop("config").preferred_transfer_client for entry in seen] == ["crt"]
        assert seen == [
            {
                "client": client,
                "endpoint": endpoint,
                "session": session,
                "crt": CrtClientOptions(allow_lockless=True),
            }
        ]

    @pytest.mark.parametrize("bundle", ["", "   "], ids=["empty", "whitespace"])
    def test_an_empty_ca_bundle_is_refused_as_invalid_config(self, bundle: str) -> None:
        # A client handed straight to the CRT seam, past get_client: the
        # engine refuses it before the lock, and the seam refines botocore's
        # error to the library's.
        from botocore.exceptions import InvalidConfigError as BotocoreInvalidConfigError

        pytest.importorskip("awscrt")
        client = SimpleNamespace(
            meta=SimpleNamespace(endpoint_url=None),
            _endpoint=SimpleNamespace(http_session=SimpleNamespace(_verify=bundle)),
        )
        config = TransferConfig(preferred_transfer_client="crt")
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle") as exc_info:
            S3().materialize_crt_engine(client, transfer_config=config)  # pyright: ignore[reportArgumentType]
        assert type(exc_info.value) is InvalidConfigError
        assert isinstance(exc_info.value.__cause__, BotocoreInvalidConfigError)

    def test_an_empty_aws_ca_bundle_is_refused_beside_the_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pytest.importorskip("awscrt")
        monkeypatch.setenv("AWS_CA_BUNDLE", "")
        client = SimpleNamespace(
            meta=SimpleNamespace(endpoint_url=None),
            _endpoint=SimpleNamespace(http_session=SimpleNamespace(_verify="/etc/ca.pem")),
        )
        config = TransferConfig(preferred_transfer_client="crt")
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle"):
            S3().materialize_crt_engine(client, transfer_config=config)  # pyright: ignore[reportArgumentType]

    def test_the_instance_transfer_config_is_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from boto3_s3 import crtsupport

        seen: list[Any] = []
        monkeypatch.setattr(
            crtsupport,
            "materialize_crt_engine",
            lambda _client, config, **_kwargs: seen.append(config),
        )
        config = TransferConfig(preferred_transfer_client="crt")
        client = SimpleNamespace(meta=SimpleNamespace(endpoint_url=None))
        S3(transfer_config=config).materialize_crt_engine(client)  # pyright: ignore[reportArgumentType]
        assert [resolved.preferred_transfer_client for resolved in seen] == ["crt"]

    def test_a_classic_selection_never_reads_the_crt_only_boto3_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Floor boto3 predates the CRT engine and carries no
        # InvalidCrtTransferConfigError. Reading it ahead of the selection gate
        # made this an ImportError on every classic run there - and every
        # transfer command calls it, so the floor had no working transfer at
        # all. Deleting the name is that floor, seen from the current SDK.
        import boto3.exceptions

        monkeypatch.delattr(boto3.exceptions, "InvalidCrtTransferConfigError", raising=False)
        client = SimpleNamespace(meta=SimpleNamespace(endpoint_url=None))
        config = TransferConfig(preferred_transfer_client="classic")
        # The no-op it is on the classic lane: no raise, nothing constructed.
        S3(transfer_config=config).materialize_crt_engine(client)  # pyright: ignore[reportArgumentType]

    def test_the_crt_lane_builds_without_the_crt_only_boto3_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # boto3 1.34.6 to 1.41 run the CRT engine (from 1.34.6 the crt extra's
        # awscrt meets boto3's own minimum) but carry no InvalidCrtTransferConfigError
        # - it arrived with 1.42's CRT-aware TransferConfig. Importing it on
        # the CRT lane failed every CRT transfer there with ImportError.
        import boto3.exceptions

        from boto3_s3 import crtsupport

        monkeypatch.delattr(boto3.exceptions, "InvalidCrtTransferConfigError", raising=False)
        built: list[Any] = []
        monkeypatch.setattr(
            crtsupport, "materialize_crt_engine", lambda _c, config, **_k: built.append(config)
        )
        client = SimpleNamespace(meta=SimpleNamespace(endpoint_url=None))
        config = TransferConfig(preferred_transfer_client="crt")
        S3(transfer_config=config).materialize_crt_engine(client)  # pyright: ignore[reportArgumentType]
        assert [resolved.preferred_transfer_client for resolved in built] == ["crt"]

    @pytest.mark.parametrize(
        ("raised", "expected"),
        [
            (
                FileNotFoundError(2, "No such file or directory", "/ca/missing.pem"),
                InvalidConfigError,
            ),
            (
                RuntimeError("34 (AWS_ERROR_INVALID_ARGUMENT): An invalid argument was passed."),
                InvalidConfigError,
            ),
            (
                ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "AssumeRole"),
                AccessDeniedError,
            ),
        ],
        ids=["missing-ca-bundle", "unparsable-ca-bundle", "refused-assume-role"],
    )
    def test_a_construction_failure_is_classified(
        self, monkeypatch: pytest.MonkeyPatch, raised: Exception, expected: type[Boto3S3Error]
    ) -> None:
        # awscrt reads the CA bundle path itself (OSError) and parses it in C
        # (RuntimeError); resolving a credential the compatibility check has
        # to compare by value raises the botocore family. None of them is the
        # documented pass-through, so each keeps its category and its text.
        from boto3_s3 import crtsupport

        def fake(_client: Any, _config: Any, **_kwargs: Any) -> None:
            raise raised

        monkeypatch.setattr(crtsupport, "materialize_crt_engine", fake)
        client = SimpleNamespace(meta=SimpleNamespace(endpoint_url=None))
        config = TransferConfig(preferred_transfer_client="crt")
        with pytest.raises(expected) as info:
            S3(transfer_config=config).materialize_crt_engine(client)  # pyright: ignore[reportArgumentType]
        assert str(info.value) == str(raised)
        assert info.value.__cause__ is raised


class TestModuleLevelConvenienceSignatures:
    """The module-level wrappers introspect as their method minus ``self``
    (design/s3.md): ``functools.wraps`` alone would expose the method's
    signature *with* ``self`` through ``__wrapped__``."""

    def test_signature_strips_self(self) -> None:
        import inspect

        import boto3_s3

        for name in ("cp", "ls", "mv", "rm", "mb", "rb", "presign", "sync", "website"):
            signature = inspect.signature(getattr(boto3_s3, name))
            assert "self" not in signature.parameters, name

    def test_signature_binds_positionally_like_the_method(self) -> None:
        import inspect

        import boto3_s3

        bound = inspect.signature(boto3_s3.cp).bind("local.txt", "s3://bucket/key")
        assert bound.args == ("local.txt", "s3://bucket/key")

    def test_wrappers_pickle_by_reference(self) -> None:
        # pickle stores a function as module + __qualname__, so the qualname
        # functools.wraps copied (``S3.cp``) resolved to the method itself and
        # the wrapper was refused as "not the same object" - unusable with a
        # ProcessPoolExecutor. The wrapper is named by its own binding instead.
        import pickle

        import boto3_s3

        for name in ("cp", "ls", "mv", "rm", "mb", "rb", "presign", "sync", "website"):
            wrapper = getattr(boto3_s3, name)
            assert wrapper.__qualname__ == name
            assert pickle.loads(pickle.dumps(wrapper)) is wrapper, name
