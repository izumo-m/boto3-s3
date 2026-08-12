"""Entry point and dispatch for the ``boto3-s3`` (``aws s3``-compatible) CLI."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import logging
import os
import re
import sys
from collections.abc import Generator, Iterable
from contextlib import AbstractContextManager, contextmanager
from difflib import get_close_matches
from typing import NoReturn, TextIO, cast

from boto3_s3 import (
    Boto3S3Error,
    ConfigurationError,
    InvalidConfigError,
    InvalidValueError,
    ValidationError,
)
from boto3_s3_cli import alias, configfiles, globalargs
from boto3_s3_cli.autoprompt import resolve
from boto3_s3_cli.commands.base import Command, Context

# Loggers a masked stderr handler is attached to under --debug, via the
# library's boto3-faithful set_stream_logger (credential masking on by default -
# design/masking.md). The library attaches no handler on import. "boto3_s3_cli"
# is the counterpart of aws-cli's own "awscli" logger (clidriver._set_logging),
# so the CLI's own debug lines (runtimeconfig's alias resolution) surface too.
# urllib3 is deliberately omitted: it logs no credentials, only
# connection-pool noise.
_DEBUG_LOGGERS = ("boto3_s3", "boto3_s3_cli", "botocore", "boto3", "s3transfer")

# aws-cli v2 exit-code conventions (awscli/constants.py). The
# exit-code charter (design/overview.md section 3) requires matching them; see
# design/cli.md section 6 for the full table.
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
# `boto3-s3 <subcommand> help`. Every parse error renders it,
# a subcommand's own parse included, exactly as aws hands one shared USAGE
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


# This run's snapshot of aws's config files, and whether an rc-252 report can
# still carry the enhanced envelope. aws renders that envelope through its
# session - the handler asks it for `cli_error_format` - so a session bound to
# a profile no config file declares raises `ProfileNotFound` inside the
# renderer, which swallows the failure and writes the bare report instead; the
# rc is decided separately and stays 252. `_main` takes the snapshot once (like
# the session caching `full_config`) and resets the flag, `_dispatch` re-decides
# it at the two points where aws's session learns the profile. `_main` is the
# only route into `_dispatch`, so the snapshot is never consulted stale.
_config_scan = configfiles.ConfigScan(None, {})
_enhanced_envelope = True

# aws's report for a `cli_timestamp_format` its timestamp-format customization
# does not accept, verbatim (its ConfigurationError; the code the envelope
# names is its ConfigurationErrorHandler's).
_UNKNOWN_TIMESTAMP_FORMAT = (
    'Unknown cli_timestamp_format value: {}, valid values are "wire" or "iso8601"'
)

# What an alias that expands to itself ends as, in aws's build's wording
# (Python 3.12+ settled on this single form) - see the handler in `_dispatch`.
_RECURSION_LIMIT_REPORT = "maximum recursion depth exceeded"

# aws's codec override for the streams it reports errors on, and the
# interpreter variable it still honors as a fallback (its compat.py).
_OUTPUT_ENCODING_ENV_VAR = "AWS_CLI_OUTPUT_ENCODING"
_PYTHONUTF8_ENV_VAR = "PYTHONUTF8"


def _unresolved_config_report(exc: BaseException) -> tuple[str, str] | None:
    """aws's envelope code and message for an unresolvable credentials / region.

    aws gives botocore's `NoCredentialsError` and `NoRegionError` handlers of
    their own (errorhandler.py). Each one names a fixed code - a per-handler
    constant, not the exception's class name - and appends its own hint to
    botocore's text: a `. ` separator for the credentials one, whose text ends
    without a period, a bare space for the region one, whose text ends with
    one. The hints keep naming the `aws` tool because that is what aws writes
    and both tools read the same config files, so the advice holds
    (docs/cli/aws-differences.md; `aws login` has no counterpart here).

    The pair is identified by the botocore exception the library keeps as
    `__cause__` (`s3storage.s3_errors`, the client builders), never by matching
    the message text. Returns `None` for every other rc-253 failure: those are
    this CLI's own (an absent awscrt, an SDK floor shortfall), aws cannot reach
    them, and they carry no such cause - so they stay bare.

    aws's other reachable rc-253 handler, `Configuration`, claims no exception
    here: its one failure on this surface (an unknown `cli_timestamp_format`)
    is a pre-dispatch gate that names its code directly. `Pager` belongs to
    the output pager, which this CLI does not implement.
    """
    # Imported here, on a path where credential / region resolution has already
    # loaded the SDK, so the informational exits stay SDK-free (design/imports.md).
    from botocore.exceptions import NoCredentialsError, NoRegionError

    cause = exc.__cause__ if isinstance(exc, Boto3S3Error) else exc
    if isinstance(cause, NoCredentialsError):
        return "NoCredentials", f'{cause}. You can configure credentials by running "aws login".'
    if isinstance(cause, NoRegionError):
        return "NoRegion", f'{cause} You can also configure your region by running "aws configure".'
    return None


def _set_preferred_output_encoding(stream: TextIO) -> None:
    """Re-encode a report stream the way aws's error writers do.

    aws builds a text writer for every error report it makes, and building one
    re-encodes the stream it wraps (`compat.set_preferred_output_encoding`):
    to `AWS_CLI_OUTPUT_ENCODING` when that names a codec, else to UTF-8 when
    `PYTHONUTF8` is `1` - the fallback aws keeps for users of the interpreter
    variable its frozen build stopped honoring. Reconfiguring an encoding
    without an error handler resets the handler to `strict`, so a report the
    codec cannot represent raises instead of being escaped; `main` reports
    that raise the way aws's entry-point chain does.

    aws builds both writers for every report and its handlers then write to
    one: stderr for every report there is, stdout for the bare newline a
    Ctrl-C prints. Each call site here re-encodes the stream it is about to
    write, which is the same thing for everything observable - nothing else on
    this surface writes after a report. Nothing outside a report re-encodes at
    all: a successful run's result, progress and warning lines keep the
    interpreter's own streams, aws included (measured on both tools).

    An unknown codec is ignored rather than raised: `_dispatch`'s gate has
    already rejected one, and aws swallows it here too so the report *about*
    the codec can still be written. A stream with nothing to reconfigure (a
    capture buffer) is left alone.
    """
    encoding = os.environ.get(_OUTPUT_ENCODING_ENV_VAR)
    if encoding is None:
        if os.environ.get(_PYTHONUTF8_ENV_VAR) != "1":
            return
        encoding = "UTF-8"
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        with contextlib.suppress(LookupError):
            reconfigure(encoding=encoding)


def _write_error(message: object, *, rc: int | None = None, code: str | None = None) -> None:
    """Write one CLI error, adding the enhanced envelope required by `rc`.

    aws renders every enveloped report through one formatter, so the code it
    names comes from whichever handler claimed the exception: `ParamValidation`
    for the whole rc-252 family, and - for the two rc-253 failures aws reaches
    through a botocore exception on the surface implemented here - one of the
    codes `_unresolved_config_report` supplies. A caller that already knows
    which handler aws would use passes `code` itself, for a failure this CLI
    settles without an exception (the `cli_timestamp_format` gate). The rcs
    below envelope nothing: an rc-254 `ClientError` already carries `An error
    occurred (<Code>) when calling ...` in its own text, and aws's general
    rc-255 handler formats nothing.

    The three steps below are aws's own sequence, and their order is
    observable. Its handler builds the enveloped message first and hands the
    result to `format_error_message`, which **then** tests the message for
    falsiness and only **then** strips it. So: an empty detail that a code
    envelopes still renders the envelope (stripped of the space the empty
    detail left), while a detail that is empty *and* uncoded renders the prefix
    alone, with **no** trailing space - the branch aws takes before it ever
    builds the spaced form. The failure that reaches that branch is awscrt's
    bare region assertion, which the CRT construction converts at rc 255, and
    the rc-255 general handler envelopes nothing.

    Stripping last is what keeps the multi-line reports clean: they end with
    the newline their usage block carries, which must not surface as a trailing
    blank line. A message carrying its own embedded `[ERROR]` line (the
    missing-subcommand report) therefore renders as two prefixed lines,
    enveloped or not, exactly as aws's does.
    """
    detail = str(message)
    if code is None:
        if rc == _PARAM_VALIDATION_ERROR_RC:
            code = "ParamValidation"
        elif rc == _CONFIGURATION_ERROR_RC and isinstance(message, BaseException):
            report = _unresolved_config_report(message)
            if report is not None:
                code, detail = report
    _set_preferred_output_encoding(sys.stderr)
    # The degradation is the renderer's, so it costs any code its envelope
    # while leaving the message the handler built - aws's fallback writes the
    # extracted `Message`, hints included. Only the rc-252 family can be
    # observed degraded: an undeclared profile makes botocore raise
    # `ProfileNotFound` (rc 255) before credentials or a region are ever
    # resolved, so no rc-253 report co-occurs with it (measured) - the
    # `Configuration` gate reads the same undeclared profile and stands down
    # for the same reason.
    if code is not None and _enhanced_envelope:
        try:
            _write_report(f"An error occurred ({code}): {detail}")
            return
        except Exception:
            # The other way into the degraded form: aws's renderer wraps the
            # whole enveloped write in a `try`, so a codec that cannot
            # represent the report drops it there and the bare message is
            # written instead - unenveloped, and only then allowed to fail
            # (measured: the position the codec error names is the bare
            # message's, and rc 252 becomes aws's 255 through that second
            # failure).
            pass
    _write_report(detail)


def _write_report(detail: str) -> None:
    """aws's `errorformat.write_error`: the prefix, the message, the newline.

    Three writes there, two here - aws leads with a blank line this CLI does
    not print (a class-1 rule of the parity normalization, design/testing.md
    section 9). Keeping the newline a write of its own still matters: under a
    stateful codec (`utf_8_sig`, `punycode`) the chunk boundaries are what the
    encoder acts on, and a report that fails to encode must leave the stream
    exactly as aws leaves it.
    """
    prefix = "boto3-s3: [ERROR]:"
    sys.stderr.write(prefix if not detail else f"{prefix} {detail.strip()}")
    sys.stderr.write("\n")


class _ParamValidationArgumentParser(argparse.ArgumentParser):
    """Parse and report failures the aws-cli way: its envelope, its token counts.

    The counterpart of aws's `CLIArgParser`, which every one of its parsers is
    built on: one error shape for all of them, plus - through
    `_match_argument` - the token consumption aws's own interpreter performs
    for the options marked `aws_nargs`. The report is
    ``<message>\\n\\n<usage>`` handed to the error formatter as a single
    string, so the usage lands after a blank line and *inside* the same
    ``[ERROR]`` report. Which usage that is comes from each parser's ``usage``
    argument: the collapsed top-level block for the ones that decide the
    subcommand and for the leaf parse parsers, and argparse's generated
    one-liner for the preliminary ``--profile`` / ``--debug`` scan.
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
        """Reject an off-list value with aws's wording, suggestions included.

        aws overrides the same hook (its `CLIArgParser`), so the report is
        `Found invalid choice '<value>'` - a trailing newline of its own
        included, which is what puts the extra blank line ahead of a usage
        block - followed by the close matches of the choice list, if any.
        """
        if action.choices is None or value in action.choices:
            return
        message = [f"Found invalid choice '{value}'\n"]
        possible = get_close_matches(str(value), [str(c) for c in action.choices], cutoff=0.8)
        if possible:
            message.append("Maybe you meant:\n")
            message.extend(f"  * {word}" for word in possible)
        raise argparse.ArgumentError(action, "\n".join(message))

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
        bundles 3.14, and the marker (``aws_nargs``, the count aws declares -
        read duck-typed for the import reason ``_aws_error_message`` states)
        keeps that outcome off the host Python version.

        Only the count is settled here. Which class a token falls into stays
        argparse's own decision, and up to 3.11 that classification pass also
        raises the ambiguous-abbreviation error - it runs over the whole token
        stream before any consumption, so a value that ambiguously
        abbreviates one of the command's own options (``--exclude --ss``) is
        rejected there while aws, and our 3.12+ runs, take it as the value.
        design/cli.md section 2 records that as the residual it is.

        ``_match_argument`` is private argparse API, the same kind of hook as
        the ``_check_value`` above.
        """
        count = getattr(action, "aws_nargs", None)
        if isinstance(count, int) and re.match(f"[AO]{{{count}}}", arg_strings_pattern):
            return count
        return super()._match_argument(action, arg_strings_pattern)

    def _aws_error_message(self, message: str) -> str:
        """Translate the small argparse wording differences visible in aws-cli."""
        required_prefix = "the following arguments are required: "
        if message.startswith(required_prefix):
            missing = message.removeprefix(required_prefix).split(", ")
            positional_names = {
                str(action.metavar): action.dest
                for action in self._actions
                if not action.option_strings and action.metavar is not None
            }
            return required_prefix + ", ".join(positional_names.get(name, name) for name in missing)
        missing_value = re.fullmatch(r"argument (\S+): expected one argument", message)
        if missing_value is not None:
            option = missing_value.group(1)
            # aws declares the filter options with nargs=1, which words their
            # missing value "expected 1 argument"; the action carries that
            # declaration as a marker (`filters.AppendFilterAction`). Only a
            # count of 1 needs this: argparse itself words every higher count
            # numerically ("expected 2 arguments", aws's wording for
            # `mb --tags`), and "one" is what an uncounted option says. Read
            # the marker duck-typed: importing the filters here would drag in
            # `boto3_s3.globsieve`, and the informational exits may reach no
            # library module beyond the lazy `boto3_s3` root (design/imports.md,
            # pinned by test_import_contract.py).
            if any(
                option in action.option_strings and getattr(action, "aws_nargs", None) == 1
                for action in self._actions
            ):
                return f"argument {option}: expected 1 argument"
        return message

    def error(self, message: str) -> NoReturn:
        report = f"{self._aws_error_message(message)}\n\n{self.format_usage()}"
        _write_error(report, rc=_PARAM_VALIDATION_ERROR_RC)
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

    Only help pages and the auto-prompt completion model are built on it (the
    dispatch parses globals in the top-level pass and the leaf with
    ``_build_command_parse_parser``); it keeps every subcommand's help listing
    the globals, including the recognized-but-ignored group. Suppressing the
    defaults keeps it inert if such a parser ever parses (nothing to clobber).
    """
    shared = _ParamValidationArgumentParser(add_help=False)
    globalargs.add_common_arguments(shared, suppress_defaults=True)
    return shared


