"""Unit tests for boto3_s3.S3: ls routing, and the client() / resolve() seams."""

from __future__ import annotations

import datetime as dt
import io
import os
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ProfileNotFound

from boto3_s3 import (
    CLIENT_REGION,
    S3,
    CancelledError,
    CancelToken,
    FileKind,
    InvalidConfigError,
    IOStorage,
    LocalStorage,
    NotFoundError,
    S3Storage,
    TransferConfig,
    ValidationError,
)
from tests.utils.fakemodel import model_meta

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
        # The ListBuckets filter gate reads the model, so the fake carries the
        # one a current botocore has.
        self.meta = model_meta({"ListBuckets": {"Prefix", "BucketRegion"}})

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

    def test_malformed_endpoint_maps_to_invalid_config_error(self) -> None:
        # botocore rejects a malformed endpoint_url with a plain ValueError,
        # which is not a BotoCoreError and used to leak raw through the
        # translator - a break of client()'s "never the raw botocore error"
        # contract. Pin the exact refinement type (rc 255 lane).
        with pytest.raises(InvalidConfigError) as exc_info:
            S3(endpoint_url="not-a-url").client()
        assert type(exc_info.value) is InvalidConfigError
        assert isinstance(exc_info.value.__cause__, ValueError)


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
    """cp / mv / sync fall back to the instance ``transfer_config``; a per-call value wins."""

    def test_instance_default_reaches_the_transferrer(
        self, _captured_transferrer_kwargs: dict[str, Any], tmp_path: Any
    ) -> None:
        src = tmp_path / "x.txt"
        src.write_text("hi")
        marker = TransferConfig()
        with pytest.raises(_StopTransferError):
            S3(transfer_config=marker).cp(str(src), "s3://bucket/key")
        assert _captured_transferrer_kwargs["transfer_config"] is marker

    def test_per_call_overrides_instance_default(
        self, _captured_transferrer_kwargs: dict[str, Any], tmp_path: Any
    ) -> None:
        src = tmp_path / "x.txt"
        src.write_text("hi")
        instance_tc, call_tc = TransferConfig(), TransferConfig()
        with pytest.raises(_StopTransferError):
            S3(transfer_config=instance_tc).cp(str(src), "s3://bucket/key", transfer_config=call_tc)
        assert _captured_transferrer_kwargs["transfer_config"] is call_tc


class TestCrtAbsentCredentialsPosture:
    """``crt_allow_absent_credentials`` reaches every route that builds a Transferrer.

    The flag is what makes ``boto3-s3-cli`` enter the CRT engine with no
    credentials the way ``aws s3`` does (design/crt.md section 4); a route that
    forgot to thread it would silently keep boto3's classic fallback there.
    """

    def _run(self, s3: S3, tmp_path: Any, route: str) -> None:
        src = tmp_path / "x.txt"
        src.write_text("hi")
        if route == "cp":
            s3.cp(str(src), "s3://bucket/key")
        elif route == "stream":
            # The streaming route builds its own Transferrer (s3.py `_cp_stream`).
            s3.cp(IOStorage(io.BytesIO(b"hi")), "s3://bucket/key")
        else:
            s3.sync(str(tmp_path), "s3://bucket/pfx")

    @pytest.mark.parametrize("route", ["cp", "stream", "sync"])
    @pytest.mark.parametrize("allow", [False, True], ids=["default", "opt-in"])
    def test_posture_reaches_every_route(
        self,
        _captured_transferrer_kwargs: dict[str, Any],
        tmp_path: Any,
        route: str,
        allow: bool,
    ) -> None:
        s3 = S3(crt_allow_absent_credentials=allow) if allow else S3()
        with pytest.raises(_StopTransferError):
            self._run(s3, tmp_path, route)
        assert _captured_transferrer_kwargs["crt_allow_absent_credentials"] is allow


class TestCrtRegionPosture:
    """``crt_region`` reaches every route that builds a Transferrer.

    Same reason as the credentials posture above: the declared region - a
    ``None`` that says "the chain resolved nothing" included - is what makes
    ``boto3-s3-cli`` reproduce ``aws s3``'s CRT construction failure
    (design/crt.md section 6), and a route that forgot to thread it would
    silently fall back to the client's ``aws-global``.
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
        "declared", [CLIENT_REGION, None, "eu-west-1"], ids=["default", "absent", "explicit"]
    )
    def test_posture_reaches_every_route(
        self,
        _captured_transferrer_kwargs: dict[str, Any],
        tmp_path: Any,
        route: str,
        declared: Any,
    ) -> None:
        s3 = S3() if declared is CLIENT_REGION else S3(crt_region=declared)
        with pytest.raises(_StopTransferError):
            self._run(s3, tmp_path, route)
        assert _captured_transferrer_kwargs["crt_region"] is declared


class TestMaterializeCrtEngine:
    """`S3.materialize_crt_engine` hands the instance's postures to crtsupport.

    The seam ``rm`` uses to pay ``aws s3``'s transfer-manager construction
    without transferring; the postures it forwards are what make that
    construction fail where aws's does.
    """

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
            crt_allow_absent_credentials=True,
            crt_region=None,
        )
        s3.materialize_crt_engine(client, transfer_config=config)  # pyright: ignore[reportArgumentType]
        assert seen == [
            {
                "client": client,
                "config": config,
                "endpoint": endpoint,
                "session": session,
                "allow_absent_credentials": True,
                "region": None,
            }
        ]

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
        assert seen == [config]


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
