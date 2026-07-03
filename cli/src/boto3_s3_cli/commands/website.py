"""The ``boto3-s3 website`` subcommand: set a bucket's website configuration."""

from __future__ import annotations

import argparse

from boto3_s3 import ValidationError
from boto3_s3_cli import usage
from boto3_s3_cli.commands.base import Command, Context


class WebsiteCommand(Command):
    """Set the bucket website configuration with ``aws s3 website`` semantics."""

    name = "website"
    help = "Set the website configuration for a bucket."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``website``-specific arguments to its subparser."""
        parser.add_argument("paths", metavar="<S3Uri>")
        parser.add_argument("--index-document", metavar="<suffix>")
        parser.add_argument("--error-document", metavar="<key>")

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Put the website configuration and return an ``aws s3``-style exit code.

        Exit-code shape (docs/cli.md section 6): no local catch - unlike
        mb/rb, aws's WebsiteCommand lets PutBucketWebsite exceptions reach
        its general handler chain, so server rejections (NoSuchBucket, an
        endpoint refusing the configuration) are rc **254** through main's
        ClientError-cause mapping; botocore's client-side parameter
        validation (empty bucket) is 252; client construction is 253.
        """
        # Deferred: dispatch is the first point that needs the library's S3
        # entry (whose chain reaches botocore); --help and usage errors stay
        # SDK-free (import contract, docs/imports.md).
        from boto3_s3 import S3, S3Storage

        # aws's _get_bucket_name: strip an optional s3://, strip ONE trailing
        # slash, then pass the remainder verbatim as the bucket name (no
        # key split - "b/k" fails botocore's bucket regex -> 252).
        path = args.paths
        if path.startswith("s3://"):
            path = path[len("s3://") :]
        if path.endswith("/"):
            path = path[:-1]

        storage = S3Storage(f"s3://{path}", client=ctx.client_factory(args))
        storage.validate()
        if path.endswith("/") or storage.key:
            # S3Storage splits "b/k" where aws would send the whole string as
            # the Bucket and let botocore's name regex reject it; reproduce
            # the rejection (same rc, botocore-shaped message) here. An
            # accesspoint ARN parses to a bucket with no key and passes
            # through, exactly like aws. The endswith check catches "b//",
            # whose leftover slash S3Storage's split would silently drop.
            raise ValidationError(
                usage.invalid_bucket_name_message(path),
                operation="website",
            )
        S3().website(
            storage,
            index_document=args.index_document,
            error_document=args.error_document,
        )
        return 0
