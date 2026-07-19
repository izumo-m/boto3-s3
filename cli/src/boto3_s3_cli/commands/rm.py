"""The ``boto3-s3 rm`` subcommand: delete S3 objects with ``aws s3 rm`` semantics."""

from __future__ import annotations

import argparse
import sys

# Keep the module-level imports limited to the names used while configuring the
# command; execution-only storage types are imported in `run()`.
from boto3_s3 import (
    BatchError,
    Boto3S3Error,
    OpOutcome,
    OpResult,
    ValidationError,
)
from boto3_s3_cli import clientfactory, filters, globalargs, output, usage
from boto3_s3_cli.commands.base import (
    Command,
    Context,
    add_page_size_argument,
    add_request_payer_argument,
    expand_integer_paramfile,
    expand_positional_paramfile,
    parse_integer_option,
)


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
        # The printed line needs the full object key; a delete record's
        # compare_key is the operation-relative form (docs/opresult.md), and
        # the listed entry always rides on src_info.
        info = result.src_info
        key = info.key if info is not None else result.compare_key
        if result.outcome is OpOutcome.FAILED:
            sys.stderr.write(output.format_delete_failed(self._bucket, key, result.error) + "\n")
        elif result.outcome is OpOutcome.DRYRUN:
            sys.stdout.write(output.format_delete(self._bucket, key, dryrun=True) + "\n")
        elif not self._only_show_errors:
            sys.stdout.write(output.format_delete(self._bucket, key, dryrun=False) + "\n")


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
        add_request_payer_argument(parser)
        parser.add_argument("--only-show-errors", action="store_true")
        filters.add_filter_arguments(parser)
        add_page_size_argument(parser)

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Delete the target(s) and return an ``aws s3 rm``-style exit code.

        Exit-code shape (differs from ``ls``): usage errors - a
        non-``s3://`` path, a rejected ARN form - exit 252 via ``main``, but
        every error after the operation starts is rc 1: per-key failures
        print ``delete failed:`` lines, anything that kills the run (the
        listing rejecting the bucket or the page size, botocore validation)
        prints one ``fatal error:`` line. Nothing maps to 254 here.
        """
        # The aws parse-to-validation order (measured, docs/cli.md section 6):
        # the --query compile (252) leads, then the --endpoint-url scheme check
        # (252), then the paramfile expansions (252, positional path and
        # --page-size) beat the integer coercion (255), which beats the session
        # profile resolution (255), which beats the path usage check below (252).
        globalargs.validate_query(args)
        clientfactory.validate_endpoint_url(args)
        expand_positional_paramfile(args, "paths", name="paths", operation="rm")
        if isinstance(args.paths, bytes):
            # Intentional aws-cli bug parity: S3TransferCommand decodes a
            # positional fileb:// back through the filesystem encoding before
            # validating and executing it. This is why rm reaches its normal
            # rc-1 operation-error path while ls / website crash at rc 255.
            args.paths = args.paths.decode(sys.getfilesystemencoding())
        expand_integer_paramfile(args, "page_size", operation="rm")
        page_size = parse_integer_option(args.page_size, operation="rm")
        s3 = ctx.s3(args)
        # Deferred: dispatch is the first point that needs the library's S3
        # entry (whose chain reaches botocore).
        from boto3_s3 import S3Storage

        target: str = args.paths
        if not target.startswith("s3://"):
            # aws check_path_type: rm takes S3 paths only -> rc 252.
            raise ValidationError(usage.single_uri_usage("rm"), operation="rm")

        bucket_part, _, key_part = target[len("s3://") :].partition("/")
        if not bucket_part:
            # aws sends Bucket="" to the API and botocore's client-side
            # validation fails the task -> rc 1: shaped like a
            # per-key failure on the blind single path, a fatal error on the
            # enumerating paths. S3Storage.validate() would reject "s3:///k" as a
            # ValidationError (252-shaped), so handle the form before construction
            # to keep this path at rc 1.
            message = usage.invalid_bucket_name_message()
            if not args.quiet:
                if key_part and not args.recursive:
                    sys.stderr.write(f"delete failed: {target} {message}\n")
                else:
                    sys.stderr.write(f"fatal error: {message}\n")
            return 1

        # Outside the fatal-catch below: rejected ARN forms (S3 Object Lambda /
        # Outposts bucket) raise ValidationError from S3Storage.validate (deferred
        # from the now non-raising construction) through main -> rc 252, matching aws.
        # scan_wait_on_interrupt=False: Ctrl-C is process-fatal in the CLI, so the
        # recursive listing must not wait for an in-flight page pull on the way out.
        storage = S3Storage(
            target, client=s3.client(), page_size=page_size, scan_wait_on_interrupt=False
        )
        storage.validate()

        # The target is both filter sides (aws sets dest = src for rm).
        item_filter = filters.compile_filter(
            args.filters, src=storage, dest=storage, dir_op=args.recursive
        )
        printer = _DeletePrinter(
            bucket=storage.bucket, quiet=args.quiet, only_show_errors=args.only_show_errors
        )
        try:
            s3.rm(
                storage,
                recursive=args.recursive,
                filter=item_filter,
                dryrun=args.dryrun,
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
