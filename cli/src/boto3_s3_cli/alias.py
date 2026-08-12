"""aws's ``~/.aws/cli/alias`` file, mapped onto this CLI's subcommand table.

aws registers an alias injector on the suffix-less ``building-command-table``
event, which its emitter matches for ``building-command-table.s3`` as well, so
the ``[command s3]`` section of that file adds subcommands to ``aws s3`` (and
shadows built-in ones). This CLI's whole surface *is* that table, so
``[command s3]`` is the section that maps onto it; ``[toplevel]``, whose
entries name services, has no counterpart here and is ignored.

The file path is hardcoded in aws (``AliasLoader``'s default argument), so
``AWS_CONFIG_FILE`` does not move it, and it is read on **every** invocation,
before the top-level parse - which is why an unreadable one aborts even
``--version`` and the help token (all measured against the pinned aws-cli).
Reading it here uses ``configparser`` alone, like `configfiles`: this runs
ahead of the informational exits, which may not load the AWS SDK
(design/imports.md).

Two aliases shapes exist. An *external* alias (``name = !cmd``) runs ``cmd``
through the shell with the invocation's remaining arguments appended, and its
exit status becomes the CLI's. An *internal* alias expands to CLI arguments,
which are parsed again: `cli` re-enters its own resolution with the expanded
tokens ahead of the user's, exactly as aws re-enters the ``s3`` command.
"""

from __future__ import annotations

import os
from typing import NamedTuple

from boto3_s3 import InvalidConfigError

# aws's report when its parser fails on the file, and when the path names
# something that is not a file (botocore's ConfigParseError / ConfigNotFound,
# which is what its loader raises through raw_config_parse).
_UNPARSEABLE = "Unable to parse config file: {}"
_NOT_FOUND = "The specified config file ({}) could not be found."

# What aws's injection does to a *leaf* table (a `[command s3 <subcommand>]`
# section): the subcommand's own table stops being empty, so its parser is
# built through the path that copies the argument table with ``required =
# False`` - which argparse rejects for the positional every one of these
# commands declares. The result is that one report, at rc 255, for every
# invocation of that subcommand, help included (measured). It is the section's
# mere presence that does it, so the entries themselves are never reached.
LEAF_SECTION_REPORT = "'required' is an invalid argument for positionals"

# The section names this CLI maps: aws's alias loader turns `[command s3]`
# into the key ('command', 's3') by splitting on whitespace, so a stray extra
# space still names the same section.
_SECTION_PREFIX = "command"
_COMMAND_SECTION = (_SECTION_PREFIX, "s3")


class AliasTable(NamedTuple):
    """The alias file's entries that bear on this CLI's command table.

    ``entries`` maps an alias name to its raw value (``configparser`` lowers
    the names, so ``LSR`` is invoked as ``lsr``; the values keep their case and
    are stripped of surrounding whitespace, as aws strips them). ``leaves`` is
    the set of subcommand names that carry a non-empty ``[command s3 <name>]``
    section, which is aws's rc-255 crash rather than a usable alias -
    `LEAF_SECTION_REPORT`.
    """

    entries: dict[str, str]
    leaves: frozenset[str]


def alias_file_path() -> str:
    """The path aws reads aliases from, ``~/.aws/cli/alias``."""
    return os.path.expanduser(os.path.join("~", ".aws", "cli", "alias"))


def load() -> AliasTable:
    """Read the alias file, or return an empty table when there is none.

    Absent is not an error (aws tests for existence first), but a path that
    exists and is not a readable INI file is: both failures below are aws's,
    reported with botocore's own wording at rc 255. A file the process cannot
    open is neither - ``configparser`` skips it silently, so it reads as having
    no aliases, which is aws's behavior too.
    """
    path = alias_file_path()
    if not os.path.exists(path):
        return AliasTable({}, frozenset())
    if not os.path.isfile(path):
        raise InvalidConfigError(_NOT_FOUND.format(path))
    sections = _parse(path)
    entries: dict[str, str] = {}
    leaves: set[str] = set()
    for section, options in sections.items():
        key = tuple(section.split())
        if key[:1] != (_SECTION_PREFIX,):
            continue
        if key == _COMMAND_SECTION:
            # Assigned, not merged: aws re-keys each section in turn, so where
            # two spellings (`[command s3]`, `[command  s3]`) normalize to the
            # same key the last one read replaces the first.
            entries = options
        elif len(key) == 3 and key[1] == _COMMAND_SECTION[1] and options:
            leaves.add(key[2])
    return AliasTable(entries, frozenset(leaves))


