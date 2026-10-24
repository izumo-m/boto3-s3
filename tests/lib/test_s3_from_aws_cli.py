"""`S3.from_aws_cli`: an `S3` assembled the way ``aws s3`` assembles its own.

Each behavior here is what the pinned aws-cli does for the same option or the
option's absence (the oracle measurements live with the CLI tests that pinned
them first, tests/cli/unit/test_clientfactory.py); an `S3()` keeps boto3's
answer, which the tests show beside it where the two differ.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from boto3_s3 import (
    S3,
    ConfigurationError,
    InvalidConfigError,
    LocalStorage,
    S3Storage,
    TransferConfig,
    ValidationError,
    awsclisession,
    localencoding,
)


@pytest.fixture(autouse=True)
def _no_imds(monkeypatch: pytest.MonkeyPatch) -> None:
    # The region chain's last link is the EC2 metadata service: keep the
    # tests that clear the region from probing it.
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    config = tmp_path / "config"
    config.write_text(text)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))


def _verify(client: Any) -> Any:
    return client._endpoint.http_session._verify


class TestTheAssembly:
    def test_a_subclass_gets_itself_back(self) -> None:
        class Mine(S3):
            pass

        assert type(Mine.from_aws_cli()) is Mine

    def test_the_session_is_the_one_its_clients_come_from(self) -> None:
        s3 = S3.from_aws_cli(region="eu-west-1")
        assert s3.session is not None
        client = s3.client()
        assert client.meta.region_name == "eu-west-1"

    def test_the_profile_env_order_is_aws_clis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AWS_PROFILE before AWS_DEFAULT_PROFILE (stock botocore reverses them).
        _config(
            tmp_path,
            monkeypatch,
            "[profile one]\nregion = eu-west-1\n[profile two]\nregion = ap-south-1\n",
        )
        monkeypatch.delenv("AWS_REGION")
        monkeypatch.delenv("AWS_DEFAULT_REGION")
        monkeypatch.setenv("AWS_PROFILE", "one")
        monkeypatch.setenv("AWS_DEFAULT_PROFILE", "two")
        assert S3.from_aws_cli().client().meta.region_name == "eu-west-1"

    def test_aws_region_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Stock botocore reads AWS_DEFAULT_REGION alone.
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
        assert S3.from_aws_cli().client().meta.region_name == "eu-central-1"
        assert S3().client().meta.region_name == "us-west-2"

    def test_the_explicit_region_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        assert S3.from_aws_cli(region="sa-east-1").client().meta.region_name == "sa-east-1"

    def test_retries_are_aws_clis(self) -> None:
        retries = S3.from_aws_cli().client().meta.config.retries
        assert retries is not None
        assert (retries.get("mode"), retries.get("total_max_attempts")) == ("standard", 3)

    def test_us_east_1_is_regional(self) -> None:
        client = S3.from_aws_cli(region="us-east-1").client()
        assert client.meta.endpoint_url == "https://s3.us-east-1.amazonaws.com"
        assert S3().client().meta.endpoint_url == "https://s3.amazonaws.com"

    @pytest.mark.parametrize(
        ("value", "expected"), [(None, 60), (0, None), (7, 7)], ids=["default", "none", "seconds"]
    )
    def test_the_timeouts(self, value: int | None, expected: int | None) -> None:
        config = S3.from_aws_cli(cli_read_timeout=value, cli_connect_timeout=value).client()
        assert config.meta.config.read_timeout == expected
        assert config.meta.config.connect_timeout == expected

    def test_no_sign_request_is_unsigned(self) -> None:
        from botocore import UNSIGNED

        client = S3.from_aws_cli(no_sign_request=True).client()
        assert client.meta.config.signature_version is UNSIGNED

    def test_local_names_take_aws_clis_encoding(self) -> None:
        s3 = S3.from_aws_cli()
        assert s3._filename_encoding == localencoding.filename_encoding()  # pyright: ignore[reportPrivateUsage]
        storage = s3.resolve(".")
        assert isinstance(storage, LocalStorage)


class TestTrust:
    def test_no_verify_ssl_beats_a_ca_bundle(self, tmp_path: Path) -> None:
        s3 = S3.from_aws_cli(no_verify_ssl=True, ca_bundle=str(tmp_path / "ca.pem"))
        assert _verify(s3.client()) is False
        assert s3._crt.crt.verify is False  # pyright: ignore[reportPrivateUsage]

    def test_a_ca_bundle_reaches_both_clients(self, tmp_path: Path) -> None:
        bundle = str(tmp_path / "ca.pem")
        s3 = S3.from_aws_cli(ca_bundle=bundle)
        assert _verify(s3.client()) == bundle
        assert s3._crt.crt.verify == bundle  # pyright: ignore[reportPrivateUsage]

    def test_the_crt_reads_neither_requests_ca_bundle_nor_certifi(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from botocore.httpsession import DEFAULT_CA_BUNDLE

        bundle = tmp_path / "requests.pem"
        bundle.write_text("")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
        s3 = S3.from_aws_cli()
        assert _verify(s3.client()) == str(bundle)
        assert s3._crt.crt.verify == DEFAULT_CA_BUNDLE  # pyright: ignore[reportPrivateUsage]

    def test_the_crt_runs_without_the_lock_under_an_explicit_crt(self) -> None:
        assert S3.from_aws_cli()._crt.crt.allow_lockless is True  # pyright: ignore[reportPrivateUsage]
        assert S3()._crt.crt.allow_lockless is False  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.parametrize("value", ["", "  "], ids=["empty", "whitespace"])
    def test_an_empty_ca_bundle_is_refused(self, value: str) -> None:
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle"):
            S3.from_aws_cli(ca_bundle=value)

    def test_an_empty_ca_bundle_config_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_CA_BUNDLE", "")
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle"):
            S3.from_aws_cli()


class TestFailingBeforeARun:
    def test_a_schemeless_endpoint_url(self) -> None:
        with pytest.raises(ValidationError, match="scheme is missing"):
            S3.from_aws_cli(endpoint_url="s3.example.com")

    def test_an_unknown_profile(self) -> None:
        with pytest.raises(InvalidConfigError, match="could not be found"):
            S3.from_aws_cli(profile="no-such-profile")

    def test_a_crt_run_without_a_region(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # aws-cli stops a CRT run with no region before it starts, a dryrun
        # included; botocore would sign for aws-global.
        pytest.importorskip("awscrt")
        from boto3_s3 import crtsupport

        monkeypatch.setattr(crtsupport, "selects_crt", lambda _config: True)
        monkeypatch.delenv("AWS_REGION")
        monkeypatch.delenv("AWS_DEFAULT_REGION")
        src = tmp_path / "f.txt"
        src.write_text("x")
        s3 = S3.from_aws_cli()
        with pytest.raises(ConfigurationError, match="You must specify a region") as excinfo:
            s3.cp(str(src), "s3://bucket/f.txt", dryrun=True)
        assert type(excinfo.value.__cause__).__name__ == "NoRegionError"
        with pytest.raises(ConfigurationError, match="You must specify a region"):
            s3.rm("s3://bucket/f.txt", dryrun=True)

    def test_a_classic_run_without_a_region_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("AWS_REGION")
        monkeypatch.delenv("AWS_DEFAULT_REGION")
        src = tmp_path / "f.txt"
        src.write_text("x")
        results: list[Any] = []
        S3.from_aws_cli().cp(
            str(src),
            "s3://bucket/f.txt",
            dryrun=True,
            transfer_config=TransferConfig(preferred_transfer_client="classic"),
            on_result=results.append,
        )
        assert len(results) == 1


class TestCredentialCache:
    def test_temporary_credentials_go_to_aws_clis_cache(self) -> None:
        s3 = S3.from_aws_cli()
        assert s3.session is not None
        chain = s3.session._session.get_component("credential_provider")  # pyright: ignore[reportPrivateUsage]
        for name in ("assume-role", "assume-role-with-web-identity", "sso"):
            cache = chain.get_provider(name).cache
            assert isinstance(cache, awsclisession.credential_cache_class())
            assert cache._dir == awsclisession.credential_cache_dir()

    def test_cache_credentials_false_keeps_boto3s_memory(self) -> None:
        s3 = S3.from_aws_cli(cache_credentials=False)
        assert s3.session is not None
        chain = s3.session._session.get_component("credential_provider")  # pyright: ignore[reportPrivateUsage]
        assert not isinstance(
            chain.get_provider("assume-role").cache, awsclisession.credential_cache_class()
        )

    def test_the_process_is_left_alone(self) -> None:
        # The SSO token cache class is process-wide: the application's to set.
        from botocore.tokens import SSOTokenProvider

        before = SSOTokenProvider.DEFAULT_CACHE_CLS
        S3.from_aws_cli()
        assert SSOTokenProvider.DEFAULT_CACHE_CLS is before


class TestTheUploadChecksum:
    @staticmethod
    def _sides(s3: S3, tmp_path: Path) -> tuple[Any, Any]:
        return s3.resolve(str(tmp_path)), s3.resolve("s3://bucket/k")

    def test_an_upload_names_aws_clis_default(self, tmp_path: Path) -> None:
        pytest.importorskip("awscrt")
        s3 = S3.from_aws_cli()
        src, dest = self._sides(s3, tmp_path)
        config = TransferConfig(preferred_transfer_client="classic")
        options = s3._upload_options({}, src, dest, config)  # pyright: ignore[reportPrivateUsage]
        assert options == {"checksum_algorithm": "CRC64NVME"}

    def test_the_callers_choice_and_other_routes_are_left_alone(self, tmp_path: Path) -> None:
        s3 = S3.from_aws_cli()
        src, dest = self._sides(s3, tmp_path)
        config = TransferConfig(preferred_transfer_client="classic")
        named = s3._upload_options({"checksum_algorithm": "SHA256"}, src, dest, config)  # pyright: ignore[reportPrivateUsage]
        assert named == {"checksum_algorithm": "SHA256"}
        assert s3._upload_options({}, dest, src, config) == {}  # pyright: ignore[reportPrivateUsage]
        copy_dest = S3Storage("s3://other/k", client=s3.client())
        assert s3._upload_options({}, dest, copy_dest, config) == {}  # pyright: ignore[reportPrivateUsage]

    def test_an_instance_the_caller_built_keeps_botocores_default(self, tmp_path: Path) -> None:
        s3 = S3()
        src, dest = self._sides(s3, tmp_path)
        config = TransferConfig(preferred_transfer_client="classic")
        assert s3._upload_options({}, src, dest, config) == {}  # pyright: ignore[reportPrivateUsage]
