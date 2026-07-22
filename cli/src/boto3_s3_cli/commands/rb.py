"""The ``boto3-s3 rb`` subcommand: delete a bucket with ``aws s3 rb`` semantics."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

# Module-level imports are fine here: rb is loaded at dispatch (stage 2 of
# the lazy dispatch), after the command is determined.
from boto3_s3 import Boto3S3Error, InvalidValueError, S3Storage, ValidationError
from boto3_s3_cli import clientfactory, globalargs, output, usage
from boto3_s3_cli.commands.base import Command, Context, expand_positional_paramfile
from boto3_s3_cli.commands.rm import RmCommand

if TYPE_CHECKING:
    from boto3_s3 import S3

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
        parser.add_argument("path", metavar="<S3Uri>")
        parser.add_argument("--force", action="store_true")

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Delete the bucket and return an ``aws s3 rb``-style exit code.

        Exit-code shape (aws-cli RbCommand): usage errors - a non-``s3://``
        path, a key part, a rejected ARN form - exit 252 via ``main``.
        ``--force`` runs a full ``rm --recursive`` first; a failure there is
        rc 255 (aws raises RuntimeError into the general handler) and the
        bucket delete is not attempted. Every classified (``Boto3S3Error``)
        failure after delete_bucket starts
        is rc 1 with one ``remove_bucket failed:`` line (an unclassified
        exception falls to the dispatcher's handler chain instead). aws builds the client
        before validating the path (``S3Command._run_main``), so a
        client-construction failure (bad ``--profile`` / unresolved credentials /
        region) takes precedence over a path usage error - we build it first.
        """
        # Parse-time head (measured, docs/cli.md section 6): the --query compile
        # (252), the --endpoint-url scheme check (252), and the positional
        # paramfile expansion (252) all precede the client build - they beat a
        # bad --profile (255) the way aws's parse-time load-cli-arg does.
        globalargs.validate_query(args)
        clientfactory.validate_endpoint_url(args)
        expand_positional_paramfile(args, "path", name="path", operation="rb")
        # Build the client up front, like aws's super()._run_main(), so a
        # construction error precedes the path checks (config -> 253, other
        # botocore -> 255), then validate the path.
        s3 = ctx.s3(args)
        client = s3.client()

        target: str = args.path
        if not target.startswith("s3://"):
            # aws rb: S3 paths only -> rc 252.
            raise ValidationError(usage.bare_single_uri_usage(), operation="rb")

        bucket_part, _, key_part = target[len("s3://") :].partition("/")
        if not bucket_part:
            # aws splits the path first: an empty bucket carrying a key is the
            # "specify a valid bucket name only" usage error (252, ending in a
            # bare "s3://"); an empty bucket with no key goes to delete_bucket,
            # where botocore's client-side validation fails inside rb's local
            # catch -> rc 1 (the same botocore-shaped line as mb / rm). Both
            # cases are handled before construction, which S3Storage.validate()
            # would otherwise reject with library-internal wording.
            if key_part:
                raise ValidationError(
                    f"Please specify a valid bucket name only. E.g. s3://{bucket_part}",
                    operation="rb",
                )
            # aws has no empty-bucket short-circuit: --force still runs the
            # inner rm --recursive first, and its (inevitable) failure aborts
            # at rc 255 before delete_bucket is attempted.
            if args.force and self._force_rm(target, args, ctx, s3) != 0:
                raise InvalidValueError(_FORCE_FAILED, operation="rb")
            message = usage.invalid_bucket_name_message()
            sys.stderr.write(output.format_remove_bucket_failed(target, message) + "\n")
            return 1

        # Rejected ARN forms -> 252 from S3Storage.validate (deferred from the now
        # non-raising construction); a key on a valid bucket is the same usage
        # error as above (aws's post-split key check).
        storage = S3Storage(target, client=client)
        storage.validate()
        if storage.key:
            raise ValidationError(
                f"Please specify a valid bucket name only. E.g. s3://{storage.bucket}",
                operation="rb",
            )

        if args.force and self._force_rm(target, args, ctx, s3) != 0:
            # aws raises RuntimeError into its general handler, which prints the
            # 'aws: [ERROR]:'-prefixed line; route it as an InvalidValueError so
            # main emits the matching 'boto3-s3: [ERROR]:' prefix -> rc 255
            # (the inner rm already streamed its own failure output: per-key
            # 'delete failed:' lines, or one fatal-error line).
            raise InvalidValueError(_FORCE_FAILED, operation="rb")

        try:
            s3.rb(storage)
        except Boto3S3Error as exc:
            sys.stderr.write(output.format_remove_bucket_failed(target, exc) + "\n")
            return 1
        output.uni_write(sys.stdout, output.format_remove_bucket(storage.bucket) + "\n")
        return 0

    @staticmethod
    def _force_rm(target: str, args: argparse.Namespace, ctx: Context, s3: S3) -> int:
        """Run the inner ``rm --recursive``, mirroring aws's ``RbCommand._force``.

        The rm parser fills the rm-only defaults; rb's parsed namespace seeds
        the parse so the rm-side validations read the invocation's globals
        (aws hands its parsed_globals to a fresh RmCommand the same way) -
        the client itself comes from the shared S3 below, built from those
        same globals. The inner run shares rb's `S3` (`Context.with_s3`) exactly as
        aws's ``RmCommand(self._session)`` shares the one CLI session - a
        second session would resolve credentials again (a
        ``credential_process`` / MFA flow re-prompting, possibly under a
        different identity than the final ``DeleteBucket``). Caveat: argparse
        only applies defaults for dests missing from
        the namespace, so an rb option whose dest collided with an rm dest
        would leak through - revisit if rb ever grows such an option.
        """
        parser = argparse.ArgumentParser(prog="boto3-s3 rm", add_help=False)
        RmCommand().configure(parser)
        rm_args = parser.parse_args(
            [target, "--recursive"], namespace=argparse.Namespace(**vars(args))
        )
        return RmCommand().run(rm_args, ctx.with_s3(s3))
