"""The ``boto3-s3 ls`` subcommand: list S3 objects, common prefixes, or buckets."""

from __future__ import annotations

import argparse
import sys

from boto3_s3 import FileInfo, FileKind, S3Storage
from boto3_s3_cli import clientfactory, globalargs, output
from boto3_s3_cli.commands.base import (
    Command,
    Context,
    add_page_size_argument,
    add_request_payer_argument,
    expand_integer_paramfile,
    expand_option_paramfile,
    expand_positional_paramfile,
    parse_integer_option,
)


class LsCommand(Command):
    """List objects/prefixes (or all buckets) with ``aws s3 ls`` semantics."""

    name = "ls"
    help = "List S3 objects and common prefixes under a prefix or all S3 buckets."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``ls``-specific arguments to its subparser."""
        parser.add_argument(
            "paths",
            nargs="?",
            default="s3://",
            metavar="<S3Uri>",
            help="prefix to list; omit it to list every bucket",
        )
        parser.add_argument(
            "--recursive", action="store_true", help="list every key under the prefix"
        )
        add_page_size_argument(parser)
        add_request_payer_argument(parser)
        parser.add_argument(
            "--human-readable", action="store_true", help="show sizes in KiB / MiB / GiB"
        )
        parser.add_argument(
            "--summarize", action="store_true", help="append the total object count and size"
        )
        # Bucket-listing filters (ListBuckets Prefix / BucketRegion); accepted but
        # inert for object listings, like aws-cli.
        parser.add_argument(
            "--bucket-name-prefix",
            metavar="PREFIX",
            help="when listing buckets, keep only names starting with PREFIX",
        )
        parser.add_argument(
            "--bucket-region",
            metavar="REGION",
            help="when listing buckets, keep only those in REGION",
        )

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """List objects/prefixes (or all buckets) and return an ``aws s3``-style code."""
        # aws's parse-time order (measured, design/cli.md section 6): the --query
        # JMESPath compile (252) leads, then the --endpoint-url scheme check
        # (252), then each option at its own slot in aws's option-table order -
        # the positional, then --page-size (paramfile load 252, then the bare
        # int() coercion 255), then the bucket-listing filters. So a bad
        # --page-size value fires ahead of a bad --bucket-name-prefix /
        # --bucket-region paramfile (measured on the pinned aws-cli).
        globalargs.validate_query(args)
        clientfactory.validate_endpoint_url(args)
        expand_positional_paramfile(args, "paths", name="paths", operation="ls")
        expand_integer_paramfile(args, "page_size", operation="ls")
        page_size = parse_integer_option(args.page_size, operation="ls")
        expand_option_paramfile(args, "bucket_name_prefix", operation="ls")
        expand_option_paramfile(args, "bucket_region", operation="ls")
        # Everything above is aws's parse layer; the client comes next, in
        # aws's own slot (S3Command._run_main), ahead of all path handling. So
        # an empty region is its "Invalid endpoint" 255 rather than the bytes
        # crash just below, and a merely absent region still builds.
        s3 = ctx.s3(args)
        client = s3.client()
        # Intentional aws-cli bug parity: a readable positional fileb:// is
        # still bytes here. Calling bytes.startswith(str) raises TypeError,
        # which the general handler maps to 255.
        if isinstance(args.paths, bytes):
            raise TypeError("startswith first arg must be bytes or a tuple of bytes, not str")
        target: str = args.paths
        # A target with no bucket lists all buckets. aws-cli even discards a key
        # left after an empty bucket ("s3:///k"), so normalize every such form to
        # the bare service root the library accepts.
        rest = target[len("s3://") :] if target.startswith("s3://") else target
        if not rest.partition("/")[0]:
            target = "s3://"

        storage = S3Storage(target, client=client, page_size=page_size)
        storage.validate()
        key_specified = bool(storage.key)

        matched = False
        total_objects = 0
        total_size = 0

        def print_result(info: FileInfo) -> None:
            """Render one listing entry and update the optional summary totals."""
            nonlocal matched, total_objects, total_size
            matched = True
            line = output.format_entry(
                info, recursive=args.recursive, human_readable=args.human_readable
            )
            # uni_write (aws's uni_print): an unencodable key on a narrow
            # console/pipe encoding must not abort the listing mid-way - aws
            # prints it with replacements and finishes rc 0.
            output.uni_write(sys.stdout, line + "\n")
            if info.kind is FileKind.FILE:
                total_objects += 1
                total_size += info.size or 0

        s3.ls(
            storage,
            on_entry=print_result,
            recursive=args.recursive,
            request_payer=args.request_payer,
            bucket_name_prefix=args.bucket_name_prefix,
            bucket_region=args.bucket_region,
        )

        if args.summarize:
            output.uni_write(
                sys.stdout,
                output.format_summary(
                    total_objects, total_size, human_readable=args.human_readable
                ),
            )

        # aws-cli parity: exit 1 when a key/prefix was given but nothing matched.
        return 1 if key_specified and not matched else 0
