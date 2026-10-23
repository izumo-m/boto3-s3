"""Entry point and dispatch for the ``boto3-s3`` (``aws s3``-compatible) CLI."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import os
import re
import sys
from collections.abc import Iterable
from difflib import get_close_matches
from typing import NoReturn, TextIO, cast

from boto3_s3 import (
    Boto3S3Error,
    ConfigurationError,
    InvalidConfigError,
    InvalidValueError,
    ValidationError,
)
from boto3_s3_cli import globalargs, textencoding
from boto3_s3_cli.commands.base import Command, Context

# Loggers a masked stderr handler is attached to under --debug, via the
# library's boto3-faithful set_stream_logger (credential masking on by default -
# design/masking.md). The library attaches no handler on import. "boto3_s3_cli"
# is the counterpart of aws-cli's own "awscli" logger (clidriver._set_logging),
# so the CLI's own debug lines surface too.
# urllib3 is deliberately omitted: it logs no credentials, only
# connection-pool noise.
_DEBUG_LOGGERS = ("boto3_s3", "boto3_s3_cli", "botocore", "boto3", "s3transfer")

# aws-cli v2 exit-code conventions (awscli/constants.py). Each code keeps the
# meaning aws-cli gives it (design/overview.md section 3); see design/cli.md
# section 6 for the full table.
_PARAM_VALIDATION_ERROR_RC = 252
_CONFIGURATION_ERROR_RC = 253
_CLIENT_ERROR_RC = 254
_GENERAL_ERROR_RC = 255

# Every wired subcommand: name -> (defining module, class name, one-line help).
# Registering here is the only wiring step. The table is the single source for
# stage 1 of the dispatch (names + help lines, rendered WITHOUT importing any
# command module - the lazy-dispatch contract, design/imports.md) and for stage 2
# (only the matched module is imported; rb also pulls in rm, its --force
# engine). The help text is duplicated from each
# class's `help` ClassVar on purpose - stage 1 must render the top-level help
# page without the class - and test_command_table.py pins the two against drift.
_COMMAND_TABLE: dict[str, tuple[str, str, str]] = {
    "cp": (
        "boto3_s3_cli.commands.cp",
        "CpCommand",
        "Copy a local file or S3 object to another location locally or in S3.",
    ),
    "ls": (
        "boto3_s3_cli.commands.ls",
        "LsCommand",
        "List S3 objects and common prefixes under a prefix or all S3 buckets.",
    ),
    "mb": ("boto3_s3_cli.commands.mb", "MbCommand", "Create an S3 bucket."),
    "mv": (
        "boto3_s3_cli.commands.mv",
        "MvCommand",
        "Move a local file or S3 object to another location locally or in S3.",
    ),
    "presign": (
        "boto3_s3_cli.commands.presign",
        "PresignCommand",
        "Generate a pre-signed URL for an Amazon S3 object.",
    ),
    "rb": (
        "boto3_s3_cli.commands.rb",
        "RbCommand",
        "Delete an empty S3 bucket (--force deletes its objects first).",
    ),
    "rm": (
        "boto3_s3_cli.commands.rm",
        "RmCommand",
        "Delete an S3 object, or objects under a prefix (--recursive).",
    ),
    "sync": ("boto3_s3_cli.commands.sync", "SyncCommand", "Syncs directories and S3 prefixes."),
    "website": (
        "boto3_s3_cli.commands.website",
        "WebsiteCommand",
        "Set the website configuration for a bucket.",
    ),
}

# Python 3.14's argparse negative-number matcher, verbatim, applied - as
# argparse applies it - as a prefix match: a dash-led token whose first
# character(s) look like the start of a negative number is classified as a
# positional (none of our parsers register numeric option strings), so it
# reaches an option as a value and the subcommand scan must not skip it as an
# option. Up to 3.13 the pattern was anchored (`^-\d+$|^-\d*\.\d+$`), which
# classifies `-1x` as option-like; aws's official distribution bundles 3.14, so
# pinning 3.14's form here keeps the classification off the host Python
# version. `_ParamValidationArgumentParser` installs it for argparse itself and
# `_find_command_token` mirrors it; the two must stay the same pattern.
_NEGATIVE_NUMBER_RE = re.compile(r"-\.?\d")

# aws-cli's top-level usage block (its argparser.py USAGE + HELP_BLURB),
# collapsed onto our flatter hierarchy: boto3-s3 IS `aws s3`, so what aws calls
# the subcommand is our only level - aws's `<command> <subcommand>` metavar
# pair collapses to `<subcommand>`, and its *first* help line (`aws help`, the
# level above us) has no counterpart here, while `aws <command> help` is our
# `boto3-s3 help` and `aws <command> <subcommand> help` our
# `boto3-s3 <subcommand> help`. Every parse error closes with it, a
# subcommand's own parse included, exactly as aws hands one shared USAGE
# constant to its main, service and leaf parsers alike.
_TOP_LEVEL_USAGE = (
    "boto3-s3 [options] <subcommand> [parameters]\n"
    "To see help text, you can run:\n"
    "\n"
    "  boto3-s3 help\n"
    "  boto3-s3 <subcommand> help\n"
)

# aws's missing-subcommand report (its command layer's usage error): the usage
# line alone - no help blurb - plus a second line that carries its own
# `[ERROR]` prefix inside the message, which is why the prefix appears twice.
_TOO_FEW_ARGUMENTS = (
    "usage: boto3-s3 [options] <subcommand> [parameters]\nboto3-s3: [ERROR]: too few arguments"
)

# The usage of aws's first-pass parser, which reads `--profile` and `--debug`
# ahead of everything else, so a failure on either closes with it instead of
# the top-level block (`_GlobalsArgumentParser`).
_FIRST_PASS_USAGE = "usage: boto3-s3 [--profile PROFILE] [--debug]\n"

# aws's codec override for its error reports: an unknown codec named there
# stops the run (`_dispatch`), a known one re-encodes the reports
# (`textencoding.prepare_error_report`).
_OUTPUT_ENCODING_ENV_VAR = textencoding.OUTPUT_ENCODING_ENV_VAR


def _configuration_report(exc: BaseException) -> tuple[str, str] | None:
    """aws's envelope code and message for an rc-253 failure aws reports.

    aws gives each rc-253 failure it can meet on this surface a handler of
    its own (errorhandler.py), and each names a fixed code - the handler's,
    not the exception's class name. Two take botocore's exceptions and append
    a hint to botocore's text: `NoCredentials` after a `. ` separator, the
    text ending without a period, and `NoRegion` after a bare space, the text
    ending with one. The hints name aws's own `aws login` and `aws configure`
    as aws writes them: both tools read the same config files, so the advice
    holds (docs/cli/aws-differences.md). The third, `Configuration`, takes
    the `ConfigurationError` aws raises for an unknown
    `cli_timestamp_format`, whose counterpart here is
    `clientfactory.UnknownTimestampFormatError`; its message is aws's as it
    stands. `Pager` belongs to the output pager, which this CLI does not
    implement.

    The botocore pair is identified by the exception the library or the client
    builders keep as `__cause__` (`sdkerrors.s3_errors`, `clientfactory`), or
    by the exception itself where one escaped untranslated, never by matching
    the message text. Returns `None` for every other rc-253 failure: those
    are this CLI's own (an absent awscrt, an SDK floor shortfall), aws cannot
    reach them, and they stay bare.
    """
    # Imported here, on a path where the session has already loaded the SDK,
    # so the informational exits stay SDK-free (design/imports.md).
    from botocore.exceptions import NoCredentialsError, NoRegionError

    from boto3_s3_cli.clientfactory import UnknownTimestampFormatError

    if isinstance(exc, UnknownTimestampFormatError):
        return "Configuration", str(exc)
    cause = exc.__cause__ if isinstance(exc, Boto3S3Error) else exc
    if isinstance(cause, NoCredentialsError):
        return "NoCredentials", f'{cause}. You can configure credentials by running "aws login".'
    if isinstance(cause, NoRegionError):
        return "NoRegion", f'{cause} You can also configure your region by running "aws configure".'
    return None


def _write_error(message: object, *, rc: int | None = None) -> None:
    """Write one CLI error report to stderr, with the envelope *rc* calls for.

    aws renders the reports of its rc-252 and rc-253 handlers in its default
    enhanced form, `An error occurred (<code>): <message>`: `ParamValidation`
    for every usage error - argparse failures, unknown options and a plain
    `ValidationError` alike - and the three codes `_configuration_report`
    supplies at 253. Every other report is the message alone: an rc-254
    `ClientError` already carries `An error occurred (<Code>) when calling
    ...` in its own text, aws's general rc-255 handler formats nothing, and
    the rc-253 failures aws cannot reach have no code to name. The form does
    not follow `--cli-error-format` (design/aws-cli-option-handling.md section
    2.1).

    The envelope goes on before the strip, as aws's formatter strips the
    whole enveloped message; stripping keeps a message that ends with a
    newline - the usage block of a parse error - from surfacing as a
    trailing blank line. The report is written in the encoding aws writes
    its reports in (`textencoding.prepare_error_report`).
    """
    text = str(message)
    if rc == _PARAM_VALIDATION_ERROR_RC:
        text = f"An error occurred (ParamValidation): {text}"
    elif rc == _CONFIGURATION_ERROR_RC and isinstance(message, BaseException):
        report = _configuration_report(message)
        if report is not None:
            text = f"An error occurred ({report[0]}): {report[1]}"
    if sys.stderr is None:
        # Closed before the process started (`2>&-`): nothing to report to,
        # and the code is the work's, as for a closed stdout.
        return
    textencoding.prepare_error_report(sys.stderr)
    try:
        sys.stderr.write(f"boto3-s3: [ERROR]: {text.strip()}".rstrip() + "\n")
    except OSError:
        # A stderr that cannot take the report (a reader that left the pipe,
        # a full disk) changes no code: what it holds goes to the null device,
        # so the exit's flush does not end the process at 120
        # (`_settle_std_streams`).
        _discard(sys.stderr)


def _invalid_choice_message(value: object, choices: Iterable[object]) -> str:
    """aws's report for an off-list value (its `CLIArgParser._check_value`).

    `Found invalid choice '<value>'` - a trailing newline of its own included,
    which is what puts an extra blank line ahead of the usage block - followed
    by the close matches of the choice list, if any (difflib, aws's cutoff
    0.8: `lss` offers `ls` while `web` offers nothing).
    """
    message = [f"Found invalid choice '{value}'\n"]
    possible = get_close_matches(str(value), [str(c) for c in choices], cutoff=0.8)
    if possible:
        message.append("Maybe you meant:\n")
        message.extend(f"  * {word}" for word in possible)
    return "\n".join(message)


class _ParamValidationArgumentParser(argparse.ArgumentParser):
    """Parse and report failures the aws-cli way: its report, its token counts.

    The counterpart of aws's `CLIArgParser`, which every one of its parsers is
    built on: one error shape for all of them, plus what decides which
    command lines are accepted - the dash-led token classification of aws's
    bundled Python (`_negative_number_matcher`) and, through
    `_match_argument`, the token consumption that interpreter performs for
    the options marked `aws_nargs`. A failure is a usage error (rc 252)
    reported as ``<message>\\n\\n<usage>`` in one ``[ERROR]`` report, so the
    usage lands after a blank line inside it; an off-list value's message is
    aws's (`_check_value`). The usage is each parser's ``usage`` argument:
    the collapsed top-level block (`_TOP_LEVEL_USAGE`) for the two parsers
    the dispatch parses with.
    """

    @property
    def _negative_number_matcher(self) -> re.Pattern[str]:
        """Classify dash-led tokens by Python 3.14's rule on every Python.

        argparse consults this attribute in `_parse_optional` to decide whether
        a token that matches no option string is option-like or a plain
        positional, and 3.14 loosened it (`_NEGATIVE_NUMBER_RE`). Since aws
        runs on 3.14, `--storage-class -1x` is aws's invalid choice, not its
        missing value, and pinning the pattern reproduces that from our 3.10
        floor up. A property rather than a plain attribute because argparse's
        own `__init__` assigns the host Python's matcher over any class
        attribute; the setter below absorbs that assignment.

        The pin covers the `_parse_optional` classification in every parser
        the dispatch builds, matching aws, where the one interpreter decides
        for its whole `CLIArgParser` family alike. argparse's other use of the
        matcher stays the host Python's: the gate that switches the
        classification off once a registered option string itself looks like a
        negative number is evaluated per container, and every optional is
        registered on a plain argument group. That is moot only because no
        option string here is digit-led, which test_exit_codes.py pins.
        """
        return _NEGATIVE_NUMBER_RE

    # Narrowing argparse's plain attribute to a property is the override this
    # needs and the one a type checker flags; the getter documents why.
    @_negative_number_matcher.setter
    def _negative_number_matcher(  # pyright: ignore[reportIncompatibleVariableOverride]
        self, matcher: re.Pattern[str]
    ) -> None:
        """Discard argparse's own assignment; the pinned pattern is the answer."""

    def _check_value(self, action: argparse.Action, value: object) -> None:
        """Reject an off-list value with aws's message (`_invalid_choice_message`).

        aws overrides the same hook. ``_check_value`` is private argparse API.
        """
        if action.choices is None or value in action.choices:
            return
        raise argparse.ArgumentError(action, _invalid_choice_message(value, action.choices))

    def _match_argument(self, action: argparse.Action, arg_strings_pattern: str) -> int:
        """Give a count-declared option its tokens even when they are dash-led.

        argparse sorts the tokens ahead of the decision into three classes -
        ``A`` (plain), ``O`` (option-like) and ``-`` (the ``--`` separator) -
        and for an option declared with a token count Python 3.12+ accepts
        ``A`` and ``O`` alike (its nargs pattern became ``[AO]{N}``), which is
        what this hook reproduces. So ``--exclude '-foo*'`` is a pattern and
        ``mb --tags -k -v`` a tag pair, while a missing token still reports
        the count and ``--`` never becomes a value (its class is neither) -
        all as aws behaves. Before 3.12 only ``A`` counted, so the same
        declarations reported a missing value; aws's official distribution
        bundles 3.14, and the marker (``aws_nargs``, the count aws declares)
        keeps that outcome off the host Python version. The marker is read
        duck-typed: importing the filters here would drag in
        ``boto3_s3.globsieve``, and the informational exits may reach no
        library module beyond the lazy ``boto3_s3`` root (design/imports.md,
        pinned by test_import_contract.py).

        Only the count is settled here. Which class a token falls into stays
        argparse's own decision, and up to 3.11 that classification pass also
        raises the ambiguous-abbreviation error - it runs over the whole token
        stream before any consumption, so a value that ambiguously
        abbreviates one of the command's own options (``--exclude --ss``) is
        rejected there while aws, and our 3.12+ runs, take it as the value.
        design/cli.md section 2 records that as the residual it is.

        ``_match_argument`` is private argparse API, the same kind of hook as
        ``_check_value`` above.
        """
        count = getattr(action, "aws_nargs", None)
        if isinstance(count, int):
            if re.match(f"[AO]{{{count}}}", arg_strings_pattern):
                return count
            # argparse's report for the count aws declares (`nargs=<count>`).
            raise argparse.ArgumentError(
                action, f"expected {count} argument{'' if count == 1 else 's'}"
            )
        return super()._match_argument(action, arg_strings_pattern)

    def error(self, message: str) -> NoReturn:
        _write_error(f"{message}\n\n{self.format_usage()}", rc=_PARAM_VALIDATION_ERROR_RC)
        self.exit(2)


