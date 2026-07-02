"""Import contract: no SDK - and no command module - until the subcommand is known.

The dispatch is two-stage (docs/imports.md section 2 item 4): stage 1 reads
the globals and the subcommand name off the static command table, so the
top-level ``--help`` / ``--version`` and the stage-1 usage errors complete
SDK-free *and* command-module-free. Once the subcommand is determined its
module loads and may reach ``botocore.exceptions``, but the ``boto3`` /
``s3transfer`` client stack (and ``prompt_toolkit``) must still stay out of
the subcommand help / usage paths - the client loads only in ``build_client``.

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
        roots = ("boto3", "botocore", "s3transfer", "prompt_toolkit")
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

    def assert_no_client_stack():
        roots = ("boto3", "s3transfer", "prompt_toolkit")
        loaded = sorted(m for m in sys.modules if m.partition(".")[0] in roots)
        assert not loaded, loaded
"""


def _run_fresh(code: str) -> str:
    """Run *code* (after the prelude) in a fresh interpreter; return its stdout."""
    source = textwrap.dedent(_PRELUDE) + textwrap.dedent(code)
    # Pin the auto-prompt mode off so the resolution can't (per a stray
    # AWS_CLI_AUTO_PROMPT / ~/.aws/config) divert a usage path into the prompt
    # branch and import prompt_toolkit - the contract under test.
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
            """
        )

    def test_subcommand_help_loads_no_client_stack(self) -> None:
        # Post-determination: each command's module (its configure()) loads,
        # and its top-level imports may reach botocore.exceptions - but the
        # boto3/s3transfer client stack must not load for --help.
        _run_fresh(
            """
            assert main(["cp", "--help"]) == 0
            assert main(["mv", "--help"]) == 0
            assert main(["rm", "--help"]) == 0
            assert main(["ls", "--help"]) == 0
            assert main(["mb", "--help"]) == 0
            assert main(["rb", "--help"]) == 0
            assert main(["presign", "--help"]) == 0
            assert main(["sync", "--help"]) == 0
            assert main(["website", "--help"]) == 0
            assert_no_client_stack()
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
            """
        )
        assert "boto3-s3-cli/" in out
        assert "botocore/" in out

    def test_stage1_usage_errors_are_sdk_and_command_free(self) -> None:
        _run_fresh(
            """
            assert main(["no-such-command"]) == 252         # argparse invalid choice
            assert main([]) == 252                          # missing subcommand
            assert main(["--no-such-option", "ls"]) == 252  # unknown option pre-command
            assert_no_heavy_imports()
            assert_no_command_modules()
            """
        )

    def test_subcommand_usage_error_loads_no_client_stack(self) -> None:
        # The unknown option sits after the subcommand, so its module loads
        # (stage 2) - still no client stack for a usage error.
        _run_fresh(
            """
            assert main(["ls", "--no-such-option"]) == 252  # "Unknown options" path
            assert_no_client_stack()
            """
        )

    def test_auto_prompt_mutual_exclusion_is_import_free(self) -> None:
        # The raw-argv resolution (mutual exclusion) is decided before argparse
        # and before any autoprompt/SDK import - even prompt_toolkit, which is
        # installed in the test env, must stay unimported here.
        _run_fresh(
            """
            assert main(["--cli-auto-prompt", "--no-cli-auto-prompt", "ls"]) == 252
            assert_no_heavy_imports()
            """
        )
