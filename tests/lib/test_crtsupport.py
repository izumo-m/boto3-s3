"""``boto3_s3.crtsupport`` - boto3-faithful CRT engine resolution.

Everything runs against monkeypatched ``s3transfer.crt`` / ``awscrt``
attributes: the CRT cannot be exercised in-process (moto bypasses it), so
these tests pin the selection matrix, the singleton + lock behavior, and the
client-derived wiring (endpoint / use_ssl / verify / credentials / part_size)
that docs/crt.md documents.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest

# s3transfer.crt imports awscrt at module top, so importorskip skips this whole
# module when awscrt is absent. The floor s3transfer (0.6.2) ships crt.py but
# predates the 0.8.0 CRT surface (the process lock etc.) that CrtStubs patches;
# the library defers this import so it stays SDK-free.
s3transfer_crt = pytest.importorskip("s3transfer.crt")
from botocore import UNSIGNED  # noqa: E402
from botocore.exceptions import MissingDependencyException  # noqa: E402

from boto3_s3 import crtsupport  # noqa: E402
from boto3_s3.transferconfig import TransferConfig  # noqa: E402

AWS_ENDPOINT = "https://s3.us-east-1.amazonaws.com"


def make_creds(access_key: str = "AK", secret: str = "SK", token: str | None = None) -> Any:
    frozen = SimpleNamespace(access_key=access_key, secret_key=secret, token=token)
    return SimpleNamespace(get_frozen_credentials=lambda: frozen)


class FakeClient:
    def __init__(
        self,
        *,
        region: str = "us-east-1",
        endpoint: str = AWS_ENDPOINT,
        verify: Any = True,
        unsigned: bool = False,
        creds: Any = None,
    ) -> None:
        signature_version: Any = UNSIGNED if unsigned else "s3v4"
        self.meta = SimpleNamespace(
            region_name=region,
            endpoint_url=endpoint,
            config=SimpleNamespace(signature_version=signature_version),
        )
        self._endpoint = SimpleNamespace(http_session=SimpleNamespace(_verify=verify))
        self._creds = creds if creds is not None else make_creds()

    def _get_credentials(self) -> Any:
        return self._creds


class FakeCredWrapper:
    """Stands in for BotocoreCRTCredentialsWrapper: wraps botocore credentials."""

    def __init__(self, credentials: Any) -> None:
        self._credentials = credentials

    def to_crt_credentials_provider(self) -> str:
        return "crt-provider"

    def __call__(self) -> Any:
        frozen = self._credentials.get_frozen_credentials()
        return SimpleNamespace(
            access_key_id=frozen.access_key,
            secret_access_key=frozen.secret_key,
            session_token=frozen.token,
        )


class CrtStubs:
    """Recorded fakes patched onto the real ``s3transfer.crt`` module."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.lock: Any = object()
        self.lock_names: list[str] = []
        self.create_kwargs: list[dict[str, Any]] = []
        self.serializer_args: list[tuple[Any, dict[str, Any]]] = []
        self.manager_kwargs: list[dict[str, Any]] = []
        self.crt_client = object()
        stubs = self

        def acquire(name: str) -> Any:
            stubs.lock_names.append(name)
            return stubs.lock

        def create_client(**kwargs: Any) -> Any:
            stubs.create_kwargs.append(kwargs)
            return stubs.crt_client

        class Serializer:
            def __init__(self, session: Any, client_kwargs: dict[str, Any]) -> None:
                stubs.serializer_args.append((session, client_kwargs))

        class Manager:
            def __init__(self, **kwargs: Any) -> None:
                stubs.manager_kwargs.append(kwargs)

        monkeypatch.setattr(s3transfer_crt, "acquire_crt_s3_process_lock", acquire)
        monkeypatch.setattr(s3transfer_crt, "create_s3_crt_client", create_client)
        monkeypatch.setattr(s3transfer_crt, "BotocoreCRTRequestSerializer", Serializer)
        monkeypatch.setattr(s3transfer_crt, "BotocoreCRTCredentialsWrapper", FakeCredWrapper)
        monkeypatch.setattr(s3transfer_crt, "CRTTransferManager", Manager)