def _build_stage1_parser() -> argparse.ArgumentParser:
    """The top-level rendering parser: globals + the subcommand names and help lines.

    No command module is imported here. The stub entries carry only the
    table's name/help, so the top-level help page renders exactly as the full
    tree renders it while the path stays SDK- and command-module-free. The
    dispatch never parses through this parser at all: ``_find_command_token``
    locates the subcommand and a rejected one is reported by
    ``_build_subcommand_error_parser``, so this one only ever prints help.

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
    for name, (_module, _cls, help_text) in _COMMAND_TABLE.items():
        subparsers.add_parser(name, help=help_text, add_help=False)
    return parser


def _build_subcommand_error_parser(
    alias_names: Iterable[str] = (),
) -> _ParamValidationArgumentParser:
    """The parser that reports a subcommand name no table entry matches.

    aws checks the name with a positional whose choices are its command
    table, so the report reads ``argument subcommand: Found invalid choice
    '<name>'`` (argparse names a positional after its dest) and closes with
    the top-level usage block. Only that report is wanted here, so this parser
    carries the positional alone - the globals are long consumed by the
    top-level pass, and the help page is stage 1's job.

    The table aws checks against is the one its alias injector has already
    added to, so alias names join the choices and can be suggested as the near
    miss of a typo (measured: with an ``lsx`` alias defined, ``lsxx`` is
    answered with ``* lsx``).
    """
    parser = _ParamValidationArgumentParser(prog="boto3-s3", add_help=False, usage=_TOP_LEVEL_USAGE)
    extra = [name for name in alias_names if name not in _COMMAND_TABLE]
    parser.add_argument("subcommand", choices=list(_COMMAND_TABLE) + extra)
    return parser


def _silencer(suppress_usage_errors: bool) -> AbstractContextManager[object]:
    """Discard a parser's own usage message when the on-partial trial asks for it."""
    if suppress_usage_errors:
        return contextlib.redirect_stderr(io.StringIO())
    return contextlib.nullcontext()


