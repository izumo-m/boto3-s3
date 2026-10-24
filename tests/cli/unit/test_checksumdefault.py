"""``transferargs.default_upload_checksum`` - aws-cli's default request checksum
for the uploads a CLI run makes.

The value is the library's (`boto3_s3.awsclisession.default_upload_algorithm`,
tests/lib/test_awsclisession.py); which runs get one is the command's.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from botocore import httpchecksum
from botocore.config import Config

from boto3_s3 import awsclisession
from boto3_s3_cli.commands import transferargs

_CAN_COMPUTE = "crc64nvme" in getattr(httpchecksum, "_SUPPORTED_CHECKSUM_ALGORITHMS", ())


def _client(**config: Any) -> Any:
    return boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="AKIAFAKEFAKEFAKEFAKE",
        aws_secret_access_key="fake",
        config=Config(**config),
    )


def _fake_client(calculation: str | None) -> Any:
    config = SimpleNamespace()
    if calculation is not None:
        config.request_checksum_calculation = calculation
    return SimpleNamespace(meta=SimpleNamespace(config=config))


_CLASSIC = SimpleNamespace(preferred_transfer_client="classic")
_CRT = SimpleNamespace(preferred_transfer_client="crt")


class TestUploadDefault:
    def test_only_an_upload_run_gets_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli sends no algorithm on a copy's CopyObject / UploadPartCopy and a
        # download writes nothing, so the transfer default is locals3's alone.
        monkeypatch.setattr(
            awsclisession, "default_upload_algorithm", lambda _client, *, crt: "CRC64NVME"
        )
        client = _fake_client("when_supported")
        assert transferargs.default_upload_checksum(client, "locals3", _CLASSIC) == "CRC64NVME"
        assert transferargs.default_upload_checksum(client, "s3s3", _CLASSIC) is None
        assert transferargs.default_upload_checksum(client, "s3local", _CLASSIC) is None

    @pytest.mark.skipif(not _CAN_COMPUTE, reason="this botocore cannot compute CRC64NVME")
    def test_the_crt_engine_keeps_the_default_under_when_required(self) -> None:
        # aws-cli's CRT module has CRC64NVME written in as its fallback and never
        # reads request_checksum_calculation - only its classic manager is
        # gated on when_supported - so a when_required CRT upload still trails
        # CRC64NVME (measured on the wire against the pinned aws-cli 2.36.40,
        # single-part and multipart) while the same classic upload sends none.
        client = _client(request_checksum_calculation="when_required")
        assert transferargs.default_upload_checksum(client, "locals3", _CRT) == "CRC64NVME"
        assert transferargs.default_upload_checksum(client, "locals3", _CLASSIC) is None
        # And an s3->s3 run is classic (no CRT copy) with no default either way.
        assert transferargs.default_upload_checksum(client, "s3s3", _CRT) is None
