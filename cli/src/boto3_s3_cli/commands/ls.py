"""The ``boto3-s3 ls`` subcommand: list S3 objects, common prefixes, or buckets."""

from __future__ import annotations

import argparse
import sys

from boto3_s3_cli import clientfactory, output
from boto3_s3_cli.commands.base import (
    Command,
    Context,
    add_page_size_argument,
    add_request_payer_argument,
    expand_option_paramfile,
    parse_integer_option,
)


class LsCommand(Command):
    """List objects/prefixes (or all buckets) with ``aws s3 ls`` semantics."""

    name = "ls"
    help = "List S3 objects and common prefixes under a prefix or all S3 buckets."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``ls``-specific arguments to its subparser."""
        parser.add_argument("paths", nargs="?", default="s3://", metavar="<S3Uri>")
        parser.add_argument("--recursive", action="store_true")
        add_page_size_argument(parser)
        add_request_payer_argument(parser)
        parser.add_argument("--human-readable", action="store_true")
        parser.add_argument("--summarize", action="store_true")
        # Bucket-listing filters (ListBuckets Prefix / BucketRegion); accepted but
        # inert for object listings, like aws-cli.
        parser.add_argument("--bucket-name-prefix", metavar="PREFIX")
        parser.add_argument("--bucket-region", metavar="REGION")

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """List objects/prefixes (or all buckets) and return an ``aws s3``-style code."""
        # aws's parse-time order (measured, docs/cli.md section 6): the
        # --endpoint-url scheme check (252) and the --page-size paramfile
        # expansion (252) both precede the bare int() coercion (255).
        clientfactory.validate_endpoint_url(args)
        expand_option_paramfile(args, "page_size", operation="ls")
        page_size = parse_integer_option(args.page_size, operation="ls")
        # Deferred: dispatch is the first point that needs the library's S3
        # entry (whose chain reaches botocore); --help and usage errors stay
        # SDK-free (import contract, docs/imports.md).
        from boto3_s3 import S3, FileKind, S3Storage

        target: str = args.paths
        # A target with no bucket lists all buckets. aws-cli even discards a key
        # left after an empty bucket ("s3:///k"), so normalize every such form to
        # the bare service root the library accepts.
        rest = target[len("s3://") :] if target.startswith("s3://") else target
        if not rest.partition("/")[0]:
            target = "s3://"

        storage = S3Storage(target, client=ctx.client_factory(args))
        storage.validate()
        key_specified = bool(storage.key)

        matched = False
        total_objects = 0
        total_size = 0
        for info in S3().ls(
            storage,
            recursive=args.recursive,
            page_size=page_size,
            request_payer=args.request_payer,
            bucket_name_prefix=args.bucket_name_prefix,
            bucket_region=args.bucket_region,
        ):
            matched = True
            line = output.format_entry(
                info, recursive=args.recursive, human_readable=args.human_readable
            )
            sys.stdout.write(line + "\n")
            if info.kind is FileKind.FILE:
                total_objects += 1
                total_size += info.size or 0

        if args.summarize:
            sys.stdout.write(
                output.format_summary(total_objects, total_size, human_readable=args.human_readable)
            )

        # aws-cli parity: exit 1 when a key/prefix was given but nothing matched.
        return 1 if key_specified and not matched else 0
