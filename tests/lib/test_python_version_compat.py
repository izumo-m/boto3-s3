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

# Each shipped source tree and the pyproject that declares its own
# ``requires-python``: ``src`` is the root package (boto3-s3), ``cli/src`` the
# CLI distribution (boto3-s3-cli), floored independently. Checking each tree
# against its own bound (not the root's) catches a drift where only one
# package raises its floor.
SOURCE_TREES = {
    "src": "pyproject.toml",
    "cli/src": "cli/pyproject.toml",
}

# ``tomllib`` is stdlib only on 3.11+; the project still supports 3.10, so the
# bound is extracted with a small regex rather than adding a TOML parser dep.
_REQUIRES_PYTHON_RE = re.compile(r'^\s*requires-python\s*=\s*"(?P<spec>[^"]+)"', re.MULTILINE)


def _declared_min_python(pyproject: Path) -> tuple[int, int]:
    text = pyproject.read_text(encoding="utf-8")
    match = _REQUIRES_PYTHON_RE.search(text)
    assert match is not None, f"requires-python not found in {pyproject}"
    spec = match.group("spec").strip()
    bound = re.fullmatch(r">=\s*(\d+)\.(\d+)", spec)
    assert bound is not None, f"unsupported requires-python spec {spec!r}; update this test"
    return int(bound.group(1)), int(bound.group(2))


@pytest.mark.parametrize("tree", SOURCE_TREES)
def test_shipped_source_stays_within_requires_python(tree: str) -> None:
    vermin = shutil.which("vermin")
    if vermin is None:
        pytest.skip("vermin not installed (sync the dev dependency group to enable)")

    target = REPO_ROOT / tree
    if not target.is_dir():
        pytest.skip(f"source tree {tree} not present")

    major, minor = _declared_min_python(REPO_ROOT / SOURCE_TREES[tree])
    result = subprocess.run(
        [
            vermin,
            f"-t={major}.{minor}-",  # trailing dash: "this version or older"
            "--no-tips",
            "--no-config-file",
            "--violations",
            str(target),
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
