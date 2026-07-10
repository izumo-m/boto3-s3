"""Entry point and dispatch for the ``boto3-s3`` (``aws s3``-compatible) CLI."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
import logging
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

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
# docs/masking.md). The library attaches no handler on import. "boto3_s3_cli"
# is the counterpart of aws-cli's own "awscli" logger (clidriver._set_logging),
# so the CLI's own debug lines (runtimeconfig's alias resolution) surface too.
# urllib3 is deliberately omitted: it logs no credentials, only
# connection-pool noise.
_DEBUG_LOGGERS = ("boto3_s3", "boto3_s3_cli", "botocore", "boto3", "s3transfer")

# aws-cli v2 exit-code conventions (awscli/constants.py). The
# exit-code charter (docs/overview.md section 3) requires matching them; see
# docs/cli.md section 6 for the full table.
_PARAM_VALIDATION_ERROR_RC = 252
_CONFIGURATION_ERROR_RC = 253
_CLIENT_ERROR_RC = 254
_GENERAL_ERROR_RC = 255

# Every wired subcommand: name -> (defining module, class name, one-line help).
# Registering here is the only wiring step. The table is the single source for
# stage 1 of the dispatch (names + help lines, rendered WITHOUT importing any
# command module - the lazy-dispatch contract, docs/imports.md) and for stage 2
# (only the matched module is imported). The help text is duplicated from each
# class's `help` ClassVar on purpose - stage 1 must render `--help` without the
# class - and test_command_table.py pins the two against drift.
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

# The stage-1 namespace slot that carries everything after the subcommand
# token, verbatim, into stage 2's real parse.
_REST_DEST = "stage2_argv"


if TYPE_CHECKING:
    # Generic only in typeshed; at runtime the class is not subscriptable.
    _SubParsersActionBase = argparse._SubParsersAction[argparse.ArgumentParser]  # pyright: ignore[reportPrivateUsage]
else:
    _SubParsersActionBase = argparse._SubParsersAction


class _Stage1CommandAction(_SubParsersActionBase):
    """Match the subcommand and capture its remainder verbatim (stage 1).

    The stock subparsers action hands the remainder to the named sub parser to
    parse; stage 1 must not interpret it at all - an option there belongs to
    the real command parser stage 2 builds, while an unknown option *before*
    the subcommand must stay a top-level extra (aws rejects it at the top
    level; ``argparse.REMAINDER`` cannot do this - it refuses to start on an
    option-like token). Overriding only ``__call__`` keeps everything else the
    parent provides: the PARSER-nargs greedy match, the invalid-choice /
    missing-command errors (the parser raises them before the action runs),
    and the ``--help`` listing ``add_parser`` feeds.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        assert isinstance(values, list)  # nargs=PARSER always yields a list
        setattr(namespace, self.dest, str(values[0]))
        setattr(namespace, _REST_DEST, [str(v) for v in values[1:]])


def _load_command(name: str) -> type[Command]:
    """Import the matched subcommand's module and return its class (stage 2)."""
    module_name, class_name, _help = _COMMAND_TABLE[name]
    return cast("type[Command]", getattr(importlib.import_module(module_name), class_name))


def _shared_globals_parent() -> argparse.ArgumentParser:
    """The suppressed-defaults globals parent every subcommand parser takes.

    Suppressing the defaults stops an unspecified flag from clobbering a value
    parsed *before* the subcommand, so a global may sit on either side -
    ``boto3-s3 --profile foo ls s3://b`` and ``boto3-s3 ls s3://b --profile
    foo`` both work (matching aws-cli).
    """
    shared = argparse.ArgumentParser(add_help=False)
    globalargs.add_common_arguments(shared, suppress_defaults=True)
    return shared


def _build_stage1_parser() -> argparse.ArgumentParser:
    """The pre-determination parser: globals + the subcommand names and help lines.

    No command module is imported here. The stub entries carry only the
    table's name/help, so top-level ``--help`` / ``--version`` and the stage-1
    usage errors (missing subcommand, invalid choice - argparse's wording,
    remapped to 252) render exactly as the full tree renders them while the
    path stays SDK- and command-module-free. The stubs are never parsed:
    ``_Stage1CommandAction`` records the matched name and the verbatim
    remainder for stage 2 instead.
    """
    parser = argparse.ArgumentParser(
        prog="boto3-s3", description="An aws s3-compatible CLI built on the boto3-s3 library."
    )
    globalargs.add_common_arguments(parser)
    subparsers = parser.add_subparsers(
        dest="command", metavar="<command>", required=True, action=_Stage1CommandAction
    )
    for name, (_module, _cls, help_text) in _COMMAND_TABLE.items():
        subparsers.add_parser(name, help=help_text, add_help=False)
    return parser


