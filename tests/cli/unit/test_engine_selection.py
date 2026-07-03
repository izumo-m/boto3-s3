"""Transfer-engine selection: the aws-cli factory decision tree + ``[s3]`` wiring.

``resolve_transfer_client`` ports ``TransferManagerFactory``'s
``_compute_transfer_client_type`` (aws-cli ``test_factory.py``'s
``test_transfer_manager_cls_resolution`` matrix). ``build_transfer_config``
turns the parsed ``[s3]`` config into the library ``TransferConfig`` the CLI
hands to ``S3``. ``TestWiring`` runs the whole path through ``main`` with a
recording client and a temp ``AWS_CONFIG_FILE`` to prove the config reaches
the engine.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

from boto3_s3 import ConfigurationError
from boto3_s3 import crtsupport as crtsupport
from boto3_s3_cli import runtimeconfig
from boto3_s3_cli.cli import exit_code_for
from boto3_s3_cli.commands import transferargs
from boto3_s3_cli.commands.base import Context
from tests.utils.harness import run_cli_in_process
from tests.utils.recorder import ApiCall, make_recording_client

_MIB = 1024 * 1024


def _runtime_config(**overrides: Any) -> dict[str, Any]:
    return runtimeconfig.RuntimeConfig().build_config(**overrides)


class TestResolveTransferClient:
    """aws-cli ``test_transfer_manager_cls_resolution`` (alias/None pre-resolved)."""

    @pytest.mark.parametrize(
        ("preferred", "paths_type", "optimized", "lock_free", "expected"),
        [
            # Non-optimized host: only an explicit 'crt' opts in.
            ("auto", "locals3", False, True, "classic"),
            ("classic", "locals3", False, True, "classic"),
            ("crt", "locals3", False, True, "crt"),
            # Optimized host with the lock free: auto upgrades to CRT.
            ("auto", "locals3", True, True, "crt"),
            ("classic", "locals3", True, True, "classic"),
            ("crt", "locals3", True, True, "crt"),
            # Optimized but another process holds the CRT lock: auto stays classic,
            # explicit crt still forces CRT (aws-cli acquires-but-proceeds).
            ("auto", "locals3", True, False, "classic"),
            ("classic", "locals3", True, False, "classic"),
            ("crt", "locals3", True, False, "crt"),
            # s3->s3 copy is unconditionally classic (CRT has no copy).
            ("auto", "s3s3", True, True, "classic"),
            ("crt", "s3s3", True, True, "classic"),
            # Downloads behave like uploads (any non-s3s3 route).
            ("crt", "s3local", False, True, "crt"),
        ],
    )
    def test_matrix(
        self,
        monkeypatch: pytest.MonkeyPatch,
        preferred: str,
        paths_type: str,
        optimized: bool,
        lock_free: bool,
        expected: str,
    ) -> None:
        monkeypatch.setattr(crtsupport, "is_optimized_for_system", lambda: optimized)
        monkeypatch.setattr(crtsupport, "acquire_process_lock", lambda: lock_free)
        runtime_config = _runtime_config(preferred_transfer_client=preferred)
        assert (
            runtimeconfig.resolve_transfer_client(runtime_config, paths_type=paths_type) == expected
        )

    def test_default_resolves_to_classic_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The 'default' alias is resolved to 'classic' by RuntimeConfig before
        # this function ever sees it.
        monkeypatch.setattr(crtsupport, "is_optimized_for_system", lambda: True)
        monkeypatch.setattr(crtsupport, "acquire_process_lock", lambda: True)
        runtime_config = _runtime_config(preferred_transfer_client="default")
        assert runtimeconfig.resolve_transfer_client(runtime_config, paths_type="locals3") == (
            "classic"
        )

    def test_auto_does_not_acquire_the_lock_on_a_non_optimized_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(crtsupport, "is_optimized_for_system", lambda: False)

        def fail() -> bool:
            raise AssertionError("lock acquired without an optimized host")

        monkeypatch.setattr(crtsupport, "acquire_process_lock", fail)
        runtime_config = _runtime_config(preferred_transfer_client="auto")
        assert runtimeconfig.resolve_transfer_client(runtime_config, paths_type="locals3") == (
            "classic"
        )


class TestExplicitCrtDegradation:
    def test_missing_awscrt_raises_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: False)
        runtime_config = _runtime_config(preferred_transfer_client="crt")
        with pytest.raises(ConfigurationError) as exc_info:
            runtimeconfig.resolve_transfer_client(runtime_config, paths_type="locals3")
        assert "boto3-s3-cli[crt]" in str(exc_info.value)
        # The PLAIN ConfigurationError, not the InvalidConfigError refinement:
        # this lane is the documented rc 253 (crt.md section 4), and the
        # subclass would silently remap it to 255.
        assert type(exc_info.value) is ConfigurationError
        assert exit_code_for(exc_info.value) == 253

    def test_s3s3_with_missing_awscrt_is_classic_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # s3->s3 short-circuits to classic before the awscrt check.
        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: False)
        runtime_config = _runtime_config(preferred_transfer_client="crt")
        assert runtimeconfig.resolve_transfer_client(runtime_config, paths_type="s3s3") == "classic"

    def test_explicit_crt_with_old_s3transfer_raises_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # awscrt present, but the floor s3transfer (< 0.8.0) lacks the CRT
        # surface: a clean ConfigurationError (rc 253), not an ImportError.
        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: True)
        monkeypatch.setattr(crtsupport, "has_crt_s3transfer", lambda: False)
        runtime_config = _runtime_config(preferred_transfer_client="crt")
        with pytest.raises(ConfigurationError) as exc_info:
            runtimeconfig.resolve_transfer_client(runtime_config, paths_type="locals3")
        assert "s3transfer" in str(exc_info.value)
        assert type(exc_info.value) is ConfigurationError  # rc 253, not the 255 refinement
        assert exit_code_for(exc_info.value) == 253

    def test_auto_with_old_s3transfer_degrades_to_classic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: True)
        monkeypatch.setattr(crtsupport, "is_optimized_for_system", lambda: True)
        monkeypatch.setattr(crtsupport, "has_crt_s3transfer", lambda: False)
        runtime_config = _runtime_config(preferred_transfer_client="auto")
        assert (
            runtimeconfig.resolve_transfer_client(runtime_config, paths_type="locals3") == "classic"
        )


class TestBuildTransferConfig:
    def test_only_explicit_keys_reach_the_constructor(self) -> None:
        scoped = {"multipart_threshold": "10MB"}
        runtime_config = _runtime_config(**scoped)
        config = runtimeconfig.build_transfer_config(scoped, runtime_config, "classic")
        assert config.multipart_threshold == 10 * _MIB
        # multipart_chunksize was not set -> boto3's UNSET sentinel is kept so
        # the CRT engine treats it as "use the dynamic part size".
        assert config.get_deep_attr("multipart_chunksize") is config.UNSET_DEFAULT

    def test_preferred_transfer_client_carries_the_resolved_engine(self) -> None:
        config = runtimeconfig.build_transfer_config({}, _runtime_config(), "crt")
        assert config.preferred_transfer_client == "crt"

    def test_max_queue_size_sets_the_request_queue_attribute(self) -> None:
        scoped = {"max_queue_size": "500"}
        config = runtimeconfig.build_transfer_config(scoped, _runtime_config(**scoped), "classic")
        assert config.max_request_queue_size == 500

    def test_max_concurrent_requests_maps_to_concurrency(self) -> None:
        scoped = {"max_concurrent_requests": "20"}
        config = runtimeconfig.build_transfer_config(scoped, _runtime_config(**scoped), "classic")
        assert config.max_request_concurrency == 20

    def test_in_memory_chunk_caps_match_the_awscli_factory(self) -> None:
        config = runtimeconfig.build_transfer_config({}, _runtime_config(), "classic")
        assert config.max_in_memory_upload_chunks == 6
        assert config.max_in_memory_download_chunks == 6

    def test_crt_tuning_fields_pass_through(self) -> None:
        scoped = {"target_bandwidth": "100MB/s", "direct_io": "true"}
        config = runtimeconfig.build_transfer_config(scoped, _runtime_config(**scoped), "crt")
        assert config.target_bandwidth == 100 * _MIB
        assert config.direct_io is True

    def test_crt_drops_classic_only_keys_that_would_trip_validation(self) -> None:
        # aws-cli ignores io_chunksize / max_bandwidth under CRT; placing them
        # on a crt-preferred config would trip boto3's CRT validation (rc 1
        # traceback) where aws exits 0 (charter). They must be left UNSET, while
        # the CRT-consumed multipart_chunksize still flows through.
        scoped = {
            "io_chunksize": "1MB",
            "max_bandwidth": "10MB/s",
            "multipart_chunksize": "16MB",
        }
        config = runtimeconfig.build_transfer_config(scoped, _runtime_config(**scoped), "crt")
        assert config.get_deep_attr("io_chunksize") is config.UNSET_DEFAULT
        assert config.get_deep_attr("max_bandwidth") is config.UNSET_DEFAULT
        assert config.get_deep_attr("multipart_chunksize") == 16 * _MIB
        # The library's CRT validation (boto3's _validate_crt_transfer_config)
        # must accept the resulting config without raising.
        crtsupport._validate_crt_transfer_config(config)

    def test_classic_keeps_io_chunksize_and_max_bandwidth(self) -> None:
        # The same keys are honored verbatim under the classic engine.
        scoped = {"io_chunksize": "1MB", "max_bandwidth": "10MB/s"}
        config = runtimeconfig.build_transfer_config(scoped, _runtime_config(**scoped), "classic")
        assert config.io_chunksize == 1 * _MIB
        assert config.max_bandwidth == 10 * _MIB

    def test_crt_omits_classic_only_attributes(self) -> None:
        # The request queue size and in-memory chunk caps are classic-only
        # tuning aws-cli never applies to the CRT client.
        scoped = {"max_queue_size": "500"}
        config = runtimeconfig.build_transfer_config(scoped, _runtime_config(**scoped), "crt")
        assert config.max_request_queue_size != 500


class TestResolveTransferConfig:
    """The command-facing orchestrator (``transferargs.resolve_transfer_config``)."""

    def _args(self, profile: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(profile=profile)

    def test_injected_config_wins(self) -> None:
        from boto3_s3 import TransferConfig

        injected = TransferConfig(use_threads=False)
        ctx = Context(transfer_config=injected)
        result = transferargs.resolve_transfer_config(self._args(), ctx, paths_type="locals3")
        assert result is injected

    def test_reads_scoped_config_when_not_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text("[default]\ns3 =\n  multipart_threshold = 7\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        ctx = Context()  # no transfer_config
        result = transferargs.resolve_transfer_config(self._args(), ctx, paths_type="locals3")
        assert result.multipart_threshold == 7
        assert result.preferred_transfer_client == "classic"  # conftest pins is_optimized False


class TestWiring:
    """The ``[s3]`` config actually reaching the transfer engine through ``main``."""

    def _ctx(self, parsed_responses: list[Any]) -> tuple[Context, list[ApiCall]]:
        client, calls = make_recording_client(parsed_responses)
        # No transfer_config injected: the scoped-config path runs.
        return Context(client_factory=lambda _args: client), calls

    def test_multipart_threshold_from_s3_config_forces_multipart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text("[default]\ns3 =\n  multipart_threshold = 1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        source = tmp_path / "small.txt"
        source.write_bytes(b"hello")  # 5 bytes: a PutObject by default (8 MiB threshold)
        ctx, calls = self._ctx([{"UploadId": "id"}, {"ETag": '"p1"'}, {}])
        result = run_cli_in_process(["cp", str(source), "s3://bucket/key"], ctx=ctx)
        assert result.rc == 0, (result.stderr, calls)
        # threshold=1 from [s3] turned the 5-byte upload multipart.
        assert [c.operation for c in calls] == [
            "CreateMultipartUpload",
            "UploadPart",
            "CompleteMultipartUpload",
        ]

    def test_default_config_keeps_the_small_upload_single_part(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text("[default]\nregion = us-east-1\n")  # no [s3] section
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        source = tmp_path / "small.txt"
        source.write_bytes(b"hello")
        ctx, calls = self._ctx([{"ETag": '"p1"'}])
        result = run_cli_in_process(["cp", str(source), "s3://bucket/key"], ctx=ctx)
        assert result.rc == 0, (result.stderr, calls)
        assert [c.operation for c in calls] == ["PutObject"]

    def test_invalid_s3_config_value_exits_255_through_main(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Charter: an invalid [s3] value exits 255 through main, like aws
        # with the byte-exact message body. The
        # error is raised parsing [s3] before any API call, so it propagates to
        # main's general handler regardless of the recording client.
        config_file = tmp_path / "config"
        config_file.write_text("[default]\ns3 =\n  preferred_transfer_client = bogus\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        source = tmp_path / "small.txt"
        source.write_bytes(b"hello")
        ctx, _calls = self._ctx([])
        result = run_cli_in_process(["cp", str(source), "s3://bucket/key"], ctx=ctx)
        assert result.rc == 255, (result.rc, result.stderr)
        assert (
            'Invalid value: "bogus" for configuration option: '
            '"preferred_transfer_client". Supported values are: auto, classic, crt'
        ) in result.stderr
