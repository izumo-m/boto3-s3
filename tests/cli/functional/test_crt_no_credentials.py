"""The CRT engine reached with no credentials (design/crt.md section 4).

A client that resolved no credentials never gets the CRT engine (boto3's rule),
so a credential-less upload under a ``crt`` preference runs classic and fails
each item with botocore's ``Unable to locate credentials``, rc 1 - the outcome
``aws s3`` reaches through its CRT credentials delegate, without the
``Exception ignored in:`` traceback block the delegate's failure prints there.

The run needs a real subprocess with the real awscrt: the engine is built at
the command's construction slot before the transfer falls back. It contacts
nothing - ``--endpoint-url http://127.0.0.1:1`` keeps even DNS out of it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from boto3_s3 import crtsupport

if not crtsupport.has_crt_s3transfer():
    # awscrt absent, or an s3transfer below 0.8.0 (the supported floor), whose
    # crt module carries neither the process lock nor the credentials wrapper -
    # the CRT engine cannot be built at all there, and the CLI degrades with a
    # configuration error instead (design/crt.md section 6).
    pytest.skip("the CRT engine needs awscrt and s3transfer >= 0.8.0", allow_module_level=True)

_DEAD_ENDPOINT = "http://127.0.0.1:1"
_BUCKET = "boto3-s3-crt-nocreds"
_RUN_CLI = "import sys; from boto3_s3_cli.cli import main; sys.exit(main())"


def _reported_source(source: Path) -> str:
    """How the result line names a source under the run's cwd (``./name``).

    aws-cli relativizes a path inside the working directory and renders it with
    the host separator; the runs below set ``cwd`` to the source's directory.
    """
    return os.path.join(".", source.name)


def _run_cli(tmp_path: Path, engine: str, source: Path) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = home / "config"
    config.write_text(
        f"[default]\nregion = us-east-1\ns3 =\n  preferred_transfer_client = {engine}\n",
        encoding="utf-8",
    )
    # Every AWS_* variable goes, so the root conftest's fake credentials (and
    # any the host exports) cannot resolve; what is put back is only the
    # isolation. Region comes from the config file, like the aws measurement.
    env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "AWS_CONFIG_FILE": str(config),
            "AWS_SHARED_CREDENTIALS_FILE": str(home / "credentials-absent"),
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _RUN_CLI,
            "cp",
            str(source),
            f"s3://{_BUCKET}/key",
            "--endpoint-url",
            _DEAD_ENDPOINT,
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(tmp_path),
    )


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "payload.txt"
    path.write_text("hello world payload\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("engine", ["crt", "classic"])
def test_an_upload_without_credentials_fails_its_item(
    tmp_path: Path, source: Path, engine: str
) -> None:
    result = _run_cli(tmp_path, engine, source)
    assert result.returncode == 1, result.stderr
    assert result.stderr == (
        f"upload failed: {_reported_source(source)} to s3://{_BUCKET}/key "
        "Unable to locate credentials\n"
    )
