"""The CRT engine reached with no region at all (design/crt.md section 6).

aws-cli resolves the CRT client's region from its own region chain, which
answers ``None`` when nothing is configured - where botocore instead hands the
*botocore* client the ``aws-global`` pseudo-region. ``create_s3_crt_client``
passes that ``None`` straight to ``awscrt.s3.S3Client``, whose
``assert isinstance(region, str)`` fires; the bare ``AssertionError`` carries
no message, and aws's general exception handler renders it as the prefix alone
at rc 255. The transfer manager is built before the run is decided, so every
transfer command reports it - ``--dryrun`` and ``--quiet`` included - and so
does ``rm``, which builds the engine like aws and then deletes without it.

These run the real awscrt: the whole point is that the assertion is awscrt's
own, not a hand-written mirror of it. `tests/cli/unit/test_engine_selection.py`
pins the ordering and the rendered bytes with the construction faked, and
`tests/lib/test_crtsupport.py` pins that the declared region is what reaches
``create_s3_crt_client``.

The runs need a real subprocess: standing up a CRT client in the pytest process
would hold the cross-process lock for the rest of the session. They contact
nothing - construction fails before any request is built.
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

_BUCKET = "boto3-s3-crt-noregion"
_RUN_CLI = "import sys; from boto3_s3_cli.cli import main; sys.exit(main())"
_EMPTY_REPORT = "boto3-s3: [ERROR]:\n"


def _run_cli(
    tmp_path: Path, engine: str, argv: list[str], *, region: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a child with *engine* selected and no AWS environment.

    Every ``AWS_*`` variable goes so neither the host's nor the root conftest's
    settings can resolve a region; what is put back is only the isolation. The
    config file carries the ``[s3]`` section and, for the control, the region.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    config = home / "config"
    region_line = f"region = {region}\n" if region is not None else ""
    config.write_text(
        f"[default]\n{region_line}s3 =\n  preferred_transfer_client = {engine}\n",
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "AWS_CONFIG_FILE": str(config),
            "AWS_SHARED_CREDENTIALS_FILE": str(home / "credentials-absent"),
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )
    return subprocess.run(
        [sys.executable, "-c", _RUN_CLI, *argv],
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

    ``PROCESS_LOCK_NAME`` is host-wide, so a child whose slot is held elsewhere
    never reaches ``create_s3_crt_client`` at all (the construction returns
    ``None`` and the run falls back to classic). Probed in a throwaway
    subprocess so the probe itself does not hold the lock when the child
    starts - the same reason ``test_crt_no_credentials.py`` probes it.
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
@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["cp", "{source}", f"s3://{_BUCKET}/key"], id="cp-upload"),
        pytest.param(["cp", "{source}", f"s3://{_BUCKET}/key", "--dryrun"], id="cp-dryrun"),
        pytest.param(["sync", "{cwd}", f"s3://{_BUCKET}/p"], id="sync"),
        pytest.param(["rm", f"s3://{_BUCKET}/key"], id="rm"),
        pytest.param(["rm", f"s3://{_BUCKET}/key", "--quiet"], id="rm-quiet"),
    ],
)
def test_no_resolvable_region_is_awscrts_own_empty_report(
    tmp_path: Path, source: Path, argv: list[str]
) -> None:
    resolved = [token.format(source=source, cwd=tmp_path) for token in argv]
    result = _run_cli(tmp_path, "crt", resolved)
    assert result.returncode == 255, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert result.stderr == _EMPTY_REPORT


def test_a_resolvable_region_builds_the_engine_and_runs(tmp_path: Path) -> None:
    # The control: with a region the construction succeeds, so the run proceeds
    # to the (unreachable) endpoint and fails there instead - rm never routes
    # its deletes through the engine it just built.
    result = _run_cli(
        tmp_path,
        "crt",
        ["rm", f"s3://{_BUCKET}/key", "--endpoint-url", "http://127.0.0.1:1"],
        region="us-east-1",
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stderr.startswith(f"delete failed: s3://{_BUCKET}/key ")


def test_the_classic_engine_is_unaffected_by_an_absent_region(tmp_path: Path) -> None:
    # The other control: nothing about a region-less classic run moved - the
    # botocore client resolves `aws-global` and the request is attempted.
    result = _run_cli(
        tmp_path,
        "classic",
        ["rm", f"s3://{_BUCKET}/key", "--endpoint-url", "http://127.0.0.1:1"],
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stderr.startswith(f"delete failed: s3://{_BUCKET}/key ")
