"""The ``boto3-s3 website`` subcommand: set a bucket's website configuration."""

from __future__ import annotations

import argparse

from boto3_s3 import ValidationError
from boto3_s3_cli import usage
from boto3_s3_cli.commands import transferargs
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
        mb/rb, aws's WebsiteCommand lets PutBucketWebsite exceptions reach
        its general handler chain, so server rejections (NoSuchBucket, an
        endpoint refusing the configuration) are rc **254** through main's
        ClientError-cause mapping; a path that names no bucket is refused
        before the request (252); credentials that cannot be located
        are 253 at the request (a missing region is no failure: the client
        defaults to us-east-1), while client construction's botocore failures
        - a bad ``--profile``, partial credentials - are 255.
        """
        # aws expands a paramfile on the positional, then indexes the value as
        # though it were still a one-element list, so its first character
        # becomes the bucket (a file holding "s3://mybucket" configures
        # bucket "s"), and a fileb:// one crashes. Neither is copied: both
        # prefixes are refused (docs/cli/aws-differences.md section 2).
        path: str = args.paths
        if path.startswith(("file://", "fileb://")):
            raise ValidationError(
                "Error parsing parameter 'paths': a file:// or fileb:// reference "
                "cannot name the bucket; give the bucket itself",
                operation="website",
            )
        # Both document options are expanded at parse time, as on aws.
        expand_option_paramfile(args, "index_document", operation="website")
        expand_option_paramfile(args, "error_document", operation="website")
        # aws's _get_bucket_name: strip an optional s3://, strip ONE trailing
        # slash, then pass the remainder verbatim as the bucket name (no
        # key split - "b/k" fails botocore's bucket regex -> 252).
        if path.startswith("s3://"):
            path = path[len("s3://") :]
        if path.endswith("/"):
            path = path[:-1]

        s3 = ctx.s3(args)
        client = s3.client()
        # build_s3_storage makes the path refusals (no bucket, the
        # unsupported ARNs) before the rejection below.
        storage = transferargs.build_s3_storage(f"s3://{path}", client=client)
        if storage.bucket != path:
            # aws hands the whole remainder to PutBucketWebsite as the Bucket
            # and lets botocore's name regex reject it (rc 252). S3Storage
            # splits it instead, which would hide that rejection, so a
            # remainder the split did not keep whole as the bucket is refused
            # here from the unsplit path: whatever S3Storage read as a key
            # ("b/some/key"), "b//", whose leftover slash the split would
            # silently drop, and an Outposts ".../accesspoint/ap:", whose ":"
            # the split eats as the key separator. Every other name botocore
            # refuses - a slash-free bad one - survives the split intact and
            # is refused by botocore at the request. An accesspoint ARN is the
            # whole bucket and passes through, exactly like aws.
            raise ValidationError(
                usage.invalid_bucket_name_message(path),
                operation="website",
            )
        s3.website(
            storage,
            index_document=args.index_document,
            error_document=args.error_document,
        )
        return 0
