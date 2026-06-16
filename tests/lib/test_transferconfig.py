"""``boto3_s3.TransferConfig`` - the boto3 subclass with CRT tuning fields.

Pins the contract docs/crt.md relies on: boto3's constructor surface is
untouched (names, order, defaults, alias attributes, the UNSET sentinel that
distinguishes "explicit" from "defaulted" values), the CRT extras are
keyword-only with ``None`` defaults, and a plain boto3 ``TransferConfig``
stays accepted by readers that fall back via ``getattr``.
"""

from __future__ import annotations

from boto3.s3.transfer import TransferConfig as Boto3TransferConfig

import boto3_s3
from boto3_s3.transferconfig import TransferConfig


class TestReExport:
    def test_public_name_is_the_subclass(self) -> None:
        assert boto3_s3.TransferConfig is TransferConfig

    def test_subclasses_boto3(self) -> None:
        assert issubclass(TransferConfig, Boto3TransferConfig)


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

    def test_plain_boto3_config_reads_as_unset(self) -> None:
        # Engine code reads the extras with getattr(..., None) so a plain
        # boto3 TransferConfig keeps working wherever a config is accepted.
        plain = Boto3TransferConfig()
        assert getattr(plain, "target_bandwidth", None) is None
        assert getattr(plain, "direct_io", None) is None
