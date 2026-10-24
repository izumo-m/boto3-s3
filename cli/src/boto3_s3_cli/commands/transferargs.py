"""The argument surface and run-pipeline pieces ``cp`` / ``mv`` / ``sync`` share.

``aws s3`` declares one ``TRANSFER_ARGS`` list plus per-command extras
(aws-cli's ``subcommands.py``); this module is that shared list and the
validation/translation steps the three commands run in the same order.
This module loads at dispatch once its command is determined (stage 2 of the
lazy dispatch).
Top-level imports may therefore reach the AWS SDK.
"""

from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from boto3_s3 import (
    AnnotationCopyMode,
    BatchError,
    CaseConflictMode,
    CopyPropsMode,
    LocalStorage,
    NotFoundError,
    S3Storage,
    TransferOptions,
    ValidationError,
    awsclisession,
    crtsupport,
    requestparams,
)
from boto3_s3.localstorage import translate_os_error
from boto3_s3.pathresolver import is_s3express_accesspoint_path, is_s3express_path
from boto3_s3_cli import (
    filters,
    output,
    paramfile,
    shorthand,
    textencoding,
    usage,
)
from boto3_s3_cli.commands.base import (
    add_page_size_argument,
    add_request_payer_argument,
    expand_option_paramfile,
    refuse_bucketless,
    resolve_integer_option,
)
from boto3_s3_cli.progress import TransferPrinter

if TYPE_CHECKING:
    from collections.abc import Callable

    from boto3.s3.transfer import TransferConfig
    from mypy_boto3_s3 import S3Client

    from boto3_s3 import S3
    from boto3_s3_cli.commands.base import Context

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

