"""Resolve whether ``--cli-auto-prompt`` fires during the pre-parse step.

The dispatcher consults this before argparse runs - aws-cli resolves at the
same stage relative to command dispatch, though after its own
``FirstPassGlobalArgParser.parse_known_args`` pre-pass (``clidriver.py``) - so
the prompt can fire even without a subcommand and its env / config chain is
honored. Only
``os.environ`` + ``configparser`` are read, never the SDK or ``prompt_toolkit``.
The interactive prompt itself (``prompt``) stays a lazy, opt-in import.
"""

from __future__ import annotations

import os

from boto3_s3_cli import configfiles
from boto3_s3_cli.globalargs import PROFILE_ENV_VARS

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


def resolve_auto_prompt_mode(raw_argv: list[str]) -> str:
    """Resolve the auto-prompt mode (``on`` / ``on-partial`` / ``off``).

    Mirrors aws-cli's ``resolve_auto_prompt_mode`` (aws-cli's ``clidriver.py``)
    plus the config chain (``clidriver.py`` ``_construct_cli_auto_prompt_chain``)
    - except the ``--cli-auto-prompt`` / ``--no-cli-auto-prompt`` mutual
    exclusion, which aws validates inside its resolver and the dispatcher's
    pre-pass here checks separately:
    ``help``/``--version`` -> off; ``--no-cli-auto-prompt`` -> off;
    ``--cli-auto-prompt`` -> on; else ``AWS_CLI_AUTO_PROMPT`` env -> profile
    ``cli_auto_prompt`` -> ``off``. The value is lowercased and anything other
    than ``on`` / ``on-partial`` behaves as off (aws's else branch). The profile
    whose config section is read is chosen by ``_active_profile``, which prefers
    ``--profile`` - a charter-exempt deviation from aws, which at this stage
    resolves the profile from the environment only (see its docstring).
    """
    if any(flag in raw_argv for flag in NO_PROMPT_ARGS):
        return "off"
    if NO_AUTO_PROMPT_FLAG in raw_argv:
        return "off"
    if AUTO_PROMPT_FLAG in raw_argv:
        return "on"
    value = os.environ.get(_AUTO_PROMPT_ENV)
    if value is None:
        value = _read_scoped_cli_auto_prompt(_active_profile(raw_argv))
    mode = value.lower() if value else "off"
    # aws's else branch treats any unrecognized value as off; normalize the
    # *return* too, honoring the documented on / on-partial / off domain
    # (design/autoprompt.md) instead of handing callers raw config text.
    return mode if mode in ("on", "on-partial") else "off"


def _active_profile(raw_argv: list[str]) -> str:
    """The profile whose config to consult: ``--profile`` > env > ``default``.

    The env ordering is ``PROFILE_ENV_VARS`` (the single home of aws's
    AWS_PROFILE > AWS_DEFAULT_PROFILE rule). Unlike
    ``clientfactory.resolve_profile`` (present-wins, because opening a session
    with an empty profile must fail like aws), an *empty* env value falls
    through here - this soft read only chooses which config section to consult
    for an interactive, charter-exempt setting.

    Deviation from aws: at auto-prompt resolution aws has not yet applied
    ``--profile`` to the session, so it reads ``cli_auto_prompt`` from the
    env-derived profile only. We prefer ``--profile`` here, so a
    ``cli_auto_prompt`` set only under a ``[profile X]`` section fires the prompt
    on ``--profile X`` for us but not for aws. Intentional usability preference,
    admissible because the auto-prompt UI is charter-exempt.
    """
    for i, arg in enumerate(raw_argv):
        if arg == "--profile" and i + 1 < len(raw_argv):
            return raw_argv[i + 1]
        if arg.startswith("--profile="):
            return arg.split("=", 1)[1]
    for name in PROFILE_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return "default"


def _read_scoped_cli_auto_prompt(profile: str) -> str | None:
    """Read ``cli_auto_prompt`` from the active profile in ``~/.aws/config``, SDK-free.

    A lightweight ``configparser`` read of botocore's ``ScopedConfigProvider``
    source: the config file (``configfiles.config_file_path``), the profile
    section (``[default]`` or ``[profile <name>]``), the key. Returns ``None``
    if absent. Not the full botocore resolution (no abbreviations / nested
    sections) - enough for this interactive, charter-exempt setting.

    Unparseable input is read as "absent" rather than raised, which on the
    normal dispatch is now defensive only: ``cli._main`` runs
    ``configfiles.scan`` upstream, and a file that fails to parse ends the run
    at rc 255 before this is ever called. What survives that pre-validation is
    the window between the two reads - a config rewritten or truncated in
    between - plus any direct caller. The tolerance stays because a
    pre-dispatch traceback is exactly what the exit-code charter
    (design/overview.md section 3) forbids.
    """
    import configparser

    path = configfiles.config_file_path()
    parser = configparser.RawConfigParser()
    try:
        # An unreadable file needs no handling: `read` swallows the OSError and
        # reports the file as unread, which is the same "absent" answer.
        if not parser.read(path):
            return None
    except (configparser.Error, UnicodeDecodeError):
        # botocore's configloader catches the same pair - UnicodeDecodeError
        # included, since it is not a configparser.Error.
        return None
    section = "default" if profile == "default" else f"profile {profile}"
    if parser.has_option(section, _AUTO_PROMPT_CONFIG_KEY):
        return parser.get(section, _AUTO_PROMPT_CONFIG_KEY)
    return None
