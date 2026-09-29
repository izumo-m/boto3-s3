"""The ``boto3-s3 presign`` subcommand: generate a presigned URL for an object."""

from __future__ import annotations

import argparse
import sys

from boto3_s3 import InvalidConfigError
from boto3_s3_cli import clientfactory, globalargs, output
from boto3_s3_cli.commands import transferargs
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
        parser.add_argument("path", metavar="<S3Uri>", help="the object to sign a URL for")
        # No type=int: a non-integer must exit 255 like aws's bare int()
        # conversion (parse_integer_option, commands/base.py). Not
        # range-validated either: aws signs any value (0 / negative / over
        # S3's 604800 maximum); S3 rejects only when the URL is *used*.
        parser.add_argument(
            "--expires-in",
            default=3600,
            metavar="<seconds>",
            help="how long the URL stays valid (default 3600)",
        )

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Print the presigned URL and return an ``aws s3``-style exit code.

        Exit-code shape (design/cli.md section 6): pure client-side
        computation, so rc 1 cannot happen and no S3 request is sent (a
        deferred credential resolution - an assume-role profile - can still
        dial STS during signing, whose ClientError maps to 254). 0 on
        success; 252 for the strict aws-cli path rejections (the unsupported
        ARN families) and for botocore's client-side parameter validation
        while signing (an absent key, and an empty bucket - the bare
        ``s3://`` service root and the bucket-less ``s3:///k``, which aws
        splits into Bucket="" plus a key rather than refusing up front),
        both surfaced as the library's ValidationError through main; 253 for
        credentials that cannot be located when signing (a missing region is
        no failure: the client signs for us-east-1); 255 for client
        construction's botocore failures - a bad ``--profile``, partial
        credentials - for a non-integer ``--expires-in``, and for the CRT
        signer's refusal of a non-positive ``--expires-in`` on a SigV4a
        target (an MRAP ARN): awscrt's bare ``AssertionError``, which aws's
        general handler reports as an empty line, converted at the one call
        below like the CRT-region case (``materialize_transfer_engine``).
        Unlike mb/rb there is no local catch: with no request ever sent,
        nothing separates "started" from "not started".
        """
        # aws's parse-time order (measured, design/cli.md section 6): the --query
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
        # aws-cli's presign takes the path with or without the s3:// scheme
        # (PresignCommand merely strips a present one), so unlike mb/rb/rm
        # there is no path-type check here; build_s3_storage reads both forms
        # the same way. It keeps the strict ARN rejections and carves out the
        # bucket-less key-carrying form, which aws signs with Bucket="" and
        # lets botocore's bad-bucket-name validation refuse.
        s3 = ctx.s3(args)
        storage = transferargs.build_s3_storage(args.path, client=s3.client())
        try:
            url = s3.presign(storage, expires_in=expires_in)
        except AssertionError as exc:
            # The CRT signer - the only SigV4a signer botocore has, so an
            # MRAP ARN always takes it - refuses a non-positive expiry with
            # awscrt's own bare AssertionError. aws lets it reach its general
            # handler, which renders the empty rc-255 report (measured,
            # 2.36.40); everywhere else this CLI re-raises AssertionError as
            # an internal-invariant bug, so the conversion is scoped to this
            # one call, the shape materialize_transfer_engine gives the
            # CRT-region refusal. str(exc) is '' for a bare assert, which
            # _write_error renders without a detail.
            raise InvalidConfigError(str(exc), operation="presign") from exc
        output.uni_write(sys.stdout, url + "\n")
        return 0
