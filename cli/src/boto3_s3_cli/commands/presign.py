"""The ``boto3-s3 presign`` subcommand: generate a presigned URL for an object."""

from __future__ import annotations

import argparse
import sys

from boto3_s3_cli import clientfactory, globalargs, output
from boto3_s3_cli.commands.base import (
    Command,
    Context,
    expand_integer_paramfile,
    expand_positional_paramfile,
    parse_integer_option,
)


class PresignCommand(Command):
    """Generate a presigned GET URL with ``aws s3 presign`` semantics."""

    name = "presign"
    help = "Generate a pre-signed URL for an Amazon S3 object."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``presign``-specific arguments to its subparser."""
        parser.add_argument("path", metavar="<S3Uri>")
        # No type=int: a non-integer must exit 255 like aws's bare int()
        # conversion (parse_integer_option, commands/base.py). Not
        # range-validated either: aws signs any value (0 / negative / over
        # S3's 604800 maximum); S3 rejects only when the URL is *used*.
        parser.add_argument("--expires-in", default=3600, metavar="<seconds>")

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Print the presigned URL and return an ``aws s3``-style exit code.

        Exit-code shape (docs/cli.md section 6): pure client-side
        computation, so rc 1 and 254 cannot happen - 0 on success, 252 for
        botocore's client-side parameter validation (empty bucket or key,
        surfaced as the library's ValidationError through main), 253 for
        client-construction failures, 255 for a non-integer ``--expires-in``.
        Unlike mb/rb there is no local catch: with no request ever sent,
        nothing separates "started" from "not started".
        """
        # aws's parse-time order (measured, docs/cli.md section 6): the --query
        # compile (252) leads, then the --endpoint-url scheme check (252), then
        # the paramfile expansions (252, the positional path and --expires-in)
        # precede the bare int() coercion (255).
        globalargs.validate_query(args)
        clientfactory.validate_endpoint_url(args)
        expand_positional_paramfile(args, "path", name="path", operation="presign")
        expand_integer_paramfile(args, "expires_in", operation="presign")
        expires_in = parse_integer_option(args.expires_in, operation="presign")
        # --expires-in's argparse default is 3600, so the unset-None branch of
        # parse_integer_option is unreachable here.
        assert expires_in is not None
        # Import the library entry point only when this execution path needs it.
        from boto3_s3 import S3Storage

        # aws-cli's presign takes the path with or without the s3:// scheme
        # (PresignCommand merely strips a present one), so unlike mb/rb/rm
        # there is no path-type check here; S3Storage takes both forms too.
        s3 = ctx.s3(args)
        storage = S3Storage(args.path, client=s3.client())
        storage.validate()
        url = s3.presign(storage, expires_in=expires_in)
        output.uni_write(sys.stdout, url + "\n")
        return 0
