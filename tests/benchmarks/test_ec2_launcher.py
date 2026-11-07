"""Unit coverage for the EC2 lane's pure logic.

The launcher itself can only be exercised end to end against real AWS, so
these tests pin the parts that are easy to break silently and do not need the
cloud: architecture selection, the AMI parameter mapping, the instance role's
policy shape, region precedence, the dev-stack-shell guard, and the fact that the
user-data template renders with no leftover placeholder.
"""

from __future__ import annotations

import io
import json
import shlex
import shutil
import subprocess
import urllib.parse
from pathlib import Path
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
        uv_version="0.13.5",
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
        # The interpreter is a uv-managed build, as on the local lane; Ubuntu
        # ships a python3.14 that uv would otherwise accept for the request.
        assert script.index("export UV_PYTHON_PREFERENCE=only-managed") < script.index("uv sync")
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
        # Ubuntu provisioning: apt with a lock timeout, unzip for the aws-cli zip,
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
        # The sync loop runs without xtrace, or its own curl would be the
        # log's newest line every minute and hide the real progress.
        sync_loop = script.split("( set +x", 1)[1].split(") &", 1)[0]
        assert "while sleep 60" in sync_loop
        assert _URLS["LOG_PUT_URL_PLACEHOLDER"] in sync_loop
        # The loop inherits errexit, so one failed upload would end it for good.
        upload = next(line for line in sync_loop.splitlines() if "curl" in line)
        assert upload.rstrip().endswith("|| true")
        # The uv mise.toml pins, installed under pipefail so a failed download
        # fails its own line rather than the `uv sync` after it.
        assert "https://astral.sh/uv/0.13.5/install.sh" in script
        assert "export UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1" in script
        assert script.index("set -o pipefail") < script.index("astral.sh/uv/")
        # cloud-init may start the script without HOME, which the installer reads.
        assert script.index("export HOME=/root") < script.index("astral.sh/uv/")
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
        # Bytes, not text: Windows text-mode stdin would turn every LF into
        # CRLF, which bash rejects, while the instance receives the LF script.
        proc = subprocess.run(["bash", "-n"], input=script.encode(), capture_output=True)
        assert proc.returncode == 0, proc.stderr.decode(errors="replace")

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
    def test_the_launcher_profile_supplies_the_region(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config = tmp_path / "config"
        config.write_text(
            "[default]\nregion = us-east-1\n[profile bench]\nregion = ap-northeast-1\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
            monkeypatch.delenv(name, raising=False)
        assert awsenv.resolve_region(profile="bench") == "ap-northeast-1"
        assert awsenv.resolve_region() == "us-east-1"

    def test_explicit_region_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        assert awsenv.resolve_region("us-east-2") == "us-east-2"

    def test_aws_region_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert awsenv.resolve_region() == "eu-west-1"

    def test_dev_stack_endpoint_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:9000")
        with pytest.raises(BenchmarkError, match="dev stack"):
            awsenv.refuse_dev_stack_env()

    def test_dev_stack_static_key_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "devadmin")
        with pytest.raises(BenchmarkError, match="dev stack"):
            awsenv.refuse_dev_stack_env()

    def test_a_clean_shell_passes_the_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for key in ("AWS_ENDPOINT_URL_S3", "AWS_ENDPOINT_URL"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
        awsenv.refuse_dev_stack_env()  # no raise


def _as_list(value: object) -> list[str]:
    return value if isinstance(value, list) else [value]  # pyright: ignore[reportReturnType]


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
            # The shapes the script writes: RC=0 with the suffix, or the per-mode codes.
            (
                Outcome("completed", "0,results-upload-failed", None, None, 35.0),
                "results upload failed",
            ),
            (
                Outcome("completed", "inprocess=0,e2e=1,results-upload-failed", None, None, 35.0),
                "and a results upload failed",
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

    def test_results_without_a_marker_point_at_the_report_path(self) -> None:
        # The first real run: both results files came back, the instance shut
        # itself down at the end, but the presigned PUTs had failed, so no DONE.
        outcome = Outcome(
            "died",
            None,
            "shutting-down",
            "Client.InstanceInitiatedShutdown: Instance initiated shutdown",
            19.0,
        )
        both = ["20260906-1_inprocess_abc.ec2-x.jsonl", "20260906-2_e2e_abc.ec2-x.jsonl"]
        verdict = ec2._classify(outcome, 60, "", results_back=both)
        assert "2 results file(s)" in verdict
        assert "DONE marker never arrived" in verdict
        assert "ran to the end" in verdict
        assert "without reporting" in ec2._classify(outcome, 60, "", results_back=[])
        # One file back is not "the end", and its name says which mode is missing:
        # the first mode can fail without a file while the second still uploads.
        e2e_only = ec2._classify(outcome, 60, "", results_back=both[1:])
        assert "ran to the end" not in e2e_only
        assert "the inprocess mode left no results" in e2e_only
        assert "the e2e mode left no results" in ec2._classify(
            outcome, 60, "", results_back=both[:1]
        )

    def test_ok_requires_a_zero_marker(self) -> None:
        assert Outcome("completed", "0", None, None, 1.0).ok
        assert not Outcome("completed", "1", None, None, 1.0).ok
        assert not Outcome("died", None, "terminated", None, 1.0).ok


class TestPresignedUrls:
    def test_presigned_urls_stay_on_the_bucket_regional_endpoint(self) -> None:
        # Presigning is offline, so fake credentials suffice. Without the
        # launcher's config botocore presigns with SigV2 on the global
        # endpoint, where a new bucket outside us-east-1 answers with a 307
        # that `curl -T` does not follow (the first run's log and DONE were
        # lost that way).
        import boto3

        session = boto3.Session(
            aws_access_key_id="AKIAFAKEFAKEFAKEFAKE",
            aws_secret_access_key="fake",
            region_name="ap-northeast-1",
        )
        s3 = session.client("s3", config=ec2._s3_config())
        url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": "boto3-s3-bench-boot-1-r", "Key": "r/DONE"},
            ExpiresIn=60,
        )
        parts = urllib.parse.urlsplit(url)
        assert parts.netloc == "boto3-s3-bench-boot-1-r.s3.ap-northeast-1.amazonaws.com"
        query = urllib.parse.parse_qs(parts.query)
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert query["X-Amz-SignedHeaders"] == ["host"]
        # And the URL is one the user-data can carry verbatim.
        assert "PLACEHOLDER" not in ec2._inline_urls(
            "x DONE_PUT_URL_PLACEHOLDER", {"DONE_PUT_URL_PLACEHOLDER": url}
        )


class _ScriptedEC2:
    """An EC2 client stand-in answering DescribeInstances from a script of states."""

    def __init__(self, states: list[dict[str, Any]] | BaseException) -> None:
        self._states = states
        self.calls: list[str] = []
        self.fail_terminate: Exception | None = None
        self.filters: list[dict[str, Any]] = []

    def describe_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_instances")
        if "Filters" in kwargs:
            self.filters = kwargs["Filters"]
            return {"Reservations": [{"Instances": [{"InstanceId": "i-left"}]}]}
        if isinstance(self._states, BaseException):
            raise self._states
        state = self._states.pop(0) if len(self._states) > 1 else self._states[0]
        return {"Reservations": [{"Instances": [state]}]}

    def terminate_instances(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append("terminate_instances")
        if self.fail_terminate is not None:
            raise self.fail_terminate
        return {}


class _MarkerS3:
    """An S3 client stand-in whose DONE marker is already there."""

    def __init__(self, marker: bytes) -> None:
        self._marker = marker

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        import io

        assert Key.endswith("/DONE")
        return {"Body": io.BytesIO(self._marker)}


class TestOutcomeReading:
    def test_an_outside_shutdown_marker_still_learns_the_stop_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The trap's marker can land while the instance is still `running`;
        # the stop reason that tells a spot reclaim apart comes a little later.
        monkeypatch.setattr(ec2.time, "sleep", lambda _seconds: None)
        spot = "Server.SpotInstanceTermination: Spot Instance interrupted"
        fake = _ScriptedEC2(
            [
                {"State": {"Name": "running"}},
                {"State": {"Name": "shutting-down"}, "StateReason": {"Message": spot}},
            ]
        )
        outcome = ec2._await_completion(
            _MarkerS3(b"rc=terminated-by-signal:unknown"),
            fake,
            "boot",
            "run",
            "i-1",
            60,
            launched=ec2.time.monotonic(),
        )
        assert outcome.kind == "completed"
        assert outcome.reason == spot
        assert "spot interruption" in ec2._classify(outcome, 60, "")

    def test_an_instance_not_yet_visible_reads_as_pending(self) -> None:
        from botocore.exceptions import ClientError

        error = ClientError({"Error": {"Code": "InvalidInstanceID.NotFound"}}, "DescribeInstances")
        assert ec2._instance_status(_ScriptedEC2(error), "i-1") == ("pending", None)

    @staticmethod
    def _cleanup(monkeypatch: pytest.MonkeyPatch, fake_ec2: _ScriptedEC2) -> int:
        import argparse

        from tests.utils import harness

        monkeypatch.setattr(ec2.time, "sleep", lambda _seconds: None)
        order = fake_ec2.calls

        class _Buckets:
            def list_buckets(self) -> dict[str, Any]:
                order.append("list_buckets")
                return {
                    "Buckets": [
                        {"Name": "boto3-s3-bench-run"},
                        {"Name": "boto3-s3-bench-boot-1-r"},
                    ]
                }

        monkeypatch.setattr(awsenv, "refuse_dev_stack_env", lambda: None)
        monkeypatch.setattr(
            ec2, "_client", lambda kind, *_args: fake_ec2 if kind == "ec2" else _Buckets()
        )
        monkeypatch.setattr(
            harness, "force_delete_bucket", lambda _client, name: order.append(f"delete {name}")
        )
        args = argparse.Namespace(region="ap-northeast-1", profile=None)
        return ec2.cmd_cleanup(args)

    def test_cleanup_stops_the_instances_before_emptying_buckets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_ec2 = _ScriptedEC2(
            [{"State": {"Name": "shutting-down"}}, {"State": {"Name": "terminated"}}]
        )
        assert self._cleanup(monkeypatch, fake_ec2) == 0
        # The wait polls until the instance is terminated, then the buckets go;
        # the hand-off bucket stays.
        assert fake_ec2.calls == [
            "describe_instances",
            "terminate_instances",
            "describe_instances",
            "describe_instances",
            "list_buckets",
            "delete boto3-s3-bench-run",
        ]

    def test_cleanup_also_waits_for_an_instance_already_shutting_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One still shutting down can be writing into the bucket being emptied.
        fake_ec2 = _ScriptedEC2([{"State": {"Name": "terminated"}}])
        self._cleanup(monkeypatch, fake_ec2)
        states = next(f["Values"] for f in fake_ec2.filters if f["Name"] == "instance-state-name")
        assert "shutting-down" in states

    def test_cleanup_removes_the_buckets_when_the_wait_fails(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from botocore.exceptions import EndpointConnectionError

        fake_ec2 = _ScriptedEC2(EndpointConnectionError(endpoint_url="https://ec2.test"))
        assert self._cleanup(monkeypatch, fake_ec2) == 2
        assert fake_ec2.calls[-1] == "delete boto3-s3-bench-run"
        out = capsys.readouterr().out
        assert "could not read the state of i-left" in out
        assert "terminated i-left" not in out
        assert "gave up waiting" in out

    def test_cleanup_exits_2_when_the_wait_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ec2, "_wait_terminated", lambda *_a: False)
        fake_ec2 = _ScriptedEC2([{"State": {"Name": "running"}}])
        assert self._cleanup(monkeypatch, fake_ec2) == 2
        assert fake_ec2.calls[-1] == "delete boto3-s3-bench-run"

    def test_the_wait_gives_up_at_its_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ec2.time, "sleep", lambda _seconds: None)
        running = _ScriptedEC2([{"State": {"Name": "running"}}])
        assert ec2._wait_terminated(running, ["i-1"], timeout=0.0) is False

    def test_the_uv_version_is_mise_toml_s(self) -> None:
        mise = (ec2.REPO_ROOT / "mise.toml").read_text()
        assert f'uv = "{ec2._pinned_uv_version()}"' in mise


class _BootBucketS3:
    """An S3 client stand-in for `_ensure_boot_bucket`: each create takes the next
    answer, an error code or None for a success."""

    class _Meta:
        region_name = "ap-northeast-1"

    meta = _Meta()

    def __init__(self, *answers: str | None, listed: tuple[str, ...] = ()) -> None:
        self._answers = list(answers)
        self._listed = listed
        self.creates = 0

    def list_buckets(self) -> dict[str, Any]:
        return {"Buckets": [{"Name": name} for name in self._listed]}

    def create_bucket(self, **_kwargs: Any) -> dict[str, Any]:
        from botocore.exceptions import ClientError

        self.creates += 1
        answer = self._answers.pop(0)
        if answer is not None:
            raise ClientError({"Error": {"Code": answer}}, "CreateBucket")
        return {}


class TestBootBucket:
    """Two launchers started together race to create the hand-off bucket."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        slept: list[float] = []
        monkeypatch.setattr(ec2.time, "sleep", slept.append)
        return slept

    def _ensure(self, s3: _BootBucketS3) -> None:
        ec2._ensure_boot_bucket(s3, "boot", "ap-northeast-1")  # pyright: ignore[reportPrivateUsage]

    def test_a_listed_bucket_is_not_created(self) -> None:
        s3 = _BootBucketS3(listed=("boot",))
        self._ensure(s3)
        assert s3.creates == 0

    def test_a_missing_bucket_is_created(self, _no_sleep: list[float]) -> None:
        s3 = _BootBucketS3(None)
        self._ensure(s3)
        assert s3.creates == 1
        assert _no_sleep == []

    def test_a_create_in_progress_elsewhere_is_waited_out(self, _no_sleep: list[float]) -> None:
        s3 = _BootBucketS3("OperationAborted", "OperationAborted", None)
        self._ensure(s3)
        assert s3.creates == 3
        assert len(_no_sleep) == 2

    def test_a_create_finished_elsewhere_is_enough(self) -> None:
        s3 = _BootBucketS3("OperationAborted", "BucketAlreadyOwnedByYou")
        self._ensure(s3)
        assert s3.creates == 2

    def test_a_create_that_never_clears_raises(self) -> None:
        from botocore.exceptions import ClientError

        attempts = ec2._BOOT_BUCKET_ATTEMPTS  # pyright: ignore[reportPrivateUsage]
        s3 = _BootBucketS3(*["OperationAborted"] * attempts)
        with pytest.raises(ClientError, match="OperationAborted"):
            self._ensure(s3)
        assert s3.creates == attempts

    def test_any_other_error_raises_at_once(self) -> None:
        from botocore.exceptions import ClientError

        s3 = _BootBucketS3("AccessDenied")
        with pytest.raises(ClientError, match="AccessDenied"):
            self._ensure(s3)
        assert s3.creates == 1


class TestProgressOutput:
    def test_a_line_reaches_a_redirected_stdout_as_it_is_printed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        written = io.BytesIO()
        monkeypatch.setattr(
            ec2.sys, "stdout", io.TextIOWrapper(written, encoding="utf-8", newline="\n")
        )
        ec2._line_buffer_stdout()  # pyright: ignore[reportPrivateUsage]
        print("launched")
        assert written.getvalue() == b"launched\n"

    def test_a_stdout_that_is_not_a_text_wrapper_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ec2.sys, "stdout", io.StringIO())
        ec2._line_buffer_stdout()  # pyright: ignore[reportPrivateUsage]


class _RunS3:
    """An S3 client stand-in for `cmd_run`: records the hand-off bucket writes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kwargs["Key"]))
        return {}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_object", kwargs["Key"]))
        return {}

    def generate_presigned_url(self, *_args: Any, **_kwargs: Any) -> str:
        return "https://example.test/boot/presigned"


