"""The argument surface and run-pipeline pieces ``cp`` and ``mv`` share.

``aws s3`` declares one ``TRANSFER_ARGS`` list plus per-command extras
(aws-cli's ``subcommands.py``); this module is that shared list and the
validation/translation steps both commands run in the same order. Everything
here is SDK-free at import time (import contract, docs/imports.md) - the one
deferred ``boto3_s3`` import sits inside :func:`resolve_locations`, past
every usage error.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import TYPE_CHECKING, Any

# Pure-Python names only (exceptions / types modules) - safe on the parse path.
from boto3_s3 import (
    BatchError,
    Boto3S3Error,
    CaseConflictMode,
    CopyPropsMode,
    TransferOptions,
    ValidationError,
)
from boto3_s3.naming import split_bucket_key
from boto3_s3_cli import filters, shorthand

if TYPE_CHECKING:
    from collections.abc import Callable

    from boto3_s3 import LocalStorage, S3Storage
    from boto3_s3_cli.commands.base import Context
    from boto3_s3_cli.progress import TransferPrinter

# aws-cli choice lists (subcommands.py ACL / STORAGE_CLASS).
_ACL_CHOICES = [
    "private",
    "public-read",
    "public-read-write",
    "authenticated-read",
    "aws-exec-read",
    "bucket-owner-read",
    "bucket-owner-full-control",
    "log-delivery-write",
]
_STORAGE_CLASS_CHOICES = [
    "STANDARD",
    "REDUCED_REDUNDANCY",
    "STANDARD_IA",
    "ONEZONE_IA",
    "INTELLIGENT_TIERING",
    "GLACIER",
    "DEEP_ARCHIVE",
    "GLACIER_IR",
]

# Free-string transfer options that take aws's ``file://`` (text) paramfile
# resolution. The choices-validated options (acl / storage_class / sse / ... )
# can't carry a ``file://`` value (argparse rejects it as an invalid choice
# before paramfile would run), and grants is a list; ``metadata`` is resolved
# separately before its shorthand parse.
_PARAMFILE_TEXT_OPTIONS = frozenset(
    {
        "website_redirect",
        "content_type",
        "cache_control",
        "content_disposition",
        "content_encoding",
        "content_language",
        "expires",
        "sse_kms_key_id",
    }
)

# aws-cli's _raise_if_paths_type_incorrect_for_param's usage rendering.
_EXPECTED_USAGE_MAP = {
    "locals3": "<LocalPath> <S3Uri>",
    "s3s3": "<S3Uri> <S3Uri>",
    "s3local": "<S3Uri> <LocalPath>",
}


def add_transfer_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_expected_size: bool = False,
    include_recursive: bool = True,
) -> None:
    """Register the shared cp/mv/sync argument surface (aws-cli's
    ``TRANSFER_ARGS`` + the metadata/copy-props/case-conflict/no-overwrite
    extras all three carry). ``--expected-size`` is cp-only (mv and sync
    reject streams); ``--recursive`` is cp/mv-only (sync is inherently
    recursive, and aws rejects the flag as an unknown option there)."""
    parser.add_argument("paths", nargs=2, metavar="<path>")
    parser.add_argument("--dryrun", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    if include_recursive:
        parser.add_argument("--recursive", action="store_true")
    # One shared ordered dest: the interleaved --exclude/--include order
    # carries aws-cli's last-match-wins semantics (cli filters module).
    parser.add_argument(
        "--exclude", action=filters.AppendFilterAction, dest="filters", metavar="PATTERN"
    )
    parser.add_argument(
        "--include", action=filters.AppendFilterAction, dest="filters", metavar="PATTERN"
    )
    parser.add_argument("--acl", choices=_ACL_CHOICES)
    parser.add_argument(
        "--follow-symlinks", action="store_true", dest="follow_symlinks", default=True
    )
    parser.add_argument("--no-follow-symlinks", action="store_false", dest="follow_symlinks")
    parser.add_argument(
        "--no-guess-mime-type", action="store_false", dest="guess_mime_type", default=True
    )
    parser.add_argument("--sse", nargs="?", const="AES256", choices=["AES256", "aws:kms"])
    parser.add_argument("--sse-c", nargs="?", const="AES256", choices=["AES256"])
    parser.add_argument("--sse-c-key")
    parser.add_argument("--sse-kms-key-id")
    parser.add_argument("--sse-c-copy-source", nargs="?", const="AES256", choices=["AES256"])
    parser.add_argument("--sse-c-copy-source-key")
    parser.add_argument("--storage-class", choices=_STORAGE_CLASS_CHOICES)
    parser.add_argument("--grants", nargs="+")
    parser.add_argument("--website-redirect")
    parser.add_argument("--content-type")
    parser.add_argument("--cache-control")
    parser.add_argument("--content-disposition")
    parser.add_argument("--content-encoding")
    parser.add_argument("--content-language")
    parser.add_argument("--expires")
    parser.add_argument("--source-region")
    parser.add_argument("--only-show-errors", action="store_true")
    parser.add_argument("--no-progress", action="store_false", dest="progress", default=True)
    # No type=int (parse_integer_option converts at run() start -> 255
    # like aws's bare int(), not argparse's 252).
    parser.add_argument("--progress-frequency", default=0)
    parser.add_argument("--progress-multiline", action="store_true")
    parser.add_argument("--page-size", default=1000)
    parser.add_argument("--ignore-glacier-warnings", action="store_true")
    parser.add_argument("--force-glacier-transfer", action="store_true")
    parser.add_argument(
        "--request-payer", nargs="?", const="requester", choices=["requester"], default=None
    )
    parser.add_argument("--metadata")
    parser.add_argument(
        "--copy-props", choices=["none", "metadata-directive", "default"], default="default"
    )
    parser.add_argument("--metadata-directive", choices=["COPY", "REPLACE"])
    if include_expected_size:
        # No type=int: a non-integer fails the bare int() at submit time like
        # aws (an in-pipeline fatal, rc 1 - not 255).
        parser.add_argument("--expected-size")
    parser.add_argument("--no-overwrite", action="store_true")
    parser.add_argument(
        "--case-conflict", choices=["ignore", "skip", "warn", "error"], default="ignore"
    )
    parser.add_argument("--checksum-mode", choices=["ENABLED"])
    # This set matches aws-cli 2.35.5's CHECKSUM_ALGORITHM choices verbatim
    # (its subcommands.py). An older installed `aws` (e.g. 2.31.x) rejects
    # SHA512 / XXHASH* because that build predates them - a version skew, not a
    # parity bug: the design tracks the aws-cli source, not whatever `aws`
    # happens to be on PATH.
    parser.add_argument(
        "--checksum-algorithm",
        choices=[
            "CRC64NVME",
            "CRC32",
            "SHA256",
            "SHA1",
            "CRC32C",
            "SHA512",
            "XXHASH64",
            "XXHASH3",
            "XXHASH128",
        ],
    )


def validate_checksum_paths_type(
    args: argparse.Namespace, paths_type: str, *, operation: str
) -> None:
    """aws-cli's ``_validate_path_args``'s checksum/path-format pairing (252).

    ``--checksum-algorithm`` shapes what gets *written* (uploads, copies);
    ``--checksum-mode`` validates what gets *read* (downloads). aws rejects
    the wrong pairing for cp and mv alike with the exact wording below.
    """
    if getattr(args, "checksum_algorithm", None) and paths_type not in ("locals3", "s3s3"):
        raise ValidationError(
            "Expected checksum-algorithm parameter to be used with one of following "
            f"path formats: {_EXPECTED_USAGE_MAP['locals3']}, {_EXPECTED_USAGE_MAP['s3s3']}. "
            f"Instead, received {_EXPECTED_USAGE_MAP[paths_type]}.",
            operation=operation,
        )
    if getattr(args, "checksum_mode", None) and paths_type != "s3local":
        raise ValidationError(
            "Expected checksum-mode parameter to be used with one of following "
            f"path formats: {_EXPECTED_USAGE_MAP['s3local']}. "
            f"Instead, received {_EXPECTED_USAGE_MAP[paths_type]}.",
            operation=operation,
        )


def validate_sse_c_pairing(args: argparse.Namespace, paths_type: str, *, operation: str) -> None:
    """aws-cli's ``_validate_sse_c_args``: paired keys, copy-source scope (252)."""
    pairs = (
        ("--sse-c", args.sse_c, "--sse-c-key", args.sse_c_key),
        (
            "--sse-c-copy-source",
            args.sse_c_copy_source,
            "--sse-c-copy-source-key",
            args.sse_c_copy_source_key,
        ),
    )
    for flag, value, key_flag, key_value in pairs:
        if value and not key_value:
            raise ValidationError(
                f"If {flag} is specified, {key_flag} must be specified as well.",
                operation=operation,
            )
        if key_value and not value:
            raise ValidationError(
                f"If {key_flag} is specified, {flag} must be specified as well.",
                operation=operation,
            )
    if args.sse_c_copy_source and paths_type != "s3s3":
        raise ValidationError(
            "--sse-c-copy-source is only supported for copy operations.", operation=operation
        )


