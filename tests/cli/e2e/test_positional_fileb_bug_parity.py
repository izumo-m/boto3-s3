"""Detect changes to aws-cli's inconsistent positional `fileb://` bugs.

This test intentionally does not use a golden. The explicit aws-cli exit-code
assertion is a tripwire: when aws-cli fixes one of these bugs, the test must
fail so the compatibility behavior can be reviewed instead of silently moving.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.utils.harness import run_aws_subprocess, run_cli_subprocess


@pytest.mark.parametrize(
    ("command", "expected_aws_rc", "aws_stderr_token", "ours_stderr_token"),
    [
        ("mb", 252, "Invalid type for parameter input", "Invalid type for parameter input"),
        ("rb", 252, "Invalid type for parameter input", "Invalid type for parameter input"),
        (
            "presign",
            252,
            "Invalid type for parameter input",
            "Invalid type for parameter input",
        ),
        (
            "rm",
            1,
            "fatal error: Parameter validation failed",
            "fatal error: Parameter validation failed",
        ),
        ("ls", 255, "startswith first arg must be bytes", "startswith first arg must be bytes"),
        (
            "website",
            255,
            "'int' object has no attribute 'startswith'",
            "'int' object has no attribute 'startswith'",
        ),
    ],
)
def test_readable_positional_fileb_bug_parity(
    command: str,
    expected_aws_rc: int,
    aws_stderr_token: str,
    ours_stderr_token: str,
    tmp_path: Path,
) -> None:
    ref = tmp_path / "path.bin"
    ref.write_bytes(b"s3://")
    argv = [command, f"fileb://{ref}"]

    aws_result = run_aws_subprocess(argv)
    ours_result = run_cli_subprocess(argv)

    assert aws_result.rc == expected_aws_rc, (
        f"aws-cli's positional fileb:// bug changed for {command!r}; "
        "review whether boto3-s3-cli should stop reproducing it. "
        f"stderr={aws_result.stderr.strip()!r}"
    )
    assert ours_result.rc == aws_result.rc, (
        f"positional fileb:// exit-code parity broken for {command!r}: "
        f"ours={ours_result.rc}, aws={aws_result.rc}, "
        f"ours stderr={ours_result.stderr.strip()!r}, "
        f"aws stderr={aws_result.stderr.strip()!r}"
    )
    assert aws_result.stdout == ""
    assert ours_result.stdout == ""
    assert aws_stderr_token in aws_result.stderr
    assert ours_stderr_token in ours_result.stderr
