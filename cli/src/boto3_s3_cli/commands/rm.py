"""The ``boto3-s3 rm`` subcommand: delete S3 objects with ``aws s3 rm`` semantics."""

from __future__ import annotations

import argparse
import logging
import sys

# Module-level imports are fine here: rm is loaded at dispatch (stage 2 of
# the lazy dispatch), after the command is determined.
from boto3_s3 import (
    BatchError,
    OpOutcome,
    OpResult,
    ValidationError,
)
from boto3_s3_cli import filters, output, usage
from boto3_s3_cli.commands import transferargs
from boto3_s3_cli.commands.base import (
    Command,
    Context,
    add_page_size_argument,
    add_request_payer_argument,
    expand_integer_paramfile,
    expand_positional_paramfile,
    parse_integer_option,
)

logger = logging.getLogger(__name__)


class _DeletePrinter:
    """Stream per-item ``OpResult``s as aws-style delete lines.

    Invoked from ``S3Deleter``'s worker thread on the batched path, so the
    streams are looked up via ``sys`` on every call (never bound early) -
    in-process tests swap ``sys.stdout`` with ``redirect_stdout`` and must
    capture worker-thread writes too.

    Suppression matrix (aws-cli builds *no* result
    printer at all under ``--quiet``): ``--quiet`` silences success, failure,
    and dryrun lines alike; ``--only-show-errors`` silences successes but
    still prints dryrun lines (aws's ``OnlyShowErrorsResultPrinter`` does not
    override ``_print_dry_run``).

    A line that cannot be written at all is dropped with a debug log and never
    escapes, aws's ``ResultProcessor._process_result`` shape: the deletes have
    already happened by then, so an unwritable stdout must leave the run's
    outcome alone.
    """

    def __init__(self, *, quiet: bool, only_show_errors: bool) -> None:
        self._quiet = quiet
        self._only_show_errors = only_show_errors

    def __call__(self, result: OpResult) -> None:
        if self._quiet:
            return
        try:
            self._write_line(result)
        except Exception as exc:
            logger.debug("Error printing result %s: %s", result, exc, exc_info=True)

    def _write_line(self, result: OpResult) -> None:
        # The line names the object the way the record's `src` does: the
        # listed entry as s3://bucket/key, the single-key target as it was
        # written - an Outposts access point's `<arn>:key` keeps its colon,
        # like aws-cli's line. Every delete record sets it (design/opresult.md).
        location = result.src
        assert location is not None
        if result.outcome is OpOutcome.FAILED:
            sys.stderr.write(output.format_delete_failed(location, result.error) + "\n")
        elif result.outcome is OpOutcome.DRYRUN:
            # uni_write (aws's uni_print): an unencodable key on a narrow
            # console/pipe encoding must not raise out of the deleter worker
            # and kill the run - aws prints it with replacements and finishes.
            output.uni_write(sys.stdout, output.format_delete(location, dryrun=True) + "\n")
        elif not self._only_show_errors:
            output.uni_write(sys.stdout, output.format_delete(location, dryrun=False) + "\n")


