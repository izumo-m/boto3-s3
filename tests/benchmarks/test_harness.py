"""Unit coverage for the benchmark harness pieces that do not need MinIO.

The scenario sets are built and inspected (names, payloads, the shared
large-transfer knob), the per-invocation runner is exercised on trivial
commands (timing, capture, peak RSS, timeout containment), and the report's
throughput / peak-RSS table is rendered from synthetic records with known
arithmetic. Nothing here talks to S3.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from benchmarks import awsenv, e2e, inprocess, report, results
from benchmarks.core import DEFAULT_LARGE_MB, BenchmarkError, ScenarioResult

_MIB = 1024 * 1024
_POSIX = hasattr(os, "fork") and hasattr(os, "killpg")


class TestScenarioSets:
    def test_e2e_has_the_sync_work_scenarios(self) -> None:
        names = {s.name for s in e2e.build_scenarios(False)}
        assert {"sync_noop_10k", "sync_tiny", "sync_changed_10k", "sync_delete_10k"} <= names

    def test_e2e_transfer_scenarios_record_a_payload(self) -> None:
        by_name = {s.name: s for s in e2e.build_scenarios(False)}
        assert by_name["sync_tiny"].payload_bytes == 11 * 1024
        assert by_name["sync_changed_10k"].payload_bytes == 2_000 * 1024
        assert by_name["cp_upload_small_1k"].payload_bytes == 1_000 * 4096
        assert by_name["cp_upload_large"].payload_bytes == DEFAULT_LARGE_MB * _MIB
        # Listing, deletion and the probes move nothing.
        for name in ("ls_recursive_10k", "sync_noop_10k", "sync_delete_10k", "rm_recursive_2k"):
            assert by_name[name].payload_bytes is None

    def test_large_transfer_knob_moves_both_modes(self) -> None:
        big = {s.name: s for s in e2e.build_scenarios(False, large_mb=256)}
        assert big["cp_upload_large"].dimensions["file_size"] == "256MB"
        assert big["cp_download_large"].payload_bytes == 256 * _MIB
        inproc = {s.name: s for s in inprocess.build_scenarios(False, large_mb=256)}
        assert inproc["inproc_cp_upload_large"].dimensions["file_size"] == "256MB"

    def test_quick_keeps_a_multipart_size_regardless_of_the_knob(self) -> None:
        quick = {s.name: s for s in e2e.build_scenarios(True, large_mb=1024)}
        assert quick["cp_upload_large"].dimensions["file_size"] == "9MB"

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


class TestLanes:
    def _write(self, mode: str, rev: str, lane: str, stamp: str) -> Path:
        meta = results.RunMeta(
            mode=mode,
            timestamp_utc=stamp,
            git_rev=rev,
            git_dirty=False,
            python="3.14.7",
            platform="test",
            versions={},
            aws_version=None,
            options={},
            lane=lane,
        )
        return results.write_run(meta, [])

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
        meta = results.collect_meta("inprocess", {})
        assert meta.lane == "local"
        assert meta.git_rev != "unknown"
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

    def test_bucket_is_fixed_only_on_minio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://127.0.0.1:9000")
        assert e2e._bench_bucket() == e2e.BUCKET
        # An explicit real-S3 endpoint is still the global namespace.
        monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://s3.eu-west-1.amazonaws.com")
        assert e2e._bench_bucket().startswith(e2e.BUCKET + "-")
        monkeypatch.delenv("AWS_ENDPOINT_URL_S3")
        assert e2e._bench_bucket().startswith(e2e.BUCKET + "-")

    def test_real_s3_refuses_a_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
        monkeypatch.setenv("AWS_PROFILE", "someone")
        with pytest.raises(BenchmarkError, match="AWS_PROFILE"):
            e2e._check_remote_credentials()


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
