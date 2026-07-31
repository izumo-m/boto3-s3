"""SDK-free reads of aws's config and credentials files, taken before dispatch.

aws-cli builds its botocore session - and with it the session-backed error
handler chain - before it parses anything beyond the preliminary ``--profile``
/ ``--debug`` scan, and building it loads the whole merged config (its
``create_clidriver`` hands ``session.full_config`` to ``load_plugins``). Three
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
  ``cli._write_error``);
- an unknown ``cli_timestamp_format`` in the *selected* profile aborts the run
  at rc 253 (``ConfigScan.invalid_timestamp_format``, reported by
  ``cli._dispatch``).

The rules are botocore's ``configloader``: ``raw_config_parse`` (the path must
name a file, ``configparser`` must accept it, and an indented ``key = value``
block is parsed one level deep, a line that will not split on ``=`` being the
parse failure) plus ``build_profile_map`` (in the config file ``[default]``
and ``[profile <name>]`` declare profiles and no other section does; in the
credentials file every section does).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import NamedTuple, TypeAlias

from boto3_s3_cli.globalargs import PROFILE_ENV_VARS

# botocore's session variables for the two files, with its defaults.
_CONFIG_FILE = ("AWS_CONFIG_FILE", "~/.aws/config")
_CREDENTIALS_FILE = ("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials")

# What one option holds after botocore's parse: its text, or - when the value
# is an indented block (`s3 =` followed by `key = value` lines) - the one-level
# map it parses that into.
ConfigValue: TypeAlias = "str | dict[str, str]"

# aws's timestamp-format setting and the only two values its customization
# accepts; anything else is the rc-253 report `cli` renders.
TIMESTAMP_FORMAT_KEY = "cli_timestamp_format"
_TIMESTAMP_FORMATS = ("wire", "iso8601")


class _UnparseableError(Exception):
    """Internal marker for what botocore turns into ``ConfigParseError``."""


class ConfigScan(NamedTuple):
    """One pass over both files: the first unparseable path, the profile map.

    ``unparseable`` is the path of the first file (config before credentials,
    botocore's load order) that failed to parse, or ``None`` when both were
    readable or absent. ``profiles`` is the merged profile map botocore's
    ``full_config`` builds: each declared profile's options, with a
    credentials-file section *updating* the config file's same-named profile
    key by key (measured: a ``cli_timestamp_format`` in the credentials file
    overrides the config file's, while some other key there leaves it
    standing).
    """

    unparseable: str | None
    profiles: Mapping[str, Mapping[str, ConfigValue]]

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

    def scoped(self, profile: str | None) -> Mapping[str, ConfigValue]:
        """The options botocore's ``get_scoped_config`` would answer with.

        ``None`` reads the ``default`` section, a name reads its own. An
        undeclared name is botocore's ``ProfileNotFound``, which every reader
        here treats as "nothing set" - aws's timestamp-format handler catches
        it explicitly and falls back to its default - so this answers with an
        empty map instead of raising.
        """
        return self.profiles.get("default" if profile is None else profile, {})

    def invalid_timestamp_format(self, profile: str | None) -> ConfigValue | None:
        """``profile``'s rejected ``cli_timestamp_format`` value, or ``None``.

        aws validates the setting in the first handler of its
        ``session-initialized`` event (its timestamp-format customization),
        reading it off the session's scoped config - so the profile is the one
        already bound by then (``--profile`` included) and an absent key is its
        ``iso8601`` default. Everything other than ``wire`` / ``iso8601`` is
        rejected verbatim, an empty value and a wrong-cased ``WIRE`` included
        (all measured).
        """
        value = self.scoped(profile).get(TIMESTAMP_FORMAT_KEY)
        if value is None or value in _TIMESTAMP_FORMATS:
            return None
        return value


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
    profiles: dict[str, dict[str, ConfigValue]] = {}
    for path, is_credentials in (
        (config_file_path(), False),
        (_resolve_path(*_CREDENTIALS_FILE), True),
    ):
        try:
            sections = _parse(path)
        except _UnparseableError:
            return ConfigScan(path, profiles)
        if sections is None:
            continue
        # Every credentials-file section is a profile; the config file needs
        # botocore's `[profile <name>]` / `[default]` filter. The per-key
        # update across the two files is botocore's `full_config`.
        found = sections if is_credentials else _config_file_profiles(sections)
        for name, options in found.items():
            profiles.setdefault(name, {}).update(options)
    return ConfigScan(None, profiles)


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


def _parse(path: str) -> dict[str, dict[str, ConfigValue]] | None:
    """The sections (name -> options) at ``path``, ``None`` when absent.

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
    sections: dict[str, dict[str, ConfigValue]] = {}
    for name in parser.sections():
        options: dict[str, ConfigValue] = {}
        for option in parser.options(name):
            value = parser.get(name, option)
            # A value starting with a newline is an indented block (`s3 =`
            # followed by `key = value` lines), which botocore keeps as a map.
            options[option] = _parse_nested(value) if value.startswith("\n") else value
        sections[name] = options
    return sections


def _parse_nested(block: str) -> dict[str, str]:
    """botocore's one-level-deep parse of an indented ``key = value`` block.

    A line that will not split on ``=`` is what botocore turns into
    ``ConfigParseError``, so it is the unparseable-file failure here too.
    """
    parsed: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise _UnparseableError
        parsed[key.strip()] = value.strip()
    return parsed


def _config_file_profiles(
    sections: dict[str, dict[str, ConfigValue]],
) -> dict[str, dict[str, ConfigValue]]:
    """botocore's ``build_profile_map`` over the config file's sections.

    ``[default]`` is a profile by name, ``[profile <name>]`` declares one
    (shell-quoted, so ``[profile "two words"]`` works), and every other
    section - ``[profilefoo]``, ``[preview]`` - is plain configuration.
    Whole sections are claimed, not merged, so where both ``[default]`` and
    ``[profile default]`` name the same profile the later one wins outright
    (botocore's loop, measured).
    """
    import shlex

    profiles: dict[str, dict[str, ConfigValue]] = {}
    for section, options in sections.items():
        if section == "default":
            profiles[section] = options
        elif section.startswith("profile"):
            try:
                parts = shlex.split(section)
            except ValueError:
                continue
            if len(parts) == 2:
                profiles[parts[1]] = options
    return profiles
