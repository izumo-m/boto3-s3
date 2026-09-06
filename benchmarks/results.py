"""Run metadata capture, JSONL result files, and baseline resolution.

One run of one mode produces one JSONL file under `benchmarks/results/`
(git-ignored): the first line is a ``meta`` record describing the run
environment, every following line is one scenario's ``result`` record.
The timestamped filename keeps the directory chronologically sorted, and the
embedded git revision is what ``--baseline <rev>`` matches against.

A run also belongs to a *lane* - ``local`` by default, ``ec2-<instance type>``
when the EC2 launcher ran it - and a non-local lane is spelled into the
filename after the revision. Baseline resolution never crosses lanes: a
``--baseline last`` on this host must not pick up a file the EC2 lane
downloaded here, since those numbers come from another machine. The lane and
the revision can be supplied by environment (`BOTO3_S3_BENCH_LANE`,
`BOTO3_S3_BENCH_GIT_REV`) because the instance runs from a ``git archive``
that carries no ``.git``.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks.core import BenchmarkError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from benchmarks.core import ScenarioResult

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).resolve().parent / "results"

LANE_ENV = "BOTO3_S3_BENCH_LANE"
GIT_REV_ENV = "BOTO3_S3_BENCH_GIT_REV"
GIT_DIRTY_ENV = "BOTO3_S3_BENCH_GIT_DIRTY"
IMAGE_ENV = "BOTO3_S3_BENCH_IMAGE"
LOCAL_LANE = "local"

# Versions recorded into every meta line; absent packages record null.
_TRACKED_PACKAGES = ("boto3-s3", "boto3-s3-cli", "boto3", "botocore", "s3transfer", "awscrt")


@dataclass(frozen=True)
class RunMeta:
    """The environment fingerprint stored as a results file's first line."""

    mode: str
    timestamp_utc: str
    git_rev: str
    git_dirty: bool
    python: str
    platform: str
    versions: dict[str, str | None]
    aws_version: str | None
    options: dict[str, object]
    lane: str = LOCAL_LANE
    image: str | None = None

    def record(self) -> dict[str, object]:
        record: dict[str, object] = {
            "kind": "meta",
            "mode": self.mode,
            "lane": self.lane,
            "timestamp_utc": self.timestamp_utc,
            "git_rev": self.git_rev,
            "git_dirty": self.git_dirty,
            "python": self.python,
            "platform": self.platform,
            "versions": self.versions,
            "aws_version": self.aws_version,
            "options": self.options,
        }
        if self.image is not None:
            # The machine image an EC2-lane run booted (an AMI id); a local
            # run has none.
            record["image"] = self.image
        return record


def _git(*args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10.0
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def collect_meta(
    mode: str, options: dict[str, object], *, aws_version: str | None = None
) -> RunMeta:
    """Capture the run fingerprint: git state, interpreter, package versions.

    The git revision and dirty flag come from the working tree, unless
    `BOTO3_S3_BENCH_GIT_REV` supplies them: the EC2 instance runs from an
    archive with no ``.git``, and the launcher knows exactly which commit it
    archived. The lane likewise defaults to ``local`` unless
    `BOTO3_S3_BENCH_LANE` says otherwise.
    """
    from importlib.metadata import PackageNotFoundError, version

    versions: dict[str, str | None] = {}
    for package in _TRACKED_PACKAGES:
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    rev_override = os.environ.get(GIT_REV_ENV)
    if rev_override:
        git_rev = rev_override[:10]
        git_dirty = os.environ.get(GIT_DIRTY_ENV) == "1"
    else:
        git_rev = _git("rev-parse", "--short=10", "HEAD") or "unknown"
        git_dirty = bool(_git("status", "--porcelain"))
    return RunMeta(
        mode=mode,
        timestamp_utc=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
        git_rev=git_rev,
        git_dirty=git_dirty,
        lane=os.environ.get(LANE_ENV) or LOCAL_LANE,
        image=os.environ.get(IMAGE_ENV) or None,
        python=platform.python_version(),
        platform=platform.platform(),
        versions=versions,
        aws_version=aws_version,
        options=options,
    )


def write_run(meta: RunMeta, results: Sequence[ScenarioResult]) -> Path:
    """Write one run's JSONL file and return its path."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    dirty = "-dirty" if meta.git_dirty else ""
    lane = "" if meta.lane == LOCAL_LANE else f".{meta.lane}"
    path = RESULTS_DIR / f"{meta.timestamp_utc}_{meta.mode}_{meta.git_rev}{dirty}{lane}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(meta.record()) + "\n")
        for result in results:
            handle.write(json.dumps(result.record()) + "\n")
    return path


def load_run(path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Read one results file back as ``(meta, result records)``."""
    meta: dict[str, object] | None = None
    records: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("kind") == "meta":
                meta = record
            elif record.get("kind") == "result":
                records.append(record)
    if meta is None:
        raise BenchmarkError(f"{path} has no meta record; not a benchmark results file")
    return meta, records


def list_runs(mode: str | None = None, *, lane: str | None = None) -> list[Path]:
    """Stored results files, oldest first (the filename sorts by time).

    *lane* restricts to one lane; None lists every lane.
    """
    if not RESULTS_DIR.is_dir():
        return []
    pattern = f"*_{mode}_*.jsonl" if mode else "*.jsonl"
    runs = sorted(RESULTS_DIR.glob(pattern))
    if lane is None:
        return runs
    return [run for run in runs if lane_of(run) == lane]


def lane_of(path: Path) -> str:
    """The lane a results filename records (``local`` when it names none)."""
    tail = path.name.removesuffix(".jsonl").split("_", 2)[2]
    _rev, dot, lane = tail.partition(".")
    return lane if dot else LOCAL_LANE


def resolve_baseline(
    spec: str, mode: str, *, lane: str = LOCAL_LANE, exclude: Path | None = None
) -> Path:
    """Resolve a ``--baseline`` value to a results file for *mode* in *lane*.

    ``last`` picks the newest stored run of the same mode and lane (excluding
    the run just written, so back-to-back runs compare against the previous
    one). An existing path is used as-is - the one way to compare across
    lanes, and the report then says so. Anything else is matched as a
    git-revision prefix against the stored filenames of that lane; the newest
    match wins.
    """
    candidate = Path(spec)
    if candidate.is_file():
        return candidate
    # Stored runs are absolute; a `report` argument is whatever the user typed
    # (usually relative), so compare resolved paths or the run under report
    # becomes its own baseline.
    excluded = exclude.resolve() if exclude is not None else None
    runs = [run for run in list_runs(mode, lane=lane) if run.resolve() != excluded]
    if spec == "last":
        if not runs:
            raise BenchmarkError(f"no stored {mode} runs in lane {lane!r} to use as baseline")
        return runs[-1]
    matches = [run for run in runs if run.name.split("_", 2)[2].startswith(spec)]
    if not matches:
        raise BenchmarkError(f"no stored {mode} run in lane {lane!r} matches baseline {spec!r}")
    return matches[-1]
