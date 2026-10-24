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
from boto3_s3_cli import paramfile, textencoding, usage
from boto3_s3_cli.clientfactory import build_client, build_s3, build_service_client

if TYPE_CHECKING:
    from collections.abc import Callable

    from boto3.s3.transfer import TransferConfig
    from mypy_boto3_s3 import S3Client

    from boto3_s3 import S3

    ClientFactory = Callable[[argparse.Namespace], S3Client]
    S3Factory = Callable[[argparse.Namespace], S3]
    # (service, args, *, region=None) -> a boto3 client for that service;
    # mv's --validate-same-s3-paths builds its s3control/sts clients here.
    ServiceClientFactory = Callable[..., Any]


class Context:
    """Runtime dependencies ``main()`` hands to the dispatched ``Command``.

    ``client_factory`` builds the boto3 S3 client from the parsed
    connection/auth globals in place of the CLI's own construction; ``None``
    (the default) leaves it to the command's `S3`, which builds it on its
    session (``boto3_s3_cli.clientfactory.build_s3`` / ``build_client``).
    Tests inject a factory returning a fake client.
    ``service_client_factory`` does the same for the non-S3 clients ``mv``'s
    path validation needs (``None``: ``clientfactory.build_service_client``
    on the command's session). ``transfer_config``
    overrides the transfer engine's defaults (aws-cli-equivalent 8 MiB
    threshold/chunk); tests inject ``TransferConfig(use_threads=False, ...)``
    to make multipart call order deterministic against canned clients.
    """

    def __init__(
        self,
        *,
        client_factory: ClientFactory | None = None,
        s3_factory: S3Factory | None = None,
        service_client_factory: ServiceClientFactory | None = None,
        transfer_config: TransferConfig | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._s3_factory = s3_factory
        self._service_client_factory = service_client_factory
        self.transfer_config: TransferConfig | None = transfer_config

    def s3(self, args: argparse.Namespace) -> S3:
        """Build the command's `S3`, adapting an injected client factory if present."""
        if self._s3_factory is not None:
            return self._s3_factory(args)

        from boto3_s3 import S3

        client_factory = self._client_factory
        if client_factory is None:
            return build_s3(args)

        class InjectedClientS3(S3):
            def client(self) -> S3Client:
                return client_factory(args)

        # reusable_after_interrupt=False like build_s3: the injected-client seam must
        # keep the CLI's process-fatal Ctrl-C posture, and its local names.
        return InjectedClientS3(
            reusable_after_interrupt=False, filename_encoding=textencoding.filename_encoding()
        )

    def with_s3(self, s3: S3) -> Context:
        """A view of this context whose ``s3`` always returns *s3*.

        ``rb --force``'s session-sharing seam: aws-cli hands its one CLI session
        to the inner rm (``RmCommand(self._session)``), so the inner run must
        not build a second session - a ``credential_process`` helper would run
        twice, an MFA / assume-role flow would prompt twice, and the object
        deletes could then run under a different identity than the final
        ``DeleteBucket``. Every other dependency (factories, transfer config)
        carries over unchanged.
        """
        clone = Context(
            client_factory=self._client_factory,
            s3_factory=lambda _args: s3,
            service_client_factory=self._service_client_factory,
            transfer_config=self.transfer_config,
        )
        return clone

    def client(self, args: argparse.Namespace, s3: S3) -> S3Client:
        """Build an additional S3 client from the command's bound session
        (an injected test factory builds from its own wiring instead)."""
        if self._client_factory is not None:
            return self._client_factory(args)
        return build_client(args, session=s3.session)

    def service_client(
        self,
        service: str,
        args: argparse.Namespace,
        s3: S3,
        *,
        region: str | None = None,
    ) -> Any:
        """Build a non-S3 client from the command's bound session
        (an injected test factory builds from its own wiring instead)."""
        if self._service_client_factory is not None:
            return self._service_client_factory(service, args, region=region)
        return build_service_client(service, args, region=region, session=s3.session)


def parse_integer_option(value: object, *, operation: str) -> int | None:
    """Convert an integer option value the way aws-cli does: rc 255 on failure.

    aws-cli converts ``cli_type_name: integer`` options with a bare ``int()``
    whose ``ValueError`` escapes to its *general* exception handler - so
    ``--page-size abc`` exits 255, not 252. argparse's
    ``type=int`` would turn the same mistake into a usage error (252), so
    integer options are declared without it and converted here at ``run()``
    start, before the client factory builds anything (aws-cli fails at parse
    time, before its client exists). ``None`` (the option not given) passes
    through unconverted, like aws-cli's unset-integer handling.
    """
    if value is None:
        return None
    if isinstance(value, int):  # an argparse integer default, already converted
        return value
    # argparse hands us a str, a fileb:// paramfile hands us bytes; both go
    # straight into int() the way aws-cli does. int() coerces str and bytes alike.
    text = value if isinstance(value, (str, bytes)) else str(value)
    try:
        return int(text)
    except ValueError as exc:
        # InvalidValueError: main() maps it to 255 (aws-cli's general handler),
        # not ValidationError's 252.
        raise InvalidValueError(str(exc), operation=operation) from exc


def _expand_string_paramfile(
    args: argparse.Namespace, dest: str, *, name: str, operation: str
) -> None:
    """Load a paramfile reference on a string-typed ``args.<dest>`` in place.

    Shared body of the string option and positional helpers below. aws-cli expands
    references at parse time, so a missing ``file://`` reference is the load
    252; a ``file://`` yields text, and a ``fileb://`` is refused unread
    (`paramfile.load_text_argument`). A value without a prefix (or a
    non-string, e.g. an integer default) is untouched. *name* is the argument
    name aws-cli reports in the load failure.
    """
    value = getattr(args, dest, None)
    if isinstance(value, str):
        setattr(args, dest, paramfile.load_text_argument(value, name, operation=operation))


def expand_option_paramfile(args: argparse.Namespace, option: str, *, operation: str) -> None:
    """aws-cli's parse-time paramfile expansion for a string-typed plain option (252).

    aws-cli expands paramfile references on every plain option during argument
    parsing, so a bad reference is its ParamValidation 252. Runs in place; a
    value without a prefix (or a non-string) is untouched. For an integer
    option use ``resolve_integer_option``, whose loaded bytes feed
    ``int()`` rather than being rejected as a string.
    """
    _expand_string_paramfile(
        args, option, name=f"--{option.replace('_', '-')}", operation=operation
    )


def resolve_integer_option(args: argparse.Namespace, option: str, *, operation: str) -> int | None:
    """aws-cli's ``integer`` option: its paramfile expanded, then the bare ``int()``.

    aws-cli loads the paramfile at parse time, before the coercion, so a missing
    reference is the load 252 *before* the coercion's 255 (measured:
    ``--page-size file:///no/x`` exits 252). Unlike the string helper it keeps
    ``fileb://`` bytes rather than rejecting them: aws-cli feeds them to ``int()``
    too (``--page-size fileb://<5>`` succeeds, ``<abc>`` fails 255 with a
    ``b'abc'`` repr). A value without a prefix falls through into ``int()``;
    ``parse_integer_option`` performs the coercion. The loaded value replaces
    ``args.<option>`` in place, as the string helpers do.
    """
    value = getattr(args, option, None)
    if isinstance(value, str):
        name = f"--{option.replace('_', '-')}"
        setattr(args, option, paramfile.load_argument(value, name, operation=operation))
    return parse_integer_option(getattr(args, option), operation=operation)


def expand_positional_paramfile(
    args: argparse.Namespace, dest: str, *, name: str, operation: str
) -> None:
    """aws-cli's parse-time paramfile expansion for a path positional, ``file://`` only.

    A ``file://`` reference loads the path as text (a missing or undecodable
    file is the load 252). A ``fileb://`` one is refused at 252 without being
    read (`paramfile.load_text_argument`): aws-cli loads the bytes into the path, and
    each command then mishandles them its own way - a crash on ls and
    website, a delete of the decoded path on rm - an aws-cli bug this CLI does
    not copy (docs/cli/aws-differences.md section 2). *name* is the argument
    name the report carries.
    """
    _expand_string_paramfile(args, dest, name=name, operation=operation)


def refuse_bucketless(bucket: str, *, operation: str | None = None) -> None:
    """Refuse an S3 path that names no bucket (252).

    aws-cli has no such check: it sends ``Bucket=""`` toward the API, and each
    command fails wherever that first bites - a fatal error (rc 1) at a
    listing or a single object's lookup, every upload or copy failing on its
    own, a ``--dryrun`` upload or single ``rm`` reporting success at rc 0, an
    ``mb`` / ``rb`` failure line at 1, ``rb --force`` at 255. A path with no
    bucket names nothing to transfer to or from, so here it is a
    command-line error before anything runs, on every command but ``ls``,
    where ``s3://`` lists the buckets.
    """
    if not bucket:
        raise ValidationError(usage.invalid_bucket_name_message(), operation=operation)


def add_page_size_argument(parser: argparse.ArgumentParser) -> None:
    """Register ``--page-size`` (shared by ls / rm and the transfer family).

    Not range-validated: aws-cli passes any int through and lets the server
    decide, and the run's outcome follows from that answer
    (0 lists nothing - ls's empty listing exits 1, rm and the transfers 0;
    a negative value is the server's
    InvalidArgument -> rc 254 from ls, but rc 1 from rm and the transfer
    family, whose post-start errors are uniformly 1 - design/cli.md sections
    5.2 / 6). No ``type=int``: a non-integer must exit 255 like aws-cli's bare
    ``int()`` conversion, not argparse's 252 (``parse_integer_option``
    converts at ``run()`` start). The default is ``None`` like aws-cli's,
    which sends **no** ``MaxKeys`` / ``MaxBuckets`` on the wire (the server
    pages at its own default) - an explicit 1000 here would be a wire
    divergence.
    """
    parser.add_argument(
        "--page-size", default=None, help="how many keys to request per listing page"
    )


def add_request_payer_argument(parser: argparse.ArgumentParser) -> None:
    """Register ``--request-payer`` (optional value; ``requester`` is the only one)."""
    parser.add_argument(
        "--request-payer",
        nargs="?",
        const="requester",
        choices=["requester"],
        default=None,
        help="confirm you accept the request and data transfer charges",
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
