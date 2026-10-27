"""The ``s3transfer``-backed transfer engine: ``TransferItem`` + ``Transferrer``.

One ``Transferrer`` serves one ``cp`` / ``mv`` / ``sync`` run, whose items all
share a single ``TransferType`` (a run moves bytes in one direction). It owns
the ``s3transfer.manager.TransferManager`` - built via ``Transferrer.prepare``
before a listing-driven run starts enumerating (client-event registration must
happen-before the scan prefetch worker's traffic on the same client), at the
first ``Transferrer.submit`` for a stream run, and never for a dry run (no
thread pools spun up; the ``s3transfer`` module itself is imported
earlier regardless, by ``boto3`` when a client is built) - and bridges
completions to the library's result model:
per-item ``OpResult`` records to ``on_result`` (from s3transfer worker
threads for submitted work - the calling thread itself under
``use_threads=False``'s NonThreadedExecutor; non-submitting records - a
directory source
rejected at submit, dryrun / skip / notice emissions - fire inline on the
submitting thread. Keep callbacks fast and non-raising either way, the
``S3Deleter`` contract; one that raises anyway ends the run with its
exception on the operation's thread, `Transferrer._deliver`),
lock-guarded rollup counters for ``BatchError``, byte progress to
``on_progress``.

Engine choices (parity-driven):

- The engine follows ``TransferConfig.preferred_transfer_client`` with
  boto3's own semantics (``'auto'`` default; ``'auto'``/``'crt'`` resolve
  through ``crtsupport``, design/crt.md): the classic
  ``s3transfer.manager.TransferManager`` unless the CRT manager is selected,
  and unconditionally classic for a copy run - the CRT manager has no copy,
  the same rule boto3 and aws-cli apply to s3->s3. The public
  ``transfer_config`` type is ``boto3_s3.transferconfig.TransferConfig``
  (boto3's subclass plus a CRT tuning field; defaults match aws-cli: 8 MiB
  threshold/chunk, 10-way concurrency, a 1000-deep download IO queue), and
  classic honors
  ``use_threads=False`` the way boto3 does - via the ``NonThreadedExecutor``
  (the CRT manager ignores the threading knobs, also like boto3).
- Backpressure is s3transfer's own bounded executors: the submission queue
  (``max_submission_queue_size``) and the request queue
  (``max_request_queue_size``) each block the submitting thread when full,
  exactly the mechanism aws-cli's handler leans on. No additional windowing
  is layered on top.
- Subscribers are plain duck-typed classes (s3transfer resolves callbacks
  with ``getattr``); no s3transfer base class is subclassed.
- The copy-props subscriber chain reproduces aws-cli's: a single-part
  CopyObject carries metadata and tags natively (directive defaults), while
  a multipart copy loses them - so metadata is re-injected from a source
  HeadObject (the cached single-source response when available) and tags via
  GetObjectTagging, inlined under S3's ~2 KiB ``Tagging`` header budget or
  applied by a post-copy PutObjectTagging whose failure rolls back the
  destination object. S3 object annotations track the mode too: wherever the
  copy-props chain runs on an annotations-capable SDK, every mode
  short of ``all`` sends ``AnnotationDirective=EXCLUDE`` so the copy carries
  none (aws-cli's default; an explicit ``metadata_directive`` bypasses the
  chain, and a pre-annotations SDK sends no directive), while
  ``copy_props=ALL`` carries them - on a multipart copy through
  `_SetAnnotations`, aws-cli's subscriber ported, which writes them after
  the copy behind the tagging write; the preload `AnnotationCopyMode`s stage
  the source payloads up front and ``DEFERRED`` reads them post-copy. A
  write that fails part way keeps the copied object and reports the failure,
  as aws-cli's ``AnnotationCopyError`` does.
"""

from __future__ import annotations

import contextlib
import enum
import errno
import functools
import logging
import mimetypes
import os
import sys
import tempfile
import threading
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from urllib.parse import quote

from botocore.exceptions import BotoCoreError, ClientError
from s3transfer.compat import seekable
from s3transfer.copies import CopySubmissionTask
from s3transfer.download import DownloadNonSeekableOutputManager, ImmediatelyWriteIOGetObjectTask
from s3transfer.exceptions import CancelledError as S3TransferCancelledError
from s3transfer.futures import NonThreadedExecutor
from s3transfer.manager import TransferManager
from s3transfer.upload import UploadSubmissionTask
from s3transfer.utils import OSUtils

