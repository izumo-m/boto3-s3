"""Degradation contract for installs without awscrt (docs/transfer.md section 9).

The dev environment is always CRT-present (the dev dependency group installs
``botocore[crt]`` so the parity tiers run under aws v2's condition), while
the dists leave CRT to the opt-in ``crt`` extra - so the awscrt-less install
needs its own pin. botocore freezes ``HAS_CRT`` (and the checksum registry
derived from it) at import time, which makes the absence impossible to fake
in this process; instead the test drives the real CLI inside a fresh
subprocess that blocks awscrt before botocore loads. Seeding
``sys.modules["awscrt"] = None`` makes ``import awscrt`` raise ImportError
(botocore's probe) and ``importlib.util.find_spec("awscrt")`` return None
(moto's probe), exactly like an environment that never installed the wheel.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

_CHILD = """\
import os
import pathlib
import sys

os.environ.update(
    {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
)
for key in list(os.environ):
    if key == "AWS_PROFILE" or key.startswith("AWS_ENDPOINT_URL"):
        del os.environ[key]

# Must precede every botocore/moto import (module docstring of the parent
# test explains the mechanism).
sys.modules["awscrt"] = None

from botocore.compat import HAS_CRT

assert not HAS_CRT, "awscrt block failed; this run proves nothing"

import boto3
from moto import mock_aws

from tests.utils.harness import run_cli_in_process

work = pathlib.Path(sys.argv[1])
src = work / "f.txt"
src.write_text("hello")

with mock_aws():
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="crt-less-bucket")

    # The CLI stays fully functional: the default integrity checksum (CRC32)
    # comes from zlib, no CRT needed.
    result = run_cli_in_process(["cp", str(src), "s3://crt-less-bucket/plain.txt"])
    assert result.rc == 0, (result.rc, result.stderr)

    # Only the CRT checksum family degrades: an in-pipeline per-item failure
    # (the library's BatchError surfaced as rc 1), not a crash.
    result = run_cli_in_process(
        ["cp", str(src), "s3://crt-less-bucket/c.txt", "--checksum-algorithm", "CRC32C"]
    )
    assert result.rc == 1, (result.rc, result.stderr)
    assert "upload failed:" in result.stderr, result.stderr
    assert "Missing Dependency" in result.stderr, result.stderr
    assert "botocore[crt]" in result.stderr, result.stderr

    # Pure-Python signers are the no-awscrt default: the build_client pin is
    # a no-op and presign keeps aws v2's parameter order.
    result = run_cli_in_process(["presign", "s3://crt-less-bucket/plain.txt"])
    assert result.rc == 0, (result.rc, result.stderr)
    url = result.stdout.strip()
    assert url.index("X-Amz-Expires=") < url.index("X-Amz-SignedHeaders="), url

print("OK")
"""


def test_only_the_crt_checksum_family_degrades(tmp_path: Path) -> None:
    script = tmp_path / "no_crt_child.py"
    script.write_text(_CHILD, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(_REPO_ROOT), env.get("PYTHONPATH")) if path
    )
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(_REPO_ROOT),
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert proc.stdout.strip().endswith("OK"), proc.stdout
