"""Build the command's `S3` from the parsed connection / auth globals.

The execution half of the global-option surface ``globalargs`` registers:
`build_s3` hands the connection / auth values to the library's
`S3.from_aws_cli`, which assembles the session and its clients the way aws-cli
assembles its own (``boto3_s3.awsclisession``), and adds what only the command
does - to its process, which it owns (the HTTPS-proxy CONNECT request, the SSO
token cache), to its clients (aws-cli's S3 error-message rewriter) and to its
configuration (the ``cli_*`` format checks). Everything here reaches the AWS
SDK.
"""

from __future__ import annotations

import argparse
from functools import cache
from typing import TYPE_CHECKING, Any, cast

# These exception names do not themselves import the AWS SDK.
from boto3_s3 import ConfigurationError, InvalidConfigError, InvalidValueError
from boto3_s3_cli import s3errormsg, textencoding

if TYPE_CHECKING:
    from botocore.session import Session as BotocoreSession

    from boto3_s3 import S3


def from_aws_cli_arguments(args: argparse.Namespace) -> dict[str, Any]:
    """The parsed connection / auth globals as `S3.from_aws_cli`'s keywords.

    The timeouts arrive as raw strings (see globalargs.add_common_arguments)
    and are coerced aws-cli-style here (`_coerce_cli_timeout`), which is what
    stops a run on a bad value (255) before anything is sent; ``--ca-bundle``
    is the typed path in the form the OS takes (`textencoding.os_path`).
    """
    return {
        "profile": args.profile,
        "region": args.region,
        "endpoint_url": args.endpoint_url,
        "no_verify_ssl": args.no_verify_ssl,
        "ca_bundle": (
            textencoding.os_path(cast("str", args.ca_bundle))
            if args.ca_bundle is not None
            else None
        ),
        "no_sign_request": args.no_sign_request,
        "cli_read_timeout": (
            _coerce_cli_timeout(args.cli_read_timeout)
            if args.cli_read_timeout is not None
            else None
        ),
        "cli_connect_timeout": (
            _coerce_cli_timeout(args.cli_connect_timeout)
            if args.cli_connect_timeout is not None
            else None
        ),
    }


def _coerce_cli_timeout(value: str) -> int:
    """aws-cli's ``_resolve_timeout`` coercion of a raw timeout string: ``int(value)``.

    aws-cli applies this in a post-parse session handler, so a non-integer value
    raises a bare ``ValueError`` that reaches its general handler (rc 255), not
    the rc 252 of an argparse/usage error - the same path ``--page-size`` already
    takes. Translate that failure to ``InvalidValueError`` (-> 255) for parity
    instead of declaring the argparse arg ``type=int``. ``0`` stays ``0``: the
    library reads it as "no timeout" (aws-cli's ``int(value) or None``, cli.json
    help), botocore (via urllib3) rejecting a literal ``0``.
    """
    try:
        return int(value)
    except ValueError as exc:
        raise InvalidValueError(str(exc)) from exc


def prepare_process() -> None:
    """What the command does to its process before it builds a session, as aws-cli does.

    Both are process-wide, which the command, owning its process, may change
    where the library may not on its own (design/overview.md section 3, rule
    1). The HTTPS-proxy CONNECT request becomes the one aws-cli's bundled
    Python sends (`boto3_s3.pin_proxy_connect`). And the SSO token cache,
    which botocore builds from a class attribute when a credential chain is
    built, gets aws-cli's write (`awsclisession.credential_cache_class`): the
    installed botocore writes ``~/.aws/sso/cache`` entries through a temporary
    file, so a cache aws-cli can write (an unwritable directory over a writable
    entry) failed, one aws-cli cannot (a read-only entry) succeeded, and the
    entry's mode changed (measured).
    """
    from botocore.tokens import SSOTokenProvider

    from boto3_s3 import awsclisession, pin_proxy_connect

    pin_proxy_connect()
    SSOTokenProvider.DEFAULT_CACHE_CLS = awsclisession.credential_cache_class()


