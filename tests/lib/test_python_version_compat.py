"""Static guard that shipped source stays within ``requires-python``.

The project supports Python 3.10+ (``requires-python = ">=3.10"``). Running the
suite on a 3.10 venv catches runtime breakage in *tests*, but a 3.11+-only
construct in the *shipped* trees (``src/`` and ``cli/src/``) can still slip past
a type checker whose typeshed does not version-gate the symbol (e.g.
``datetime.UTC``). ``vermin`` detects the real minimum version a source tree
needs; this test fails if that minimum drifts above the declared lower bound.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_TREES = ("src", "cli/src")

# ``tomllib`` is stdlib only on 3.11+; the project still supports 3.10, so the
# bound is extracted with a small regex rather than adding a TOML parser dep.
_REQUIRES_PYTHON_RE = re.compile(r'^\s*requires-python\s*=\s*"(?P<spec>[^"]+)"', re.MULTILINE)


def _declared_min_python() -> tuple[int, int]:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = _REQUIRES_PYTHON_RE.search(text)
    assert match is not None, "requires-python not found in pyproject.toml"
    spec = match.group("spec").strip()
    bound = re.fullmatch(r">=\s*(\d+)\.(\d+)", spec)
    assert bound is not None, f"unsupported requires-python spec {spec!r}; update this test"
    return int(bound.group(1)), int(bound.group(2))


def test_shipped_source_stays_within_requires_python() -> None:
    vermin = shutil.which("vermin")
    if vermin is None:
        pytest.skip("vermin not installed (sync the dev dependency group to enable)")

    targets = [str(REPO_ROOT / tree) for tree in SOURCE_TREES if (REPO_ROOT / tree).is_dir()]
    assert targets, "no source trees found to analyze"

    major, minor = _declared_min_python()
    result = subprocess.run(
        [
            vermin,
            f"-t={major}.{minor}-",  # trailing dash: "this version or older"
            "--no-tips",
            "--no-config-file",
            "--violations",
            *targets,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)

    assert result.returncode == 0, (
        f"vermin reports shipped source needs Python newer than the declared "
        f">={major}.{minor}. Lower the offending feature or raise requires-python "
        f"in pyproject.toml.\n"
        f"--- vermin stdout ---\n{result.stdout}\n"
        f"--- vermin stderr ---\n{result.stderr}"
    )