from boto3_s3 import crtsupport, mimetable, requestparams, signingreads, transferconfig
from boto3_s3.exceptions import (
    AccessDeniedError,
    Boto3S3Error,
    CancelledError,
    ConfigurationError,
    InvalidConfigError,
    NotFoundError,
    TransportError,
    ValidationError,
)
from boto3_s3.localstorage import LocalStorage, name_files_as_text, translate_os_error
from boto3_s3.s3storage import S3Storage, request_pages
from boto3_s3.sdkerrors import (
    attribute_failure,
    client_build_error,
    crt_error_name,
    s3_errors,
    s3_request,
    translate_boto_error,
)
from boto3_s3.types import (
    AnnotationCopyMode,
    CancelMode,
    CancelToken,
    CopyPropsMode,
    FileInfo,
    OpOutcome,
    OpResult,
    ProgressCallback,
    ResultCallback,
    TransferOptions,
    TransferProgress,
    TransferType,
    strip_response_metadata,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from boto3.s3.transfer import TransferConfig
    from mypy_boto3_s3 import S3Client

    from boto3_s3.storage import Storage

logger = logging.getLogger(__name__)

# Object properties carried by MetadataDirective=COPY semantics - what the
# multipart metadata re-injection reads from the source HeadObject (aws-cli
# subscribers.SetMetadataDirectivePropsSubscriber).
_METADATA_DIRECTIVE_PROPS: tuple[str, ...] = (
    "CacheControl",
    "ContentDisposition",
    "ContentEncoding",
    "ContentLanguage",
    "ContentType",
    "Expires",
    "Metadata",
)

# S3 caps the ``Tagging`` request header at ~2 KiB (URL-encoded); larger tag
# sets fall back to a post-copy PutObjectTagging.
_MAX_TAGGING_HEADER_SIZE = 2 * 1024

# S3's limits on one object's annotations (PutObjectAnnotation): at most
# 1,000 of them, each payload at most 1 MiB. A source reply past either is
# outside the API, and the copy stops there instead of storing without bound.
_MAX_ANNOTATIONS = 1_000
_MAX_ANNOTATION_PAYLOAD = 1024 * 1024

# user_context slot handing an oversized TagSet from on_queued to on_done.
_POST_TAGGING_KEY = "boto3_s3_post_copy_tag_set"

# user_context slot handing mv's source delete response from _DeleteSource
# to _Completion for capture_response (extra_info["delete"]); an S3 source's
# DeleteObject response populates it, a local delete returns None and leaves
# it unset, and a custom backend's own Mapping response is captured the same
# way as S3's.
_DELETE_RESPONSE_KEY = "boto3_s3_delete_response"

_DEFAULT_MULTIPART_THRESHOLD = 8 * 1024 * 1024


@dataclass(slots=True, kw_only=True)
class TransferItem:
    """One unit of work handed to ``Transferrer.submit``.

    ``compare_key`` is the item's operation-relative name (``transferplan.item_paths``)
    - the identity under which results and progress are reported. The
    per-route fields are populated by the orchestrator: ``src_path`` /
    ``dest_path`` are local paths with native separators, in the local
    storage's text form (``LocalStorage.filename_encoding``; the engine hands
    the OS its own form, `_os_form`); bucket/key pairs are S3 sides.
    ``head`` carries the single-source HeadObject payload so a single-file
    copy never heads the source twice; ``mtime`` is the source LastModified a
    download stamps onto the local file; ``src_display`` / ``dest_display``
    are the rendered endpoints reported on ``OpResult``.

    ``size`` is the transfer byte count, provided to s3transfer to skip its
    size probe; a copy also reads it for the copy-props multipart decision, so
    a copy item must carry it - an under-threshold value routes metadata/tags
    down the single-part path. ``etag`` is the source object's ETag exactly as
    the listing or the HEAD carried it (``S3FileInfo.etag``, quotes and all),
    and the engine provides it to s3transfer untouched - the text aws-cli
    sends as ``If-Match``. ``size_hint`` is a stream
    upload's ``expected_size``: a sizing hint for a source the engine cannot
    size itself, never a byte count - `_applied_size_hint` provides it only
    for a non-seekable fileobj, and SUCCEEDED reports the bytes counted
    rather than the hint.
    """

    compare_key: str
    size: int | None = None
    size_hint: int | None = None
    src_bucket: str | None = None
    src_key: str | None = None
    src_path: str | None = None
    # The source listing entry, set by every listing-driven route (upload,
    # download, copy, and their open-route variants); ``mv`` deletes the source
    # through it (``src_info.storage.delete(src_info)``, addressed by its own
    # ``key``, distinct from ``src_path``, the path s3transfer reads). None only
    # on the stream routes, which list nothing.
    src_info: FileInfo | None = None
    # The destination listing entry, set only by ``sync`` (the pair's ``dest``):
    # the pre-existing object an update overwrites, or None for a new key. cp /
    # mv never list the destination, so it stays None there.
    dest_info: FileInfo | None = None
    dest_bucket: str | None = None
    dest_key: str | None = None
    dest_path: str | None = None
    etag: str | None = None
    mtime: datetime | None = None
    head: Mapping[str, Any] | None = None
    src_display: str = ""
    dest_display: str = ""
    # Streaming sides (cp with stdin/stdout, or any caller-supplied binary
    # stream): a readable object replaces src_path on uploads, a writable one
    # replaces dest_path on downloads. Streams get no directory creation and
    # no mtime stamp, and a download without size+etag lets s3transfer probe
    # the object itself - a HeadObject under the default
    # response_checksum_validation ("when_supported"), the aws-cli stream wire
    # shape; a "when_required" client takes s3transfer's first-chunk GET
    # probe instead.
    src_fileobj: Any = None
    dest_fileobj: Any = None
    # A download admitted by the --case-conflict gate carries the callback that
    # drops its lower-cased key from the gate's in-flight set (aws-cli's
    # CaseConflictCleanupSubscriber wiring); _submit_download fires it on the
    # transfer's terminal. None unless the gate admitted this item.
    case_conflict_cleanup: Callable[[], None] | None = None


# The writes ``no_overwrite`` makes conditional (``IfNoneMatch="*"``): a
# single-part upload or copy, or a multipart one's completing call
# (`_allow_if_none_match`).
_CONDITIONAL_WRITE_OPERATIONS = frozenset({"PutObject", "CopyObject", "CompleteMultipartUpload"})


def _is_conditional_write_rejection(exc: BaseException) -> bool:
    """A botocore ClientError carrying S3's rejection of a conditional write.

    A ``PreconditionFailed`` answer to one of the writes ``no_overwrite``
    makes conditional. Every other request a transfer sends may meet a 412
    too - s3transfer's ranged GETs carry ``IfMatch`` and its part copies
    ``CopySourceIfMatch`` (the object changed since it was listed), and the
    post-copy tag write can be answered that way - and each of those is a
    failure. The operation comes from the error itself: botocore names it on
    the classic engine, and pip s3transfer's CRT translation carries
    awscrt's ``operation_name`` (an error naming none is not taken for the
    conditional write).
    """
    response: Any = getattr(exc, "response", None)
    try:
        code = response["Error"]["Code"]
    except (TypeError, KeyError):
        return False
    return (
        code == "PreconditionFailed"
        and getattr(exc, "operation_name", None) in _CONDITIONAL_WRITE_OPERATIONS
    )


def _applied_size_hint(item: TransferItem) -> int | None:
    """The ``size_hint`` the engine is given, or None where the hint is ignored.

    A hint sizes only a stream the engine cannot size itself: s3transfer's
    non-seekable upload plans its parts from a provided size and reads to EOF
    regardless - aws-cli's ``--expected-size``, which applies to its
    non-seekable stdin alone. A seekable stream (a file opened ``"rb"``) is
    sized by the engine, and a provided size would become the byte count it
    reads: an understated hint truncated the object silently, SUCCEEDED
    (measured against MinIO).
    """
    if item.size is not None or item.size_hint is None or item.src_fileobj is None:
        return None
    try:
        engine_sizes_it = seekable(item.src_fileobj)
    except Exception:
        # A stream that cannot even answer - a closed file raises here. Not
        # this question's failure to report: the hint goes through, and the
        # engine's own first touch of the stream fails the item on its task,
        # a FAILED record like every other unusable source.
        engine_sizes_it = False
    return None if engine_sizes_it else item.size_hint


def _outcome(future: Any) -> BaseException | None:
    """A settled future's exception, or None when the transfer succeeded.

    What every ``on_done`` subscriber asks first. It catches ``BaseException``
    on purpose: an interrupt can be the stored outcome - upstream's submission
    task stores whatever stopped it, a ``KeyboardInterrupt`` under
    ``use_threads=False`` included, and the subscribers here store one that
    lands in their own work (`_CloseFileobj`, `_SetTags`, `_SetAnnotations`,
    `_DeleteSource`) - and a subscriber that let it escape would skip its
    failure branch and leave the item without a record.
    `Transferrer._record_failure` turns that outcome into CANCELLED and hands
    the interrupt back to the caller.
    """
    try:
        future.result()
    except BaseException as exc:
        return exc
    return None


def _future_revoked(future: Any) -> bool | None:
    """Whether classic s3transfer settled ``future`` through ``cancel()``.

    The classic coordinator's ``status`` is ``"cancelled"`` for an outcome its
    ``cancel()`` installed - the manager's fatal / Ctrl-C shutdown, the token's
    ``future.cancel()``, `_SerialExecutor`'s interrupt - and ``"failed"`` for
    an exception a task raised, which may wear the cancellation type too: a
    ``CancelledError`` a caller's stream read threw is a failure nobody
    ordered. None where the engine keeps no such status (the CRT
    coordinator); `_is_cancellation` then asks whether this run's cancel
    token ordered a cancel.
    """
    status = getattr(getattr(future, "_coordinator", None), "status", None)
    if not isinstance(status, str):
        return None
    return status == "cancelled"


def _is_cancellation(
    exc: BaseException, *, cancel_initiated: bool, revoked: bool | None = None
) -> bool:
    """A future outcome meaning "revoked", not "failed".

    Classic s3transfer settles cancelled futures with its own
    ``CancelledError`` (``FatalError`` on aws-cli's fatal path is a subclass):
    revoked when the coordinator says so (``revoked``, `_future_revoked`),
    a failure when a task raised that type itself - a caller's stream throwing
    it, nobody having ordered a cancel. The library's own `CancelledError` is
    judged the same way: nothing in the library puts one on a future, so one
    found there came from the caller's code too. Where the engine keeps no
    such status (``revoked`` is None: the CRT coordinator, which never
    produces either type itself, and a submit-time failure) the type cannot
    tell an ordered cancel from a thrown one, and only this run having
    ordered one (``cancel_initiated``) makes it a revocation.
    The CRT data plane surfaces awscrt's ``AWS_ERROR_S3_CANCELED`` instead,
    matched by the error's ``name`` without importing awscrt, and a
    revocation under the same condition: the run ordered the cancel - its
    cancel token, a fatal or a Ctrl-C folding the manager, or a drain-time
    Ctrl-C pip s3transfer's CRT manager swallowed (`Transferrer`'s
    ``_cancel_initiated``) - so an item revoked is ``CANCELLED`` on both
    engines. aws-cli prints a failed line for each such CRT item instead
    (measured: its ``DoneResultSubscriber`` treats every
    non-``CancelledError`` as a per-item failure), which is not copied. A
    CRT cancellation the run did not order stays a failure.
    """
    if isinstance(exc, (CancelledError, S3TransferCancelledError)):
        if revoked is None:
            return cancel_initiated
        return revoked
    return cancel_initiated and getattr(exc, "name", None) == "AWS_ERROR_S3_CANCELED"


def _allow_if_none_match() -> None:
    """Teach pip s3transfer the ``IfNoneMatch`` upload/copy argument.

    aws-cli's bundled s3transfer accepts ``IfNoneMatch`` (--no-overwrite) on
    uploads and copies - applied to PutObject / CopyObject and, on the
    multipart paths, to CompleteMultipartUpload only (never the create/part
    calls). pip s3transfer has not shipped that yet, so the same entries are
    appended to its argument tables here, idempotently: a future release that
    ships them natively leaves the lists untouched.

    The upload create-multipart blocklist is version-gated (see the guard
    below); every other table this touches exists across the whole supported
    s3transfer range.
    """
    for allowed in (TransferManager.ALLOWED_UPLOAD_ARGS, TransferManager.ALLOWED_COPY_ARGS):
        if "IfNoneMatch" not in allowed:
            allowed.append("IfNoneMatch")
    upload_cls = UploadSubmissionTask
    # Back-compat (supported floor s3transfer 0.6.2): UploadSubmissionTask grew
    # CREATE_MULTIPART_BLOCKLIST only in s3transfer 0.11.0. On older s3transfer
    # the create-multipart call is handed the full extra_args (no per-arg
    # blocklist to extend), so skip it when the attribute is absent - and
    # `conditional_write_unsupported_reason` refuses --no-overwrite uploads on
    # such installs up front (botocore can model IfNoneMatch there: boto3
    # 1.35.2+ pins s3transfer 0.10.x). Drop this getattr guard once the
    # s3transfer floor is raised to >= 0.11.
    create_blocklist = getattr(upload_cls, "CREATE_MULTIPART_BLOCKLIST", None)
    if create_blocklist is not None and "IfNoneMatch" not in create_blocklist:
        create_blocklist.append("IfNoneMatch")
    if "IfNoneMatch" not in upload_cls.COMPLETE_MULTIPART_ARGS:
        upload_cls.COMPLETE_MULTIPART_ARGS.append("IfNoneMatch")
    copy_cls = CopySubmissionTask
    if "IfNoneMatch" not in copy_cls.CREATE_MULTIPART_ARGS_BLACKLIST:
        copy_cls.CREATE_MULTIPART_ARGS_BLACKLIST.append("IfNoneMatch")
    if "IfNoneMatch" not in copy_cls.COMPLETE_MULTIPART_ARGS:
        copy_cls.COMPLETE_MULTIPART_ARGS.append("IfNoneMatch")


def _input_member_modeled(client: S3Client, operation: str, member: str) -> bool:
    """Whether the installed botocore's S3 model has ``member`` in ``operation``'s input.

    The member-presence probe the SDK-floor checks below share: the model is
    introspected directly (version-agnostic), so a refusal's message names a
    minimum version only as a hint.
    """
    input_shape = client.meta.service_model.operation_model(operation).input_shape
    return member in getattr(input_shape, "members", {})


# Back-compat (supported floor botocore 1.31, docs/compatibility.md):
# IfNoneMatch reached the S3 write ops only in later botocore - PutObject and
# CompleteMultipartUpload in 1.35.2, CopyObject in 1.40.61 (each the first
# release whose S3 model carries the member, read from the published wheels).
# Below those, --no-overwrite is rejected up front, before any request,
# instead of failing deep in botocore with an opaque
# "Unknown parameter in input: IfNoneMatch". Drop this gate once the botocore
# floor reaches them.
_CONDITIONAL_WRITE_MIN_BOTOCORE = {"upload": "1.35.2", "copy": "1.40.61"}

# Uploads additionally need s3transfer's create-multipart blocklist
# (`UploadSubmissionTask.CREATE_MULTIPART_BLOCKLIST`, s3transfer 0.11.0): older
# s3transfer hands the *full* extra_args to CreateMultipartUpload, whose model
# has no IfNoneMatch -> ParamValidationError on any multipart-threshold upload.
# The combination is real, not theoretical: boto3 1.35.2+ pins s3transfer
# 0.10.x while its botocore already models IfNoneMatch, so the botocore gate
# above passes there. Rejected up front (uniformly, small files included -
# sizes are unknown at validation time and a size-dependent refusal would be
# unpredictable). Drop once the s3transfer floor reaches 0.11.
_CONDITIONAL_WRITE_MIN_S3TRANSFER = "0.11.0"


def conditional_write_unsupported_reason(client: S3Client, *, is_copy: bool) -> str | None:
    """Why the installed SDK cannot honor ``--no-overwrite`` here, or ``None``.

    ``--no-overwrite`` maps to a conditional write (``IfNoneMatch="*"``), applied
    to PutObject (and CompleteMultipartUpload) on uploads and CopyObject on
    copies. The client's S3 model must define that input member, and an upload
    additionally needs s3transfer's create-multipart blocklist so the param
    stays off CreateMultipartUpload (see `_CONDITIONAL_WRITE_MIN_S3TRANSFER`).
    Both are introspected directly (version-agnostic), while the returned
    message names the minimum version as a hint. Returns ``None`` when
    supported.
    """
    op = "CopyObject" if is_copy else "PutObject"
    if not _input_member_modeled(client, op, "IfNoneMatch"):
        minimum = _CONDITIONAL_WRITE_MIN_BOTOCORE["copy" if is_copy else "upload"]
        return (
            f"--no-overwrite requires botocore >= {minimum} for {op} "
            f"(S3 conditional writes); the installed botocore is {version('botocore')}."
        )
    if not is_copy:
        if getattr(UploadSubmissionTask, "CREATE_MULTIPART_BLOCKLIST", None) is None:
            return (
                f"--no-overwrite requires s3transfer >= {_CONDITIONAL_WRITE_MIN_S3TRANSFER} "
                "for uploads (keeping IfNoneMatch off the CreateMultipartUpload call); "
                f"the installed s3transfer is {version('s3transfer')}."
            )
    return None


# Feature-level degradation (docs/compatibility.md): S3 object
# annotations reached botocore's S3 model in 1.43.31, introspected by member
# presence (version-agnostic); the version below only names the hint in the
# refusal message for `copy_props=ALL`.
_ANNOTATIONS_MIN_BOTOCORE = "1.43.31"


def _copy_annotations_param_supported(client: S3Client) -> bool:
    """Whether the installed botocore's CopyObject can carry `AnnotationDirective`."""
    return _input_member_modeled(client, "CopyObject", "AnnotationDirective")


def _annotation_directive_blacklisted() -> bool:
    """Whether s3transfer knows `AnnotationDirective` on multipart copies.

    Upstream s3transfer >= 0.19 blacklists the directive from the
    CreateMultipartUpload call and runs its own annotation carryover when it
    is COPY; an older s3transfer would forward the directive to
    CreateMultipartUpload (which has no such member) and fail.
    """
    return "AnnotationDirective" in CopySubmissionTask.CREATE_MULTIPART_ARGS_BLACKLIST


def annotations_copy_unsupported_reason(client: S3Client) -> str | None:
    """Why the installed SDK cannot honor ``copy_props=ALL``, or ``None``.

    `CopyPropsMode.ALL` copies S3 object annotations, which the botocore S3
    model must know - introspected directly (version-agnostic); the message
    names the minimum version as a hint. The multipart carryover is this
    module's own (`_SetAnnotations`, aws-cli's subscriber), so the installed
    s3transfer does not matter. Returns ``None`` when the mode is supported.
    """
    if not _copy_annotations_param_supported(client):
        return (
            f"copy_props=ALL requires botocore >= {_ANNOTATIONS_MIN_BOTOCORE} "
            f"(S3 object annotations); the installed botocore is {version('botocore')}."
        )
    return None


# Back-compat (supported floor botocore 1.31, docs/compatibility.md): the S3
# model gained each newer checksum algorithm in one release - CRC64NVME in
# 1.36.0, SHA512 and the XXHASH family in 1.42.94 - with every member that
# carries it (the request's Checksum<ALG>, UploadPart's and UploadPartCopy's
# results, CompletedPart), each read from the published wheels. Below it a run
# with that algorithm cannot work whole: an upload fails on botocore's
# "Unsupported checksum algorithm" (a multipart one at its first part) - except
# on 1.42.53 - 1.42.93, where botocore computes SHA512 / XXHASH before the model
# lists them and a multipart upload completes without its parts' checksums,
# s3transfer reading them off part results the model does not parse; a
# multipart copy completes without them at every such version. Only a
# single-part copy gets by (S3 computes it), and sizes are unknown up front, so
# uploads and copies are refused alike, as --no-overwrite is. An algorithm the
# model knows but botocore computes only through awscrt is not refused here:
# without the crt extra it fails per item with botocore's Missing Dependency
# report (docs/compatibility.md section 2). Drop an entry once the floor
# reaches it.
_CHECKSUM_MIN_BOTOCORE = {
    "crc64nvme": "1.36.0",
    "sha512": "1.42.94",
    "xxhash64": "1.42.94",
    "xxhash3": "1.42.94",
    "xxhash128": "1.42.94",
}


def _checksum_algorithm_unsupported_reason(client: S3Client, algorithm: object) -> str | None:
    """Why the installed SDK cannot run a transfer with ``algorithm``, or ``None``.

    Membership of the algorithm's ``Checksum<ALG>`` member is introspected on
    the client's S3 model (``PutObject``'s input - the release that adds it adds
    the part members a multipart transfer needs too), version-agnostic; the
    message names the minimum version as a hint. Only the algorithms newer than
    the floor are probed: CRC32 / CRC32C / SHA1 / SHA256 are in every supported
    model, and a name botocore never computes (MD5) or does not know at all
    keeps botocore's own per-item report.
    """
    if not isinstance(algorithm, str):
        return None
    minimum = _CHECKSUM_MIN_BOTOCORE.get(algorithm.lower())
    if minimum is None:
        return None
    if _input_member_modeled(client, "PutObject", f"Checksum{algorithm.upper()}"):
        return None
    return (
        f"checksum_algorithm={algorithm.upper()} requires botocore >= {minimum}; "
        f"the installed botocore is {version('botocore')}."
    )


# A CRT failure of the item's local file, by awscrt's error name: the local
# taxonomy's categories (`Transferrer._local_failure`).
_CRT_LOCAL_CATEGORIES: dict[str, type[Boto3S3Error]] = {
    "AWS_ERROR_NO_PERMISSION": AccessDeniedError,
    "AWS_ERROR_FILE_INVALID_PATH": NotFoundError,
    "AWS_ERROR_FILE_OPEN_FAILURE": TransportError,
    "AWS_ERROR_FILE_READ_FAILURE": TransportError,
    "AWS_ERROR_FILE_WRITE_FAILURE": TransportError,
    "AWS_ERROR_NO_SPACE": TransportError,
}


def _names_item_file(exc: OSError, path: str) -> bool:
    """Whether ``exc`` is about the item's local file at ``path``.

    True for an error naming the file or the temporary file a download
    writes beside it (``<path>.<suffix>``, renamed into place), on either
    side of a rename, and for one naming no file at all (a full disk on a
    write); False for one naming another file.
    """
    names: list[str | bytes] = [
        name for name in (exc.filename, exc.filename2) if isinstance(name, (str, bytes))
    ]
    if not names:
        return True
    own = os.path.normcase(os.path.abspath(path))
    for name in names:
        other = os.path.normcase(os.path.abspath(os.fsdecode(name)))
        if other == own or other.startswith(own + "."):
            return True
    return False


def _same_path(path: str) -> str:
    return path


def _os_form(storage: Storage | None, path: str) -> str:
    """A local side's text path as the OS calls take it (`LocalStorage.os_path`).

    Items carry their local paths as the storage's text - what the results
    report - and every OS call, s3transfer's and the CRT's included, gets
    this form. A name the filename encoding cannot represent raises the
    ``UnicodeEncodeError`` for the caller to record as the item's failure.
    """
    return storage.os_path(path) if isinstance(storage, LocalStorage) else path


def _text_form(storage: Storage | None) -> Callable[[str], str]:
    """The reverse of `_os_form`: a local side's OS-form names as its text."""
    return storage.text_path if isinstance(storage, LocalStorage) else _same_path


def _set_file_utime(path: str, timestamp: float) -> None:
    """Set a file's atime/mtime (aws-cli's ``set_file_utime``).

    A permission failure (EPERM - typically a file owned by another user) is
    re-worded the way aws-cli words it, so the resulting warning carries
    aws-cli's "attempting to modify the utime" text; every other error keeps
    its own message.
    """
    try:
        os.utime(path, (timestamp, timestamp))
    except OSError as exc:
        if exc.errno != errno.EPERM:
            raise
        raise AccessDeniedError(
            "The file was downloaded, but attempting to modify the utime of the file "
            "failed. Is the file owned by another user?"
        ) from exc


_mime_db: mimetypes.MimeTypes | None = None


def _mime_types() -> mimetypes.MimeTypes:
    """The MIME datastore an upload's guessed ``Content-Type`` comes from.

    Assembled the way ``mimetypes.init`` assembles the module-level one, with
    one substitution: the built-in tables come from ``mimetable`` (frozen
    CPython 3.14) rather than from the running interpreter, because the guess
    has to be the one aws-cli makes and aws-cli's official distribution is frozen
    against 3.14. The host-side overlays stay live and keep ``init``'s order -
    the Windows registry first, then whichever ``knownfiles`` exist - so local
    mime.types edits reach both tools alike. Built on first use, and only then:
    an upload that sets ``content_type`` never needs it.
    """
    global _mime_db
    if _mime_db is None:
        db = mimetypes.MimeTypes()
        db.encodings_map = dict(mimetable.ENCODINGS_MAP)
        db.suffix_map = dict(mimetable.SUFFIX_MAP)
        db.types_map = ({}, {})
        db.types_map_inv = ({}, {})
        for extension, mime_type in mimetable.TYPES_MAP.items():
            db.add_type(mime_type, extension, strict=True)
        for extension, mime_type in mimetable.COMMON_TYPES.items():
            db.add_type(mime_type, extension, strict=False)
        db.read_windows_registry()
        for name in mimetable.KNOWNFILES:
            if os.path.isfile(name):
                db.read(name)
        _mime_db = db
    return _mime_db


def _guess_content_type(path: str) -> str | None:
    """The ``Content-Type`` aws-cli guesses for *path*: CPython 3.14's ``guess_type``.

    The tables are frozen (`_mime_types`) and so is the algorithm, a port of
    3.14's ``MimeTypes.guess_file_type``: the name is a file path, split with
    the host's ``os.path`` after its drive is removed. aws-cli guesses on an
    absolute local path, which never takes ``guess_type``'s URL branch (a
    scheme-like prefix of two or more characters), so only the file branch is
    ported, and an open-route name (a custom source's ``compare_key``) is read
    as a file name too. The interpreter aws-cli bundles runs exactly that,
    while a host interpreter before 3.13 split every name with
    ``posixpath``, so on Windows ``C:\\d\\.json`` kept ``.json`` as its
    extension and went up as ``application/json`` where aws-cli sends the name
    untyped - it is all extension to ``ntpath`` (measured 2026-10-06 against
    aws.exe: 8 of 12 names differed under Python 3.10). Strict (IANA-only)
    matching, like aws.

    The guess can raise ``UnicodeDecodeError`` on Windows when a registry MIME
    entry is in an undecodable encoding (bpo-9291) - here from the registry read
    the first call performs, where aws-cli takes it from its own lazy
    ``mimetypes.init``. Any other failure to load the host overlays - an
    ``OSError`` from a ``knownfiles`` entry that exists but cannot be read -
    propagates, and the submit records it as the item's failure, where
    aws-cli's queued subscriber lets it land; the store stays unbuilt, so every
    later item retries and fails the same way, as aws-cli's does.
    """
    try:
        return _guess_file_type(_mime_types(), os.path.splitdrive(path)[1])
    except UnicodeDecodeError:
        return None


def _guess_file_type(db: mimetypes.MimeTypes, path: str) -> str | None:
    """CPython 3.14's ``MimeTypes._guess_file_type``, strict, the type alone.

    Multi-suffix aliases (``.tgz`` -> ``.tar.gz``) are looked up without
    regard to case, an encoding suffix (``.gz``) is peeled case-sensitively,
    and the type is looked up by the lowercased extension that remains.
    """
    base, ext = os.path.splitext(path)
    while (ext_lower := ext.lower()) in db.suffix_map:
        base, ext = os.path.splitext(base + db.suffix_map[ext_lower])
    if ext in db.encodings_map:
        base, ext = os.path.splitext(base)
    return db.types_map[True].get(ext.lower())


def _crt_config_error_type() -> type[Exception] | None:
    """boto3's ``InvalidCrtTransferConfigError``, or None on a boto3 without it.

    The name arrived in boto3 1.42.0 with the CRT-aware ``TransferConfig``
    (``UNSET_DEFAULT``), while the CRT engine is usable from boto3 1.34.6 -
    the first release whose ``crt`` extra brings an awscrt at boto3's own
    minimum (docs/compatibility.md) - so the CRT lane runs on eight minor
    releases that cannot import it, where an unconditional import failed
    every CRT transfer with ``ImportError``. Only a boto3 that validates the
    config can raise it, so its absence means there is nothing to catch.
    """
    try:
        from boto3.exceptions import InvalidCrtTransferConfigError
    except ImportError:
        return None
    return InvalidCrtTransferConfigError


@contextmanager
def crt_engine_errors(operation: str | None) -> Generator[None, None, None]:
    """Classify what building the CRT transfer engine raises.

    ``crtsupport.create_crt_transfer_manager`` runs third-party construction
    code, and each failure keeps the category design/exceptions.md gives its
    cause: boto3's explicit-'crt' config validation
    (``InvalidCrtTransferConfigError``: classic-only tuning set, a caller
    argument) is a ``ValidationError``; a CA bundle the CRT cannot read
    (``FileNotFoundError`` / ``IsADirectoryError`` / ``PermissionError`` from
    awscrt reading the path) or cannot parse (awscrt's own ``RuntimeError``,
    ``AWS_ERROR_INVALID_ARGUMENT``) is a setting present but unusable, an
    ``InvalidConfigError``; the botocore family - a refreshable credential the
    compatibility check had to resolve for a client holding a credentials
    object other than the singleton's, the serializer's client build - goes
    through `s3_errors`, and what that build raises outside the family
    (``ValueError`` / ``AttributeError`` reading a malformed configuration)
    through `client_build_error`, as for any client build.
    boto3's ``MissingDependencyException`` never reaches this seam - the
    engine selection raises it ahead, and keeps its documented pass-through.
    """
    with s3_errors(operation=operation):
        try:
            yield
        except (ClientError, BotoCoreError):
            # The boto family ahead of the OSError clause: four of botocore's
            # transport errors (SSLError, the connect / read timeouts,
            # ProxyConnectionError) derive from its vendored requests'
            # IOError, and they are an unreachable STS - a TransportError
            # from `s3_errors` - not a setting that is wrong.
            raise
        except RecursionError as exc:
            # A redirect loop inside a credential resolution: the request
            # failing, as everywhere else (`request_failure`'s last resort).
            raise translate_boto_error(exc, operation=operation) from exc
        except (OSError, RuntimeError) as exc:
            raise InvalidConfigError(str(exc), operation=operation) from exc
        except (ValueError, AttributeError) as exc:
            # What the serializer's client build raises outside botocore's
            # family while it reads the configuration of the session it is
            # built from - an `s3 = foo` scalar where it reads a section -
            # classified as `S3.client()` classifies the same build.
            error = client_build_error(exc)
            attribute_failure(error, operation=operation, bucket=None, key=None)
            raise error from exc
        except Exception as exc:
            invalid_config = _crt_config_error_type()
            if invalid_config is not None and isinstance(exc, invalid_config):
                raise ValidationError(str(exc), operation=operation) from exc
            raise


def _extract_etag(response: Mapping[str, Any]) -> str | None:
    """An object's ETag from a captured write or read response, normalized.

    ``PutObject`` / ``CompleteMultipartUpload`` / ``GetObject`` carry ``ETag`` at
    the top level; ``CopyObject`` nests it under ``CopyObjectResult``. Collapse
    both so ``extra_info["ETag"]`` reads the same regardless of which API produced
    the response.
    """
    etag: Any = response.get("ETag")
    if etag is None:
        result: Any = response.get("CopyObjectResult")
        if result is not None:
            etag = result.get("ETag")
    return etag


class _ResponseCapture:
    """Capture a transfer's terminal write / read response via botocore events.

    ``capture_response`` opt-in: s3transfer discards the ``PutObject`` /
    ``CopyObject`` / ``CompleteMultipartUpload`` (write) and ``GetObject`` (read)
    responses, so the only way to surface them is to observe the client's event
    stream. A ``before-parameter-build`` handler stashes the call's
    ``(Bucket, Key)`` in the per-request botocore ``context``; the paired
    ``after-call`` handler reads the parsed response back out (dropping the
    streaming ``Body`` and ``ResponseMetadata``) and files it under that key -
    write and read operations into **separate stores**, so a same-run
    ``GetObject`` on a key about to be written (e.g. a sync content filter
    reading the destination through ``S3Storage.open(..., "rb")`` on this same
    client) can never satisfy a write pop: ``extra_info["write"]`` is always
    the transfer's own write response. ``_build_extra_info`` drains per item.
    A multipart download issues many ranged ``GetObject`` calls, so the first
    stored read wins and the range-specific fields are dropped, leaving the
    object-level metadata. That same first-stored-wins rule would let a
    same-run content filter's source read beat the transfer's own ``GetObject``
    to the read slot, so ``Transferrer._admit_capture`` admits an item's key
    only as the item is submitted (``expect``): a read on a key not yet
    admitted is simply never stored, so the filter's earlier ``GetObject``
    cannot occupy the slot.

    One instance per operation, registered on the transfer client when the
    manager is built - before enumeration starts for a listing-driven run
    (``Transferrer.prepare``), at the first submit for a stream run - and
    unregistered after the manager shuts down (so no request is emitting on the
    client during a registration change). The handlers are this
    instance's bound methods, held once so unregister removes exactly what register
    added - never colliding with another operation's capture or with a handler the
    application put on a shared client. The events engine has no lock, so a capture
    operation needs exclusive use of its client for the operation's span
    (design/s3.md).

    ``_WRITE_OPS`` are the terminal object-writing operations observed - the
    intermediate multipart calls (CreateMultipartUpload / UploadPart /
    UploadPartCopy) are deliberately not, only the response describing the finished
    object is wanted. ``_READ_OPS`` is the object-reading operation (a download);
    HeadObject is not observed.
    """

    _WRITE_OPS = ("PutObject", "CopyObject", "CompleteMultipartUpload")
    _READ_OPS = ("GetObject",)

    def __init__(self) -> None:
        self._writes: dict[tuple[str, str], dict[str, Any]] = {}
        self._reads: dict[tuple[str, str], dict[str, Any]] = {}
        # Only keys admitted by expect() (submitted items) are recorded: a
        # same-client GetObject issued outside the engine - a content filter
        # reading a pair the run then rejects - must not grow the read store
        # for the rest of the run, since no item would ever drain it.
        self._expected: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        # Bind once so unregister removes the same objects register added.
        self._stash_handler = self._stash_key
        self._record_write_handler = self._record_write
        self._record_read_handler = self._record_read

    def _stash_key(self, params: Mapping[str, Any], context: dict[str, Any], **kwargs: Any) -> None:
        bucket = params.get("Bucket")
        key = params.get("Key")
        if bucket is not None and key is not None:
            context["boto3_s3_capture_key"] = (bucket, key)

    def _record_write(
        self, parsed: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any
    ) -> None:
        self._record(self._writes, parsed, context)

    def _record_read(
        self, parsed: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any
    ) -> None:
        self._record(self._reads, parsed, context)

    def _record(
        self,
        store: dict[tuple[str, str], dict[str, Any]],
        parsed: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> None:
        bucket_key = context.get("boto3_s3_capture_key")
        if bucket_key is None:
            return
        # Drop the streaming Body (a download response carries the object bytes)
        # along with the transport internals.
        response = strip_response_metadata(parsed, drop_body=True)
        if "ContentRange" in response:
            # A ranged (partial) GetObject from a multipart download: the
            # object-level fields (ETag / VersionId / Metadata / ...) describe the
            # object, but ContentRange / ContentLength describe one range - drop
            # them so the slot reads like a whole-object response.
            response.pop("ContentRange", None)
            response.pop("ContentLength", None)
        with self._lock:
            if bucket_key not in self._expected:
                return
            # First stored wins: a multipart download issues many ranged GETs for
            # the same key, all carrying the same object-level metadata (an item
            # writes its key once, so the write store sees one terminal response).
            store.setdefault(bucket_key, response)

    def register(self, client: Any) -> None:
        events = client.meta.events
        for op in (*self._WRITE_OPS, *self._READ_OPS):
            events.register(f"before-parameter-build.s3.{op}", self._stash_handler)
        for op in self._WRITE_OPS:
            events.register(f"after-call.s3.{op}", self._record_write_handler)
        for op in self._READ_OPS:
            events.register(f"after-call.s3.{op}", self._record_read_handler)

    def unregister(self, client: Any) -> None:
        events = client.meta.events
        for op in (*self._WRITE_OPS, *self._READ_OPS):
            events.unregister(f"before-parameter-build.s3.{op}", self._stash_handler)
        for op in self._WRITE_OPS:
            events.unregister(f"after-call.s3.{op}", self._record_write_handler)
        for op in self._READ_OPS:
            events.unregister(f"after-call.s3.{op}", self._record_read_handler)

    def expect(self, bucket: str | None, key: str | None) -> None:
        """Admit a submitted item's key into the stores (see ``_expected``)."""
        if bucket is not None and key is not None:
            with self._lock:
                self._expected.add((bucket, key))

    def pop_write(self, bucket: str | None, key: str | None) -> dict[str, Any] | None:
        return self._pop(self._writes, bucket, key)

    def pop_read(self, bucket: str | None, key: str | None) -> dict[str, Any] | None:
        return self._pop(self._reads, bucket, key)

    def _pop(
        self, store: dict[tuple[str, str], dict[str, Any]], bucket: str | None, key: str | None
    ) -> dict[str, Any] | None:
        if bucket is None or key is None:
            return None
        with self._lock:
            self._expected.discard((bucket, key))
            return store.pop((bucket, key), None)


class Warner:
    """The op-level advisory sink: a message body -> a WARNED or NOTICE ``OpResult``.

    A single place that turns an advisory into an ``OpResult`` and reports it,
    so both a backend enumeration (``ScanOptions.on_warning``) and the transfer
    engine funnel their warnings through the same emitter instead of the walk
    reaching into the transfer engine for one. Only WARNED counts: warnings
    count independently of files (``warned > 0`` alone maps to exit code 2,
    aws-cli rc model), and the count is thread-safe because a walk worker and a
    transfer worker can warn at once; a NOTICE is display-only. The transfer
    run's ``Transferrer`` builds one from its result context and exposes it as
    ``Transferrer.warner``.
    """

    def __init__(
        self,
        *,
        transfer_type: TransferType,
        src_storage: Storage | None,
        dest_storage: Storage | None,
        operation: str,
        on_result: ResultCallback | None,
    ) -> None:
        self._transfer_type = transfer_type
        self._src_storage = src_storage
        self._dest_storage = dest_storage
        self._operation = operation
        self._on_result = on_result
        self._warned = 0
        self._lock = threading.Lock()

    @property
    def warned(self) -> int:
        return self._warned

    def warn(self, body: str, *, info: FileInfo | None = None) -> None:
        """Record a warning (aws-cli message body, no ``warning: `` prefix).

        ``info`` is the source entry the warning is about, when there is one
        (a walk's ``on_warning`` names none): the record carries it as
        ``src_info`` and takes its ``compare_key``.
        """
        with self._lock:
            self._warned += 1
        self._report(OpOutcome.WARNED, body, info)

    def notice(self, body: str, *, info: FileInfo | None = None) -> None:
        """Emit display-only advisory text (no counts, no exit-code effect).

        aws-cli prints the case-conflict messages straight to stderr, bypassing
        its result recorder - they look like warnings but never count as
        them. The full text (its ``warning: `` prefix included) rides on
        ``OpResult.error``; ``info`` is as for `warn`.
        """
        self._report(OpOutcome.NOTICE, body, info)

    def _report(self, outcome: OpOutcome, body: str, info: FileInfo | None) -> None:
        if self._on_result is not None:
            self._on_result(
                OpResult(
                    transfer_type=self._transfer_type,
                    compare_key=_advisory_key(info),
                    outcome=outcome,
                    error=Boto3S3Error(body, operation=self._operation),
                    src_info=info,
                    src_storage=self._src_storage,
                    dest_storage=self._dest_storage,
                )
            )


def _advisory_key(info: FileInfo | None) -> str:
    """An advisory record's ``compare_key``: its entry's, or ``""`` without one."""
    if info is None or info.compare_key is None:
        return ""
    return info.compare_key


_Mode = TypeVar("_Mode", bound=enum.Enum)


def option_mode(
    options: TransferOptions,
    name: Literal["copy_props", "annotation_copy_mode", "case_conflict"],
    default: _Mode,
    *,
    operation: str,
) -> _Mode:
    """Read one of the three mode options of ``options`` as its enum member.

    The member and its string value are both accepted. Unspecified is
    ``None``, which reads as ``default``; the check is is-None, not falsy, so
    a present-but-empty value from an untyped caller (``""``) fails the
    conversion instead of silently running with the default. Anything else is
    a caller error reported as ``ValidationError``: the enum's raw
    ``ValueError`` must not leak past the public API (the exception model
    keeps every error in the ``Boto3S3Error`` family, design/exceptions.md).
    The CLI never gets one wrong, having validated each against its choices.
    """
    raw = options.get(name)
    if raw is None:
        return default
    try:
        return type(default)(raw)
    except ValueError as exc:
        raise ValidationError(f"Invalid {name} value: {raw!r}", operation=operation) from exc


class Transferrer:
    """Submit transfer items of one kind and aggregate their outcomes.

    Use as a context manager around the submission loop: ``__exit__`` shuts
    the manager down - waiting for in-flight transfers on a clean exit, and
    **cancelling accepted work on any exception** except a graceful cancel
    (aws-cli's manager context cancels everything still pending when a fatal
    escapes the submission loop - measured: a mid-listing fatal leaves the
    queued transfers unrun and prints only the one fatal line). The exception
    is a `CancelledError` backed by a graceful `CancelToken`: a graceful
    cancel means "drain accepted work", so submitted transfers still run to
    completion; immediate mode cancels them, and a non-`Exception` interrupt
    (Ctrl-C) uses the manager's direct cancellation path. Every accepted item
    still gets its one result record under a cancellation: an item that never
    ran - or was abandoned mid-flight - reports ``CANCELLED`` with a
    `CancelledError` naming the cause, while an in-flight request that
    completes despite the cancel reports its real outcome (s3transfer lets
    the completion win; design/opresult.md). The rollup counters are
    approximate while transfers are in flight and exact after the `with`
    block exits.

    ``client`` is the manager-owning side: the destination client for uploads
    and copies, the source client for downloads; ``source_client`` (copies
    only) serves the CopySource reads - HeadObject, GetObjectTagging, and
    s3transfer's own size probe. ``src_storage`` / ``dest_storage`` are the run's
    two resolved side ``Storage`` objects (``plan.src`` / ``plan.dest``), retained
    so a completion can resolve or report either endpoint alongside its listing
    entries.

    ``is_move`` turns the run into ``mv``: every record reports
    ``TransferType.MOVE`` while ``transfer_type`` keeps routing the bytes (aws-cli
    ``operation_name`` vs ``transfer_type='move'``), and each successful
    transfer deletes its source through the backend its listing entry came
    from, ``item.src_info.storage.delete(item.src_info)`` - a local file or a
    custom backend's entry on an upload, a DeleteObject on the source bucket's
    client otherwise - so every ``mv`` item carries ``src_info`` with its
    ``storage``.
    """

    def __init__(
        self,
        transfer_type: TransferType,
        client: S3Client,
        *,
        source_client: S3Client | None = None,
        src_storage: Storage | None = None,
        dest_storage: Storage | None = None,
        transfer_config: TransferConfig | None = None,
        options: TransferOptions | None = None,
        operation: str = "cp",
        is_move: bool = False,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        capture_response: bool = False,
        crt: crtsupport.CrtPostures | None = None,
    ) -> None:
        if transfer_type not in (TransferType.UPLOAD, TransferType.DOWNLOAD, TransferType.COPY):
            raise ValidationError(
                f"Transferrer does not handle {transfer_type!r}", operation=operation
            )
        self._transfer_type = transfer_type
        self._result_transfer_type = TransferType.MOVE if is_move else transfer_type
        self._is_move = is_move
        self._client = client
        self._source_client = source_client if source_client is not None else client
        # The run's two resolved side Storages (plan.src / plan.dest), retained so
        # a completion can surface them with the listing entries.
        self._src_storage = src_storage
        self._dest_storage = dest_storage
        self._transfer_config = transfer_config
        self._options: TransferOptions = options if options is not None else TransferOptions()
        self._operation = operation
        # The --no-overwrite gate (the CLI reports it on a fatal error: line):
        # only uploads/copies carry IfNoneMatch, and an old botocore lacks it.
        if self._options.get("no_overwrite") and transfer_type in (
            TransferType.UPLOAD,
            TransferType.COPY,
        ):
            reason = conditional_write_unsupported_reason(
                client, is_copy=transfer_type is TransferType.COPY
            )
            if reason is not None:
                # The environment (SDK floor) lacks the capability, not the
                # caller's arguments: a ConfigurationError.
                raise ConfigurationError(reason, operation=operation)
        # Checksum-algorithm gate: only uploads and copies send ChecksumAlgorithm.
        if transfer_type in (TransferType.UPLOAD, TransferType.COPY):
            reason = _checksum_algorithm_unsupported_reason(
                client, self._options.get("checksum_algorithm")
            )
            if reason is not None:
                raise ConfigurationError(reason, operation=operation)
        # The copy-props modes are interpreted once here so a bad value fails
        # at construction rather than at the first submit (`option_mode`).
        self._copy_props = CopyPropsMode.DEFAULT
        self._annotation_copy_mode = AnnotationCopyMode.PRELOAD_MEMORY
        # Whether a copy runs the copy-props subscriber chain: an explicit
        # metadata_directive disables the whole chain (aws-cli's
        # s3handler.CopyRequestSubmitter), so `_submit_copy` attaches none.
        self._runs_copy_props = False
        # `copy_props=ALL`'s multipart annotation writes pin the destination
        # by its CompleteMultipartUpload response, which s3transfer discards:
        # the classic manager of such a run is handed this stand-in for the
        # client, which keeps that one response (`_CopyCompletions`). Only
        # where the chain runs - nothing would otherwise take the responses
        # back out.
        self._completions: _CopyCompletions | None = None
        if transfer_type is TransferType.COPY:
            self._copy_props = option_mode(
                self._options, "copy_props", CopyPropsMode.DEFAULT, operation=operation
            )
            self._annotation_copy_mode = option_mode(
                self._options,
                "annotation_copy_mode",
                AnnotationCopyMode.PRELOAD_MEMORY,
                operation=operation,
            )
            self._runs_copy_props = not self._options.get("metadata_directive")
            if self._runs_copy_props and self._copy_props is CopyPropsMode.ALL:
                # copy_props=ALL gate: annotations need a capable SDK; every
                # other mode degrades silently on an old one
                # (design/transfer.md section 4). Without the chain, ALL never
                # reaches the annotations path - the combination is not
                # refused for a feature that won't run.
                reason = annotations_copy_unsupported_reason(client)
                if reason is not None:
                    raise ConfigurationError(reason, operation=operation)
                self._completions = _CopyCompletions(client)
        self._on_progress = on_progress
        self._on_result = on_result
        self._cancel_token = cancel_token
        self._capture_response = capture_response
        # The caller's CRT engine postures (`S3`'s, documented on
        # crtsupport.create_crt_transfer_manager); the default is boto3's rules.
        self._crt = crt if crt is not None else crtsupport.CrtPostures()
        self._manager: Any = None
        self._capture: _ResponseCapture | None = None
        # Whether the classic engine's client is watched for a body read that
        # fails while awscrt signs it (`signingreads`), so __exit__ removes it.
        self._signing_reads_kept = False
        self._lock = threading.Lock()
        self._futures_lock = threading.Lock()
        self._futures: set[Any] = set()
        self._shutdown_done = threading.Event()
        # The immediate-cancel watcher, alive from the manager's construction
        # (`_get_manager`) until the shutdown is done (`__exit__`).
        self._watcher: threading.Thread | None = None
        # True once this run ordered accepted transfers cancelled - set by
        # `__exit__` folding the manager on any exception (the token's own
        # `CancelledError`, a fatal, a Ctrl-C), by `_cancel_futures` when the
        # drain-time watcher escalates, and by `_keep_swallowed_interrupt` when
        # pip s3transfer's CRT drain swallows a Ctrl-C. Read by the done
        # callbacks to tell a revocation (CANCELLED) from a CRT cancellation
        # nobody ordered (FAILED; `_is_cancellation`). Monotonic
        # False->True, always set before the cancel it describes is issued,
        # so the callback that observes a cancel outcome observes the flag
        # too.
        self._cancel_initiated = False
        # The Ctrl-C pip s3transfer's CRT drain swallowed, kept by
        # `_keep_swallowed_interrupt` (once) for `__exit__` to re-raise.
        self._swallowed_interrupt: KeyboardInterrupt | None = None
        # The first exception the run must hand back to the caller from a
        # place that cannot raise it there, kept by `_note_raise` for `submit`
        # / `__exit__` to re-raise: an interrupt upstream's serial executor
        # swallows inside the manager call, one the caller's own code raised
        # on a worker (`_TaskGuard`, `_GuardedReader`), and anything the
        # caller's ``on_result`` raised wherever a record settled
        # (`_deliver`).
        self._pending_raise: BaseException | None = None
        self._pending_lock = threading.Lock()
        # The thread the caller drives the run from: a record settled there
        # can raise the callback's exception on the spot (`_deliver`).
        self._caller_thread = threading.get_ident()
        self._succeeded = 0
        self._failed = 0
        self._skipped = 0
        self._cancelled = 0
        self._first_error: Boto3S3Error | None = None
        # The shared warning sink: the walk (ScanOptions.on_warning) and this
        # engine both report through it, so a warning never routes into the
        # engine itself. It owns the warned count (rolled into the rc).
        self._warner = Warner(
            transfer_type=self._result_transfer_type,
            src_storage=src_storage,
            dest_storage=dest_storage,
            operation=operation,
            on_result=self._deliver if on_result is not None else None,
        )

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> Transferrer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        """Drain submitted work, cancelling it when the exit requires that.

        Any exception cancels the accepted transfers, like aws-cli's manager
        context (its ``__exit__`` cancels with ``FatalError(str(exc))`` on a
        fatal and ``CancelledError`` on Ctrl-C; measured live: a fatal
        mid-listing leaves every queued transfer unrun). The one drain-on-
        exception case is a graceful token's `CancelledError`: graceful
        cancellation is *defined* as draining accepted work (exceptions.md),
        and the watcher thread keeps a mid-drain escalation to immediate
        effective. The cancelling shutdown is delegated to the manager's own
        ``__exit__`` - aws-cli's actual path - because s3transfer's public
        ``shutdown()`` forwards its arguments shifted (``(cancel, cancel,
        cancel_msg)``), which would cancel a pending coordinator with the
        message as the exception *class*. ``__exit__`` exists on both engines;
        the classic one cancels with aws-cli's exception shape
        (``FatalError(str(exc))``, Ctrl-C's plain ``CancelledError``), so a
        classic ``CANCELLED`` record names the fatal that revoked it. The CRT
        manager's cancel surfaces awscrt's ``AWS_ERROR_S3_CANCELED``, which
        classifies ``CANCELLED`` too, the fold having marked the run's order
        first (`_is_cancellation`; aws-cli prints a failed line per such
        item, which is not copied). pip s3transfer's CRT manager converts a
        drain-time ``KeyboardInterrupt`` into ``cancel()`` and discards the
        interrupt; `_keep_swallowed_interrupt` catches it at that cancel, so
        what it cancelled classifies ``CANCELLED`` and the interrupt is
        re-raised here once the drain is over (design/crt.md section 6). The
        classic manager re-raises a drain-time interrupt itself.
        """
        if self._manager is None:
            # No transfer ran, but a warning the walk reported from its
            # prefetch worker may still have noted the caller's exception.
            if exc is None:
                self._raise_pending()
            return
        graceful_drain = (
            isinstance(exc, CancelledError)
            and self._cancel_token is not None
            and self._cancel_token.mode is CancelMode.GRACEFUL
        )
        cancel = exc is not None and not graceful_drain
        # The immediate-cancel watcher has run since the manager was built
        # (`_get_manager`); the drain keeps it alive until the shutdown is
        # done, so a mid-drain escalation to IMMEDIATE stays effective.
        watcher = self._watcher
        try:
            if cancel:
                # The manager cancel this fold forces - the token's own
                # CancelledError surfacing through the submission loop
                # (s3._raise_if_cancelled), a fatal, a Ctrl-C - is this run's
                # order: mark it before it is issued, and the revoked items
                # classify CANCELLED on the CRT engine as on the classic one.
                self._cancel_initiated = True
                self._manager.__exit__(type(exc), exc, None)
            else:
                self._manager.shutdown()
        finally:
            self._shutdown_done.set()
            if watcher is not None:
                watcher.join()
            if self._capture is not None:
                # After shutdown every transfer is drained, so no request is
                # emitting on the client while the capture handlers are removed.
                # The finally covers shutdown itself raising (s3transfer re-raises
                # a KeyboardInterrupt arriving during its drain wait): better to
                # unregister with stragglers in flight than to leave the handlers
                # feeding its stores forever on a longer-lived client.
                self._capture.unregister(self._client)
            if self._signing_reads_kept:
                signingreads.unregister(self._client)
        # A Ctrl-C the CRT drain swallowed reaches the caller as the classic
        # manager's re-raise does: ahead of anything noted, and in place of
        # the exception that folded the run when it landed in that fold's
        # drain (the fold's own Ctrl-C is that exception, already on its way).
        swallowed = self._swallowed_interrupt
        if swallowed is not None and swallowed is not exc:
            raise swallowed
        # An interrupt that became an item's outcome during the drain (or on
        # the last submit's worker), or a callback's exception noted there,
        # has no later `submit` to re-raise it.
        if exc is None:
            self._raise_pending()

    def _watch_for_immediate_cancel(self) -> None:
        """Cancel tracked futures once the token escalates to IMMEDIATE.

        Runs from the manager's construction until the shutdown is done, so
        the escalation is acted on during submission and during the drain
        alike.
        """
        token = self._cancel_token
        assert token is not None
        while not self._shutdown_done.wait(0.1):
            if token.mode is CancelMode.IMMEDIATE:
                self._cancel_futures()
                return

    def _track_future(self, future: Any | None) -> None:
        """Track a submitted future without losing a synchronous completion race."""
        if future is None:
            return
        if not isinstance(self._manager, TransferManager):
            self._guard_crt_cancel(future)
        with self._futures_lock:
            self._futures.add(future)
        # A non-threaded manager may finish before submit() returns and before
        # _ForgetFuture can remove it. Re-check after insertion to close that race.
        if future.done():
            self._forget_future(future)

    def _forget_future(self, future: Any) -> None:
        with self._futures_lock:
            self._futures.discard(future)

    def _guard_crt_cancel(self, future: Any) -> None:
        """Catch the Ctrl-C pip s3transfer's CRT drain swallows, at its cancel.

        The CRT manager's drain waits on each coordinator's ``result()``, and
        a ``KeyboardInterrupt`` landing there is turned into that
        coordinator's ``cancel()`` and then discarded - by the coordinator,
        whose cancellation error replaces it, or by the manager's shutdown,
        which cancels every transfer and drops it. Both issue the cancel from
        inside their ``except KeyboardInterrupt`` block, so the coordinator's
        ``cancel`` is wrapped here and reads the interrupt being handled
        (``sys.exc_info``) before passing the call on: a cancel issued under
        an interrupt hands it to `_keep_swallowed_interrupt`. A cancel issued
        any other way passes through untouched. Which transfers are cancelled
        stays the manager's decision. A coordinator without a ``cancel`` (not
        the CRT shape) is left alone.
        """
        coordinator: Any = getattr(future, "_coordinator", None)
        cancel = getattr(coordinator, "cancel", None)
        if cancel is None:
            return

        def guarded_cancel() -> None:
            handled = sys.exc_info()[1]
            if isinstance(handled, KeyboardInterrupt):
                self._keep_swallowed_interrupt(handled)
            cancel()

        coordinator.cancel = guarded_cancel

    def _keep_swallowed_interrupt(self, interrupt: KeyboardInterrupt) -> None:
        """Keep a Ctrl-C the CRT drain is about to swallow, before its cancel.

        The cancel it guards is marked as the run's order
        (``_cancel_initiated``, before the cancel is issued), so the transfer
        it revokes records CANCELLED, and the interrupt is kept, the first
        one only, for `__exit__` to re-raise once the drain is over. The
        manager decides which transfers it cancels - the one whose wait the
        interrupt landed in, or every one still running - and the rest finish
        and report their real outcomes, as on aws. A fold under the caller's
        own Ctrl-C comes through here too, with that interrupt, which
        `__exit__` then leaves to propagate as it is.
        """
        self._cancel_initiated = True
        with self._lock:
            if self._swallowed_interrupt is None:
                self._swallowed_interrupt = interrupt

    def _cancel_futures(self) -> None:
        self._cancel_initiated = True
        with self._futures_lock:
            futures = tuple(self._futures)
        for future in futures:
            future.cancel()

    # -- rollup (approximate until the with block exits) --------------------

    @property
    def succeeded(self) -> int:
        return self._succeeded

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def cancelled(self) -> int:
        return self._cancelled

    @property
    def warned(self) -> int:
        return self._warner.warned

    @property
    def warner(self) -> Warner:
        """The run's advisory sink - what a walk's ``ScanOptions.on_warning`` targets."""
        return self._warner

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def first_error(self) -> Boto3S3Error | None:
        return self._first_error

    # -- non-transfer outcomes ----------------------------------------------
    #
    # Warnings and notices go through the shared sink (``warner``), the
    # engine's own as well as a backend walk's.

    def skip(self, item: TransferItem) -> None:
        """Record a silent, non-warning skip (no exit-code effect)."""
        with self._lock:
            self._skipped += 1
        self._emit(self._result(item, OpOutcome.SKIPPED))

    def dryrun(self, item: TransferItem) -> None:
        """Validate per-item request parameters, then report without an API call.

        aws-cli still runs its request-parameter mapper for a dry run. The
        operation layer has already refused a malformed `grants`
        (`requestparams.validate_grants`); the mapping here keeps the check
        for a caller driving the `Transferrer` itself.
        """
        if self._transfer_type is TransferType.UPLOAD:
            requestparams.map_put_object_params(self._options, self._operation)
        elif self._transfer_type is TransferType.COPY:
            requestparams.map_copy_object_params(self._options, self._operation)
        self._emit(self._result(item, OpOutcome.DRYRUN))

    # -- submission ----------------------------------------------------------

    def prepare(self) -> None:
        """Build the manager (and register capture) before enumeration starts.

        Building the manager mutates the client's event registry
        (``request-created.s3`` handlers; the capture handlers too), and the
        events engine has no lock - the mutation must happen-before any
        concurrent traffic on this client. A listing-driven run fetches pages
        on the scan prefetch worker with this same client while the caller
        submits, so the orchestrator calls this once, before pulling the first
        item (aws-cli likewise builds its TransferManager before the file
        generator starts). A dry run never calls it: no manager is built, so
        listing traffic races no registration.
        """
        self._get_manager()

    def submit(self, item: TransferItem) -> None:
        """Queue one transfer (blocks when the manager queue is full).

        Request-parameter mapping happens here, per item, as on aws. The
        operation layer refuses a malformed ``grants`` before the run
        (`requestparams.validate_grants`); a caller driving the `Transferrer`
        itself meets the mapping's own ``ValidationError`` here instead.
        """
        try:
            if self._transfer_type is TransferType.UPLOAD:
                future = self._submit_upload(item)
            elif self._transfer_type is TransferType.DOWNLOAD:
                future = self._submit_download(item)
            else:
                future = self._submit_copy(item)
            self._track_future(future)
        except BaseException as raised:
            with self._pending_lock:
                if raised is self._pending_raise:
                    # Already on its way to the caller: nothing left to hand back.
                    self._pending_raise = None
            # The item may carry a stream handle - _cp_stream's already-open
            # fileobj, or the open route's deferred reader/writer. A submit
            # that raises before the manager accepted the work - a
            # ValidationError from param mapping, a manager-build failure -
            # means _CloseFileobj never runs, so release here (best-effort;
            # mirrors _CloseFileobj's failure branch, discard-preferring).
            self._close_item_fileobjs(item)
            raise
        # A use_threads=False run's interrupt: the serial executor settled the
        # item (CANCELLED) and noted the interrupt, and upstream's submission
        # task then swallowed it, so the manager call returned as if nothing
        # had happened. The same holds for whatever a worker noted since the
        # last submit (`_note_raise`). Re-raise it here - the item's fileobjs
        # were already released by its done subscribers - and the caller's
        # exit folds the manager the way any mid-run interrupt does.
        self._raise_pending()

    def _note_raise(self, exc: BaseException) -> None:
        """Keep the first exception owed to the caller (`_pending_raise`)."""
        with self._pending_lock:
            if self._pending_raise is None:
                self._pending_raise = exc

    def _raise_pending(self) -> None:
        """Re-raise, once, the exception `_note_raise` kept."""
        with self._pending_lock:
            pending, self._pending_raise = self._pending_raise, None
        if pending is not None:
            raise pending

    def _deliver(self, result: OpResult) -> None:
        """Hand one record to the caller's ``on_result``, whatever thread settled it.

        A record settles where its item ends: the calling thread for a
        submit-time outcome, a walk warning or a ``use_threads=False``
        transfer, an s3transfer worker for a threaded one, an awscrt thread
        for the CRT engine's. Whatever the callback raises belongs to the
        caller, but only the calling thread can raise it there: s3transfer
        logs and drops an ``Exception`` from a done callback (the calling
        thread under ``use_threads=False`` included), a worker's
        ``BaseException`` dies with the pool's work item, and one escaping an
        awscrt callback leaves the CRT manager's drain waiting for good. So
        the exception is noted first (`_note_raise`, re-raised by the next
        `submit` or by ``__exit__``) and raised on the spot only on the
        calling thread - where it propagates as it always did, or, inside
        s3transfer's callback runner, is dropped there and still reaches the
        caller through the note.
        """
        callback = self._on_result
        assert callback is not None  # only wired when the caller passed one
        try:
            callback(result)
        except BaseException as exc:
            self._note_raise(exc)
            if threading.get_ident() == self._caller_thread:
                raise

    def _deliver_progress(self, progress: TransferProgress) -> None:
        """Hand one progress record to the caller's ``on_progress``.

        Progress fires inside the transfer it reports on - s3transfer's read
        and write path (on the calling thread too under ``use_threads=False``)
        or an awscrt thread - where an exception would fail that item, or
        escape into awscrt. What the callback raises is the caller's failure,
        not the item's, so an ``Exception`` is only noted here and reaches the
        caller from the next `submit` or from ``__exit__``, as `_deliver` hands
        back ``on_result``'s. An interrupt (``KeyboardInterrupt`` /
        ``SystemExit``) is left to propagate, so it cancels the item as one
        raised anywhere else in the transfer does.
        """
        callback = self._on_progress
        assert callback is not None  # only wired when the caller passed one
        try:
            callback(progress)
        except Exception as exc:
            self._note_raise(exc)

    @staticmethod
    def _close_item_fileobjs(item: TransferItem) -> None:
        for fileobj in (item.src_fileobj, item.dest_fileobj):
            if fileobj is None:
                continue
            # Prefer discard (the open route's deferred handles): a submit
            # that failed before the manager accepted the work must not
            # commit an empty object on the backend by opening at close.
            release = getattr(fileobj, "discard", None)
            try:
                release() if release is not None else fileobj.close()
            except Exception:
                logger.debug("closing a fileobj after a submit error failed", exc_info=True)

    def _submit_upload(self, item: TransferItem) -> Any | None:
        """Map one upload and attach close, move-delete, completion, and tracking hooks."""
        # Mapped first, like aws-cli's _do_submit.
        extra_args = requestparams.map_put_object_params(self._options, self._operation)
        # A directory source is handed through to fail as on aws-cli ([Errno 21]
        # Is a directory, rc 1); botocore's default checksum wrapper would
        # otherwise open it and mask the read failure as an opaque rewind
        # error, so detect it and surface the OS error directly. A stream
        # (src_fileobj) is never a directory.
        src_path = item.src_path
        if item.src_fileobj is None and src_path:
            try:
                src_path = _os_form(self._src_storage, src_path)
            except ValueError as exc:
                self._record_failure(item, exc)
                return None
            if os.path.isdir(src_path):
                self._record_failure(
                    item, IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), item.src_path)
                )
                return None
        name = ""
        if self._options.get("guess_mime_type", True) and "ContentType" not in extra_args:
            name = item.src_path or ""
            if not name and item.src_info is not None:
                # An open-route item has no local path: guess from its
                # compare_key, the name its S3 key derives from (the basename
                # for a single source), or from the destination key when a
                # single source's entry names nothing. A stream item carries no
                # src_info and stays guess-free (no filename, aws-cli parity) - its
                # empty name never opens the store below, so an overlay the
                # host cannot read is no concern of a stream upload, exactly as
                # aws-cli's stream submitter, which attaches no guessing subscriber.
                name = item.compare_key or item.dest_key or ""
        if name:
            try:
                guessed = _guess_content_type(name)
            except OSError as exc:
                # A host overlay the guess reads failed to load (a mime.types
                # that exists but cannot be read, say). aws-cli guesses inside
                # s3transfer's queued subscriber, so the error fails that
                # item - `upload failed: ... [Errno 13] Permission denied:
                # '/etc/mime.types'` - and the run goes on, a sync's deletes
                # included; raised here it ended the run as one fatal error.
                # Nothing was submitted, so the item's fileobj is released the
                # way a submit-time failure releases it.
                self._close_item_fileobjs(item)
                self._record_failure(item, exc)
                return None
            if guessed is not None:
                extra_args["ContentType"] = guessed
        subscribers, counted = self._common_subscribers(item)
        if item.src_fileobj is not None:
            subscribers.append(_CloseFileobj(item.src_fileobj))
        if self._is_move:
            subscribers.append(self._delete_source_subscriber(item))
        completion = self._completion(item, counted)
        subscribers.append(completion)
        subscribers.append(_ForgetFuture(self._forget_future))
        manager = self._get_manager()
        self._admit_capture(item)
        fileobj: Any = item.src_fileobj if item.src_fileobj is not None else src_path
        if item.src_fileobj is not None and not isinstance(manager, TransferManager):
            # The CRT engine reads the body on an awscrt thread (`_GuardedReader`);
            # the completion reads back what the guard caught there.
            guard = _GuardedReader(item.src_fileobj, self._note_raise, self._caller_thread)
            completion.stream_guard = guard
            fileobj = guard
        return self._accept(
            manager.upload,
            fileobj=fileobj,
            bucket=item.dest_bucket,
            key=item.dest_key,
            extra_args=extra_args,
            subscribers=subscribers,
        )

    def _submit_download(self, item: TransferItem) -> Any:
        """Map one download and order destination, durability, and completion hooks."""
        extra_args = requestparams.map_get_object_params(self._options)
        dest_path = item.dest_path
        if dest_path is not None:
            # The OS form of the destination (`_os_form`); a key whose name the
            # filename encoding cannot represent fails here, as the item.
            try:
                dest_path = _os_form(self._dest_storage, dest_path)
            except ValueError as exc:
                self._record_failure(item, exc)
                return None
        subscribers, counted = self._common_subscribers(item)
        if dest_path is not None:
            subscribers.append(_DirectoryCreator(_text_form(self._dest_storage)))
        if item.dest_fileobj is not None:
            subscribers.append(_CloseFileobj(item.dest_fileobj))
        # Before the mv source delete, like aws-cli registering
        # ProvideLastModifiedTimeSubscriber ahead of DeleteSourceObjectSubscriber:
        # a failed delete then still leaves the downloaded file carrying the
        # source mtime (a later sync compares equal instead of re-downloading).
        subscribers.append(_StampMtime(item, self._stamp_mtime))
        if self._is_move:
            # Durability barrier before the source delete (LocalStorage(fsync=True),
            # a library-only opt-in; off by default = aws-cli parity). Only a real local
            # destination path can be fsynced - a stream / custom (open-route) dest
            # owns its own durability, so item.dest_fileobj downloads are excluded.
            if (
                dest_path is not None
                and isinstance(self._dest_storage, LocalStorage)
                and self._dest_storage.fsync
            ):
                subscribers.append(_FsyncDest(dest_path, self._dest_storage.text_path))
            subscribers.append(self._delete_source_subscriber(item))
        if item.case_conflict_cleanup is not None:
            # After the source delete, aws-cli's slot for
            # CaseConflictCleanupSubscriber: on_done subscribers run in
            # registration order, so releasing the key earlier would shrink the
            # window in which a same-name-different-case twin still counts as
            # in flight - by an os.utime on cp, by a whole DeleteObject on mv.
            subscribers.append(_CaseConflictCleanup(item.case_conflict_cleanup))
        subscribers.append(self._completion(item, counted))
        subscribers.append(_ForgetFuture(self._forget_future))
        manager = self._get_manager()
        if dest_path is not None and not isinstance(manager, TransferManager):
            subscribers.insert(0, _KeepRenameFailure())
        self._admit_capture(item)
        return self._accept(
            manager.download,
            bucket=item.src_bucket,
            key=item.src_key,
            fileobj=item.dest_fileobj if item.dest_fileobj is not None else dest_path,
            extra_args=extra_args,
            subscribers=subscribers,
        )

    def _submit_copy(self, item: TransferItem) -> Any:
        """Map one S3 copy, including copy-props and optional source deletion."""
        extra_args = requestparams.map_copy_object_params(self._options, self._operation)
        # Whether s3transfer will run this copy as multipart: upstream's own
        # comparison (size >= threshold), which the flip below and the
        # copy-props subscribers share. A caller-less size (a custom scan) is
        # predicted single-part; s3transfer then sizes it with its own probe.
        multipart = item.size is not None and item.size >= self._multipart_threshold
        if extra_args.get("MetadataDirective") == "COPY" and multipart:
            # A multipart-bound explicit COPY flips to REPLACE in the request
            # parameters only. Every supported s3transfer blocklists
            # MetadataDirective from CreateMultipartUpload, so no wire request
            # changes; the flip is what keeps upstream 0.19's
            # PRESERVED_METADATA_FIELDS strip from dropping the caller's own
            # properties (ContentType and friends) where aws-cli's fork -
            # tableless - passes them through. The single-part CopyObject keeps
            # the real COPY. A caller-less size skips the flip and leaves the
            # decision - and upstream's own preservation - to its probe.
            extra_args["MetadataDirective"] = "REPLACE"
        subscribers, counted = self._common_subscribers(item)
        if self._runs_copy_props:
            subscribers.extend(self._copy_props_subscribers(item, multipart=multipart))
        if self._is_move:
            subscribers.append(self._delete_source_subscriber(item))
        subscribers.append(self._completion(item, counted))
        subscribers.append(_ForgetFuture(self._forget_future))
        manager = self._get_manager()
        self._admit_capture(item)
        return self._accept(
            manager.copy,
            copy_source={"Bucket": item.src_bucket, "Key": item.src_key},
            bucket=item.dest_bucket,
            key=item.dest_key,
            extra_args=extra_args,
            subscribers=subscribers,
            source_client=self._source_client,
        )

    def _accept(self, hand_over: Callable[..., Any], **kwargs: Any) -> Any:
        """Hand one transfer to the manager, keeping its own refusal in the taxonomy.

        s3transfer refuses some caller arguments synchronously, before it
        accepts the work - the CRT manager's checksum algorithm outside the set
        awscrt computes (``ChecksumAlgorithm: MD5 not supported``), either
        engine's extra_args key check - with a bare ``ValueError``. That is a
        caller-argument problem like `InvalidCrtTransferConfigError` at the
        manager build, and stays inside the taxonomy the same way
        (design/exceptions.md carves out exactly one pass-through,
        ``MissingDependencyException``). The classic engine's botocore-level
        refusals arrive per item on the task instead (FAILED records); this is
        the up-front one, and the run ends the way any submit-time
        `ValidationError` does.
        """
        try:
            return hand_over(**kwargs)
        except ValueError as exc:
            raise ValidationError(str(exc), operation=self._operation) from exc

    def _common_subscribers(self, item: TransferItem) -> tuple[list[Any], _CountBytes | None]:
        """Build the size, ETag, and progress hooks shared by all transfer routes.

        The second element is the byte counter a size-unknown item carries
        (``None`` otherwise), for `_completion` to read.
        """
        # Size and etag are provided for every kind, aws-cli-style: under the
        # default response_checksum_validation ("when_supported") s3transfer
        # skips its pre-transfer HeadObject probe (downloads AND copies) only
        # when both are present (a "when_required" client skips the HEAD
        # regardless, probing with a first-chunk GET). S3-sourced items always
        # carry an etag; local sources have none, and uploads never probe.
        subscribers: list[Any] = []
        counted: _CountBytes | None = None
        total = item.size
        if item.size is not None:
            subscribers.append(_ProvideSize(item.size))
        else:
            hint = _applied_size_hint(item)
            if hint is not None:
                subscribers.append(_ProvideSize(hint))
                # The caller's word on the size is also the only total a
                # progress report can carry for a stream nobody can measure.
                total = hint
            # A size-unknown item (a stream, a custom backend's deferred
            # reader): count what actually moves, so SUCCEEDED can report it
            # where the engine resolves no size of its own (`_CountBytes`) -
            # or resolves the hint it was handed, which is not the count.
            counted = _CountBytes(hinted=hint is not None)
            subscribers.append(counted)
        # "is not None", not truthiness: an endpoint's empty ETag is provided
        # like any other (aws-cli hands s3transfer whatever the response
        # carried), and a provided etag - empty or not - is what makes
        # s3transfer skip its HeadObject probe.
        if item.etag is not None:
            subscribers.append(_ProvideETag(item.etag))
        if self._on_progress is not None:
            subscribers.append(
                _Progress(
                    self._result_transfer_type, item.compare_key, total, self._deliver_progress
                )
            )
        return subscribers, counted

    def _completion(self, item: TransferItem, counted: _CountBytes | None) -> _Completion:
        """Bind an item's terminal callbacks into one s3transfer subscriber."""
        return _Completion(
            item,
            on_success=self._record_success,
            on_failure=self._record_failure,
            counted=counted,
            hinted=counted is not None and counted.hinted,
        )

    def _delete_source_subscriber(self, item: TransferItem) -> _DeleteSource:
        """The per-item source deletion for ``mv`` (aws-cli DeleteSource* trio).

        The source is removed through its own backend's ``Storage.delete(info)``,
        keyed by the source listing entry (``src_info``, whose ``storage`` is
        the backend it came from): a local file or a custom (open-route)
        backend object on an upload, a single DeleteObject on the source
        bucket's client for a download or a copy, with ``RequestPayer``
        forwarded like every other request. Each delete raises in the library
        taxonomy with the source's own key - an OS error keeps the message ``aws``
        prints (``move failed: ... [Errno 13] ...``) - so the record never
        attributes it to the destination that copied fine.
        """
        info = item.src_info
        assert info is not None and info.storage is not None
        storage = info.storage
        if isinstance(storage, S3Storage):
            request_payer = self._options.get("request_payer") or None
            return _DeleteSource(
                lambda: storage.delete(info, request_payer=request_payer),
                capture=self._capture_response,
            )
        return _DeleteSource(lambda: storage.delete(info), capture=self._capture_response)

    def _copy_props_subscribers(self, item: TransferItem, *, multipart: bool) -> list[Any]:
        """Translate the selected copy-props mode into ordered request subscribers.

        ``multipart`` is `_submit_copy`'s prediction of how s3transfer runs
        the copy, which every subscriber that differs by it reads.
        """
        mode = self._copy_props
        exclude = _ExcludeAnnotationDirective(client=self._client, multipart=multipart)
        if mode is CopyPropsMode.NONE:
            return [_ReplaceMetadataDirective(), _ReplaceTaggingDirective(), exclude]
        metadata = _SetMetadataDirectiveProps(
            item,
            source_client=self._source_client,
            multipart=multipart,
            head_params=requestparams.map_head_object_params_with_copy_source_sse(self._options),
            operation=self._operation,
        )
        if mode is CopyPropsMode.METADATA_DIRECTIVE:
            return [metadata, _ReplaceTaggingDirective(), exclude]
        tags = _SetTags(
            item,
            source_client=self._source_client,
            dest_client=self._client,
            multipart=multipart,
            options=self._options,
            operation=self._operation,
        )
        if mode is CopyPropsMode.DEFAULT:
            return [metadata, tags, exclude]
        # CopyPropsMode.ALL - annotations carried instead of excluded (the
        # constructor already refused the mode on an incapable SDK).
        assert self._completions is not None
        annotations = _SetAnnotations(
            item,
            source_client=self._source_client,
            dest_client=self._client,
            completions=self._completions,
            multipart=multipart,
            mode=self._annotation_copy_mode,
            options=self._options,
            temp_dir=getattr(self._transfer_config, "annotation_temp_dir", None),
            operation=self._operation,
        )
        return [metadata, tags, annotations]

    # -- engine internals ----------------------------------------------------

    def _get_manager(self) -> Any:
        """Create and cache the selected transfer manager and response capture."""
        if self._manager is None:
            # Built lazily so a dryrun never constructs a manager (no thread
            # pools, no client-event registration). Listing-driven live runs
            # build it up front via prepare() - registration must precede the
            # scan prefetch worker's traffic on this client; a stream run
            # builds it here at first submit. The s3transfer.manager module
            # itself is already imported (by boto3 at client build).
            manager = self._create_crt_manager()
            if manager is None:
                manager = self._create_classic_manager()
                # botocore signs the classic engine's requests, with awscrt's
                # signer when awscrt is importable: a part or file read failing
                # while it signs reaches the item as the read's own exception,
                # not as awscrt's AWS_ERROR_UNKNOWN and a traceback on stderr
                # (`signingreads`). The CRT engine signs in awscrt's own client.
                # Removed in __exit__ after the manager shuts down.
                self._signing_reads_kept = signingreads.register(self._client)
            # Transfer-time breadcrumb: names the engine actually built for
            # this run (CRTTransferManager vs the classic TransferManager),
            # after any CRT->classic fallback. Lets --debug distinguish a real
            # CRT transfer from a silent classic fallback (design/testing.md).
            logger.debug("transfer engine: %s", type(manager).__name__)
            if self._capture_response:
                # Registered once, before the first submit; drained per item by
                # _build_extra_info; removed in __exit__ after the manager shuts
                # down (capture forces the classic engine, so it is always live).
                self._capture = _ResponseCapture()
                self._capture.register(self._client)
            self._manager = manager
            if self._cancel_token is not None:
                # From here to the end of the drain, not only during the
                # drain: an IMMEDIATE order must reach in-flight work while
                # the submitting thread waits on the engine's backpressure
                # (the CRT manager's transfer semaphore), where a stalled
                # request would otherwise hold the run until it completed by
                # itself (measured: 25 s of a 25 s stall for 140 files,
                # against 3 s once the loop had finished submitting 20).
                self._watcher = threading.Thread(
                    target=self._watch_for_immediate_cancel,
                    name="boto3-s3-cancel",
                    daemon=True,
                )
                self._watcher.start()
        return self._manager

    def _create_crt_manager(self) -> Any | None:
        """The boto3-faithful CRT attempt; ``None`` selects the classic engine.

        ``preferred_transfer_client`` is read with boto3's defaults (no config
        = ``'auto'``); a copy run is unconditionally classic - the CRT manager
        has no copy, the same rule boto3 and aws-cli apply to s3->s3.
        The lock and signing postures in ``crt`` are the caller-declared
        departures from boto3's rules available here, and apply only when the
        caller asks for them.
        """
        if self._transfer_type is TransferType.COPY:
            return None
        if self._capture_response:
            # Response capture rides the botocore client's event stream, which the
            # CRT data plane bypasses; force the classic engine so the write
            # response is observable. A deliberate capture_response trade - the
            # flag is library-only (no aws-cli equivalent), so no parity impact.
            logger.debug("transfer engine: classic forced by capture_response")
            return None
        if not crtsupport.selects_crt(self._transfer_config):
            return None
        _allow_if_none_match()  # the CRT manager aliases the classic arg lists
        # The copy tables are inert for CRT (it has no copy path) but keeping
        # one alignment site means the classic fallback never sees them unpatched.
        _allow_inline_mpu_tagging()
        # Every construction failure is classified at this seam
        # (`crt_engine_errors`): design/exceptions.md carves out exactly one
        # pass-through (MissingDependencyException, raised by selects_crt
        # above), and none of these is it.
        with crt_engine_errors(self._operation):
            # The run's client is the route-selected one (an S3Storage may
            # carry its own), so the S3-level endpoint pin only applies to the
            # client it built (`CrtPostures.engine_arguments`).
            return crtsupport.create_crt_transfer_manager(
                self._client, self._transfer_config, **self._crt.engine_arguments(self._client)
            )

    def _create_classic_manager(self) -> Any:
        """Build the classic s3transfer manager, honoring threaded execution config."""
        _allow_if_none_match()
        _allow_inline_mpu_tagging()
        config: Any = self._transfer_config
        if config is None:
            # The library's own defaults, not s3transfer's bare ones: an omitted
            # config must land on exactly the values a default-constructed
            # TransferConfig carries, or the two drift (they differ in the
            # download IO queue depth).
            config = transferconfig.TransferConfig()
        # boto3's NonThreadedExecutor semantics under use_threads=False, and
        # s3transfer's default thread pool otherwise, each with the task hooks
        # (`_TaskGuard`); s3transfer instantiates the class per executor with
        # max_workers alone, so the note hook rides a partial.
        executor_cls: Any = functools.partial(
            _SerialExecutor if not getattr(config, "use_threads", True) else _ThreadedExecutor,
            self._note_raise,
        )
        # TransferManager registers handlers on self._client at construction and
        # leaves them there - shutdown() never removes them. Intentional and
        # boto3-faithful (boto3's own TransferManager does the same): we do not
        # restore the client, because it may be shared and unregistering could
        # disrupt a concurrent transfer. Run parallel operations with one client
        # per thread (design/s3.md thread-safety note).
        client: Any = self._completions if self._completions is not None else self._client
        return TransferManager(
            client, config=config, osutil=_ReplacingOSUtils(), executor_cls=executor_cls
        )

    @property
    def _multipart_threshold(self) -> int:
        threshold = getattr(self._transfer_config, "multipart_threshold", None)
        return threshold if threshold is not None else _DEFAULT_MULTIPART_THRESHOLD

    def _result(
        self,
        item: TransferItem,
        outcome: OpOutcome,
        *,
        bytes_transferred: int = 0,
        error: Boto3S3Error | None = None,
        extra_info: Mapping[str, Any] | None = None,
    ) -> OpResult:
        """Build one public result with the run's storage and display context."""
        return OpResult(
            transfer_type=self._result_transfer_type,
            compare_key=item.compare_key,
            outcome=outcome,
            bytes_transferred=bytes_transferred,
            error=error,
            src=item.src_display or None,
            dest=item.dest_display or None,
            src_info=item.src_info,
            dest_info=item.dest_info,
            src_storage=self._src_storage,
            dest_storage=self._dest_storage,
            extra_info=extra_info,
        )

    def _emit(self, result: OpResult) -> None:
        if self._on_result is not None:
            self._deliver(result)

    def _record_success(
        self,
        item: TransferItem,
        resolved_size: int | None = None,
        etag: str | None = None,
        delete_response: dict[str, Any] | None = None,
    ) -> None:
        """Count and emit a successful terminal result with captured response data."""
        # item.size is unset for an unknown-size transfer (a stream, a custom
        # backend's deferred reader); fall back to what `_Completion` resolved
        # - the size the engine put on the future, or the bytes `_CountBytes`
        # counted where it put none or only echoed a hint - so SUCCEEDED
        # reports the real byte count.
        size = item.size if item.size is not None else resolved_size
        extra_info = self._build_extra_info(item, etag, delete_response)
        with self._lock:
            self._succeeded += 1
        self._emit(
            self._result(
                item, OpOutcome.SUCCEEDED, bytes_transferred=size or 0, extra_info=extra_info
            )
        )

    def _build_extra_info(
        self,
        item: TransferItem,
        etag: str | None,
        delete_response: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Assemble the result's ``extra_info`` from the captured S3 responses.

        Without ``capture_response`` this keeps the historical shape: the ETag as
        ``{"ETag": ...}`` from ``future.meta.etag`` - the source object's ETag that
        boto3-s3 hands s3transfer for a copy and a download (not necessarily equal to
        the written object's: a multipart copy, or a source whose ETag is not a plain
        MD5, gives the destination a different one); an upload has none, so
        ``extra_info`` is then ``None``. With ``capture_response`` a
        classic upload / copy also carries the full write response under ``"write"``
        (``PutObject`` / ``CopyObject`` / ``CompleteMultipartUpload``, minus
        ``ResponseMetadata``), and ``"ETag"`` is promoted from it - the written
        object's own ETag, the one field whose location varies by write API. An
        ``mv`` whose source is S3 also carries that source's DeleteObject response
        under ``"delete"`` (captured by ``_DeleteSource``), and a download carries
        the source's GetObject response (Body-stripped) under ``"read"``.
        """
        write, read = self._drain_captured(item)
        info: dict[str, Any] = {}
        if write is not None:
            info["write"] = write
        if read is not None:
            info["read"] = read
        if delete_response is not None:
            info["delete"] = delete_response
        # ETag comes from the transferred object's own response when captured
        # (write for upload/copy, read for download), else the s3transfer etag.
        captured = write if write is not None else read
        etag_value = _extract_etag(captured) if captured is not None else etag
        if etag_value:
            info["ETag"] = etag_value
        return info or None

    def _drain_captured(
        self, item: TransferItem
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Pop the item's captured ``(write, read)`` responses, if any.

        Every terminal outcome must drain: ``after-call`` fires before botocore
        raises for a >=300 status, so a FAILED / SKIPPED item may have stored a
        response (possibly an error payload) - and an ``mv`` whose source delete
        failed has stored its successful write - that would otherwise sit in the
        store for the rest of the run.
        """
        if self._capture is None:
            return None, None
        bucket, key = self._captured_key(item)
        if self._transfer_type is TransferType.DOWNLOAD:
            return None, self._capture.pop_read(bucket, key)
        # Also drain any read recorded for this key (a content filter's
        # GetObject of the pre-overwrite destination through this client):
        # it is not the item's transfer response, and must not linger.
        self._capture.pop_read(bucket, key)
        return self._capture.pop_write(bucket, key), None

    def _captured_key(self, item: TransferItem) -> tuple[str | None, str | None]:
        """The object whose response the capture keeps for ``item``.

        The source for a download, else the destination. The key admitted at
        submit (`_admit_capture`) and the key drained at the item's terminal
        (`_drain_captured`) are chosen here, so they cannot disagree - an
        admitted key nobody drained would sit in the store for the rest of
        the run.
        """
        if self._transfer_type is TransferType.DOWNLOAD:
            return item.src_bucket, item.src_key
        return item.dest_bucket, item.dest_key

    def _admit_capture(self, item: TransferItem) -> None:
        """Admit ``item``'s key into the response capture (`_ResponseCapture.expect`).

        Called by each route after `_get_manager`, which builds the capture
        (on a stream run, at the first submit). Only now, at submit: only
        submitted items' responses are recorded. A same-run content filter
        may have read a download's source through the run's own client (a
        ``SyncPair`` content ``update_filter`` via ``S3Storage.open(...,
        "rb")``) before submit, and that GetObject must neither occupy the
        first-stored-wins read slot (the transfer's own GetObject is the
        item's read) nor sit in the store forever for a pair the run never
        submits.
        """
        if self._capture is not None:
            self._capture.expect(*self._captured_key(item))

    def _record_failure(
        self, item: TransferItem, exc: BaseException, revoked: bool | None = None
    ) -> None:
        """Classify a terminal exception as a cancellation, no-overwrite skip, or failure.

        ``revoked`` is the classic coordinator's word on whether its
        ``cancel()`` installed the outcome (`_future_revoked`); None where
        there is no such word (a submit-time failure, the CRT engine).
        """
        # Failed / skipped / cancelled items surface no captured response, but
        # the entry must still leave the store (see _drain_captured).
        self._drain_captured(item)
        interrupted = not isinstance(exc, Exception)
        if interrupted:
            # An interrupt stored as the item's outcome (`_outcome`): the item
            # was cut short, not found faulty, and whoever stored it swallowed
            # it on the way - so it is kept for `submit` / `__exit__` to hand
            # back to the caller, and the item records CANCELLED under the
            # wording the serial executor's own cancel uses.
            self._note_raise(exc)
            exc = S3TransferCancelledError(str(exc) or repr(exc))
        if interrupted or _is_cancellation(
            exc, cancel_initiated=self._cancel_initiated, revoked=revoked
        ):
            # An accepted item revoked as a cancellation: classic s3transfer's
            # CancelledError shapes (a fatal elsewhere, classic Ctrl-C) or a
            # CRT cancel this run ordered (its token, a fatal, a Ctrl-C).
            # CANCELLED, not FAILED - nothing is wrong with the item itself,
            # and the run's outcome is the exception the operation raises.
            # Deliberately richer than aws-cli, which drops cancelled items
            # from output and counts on its classic engine and fails them on
            # its CRT one; first_error stays reserved for real failures
            # (BatchError's diagnostic sample). A CRT cancel the run did not
            # order falls through to the failure branch below
            # (`_is_cancellation`).
            error = CancelledError(
                str(exc) or "canceled",
                operation=self._operation,
                bucket=item.dest_bucket or item.src_bucket,
                key=item.dest_key or item.src_key,
            )
            with self._lock:
                self._cancelled += 1
            self._emit(self._result(item, OpOutcome.CANCELLED, error=error))
            return
        # A failure without any response (a connection closed, a read timeout)
        # is FAILED like every other: aws-cli's completion handler loses it
        # with no line at rc 0, which is not copied (design/opresult.md).
        if self._options.get("no_overwrite") and _is_conditional_write_rejection(exc):
            # --no-overwrite's IfNoneMatch rejection: the object already
            # exists, which is the asked-for outcome - a silent skip, not a
            # failure (aws-cli DoneResultSubscriber; rc stays 0). aws-cli skips
            # every PreconditionFailed whatever the request was; any other 412
            # fails the item here (docs/cli/aws-differences.md).
            with self._lock:
                self._skipped += 1
            self._emit(self._result(item, OpOutcome.SKIPPED))
            return
        # The library's own CancelledError on a future nobody revoked was
        # thrown by the caller's code: like every library exception the
        # caller's code raises, it is recorded as it is (a FAILED record).
        error = self._local_failure(item, exc) or translate_boto_error(
            exc,
            operation=self._operation,
            bucket=item.dest_bucket or item.src_bucket,
            key=item.dest_key or item.src_key,
        )
        if error is not exc:
            # Link the original exception the way the raise-from paths
            # (s3_errors) do, so a record's error carries its ClientError at
            # __cause__ (exceptions.md section 2.1). A pass-through
            # Boto3S3Error keeps whatever cause it already has.
            error.__cause__ = exc
        # Best-effort attribution: fill what the raiser could not know, never
        # overwrite what it did. A family error raised in-pipeline passes
        # through the translation unchanged, so this is where the run's context
        # reaches it - a storage's open / delete, a backend's reads and writes,
        # the stream storages' missing-stdio check, and this module's own
        # subscribers that raise unnamed (_DirectoryCreator / _FsyncDest); a
        # translated error already carries these values.
        attribute_failure(
            error,
            operation=self._operation,
            bucket=item.dest_bucket or item.src_bucket,
            key=item.dest_key or item.src_key,
        )
        with self._lock:
            self._failed += 1
            if self._first_error is None:
                self._first_error = error
        self._emit(self._result(item, OpOutcome.FAILED, error=error))

    def _local_failure(self, item: TransferItem, exc: BaseException) -> Boto3S3Error | None:
        """The local taxonomy's error for a failure of the item's local file, else None.

        The engines open, write and rename the local file themselves - the
        classic engine's s3transfer raising the ``OSError`` (``[Errno 13]
        Permission denied`` for a download into a directory it cannot write),
        the CRT engine reporting it under awscrt's name - so the failure takes
        the category the library's own file paths give it
        (`translate_os_error`), its message (aws-cli's words) as it is, and names
        the file in ``key`` with no ``bucket`` (design/exceptions.md section
        2.1), not the object. botocore's own errors that are also ``OSError``
        (``SSLError``) are the request's; so is an ``OSError`` naming some
        other file - a credential provider's token file, the host's
        ``mime.types`` - raised in the same task; a stream item has no local
        path and keeps the request's attribution.
        """
        download = self._transfer_type is TransferType.DOWNLOAD
        path = item.dest_path if download else item.src_path
        if path is None or isinstance(exc, BotoCoreError):
            return None
        key = path.replace(os.sep, "/")
        if isinstance(exc, OSError):
            # The engines name the files in the OS form; the item's path and
            # every report are the local side's text.
            name_files_as_text(
                exc, _text_form(self._dest_storage if download else self._src_storage)
            )
            if not _names_item_file(exc, path):
                return None
            return translate_os_error(exc, operation=self._operation, key=key)
        category = _CRT_LOCAL_CATEGORIES.get(crt_error_name(exc) or "")
        if category is None:
            return None
        return category(str(exc), operation=self._operation, key=key)

    def _stamp_mtime(self, item: TransferItem) -> None:
        """Stamp the source LastModified onto the downloaded file.

        Runs where the download's subscribers do - an s3transfer worker, or
        the calling thread under ``use_threads=False`` - after a successful
        download. Failure
        does not undo the transfer (the bytes are on disk): it adds a WARNED
        record - the broad catch is deliberate, ``os.utime``
        can raise ``OverflowError`` / ``ValueError`` on Windows for timestamps
        outside ``localtime()``'s range besides the common ``OSError``.

        Whole seconds only: aws-cli stamps its source timestamp through
        ``timetuple`` / ``time.mktime``, which drops the sub-second part, and an
        endpoint whose listing carries milliseconds (MinIO and other
        S3-compatible servers - real S3 lists whole seconds) would otherwise
        leave a stamp that ``--exact-timestamps`` accepts here and rejects
        there.
        """
        if item.mtime is None or item.dest_path is None:
            return
        try:
            _set_file_utime(
                _os_form(self._dest_storage, item.dest_path),
                item.mtime.replace(microsecond=0).timestamp(),
            )
        except Exception as exc:
            name_files_as_text(exc, _text_form(self._dest_storage))
            self._warner.warn(
                f"Skipping file {item.dest_path}. Successfully Downloaded {item.dest_path} "
                f"but was unable to update the last modified time. {exc}",
                info=item.src_info,
            )


# -- subscribers --------------------------------------------------------------
#
# Plain classes, not s3transfer.subscribers.BaseSubscriber subclasses:
# s3transfer resolves callbacks by getattr (duck typing), and inheriting would
# import the SDK at module load. Method signatures follow the subscriber
# protocol: on_queued(future), on_progress(future, bytes_transferred),
# on_done(future), all with **kwargs.


class _TaskGuard:
    """The hooks both classic executors install on each task s3transfer submits.

    s3transfer hands every task object to its executor's ``submit``, so the
    hooks go onto the task instance there, before it runs (`_install`):

    - ``_execute_main`` is wrapped (`_guarded`) so a ``BaseException`` outside
      ``Exception`` landing in a task's main first cancels the transfer's
      coordinator and is then noted for the caller's thread to re-raise
      (`Transferrer._note_raise`). Upstream's ``Task.__call__`` catches
      ``Exception`` alone, and its ``finally`` still runs the task's done
      callbacks - a download's final rename task among them - and, for the
      final task, ``announce_done``, with the coordinator still ``running``:
      ``result()`` then returns ``None`` as if the transfer had succeeded, so
      the done subscribers would delete a ``mv`` source, rename a half-written
      download into place, and record SUCCEEDED for bytes that never arrived.
      Cancelled first - ``CancelledError`` naming the exception, the shape the
      manager's own Ctrl-C shutdown gives in-flight transfers - the item
      records CANCELLED and upstream's failure cleanups run (temp file
      removed, multipart aborted, the rename skipped).
    - a submission task's ``_log_and_set_exception`` notes such an exception
      too (`_noting`).
    - a single-request download into a destination that cannot seek writes
      each byte once across retries (`_writing_once`).

    The attribute lookups are guards: an upstream that reshapes its task
    degrades to its own behavior.
    """

    _note: Callable[[BaseException], None]

    def _install(self, fn: Any) -> None:
        coordinator = getattr(fn, "_transfer_coordinator", None)
        execute = getattr(fn, "_execute_main", None)
        if coordinator is not None and execute is not None:
            fn._execute_main = self._guarded(execute, coordinator)
        log_and_set = getattr(fn, "_log_and_set_exception", None)
        if log_and_set is not None and hasattr(fn, "_submit"):
            fn._log_and_set_exception = self._noting(log_and_set)
        if isinstance(fn, ImmediatelyWriteIOGetObjectTask):
            task: Any = fn
            main_kwargs: dict[str, Any] = getattr(task, "_main_kwargs", None) or {}
            output: Any = main_kwargs.get("download_output_manager")
            handle_io = getattr(task, "_handle_io", None)
            if isinstance(output, DownloadNonSeekableOutputManager) and handle_io is not None:
                task._handle_io = _writing_once(handle_io)

    def _noting(self, log_and_set: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """Wrap a submission task's ``_log_and_set_exception`` to note an interrupt.

        Upstream's ``SubmissionTask._main`` catches ``BaseException`` and hands
        it here to be stored as the transfer's outcome - which does nothing
        once the coordinator has settled. That is exactly where an interrupt
        can land under ``use_threads=False``: the item's final task ran
        inside the submission call, announced done and cleared the done
        callbacks, and the submission task is still unwinding
        (``BoundedExecutor.submit``'s bookkeeping, ``add_associated_future``).
        Nothing would then record or re-raise it and the run would go on to
        the next item (measured: about 1 real SIGINT in 100 on a run of small
        uploads). Noted here, it is re-raised by `Transferrer.submit` whether
        upstream manages to store it or not.
        """

        def _log_and_set_exception(exception: Any) -> Any:
            if not isinstance(exception, Exception):
                self._note(exception)
            return log_and_set(exception)

        return _log_and_set_exception

    def _guarded(self, execute: Callable[[Any], Any], coordinator: Any) -> Callable[[Any], Any]:
        def _execute_main(main_kwargs: Any) -> Any:
            try:
                return execute(main_kwargs)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    self._note(exc)
                    if not coordinator.done():
                        # The manager's own Ctrl-C cancel wording (str, else repr).
                        coordinator.cancel(str(exc) or repr(exc))
                raise

        return _execute_main


def _writing_once(handle_io: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a single-request download's ``_handle_io`` so a retry rewrites nothing.

    Upstream sends a download below the multipart threshold as one GetObject
    whose chunks it writes as they arrive (``ImmediatelyWriteIOGetObjectTask``),
    and a body cut mid-stream is retried from its first byte. A destination
    that can seek takes each chunk at its offset, so the retry overwrites; one
    that cannot (a stream destination, behind the deferred writer that offers
    only ``write``) gets the chunks appended, the bytes before the cut twice
    and still reported SUCCEEDED (measured: a 50-byte object cut at 3 bytes
    came out 53 bytes, exit code 0). The ranged path does not have the problem
    - its writes pass through s3transfer's ``DeferQueue``, which drops what it
    has already written - so this applies the same rule to the single request:
    chunks run on one thread, in order, each carrying its offset, and anything
    below the high-water mark is dropped or trimmed before it is written.
    """
    written = 0

    def _handle_io(download_output_manager: Any, fileobj: Any, chunk: Any, index: int) -> None:
        nonlocal written
        end = index + len(chunk)
        if end <= written:
            return
        if index < written:
            chunk = chunk[written - index :]
            index = written
        handle_io(download_output_manager, fileobj, chunk, index)
        written = end

    return _handle_io


class _SerialExecutor(_TaskGuard, NonThreadedExecutor):
    """``use_threads=False``'s executor: s3transfer's serial one, with an
    interrupt settling the transfer before the task's teardown runs.

    Under ``NonThreadedExecutor`` every task runs on the calling thread, so a
    ``KeyboardInterrupt`` (any ``BaseException`` outside ``Exception``) lands
    inside a task's main, where `_TaskGuard` settles it (measured before the
    guard: a SIGINT mid-PutObject under ``use_threads=False`` left ``mv``
    with neither the file nor the object, and ``SubmissionTask._main``
    swallowed the interrupt, so the run went on to the next item). An
    interrupt escaping a done callback (past ``_execute_main``) is noted at
    this executor's boundary the same way, and the terminal subscribers and
    those that send requests run once per item so upstream's second
    announce, re-running the uncleared list, cannot double-count or send
    twice. The subscribers whose own work decides the item's fate do not let
    one escape at all - the open route's commit, copy-props' post-copy
    writes and mv's source delete store it as the future's outcome, as
    upstream's submission task does for one that lands in its own body - and
    that outcome records CANCELLED and is handed back the same way (`_outcome`,
    `Transferrer._record_failure`). The last gap is the submission task's own
    unwind after its final task settled the item: upstream swallows an
    interrupt there without storing it, so the hook that would have stored it
    notes it too (`_TaskGuard._noting`).
    """

    def __init__(
        self, note: Callable[[BaseException], None], max_workers: int | None = None
    ) -> None:
        super().__init__(max_workers)
        self._note = note

    def submit(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        self._install(fn)
        try:
            return super().submit(fn, *args, **kwargs)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                self._note(exc)
            raise


class _ThreadedExecutor(_TaskGuard, ThreadPoolExecutor):
    """A threaded run's executor: s3transfer's default thread pool, guarded.

    A signal is only ever delivered to the main thread, so a ``BaseException``
    outside ``Exception`` raised on one of these workers comes from the
    caller's own code running there: an event handler the caller registered
    on the client, the caller's stream read or written by a task.
    Upstream lets it pass `Task.__call__` with the transfer still unsettled -
    the item then recorded SUCCEEDED, so a ``mv`` deleted its source though
    no PutObject went out and a download into a stream reported success with
    nothing written - and the pool's work item swallowed it, so the run
    returned normally (measured: ``sys.exit(3)`` in a PutObject event
    handler). `_TaskGuard` settles the item CANCELLED and notes the exception;
    one escaping a task past its main (a done callback) is noted here.
    `Transferrer.submit` / ``__exit__`` re-raise it on the calling thread, so
    it ends the operation the way it would have on that thread.
    """

    def __init__(
        self, note: Callable[[BaseException], None], max_workers: int | None = None
    ) -> None:
        super().__init__(max_workers=max_workers)
        self._note = note

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        self._install(fn)
        return super().submit(self._noting_call, fn, *args, **kwargs)

    def _noting_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                self._note(exc)
            raise


class _ReplacingOSUtils(OSUtils):
    """s3transfer's ``OSUtils`` for the classic engine, committing a download in one step.

    s3transfer puts a filename download in place with ``rename_file``, which
    on Windows removes the destination and then renames the temp file onto
    it: two downloads whose names differ only in case can interleave between
    the two steps on a case-insensitive filesystem, and one then fails with
    ``[WinError 183]``. This commit is ``os.replace``, one step. Where Windows
    refuses that, s3transfer's own commit runs as it would have, so the
    outcome and the report are aws-cli's: a destination another process
    holds open is ``[WinError 32]`` rather than the replace's ``[WinError
    5]``, and a link to a directory is removed and replaced as aws-cli
    replaces it. Elsewhere s3transfer's commit is the same rename and fails
    the same way again. The CRT engine's manager cannot take an ``OSUtils``
    (`crtsupport.create_crt_transfer_manager`).
    """

    def rename_file(self, current_filename: str, new_filename: str) -> None:
        try:
            os.replace(current_filename, new_filename)
        except OSError:
            super().rename_file(current_filename, new_filename)


class _StreamInterruptedError(RuntimeError):
    """What `_GuardedReader` raises to awscrt in place of the caller's interrupt."""


class _GuardedReader:
    """A CRT upload's body that hands the caller's interrupt back, not to awscrt.

    awscrt reads an upload body from its own event-loop thread (its
    ``InputStream`` duck-types ``read`` / ``readinto`` / ``seek``). A
    ``BaseException`` outside ``Exception`` the caller's stream raises there
    reaches awscrt's native layer as is: a ``SystemExit`` stalled the process
    for good, and a ``KeyboardInterrupt`` became the request's
    ``AWS_ERROR_UNKNOWN`` with the interrupt itself gone (measured). This view
    notes it for the calling thread (`Transferrer._note_raise`, re-raised by
    the next ``submit`` or ``__exit__``) and raises an ordinary exception in
    its place, which awscrt turns into that request's failure; it also keeps
    the interrupt (``interrupt``) for the item's completion, which records
    the item CANCELLED as every interrupted item is (`_Completion`). On the
    calling thread itself (s3transfer positioning the body at submit) the
    exception propagates unchanged. Every other attribute delegates to the
    stream.

    An ordinary exception the stream raises on awscrt's thread (a text
    stream's ``UnicodeEncodeError``, an ``OSError``) reaches awscrt as is,
    and awscrt reports the request as ``AWS_ERROR_UNKNOWN``, dropping it.
    The last one is kept in ``failure`` - awscrt handles some on its own (a
    ``BlockingIOError`` meaning "no data yet") and fails the request on the
    first it does not, so the last is the cause - and the item fails with it
    instead (`_Completion`), as it does on the classic engine.
    """

    _GUARDED = frozenset({"read", "read1", "readinto", "readinto1", "seek", "tell"})

    def __init__(self, raw: Any, note: Callable[[BaseException], None], caller: int) -> None:
        self._raw = raw
        self._note = note
        self._caller = caller
        # What the stream raised on awscrt's thread, kept for the item's
        # completion: awscrt reports only its own failure for the read.
        self.interrupt: BaseException | None = None
        self.failure: Exception | None = None

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._raw, name)
        if name not in self._GUARDED or not callable(attr):
            return attr
        guard, note, caller = self, self._note, self._caller

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return attr(*args, **kwargs)
            except BaseException as exc:
                if threading.get_ident() == caller:
                    raise
                if isinstance(exc, Exception):
                    guard.failure = exc
                    raise
                if guard.interrupt is None:
                    guard.interrupt = exc
                note(exc)
                raise _StreamInterruptedError(str(exc) or repr(exc)) from exc

        return call


