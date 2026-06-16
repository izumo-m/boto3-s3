"""Shared fixtures for the CLI test tiers."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_auto_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``--cli-auto-prompt`` off so a developer's config can't sway the suite.

    ``cli.main`` now resolves the auto-prompt mode from ``AWS_CLI_AUTO_PROMPT`` /
    the profile ``cli_auto_prompt`` on every invocation (autoprompt.md section 5). A
    machine with ``cli_auto_prompt = on`` in ``~/.aws/config`` would otherwise
    make ordinary ``cli.main`` calls try to prompt. Setting the env var off
    short-circuits the whole chain; tests that exercise the resolution override
    this with ``setenv`` / ``delenv`` (and ``AWS_CONFIG_FILE`` for the config
    path). Inherited by the e2e subprocesses too, keeping them prompt-free.
    """
    monkeypatch.setenv("AWS_CLI_AUTO_PROMPT", "off")
