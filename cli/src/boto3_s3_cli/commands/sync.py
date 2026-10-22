"""The ``boto3-s3 sync`` subcommand: synchronize trees with ``aws s3 sync`` semantics."""

from __future__ import annotations

import argparse

# Loaded at dispatch once sync is determined (stage 2 of the lazy dispatch).
from boto3_s3 import ValidationError
from boto3_s3.awsclicompare import AwsCliComparison
from boto3_s3_cli import filters
from boto3_s3_cli.commands import transferargs
from boto3_s3_cli.commands.base import Command, Context


class SyncCommand(Command):
    """Syncs directories and S3 prefixes."""

    name = "sync"
    help = "Syncs directories and S3 prefixes."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``sync`` arguments: the shared transfer surface without
        ``--recursive`` / ``--expected-size`` (sync is inherently recursive
        and has no streaming form - aws rejects both as unknown options),
        plus the strategy flags."""
        transferargs.add_transfer_arguments(parser, include_recursive=False)
        parser.add_argument(
            "--delete",
            action="store_true",
            help="delete destination entries that are no longer in the source",
        )
        parser.add_argument(
            "--size-only",
            action="store_true",
            help="compare by size alone, ignoring modification times",
        )
        parser.add_argument(
            "--exact-timestamps",
            action="store_true",
            help="on download, treat any differing timestamp as a change",
        )

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Sync and return an ``aws s3 sync``-style exit code.

        The shape is cp's (design/cli.md section 6) with sync's own usage
        errors: the local-local pair 252, a path that names no bucket 252,
        any ``-`` path 252 ("Streaming currently is only compatible with
        non-recursive cp commands" - cp-worded even here), the checksum /
        SSE-C pairings 252, and an S3 Express directory bucket or one of
        its access points on either side 252 ("Cannot use sync command with
        a directory bucket."; aws refuses the bucket only). A missing local
        source, and one whose root the walk would skip (a file, an
        unreadable directory, a symlink under ``--no-follow-symlinks``),
        exit 255; the
        ``--exclude`` / ``--include`` patterns compile once and apply to both
        sides (design/sync.md section 1).
        """
        head = transferargs.classify_paths(args, ctx, operation="sync")
        src, dest = head.src, head.dest
        transferargs.validate_streaming_paths(head, recursive=True, operation="sync")
        paths_type = head.paths_type  # "locals3" | "s3local" | "s3s3" here
        transferargs.validate_checksum_paths_type(args, paths_type, operation="sync")
        transferargs.check_local_source(args, head, recursive=True, operation="sync")
        transferargs.validate_sse_c_pairing(args, paths_type, operation="sync")
        if any(transferargs.lists_unordered(path) for path in (src, dest)):
            # aws-cli's _validate_not_s3_express_bucket_for_sync: directory-bucket
            # listings are not lexicographic, so sync rejects them outright.
            # aws tests only the --x-s3 suffix and so runs a sync through an
            # access point of a directory bucket (--xa-s3), whose listing is
            # just as unordered: --delete then removes destination keys the
            # source has too. That is refused here as well (a deliberate
            # difference, docs/cli/aws-differences.md).
            raise ValidationError(
                "Cannot use sync command with a directory bucket.", operation="sync"
            )
        case_conflict = transferargs.resolve_case_conflict(
            args, src, paths_type, operation="sync", recursive=True
        )
        s3 = head.s3
        client = head.client

        src_location, dest_location = transferargs.resolve_locations(args, ctx, head)

        # One filter applied to both sides by S3.sync (design/sync.md section
        # 1): each side's entries carry their path below that side's base, as
        # aws joins every pattern onto each side's rootdir.
        item_filter = filters.compile_filter(args.filters)
        transfer_config = transferargs.build_transfer_engine(ctx, s3, client, paths_type=paths_type)
        # After the engine is resolved: the upload checksum default is the
        # engine's.
        options = transferargs.build_transfer_options(
            args,
            case_conflict,
            default_checksum_algorithm=transferargs.default_upload_checksum(
                client, paths_type, transfer_config
            ),
        )
        if head.dest_type == "local":
            # The s3local destination aws's _validate_path_args creates, once
            # every check above has passed (transferargs.create_local_dest_dir).
            transferargs.create_local_dest_dir(dest, operation="sync")
        printer = transferargs.build_printer(args, head.progress_frequency)

        def run_sync() -> None:
            s3.sync(
                src_location,  # type: ignore[arg-type]
                dest_location,  # type: ignore[arg-type]
                delete_filter=args.delete,
                update_filter=AwsCliComparison(
                    size_only=args.size_only, exact_timestamps=args.exact_timestamps
                ),
                filter=item_filter,
                dryrun=args.dryrun,
                on_progress=printer.on_progress if printer.wants_progress else None,
                on_result=printer.on_result,
                transfer_config=transfer_config,
                **options,
            )

        return transferargs.finish_transfer(printer, quiet=args.quiet, run=run_sync)


__all__ = ["SyncCommand"]
