"""The ``boto3-s3 cp`` subcommand: copy files/objects with ``aws s3 cp`` semantics."""

from __future__ import annotations

import argparse
import os
import sys

# Pure-Python names only (exceptions / naming modules) - safe on the
# parse path; S3 / S3Storage reach botocore and are imported in run() instead
# (import contract, docs/imports.md).
from boto3_s3 import Boto3S3Error, ValidationError
from boto3_s3.naming import classify, item_paths, plan_transfer
from boto3_s3_cli import filters
from boto3_s3_cli.commands import transferargs
from boto3_s3_cli.commands.base import Command, Context, parse_integer_option
from boto3_s3_cli.progress import TransferPrinter

_USAGE = (
    "usage: boto3-s3 cp <LocalPath> <S3Uri> or <S3Uri> <LocalPath> or <S3Uri> <S3Uri>\n"
    "Error: Invalid argument type"
)


class CpCommand(Command):
    """Copy a local file or S3 object to another location locally or in S3."""

    name = "cp"
    help = "Copy a local file or S3 object to another location locally or in S3."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``cp``-specific arguments (the full ``aws s3 cp`` surface)."""
        transferargs.add_transfer_arguments(parser, include_expected_size=True)

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Copy and return an ``aws s3 cp``-style exit code.

        Exit-code shape (docs/cli.md section 6): pre-pipeline errors keep
        their class - usage errors 252 (path types, SSE-C pairing, the
        checksum/path-format pairing, streaming with ``--recursive`` or
        ``--no-overwrite``, ``--no-overwrite`` on a botocore without S3
        conditional writes, ``--metadata`` parsing, blob decoding, the S3
        Express case-conflict rejection), the bare integer conversion and
        the missing local source 255, client construction 253 - while
        everything raised by the transfer pipeline is rc 1: per-item
        failures stream ``<kind> failed:`` lines, anything that kills the
        run (a listing error, the single-source 404, a bad ``--grants``
        shape, a non-integer ``--expected-size``, a missing stdin) prints
        one ``fatal error:`` line, and a warnings-only run exits 2. The
        boundary is the ``S3().cp`` call.
        """
        page_size = parse_integer_option(args.page_size, operation="cp")
        progress_frequency = parse_integer_option(args.progress_frequency, operation="cp")
        src, dst = args.paths
        src_type = classify(src)
        dst_type = classify(dst)
        if src_type == "local" and dst_type == "local":
            raise ValidationError(_USAGE, operation="cp")
        is_stream = src == "-" or dst == "-"
        if is_stream:
            # aws-cli's _validate_streaming_paths: cp-only, never recursive, and
            # a streaming download cannot honor --no-overwrite.
            if args.recursive:
                raise ValidationError(
                    "Streaming currently is only compatible with non-recursive cp commands",
                    operation="cp",
                )
            if dst == "-" and args.no_overwrite:
                raise ValidationError(
                    "--no-overwrite parameter is not supported for streaming downloads",
                    operation="cp",
                )
        paths_type = src_type + dst_type  # "locals3" | "s3local" | "s3s3" here
        transferargs.validate_checksum_paths_type(args, paths_type, operation="cp")
        if src_type == "local" and src != "-" and not os.path.exists(src):
            # aws-cli's _validate_path_args checks the missing local source (its bare
            # RuntimeError -> rc 255) right after the checksum/path pairing and
            # before SSE-C (rc 252), so when both fail that order decides the exit
            # code; '-' is a stream, not a path.
            raise Boto3S3Error(f"The user-provided path {src} does not exist.", operation="cp")
        transferargs.validate_sse_c_pairing(args, paths_type, operation="cp")
        case_conflict = transferargs.resolve_case_conflict(args, src, paths_type, operation="cp")
        options = transferargs.build_transfer_options(args, case_conflict, operation="cp")

        # Deferred: dispatch is the first point that needs the library's S3
        # entry (whose chain reaches botocore); --help and usage errors stay
        # SDK-free (import contract, docs/imports.md).
        from boto3_s3 import S3, S3Storage

        client = ctx.client_factory(args)
        # --no-overwrite on uploads/copies needs a botocore with S3 conditional
        # writes; reject up front (rc 252) on an older one (docs/overview.md
        # section 2). Placed after the client exists so its model can be probed.
        transferargs.validate_no_overwrite_supported(
            args.no_overwrite, paths_type, client, operation="cp"
        )
        src_location: object
        dst_location: object
        if src == "-":
            # Resolved inside the pipeline boundary below (a missing stdin is
            # aws's in-flight fatal, rc 1); the destination key reproduces
            # aws's quirk of appending the source's basename - literally "-"
            # - when the destination takes the source's name.
            src_location = None
            plan = plan_transfer(src, dst, recursive=False)
            dest, _compare_key = item_paths(plan, plan.src_root)
            dst_location = S3Storage(f"s3://{dest}", client=client)
        elif dst == "-":
            src_location = S3Storage(src, client=client)
            dst_location = _stdout_writer()
        else:
            src_location, dst_location = transferargs.resolve_locations(
                args, ctx, client, src, dst, src_type=src_type, dst_type=dst_type
            )

        item_filter = None
        if not is_stream:
            plan = plan_transfer(src, dst, recursive=args.recursive)
            item_filter = filters.compile_for_root(args.filters, root=plan.filter_root)
        transfer_config = transferargs.resolve_transfer_config(args, ctx, paths_type=paths_type)
        printer = TransferPrinter(
            quiet=args.quiet,
            # Streams force the errors-only printer (aws-cli is_stream rule):
            # a streaming download owns stdout for the object bytes.
            only_show_errors=args.only_show_errors or is_stream,
            progress=args.progress,
            frequency=progress_frequency,
            multiline=args.progress_multiline,
        )

        def run_cp() -> None:
            # Both conversions fail in-pipeline like aws: --expected-size is
            # a bare int() at submit time, stdin resolves when first needed.
            # aws only ever converts --expected-size on the streaming-upload
            # route (UploadStreamRequestSubmitter); on every other route the
            # value is untouched and ignored, so a non-integer there stays rc 0.
            expected_size = None
            if src == "-" and args.expected_size is not None:
                expected_size = int(args.expected_size)
            source: object = src_location
            if src == "-":
                source = _binary_stdin()
            S3().cp(
                source,  # type: ignore[arg-type]
                dst_location,  # type: ignore[arg-type]
                recursive=args.recursive,
                filter=item_filter,
                follow_symlinks=args.follow_symlinks,
                dryrun=args.dryrun,
                page_size=page_size,
                expected_size=expected_size,
                on_progress=printer.on_progress if printer.wants_progress else None,
                on_result=printer.on_result,
                transfer_config=transfer_config,
                **options,
            )

        return transferargs.finish_transfer(printer, quiet=args.quiet, run=run_cp)


class _NonSeekableStream:
    """Read-only view of a file-like object (aws-cli's ``NonSeekableStream``).

    Some streams that are not truly seekable still *look* seekable (Windows
    stdin), which would mislead s3transfer's input manager; exposing only
    ``read`` forces the buffered non-seekable upload path, like aws.
    """

    def __init__(self, fileobj: object) -> None:
        self._fileobj = fileobj

    def read(self, amt: int | None = None) -> bytes:
        reader: object = self._fileobj
        if amt is None:
            return reader.read()  # type: ignore[attr-defined]
        return reader.read(amt)  # type: ignore[attr-defined]


def _binary_stdin() -> _NonSeekableStream:
    """The process's binary stdin, or aws's in-pipeline error when absent."""
    stdin = sys.stdin
    if stdin is None:
        raise Boto3S3Error(
            "stdin is required for this operation, but is not available.", operation="cp"
        )
    return _NonSeekableStream(getattr(stdin, "buffer", stdin))


def _stdout_writer() -> object:
    """The process's binary stdout for streaming downloads."""
    return getattr(sys.stdout, "buffer", sys.stdout)


__all__ = ["CpCommand"]
