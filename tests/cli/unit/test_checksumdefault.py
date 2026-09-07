"""``boto3_s3_cli.checksumdefault`` - aws's default request checksum on the
CLI's clients.

aws v2's bundled botocore defaults to CRC64NVME where pip botocore defaults to
CRC32; the CLI names aws's value where botocore would have stamped its own
(the transfer options of an upload run, and a ``provide-client-params``
handler for every other request), and only where botocore can compute it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from botocore import httpchecksum
from botocore.config import Config

from boto3_s3_cli import checksumdefault
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


class TestDefaultAlgorithm:
    @pytest.mark.skipif(not _CAN_COMPUTE, reason="this botocore cannot compute CRC64NVME")
    def test_when_supported_client_gets_aws_default(self) -> None:
        assert checksumdefault.default_algorithm(_client()) == "CRC64NVME"

    def test_when_required_client_gets_none(self) -> None:
        # botocore stamps no default on an upload there, on either tool (the
        # checksum-required operations are register's business).
        assert (
            checksumdefault.default_algorithm(_client(request_checksum_calculation="when_required"))
            is None
        )

    def test_a_botocore_without_the_setting_gets_none(self) -> None:
        assert checksumdefault.default_algorithm(_fake_client(None)) is None

    def test_none_when_botocore_cannot_compute_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No awscrt (or one too old): botocore's own default stands rather than
        # a per-item MissingDependency failure.
        monkeypatch.setattr(httpchecksum, "_SUPPORTED_CHECKSUM_ALGORITHMS", ["crc32", "sha256"])
        assert checksumdefault.default_algorithm(_fake_client("when_supported")) is None


@pytest.mark.skipif(not _CAN_COMPUTE, reason="this botocore cannot compute CRC64NVME")
class TestRegister:
    def _emit(self, client: Any, operation: str, params: dict[str, Any], **context: Any) -> None:
        client.meta.events.emit(
            f"provide-client-params.s3.{operation}",
            params=params,
            model=client.meta.service_model.operation_model(operation),
            context=context,
        )

    def test_stamps_operations_botocore_would_default(self) -> None:
        # The same test botocore applies before stamping its own default: the
        # operation names a ChecksumAlgorithm member and the caller left it out.
        client = _client()
        checksumdefault.register(client)
        for operation in ("DeleteObjects", "PutBucketTagging", "PutObjectAnnotation", "PutObject"):
            params: dict[str, Any] = {"Bucket": "b"}
            self._emit(client, operation, params)
            assert params["ChecksumAlgorithm"] == "CRC64NVME", operation

    def test_leaves_an_explicit_algorithm_and_other_operations_alone(self) -> None:
        client = _client()
        checksumdefault.register(client)
        explicit: dict[str, Any] = {"Bucket": "b", "ChecksumAlgorithm": "SHA256"}
        self._emit(client, "PutObject", explicit)
        assert explicit["ChecksumAlgorithm"] == "SHA256"
        for operation in ("GetObject", "CopyObject", "HeadObject", "ListObjectsV2"):
            params: dict[str, Any] = {"Bucket": "b"}
            self._emit(client, operation, params)
            assert "ChecksumAlgorithm" not in params, operation

    def test_presigned_requests_get_none(self) -> None:
        # botocore skips its default on a presign; the stamp does the same.
        client = _client()
        checksumdefault.register(client)
        params: dict[str, Any] = {"Bucket": "b", "Key": "k"}
        self._emit(client, "PutObject", params, is_presign_request=True)
        assert "ChecksumAlgorithm" not in params

    def test_when_required_client_stamps_only_the_checksum_required_operations(self) -> None:
        # botocore's own order: a `requestChecksumRequired` operation gets the
        # default under either setting (aws's bundled botocore stamps CRC64NVME
        # on a `when_required` `website`, measured on the wire against the
        # pinned aws 2.36.40; pip botocore would stamp its CRC32 there), the
        # others only under when_supported.
        client = _client(request_checksum_calculation="when_required")
        checksumdefault.register(client)
        for operation in ("DeleteObjects", "PutBucketTagging", "PutBucketWebsite"):
            params: dict[str, Any] = {"Bucket": "b"}
            self._emit(client, operation, params)
            assert params["ChecksumAlgorithm"] == "CRC64NVME", operation
        for operation in ("PutObject", "PutObjectAnnotation", "UploadPart"):
            params = {"Bucket": "b"}
            self._emit(client, operation, params)
            assert "ChecksumAlgorithm" not in params, operation

    def test_the_required_set_is_read_off_the_model(self) -> None:
        # The stamp asks the operation model, not a list of its own: the three
        # the CLI reaches are marked required there (with two dozen bucket
        # configuration puts the CLI never sends), and every marked operation
        # names the ChecksumAlgorithm member the stamp fills.
        model = _client().meta.service_model
        required = {
            name
            for name in model.operation_names
            if model.operation_model(name).http_checksum.get("requestChecksumRequired")
        }
        assert {"DeleteObjects", "PutBucketTagging", "PutBucketWebsite"} <= required
        for name in required:
            member = model.operation_model(name).http_checksum.get("requestAlgorithmMember")
            assert member == "ChecksumAlgorithm", name


_CLASSIC = SimpleNamespace(preferred_transfer_client="classic")
_CRT = SimpleNamespace(preferred_transfer_client="crt")


class TestUploadDefault:
    def test_only_an_upload_run_gets_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws sends no algorithm on a copy's CopyObject / UploadPartCopy and a
        # download writes nothing, so the transfer default is locals3's alone.
        monkeypatch.setattr(checksumdefault, "default_algorithm", lambda _client: "CRC64NVME")
        client = _fake_client("when_supported")
        assert transferargs.default_upload_checksum(client, "locals3", _CLASSIC) == "CRC64NVME"
        assert transferargs.default_upload_checksum(client, "s3s3", _CLASSIC) is None
        assert transferargs.default_upload_checksum(client, "s3local", _CLASSIC) is None

    @pytest.mark.skipif(not _CAN_COMPUTE, reason="this botocore cannot compute CRC64NVME")
    def test_the_crt_engine_keeps_the_default_under_when_required(self) -> None:
        # aws's CRT module has CRC64NVME written in as its fallback and never
        # reads request_checksum_calculation - only its classic manager is
        # gated on when_supported - so a when_required CRT upload still trails
        # CRC64NVME (measured on the wire against the pinned aws 2.36.40,
        # single-part and multipart) while the same classic upload sends none.
        client = _client(request_checksum_calculation="when_required")
        assert transferargs.default_upload_checksum(client, "locals3", _CRT) == "CRC64NVME"
        assert transferargs.default_upload_checksum(client, "locals3", _CLASSIC) is None
        # And an s3->s3 run is classic (no CRT copy) with no default either way.
        assert transferargs.default_upload_checksum(client, "s3s3", _CRT) is None
