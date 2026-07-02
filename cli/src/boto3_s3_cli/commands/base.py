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

# Pure-Python name (exceptions module) - safe on the parse path (import
# contract, docs/imports.md).
from boto3_s3 import InvalidValueError
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
    """Runtime dependencies ``main()`` hands to the dispatched :class:`Command`.

    ``client_factory`` builds the boto3 S3 client from the parsed
    connection/auth globals (default: :func:`boto3_s3_cli.clientfactory.build_client`).
    Tests inject a factory returning a fake client.
    ``service_client_factory`` does the same for the non-S3 clients ``mv``'s
    path validation needs (default:
    :func:`boto3_s3_cli.clientfactory.build_service_client`). ``transfer_config``
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
    try:
        return int(str(value))
    except ValueError as exc:
        # InvalidValueError: main() maps it to 255 (aws's general handler),
        # not ValidationError's 252, and str(exc) mirrors aws's message.
        raise InvalidValueError(str(exc), operation=operation) from exc


def expand_option_paramfile(args: argparse.Namespace, option: str, *, operation: str) -> None:
    """aws's parse-time ``file://`` expansion for a plain option value (252).

    aws expands paramfile references on every plain option during argument
    parsing - even the string-typed integer options - so a bad reference is
    its ParamValidation 252 *before* the bare ``int()`` coercion's 255
    (measured: ``--page-size file:///no/x`` exits 252). Runs in place; a
    value without the prefix (or a non-string, e.g. an integer default) is
    untouched.
    """
    value = getattr(args, option, None)
    if isinstance(value, str) and value.startswith("file://"):
        loaded = paramfile.read_text_paramfile(
            value, name=f"--{option.replace('_', '-')}", operation=operation
        )
        setattr(args, option, loaded)


def add_page_size_argument(parser: argparse.ArgumentParser) -> None:
    """Register ``--page-size`` (shared by ls / rm and the transfer family).

    Not range-validated: aws-cli passes any int through and lets the server
    decide (0 lists nothing -> rc 1; negative -> InvalidArgument -> rc 254),
    and the exit-code charter requires matching both. No ``type=int``: a
    non-integer must exit 255 like aws's bare ``int()`` conversion, not
    argparse's 252 (:func:`parse_integer_option` converts at ``run()`` start).
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
