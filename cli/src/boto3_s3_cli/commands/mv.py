"""The ``boto3-s3 mv`` subcommand: move files/objects with ``aws s3 mv`` semantics."""

from __future__ import annotations

import argparse
import os
import sys

# Loaded only once mv is determined (stage 2 of the lazy dispatch).
from boto3_s3 import (
    S3,
    NotFoundError,
    S3PathResolver,
    S3Storage,
    ValidationError,
    has_underlying_s3_path,
)
from boto3_s3_cli import filters
from boto3_s3_cli.commands import transferargs
from boto3_s3_cli.commands.base import Command, Context

_VALIDATE_ENV_VAR = "AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS"

# aws-cli's _emit_validate_s3_paths_warning, verbatim (emitted to stderr,
# execution continues, rc unaffected).
_VALIDATE_WARNING = (
    "warning: Provided s3 paths may resolve to same underlying "
    "s3 object(s) and result in deletion instead of being moved. "
    "To resolve and validate underlying s3 paths are not the same, "
    "specify the --validate-same-s3-paths flag or set the "
    "AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS environment variable to true. "
    "To resolve s3 outposts access point path, the arn must be "
    "used instead of the alias.\n"
)


class MvCommand(Command):
    """Move a local file or S3 object to another location locally or in S3."""

    name = "mv"
    help = "Move a local file or S3 object to another location locally or in S3."

    def configure(self, parser: argparse.ArgumentParser) -> None:
        """Add the ``mv`` arguments: the shared transfer surface plus
        ``--validate-same-s3-paths`` (and no ``--expected-size`` - mv has no
        streaming form)."""
        transferargs.add_transfer_arguments(parser)
        parser.add_argument("--validate-same-s3-paths", action="store_true")

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        """Move and return an ``aws s3 mv``-style exit code.

        The shape is cp's (docs/cli.md section 6) with mv's own usage errors in
        aws-cli order: the local-local pair 252, any
        ``-`` path 252 ("Streaming currently is only compatible with
        non-recursive cp commands"), and for s3->s3 the onto-itself guard /
        ``--validate-same-s3-paths`` resolution / access-point warning -
        all before any S3 client exists. Resolution failures keep their
        class: an unresolvable path 252, a failing s3control/sts call 254.
        """
        head = transferargs.classify_paths(args, ctx, operation="mv")
        page_size, progress_frequency = head.page_size, head.progress_frequency
        src, dest = head.src, head.dest
        src_type, dest_type = head.src_type, head.dest_type
        if src == "-" or dest == "-":
            # aws-cli's _validate_streaming_paths: only cp streams, and its
            # wording names cp even from mv.
            raise ValidationError(
                "Streaming currently is only compatible with non-recursive cp commands",
                operation="mv",
            )
        paths_type = head.paths_type  # "locals3" | "s3local" | "s3s3" here
        if paths_type == "s3s3":
            self._validate_same_paths(args, ctx, head.s3, src, dest)
        transferargs.validate_checksum_paths_type(args, paths_type, operation="mv")
        if src_type == "local" and not os.path.exists(src):
            # aws-cli's _validate_path_args checks the missing local source (its bare
            # RuntimeError -> rc 255) right after the checksum/path pairing and
            # before SSE-C (rc 252), so when both fail that order decides the exit
            # code. NotFoundError without a ClientError cause maps to the same rc 255.
            raise NotFoundError(f"The user-provided path {src} does not exist.", operation="mv")
        if args.recursive and paths_type == "s3local":
            # The dir_op half of aws's _validate_path_args: the s3local
            # destination is created during validation (pre-pipeline rc 255).
            transferargs.create_local_dest_dir(dest, operation="mv")
        transferargs.validate_sse_c_pairing(args, paths_type, operation="mv")
        case_conflict = transferargs.resolve_case_conflict(args, src, paths_type, operation="mv")
        options = transferargs.build_transfer_options(args, case_conflict, operation="mv")

        s3 = head.s3
        client = s3.client()
        # --no-overwrite on uploads/copies needs a botocore with S3 conditional
        # writes; reject up front (rc 252) on an older one (docs/overview.md
        # section 2). Placed after the client exists so its model can be probed.
        transferargs.validate_no_overwrite_supported(
            args.no_overwrite, paths_type, client, operation="mv"
        )
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

        item_filter = filters.compile_filter(args.filters)
        transfer_config = transferargs.resolve_transfer_config(ctx, s3, paths_type=paths_type)
        printer = transferargs.build_printer(args, progress_frequency)

        def run_mv() -> None:
            s3.mv(
                src_location,  # type: ignore[arg-type]
                dest_location,  # type: ignore[arg-type]
                recursive=args.recursive,
                filter=item_filter,
                dryrun=args.dryrun,
                on_progress=printer.on_progress if printer.wants_progress else None,
                on_result=printer.on_result,
                transfer_config=transfer_config,
                **options,
            )

        return transferargs.finish_transfer(printer, quiet=args.quiet, run=run_mv)

    @staticmethod
    def _validate_same_paths(
        args: argparse.Namespace, ctx: Context, s3: S3, src: str, dest: str
    ) -> None:
        """The mv s3->s3 validation block (aws-cli's ``_validate_path_args`` head).

        Always: the textual onto-itself guard on the keyless-normalized URIs
        (the form aws prints - ``mv s3://b/k s3://b`` reports ``s3://b/``),
        ``--recursive`` included. When validation is on (the flag, or the
        env variable set to ``true`` case-insensitively - aws-cli's
        ``ensure_boolean``, so ``TRUE`` / ``True`` count too, any other value
        off) and the *keys* match, both sides resolve
        through ``S3PathResolver`` - the source-side s3control client in
        ``--source-region``, the destination's in ``--region``, sts without
        one (aws-cli's ``from_session`` wiring) - and every resolved pair runs
        the same guard, still reporting the *original* URIs. When validation
        is off but a side looks access-point-shaped, aws's standing warning
        goes to stderr and the move proceeds.
        """
        norm_src = S3Storage.normalize_s3_uri(src)
        norm_dest = S3Storage.normalize_s3_uri(dest)
        message = f"Cannot mv a file onto itself: {norm_src} - {norm_dest}"
        if S3Storage.same_path(norm_src, norm_dest):
            raise ValidationError(message, operation="mv")
        if not S3Storage.same_key(norm_src, norm_dest):
            return
        enabled = (
            args.validate_same_s3_paths or os.environ.get(_VALIDATE_ENV_VAR, "").lower() == "true"
        )
        if enabled:
            src_resolver = S3PathResolver(
                ctx.service_client("s3control", args, s3, region=args.source_region),
                ctx.service_client("sts", args, s3),
            )
            dest_resolver = S3PathResolver(
                ctx.service_client("s3control", args, s3, region=args.region),
                ctx.service_client("sts", args, s3),
            )
            src_paths = src_resolver.resolve_underlying_s3_paths(norm_src)
            dest_paths = dest_resolver.resolve_underlying_s3_paths(norm_dest)
            for src_path in src_paths:
                for dest_path in dest_paths:
                    if S3Storage.same_path(src_path, dest_path):
                        raise ValidationError(message, operation="mv")
        elif has_underlying_s3_path(norm_src) or has_underlying_s3_path(norm_dest):
            sys.stderr.write(_VALIDATE_WARNING)


__all__ = ["MvCommand"]