def validate_no_overwrite_supported(
    no_overwrite: bool, paths_type: str, client: Any, *, operation: str
) -> None:
    """Reject ``--no-overwrite`` (252) when the installed botocore lacks the S3
    conditional-write parameter for this route - the upload/copy parallel of the
    streaming rejection. Only ``locals3`` (upload) and ``s3s3`` (copy) send
    ``IfNoneMatch``; ``s3local`` (download) and ``sync`` never do, so they stay
    usable on an old botocore (back-compat floor, docs/overview.md section 2)."""
    if not no_overwrite or paths_type not in ("locals3", "s3s3"):
        return
    # Deferred: introspects botocore via the client's model; the parse path
    # stays SDK-free (import contract, docs/imports.md).
    from boto3_s3.transfer import conditional_write_unsupported_reason

    reason = conditional_write_unsupported_reason(client, is_copy=paths_type == "s3s3")
    if reason is not None:
        raise ValidationError(reason, operation=operation)


def is_s3express_path(path: str) -> bool:
    """Whether an ``s3://`` path names an S3 Express directory bucket
    (aws-cli's ``is_s3express_bucket``: the ``--x-s3`` suffix)."""
    if not path.startswith("s3://"):
        return False
    return split_bucket_key(path[len("s3://") :])[0].endswith("--x-s3")


