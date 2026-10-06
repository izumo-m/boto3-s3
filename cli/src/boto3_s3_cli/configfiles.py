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
import re
from collections.abc import Mapping
from typing import NamedTuple, TypeAlias
from urllib.parse import urlsplit

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

# aws's binary-format setting and the two keys its customization's handler
# table declares; anything else is the rc-255 report `cli` renders.
BINARY_FORMAT_KEY = "cli_binary_format"
_BINARY_FORMATS = ("base64", "raw-in-base64-out")


# What botocore reads while it *builds* the credential provider chain (its
# `create_credential_resolver` and the IMDS fetcher that constructs), each as
# (environment variable, config key) and in the order it reads them: two
# values it converts with `int`, the endpoint it later checks for being a
# URL, the endpoint mode it checks against its two modes, and a boolean it
# cannot fail on. The order matters for one case, an undeclared profile (see
# `ConfigScan.credential_chain_suspect`). One more boolean, `imds_use_ipv6`,
# is read between the last two when no mode is set; it is left out because
# only a declared profile can get that far without a mode, and for a
# declared profile no read can fail.
_CHAIN_INTEGER_SETTINGS = (
    ("AWS_METADATA_SERVICE_TIMEOUT", "metadata_service_timeout"),
    ("AWS_METADATA_SERVICE_NUM_ATTEMPTS", "metadata_service_num_attempts"),
)
_CHAIN_ENDPOINT_SETTING = ("AWS_EC2_METADATA_SERVICE_ENDPOINT", "ec2_metadata_service_endpoint")
_CHAIN_ENDPOINT_MODE_SETTING = (
    "AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE",
    "ec2_metadata_service_endpoint_mode",
)
_CHAIN_V1_DISABLED_SETTING = ("AWS_EC2_METADATA_V1_DISABLED", "ec2_metadata_v1_disabled")
_IMDS_ENDPOINT_MODES = ("ipv4", "ipv6")
# botocore's own test for that endpoint (its `is_valid_uri`), which the IMDS
# fetcher applies to a configured one: no tab, CR or LF anywhere, and a host
# that is either DNS-shaped - at most 255 characters, labels of 1 to 63
# letters, digits and inner hyphens, one trailing dot allowed - or, between
# brackets, an RFC 3986 IPv6 literal with an optional zone id. The patterns
# are botocore's verbatim and are the same in the copy aws bundles; they are
# kept here because the scan must not import the SDK.
_UNSAFE_URL_CHARS = frozenset("\t\r\n")
_DNS_HOST_RE = re.compile(
    r"^((?!-)[A-Z\d-]{1,63}(?<!-)\.)*((?!-)[A-Z\d-]{1,63}(?<!-))$", re.IGNORECASE
)
_IPV4_PAT = r"(?:[0-9]{1,3}\.){3}[0-9]{1,3}"
_HEX_PAT = "[0-9A-Fa-f]{1,4}"
_LS32_PAT = f"(?:{_HEX_PAT}:{_HEX_PAT}|{_IPV4_PAT})"
_IPV6_PAT = "(?:" + "|".join(
    variation % {"hex": _HEX_PAT, "ls32": _LS32_PAT}
    for variation in (
        "(?:%(hex)s:){6}%(ls32)s",
        "::(?:%(hex)s:){5}%(ls32)s",
        "(?:%(hex)s)?::(?:%(hex)s:){4}%(ls32)s",
        "(?:(?:%(hex)s:)?%(hex)s)?::(?:%(hex)s:){3}%(ls32)s",
        "(?:(?:%(hex)s:){0,2}%(hex)s)?::(?:%(hex)s:){2}%(ls32)s",
        "(?:(?:%(hex)s:){0,3}%(hex)s)?::%(hex)s:%(ls32)s",
        "(?:(?:%(hex)s:){0,4}%(hex)s)?::%(ls32)s",
        "(?:(?:%(hex)s:){0,5}%(hex)s)?::%(hex)s",
        "(?:(?:%(hex)s:){0,6}%(hex)s)?::",
    )
) + ")"  # fmt: skip
_UNRESERVED_PAT = r"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._!\-~"
_ZONE_ID_PAT = "(?:%25|%)(?:[" + _UNRESERVED_PAT + "]|%[a-fA-F0-9]{2})+"
_IPV6_ADDRZ_RE = re.compile(r"^\[" + _IPV6_PAT + r"(?:" + _ZONE_ID_PAT + r")?\]$")


class _UnparseableError(Exception):
    """Internal marker for what botocore turns into ``ConfigParseError``."""


