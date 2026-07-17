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
nondeterministic in aws); the end state pins what the sort relaxes. The ``ls``
capture passes no ``remaining_keys``, so it persists as ``null`` and is not
compared.
Bucket-lifecycle commands (``mb`` / ``rb``) further record ``bucket_exists``
- whether the scenario bucket exists after the run; ``remaining_keys`` is
``None`` when it does not (a bucket that is gone has no key listing).

Platform variants (docs/testing.md sections 3 and 8): the transfer kinds
(`WINDOWS_VARIANT_KINDS`) have an OS-dependent local side, so next to the
POSIX base ``<name>.json`` they may carry a ``<name>.windows.json`` captured
from ``aws.exe``. Loading on Windows prefers the variant and falls back to
the base (most captures are identical); capturing on Windows writes only
variants - and only where the capture differs from the base on a compared
field - never the base files, which stay POSIX captures. Every other kind's
golden is platform-independent (the Windows e2e drift run verifies the same
files against ``aws.exe`` unchanged).
"""

from __future__ import annotations

import difflib
import functools
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest

GOLDENS_DIR = Path(__file__).resolve().parents[1] / "cli" / "goldens"

# Kinds whose goldens have a host-OS-dependent local side (result-line
# separators, dir-vs-file outcomes) and may therefore carry a Windows variant.
WINDOWS_VARIANT_KINDS = frozenset({"cp", "mv", "sync"})


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
    """Path of the base (POSIX-captured) golden for scenario *name* of *kind*."""
    return GOLDENS_DIR / kind / f"{name}.json"


def _windows_variant_path(kind: str, name: str) -> Path:
    """Path of the Windows-captured variant next to the base golden."""
    return GOLDENS_DIR / kind / f"{name}.windows.json"


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


# The version inside an ``aws --version`` line ("aws-cli/2.36.1 Python/... ...").
_AWS_VERSION_RE = re.compile(r"aws-cli/(\d+(?:\.\d+)*)")

_VENDORED_AWSCLI_INIT = (
    Path(__file__).resolve().parents[2] / "vendor" / "aws-cli" / "awscli" / "__init__.py"
)


def parse_aws_cli_version(version_line: str) -> str | None:
    """Extract the ``2.x.y`` version from an ``aws --version`` line, or None."""
    match = _AWS_VERSION_RE.search(version_line)
    return match.group(1) if match else None


@functools.lru_cache(maxsize=1)
def pinned_aws_version() -> str | None:
    """The aws-cli version the goldens are pinned to (the vendored submodule).

    Read from the vendored ``awscli/__init__.py`` ``__version__`` - the same
    source ``scripts/install-awscli.sh`` derives its install target from, so
    the live ``aws`` and the goldens track one reference. ``None`` when the
    submodule is not checked out.
    """
    try:
        text = _VENDORED_AWSCLI_INIT.read_text()
    except OSError:
        return None
    match = re.search(r"__version__ = '([0-9][^']*)'", text)
    return match.group(1) if match else None


def _dump_golden(path: Path, golden: Golden) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(golden), indent=2, sort_keys=True) + "\n")
    return path


def write_golden(kind: str, golden: Golden) -> Path | None:
    """Persist *golden*; returns the path written (``None`` = nothing written).

    On POSIX this writes the base file. On Windows only the
    `WINDOWS_VARIANT_KINDS` are written, as ``<name>.windows.json`` variants,
    and only when the capture differs from the base on a compared field
    (``aws_version`` excluded - it always differs); a variant whose capture
    stopped differing is pruned. Base files are never written from Windows:
    they are POSIX captures (platform-independent kinds included, which is
    why their Windows capture is skipped outright).
    """
    base_path = golden_path(kind, golden.scenario)
    if os.name != "nt":
        return _dump_golden(base_path, golden)
    if kind not in WINDOWS_VARIANT_KINDS:
        return None
    if not base_path.exists():
        pytest.fail(
            f"golden {base_path} is missing; capture the POSIX baseline first "
            "(docs/testing.md section 8)"
        )
    base = json.loads(base_path.read_text())
    captured = asdict(golden)
    variant = _windows_variant_path(kind, golden.scenario)
    if all(value == base.get(key) for key, value in captured.items() if key != "aws_version"):
        variant.unlink(missing_ok=True)
        return None
    return _dump_golden(variant, golden)


def load_golden(kind: str, name: str) -> Golden:
    """Load the committed golden, failing with regeneration instructions if absent.

    On Windows a ``<name>.windows.json`` variant wins over the base file for
    the `WINDOWS_VARIANT_KINDS` (absent variant = the captures are identical,
    fall back to the base).
    """
    path = golden_path(kind, name)
    if os.name == "nt" and kind in WINDOWS_VARIANT_KINDS:
        variant = _windows_variant_path(kind, name)
        if variant.exists():
            path = variant
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
    *compare_stdout* only relaxes the stdout comparison. Every end-state
    argument (*remaining_keys*, *bucket_exists*, *local_tree*, *head_fields*,
    *src_tree*) is compared only when the golden recorded it.
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