class _ProvideSize:
    """Pre-populate ``TransferFuture.meta.size`` (with the etag: skips the probe).

    Classic download/copy probes are skipped only when size *and* etag are
    both provided (the default checksum-validation mode); the CRT meta lacks
    the provide hook, so there the guard is a no-op and the CRT probes
    itself.
    """

    def __init__(self, size: int) -> None:
        self._size = size

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        # The CRT manager's meta cannot take a provided size; guard like
        # aws-cli's ProvideSizeSubscriber (the CRT probes sizes itself).
        if hasattr(future.meta, "provide_transfer_size"):
            future.meta.provide_transfer_size(self._size)


class _ProvideETag:
    """Pre-populate ``TransferFuture.meta.etag`` (with size: skips the HEAD).

    Guarded on the meta exposing ``provide_object_etag``: the floor
    s3transfer and the CRT meta lack it, and the guard then quietly leaves
    s3transfer's own probe rules in force.
    """

    def __init__(self, etag: str) -> None:
        self._etag = etag

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        if hasattr(future.meta, "provide_object_etag"):
            future.meta.provide_object_etag(self._etag)


class _Progress:
    """Accumulate per-chunk deltas into absolute ``TransferProgress`` records.

    s3transfer reports each chunk as a delta, possibly from several worker
    threads at once for multipart parts; the lock makes accumulate-and-read
    atomic so two workers' snapshots cannot be delivered out of order.
    ``bytes_done`` can still step backward when s3transfer rewinds a
    mid-transfer retry with a negative delta - aws-cli's own progress sums
    those the same way. A zero-byte record is
    emitted at queue time so consumers learn the in-flight set (what feeds
    the ``N file(s) remaining`` display).
    """

    def __init__(
        self, transfer_type: TransferType, key: str, size: int | None, callback: ProgressCallback
    ) -> None:
        self._transfer_type = transfer_type
        self._key = key
        self._size = size
        self._callback = callback
        self._lock = threading.Lock()
        self._done = 0

    def _fire(self, bytes_done: int) -> None:
        self._callback(
            TransferProgress(
                transfer_type=self._transfer_type,
                compare_key=self._key,
                bytes_done=bytes_done,
                bytes_total=self._size,
            )
        )

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        self._fire(0)

    def on_progress(self, future: Any, bytes_transferred: int, **kwargs: Any) -> None:
        # Fire inside the lock: accumulate-and-deliver must be atomic, else two
        # multipart workers can deliver their snapshots out of order (a later,
        # larger `done` before an earlier one) - the ordered delivery this
        # class promises. (A retry's negative delta still steps `bytes_done`
        # backward by design; only reordering is prevented.) The callback is
        # fast (enqueue / throttled paint), and the lock is per-item, so this
        # serializes only one object's part callbacks.
        with self._lock:
            self._done += bytes_transferred
            self._fire(self._done)


