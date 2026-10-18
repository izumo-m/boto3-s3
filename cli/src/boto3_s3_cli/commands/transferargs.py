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
import sys
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from boto3_s3 import (
    AnnotationCopyMode,
    BatchError,
    CaseConflictMode,
    CopyPropsMode,
    InvalidConfigError,
    LocalStorage,
    S3Storage,
    TransferOptions,
    ValidationError,
    crtsupport,
)
from boto3_s3.localstorage import translate_os_error
from boto3_s3.pathresolver import is_s3express_accesspoint_path, is_s3express_path
from boto3_s3.transfer import conditional_write_unsupported_reason
from boto3_s3_cli import (
    checksumdefault,
    filters,
    paramfile,
    shorthand,
    usage,
)
from boto3_s3_cli.commands.base import (
    add_page_size_argument,
    add_request_payer_argument,
    expand_integer_paramfile,
    expand_option_paramfile,
    parse_integer_option,
)
from boto3_s3_cli.output import uni_write
from boto3_s3_cli.progress import TransferPrinter

if TYPE_CHECKING:
    from collections.abc import Callable

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

# The contiguous run of free-string transfer options that take aws's paramfile
# resolution, in aws's ``TRANSFER_ARGS`` registration order (subcommands.py):
# ``website-redirect`` through ``source-region``. Each is a string-typed
# server parameter, so a ``file://`` reference sends the file text and a
# ``fileb://`` one is rejected (bytes for a string parameter, 252 -
# ``resolve_text_paramfile``). The choices-validated options (acl /
# storage_class / sse / ... ) can't carry such a value (argparse rejects it as
# an invalid choice first); ``sse-kms-key-id`` and ``grants`` sit earlier in
# the registration order (resolved individually in ``resolve_paramfile_values``);
# ``metadata`` is a map, resolved after the integer coercions.
_PARAMFILE_TEXT_OPTIONS: tuple[str, ...] = (
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
    recursive, and aws rejects the flag as an unknown option there)."""
    parser.add_argument("paths", nargs=2, metavar="<path>", help="source and destination location")
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
    # like aws's bare int(), not argparse's 252).
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
        # aws (an in-pipeline fatal, rc 1 - not 255).
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
    """``"s3"`` iff the path starts with ``s3://`` - the only S3 marker aws knows.

    The same ``s3://`` test as aws-cli's ``FileFormat.identify_type`` (only the
    classification rule is ported; aws also strips the prefix and returns a
    ``(type, path)`` tuple). String classification is the CLI layer's job (the
    library planner receives resolved ``Storage`` objects and never re-parses a
    path).
    """
    return "s3" if path.startswith("s3://") else "local"


def resolve_paramfile_values(args: argparse.Namespace, *, operation: str) -> tuple[int | None, int]:
    """Resolve the direct-option paramfiles and integer coercions.

    The ``file://`` / ``fileb://`` paramfile expansion (a bad reference is
    252) and, for the two ``integer`` options, the bare ``int()`` coercion
    (255): the SSE-C key blob, ``--sse-kms-key-id``, the copy-source blob,
    the refusal of a ``--grants`` paramfile, the free-string text block, then
    ``--progress-frequency`` and ``--page-size`` (paramfile then coercion).
    Returns the two coerced integers. ``--metadata`` (a map) and cp's
    ``--expected-size`` are resolved by ``classify_paths`` (``resolve_metadata_option`` /
    ``expand_option_paramfile``). ``build_transfer_options`` consumes the
    resolved values verbatim.
    """
    if args.sse_c_key is not None:
        args.sse_c_key = blob_value(args.sse_c_key, "--sse-c-key", operation=operation)
    if args.sse_kms_key_id is not None:
        args.sse_kms_key_id = resolve_text_paramfile(
            args.sse_kms_key_id, "--sse-kms-key-id", operation=operation
        )
    if args.sse_c_copy_source_key is not None:
        args.sse_c_copy_source_key = blob_value(
            args.sse_c_copy_source_key, "--sse-c-copy-source-key", operation=operation
        )
    _resolve_grants(args, operation=operation)
    for option in _PARAMFILE_TEXT_OPTIONS:
        value = getattr(args, option, None)
        if value is not None:
            resolved = resolve_text_paramfile(
                value, f"--{option.replace('_', '-')}", operation=operation
            )
            setattr(args, option, resolved)
    progress_frequency = _resolve_integer_option(args, "progress_frequency", operation=operation)
    # --progress-frequency's argparse default is 0, so its unset-None branch is
    # unreachable; --page-size's default is None (nothing sent, aws parity).
    assert progress_frequency is not None
    page_size = _resolve_integer_option(args, "page_size", operation=operation)
    return page_size, progress_frequency


def _resolve_grants(args: argparse.Namespace, *, operation: str) -> None:
    """Refuse a lone ``--grants`` paramfile reference (252).

    aws unwraps a one-element list before its paramfile hook, so ``--grants
    file://g`` loads the file and its grant parser then walks the text
    character by character (bytes int by int) - an aws bug this CLI does not
    copy (docs/cli/aws-differences.md section 2). The reference is refused
    without being read. A multi-element ``--grants`` or a prefix-less value
    stays the list argparse produced; a ``file://`` element there is a literal
    grant on aws too, refused by the grant check in flight.
    """
    grants: object = args.grants
    if isinstance(grants, list):
        items = cast("list[object]", grants)
        if (
            len(items) == 1
            and isinstance(items[0], str)
            and items[0].startswith(("file://", "fileb://"))
        ):
            raise ValidationError(
                "Error parsing parameter '--grants': a file:// or fileb:// reference "
                "is not supported; give each grant as an argument",
                operation=operation,
            )


def _resolve_integer_option(args: argparse.Namespace, dest: str, *, operation: str) -> int | None:
    """Expand a string-typed integer option's paramfile, then coerce.

    aws expands the paramfile then runs its bare ``int()`` at the option's
    registration position (a missing reference is the load 252, a non-integer
    the coercion 255). ``expand_integer_paramfile`` loads both ``file://`` text
    and ``fileb://`` bytes (aws feeds either to ``int()``); prefix-less values
    fall through into ``int()``.
    """
    expand_integer_paramfile(args, dest, operation=operation)
    return parse_integer_option(getattr(args, dest), operation=operation)


def resolve_metadata_option(args: argparse.Namespace, *, operation: str) -> None:
    """Resolve ``--metadata`` in place: paramfile, then the shorthand parse.

    A ``file://`` whole-value reference loads the map text (252 on a missing
    file). A ``fileb://`` one is refused at 252 without being read: aws loads
    the bytes and then crashes in its shorthand parser (rc 255), and no bytes
    can be a map (docs/cli/aws-differences.md section 2).
    """
    if args.metadata is None:
        return
    if args.metadata.startswith("fileb://"):
        raise ValidationError(
            "Error parsing parameter '--metadata': a fileb:// reference is not "
            "supported; use file:// to read the map from a file",
            operation=operation,
        )
    metadata_value = resolve_text_paramfile(args.metadata, "--metadata", operation=operation)
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
    then the local-local pair gate (252) and aws's
    ``_normalize_s3_trailing_slash`` over both paths (``normalize_s3_path``:
    the refused ARN forms, 252, then the keyless trailing ``/``; the returned
    paths are the normalized ones), and last the S3 client build. Only this
    shared prefix lives here - the stream / checksum / SSE-C checks that
    follow stay with each command.
    """
    page_size, progress_frequency = resolve_paramfile_values(args, operation=operation)
    resolve_metadata_option(args, operation=operation)
    # cp-only (``--expected-size``); a no-op where the attribute is absent.
    # A *string*-typed option in aws (no cli_type_name, unlike --page-size),
    # so a readable fileb:// is rejected with botocore's bytes 252 at parse -
    # measured: aws 252 where the integer helper would run int(b'5') and
    # proceed. The stream route's int() coercion still runs at submit time.
    expand_option_paramfile(args, "expected_size", operation=operation)
    src, dest = args.paths
    src_type = identify_type(src)
    dest_type = identify_type(dest)
    if src_type == "local" and dest_type == "local":
        raise ValidationError(usage.two_path_usage(operation), operation=operation)
    # aws's add_paths runs _normalize_s3_trailing_slash right after the
    # path-type check.
    src = normalize_s3_path(src)
    dest = normalize_s3_path(dest)
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


