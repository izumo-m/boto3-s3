"""The subcommand interface (``Command``) and its runtime dependencies (``Context``).

``Command`` formalizes the contract ``cli.py`` dispatches through - argument
registration plus execution - so wiring a new subcommand is one subclass plus
one entry in ``cli._COMMAND_TABLE``. ``Context`` is the injection point for the
dependencies ``main()`` resolves at runtime; tests hand it fakes instead of
monkeypatching module attributes.
"""

from __future__ import annotations

import argparse
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

# These exception names do not themselves import the AWS SDK.
from boto3_s3 import InvalidValueError, ValidationError
from boto3_s3_cli import paramfile
from boto3_s3_cli.clientfactory import build_client, build_service_client

if TYPE_CHECKING:
    from collections.abc import Callable

    from boto3.s3.transfer import TransferConfig
    from mypy_boto3_s3 import S3Client

    from boto3_s3_cli.autoprompt.prompter import AutoPrompter

    ClientFactory = Callable[[argparse.Namespace], S3Client]
    # (service, args, *, region=None) -> a boto3 client for that service;
    # mv's --validate-same-s3-paths builds its s3control/sts clients here.
    ServiceClientFactory = Callable[..., Any]


class Context:
    """Runtime dependencies ``main()`` hands to the dispatched ``Command``.

    ``client_factory`` builds the boto3 S3 client from the parsed
    connection/auth globals (default: ``boto3_s3_cli.clientfactory.build_client``).
    Tests inject a factory returning a fake client.
    ``service_client_factory`` does the same for the non-S3 clients ``mv``'s
    path validation needs (default:
    ``boto3_s3_cli.clientfactory.build_service_client``). ``transfer_config``
    overrides the transfer engine's defaults (aws-equivalent 8 MiB
    threshold/chunk); tests inject ``TransferConfig(use_threads=False, ...)``
    to make multipart call order deterministic against canned clients.
    ``auto_prompter`` overrides the ``--cli-auto-prompt`` backend (default: the
    real ``prompt_toolkit`` prompter, built lazily by ``main``); tests inject a
    fake returning a canned argv to exercise the re-dispatch without a terminal.
    """

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        service_client_factory: ServiceClientFactory | None = None,
        transfer_config: TransferConfig | None = None,
        auto_prompter: AutoPrompter | None = None,
    ) -> None:
        self.client_factory: ClientFactory = (
            build_client if client_factory is None else client_factory
        )
        self.service_client_factory: ServiceClientFactory = (
            build_service_client if service_client_factory is None else service_client_factory
        )
        self.transfer_config: TransferConfig | None = transfer_config
        self.auto_prompter: AutoPrompter | None = auto_prompter


def parse_integer_option(value: object, *, operation: str) -> int:
    """Convert an integer option value the way aws-cli does: rc 255 on failure.

    aws-cli converts ``cli_type_name: integer`` options with a bare ``int()``
    whose ``ValueError`` escapes to its *general* exception handler - so
    ``--page-size abc`` exits 255, not 252. argparse's
    ``type=int`` would turn the same mistake into a usage error (252), so
    integer options are declared without it and converted here at ``run()``
    start, before the client factory builds anything (aws fails at parse
    time, before its client exists).
    """
    if isinstance(value, int):  # the argparse default, already converted
        return value
    # argparse hands us a str, a fileb:// paramfile hands us bytes; both go
    # straight into int() the way aws does. int() coerces str and bytes alike,
    # and its ValueError repr differs (``'abc'`` vs ``b'abc'``), so the failure
    # message matches aws's for a file:// vs a fileb:// source.
    text = value if isinstance(value, (str, bytes)) else str(value)
    try:
        return int(text)
    except ValueError as exc:
        # InvalidValueError: main() maps it to 255 (aws's general handler),
        # not ValidationError's 252, and str(exc) mirrors aws's message.
        raise InvalidValueError(str(exc), operation=operation) from exc


def _expand_string_paramfile(
    args: argparse.Namespace, dest: str, *, name: str, operation: str
) -> None:
    """Load a paramfile reference on a string-typed ``args.<dest>`` in place.

    Shared body of the string option and positional helpers below. aws expands
    both prefixes at parse time, so a missing reference is the load 252; a
    ``file://`` yields text, while a ``fileb://`` yields bytes that botocore
    then rejects for a string parameter with its own 252 (measured against aws
    2.35.18: ``value: b'...', valid types: <class 'str'>``). A value without a
    prefix (or a non-string, e.g. an integer default) is untouched. *name* is
    the argument name aws reports in the load failure.
    """
    value = getattr(args, dest, None)
    if not isinstance(value, str):
        return
    loaded = paramfile.get_paramfile(value, name=name, operation=operation)
    if loaded is None:
        return
    if isinstance(loaded, bytes):
        raise ValidationError(
            "Parameter validation failed:\n"
            f"Invalid type for parameter input, value: {loaded!r}, "
            "type: <class 'bytes'>, valid types: <class 'str'>",
            operation=operation,
        )
    setattr(args, dest, loaded)


