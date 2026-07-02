"""The ``boto3-s3 rb`` subcommand: delete a bucket with ``aws s3 rb`` semantics."""

from __future__ import annotations

import argparse
import sys

# Pure-Python names only on the parse path (import contract, docs/imports.md);
# rm.py's module top level is itself SDK-free, so RmCommand is safe here.
from boto3_s3 import Boto3S3Error, ValidationError
from boto3_s3_cli import output, usage
from boto3_s3_cli.commands.base import Command, Context
from boto3_s3_cli.commands.rm import RmCommand

# aws-cli RbCommand._force raises RuntimeError with this exact sentence when
# the inner rm fails; it reaches the general handler -> rc 255.
_FORCE_FAILED = (
    "remove_bucket failed: Unable to delete all objects in the bucket, bucket will not be deleted."
)


class RbCommand(Command):
    """Delete an (empty) S3 bucket with ``aws s3 rb`` semantics."""

    name = "rb"
    help = "Delete an empty S3 bucket (--force deletes its objects first)."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``rb``-specific arguments to its subparser."""
        parser.add_argument("paths", metavar="<S3Uri>")
        parser.add_argument("--force", action="store_true")

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Delete the bucket and return an ``aws s3 rb``-style exit code.

        Exit-code shape (aws-cli RbCommand): usage errors - a non-``s3://``
        path, a key part, a rejected ARN form - exit 252 via ``main``.
        ``--force`` runs a full ``rm --recursive`` first; a failure there is
        rc 255 (aws raises RuntimeError into the general handler) and the
        bucket delete is not attempted. Everything after delete_bucket starts
        is rc 1 with one ``remove_bucket failed:`` line. aws builds the client
        before validating the path (``S3Command._run_main``), so a
        client-construction failure (bad ``--profile`` / unresolved credentials /
        region) takes precedence over a path usage error - we build it first.
        """
        # Deferred: the library's S3 entry reaches botocore; --help / --version /
        # pre-subcommand usage errors stay SDK-free (import contract,
        # docs/imports.md). A subcommand's run() may load the SDK.
        from boto3_s3 import S3, S3Storage

        # Build the client up front, like aws's super()._run_main(), so a
        # construction error precedes the path checks (config -> 253, other
        # botocore -> 255), then validate the path.
        client = ctx.client_factory(args)

        target: str = args.paths
        if not target.startswith("s3://"):
            # aws rb: S3 paths only -> rc 252.
            raise ValidationError(usage.single_uri_usage("rb"), operation="rb")

        # Rejected ARN forms -> 252 from S3Storage.validate (deferred from the now
        # non-raising construction); for "s3:///k" validate lands on 252 too, just
        # like aws's key check.
        storage = S3Storage(target, client=client)
        storage.validate()
        if storage.key:
            raise ValidationError(
                f"Please specify a valid bucket name only. E.g. s3://{storage.bucket}",
                operation="rb",
            )

        if args.force and self._force_rm(target, args, ctx) != 0:
            sys.stderr.write(_FORCE_FAILED + "\n")
            return 255

        try:
            S3().rb(storage)
        except Boto3S3Error as exc:
            sys.stderr.write(output.format_remove_bucket_failed(target, exc) + "\n")
            return 1
        sys.stdout.write(output.format_remove_bucket(storage.bucket) + "\n")
        return 0

    @staticmethod
    def _force_rm(target: str, args: argparse.Namespace, ctx: Context) -> int:
        """Run the inner ``rm --recursive``, mirroring aws's ``RbCommand._force``.

        The rm parser fills the rm-only defaults; rb's parsed namespace seeds
        the parse so the invocation's globals reach the inner run's client
        factory (aws hands its parsed_globals to a fresh RmCommand the same
        way). Caveat: argparse only applies defaults for dests missing from
        the namespace, so an rb option whose dest collided with an rm dest
        would leak through - revisit if rb ever grows such an option.
        """
        parser = argparse.ArgumentParser(prog="boto3-s3 rm", add_help=False)
        RmCommand().configure(parser)
        rm_args = parser.parse_args(
            [target, "--recursive"], namespace=argparse.Namespace(**vars(args))
        )
        return RmCommand().run(rm_args, ctx)