def _build_first_pass_parser() -> _ParamValidationArgumentParser:
    """aws's preliminary ``--profile`` / ``--debug`` scan, run before everything.

    aws reads those two options off the raw argv with a parser that knows
    nothing else (its ``FirstPassGlobalArgParser``, used while the driver is
    still being constructed) to pick the profile whose config the run loads
    and to switch on debug logging early. Only that scan's *failure* is
    reproduced here: both values are parsed again by the top-level globals
    pass, which is where ours takes them from, so this parse's namespace is
    discarded. Because the scan happens first its failure beats every other
    outcome - ``--version``, the help token, the auto-prompt flag rejection
    (all measured) - and it reports argparse's own two-option usage rather
    than the top-level block. Both options can fail: ``--profile`` by having
    no value, ``--debug`` by being handed one (``--debug=1``, abbreviations
    included).
    """
    parser = _ParamValidationArgumentParser(prog="boto3-s3", add_help=False)
    parser.add_argument("--profile", type=str)
    parser.add_argument("--debug", action="store_true", default=False)
    return parser


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
    options: -h``, and ``s3 ls -h --output bad`` blames ``--output`` because
    this parse runs first). A failure here reports the same top-level usage
    block as a rejected subcommand name, like aws, whose main parser and
    command layers share one usage string.
    """
    parser = _ParamValidationArgumentParser(prog="boto3-s3", add_help=False, usage=_TOP_LEVEL_USAGE)
    globalargs.add_common_arguments(parser)
    return parser


def _build_command_parser(name: str, command: Command) -> argparse.ArgumentParser:
    """The subcommand's full rendering parser: globals section + command args.

    ``prog`` / ``description`` match what ``add_parser`` produces under the
    full tree, so ``boto3-s3 <cmd> help`` pages exactly as the whole tree
    pages it. The dispatch parses with ``_build_command_parse_parser`` and
    only renders help pages through this one; the auto-prompt model keeps
    deriving from the full tree.
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
    --region,s3://b/k``). It carries the same top-level guidance block aws
    builds its ``ArgTableArgParser`` with, so a usage error here closes with
    that block; the command's whole option surface stays on its help page,
    which ``_build_command_parser`` renders.
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

    The normal dispatch no longer calls this (stage 1 + stage 2 above); it
    remains the single source of truth the auto-prompt completion model
    derives from (autoprompt/model.py), which needs every command's options at
    once - so it imports all the command modules, a cost only the interactive
    prompt pays.
    """
    parser = _ParamValidationArgumentParser(
        prog="boto3-s3",
        description="An aws s3-compatible CLI built on the boto3-s3 library.",
        add_help=False,
    )
    globalargs.add_common_arguments(parser)
    shared = _shared_globals_parent()
    subparsers = parser.add_subparsers(dest="command", metavar="<subcommand>", required=True)
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
    from boto3_s3 import set_stream_logger
    from boto3_s3.masking import SecretMaskingFilter

    for name in _DEBUG_LOGGERS:
        # Idempotent, like aws's set_stream_logger (it removes its named
        # handler before re-adding): the on-partial trial dispatch can reach
        # here before the prompt re-dispatches, and the library's
        # boto3-faithful set_stream_logger appends unconditionally - drop the
        # previously attached masking handlers first or every line doubles.
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if any(isinstance(f, SecretMaskingFilter) for f in handler.filters):
                logger.removeHandler(handler)
        set_stream_logger(name, logging.DEBUG, stream=sys.stderr, mask_secrets=True)


@contextmanager
def _debug_handlers_detached() -> Generator[None, None, None]:
    """Detach the ``--debug`` stream handlers while the prompt owns the terminal.

    The on-partial trial dispatch can enable debug logging before its usage
    error falls back to the prompt; live stderr DEBUG handlers would then paint
    over the prompt_toolkit screen (the first ``--region`` / ``--profile``
    completion triggers a boto3 session load, dozens of botocore DEBUG lines).
    aws swaps every logger's handlers into its debug-panel buffer for the
    duration of the app run; there is no panel here, so records emitted during
    the prompt are dropped instead, and the handlers come back for the
    re-dispatch.
    """
    saved: list[tuple[logging.Logger, list[logging.Handler]]] = []
    for name in _DEBUG_LOGGERS:
        logger = logging.getLogger(name)
        if logger.handlers:
            saved.append((logger, logger.handlers[:]))
            logger.handlers.clear()
    try:
        yield
    finally:
        for logger, handlers in saved:
            logger.handlers[:] = handlers


def exit_code_for(exc: Boto3S3Error) -> int:
    """Map a library error to the aws-cli v2 exit code (design/cli.md section 6).

    Server-rejected calls carry the botocore ``ClientError`` as ``__cause__``
    (``boto3_s3.s3storage.s3_errors``) and exit 254 like aws-cli regardless of
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
    path can crash the CLI with a traceback (rc 1), which the exit-code charter
    forbids (design/overview.md section 3) - with two deliberate escapes:
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
    factory, the auto-prompt backend); tests inject a ``Context`` built
    around fakes. Returns the exit code (the deliberate escapes: an
    ``AssertionError`` - an internal bug - and a command-raised
    ``SystemExit``, whose orderly exit is honored) - argparse's ``SystemExit`` is
    absorbed downstream so usage errors map to aws-cli's 252, not argparse's 2,
    and a Ctrl-C mirrors aws's ``InterruptExceptionHandler``: a bare
    newline on stdout and rc 130 (128+SIGINT), never a traceback (the
    auto-prompt UI catches its own interrupt and returns 130 directly).

    ``--cli-auto-prompt`` is resolved here from the raw argv, before argparse, so
    it works without a subcommand, its mutual exclusion with
    ``--no-cli-auto-prompt`` matches aws-cli, and the ``AWS_CLI_AUTO_PROMPT`` env /
    ``cli_auto_prompt`` config / ``on-partial`` chain is honored (option-handling
    section 3, autoprompt.md).
    """
    try:
        return _main(argv, ctx)
    except KeyboardInterrupt:
        with contextlib.suppress(Exception):
            # aws's `InterruptExceptionHandler` writes this newline through
            # the stdout writer its chain built, so the codec applies to it -
            # the one report on this surface that is not stderr's.
            _set_preferred_output_encoding(sys.stdout)
            sys.stdout.write("\n")
        return 130
    except UnicodeError as exc:
        # aws's entry-point handler chain (its `AWSCLIEntryPoint.main`), whose
        # one reachable case here is a report `AWS_CLI_OUTPUT_ENCODING` cannot
        # represent: the codec is strict, so the write raises out of the
        # handler that was making the report and the general handler reports
        # *that* at rc 255 - which is what turns a 252 into a 255 when the
        # message carries a character the codec lacks (measured). The second
        # report is deliberately unguarded: when the codec cannot write it
        # either, aws lets the error escape its entry point as well.
        _write_error(exc, rc=_GENERAL_ERROR_RC)
        return _GENERAL_ERROR_RC


def _main(argv: list[str] | None, ctx: Context | None) -> int:
    """The body of `main` (split out so its Ctrl-C backstop wraps everything)."""
    global _config_scan, _enhanced_envelope
    # Nothing has built a session yet, so every report below the preliminary
    # scan is rendered the enhanced way (aws's entry-point handler chain is
    # constructed without one).
    _enhanced_envelope = True
    if ctx is None:
        ctx = Context()
    raw = list(sys.argv[1:] if argv is None else argv)
    # aws's preliminary --profile / --debug scan of the raw argv, run before
    # the driver even exists, so either of them failing to parse - --profile
    # without a value, --debug handed one - settles the run here, ahead of
    # --version, the help token, the auto-prompt rejection and every parse
    # below (all measured). The parsed values are dropped: the top-level
    # globals pass reads them again. It is outside the on-partial silencing
    # too (aws's silencer is installed on the driver, which this precedes),
    # so the message always reaches stderr.
    try:
        _build_first_pass_parser().parse_known_args(raw)
    except SystemExit:
        return _PARAM_VALIDATION_ERROR_RC
    # aws reads the whole merged config next, while it is still constructing the
    # driver, so a file that is not valid INI settles the run here - ahead of
    # the auto-prompt rejection, --version, the help token and every parse below
    # (all measured), and with botocore's own wording through aws's general
    # handler (255). Only the preliminary scan above outranks it.
    _config_scan = configfiles.scan()
    if _config_scan.unparseable is not None:
        _write_error(
            f"Unable to parse config file: {_config_scan.unparseable}", rc=_GENERAL_ERROR_RC
        )
        return _GENERAL_ERROR_RC
    if resolve.AUTO_PROMPT_FLAG in raw and resolve.NO_AUTO_PROMPT_FLAG in raw:
        _write_error(
            "Both --cli-auto-prompt and --no-cli-auto-prompt cannot be specified at the same time.",
            rc=_PARAM_VALIDATION_ERROR_RC,
        )
        return _PARAM_VALIDATION_ERROR_RC
    try:
        mode = resolve.resolve_auto_prompt_mode(raw, _config_scan)
    except AttributeError as exc:
        # The resolution runs aws's own `config.lower()` over whatever the
        # setting holds, so an indented block - a map after botocore's parse -
        # raises there and aws's entry-point chain reports it at rc 255,
        # ahead of the command it was about to run (measured). Reported here
        # rather than left to `main`'s backstop, which only claims the codec
        # failures of the report streams.
        _write_error(exc, rc=_GENERAL_ERROR_RC)
        return _GENERAL_ERROR_RC
    if mode == "on":
        return _run_auto_prompt(raw, ctx, explicit=resolve.AUTO_PROMPT_FLAG in raw)
    if mode == "on-partial":
        # Run the command as-is; only a usage error (rc 252, which aws-cli and we
        # both raise before any S3 call) falls back to prompting (aws-cli's
        # on-partial branch in clidriver's _do_main). The usage message is silenced on
        # this trial so the prompt isn't buried under it (aws's
        # SilenceParamValidationMsgErrorHandler).
        rc = _dispatch(raw, ctx, suppress_usage_errors=True)
        if rc != _PARAM_VALIDATION_ERROR_RC:
            return rc
        return _run_auto_prompt(raw, ctx, explicit=False)
    return _dispatch(raw, ctx)


def _run_auto_prompt(raw_argv: list[str], ctx: Context, *, explicit: bool) -> int:
    """Run the interactive prompt, then re-dispatch the completed argv once.

    The ``prompt_toolkit`` dependency is opt-in (the ``autoprompt`` extra). When
    it is absent and the prompt was *explicitly* requested (``--cli-auto-prompt``)
    we reject with an install hint and rc 252 (non-contractual; the interactive
    UI is outside the exit-code charter - overview.md section 3 exception 2). When it is
    absent but the prompt was only *config/env-driven*, we fall through to normal
    dispatch instead - a missing optional dep must not break every command. An
    injected ``ctx.auto_prompter`` (tests) bypasses the dependency probe.
    """
    prompter = ctx.auto_prompter
    if prompter is None:
        import importlib.util

        if importlib.util.find_spec("prompt_toolkit") is None:
            if not explicit:
                return _dispatch(raw_argv, ctx)
            _write_error(
                "--cli-auto-prompt requires the optional 'prompt_toolkit' dependency. "
                "Install it with: pip install 'boto3-s3-cli[autoprompt]'"
            )
            return _PARAM_VALIDATION_ERROR_RC
        # Construct inside the try below: a broken/partial prompt_toolkit install
        # (find_spec succeeds but the import or model build fails) must degrade
        # with a message, not escape as a traceback.

    # Seed the prompt with what was typed, minus the auto-prompt flags (they take
    # no value, so a plain filter is exact).
    seed = [a for a in raw_argv if a not in (resolve.AUTO_PROMPT_FLAG, resolve.NO_AUTO_PROMPT_FLAG)]
    try:
        if prompter is None:
            from boto3_s3_cli.autoprompt.prompt import build_default_prompter

            prompter = build_default_prompter()
        with _debug_handlers_detached():
            completed = prompter.prompt_for_args(seed)
    except (KeyboardInterrupt, EOFError):
        return 130
    except Exception as exc:
        _write_error(exc)
        return _GENERAL_ERROR_RC
    # Re-dispatch without prompting again - strip the flags so a re-typed
    # --cli-auto-prompt can't loop.
    completed = [
        a for a in completed if a not in (resolve.AUTO_PROMPT_FLAG, resolve.NO_AUTO_PROMPT_FLAG)
    ]
    return _dispatch(completed, ctx)


def _dispatch(argv: list[str], ctx: Context, *, suppress_usage_errors: bool = False) -> int:
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
    minus the subcommand token into the pre-pass namespace. Once the
    subcommand is determined the SDK may load - the aws-clidriver-shaped lazy
    command table.

    ``suppress_usage_errors`` silences the usage-error output (argparse's usage
    block, ``Unknown options``, and a 252 ``ValidationError``) - used by the
    ``on-partial`` trial run so the fall-back prompt isn't preceded by the error
    the user is about to fix (aws-cli's ``SilenceParamValidationMsgErrorHandler``,
    errorhandler.py:250, injected on the on-partial path in clidriver's ``_do_main``).
    argparse writes its own message inside ``parse_*``, so the
    parses (and only the parses - they are instant, no live output to lose) are
    wrapped to discard it; the command itself still runs with stderr live.
    """
    global _enhanced_envelope
    # aws's CLIDriver.main: from here down its session-backed handler chain does
    # the rendering, and that session already read the profile env vars while it
    # was being constructed. So a profile named there and declared nowhere costs
    # every report below its envelope - the top-level parse and the resolutions
    # right after it included, which a bad --profile (bound further down) leaves
    # alone.
    _enhanced_envelope = _config_scan.declares(configfiles.env_profile())
    # aws reads `~/.aws/cli/alias` while it is still assembling its parser, so
    # a file it cannot read aborts the run before the top-level parse - ahead
    # of `--version`, the help token and a bad global alike (all measured),
    # with only the preliminary scan and the config-file read above it.
    try:
        aliases = alias.load()
    except Boto3S3Error as exc:
        rc = exit_code_for(exc)
        _write_error(exc, rc=rc)
        return rc

    # aws-shaped top-level pass: aws parses the globals over the full argv
    # (MainArgParser.parse_known_args), REMOVES them, and resolves them - the
    # --query compile (252), the --endpoint-url scheme check (252), then the
    # timeout coercions (255, read before connect, aws's registration order) -
    # before ANY command-layer parsing, so those errors beat an invalid
    # choice, unknown options, and missing arguments (measured on the pinned
    # aws-cli). argparse's ``--`` handling does the rest and is identical on
    # every supported Python (enumerated 3.10 vs aws's bundled 3.14): nothing
    # after the first ``--`` is consumed as a global, and the marker stays in
    # the remainder for stage 2's parse to honor - except a leading ``--``,
    # which aws's very first parse uses up against its service token (`aws
    # s3`` always precedes it; measured: ``s3 -- help`` pages while
    # ``s3 --region us-east-2 -- help`` is an invalid choice 'help', and
    # ``s3 -- presign --expires-in 120 s3://b/k`` re-reads the option at the
    # leaf, rc 0). Ours has no service token, so the equivalent is dropping
    # the marker off a ``--``-led argv after the parse. A parse-time --version
    # wins over the resolutions (aws's parser action fires first), and an
    # exactly-['help'] remainder is aws's help-token rule: the top-level help
    # page, rc 0 - globals around the token are already stripped, so
    # `help --region us-east-1` still pages (aws does too). The parse itself
    # settles even earlier: a global that fails to parse, and a parse-time
    # --version, exit during aws's very first parse and beat everything
    # downstream, the invalid-subcommand error included (measured: `s3 bogus
    # --output bad` blames --output, `s3 ls -h --output bad` blames --output
    # rather than the unknown -h, `s3 bogus --version` prints the version).
    # Those two exits are replayed from the capture; falling through to the
    # command scan would blame the subcommand first.
    pre_stdout, pre_stderr = io.StringIO(), io.StringIO()
    try:
        with (
            contextlib.redirect_stdout(pre_stdout),
            contextlib.redirect_stderr(pre_stderr),
        ):
            head, remainder = _build_globals_parser().parse_known_args(argv)
    except SystemExit as exc:
        if not exc.code:
            # --version fired - the globals parser's only zero-exit action
            # (it declares no help option).
            sys.stdout.write(pre_stdout.getvalue())
            return 0
        if not suppress_usage_errors:
            # Replayed onto the real stream, so the codec `_write_error`
            # applies to a live report has to be applied here too - the
            # capture buffer it was written into could not take it.
            _set_preferred_output_encoding(sys.stderr)
            sys.stderr.write(pre_stderr.getvalue())
        return _PARAM_VALIDATION_ERROR_RC
    tokens = remainder[1:] if argv[:1] == ["--"] else remainder
    # Deferred so importing `cli` does not drag the client builders in; the
    # module itself reaches the AWS SDK only from inside its functions, so the
    # import contract of the help token and `--version` still holds.
    from boto3_s3_cli import clientfactory

    try:
        globalargs.validate_query(head)
        clientfactory.validate_endpoint_url(head)
        clientfactory.resolve_cli_timeouts(head)
    except Boto3S3Error as exc:
        rc = exit_code_for(exc)
        if not (suppress_usage_errors and rc == _PARAM_VALIDATION_ERROR_RC):
            _write_error(exc, rc=rc)
        return rc
    except Exception as exc:
        # The same backstop the command below gets: aws resolves these three
        # inside its handler chain, so a raw exception they raise is its
        # general report rather than a traceback (`--endpoint-url
        # 'http://[::1:9000'` - urlsplit rejects the netloc with a bare
        # ValueError - measured as rc 255 on both tools).
        rc = _exit_code_for_unexpected(exc)
        _write_error(exc, rc=rc)
        return rc
    # aws's _handle_top_level_args binds --profile onto the session here, after
    # emitting the event the three resolutions above hang off. The ordering is
    # observable: a bad --profile leaves those errors and the top-level parse
    # enhanced and only degrades the command layers below, where a bad
    # AWS_PROFILE degrades all of them (measured). The truthy guard is aws's, so
    # --profile "" leaves the env chain in force, like `resolve_profile`.
    if head.profile:
        _enhanced_envelope = _config_scan.declares(head.profile)
    # aws validates AWS_CLI_OUTPUT_ENCODING right here: after handling the
    # top-level args and before `session-initialized` (clidriver.main calls
    # compat.validate_preferred_output_encoding between the two), so an
    # unknown codec beats both config gates below, the help token, an invalid
    # subcommand and every leaf error, while the three resolutions above, an
    # unparseable config file and a parse-time --version still win (all
    # measured). The validation is separate from the application on purpose,
    # aws's own split: `_set_preferred_output_encoding` swallows an unknown
    # codec so a report can still be written, and this gate is what makes one
    # impossible to reach with the codec still unknown.
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
    # aws emits `session-initialized` right after binding --profile, and the
    # first handler on it validates `cli_timestamp_format` against the now-bound
    # profile's scoped config. So an unknown value settles the run here: after
    # the three resolutions above (a bad --endpoint-url / --query is still their
    # 252, a bad --cli-read-timeout still its 255) and ahead of everything the
    # command layers decide - the help token, an invalid subcommand, unknown
    # options, missing arguments (all measured). `--version` and the
    # preliminary scan escape it by exiting further up. The on-partial silencer
    # does not cover it: aws silences the 252 family alone.
    invalid_format = _config_scan.invalid_timestamp_format(clientfactory.resolve_profile(head))
    if invalid_format is not None:
        _write_error(
            _UNKNOWN_TIMESTAMP_FORMAT.format(invalid_format),
            rc=_CONFIGURATION_ERROR_RC,
            code="Configuration",
        )
        return _CONFIGURATION_ERROR_RC
    # aws's next `session-initialized` handler resolves `cli_binary_format`
    # (its binary-format customization), so a bad value in the bound profile
    # settles the run here - after the timestamp gate above (measured: with
    # both broken, the timestamp report wins) and ahead of the help token and
    # every command layer. An explicit --cli-binary-format never consults the
    # config (argparse restricted it to the valid choices already), and the
    # bad value renders as aws's bare KeyError - its repr through the general
    # rc-255 handler, no envelope.
    if head.cli_binary_format is None:
        invalid_binary = _config_scan.invalid_binary_format(clientfactory.resolve_profile(head))
        if invalid_binary is not None:
            # An indented block parses to a map, which aws's table lookup
            # rejects as an unhashable key - its official build's (Python
            # 3.14) TypeError text, pinned across hosts like the argparse
            # corners; a string renders as the KeyError's repr.
            message = (
                "cannot use 'dict' as a dict key (unhashable type: 'dict')"
                if isinstance(invalid_binary, dict)
                else f"{invalid_binary!r}"
            )
            _write_error(message, rc=_GENERAL_ERROR_RC)
            return _GENERAL_ERROR_RC
    try:
        return _resolve_command(
            tokens, head, ctx, aliases, suppress_usage_errors=suppress_usage_errors
        )
    except Boto3S3Error as exc:
        # The alias layer's own failures (an unparseable value, a global it
        # refuses); a command's are reported inside `_run_command`.
        rc = exit_code_for(exc)
        if not (suppress_usage_errors and rc == _PARAM_VALIDATION_ERROR_RC):
            _write_error(exc, rc=rc)
        return rc
    except RecursionError:
        # An alias that expands to itself re-enters until the interpreter's
        # stack runs out; aws reaches the same wall (its alias command calls
        # back into the command it was injected into) and reports it at rc 255.
        # Caught here, where the stack has fully unwound, so the report itself
        # cannot hit the wall again. The text is written rather than taken from
        # the exception because Python worded this failure two ways before 3.12
        # ("... while calling a Python object" when the limit is reached
        # entering a C-level call); aws's official build carries the settled
        # wording, and pinning it keeps every supported host on that answer -
        # the same pin the argparse corners use (design/cli.md section 2).
        _write_error(_RECURSION_LIMIT_REPORT, rc=_GENERAL_ERROR_RC)
        return _GENERAL_ERROR_RC