def resolve_case_conflict(
    args: argparse.Namespace,
    src: str,
    paths_type: str,
    *,
    operation: str,
    recursive: bool | None = None,
) -> CaseConflictMode:
    """The effective ``--case-conflict`` mode (aws-cli S3 Express branch).

    Conflicts cannot be detected on an S3 Express directory bucket
    (listings are not lexicographic), so aws rejects ``skip`` / ``error``
    there (252), prints a standing warning for ``warn``, and runs the
    transfer without the comparator - modeled here by downgrading the
    mode to ``ignore`` after the message. ``recursive`` overrides the
    parsed flag for commands without one (sync is always recursive).
    """
    if recursive is None:
        recursive = bool(args.recursive)
    mode = CaseConflictMode(args.case_conflict)
    if mode is CaseConflictMode.IGNORE or paths_type != "s3local" or not recursive:
        return mode
    if not is_s3express_path(src):
        return mode
    if mode is not CaseConflictMode.WARN:
        raise ValidationError(
            f"`{args.case_conflict}` is not a valid value for `--case-conflict` "
            "when operating on S3 Express directory buckets. "
            "Valid values: `warn`, `ignore`.",
            operation=operation,
        )
    sys.stderr.write(
        "warning: Recursive copies/moves from an S3 Express directory "
        "bucket to a case-insensitive local filesystem may result in "
        "undefined behavior if there are S3 object key names that differ "
        "only by case. To disable this warning, set the `--case-conflict` "
        "parameter to `ignore`. For more information, see "
        "https://docs.aws.amazon.com/cli/latest/topic/"
        "s3-case-insensitivity.html.\n"
    )
    return CaseConflictMode.IGNORE