# The free-string transfer options that take aws-cli's paramfile resolution
# (``expand_option_paramfile``). Each is a string-typed server parameter, so a
# ``file://`` reference sends the file text and a ``fileb://`` one is refused
# (252). The choices-validated options (acl / storage_class / sse / ... ) can't
# carry such a value (argparse rejects it as an invalid choice first);
# ``grants`` is resolved on its own (``_resolve_grants``), and ``metadata`` is
# a map, resolved after the integer coercions.
_PARAMFILE_TEXT_OPTIONS: tuple[str, ...] = (
    "sse_kms_key_id",
    "website_redirect",
    "content_type",
    "cache_control",
    "content_disposition",
    "content_encoding",
    "content_language",
    "expires",
    "source_region",
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
    recursive, and aws-cli rejects the flag as an unknown option there)."""
    parser.add_argument("paths", nargs=2, help="source and destination location")
    parser.add_argument(
        "--dryrun",
        action="store_true",
        help="report what would happen without transferring or deleting anything",
    )
    parser.add_argument("--quiet", action="store_true", help="do not print per-item result lines")
    if include_recursive:
        parser.add_argument(
            "--recursive",
            action="store_true",
            help="act on every file or object under the source",
        )
    filters.add_filter_arguments(parser)
    parser.add_argument(
        "--acl", choices=_ACL_CHOICES, help="canned ACL to apply to the written object"
    )
    parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        dest="follow_symlinks",
        default=True,
        help="follow symbolic links when walking a local directory (default)",
    )
    parser.add_argument(
        "--no-follow-symlinks",
        action="store_false",
        dest="follow_symlinks",
        help="do not follow symbolic links when walking a local directory",
    )
    parser.add_argument(
        "--no-guess-mime-type",
        action="store_false",
        dest="guess_mime_type",
        default=True,
        help="do not infer Content-Type from the file extension on upload",
    )
    parser.add_argument(
        "--sse",
        nargs="?",
        const="AES256",
        choices=["AES256", "aws:kms"],
        help="server-side encryption for the written object",
    )
    parser.add_argument(
        "--sse-c",
        nargs="?",
        const="AES256",
        choices=["AES256"],
        help="encrypt the written object with a customer-provided key",
    )
    parser.add_argument(
        "--sse-c-key", help="the customer-provided key; use fileb:// to read raw bytes"
    )
    parser.add_argument("--sse-kms-key-id", help="KMS key to use with --sse aws:kms")
    parser.add_argument(
        "--sse-c-copy-source",
        nargs="?",
        const="AES256",
        choices=["AES256"],
        help="the S3 copy source is encrypted with a customer-provided key",
    )
    parser.add_argument(
        "--sse-c-copy-source-key",
        help="the copy source's customer-provided key; use fileb:// for raw bytes",
    )
    parser.add_argument(
        "--storage-class",
        choices=_STORAGE_CLASS_CHOICES,
        help="storage class for the written object",
    )
    parser.add_argument(
        "--grants", nargs="+", help="explicit grants, as Permission=Grantee arguments"
    )
    parser.add_argument(
        "--website-redirect", help="where a website request for the object is redirected"
    )
    parser.add_argument("--content-type", help="Content-Type for the written object")
    parser.add_argument("--cache-control", help="Cache-Control for the written object")
    parser.add_argument("--content-disposition", help="Content-Disposition for the written object")
    parser.add_argument("--content-encoding", help="Content-Encoding for the written object")
    parser.add_argument("--content-language", help="Content-Language for the written object")
    parser.add_argument("--expires", help="Expires date for the written object")
    parser.add_argument("--source-region", help="region of the source bucket of an S3-to-S3 copy")
    parser.add_argument(
        "--only-show-errors",
        action="store_true",
        help="print only errors and warnings, no progress or result lines",
    )
    parser.add_argument(
        "--no-progress",
        action="store_false",
        dest="progress",
        default=True,
        help="do not display transfer progress",
    )
    # No type=int (parse_integer_option converts at run() start -> 255
    # like aws-cli's bare int(), not argparse's 252).
    parser.add_argument(
        "--progress-frequency", default=0, help="minimum seconds between progress redraws"
    )
    parser.add_argument(
        "--progress-multiline",
        action="store_true",
        help="print each progress update on its own line instead of redrawing one",
    )
    add_page_size_argument(parser)
    parser.add_argument(
        "--ignore-glacier-warnings",
        action="store_true",
        help="do not warn about objects skipped because they are archived",
    )
    parser.add_argument(
        "--force-glacier-transfer",
        action="store_true",
        help="attempt the transfer even for archived objects",
    )
    add_request_payer_argument(parser)
    parser.add_argument("--metadata", help="user metadata for the written object, as JSON")
    parser.add_argument(
        "--copy-props",
        choices=["none", "metadata-directive", "default", "all"],
        default="default",
        help="which source properties an S3-to-S3 copy carries over",
    )
    parser.add_argument(
        "--metadata-directive",
        choices=["COPY", "REPLACE"],
        help="whether an S3-to-S3 copy keeps or replaces the source metadata",
    )
    if include_expected_size:
        # No type=int: a non-integer fails the bare int() at submit time like
        # aws-cli (an in-pipeline fatal, rc 1 - not 255).
        parser.add_argument(
            "--expected-size",
            help="size in bytes of a streamed upload, so multipart can be planned",
        )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="skip, without failing, anything that already exists at the destination",
    )
    parser.add_argument(
        "--case-conflict",
        choices=["ignore", "skip", "warn", "error"],
        default="ignore",
        help="what to do when destinations differ only by letter case",
    )
    parser.add_argument(
        "--checksum-mode",
        choices=["ENABLED"],
        help="verify the object's stored checksum when downloading",
    )
    # This set matches the pinned aws-cli's CHECKSUM_ALGORITHM choices verbatim
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
        help="checksum algorithm to compute for the written object",
    )


def identify_type(path: str) -> str:
    """``"s3"`` iff the path starts with ``s3://`` - the only S3 marker aws-cli knows.

    The same ``s3://`` test as aws-cli's ``FileFormat.identify_type`` (only the
    classification rule is ported; aws-cli also strips the prefix and returns a
    ``(type, path)`` tuple). String classification is the CLI layer's job (the
    library planner receives resolved ``Storage`` objects and never re-parses a
    path).
    """
    return "s3" if path.startswith("s3://") else "local"


def resolve_paramfile_values(args: argparse.Namespace, *, operation: str) -> tuple[int | None, int]:
    """Resolve the direct-option paramfiles and integer coercions.

    The ``file://`` / ``fileb://`` paramfile expansion (a bad reference is
    252) and, for the two ``integer`` options, the bare ``int()`` coercion
    (255): the SSE-C key blob, the copy-source blob, the ``--grants`` check
    (a lone paramfile, a malformed grant: 252), the free-string text block,
    then ``--progress-frequency`` and ``--page-size`` (paramfile then
    coercion).
    Returns the two coerced integers. ``--metadata`` (a map) and cp's
    ``--expected-size`` are resolved by ``classify_paths`` (``resolve_metadata_option`` /
    ``expand_option_paramfile``). ``build_transfer_options`` consumes the
    resolved values verbatim.
    """
    # The SSE-C key blobs take aws-cli's paramfile on both prefixes: a ``fileb://``
    # loads raw bytes, a ``file://`` text, and anything else passes through
    # verbatim - aws-cli sends an arbitrary ``--sse-c-key`` string to the server
    # untouched (no base64 decoding, rc 1 from the endpoint), so a malformed
    # key is *not* a usage error.
    if args.sse_c_key is not None:
        args.sse_c_key = paramfile.load_argument(args.sse_c_key, "--sse-c-key", operation=operation)
    if args.sse_c_copy_source_key is not None:
        args.sse_c_copy_source_key = paramfile.load_argument(
            args.sse_c_copy_source_key, "--sse-c-copy-source-key", operation=operation
        )
    _resolve_grants(args, operation=operation)
    for option in _PARAMFILE_TEXT_OPTIONS:
        expand_option_paramfile(args, option, operation=operation)
    progress_frequency = resolve_integer_option(args, "progress_frequency", operation=operation)
    # --progress-frequency's argparse default is 0, so its unset-None branch is
    # unreachable; --page-size's default is None (nothing sent, aws-cli parity).
    assert progress_frequency is not None
    page_size = resolve_integer_option(args, "page_size", operation=operation)
    return page_size, progress_frequency


def _resolve_grants(args: argparse.Namespace, *, operation: str) -> None:
    """Refuse a lone ``--grants`` paramfile reference, and a malformed grant (252).

    aws-cli unwraps a one-element list before its paramfile hook, so ``--grants
    file://g`` loads the file and its grant parser then walks the text
    character by character (bytes int by int) - an aws-cli bug this CLI does not
    copy (docs/cli/aws-differences.md section 2). The reference is refused
    without being read. A multi-element ``--grants`` or a prefix-less value
    stays the list argparse produced, a ``file://`` element there a literal
    grant as on `aws`.

    Each grant must then be ``<permission>=<grantee>`` with a known
    permission - the library's own check (``requestparams.validate_grants``),
    made here before the run, on every route, so a malformed value is a
    command-line error naming the option. aws-cli checks it only as it maps the
    first upload or copy - an in-flight fatal error after any deletes already
    sent, rc 0 when nothing needs transferring, never on a download. (The
    library makes the same check at the start of its operations, inside the
    run span, where it would be a ``fatal error`` at rc 1.)
    """
    grants: object = args.grants
    if not isinstance(grants, list):
        return
    items = cast("list[object]", grants)
    if (
        len(items) == 1
        and isinstance(items[0], str)
        and items[0].startswith(("file://", "fileb://"))
    ):
        raise ValidationError(
            usage.param_error(
                "--grants",
                "a file:// or fileb:// reference is not supported; give each grant as an argument",
            ),
            operation=operation,
        )
    try:
        requestparams.validate_grants(cast("list[str]", items), operation=operation)
    except ValidationError as exc:
        raise ValidationError(usage.param_error("--grants", exc), operation=operation) from exc


def resolve_metadata_option(args: argparse.Namespace, *, operation: str) -> None:
    """Resolve ``--metadata`` in place: paramfile, then the shorthand parse.

    A ``file://`` whole-value reference loads the map text (252 on a missing
    file). A ``fileb://`` one is refused at 252 without being read
    (``paramfile.load_text_argument``): aws-cli loads the bytes and then crashes in
    its shorthand parser (rc 255), and no bytes can be a map
    (docs/cli/aws-differences.md section 2).
    """
    if args.metadata is None:
        return
    metadata_value = paramfile.load_text_argument(args.metadata, "--metadata", operation=operation)
    args.metadata = shorthand.parse_map_option(
        metadata_value, name="--metadata", operation=operation
    )


class TransferPaths(NamedTuple):
    """The classified head of a cp/mv/sync invocation (the path-type gate's input).

    ``client`` is the run's S3 client, already built by `classify_paths`; each
    command consumes it rather than building its own.
    """

    page_size: int | None
    progress_frequency: int
    src: str
    dest: str
    src_type: str
    dest_type: str
    paths_type: str  # "locals3" | "s3local" | "s3s3" on the CLI surface
    s3: S3
    client: S3Client


def classify_paths(args: argparse.Namespace, ctx: Context, *, operation: str) -> TransferPaths:
    """The shared ``run()`` head of cp/mv/sync.

    The option values are resolved first - the direct-option paramfiles and
    the two integer coercions (``resolve_paramfile_values``: a bad paramfile
    is 252, a bad ``int()`` 255), the ``--metadata`` resolution (252,
    paramfile + shorthand) and cp's ``--expected-size`` paramfile (252) -
    then the local-local pair gate (252) and aws-cli's
    ``_normalize_s3_trailing_slash`` over both paths (``normalize_s3_path``:
    the refused ARN forms, 252, then the keyless trailing ``/``; the returned
    paths are the normalized ones), and last the S3 client build. Only this
    shared prefix lives here - the stream / checksum / SSE-C checks that
    follow stay with each command.
    """
    page_size, progress_frequency = resolve_paramfile_values(args, operation=operation)
    resolve_metadata_option(args, operation=operation)
    # cp-only (``--expected-size``); a no-op where the attribute is absent.
    # A *string*-typed option in aws-cli (no cli_type_name, unlike --page-size),
    # so a fileb:// is refused (252) where the integer helper would run
    # int(b'5') and proceed. The stream route's int() coercion still runs at
    # submit time.
    expand_option_paramfile(args, "expected_size", operation=operation)
    src, dest = args.paths
    src_type = identify_type(src)
    dest_type = identify_type(dest)
    if src_type == "local" and dest_type == "local":
        raise ValidationError(usage.two_path_usage(operation), operation=operation)
    # aws-cli's add_paths runs _normalize_s3_trailing_slash right after the
    # path-type check.
    src = normalize_s3_path(src, operation=operation)
    dest = normalize_s3_path(dest, operation=operation)
    s3 = ctx.s3(args)
    client = s3.client()
    return TransferPaths(
        page_size,
        progress_frequency,
        src,
        dest,
        src_type,
        dest_type,
        src_type + dest_type,
        s3,
        client,
    )


def normalize_s3_path(path: str, *, operation: str) -> str:
    """One path argument as aws-cli's ``_normalize_s3_trailing_slash`` leaves it.

    aws-cli runs this over cp / mv / sync / rm's paths right after the path-type
    check. Its ``find_bucket_key`` refuses the S3 Object Lambda and Outposts
    *bucket* ARNs first (rc 252, before a download directory is created), then a
    keyless ``s3://`` path gains the trailing ``/`` that makes it the bucket
    root, and every later step re-reads the result. The re-read changes one
    spelling: an Outposts access point ARN set off with ``:`` and no key
    (``.../accesspoint/ap:``) becomes ``.../accesspoint/ap:/``, whose key is
    ``/`` - so ``rm --recursive`` lists under ``/`` and a single download
    heads the key ``/``, not the whole access point. A local path passes
    unchanged; a path that names no bucket (``s3://``, ``s3:///k``) is
    refused first (`refuse_bucketless`, 252).
    """
    if not path.startswith("s3://"):
        return path
    bucket, _key = S3Storage.split_bucket_key(S3Storage.strip_scheme(path))
    refuse_bucketless(bucket, operation=operation)
    # The ARN rejections are all S3Storage.validate checks here.
    S3Storage(path).validate()
    return S3Storage.normalize_s3_uri(path)


def local_storage(path: str, *, follow_symlinks: bool = True) -> LocalStorage:
    """A ``LocalStorage`` for a command-line path, its names in the CLI's encoding.

    ``filename_encoding`` is `textencoding.filename_encoding`: the encoding
    ``aws`` reads and writes local names with here (design/cli.md section 1).
    """
    return LocalStorage(
        path, follow_symlinks=follow_symlinks, filename_encoding=textencoding.filename_encoding()
    )


def local_path_exists(path: str) -> bool:
    """aws-cli's missing-source probe (``os.path.exists``) on a command-line path.

    The path is the CLI's text; the OS gets its own form of it
    (`textencoding.os_path`), and a name the encoding cannot represent names
    no file.
    """
    try:
        return os.path.exists(textencoding.os_path(path))
    except ValueError:
        return False


def validate_streaming_paths(head: TransferPaths, *, recursive: bool, operation: str) -> bool:
    """aws-cli's ``_validate_streaming_paths``: whether the run streams (252 if it may not).

    A ``-`` on either side is a stream, and only a non-recursive ``cp``
    streams: anywhere else aws-cli refuses it, with a message that names cp
    whatever the command (``mv`` and ``sync`` included).
    """
    if head.src != "-" and head.dest != "-":
        return False
    if operation != "cp" or recursive:
        raise ValidationError(
            "Streaming currently is only compatible with non-recursive cp commands",
            operation=operation,
        )
    return True


def check_local_source(
    args: argparse.Namespace, head: TransferPaths, *, recursive: bool, operation: str
) -> None:
    """aws-cli's missing-source check (``_validate_path_args``), then the walk's root check.

    A local source that does not exist stops the run (aws-cli's bare
    ``RuntimeError``, rc 255; a `NotFoundError` without a ``ClientError``
    cause maps to the same 255). A ``-`` source is a stream, not a path, and
    is not probed. A *recursive* run (``cp`` / ``mv`` ``--recursive``, every
    ``sync``) walks the source as a directory, so a root the walk would skip
    is refused next (`refuse_unwalkable_source`, 255).
    """
    if head.src_type != "local" or head.src == "-":
        return
    if not local_path_exists(head.src):
        raise NotFoundError(
            f"The user-provided path {head.src} does not exist.", operation=operation
        )
    if recursive:
        refuse_unwalkable_source(
            head.src, follow_symlinks=args.follow_symlinks, operation=operation
        )


def refuse_unwalkable_source(src: str, *, follow_symlinks: bool, operation: str) -> None:
    """Refuse a directory source whose root the walk would skip (255).

    A recursive ``cp`` / ``mv`` and every ``sync`` walk a local source as a
    directory, and the walk vets its root first - the library's walker
    decides (``LocalFileGenerator.should_ignore_file`` on the root with a
    trailing separator): a file, a special or unreadable path, a path whose
    ``..`` after a symlinked directory names nothing, or a symlink under
    ``--no-follow-symlinks``. aws-cli skips such a root - with a warning, or in
    silence for the symlink - and runs on an empty source, so a ``sync
    --delete`` then empties the destination. Here the run stops before it
    starts, like a missing source (`NotFoundError`, 255), before anything is
    listed, sent or deleted. The library refuses the same roots for its own
    callers; this is the CLI's check, made where the missing-source one is.
    """
    storage = local_storage(src, follow_symlinks=follow_symlinks)
    reasons: list[str] = []
    # The walker takes the OS form (the source exists, so the encoding
    # represents its name) and names it in its warnings, read back as text.
    path = storage.os_path(storage.abspath)
    if not storage.walker.should_ignore_file(
        path + os.sep, follow_symlinks=follow_symlinks, notify=reasons.append
    ):
        return
    reasons = [storage.text_path(reason) for reason in reasons]
    if not follow_symlinks and os.path.islink(path):
        why = "It is a symbolic link, which --no-follow-symlinks does not follow."
    elif not os.path.lexists(path):
        # The kernel found the path as typed (the missing-source check), the
        # walk's absolute form names nothing: a `..` after a symlinked
        # directory, which the absolute form folds lexically.
        why = f"It does not exist as {storage.abspath}."
    elif not os.path.isdir(path):
        why = "It is not a directory."
    elif reasons:
        # The walk's warning names the path and then the reason
        # ("Skipping file <path>. File/Directory is not readable.").
        why = reasons[0].rpartition(". ")[2]
    else:
        why = "The walk skips it."
    raise NotFoundError(
        f"The user-provided path {src} cannot be walked as a directory. {why}",
        operation=operation,
    )


def create_local_dest_dir(dest: str, *, operation: str) -> None:
    """Pre-create the ``s3local`` destination directory (aws-cli's ``_validate_path_args``).

    aws-cli creates the destination whenever the operation is a ``dir_op``
    (``cp``/``mv`` ``--recursive`` and every ``sync``) - before the pipeline,
    so it is there even when nothing transfers, and a creation failure is the
    pre-pipeline rc 255, not the pipeline's rc 1. Pre-creating it outside
    ``finish_transfer``'s catch keeps that shape (the library still ensures
    the dir for direct callers; this makes it a no-op there). The commands
    call it after every check of their own, where aws-cli creates it in the middle
    of its validation: a run refused before it starts leaves nothing behind.
    The bare ``exists`` test then bare ``makedirs`` mirror aws-cli; every
    ``translate_os_error`` category maps to the same rc 255. The path is the
    CLI's text, handed to the OS in its own form (`textencoding.os_path`).
    """
    try:
        native = textencoding.os_path(dest)
        if not os.path.exists(native):
            os.makedirs(native)
    except OSError as exc:
        textencoding.name_files_as_text(exc)
        raise translate_os_error(exc, operation=operation, key=None) from exc
    except ValueError as exc:
        # A name the CLI's encoding cannot represent (aws-cli's own makedirs
        # fails the same way): the pre-pipeline 255 all the same.
        raise translate_os_error(exc, operation=operation, key=None) from exc


def validate_checksum_paths_type(
    args: argparse.Namespace, paths_type: str, *, operation: str
) -> None:
    """aws-cli's ``_validate_path_args``'s checksum/path-format pairing (252).

    ``--checksum-algorithm`` shapes what gets *written* (uploads, copies);
    ``--checksum-mode`` validates what gets *read* (downloads). aws-cli rejects
    the wrong pairing for cp and mv alike.
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
    (listings are not lexicographic), so aws-cli rejects ``skip`` / ``error``
    there (252), prints a standing warning for ``warn``, and runs the
    transfer without the comparator - modeled here by downgrading the
    mode to ``ignore`` after the message. aws-cli takes that branch for the
    ``--x-s3`` suffix only; a source named through an access point of a
    directory bucket (``--xa-s3``) lists just as unordered, and under `aws`
    its keys pair wrongly with the destination's - ``skip`` drops objects
    that conflict with nothing (measured on real S3) - so it takes the
    branch here too (docs/cli/aws-differences.md). ``recursive`` overrides
    the parsed flag for commands without one (sync is always recursive).
    """
    if recursive is None:
        recursive = bool(args.recursive)
    mode = CaseConflictMode(args.case_conflict)
    if mode is CaseConflictMode.IGNORE or paths_type != "s3local" or not recursive:
        return mode
    if not lists_unordered(src):
        return mode
    if mode is not CaseConflictMode.WARN:
        raise ValidationError(
            f"`{args.case_conflict}` is not a valid value for `--case-conflict` "
            "when operating on S3 Express directory buckets. "
            "Valid values: `warn`, `ignore`.",
            operation=operation,
        )
    # aws-cli emits this via ``uni_print`` with no trailing newline (measured: the
    # message ends at ``...html.`` and the next stderr output concatenates);
    # uni_write (under `output.write_stderr`) is that port - same bytes plus
    # the flush that keeps the newline-less warning from sitting in a
    # block-buffered stderr (a redirected run) until process exit, behind
    # output `aws` prints after it.
    output.write_stderr(
        "warning: Recursive copies/moves from an S3 Express directory "
        "bucket to a case-insensitive local filesystem may result in "
        "undefined behavior if there are S3 object key names that differ "
        "only by case. To disable this warning, set the `--case-conflict` "
        "parameter to `ignore`. For more information, see "
        "https://docs.aws.amazon.com/cli/latest/topic/"
        "s3-case-insensitivity.html.",
    )
    return CaseConflictMode.IGNORE


def lists_unordered(path: str) -> bool:
    """Whether *path* lists in no lexicographic order: a directory bucket or its access point.

    aws-cli special-cases an S3 Express directory bucket (``--x-s3``) wherever a
    step relies on ordered listings - sync, the ``--case-conflict`` check -
    and tests that suffix only. An access point of a directory bucket
    (``--xa-s3``) lists just as unordered, so it counts here too (a
    deliberate difference, docs/cli/aws-differences.md).
    """
    return is_s3express_path(path) or is_s3express_accesspoint_path(path)


def default_upload_checksum(client: Any, paths_type: str, transfer_config: Any) -> str | None:
    """aws-cli's default request checksum for the uploads a run makes, else None.

    Only an upload run (``locals3``) gets one: by default aws-cli sends no
    checksum algorithm on a copy's CopyObject / UploadPartCopy (measured
    against the pinned `aws`; an explicit ``--checksum-algorithm`` reaches them
    on both tools through the request mapping), and a download writes
    nothing. The value is `awsclisession.default_upload_algorithm`'s, chosen by the engine
    *transfer_config* resolves to (`resolve_transfer_config`; the same
    `crtsupport.selects_crt` test the engine applies): the CRT engine's
    default holds whatever ``request_checksum_calculation`` says, the classic
    engine's only under ``when_supported`` - aws-cli's own split, measured on
    the wire.
    """
    if paths_type != "locals3":
        return None
    return awsclisession.default_upload_algorithm(
        client, crt=crtsupport.selects_crt(transfer_config)
    )


def build_transfer_options(
    args: argparse.Namespace,
    case_conflict: CaseConflictMode,
    *,
    default_checksum_algorithm: str | None = None,
) -> TransferOptions:
    """Translate parsed flags into the library's ``TransferOptions``.

    ``default_checksum_algorithm`` (`default_upload_checksum`) stands in for
    an absent ``--checksum-algorithm`` the way aws-cli's bundled botocore default
    does for aws-cli: named here, it reaches both transfer engines through the
    options, where the library would otherwise leave each engine to pip
    s3transfer's own CRC32.
    """
    options = TransferOptions(
        annotation_copy_mode=AnnotationCopyMode.PRELOAD_MEMORY,
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
            # Paramfiles and shorthand are already resolved in place -
            # `classify_paths` runs at the head of every cp/mv/sync
            # (aws-cli resolves them at parse time), so the values here are
            # consumed verbatim.
            options[option] = value  # type: ignore[literal-required]
    if args.checksum_algorithm is None and default_checksum_algorithm is not None:
        options["checksum_algorithm"] = default_checksum_algorithm
    if args.metadata is not None:
        options["metadata"] = args.metadata
    if args.sse_c_key is not None:
        options["sse_c_key"] = args.sse_c_key
    if args.sse_c_copy_source_key is not None:
        options["sse_c_copy_source_key"] = args.sse_c_copy_source_key
    if args.force_glacier_transfer:
        options["force_glacier_transfer"] = True
    if args.ignore_glacier_warnings:
        options["ignore_glacier_warnings"] = True
    if args.no_overwrite:
        options["no_overwrite"] = True
    return options


def resolve_locations(
    args: argparse.Namespace, ctx: Context, head: TransferPaths
) -> tuple[object, object]:
    """Build the library-side location pair for the non-stream routes.

    The S3 sides become ``S3Storage`` objects carrying the CLI-built client and
    the ``--page-size`` listing config; a local side becomes a ``LocalStorage``
    carrying ``--follow-symlinks``. The library reads how a source is walked /
    listed from the storage itself now (not from a per-operation argument), so the
    CLI bakes each command flag into the storage it builds here. For S3->S3 with
    ``--source-region`` the source side gets its own client in that region with the
    ``--endpoint-url`` override dropped (aws-cli ClientFactory).
    """
    src, dest, client = head.src, head.dest, head.client

    def _s3(arg: str, client_for: Any) -> S3Storage:
        # build_s3_storage applies the path refusals (rc 252 ahead of the
        # pipeline). The CLI's process-fatal Ctrl-C posture is not a storage
        # concern:
        # build_s3 declares it once (S3's reusable_after_interrupt).
        return build_s3_storage(arg, client=client_for, page_size=head.page_size)

    def _local(path: str) -> LocalStorage:
        return local_storage(path, follow_symlinks=args.follow_symlinks)

    if head.src_type == "local":
        return _local(src), _s3(dest, client)
    if head.dest_type == "local":
        # aws-cli reads an empty destination of a single download as the
        # working directory (its local_format absolutizes "" to the cwd, which
        # is a directory); the library refuses "" as no location at all, so
        # the CLI spells out what aws-cli means. A dir_op with "" gets here
        # too, and the destination pre-create that follows every check stops
        # it before the run (`os.makedirs("")` fails: rc 255, as aws-cli's own
        # pre-create does); an empty source is refused as missing earlier.
        return _s3(src, client), _local(dest or os.curdir)
    source_client = client
    if args.source_region:
        source_args = argparse.Namespace(**vars(args))
        source_args.region = args.source_region
        source_args.endpoint_url = None
        source_client = ctx.client(source_args, head.s3)
    return _s3(src, source_client), _s3(dest, client)


def path_storage(arg: str, kind: str) -> S3Storage | LocalStorage:
    """A plan-only endpoint Storage for ``transferplan.plan_transfer``.

    The planner routes by concrete type and each side formats itself from its
    held state (``Storage.format``), so the S3 side carries no client here -
    this is the throwaway used purely to derive the path shapes (roots, the
    stdin dest key). The real transfer storages (with a client) are built in
    ``resolve_locations``.
    """
    return S3Storage(arg) if kind == "s3" else local_storage(arg)


def build_s3_storage(arg: str, *, client: Any, page_size: int | None = None) -> S3Storage:
    """An ``S3Storage`` for a CLI s3 path, validated before anything runs.

    Shared by the transfer family, ``rm``, ``presign`` and ``website``. The
    refusals fire here, ahead of the pipeline (rc 252): a path that names no
    bucket (``s3://``, ``s3:///k`` - `refuse_bucketless`) and the
    unsupported ARN families. The storage reads *arg* the way its
    constructor does - the ``s3://`` scheme optional, access-point ARNs kept
    whole - so the scheme-less path ``presign`` accepts (``/k``) is refused
    as its ``s3:///k`` spelling is.
    """
    storage = S3Storage(arg, client=client, page_size=page_size)
    refuse_bucketless(storage.bucket)
    storage.validate()
    return storage


def resolve_transfer_config(ctx: Context, s3: S3, *, paths_type: str) -> Any:
    """The transfer config for this run, resolved by the library.

    ``ctx.transfer_config`` (a test's determinism lever) supplies the settings
    it sets; the rest come from the profile's ``[s3]`` section of the exact
    session bound to *s3*, and the engine is picked as aws-cli picks it
    (`S3.resolve_transfer_config`). The first half of `build_transfer_engine`.
    """
    return s3.resolve_transfer_config(ctx.transfer_config, copy=paths_type == "s3s3")


def build_transfer_engine(
    ctx: Context, s3: S3, client: S3Client, *, paths_type: str
) -> TransferConfig:
    """aws-cli's ``_get_transfer_manager`` slot: ``[s3]`` read, then the engine built.

    Every command that has the slot calls it there - cp, mv, sync and rm:
    after the path validations, before anything the run itself does. The
    config comes from `resolve_transfer_config` (an invalid ``[s3]`` value
    is 255) and is returned for the run. aws-cli then builds the manager
    (``TransferManagerFactory.create_transfer_manager``) before it decides
    anything about the run, so a CRT selection pays its whole construction
    there - the cross-process lock and ``create_s3_crt_client`` - even for a
    ``--dryrun`` that transfers nothing, and even for ``rm``, whose deletes
    ride it but build it later (with the deleter, or just before a single
    key's request). `S3.materialize_crt_engine` pays the same construction
    here, so the same failures land at the same point.

    The CLI's `S3` stops a CRT run there when the region chain resolved
    nothing (`clientfactory.build_s3`); every failure the construction raises
    is reported as it is.
    """
    transfer_config = resolve_transfer_config(ctx, s3, paths_type=paths_type)
    s3.materialize_crt_engine(client, transfer_config=transfer_config)
    return transfer_config


def build_printer(
    args: argparse.Namespace, progress_frequency: int, *, only_show_errors: bool = False
) -> TransferPrinter:
    """The shared ``TransferPrinter`` wiring.

    ``only_show_errors`` ORs into the flag for cp's stream rule (a streaming
    download owns stdout for the object bytes, so aws-cli forces the errors-only
    printer); mv/sync pass nothing.
    """
    return TransferPrinter(
        quiet=args.quiet,
        only_show_errors=args.only_show_errors or only_show_errors,
        progress=args.progress,
        frequency=progress_frequency,
        multiline=args.progress_multiline,
    )


def finish_transfer(printer: TransferPrinter, *, quiet: bool, run: Callable[[], None]) -> int:
    """Run the library call and derive the `aws` exit code (design/cli.md section 6).

    Everything the pipeline raises is rc 1: ``BatchError`` after per-item
    ``failed`` lines, a ``KeyboardInterrupt`` as one ``cancelled: ctrl-c
    received`` line, anything else run-killing as one ``fatal error:`` line -
    any exception type except ``AssertionError`` (an internal-invariant bug,
    re-raised loudly like the dispatcher does) and ``SystemExit`` (an orderly
    exit request, honored - a deliberate divergence from aws-cli's recorder,
    which suppresses even that; the taxonomy special-cases only
    KeyboardInterrupt), matching aws-cli's
    ``CommandResultRecorder.__exit__``,
    which converts whatever escapes the pipeline span into an ``ErrorResult``
    (aws-cli's ``CtrlCResult`` is minted separately - its ``DoneResultSubscriber``
    on a ``CancelledError`` - and our cancelled line renders that same shape;
    so e.g. a ``RecursionError`` from a
    pathologically deep tree is aws-cli's ``fatal error`` rc 1, never the
    dispatcher's 255, and a mid-run Ctrl-C is never its 130). A clean run exits 2
    when only warnings accumulated, else 0. The printer's rendering thread
    outlives ``run()`` only by its drain: every queued line is written (and
    precedes a ``fatal error:`` line) before the exit code is derived.

    That drain is past aws-cli's recorder span too - its recorder's shutdown,
    waiting for its printer thread - so a Ctrl-C landing there is not
    converted: it reaches the dispatcher's 130 (and its newline) once the
    printer has written what was queued (``TransferPrinter.finish``),
    whatever the run itself ended with (measured: all lines, rc 130).
    """
    error: BaseException | None = None
    try:
        with printer:
            try:
                run()
            except BaseException as exc:  # classified below, once every line is out
                error = exc
    except KeyboardInterrupt:
        # From the drain: aws-cli's printer thread still writes the run's own
        # closing line - it is a queued result there - before the 130
        # (measured: a mid-run Ctrl-C, then one in the drain, prints
        # `cancelled: ctrl-c received` and exits 130).
        line = None if error is None else _closing_line(error)
        if line is not None and not quiet:
            output.write_stderr(line)
        raise
    if error is None:
        return 2 if printer.warned else 0
    return report_failed_run(error, quiet=quiet)


def report_failed_run(error: BaseException, *, quiet: bool) -> int:
    """aws-cli's ``CommandResultRecorder`` on a run span that raised *error*: rc 1.

    The span's closing line (`_closing_line`) goes to stderr unless *quiet*,
    and the run exits 1 - `finish_transfer`'s verdict once its printer has
    drained, and ``rm``'s, whose run span sits inside the same recorder on
    aws-cli (rm is an ``S3TransferCommand`` there too). An ``AssertionError`` -
    an internal-invariant violation, a bug - is re-raised instead, surfacing
    loudly like the dispatcher's own re-raise (cli.py): masking it as a fatal
    error would also blunt the test doubles' unexpected-call guards.
    ``SystemExit`` and the rest of the ``BaseException`` family (a
    ``KeyboardInterrupt`` aside) pass the same way.
    """
    if isinstance(error, AssertionError) or not isinstance(error, Exception | KeyboardInterrupt):
        raise error
    line = _closing_line(error)
    if line is not None and not quiet:
        output.write_stderr(line)
    return 1


def _closing_line(error: BaseException) -> str | None:
    """The line a run that raised *error* ends with, or None.

    `report_failed_run` writes it, and `finish_transfer` for a Ctrl-C in its
    drain.

    ``BatchError`` has none - its per-item failure lines were already
    streamed by the printer. A ``KeyboardInterrupt`` inside the pipeline span
    is a cancelled run, not the dispatcher's 130: aws-cli's result machinery
    swallows the interrupt (``CommandResultRecorder.__exit__``), the shutdown
    cancels the accepted transfers, and the printer emits one ``cancelled:
    ctrl-c received`` line at rc 1 (measured mid-sync and mid-rm, 2.36.40);
    the per-item CANCELLED records stay silent like aws-cli's (progress.py
    ``_prints``), and the pre-pipeline spans keep the 130 backstop. Anything
    else that killed the run is one ``fatal error:`` line.
    """
    if isinstance(error, BatchError):
        return None
    if isinstance(error, KeyboardInterrupt):
        return "cancelled: ctrl-c received\n"
    if isinstance(error, Exception) and not isinstance(error, AssertionError):
        return f"fatal error: {error}\n"
    return None


__all__ = [
    "TransferPaths",
    "add_transfer_arguments",
    "build_printer",
    "build_transfer_engine",
    "build_transfer_options",
    "check_local_source",
    "classify_paths",
    "create_local_dest_dir",
    "finish_transfer",
    "identify_type",
    "lists_unordered",
    "normalize_s3_path",
    "path_storage",
    "refuse_unwalkable_source",
    "report_failed_run",
    "resolve_case_conflict",
    "resolve_locations",
    "resolve_metadata_option",
    "resolve_paramfile_values",
    "resolve_transfer_config",
    "validate_checksum_paths_type",
    "validate_sse_c_pairing",
    "validate_streaming_paths",
]