@pytest.fixture(autouse=True)
def reset_singletons() -> Any:
    crtsupport._reset_for_tests()
    yield
    crtsupport._reset_for_tests()


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch) -> CrtStubs:
    return CrtStubs(monkeypatch)


def set_optimized(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    import awscrt.s3

    monkeypatch.setattr(awscrt.s3, "is_optimized_for_system", lambda: value)


def hide_awscrt(monkeypatch: pytest.MonkeyPatch) -> None:
    # A None entry in sys.modules makes ``import awscrt`` raise ImportError.
    monkeypatch.setitem(sys.modules, "awscrt", None)
    monkeypatch.setitem(sys.modules, "awscrt.s3", None)


class TestShouldUseCrt:
    def test_classic_never_attempts_crt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_optimized(monkeypatch, True)
        assert crtsupport.should_use_crt("classic") is False

    def test_auto_requires_optimized_system(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_optimized(monkeypatch, False)
        assert crtsupport.should_use_crt("auto") is False
        set_optimized(monkeypatch, True)
        assert crtsupport.should_use_crt("auto") is True

    def test_explicit_crt_skips_the_optimized_gate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_optimized(monkeypatch, False)
        assert crtsupport.should_use_crt("crt") is True

    def test_preference_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_optimized(monkeypatch, False)
        assert crtsupport.should_use_crt("CRT") is True

    def test_auto_without_awscrt_is_classic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hide_awscrt(monkeypatch)
        assert crtsupport.should_use_crt("auto") is False

    def test_explicit_crt_without_awscrt_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        hide_awscrt(monkeypatch)
        with pytest.raises(MissingDependencyException) as exc_info:
            crtsupport.should_use_crt("crt")
        assert "CRT installed: False" in str(exc_info.value)

    def test_explicit_crt_with_old_awscrt_raises_with_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        old = SimpleNamespace(__version__="0.19.17")
        monkeypatch.setitem(sys.modules, "awscrt", old)
        with pytest.raises(MissingDependencyException) as exc_info:
            crtsupport.should_use_crt("crt")
        assert "CRT installed: True, with version: 0.19.17" in str(exc_info.value)

    def test_auto_without_crt_s3transfer_is_classic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # awscrt present + host optimized, but the floor s3transfer (< 0.8.0)
        # lacks the CRT surface -> degrade to classic, do not crash.
        set_optimized(monkeypatch, True)
        monkeypatch.setattr(crtsupport, "has_crt_s3transfer", lambda: False)
        assert crtsupport.should_use_crt("auto") is False

    def test_explicit_crt_with_old_s3transfer_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        set_optimized(monkeypatch, True)  # awscrt present
        monkeypatch.setattr(crtsupport, "has_crt_s3transfer", lambda: False)
        with pytest.raises(MissingDependencyException) as exc_info:
            crtsupport.should_use_crt("crt")
        assert "s3transfer" in str(exc_info.value)

    def test_has_crt_s3transfer_false_when_lock_symbol_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(s3transfer_crt, "acquire_crt_s3_process_lock", raising=False)
        assert crtsupport.has_crt_s3transfer() is False


class TestCreateCrtTransferManager:
    def test_creates_manager_with_derived_wiring(self, stubs: CrtStubs) -> None:
        client = FakeClient(endpoint="http://127.0.0.1:9000")
        config = TransferConfig(preferred_transfer_client="crt")
        manager = crtsupport.create_crt_transfer_manager(client, config)  # pyright: ignore[reportArgumentType]
        assert manager is not None
        assert stubs.lock_names == ["boto3-s3"]
        [kwargs] = stubs.create_kwargs
        assert kwargs["region"] == "us-east-1"
        assert kwargs["use_ssl"] is False  # http endpoint
        assert kwargs["verify"] is None  # botocore default True maps to None
        assert kwargs["part_size"] is None  # chunksize not explicit -> CRT dynamic
        assert kwargs["target_throughput"] is None
        assert kwargs["crt_credentials_provider"] == "crt-provider"
        [(_, client_kwargs)] = stubs.serializer_args
        assert client_kwargs == {
            "region_name": "us-east-1",
            "endpoint_url": "http://127.0.0.1:9000",
            # The client's own Config rides along so the serializer's nested
            # client keeps the caller's endpoint/addressing settings (the CLI's
            # us-east-1 regional pin included - aws v2's bundled botocore is
            # regional-only there, stock botocore is not).
            "config": client.meta.config,
        }
        [manager_kwargs] = stubs.manager_kwargs
        assert manager_kwargs["crt_s3_client"] is stubs.crt_client
        assert manager_kwargs["config"] is config

    def test_aws_default_endpoint_stays_none(self, stubs: CrtStubs) -> None:
        client = FakeClient(endpoint=AWS_ENDPOINT)
        assert crtsupport.create_crt_transfer_manager(client, None) is not None  # pyright: ignore[reportArgumentType]
        [(_, client_kwargs)] = stubs.serializer_args
        assert client_kwargs["endpoint_url"] is None
        [kwargs] = stubs.create_kwargs
        assert kwargs["use_ssl"] is True

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://s3.cn-north-1.amazonaws.com.cn",
            "https://s3.eusc-de-east-1.amazonaws.eu",
            "https://s3.us-iso-east-1.c2s.ic.gov",
            "https://s3.dualstack.us-east-1.api.aws",
        ],
        ids=["china", "eusc", "iso", "dualstack"],
    )
    def test_non_commercial_partition_endpoints_stay_none(
        self, stubs: CrtStubs, endpoint: str
    ) -> None:
        # A standard endpoint in any botocore partition - not just the two
        # commercial suffixes - is the AWS default form and must not be pinned.
        client = FakeClient(endpoint=endpoint)
        assert crtsupport.create_crt_transfer_manager(client, None) is not None  # pyright: ignore[reportArgumentType]
        [(_, client_kwargs)] = stubs.serializer_args
        assert client_kwargs["endpoint_url"] is None

    def test_custom_host_is_pinned(self, stubs: CrtStubs) -> None:
        # A host outside every partition suffix (a lookalike domain, a gateway)
        # is custom and must ride into the CRT wiring, not be dropped to None.
        client = FakeClient(endpoint="https://s3.example.com")
        assert crtsupport.create_crt_transfer_manager(client, None) is not None  # pyright: ignore[reportArgumentType]
        [(_, client_kwargs)] = stubs.serializer_args
        assert client_kwargs["endpoint_url"] == "https://s3.example.com"

    def test_aws_domain_custom_endpoint_dropped_without_the_explicit_signal(
        self, stubs: CrtStubs
    ) -> None:
        # A VPC interface endpoint sits under an AWS suffix, so the host heuristic
        # alone cannot tell it from a resolved standard endpoint and drops it to
        # None (the gap the explicit signal below closes).
        vpce = "https://bucket.vpce-0abc.s3.us-east-1.vpce.amazonaws.com"
        client = FakeClient(endpoint=vpce)
        assert crtsupport.create_crt_transfer_manager(client, None) is not None  # pyright: ignore[reportArgumentType]
        [(_, client_kwargs)] = stubs.serializer_args
        assert client_kwargs["endpoint_url"] is None

    def test_explicit_endpoint_is_pinned_even_under_an_aws_domain(self, stubs: CrtStubs) -> None:
        # The caller's --endpoint-url is honored verbatim, matching aws-cli - so
        # the VPC endpoint reaches the CRT serializer instead of re-resolving to
        # public S3.
        vpce = "https://bucket.vpce-0abc.s3.us-east-1.vpce.amazonaws.com"
        client = FakeClient(endpoint=vpce)
        manager = crtsupport.create_crt_transfer_manager(client, None, endpoint=vpce)  # pyright: ignore[reportArgumentType]
        assert manager is not None
        [(_, client_kwargs)] = stubs.serializer_args
        assert client_kwargs["endpoint_url"] == vpce

    def test_explicit_none_keeps_the_heuristic(self, stubs: CrtStubs) -> None:
        # No --endpoint-url (endpoint=None) means "use the heuristic": a resolved
        # AWS endpoint still maps to None, unchanged from the no-signal call.
        assert (
            crtsupport.create_crt_transfer_manager(
                FakeClient(endpoint=AWS_ENDPOINT), None, endpoint=None
            )  # pyright: ignore[reportArgumentType]
            is not None
        )
        [(_, client_kwargs)] = stubs.serializer_args
        assert client_kwargs["endpoint_url"] is None

    def test_lock_held_elsewhere_falls_back_to_classic(
        self, stubs: CrtStubs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(s3transfer_crt, "acquire_crt_s3_process_lock", lambda name: None)
        assert crtsupport.create_crt_transfer_manager(FakeClient(), None) is None  # pyright: ignore[reportArgumentType]
        # boto3 re-attempts on the next call while the singleton is unset.
        monkeypatch.setattr(s3transfer_crt, "acquire_crt_s3_process_lock", lambda name: object())
        assert crtsupport.create_crt_transfer_manager(FakeClient(), None) is not None  # pyright: ignore[reportArgumentType]

    def test_singleton_is_reused_for_a_compatible_client(self, stubs: CrtStubs) -> None:
        first = crtsupport.create_crt_transfer_manager(FakeClient(), None)  # pyright: ignore[reportArgumentType]
        second = crtsupport.create_crt_transfer_manager(FakeClient(), None)  # pyright: ignore[reportArgumentType]
        assert first is not None and second is not None
        assert len(stubs.create_kwargs) == 1  # one CRT client for the process

    @pytest.mark.parametrize(
        "mismatch",
        [
            {"region": "eu-west-1"},
            {"endpoint": "http://127.0.0.1:9000"},
            {"unsigned": True},
            {"creds": make_creds(access_key="OTHER")},
        ],
        ids=["region", "endpoint", "unsigned", "credentials"],
    )
    def test_incompatible_second_client_falls_back(
        self, stubs: CrtStubs, mismatch: dict[str, Any]
    ) -> None:
        assert crtsupport.create_crt_transfer_manager(FakeClient(), None) is not None  # pyright: ignore[reportArgumentType]
        other = FakeClient(**mismatch)
        assert crtsupport.create_crt_transfer_manager(other, None) is None  # pyright: ignore[reportArgumentType]

    def test_unsigned_client_omits_the_credentials_provider(self, stubs: CrtStubs) -> None:
        client = FakeClient(unsigned=True)
        assert crtsupport.create_crt_transfer_manager(client, None) is not None  # pyright: ignore[reportArgumentType]
        [kwargs] = stubs.create_kwargs
        assert "crt_credentials_provider" not in kwargs
        # A second unsigned client is compatible with the unsigned singleton.
        assert crtsupport.create_crt_transfer_manager(FakeClient(unsigned=True), None) is not None  # pyright: ignore[reportArgumentType]

    def test_explicit_chunksize_becomes_part_size(self, stubs: CrtStubs) -> None:
        config = TransferConfig(multipart_chunksize=16 * 1024 * 1024)
        crtsupport.create_crt_transfer_manager(FakeClient(), config)  # pyright: ignore[reportArgumentType]
        [kwargs] = stubs.create_kwargs
        assert kwargs["part_size"] == 16 * 1024 * 1024

    def test_target_bandwidth_becomes_target_throughput(self, stubs: CrtStubs) -> None:
        config = TransferConfig(target_bandwidth=100_000_000)
        crtsupport.create_crt_transfer_manager(FakeClient(), config)  # pyright: ignore[reportArgumentType]
        [kwargs] = stubs.create_kwargs
        assert kwargs["target_throughput"] == 100_000_000

    @pytest.mark.parametrize(
        ("botocore_verify", "expected"),
        [(True, None), (False, False), ("/etc/ca.pem", "/etc/ca.pem")],
        ids=["default", "disabled", "ca-bundle"],
    )
    def test_verify_mapping(self, stubs: CrtStubs, botocore_verify: Any, expected: Any) -> None:
        client = FakeClient(verify=botocore_verify)
        crtsupport.create_crt_transfer_manager(client, None)  # pyright: ignore[reportArgumentType]
        [kwargs] = stubs.create_kwargs
        assert kwargs["verify"] == expected

    def test_plain_config_is_not_forwarded_to_the_manager(
        self, stubs: CrtStubs, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No UNSET machinery (old s3transfer < 0.16, which lacks the manager's
        # ``config`` kwarg) -> drop the config and warn, boto3-faithfully.
        config = SimpleNamespace(target_bandwidth=None)
        with caplog.at_level(logging.WARNING, logger="boto3_s3.crtsupport"):
            crtsupport.create_crt_transfer_manager(FakeClient(), config)  # pyright: ignore[reportArgumentType]
        [manager_kwargs] = stubs.manager_kwargs
        assert "config" not in manager_kwargs
        assert "s3transfer >= 0.16.0" in caplog.text

    def test_absent_config_does_not_warn(
        self, stubs: CrtStubs, caplog: pytest.LogCaptureFixture
    ) -> None:
        # No config at all is the normal path - nothing to drop, no warning.
        with caplog.at_level(logging.WARNING, logger="boto3_s3.crtsupport"):
            crtsupport.create_crt_transfer_manager(FakeClient(), None)  # pyright: ignore[reportArgumentType]
        assert "s3transfer >= 0.16.0" not in caplog.text

    def test_explicit_crt_rejects_classic_only_options(self, stubs: CrtStubs) -> None:
        from boto3.exceptions import InvalidCrtTransferConfigError

        config = TransferConfig(max_bandwidth=1_000_000, preferred_transfer_client="crt")
        with pytest.raises(InvalidCrtTransferConfigError) as exc_info:
            crtsupport.create_crt_transfer_manager(FakeClient(), config)  # pyright: ignore[reportArgumentType]
        assert "max_bandwidth" in str(exc_info.value)

    def test_auto_mode_allows_classic_only_options(self, stubs: CrtStubs) -> None:
        # boto3 only validates the explicit-'crt' config, not 'auto'.
        config = TransferConfig(max_bandwidth=1_000_000)
        assert crtsupport.create_crt_transfer_manager(FakeClient(), config) is not None  # pyright: ignore[reportArgumentType]


class TestFioOptions:
    def test_skipped_when_create_fn_lacks_the_parameter(self, stubs: CrtStubs) -> None:
        config = TransferConfig(should_stream=True, disk_throughput=1_000_000_000, direct_io=True)
        crtsupport.create_crt_transfer_manager(FakeClient(), config)  # pyright: ignore[reportArgumentType]
        [kwargs] = stubs.create_kwargs
        assert "fio_options" not in kwargs

    def test_forwarded_once_s3transfer_supports_it(
        self, stubs: CrtStubs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: list[dict[str, Any]] = []

        def create_client_with_fio(*, fio_options: Any = None, **kwargs: Any) -> Any:
            recorded.append({"fio_options": fio_options, **kwargs})
            return stubs.crt_client

        monkeypatch.setattr(s3transfer_crt, "create_s3_crt_client", create_client_with_fio)
        config = TransferConfig(should_stream=False, disk_throughput=1_000_000_000, direct_io=True)
        crtsupport.create_crt_transfer_manager(FakeClient(), config)  # pyright: ignore[reportArgumentType]
        [kwargs] = recorded
        assert kwargs["fio_options"] == {
            "should_stream": False,
            "disk_throughput_gbps": 8.0,  # 1e9 bytes/s * 8 / 1e9
            "direct_io": True,
        }


class TestBotocoreSession:
    """`_botocore_session` preference: caller's session, boto3 default, fresh.

    The serializer must reuse a warm session when one is reachable - a fresh
    botocore session re-parses the S3 model per process, the CRT lane's
    dominant fixed cost versus aws-cli (docs/crt.md, benchmark-measured).
    """

    def test_prefers_the_callers_session(self) -> None:
        inner = object()
        caller = SimpleNamespace(_session=inner)
        assert crtsupport._botocore_session(caller) is inner  # pyright: ignore[reportArgumentType]

    def test_falls_back_to_the_boto3_default_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import boto3

        inner = object()
        monkeypatch.setattr(boto3, "DEFAULT_SESSION", SimpleNamespace(_session=inner))
        assert crtsupport._botocore_session(None) is inner

    def test_fresh_session_when_nothing_is_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import boto3
        from botocore.session import Session

        monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
        assert isinstance(crtsupport._botocore_session(None), Session)