def normalize_s3_path(path: str) -> str:
    """One path argument as aws's ``_normalize_s3_trailing_slash`` leaves it.

    aws runs this over cp / mv / sync / rm's paths right after the path-type
    check. Its ``find_bucket_key`` refuses the S3 Object Lambda and Outposts
    *bucket* ARNs first (rc 252, whatever else is wrong with the run), then a
    keyless ``s3://`` path gains the trailing ``/`` that makes it the bucket
    root, and every later step re-reads the result. The re-read changes one
    spelling: an Outposts access point ARN set off with ``:`` and no key
    (``.../accesspoint/ap:``) becomes ``.../accesspoint/ap:/``, whose key is
    ``/`` - so ``rm --recursive`` lists under ``/`` and a single download
    heads the key ``/``, not the whole access point. A local path, and the
    bucket-less forms the commands handle on their own (``s3://``,
    ``s3:///k``), pass unchanged.
    """
    if not path.startswith("s3://"):
        return path
    bucket, _key = S3Storage.split_bucket_key(S3Storage.strip_scheme(path))
    if bucket:
        # The ARN rejections are all S3Storage.validate checks here; its other
        # one (a key without a bucket) needs no bucket to apply to.
        S3Storage(path).validate()
    return S3Storage.normalize_s3_uri(path)


