"""The CRT engine reached with no credentials (design/crt.md section 4).

aws-cli builds its CRT client around a credentials delegate wrapping whatever
the session resolved - ``None`` included - so a credential-less CRT upload is
attempted and fails inside the delegate: the C layer cannot propagate the
``NoCredentialsError`` its Python callback raised, so the interpreter prints an
``Exception ignored in:`` block and the request comes back as
``AWS_AUTH_CREDENTIALS_PROVIDER_DELEGATE_FAILURE``. boto3 (and this library by
default) refuses the CRT engine there and runs classic instead, which reports
botocore's ``Unable to locate credentials``. The CLI takes aws's side.

The expectations below are the pinned aws's own measured bytes, normalized by
design/testing.md section 9: the object repr's address and the traceback body
are Class 2 (implementation internals) and collapse to placeholders, while the
``Exception ignored in: `` marker, the block's position ahead of the result
line, the result line itself, and the exit code are compared.

The run needs a real subprocess: the unraisable block is written by the
interpreter from a CRT worker thread, and standing up a real CRT client in the
pytest process would hold the cross-process lock for the rest of the session.
It contacts nothing - ``--endpoint-url http://127.0.0.1:1`` keeps even DNS out
of it, and the delegate fails before any connection is attempted (verified
against the pinned aws, which behaves identically with that endpoint).
"""

from __future__ import annotations

import os
import re
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
_ADDRESS = re.compile(r" at 0x[0-9a-f]+>")
_TRACEBACK_BODY = re.compile(
    r"^(  |Traceback \(most recent call last\):|[A-Za-z_][\w.]*(Error|Exception)\b)"
)


def _normalize(stderr: str) -> str:
    """design/testing.md section 9: keep each unraisable block's frame only."""
    lines: list[str] = []
    for line in stderr.splitlines():
        if line.startswith("Exception ignored in: "):
            lines.append(_ADDRESS.sub(" at <ADDRESS>>", line))
            lines.append("<TRACEBACK>")
        elif not _TRACEBACK_BODY.match(line):
            lines.append(line)
    return "\n".join(lines)


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


@pytest.fixture
def crt_lock_is_free() -> None:
    """Skip when this application's CRT slot is already taken.

    ``PROCESS_LOCK_NAME`` is host-wide, so the child falls back to classic -
    silently - whenever another process on the host holds it, or an in-process
    CRT client was built earlier in this session. Probed in a throwaway
    subprocess so the probe itself does not hold the lock when the child
    starts. Requested by the CRT case alone, never autouse: a stray host
    process legitimately owns the slot, so a hard failure would be wrong, and
    leaving the classic control unskipped bounds what a skip can cost.
    """
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from s3transfer.crt import acquire_crt_s3_process_lock as acquire;"
            "print(acquire('boto3-s3') is not None)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.stdout.strip() != "True":
        pytest.skip("this application's CRT process slot is held by another process")


@pytest.mark.usefixtures("crt_lock_is_free")
def test_crt_upload_without_credentials_fails_inside_the_delegate(
    tmp_path: Path, source: Path
) -> None:
    result = _run_cli(tmp_path, "crt", source)
    assert result.returncode == 1, result.stderr
    assert _normalize(result.stderr) == (
        "Exception ignored in: "
        "<s3transfer.crt.BotocoreCRTCredentialsWrapper object at <ADDRESS>>\n"
        "<TRACEBACK>\n"
        f"upload failed: {_reported_source(source)} to s3://{_BUCKET}/key "
        "AWS_AUTH_CREDENTIALS_PROVIDER_DELEGATE_FAILURE: "
        "Valid credentials could not be sourced by the delegate provider"
    )


def test_classic_upload_without_credentials_is_unchanged(tmp_path: Path, source: Path) -> None:
    # The control: nothing about the classic engine's report moved, and no
    # unraisable block appears when the CRT is not in play.
    result = _run_cli(tmp_path, "classic", source)
    assert result.returncode == 1, result.stderr
    assert result.stderr == (
        f"upload failed: {_reported_source(source)} to s3://{_BUCKET}/key "
        "Unable to locate credentials\n"
    )