def _find_command_token(tokens: list[str]) -> int:
    """Locate the subcommand in the post-globals token stream, aws style.

    aws's command layers assume an unknown optional consumes no value
    (``SubCommandArgParser._remove_subcommand``): the first positional-looking
    token names the subcommand, everything else stays in place for the leaf
    parser. Positional-looking follows argparse's classification - not
    dash-led, a lone ``-``, a negative-number *opening*
    (``_NEGATIVE_NUMBER_RE``, the same pattern
    ``_ParamValidationArgumentParser`` pins for argparse itself), a token with
    a space, or anything after ``--``.

    Returns the index of that token, or ``-1`` when nothing positional-looking
    remains.
    """
    protected = False
    for index, token in enumerate(tokens):
        if not protected:
            if token == "--":
                protected = True
                continue
            if (
                token.startswith("-")
                and token != "-"
                and " " not in token
                and not _NEGATIVE_NUMBER_RE.match(token)
            ):
                continue
        return index
    return -1


def _load_command(name: str) -> type[Command]:
    """Import the matched subcommand's module and return its class (stage 2)."""
    module_name, class_name, _help = _COMMAND_TABLE[name]
    return cast("type[Command]", getattr(importlib.import_module(module_name), class_name))


def _shared_globals_parent() -> _ParamValidationArgumentParser:
    """The suppressed-defaults globals parent of the *rendering* parsers.

    Only help pages and `build_parser` are built on it (the
    dispatch parses globals in the top-level pass and the leaf with
    ``_build_command_parse_parser``); it keeps every subcommand's help listing
    the globals, including the recognized-but-ignored group. Suppressing the
    defaults keeps it inert if such a parser ever parses (nothing to clobber).
    """
    shared = _ParamValidationArgumentParser(add_help=False)
    globalargs.add_common_arguments(shared, suppress_defaults=True)
    return shared