class _CountBytes:
    """Sum a size-unknown item's chunk deltas so SUCCEEDED can report its bytes.

    Registered only for an item submitted without a size (a stream, a custom
    backend's deferred reader). Classic s3transfer sizes an unknown download
    with its HeadObject probe (``meta.size``) but never a non-seekable
    upload, and the CRT manager's meta carries no size; `_Completion` falls
    back to this total whenever the future resolves none. A retry's negative
    delta is summed like `_Progress` sums it, so the total ends at the bytes
    that actually landed.
    """

    def __init__(self, *, hinted: bool = False) -> None:
        self._lock = threading.Lock()
        self.total = 0
        # Whether the engine was handed a size hint for this item: its
        # meta.size is then the hint, not a count (`_Completion`).
        self.hinted = hinted

    def on_progress(self, future: Any, bytes_transferred: int, **kwargs: Any) -> None:
        with self._lock:
            self.total += bytes_transferred


class _DirectoryCreator:
    """Create the download destination's parent directory (aws-cli subscriber).

    The download's file is in the OS form (`_os_form`); ``text_path`` turns
    the names a failure reports back into the destination's text.
    """

    def __init__(self, text_path: Callable[[str], str] = _same_path) -> None:
        self._text_path = text_path

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        fileobj: str = future.meta.call_args.fileobj
        directory = os.path.dirname(fileobj)
        try:
            if not os.path.exists(directory):
                os.makedirs(directory)
        except FileExistsError:
            pass
        except OSError as exc:
            # key names the file being downloaded (a local failure's address,
            # design/exceptions.md section 2.1), as the engine's own record
            # of the item's local file does.
            name_files_as_text(exc, self._text_path)
            raise translate_os_error(
                exc,
                operation=None,
                key=self._text_path(fileobj).replace(os.sep, "/"),
                message=f"Could not create directory {self._text_path(directory)}: {exc}",
            ) from exc


