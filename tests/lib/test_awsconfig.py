"""Unit tests for boto3_s3.awsconfig: the general ~/.aws/config reader.

The parsing/lookup logic is exercised directly on ``ConfigSection`` (SDK-free,
with literal dicts); the file resolution, profile selection, and the
``S3.aws_config()`` seam are exercised against a temp ``AWS_CONFIG_FILE`` read
through a real boto3/botocore session.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest

from boto3_s3 import S3, ConfigurationError
from boto3_s3.awsconfig import AwsConfig, ConfigSection

_MIB = 1024 * 1024
_GIB = 1024**3

_CONFIG = """\
[default]
region = us-east-1
s3 =
    multipart_chunksize = 16MB
    max_concurrent_requests = 20
    max_bandwidth = 10MB/s
    use_accelerate_endpoint = true

[profile prod]
region = eu-west-1
s3 =
    multipart_chunksize = 32MB

[services my-svc]
s3 =
    endpoint_url = https://example.com

[sso-session my-sso]
sso_region = us-west-2

[plugins]
cli_legacy_plugin_path = /opt/plugins
"""


def _reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str = _CONFIG,
    *,
    profile: str | None = None,
) -> AwsConfig:
    """Write *content* to a temp config file and return a reader over it."""
    path = tmp_path / "config"
    path.write_text(content)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
    return AwsConfig.from_session(boto3.Session(profile_name=profile))


class TestActiveProfile:
    def test_typed_getters(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _reader(tmp_path, monkeypatch)
        assert cfg.get_str("region") == "us-east-1"
        assert cfg.get_size("s3.multipart_chunksize") == 16 * _MIB
        assert cfg.get_int("s3.max_concurrent_requests") == 20
        assert cfg.get_rate("s3.max_bandwidth") == 10 * _MIB
        assert cfg.get_bool("s3.use_accelerate_endpoint") is True

    def test_missing_key_returns_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _reader(tmp_path, monkeypatch)
        assert cfg.get_str("does_not_exist") is None
        assert cfg.get_str("does_not_exist", "fallback") == "fallback"
        assert cfg.get_size("s3.does_not_exist", 99) == 99

    def test_malformed_value_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _reader(tmp_path, monkeypatch, "[default]\ns3 =\n    multipart_chunksize = huge\n")
        with pytest.raises(ConfigurationError):
            cfg.get_size("s3.multipart_chunksize")

    def test_key_naming_a_section_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _reader(tmp_path, monkeypatch)
        with pytest.raises(ConfigurationError):
            cfg.get_str("s3")  # "s3" is a subsection, not a value


class TestProfileSelector:
    def test_named_profile(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _reader(tmp_path, monkeypatch)
        assert cfg.profile("prod").get_str("region") == "eu-west-1"
        assert cfg.profile("prod").get_size("s3.multipart_chunksize") == 32 * _MIB

    def test_default_profile_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _reader(tmp_path, monkeypatch)
        assert cfg.profile("default").get_str("region") == "us-east-1"

    def test_missing_profile_is_tolerant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _reader(tmp_path, monkeypatch)
        assert cfg.profile("ghost").get_str("region", "d") == "d"
        assert cfg.profile("ghost").get_size("s3.multipart_chunksize", 1) == 1


class TestActiveProfileResolution:
    def test_profile_name_selects_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _reader(tmp_path, monkeypatch, profile="prod")
        assert cfg.get_str("region") == "eu-west-1"
        assert cfg.get_size("s3.multipart_chunksize") == 32 * _MIB

    def test_aws_profile_env_selects_active(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "config"
        path.write_text(_CONFIG)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
        monkeypatch.setenv("AWS_PROFILE", "prod")
        cfg = AwsConfig.from_session()  # default session honors AWS_PROFILE
        assert cfg.get_str("region") == "eu-west-1"

    def test_set_but_missing_profile_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A set-but-missing AWS_PROFILE: the default session boto3 builds inside
        # from_session resolves the profile eagerly, so ProfileNotFound surfaces
        # here, converted to the library taxonomy.
        path = tmp_path / "config"
        path.write_text(_CONFIG)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
        monkeypatch.setenv("AWS_PROFILE", "ghost")
        with pytest.raises(ConfigurationError):
            AwsConfig.from_session()


class TestOtherSections:
    def test_services(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _reader(tmp_path, monkeypatch)
        assert cfg.services("my-svc").get_str("s3.endpoint_url") == "https://example.com"

    def test_sso_session(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _reader(tmp_path, monkeypatch)
        assert cfg.sso_session("my-sso").get_str("sso_region") == "us-west-2"

    def test_plugins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = _reader(tmp_path, monkeypatch)
        assert cfg.plugins().get_str("cli_legacy_plugin_path") == "/opt/plugins"

    def test_absent_section_is_tolerant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfg = _reader(tmp_path, monkeypatch)
        assert cfg.services("absent").get_str("s3.endpoint_url", "d") == "d"


class TestNoConfigFile:
    def test_missing_file_returns_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "does-not-exist"))
        cfg = AwsConfig.from_session(boto3.Session())
        assert cfg.get_str("region", "d") == "d"
        assert cfg.get_size("s3.multipart_chunksize", 7) == 7


class TestS3Method:
    def test_aws_config_reads_active_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "config"
        path.write_text(_CONFIG)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
        s3 = S3(session=boto3.Session())
        assert s3.aws_config().get_size("s3.multipart_chunksize") == 16 * _MIB

    def test_memoized_per_instance(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "config"
        path.write_text(_CONFIG)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
        s3 = S3(session=boto3.Session())
        assert s3.aws_config() is s3.aws_config()

    def test_zero_config_s3_uses_default_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "config"
        path.write_text(_CONFIG)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(path))
        assert S3().aws_config().get_str("region") == "us-east-1"


class TestConfigSectionParsing:
    """Parsing / lookup in isolation - literal dicts, no file or SDK."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("16MB", 16 * _MIB),
            ("16MiB", 16 * _MIB),
            ("5GB", 5 * _GIB),
            ("1048576", 1048576),
            ("0", 0),
        ],
    )
    def test_get_size(self, text: str, expected: int) -> None:
        assert ConfigSection({"k": text}).get_size("k") == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("10MB/s", 10 * _MIB),  # bytes/s
            ("1024B/s", 1024),  # bytes/s, no magnitude prefix
            ("800Kb/s", 102400),  # bits/s -> /8
            ("5Mb/s", 5 * _MIB // 8),  # bits/s -> /8
            ("1024b/s", 128),  # bits/s, no magnitude prefix -> /8
            ("1048576", 1048576),  # bare int = bytes/s
        ],
    )
    def test_get_rate(self, text: str, expected: int) -> None:
        assert ConfigSection({"k": text}).get_rate("k") == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [("true", True), ("True", True), ("false", False), ("yes", False), ("1", False)],
    )
    def test_get_bool(self, text: str, expected: bool) -> None:
        assert ConfigSection({"k": text}).get_bool("k") is expected

    @pytest.mark.parametrize("text", ["abc", "", "10XB", "MB", "1.5MB"])
    def test_bad_size_raises(self, text: str) -> None:
        with pytest.raises(ConfigurationError):
            ConfigSection({"k": text}).get_size("k")

    def test_bad_int_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            ConfigSection({"k": "abc"}).get_int("k")

    def test_bad_rate_raises(self) -> None:
        with pytest.raises(ConfigurationError):
            ConfigSection({"k": "fast"}).get_rate("k")

    def test_nested_lookup(self) -> None:
        sec = ConfigSection({"s3": {"multipart_chunksize": "16MB"}})
        assert sec.get_size("s3.multipart_chunksize") == 16 * _MIB

    def test_nested_missing_returns_default(self) -> None:
        sec = ConfigSection({"s3": {"x": "1"}})
        assert sec.get_str("s3.y", "d") == "d"
        assert sec.get_str("absent.deep", "d") == "d"

    def test_descend_into_scalar_returns_default(self) -> None:
        # A scalar cannot be descended into: "region.sub" is a miss, not an error.
        sec = ConfigSection({"region": "r"})
        assert sec.get_str("region.sub", "d") == "d"

    def test_section_as_value_raises(self) -> None:
        sec = ConfigSection({"s3": {"x": "1"}})
        with pytest.raises(ConfigurationError):
            sec.get_str("s3")

    def test_missing_returns_default_for_every_type(self) -> None:
        sec = ConfigSection({})
        assert sec.get_str("k") is None
        assert sec.get_int("k", 5) == 5
        assert sec.get_size("k", 5) == 5
        assert sec.get_rate("k", 5) == 5
        assert sec.get_bool("k", True) is True