def build_s3(args: argparse.Namespace) -> S3:
    """Build the command's `S3`: `S3.from_aws_cli` on the parsed globals.

    The library opens the session the way aws-cli opens its own and walks the
    region chain once (its last link, the EC2 IMDS probe, costs seconds on a
    host with no region configured and no metadata service answering); every
    client the `S3` hands out comes from that session (design/cli.md section
    4). ``reusable_after_interrupt=False``: Ctrl-C is process-fatal in the
    CLI, so an operation's unwind must not wait for an in-flight listing page
    pull (aws-cli dies immediately); the library default keeps waiting. Its S3
    clients carry aws-cli's error-message rewriter (`s3errormsg`), and the
    profile's ``cli_*`` formats are checked as aws-cli checks them when its
    session starts (`_reject_unknown_cli_formats`).
    """
    prepare_process()
    s3 = _cli_s3_class().from_aws_cli(
        **from_aws_cli_arguments(args), reusable_after_interrupt=False
    )
    assert s3.session is not None  # from_aws_cli opened it
    _reject_unknown_cli_formats(s3.session._session, args)  # pyright: ignore[reportPrivateUsage]
    return s3


@cache
def _cli_s3_class() -> type[S3]:
    """The command's `S3`: the library's, its S3 clients carrying `s3errormsg`.

    aws-cli registers its S3 error-message rewriter on its session, so it rides
    every S3 client - the run's and the source client of ``--source-region``.
    Built on first use: ``boto3_s3.S3`` imports the SDK.
    """
    from boto3_s3 import S3

    class CliS3(S3):
        def client(self) -> Any:
            client = super().client()
            s3errormsg.register(client)
            return client

        def source_client(self, region: str) -> Any:
            client = super().source_client(region)
            s3errormsg.register(client)
            return client

    return CliS3


# The values aws-cli's `cli_timestamp_format` and `cli_binary_format`
# customizations accept; the comparison is exact and case-sensitive.
_TIMESTAMP_FORMATS = ("wire", "iso8601")
_BINARY_FORMATS = ("base64", "raw-in-base64-out")


class UnknownTimestampFormatError(ConfigurationError):
    """An unknown ``cli_timestamp_format`` in the selected profile (rc 253).

    aws-cli raises its own ``ConfigurationError`` for it, the one failure on this
    surface its ``ConfigurationErrorHandler`` reports, so the report carries
    that handler's ``Configuration`` code (`cli._configuration_report`).
    """


def _reject_unknown_cli_formats(session: BotocoreSession, args: argparse.Namespace) -> None:
    """Stop on a ``cli_timestamp_format`` / ``cli_binary_format`` aws-cli refuses.

    aws-cli checks both settings in the selected profile when its session starts,
    so a value it does not accept stops every command: rc 253 for the
    timestamp format, rc 255 for the binary format (design/cli.md section 6).
    This CLI uses neither value for anything else - its listings print the
    same line under every timestamp format, and no option here takes binary
    data - so the check is all that is left of them. An empty value is a
    value and is refused. An explicit ``--cli-binary-format`` replaces the
    config value, and argparse has already held it to the two choices.
    """
    scoped = session.get_scoped_config()
    timestamp_format = scoped.get("cli_timestamp_format")
    if timestamp_format is not None and timestamp_format not in _TIMESTAMP_FORMATS:
        raise UnknownTimestampFormatError(
            f"Unknown cli_timestamp_format value: {timestamp_format}, "
            'valid values are "wire" or "iso8601"'
        )
    binary_format = scoped.get("cli_binary_format")
    if (
        args.cli_binary_format is None
        and binary_format is not None
        and binary_format not in _BINARY_FORMATS
    ):
        raise InvalidConfigError(
            f"Unknown cli_binary_format value: {binary_format}, "
            'valid values are "base64" or "raw-in-base64-out"'
        )
