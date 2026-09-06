"""Unit coverage for the EC2 lane's pure logic.

The launcher itself can only be exercised end to end against real AWS, so
these tests pin the parts that are easy to break silently and do not need the
cloud: architecture selection, the AMI parameter mapping, the instance role's
policy shape, region precedence, the MinIO-shell guard, and the fact that the
user-data template renders with no leftover placeholder.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from benchmarks import awsenv, ec2
from benchmarks.core import BenchmarkError


class _FakeEC2:
    """Just enough of an EC2 client for `_resolve_architecture`."""

    def __init__(self, arches: list[str]) -> None:
        self._arches = arches

    def describe_instance_types(self, InstanceTypes: list[str]) -> dict[str, Any]:  # noqa: N803
        if not self._arches:
            return {"InstanceTypes": []}
        return {"InstanceTypes": [{"ProcessorInfo": {"SupportedArchitectures": self._arches}}]}


class TestArchitecture:
    def test_intel_type_resolves_to_x86_64(self) -> None:
        assert ec2._resolve_architecture(_FakeEC2(["x86_64"]), "m7i.xlarge") == "x86_64"

    def test_graviton_type_resolves_to_arm64(self) -> None:
        assert ec2._resolve_architecture(_FakeEC2(["arm64"]), "m7g.xlarge") == "arm64"

    def test_unknown_type_is_an_error(self) -> None:
        with pytest.raises(BenchmarkError, match="unknown instance type"):
            ec2._resolve_architecture(_FakeEC2([]), "nonexistent.type")

    def test_both_architectures_map_to_a_public_al2023_parameter(self) -> None:
        for arch in ("x86_64", "arm64"):
            assert ec2._AL2023_SSM[arch].startswith("/aws/service/ami-amazon-linux-latest/")
            assert ec2._AL2023_SSM[arch].endswith(arch)


class TestInstancePolicy:
    def test_policy_is_scoped_to_the_benchmark_bucket_family(self) -> None:
        doc = ec2.instance_policy_document()
        resources = {r for stmt in doc["Statement"] for r in _as_list(stmt["Resource"])}
        # Every resource is under the boto3-s3-bench* family (the throwaway
        # buckets and the boot bucket both match), never a wildcard on all S3.
        assert resources
        assert all("boto3-s3-bench" in r for r in resources)
        assert "arn:aws:s3:::*" not in resources

    def test_policy_serializes_as_json(self) -> None:
        json.dumps(ec2.instance_policy_document())


class TestUserData:
    def test_renders_with_no_leftover_placeholder(self) -> None:
        script = ec2._user_data(
            boot_bucket="boot",
            run_id="20260906-000000-abc",
            region="ap-northeast-1",
            python="3.14",
            aws_version="2.36.40",
            max_minutes=45,
        )
        script = ec2._write_tarball_url_step(script, "https://example.test/x?sig=a&exp=b")
        assert "PLACEHOLDER" not in script
        # The safety-net shutdown is armed before the work.
        assert script.index("shutdown -h +45") < script.index("uv sync")
        assert "BOTO3_S3_BENCH_ALLOW_REMOTE=1" in script
        assert "scripts/install-awscli.sh 2.36.40" in script
        assert "example.test/x?sig=a&exp=b" in script

    def test_a_signed_url_with_braces_survives_substitution(self) -> None:
        # The URL is inlined after str.format, so query strings cannot collide
        # with the template's own braces.
        script = ec2._user_data(
            boot_bucket="boot",
            run_id="r",
            region="us-east-1",
            python="3.14",
            aws_version="2.36.40",
            max_minutes=60,
        )
        url = "https://example.test/o?X-Amz-Signature={not-a-field}"
        assert url in ec2._write_tarball_url_step(script, url)


class TestRegionAndGuards:
    def test_explicit_region_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        assert awsenv.resolve_region("us-east-2") == "us-east-2"

    def test_aws_region_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert awsenv.resolve_region() == "eu-west-1"

    def test_minio_endpoint_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:9000")
        with pytest.raises(BenchmarkError, match="MinIO"):
            awsenv.refuse_minio_env()

    def test_minio_static_key_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "minioadmin")
        with pytest.raises(BenchmarkError, match="MinIO"):
            awsenv.refuse_minio_env()

    def test_a_clean_shell_passes_the_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in ("AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        awsenv.refuse_minio_env()  # no raise


def _as_list(value: object) -> list[str]:
    return value if isinstance(value, list) else [value]  # type: ignore[return-value]
