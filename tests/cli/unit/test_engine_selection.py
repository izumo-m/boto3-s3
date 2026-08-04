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

import boto3
import pytest

from boto3_s3 import S3, ConfigurationError
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
        # multipart_chunksize keeps boto3's UNSET sentinel - what lets the CRT
        # engine use its dynamic part size.
        assert config.get_deep_attr("multipart_chunksize") is config.UNSET_DEFAULT
        # And every other constructor key sits at a fresh constructor's own
        # default (UNSET where boto3 uses the sentinel, None elsewhere).
        base = type(config)()
        ctor_keys = runtimeconfig._TRANSFER_CONFIG_CTOR_KEYS  # pyright: ignore[reportPrivateUsage]
        for rc_key, ctor_key in ctor_keys.items():
            if rc_key in scoped:
                continue
            assert config.get_deep_attr(ctor_key) == base.get_deep_attr(ctor_key), ctor_key

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

    def test_download_io_queue_depth_matches_awscli(self) -> None:
        # aws-cli's bundled s3transfer defaults max_io_queue_size to 1000 and
        # no [s3] key maps to it, so aws always runs there. The library's
        # TransferConfig already defaults to that depth (where boto3 alone would
        # have dialed it to 100), so the CLI inherits it without a pin.
        config = runtimeconfig.build_transfer_config({}, _runtime_config(), "classic")
        assert config.max_io_queue_size == 1000

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

    def test_crt_pins_the_threshold_to_an_explicit_chunksize(self) -> None:
        # aws-cli sends no per-request threshold under CRT and aws-c-s3 falls
        # back to the client part size, so an explicit multipart_chunksize IS
        # aws's effective threshold - while the installed s3transfer stamps
        # the config's *resolved* threshold onto every CRT put (8 MiB default
        # if left unset, which would multipart a 16 MiB file aws single-puts
        # at chunksize 64MB). The [s3] multipart_threshold key itself stays
        # ignored under CRT, exactly like aws.
        scoped = {"multipart_chunksize": "64MB", "multipart_threshold": "10MB"}
        config = runtimeconfig.build_transfer_config(scoped, _runtime_config(**scoped), "crt")
        assert config.multipart_threshold == 64 * _MIB
        assert config.multipart_chunksize == 64 * _MIB
        crtsupport._validate_crt_transfer_config(config)

    def test_crt_threshold_pin_never_drops_below_the_5_mib_part_floor(self) -> None:
        # aws-c-s3's fallback is max(part size, 5 MiB), not the part size, so a
        # chunksize under S3's minimum part size leaves aws single-putting up
        # to 5 MiB. Pinning the raw chunksize would multipart a 3 MiB file aws
        # sends as one PutObject; the part size itself still carries the
        # configured value (aws passes it to the client verbatim).
        scoped = {"multipart_chunksize": "1MB"}
        config = runtimeconfig.build_transfer_config(scoped, _runtime_config(**scoped), "crt")
        assert config.multipart_threshold == 5 * _MIB
        assert config.multipart_chunksize == _MIB
        crtsupport._validate_crt_transfer_config(config)

    def test_crt_without_an_explicit_chunksize_leaves_the_threshold_unset(self) -> None:
        # No pin without an explicit chunksize: the stamped 8 MiB resolved
        # default equals aws-c-s3's default part size - the same effective
        # cutoff aws gets with its dynamic part sizing.
        scoped = {"multipart_threshold": "10MB"}
        config = runtimeconfig.build_transfer_config(scoped, _runtime_config(**scoped), "crt")
        assert config.get_deep_attr("multipart_threshold") is config.UNSET_DEFAULT

    def test_classic_keeps_io_chunksize_and_max_bandwidth(self) -> None:
        # The same keys are honored verbatim under the classic engine.
        scoped = {"io_chunksize": "1MB", "max_bandwidth": "10MB/s"}
        config = runtimeconfig.build_transfer_config(scoped, _runtime_config(**scoped), "classic")
        assert config.io_chunksize == 1 * _MIB
        assert config.max_bandwidth == 10 * _MIB

    def test_crt_omits_classic_only_attributes(self) -> None:
        # The request queue size and the in-memory chunk caps are classic-only
        # tuning aws-cli never applies to the CRT client.
        scoped = {"max_queue_size": "500"}
        config = runtimeconfig.build_transfer_config(scoped, _runtime_config(**scoped), "crt")
        # All three knobs sit at a fresh constructor's own defaults: the scoped
        # queue size was not applied and the classic pins did not run.
        base = type(config)()
        assert config.max_request_queue_size == base.max_request_queue_size
        assert config.max_in_memory_upload_chunks == base.max_in_memory_upload_chunks
        assert config.max_in_memory_download_chunks == base.max_in_memory_download_chunks


