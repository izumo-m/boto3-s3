"""Transfer-engine selection and the ``[s3]`` merge (`boto3_s3.transferconfig`).

``resolve_transfer_client`` ports aws-cli's ``TransferManagerFactory.
_compute_transfer_client_type`` (aws-cli ``test_factory.py``'s
``test_transfer_manager_cls_resolution`` matrix). ``resolve_transfer_config``
turns the caller's config plus the parsed ``[s3]`` section into the config a
run uses; `_build` drives it with a fixed engine.
"""

from __future__ import annotations

import sys
from typing import Any, cast
from unittest import mock

import pytest
from boto3.s3.transfer import TransferConfig as Boto3TransferConfig

from boto3_s3 import ConfigurationError, TransferConfig, transferconfig
from boto3_s3 import crtsupport as crtsupport

_MIB = 1024 * 1024


def _runtime_config(**overrides: Any) -> dict[str, Any]:
    return transferconfig.RuntimeConfig().build_config(**overrides)


class _Scoped:
    """An `AwsConfig` stand-in whose ``[s3]`` section holds *scoped*."""

    def __init__(self, scoped: dict[str, str]) -> None:
        self._scoped = scoped

    def get_str(self, key: str, default: str | None = None) -> str | None:
        return self._scoped.get(key.removeprefix("s3."), default)


def _build(
    scoped: dict[str, str], engine: str, config: TransferConfig | None = None, *, copy: bool = False
) -> Any:
    with mock.patch.object(transferconfig, "resolve_transfer_client", return_value=engine):
        return transferconfig.resolve_transfer_config(
            cast("Any", _Scoped(scoped)), config, copy=copy
        )


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
        assert transferconfig.resolve_transfer_client(preferred, copy=paths_type == "s3s3") == (
            expected
        )

    def test_default_resolves_to_classic_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The 'default' alias is resolved to 'classic' by RuntimeConfig before
        # this function ever sees it.
        monkeypatch.setattr(crtsupport, "is_optimized_for_system", lambda: True)
        monkeypatch.setattr(crtsupport, "acquire_process_lock", lambda: True)
        runtime_config = _runtime_config(preferred_transfer_client="default")
        assert transferconfig.resolve_transfer_client(
            runtime_config["preferred_transfer_client"], copy=False
        ) == ("classic")

    def test_auto_does_not_acquire_the_lock_on_a_non_optimized_host(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(crtsupport, "is_optimized_for_system", lambda: False)

        def fail() -> bool:
            raise AssertionError("lock acquired without an optimized host")

        monkeypatch.setattr(crtsupport, "acquire_process_lock", fail)
        runtime_config = _runtime_config(preferred_transfer_client="auto")
        assert transferconfig.resolve_transfer_client(
            runtime_config["preferred_transfer_client"], copy=False
        ) == ("classic")


class TestExplicitCrtDegradation:
    def test_missing_awscrt_raises_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: False)
        monkeypatch.setitem(sys.modules, "awscrt", None)  # the import raises ImportError
        runtime_config = _runtime_config(preferred_transfer_client="crt")
        with pytest.raises(ConfigurationError) as exc_info:
            transferconfig.resolve_transfer_client(
                runtime_config["preferred_transfer_client"], copy=False
            )
        assert "boto3-s3[crt]" in str(exc_info.value)
        # The plain ConfigurationError, not the InvalidConfigError refinement:
        # the CLI maps this lane to rc 253 (crt.md section 4).
        assert type(exc_info.value) is ConfigurationError

    def test_too_old_awscrt_names_the_boto3_upgrade(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The crt extra of boto3 1.28 - 1.33 installs an awscrt below boto3's
        # CRT minimum: the extra is already there, so the remedy named is a
        # newer boto3, not the extra (same rc 253).
        import awscrt

        monkeypatch.setattr(awscrt, "__version__", "0.16.9")
        runtime_config = _runtime_config(preferred_transfer_client="crt")
        with pytest.raises(ConfigurationError) as exc_info:
            transferconfig.resolve_transfer_client(
                runtime_config["preferred_transfer_client"], copy=False
            )
        message = str(exc_info.value)
        assert "awscrt 0.16.9 is too old" in message and "boto3 >= 1.34.6" in message
        assert "[crt]'" not in message

    def test_s3s3_with_missing_awscrt_is_classic_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # s3->s3 short-circuits to classic before the awscrt check.
        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: False)
        runtime_config = _runtime_config(preferred_transfer_client="crt")
        assert (
            transferconfig.resolve_transfer_client(
                runtime_config["preferred_transfer_client"], copy=True
            )
            == "classic"
        )

    def test_explicit_crt_with_old_s3transfer_raises_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # awscrt present, but the floor s3transfer (< 0.8.0) lacks the CRT
        # surface: a clean ConfigurationError (rc 253), not an ImportError.
        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: True)
        monkeypatch.setattr(crtsupport, "has_crt_s3transfer", lambda: False)
        runtime_config = _runtime_config(preferred_transfer_client="crt")
        with pytest.raises(ConfigurationError) as exc_info:
            transferconfig.resolve_transfer_client(
                runtime_config["preferred_transfer_client"], copy=False
            )
        assert "s3transfer" in str(exc_info.value)
        assert type(exc_info.value) is ConfigurationError  # rc 253, not the 255 refinement

    def test_auto_with_old_s3transfer_degrades_to_classic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: True)
        monkeypatch.setattr(crtsupport, "is_optimized_for_system", lambda: True)
        monkeypatch.setattr(crtsupport, "has_crt_s3transfer", lambda: False)
        runtime_config = _runtime_config(preferred_transfer_client="auto")
        assert (
            transferconfig.resolve_transfer_client(
                runtime_config["preferred_transfer_client"], copy=False
            )
            == "classic"
        )


