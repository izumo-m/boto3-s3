"""Resolve whether ``--cli-auto-prompt`` fires during the pre-parse step.

The dispatcher consults this before argparse runs - aws-cli resolves at the
same stage relative to command dispatch, though after its own
``FirstPassGlobalArgParser.parse_known_args`` pre-pass (``clidriver.py``) - so
the prompt can fire even without a subcommand and its env / config chain is
honored. Only ``os.environ`` and the dispatcher's ``configfiles`` scan are
read, never the SDK or ``prompt_toolkit``. The interactive prompt itself
(``prompt``) stays a lazy, opt-in import.
"""

from __future__ import annotations

import os
from typing import cast

from boto3_s3_cli import configfiles

# The flags take no value, so a raw-argv membership test is exact. They are
# also declared on the parser so argparse accepts them on the normal off-mode
# dispatch (e.g. `ls --no-cli-auto-prompt`) and the help page lists them; the
# dispatcher strips them before re-dispatching a completed command line.
AUTO_PROMPT_FLAG = "--cli-auto-prompt"
NO_AUTO_PROMPT_FLAG = "--no-cli-auto-prompt"
# Presence of any of these means "show help/version, don't prompt" - aws-cli's
# _NO_AUTO_PROMPT_ARGS, token for token (its clidriver.py tests the raw argv
# for exactly these two).
NO_PROMPT_ARGS = ("help", "--version")
# The env var and profile config key aws-cli resolves cli_auto_prompt from
# (aws-cli clidriver.py _construct_cli_auto_prompt_chain: env > scoped config >
# 'off').
_AUTO_PROMPT_ENV = "AWS_CLI_AUTO_PROMPT"
_AUTO_PROMPT_CONFIG_KEY = "cli_auto_prompt"


def resolve_auto_prompt_mode(raw_argv: list[str], scan: configfiles.ConfigScan) -> str:
    """Resolve the auto-prompt mode (``on`` / ``on-partial`` / ``off``).

    Mirrors aws-cli's ``resolve_auto_prompt_mode`` (aws-cli's ``clidriver.py``)
    plus the config chain (``clidriver.py`` ``_construct_cli_auto_prompt_chain``)
    - except the ``--cli-auto-prompt`` / ``--no-cli-auto-prompt`` mutual
    exclusion, which aws validates inside its resolver and the dispatcher's
    pre-pass here checks separately:
    ``help``/``--version`` -> off; ``--no-cli-auto-prompt`` -> off;
    ``--cli-auto-prompt`` -> on; else ``AWS_CLI_AUTO_PROMPT`` env -> profile
    ``cli_auto_prompt`` -> ``off``. The value is lowercased and anything other
    than ``on`` / ``on-partial`` behaves as off (aws's else branch).

    The profile whose section is read is the one the *environment* names
    (``configfiles.env_profile``), never ``--profile``: aws resolves the mode
    before it binds ``--profile`` to its session, so the setting under
    ``[profile X]`` is not read on ``--profile X`` and the one under the
    environment's profile - ``[default]`` when it names none - is. An
    environment profile no file declares reads as nothing set, the
    ``ProfileNotFound`` aws catches into ``off``; an empty ``AWS_PROFILE=``
    names such a profile. Reading from ``--profile`` instead is not a UI
    nicety: the mode decides non-interactive runs too (measured: with
    ``cli_auto_prompt = on`` under ``[profile X]``, ``--profile X`` on a
    terminal-less run executed the command on aws and stopped at rc 255 here).

    *scan* is the dispatcher's snapshot of the config files, so the setting is
    read through botocore's own rules - the credentials file merged in, an
    indented block parsed into the map botocore makes of it - instead of a
    second, looser INI read of this module's own. That map is what makes the
    lowercasing below aws's verbatim operation rather than a formality: aws
    calls ``.lower()`` on whatever its config chain answered with, so a block
    raises ``AttributeError`` there and its entry-point chain reports it at rc
    255. Running the same call raises the same error, which the dispatcher
    reports; nothing about that message is rebuilt here.
    """
    if any(flag in raw_argv for flag in NO_PROMPT_ARGS):
        return "off"
    if NO_AUTO_PROMPT_FLAG in raw_argv:
        return "off"
    if AUTO_PROMPT_FLAG in raw_argv:
        return "on"
    value: configfiles.ConfigValue | None = os.environ.get(_AUTO_PROMPT_ENV)
    if value is None:
        value = scan.scoped(configfiles.env_profile()).get(_AUTO_PROMPT_CONFIG_KEY)
    # The cast is what lets a map reach `.lower()` and raise there, as aws's
    # call does; screening it out instead would answer "off" for a config that
    # stops aws dead.
    mode = cast("str", value).lower() if value else "off"
    # aws's else branch treats any unrecognized value as off; normalize the
    # *return* too, honoring the documented on / on-partial / off domain
    # (design/autoprompt.md) instead of handing callers raw config text.
    return mode if mode in ("on", "on-partial") else "off"
