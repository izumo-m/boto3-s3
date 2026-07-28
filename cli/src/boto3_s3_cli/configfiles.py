"""SDK-free reads of aws's config and credentials files, taken before dispatch.

aws-cli builds its botocore session - and with it the session-backed error
handler chain - before it parses anything beyond the preliminary ``--profile``
/ ``--debug`` scan, and building it loads the whole merged config (its
``create_clidriver`` hands ``session.full_config`` to ``load_plugins``). Two
outcomes of that load are visible on the command line long before any S3 call,
so the dispatcher reproduces them from ``configparser`` alone - the AWS SDK
must stay unimported on the informational exits (design/imports.md):

- a file that is not valid INI aborts the whole run with botocore's
  ``ConfigParseError`` wording and rc 255, ahead of every parse outcome,
  ``--version`` and the help token included (``ConfigScan.unparseable``);
- a profile that is *named* but declared by neither file makes botocore's
  scoped-config read raise ``ProfileNotFound``. aws's error renderer asks the
  session for ``cli_error_format`` and swallows that failure, which costs the
  report its ``ParamValidation`` envelope (``ConfigScan.declares``, read by
  ``cli._write_error``).

The rules are botocore's ``configloader``: ``raw_config_parse`` (the path must
name a file, ``configparser`` must accept it, and an indented ``key = value``
block must split on ``=``) plus ``build_profile_map`` (in the config file
``[default]`` and ``[profile <name>]`` declare profiles and no other section
does; in the credentials file every section does).
"""

from __future__ import annotations

import os
from typing import NamedTuple

from boto3_s3_cli.globalargs import PROFILE_ENV_VARS

# botocore's session variables for the two files, with its defaults.
_CONFIG_FILE = ("AWS_CONFIG_FILE", "~/.aws/config")
_CREDENTIALS_FILE = ("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials")


class _UnparseableError(Exception):
    """Internal marker for what botocore turns into ``ConfigParseError``."""


class ConfigScan(NamedTuple):
    """One pass over both files: the first unparseable path, every profile name.

    ``unparseable`` is the path of the first file (config before credentials,
    botocore's load order) that failed to parse, or ``None`` when both were
    readable or absent. ``profiles`` is the merged profile map's key set.
    """

    unparseable: str | None
    profiles: frozenset[str]

    def declares(self, profile: str | None) -> bool:
        """Whether botocore's scoped-config read would accept ``profile``.

        ``None`` - nothing named a profile - is accepted: botocore answers
        from the ``default`` section, or an empty dict, without raising. An
        empty string is not, because the environment read is present-wins, so
        ``AWS_PROFILE=`` names the empty profile and ``ProfileNotFound``
        follows; an explicit ``--profile default`` against a machine with no
        config file is rejected for the same reason (both measured).
        """
        return profile is None or profile in self.profiles


def env_profile() -> str | None:
    """The profile the environment names, or ``None``.

    Present-wins over ``PROFILE_ENV_VARS`` (the single home of aws's
    ``AWS_PROFILE`` > ``AWS_DEFAULT_PROFILE`` order), an empty value included -
    the same rule ``clientfactory.resolve_profile`` opens its session with.
    """
    for name in PROFILE_ENV_VARS:
        if name in os.environ:
            return os.environ[name]
    return None


def config_file_path() -> str:
    """The path botocore would read the config file from."""
    return _resolve_path(*_CONFIG_FILE)


def scan() -> ConfigScan:
    """Parse both files once, in botocore's order."""
    profiles: set[str] = set()
    for path, is_credentials in (
        (config_file_path(), False),
        (_resolve_path(*_CREDENTIALS_FILE), True),
    ):
        try:
            sections = _parse(path)
        except _UnparseableError:
            return ConfigScan(path, frozenset(profiles))
        if sections is None:
            continue
        # Every credentials-file section is a profile; the config file needs
        # botocore's `[profile <name>]` / `[default]` filter.
        profiles |= set(sections) if is_credentials else _config_file_profiles(sections)
    return ConfigScan(None, frozenset(profiles))


def _resolve_path(env_var: str, default: str) -> str:
    """Resolve one file's path the way botocore's ``raw_config_parse`` does.

    Present-wins on the environment variable, an empty value included: an
    ``AWS_CONFIG_FILE=`` run has no config file (the empty path names no file)
    rather than falling back to ``~/.aws/config``. Both expansions are
    botocore's, in its order.
    """
    path = os.environ.get(env_var)
    if path is None:
        path = default
    return os.path.expanduser(os.path.expandvars(path))


def _parse(path: str) -> dict[str, list[str]] | None:
    """The sections (name -> option values) at ``path``, ``None`` when absent.

    Raises ``_UnparseableError`` for exactly the failures botocore reports as
    ``ConfigParseError``. A path that names no file is botocore's
    ``ConfigNotFound``, which its loader ignores, so it is ``None`` here rather
    than an error; ``configparser`` itself skips a file it cannot open.
    """
    import configparser

    if not os.path.isfile(path):
        return None
    parser = configparser.RawConfigParser()
    try:
        parser.read([path])
    except (configparser.Error, UnicodeDecodeError):
        raise _UnparseableError from None
    sections = {
        name: [parser.get(name, option) for option in parser.options(name)]
        for name in parser.sections()
    }
    for values in sections.values():
        for value in values:
            # A value starting with a newline is an indented block (`s3 =`
            # followed by `key = value` lines); botocore parses one level of
            # it and reports a line that will not split as a parse error.
            if value.startswith("\n") and not all(
                "=" in line for line in map(str.strip, value.splitlines()) if line
            ):
                raise _UnparseableError
    return sections


def _config_file_profiles(sections: dict[str, list[str]]) -> set[str]:
    """botocore's ``build_profile_map`` over the config file's section names.

    ``[default]`` is a profile by name, ``[profile <name>]`` declares one
    (shell-quoted, so ``[profile "two words"]`` works), and every other
    section - ``[profilefoo]``, ``[preview]`` - is plain configuration.
    """
    import shlex

    names: set[str] = set()
    for section in sections:
        if section == "default":
            names.add(section)
        elif section.startswith("profile"):
            try:
                parts = shlex.split(section)
            except ValueError:
                continue
            if len(parts) == 2:
                names.add(parts[1])
    return names
