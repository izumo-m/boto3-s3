"""`boto3_s3.TransferConfig` - the boto3 subclass with library settings.

Pins the contract docs/crt.md relies on: boto3's constructor surface is
untouched (names, order, defaults, alias attributes, the UNSET sentinel that
distinguishes "explicit" from "defaulted" values), the extras are keyword-only
with `None` defaults, and a plain boto3 `TransferConfig` stays accepted by
readers that fall back via `getattr`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from boto3.s3.transfer import TransferConfig as Boto3TransferConfig

import boto3_s3
from boto3_s3.transferconfig import TransferConfig
from boto3_s3.types import AnnotationCopyMode


class TestSdkFloorCompat:
    def test_unset_base_params_are_omitted_not_forwarded_as_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: forwarding None for an omitted base param overwrites the
        # base ctor's concrete default on the SDK floor (boto3 1.28 - ~1.40), so
        # None reaches s3transfer (a TypeError on the first size comparison, and
        # use_threads=None silently disables threading). Unset base params must
        # be omitted so the base ctor supplies its own default.
        captured: dict[str, object] = {}
        real_init = Boto3TransferConfig.__init__

        def spy(this: object, *args: object, **kwargs: object) -> None:
            captured.clear()
            captured.update(kwargs)
            real_init(this, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Boto3TransferConfig, "__init__", spy)
        TransferConfig(max_concurrency=5)
        assert captured.get("max_concurrency") == 5
        for omitted in (
            "multipart_threshold",
            "multipart_chunksize",
            "num_download_attempts",
            "max_io_queue",
            "io_chunksize",
            "use_threads",
            "max_bandwidth",
        ):
            assert omitted not in captured, f"{omitted} must be omitted, not forwarded as None"


class TestReExport:
    def test_public_name_is_the_subclass(self) -> None:
        assert boto3_s3.TransferConfig is TransferConfig

    def test_subclasses_boto3(self) -> None:
        assert issubclass(TransferConfig, Boto3TransferConfig)

    def test_annotation_copy_mode_is_public(self) -> None:
        assert boto3_s3.AnnotationCopyMode is AnnotationCopyMode


class TestBoto3Surface:
    def test_defaults_match_boto3(self) -> None:
        ours = TransferConfig()
        theirs = Boto3TransferConfig()
        assert ours.multipart_threshold == theirs.multipart_threshold
        assert ours.multipart_chunksize == theirs.multipart_chunksize
        assert ours.max_concurrency == theirs.max_concurrency
        assert ours.preferred_transfer_client == "auto"

    def test_positional_args_keep_boto3_order(self) -> None:
        config = TransferConfig(1, 2, 3)
        assert config.multipart_threshold == 1
        assert config.max_concurrency == 2
        assert config.multipart_chunksize == 3

    def test_alias_attributes_still_work(self) -> None:
        config = TransferConfig(max_concurrency=4)
        assert config.max_request_concurrency == 4
        config.max_concurrency = 7
        assert config.max_request_concurrency == 7

    def test_unset_sentinel_distinguishes_explicit_values(self) -> None:
        # boto3/crt.py uses get_deep_attr to pass part_size only when the
        # user set multipart_chunksize explicitly; the subclass must keep
        # that machinery intact.
        defaulted = TransferConfig()
        explicit = TransferConfig(multipart_chunksize=16 * 1024 * 1024)
        if not hasattr(defaulted, "get_deep_attr"):
            pytest.skip("installed boto3 predates the CRT explicit-value sentinel")
        assert defaulted.get_deep_attr("multipart_chunksize") is TransferConfig.UNSET_DEFAULT
        assert explicit.get_deep_attr("multipart_chunksize") == 16 * 1024 * 1024
        # Reads still resolve the default through __getattribute__.
        assert defaulted.multipart_chunksize == 8 * 1024 * 1024


class TestPreferredTransferClient:
    def test_explicit_value_is_readable(self) -> None:
        # The engine reads preferred_transfer_client via getattr (transfer.py);
        # it must be set whether the base ctor accepts the kwarg (modern boto3)
        # or the floor shim stores it as a plain attribute (boto3 < ~1.33).
        for value in ("classic", "crt", "auto"):
            assert (
                TransferConfig(preferred_transfer_client=value).preferred_transfer_client == value
            )

    def test_bare_construct_does_not_raise(self) -> None:
        # The floor shim must not crash on a default construct (it forwards
        # preferred_transfer_client=None to a base ctor that may lack it).
        assert getattr(TransferConfig(), "preferred_transfer_client", None) == "auto"


class TestCrtExtras:
    def test_extras_default_to_none(self) -> None:
        config = TransferConfig()
        assert config.target_bandwidth is None
        assert config.should_stream is None
        assert config.disk_throughput is None
        assert config.direct_io is None

    def test_extras_are_keyword_only(self) -> None:
        config = TransferConfig(
            target_bandwidth=100_000_000,
            should_stream=False,
            disk_throughput=1_000_000_000,
            direct_io=True,
        )
        assert config.target_bandwidth == 100_000_000
        assert config.should_stream is False
        assert config.disk_throughput == 1_000_000_000
        assert config.direct_io is True
        # The extras sit past the `*` barrier: nine positional slots fill the
        # base params through preferred_transfer_client, so a tenth positional
        # (target_bandwidth) has no slot and raises rather than being accepted.
        with pytest.raises(TypeError):
            TransferConfig(1, 2, 3, 4, 5, 6, 7, 8, "auto", 100)  # pyright: ignore[reportCallIssue]

    def test_plain_boto3_config_reads_as_unset(self) -> None:
        # Engine code reads the extras with getattr(..., None) so a plain
        # boto3 TransferConfig keeps working wherever a config is accepted.
        plain = Boto3TransferConfig()
        assert getattr(plain, "target_bandwidth", None) is None
        assert getattr(plain, "direct_io", None) is None


class TestAnnotationTempDir:
    def test_defaults_to_os_temp_selection(self) -> None:
        assert TransferConfig().annotation_temp_dir is None

    def test_accepts_a_library_selected_directory(self, tmp_path: Path) -> None:
        config = TransferConfig(annotation_temp_dir=tmp_path)
        assert config.annotation_temp_dir == tmp_path

    def test_plain_boto3_config_reads_as_unset(self) -> None:
        plain = Boto3TransferConfig()
        assert getattr(plain, "annotation_temp_dir", None) is None
