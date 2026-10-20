"""The ``boto3-s3 mb`` subcommand: create a bucket with ``aws s3 mb`` semantics."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any, ClassVar, cast

# Module-level imports are fine here: mb is loaded at dispatch (stage 2 of
# the lazy dispatch), after the command is determined.
from boto3_s3 import S3Storage, ValidationError
from boto3_s3_cli import output, usage
from boto3_s3_cli.commands.base import Command, Context, expand_positional_paramfile


class _AppendTagsAction(argparse.Action):
    """Append one ``KEY VALUE`` pair, carrying aws's token count as a marker.

    Stock ``append`` behavior (the value stays argparse's own shape - a list
    of two-item lists, which ``run`` unpacks as pairs) plus ``aws_nargs``:
    aws declares TAGS with a token count, so both tokens are taken however
    they look (``mb --tags -k -v`` tags the bucket ``-k=-v``). The parser base
    class reads the marker and reproduces that on every supported Python
    (``cli._ParamValidationArgumentParser._match_argument``), which stock
    argparse only does from 3.12 on. The real ``nargs=2`` stays, so a missing
    token still fails the parse (252).
    """

    # The count aws declares; not an argparse attribute.
    aws_nargs: ClassVar[int] = 2

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        # The typeshed base signature; nargs is 2, so at runtime *values* is
        # always a two-item list of str.
        pairs: list[list[str]] = getattr(namespace, self.dest, None) or []
        pairs.append(cast("list[str]", values))
        setattr(namespace, self.dest, pairs)


class MbCommand(Command):
    """Create an S3 bucket with ``aws s3 mb`` semantics."""

    name = "mb"
    help = "Create an S3 bucket."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``mb``-specific arguments to its subparser."""
        parser.add_argument("path", metavar="<S3Uri>", help="the bucket to create")
        # Repeatable KEY VALUE pairs, duplicates passed through for the server
        # to reject (aws-cli TAGS arg: action append, nargs 2).
        parser.add_argument(
            "--tags",
            action=_AppendTagsAction,
            nargs=2,
            metavar=("KEY", "VALUE"),
            help="tag to attach to the new bucket; repeatable",
        )

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Create the bucket and return an ``aws s3 mb``-style exit code.

        Exit-code shape (aws-cli MbCommand): usage errors - a non-``s3://``
        path, an S3 Express (``--x-s3``) bucket, a rejected ARN form - exit
        252 via ``main``; every failure of the create is rc 1 with one
        ``make_bucket failed:`` line (aws catches every create_bucket
        exception locally, even request-time credential errors). The success
        line is past that catch: the bucket exists by then, so a stdout that
        cannot take the line is lost output (rc 255 with the error, like every
        command's), and a process without a stdout (``1>&-``) keeps rc 0 -
        where aws's catch spans the line and reports the created bucket as an
        mb failure. The key part
        of the path is silently dropped, exactly like aws. A client-construction
        failure (a bad ``--profile``, partial credentials, an empty region)
        reaches main's exit-code mapping (config -> 253, other botocore -> 255).
        """
        expand_positional_paramfile(args, "path", name="path", operation="mb")
        target: str = args.path
        if not target.startswith("s3://"):
            # aws mb: S3 paths only -> rc 252.
            raise ValidationError(usage.single_uri_usage("mb"), operation="mb")

        s3 = ctx.s3(args)
        client = s3.client()

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
        except Exception as exc:
            sys.stderr.write(output.format_make_bucket_failed(target, exc) + "\n")
            return 1
        # The bucket exists from here, so the line is outside the failure
        # catch (aws's spans it, reporting a created bucket as failed).
        output.write_result_line(output.format_make_bucket(storage.bucket))
        return 0