def _parse(path: str) -> dict[str, dict[str, str]]:
    """botocore's ``raw_config_parse(path, parse_subsections=False)``.

    Subsection parsing is off in aws's alias loader, so an indented
    continuation stays part of its option's value (a multi-line alias value is
    one string with embedded newlines) instead of becoming a map.
    """
    import configparser

    parser = configparser.RawConfigParser()
    try:
        parser.read([path])
    except (configparser.Error, UnicodeDecodeError) as exc:
        raise InvalidConfigError(_UNPARSEABLE.format(path)) from exc
    return {
        # aws strips every value it loads, so a value written on the line
        # below its `name =` does not start with a newline.
        section: {option: parser.get(section, option).strip() for option in parser.options(section)}
        for section in parser.sections()
    }


def is_external(value: str) -> bool:
    """Does this alias value run a shell command rather than CLI arguments?"""
    return value.startswith("!")


def split_value(name: str, value: str) -> list[str]:
    """Split an internal alias's value into arguments, aws's way (rc 255 on failure).

    ``shlex`` is the splitter, so quoting inside the value works and an
    unbalanced quote is aws's ``InvalidAliasException`` - reported verbatim,
    the offending value included. Each token is then stripped of line
    separators, which is what keeps a multi-line value's last token clean.
    """
    import shlex

    try:
        arguments = shlex.split(value)
    except ValueError as exc:
        raise InvalidConfigError(
            f'Value of alias "{name}" could not be parsed. '
            f"Received error: {exc} when parsing:\n{value}"
        ) from exc
    return [argument.strip(os.linesep) for argument in arguments]


def run_external(value: str, arguments: list[str]) -> int:
    """Run an external alias through the shell and return its exit status.

    aws builds one command line - the value after the ``!``, then the
    invocation's remaining arguments, each shell-quoted - and hands it to
    ``subprocess.call(shell=True)``, so the alias may be a pipeline and its
    status is what the CLI exits with (measured: a value of ``!exit 7`` exits
    7, an unknown program exits the shell's 127).

    Running a shell is the feature, not an oversight: the command line comes
    from the user's own ``~/.aws/cli/alias``, the same trust level as a shell
    rc file. What the *invocation* contributes is only the arguments, and
    those are quoted (`_shell_quote`) so they stay arguments.
    """
    import subprocess

    command = " ".join([value[1:], *(_shell_quote(argument) for argument in arguments)])
    return subprocess.call(command, shell=True)


def _shell_quote(value: str) -> str:
    """aws's ``compat_shell_quote(value, shell=True)``.

    ``shlex.quote`` is POSIX-only, so Windows gets aws's own rule: quote when
    the value carries a character ``cmd.exe`` would act on, and escape the
    double quotes and the backslash runs that lead into them (and, before a
    closing quote, a trailing run) for the C runtime's argv parser.
    """
    import sys

    if sys.platform != "win32":
        import shlex

        return shlex.quote(value)
    return _windows_cmd_quote(value)


# The characters aws's Windows quoting treats as cmd.exe metacharacters.
_WINDOWS_UNSAFE = frozenset("&<>[]|{}^=;!'()+,`~ \t")


def _windows_cmd_quote(value: str) -> str:
    """The ``cmd.exe`` half of `_shell_quote` (aws's ``_windows_cmd_shell_quote``)."""
    if not value:
        return '""'
    quoted: list[str] = []
    backslashes = 0
    needs_quoting = False
    for character in value:
        if character == "\\":
            backslashes += 1
            continue
        if character == '"':
            if backslashes:
                quoted.append("\\" * (backslashes * 2))
                backslashes = 0
            quoted.append('\\"')
            needs_quoting = True
            continue
        if backslashes:
            quoted.append("\\" * backslashes)
            backslashes = 0
        if character in _WINDOWS_UNSAFE:
            needs_quoting = True
        quoted.append(character)
    if needs_quoting:
        # A trailing backslash run would escape the closing quote, so it is
        # doubled first.
        quoted.append("\\" * (backslashes * 2))
        return '"{}"'.format("".join(quoted))
    quoted.append("\\" * backslashes)
    return "".join(quoted)
