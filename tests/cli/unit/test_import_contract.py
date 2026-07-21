"""Import contract for the two top-level informational exits.

Top-level ``--help`` and ``--version`` complete without importing the AWS SDK,
a command module, or any library module beyond the lazy ``boto3_s3`` root and
its pure exceptions taxonomy - in particular not the SDK-backed ``boto3_s3.s3``,
which imports boto3 at module top. No import guarantee applies to normal
dispatch, usage errors, or subcommand help.

Each case runs ``main()`` in a fresh interpreter (``python -c``) so imports
already made by the test runner can't mask a regression.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

_PRELUDE = """
    import sys

    from boto3_s3_cli.cli import main

    def assert_no_heavy_imports():
        roots = ("boto3", "botocore", "s3transfer")
        loaded = sorted(m for m in sys.modules if m.partition(".")[0] in roots)
        assert not loaded, loaded

    def assert_no_command_modules():
        # commands/base.py (the Command/Context infrastructure) is part of the
        # dispatcher core and SDK-free; the guarantee is that no per-command
        # module loads before the subcommand is determined.
        infra = {"boto3_s3_cli.commands", "boto3_s3_cli.commands.base"}
        loaded = sorted(
            m
            for m in sys.modules
            if m.startswith("boto3_s3_cli.commands") and m not in infra
        )
        assert not loaded, loaded

    def assert_no_library_modules():
        # The lazy boto3_s3 root and the pure exceptions taxonomy are the only
        # library modules these exits may touch; in particular the SDK-backed
        # boto3_s3.s3 (which imports boto3 at module top) must not load.
        allowed = {"boto3_s3", "boto3_s3.exceptions"}
        loaded = sorted(
            m
            for m in sys.modules
            if m.partition(".")[0] == "boto3_s3" and m not in allowed
        )
        assert not loaded, loaded

"""


def _run_fresh(code: str) -> str:
    """Run *code* (after the prelude) in a fresh interpreter; return its stdout."""
    source = textwrap.dedent(_PRELUDE) + textwrap.dedent(code)
    # Pin the auto-prompt mode off so ambient configuration cannot divert an
    # informational exit into the interactive prompt.
    env = {**os.environ, "AWS_CLI_AUTO_PROMPT": "off"}
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, check=False, env=env
    )
    assert result.returncode == 0, (
        f"\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout


class TestCliImportContract:
    def test_top_level_help_is_sdk_and_command_free(self) -> None:
        _run_fresh(
            """
            assert main(["--help"]) == 0
            assert_no_heavy_imports()
            assert_no_command_modules()
            assert_no_library_modules()
            """
        )

    def test_version_is_sdk_and_command_free(self) -> None:
        # The boto3/botocore tokens must come from distribution metadata, not
        # from importing the packages.
        out = _run_fresh(
            """
            assert main(["--version"]) == 0
            assert_no_heavy_imports()
            assert_no_command_modules()
            assert_no_library_modules()
            """
        )
        assert "boto3-s3-cli/" in out
        assert "botocore/" in out
