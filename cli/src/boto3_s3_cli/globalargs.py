"""Common (global) option registration shared by every ``boto3-s3`` subcommand.

These mirror ``aws s3``'s connection / auth and presentation globals (the file
plays the role of aws-cli's ``globalargs.py``). :func:`add_common_arguments`
registers them on the top-level parser and on each subparser, so a global may
sit before or after the subcommand (``boto3-s3 --profile foo ls s3://b`` and
``boto3-s3 ls s3://b --profile foo``, matching ``aws s3``). Turning the parsed
connection / auth values into a boto3 client is :mod:`~boto3_s3_cli.clientfactory`'s
job; this module stays SDK-free so the parse path never pays an SDK import
(import contract, docs/imports.md). The presentation globals are accepted and
ignored (``docs/aws-cli-option-handling.md`` section 2); ``--cli-auto-prompt``
launches the interactive prompt, resolved from raw argv by the dispatcher
before parsing (section 3).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

# Pure-Python name (exceptions module) - safe on the parse path (import
# contract, docs/imports.md).
from boto3_s3 import ValidationError

# Mirrored from aws-cli (awscli/data/cli.json) so an invalid value
# still errors the same way before the flag is ignored (option-handling section 2).
_OUTPUT_CHOICES = ["json", "text", "table", "yaml", "yaml-stream", "off"]
_COLOR_CHOICES = ["on", "off", "auto"]
_CLI_ERROR_FORMAT_CHOICES = ["legacy", "json", "yaml", "text", "table", "enhanced"]
_BINARY_FORMAT_CHOICES = ["base64", "raw-in-base64-out"]

# aws-cli resolves the active profile as --profile > AWS_PROFILE >
# AWS_DEFAULT_PROFILE > default: its bundled botocore lists the env vars as
# ['AWS_PROFILE', 'AWS_DEFAULT_PROFILE'], whereas stock botocore reverses them to
# ['AWS_DEFAULT_PROFILE', 'AWS_PROFILE'] - a long-standing divergence (botocore
# #1725). A bare boto3.Session(profile_name=None) would inherit stock order, so
# the two CLIs would pick *different* profiles when both env vars are set.
# The single home of that ordering; every profile read goes through it.
PROFILE_ENV_VARS = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE")


def validate_query(args: argparse.Namespace) -> None:
    """Reject an invalid ``--query`` JMESPath with aws's wording (rc 252).

    aws-cli compiles ``--query`` at ``top-level-args-parsed`` (its globalargs
    ``_resolve_query``) - before it resolves ``--endpoint-url`` and before any
    paramfile expansion - so a bad expression is its ParamValidation 252 ahead
    of every other head check (measured against aws 2.35.18: it beats a bad
    ``--endpoint-url``, a bad ``--page-size`` paramfile, and a bad
    ``--profile``). Every command's ``run()`` calls this first. ``jmespath`` is
    a botocore dependency that is always importable and pulls in no SDK, and it
    is loaded only when ``--query`` is actually present, so the parse path stays
    SDK-free (import contract, docs/imports.md).
    """
    value = getattr(args, "query", None)
    if value is None:
        return
    import jmespath
    from jmespath.exceptions import JMESPathError

    try:
        jmespath.compile(value)
    except JMESPathError as exc:
        raise ValidationError(f"Bad value for --query {value}: {exc}") from exc


def _pkg_version_or_unknown(pkg: str) -> str:
    """Return *pkg*'s installed version, or ``"unknown"`` if it isn't installed.

    ``importlib.metadata.version`` raises ``PackageNotFoundError`` for a
    distribution missing from the environment (an unbuilt checkout, or a missing
    optional dependency). ``--version`` must never crash, so emit a placeholder.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(pkg)
    except PackageNotFoundError:
        return "unknown"


def _version_string() -> str:
    """Build the ``--version`` line in aws-cli v2 User-Agent style.

    Mirrors ``aws --version``'s token shape: both
    boto3-s3 distributions, both AWS SDK halves (``boto3`` / ``botocore`` - their
    patch versions can drift inside boto3's botocore range pin), then the Python
    and OS ``boto3-s3`` runs on. The two distributions are versioned
    independently, so each token is resolved on its own.

    Only called when ``--version`` fires: the metadata lookups cost ~20ms to
    import, which every other invocation must not pay (import contract,
    docs/imports.md). The boto3/botocore tokens come from distribution
    metadata, not from importing the packages.
    """
    import platform

    return (
        f"boto3-s3-cli/{_pkg_version_or_unknown('boto3-s3-cli')} "
        f"boto3-s3/{_pkg_version_or_unknown('boto3-s3')} "
        f"boto3/{_pkg_version_or_unknown('boto3')} "
        f"botocore/{_pkg_version_or_unknown('botocore')} "
        f"Python/{platform.python_version()} "
        f"{platform.system()}/{platform.release()}"
    )


