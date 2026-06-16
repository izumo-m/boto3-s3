"""Import contract: CLI paths that end before dispatch load no heavy module.

``--help`` / ``--version`` / usage errors must not pay for boto3 - importing
it drags in botocore and s3transfer (~100ms) via boto3's ``compat`` module. Nor
may they import ``prompt_toolkit``: the auto-prompt port is reached only when
``--cli-auto-prompt`` actually fires (``docs/autoprompt.md``). The SDK loads
exactly once a subcommand builds its client (``build_client`` / ``run()``);
policy in ``docs/imports.md``.

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
    def test_top_level_help_is_sdk_free(self) -> None:
        _run_fresh(
            """
            assert main(["--help"]) == 0
            assert_no_heavy_imports()
            """
        )

    def test_subcommand_help_is_sdk_free(self) -> None:
        # Exercises every registered configure() (full subparser build) plus
        # the rm filter/output module chain.
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
            assert_no_heavy_imports()
            """
        )

    def test_version_is_sdk_free(self) -> None:
        # The boto3/botocore tokens must come from distribution metadata, not
        # from importing the packages.
        out = _run_fresh(
            """
            assert main(["--version"]) == 0
            assert_no_heavy_imports()
            """
        )
        assert "boto3-s3-cli/" in out
        assert "botocore/" in out

    def test_usage_errors_are_sdk_free(self) -> None:
        _run_fresh(
            """
            assert main(["no-such-command"]) == 252      # argparse invalid choice
            assert main(["ls", "--no-such-option"]) == 252  # "Unknown options" path
            assert main([]) == 252                       # missing subcommand
            assert_no_heavy_imports()
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
