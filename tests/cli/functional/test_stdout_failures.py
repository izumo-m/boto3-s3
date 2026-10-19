"""A stdout that cannot take the output, end to end through the process's exit.

Bytes a standard stream refuses stay in Python's buffer, and the interpreter
flushes the streams again as it exits: a failure there replaces any exit code
with 120. Only a real process shows that, so each case runs the CLI in a fresh
subprocess (against moto) whose stdout is a pipe nobody reads or ``/dev/full``,
and checks the code the *process* ends with (design/cli.md section 6). POSIX
only: the cases need a pipe whose reader is gone, or ``/dev/full``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="needs POSIX pipes and devices")

_REPO_ROOT = Path(__file__).resolve().parents[3]

_BUCKET = "stdout-failure-bucket"

# Runs the CLI the way its console script does (``sys.exit(main())``), so the
# interpreter's own flush at exit is part of what is measured. Nothing but the
# CLI may write to stdout: it is the stream under test.
_CHILD = f"""\
import sys

import boto3
from moto import mock_aws

from boto3_s3_cli import cli

with mock_aws():
    client = boto3.session.Session().client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="{_BUCKET}")
    client.put_object(Bucket="{_BUCKET}", Key="small.txt", Body=b"payload")
    rc = cli.main(sys.argv[1:])
sys.exit(rc)
"""


def _run(tmp_path: Path, argv: list[str], stdout: int) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(_REPO_ROOT), env.get("PYTHONPATH")) if path
    )
    return subprocess.run(
        [sys.executable, str(script), *argv],
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
        env=env,
        cwd=str(tmp_path),
    )


def _run_into_a_pipe_nobody_reads(
    tmp_path: Path, argv: list[str]
) -> subprocess.CompletedProcess[str]:
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        return _run(tmp_path, argv, write_end)
    finally:
        os.close(write_end)


def _run_into_dev_full(tmp_path: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    if not os.path.exists("/dev/full"):
        pytest.skip("no /dev/full on this host")
    with open("/dev/full", "wb") as full:
        return _run(tmp_path, argv, full.fileno())


def test_a_download_whose_reader_is_gone_fails_with_its_line(tmp_path: Path) -> None:
    # The object fits Python's buffer, so only the flush as the item settles
    # meets the closed pipe.
    proc = _run_into_a_pipe_nobody_reads(tmp_path, ["cp", f"s3://{_BUCKET}/small.txt", "-"])
    assert proc.returncode == 1, proc.stderr
    assert f"download failed: s3://{_BUCKET}/small.txt to - [Errno 32] Broken pipe" in proc.stderr
    assert "Exception ignored" not in proc.stderr


def test_a_download_to_a_full_stdout_fails_with_its_line(tmp_path: Path) -> None:
    proc = _run_into_dev_full(tmp_path, ["cp", f"s3://{_BUCKET}/small.txt", "-"])
    assert proc.returncode == 1, proc.stderr
    assert f"download failed: s3://{_BUCKET}/small.txt to - [Errno 28]" in proc.stderr
    assert "Exception ignored" not in proc.stderr


def test_an_upload_whose_result_line_is_lost_exits_255(tmp_path: Path) -> None:
    # The upload succeeds; the `upload:` line it asked for cannot be written.
    (tmp_path / "a.txt").write_bytes(b"x")
    proc = _run_into_dev_full(tmp_path, ["cp", "a.txt", f"s3://{_BUCKET}/a.txt"])
    assert proc.returncode == 255, proc.stderr
    assert "boto3-s3: [ERROR]: [Errno 28] No space left on device" in proc.stderr
    assert "Exception ignored" not in proc.stderr


def test_a_listing_whose_reader_is_gone_keeps_its_255(tmp_path: Path) -> None:
    # `ls | head`: the listing's own write fails and is reported once; the
    # failed flush at the end changes neither the code nor the report.
    proc = _run_into_a_pipe_nobody_reads(tmp_path, ["ls", f"s3://{_BUCKET}/"])
    assert proc.returncode == 255, proc.stderr
    assert proc.stderr.count("[Errno 32] Broken pipe") == 1, proc.stderr
    assert "Exception ignored" not in proc.stderr