def _top_level_parser() -> tuple[
    _ParamValidationArgumentParser,
    argparse._SubParsersAction[_ParamValidationArgumentParser],  # pyright: ignore[reportPrivateUsage]
]:
    """The top-level parser both trees share, and its empty subcommand slot.

    The program name, description, the globals and the ``<subcommand>``
    placeholder - so `_build_stage1_parser` and `build_parser` render the same
    top-level page by construction; each fills the slot with its own entries.

    ``add_help=False`` everywhere in the tree: like aws, the only way to a help
    page is the ``help`` token, so no parser declares a help option and none
    renders one.
    """
    parser = _ParamValidationArgumentParser(
        prog="boto3-s3",
        description="An aws s3-compatible CLI built on the boto3-s3 library.",
        add_help=False,
    )
    globalargs.add_common_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="<subcommand>", required=True)
    return parser, subparsers


def _build_stage1_parser() -> argparse.ArgumentParser:
    """The top-level rendering parser: globals + the subcommand names and help lines.

    No command module is imported here. The stub entries carry only the
    table's name/help, so the top-level help page renders exactly as the full
    tree renders it while the path stays SDK- and command-module-free. The
    dispatch never parses through this parser at all: ``_find_command_token``
    locates the subcommand and ``_resolve_command`` reports a rejected one, so
    this one only ever prints help.
    """
    parser, subparsers = _top_level_parser()
    for name, (_module, _cls, help_text) in _COMMAND_TABLE.items():
        subparsers.add_parser(name, help=help_text, add_help=False)
    return parser