class TestResolveTransferConfig:
    """The command-facing orchestrator (``transferargs.resolve_transfer_config``)."""

    def _args(self, profile: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(profile=profile)

    def test_injected_config_wins(self) -> None:
        from boto3_s3 import TransferConfig

        injected = TransferConfig(use_threads=False)
        ctx = Context(transfer_config=injected)
        result = transferargs.resolve_transfer_config(ctx, S3(), paths_type="locals3")
        assert result is injected

    def test_reads_scoped_config_when_not_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text("[default]\ns3 =\n  multipart_threshold = 7\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        ctx = Context()  # no transfer_config
        s3 = S3(session=boto3.Session())
        result = transferargs.resolve_transfer_config(ctx, s3, paths_type="locals3")
        assert result.multipart_threshold == 7
        assert result.preferred_transfer_client == "classic"  # conftest pins is_optimized False


class TestWiring:
    """The ``[s3]`` config actually reaching the transfer engine through ``main``."""

    def _ctx(self, parsed_responses: list[Any]) -> tuple[Context, list[ApiCall]]:
        client, calls = make_recording_client(parsed_responses)
        # No transfer_config injected: the scoped-config path runs.
        return Context(client_factory=lambda _args: client), calls

    def test_client_and_runtime_config_share_the_context_s3_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text(
            "[default]\ns3 =\n  multipart_threshold = 8MB\n"
            "[profile bound]\ns3 =\n  multipart_threshold = 1\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        source = tmp_path / "small.txt"
        source.write_bytes(b"hello")
        client, calls = make_recording_client([{"UploadId": "id"}, {"ETag": '"p1"'}, {}])

        class BoundS3(S3):
            def client(self) -> Any:
                return client

        s3 = BoundS3(session=boto3.Session(profile_name="bound"))

        import botocore.session

        def unexpected_session(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the command must reuse the session bound to S3")

        monkeypatch.setattr(botocore.session, "Session", unexpected_session)
        ctx = Context(s3_factory=lambda _args: s3)

        result = run_cli_in_process(["cp", str(source), "s3://bucket/key"], ctx=ctx)

        assert result.rc == 0, (result.stderr, calls)
        assert [call.operation for call in calls] == [
            "CreateMultipartUpload",
            "UploadPart",
            "CompleteMultipartUpload",
        ]

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


_INVALID_S3_VALUE = (
    'boto3-s3: [ERROR]: Invalid value: "bogus" for configuration option: '
    '"preferred_transfer_client". Supported values are: auto, classic, crt\n'
)

# aws's general handler formats a bare AssertionError - awscrt's region check -
# as the prefix alone, with no trailing space (aws-cli errorformat.py).
_EMPTY_REPORT = "boto3-s3: [ERROR]:\n"


def _config_ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> tuple[Context, list[ApiCall]]:
    """A Context on a recording client scripted with nothing, plus a temp ``[s3]``.

    The rows below all fail before any request, so an exhausted recorder is the
    guard: a row that slipped past the config would call the client and the root
    conftest would fail the test.
    """
    config_file = tmp_path / "config"
    config_file.write_text(body)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
    client, calls = make_recording_client([])
    return Context(client_factory=lambda _args: client), calls


class TestRmReadsTheRuntimeConfig:
    """``rm`` is an ``S3TransferCommand`` in aws, so it reads ``[s3]`` like cp.

    Measured on the pinned aws-cli: an invalid ``[s3]`` value is 255 for every
    ``rm`` form - the bucket-less enumerating ones and ``--quiet`` included,
    since the report comes from the error handler and not from the result
    printer - but it still *loses* to the path-type 252, which aws checks in
    ``add_paths``, one step earlier.
    """

    _BAD = "[default]\nregion = us-east-1\ns3 =\n  preferred_transfer_client = bogus\n"

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["rm", "s3://bucket/key"], id="single"),
            pytest.param(["rm", "s3://bucket/p/", "--recursive"], id="recursive"),
            pytest.param(["rm", "s3://bucket/key", "--dryrun"], id="dryrun"),
            pytest.param(["rm", "s3://bucket/key", "--quiet"], id="quiet"),
            pytest.param(["rm", "s3://"], id="bucket-less-keyless"),
            pytest.param(["rm", "s3://", "--recursive"], id="bucket-less-recursive"),
        ],
    )
    def test_an_invalid_value_is_255(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
    ) -> None:
        ctx, _calls = _config_ctx(tmp_path, monkeypatch, self._BAD)
        result = run_cli_in_process(argv, ctx=ctx)
        assert (result.rc, result.stderr) == (255, _INVALID_S3_VALUE)

    def test_the_path_type_check_still_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The negative control: aws's check_path_type runs in add_paths, before
        # the runtime config is read, so a local path stays the usage 252.
        ctx, _calls = _config_ctx(tmp_path, monkeypatch, self._BAD)
        result = run_cli_in_process(["rm", "/local/path"], ctx=ctx)
        assert result.rc == 252
        assert "Error: Invalid argument type" in result.stderr

    def test_a_valid_config_leaves_the_run_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text(
            "[default]\nregion = us-east-1\ns3 =\n  preferred_transfer_client = classic\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        client, calls = make_recording_client([{}])
        ctx = Context(client_factory=lambda _args: client)
        result = run_cli_in_process(["rm", "s3://bucket/key"], ctx=ctx)
        assert (result.rc, result.stdout) == (0, "delete: s3://bucket/key\n")
        assert [call.operation for call in calls] == ["DeleteObject"]


class TestCrtEngineConstructionSlot:
    """The transfer manager is built where aws builds it, ``rm`` included.

    aws's ``_get_transfer_manager`` runs right after the ``[s3]`` read and
    before anything about the run is decided, so a CRT selection pays the whole
    construction even for a ``--dryrun``, and even for ``rm``, whose deletes
    never ride the engine here (design/crt.md section 6). When no region
    resolves, that construction is awscrt's ``assert isinstance(region, str)``:
    a bare ``AssertionError`` that aws's general handler renders as an empty
    rc-255 report.

    awscrt is faked here (the suite must run without it, and a real CRT client
    would hold the host-wide process lock for the rest of the session): the
    construction itself is stubbed to raise the same bare ``AssertionError``.
    `tests/lib/test_crtsupport.py` pins that the declared region really reaches
    ``create_s3_crt_client``, and `tests/cli/functional/test_crt_no_region.py`
    runs the whole thing against the real awscrt.
    """

    _CRT = "[default]\ns3 =\n  preferred_transfer_client = crt\n"

    @pytest.fixture(autouse=True)
    def _no_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The real shape of these rows: nothing anywhere resolves a region, so
        # the CLI declares None and awscrt is the thing that refuses. Only that
        # refusal is faked below.
        for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    @pytest.fixture
    def crt_constructions(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        """Fake a usable awscrt and record every CRT construction attempt."""
        seen: list[dict[str, Any]] = []

        def create(client: Any, config: Any, **kwargs: Any) -> Any:
            seen.append({"client": client, "config": config, **kwargs})
            raise AssertionError  # awscrt's own, message-less

        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: True)
        monkeypatch.setattr(crtsupport, "has_crt_s3transfer", lambda: True)
        monkeypatch.setattr(crtsupport, "acquire_process_lock", lambda: True)
        monkeypatch.setattr(crtsupport, "should_use_crt", lambda preferred: preferred == "crt")
        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", create)
        return seen

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["cp", "{file}", "s3://bucket/key"], id="cp-upload"),
            pytest.param(["cp", "s3://bucket/key", "{dir}/dl.txt"], id="cp-download"),
            pytest.param(["cp", "{file}", "s3://bucket/key", "--dryrun"], id="cp-dryrun"),
            pytest.param(["mv", "{file}", "s3://bucket/key"], id="mv"),
            pytest.param(["sync", "{dir}", "s3://bucket/p"], id="sync"),
            pytest.param(["rm", "s3://bucket/key"], id="rm"),
            pytest.param(["rm", "s3://bucket/key", "--dryrun"], id="rm-dryrun"),
            pytest.param(["rm", "s3://bucket/key", "--quiet"], id="rm-quiet"),
            pytest.param(["rm", "s3://"], id="rm-bucket-less"),
        ],
    )
    def test_a_construction_failure_is_the_empty_255_report(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        crt_constructions: list[dict[str, Any]],
        argv: list[str],
    ) -> None:
        source = tmp_path / "f.txt"
        source.write_bytes(b"x")
        ctx, _calls = _config_ctx(tmp_path, monkeypatch, self._CRT)
        resolved = [token.format(file=source, dir=tmp_path) for token in argv]
        result = run_cli_in_process(resolved, ctx=ctx)
        # --quiet suppresses the result printer's lines, never the handler's
        # report; --dryrun never gets to record anything.
        assert (result.rc, result.stdout, result.stderr) == (255, "", _EMPTY_REPORT), resolved
        assert len(crt_constructions) == 1, resolved

    @pytest.mark.parametrize("command", ["cp", "mv"])
    def test_it_also_precedes_the_case_conflict_rejection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        crt_constructions: list[dict[str, Any]],
        command: str,
    ) -> None:
        # The far end of aws's step 5 -> step 6 boundary: `--case-conflict
        # skip` against an S3 Express source is rejected in aws's
        # CommandArchitecture, *after* the transfer manager is built, so the
        # construction failure wins (255 empty report, not the 252). This is
        # the pairing that pins the construction's position after the
        # `[s3]` read and before the run - moving it one step later would
        # silently reintroduce a 252 here.
        ctx, _calls = _config_ctx(tmp_path, monkeypatch, self._CRT)
        argv = [
            command,
            "s3://bucket--use1-az1--x-s3/p",
            str(tmp_path / "dl"),
            "--recursive",
            "--case-conflict",
            "skip",
        ]
        result = run_cli_in_process(argv, ctx=ctx)
        assert (result.rc, result.stderr) == (255, _EMPTY_REPORT)
        assert len(crt_constructions) == 1

    def test_the_case_conflict_rejection_stands_without_the_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crt_constructions: list[Any]
    ) -> None:
        # The control for the pair above: with a classic engine nothing
        # preempts it, so the same argv is the 252 it has always been.
        body = "[default]\ns3 =\n  preferred_transfer_client = classic\n"
        ctx, _calls = _config_ctx(tmp_path, monkeypatch, body)
        argv = [
            "cp",
            "s3://bucket--use1-az1--x-s3/p",
            str(tmp_path / "dl"),
            "--recursive",
            "--case-conflict",
            "skip",
        ]
        result = run_cli_in_process(argv, ctx=ctx)
        assert result.rc == 252
        assert "is not a valid value for `--case-conflict`" in result.stderr
        assert crt_constructions == []

    def test_an_s3s3_route_never_reaches_the_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crt_constructions: list[Any]
    ) -> None:
        # aws's _compute_transfer_client_type forces classic for s3s3 (the CRT
        # manager has no copy), so the construction is not attempted at all.
        config_file = tmp_path / "config"
        config_file.write_text(self._CRT)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        client, calls = make_recording_client([{"ContentLength": 1, "ETag": '"e"'}, {}])
        ctx = Context(client_factory=lambda _args: client, transfer_config=None)
        result = run_cli_in_process(["cp", "s3://bucket/a", "s3://bucket/b"], ctx=ctx)
        assert result.rc == 0, (result.stderr, calls)
        assert crt_constructions == []

    def test_a_classic_selection_never_reaches_the_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crt_constructions: list[Any]
    ) -> None:
        body = "[default]\ns3 =\n  preferred_transfer_client = classic\n"
        ctx, _calls = _config_ctx(tmp_path, monkeypatch, body)
        result = run_cli_in_process(["rm", "s3://bucket/key", "--dryrun"], ctx=ctx)
        assert (result.rc, result.stdout) == (0, "(dryrun) delete: s3://bucket/key\n")
        assert crt_constructions == []