def build_transfer_options(
    args: argparse.Namespace, case_conflict: CaseConflictMode, *, operation: str
) -> TransferOptions:
    """Translate parsed flags into the library's ``TransferOptions``."""
    options = TransferOptions(
        copy_props=CopyPropsMode(args.copy_props),
        guess_mime_type=args.guess_mime_type,
        case_conflict=case_conflict,
    )
    for option, value in (
        ("acl", args.acl),
        ("storage_class", args.storage_class),
        ("website_redirect", args.website_redirect),
        ("content_type", args.content_type),
        ("cache_control", args.cache_control),
        ("content_disposition", args.content_disposition),
        ("content_encoding", args.content_encoding),
        ("content_language", args.content_language),
        ("expires", args.expires),
        ("sse", args.sse),
        ("sse_kms_key_id", args.sse_kms_key_id),
        ("sse_c", args.sse_c),
        ("sse_c_copy_source", args.sse_c_copy_source),
        ("metadata_directive", args.metadata_directive),
        ("request_payer", args.request_payer),
        ("grants", args.grants),
        ("checksum_mode", args.checksum_mode),
        ("checksum_algorithm", args.checksum_algorithm),
    ):
        if value is not None:
            if option in _PARAMFILE_TEXT_OPTIONS:
                # aws applies file:// (text) paramfile resolution to every arg.
                value = resolve_text_paramfile(
                    value, f"--{option.replace('_', '-')}", operation=operation
                )
            options[option] = value  # type: ignore[literal-required]
    if args.metadata is not None:
        # file:// resolves before the shorthand parse (aws unpacks the paramfile,
        # then parses the loaded text as the map value).
        metadata_value = resolve_text_paramfile(args.metadata, "--metadata", operation=operation)
        options["metadata"] = shorthand.parse_map_option(
            metadata_value, name="--metadata", operation=operation
        )
    if args.sse_c_key is not None:
        options["sse_c_key"] = blob_value(args.sse_c_key, "--sse-c-key", operation=operation)
    if args.sse_c_copy_source_key is not None:
        options["sse_c_copy_source_key"] = blob_value(
            args.sse_c_copy_source_key, "--sse-c-copy-source-key", operation=operation
        )
    if args.force_glacier_transfer:
        options["force_glacier_transfer"] = True
    if args.ignore_glacier_warnings:
        options["ignore_glacier_warnings"] = True
    if args.no_overwrite:
        options["no_overwrite"] = True
    return options


def _read_text_paramfile(original: str, *, name: str, operation: str) -> str:
    """Load a ``file://`` reference as text (aws paramfile ``mode='r'``).

    Path expansion matches aws's ``get_file``: ``expandvars(expanduser(...))``
    (expanduser inner). The encoding honors ``AWS_CLI_FILE_ENCODING`` (aws's
    ``compat_open`` / ``getpreferredencoding``), falling back to the locale
    default (``open``'s default when ``encoding`` is ``None``).
    """
    path = os.path.expandvars(os.path.expanduser(original[len("file://") :]))
    encoding = os.environ.get("AWS_CLI_FILE_ENCODING")
    try:
        with open(path, encoding=encoding) as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        # aws wording (paramfile.get_file): the decode-error message names the
        # EXPANDED path in parentheses; the OSError one names the full original.
        raise ValidationError(
            f"Error parsing parameter '{name}': Unable to load paramfile ({path}), "
            "text contents could not be decoded.  If this is a binary file, please use "
            "the fileb:// prefix instead of the file:// prefix.",
            operation=operation,
        ) from exc
    except OSError as exc:
        raise ValidationError(
            f"Error parsing parameter '{name}': Unable to load paramfile {original}: {exc}",
            operation=operation,
        ) from exc


def _read_binary_paramfile(original: str, *, name: str, operation: str) -> bytes:
    """Load a ``fileb://`` reference as raw bytes (aws paramfile ``mode='rb'``).

    Path expansion matches aws's ``get_file``: ``expandvars(expanduser(...))``.
    """
    path = os.path.expandvars(os.path.expanduser(original[len("fileb://") :]))
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise ValidationError(
            f"Error parsing parameter '{name}': Unable to load paramfile {original}: {exc}",
            operation=operation,
        ) from exc


def resolve_text_paramfile(value: str, name: str, *, operation: str) -> str:
    """Apply aws's ``file://`` (text) paramfile resolution to a string argument.

    aws-cli registers a global URI paramfile handler on every ``aws s3`` arg, so
    ``--content-type file://ct.txt`` (etc.) sends the *file contents*. A value
    without the prefix passes through verbatim.
    """
    if value.startswith("file://"):
        return _read_text_paramfile(value, name=name, operation=operation)
    return value


def blob_value(value: str, name: str, *, operation: str) -> str | bytes:
    """Resolve a blob argument the way ``aws s3`` does.

    A ``fileb://`` reference loads raw bytes and a ``file://`` reference loads
    text (aws's paramfile, both prefixes); anything else passes through verbatim
    - aws sends an arbitrary ``--sse-c-key`` string to the
    server untouched (no base64 decoding, rc 1 from the endpoint), so a malformed
    key is *not* a usage error.
    """
    if value.startswith("fileb://"):
        return _read_binary_paramfile(value, name=name, operation=operation)
    return resolve_text_paramfile(value, name, operation=operation)