class _GlobalsArgumentParser(_ParamValidationArgumentParser):
    """The globals parser, closing a ``--profile`` / ``--debug`` failure as aws does.

    aws parses those two in a first pass of their own (``FirstPassGlobalArgParser``)
    whose usage line lists only them; every other global fails in its main
    parser, under the top-level block.
    """

    def error(self, message: str) -> NoReturn:
        if message.startswith(("argument --profile:", "argument --debug:")):
            _write_error(f"{message}\n\n{_FIRST_PASS_USAGE}", rc=_PARAM_VALIDATION_ERROR_RC)
            self.exit(2)
        super().error(message)


def _build_globals_parser() -> _ParamValidationArgumentParser:
    """Globals-only parser for the aws-shaped top-level pass (``_dispatch``).

    aws parses the top-level globals over the *full* argv first
    (``MainArgParser.parse_known_args``) and *removes* them: the command
    layers only ever see that parse's remainder. This parser is therefore the
    dispatcher's real tokenizer, not a probe - a global is recognized on
    either side of the subcommand, with argparse prefix abbreviation
    (``--e`` resolves to ``--endpoint-url`` even where a command option like
    ``--expires-in`` shares the prefix, because this parse runs first -
    measured), and even between a command option and its value (aws accepts
    ``presign --expires-in --region us-east-1 120``). The parse also settles
    two outcomes on the spot: a global that fails to parse (invalid choice,
    missing value, an ambiguous abbreviation) and a parse-time ``--version``.
    ``add_help=False`` leaves ``-h`` / ``--help`` in the remainder as the
    unrecognized options they are on aws (measured: ``s3 ls -h`` is ``Unknown
    options: -h``). A failure here closes with the top-level usage block, like
    aws, whose main parser and command layers share one usage string.
    """
    parser = _GlobalsArgumentParser(prog="boto3-s3", add_help=False, usage=_TOP_LEVEL_USAGE)
    globalargs.add_common_arguments(parser)
    return parser