def create_local_dest_dir(dest: str, *, operation: str) -> None:
    """Pre-create the ``s3local`` destination directory (aws's ``_validate_path_args``).

    aws creates the destination during validation whenever the operation is a
    ``dir_op`` (``cp``/``mv`` ``--recursive`` and every ``sync``) - before the
    pipeline - so a creation failure is its pre-pipeline rc 255, not the
    pipeline's rc 1. Pre-creating it outside ``finish_transfer``'s catch keeps
    that shape (the library still ensures the dir for direct callers; this
    makes it a no-op there). The bare ``exists`` test then bare ``makedirs``
    mirror aws exactly; every ``translate_os_error`` category maps to the
    same rc 255.
    """
    if not os.path.exists(dest):
        try:
            os.makedirs(dest)
        except OSError as exc:
            raise translate_os_error(exc, operation=operation, key=None) from exc


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
    usable on an old botocore (back-compat floor, docs/compatibility.md)."""
    if not no_overwrite or paths_type not in ("locals3", "s3s3"):
        return
    reason = conditional_write_unsupported_reason(client, is_copy=paths_type == "s3s3")
    if reason is not None:
        raise ValidationError(reason, operation=operation)


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
    mode to ``ignore`` after the message. aws takes that branch for the
    ``--x-s3`` suffix only; a source named through an access point of a
    directory bucket (``--xa-s3``) lists just as unordered, and under aws
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
    if not (is_s3express_path(src) or is_s3express_accesspoint_path(src)):
        return mode
    if mode is not CaseConflictMode.WARN:
        raise ValidationError(
            f"`{args.case_conflict}` is not a valid value for `--case-conflict` "
            "when operating on S3 Express directory buckets. "
            "Valid values: `warn`, `ignore`.",
            operation=operation,
        )
    # aws emits this via ``uni_print`` with no trailing newline (measured: the
    # message ends at ``...html.`` and the next stderr output concatenates);
    # uni_write is that port - same bytes plus the flush that keeps the
    # newline-less warning from sitting in a block-buffered stderr (a
    # redirected run) until process exit, behind output aws prints after it.
    uni_write(
        sys.stderr,
        "warning: Recursive copies/moves from an S3 Express directory "
        "bucket to a case-insensitive local filesystem may result in "
        "undefined behavior if there are S3 object key names that differ "
        "only by case. To disable this warning, set the `--case-conflict` "
        "parameter to `ignore`. For more information, see "
        "https://docs.aws.amazon.com/cli/latest/topic/"
        "s3-case-insensitivity.html.",
    )
    return CaseConflictMode.IGNORE


def default_upload_checksum(client: Any, paths_type: str, transfer_config: Any) -> str | None:
    """aws's default request checksum for the uploads a run makes, else None.

    Only an upload run (``locals3``) gets one: by default aws sends no
    checksum algorithm on a copy's CopyObject / UploadPartCopy (measured
    against the pinned aws; an explicit ``--checksum-algorithm`` reaches them
    on both tools through the request mapping), and a download writes
    nothing. The value is `checksumdefault`'s, chosen by the engine
    *transfer_config* resolves to (`resolve_transfer_config`; the same
    `crtsupport.selects_crt` test the engine applies): the CRT engine's
    default holds whatever ``request_checksum_calculation`` says, the classic
    engine's only under ``when_supported`` - aws's own split, measured on
    the wire.
    """
    if paths_type != "locals3":
        return None
    if crtsupport.selects_crt(transfer_config):
        return checksumdefault.crt_default_algorithm()
    return checksumdefault.default_algorithm(client)


def build_transfer_options(
    args: argparse.Namespace,
    case_conflict: CaseConflictMode,
    *,
    operation: str,
    default_checksum_algorithm: str | None = None,
) -> TransferOptions:
    """Translate parsed flags into the library's ``TransferOptions``.

    ``default_checksum_algorithm`` (`default_upload_checksum`) stands in for
    an absent ``--checksum-algorithm`` the way aws's bundled botocore default
    does for aws: named here, it reaches both transfer engines through the
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
            # (aws resolves them at parse time), so the values here are
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


