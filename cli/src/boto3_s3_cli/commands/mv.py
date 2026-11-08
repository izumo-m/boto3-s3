"""The ``boto3-s3 mv`` subcommand: move files/objects with ``aws s3 mv`` semantics."""

from __future__ import annotations

import argparse
import os

# Loaded at dispatch once mv is determined (stage 2 of the lazy dispatch).
from boto3_s3 import OpResult
from boto3_s3_cli import filters, output
from boto3_s3_cli.commands import transferargs
from boto3_s3_cli.commands.base import Command, Context

_VALIDATE_ENV_VAR = "AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS"


class MvCommand(Command):
    """Move a local file or S3 object to another location locally or in S3."""

    name = "mv"
    help = "Move a local file or S3 object to another location locally or in S3."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``mv`` arguments: the shared transfer surface plus
        ``--validate-same-s3-paths`` (and no ``--expected-size`` - mv has no
        streaming form)."""
        transferargs.add_transfer_arguments(parser)
        parser.add_argument(
            "--validate-same-s3-paths",
            action="store_true",
            help="resolve access points before checking that source and destination differ",
        )

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Move and return an ``aws s3 mv``-style exit code.

        The shape is cp's (design/cli.md section 6) with mv's own usage
        errors: the local-local pair 252, a path that names no bucket 252,
        any ``-`` path 252 ("Streaming currently is only compatible with
        non-recursive cp commands"), and for s3->s3 the onto-itself guard /
        ``--validate-same-s3-paths`` resolution / access-point warning. A
        missing local source, and a ``--recursive`` one whose root the walk
        would skip, exit 255.
        Resolution failures keep their class: an unresolvable path 252, a
        failing s3control/sts call its translated category (a
        ClientError-caused failure 254, a transport failure 255).
        """
        head = transferargs.classify_paths(args, ctx, operation="mv")
        src, dest = head.src, head.dest
        transferargs.validate_streaming_paths(head, recursive=args.recursive, operation="mv")
        paths_type = head.paths_type  # "locals3" | "s3local" | "s3s3" here
        if paths_type == "s3s3":
            # Before the run, as aws-cli's _validate_path_args: the same object
            # is a usage error (252) and a resolver failure keeps its class,
            # where the run's own failures are its fatal error (rc 1).
            head.s3.check_mv_paths(
                src,
                dest,
                validate_same_s3_paths=args.validate_same_s3_paths
                or os.environ.get(_VALIDATE_ENV_VAR, "").lower() == "true",
                source_region=args.source_region,
                on_result=_write_notice,
            )
        transferargs.validate_checksum_paths_type(args, paths_type, operation="mv")
        transferargs.check_local_source(args, head, recursive=args.recursive, operation="mv")
        transferargs.validate_sse_c_pairing(args, paths_type, operation="mv")

        s3 = head.s3
        client = head.client
        src_location, dest_location = transferargs.resolve_locations(args, ctx, head)

        item_filter = filters.compile_filter(args.filters)
        transfer_config = transferargs.build_transfer_engine(ctx, s3, client, paths_type=paths_type)
        # aws-cli validates --case-conflict against S3 Express while building the
        # run's instructions, past the transfer manager.
        case_conflict = transferargs.resolve_case_conflict(args, src, paths_type, operation="mv")
        options = transferargs.build_transfer_options(
            args,
            case_conflict,
        )
        if args.recursive and paths_type == "s3local":
            # The dir_op half of aws-cli's _validate_path_args, once every check
            # above has passed (transferargs.create_local_dest_dir).
            transferargs.create_local_dest_dir(dest, operation="mv")
        printer = transferargs.build_printer(args, head.progress_frequency)

        def run_mv() -> None:
            s3.mv(
                src_location,  # pyright: ignore[reportArgumentType]
                dest_location,  # pyright: ignore[reportArgumentType]
                recursive=args.recursive,
                filter=item_filter,
                dryrun=args.dryrun,
                on_progress=printer.on_progress if printer.wants_progress else None,
                on_result=printer.on_result,
                transfer_config=transfer_config,
                validate_same_s3_paths=None,  # checked above, before the run
                **options,
            )

        return transferargs.finish_transfer(printer, quiet=args.quiet, run=run_mv)


def _write_notice(result: OpResult) -> None:
    """aws-cli's standing same-path warning, straight to stderr before the run.

    aws-cli's ``_emit_validate_s3_paths_warning`` writes it before any result
    line and counts it as nothing (rc unaffected), ``--quiet`` included.
    """
    output.write_stderr(f"{result.error}\n")


__all__ = ["MvCommand"]