def _build_command_parser(name: str, command: Command) -> argparse.ArgumentParser:
    """The subcommand's full rendering parser: globals section + command args.

    ``prog`` / ``description`` match what ``add_parser`` produces under the
    full tree, so ``boto3-s3 <cmd> help`` pages exactly as the whole tree
    pages it. The dispatch parses with ``_build_command_parse_parser`` and
    only renders help pages through this one.
    """
    parser = _ParamValidationArgumentParser(
        prog=f"boto3-s3 {name}",
        description=type(command).help,
        parents=[_shared_globals_parent()],
        add_help=False,
    )
    command.configure(parser)
    return parser


def _build_command_parse_parser(name: str, command: Command) -> argparse.ArgumentParser:
    """The parser stage 2 actually parses with: the command's own args only.

    The globals were all consumed by the top-level pass (either side of the
    subcommand), so like aws's leaf parser this one does not know them - a
    global-looking token that reaches stage 2, which only a dropped leading
    ``--`` can produce, is rejected as unknown exactly like aws (measured:
    ``s3 -- presign --region us-east-2 s3://b/k`` reports ``Unknown options:
    --region,s3://b/k``). It carries the same top-level usage block aws builds
    its leaf parser with, so a usage error here closes with that block; the
    command's whole option surface stays on its help page, which
    ``_build_command_parser`` renders.
    """
    parser = _ParamValidationArgumentParser(
        prog=f"boto3-s3 {name}",
        description=type(command).help,
        usage=_TOP_LEVEL_USAGE,
        add_help=False,
    )
    command.configure(parser)
    return parser


def build_parser() -> argparse.ArgumentParser:
    """Build the complete parser: every subcommand's full argument surface.

    The normal dispatch does not call this (stage 1 + stage 2 above); it
    builds the whole surface at once - importing every command module - for
    the tests that parse a full command line.
    """
    parser, subparsers = _top_level_parser()
    shared = _shared_globals_parent()
    for name in _COMMAND_TABLE:
        command_cls = _load_command(name)
        command_cls().configure(
            subparsers.add_parser(
                name,
                parents=[shared],
                help=command_cls.help,
                description=command_cls.help,
                add_help=False,
            )
        )
    return parser


def _enable_debug_logging() -> None:
    # Deferred (only --debug pays it): the library's masked, boto3-faithful
    # stream-logger setup. mask_secrets defaults to True, so credentials in the
    # botocore DEBUG output (signed headers, signatures, tokens) are redacted.
    import logging

    from boto3_s3 import set_stream_logger
    from boto3_s3.masking import SecretMaskingFilter

    for name in _DEBUG_LOGGERS:
        # Idempotent, like aws's set_stream_logger (it removes its named
        # handler before re-adding): the library's boto3-faithful
        # set_stream_logger appends unconditionally, so drop the previously
        # attached masking handlers first or a second call doubles every line.
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if any(isinstance(f, SecretMaskingFilter) for f in handler.filters):
                logger.removeHandler(handler)
        set_stream_logger(name, logging.DEBUG, stream=sys.stderr, mask_secrets=True)


def exit_code_for(exc: Boto3S3Error) -> int:
    """Map a library error to the aws-cli v2 exit code (design/cli.md section 6).

    Server-rejected calls carry the botocore ``ClientError`` as ``__cause__``
    (``boto3_s3.sdkerrors.s3_errors``) and exit 254 like aws-cli regardless of
    the library category - aws-cli treats every error that reached the server
    as a client error, even ones our taxonomy files under ``ValidationError``.
    ``InvalidValueError`` / ``InvalidConfigError`` refine their parents back to
    the general 255: aws routes those failures (a post-parse ``int()``, a bad
    ``[s3]`` value, an unusable profile) through its general handler, not the
    dedicated 252 / 253 ones.
    """
    # Import locally because this mapping is the only code here that needs the
    # concrete botocore exception type.
    from botocore.exceptions import ClientError

    if isinstance(exc.__cause__, ClientError):
        return _CLIENT_ERROR_RC
    # The refining subclasses come first: aws reports a post-parse value
    # failure or a bad config through its *general* handler (255), even
    # though the taxonomy files them under Validation / Configuration.
    if isinstance(exc, (InvalidValueError, InvalidConfigError)):
        return _GENERAL_ERROR_RC
    if isinstance(exc, ValidationError):
        return _PARAM_VALIDATION_ERROR_RC
    if isinstance(exc, ConfigurationError):
        return _CONFIGURATION_ERROR_RC
    return _GENERAL_ERROR_RC


