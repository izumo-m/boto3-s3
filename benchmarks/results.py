"""Run metadata capture, JSONL result files, and baseline resolution.

One run of one mode produces one JSONL file under `benchmarks/results/`
(git-ignored): the first line is a ``meta`` record describing the run
environment, every following line is one scenario's ``result`` record.
The timestamped filename keeps the directory chronologically sorted, and the
embedded git revision is what ``--baseline <rev>`` matches against.
"""

from __future__ import annotations

import json
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

    def record(self) -> dict[str, object]:
        return {
            "kind": "meta",
            "mode": self.mode,
            "timestamp_utc": self.timestamp_utc,
            "git_rev": self.git_rev,
            "git_dirty": self.git_dirty,
            "python": self.python,
            "platform": self.platform,
            "versions": self.versions,
            "aws_version": self.aws_version,
            "options": self.options,
        }


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
    """Capture the run fingerprint: git state, interpreter, package versions."""
    from importlib.metadata import PackageNotFoundError, version

    versions: dict[str, str | None] = {}
    for package in _TRACKED_PACKAGES:
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    return RunMeta(
        mode=mode,
        timestamp_utc=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
        git_rev=_git("rev-parse", "--short=10", "HEAD") or "unknown",
        git_dirty=bool(_git("status", "--porcelain")),
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
    path = RESULTS_DIR / f"{meta.timestamp_utc}_{meta.mode}_{meta.git_rev}{dirty}.jsonl"
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


def list_runs(mode: str | None = None) -> list[Path]:
    """All stored results files, oldest first (the filename sorts by time)."""
    if not RESULTS_DIR.is_dir():
        return []
    pattern = f"*_{mode}_*.jsonl" if mode else "*.jsonl"
    return sorted(RESULTS_DIR.glob(pattern))


def resolve_baseline(spec: str, mode: str, *, exclude: Path | None = None) -> Path:
    """Resolve a ``--baseline`` value to a results file for *mode*.

    ``last`` picks the newest stored run of the same mode (excluding the run
    just written, so back-to-back runs compare against the previous one). An
    existing path is used as-is. Anything else is matched as a git-revision
    prefix against the stored filenames; the newest match wins.
    """
    candidate = Path(spec)
    if candidate.is_file():
        return candidate
    runs = [run for run in list_runs(mode) if exclude is None or run != exclude]
    if spec == "last":
        if not runs:
            raise BenchmarkError(f"no stored {mode} runs to use as baseline")
        return runs[-1]
    matches = [run for run in runs if run.name.split("_", 2)[2].startswith(spec)]
    if not matches:
        raise BenchmarkError(f"no stored {mode} run matches baseline {spec!r}")
    return matches[-1]
