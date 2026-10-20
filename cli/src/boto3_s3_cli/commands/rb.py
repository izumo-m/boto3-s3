"""The ``boto3-s3 rb`` subcommand: delete a bucket with ``aws s3 rb`` semantics."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

# Module-level imports are fine here: rb is loaded at dispatch (stage 2 of
# the lazy dispatch), after the command is determined.
from boto3_s3 import InvalidValueError, S3Storage, ValidationError
from boto3_s3_cli import output, usage
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
        parser.add_argument("path", metavar="<S3Uri>", help="the bucket to delete")
        parser.add_argument(
            "--force",
            action="store_true",
            help="delete every object in the bucket first",
        )

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Delete the bucket and return an ``aws s3 rb``-style exit code.

        Exit-code shape (aws-cli RbCommand): usage errors - a non-``s3://``
        path, a key part on a bucket, a rejected ARN form - exit 252 via
        ``main``; an empty bucket name is rc 1, like a delete_bucket failure.
        ``--force`` runs a full ``rm --recursive`` first; a failure there is
        rc 255 (aws raises RuntimeError into the general handler) and the
        bucket delete is not attempted. Every failure of delete_bucket is rc
        1 with one ``remove_bucket failed:`` line. The success line is past
        that catch: the bucket is gone by then, so a stdout that cannot take
        the line is lost output (rc 255 with the error, like every command's),
        and a process without a stdout (``1>&-``) keeps rc 0 - where aws's
        catch spans the line and reports the removed bucket as an rb failure.
        A client-construction
        failure (a bad ``--profile``, partial credentials, an empty region)
        reaches main's exit-code mapping (config -> 253, other botocore -> 255).
        """
        expand_positional_paramfile(args, "path", name="path", operation="rb")
        target: str = args.path
        if not target.startswith("s3://"):
            # aws rb: S3 paths only -> rc 252.
            raise ValidationError(usage.single_uri_usage("rb"), operation="rb")

        bucket_part = target[len("s3://") :].partition("/")[0]

        s3 = ctx.s3(args)
        client = s3.client()
        if not bucket_part:
            # An empty bucket goes to delete_bucket on aws, where botocore's
            # client-side validation fails inside rb's local catch -> rc 1
            # (the same line as mb / rm). Handled before construction, which
            # would refuse a key-carrying form (`s3:///k`) as a usage error.
            # aws has no empty-bucket short-circuit: --force still runs the
            # inner rm --recursive first, and its (inevitable) failure aborts
            # at rc 255 before delete_bucket is attempted.
            if args.force and self._force_rm(target, args, ctx, s3) != 0:
                raise InvalidValueError(_FORCE_FAILED, operation="rb")
            message = usage.invalid_bucket_name_message()
            sys.stderr.write(output.format_remove_bucket_failed(target, message) + "\n")
            return 1

        # Rejected ARN forms -> 252 from S3Storage.validate (deferred from the now
        # non-raising construction); a key on the bucket is aws's post-split
        # key check, also a usage error (252).
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
        except Exception as exc:
            sys.stderr.write(output.format_remove_bucket_failed(target, exc) + "\n")
            return 1
        # The bucket is gone from here, so the line is outside the failure
        # catch (aws's spans it, reporting a removed bucket as failed).
        output.write_result_line(output.format_remove_bucket(storage.bucket))
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
