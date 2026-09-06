"""Unit coverage for the EC2 lane's pure logic.

The launcher itself can only be exercised end to end against real AWS, so
these tests pin the parts that are easy to break silently and do not need the
cloud: architecture selection, the AMI parameter mapping, the instance role's
policy shape, region precedence, the MinIO-shell guard, and the fact that the
user-data template renders with no leftover placeholder.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
from typing import Any

import pytest

from benchmarks import awsenv, ec2
from benchmarks.core import BenchmarkError


class _FakeEC2:
    """Just enough of an EC2 client for `_describe_instance_type`."""

    def __init__(self, arches: list[str], memory_mib: int = 16384) -> None:
        self._arches = arches
        self._memory = memory_mib

    def describe_instance_types(self, InstanceTypes: list[str]) -> dict[str, Any]:  # noqa: N803
        if not self._arches:
            return {"InstanceTypes": []}
        return {
            "InstanceTypes": [
                {
                    "ProcessorInfo": {"SupportedArchitectures": self._arches},
                    "MemoryInfo": {"SizeInMiB": self._memory},
                }
            ]
        }


class TestInstanceType:
    def test_intel_type_resolves_to_x86_64(self) -> None:
        arch, memory = ec2._describe_instance_type(_FakeEC2(["x86_64"]), "m7i.xlarge")
        assert (arch, memory) == ("x86_64", 16384)

    def test_graviton_type_resolves_to_arm64(self) -> None:
        arch, _memory = ec2._describe_instance_type(_FakeEC2(["arm64"]), "m7g.xlarge")
        assert arch == "arm64"

    def test_unknown_type_is_an_error(self) -> None:
        with pytest.raises(BenchmarkError, match="unknown instance type"):
            ec2._describe_instance_type(_FakeEC2([]), "nonexistent.type")

    def test_tmpfs_is_sized_for_three_payloads_within_the_instance_memory(self) -> None:
        # 1 GB payloads -> 5 GB; fits a 16 GiB xlarge with headroom to spare.
        assert ec2._tmpfs_gb(1024, 16384, instance_type="m7i.xlarge") == 5
        # The local default barely needs anything.
        assert ec2._tmpfs_gb(64, 8192, instance_type="m7i.large") == 2
        # 1 GB payloads on an 8 GiB instance would leave under 4 GiB: refused.
        with pytest.raises(BenchmarkError, match=r"m7i\.large"):
            ec2._tmpfs_gb(1024, 8192, instance_type="m7i.large")

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
            large_mb=1024,
            tmpfs_gb=5,
            git_rev="0123456789abcdef",
            lane="ec2-m7i.xlarge",
        )
        url = "https://example.test/x?sig=a&exp=b"
        script = ec2._write_tarball_url_step(script, url)
        assert "PLACEHOLDER" not in script
        # The safety-net shutdown is armed before the work.
        assert script.index("shutdown -h +45") < script.index("uv sync")
        assert "BOTO3_S3_BENCH_ALLOW_REMOTE=1" in script
        assert "scripts/install-awscli.sh 2.36.40" in script
        # The curl line hands the URL over as one word, exactly - no quoting
        # left inside it (a `"'url'"` would make curl reject the URL).
        (curl_line,) = [line for line in script.splitlines() if line.startswith("curl -fsSL")]
        assert shlex.split(curl_line) == ["curl", "-fsSL", url, "-o", "repo.tar.gz"]
        # The work tree is a tmpfs of the size the launcher computed, and the
        # size knob reaches the run line.
        assert "mount -t tmpfs -o size=5g" in script
        assert "--large-transfer-mb 1024" in script
        # install-awscli.sh unpacks a zip; the provenance the archive lacks
        # travels as environment.
        assert "unzip" in script.split("uv sync --all-packages --locked")[0]
        assert "export BOTO3_S3_BENCH_GIT_REV=0123456789abcdef" in script
        assert "export BOTO3_S3_BENCH_LANE=ec2-m7i.xlarge" in script
        # Reporting does not depend on the venv: the EXIT trap uses the system
        # aws first, and provisioning runs under errexit with an ERR trap.
        finish = script.split("finish() {", 1)[1].split("}", 1)[0]
        assert "/usr/bin/aws s3 cp" in finish
        assert "trap 'RC=\"failed-at-line-$LINENO\"' ERR" in script

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash for a syntax check")
    def test_rendered_user_data_is_valid_bash(self) -> None:
        script = ec2._user_data(
            boot_bucket="boot",
            run_id="r",
            region="us-east-1",
            python="3.14",
            aws_version="2.36.40",
            max_minutes=60,
            large_mb=1024,
            tmpfs_gb=5,
            git_rev="0",
            lane="ec2-x",
        )
        script = ec2._write_tarball_url_step(script, "https://example.test/x?a=b&c=d")
        proc = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr

    def test_a_shell_active_url_is_refused(self) -> None:
        script = ec2._user_data(
            boot_bucket="boot",
            run_id="r",
            region="us-east-1",
            python="3.14",
            aws_version="2.36.40",
            max_minutes=60,
            large_mb=64,
            tmpfs_gb=2,
            git_rev="0",
            lane="ec2-x",
        )
        with pytest.raises(BenchmarkError, match="shell-active"):
            ec2._write_tarball_url_step(script, 'https://example.test/x?a="b"')

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
            large_mb=64,
            tmpfs_gb=2,
            git_rev="0",
            lane="ec2-x",
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