class _UndeclaredProfileError(Exception):
    """Internal marker for a read botocore answers with ``ProfileNotFound``."""


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

    def invalid_binary_format(self, profile: str | None) -> ConfigValue | None:
        """``profile``'s rejected ``cli_binary_format`` value, or ``None``.

        aws resolves the setting in its ``session-initialized`` binary-format
        customization, registered after the timestamp one - so a bad
        ``cli_timestamp_format`` wins (measured). An explicit
        ``--cli-binary-format`` never reaches the config (argparse restricts
        it to the valid choices first), and the config value indexes the
        customization's handler table directly, so anything but the two table
        keys surfaces as the bare ``KeyError`` - the ``repr`` of the value -
        through aws's general rc-255 handler (measured over four values,
        ``Base64`` included: the match is case-sensitive). An undeclared
        profile stands down to the ``base64`` default (``ProfileNotFound``
        caught, like the timestamp handler); an absent key is that default
        too.
        """
        value = self.scoped(profile).get(BINARY_FORMAT_KEY)
        if value is None or value in _BINARY_FORMATS:
            return None
        return value

    def credential_chain_suspect(self, profile: str | None) -> bool:
        """Whether building botocore's credential provider chain could fail.

        aws builds that chain during startup (its ``session-initialized``
        handler that installs the credential cache asks the session for it),
        and botocore converts and validates four settings while it does: a
        non-integer ``metadata_service_timeout`` /
        ``metadata_service_num_attempts``, an unknown
        ``ec2_metadata_service_endpoint_mode`` and a malformed
        ``ec2_metadata_service_endpoint`` each raise there, which stops the
        run before the help token and every command layer.

        This answers only *whether one of them would*, so the dispatcher
        builds the chain for real - importing the SDK - for the
        configurations that fail, and botocore raises its own error with its
        own wording. The tests are botocore's own, applied without it: the
        very ``int`` it converts the two integers with, its two endpoint
        modes, and its endpoint check (`_imds_endpoint_refused`).

        The settings are read the way botocore reads them, and in its order.
        Each is the environment variable when present, an empty value
        included, else ``profile``'s config key. For a profile no file
        declares, that second read is where botocore raises
        ``ProfileNotFound`` - which aws swallows at this step, abandoning the
        build - so whatever botocore would have read *after* the first
        setting missing from the environment is never looked at, however
        broken (measured: ``AWS_PROFILE=nope`` with a non-integer
        ``AWS_METADATA_SERVICE_NUM_ATTEMPTS`` and no
        ``AWS_METADATA_SERVICE_TIMEOUT`` still pages ``help`` at rc 0, on
        aws as here). The endpoint is checked last of all, when the fetcher
        is constructed, so under such a profile it counts only if every
        setting ahead of that construction came from the environment.

        Both ways of guessing wrong are visible, which is why this copies
        botocore rather than approximating it: a setting botocore refuses
        that this lets through leaves the help page and every usage error on
        their own outcome where aws stops at rc 255, and one botocore
        accepts that this suspects loads the SDK ahead of an informational
        exit.
        """
        declared = self.declares(profile)
        scoped = self.scoped(profile)

        def setting(names: tuple[str, str]) -> ConfigValue | None:
            env_var, key = names
            if env_var in os.environ:
                return os.environ[env_var]
            if not declared:
                raise _UndeclaredProfileError
            return scoped.get(key)

        try:
            for names in _CHAIN_INTEGER_SETTINGS:
                value = setting(names)
                if value is not None:
                    try:
                        int(value)  # pyright: ignore[reportArgumentType]
                    except (TypeError, ValueError):
                        return True
            endpoint = setting(_CHAIN_ENDPOINT_SETTING)
            mode = setting(_CHAIN_ENDPOINT_MODE_SETTING)
            if mode is not None and (
                not isinstance(mode, str) or mode.lower() not in _IMDS_ENDPOINT_MODES
            ):
                return True
            setting(_CHAIN_V1_DISABLED_SETTING)
        except _UndeclaredProfileError:
            return False
        return endpoint is not None and (
            not isinstance(endpoint, str) or _imds_endpoint_refused(endpoint)
        )


def _imds_endpoint_refused(endpoint: str) -> bool:
    """Whether botocore refuses *endpoint* as the IMDS endpoint.

    The IMDS fetcher uses a configured endpoint only when it is non-empty,
    and then requires botocore's ``is_valid_uri`` of it: the DNS-shaped host
    test or, failing that, the bracketed-IPv6 one, with the checks in
    botocore's order. Whatever makes botocore's own code raise instead of
    answer - ``urlsplit`` rejecting the netloc, the empty host it then
    indexes - is a refusal as well, since the chain build fails either way.
    """
    if not endpoint:
        return False
    if _UNSAFE_URL_CHARS.intersection(endpoint):
        return True
    try:
        hostname = urlsplit(endpoint).hostname
        if hostname is None:
            return True
        dns_host = hostname[:-1] if hostname[-1] == "." else hostname
        if len(hostname) <= 255 and _DNS_HOST_RE.match(dns_host) is not None:
            return False
        return _IPV6_ADDRZ_RE.match(f"[{hostname}]") is None
    except (ValueError, IndexError):
        return True


def env_profile() -> str | None:
    """The profile the environment names, or ``None``.

    Present-wins over ``PROFILE_ENV_VARS`` (the single home of aws's
    ``AWS_PROFILE`` > ``AWS_DEFAULT_PROFILE`` order), an empty value included -
    the rule ``clientfactory.resolve_profile`` falls back to, and the one
    botocore itself applies to the session variable
    ``clientfactory._open_botocore_session`` redeclares with the same names.
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