def _exit_code_for_unexpected(exc: BaseException) -> int:
    """Map a non-`Boto3S3Error` exception escaping a command to aws-cli's rc.

    Mirrors aws-cli's error-handler chain for exceptions that reach the entry
    point (errorhandler.py): a raw botocore parameter-validation failure is
    252, a credential / region resolution failure is 253, a `ClientError` is
    254, and everything else is the general 255 (`GeneralExceptionHandler`).
    The common paths are already translated into `Boto3S3Error` (the library's
    `s3_errors` and the CLI's `build_client`); this is the catch-all so no
    path can crash the CLI with a traceback (rc 1), which the error rules
    forbid (design/overview.md section 3) - with two deliberate escapes:
    `AssertionError` (an internal-invariant bug) re-raises loudly instead of
    being masked as a generic rc, and the ``BaseException`` family passes
    (``SystemExit`` honors the requested orderly exit; ``KeyboardInterrupt``
    is the outer 130 wrapper's).
    """
    # Import locally because only unexpected command failures need these types.
    from botocore.exceptions import (
        ClientError,
        NoCredentialsError,
        NoRegionError,
        ParamValidationError,
    )

    # Only NoCredentials / NoRegion are 253 (aws errorhandler.py dedicated
    # handlers). PartialCredentialsError has no aws handler ->
    # GeneralExceptionHandler -> 255, so it must fall through here, not map to 253.
    if isinstance(exc, (NoCredentialsError, NoRegionError)):
        return _CONFIGURATION_ERROR_RC
    if isinstance(exc, ClientError):
        return _CLIENT_ERROR_RC
    if isinstance(exc, ParamValidationError):
        return _PARAM_VALIDATION_ERROR_RC
    return _GENERAL_ERROR_RC


def main(argv: list[str] | None = None, *, ctx: Context | None = None) -> int:
    """Parse ``argv``, dispatch to the requested subcommand, and return its exit code.

    *ctx* carries the runtime dependencies the command resolves (the S3 client
    factory); tests inject a ``Context`` built
    around fakes. Returns the exit code (the deliberate escapes: an
    ``AssertionError`` - an internal bug - and a command-raised
    ``SystemExit``, whose orderly exit is honored) - argparse's ``SystemExit`` is
    absorbed downstream so usage errors map to aws-cli's 252, not argparse's 2,
    and a Ctrl-C mirrors aws's ``InterruptExceptionHandler``: a bare
    newline on stdout and rc 130 (128+SIGINT), never a traceback. The
    standard streams are flushed before the code is returned
    (`_settle_std_streams`), so the code is the one the process ends with.

    Without ``argv`` - the console script - the process is set up as ``aws``
    sets itself up first: the standard streams take the encoding ``aws``
    writes (`textencoding.configure_std_streams`) and the command line is
    read with the encoding ``aws`` reads it with
    (`textencoding.decode_argv`). An ``argv`` given by a caller is taken as
    it is.
    """
    if argv is None:
        textencoding.configure_std_streams()
        argv = textencoding.decode_argv(sys.argv[1:])
    try:
        rc = _main(argv, ctx)
    except KeyboardInterrupt:
        with contextlib.suppress(Exception):
            sys.stdout.write("\n")
        rc = 130
    except AssertionError:
        # An internal-invariant violation, kept loud (see `_run_command`).
        raise
    except Exception as exc:
        # The rest of that chain: whatever escapes aws's driver - not only a
        # command - reaches its entry point's handlers and is reported there,
        # never as a traceback. A command's failures are settled inside
        # `_run_command`; this is the same mapping for the dispatcher's own
        # steps, where an exception no step claims must still not surface as
        # a traceback.
        rc = _exit_code_for_unexpected(exc)
        _write_error(exc, rc=rc)
    try:
        return _settle_std_streams(rc)
    except KeyboardInterrupt:
        # A flush held up by a reader that stopped reading, cut short: what
        # the streams still hold is dropped, or the interpreter's flush at
        # exit would wait on the same reader again (or end at 120).
        _discard(sys.stdout)
        _discard(sys.stderr)
        return 130


def _settle_std_streams(rc: int) -> int:
    """Flush stdout and stderr, and return the exit code the process ends with.

    Bytes a standard stream refused - a full disk, a reader that left the
    pipe - stay in Python's buffer, and the interpreter flushes the streams
    again as it exits: a failure there replaces any exit code with 120 and
    prints an ``Exception ignored`` report. A stream that cannot be flushed
    here is therefore pointed at the null device (`_flush_or_discard`), so the
    exit's flush succeeds and the code stands. A run that already failed
    keeps its code: its failure was reported (a ``download failed:`` line,
    ``ls``'s broken pipe). A run that succeeded - 0, or 2 for a transfer
    that finished with warnings only - but whose output could not be written
    exits 255 with the cause: the output asked for was lost, which is a local
    I/O failure, not a success. A stream that is None (closed before the
    process started, ``1>&-``) is left alone and the code is the work's.
    """
    failure = _flush_or_discard(sys.stdout)
    if failure is not None and rc in (0, 2):
        rc = _GENERAL_ERROR_RC
        with contextlib.suppress(Exception):
            _write_error(failure, rc=rc)
    _flush_or_discard(sys.stderr)
    return rc


