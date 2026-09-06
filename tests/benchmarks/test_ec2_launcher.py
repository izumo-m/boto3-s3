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
from benchmarks.ec2 import Outcome


def _has_real_bash() -> bool:
    """True when `bash` on PATH is a shell, not Windows' WSL launcher stub.

    On a Windows host without a WSL distribution, `bash` resolves to
    System32's bash.exe, which prints an install hint and exits 1 for any
    argument, `-n` included; a syntax check there says nothing about the
    script. The user data only ever runs on the Linux instance, so the check
    is skipped wherever the shell cannot answer it.
    """
    if shutil.which("bash") is None:
        return False
    try:
        proc = subprocess.run(["bash", "-c", "true"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


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

    def describe_images(self, ImageIds: list[str]) -> dict[str, Any]:  # noqa: N803
        if ImageIds == ["ami-missing"]:
            return {"Images": []}
        return {"Images": [{"ImageId": ImageIds[0], "RootDeviceName": "/dev/sda1"}]}


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

    def test_both_architectures_map_to_a_canonical_ubuntu_parameter(self) -> None:
        assert ec2.ubuntu_ssm_parameter("26.04", "x86_64") == (
            "/aws/service/canonical/ubuntu/server/26.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
        )
        assert ec2.ubuntu_ssm_parameter("24.04", "arm64") == (
            "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
        )

    def test_root_device_comes_from_the_ami(self) -> None:
        # Ubuntu images root on /dev/sda1; a hard-coded /dev/xvda would have
        # attached a second volume and left the root at the image default.
        assert ec2._root_device_name(_FakeEC2(["x86_64"]), "ami-0123") == "/dev/sda1"
        with pytest.raises(BenchmarkError, match="not found"):
            ec2._root_device_name(_FakeEC2(["x86_64"]), "ami-missing")


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


_URLS = {
    "TARBALL_URL_PLACEHOLDER": "https://example.test/boot/r/repo.tar.gz?X-Amz-Signature=t&exp=1",
    "LOG_PUT_URL_PLACEHOLDER": "https://example.test/boot/r/bench-userdata.log?X-Amz-Signature=l",
    "DONE_PUT_URL_PLACEHOLDER": "https://example.test/boot/r/DONE?X-Amz-Signature=d",
}


def _render(**overrides: Any) -> str:
    params: dict[str, Any] = dict(
        boot_bucket="boot",
        run_id="20260906-000000-abc",
        region="ap-northeast-1",
        python="3.14",
        aws_version="2.36.40",
        max_minutes=45,
        large_mb=1024,
        tmpfs_gb=5,
        git_rev="0123456789abcdef",
        lane="ec2-m7i.xlarge-ubuntu26.04",
        image="ami-0123",
        bench_bucket="boto3-s3-bench-20260906-000000-abc",
    )
    params.update(overrides)
    return ec2._user_data(**params)


class TestUserData:
    def test_renders_with_every_url_in_place(self) -> None:
        script = ec2._inline_urls(_render(), _URLS)
        assert "PLACEHOLDER" not in script
        # The safety-net shutdown is armed before the work.
        assert script.index("shutdown -h +45") < script.index("uv sync")
        assert "BOTO3_S3_BENCH_ALLOW_REMOTE=1" in script
        assert "scripts/install-awscli.sh 2.36.40" in script
        # Each curl hands its URL over as exactly one word - no quoting left
        # inside it (a `"'url'"` would make curl reject the URL).
        curls = [
            shlex.split(line.strip())
            for line in script.splitlines()
            if line.strip().startswith("curl ")
        ]
        fetch = next(w for w in curls if w[-1] == "repo.tar.gz")
        assert fetch == ["curl", "-fsSL", _URLS["TARBALL_URL_PLACEHOLDER"], "-o", "repo.tar.gz"]
        last_words = {words[-1] for words in curls}
        assert _URLS["LOG_PUT_URL_PLACEHOLDER"] in last_words
        assert _URLS["DONE_PUT_URL_PLACEHOLDER"] in last_words
        # Reporting goes through the presigned PUTs from the EXIT trap, before
        # and independent of any venv, and the curl uploads carry the files.
        finish = script.split("finish() {", 1)[1].split("}", 1)[0]
        assert "-T /var/log/bench-userdata.log" in finish
        assert "-T /run/bench-done" in finish
        assert "trap 'RC=\"failed-at-line-$LINENO\"' ERR" in script
        # Ubuntu provisioning: apt with a lock timeout, unzip for the aws zip,
        # background updaters silenced first; no dnf anywhere.
        assert "dnf" not in script
        assert "apt-get -o DPkg::Lock::Timeout=300 -q install -y unzip" in script
        assert script.index("systemctl disable --now apt-daily.timer") < script.index("apt-get")
        # The tmpfs the launcher sized, the size knob on the run line, and
        # the provenance the archive lacks.
        assert "mount -t tmpfs -o size=5g" in script
        assert "--large-transfer-mb 1024" in script
        assert "export BOTO3_S3_BENCH_GIT_REV=0123456789abcdef" in script
        assert "export BOTO3_S3_BENCH_LANE=ec2-m7i.xlarge-ubuntu26.04" in script
        assert "export BOTO3_S3_BENCH_IMAGE=ami-0123" in script
        assert "export BOTO3_S3_BENCH_BUCKET=boto3-s3-bench-20260906-000000-abc" in script
        # An outside shutdown is recorded as a signal; the log is synced every
        # minute through the same presigned PUT the trap uses.
        assert "trap 'RC=\"terminated-by-signal:$RC\"; exit 143' TERM" in script
        assert script.count(_URLS["LOG_PUT_URL_PLACEHOLDER"]) == 2
        # Each mode runs on its own and uploads before the next starts, with
        # errexit and the ERR trap off around the benchmark's own exit codes.
        runs = [line for line in script.splitlines() if "python -m benchmarks run" in line]
        assert [("--mode inprocess" in r, "--mode e2e --engine both" in r) for r in runs] == [
            (True, False),
            (False, True),
        ]
        assert script.count("upload_results\n") == 2
        assert script.index("trap - ERR") < script.index("--mode inprocess")

    @pytest.mark.skipif(not _has_real_bash(), reason="needs a working bash for a syntax check")
    def test_rendered_user_data_is_valid_bash(self) -> None:
        script = ec2._inline_urls(_render(), _URLS)
        proc = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr

    def test_a_signed_url_with_braces_survives_substitution(self) -> None:
        # URLs are inlined after str.format, so query strings cannot collide
        # with the template's own braces.
        urls = dict(_URLS)
        urls["TARBALL_URL_PLACEHOLDER"] = "https://example.test/o?X-Amz-Signature={not-a-field}"
        assert urls["TARBALL_URL_PLACEHOLDER"] in ec2._inline_urls(_render(), urls)

    def test_a_shell_active_url_is_refused(self) -> None:
        urls = dict(_URLS)
        urls["LOG_PUT_URL_PLACEHOLDER"] = 'https://example.test/x?a="b"'
        with pytest.raises(BenchmarkError, match="shell-active"):
            ec2._inline_urls(_render(), urls)

    def test_a_missing_url_is_refused(self) -> None:
        urls = dict(_URLS)
        del urls["DONE_PUT_URL_PLACEHOLDER"]
        with pytest.raises(BenchmarkError, match="unfilled"):
            ec2._inline_urls(_render(), urls)


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


class TestMarketAndOutcome:
    def test_spot_is_a_one_time_request_that_terminates(self) -> None:
        options = ec2._market_options(True)["InstanceMarketOptions"]
        assert options["MarketType"] == "spot"
        assert options["SpotOptions"] == {
            "SpotInstanceType": "one-time",
            "InstanceInterruptionBehavior": "terminate",
        }
        assert ec2._market_options(False) == {}

    def test_failing_line_is_looked_up_in_the_script(self) -> None:
        script = "line one\nline two\nline three\n"
        assert ec2._failing_line(script, "failed-at-line-2") == "line two"
        assert ec2._failing_line(script, "failed-at-line-9") is None
        assert ec2._failing_line(script, "0") is None

    @pytest.mark.parametrize(
        ("outcome", "expect"),
        [
            (Outcome("completed", "0", None, None, 35.0), "completed"),
            (
                Outcome("completed", "inprocess=0,e2e=1", None, None, 35.0),
                "non-zero exit code",
            ),
            (
                Outcome("completed", "inprocess=0,e2e=0,results-upload-failed", None, None, 35.0),
                "results upload failed",
            ),
            (Outcome("completed", "failed-at-line-2", None, None, 3.0), "provisioning failed"),
            (
                Outcome(
                    "completed",
                    "terminated-by-signal:unknown",
                    "shutting-down",
                    "Server.SpotInstanceTermination: Spot Instance interrupted",
                    20.0,
                ),
                "spot interruption",
            ),
            (
                Outcome(
                    "completed",
                    "terminated-by-signal:unknown",
                    "shutting-down",
                    "Client.InstanceInitiatedShutdown: Instance initiated shutdown",
                    60.2,
                ),
                "budget exceeded",
            ),
            (
                Outcome(
                    "died", None, "terminated", "Server.SpotInstanceTermination: reclaimed", 12.0
                ),
                "spot interruption",
            ),
            (
                Outcome("died", None, "terminated", "Server.InternalError: host failure", 12.0),
                "EC2 stopped the instance",
            ),
            (Outcome("died", None, "terminated", None, 12.0), "without reporting"),
            (Outcome("timeout", None, "running", None, 66.0), "stopped waiting"),
        ],
    )
    def test_classify_names_the_cause(self, outcome: Outcome, expect: str) -> None:
        verdict = ec2._classify(outcome, 60, "l1\ndnf install\nl3\n")
        assert expect in verdict
        if outcome.rc == "failed-at-line-2":
            assert "dnf install" in verdict

    def test_ok_requires_a_zero_marker(self) -> None:
        assert Outcome("completed", "0", None, None, 1.0).ok
        assert not Outcome("completed", "1", None, None, 1.0).ok
        assert not Outcome("died", None, "terminated", None, 1.0).ok
