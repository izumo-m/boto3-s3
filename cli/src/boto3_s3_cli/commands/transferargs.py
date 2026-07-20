"""The argument surface and run-pipeline pieces ``cp`` / ``mv`` / ``sync`` share.

``aws s3`` declares one ``TRANSFER_ARGS`` list plus per-command extras
(aws-cli's ``subcommands.py``); this module is that shared list and the
validation/translation steps the three commands run in the same order.
This module loads only once its command is determined (stage 2 of the lazy
dispatch). Top-level imports may therefore reach the AWS SDK.
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
    InvalidValueError,
    S3Storage,
    TransferOptions,
    ValidationError,
)
from boto3_s3_cli import clientfactory, filters, globalargs, paramfile, shorthand, usage
from boto3_s3_cli.commands.base import (
    add_page_size_argument,
    add_request_payer_argument,
    expand_integer_paramfile,
    expand_option_paramfile,
    parse_integer_option,
)
from boto3_s3_cli.progress import TransferPrinter

if TYPE_CHECKING:
    from collections.abc import Callable

    from boto3_s3 import S3, LocalStorage
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
# ``metadata`` is a map, resolved after the integer coercions. Ordered (a tuple)
# so the first failing paramfile matches aws's option-by-option processing.
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
    parser.add_argument("paths", nargs=2, metavar="<path>")
    parser.add_argument("--dryrun", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    if include_recursive:
        parser.add_argument("--recursive", action="store_true")
    filters.add_filter_arguments(parser)
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
    add_page_size_argument(parser)
    parser.add_argument("--ignore-glacier-warnings", action="store_true")
    parser.add_argument("--force-glacier-transfer", action="store_true")
    add_request_payer_argument(parser)
    parser.add_argument("--metadata")
    parser.add_argument(
        "--copy-props", choices=["none", "metadata-directive", "default", "all"], default="default"
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
    """Resolve the direct-option paramfiles and integer coercions, in aws order.

    aws processes each argument one at a time in its ``TRANSFER_ARGS``
    registration order (``BasicCommand.__call__``'s unpack loop): the
    ``file://`` / ``fileb://`` paramfile expansion and, for the two
    ``integer`` options, the bare ``int()`` coercion happen together at that
    option's position. So a bad reference or a bad ``int()`` wins by whichever
    option comes first - a bad ``--progress-frequency`` (255) beats a later
    ``--page-size file://missing`` (252), while an earlier ``--content-type
    file://missing`` (252) beats ``--page-size abc`` (255); all measured
    against the pinned aws-cli. The order here mirrors that: the SSE-C key blob,
    ``--sse-kms-key-id``, the copy-source blob, ``--grants``, the free-string
    text block, then ``--progress-frequency`` and ``--page-size`` (paramfile
    then coercion). Returns the two coerced integers. ``--metadata`` (a map)
    and cp's ``--expected-size`` come later, at their own registration
    positions (``resolve_metadata_option`` / ``expand_integer_paramfile`` in
    ``classify_paths``). ``build_transfer_options`` consumes the resolved
    values verbatim.
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
    """Apply aws's single-element paramfile unwrap to ``--grants`` in place.

    aws's ``URIArgumentHandler`` unwraps a length-1 list before the paramfile
    map, so ``--grants file://g`` (a lone value) sends the file's text and
    ``fileb://`` its bytes - the grant parser then consumes that verbatim
    (a text file iterates character by character, bytes int by int, both
    surfacing in-flight the way aws does). A multi-element ``--grants`` or a
    prefix-less value stays the list argparse produced. Measured against the pinned
    aws-cli: ``--grants file:///missing`` is the load 252.
    """
    grants: object = args.grants
    if isinstance(grants, list):
        items = cast("list[object]", grants)
        if len(items) == 1 and isinstance(items[0], str):
            loaded = paramfile.get_paramfile(items[0], name="--grants", operation=operation)
            if loaded is not None:
                args.grants = loaded


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

    Ordered *after* the integer coercions - unlike the direct-option
    paramfiles above - because aws handles the map option's value with its
    shorthand machinery (measured: ``--metadata file:///no/x --page-size
    abc`` is the coercion's 255, while a direct option's bad paramfile wins
    with 252). A ``file://`` whole-value reference loads the map text (252 on
    a missing file); a ``fileb://`` one loads bytes and then aws crashes
    indexing them in its shorthand parser (measured: rc 255 with this exact
    message, the ``int`` from a bytes index reaching ``in`` a string), so a
    missing file is still the load 252 but an existing one is the 255.
    """
    if args.metadata is None:
        return
    if args.metadata.startswith("fileb://"):
        paramfile.read_binary_paramfile(args.metadata, name="--metadata", operation=operation)
        raise InvalidValueError(
            "'in <string>' requires string as left operand, not int", operation=operation
        )
    metadata_value = resolve_text_paramfile(args.metadata, "--metadata", operation=operation)
    args.metadata = shorthand.parse_map_option(
        metadata_value, name="--metadata", operation=operation
    )


class TransferPaths(NamedTuple):
    """The classified head of a cp/mv/sync invocation (the path-type gate's input)."""

    page_size: int | None
    progress_frequency: int
    src: str
    dest: str
    src_type: str
    dest_type: str
    paths_type: str  # "locals3" | "s3local" | "s3s3" on the CLI surface
    s3: S3


def classify_paths(args: argparse.Namespace, ctx: Context, *, operation: str) -> TransferPaths:
    """The shared ``run()`` head, in aws's parse-to-validation order.

    The order is exit-code-load-bearing, identical across cp/mv/sync, and
    measured against the pinned aws-cli on the combined-error cases: the ``--query``
    compile (252, aws resolves it at ``top-level-args-parsed``, ahead of
    everything) -> the ``--endpoint-url`` scheme check (252, aws validates the
    value at parse time) -> the direct-option paramfiles and the two integer
    coercions, interleaved per aws's option-by-option order
    (``resolve_paramfile_values``: a bad paramfile is 252, a bad ``int()`` 255,
    and the earlier option wins) -> the ``--metadata`` resolution (252,
    paramfile + shorthand, which the coercions beat) -> cp's ``--expected-size``
    paramfile (252) -> the session profile resolution (255: aws binds the
    profile at startup, so a bad ``--profile`` beats every post-parse usage
    error) -> the local-local pair gate (252). Only this shared, order-stable
    prefix lives here - the stream / checksum / SSE-C checks that follow differ
    in relative order per command and stay with each command.
    """
    globalargs.validate_query(args)
    clientfactory.validate_endpoint_url(args)
    page_size, progress_frequency = resolve_paramfile_values(args, operation=operation)
    resolve_metadata_option(args, operation=operation)
    # cp-only (``--expected-size``); a no-op where the attribute is absent.
    # A *string*-typed option in aws (no cli_type_name, unlike --page-size),
    # so a readable fileb:// is rejected with botocore's bytes 252 at parse -
    # measured: aws 252 where the integer helper would run int(b'5') and
    # proceed. The stream route's int() coercion still runs at submit time.
    expand_option_paramfile(args, "expected_size", operation=operation)
    s3 = ctx.s3(args)
    src, dest = args.paths
    src_type = identify_type(src)
    dest_type = identify_type(dest)
    if src_type == "local" and dest_type == "local":
        raise ValidationError(usage.two_path_usage(operation), operation=operation)
    return TransferPaths(
        page_size,
        progress_frequency,
        src,
        dest,
        src_type,
        dest_type,
        src_type + dest_type,
        s3,
    )


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
            from boto3_s3.localstorage import translate_os_error

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
    usable on an old botocore (back-compat floor, docs/overview.md section 2)."""
    if not no_overwrite or paths_type not in ("locals3", "s3s3"):
        return
    # Import only when the installed client's model must be inspected.
    from boto3_s3.transfer import conditional_write_unsupported_reason

    reason = conditional_write_unsupported_reason(client, is_copy=paths_type == "s3s3")
    if reason is not None:
        raise ValidationError(reason, operation=operation)


def is_s3express_path(path: str) -> bool:
    """Whether an ``s3://`` path names an S3 Express directory bucket
    (aws-cli's ``is_s3express_bucket``: the ``--x-s3`` suffix)."""
    if not path.startswith("s3://"):
        return False
    return S3Storage.split_bucket_key(path[len("s3://") :])[0].endswith("--x-s3")


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
    # aws emits this via ``uni_print`` with no trailing newline (measured: the
    # message ends at ``...html.`` and the next stderr output concatenates).
    sys.stderr.write(
        "warning: Recursive copies/moves from an S3 Express directory "
        "bucket to a case-insensitive local filesystem may result in "
        "undefined behavior if there are S3 object key names that differ "
        "only by case. To disable this warning, set the `--case-conflict` "
        "parameter to `ignore`. For more information, see "
        "https://docs.aws.amazon.com/cli/latest/topic/"
        "s3-case-insensitivity.html."
    )
    return CaseConflictMode.IGNORE


def build_transfer_options(
    args: argparse.Namespace, case_conflict: CaseConflictMode, *, operation: str
) -> TransferOptions:
    """Translate parsed flags into the library's ``TransferOptions``."""
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
        return paramfile.read_text_paramfile(value, name=name, operation=operation)
    if value.startswith("fileb://"):
        loaded = paramfile.read_binary_paramfile(value, name=name, operation=operation)
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
        return paramfile.read_binary_paramfile(value, name=name, operation=operation)
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
    # Import the storage implementations when locations are resolved.
    from boto3_s3 import LocalStorage, S3Storage

    def _s3(arg: str, client_for: Any) -> S3Storage:
        # Construction is permissive (non-raising); validate the strict aws-cli
        # forms here so a usage error (rc 252) still precedes the transfer
        # pipeline. Ctrl-C is process-fatal in the CLI, so scans must not wait
        # for an in-flight page pull on the way out (aws dies immediately).
        storage = S3Storage(
            arg, client=client_for, page_size=page_size, scan_wait_on_interrupt=False
        )
        storage.validate()
        return storage

    def _local(path: str) -> LocalStorage:
        return LocalStorage(
            path, follow_symlinks=args.follow_symlinks, scan_wait_on_interrupt=False
        )

    if src_type == "local":
        return _local(src), _s3(dest, client)
    if dest_type == "local":
        return _s3(src, client), _local(dest)
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
    from boto3_s3 import LocalStorage, S3Storage

    return S3Storage(arg) if kind == "s3" else LocalStorage(arg)


def resolve_transfer_config(ctx: Context, s3: S3, *, paths_type: str) -> Any:
    """The transfer config for this run: the injected one, else from ``[s3]``.

    A test-injected ``ctx.transfer_config`` always wins (the existing
    determinism lever). Otherwise the profile's ``[s3]`` section is read from
    the exact session bound to *s3*, parsed (aws-cli's ``RuntimeConfig``), and
    turned into a ``TransferConfig`` whose ``preferred_transfer_client`` carries
    the aws-faithful engine decision (``runtimeconfig.resolve_transfer_client``).
    Reading ``[s3]`` here - past the usage (252) and missing-source (255)
    validations - matches aws's ordering (an invalid ``[s3]`` value loses to
    both).
    """
    if ctx.transfer_config is not None:
        return ctx.transfer_config
    from boto3_s3_cli import runtimeconfig

    scoped = runtimeconfig.load_scoped_s3_config(s3.aws_config())
    runtime_config = runtimeconfig.RuntimeConfig().build_config(**scoped)
    resolved = runtimeconfig.resolve_transfer_client(runtime_config, paths_type=paths_type)
    return runtimeconfig.build_transfer_config(scoped, runtime_config, resolved)


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
    """Run the library call and derive the aws exit code (docs/cli.md section 6).

    Everything the pipeline raises is rc 1: ``BatchError`` after per-item
    ``failed`` lines, a ``KeyboardInterrupt`` as one ``cancelled: ctrl-c
    received`` line, anything else run-killing as one ``fatal error:`` line -
    *any* exception type, matching aws's ``CommandResultRecorder.__exit__``,
    which converts whatever escapes the pipeline span into an ``ErrorResult``
    (Ctrl-C into a ``CtrlCResult``; so e.g. a ``RecursionError`` from a
    pathologically deep tree is aws's ``fatal error`` rc 1, never the
    dispatcher's 255, and a mid-run Ctrl-C is never its 130). A clean run exits 2
    when only warnings accumulated, else 0. The printer's rendering thread
    runs for exactly the ``run()`` span - the ``with`` drains it on every
    path, so all queued lines are written (and precede a ``fatal error:``
    line) before the exit code is derived.
    """
    try:
        with printer:
            run()
    except BatchError:
        # Per-item failure lines were already streamed by the printer.
        return 1
    except KeyboardInterrupt:
        # Ctrl-C inside the pipeline span is a cancelled run, not the
        # dispatcher's 130: aws's result machinery swallows the interrupt
        # (`CommandResultRecorder.__exit__`), the shutdown cancels the
        # accepted transfers, and the printer emits one
        # `cancelled: ctrl-c received` line at rc 1 (measured mid-sync and
        # mid-rm, 2.36.1). The per-item CANCELLED records stay silent like
        # aws's (progress.py `_prints`); the pre-pipeline spans keep the
        # 130 backstop.
        if not quiet:
            sys.stderr.write("cancelled: ctrl-c received\n")
        return 1
    except AssertionError:
        # An internal-invariant violation (a bug) surfaces loudly, like the
        # dispatcher's AssertionError re-raise (cli.py) - masking it as a
        # fatal error would also blunt the test doubles' unexpected-call
        # guards.
        raise
    except Exception as exc:
        if not quiet:
            sys.stderr.write(f"fatal error: {exc}\n")
        return 1
    return 2 if printer.warned else 0


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
    "is_s3express_path",
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
