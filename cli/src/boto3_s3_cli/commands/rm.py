"""The ``boto3-s3 rm`` subcommand: delete S3 objects with ``aws s3 rm`` semantics."""

from __future__ import annotations

import argparse
import sys

# Pure-Python names only (exceptions / types modules) - safe on the parse
# path; S3 / S3Storage reach botocore and are imported in run() instead
# (import contract, docs/imports.md).
from boto3_s3 import (
    BatchError,
    Boto3S3Error,
    OpOutcome,
    OpResult,
    ValidationError,
)
from boto3_s3_cli import filters, output
from boto3_s3_cli.commands.base import Command, Context, parse_integer_option


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
    """

    def __init__(self, *, bucket: str, quiet: bool, only_show_errors: bool) -> None:
        self._bucket = bucket
        self._quiet = quiet
        self._only_show_errors = only_show_errors

    def __call__(self, result: OpResult) -> None:
        if self._quiet:
            return
        if result.outcome is OpOutcome.FAILED:
            sys.stderr.write(
                output.format_delete_failed(self._bucket, result.key, result.error) + "\n"
            )
        elif result.outcome is OpOutcome.DRYRUN:
            sys.stdout.write(output.format_delete(self._bucket, result.key, dryrun=True) + "\n")
        elif not self._only_show_errors:
            sys.stdout.write(output.format_delete(self._bucket, result.key, dryrun=False) + "\n")


class RmCommand(Command):
    """Delete objects under a key, prefix, or bucket with ``aws s3 rm`` semantics."""

    name = "rm"
    help = "Delete an S3 object, or objects under a prefix (--recursive)."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``rm``-specific arguments to its subparser."""
        parser.add_argument("paths", metavar="<S3Uri>")
        parser.add_argument("--dryrun", action="store_true")
        parser.add_argument("--quiet", action="store_true")
        parser.add_argument("--recursive", action="store_true")
        parser.add_argument(
            "--request-payer", nargs="?", const="requester", choices=["requester"], default=None
        )
        parser.add_argument("--only-show-errors", action="store_true")
        # One shared ordered dest: the interleaved --exclude/--include order
        # carries aws-cli's last-match-wins semantics (cli filters module).
        parser.add_argument(
            "--exclude", action=filters.AppendFilterAction, dest="filters", metavar="PATTERN"
        )
        parser.add_argument(
            "--include", action=filters.AppendFilterAction, dest="filters", metavar="PATTERN"
        )
        # Not range-validated: aws-cli passes any int through and lets the
        # server decide (same policy as ls; charter, docs/overview.md section 3). No
        # type=int: a non-integer must exit 255 like aws's bare int()
        # conversion, not argparse's 252 (parse_integer_option, commands/base.py).
        parser.add_argument("--page-size", default=1000)

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Delete the target(s) and return an ``aws s3 rm``-style exit code.

        Exit-code shape (differs from ``ls``): usage errors - a
        non-``s3://`` path, a rejected ARN form - exit 252 via ``main``, but
        every error after the operation starts is rc 1: per-key failures
        print ``delete failed:`` lines, anything that kills the run (the
        listing rejecting the bucket or the page size, botocore validation)
        prints one ``fatal error:`` line. Nothing maps to 254 here.
        """
        # First, like aws's parse-time conversion: a non-integer exits 255
        # before the path checks and before any client (or the SDK) is touched.
        page_size = parse_integer_option(args.page_size, operation="rm")
        # Deferred: dispatch is the first point that needs the library's S3
        # entry (whose chain reaches botocore); --help and usage errors stay
        # SDK-free (import contract, docs/imports.md).
        from boto3_s3 import S3, S3Storage

        target: str = args.paths
        if not target.startswith("s3://"):
            # aws check_path_type: rm takes S3 paths only -> rc 252.
            raise ValidationError(
                "usage: boto3-s3 rm <S3Uri>\nError: Invalid argument type", operation="rm"
            )

        bucket_part, _, key_part = target[len("s3://") :].partition("/")
        if not bucket_part:
            # aws sends Bucket="" to the API and botocore's client-side
            # validation fails the task -> rc 1: shaped like a
            # per-key failure on the blind single path, a fatal error on the
            # enumerating paths. S3Storage.validate() would reject "s3:///k" as a
            # ValidationError (252-shaped), so handle the form before construction
            # to keep this path at rc 1.
            # botocore's ParamValidationError str uses a colon + newline.
            message = 'Parameter validation failed:\nInvalid bucket name ""'
            if not args.quiet:
                if key_part and not args.recursive:
                    sys.stderr.write(f"delete failed: {target} {message}\n")
                else:
                    sys.stderr.write(f"fatal error: {message}\n")
            return 1

        # Outside the fatal-catch below: rejected ARN forms (S3 Object Lambda /
        # Outposts bucket) raise ValidationError from S3Storage.validate (deferred
        # from the now non-raising construction) through main -> rc 252, matching aws.
        storage = S3Storage(target, client=ctx.client_factory(args))
        storage.validate()

        item_filter = filters.compile_filter(args.filters)
        printer = _DeletePrinter(
            bucket=storage.bucket, quiet=args.quiet, only_show_errors=args.only_show_errors
        )
        try:
            S3().rm(
                storage,
                recursive=args.recursive,
                filter=item_filter,
                dryrun=args.dryrun,
                page_size=page_size,
                request_payer=args.request_payer,
                on_result=printer,
            )
        except BatchError:
            # Per-key failure lines were already streamed by the printer.
            return 1
        except Boto3S3Error as exc:
            if not args.quiet:
                sys.stderr.write(f"fatal error: {exc}\n")
            return 1
        return 0
