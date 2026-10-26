"""The ``boto3-s3 website`` subcommand: set a bucket's website configuration."""

from __future__ import annotations

import argparse

from boto3_s3 import ValidationError
from boto3_s3_cli import usage
from boto3_s3_cli.commands.base import Command, Context, expand_option_paramfile


class WebsiteCommand(Command):
    """Set the bucket website configuration with ``aws s3 website`` semantics."""

    name = "website"
    help = "Set the website configuration for a bucket."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``website``-specific arguments to its subparser."""
        parser.add_argument("paths", help="the bucket to configure")
        parser.add_argument(
            "--index-document",
            metavar="<suffix>",
            help="file name served for a request that names a directory",
        )
        parser.add_argument(
            "--error-document", metavar="<key>", help="object returned when an error occurs"
        )

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Put the website configuration and return an ``aws s3``-style exit code.

        Exit-code shape (design/cli.md section 6): no local catch - unlike
        mb/rb, aws-cli's WebsiteCommand lets PutBucketWebsite exceptions reach
        its general handler chain, so server rejections (NoSuchBucket, an
        endpoint refusing the configuration) are rc **254** through main's
        ClientError-cause mapping; a bucket name botocore does not take, an
        empty one included, and the unsupported ARN families are refused
        before the request (252); credentials that cannot be located
        are 253 at the request (a missing region is no failure: the client
        defaults to us-east-1), while client construction's botocore failures
        - a bad ``--profile``, partial credentials - are 255.
        """
        # aws-cli expands a paramfile on the positional, then indexes the value as
        # though it were still a one-element list, so its first character
        # becomes the bucket (a file holding "s3://mybucket" configures
        # bucket "s"), and a fileb:// one crashes. Neither is copied: both
        # prefixes are refused (docs/cli/aws-differences.md section 2).
        path: str = args.paths
        if path.startswith(("file://", "fileb://")):
            raise ValidationError(
                usage.param_error(
                    "paths",
                    "a file:// or fileb:// reference cannot name the bucket; "
                    "give the bucket itself",
                ),
                operation="website",
            )
        # Both document options are expanded at parse time, as on `aws`.
        expand_option_paramfile(args, "index_document", operation="website")
        expand_option_paramfile(args, "error_document", operation="website")
        # The library reads the path as aws-cli's _get_bucket_name does - the
        # s3:// prefix and one trailing slash off, the unsupported ARNs refused,
        # the rest the bucket name whole - so a name botocore does not take is
        # refused at the request in botocore's words (252), as on aws-cli.
        ctx.s3(args).website(
            path,
            index_document=args.index_document,
            error_document=args.error_document,
        )
        return 0