def resolve_locations(
    args: argparse.Namespace,
    ctx: Context,
    client: Any,
    src: str,
    dest: str,
    *,
    src_type: str,
    dest_type: str,
) -> tuple[object, object]:
    """Build the library-side location pair for the non-stream routes.

    The S3 sides become ``S3Storage`` objects carrying the CLI-built client;
    for S3->S3 with ``--source-region`` the source side gets its own client
    in that region with the ``--endpoint-url`` override dropped (aws-cli
    ClientFactory).
    """
    # Deferred: S3Storage's chain reaches botocore; --help and usage errors
    # stay SDK-free (import contract, docs/imports.md).
    from boto3_s3 import S3Storage

    def _s3(arg: str, client_for: Any) -> S3Storage:
        # Construction is permissive (non-raising); validate the strict aws-cli
        # forms here - the same point construction used to reject them, so a usage
        # error (rc 252) still precedes the transfer pipeline.
        storage = S3Storage(arg, client=client_for)
        storage.validate()
        return storage

    if src_type == "local":
        return src, _s3(dest, client)
    if dest_type == "local":
        return _s3(src, client), dest
    source_client = client
    if args.source_region:
        source_args = argparse.Namespace(**vars(args))
        source_args.region = args.source_region
        source_args.endpoint_url = None
        source_client = ctx.client_factory(source_args)
    return _s3(src, source_client), _s3(dest, client)


def path_storage(arg: str, kind: str) -> S3Storage | LocalStorage:
    """The scheme-bearing Storage ``naming.plan_transfer`` reads for cp/mv/sync.

    ``plan_transfer`` needs only ``.scheme`` / ``.as_text()``, so the S3 side
    carries no client here - this is the throwaway used purely to derive the path
    shapes (roots, the stdin dest key). The real transfer storages (with a client)
    are built in :func:`resolve_locations`.
    """
    from boto3_s3 import LocalStorage, S3Storage

    return S3Storage(arg) if kind == "s3" else LocalStorage(arg)


def resolve_transfer_config(args: argparse.Namespace, ctx: Context, *, paths_type: str) -> Any:
    """The transfer config for this run: the injected one, else from ``[s3]``.

    A test-injected ``ctx.transfer_config`` always wins (the existing
    determinism lever). Otherwise the profile's ``[s3]`` section is read,
    parsed (aws-cli's ``RuntimeConfig``), and turned into a ``TransferConfig``
    whose ``preferred_transfer_client`` carries the aws-faithful engine
    decision (``runtimeconfig.resolve_transfer_client``). Reading ``[s3]`` here
    - past the usage (252) and missing-source (255) validations - matches
    aws's ordering (an invalid ``[s3]`` value loses to both).
    """
    if ctx.transfer_config is not None:
        return ctx.transfer_config
    from boto3_s3_cli import runtimeconfig

    scoped = runtimeconfig.load_scoped_s3_config(args)
    runtime_config = runtimeconfig.RuntimeConfig().build_config(**scoped)
    resolved = runtimeconfig.resolve_transfer_client(runtime_config, paths_type=paths_type)
    return runtimeconfig.build_transfer_config(scoped, runtime_config, resolved)


def finish_transfer(printer: TransferPrinter, *, quiet: bool, run: Callable[[], None]) -> int:
    """Run the library call and derive the aws exit code (docs/cli.md section 6).

    Everything the pipeline raises is rc 1: ``BatchError`` after per-item
    ``failed`` lines, anything run-killing as one ``fatal error:`` line. A
    clean run exits 2 when only warnings accumulated, else 0.
    """
    try:
        run()
    except BatchError:
        # Per-item failure lines were already streamed by the printer.
        return 1
    except (Boto3S3Error, ValueError) as exc:
        if not quiet:
            sys.stderr.write(f"fatal error: {exc}\n")
        return 1
    return 2 if printer.warned else 0


__all__ = [
    "add_transfer_arguments",
    "blob_value",
    "build_transfer_options",
    "finish_transfer",
    "is_s3express_path",
    "resolve_case_conflict",
    "resolve_locations",
    "resolve_transfer_config",
    "validate_checksum_paths_type",
    "validate_sse_c_pairing",
]