class RmCommand(Command):
    """Delete objects under a key, prefix, or bucket with ``aws s3 rm`` semantics."""

    name = "rm"
    help = "Delete an S3 object, or objects under a prefix (--recursive)."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``rm``-specific arguments to its subparser."""
        parser.add_argument("paths", help="the object or prefix to delete")
        parser.add_argument(
            "--dryrun",
            action="store_true",
            help="report what would be deleted without deleting anything",
        )
        parser.add_argument(
            "--quiet", action="store_true", help="do not print per-item result lines"
        )
        parser.add_argument(
            "--recursive", action="store_true", help="delete every object under the prefix"
        )
        add_request_payer_argument(parser)
        parser.add_argument(
            "--only-show-errors",
            action="store_true",
            help="print only errors and warnings, no result lines",
        )
        filters.add_filter_arguments(parser)
        add_page_size_argument(parser)

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Delete the target(s) and return an ``aws s3 rm``-style exit code.

        Exit-code shape (differs from ``ls``): usage errors - a
        non-``s3://`` path, a path that names no bucket, a rejected ARN
        form - exit 252 via ``main``, but
        every failure after the operation starts is rc 1 whatever its type
        (rm is an ``S3TransferCommand`` in aws too, so its execution span sits
        inside the same result recorder cp / mv / sync's does -
        ``transferargs.finish_transfer`` derives its code from the same rule):
        per-key failures
        print ``delete failed:`` lines, a Ctrl-C prints one ``cancelled:
        ctrl-c received`` line, anything else that kills the run (the
        listing rejecting the bucket or the page size, botocore validation)
        prints one ``fatal error:`` line - all suppressed by ``--quiet``
        with the exit codes kept. Nothing maps to 254 here. Ahead of all of
        that sit the client build and the ``[s3]`` runtime config, whose
        failures stop the run before it starts (253 or 255 by kind).
        """
        expand_positional_paramfile(args, "paths", name="paths", operation="rm")
        expand_integer_paramfile(args, "page_size", operation="rm")
        page_size = parse_integer_option(args.page_size, operation="rm")
        target: str = args.paths
        if not target.startswith("s3://"):
            # aws check_path_type: rm takes S3 paths only -> rc 252.
            raise ValidationError(usage.single_uri_usage("rm"), operation="rm")
        # Next in aws's add_paths: the refused ARN forms (252) and the keyless
        # trailing "/" whose re-read gives an Outposts "...:accesspoint/ap:"
        # the key "/". A path that names no bucket is refused here too (252).
        target = transferargs.normalize_s3_path(target, operation="rm")

        s3 = ctx.s3(args)
        client = s3.client()
        # rm is an S3TransferCommand in aws, so it reads and validates [s3] and
        # builds a transfer manager exactly like cp, before anything is
        # deleted. The deletes ride the engine but the library builds it later
        # - with the deleter, or just before a single key's request
        # (design/crt.md sections 4 and 6) - so its construction failures are
        # brought forward here, where they stop the run as they do on aws.
        transfer_config = transferargs.resolve_transfer_config(ctx, s3, paths_type="s3")
        transferargs.materialize_transfer_engine(s3, client, transfer_config)

        storage = transferargs.build_s3_storage(target, client=client, page_size=page_size)

        item_filter = filters.compile_filter(args.filters)
        printer = _DeletePrinter(quiet=args.quiet, only_show_errors=args.only_show_errors)
        try:
            s3.rm(
                storage,
                recursive=args.recursive,
                filter=item_filter,
                dryrun=args.dryrun,
                request_payer=args.request_payer,
                on_result=printer,
                # The engine aws would carry these deletes on, which decides
                # the client they ride and its retry policy.
                transfer_config=transfer_config,
            )
        except BatchError:
            # Per-key failure lines were already streamed by the printer.
            return 1
        except KeyboardInterrupt:
            # Same conversion as the cp/mv/sync span (transferargs.
            # finish_transfer): aws's shared result machinery makes a
            # mid-run Ctrl-C a cancelled run - one `cancelled: ctrl-c
            # received` line, rc 1 (measured mid-rm, 2.36.40) - never the
            # dispatcher's 130.
            if not args.quiet:
                sys.stderr.write("cancelled: ctrl-c received\n")
            return 1
        except AssertionError:
            # An internal-invariant violation (a bug) surfaces loudly, like the
            # dispatcher's AssertionError re-raise (cli.py) and the cp/mv/sync
            # span's - masking it as a fatal error would also blunt the test
            # doubles' unexpected-call guards.
            raise
        except Exception as exc:
            # Everything the operation raises is one `fatal error:` line at
            # rc 1, by position rather than by type: aws's rm runs inside the
            # same `CommandResultRecorder` cp/mv/sync do, which turns whatever
            # escapes the pipeline into an ErrorResult.
            # `SystemExit` is a BaseException and still passes, like there.
            if not args.quiet:
                sys.stderr.write(f"fatal error: {exc}\n")
            return 1
        return 0