class TestBuildTransferConfig:
    def test_only_explicit_keys_reach_the_constructor(self) -> None:
        scoped = {"multipart_threshold": "10MB"}
        config = _build(scoped, "classic")
        assert config.multipart_threshold == 10 * _MIB
        # multipart_chunksize keeps boto3's UNSET sentinel - what lets the CRT
        # engine use its dynamic part size.
        assert config.get_deep_attr("multipart_chunksize") is config.UNSET_DEFAULT
        # And every other constructor key sits at a fresh constructor's own
        # default (UNSET where boto3 uses the sentinel, None elsewhere).
        base = type(config)()
        ctor_keys = transferconfig._CTOR_KEYS  # pyright: ignore[reportPrivateUsage]
        for rc_key, ctor_key in ctor_keys.items():
            if rc_key in scoped:
                continue
            assert config.get_deep_attr(ctor_key) == base.get_deep_attr(ctor_key), ctor_key

    def test_preferred_transfer_client_carries_the_resolved_engine(self) -> None:
        config = _build({}, "crt")
        assert config.preferred_transfer_client == "crt"

    def test_max_queue_size_sets_the_request_queue_attribute(self) -> None:
        scoped = {"max_queue_size": "500"}
        config = _build(scoped, "classic")
        assert config.max_request_queue_size == 500

    def test_max_concurrent_requests_maps_to_concurrency(self) -> None:
        scoped = {"max_concurrent_requests": "20"}
        config = _build(scoped, "classic")
        assert config.max_request_concurrency == 20

    def test_in_memory_chunk_caps_match_the_awscli_factory(self) -> None:
        config = _build({}, "classic")
        assert config.max_in_memory_upload_chunks == 6
        assert config.max_in_memory_download_chunks == 6

    def test_download_io_queue_depth_matches_awscli(self) -> None:
        # aws-cli's bundled s3transfer defaults max_io_queue_size to 1000 and
        # no [s3] key maps to it, so aws always runs there. The library's
        # TransferConfig already defaults to that depth (where boto3 alone would
        # have dialed it to 100), so the CLI inherits it without a pin.
        config = _build({}, "classic")
        assert config.max_io_queue_size == 1000

    def test_crt_tuning_fields_pass_through(self) -> None:
        scoped = {"target_bandwidth": "100MB/s", "direct_io": "true"}
        config = _build(scoped, "crt")
        assert config.target_bandwidth == 100 * _MIB
        assert config.direct_io is True

    def test_crt_drops_classic_only_keys_that_would_trip_validation(self) -> None:
        # aws-cli ignores io_chunksize / max_bandwidth under CRT; placing them
        # on a crt-preferred config would trip boto3's CRT validation (rc 1
        # traceback) where aws exits 0. They must be left UNSET, while
        # the CRT-consumed multipart_chunksize still flows through.
        scoped = {
            "io_chunksize": "1MB",
            "max_bandwidth": "10MB/s",
            "multipart_chunksize": "16MB",
        }
        config = _build(scoped, "crt")
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
        config = _build(scoped, "crt")
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
        config = _build(scoped, "crt")
        assert config.multipart_threshold == 5 * _MIB
        assert config.multipart_chunksize == _MIB
        crtsupport._validate_crt_transfer_config(config)

    def test_crt_without_an_explicit_chunksize_leaves_the_threshold_unset(self) -> None:
        # No pin without an explicit chunksize: the stamped 8 MiB resolved
        # default equals aws-c-s3's default part size - the same effective
        # cutoff aws gets with its dynamic part sizing.
        scoped = {"multipart_threshold": "10MB"}
        config = _build(scoped, "crt")
        assert config.get_deep_attr("multipart_threshold") is config.UNSET_DEFAULT

    def test_classic_keeps_an_explicit_threshold_alongside_a_chunksize(self) -> None:
        # The threshold pin is CRT-only: under the classic engine both keys
        # are aws's own [s3] knobs and ride verbatim (aws-cli's translation
        # map), so an explicit multipart_threshold must survive an explicit
        # chunksize instead of being overwritten by the pin.
        scoped = {"multipart_chunksize": "64MB", "multipart_threshold": "10MB"}
        config = _build(scoped, "classic")
        assert config.multipart_threshold == 10 * _MIB
        assert config.multipart_chunksize == 64 * _MIB

    def test_classic_keeps_io_chunksize_and_max_bandwidth(self) -> None:
        # The same keys are honored verbatim under the classic engine.
        scoped = {"io_chunksize": "1MB", "max_bandwidth": "10MB/s"}
        config = _build(scoped, "classic")
        assert config.io_chunksize == 1 * _MIB
        assert config.max_bandwidth == 10 * _MIB

    def test_crt_omits_classic_only_attributes(self) -> None:
        # The request queue size and the in-memory chunk caps are classic-only
        # tuning aws-cli never applies to the CRT client.
        scoped = {"max_queue_size": "500"}
        config = _build(scoped, "crt")
        # All three knobs sit at a fresh constructor's own defaults: the scoped
        # queue size was not applied and the classic pins did not run.
        base = type(config)()
        assert config.max_request_queue_size == base.max_request_queue_size
        assert config.max_in_memory_upload_chunks == base.max_in_memory_upload_chunks
        assert config.max_in_memory_download_chunks == base.max_in_memory_download_chunks


