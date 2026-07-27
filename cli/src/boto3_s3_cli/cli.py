"""Entry point and dispatch for the ``boto3-s3`` (``aws s3``-compatible) CLI."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import logging
import re
import sys
from collections.abc import Generator
from contextlib import contextmanager
from difflib import get_close_matches
from typing import NoReturn, cast

from boto3_s3 import (
    Boto3S3Error,
    ConfigurationError,
    InvalidConfigError,
    InvalidValueError,
    ValidationError,
)
from boto3_s3_cli import globalargs
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

# argparse's own negative-number shape: a token matching it is classified as a
# positional (none of our parsers register numeric option strings), so the
# subcommand scan must not skip it as an option.
_NEGATIVE_NUMBER_RE = re.compile(r"^-\d+$|^-\d*\.\d+$")

# aws-cli's top-level usage block (its argparser.py USAGE + HELP_BLURB),
# collapsed onto our flatter hierarchy: boto3-s3 IS `aws s3`, so what aws calls
# the subcommand is our only level - aws's `<command> <subcommand>` pair and
# its third help line have no counterpart here. Every parse error renders it,
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


def _write_error(message: object, *, rc: int | None = None) -> None:
    """Write one CLI error, adding the enhanced envelope required by `rc`.

    The message is stripped first, like aws's error formatter: the multi-line
    reports assembled below end with the newline their usage block carries, and
    it must not surface as a trailing blank line.
    """
    detail = str(message).strip()
    if rc == _PARAM_VALIDATION_ERROR_RC:
        detail = f"An error occurred (ParamValidation): {detail}"
    sys.stderr.write(f"boto3-s3: [ERROR]: {detail}\n")


class _ParamValidationArgumentParser(argparse.ArgumentParser):
    """Render argparse failures through aws-cli's ParamValidation envelope.

    The counterpart of aws's `CLIArgParser`, which every one of its parsers is
    built on: one error shape for all of them. The report is
    ``<message>\\n\\n<usage>`` handed to the error formatter as a single
    string, so the usage lands after a blank line and *inside* the same
    ``[ERROR]`` report. Which usage that is comes from each parser's ``usage``
    argument: the collapsed top-level block for the ones that decide the
    subcommand and for the leaf parse parsers, and argparse's generated
    one-liner for the preliminary ``--profile`` / ``--debug`` scan.
    """

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
            # declaration as a marker (`filters.AppendFilterAction`). Read it
            # duck-typed: importing the filters here would drag in
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
    dash-led, a lone ``-``, a negative number, a token with a space, or
    anything after ``--``.

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


def _build_subcommand_error_parser() -> _ParamValidationArgumentParser:
    """The parser that reports a subcommand name no table entry matches.

    aws checks the name with a positional whose choices are its command
    table, so the report reads ``argument subcommand: Found invalid choice
    '<name>'`` (argparse names a positional after its dest) and closes with
    the top-level usage block. Only that report is wanted here, so this parser
    carries the positional alone - the globals are long consumed by the
    top-level pass, and the help page is stage 1's job.
    """
    parser = _ParamValidationArgumentParser(prog="boto3-s3", add_help=False, usage=_TOP_LEVEL_USAGE)
    parser.add_argument("subcommand", choices=list(_COMMAND_TABLE))
    return parser


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
            sys.stdout.write("\n")
        return 130


def _main(argv: list[str] | None, ctx: Context | None) -> int:
    """The body of `main` (split out so its Ctrl-C backstop wraps everything)."""
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
    if resolve.AUTO_PROMPT_FLAG in raw and resolve.NO_AUTO_PROMPT_FLAG in raw:
        _write_error(
            "Both --cli-auto-prompt and --no-cli-auto-prompt cannot be specified at the same time.",
            rc=_PARAM_VALIDATION_ERROR_RC,
        )
        return _PARAM_VALIDATION_ERROR_RC
    mode = resolve.resolve_auto_prompt_mode(raw)
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
    silencer = (
        contextlib.redirect_stderr(io.StringIO())
        if suppress_usage_errors
        else contextlib.nullcontext()
    )

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
    if tokens[index] not in _COMMAND_TABLE:
        # Report the rejected name through the dedicated parser so argparse
        # words it exactly as aws's command-table positional does (it writes
        # the message itself; its exit 2 remaps per the charter). The parse
        # always errors - the token is known not to be in the table. It is fed
        # behind a `--`: the scan reaches option-form tokens too (behind a
        # `--` of their own, they are data), and without the marker argparse
        # would read one back as an option and report a missing subcommand
        # instead of the name aws blames (`--region us-east-1 -- --bogus`).
        with contextlib.suppress(SystemExit), silencer:
            _build_subcommand_error_parser().parse_args(["--", tokens[index]])
        return _PARAM_VALIDATION_ERROR_RC

    name = tokens[index]
    # aws's subcommand extraction (SubCommandArgParser._remove_subcommand):
    # only the matched token leaves the stream; an option-like token ahead of
    # it - `s3 --expires-in=120 presign s3://b/k` parses, measured - and
    # everything behind it flow to the leaf parser in their original order,
    # where an unknown one is rejected with the leaf's own wording.
    stage2_tokens = tokens[:index] + tokens[index + 1 :]
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
        with silencer:
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
    except BrokenPipeError:
        return 0
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
