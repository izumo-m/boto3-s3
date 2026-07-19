"""The ``boto3-s3 website`` subcommand: set a bucket's website configuration."""

from __future__ import annotations

import argparse

from boto3_s3 import ValidationError
from boto3_s3_cli import clientfactory, globalargs, usage
from boto3_s3_cli.commands.base import (
    Command,
    Context,
    expand_option_paramfile,
    expand_positional_paramfile,
)


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
        # aws's parse-time order (measured, docs/cli.md section 6): the --query
        # compile (252) leads, then the --endpoint-url scheme check (252), then
        # the paramfile expansions (252) - the positional path and both document
        # options are all expanded at parse time - before the request is built.
        globalargs.validate_query(args)
        clientfactory.validate_endpoint_url(args)
        raw_paths = args.paths
        expand_positional_paramfile(args, "paths", name="paths", operation="website")
        paths_expanded = args.paths is not raw_paths
        expand_option_paramfile(args, "index_document", operation="website")
        expand_option_paramfile(args, "error_document", operation="website")
        # Import the library entry point only when this execution path needs it.
        from boto3_s3 import S3Storage

        # aws's _get_bucket_name: strip an optional s3://, strip ONE trailing
        # slash, then pass the remainder verbatim as the bucket name (no
        # key split - "b/k" fails botocore's bucket regex -> 252).
        if isinstance(args.paths, bytes):
            # Intentional aws-cli bug parity: WebsiteCommand declares a
            # one-element positional list, the URI handler unwraps it to bytes,
            # and the command indexes those bytes as though the list remained.
            # The resulting int has no startswith(), so aws exits 255.
            raise AttributeError("'int' object has no attribute 'startswith'")
        path: str = args.paths
        if paths_expanded:
            # The file:// half of the same bug: the unwrapped value is the
            # loaded text, and indexing it takes its FIRST CHARACTER as the
            # path (measured: a paramfile containing "s3://mybucket" makes aws
            # PutBucketWebsite on bucket "s"; an empty file is aws's
            # IndexError, rc 255 through the general handler). Reproducing it
            # matters beyond the rc: using the full text would perform a write
            # on a bucket aws never touches.
            path = path[0]
        if path.startswith("s3://"):
            path = path[len("s3://") :]
        if path.endswith("/"):
            path = path[:-1]

        s3 = ctx.s3(args)
        storage = S3Storage(f"s3://{path}", client=s3.client())
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
        s3.website(
            storage,
            index_document=args.index_document,
            error_document=args.error_document,
        )
        return 0