class TestCallerSettings:
    """What the caller gave wins over ``[s3]``; the rest comes from it."""

    def test_a_given_setting_wins_over_the_section(self) -> None:
        config = _build(
            {"max_concurrent_requests": "20", "multipart_chunksize": "16MB"},
            "classic",
            TransferConfig(max_concurrency=4),
        )
        assert config.max_request_concurrency == 4
        assert config.multipart_chunksize == 16 * _MIB

    def test_settings_the_section_has_no_key_for_carry_over(self) -> None:
        config = _build({}, "classic", TransferConfig(use_threads=False, num_download_attempts=2))
        assert config.use_threads is False
        assert config.num_download_attempts == 2

    def test_attributes_set_after_construction_carry_over(self) -> None:
        given = TransferConfig()
        given.max_in_memory_upload_chunks = 20
        config = _build({}, "classic", given)
        assert config.max_in_memory_upload_chunks == 20
        assert config.max_in_memory_download_chunks == 6

    def test_a_setting_assigned_after_construction_counts_as_given(self) -> None:
        given = TransferConfig()
        given.use_threads = False
        given.max_request_concurrency = 3
        given.target_bandwidth = 5
        config = _build(
            {"max_concurrent_requests": "20", "target_bandwidth": "9"}, "classic", given
        )
        assert config.use_threads is False
        assert config.max_request_concurrency == 3
        assert config.target_bandwidth == 5

    def test_a_subclass_and_its_own_attributes_survive(self) -> None:
        class Tuned(TransferConfig):
            pass

        given = Tuned(max_concurrency=4)
        given.note = "kept"  # pyright: ignore[reportAttributeAccessIssue]
        config = _build({}, "classic", given)
        assert type(config) is Tuned
        assert config.note == "kept"
        assert config is not given  # a copy: the caller's object is left alone

    @pytest.mark.parametrize("asked", [False, True])
    def test_classic_only_settings_fail_only_an_asked_for_crt(self, asked: bool) -> None:
        # boto3 rejects classic-only settings under an explicit 'crt'; a crt
        # chosen by auto or by [s3] leaves them to the engine, which ignores
        # them, as boto3's auto does.
        given = (
            TransferConfig(io_chunksize=_MIB, preferred_transfer_client="crt")
            if asked
            else TransferConfig(io_chunksize=_MIB)
        )
        config = _build({}, "crt", given)
        if asked:
            with pytest.raises(Exception, match="io_chunksize"):
                crtsupport._validate_crt_transfer_config(config)  # pyright: ignore[reportPrivateUsage]
        else:
            crtsupport._validate_crt_transfer_config(config)  # pyright: ignore[reportPrivateUsage]
            assert config.io_chunksize == _MIB

    def test_a_given_preference_is_resolved_like_the_sections(self) -> None:
        seen: list[tuple[str, bool]] = []

        def pick(preferred: str, *, copy: bool) -> str:
            seen.append((preferred, copy))
            return "classic"

        with mock.patch.object(transferconfig, "resolve_transfer_client", side_effect=pick):
            transferconfig.resolve_transfer_config(
                cast("Any", _Scoped({"preferred_transfer_client": "crt"})),
                TransferConfig(preferred_transfer_client="auto"),
                copy=True,
            )
        assert seen == [("auto", True)]

    def test_a_plain_boto3_config_is_used_as_it_is(self) -> None:
        plain = Boto3TransferConfig(max_concurrency=3)
        assert _build({"max_concurrent_requests": "20"}, "crt", cast("Any", plain)) is plain

    def test_a_resolved_config_passes_through(self) -> None:
        resolved = _build({"max_concurrent_requests": "20"}, "classic")
        assert _build({"max_concurrent_requests": "30"}, "crt", resolved) is resolved

    def test_an_invalid_section_value_is_refused(self) -> None:
        from boto3_s3 import InvalidConfigError

        with pytest.raises(InvalidConfigError, match="must be a positive integer"):
            _build({"max_concurrent_requests": "0"}, "classic")
