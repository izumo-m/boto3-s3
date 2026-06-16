"""Golden-file plumbing for the aws-cli parity contract.

A golden records what the **real aws-cli** did for one scenario against a
real S3-compatible endpoint (rc + normalized stdout). The e2e suite writes
them under ``UPDATE_GOLDENS=1`` and verifies them against the live aws binary
otherwise (drift detection); the functional (moto) suite replays the same
scenario through boto3-s3 and compares against the stored golden.

The record shape::

    {"scenario": ..., "argv": [...], "rc": 0,
     "stdout_lines": [...], "aws_version": "aws-cli/2.x ..."}

Destructive commands (``rm``) additionally record ``remaining_keys`` - the
bucket's end state after the aws run, with the seeded layout as the start
state - because their stdout is normalized *sorted* (delete-line order is
nondeterministic in aws); the end state pins what the sort relaxes. ``ls``
goldens predate the field and omit it (loaded as ``None``, not compared).
Bucket-lifecycle commands (``mb`` / ``rb``) further record ``bucket_exists``
- whether the scenario bucket exists after the run; ``remaining_keys`` is
``None`` when it does not (a bucket that is gone has no key listing).
"""

from __future__ import annotations

import difflib
import functools
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

GOLDENS_DIR = Path(__file__).resolve().parents[1] / "cli" / "goldens"


@dataclass(frozen=True)
class Golden:
    """One captured aws-cli run, normalized (``harness.normalize_*_stdout``)."""

    scenario: str
    argv: list[str]
    rc: int
    stdout_lines: list[str]
    aws_version: str
    # Bucket end state after the run (destructive commands only; None = not
    # captured / not compared, which keeps the ls goldens valid unchanged).
    remaining_keys: list[str] | None = None
    # Whether the scenario bucket exists after the run (bucket-lifecycle
    # commands only; same None-means-not-compared convention).
    bucket_exists: bool | None = None
    # Transfer-scenario end states (cp/mv; same convention): the local
    # destination tree (harness.capture_local_tree) and selected HeadObject
    # fields of one probe key (harness.head_object_fields).
    local_tree: list[str] | None = None
    head_fields: dict[str, Any] | None = None
    # The local *source* tree after the run (mv only - what the move
    # deleted, or left behind on dryrun/filter/failure; cp goldens predate
    # the field and load it as None).
    src_tree: list[str] | None = None


def golden_path(kind: str, name: str) -> Path:
    """Path of the golden for scenario *name* of *kind* (e.g. ``"ls"``)."""
    return GOLDENS_DIR / kind / f"{name}.json"


def update_goldens_enabled() -> bool:
    """True when the env asks the e2e suite to (re)write goldens."""
    return os.environ.get("UPDATE_GOLDENS", "").strip().lower() in {"1", "true", "yes", "on"}


@functools.lru_cache(maxsize=1)
def detect_aws_version() -> str:
    """The ``aws --version`` line, recorded in goldens for drift triage."""
    aws = shutil.which("aws")
    if aws is None:
        return "unknown"
    proc = subprocess.run([aws, "--version"], check=False, capture_output=True, timeout=15.0)
    return proc.stdout.decode(errors="replace").strip() or "unknown"


def write_golden(kind: str, golden: Golden) -> Path:
    """Persist *golden*; returns the path written."""
    path = golden_path(kind, golden.scenario)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(golden), indent=2, sort_keys=True) + "\n")
    return path


def load_golden(kind: str, name: str) -> Golden:
    """Load the committed golden, failing with regeneration instructions if absent."""
    path = golden_path(kind, name)
    if not path.exists():
        pytest.fail(
            f"golden {path} is missing. Generate it against MinIO:\n"
            f"  scripts/compose-up.sh\n"
            f"  source scripts/minio-env.sh\n"
            f"  UPDATE_GOLDENS=1 uv run pytest tests/cli/e2e"
        )
    data = json.loads(path.read_text())
    return Golden(**data)


def assert_matches_golden(
    golden: Golden,
    *,
    rc: int,
    stdout_lines: list[str],
    side: str,
    compare_stdout: bool = True,
    remaining_keys: list[str] | None = None,
    bucket_exists: bool | None = None,
    local_tree: list[str] | None = None,
    head_fields: dict[str, Any] | None = None,
    src_tree: list[str] | None = None,
) -> None:
    """Compare one run against *golden*; *side* labels who diverged.

    ``side="ours"`` is the functional replay (boto3-s3 vs recorded aws-cli);
    ``side="aws"`` is the e2e drift check (live aws-cli vs its own recording).
    rc is always compared (exit-code charter, docs/overview.md section 3);
    *compare_stdout* only relaxes the stdout comparison. *remaining_keys* and
    *bucket_exists* are compared only when the golden recorded them.
    """
    problems: list[str] = []
    if rc != golden.rc:
        problems.append(f"rc: {side} returned {rc}, golden has {golden.rc}")
    if compare_stdout and stdout_lines != golden.stdout_lines:
        diff = "\n".join(
            difflib.unified_diff(
                golden.stdout_lines, stdout_lines, fromfile="golden", tofile=side, lineterm=""
            )
        )
        problems.append(f"stdout:\n{diff}")
    if golden.remaining_keys is not None and remaining_keys != golden.remaining_keys:
        problems.append(
            f"end state: {side} left {remaining_keys!r}, golden has {golden.remaining_keys!r}"
        )
    if golden.bucket_exists is not None and bucket_exists != golden.bucket_exists:
        problems.append(
            f"bucket existence: {side} ended with {bucket_exists!r}, "
            f"golden has {golden.bucket_exists!r}"
        )
    if golden.local_tree is not None and local_tree != golden.local_tree:
        problems.append(f"local tree: {side} left {local_tree!r}, golden has {golden.local_tree!r}")
    if golden.src_tree is not None and src_tree != golden.src_tree:
        problems.append(f"source tree: {side} left {src_tree!r}, golden has {golden.src_tree!r}")
    if golden.head_fields is not None and head_fields != golden.head_fields:
        problems.append(
            f"head fields: {side} shows {head_fields!r}, golden has {golden.head_fields!r}"
        )
    if problems:
        pytest.fail(f"[{golden.scenario}] {side} diverged from golden:\n" + "\n".join(problems))
