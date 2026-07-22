"""The ``boto3-s3 mb`` subcommand: create a bucket with ``aws s3 mb`` semantics."""

from __future__ import annotations

import argparse
import sys

# Module-level imports are fine here: mb is loaded at dispatch (stage 2 of
# the lazy dispatch), after the command is determined.
from boto3_s3 import Boto3S3Error, S3Storage, ValidationError
from boto3_s3_cli import clientfactory, globalargs, output, usage
from boto3_s3_cli.commands.base import Command, Context, expand_positional_paramfile


class MbCommand(Command):
    """Create an S3 bucket with ``aws s3 mb`` semantics."""

    name = "mb"
    help = "Create an S3 bucket."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``mb``-specific arguments to its subparser."""
        parser.add_argument("path", metavar="<S3Uri>")
        # Repeatable KEY VALUE pairs, duplicates passed through for the server
        # to reject (aws-cli TAGS arg: action append, nargs 2).
        parser.add_argument("--tags", action="append", nargs=2, metavar=("KEY", "VALUE"))

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Create the bucket and return an ``aws s3 mb``-style exit code.

        Exit-code shape (aws-cli MbCommand): usage errors - a non-``s3://``
        path, an S3 Express (``--x-s3``) bucket, a rejected ARN form - exit
        252 via ``main``; every classified (``Boto3S3Error``) failure after the
        operation starts is rc 1 with
        one ``make_bucket failed:`` line (aws catches every create_bucket
        exception locally, even request-time credential errors; an
        unclassified exception here falls to the dispatcher's handler chain -
        the taxonomy classifies the known failures, docs/exceptions.md). The key part
        of the path is silently dropped, exactly like aws. aws builds the client
        before validating the path (``S3Command._run_main``), so a
        client-construction failure (bad ``--profile`` / unresolved credentials /
        region) takes precedence over a path usage error - we build it first to
        match (253/255 wins over the 252).
        """
        # Parse-time head (measured, docs/cli.md section 6): the --query compile
        # (252), the --endpoint-url scheme check (252), and the positional
        # paramfile expansion (252) all precede the client build - they beat a
        # bad --profile (255) the way aws's parse-time load-cli-arg does.
        globalargs.validate_query(args)
        clientfactory.validate_endpoint_url(args)
        expand_positional_paramfile(args, "path", name="path", operation="mb")
        # Build the client up front, like aws's super()._run_main(), so a
        # construction error precedes the path checks below (it reaches main's
        # exit-code mapping: config -> 253, other botocore -> 255).
        s3 = ctx.s3(args)
        client = s3.client()

        target: str = args.path
        if not target.startswith("s3://"):
            # aws mb: S3 paths only -> rc 252.
            raise ValidationError(usage.bare_single_uri_usage(), operation="mb")

        bucket_part, _, _key_part = target[len("s3://") :].partition("/")
        if not bucket_part:
            # aws sends Bucket="" to the API and botocore's client-side
            # validation fails inside mb's local catch -> rc 1. S3Storage.validate()
            # would reject "s3:///k" as a ValidationError (252-shaped), so handle
            # the form before construction to keep this path at rc 1 (same as rm).
            message = usage.invalid_bucket_name_message()
            sys.stderr.write(output.format_make_bucket_failed(target, message) + "\n")
            return 1

        # Rejected ARN forms (S3 Object Lambda / Outposts bucket) raise
        # ValidationError from S3Storage.validate -> rc 252, matching aws (the
        # check is deferred from the now non-raising construction).
        storage = S3Storage(target, client=client)
        storage.validate()
        if storage.bucket.endswith("--x-s3"):
            # botocore is_s3express_bucket on the ARN-aware bucket, ordered like
            # aws (split_s3_bucket_key's ARN rejection runs first, inside
            # validate); a slash-form accesspoint ARN whose name ends --x-s3 is
            # caught here where a naive partition on the first "/" would miss it.
            raise ValidationError("Cannot use mb command with a directory bucket.", operation="mb")
        tags = [(key, value) for key, value in args.tags] if args.tags else None
        try:
            s3.mb(storage, tags=tags)
        except Boto3S3Error as exc:
            sys.stderr.write(output.format_make_bucket_failed(target, exc) + "\n")
            return 1
        output.uni_write(sys.stdout, output.format_make_bucket(storage.bucket) + "\n")
        return 0