class _FsyncDest:
    """Fsync a ``mv`` download's destination file before its S3 source is deleted.

    A library-only durability step, opt-in via ``LocalStorage(fsync=True)`` (off
    by default = aws-cli parity). s3transfer finalizes a filename download with a
    temp-file write and a rename and never fsyncs, so aws-cli / s3transfer
    delete the durable S3 source while the downloaded bytes may still be only in
    the page cache - a crash between the two loses the move outright. Placed
    immediately before ``_DeleteSource`` (the same slot and flip contract as
    ``_CloseFileobj`` on the open route): on a successful transfer it fsyncs the
    file and its parent directory, and any durability failure flips the settled
    future via ``set_exception`` so ``_DeleteSource`` sees the failure and skips
    the delete, leaving the S3 copy in place (``_Completion`` records ``move
    failed``). A transfer that already failed does nothing.

    The file is reopened by path - the rename left the inode unchanged, so its
    dirty pages flush regardless of which fd fsyncs them - then the *immediate*
    parent directory is fsynced to persist the rename's dirent. The directory
    fsync is POSIX-only: a directory has no fsyncable handle on Windows, where the
    file fsync alone is the durability step (NTFS journaling carries the
    metadata). A freshly created intermediate directory is not walked back to its
    own parent; the common case downloads into an existing tree.
    """

    def __init__(self, path: str, text_path: Callable[[str], str] = _same_path) -> None:
        # ``path`` is the OS form the download wrote (`_os_form`); ``text_path``
        # names it - and the files an error carries - as the destination's text.
        self._path = path
        self._text_path = text_path

    def on_done(self, future: Any, **kwargs: Any) -> None:
        if _outcome(future) is not None:
            return
        try:
            self._fsync()
        except OSError as exc:
            name_files_as_text(exc, self._text_path)
            text = self._text_path(self._path)
            future.set_exception(
                translate_os_error(
                    exc,
                    operation=None,
                    key=text.replace(os.sep, "/"),
                    message=f"Failed to persist {text} to disk: {exc}",
                )
            )

    def _fsync(self) -> None:
        # POSIX fsyncs a read-only fd; Windows implements os.fsync as
        # FlushFileBuffers, which requires write access (GENERIC_WRITE) on the
        # handle - O_RDONLY there fails every flush with EBADF/EACCES. The
        # freshly downloaded file is writable (s3transfer created it), so open
        # for write off POSIX; O_BINARY/O_NOINHERIT exist only there.
        flags = (
            os.O_RDONLY
            if os.name == "posix"
            else os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
        )
        fd = os.open(self._path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if os.name != "posix":
            return
        directory = os.path.dirname(self._path) or os.curdir
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


class _KeepRenameFailure:
    """Let a CRT download's failed rename settle its item.

    s3transfer's CRT manager downloads into a temporary file and renames it
    into place from a done handler that runs before the subscribers
    (``RenameTempFileHandler``). A rename that fails - a directory already at
    the destination, a name too long to create, on Windows a file another
    process holds open - is handed to the coordinator's ``set_exception``,
    which ignores it once the transfer is done: the item read as a success, a
    ``mv`` deleted its source, and nothing was written (aws-cli's outcome as
    well, measured). Installed when the transfer is queued, this makes that
    late ``set_exception`` an override, so every subscriber after it - the
    stamp, the source delete, the completion - sees the failure. Guarded on
    the attributes: an upstream that reshapes its coordinator keeps its own
    behavior.
    """

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        coordinator = getattr(future, "_coordinator", None)
        set_exception = getattr(coordinator, "set_exception", None)
        if coordinator is None or set_exception is None:
            return

        def set_exception_overriding(exception: BaseException, override: bool = False) -> None:
            set_exception(exception, override=True)

        coordinator.set_exception = set_exception_overriding


class _StampMtime:
    """Stamp the source LastModified onto a successfully downloaded file.

    aws-cli's ``ProvideLastModifiedTimeSubscriber`` slot: registered *before*
    ``_DeleteSource``, so on ``mv`` the mtime is already stamped when the
    source delete runs - a delete failure (``move failed``, the file stays)
    still leaves the mtime aws-shaped, and the opt-in ``_FsyncDest`` barrier
    flushes the final metadata. The stamp itself (`Transferrer._stamp_mtime`)
    never raises - a failure becomes a WARNED record - so this cannot flip
    the settled future.
    """

    def __init__(self, item: TransferItem, stamp: Callable[[TransferItem], None]) -> None:
        self._item = item
        self._stamp = stamp

    def on_done(self, future: Any, **kwargs: Any) -> None:
        if _outcome(future) is not None:
            return
        self._stamp(self._item)


class _PostTransferWrite:
    """An ``on_done`` subscriber that writes after the transfer, its failure the item's outcome.

    mv's source delete (`_DeleteSource`) and copy-props' tagging and
    annotation writes (`_SetTags`, `_SetAnnotations`); aws-cli's
    counterparts are ``OnDoneFilteredSubscriber`` subclasses, which have
    neither rule below. A subclass's `_settle` does the work, and:

    - it runs once per item: an interrupt escaping a later done callback on
      the serial executor leaves upstream's callback list uncleared, and its
      second announce re-runs the whole list (`_SerialExecutor`). A repeat
      returns before the ``try``, and the flag is set inside it, so an
      interrupt landing on the flag is stored too;
    - whatever it raises - an interrupt included (``use_threads=False`` runs
      it on the calling thread), anywhere past the flag - settles the item
      here instead of escaping into the engine's callback loop: stored as
      the future's outcome, it becomes CANCELLED (an interrupt) or FAILED,
      an interrupt is re-raised to the caller (`_outcome`), and a second
      announce cannot find the flag set and record a success.

    `_CloseFileobj` also stores its failure, but has no once flag.
    """

    _ran: bool = False

    def on_done(self, future: Any, **kwargs: Any) -> None:
        if self._ran:
            return
        try:
            self._ran = True
            self._settle(future)
        except BaseException as exc:
            future.set_exception(exc)

    def _settle(self, future: Any) -> None:
        raise NotImplementedError


class _DeleteSource(_PostTransferWrite):
    """Delete the transfer's source after it succeeds (mv; aws-cli port).

    Sits ahead of ``_Completion`` - the aws-cli slot between the route extras
    and the Done recorder, with only the case-conflict gate's
    ``_CaseConflictCleanup`` allowed in between (aws-cli's own order) - so a
    deletion failure can flip the already-settled future via
    ``set_exception`` (s3transfer accepts the post-done override) and
    ``_Completion`` then records aws-cli's ``move failed`` outcome. A failed
    transfer leaves the source untouched. A delete cut short by an interrupt
    is stored too: whether it reached the service is unknown, so the item
    must not be recorded as moved.
    """

    def __init__(self, delete: Callable[[], Any], *, capture: bool = False) -> None:
        self._delete = delete
        self._capture = capture

    def _settle(self, future: Any) -> None:
        if _outcome(future) is not None:
            return
        response = self._delete()
        # mv's S3 source removal returns a DeleteObject response; stash it
        # (minus ResponseMetadata) for _Completion to surface under
        # extra_info["delete"]. A local source delete returns None (nothing
        # to capture); a custom backend may return its own Mapping response.
        if self._capture and response is not None:
            future.meta.user_context[_DELETE_RESPONSE_KEY] = strip_response_metadata(response)


class _CloseFileobj:
    """Close the custom-backend fileobj an ``open``-routed item carries.

    The transfer owns every fileobj a ``Storage.open`` hands it (the file
    protocol the open route relies on): a real backend's ``close`` releases a
    reader or flushes a writer's buffered writes (``Storage.open``'s contract),
    while a caller-supplied stream (``IOStorage``, ``TextIOStorage``) hands back
    a close-suppressing view, so this is a harmless flush there. Sits before ``_DeleteSource`` /
    ``_Completion`` so a writer's ``close`` (flush) failure flips the settled
    future to a failure - and, for ``mv``, leaves the source in place (``_DeleteSource``
    then sees the failure and skips its delete). A transfer that already failed
    still releases the resource, preferring the fileobj's ``discard`` when it
    has one (the open route's deferred handles: close only what was opened,
    so a failed or cancelled item leaves the backend untouched instead of
    committing an empty object) - and never lets a release error overwrite
    the original failure.
    """

    def __init__(self, fileobj: Any) -> None:
        self._fileobj = fileobj

    def on_done(self, future: Any, **kwargs: Any) -> None:
        if _outcome(future) is not None:
            release = getattr(self._fileobj, "discard", None)
            try:
                release() if release is not None else self._fileobj.close()
            except Exception:
                pass
            return
        try:
            self._fileobj.close()
        except BaseException as exc:
            # An interrupt included (use_threads=False runs this on the calling
            # thread): a writer's commit that was cut short committed nothing
            # the run can vouch for. Stored as the outcome, it keeps
            # `_DeleteSource` from deleting a mv source and makes the item
            # CANCELLED instead of SUCCEEDED; the interrupt itself is
            # re-raised to the caller (`_outcome`).
            future.set_exception(exc)


class _CaseConflictCleanup:
    """Drop an admitted download's lower-cased key from the gate's in-flight set.

    aws-cli's ``CaseConflictCleanupSubscriber``: the ``--case-conflict`` gate adds
    a key when it admits a download and this removes it on the transfer's terminal
    (success *or* failure, like ``on_done``), so the set tracks only downloads
    still in flight. Without it the set keeps every key ever admitted, and a later
    item whose name differs only by case is wrongly judged to conflict with an
    already-finished download (skipped, or - in ``error`` mode - a spurious
    failure) instead of being let through as aws-cli does. Detecting an in-flight twin
    relies on the submit loop running ahead of completions (a non-blocking,
    threaded manager - aws-cli runs at ``max_concurrent_requests=1`` and still detects;
    a fully synchronous NonThreadedExecutor completes each item before the next is
    judged, so the set would always be empty).
    """

    def __init__(self, cleanup: Callable[[], None]) -> None:
        self._cleanup = cleanup

    def on_done(self, future: Any, **kwargs: Any) -> None:
        self._cleanup()


class _Completion:
    """Bridge the transfer future's outcome into the rollup and ``on_result``.

    ``on_done`` fires after the future's done event is set, so ``result()``
    here is non-blocking - it returns or raises the stored outcome. It is the
    last outcome subscriber, immediately before `_ForgetFuture`, so copy-props'
    post-copy tagging (which may flip the future to an exception) has already
    settled. The outcome sinks (`Transferrer`'s rollup recorders) are injected
    at submit time.
    """

    def __init__(
        self,
        item: TransferItem,
        *,
        on_success: Callable[[TransferItem, int | None, str | None, dict[str, Any] | None], None],
        on_failure: Callable[[TransferItem, BaseException, bool | None], None],
        counted: _CountBytes | None = None,
        hinted: bool = False,
    ) -> None:
        self._item = item
        self._on_success = on_success
        self._on_failure = on_failure
        self._counted = counted
        self._hinted = hinted
        self._settled = False
        # A CRT upload's stream guard (`_GuardedReader`), set at submit.
        self.stream_guard: _GuardedReader | None = None

    def on_done(self, future: Any, **kwargs: Any) -> None:
        # Once per item: an interrupt escaping `on_result` on the serial
        # executor leaves upstream's callback list uncleared, and its second
        # announce re-runs the whole list (`_SerialExecutor`); the item must
        # not be counted or reported twice.
        if self._settled:
            return
        self._settled = True
        exc = _outcome(future)
        if exc is not None:
            guard = self.stream_guard
            if guard is not None and guard.interrupt is not None:
                # The stream raised an interrupt on awscrt's thread, and awscrt
                # failed the request with its own error in its place: the
                # item's outcome is the interrupt, CANCELLED like every
                # interrupted item (`Transferrer._record_failure`).
                exc = guard.interrupt
            elif (
                guard is not None
                and guard.failure is not None
                and crt_error_name(exc) == "AWS_ERROR_UNKNOWN"
            ):
                # awscrt reported the read that raised as its own unknown
                # error and dropped the exception: the item fails with what
                # the stream raised, as on the classic engine. Any other
                # outcome (a cancellation, a network failure) stays awscrt's.
                exc = guard.failure
            self._on_failure(self._item, exc, _future_revoked(future))
            return
        # Classic s3transfer resolves an unknown-size download's size on the
        # future (its HeadObject probe); a non-seekable upload gets none, and
        # the CRT manager's meta has no size at all - there the item's own
        # byte counter (`_CountBytes`) is what SUCCEEDED reports, as it is
        # where meta.size is the hint the engine was handed (``hinted``), not
        # a count. meta.etag carries the affected object's ETag on a copy /
        # download (None on an upload); the user_context slot carries mv's
        # source-delete response (capture_response).
        size = getattr(future.meta, "size", None)
        if self._counted is not None and (size is None or self._hinted):
            size = self._counted.total
        self._on_success(
            self._item,
            size,
            getattr(future.meta, "etag", None),
            future.meta.user_context.get(_DELETE_RESPONSE_KEY),
        )


class _ForgetFuture:
    """Remove a settled top-level future from cancellation tracking."""

    def __init__(self, forget: Callable[[Any], None]) -> None:
        self._forget = forget

    def on_done(self, future: Any, **kwargs: Any) -> None:
        self._forget(future)


def _source_request(item: TransferItem, operation: str) -> contextlib.AbstractContextManager[None]:
    """`s3_request` for a copy-props read of the copy's source, carrying its bucket/key.

    Translated here, the failure names the object it came from: a raw
    exception from a source-side read would reach
    `Transferrer._record_failure`, which attributes it to the copy's
    destination.
    """
    return s3_request(operation=operation, bucket=item.src_bucket, key=item.src_key)


class _ReplaceMetadataDirective:
    """``copy-props none``: propagate no metadata (directive REPLACE)."""

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        future.meta.call_args.extra_args.setdefault("MetadataDirective", "REPLACE")


class _ReplaceTaggingDirective:
    """``copy-props none`` / ``metadata-directive``: propagate no tags."""

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        future.meta.call_args.extra_args.setdefault("TaggingDirective", "REPLACE")


class _ExcludeAnnotationDirective:
    """Every ``copy-props`` mode short of ``all``: propagate no annotations
    (aws-cli's ExcludeAnnotationDirectiveSubscriber).

    A single-part CopyObject carries S3 object annotations by default (the
    server-side ``AnnotationDirective`` default is COPY), so an explicit
    EXCLUDE is sent. Two capability guards adapt what aws-cli does
    unconditionally (its bundled SDK always knows the parameter):

    - a botocore whose CopyObject lacks ``AnnotationDirective`` cannot send
      the parameter at all - the injection is skipped silently and copies
      behave like pre-annotations aws-cli (feature-level degradation,
      docs/compatibility.md);
    - on a multipart copy, an s3transfer without the directive in its create
      blacklist would forward it to CreateMultipartUpload (no such member)
      and fail - the multipart path does not carry annotations anyway, so the
      injection is skipped there too.
    """

    def __init__(self, *, client: Any, multipart: bool) -> None:
        self._client = client
        self._multipart = multipart

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        if not _copy_annotations_param_supported(self._client):
            return
        if self._multipart and not _annotation_directive_blacklisted():
            return
        future.meta.call_args.extra_args["AnnotationDirective"] = "EXCLUDE"


class _CopyCompletions:
    """The copy manager's client, keeping each CompleteMultipartUpload response.

    `copy_props=ALL`'s multipart annotation writes pin the new object by the
    ETag and VersionId of the CompleteMultipartUpload that created it (aws-cli's
    subscriber reads them off its fork's future result), and upstream
    s3transfer discards that response. So the classic manager of such a run is
    handed this stand-in: the one call is delegated and its response kept per
    ``(Bucket, Key)`` until `_SetAnnotations` takes it; every other attribute
    is the real client's. Nothing is registered on the client's events, so a
    client the caller shares is left as it was.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._responses: dict[tuple[Any, Any], Mapping[str, Any]] = {}
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def complete_multipart_upload(self, **kwargs: Any) -> Any:
        response = self._client.complete_multipart_upload(**kwargs)
        with self._lock:
            self._responses[(kwargs.get("Bucket"), kwargs.get("Key"))] = response
        return response

    def take(self, bucket: str, key: str) -> Mapping[str, Any] | None:
        """Hand over (and forget) the response of ``bucket`` / ``key``'s completion."""
        with self._lock:
            return self._responses.pop((bucket, key), None)


class _SetAnnotations(_PostTransferWrite):
    """`copy-props all`: carry S3 object annotations onto a multipart copy
    (aws-cli subscriber port).

    A single-part CopyObject carries annotations server-side (the directive
    default is COPY), so nothing is sent - aws-cli's wire behavior. A
    multipart copy carries none, so they are read from the source and written
    onto the destination after the copy completes, from this subscriber's
    ``on_done`` - aws-cli's ``SetAnnotationsSubscriber``. Registered after
    `_SetTags`, it sees the outcome the post-copy tagging write left: a
    tagging failure has already settled the future, so nothing is written -
    aws-cli's order and request set, except where the rollback delete fails
    too: aws-cli's future is then left a success and it writes the
    annotations, while here the item has failed and none is written.

    The reads follow ``annotation_copy_mode``: the memory and tempfile modes
    list every page and read every payload in ``on_queued``, before the
    destination exists (aws-cli's timing - a source read failure leaves no
    destination); the library-only ``DEFERRED`` reads them in ``on_done``
    instead, after the copy, keeping a source read failure's destination.
    Source ``VersionId`` and request-payer parameters are pinned on the
    reads. Each write is pinned to the new object by the
    CompleteMultipartUpload response's ETag (``ObjectIfMatch``) and
    ``VersionId``, as aws-cli pins it - kept for it by `_CopyCompletions`,
    since s3transfer discards that response. A write that
    fails does not stop the others, and any failure settles the future with
    an `_AnnotationCopyError` (aws-cli's ``AnnotationCopyError``), the
    destination kept. An interrupt landing in a deferred read or a write
    stops there and is stored as the item's outcome, as `_SetTags` stores
    one, and the subscriber runs once per item, so upstream's second announce
    (`_SerialExecutor`) neither re-reads the source nor re-sends the writes.
    """

    def __init__(
        self,
        item: TransferItem,
        *,
        source_client: Any,
        dest_client: Any,
        completions: _CopyCompletions,
        multipart: bool,
        mode: AnnotationCopyMode,
        options: TransferOptions,
        temp_dir: str | os.PathLike[str] | None,
        operation: str,
    ) -> None:
        self._item = item
        self._source_client = source_client
        self._dest_client = dest_client
        self._completions = completions
        self._multipart = multipart
        self._mode = mode
        self._options = options
        self._temp_dir = temp_dir
        self._operation = operation
        self._store: _MemoryAnnotationStore | _TempfileAnnotationStore | None = None

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        if self._multipart and self._mode is not AnnotationCopyMode.DEFERRED:
            self._read_source()

    def on_done(self, future: Any, **kwargs: Any) -> None:
        # Release the store whatever happened, after any outcome is stored.
        try:
            super().on_done(future, **kwargs)
        finally:
            self.close()

    def _settle(self, future: Any) -> None:
        if not self._multipart:
            return
        bucket = future.meta.call_args.bucket
        key = future.meta.call_args.key
        completed = self._completions.take(bucket, key)
        if _outcome(future) is not None:
            return  # the copy (or the tagging write before this) failed
        if self._store is None:
            # DEFERRED: a failed read is stored as the outcome too.
            self._read_source()
        self._write(future, bucket, key, completed or {})

    def _read_source(self) -> None:
        """Read every source annotation into the selected store.

        List all pages and collect every name before fetching payloads,
        aws-cli's order. A source past S3's limits (more than
        ``_MAX_ANNOTATIONS`` names, a payload over ``_MAX_ANNOTATION_PAYLOAD``)
        fails the item there, so a faulty server cannot grow the store without
        bound. Any failure closes and discards the partial store before the
        exception propagates.
        """
        if self._mode is AnnotationCopyMode.PRELOAD_TEMPFILE:
            store: _MemoryAnnotationStore | _TempfileAnnotationStore = _TempfileAnnotationStore(
                self._temp_dir
            )
        else:
            store = _MemoryAnnotationStore()
        self._store = store
        list_params = requestparams.map_list_object_annotations_params(self._options)
        get_params = requestparams.map_get_object_annotation_params(self._options)
        version_id = self._item.head.get("VersionId") if self._item.head is not None else None
        if version_id is not None:
            list_params["VersionId"] = version_id
            get_params["VersionId"] = version_id
        try:
            paginator = self._source_client.get_paginator("list_object_annotations")
            pages = iter(
                paginator.paginate(
                    Bucket=self._item.src_bucket,
                    Key=self._item.src_key,
                    **list_params,
                )
            )
            names: list[str] = []
            for page in request_pages(
                pages,
                operation=self._operation,
                bucket=self._item.src_bucket,
                key=self._item.src_key,
            ):
                names.extend(
                    annotation["AnnotationName"] for annotation in page.get("Annotations", [])
                )
                if len(names) > _MAX_ANNOTATIONS:
                    raise Boto3S3Error(
                        f"The source lists more than {_MAX_ANNOTATIONS} annotations",
                        operation=self._operation,
                        bucket=self._item.src_bucket,
                        key=self._item.src_key,
                    )
            for name in names:
                with _source_request(self._item, self._operation):
                    response = self._source_client.get_object_annotation(
                        Bucket=self._item.src_bucket,
                        Key=self._item.src_key,
                        AnnotationName=name,
                        **get_params,
                    )
                body = response["AnnotationPayload"]
                with _source_request(self._item, self._operation):
                    payload = body.read(_MAX_ANNOTATION_PAYLOAD + 1)
                    if len(payload) <= _MAX_ANNOTATION_PAYLOAD:
                        # botocore checks the body's length and checksum only on
                        # the read that finds its end, which a sized read that
                        # takes the whole payload never is.
                        payload += body.read(1)
                if len(payload) > _MAX_ANNOTATION_PAYLOAD:
                    raise Boto3S3Error(
                        f"The source's annotation {name!r} is larger than 1 MiB",
                        operation=self._operation,
                        bucket=self._item.src_bucket,
                        key=self._item.src_key,
                    )
                store.add(name, payload)
        except BaseException:
            self.close()
            raise

    def _write(self, future: Any, bucket: str, key: str, completed: Mapping[str, Any]) -> None:
        """Write the staged annotations onto the destination, aws-cli's loop."""
        store = self._store
        assert store is not None
        params = requestparams.map_put_object_annotation_params(self._options)
        etag = completed.get("ETag")
        version_id = completed.get("VersionId")
        if etag is not None:
            params["ObjectIfMatch"] = etag
        if version_id is not None:
            params["VersionId"] = version_id
        written: list[str] = []
        failed: list[tuple[str, str]] = []
        for name in store.names():
            try:
                self._dest_client.put_object_annotation(
                    Bucket=bucket,
                    Key=key,
                    AnnotationName=name,
                    AnnotationPayload=store.read(name),
                    **params,
                )
                written.append(name)
            except Exception as exc:
                failed.append((name, str(exc)))
            except BaseException as exc:
                # An interrupt: the remaining writes are not sent, and it is
                # stored as the outcome in place of any write failure so far.
                future.set_exception(exc)
                return
        if failed:
            future.set_exception(_AnnotationCopyError(bucket, key, written, failed))

    def close(self) -> None:
        store, self._store = self._store, None
        if store is not None:
            store.close()


class _AnnotationCopyError(Exception):
    """A completed multipart copy that could not carry every annotation.

    aws-cli's ``AnnotationCopyError``: names written join with ``", "`` and
    collapse to ``(none)`` when the first write already failed, failures join
    with ``"; "`` as ``name: message``, both in the source listing's order.
    """

    def __init__(
        self, bucket: str, key: str, written: list[str], failed: list[tuple[str, str]]
    ) -> None:
        written_names = ", ".join(written) or "(none)"
        failed_descriptions = "; ".join(f"{name}: {message}" for name, message in failed)
        super().__init__(
            f"Failed to copy all annotations to s3://{bucket}/{key}. "
            f"The object was copied successfully and was not deleted. "
            f"Annotations written: {written_names}. "
            f"Annotations that failed: {failed_descriptions}."
        )


class _MemoryAnnotationStore:
    """Per-copy annotation payloads retained in memory until transfer completion."""

    def __init__(self) -> None:
        self._payloads: dict[str, bytes] = {}

    def add(self, name: str, payload: bytes) -> None:
        self._payloads[name] = payload

    def names(self) -> list[str]:
        return list(self._payloads)

    def read(self, name: str) -> bytes:
        return self._payloads[name]

    def close(self) -> None:
        self._payloads.clear()


class _TempfileAnnotationStore:
    """Per-copy payloads packed into one auto-deleting temporary file."""

    def __init__(self, directory: str | os.PathLike[str] | None) -> None:
        self._file = tempfile.TemporaryFile(mode="w+b", dir=directory)
        self._payloads: dict[str, tuple[int, int]] = {}

    def add(self, name: str, payload: bytes) -> None:
        self._file.seek(0, os.SEEK_END)
        offset = self._file.tell()
        self._file.write(payload)
        self._payloads[name] = offset, len(payload)

    def names(self) -> list[str]:
        return list(self._payloads)

    def read(self, name: str) -> bytes:
        offset, length = self._payloads[name]
        self._file.seek(offset)
        return self._file.read(length)

    def close(self) -> None:
        self._payloads.clear()
        self._file.close()


class _SetMetadataDirectiveProps:
    """Carry source metadata into a multipart copy (aws-cli subscriber port).

    A single-part CopyObject inherits metadata via S3's default
    ``MetadataDirective=COPY``; the multipart path (CreateMultipartUpload +
    UploadPartCopy) does not honor the directive, so the seven COPY-carried
    properties are read from the source HeadObject - the cached single-source
    response when present - and injected into the request. When the caller
    already replaced *some* properties explicitly, the remaining ones are
    still source-injected and the directive flips to REPLACE (aws-cli rule),
    on the single-part path too.

    Unlike aws-cli's subscriber, the directive is set to REPLACE on *every*
    injection, not only the explicit-props case. Upstream s3transfer >= 0.19
    grew its own multipart metadata preservation that strips these seven
    properties from CreateMultipartUpload unless ``MetadataDirective`` is
    REPLACE (and its head-based re-preserve never runs here because the
    engine pre-provides size and ETag); always sending REPLACE keeps the
    injected properties alive there. On older s3transfer the directive is
    simply dropped from the create call (it sits in
    ``CREATE_MULTIPART_ARGS_BLACKLIST`` in every supported version), so the
    wire request is unchanged. A caller-supplied directive is never
    overwritten - though in practice none can be present, because the engine
    only attaches copy-props subscribers when ``metadata_directive`` is unset
    (the aws-cli gate) and ``_auto_populate_metadata_directive`` only ever
    seeds REPLACE.
    """

    def __init__(
        self,
        item: TransferItem,
        *,
        source_client: Any,
        multipart: bool,
        head_params: dict[str, Any],
        operation: str,
    ) -> None:
        self._item = item
        self._source_client = source_client
        self._multipart = multipart
        self._head_params = head_params
        self._operation = operation

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        extra_args: dict[str, Any] = future.meta.call_args.extra_args
        has_explicit = any(prop in extra_args for prop in _METADATA_DIRECTIVE_PROPS)
        if not self._multipart and not has_explicit:
            return
        head = self._head_source()
        extra_args.setdefault("MetadataDirective", "REPLACE")
        for prop in _METADATA_DIRECTIVE_PROPS:
            if prop not in extra_args and prop in head:
                extra_args[prop] = head[prop]

    def _head_source(self) -> Mapping[str, Any]:
        if self._item.head is not None:
            return self._item.head
        with _source_request(self._item, self._operation):
            return self._source_client.head_object(
                Bucket=self._item.src_bucket, Key=self._item.src_key, **self._head_params
            )


def _allow_inline_mpu_tagging() -> None:
    """Realign s3transfer's create-multipart blacklist with aws-cli's.

    aws-cli's bundled s3transfer keeps a plain ``Tagging`` off its copy
    ``CREATE_MULTIPART_ARGS_BLACKLIST``, so `_SetTags`' inline small-tag
    header rides CreateMultipartUpload - the destination is created with its
    tags atomically, no separate write. Upstream s3transfer 0.19 blacklists
    the arg (its own ``TaggingDirective``-driven tag copy replaced the
    pass-through - a path `_SetTags` does not use because it has no
    destination rollback when the tagging write fails), which would silently
    strip the header and force every small tag set through the post-copy
    PutObjectTagging fallback: a wire divergence, and a failure-timing one
    (aws-cli fails at create and keeps the source where tagging is denied).
    Removed EAFP-style: the table is process-shared, so two threads building
    their first manager concurrently could both pass a membership check and
    the loser's ``remove`` would raise; catching ``ValueError`` instead also
    keeps this a no-op on older s3transfer that never blacklisted it.
    """
    try:
        CopySubmissionTask.CREATE_MULTIPART_ARGS_BLACKLIST.remove("Tagging")
    except ValueError:
        pass


class _SetTags(_PostTransferWrite):
    """Carry source tags into a multipart copy (aws-cli subscriber port).

    Single-part copies inherit tags via ``TaggingDirective=COPY``; multipart
    copies read GetObjectTagging from the source and either inline the
    percent-encoded set in the ``Tagging`` header on CreateMultipartUpload
    (<= ~2 KiB - `_allow_inline_mpu_tagging` keeps the header off upstream's
    create blacklist, like aws-cli's bundled table) or apply it with a
    post-copy PutObjectTagging - rolling the destination object back
    with a delete when that tagging write fails, then surfacing the tagging
    failure on the transfer future. A rollback delete that fails too leaves
    the destination in place without its tags, and the item still fails with
    the tagging error (`_rollback`); aws-cli lets that delete error escape its
    done callback before the future is settled, so its copy reads as a
    success and a ``mv`` deletes its source - not copied (a failure is never
    reported as a success).

    An interrupt landing in either request (``use_threads=False`` runs this
    on the calling thread) is stored as the item's outcome rather than let
    escape, as `_DeleteSource` stores one: the item records CANCELLED, a
    ``mv`` keeps its source, the interrupt is re-raised to the caller, and no
    rollback is sent after it - whether the write reached the service is
    unknown. It runs once per item, so upstream's second announce after an
    interrupt escaping a later done callback (`_SerialExecutor`) does not
    send the tagging write again, nor record a success for an untagged copy.
    """

    def __init__(
        self,
        item: TransferItem,
        *,
        source_client: Any,
        dest_client: Any,
        multipart: bool,
        options: TransferOptions,
        operation: str,
    ) -> None:
        self._item = item
        self._source_client = source_client
        self._dest_client = dest_client
        self._multipart = multipart
        self._options = options
        self._operation = operation

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        extra_args: dict[str, Any] = future.meta.call_args.extra_args
        if "Tagging" in extra_args or "TaggingDirective" in extra_args:
            return
        if not self._multipart:
            return
        tag_set = self._get_source_tags()
        if not tag_set:
            return
        header = "&".join(
            f"{quote(tag['Key'], safe='')}={quote(tag['Value'], safe='')}" for tag in tag_set
        )
        fits_inline = len(header.encode("utf-8")) <= _MAX_TAGGING_HEADER_SIZE
        if fits_inline:
            extra_args["Tagging"] = header
        else:
            future.meta.user_context[_POST_TAGGING_KEY] = tag_set

    def _settle(self, future: Any) -> None:
        tag_set = future.meta.user_context.get(_POST_TAGGING_KEY)
        if not tag_set:
            return
        if _outcome(future) is not None:
            return  # the copy itself failed; nothing to tag or roll back
        bucket = future.meta.call_args.bucket
        key = future.meta.call_args.key
        try:
            self._dest_client.put_object_tagging(
                Bucket=bucket,
                Key=key,
                Tagging={"TagSet": tag_set},
                **requestparams.map_put_object_tagging_params(self._options),
            )
        except Exception as exc:
            # A failed write rolls back; an interrupt in the write sends no
            # rollback and, like one while a failed write is handled, is
            # stored as the outcome (`_PostTransferWrite.on_done`).
            interrupt = self._rollback(bucket, key)
            future.set_exception(exc if interrupt is None else interrupt)

    def _get_source_tags(self) -> list[dict[str, str]]:
        with _source_request(self._item, self._operation):
            response = self._source_client.get_object_tagging(
                Bucket=self._item.src_bucket,
                Key=self._item.src_key,
                **requestparams.map_get_object_tagging_params(self._options),
            )
        return response["TagSet"]

    def _rollback(self, bucket: str, key: str) -> BaseException | None:
        """Delete the destination whose tagging write failed, best-effort.

        A delete that fails is logged at debug level and leaves the
        destination untagged; the item still fails with the tagging error.
        An interrupt that cuts the delete short is returned, to be stored as
        the item's outcome in place of that error.
        """
        try:
            self._dest_client.delete_object(
                Bucket=bucket,
                Key=key,
                **requestparams.map_delete_object_params(self._options),
            )
        except Exception:
            logger.debug(
                "deleting s3://%s/%s after its tagging write failed also failed",
                bucket,
                key,
                exc_info=True,
            )
        except BaseException as exc:
            return exc
        return None


__all__ = [
    "TransferItem",
    "Transferrer",
    "Warner",
]
