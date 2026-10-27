"""The ``boto3-s3 cp`` subcommand: copy files/objects with ``aws s3 cp`` semantics."""

from __future__ import annotations

import argparse

# Loaded at dispatch once cp is determined (stage 2 of the lazy dispatch).
from boto3_s3 import StdioStorage, ValidationError
from boto3_s3.transferplan import item_paths, plan_transfer
from boto3_s3_cli import filters
from boto3_s3_cli.commands import transferargs
from boto3_s3_cli.commands.base import Command, Context


class CpCommand(Command):
    """Copy a local file or S3 object to another location locally or in S3."""

    name = "cp"
    help = "Copy a local file or S3 object to another location locally or in S3."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``cp``-specific arguments (the full ``aws s3 cp`` surface)."""
        transferargs.add_transfer_arguments(parser, include_expected_size=True)

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Copy and return an ``aws s3 cp``-style exit code.

        Exit-code shape (design/cli.md section 5.7/6): pre-pipeline errors
        keep their class - paramfile / blob loads, ``--metadata`` parsing, a
        malformed ``--grants``, path types, a path that names no bucket, the
        ``--endpoint-url`` scheme,
        the checksum/path-format pairing,
        SSE-C pairing,
        streaming with ``--recursive`` (either side) or a streaming
        *download* with ``--no-overwrite``, and the S3
        Express case-conflict rejection are 252; the bare integer
        conversion, the session profile resolution, the missing local
        source, a ``--recursive`` local source whose root the walk would
        skip, and the ``--recursive`` destination-directory pre-create
        are 255 - while everything raised by the transfer pipeline is rc 1,
        credentials that cannot be located included (the client builds
        without them; the first request fails)
        (``AssertionError`` excepted - an internal bug, re-raised):
        per-item failures stream ``<kind> failed:`` lines, anything that
        kills the run (a listing error, the single-source 404, a
        non-integer ``--expected-size``, a missing stdin) prints one
        ``fatal error:`` line - all suppressed by
        ``--quiet`` with the exit codes kept - and a warnings-only run
        exits 2. The boundary is ``finish_transfer``'s run span: the
        ``S3().cp`` call plus the ``--expected-size`` integer conversion
        just ahead of it.
        """
        head = transferargs.classify_paths(args, ctx, operation="cp")
        src, dest = head.src, head.dest
        src_type, dest_type = head.src_type, head.dest_type
        is_stream = transferargs.validate_streaming_paths(
            head, recursive=args.recursive, operation="cp"
        )
        if dest == "-" and args.no_overwrite:
            # aws-cli's _validate_no_overwrite_for_download_streaming: a
            # streaming download cannot honor --no-overwrite.
            raise ValidationError(
                "--no-overwrite parameter is not supported for streaming downloads",
                operation="cp",
            )
        paths_type = head.paths_type  # "locals3" | "s3local" | "s3s3" here
        transferargs.validate_checksum_paths_type(args, paths_type, operation="cp")
        transferargs.check_local_source(args, head, recursive=args.recursive, operation="cp")
        transferargs.validate_sse_c_pairing(args, paths_type, operation="cp")

        s3 = head.s3
        client = head.client
        src_location: object
        dest_location: object
        if src == "-":
            # The destination key reproduces aws-cli's quirk of appending the
            # source's basename - literally "-" - when the dest takes the source
            # name. A missing stdin stays aws-cli's in-flight fatal (rc 1): the check
            # lives in StdioStorage.open, reached inside the transfer below.
            src_location = StdioStorage()
            plan = plan_transfer(
                transferargs.path_storage(src, src_type),
                transferargs.path_storage(dest, dest_type),
                recursive=False,
            )
            dest, _compare_key = item_paths(plan, plan.src_root)
            dest_location = transferargs.build_s3_storage(f"s3://{dest}", client=client)
        elif dest == "-":
            src_location = transferargs.build_s3_storage(src, client=client)
            dest_location = StdioStorage()
        else:
            src_location, dest_location = transferargs.resolve_locations(args, ctx, head)

        # The stream route consults no filter (aws-cli applies none to a stream).
        item_filter = None if is_stream else filters.compile_filter(args.filters)
        transfer_config = transferargs.build_transfer_engine(ctx, s3, client, paths_type=paths_type)
        # aws-cli validates --case-conflict against S3 Express while building the
        # run's instructions, past the transfer manager.
        case_conflict = transferargs.resolve_case_conflict(args, src, paths_type, operation="cp")
        options = transferargs.build_transfer_options(
            args,
            case_conflict,
        )
        if args.recursive and paths_type == "s3local":
            # The dir_op half of aws-cli's _validate_path_args, once every check
            # above has passed (transferargs.create_local_dest_dir).
            transferargs.create_local_dest_dir(dest, operation="cp")
        # Streams force the errors-only printer (aws-cli is_stream rule):
        # a streaming download owns stdout for the object bytes.
        printer = transferargs.build_printer(
            args, head.progress_frequency, only_show_errors=is_stream
        )

        def run_cp() -> None:
            # Both conversions fail in-pipeline like aws-cli: --expected-size is
            # a bare int() at submit time, stdin resolves when first needed.
            # aws-cli only ever converts --expected-size on the streaming-upload
            # route (UploadStreamRequestSubmitter); on every other route the
            # value is untouched and ignored, so a non-integer there stays
            # rc 0 - and a dryrun never converts either (aws-cli branches to the
            # dryrun record before building the live subscribers, so
            # `cp - s3://b/k --expected-size abc --dryrun` is rc 0, measured).
            expected_size = None
            if src == "-" and args.expected_size is not None and not args.dryrun:
                expected_size = int(args.expected_size)
            s3.cp(
                src_location,  # pyright: ignore[reportArgumentType]
                dest_location,  # pyright: ignore[reportArgumentType]
                recursive=args.recursive,
                filter=item_filter,
                dryrun=args.dryrun,
                expected_size=expected_size,
                on_progress=printer.on_progress if printer.wants_progress else None,
                on_result=printer.on_result,
                transfer_config=transfer_config,
                **options,
            )

        return transferargs.finish_transfer(printer, quiet=args.quiet, run=run_cp)


__all__ = ["CpCommand"]