def _build_command_parser(name: str, command: Command) -> argparse.ArgumentParser:
    """The determined subcommand's real parser (stage 2).

    ``prog`` / ``description`` match what ``add_parser`` produces under the
    full tree, so ``boto3-s3 <cmd> --help`` and the subcommand's usage errors
    render identically to the pre-split output.
    """
    parser = argparse.ArgumentParser(
        prog=f"boto3-s3 {name}",
        description=type(command).help,
        parents=[_shared_globals_parent()],
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
    parser = argparse.ArgumentParser(
        prog="boto3-s3", description="An aws s3-compatible CLI built on the boto3-s3 library."
    )
    globalargs.add_common_arguments(parser)
    shared = _shared_globals_parent()
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    for name in _COMMAND_TABLE:
        command_cls = _load_command(name)
        command_cls().configure(
            subparsers.add_parser(
                name,
                parents=[shared],
                help=command_cls.help,
                description=command_cls.help,
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


def exit_code_for(exc: Boto3S3Error) -> int:
    """Map a library error to the aws-cli v2 exit code (docs/cli.md section 6).

    Server-rejected calls carry the botocore ``ClientError`` as ``__cause__``
    (``boto3_s3.s3storage.s3_errors``) and exit 254 like aws-cli regardless of
    the library category - aws-cli treats every error that reached the server
    as a client error, even ones our taxonomy files under ``ValidationError``.
    ``InvalidValueError`` / ``InvalidConfigError`` refine their parents back to
    the general 255: aws routes those failures (a post-parse ``int()``, a bad
    ``[s3]`` value, an unusable profile) through its general handler, not the
    dedicated 252 / 253 ones.
    """
    # Deferred so the parse-only paths never load botocore (import contract,
    # docs/imports.md): when a ClientError cause can exist botocore is already
    # loaded, so this re-import is free on the paths that matter.
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
    """Map a non-``Boto3S3Error`` exception escaping a command to aws-cli's rc.

    Mirrors aws-cli's error-handler chain for exceptions that reach the entry
    point (errorhandler.py): a botocore credential / region resolution failure
    is 253, a ``ClientError`` is 254, everything else is the general 255
    (``GeneralExceptionHandler``). The common paths are already translated into
    ``Boto3S3Error`` (the library's ``s3_errors`` and the CLI's ``build_client``);
    this is the catch-all so no path can crash the CLI with a traceback (rc 1),
    which the exit-code charter forbids (docs/overview.md section 3).
    """
    # Deferred: botocore is already loaded once a command has run far enough to
    # raise one of these (import contract, docs/imports.md).
    from botocore.exceptions import ClientError, NoCredentialsError, NoRegionError

    # Only NoCredentials / NoRegion are 253 (aws errorhandler.py dedicated
    # handlers). PartialCredentialsError has no aws handler ->
    # GeneralExceptionHandler -> 255, so it must fall through here, not map to 253.
    if isinstance(exc, (NoCredentialsError, NoRegionError)):
        return _CONFIGURATION_ERROR_RC
    if isinstance(exc, ClientError):
        return _CLIENT_ERROR_RC
    return _GENERAL_ERROR_RC


def main(argv: list[str] | None = None, *, ctx: Context | None = None) -> int:
    """Parse ``argv``, dispatch to the requested subcommand, and return its exit code.

    *ctx* carries the runtime dependencies the command resolves (the S3 client
    factory, the auto-prompt backend); tests inject a ``Context`` built
    around fakes. Always returns the exit code - argparse's ``SystemExit`` is
    absorbed downstream so usage errors map to aws-cli's 252, not argparse's 2.

    ``--cli-auto-prompt`` is resolved here from the raw argv, before argparse, so
    it works without a subcommand, its mutual exclusion with
    ``--no-cli-auto-prompt`` matches aws-cli, and the ``AWS_CLI_AUTO_PROMPT`` env /
    ``cli_auto_prompt`` config / ``on-partial`` chain is honored (option-handling
    section 3, autoprompt.md).
    """
    if ctx is None:
        ctx = Context()
    raw = list(sys.argv[1:] if argv is None else argv)
    if resolve.AUTO_PROMPT_FLAG in raw and resolve.NO_AUTO_PROMPT_FLAG in raw:
        sys.stderr.write(
            "boto3-s3: [ERROR]: Both --cli-auto-prompt and --no-cli-auto-prompt "
            "cannot be specified at the same time.\n"
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
            sys.stderr.write(
                "boto3-s3: [ERROR]: --cli-auto-prompt requires the optional 'prompt_toolkit' "
                "dependency. Install it with: pip install 'boto3-s3-cli[autoprompt]'\n"
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
        completed = prompter.prompt_for_args(seed)
    except (KeyboardInterrupt, EOFError):
        return 130
    except Exception as exc:
        sys.stderr.write(f"boto3-s3: [ERROR]: {exc}\n")
        return _GENERAL_ERROR_RC
    # Re-dispatch without prompting again - strip the flags so a re-typed
    # --cli-auto-prompt can't loop.
    completed = [
        a for a in completed if a not in (resolve.AUTO_PROMPT_FLAG, resolve.NO_AUTO_PROMPT_FLAG)
    ]
    return _dispatch(completed, ctx)


def _dispatch(argv: list[str], ctx: Context, *, suppress_usage_errors: bool = False) -> int:
    """Parse ``argv`` in two stages and run the matched subcommand.

    Stage 1 reads the globals and the subcommand name off the stub tree - no
    command module is imported, so ``--help`` / ``--version`` and the
    stage-1 usage errors stay SDK-free (import contract, docs/imports.md).
    Stage 2 imports just the matched command's module, builds its real parser,
    and parses the stub-captured remainder into the stage-1 namespace (the
    suppressed-defaults parent keeps pre-subcommand globals intact). Once the
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
    try:
        with silencer:
            args, extras = _build_stage1_parser().parse_known_args(argv)
    except SystemExit as exc:
        # argparse already wrote its message (--help/--version exit 0; usage
        # errors such as an invalid choice exit 2 -> remap per the charter).
        return 0 if not exc.code else _PARAM_VALIDATION_ERROR_RC
    if extras:
        # Unknown options *before* the subcommand: rejected at the top level
        # like aws, never handed to a command parser that may define the same
        # flag. boto3-s3's top command is `aws s3`-equivalent, so this matches the
        # customizations command layer's wording (awscli customizations/commands.py
        # joins with "," and NO space - verified against real aws 2.35.18, unlike the
        # top-level clidriver.py which uses ", "), prefixed like aws's error handler
        # (errorformat.py "<prog>: [ERROR]: <msg>").
        if not suppress_usage_errors:
            sys.stderr.write(f"boto3-s3: [ERROR]: Unknown options: {','.join(extras)}\n")
        return _PARAM_VALIDATION_ERROR_RC

    rest: list[str] = getattr(args, _REST_DEST, None) or []
    if hasattr(args, _REST_DEST):
        delattr(args, _REST_DEST)
    command = _load_command(args.command)()
    try:
        with silencer:
            args, extras = _build_command_parser(args.command, command).parse_known_args(
                rest, namespace=args
            )
    except SystemExit as exc:
        return 0 if not exc.code else _PARAM_VALIDATION_ERROR_RC
    if extras:
        # aws-cli wording again ("," with no space) - exercised by the ported
        # test_errors_out_with_extra_arguments.
        if not suppress_usage_errors:
            sys.stderr.write(f"boto3-s3: [ERROR]: Unknown options: {','.join(extras)}\n")
        return _PARAM_VALIDATION_ERROR_RC

    if getattr(args, "debug", False):
        _enable_debug_logging()

    try:
        return command.run(args, ctx)
    except Boto3S3Error as exc:
        rc = exit_code_for(exc)
        if not (suppress_usage_errors and rc == _PARAM_VALIDATION_ERROR_RC):
            sys.stderr.write(f"boto3-s3: [ERROR]: {exc}\n")
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
        # (the binding exit-code charter, docs/overview.md section 3).
        # KeyboardInterrupt / SystemExit are BaseException, not Exception, so
        # they still propagate (Ctrl-C keeps its default rc 130).
        sys.stderr.write(f"boto3-s3: [ERROR]: {exc}\n")
        return _exit_code_for_unexpected(exc)