class _VersionAction(argparse.Action):
    """``--version`` action: print one unwrapped line on stdout, then exit 0.

    The stock ``argparse`` version action runs the text through the help
    formatter, which wraps to terminal width and splits the User-Agent line at
    awkward points. ``aws --version`` emits a single line; mirroring that keeps
    the output copy-pasteable into bug reports. The line is rendered here, at
    fire time, rather than taken as a parser-build kwarg - see
    :func:`_version_string` for why it must not run on every invocation.
    """

    def __init__(
        self,
        option_strings: Sequence[str],
        dest: str = argparse.SUPPRESS,
        default: str = argparse.SUPPRESS,
        help: str | None = None,
    ) -> None:
        super().__init__(
            option_strings=list(option_strings),
            dest=dest,
            default=default,
            nargs=0,
            help=help,
        )

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        sys.stdout.write(_version_string() + "\n")
        parser.exit()


def add_common_arguments(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    """Register the connection/auth (effective) and presentation (ignored) globals.

    Added to BOTH the top-level parser (with real defaults) and each subparser
    (``suppress_defaults=True``). Suppressing the subparser-side defaults stops an
    unspecified flag from clobbering a value parsed *before* the subcommand, so a
    global may sit on either side - ``boto3-s3 --profile foo ls s3://b`` and
    ``boto3-s3 ls s3://b --profile foo`` both work (matching aws-cli).
    """
    flag = argparse.SUPPRESS if suppress_defaults else False
    value = argparse.SUPPRESS if suppress_defaults else None

    conn = parser.add_argument_group("connection / auth")
    conn.add_argument("--profile", metavar="NAME", default=value)
    conn.add_argument("--region", metavar="REGION", default=value)
    conn.add_argument("--endpoint-url", metavar="URL", default=value)
    conn.add_argument("--no-verify-ssl", action="store_true", default=flag)
    conn.add_argument("--ca-bundle", metavar="PATH", default=value)
    conn.add_argument("--no-sign-request", action="store_true", default=flag)
    # Not argparse type=int on purpose: aws coerces these in a post-parse session
    # handler (globalargs._resolve_timeout), so a non-integer value raises there
    # and exits 255 - not the parse-time 252 a type=int would give. build_client
    # coerces and maps a bad value to rc 255 (matching aws and --page-size).
    conn.add_argument("--cli-read-timeout", metavar="SECONDS", default=value)
    conn.add_argument("--cli-connect-timeout", metavar="SECONDS", default=value)
    conn.add_argument("--debug", action="store_true", default=flag)

    ignored = parser.add_argument_group("recognized but ignored")
    ignored.add_argument("--output", choices=_OUTPUT_CHOICES, default=value)
    ignored.add_argument("--query", default=value)
    ignored.add_argument("--no-paginate", action="store_true", default=flag)
    ignored.add_argument("--no-cli-pager", action="store_true", default=flag)
    ignored.add_argument("--color", choices=_COLOR_CHOICES, default=value)
    ignored.add_argument("--cli-error-format", choices=_CLI_ERROR_FORMAT_CHOICES, default=value)
    ignored.add_argument("--no-cli-auto-prompt", action="store_true", default=flag)
    # Recognized for parity; consumed by no command - aws s3's own blob
    # argument (cp --sse-c-key) ignores it too (verbatim pass-through with
    # fileb:// file loading; option-handling section 2).
    ignored.add_argument("--cli-binary-format", choices=_BINARY_FORMAT_CHOICES, default=value)

    # Prints the multi-component version line and exits 0; works before or after
    # the subcommand. Custom action keeps it to one unwrapped line and defers
    # building it until the flag actually fires (see above).
    parser.add_argument("--version", action=_VersionAction)

    # Opt-in interactive UI (section 3, the "autoprompt" extra); the dispatcher
    # resolves it from raw argv before parsing. Listed in --help like its
    # --no-cli-auto-prompt counterpart (aws shows both).
    parser.add_argument("--cli-auto-prompt", action="store_true", default=flag)
