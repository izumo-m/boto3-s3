"""Unit coverage for the benchmark harness pieces that do not need the dev stack.

The scenario sets are built and inspected (names, payloads, the shared
large-transfer knob), the per-invocation runner is exercised on trivial
commands (timing, capture, peak RSS, timeout containment), and the report's
throughput / peak-RSS table is rendered from synthetic records with known
arithmetic. Nothing here talks to S3.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from benchmarks import awsenv, e2e, inprocess, report, results
from benchmarks.core import DEFAULT_LARGE_MB, BenchmarkError, ScenarioResult
from benchmarks.e2e import E2EEnv, E2EScenario
from benchmarks.inprocess import Prepared

if TYPE_CHECKING:
    from collections.abc import Callable

_MIB = 1024 * 1024
_POSIX = hasattr(os, "fork") and hasattr(os, "killpg")


class TestScenarioSets:
    def test_e2e_has_the_sync_work_scenarios(self) -> None:
        names = {s.name for s in e2e.build_scenarios(False)}
        assert {"sync_noop_10k", "sync_tiny", "sync_changed_10k", "sync_delete_10k"} <= names

    def test_e2e_transfer_scenarios_record_a_payload(self) -> None:
        by_name = {s.name: s for s in e2e.build_scenarios(False)}
        assert by_name["sync_tiny"].payload_bytes == 11 * 1024
        assert by_name["sync_changed_10k"].payload_bytes == 2_000 * 1025
        assert by_name["cp_upload_small_1k"].payload_bytes == 1_000 * 4096
        assert by_name["cp_upload_large"].payload_bytes == DEFAULT_LARGE_MB * _MIB
        assert by_name["mv_download_small_1k"].payload_bytes == 1_000 * 4096
        # Listing, deletion, the server-side copy and the probes move nothing
        # through the client.
        for name in (
            "ls_recursive_10k",
            "sync_noop_10k",
            "sync_delete_10k",
            "rm_recursive_2k",
            "mv_s3_to_s3_1k",
        ):
            assert by_name[name].payload_bytes is None

    def test_mv_has_a_crt_lane_only_for_the_download(self) -> None:
        by_name = {s.name: s for s in e2e.build_scenarios(False)}
        # Both tools copy S3 to S3 on the classic engine whatever is configured.
        assert not by_name["mv_s3_to_s3_1k"].crt_capable
        assert by_name["mv_download_small_1k"].crt_capable
        # The download mirrors cp_download_small_1k so the two rows compare.
        assert by_name["mv_download_small_1k"].dimensions == (
            by_name["cp_download_small_1k"].dimensions
        )

    def test_large_transfer_knob_moves_both_modes(self) -> None:
        big = {s.name: s for s in e2e.build_scenarios(False, large_mb=256)}
        assert big["cp_upload_large"].dimensions["file_size"] == "256MB"
        assert big["cp_download_large"].payload_bytes == 256 * _MIB
        inproc = {s.name: s for s in inprocess.build_scenarios(False, large_mb=256)}
        assert inproc["inproc_cp_upload_large"].dimensions["file_size"] == "256MB"

    def test_quick_keeps_a_multipart_size_regardless_of_the_knob(self) -> None:
        quick = {s.name: s for s in e2e.build_scenarios(True, large_mb=1024)}
        assert quick["cp_upload_large"].dimensions["file_size"] == "9MB"

    def _env(self, tmp_path: Path, client: object = None) -> E2EEnv:
        return E2EEnv(
            client=client,
            bucket="b",
            engine="classic",
            workdir=tmp_path,
            ours_exe="boto3-s3",
            aws_exe="aws",
            overlay={},
        )

    def test_the_changed_sync_restages_its_local_side_without_a_request(
        self, tmp_path: Path
    ) -> None:
        from benchmarks import workload

        scenario = {s.name: s for s in e2e.build_scenarios(True)}["sync_changed_10k"]
        assert scenario.reset is not None
        env = self._env(tmp_path)  # no client: any S3 request would fail
        tree = env.dir_for("sync_changed") / "tree"
        workload.generate_tree(tree, 100, 1024)
        changed, untouched = workload.rel_paths(100)[0], workload.rel_paths(100)[-1]
        sizes: list[int] = []
        for _ in range(3):
            scenario.reset(env)
            sizes.append((tree / changed).stat().st_size)
        # Each invocation uploads the subset, so consecutive sizes differ, and
        # none is the corpus's own 1 KB.
        assert sizes[0] != sizes[1] != sizes[2]
        assert 1024 not in sizes
        assert (tree / untouched).stat().st_size == 1024

    def test_the_s3_move_bounces_between_two_prefixes(self, tmp_path: Path) -> None:
        class _Puts:
            def put_object(self, **_kwargs: object) -> None:
                return None

        scenario = {s.name: s for s in e2e.build_scenarios(True)}["mv_s3_to_s3_1k"]
        assert scenario.setup is not None and scenario.reset is not None
        env = self._env(tmp_path, _Puts())
        scenario.setup(env)
        moves: list[tuple[str, str]] = []
        for _ in range(3):
            scenario.reset(env)
            argv = scenario.make_argv(env, e2e.Side.OURS)
            moves.append((argv[-2].rsplit("/", 2)[-2], argv[-1].rsplit("/", 2)[-2]))
        # The copies of one invocation are the source of the next: no re-seed.
        assert moves == [("a", "b"), ("b", "a"), ("a", "b")]
        # So an invocation that moved nothing must not pass unnoticed.
        assert scenario.verify_each

    def test_inprocess_has_the_changed_sync(self) -> None:
        by_name = {s.name: s for s in inprocess.build_scenarios(False)}
        assert by_name["inproc_sync_changed_20k"].dimensions["changed_count"] == "2000"


@pytest.mark.skipif(not _POSIX, reason="the intermediary needs fork/killpg")
class TestRunChild:
    def test_times_captures_and_measures_a_trivial_child(self) -> None:
        elapsed, result, rss = e2e._run_child(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
            dict(os.environ),
        )
        assert result.rc == 0
        assert result.stdout.strip() == "out"
        assert result.stderr.strip() == "err"
        assert 0 < elapsed < 10
        # A bare interpreter is a few MiB; anything under 1 MiB or over 1 GiB
        # means the number is not this child's.
        assert rss is not None and 1 * _MIB < rss < 1024 * _MIB

    def test_reports_the_child_exit_code(self) -> None:
        _elapsed, result, _rss = e2e._run_child(
            [sys.executable, "-c", "raise SystemExit(3)"], dict(os.environ)
        )
        assert result.rc == 3

    def test_timeout_kills_the_child_and_surfaces(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        monkeypatch.setattr(e2e, "_SUBPROCESS_TIMEOUT", 0.3)
        with pytest.raises(subprocess.TimeoutExpired):
            e2e._run_child([sys.executable, "-c", "import time; time.sleep(30)"], dict(os.environ))


class TestResultRecord:
    def test_optional_fields_only_when_present(self) -> None:
        bare = ScenarioResult("s", "e2e", "classic", {}, {"boto3-s3": [1.0]}, ["boto3-s3"])
        assert "rss" not in bare.record()
        assert "payload_bytes" not in bare.record()
        full = ScenarioResult(
            "s",
            "e2e",
            "classic",
            {},
            {"boto3-s3": [1.0]},
            ["boto3-s3"],
            rss={"boto3-s3": [2.0]},
            payload_bytes=7,
        )
        record = full.record()
        assert record["rss"] == {"boto3-s3": [2.0]}
        assert record["rss_unit"] == "bytes"
        assert record["payload_bytes"] == 7


class TestResourceTable:
    def _records(self) -> list[dict[str, object]]:
        return [
            {
                "scenario": "startup_minimal",
                "engine": "classic",
                "dimensions": {},
                "samples": {"boto3-s3": [0.2], "aws": [0.4]},
                "rss": {"boto3-s3": [60 * _MIB], "aws": [80 * _MIB]},
            },
            {
                "scenario": "cp_upload_large",
                "engine": "classic",
                "dimensions": {"file_size": "64MB"},
                "samples": {"boto3-s3": [1.2], "aws": [2.4]},
                "rss": {"boto3-s3": [60 * _MIB], "aws": [80 * _MIB]},
                "payload_bytes": 64 * _MIB,
            },
            {
                "scenario": "rm_recursive_2k",
                "engine": "classic",
                "dimensions": {},
                "samples": {"boto3-s3": [0.5], "aws": [0.6]},
            },
        ]

    def test_throughput_uses_the_net_median_and_rss_the_per_side_median(self) -> None:
        records = self._records()
        meta = {"mode": "e2e", "git_rev": "abc", "timestamp_utc": "t", "python": "3.14.7"}
        text, _flagged = report.render((meta, records), None)
        rows = {
            line.split()[0]: line
            for line in text.splitlines()
            if line.strip() and "MiB" not in line
        }
        # 64 MiB over (1.2 - 0.2) s and over (2.4 - 0.4) s.
        assert rows["cp_upload_large"].split()[2:] == ["64.0", "32.0", "60.0", "80.0", "0.75"]
        # No payload: the throughput cells stay empty, the RSS cells do not.
        assert rows["startup_minimal"].split()[2:] == ["-", "-", "60.0", "80.0", "0.75"]
        # Neither payload nor RSS: no row in the resource table at all.
        assert "rm_recursive_2k" not in text.split("throughput =", 1)[1]

    def test_no_resource_table_without_data(self) -> None:
        records = [self._records()[2]]
        meta = {"mode": "e2e", "git_rev": "abc", "timestamp_utc": "t", "python": "3.14.7"}
        text, _flagged = report.render((meta, records), None)
        assert "throughput =" not in text

    def test_a_raw_row_has_no_throughput(self) -> None:
        # sync_tiny's net is noise the main table does not show; no rate over it.
        records = [
            self._records()[0],
            {
                "scenario": "sync_tiny",
                "engine": "classic",
                "dimensions": {},
                "samples": {"boto3-s3": [0.3], "aws": [0.5]},
                "rss": {"boto3-s3": [60 * _MIB], "aws": [80 * _MIB]},
                "payload_bytes": 11 * 1024,
            },
        ]
        meta = {"mode": "e2e", "git_rev": "abc", "timestamp_utc": "t", "python": "3.14.7"}
        text, _flagged = report.render((meta, records), None)
        resource = text.split("throughput =", 1)[1]
        row = next(line for line in resource.splitlines() if line.startswith("sync_tiny"))
        assert row.split()[2:4] == ["-", "-"]


class TestLanes:
    def _write(
        self, mode: str, rev: str, lane: str, stamp: str, *, handicap_ms: float = 0.0
    ) -> Path:
        meta = results.RunMeta(
            mode=mode,
            timestamp_utc=stamp,
            git_rev=rev,
            git_dirty=False,
            python="3.14.7",
            platform="test",
            versions={},
            aws_version=None,
            options={"handicap_ms": handicap_ms},
            lane=lane,
        )
        return results.write_run(meta, [])

    def test_last_is_the_run_before_the_one_reported(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(results, "RESULTS_DIR", tmp_path)
        first = self._write("e2e", "aaaaaaaaaa", "local", "20260101-000000")
        second = self._write("e2e", "bbbbbbbbbb", "local", "20260102-000000")
        self._write("e2e", "cccccccccc", "local", "20260103-000000")
        # Re-rendering an older run compares it with the one before it, not a later one.
        assert results.resolve_baseline("last", "e2e", exclude=second) == first
        with pytest.raises(BenchmarkError, match="no stored e2e runs"):
            results.resolve_baseline("last", "e2e", exclude=first)

    def test_a_self_test_run_is_never_a_baseline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(results, "RESULTS_DIR", tmp_path)
        real = self._write("e2e", "aaaaaaaaaa", "local", "20260101-000000")
        self._write("e2e", "bbbbbbbbbb", "local", "20260102-000000", handicap_ms=50.0)
        current = self._write("e2e", "cccccccccc", "local", "20260103-000000")
        assert results.resolve_baseline("last", "e2e", exclude=current) == real
        with pytest.raises(BenchmarkError, match="matches baseline 'bbbb'"):
            results.resolve_baseline("bbbb", "e2e", exclude=current)

    def test_provenance_can_come_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(results.GIT_REV_ENV, "0123456789abcdef0123")
        monkeypatch.setenv(results.LANE_ENV, "ec2-m7i.xlarge-ubuntu26.04")
        monkeypatch.setenv(results.IMAGE_ENV, "ami-0123")
        monkeypatch.delenv(results.GIT_DIRTY_ENV, raising=False)
        meta = results.collect_meta("e2e", {})
        assert meta.git_rev == "0123456789"
        assert meta.git_dirty is False
        assert meta.lane == "ec2-m7i.xlarge-ubuntu26.04"
        assert meta.record()["lane"] == "ec2-m7i.xlarge-ubuntu26.04"
        assert meta.record()["image"] == "ami-0123"

    def test_default_lane_is_local_and_git_backed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(results.GIT_REV_ENV, raising=False)
        monkeypatch.delenv(results.LANE_ENV, raising=False)
        monkeypatch.delenv(results.IMAGE_ENV, raising=False)
        # Stub git so the test holds outside a work tree too (a copy without
        # `.git` legitimately yields "unknown").
        answers = {("rev-parse", "--short=10", "HEAD"): "0123456789", ("status", "--porcelain"): ""}
        monkeypatch.setattr(results, "_git", lambda *args: answers[args])
        meta = results.collect_meta("inprocess", {})
        assert meta.lane == "local"
        assert meta.git_rev == "0123456789"
        assert meta.git_dirty is False
        assert "image" not in meta.record()

    def test_filename_spells_a_non_local_lane_and_baselines_stay_in_lane(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(results, "RESULTS_DIR", tmp_path)
        local_old = self._write("e2e", "aaaaaaaaaa", "local", "20260101-000000")
        ec2_new = self._write("e2e", "bbbbbbbbbb", "ec2-m7i.xlarge", "20260102-000000")
        local_new = self._write("e2e", "cccccccccc", "local", "20260103-000000")
        assert local_old.name == "20260101-000000_e2e_aaaaaaaaaa.jsonl"
        assert ec2_new.name == "20260102-000000_e2e_bbbbbbbbbb.ec2-m7i.xlarge.jsonl"
        assert results.lane_of(ec2_new) == "ec2-m7i.xlarge"
        assert results.lane_of(local_new) == "local"
        # `last` for the local lane skips the newer EC2 file; the EC2 lane
        # sees only its own; a revision prefix is scoped the same way.
        assert results.resolve_baseline("last", "e2e", lane="local", exclude=local_new) == local_old
        # `report <file> --baseline last` excludes by the path as typed, which
        # is usually relative while stored runs are absolute.
        monkeypatch.chdir(tmp_path.parent)
        relative = Path(tmp_path.name) / local_new.name
        assert results.resolve_baseline("last", "e2e", lane="local", exclude=relative) == local_old
        assert results.resolve_baseline("last", "e2e", lane="ec2-m7i.xlarge") == ec2_new
        assert results.resolve_baseline("bbbb", "e2e", lane="ec2-m7i.xlarge") == ec2_new
        with pytest.raises(Exception, match="lane 'local'"):
            results.resolve_baseline("bbbb", "e2e", lane="local")

    def test_report_header_names_the_lane_and_a_cross_lane_baseline(self) -> None:
        current = (
            {
                "mode": "inprocess",
                "git_rev": "c",
                "timestamp_utc": "t2",
                "python": "3.14.7",
                "lane": "ec2-m7i.xlarge",
            },
            [],
        )
        baseline = (
            {"mode": "inprocess", "git_rev": "a", "timestamp_utc": "t1", "python": "3.14.7"},
            [],
        )
        text, _flagged = report.render(current, baseline)
        assert "[ec2-m7i.xlarge]" in text.splitlines()[0]
        assert "lane: ec2-m7i.xlarge now, local in the baseline" in text


class TestRemoteGuards:
    def test_opt_in_is_exactly_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ("0", "false", "no", ""):
            monkeypatch.setenv(awsenv.ALLOW_REMOTE_ENV, value)
            assert awsenv.remote_opted_in() is False
        monkeypatch.setenv(awsenv.ALLOW_REMOTE_ENV, "1")
        assert awsenv.remote_opted_in() is True

    def test_bucket_is_fixed_only_on_dev_stack(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(e2e.BUCKET_ENV, raising=False)
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:9000")
        assert e2e._bench_bucket() == e2e.BUCKET
        # An explicit real-S3 endpoint is still the global namespace.
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://s3.eu-west-1.amazonaws.com")
        assert e2e._bench_bucket().startswith(e2e.BUCKET + "-")
        monkeypatch.delenv("AWS_ENDPOINT_URL_S3")
        assert e2e._bench_bucket().startswith(e2e.BUCKET + "-")

    def test_launcher_may_name_the_bucket_inside_the_family(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
        monkeypatch.setenv(e2e.BUCKET_ENV, "boto3-s3-bench-20260906-000000-abc")
        assert e2e._bench_bucket() == "boto3-s3-bench-20260906-000000-abc"
        # Outside the family the instance role could not touch it: refused.
        monkeypatch.setenv(e2e.BUCKET_ENV, "someone-elses-bucket")
        with pytest.raises(BenchmarkError, match="must start with"):
            e2e._bench_bucket()
        # The dev stack ignores the override; its bucket is the documented fixed name.
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:9000")
        assert e2e._bench_bucket() == e2e.BUCKET

    def test_real_s3_refuses_a_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
        monkeypatch.setenv("AWS_PROFILE", "someone")
        with pytest.raises(BenchmarkError, match="AWS_PROFILE"):
            e2e._check_remote_credentials(require_region=True)

    def test_only_real_s3_requires_a_region(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Another endpoint (a MinIO on the LAN, say) is signed for us-east-1
        # when no region is set; real S3 needs one for CreateBucket.
        for name in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config-absent"))
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "A")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "B")
        e2e._check_remote_credentials(require_region=False)
        with pytest.raises(BenchmarkError, match="needs a region"):
            e2e._check_remote_credentials(require_region=True)

    def test_only_a_run_without_an_endpoint_is_asked_for_a_region(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config-absent"))
        monkeypatch.setenv(awsenv.ALLOW_REMOTE_ENV, "1")
        monkeypatch.setattr(e2e.shutil, "which", lambda name, path=None: f"/bin/{name}")
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://minio.example.test")
        assert e2e.check_environment() == ("/bin/boto3-s3", "/bin/aws")
        monkeypatch.delenv("AWS_ENDPOINT_URL_S3")
        with pytest.raises(BenchmarkError, match="needs a region"):
            e2e.check_environment()

    def test_a_profile_is_refused_before_the_region_is_read(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Resolving the region through an undefined profile would raise
        # ProfileNotFound instead of the refusal that says what to do.
        for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config-absent"))
        monkeypatch.setenv("AWS_PROFILE", "not-defined-here")
        with pytest.raises(BenchmarkError, match="cannot use AWS_PROFILE"):
            e2e._check_remote_credentials(require_region=True)

    def test_an_explicit_remote_endpoint_is_checked_like_real_s3(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://s3.ap-northeast-1.amazonaws.com")
        monkeypatch.setenv(awsenv.ALLOW_REMOTE_ENV, "1")
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
        monkeypatch.setenv("AWS_DEFAULT_PROFILE", "someone")
        with pytest.raises(BenchmarkError, match="AWS_DEFAULT_PROFILE"):
            e2e.check_environment()

    def test_remote_credentials_ignore_the_host_files(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A [default] key in the credentials file is invisible to both CLIs
        # (their home is the run's), so the harness must not find it either.
        credentials = tmp_path / "credentials"
        credentials.write_text("[default]\naws_access_key_id = A\naws_secret_access_key = B\n")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config-absent"))
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
        # No instance metadata here; keep the lookup from waiting on it.
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        assert e2e._harness_session().get_credentials() is None
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:9000")
        assert e2e._harness_session().get_credentials() is not None

    def test_children_run_without_the_host_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_PROFILE", "someone")
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/somewhere/credentials")
        env = e2e._child_env({"HOME": "/run/home"})
        assert "AWS_PROFILE" not in env
        assert "AWS_SHARED_CREDENTIALS_FILE" not in env
        assert env["HOME"] == "/run/home"

    def test_the_hand_off_bucket_is_never_the_run_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
        monkeypatch.setenv(e2e.BUCKET_ENV, "boto3-s3-bench-boot-123456789012-ap-northeast-1")
        with pytest.raises(BenchmarkError, match="hand-off"):
            e2e._bench_bucket()

    def test_ours_is_the_console_script_of_the_running_interpreter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:9000")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "devadmin")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "devadmin")
        # A boto3-s3 earlier on PATH (a pipx install, say) is not the tree under
        # test; only the interpreter's own directory counts. The host may have
        # no aws at all, so that lookup is answered here.
        real_which = shutil.which
        bindir = str(Path(sys.executable).parent)
        searched: list[tuple[str, str | None]] = []
        aws_next_to_python: list[str | None] = [None]

        def which(name: str, mode: int = os.F_OK | os.X_OK, path: str | None = None) -> str | None:
            searched.append((name, path))
            if name == "aws":
                return aws_next_to_python[0] if path == bindir else "/usr/bin/aws"
            return real_which(name, mode, path)

        monkeypatch.setattr(e2e.shutil, "which", which)
        ours, aws = e2e.check_environment()
        assert Path(ours).parent == Path(bindir)
        # No aws next to the interpreter (Windows): PATH, after the interpreter's own.
        assert searched == [("boto3-s3", bindir), ("aws", bindir), ("aws", None)]
        assert aws == "/usr/bin/aws"
        # The pinned aws next to the interpreter wins over one earlier on PATH.
        aws_next_to_python[0] = bindir + "/aws"
        _ours, aws = e2e.check_environment()
        assert aws == bindir + "/aws"

    def test_a_run_warns_when_aws_is_not_the_pinned_version(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from benchmarks import cli
        from tests.utils import golden

        monkeypatch.setattr(e2e, "check_environment", lambda: ("boto3-s3", "/usr/bin/aws"))
        monkeypatch.setattr(e2e, "aws_version", lambda _exe: "aws-cli/2.38.5 Python/3.13.9")
        monkeypatch.setattr(golden, "pinned_aws_version", lambda: "2.36.40")
        # No work scenario matches, so the mode is skipped after the check.
        assert cli.main(["run", "--mode", "e2e", "--scenario", "no-such-scenario"]) == 0
        assert "not the pinned aws-cli 2.36.40" in capsys.readouterr().err
        monkeypatch.setattr(e2e, "aws_version", lambda _exe: "aws-cli/2.36.40 Python/3.14.6")
        assert cli.main(["run", "--mode", "e2e", "--scenario", "no-such-scenario"]) == 0
        assert "not the pinned" not in capsys.readouterr().err


class _RecordingClient:
    """An S3 client stand-in that records the operations the runner sends."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[str] = []
        self.meta = type("Meta", (), {"region_name": "ap-northeast-1"})()
        self._fail = fail

    def __getattr__(self, name: str) -> object:
        def call(**_kwargs: object) -> dict[str, object]:
            self.calls.append(name)
            if name == self._fail:
                from botocore.exceptions import ClientError

                raise ClientError({"Error": {"Code": "Refused"}}, name)
            return {}

        return call