def _resolve_command(
    tokens: list[str],
    head: argparse.Namespace,
    ctx: Context,
    aliases: alias.AliasTable,
    *,
    suppress_usage_errors: bool,
) -> int:
    """Pick the subcommand out of the globals-free token stream and run it.

    aws's ``s3`` command does this: the help token first, then the first
    positional-looking token as the subcommand name, looked up in a table its
    alias injector has already added the ``[command s3]`` entries to - so an
    alias name resolves here, and one that repeats a built-in name shadows it.
    An internal alias re-enters this same resolution with its expansion ahead
    of the user's arguments, which is what makes alias-to-alias chains work.
    """
    if tokens == ["help"]:
        _build_stage1_parser().print_help()
        return 0

    index = _find_command_token(tokens)
    if index < 0:
        # No subcommand token at all. aws reports the unconsumed options first
        # (measured: `s3 --bogus` -> `Unknown options: --bogus`, not the
        # missing-subcommand usage error), with the customizations command
        # layer's wording (awscli customizations/commands.py joins with ","
        # and NO space - verified against the pinned aws-cli, unlike the
        # top-level clidriver.py which uses ", "), prefixed like aws's error
        # handler (errorformat.py "<prog>: [ERROR]: <msg>"). With nothing left
        # to report it is aws's bare missing-subcommand usage error, which
        # carries no help blurb (measured: `s3` and `s3 --`).
        leftovers = [token for token in tokens if token != "--"]
        if not suppress_usage_errors:
            message = f"Unknown options: {','.join(leftovers)}" if leftovers else _TOO_FEW_ARGUMENTS
            _write_error(message, rc=_PARAM_VALIDATION_ERROR_RC)
        return _PARAM_VALIDATION_ERROR_RC
    name = tokens[index]
    # aws's subcommand extraction (SubCommandArgParser._remove_subcommand):
    # only the matched token leaves the stream; an option-like token ahead of
    # it - `s3 --expires-in=120 presign s3://b/k` parses, measured - and
    # everything behind it flow to the leaf parser in their original order,
    # where an unknown one is rejected with the leaf's own wording.
    stage2_tokens = tokens[:index] + tokens[index + 1 :]
    value = aliases.entries.get(name)
    if value is not None:
        return _run_alias(
            name,
            value,
            stage2_tokens,
            head,
            ctx,
            aliases,
            suppress_usage_errors=suppress_usage_errors,
        )
    if name not in _COMMAND_TABLE:
        # Report the rejected name through the dedicated parser so argparse
        # words it exactly as aws's command-table positional does (it writes
        # the message itself; its exit 2 remaps per the charter). The parse
        # always errors - the token is known not to be in the table. It is fed
        # behind a `--`: the scan reaches option-form tokens too (behind a
        # `--` of their own, they are data), and without the marker argparse
        # would read one back as an option and report a missing subcommand
        # instead of the name aws blames (`--region us-east-1 -- --bogus`).
        with contextlib.suppress(SystemExit), _silencer(suppress_usage_errors):
            _build_subcommand_error_parser(aliases.entries).parse_args(["--", name])
        return _PARAM_VALIDATION_ERROR_RC
    return _run_command(
        name, stage2_tokens, head, ctx, aliases, suppress_usage_errors=suppress_usage_errors
    )