def _flush_or_discard(stream: TextIO | None) -> OSError | None:
    """Flush *stream*; on failure, `_discard` what it still holds.

    A closed stream has nothing left to flush. Returns the flush's error, or
    None when the stream took everything.
    """
    if stream is None:
        return None
    try:
        stream.flush()
    except ValueError:
        return None
    except OSError as exc:
        _discard(stream)
        return exc
    return None


def _discard(stream: TextIO | None) -> None:
    """Point *stream*'s descriptor at the null device.

    Python's own recipe for a broken pipe (``os.dup2`` of ``os.devnull``):
    the bytes the stream still holds then go nowhere at exit instead of
    failing - or waiting on - the interpreter's flush. A stream without a
    descriptor (a test double) has nothing to repoint.
    """
    if stream is None:
        return
    with contextlib.suppress(Exception):
        fd = stream.fileno()
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, fd)
        finally:
            os.close(null)


def _main(argv: list[str] | None, ctx: Context | None) -> int:
    """The body of `main` (split out so its Ctrl-C backstop wraps everything)."""
    if ctx is None:
        ctx = Context()
    return _dispatch(list(sys.argv[1:] if argv is None else argv), ctx)


def _dispatch(argv: list[str], ctx: Context) -> int:
    """Parse ``argv`` in two stages and run the matched subcommand.

    Stage 1 is aws's own shape: the globals parser consumes the globals off
    the full argv (either side of the subcommand) and everything below only
    ever sees its remainder; ``_find_command_token`` then locates the
    subcommand in that remainder without touching the other tokens. Its
    static metadata lets top-level help and ``--version`` exit without
    importing a command module or the AWS SDK (import contract,
    design/imports.md).
    Stage 2 imports just the matched command's module (rb also pulls in rm,
    its --force engine), builds its real parser, and parses the remainder
    minus the subcommand token into the globals pass's namespace. Once the
    subcommand is determined the SDK may load - the aws-clidriver-shaped lazy
    command table.
    """
    # aws reads `~/.aws/cli/alias` here; this CLI does not
    # (docs/cli/aws-differences.md).
    #
    # aws-shaped top-level pass: aws parses the globals over the full argv
    # (MainArgParser.parse_known_args) and REMOVES them before any
    # command-layer parsing. argparse's ``--`` handling does the rest and is
    # identical on every supported Python (enumerated 3.10 vs aws's bundled
    # 3.14): nothing after the first ``--`` is consumed as a global, and the
    # marker stays in the remainder for stage 2's parse to honor - except a
    # leading ``--``, which aws's very first parse uses up against its service
    # token (``aws s3`` always precedes it; measured: ``s3 -- help`` pages
    # while ``s3 --region us-east-2 -- help`` is an invalid choice 'help', and
    # ``s3 -- presign --expires-in 120 s3://b/k`` re-reads the option at the
    # leaf, rc 0). Ours has no service token, so the equivalent is dropping
    # the marker off a ``--``-led argv after the parse. An exactly-['help']
    # remainder is aws's help-token rule: the top-level help page, rc 0 -
    # globals around the token are already stripped, so
    # `help --region us-east-1` still pages (aws does too). A global that
    # fails to parse, and a parse-time --version, end the run right here,
    # written by the parser itself: the report through `_write_error` like
    # every other one, the version line by its action. A zero exit can only
    # be --version - the globals parser declares no help option.
    try:
        head, remainder = _build_globals_parser().parse_known_args(argv)
    except SystemExit as exc:
        return 0 if not exc.code else _PARAM_VALIDATION_ERROR_RC
    tokens = remainder[1:] if argv[:1] == ["--"] else remainder
    # aws refuses an unknown AWS_CLI_OUTPUT_ENCODING codec before any command
    # runs (rc 255); a known one sets the error reports' encoding
    # (`textencoding.prepare_error_report`).
    output_encoding = os.environ.get(_OUTPUT_ENCODING_ENV_VAR)
    if output_encoding is not None:
        try:
            "".encode(output_encoding)
        except LookupError:
            _write_error(
                f"Unknown codec `{output_encoding}` specified for {_OUTPUT_ENCODING_ENV_VAR}.",
                rc=_GENERAL_ERROR_RC,
            )
            return _GENERAL_ERROR_RC
    return _resolve_command(tokens, head, ctx)


def _print_help(parser: argparse.ArgumentParser) -> None:
    """Write *parser*'s help page to stdout, or to stderr without a stdout.

    Not argparse's ``print_help``: from Python 3.11 it swallows an
    ``OSError`` from the write, and a page larger than the stream's buffer
    goes straight to the descriptor, so a page stdout cannot take would be
    lost with no report and rc 0. Written here, the error reaches `main`,
    which reports it at 255. The stderr fallback is the one ``print_help``
    makes for a process without a stdout.
    """
    stream = sys.stdout if sys.stdout is not None else sys.stderr
    if stream is not None:
        stream.write(parser.format_help())


