"""Entry point and dispatch for the ``boto3-s3`` (``aws s3``-compatible) CLI."""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import sys

from boto3_s3 import Boto3S3Error, ConfigurationError, ValidationError
from boto3_s3_cli import globalargs
from boto3_s3_cli.autoprompt import resolve
from boto3_s3_cli.commands.base import Command, Context
from boto3_s3_cli.commands.cp import CpCommand
from boto3_s3_cli.commands.ls import LsCommand
from boto3_s3_cli.commands.mb import MbCommand
from boto3_s3_cli.commands.mv import MvCommand
from boto3_s3_cli.commands.presign import PresignCommand
from boto3_s3_cli.commands.rb import RbCommand
from boto3_s3_cli.commands.rm import RmCommand
from boto3_s3_cli.commands.sync import SyncCommand
from boto3_s3_cli.commands.website import WebsiteCommand

# Loggers a masked stderr handler is attached to under --debug, via the
# library's boto3-faithful set_stream_logger (credential masking on by default -
# docs/masking.md). The library attaches no handler on import (NullHandler
# discipline). urllib3 is deliberately omitted: it logs no credentials, only
# connection-pool noise.
_DEBUG_LOGGERS = ("boto3_s3", "botocore", "boto3", "s3transfer")

# aws-cli v2 exit-code conventions (awscli/constants.py). The
# exit-code charter (docs/overview.md section 3) requires matching them; see
# docs/cli.md section 6 for the full table.
_PARAM_VALIDATION_ERROR_RC = 252
_CONFIGURATION_ERROR_RC = 253
_CLIENT_ERROR_RC = 254
_GENERAL_ERROR_RC = 255

# Every wired subcommand. Registering the class here is the only wiring step; a
# fresh instance is created per parser build and per dispatch (commands/base.py).
_COMMANDS: tuple[type[Command], ...] = (
    CpCommand,
    LsCommand,
    MbCommand,
    MvCommand,
    PresignCommand,
    RbCommand,
    RmCommand,
    SyncCommand,
    WebsiteCommand,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level ``boto3-s3`` parser with one subparser per subcommand.

    Globals are registered on the top-level parser (real defaults) and again on a
    shared parent for the subparsers (suppressed defaults), so they work before or
    after the subcommand without the subcommand clobbering them.
    """
    parser = argparse.ArgumentParser(
        prog="boto3-s3", description="An aws s3-compatible CLI built on the boto3-s3 library."
    )
    globalargs.add_common_arguments(parser)
    shared = argparse.ArgumentParser(add_help=False)
    globalargs.add_common_arguments(shared, suppress_defaults=True)
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    for command_cls in _COMMANDS:
        command_cls().configure(
            subparsers.add_parser(
                command_cls.name,
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

    for name in _DEBUG_LOGGERS:
        set_stream_logger(name, logging.DEBUG, stream=sys.stderr, mask_secrets=True)


def exit_code_for(exc: Boto3S3Error) -> int:
    """Map a library error to the aws-cli v2 exit code (docs/cli.md section 6).

    Server-rejected calls carry the botocore ``ClientError`` as ``__cause__``
    (``boto3_s3.s3storage.s3_errors``) and exit 254 like aws-cli regardless of
    the library category - aws-cli treats every error that reached the server
    as a client error, even ones our taxonomy files under ``ValidationError``.
    """
    # Deferred so the parse-only paths never load botocore (import contract,
    # docs/imports.md): when a ClientError cause can exist botocore is already
    # loaded, so this re-import is free on the paths that matter.
    from botocore.exceptions import ClientError

    if isinstance(exc.__cause__, ClientError):
        return _CLIENT_ERROR_RC
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
    # handlers). PartialCredentialsError has no aws handler -> GeneralException
    # -> 255, so it must fall through here, not map to 253.
    if isinstance(exc, (NoCredentialsError, NoRegionError)):
        return _CONFIGURATION_ERROR_RC
    if isinstance(exc, ClientError):
        return _CLIENT_ERROR_RC
    return _GENERAL_ERROR_RC


def main(argv: list[str] | None = None, *, ctx: Context | None = None) -> int:
    """Parse ``argv``, dispatch to the requested subcommand, and return its exit code.

    *ctx* carries the runtime dependencies the command resolves (the S3 client
    factory, the auto-prompt backend); tests inject a :class:`Context` built
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
        # on-partial branch, clidriver.py:277). The usage message is silenced on
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


def _dispatch(argv: list[str] | None, ctx: Context, *, suppress_usage_errors: bool = False) -> int:
    """Parse ``argv`` with argparse and run the matched subcommand.

    ``suppress_usage_errors`` silences the usage-error output (argparse's usage
    block, ``Unknown options``, and a 252 ``ValidationError``) - used by the
    ``on-partial`` trial run so the fall-back prompt isn't preceded by the error
    the user is about to fix (aws-cli's ``SilenceParamValidationMsgErrorHandler``,
    errorhandler.py:250, injected on the on-partial path at clidriver.py:281).
    argparse writes its own message inside ``parse_*``, so the
    parse (and only the parse - it is instant, no live output to lose) is wrapped
    to discard it; the command itself still runs with stderr live.
    """
    parser = build_parser()
    try:
        if suppress_usage_errors:
            with contextlib.redirect_stderr(io.StringIO()):
                args, extras = parser.parse_known_args(argv)
        else:
            args, extras = parser.parse_known_args(argv)
    except SystemExit as exc:
        # argparse already wrote its message (--help/--version exit 0; usage
        # errors such as an invalid choice exit 2 -> remap per the charter).
        return 0 if not exc.code else _PARAM_VALIDATION_ERROR_RC
    if extras:
        # aws-cli wording (UnknownArgumentError, defined in awscli/arguments.py and
        # raised with "Unknown options: %s" in awscli/clidriver.py), prefixed like
        # aws's error handler (errorformat.py "<prog>: [ERROR]: <msg>"). Exercised
        # by the ported test_errors_out_with_extra_arguments.
        if not suppress_usage_errors:
            sys.stderr.write(f"boto3-s3: [ERROR]: Unknown options: {', '.join(extras)}\n")
        return _PARAM_VALIDATION_ERROR_RC

    if getattr(args, "debug", False):
        _enable_debug_logging()

    # args.command is one of the registered subparser names (required=True), so
    # the match always exists; build_parser() and dispatch share _COMMANDS as the
    # single source of truth, dispensing with a parallel by-name index.
    try:
        return next(cls for cls in _COMMANDS if cls.name == args.command)().run(args, ctx)
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
