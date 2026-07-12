"""The ``boto3-s3 sync`` subcommand: synchronize trees with ``aws s3 sync`` semantics."""

from __future__ import annotations

import argparse
import os

# Loaded only once sync is determined (stage 2 of the lazy dispatch).
from boto3_s3 import NotFoundError, ValidationError
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
        parser.add_argument("--delete", action="store_true")
        parser.add_argument("--size-only", action="store_true")
        parser.add_argument("--exact-timestamps", action="store_true")

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Sync and return an ``aws s3 sync``-style exit code.

        The shape is cp's (docs/cli.md section 6) with sync's own usage errors in
        aws-cli order: the local-local pair 252, any
        ``-`` path 252 ("Streaming currently is only compatible with
        non-recursive cp commands" - cp-worded even here), the checksum /
        SSE-C pairings 252, and an S3 Express directory bucket on either
        side 252 ("Cannot use sync command with a directory bucket.") - all
        before any S3 client exists. A missing local source exits 255; the
        ``--exclude`` / ``--include`` patterns compile once and apply to both
        sides (rootless anchoring; sync.md section 1).
        """
        head = transferargs.classify_paths(args, ctx, operation="sync")
        page_size, progress_frequency = head.page_size, head.progress_frequency
        src, dest = head.src, head.dest
        src_type, dest_type = head.src_type, head.dest_type
        if src == "-" or dest == "-":
            # aws-cli's _validate_streaming_paths: only cp streams, and its
            # wording names cp even from sync.
            raise ValidationError(
                "Streaming currently is only compatible with non-recursive cp commands",
                operation="sync",
            )
        paths_type = head.paths_type  # "locals3" | "s3local" | "s3s3" here
        transferargs.validate_checksum_paths_type(args, paths_type, operation="sync")
        # aws-cli's _validate_path_args (one function) does BOTH the missing-source
        # check and the s3local dest-dir creation, and runs entirely BEFORE
        # _validate_sse_c_args and the directory-bucket check. So both 255 checks
        # below precede the SSE-C / S3 Express 252 checks: when more than one
        # fails, that order decides the exit code.
        if src_type == "local" and not os.path.exists(src):
            # Missing local source: aws's bare RuntimeError -> rc 255
            # (NotFoundError without a ClientError cause maps the same).
            raise NotFoundError(f"The user-provided path {src} does not exist.", operation="sync")
        if dest_type == "local":
            # aws creates the s3local destination during validation (before
            # run): a creation failure is the pre-pipeline rc 255.
            transferargs.create_local_dest_dir(dest, operation="sync")
        transferargs.validate_sse_c_pairing(args, paths_type, operation="sync")
        if transferargs.is_s3express_path(src) or transferargs.is_s3express_path(dest):
            # aws-cli's _validate_not_s3express_bucket_for_sync: directory-bucket
            # listings are not lexicographic, so sync rejects them outright.
            raise ValidationError(
                "Cannot use sync command with a directory bucket.", operation="sync"
            )
        case_conflict = transferargs.resolve_case_conflict(
            args, src, paths_type, operation="sync", recursive=True
        )
        options = transferargs.build_transfer_options(args, case_conflict, operation="sync")

        s3 = head.s3
        client = s3.client()
        src_location, dest_location = transferargs.resolve_locations(
            args,
            ctx,
            s3,
            client,
            src,
            dest,
            src_type=src_type,
            dest_type=dest_type,
            page_size=page_size,
        )

        # One symmetric filter applied to both sides by S3.sync (sync.md section
        # 1). It needs no root: a relative pattern matches each entry's
        # compare_key, an absolute one its full key, so the same filter prunes
        # the source and destination per-side (globsieve.Anchored).
        item_filter = filters.compile_filter(args.filters)
        transfer_config = transferargs.resolve_transfer_config(ctx, s3, paths_type=paths_type)
        printer = transferargs.build_printer(args, progress_frequency)

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