def expand_option_paramfile(args: argparse.Namespace, option: str, *, operation: str) -> None:
    """aws's parse-time paramfile expansion for a string-typed plain option (252).

    aws expands paramfile references on every plain option during argument
    parsing, so a bad reference is its ParamValidation 252. Runs in place; a
    value without a prefix (or a non-string) is untouched. For an integer
    option use ``expand_integer_paramfile``, whose loaded bytes feed
    ``int()`` rather than being rejected as a string.
    """
    _expand_string_paramfile(
        args, option, name=f"--{option.replace('_', '-')}", operation=operation
    )


def expand_integer_paramfile(args: argparse.Namespace, option: str, *, operation: str) -> None:
    """aws's parse-time paramfile expansion for a string-typed integer option (252).

    aws loads the paramfile before the bare ``int()`` coercion, so a missing
    reference is the load 252 *before* the coercion's 255 (measured:
    ``--page-size file:///no/x`` exits 252). Unlike the string helper it keeps
    ``fileb://`` bytes rather than rejecting them: aws feeds them to ``int()``
    too (``--page-size fileb://<5>`` succeeds, ``<abc>`` fails 255 with a
    ``b'abc'`` repr). ``parse_integer_option`` performs the coercion.
    """
    value = getattr(args, option, None)
    if isinstance(value, str):
        name = f"--{option.replace('_', '-')}"
        loaded = paramfile.get_paramfile(value, name=name, operation=operation)
        if loaded is not None:
            setattr(args, option, loaded)


def expand_positional_paramfile(
    args: argparse.Namespace, dest: str, *, name: str, operation: str
) -> None:
    """Apply aws's command-specific positional paramfile expansion in place.

    Missing files remain the loader's rc 252 for every command. Readable
    `fileb://` values are deliberately less uniform: aws-cli leaves the
    bytes in the parsed positional, after which mb / rb / presign reject them
    as string parameters (252), rm decodes them back to a path, and ls /
    website crash in their own path handling (255). The callers retain those
    downstream quirks so the exit codes stay compatible.
    """
    value = getattr(args, dest, None)
    if not isinstance(value, str):
        return
    loaded = paramfile.get_paramfile(value, name=name, operation=operation)
    if loaded is None:
        return
    if isinstance(loaded, bytes) and operation in {"mb", "rb", "presign"}:
        # Intentional aws-cli bug parity: these three commands happen to route
        # positional bytes through string-parameter validation, while rm, ls,
        # and website mishandle the same bytes differently.
        raise ValidationError(
            "Parameter validation failed:\n"
            f"Invalid type for parameter input, value: {loaded!r}, "
            "type: <class 'bytes'>, valid types: <class 'str'>",
            operation=operation,
        )
    setattr(args, dest, loaded)


def add_page_size_argument(parser: argparse.ArgumentParser) -> None:
    """Register ``--page-size`` (shared by ls / rm and the transfer family).

    Not range-validated: aws-cli passes any int through and lets the server
    decide, and the exit-code charter requires matching the resulting codes
    (0 lists nothing -> rc 1; a negative value is the server's
    InvalidArgument -> rc 254 from ls, but rc 1 from rm and the transfer
    family, whose post-start errors are uniformly 1 - docs/cli.md sections
    5.2 / 6). No ``type=int``: a non-integer must exit 255 like aws's bare
    ``int()`` conversion, not argparse's 252 (``parse_integer_option``
    converts at ``run()`` start).
    """
    parser.add_argument("--page-size", default=1000)


def add_request_payer_argument(parser: argparse.ArgumentParser) -> None:
    """Register ``--request-payer`` (optional value; ``requester`` is the only one)."""
    parser.add_argument(
        "--request-payer", nargs="?", const="requester", choices=["requester"], default=None
    )


class Command(ABC):
    """One ``aws s3`` subcommand: argument registration plus execution.

    ``cli.py`` creates a fresh instance per parser build and per dispatch, so
    ``run()`` may keep per-run state (counters, progress) in instance
    attributes without it leaking across ``main()`` calls in one process.
    """

    name: ClassVar[str]
    help: ClassVar[str]

    @abstractmethod
    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Register the subcommand-specific arguments on its subparser."""

    @abstractmethod
    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Execute against the parsed *args* and return the process exit code."""