def resolve_text_paramfile(value: str, name: str, *, operation: str) -> str:
    """Apply aws's paramfile resolution to a string-typed argument.

    aws-cli registers a global URI paramfile handler on every ``aws s3`` arg, so
    ``--content-type file://ct.txt`` (etc.) sends the *file contents*. A value
    without a paramfile prefix passes through verbatim. A ``fileb://`` reference
    loads bytes, which botocore then rejects for a string-typed parameter at
    parse time (measured: rc 252, this exact wording) - a missing file is still
    the load 252.
    """
    if value.startswith("file://"):
        with paramfile.named_argument(name, operation=operation):
            return paramfile.read_text_paramfile(value, operation=operation)
    if value.startswith("fileb://"):
        with paramfile.named_argument(name, operation=operation):
            loaded = paramfile.read_binary_paramfile(value, operation=operation)
        raise ValidationError(
            "Parameter validation failed:\n"
            f"Invalid type for parameter input, value: {loaded!r}, "
            "type: <class 'bytes'>, valid types: <class 'str'>",
            operation=operation,
        )
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
        with paramfile.named_argument(name, operation=operation):
            return paramfile.read_binary_paramfile(value, operation=operation)
    return resolve_text_paramfile(value, name, operation=operation)


def resolve_locations(
    args: argparse.Namespace,
    ctx: Context,
    s3: S3,
    client: Any,
    src: str,
    dest: str,
    *,
    src_type: str,
    dest_type: str,
    page_size: int | None,
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

    def _s3(arg: str, client_for: Any) -> S3Storage:
        # build_s3_storage applies the strict aws-cli validation (rc 252
        # ahead of the pipeline) with the measured bucket-less carve-out. The
        # CLI's process-fatal Ctrl-C posture is not a storage concern:
        # build_s3 declares it once (S3's reusable_after_interrupt).
        return build_s3_storage(arg, client=client_for, page_size=page_size)

    def _local(path: str) -> LocalStorage:
        return LocalStorage(path, follow_symlinks=args.follow_symlinks)

    if src_type == "local":
        return _local(src), _s3(dest, client)
    if dest_type == "local":
        # aws-cli reads an empty destination of a single download as the
        # working directory (its local_format absolutizes "" to the cwd, which
        # is a directory); the library refuses "" as no location at all, so
        # the CLI spells out what aws-cli means. A dir_op never gets here with
        # "": pre-creating the destination already failed it (rc 255, like
        # aws-cli), and an empty source is refused as missing before that.
        return _s3(src, client), _local(dest or os.curdir)
    source_client = client
    if args.source_region:
        source_args = argparse.Namespace(**vars(args))
        source_args.region = args.source_region
        source_args.endpoint_url = None
        source_client = ctx.client(source_args, s3)
    return _s3(src, source_client), _s3(dest, client)


def path_storage(arg: str, kind: str) -> S3Storage | LocalStorage:
    """A plan-only endpoint Storage for ``transferplan.plan_transfer``.

    The planner routes by concrete type and each side formats itself from its
    held state (``Storage.format``), so the S3 side carries no client here -
    this is the throwaway used purely to derive the path shapes (roots, the
    stdin dest key). The real transfer storages (with a client) are built in
    ``resolve_locations``.
    """
    return S3Storage(arg) if kind == "s3" else LocalStorage(arg)


class _BucketlessS3Storage(S3Storage):
    """A key-carrying bucket-less s3 URI (``s3:///k``) riding onward.

    aws has no up-front rejection for the form: it sends Bucket="" toward the
    API and each command fails wherever that first bites. ``S3Storage.validate``
    (which the library entry re-runs via ``_validate_storage``) would instead
    refuse the form as a 252-shaped ``ValidationError``, so this override is
    what lets it ride, and the failure that follows is aws's own. Measured
    against the pinned aws-cli: on the transfer family botocore's client-side
    validation makes the blind single download's HeadObject and every listing
    fatal (rc 1, ``--dryrun`` included), a live upload/copy fails per item at
    submit time, and a dryrun upload never reaches the submit at all (rc 0);
    ``presign`` fails the same validation while signing (rc 252). ``website``
    never reaches botocore - it folds the whole remainder into the bucket name
    like aws does and rejects it itself - and needs the pass-through only to
    get that far. The ARN rejections this override skips cannot apply to a
    bucket-less URI.
    """

    def validate(self) -> None:
        """aws-faithful pass-through: botocore validates at request time."""


def build_s3_storage(arg: str, *, client: Any, page_size: int | None = None) -> S3Storage:
    """An ``S3Storage`` for a CLI s3 path, validated the aws-cli way.

    Shared by the transfer family, ``rm``, ``presign`` and ``website``. The
    strict aws-cli rejections (the unsupported ARN families) fire here, ahead
    of the pipeline (rc 252, aws's parse-time parity); the bucket-less
    key-carrying form is the one carve-out, flowing through
    ``_BucketlessS3Storage`` so aws's stage-shaped failures fall out of the
    normal machinery.

    The split reads *arg* exactly as the ``S3Storage`` constructor reads it -
    the ``s3://`` scheme optional, access-point ARNs kept whole - so the
    scheme-less path ``presign`` accepts (``/k``) is recognized as the same
    carve-out its ``s3:///k`` spelling is.
    """
    bucket, key = S3Storage.split_bucket_key(S3Storage.strip_scheme(arg))
    if not bucket and key:
        return _BucketlessS3Storage(arg, client=client, page_size=page_size)
    storage = S3Storage(arg, client=client, page_size=page_size)
    storage.validate()
    return storage


def resolve_transfer_config(ctx: Context, s3: S3, *, paths_type: str) -> Any:
    """The transfer config for this run, resolved by the library.

    ``ctx.transfer_config`` (a test's determinism lever) supplies the settings
    it sets; the rest come from the profile's ``[s3]`` section of the exact
    session bound to *s3*, and the engine is picked as aws picks it
    (`S3.resolve_transfer_config`). Called past the usage (252) and
    missing-source (255) validations, so an invalid ``[s3]`` value loses to
    both, as on aws.
    """
    return s3.resolve_transfer_config(ctx.transfer_config, copy=paths_type == "s3s3")


def materialize_transfer_engine(
    s3: S3, client: S3Client, transfer_config: Any, *, operation: str
) -> None:
    """aws's transfer-manager construction slot, for every command that has one.

    aws builds the manager (``TransferManagerFactory.create_transfer_manager``)
    right after reading ``[s3]`` and before it decides anything about the run,
    so a CRT selection pays its whole construction there - the cross-process
    lock and ``create_s3_crt_client`` - even for a ``--dryrun`` that transfers
    nothing, and even for ``rm``, whose deletes ride it but build it later
    (with the deleter, or just before a single key's request). Call it at that
    slot so the same failures land at the same point.

    The one failure it converts is awscrt's own ``assert isinstance(region,
    str)``, which fires when the region chain resolved nothing (`clientfactory`
    declares the chain's answer as the CRT region). aws catches it in its
    general handler, which formats a bare ``AssertionError`` as an empty rc-255
    report; this CLI re-raises ``AssertionError`` everywhere else (an
    internal-invariant bug, and the test doubles' unexpected-call guards depend
    on it), so the conversion is scoped to this one call site and keeps the
    empty message verbatim.
    """
    try:
        s3.materialize_crt_engine(client, transfer_config=transfer_config)
    except AssertionError as exc:
        # InvalidConfigError -> rc 255, aws's general handler; str(exc) is ''
        # for a bare assert, which the error writer renders without a detail.
        raise InvalidConfigError(str(exc), operation=operation) from exc


def build_printer(
    args: argparse.Namespace, progress_frequency: int, *, only_show_errors: bool = False
) -> TransferPrinter:
    """The shared ``TransferPrinter`` wiring.

    ``only_show_errors`` ORs into the flag for cp's stream rule (a streaming
    download owns stdout for the object bytes, so aws forces the errors-only
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
    """Run the library call and derive the aws exit code (design/cli.md section 6).

    Everything the pipeline raises is rc 1: ``BatchError`` after per-item
    ``failed`` lines, a ``KeyboardInterrupt`` as one ``cancelled: ctrl-c
    received`` line, anything else run-killing as one ``fatal error:`` line -
    any exception type except ``AssertionError`` (an internal-invariant bug,
    re-raised loudly like the dispatcher does) and ``SystemExit`` (an orderly
    exit request, honored - a deliberate divergence from aws's recorder,
    which suppresses even that; the taxonomy special-cases only
    KeyboardInterrupt), matching aws's
    ``CommandResultRecorder.__exit__``,
    which converts whatever escapes the pipeline span into an ``ErrorResult``
    (aws's ``CtrlCResult`` is minted separately - its ``DoneResultSubscriber``
    on a ``CancelledError`` - and our cancelled line renders that same shape;
    so e.g. a ``RecursionError`` from a
    pathologically deep tree is aws's ``fatal error`` rc 1, never the
    dispatcher's 255, and a mid-run Ctrl-C is never its 130). A clean run exits 2
    when only warnings accumulated, else 0. The printer's rendering thread
    outlives ``run()`` only by its drain: every queued line is written (and
    precedes a ``fatal error:`` line) before the exit code is derived.

    That drain is past aws's recorder span too - its recorder's shutdown,
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
        # From the drain: aws's printer thread still writes the run's own
        # closing line - it is a queued result there - before the 130
        # (measured: a mid-run Ctrl-C, then one in the drain, prints
        # `cancelled: ctrl-c received` and exits 130).
        line = None if error is None else _closing_line(error)
        if line is not None and not quiet:
            sys.stderr.write(line)
        raise
    if error is None:
        return 2 if printer.warned else 0
    if isinstance(error, AssertionError) or not isinstance(error, Exception | KeyboardInterrupt):
        # An internal-invariant violation (a bug) surfaces loudly, like the
        # dispatcher's AssertionError re-raise (cli.py) - masking it as a
        # fatal error would also blunt the test doubles' unexpected-call
        # guards; SystemExit and the rest of the BaseException family pass.
        raise error
    line = _closing_line(error)
    if line is not None and not quiet:
        sys.stderr.write(line)
    return 1


def _closing_line(error: BaseException) -> str | None:
    """The line a run that raised *error* ends with, or None (finish_transfer).

    ``BatchError`` has none - its per-item failure lines were already
    streamed by the printer. A ``KeyboardInterrupt`` inside the pipeline span
    is a cancelled run, not the dispatcher's 130: aws's result machinery
    swallows the interrupt (``CommandResultRecorder.__exit__``), the shutdown
    cancels the accepted transfers, and the printer emits one ``cancelled:
    ctrl-c received`` line at rc 1 (measured mid-sync and mid-rm, 2.36.40);
    the per-item CANCELLED records stay silent like aws's (progress.py
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
    "blob_value",
    "build_printer",
    "build_transfer_options",
    "classify_paths",
    "create_local_dest_dir",
    "finish_transfer",
    "identify_type",
    "is_s3express_accesspoint_path",
    "is_s3express_path",
    "materialize_transfer_engine",
    "normalize_s3_path",
    "path_storage",
    "resolve_case_conflict",
    "resolve_locations",
    "resolve_metadata_option",
    "resolve_paramfile_values",
    "resolve_text_paramfile",
    "resolve_transfer_config",
    "validate_checksum_paths_type",
    "validate_no_overwrite_supported",
    "validate_sse_c_pairing",
]