def _run_alias(
    name: str,
    value: str,
    arguments: list[str],
    head: argparse.Namespace,
    ctx: Context,
    aliases: alias.AliasTable,
    *,
    suppress_usage_errors: bool,
) -> int:
    """Run one ``[command s3]`` alias over the arguments that followed its name.

    An external alias (``!``-led) is a shell command line and its status is the
    run's. An internal one expands to CLI arguments, which aws hands to the
    *main* parser first so any global option in the value is taken out and
    applied - overriding what the user typed, since aws copies the alias's
    values onto the already-parsed globals (measured: an alias's ``--region``
    beats one on the command line). What the main parser leaves goes back
    through the command resolution ahead of the user's own arguments.

    An alias that repeats a built-in name proxies to that built-in instead of
    re-resolving, and aws drops the expansion's first token when it does -
    which is what makes `ls = ls --recursive` work, and what makes an
    expansion that names something else (``ls = cp``) still run ``ls``.
    """
    if alias.is_external(value):
        return alias.run_external(value, arguments)
    expanded = alias.split_value(name, value)
    # Same capture-and-replay as the top-level pass: a global in the value can
    # fail to parse (rc 252, silenced on the on-partial trial like any other
    # usage error) or be a `--version` that prints and exits 0 (measured).
    pre_stdout, pre_stderr = io.StringIO(), io.StringIO()
    parser = _build_globals_parser()
    try:
        with (
            contextlib.redirect_stdout(pre_stdout),
            contextlib.redirect_stderr(pre_stderr),
        ):
            parsed, remainder = parser.parse_known_args(expanded)
    except SystemExit as exc:
        if not exc.code:
            sys.stdout.write(pre_stdout.getvalue())
            return 0
        if not suppress_usage_errors:
            _set_preferred_output_encoding(sys.stderr)
            sys.stderr.write(pre_stderr.getvalue())
        return _PARAM_VALIDATION_ERROR_RC
    _apply_alias_globals(name, parser, parsed, head)
    tokens = remainder + arguments
    if name in _COMMAND_TABLE:
        return _run_command(
            name, tokens[1:], head, ctx, aliases, suppress_usage_errors=suppress_usage_errors
        )
    return _resolve_command(tokens, head, ctx, aliases, suppress_usage_errors=suppress_usage_errors)


