"""``boto3_s3_cli.runtimeconfig`` - the aws-cli ``[s3]`` config parser, ported.

The parsing matrix is aws-cli ``tests/unit/customizations/s3/
test_transferconfig.py`` translated (their ``InvalidConfigError`` escapes to
the aws general handler = rc 255; ours is the library class of the same name,
which the CLI's exit-code mapping sends to the same 255). The added classes pin
what the port owns beyond the aws-cli file: byte-exact error wording and the
scoped-config read.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import boto3
import pytest

from boto3_s3 import S3, InvalidConfigError
from boto3_s3_cli import runtimeconfig


def build_config_with(**config_from_user: Any) -> dict[str, Any]:
    return runtimeconfig.RuntimeConfig().build_config(**config_from_user)


class TestRuntimeConfig:
    def test_user_provides_no_config_uses_default(self) -> None:
        assert build_config_with() == runtimeconfig.DEFAULTS

    def test_defaults_are_awscli_defaults_verbatim(self) -> None:
        # Literal pin of aws-cli's transferconfig DEFAULTS: comparing
        # build_config() against runtimeconfig.DEFAULTS alone would stay green
        # if both drifted from aws-cli's table together.
        assert runtimeconfig.DEFAULTS == {
            "multipart_threshold": 8 * (1024**2),
            "multipart_chunksize": 8 * (1024**2),
            "max_concurrent_requests": 10,
            "max_queue_size": 1000,
            "max_bandwidth": None,
            "preferred_transfer_client": "auto",
            "target_bandwidth": None,
            "io_chunksize": 256 * 1024,
            "should_stream": None,
            "disk_throughput": None,
            "direct_io": None,
        }

    def test_user_provides_partial_overrides(self) -> None:
        config_from_user = {
            "max_concurrent_requests": "20",
            "multipart_threshold": str(64 * (1024**2)),
        }
        runtime_config = build_config_with(**config_from_user)
        assert runtime_config["multipart_threshold"] == 64 * (1024**2)
        assert runtime_config["max_concurrent_requests"] == 20
        assert runtime_config["max_queue_size"] == runtimeconfig.DEFAULTS["max_queue_size"]

    def test_validates_integer_types(self) -> None:
        with pytest.raises(InvalidConfigError):
            build_config_with(max_concurrent_requests="not an int")

    def test_validates_positive_integers(self) -> None:
        with pytest.raises(InvalidConfigError):
            build_config_with(max_concurrent_requests="-10")

    def test_min_value(self) -> None:
        with pytest.raises(InvalidConfigError):
            build_config_with(max_concurrent_requests="0")

    def test_human_readable_sizes_converted_to_bytes(self) -> None:
        runtime_config = build_config_with(multipart_threshold="10MB")
        assert runtime_config["multipart_threshold"] == 10 * 1024 * 1024

    def test_long_value(self) -> None:
        long_value = sys.maxsize + 1
        runtime_config = build_config_with(multipart_threshold=long_value)
        assert runtime_config["multipart_threshold"] == long_value

    @pytest.mark.parametrize(
        ("provided", "resolved"),
        [
            (None, "auto"),
            ("auto", "auto"),
            ("classic", "classic"),
            ("default", "classic"),
            ("crt", "crt"),
        ],
    )
    def test_set_preferred_transfer_client(self, provided: str | None, resolved: str) -> None:
        config_kwargs: dict[str, Any] = {}
        if provided is not None:
            config_kwargs["preferred_transfer_client"] = provided
        runtime_config = build_config_with(**config_kwargs)
        assert runtime_config["preferred_transfer_client"] == resolved

    @pytest.mark.parametrize(
        ("config_name", "provided", "expected"),
        [
            ("max_bandwidth", "1MB/s", 1024 * 1024),
            ("max_bandwidth", "8Mb/s", 1024 * 1024),
            ("max_bandwidth", "1000", 1000),
            ("max_bandwidth", "1000B/s", 1000),
            ("max_bandwidth", "8000b/s", 1000),
            ("target_bandwidth", "5MB/s", 5 * 1024 * 1024),
            ("target_bandwidth", "1Mb/s", 1 * 1024 * 1024 / 8),
            ("target_bandwidth", "1000", 1000),
            ("target_bandwidth", "1000B/s", 1000),
            ("target_bandwidth", "8000b/s", 1000),
            ("disk_throughput", "1MB/s", 1024 * 1024),
            ("disk_throughput", "10Mb/s", 10 * 1024 * 1024 / 8),
            ("disk_throughput", "1000", 1000),
            ("disk_throughput", "1000B/s", 1000),
            ("disk_throughput", "8000b/s", 1000),
        ],
    )
    def test_rate_conversions(self, config_name: str, provided: str, expected: float) -> None:
        runtime_config = build_config_with(**{config_name: provided})
        assert runtime_config[config_name] == expected

    @pytest.mark.parametrize(
        ("config_name", "provided"),
        [
            (name, value)
            for name in ("max_bandwidth", "target_bandwidth", "disk_throughput")
            for value in ("1MB", "1B", "1b", "100/s", "", "value-with-no-digits")
        ],
    )
    def test_invalid_rate_values(self, config_name: str, provided: str) -> None:
        with pytest.raises(InvalidConfigError):
            build_config_with(**{config_name: provided})

    def test_validates_preferred_transfer_client_choices(self) -> None:
        with pytest.raises(InvalidConfigError):
            build_config_with(preferred_transfer_client="not-supported")

    @pytest.mark.parametrize(
        ("attr", "val", "expected"),
        [
            ("should_stream", "true", True),
            ("should_stream", "false", False),
            ("should_stream", None, None),
            ("direct_io", "true", True),
            ("direct_io", "false", False),
            ("direct_io", None, None),
        ],
    )
    def test_convert_booleans(self, attr: str, val: str | None, expected: bool | None) -> None:
        runtime_config = build_config_with(**{attr: val})
        assert runtime_config[attr] == expected


class TestErrorWording:
    """Byte-exact aws wording (each exits 255 over there)."""

    def test_invalid_choice_message(self) -> None:
        with pytest.raises(InvalidConfigError) as exc_info:
            build_config_with(preferred_transfer_client="bogus")
        assert str(exc_info.value) == (
            'Invalid value: "bogus" for configuration option: '
            '"preferred_transfer_client". Supported values are: auto, classic, crt'
        )

    def test_positive_integer_message(self) -> None:
        with pytest.raises(InvalidConfigError) as exc_info:
            build_config_with(max_queue_size="0")
        assert str(exc_info.value) == "Value for max_queue_size must be a positive integer: 0"

    def test_invalid_size_message(self) -> None:
        with pytest.raises(InvalidConfigError) as exc_info:
            build_config_with(multipart_threshold="10XB")
        assert str(exc_info.value) == "Invalid size value: 10xb"


class TestLoadScopedS3Config:
    def _config(self, profile: str | None = None):
        return S3(session=boto3.Session(profile_name=profile)).aws_config()

    def _write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> None:
        config = tmp_path / "config"
        config.write_text(content)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))

    def test_reads_the_nested_s3_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write(
            tmp_path,
            monkeypatch,
            "[default]\ns3 =\n  preferred_transfer_client = crt\n  multipart_threshold = 10MB\n",
        )
        scoped = runtimeconfig.load_scoped_s3_config(self._config())
        assert scoped == {
            "preferred_transfer_client": "crt",
            "multipart_threshold": "10MB",
        }

    def test_reads_from_the_s3_instances_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write(
            tmp_path,
            monkeypatch,
            "[default]\ns3 =\n  preferred_transfer_client = crt\n"
            "[profile alt]\ns3 =\n  preferred_transfer_client = classic\n",
        )
        scoped = runtimeconfig.load_scoped_s3_config(self._config("alt"))

        assert scoped == {"preferred_transfer_client": "classic"}

    def test_missing_section_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write(tmp_path, monkeypatch, "[default]\nregion = us-east-1\n")
        assert runtimeconfig.load_scoped_s3_config(self._config()) == {}

    def test_profile_selects_its_own_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write(
            tmp_path,
            monkeypatch,
            "[default]\ns3 =\n  preferred_transfer_client = crt\n"
            "[profile alt]\ns3 =\n  preferred_transfer_client = classic\n",
        )
        scoped = runtimeconfig.load_scoped_s3_config(self._config("alt"))
        assert scoped == {"preferred_transfer_client": "classic"}

    def test_bound_session_wins_over_ambient_profile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Runtime config must not resolve a second ambient session. The S3-bound
        # session is authoritative even when the process environment names a
        # different profile.
        self._write(
            tmp_path,
            monkeypatch,
            "[profile aws_profile]\ns3 =\n  preferred_transfer_client = crt\n"
            "[profile aws_default_profile]\ns3 =\n  preferred_transfer_client = classic\n",
        )
        monkeypatch.setenv("AWS_PROFILE", "aws_profile")
        monkeypatch.setenv("AWS_DEFAULT_PROFILE", "aws_default_profile")
        scoped = runtimeconfig.load_scoped_s3_config(self._config("aws_profile"))
        assert scoped == {"preferred_transfer_client": "crt"}