class TestRunBucketLifecycle:
    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        client: _RecordingClient,
        *,
        endpoint: str | None,
        scenarios: list[E2EScenario] | None = None,
    ) -> tuple[list[ScenarioResult], list[str]]:
        from tests.utils import harness

        if endpoint is None:
            monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
        else:
            monkeypatch.setenv("AWS_ENDPOINT_URL_S3", endpoint)
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
        monkeypatch.setattr(e2e, "check_environment", lambda: ("boto3-s3", "aws"))
        monkeypatch.setattr(e2e, "_build_client", lambda: client)
        monkeypatch.setattr(
            harness, "delete_under", lambda c, _b, _p: c.calls.append("delete_under")
        )
        return e2e.run_scenarios(scenarios or [], engines=("classic",), log=lambda _line: None)

    def test_real_s3_never_lists_the_account_buckets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The EC2 instance role holds S3 on the benchmark buckets only, so a
        # ListBuckets (s3:ListAllMyBuckets) would fail the whole E2E mode.
        client = _RecordingClient()
        _results, failures = self._run(monkeypatch, client, endpoint=None)
        assert client.calls == ["create_bucket", "delete_under", "delete_bucket"]
        assert failures == []

    def test_dev_stack_clears_its_fixed_name_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _RecordingClient()
        self._run(monkeypatch, client, endpoint="http://127.0.0.1:9000")
        assert client.calls[0] == "list_buckets"
        assert client.calls[-2:] == ["delete_under", "delete_bucket"]

    def test_a_failed_bucket_delete_is_a_failure_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _RecordingClient(fail="delete_bucket")
        _results, failures = self._run(monkeypatch, client, endpoint=None)
        assert len(failures) == 1
        assert "could not delete bucket" in failures[0]

    def test_a_bucket_that_was_never_created_is_never_deleted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from botocore.exceptions import ClientError

        client = _RecordingClient(fail="create_bucket")
        with pytest.raises(ClientError):
            self._run(monkeypatch, client, endpoint=None)
        assert client.calls == ["create_bucket"]

    def test_an_s3_error_staging_one_scenario_fails_only_that_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from botocore.exceptions import ClientError

        from tests.utils.harness import CliResult

        monkeypatch.setattr(e2e, "_run_child", lambda *_args: (0.1, CliResult(0, "", ""), None))

        def refused(_env: E2EEnv) -> None:
            raise ClientError({"Error": {"Code": "SlowDown"}}, "PutObject")

        scenarios = [
            e2e.E2EScenario(name="broken", dimensions={}, make_argv=lambda *_a: [], reset=refused),
            e2e.E2EScenario(name="fine", dimensions={}, make_argv=lambda *_a: [], samples=1),
        ]
        results_, failures = self._run(
            monkeypatch, _RecordingClient(), endpoint=None, scenarios=scenarios
        )
        assert [r.scenario for r in results_] == ["fine"]
        assert len(failures) == 1
        assert failures[0].startswith("[e2e/classic] broken:")

    def test_each_cli_runs_in_the_run_home_without_the_host_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.utils.harness import CliResult

        seen: list[dict[str, str]] = []

        def child(_cmd: list[str], env: dict[str, str]) -> tuple[float, CliResult, None]:
            seen.append(env)
            return 0.1, CliResult(0, "", ""), None

        monkeypatch.setattr(e2e, "_run_child", child)
        monkeypatch.setenv("AWS_PROFILE", "someone")
        scenario = e2e.E2EScenario(name="fine", dimensions={}, make_argv=lambda *_a: [], samples=1)
        self._run(monkeypatch, _RecordingClient(), endpoint=None, scenarios=[scenario])
        assert seen
        for env in seen:
            # Inside the run's work root: the test session's own HOME is
            # also a directory named "home".
            assert Path(env["HOME"]).parent.name.startswith("boto3-s3-bench-e2e-")
            assert env["USERPROFILE"] == env["HOME"]
            assert "AWS_PROFILE" not in env

    def test_verify_each_reads_every_timed_invocation_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.utils.harness import CliResult

        monkeypatch.setattr(e2e, "_run_child", lambda *_args: (0.1, CliResult(0, "", ""), None))
        calls = {"warmup_only": 0, "each": 0}

        def counting(name: str) -> Callable[..., None]:
            def verify(*_args: object) -> None:
                calls[name] += 1

            return verify

        scenarios = [
            e2e.E2EScenario(
                name=name,
                dimensions={},
                make_argv=lambda *_a: [],
                samples=1,
                verify=counting(name),
                verify_each=name == "each",
            )
            for name in calls
        ]
        self._run(monkeypatch, _RecordingClient(), endpoint=None, scenarios=scenarios)
        # Two warmups, then one round on each of the two sides.
        assert calls == {"warmup_only": 2, "each": 4}

    def test_a_timed_invocation_failing_verify_each_fails_the_scenario(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.utils.harness import CliResult

        monkeypatch.setattr(e2e, "_run_child", lambda *_args: (0.1, CliResult(0, "", ""), None))
        seen = []

        def verify(*_args: object) -> None:
            seen.append(1)
            if len(seen) > 2:  # past both warmups
                raise e2e.VerificationError("moved nothing")

        scenario = e2e.E2EScenario(
            name="bounce",
            dimensions={},
            make_argv=lambda *_a: [],
            samples=1,
            verify=verify,
            verify_each=True,
        )
        results_, failures = self._run(
            monkeypatch, _RecordingClient(), endpoint=None, scenarios=[scenario]
        )
        assert results_ == []
        assert "moved nothing" in failures[0]


class TestSelfTestHandicap:
    def test_the_probe_every_net_subtracts_is_spared(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Inflating startup_minimal too would cancel out of every net, and no
        # E2E ratio flag could fire in the self-test.
        from tests.utils.harness import CliResult

        monkeypatch.setattr(e2e, "_invoke", lambda *_args: (1.0, CliResult(0, "", ""), None))
        env = e2e.E2EEnv(
            client=None,
            bucket="b",
            engine="classic",
            workdir=tmp_path,
            ours_exe="boto3-s3",
            aws_exe="aws",
            overlay={},
        )

        def run(name: str) -> ScenarioResult:
            scenario = e2e.E2EScenario(name=name, dimensions={}, make_argv=lambda *_args: [])
            return e2e._run_scenario(
                env, scenario, samples_override=1, handicap=0.5, log=lambda _line: None
            )

        assert run("startup_minimal").samples == {"boto3-s3": [1.0], "aws": [1.0]}
        assert run("cp_upload_large").samples == {"boto3-s3": [1.5], "aws": [1.0]}


class TestInProcessFailures:
    def test_a_local_io_error_skips_only_that_scenario(self) -> None:
        def full_disk(_workdir: Path) -> Prepared:
            raise OSError(28, "No space left on device")

        scenarios = [
            inprocess.InProcScenario(name="broken", dimensions={}, prepare=full_disk),
            next(s for s in inprocess.build_scenarios(True) if s.name == "inproc_dispatch"),
        ]
        results_, failures = inprocess.run_scenarios(
            scenarios, samples_override=1, log=lambda _line: None
        )
        assert [r.scenario for r in results_] == ["inproc_dispatch"]
        assert len(failures) == 1
        assert "No space left" in failures[0]


class TestExitCodes:
    def test_an_unexpected_error_exits_2_not_the_flag_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from benchmarks import cli

        def broken() -> int:
            raise RuntimeError("harness bug")

        monkeypatch.setattr(cli, "_cmd_list", broken)
        assert cli.main(["list"]) == 2


class TestRawRows:
    def test_sync_tiny_is_reported_on_raw_medians(self) -> None:
        meta = {"mode": "e2e", "git_rev": "abc", "timestamp_utc": "t", "python": "3.14.7"}
        records: list[dict[str, object]] = [
            {
                "scenario": "startup_minimal",
                "engine": "classic",
                "dimensions": {},
                "samples": {"boto3-s3": [0.2], "aws": [0.4]},
            },
            {
                "scenario": "sync_tiny",
                "engine": "classic",
                "dimensions": {},
                "samples": {"boto3-s3": [0.3], "aws": [0.5]},
            },
        ]
        text, flagged = report.render((meta, records), None)
        row = next(line for line in text.splitlines() if line.startswith("sync_tiny"))
        # net and ratio cells are empty, like the probes; no flag from a
        # 0.1 s / 0.1 s "ratio" that would otherwise be noise.
        assert row.split()[2:] == ["0.300s", "-", "0.000s", "0.500s", "-", "-", "-", "-"]
        assert flagged is False

    def test_missing_startup_probe_blanks_the_adjusted_cells(self) -> None:
        meta = {"mode": "e2e", "git_rev": "abc", "timestamp_utc": "t", "python": "3.14.7"}
        records: list[dict[str, object]] = [
            {
                "scenario": "cp_upload_large",
                "engine": "classic",
                "dimensions": {},
                "samples": {"boto3-s3": [1.0], "aws": [2.0]},
                "payload_bytes": 64 * _MIB,
            }
        ]
        text, _flagged = report.render((meta, records), None)
        row = next(line for line in text.splitlines() if line.startswith("cp_upload_large"))
        # No startup_minimal in this run: net, ratio and throughput are "-",
        # never the raw figure passed off as adjusted.
        assert row.split()[3] == "-"
        assert row.split()[7] == "-"
        resource_row = [line for line in text.splitlines() if line.startswith("cp_upload_large")][1]
        assert resource_row.split()[2:4] == ["-", "-"]