class _AnyCall:
    """A client stand-in answering every operation with an empty response."""

    def __init__(self, **attributes: Any) -> None:
        self.__dict__.update(attributes)

    def __getattr__(self, _name: str) -> Any:
        return lambda *_args, **_kwargs: {"Account": "123456789012"}


def _run_args(**overrides: Any) -> Any:
    import argparse

    values: dict[str, Any] = dict(
        region=None,
        profile="bench",
        instance_type="m7i.xlarge",
        python="3.14",
        ubuntu_release="26.04",
        max_minutes=60,
        keep=False,
        on_demand=False,
        large_transfer_mb=1024,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


class TestRunTeardown:
    """`cmd_run`'s endings, with every AWS call answered by a stand-in."""

    def _stub(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        wait: Any = None,
        launch: Any = None,
        token: str | None = None,
        states: list[dict[str, Any]] | BaseException | None = None,
    ) -> tuple[_RunS3, _ScriptedEC2, list[str], list[str | None]]:
        import types

        import boto3

        from tests.utils import harness

        s3 = _RunS3()
        fake_ec2 = _ScriptedEC2([{"State": {"Name": "terminated"}}] if states is None else states)
        deleted: list[str] = []
        profiles: list[str | None] = []

        def delete_bucket(_client: Any, name: str) -> None:
            deleted.append(name)
            # In the instance's own record, so a test can read the order.
            fake_ec2.calls.append("delete_bucket")

        def resolve_region(_explicit: str | None = None, *, profile: str | None = None) -> str:
            profiles.append(profile)
            return "ap-northeast-1"

        def done(*_args: Any, **_kwargs: Any) -> Outcome:
            return Outcome("completed", "0", None, None, 30.0)

        frozen = types.SimpleNamespace(token=token)
        credentials = types.SimpleNamespace(get_frozen_credentials=lambda: frozen)
        clients = {"ec2": fake_ec2, "s3": s3}
        monkeypatch.setattr(awsenv, "refuse_dev_stack_env", lambda: None)
        monkeypatch.setattr(awsenv, "resolve_region", resolve_region)
        monkeypatch.setattr(ec2, "_client", lambda kind, *_a: clients.get(kind, _AnyCall()))
        monkeypatch.setattr(ec2, "_describe_instance_type", lambda *_a: ("x86_64", 16384))
        monkeypatch.setattr(ec2, "_resolve_ami", lambda *_a: "ami-0123")
        monkeypatch.setattr(ec2, "_root_device_name", lambda *_a: "/dev/sda1")
        monkeypatch.setattr(ec2, "_require_instance_profile", lambda *_a: None)
        # Read from the aws-cli submodule, which CI does not check out.
        monkeypatch.setattr(ec2, "_pinned_aws_version", lambda: "2.36.40")
        monkeypatch.setattr(ec2, "_ensure_boot_bucket", lambda *_a: None)
        monkeypatch.setattr(ec2, "_archive_working_tree", lambda: b"tar")
        monkeypatch.setattr(ec2, "_launch", launch or (lambda *_a, **_k: "i-1"))
        monkeypatch.setattr(ec2, "_await_completion", wait or done)
        monkeypatch.setattr(
            ec2, "_download_results", lambda *_a: ["t_inprocess_r.jsonl", "t_e2e_r.jsonl"]
        )
        monkeypatch.setattr(ec2, "_dump_diagnostics", lambda *_a: None)
        monkeypatch.setattr(ec2.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(harness, "force_delete_bucket", delete_bucket)
        monkeypatch.setattr(
            boto3,
            "Session",
            lambda **_kwargs: types.SimpleNamespace(get_credentials=lambda: credentials),
        )
        return s3, fake_ec2, deleted, profiles

    @staticmethod
    def _interrupt(*_args: Any, **_kwargs: Any) -> Outcome:
        raise KeyboardInterrupt

    @staticmethod
    def _timed_out(*_args: Any, **_kwargs: Any) -> Outcome:
        return Outcome("timeout", None, "running", None, 65.0)

    def test_a_clean_run_tears_down_and_uses_the_launcher_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s3, fake_ec2, deleted, profiles = self._stub(monkeypatch)
        assert ec2.cmd_run(_run_args()) == 0
        assert profiles == ["bench"]
        # It has finished, so there is nothing to wait out.
        assert fake_ec2.calls == ["terminate_instances", "delete_bucket"]
        assert len(deleted) == 1
        assert [call for call, _key in s3.calls] == ["put_object", "delete_object"]

    def test_a_failed_launch_still_removes_the_tarball(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s3, fake_ec2, deleted, _profiles = self._stub(monkeypatch, launch=self._interrupt)
        with pytest.raises(KeyboardInterrupt):
            ec2.cmd_run(_run_args())
        assert [call for call, _key in s3.calls] == ["put_object", "delete_object"]
        assert fake_ec2.calls == []
        assert deleted == []

    def test_an_interrupted_wait_waits_out_the_instance_then_cleans_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s3, fake_ec2, _deleted, _profiles = self._stub(monkeypatch, wait=self._interrupt)
        with pytest.raises(KeyboardInterrupt):
            ec2.cmd_run(_run_args())
        # It may still be writing into its bucket: terminated first, then emptied.
        assert fake_ec2.calls == ["terminate_instances", "describe_instances", "delete_bucket"]
        assert ("delete_object", s3.calls[0][1]) in s3.calls

    def test_a_second_interrupt_abandons_the_wait_and_leaves_the_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s3, fake_ec2, deleted, _profiles = self._stub(
            monkeypatch, wait=self._interrupt, states=KeyboardInterrupt()
        )
        with pytest.raises(KeyboardInterrupt):
            ec2.cmd_run(_run_args())
        assert fake_ec2.calls == ["terminate_instances", "describe_instances"]
        assert deleted == []
        assert [call for call, _key in s3.calls] == ["put_object", "delete_object"]

    def test_the_launcher_timeout_waits_out_the_instance_too(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _s3, fake_ec2, _deleted, _profiles = self._stub(monkeypatch, wait=self._timed_out)
        assert ec2.cmd_run(_run_args()) == 2
        assert fake_ec2.calls == ["terminate_instances", "describe_instances", "delete_bucket"]
        # The launcher terminated it, so the verdict must not promise a budget.
        assert "self-terminates" not in capsys.readouterr().out

    def test_a_failed_wait_is_reported_and_still_removes_the_bucket(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from botocore.exceptions import EndpointConnectionError

        _s3, fake_ec2, _deleted, _profiles = self._stub(
            monkeypatch,
            wait=self._timed_out,
            states=EndpointConnectionError(endpoint_url="https://ec2.test"),
        )
        assert ec2.cmd_run(_run_args()) == 2
        assert fake_ec2.calls[-1] == "delete_bucket"
        out = capsys.readouterr().out
        # The terminate call went through; only the wait for it failed.
        assert "could not read the state of i-1" in out
        assert "could not terminate" not in out

    def test_keep_leaves_a_timed_out_run_to_the_instance(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        s3, fake_ec2, deleted, _profiles = self._stub(monkeypatch, wait=self._timed_out)
        assert ec2.cmd_run(_run_args(keep=True)) == 2
        assert fake_ec2.calls == []
        assert deleted == []
        assert [call for call, _key in s3.calls] == ["put_object"]
        assert "removes the bucket, not the tarball" in capsys.readouterr().out

    def test_a_mode_that_brought_back_no_file_is_named(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._stub(monkeypatch)
        monkeypatch.setattr(ec2, "_download_results", lambda *_a: ["t_e2e_r.jsonl"])
        assert ec2.cmd_run(_run_args()) == 0
        assert "no results file came back for the inprocess mode" in capsys.readouterr().out

    def test_keep_leaves_an_unfinished_run_to_the_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s3, fake_ec2, deleted, _profiles = self._stub(monkeypatch, wait=self._interrupt)
        with pytest.raises(KeyboardInterrupt):
            ec2.cmd_run(_run_args(keep=True))
        # The instance may not have fetched the code yet, so the tarball stays too.
        assert fake_ec2.calls == []
        assert deleted == []
        assert [call for call, _key in s3.calls] == ["put_object"]

    def test_keep_changes_nothing_once_the_run_has_ended(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        s3, fake_ec2, deleted, _profiles = self._stub(monkeypatch)
        assert ec2.cmd_run(_run_args(keep=True)) == 0
        assert "terminate_instances" in fake_ec2.calls
        assert len(deleted) == 1
        assert [call for call, _key in s3.calls] == ["put_object", "delete_object"]

    def test_a_failed_terminate_still_removes_the_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from botocore.exceptions import EndpointConnectionError

        _s3, fake_ec2, deleted, _profiles = self._stub(monkeypatch)
        fake_ec2.fail_terminate = EndpointConnectionError(endpoint_url="https://ec2.test")
        assert ec2.cmd_run(_run_args()) == 0
        assert len(deleted) == 1

    def test_temporary_credentials_are_flagged(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._stub(monkeypatch, token="session-token")
        ec2.cmd_run(_run_args())
        assert "credentials are temporary" in capsys.readouterr().out
        self._stub(monkeypatch, token=None)
        ec2.cmd_run(_run_args())
        assert "temporary" not in capsys.readouterr().out

    def test_setup_iam_and_cleanup_use_the_launcher_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import types

        _s3, _ec2, _deleted, profiles = self._stub(monkeypatch)
        refused = type("Refused", (Exception,), {})
        iam = _AnyCall(
            exceptions=types.SimpleNamespace(
                EntityAlreadyExistsException=refused, LimitExceededException=refused
            )
        )
        monkeypatch.setattr(
            ec2,
            "_client",
            lambda kind, *_a: (
                iam
                if kind == "iam"
                else _ScriptedEC2([{"State": {"Name": "terminated"}}])
                if kind == "ec2"
                else _AnyCall(list_buckets=lambda: {"Buckets": []})
            ),
        )
        ec2.cmd_setup_iam(_run_args())
        ec2.cmd_cleanup(_run_args())
        assert profiles == ["bench", "bench"]
