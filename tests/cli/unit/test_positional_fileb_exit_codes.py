"""Pin aws-cli's command-specific bugs for readable positional `fileb://`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context


def _unused_factory(_args: Any) -> Any:
    raise AssertionError("fileb positional failures must happen before client creation")


@pytest.mark.parametrize(
    ("command", "expected_rc", "stderr_token"),
    [
        ("mb", 252, "Invalid type for parameter input"),
        ("rb", 252, "Invalid type for parameter input"),
        ("presign", 252, "Invalid type for parameter input"),
        ("rm", 1, "fatal error: Parameter validation failed"),
        ("ls", 255, "startswith first arg must be bytes"),
        ("website", 255, "'int' object has no attribute 'startswith'"),
    ],
)
def test_readable_positional_fileb_keeps_command_specific_aws_bug(
    command: str,
    expected_rc: int,
    stderr_token: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ref = tmp_path / "path.bin"
    ref.write_bytes(b"s3://")

    result = cli.main(
        [command, f"fileb://{ref}"],
        ctx=Context(client_factory=_unused_factory),
    )

    assert result == expected_rc
    captured = capsys.readouterr()
    assert captured.out == ""
    assert stderr_token in captured.err