def _apply_alias_globals(
    name: str,
    parser: argparse.ArgumentParser,
    parsed: argparse.Namespace,
    head: argparse.Namespace,
) -> None:
    """Move the global options an alias value carried onto the run's globals.

    aws decides what the value carried by comparing the alias's parse against
    the parser's defaults, and refuses two of them outright (``--debug`` and
    ``--profile``, whose effects are settled before an alias can be reached) -
    checked in aws's own order, so a value carrying both blames ``--debug``.
    The values it keeps then go through the same resolutions the top-level
    pass runs, which is where a bad ``--endpoint-url`` or ``--query`` in an
    alias is rejected, before they are copied over the user's.
    """
    from boto3_s3_cli import clientfactory

    updates = [dest for dest, value in vars(parsed).items() if parser.get_default(dest) != value]
    for unsupported in ("debug", "profile"):
        if unsupported in updates:
            raise InvalidConfigError(
                f'Global parameter "--{unsupported}" detected in alias "{name}" '
                "which is not supported in subcommand aliases."
            )
    globalargs.validate_query(parsed)
    clientfactory.validate_endpoint_url(parsed)
    clientfactory.resolve_cli_timeouts(parsed)
    for dest in updates:
        setattr(head, dest, getattr(parsed, dest))


def _run_command(
    name: str,
    stage2_tokens: list[str],
    head: argparse.Namespace,
    ctx: Context,
    aliases: alias.AliasTable,
    *,
    suppress_usage_errors: bool,
) -> int:
    """Stage 2: build the matched subcommand's parser, parse, and run it."""
    if name in aliases.leaves:
        # A `[command s3 <name>]` section is aws's own crash, not a usable
        # alias: injecting into a leaf's table breaks the parser it then
        # builds, for every invocation of that subcommand including its help
        # (measured, rc 255). It settles the run before the parse, as there.
        _write_error(alias.LEAF_SECTION_REPORT, rc=_GENERAL_ERROR_RC)
        return _GENERAL_ERROR_RC
    command = _load_command(name)()
    if stage2_tokens == ["help"]:
        # aws's help-token rule at the subcommand level (its ArgTableArgParser
        # special-cases an exactly-['help'] remainder): the command's help
        # page, rc 0 - even where a normal parse would fail or run a listing.
        # The globals are already stripped, so a `help` wrapped in globals
        # still pages (aws: `s3 presign help --region us-east-1` pages).
        _build_command_parser(name, command).print_help()
        return 0
    # The top-level pass's namespace carries every parsed global (either side
    # of the subcommand - all consumed there) plus their defaults; the leaf
    # parse fills in the command's own arguments.
    head.command = name
    try:
        with _silencer(suppress_usage_errors):
            args, extras = _build_command_parse_parser(name, command).parse_known_args(
                stage2_tokens, namespace=head
            )
    except SystemExit as exc:
        return 0 if not exc.code else _PARAM_VALIDATION_ERROR_RC
    if extras:
        # aws-cli wording again ("," with no space) - exercised by the ported
        # test_errors_out_with_extra_arguments.
        if not suppress_usage_errors:
            _write_error(f"Unknown options: {','.join(extras)}", rc=_PARAM_VALIDATION_ERROR_RC)
        return _PARAM_VALIDATION_ERROR_RC

    if getattr(args, "debug", False):
        _enable_debug_logging()

    try:
        return command.run(args, ctx)
    except Boto3S3Error as exc:
        rc = exit_code_for(exc)
        if not (suppress_usage_errors and rc == _PARAM_VALIDATION_ERROR_RC):
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
        # (the binding exit-code charter, design/overview.md section 3).
        # KeyboardInterrupt / SystemExit are BaseException, not Exception, so
        # they still propagate - a Ctrl-C reaches main's aws-shaped backstop
        # (a bare newline + rc 130, no traceback).
        rc = _exit_code_for_unexpected(exc)
        _write_error(exc, rc=rc)
        return rc
