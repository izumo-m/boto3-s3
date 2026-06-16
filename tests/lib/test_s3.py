"""Unit tests for boto3_s3.S3: ls routing, and the client() / resolve() seams."""

from __future__ import annotations

import datetime as dt
from typing import Any

import boto3
import pytest
from botocore.config import Config

from boto3_s3 import S3, FileKind, LocalStorage, S3Storage, TransferConfig, ValidationError

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
    def test_s3storage_target_delegates_to_scan(self) -> None:
        client = _FakeS3Client([{"Contents": [{"Key": "p/a", "Size": 1, "LastModified": _MTIME}]}])
        storage = S3Storage("s3://b/p/", client=client)
        assert [info.key for info in S3().ls(storage)] == ["p/a"]

    def test_scheme_optional_storage_delegates_to_scan(self) -> None:
        # S3Storage accepts an s3://-less "bucket/key"; ls drives its scan the same.
        client = _FakeS3Client([{"Contents": [{"Key": "p/a", "Size": 1, "LastModified": _MTIME}]}])
        storage = S3Storage("b/p/", client=client)  # no s3:// scheme
        assert [info.key for info in S3().ls(storage)] == ["p/a"]

    def test_non_s3_location_raises_eagerly(self) -> None:
        # ls() is a regular method (not a generator): a non-S3 target raises on
        # the call itself, before any iteration.
        with pytest.raises(ValidationError):
            S3().ls(LocalStorage("/tmp/x"))

    def test_service_root_storage_lists_buckets(self) -> None:
        client = _FakeS3Client([{"Buckets": [{"Name": "alpha", "CreationDate": _MTIME}]}])
        storage = S3Storage("s3://", client=client)
        infos = list(S3().ls(storage))
        assert [(i.key, i.kind) for i in infos] == [("alpha", FileKind.BUCKET)]

    def test_bucket_filters_reach_the_scan(self) -> None:
        client = _FakeS3Client([])
        storage = S3Storage("s3://", client=client)
        list(S3().ls(storage, bucket_name_prefix="al", bucket_region="us-east-1"))
        assert client.calls[0]["Prefix"] == "al"
        assert client.calls[0]["BucketRegion"] == "us-east-1"

    def test_key_without_bucket_raises_eagerly(self) -> None:
        with pytest.raises(ValidationError):
            S3().ls("s3:///key")


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

    def test_fresh_client_each_call(self) -> None:
        # Documented contract: a fresh client per call, owned by the caller.
        s3 = S3()
        assert s3.client() is not s3.client()


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


class _StopTransferError(Exception):
    """Raised by the Transferrer spy to short-circuit before any transfer runs."""


@pytest.fixture
def _captured_transfer_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the ``transfer_config`` a transfer would build its Transferrer with.

    Patches ``Transferrer.__init__`` to record the kwarg and raise, so the test
    stops at construction - before the plan submits anything to S3.
    """
    import boto3_s3.s3 as s3mod

    captured: dict[str, Any] = {}

    def spy(_self: Any, _kind: Any, _client: Any, *, transfer_config: Any = None, **_: Any) -> None:
        captured["transfer_config"] = transfer_config
        raise _StopTransferError

    monkeypatch.setattr(s3mod.Transferrer, "__init__", spy)
    return captured


class TestTransferConfigDefault:
    """cp / mv / sync fall back to the instance ``transfer_config``; a per-call value wins."""

    def test_instance_default_reaches_the_transferrer(
        self, _captured_transfer_config: dict[str, Any], tmp_path: Any
    ) -> None:
        src = tmp_path / "x.txt"
        src.write_text("hi")
        marker = TransferConfig()
        with pytest.raises(_StopTransferError):
            S3(transfer_config=marker).cp(str(src), "s3://bucket/key")
        assert _captured_transfer_config["transfer_config"] is marker

    def test_per_call_overrides_instance_default(
        self, _captured_transfer_config: dict[str, Any], tmp_path: Any
    ) -> None:
        src = tmp_path / "x.txt"
        src.write_text("hi")
        instance_tc, call_tc = TransferConfig(), TransferConfig()
        with pytest.raises(_StopTransferError):
            S3(transfer_config=instance_tc).cp(str(src), "s3://bucket/key", transfer_config=call_tc)
        assert _captured_transfer_config["transfer_config"] is call_tc
