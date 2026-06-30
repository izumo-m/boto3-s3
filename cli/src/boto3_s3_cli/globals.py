"""Common (global) options shared by every ``boto3-s3`` subcommand.

These mirror ``aws s3``'s connection / auth and presentation globals.
:func:`add_common_arguments` registers them on the top-level parser and on each
subparser, so a global may sit before or after the subcommand
(``boto3-s3 --profile foo ls s3://b`` and ``boto3-s3 ls s3://b --profile foo``,
matching ``aws s3``). :func:`build_client` turns the
connection / auth ones into the boto3 S3 client the library consumes
(``docs/aws-cli-option-handling.md`` section 5). The presentation globals are
accepted and ignored (section 2); ``--cli-auto-prompt`` launches the interactive
prompt, resolved from raw argv by the dispatcher before parsing (section 3).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

# Pure-Python names (exceptions module) - safe on the parse path (import
# contract, docs/imports.md).
from boto3_s3 import Boto3S3Error, ConfigurationError, ValidationError

if TYPE_CHECKING:
    from botocore.session import Session as BotocoreSession
    from mypy_boto3_s3 import S3Client

# Mirrored from aws-cli (awscli/data/cli.json) so an invalid value
# still errors the same way before the flag is ignored (option-handling section 2).
_OUTPUT_CHOICES = ["json", "text", "table", "yaml", "yaml-stream", "off"]
_COLOR_CHOICES = ["on", "off", "auto"]
_CLI_ERROR_FORMAT_CHOICES = ["legacy", "json", "yaml", "text", "table", "enhanced"]
_BINARY_FORMAT_CHOICES = ["base64", "raw-in-base64-out"]


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
    # resolves it from raw argv before parsing.
    parser.add_argument(
        "--cli-auto-prompt", action="store_true", default=flag, help=argparse.SUPPRESS
    )


def _pin_python_sigv4_signers() -> None:
    """Keep the symmetric-SigV4 signers pure-Python, as aws v2 does.

    Stock botocore swaps ``v4`` / ``v4-query`` / ``s3v4`` / ``s3v4-query`` to
    CRT-backed signers whenever awscrt is importable - true once the ``crt``
    extra (it backs the CRT checksum algorithms, ``cp --checksum-algorithm
    CRC32C`` ...) or any co-installed package brings awscrt in. aws v2's
    bundled botocore instead hard-pins the pure-Python classes for those
    four, reserving CRT for the asymmetric SigV4a family (aws-cli's
    ``awscli/botocore/auth.py`` ``AUTH_TYPE_MAPS``). The difference is
    user-visible: the CRT presigner renders ``X-Amz-Expires`` after
    ``X-Amz-SignedHeaders``, so ``presign``'s URLs would diverge. Restore the
    aws-cli's entries (in-place table update - botocore resolves the signer
    from this table per request, however it was imported; without awscrt
    this just re-asserts the defaults).
    """
    from botocore import auth

    auth.AUTH_TYPE_MAPS.update(
        {
            "v4": auth.SigV4Auth,
            "v4-query": auth.SigV4QueryAuth,
            "s3v4": auth.S3SigV4Auth,
            "s3v4-query": auth.S3SigV4QueryAuth,
        }
    )


# aws-cli resolves the active profile as --profile > AWS_PROFILE >
# AWS_DEFAULT_PROFILE > default: its bundled botocore lists the env vars as
# ['AWS_PROFILE', 'AWS_DEFAULT_PROFILE'], whereas stock botocore reverses them to
# ['AWS_DEFAULT_PROFILE', 'AWS_PROFILE'] - a long-standing divergence (botocore
# #1725). A bare boto3.Session(profile_name=None) would inherit stock order, so
# the two CLIs would pick *different* profiles when both env vars are set.
_PROFILE_ENV_VARS = ("AWS_PROFILE", "AWS_DEFAULT_PROFILE")


def resolve_profile(args: argparse.Namespace) -> str | None:
    """The profile to open the session with (aws-cli precedence).

    ``--profile`` > the first env var of :data:`_PROFILE_ENV_VARS` that is
    *present* > ``None`` (boto3 then falls back to the ``default`` profile).
    Mirrors aws-cli's "first present env var wins" exactly, an empty value
    included (``AWS_PROFILE=`` selects the empty profile -> ProfileNotFound, like
    aws), so that `aws s3` parity holds even when both env vars are set. Only the
    CLI layer corrects this; the library (boto3.client fallback) stays
    boto3/botocore-faithful and keeps stock order on purpose.
    """
    if args.profile is not None:
        return args.profile
    for name in _PROFILE_ENV_VARS:
        if name in os.environ:
            return os.environ[name]
    return None


def _resolve_region(explicit: str | None, session: BotocoreSession) -> str | None:
    """The region to build the client in, via aws-cli's region chain.

    Mirrors aws-cli's ``_construct_cli_region_chain``: the *explicit* value
    (``--region``, or the caller's region for :func:`build_service_client`) >
    ``AWS_REGION`` env > ``AWS_DEFAULT_REGION`` env > the profile's config-file
    ``region`` > the EC2 IMDS region. Stock botocore never adopted ``AWS_REGION``
    (its region env is ``AWS_DEFAULT_REGION`` alone) and reserves its
    ``IMDSRegionProvider`` for smart-defaults, so a bare client would resolve a
    *different* region whenever ``AWS_REGION`` is the only source, or on an EC2
    host with no region configured. The env vars are present-wins, an empty value
    included (``AWS_REGION=`` -> ``""`` -> the same ``Invalid endpoint`` failure
    as aws, rc 255). Only the CLI corrects this; the library
    (``S3.client``'s ``boto3.client`` fallback) keeps stock botocore order on
    purpose - the same library=boto3 / CLI=aws split as the profile chain.
    """
    if explicit is not None:
        return explicit
    # Deferred like the SDK imports in the builders below (import contract,
    # docs/imports.md): the providers are only reached once a client is built.
    from botocore.configprovider import (
        ChainProvider,
        EnvironmentProvider,
        ScopedConfigProvider,
    )
    from botocore.utils import IMDSRegionProvider

    # botocore-stubs types ChainProvider's `providers` as Sequence[BaseProvider],
    # but IMDSRegionProvider (botocore.utils) is not declared a BaseProvider there
    # even though aws-cli composes it into this very chain; list[Any] bridges the
    # stub gap (all four are duck-typed providers exposing .provide()).
    providers: list[Any] = [
        EnvironmentProvider(name="AWS_REGION", env=os.environ),
        EnvironmentProvider(name="AWS_DEFAULT_REGION", env=os.environ),
        ScopedConfigProvider(config_var_name="region", session=session),
        IMDSRegionProvider(session),
    ]
    return ChainProvider(providers=providers).provide()


def build_service_client(
    service: str, args: argparse.Namespace, *, region: str | None = None
) -> Any:
    """Build a non-S3 service client for path validation (``mv``'s resolver).

    aws-cli's ``S3PathResolver.from_session``: a plain ``create_client`` carrying
    only the profile session, the caller's region choice (the source side
    passes ``--source-region``, the destination ``--region``, sts none - a
    ``None`` falls through to the session default, *not* to ``--region``,
    like aws-cli's dead-defaulted ``parameters.get('source_region', ...)``),
    and the ``--no-verify-ssl`` / ``--ca-bundle`` verify setting - no endpoint
    override, no timeout config. A ``None`` region resolves through the same
    :func:`_resolve_region` chain as :func:`build_client` (``AWS_REGION`` >
    ``AWS_DEFAULT_REGION`` > config > IMDS), keeping the session default
    aws v2-shaped.
    """
    # Deferred like build_client: only a command that actually resolves
    # access-point paths pays the boto3 import.
    import boto3
    import botocore.session
    from botocore.exceptions import BotoCoreError, NoCredentialsError, NoRegionError

    verify: bool | str | None
    if args.no_verify_ssl:
        verify = False
    else:
        verify = args.ca_bundle
    # Client construction can raise raw botocore errors (e.g. ProfileNotFound for
    # a bad --profile); translate them so they reach the exit-code mapping
    # instead of escaping as an uncaught traceback (see build_client).
    try:
        botocore_session = botocore.session.Session(profile=resolve_profile(args))
        session = boto3.Session(botocore_session=botocore_session)
        return session.client(
            service, region_name=_resolve_region(region, botocore_session), verify=verify
        )
    except (NoCredentialsError, NoRegionError) as exc:
        raise ConfigurationError(str(exc)) from exc
    except BotoCoreError as exc:
        raise Boto3S3Error(str(exc)) from exc


def _coerce_cli_timeout(value: str) -> int | None:
    """aws-cli's ``_resolve_timeout`` coercion: ``int(value)``, then ``0`` -> ``None``.

    aws applies this in a post-parse session handler, so a non-integer value
    raises a bare ``ValueError`` that reaches its general handler (rc 255), not
    the rc 252 of an argparse/usage error - the same path ``--page-size`` already
    takes. Translate that failure to ``Boto3S3Error`` (-> 255) for parity instead
    of declaring the argparse arg ``type=int``. The ``0`` -> ``None`` sentinel
    ("no timeout"; cli.json help) is preserved because ``int("0") or None`` is
    ``None``, and botocore (via urllib3) rejects a literal ``0`` anyway.
    """
    try:
        return int(value) or None
    except ValueError as exc:
        raise Boto3S3Error(str(exc)) from exc


def build_client(args: argparse.Namespace) -> S3Client:
    """Build the boto3 S3 client from the connection/auth globals (section 5).

    ``--profile`` selects the session (falling back to the ``AWS_PROFILE`` >
    ``AWS_DEFAULT_PROFILE`` env chain, aws-cli order - :func:`resolve_profile`);
    the region resolves through aws-cli's chain (``--region`` > ``AWS_REGION`` >
    ``AWS_DEFAULT_REGION`` > config > IMDS - :func:`_resolve_region`);
    ``--endpoint-url``, the timeouts, and ``--no-sign-request`` map to client
    kwargs / a botocore ``Config``; ``--no-verify-ssl`` and ``--ca-bundle`` map to
    ``verify``. The client is handed to the library through ``S3Storage`` - the
    library never rebuilds connection settings itself.
    """
    # Deferred: importing boto3 drags in botocore and s3transfer (~100ms), so
    # it happens only once a command actually needs a client; --help/--version
    # and usage errors never reach it (import contract, docs/imports.md).
    import boto3
    import botocore.session
    from botocore import UNSIGNED
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, NoCredentialsError, NoRegionError

    _pin_python_sigv4_signers()

    # aws-cli validates --endpoint-url at parse time: a value with no scheme is a
    # usage error (rc 252). Without this, botocore raises a bare ValueError at
    # client creation, which would escape as an uncaught traceback.
    endpoint_url: str | None = args.endpoint_url
    if endpoint_url is not None and not urlparse(endpoint_url).scheme:
        raise ValidationError(
            f'Bad value for --endpoint-url "{endpoint_url}": scheme is '
            "missing.  Must be of the form http://<hostname>/ or https://<hostname>/"
        )

    verify: bool | str | None
    if args.no_verify_ssl:
        verify = False
    else:
        verify = args.ca_bundle  # a path, or None to use the default trust store

    # aws-cli v2's bundled botocore has no SigV2 left at all, while stock
    # botocore still downgrades *presigned URLs* to SigV2 in regions that
    # accept it (a default us-east-1 client) and resolves us-east-1
    # to the legacy global endpoint where aws v2 uses the regional one. Pin
    # both so every command - visibly, presign's URLs - matches aws v2.
    overrides: dict[str, Any] = {
        "signature_version": "s3v4",
        "s3": {"us_east_1_regional_endpoint": "regional"},
    }
    if args.no_sign_request:
        overrides["signature_version"] = UNSIGNED
    # The timeouts arrive as raw strings (see add_common_arguments) and are
    # coerced here, aws-cli-style: int() with a 0 -> None ("no timeout") sentinel,
    # a bad value mapped to rc 255 rather than a parse-time rc 252.
    if args.cli_read_timeout is not None:
        overrides["read_timeout"] = _coerce_cli_timeout(args.cli_read_timeout)
    if args.cli_connect_timeout is not None:
        overrides["connect_timeout"] = _coerce_cli_timeout(args.cli_connect_timeout)
    config = Config(**overrides)

    # Client construction can raise raw botocore errors (e.g. ProfileNotFound for
    # a bad --profile, or credential/region resolution failures). Translate them
    # into the library taxonomy so exit_code_for maps them (credential/region ->
    # ConfigurationError [253], the rest -> 255) instead of letting a raw
    # botocore exception escape main() as an uncaught traceback (rc 1).
    try:
        botocore_session = botocore.session.Session(profile=resolve_profile(args))
        session = boto3.Session(botocore_session=botocore_session)
        return session.client(
            "s3",
            region_name=_resolve_region(args.region, botocore_session),
            endpoint_url=args.endpoint_url,
            verify=verify,
            config=config,
        )
    except (NoCredentialsError, NoRegionError) as exc:
        # aws has dedicated handlers for these two (-> 253); every other botocore
        # error (ProfileNotFound, PartialCredentialsError, ...) falls to its
        # GeneralExceptionHandler (-> 255) via the BotoCoreError clause below.
        raise ConfigurationError(str(exc)) from exc
    except BotoCoreError as exc:
        raise Boto3S3Error(str(exc)) from exc