def _unknown_options_message(tokens: list[str]) -> str:
    """aws's report for tokens no parser placed (its customizations command
    layer joins them with "," and no space; two ported aws-cli tests read it)."""
    return f"Unknown options: {','.join(tokens)}"


def _resolve_command(tokens: list[str], head: argparse.Namespace, ctx: Context) -> int:
    """Pick the subcommand out of the globals-free token stream and run it.

    aws's ``s3`` command does this: the help token first, then the first
    positional-looking token as the subcommand name, looked up in its command
    table.
    """
    if tokens == ["help"]:
        _print_help(_build_stage1_parser())
        return 0

    index = _find_command_token(tokens)
    if index < 0:
        # No subcommand token at all: whatever is left is an unknown option
        # (aws: `s3 --bogus`), and with nothing left it is aws's bare
        # missing-subcommand usage error, which carries no help blurb
        # (measured: `s3` and `s3 --`).
        leftovers = [token for token in tokens if token != "--"]
        message = _unknown_options_message(leftovers) if leftovers else _TOO_FEW_ARGUMENTS
        _write_error(message, rc=_PARAM_VALIDATION_ERROR_RC)
        return _PARAM_VALIDATION_ERROR_RC
    name = tokens[index]
    # aws's subcommand extraction (SubCommandArgParser._remove_subcommand):
    # only the matched token leaves the stream; an option-like token ahead of
    # it - `s3 --expires-in=120 presign s3://b/k` parses, measured - and
    # everything behind it flow to the leaf parser in their original order,
    # where an unknown one is rejected with the leaf's own wording.
    stage2_tokens = tokens[:index] + tokens[index + 1 :]
    if name not in _COMMAND_TABLE:
        # aws checks the name with a positional whose choices are its command
        # table, so the report is that positional's parse error: named after
        # its dest - a bare `subcommand`, where the help page displays
        # `<subcommand>` - and closed with the top-level usage block.
        _write_error(
            f"argument subcommand: {_invalid_choice_message(name, _COMMAND_TABLE)}"
            f"\n\nusage: {_TOP_LEVEL_USAGE}",
            rc=_PARAM_VALIDATION_ERROR_RC,
        )
        return _PARAM_VALIDATION_ERROR_RC
    return _run_command(name, stage2_tokens, head, ctx)


def _run_command(
    name: str, stage2_tokens: list[str], head: argparse.Namespace, ctx: Context
) -> int:
    """Stage 2: build the matched subcommand's parser, parse, and run it."""
    command = _load_command(name)()
    if stage2_tokens == ["help"]:
        # aws's help-token rule at the subcommand level (its ArgTableArgParser
        # special-cases an exactly-['help'] remainder): the command's help
        # page, rc 0 - even where a normal parse would fail or run a listing.
        # The globals are already stripped, so a `help` wrapped in globals
        # still pages (aws: `s3 presign help --region us-east-1` pages).
        _print_help(_build_command_parser(name, command))
        return 0
    # The top-level pass's namespace carries every parsed global (either side
    # of the subcommand - all consumed there) plus their defaults; the leaf
    # parse fills in the command's own arguments.
    head.command = name
    try:
        args, extras = _build_command_parse_parser(name, command).parse_known_args(
            stage2_tokens, namespace=head
        )
    except SystemExit as exc:
        return 0 if not exc.code else _PARAM_VALIDATION_ERROR_RC
    if extras:
        # What the leaf parse could not place (exercised by the ported
        # test_errors_out_with_extra_arguments). aws's command layer raises it
        # as a plain message: no usage block.
        _write_error(_unknown_options_message(extras), rc=_PARAM_VALIDATION_ERROR_RC)
        return _PARAM_VALIDATION_ERROR_RC

    if getattr(args, "debug", False):
        _enable_debug_logging()

    try:
        # --query is validated and then ignored, so nothing downstream reads
        # it; this is its one check.
        globalargs.validate_query(args)
        return command.run(args, ctx)
    except Boto3S3Error as exc:
        rc = exit_code_for(exc)
        _write_error(exc, rc=rc)
        return rc
    except AssertionError:
        # An AssertionError is an internal-invariant violation (a bug), not a
        # user-facing error condition - let it surface loudly rather than be
        # masked as a generic rc. This also keeps test doubles' "unexpected
        # call" guards (the recording client / injected factories, which raise
        # AssertionError) effective; the catch-all below would otherwise swallow
        # them into rc 255.
        raise
    except Exception as exc:
        # Defense in depth: a non-library exception escaping a command (e.g. a
        # raw botocore error from a path that does not translate) maps to
        # aws-cli's handler chain instead of crashing with a traceback + rc 1
        # (a failure is reported, not a traceback - design/overview.md
        # section 3).
        # KeyboardInterrupt / SystemExit are BaseException, not Exception, so
        # they still propagate - a Ctrl-C reaches main's aws-shaped backstop
        # (a bare newline + rc 130, no traceback).
        rc = _exit_code_for_unexpected(exc)
        _write_error(exc, rc=rc)
        return rc
