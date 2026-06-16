"""The ``boto3-s3 sync`` subcommand: synchronize trees with ``aws s3 sync`` semantics."""

from __future__ import annotations

import argparse
import os

# Pure-Python names only (exceptions / naming modules) - safe on the parse
# path; S3 / S3Storage reach botocore and are imported in run() instead
# (import contract, docs/imports.md).
from boto3_s3 import Boto3S3Error, ValidationError
from boto3_s3.naming import classify, plan_transfer
from boto3_s3_cli import filters
from boto3_s3_cli.commands import transferargs
from boto3_s3_cli.commands.base import Command, Context, parse_integer_option
from boto3_s3_cli.progress import TransferPrinter

_USAGE = (
    "usage: boto3-s3 sync <LocalPath> <S3Uri> or <S3Uri> <LocalPath> or <S3Uri> <S3Uri>\n"
    "Error: Invalid argument type"
)


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
        ``--exclude`` / ``--include`` patterns compile once against the source
        root and apply to both sides (sync.md section 1).
        """
        page_size = parse_integer_option(args.page_size, operation="sync")
        progress_frequency = parse_integer_option(args.progress_frequency, operation="sync")
        src, dst = args.paths
        src_type = classify(src)
        dst_type = classify(dst)
        if src_type == "local" and dst_type == "local":
            raise ValidationError(_USAGE, operation="sync")
        if src == "-" or dst == "-":
            # aws-cli's _validate_streaming_paths: only cp streams, and its
            # wording names cp even from sync.
            raise ValidationError(
                "Streaming currently is only compatible with non-recursive cp commands",
                operation="sync",
            )
        paths_type = src_type + dst_type  # "locals3" | "s3local" | "s3s3" here
        transferargs.validate_checksum_paths_type(args, paths_type, operation="sync")
        # aws-cli's _validate_path_args (one function) does BOTH the missing-source
        # check and the s3local dest-dir creation, and runs entirely BEFORE
        # _validate_sse_c_args and the directory-bucket check. So both 255 checks
        # below precede the SSE-C / S3 Express 252 checks: when more than one
        # fails, that order decides the exit code.
        if src_type == "local" and not os.path.exists(src):
            # Missing local source: aws's bare RuntimeError -> rc 255.
            raise Boto3S3Error(f"The user-provided path {src} does not exist.", operation="sync")
        if dst_type == "local" and not os.path.exists(dst):
            # aws creates the s3local destination during validation (before run),
            # so an OSError here is its pre-pipeline rc 255 - not the transfer
            # pipeline's rc 1. Pre-creating it outside finish_transfer's catch
            # makes a creation failure surface with the validation exit code (the
            # library S3.sync still ensures the dir for direct callers; this
            # pre-check makes it a no-op there).
            try:
                os.makedirs(dst)
            except OSError as exc:
                raise Boto3S3Error(str(exc), operation="sync") from exc
        transferargs.validate_sse_c_pairing(args, paths_type, operation="sync")
        if transferargs.is_s3express_path(src) or transferargs.is_s3express_path(dst):
            # aws-cli's _validate_not_s3express_bucket_for_sync: directory-bucket
            # listings are not lexicographic, so sync rejects them outright.
            raise ValidationError(
                "Cannot use sync command with a directory bucket.", operation="sync"
            )
        case_conflict = transferargs.resolve_case_conflict(
            args, src, paths_type, operation="sync", recursive=True
        )
        options = transferargs.build_transfer_options(args, case_conflict, operation="sync")
        # no_overwrite rides the copy decision (DefaultCopyFilter) for sync, not
        # the engine options - drop it so it never reaches the transfer engine.
        options.pop("no_overwrite", None)

        # Deferred: dispatch is the first point that needs the library's S3
        # entry (whose chain reaches botocore); --help and usage errors stay
        # SDK-free (import contract, docs/imports.md).
        from boto3_s3 import S3, DefaultCopyFilter

        client = ctx.client_factory(args)
        src_location, dst_location = transferargs.resolve_locations(
            args, ctx, client, src, dst, src_type=src_type, dst_type=dst_type
        )

        plan = plan_transfer(src, dst, recursive=True)
        # One symmetric matcher, compiled against the source root and applied to
        # both sides by S3.sync (sync.md section 1; relative patterns are
        # root-independent, so one compilation suffices).
        matcher = filters.compile_for_root(args.filters, root=plan.filter_root)
        transfer_config = transferargs.resolve_transfer_config(args, ctx, paths_type=paths_type)
        printer = TransferPrinter(
            quiet=args.quiet,
            only_show_errors=args.only_show_errors,
            progress=args.progress,
            frequency=progress_frequency,
            multiline=args.progress_multiline,
        )

        def run_sync() -> None:
            S3().sync(
                src_location,  # type: ignore[arg-type]
                dst_location,  # type: ignore[arg-type]
                delete=args.delete,
                copy_filter=DefaultCopyFilter(
                    size_only=args.size_only,
                    exact_timestamps=args.exact_timestamps,
                    no_overwrite=args.no_overwrite,
                ),
                filter=matcher,
                follow_symlinks=args.follow_symlinks,
                dryrun=args.dryrun,
                page_size=page_size,
                on_progress=printer.on_progress if printer.wants_progress else None,
                on_result=printer.on_result,
                transfer_config=transfer_config,
                **options,
            )

        return transferargs.finish_transfer(printer, quiet=args.quiet, run=run_sync)


__all__ = ["SyncCommand"]
