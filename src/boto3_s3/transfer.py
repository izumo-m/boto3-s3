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
``S3Deleter`` contract),
lock-guarded rollup counters for ``BatchError``, byte progress to
``on_progress``.

Engine choices (parity-driven):

- The engine follows ``TransferConfig.preferred_transfer_client`` with
  boto3's own semantics (``'auto'`` default; ``'auto'``/``'crt'`` resolve
  through ``crtsupport``, docs/crt.md): the classic
  ``s3transfer.manager.TransferManager`` unless the CRT manager is selected,
  and unconditionally classic for a copy run - the CRT manager has no copy,
  the same rule boto3 and aws-cli apply to s3->s3. The public
  ``transfer_config`` type is ``boto3_s3.transferconfig.TransferConfig``
  (boto3's subclass plus CRT tuning fields; defaults match aws-cli: 8 MiB
  threshold/chunk, 10-way concurrency), and classic honors
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
  ``copy_props=ALL`` carries them - riding
  s3transfer >= 0.19's native write path on a multipart copy, the preload
  `AnnotationCopyMode`s staging the source payloads up front and ``DEFERRED``
  letting s3transfer read them post-copy.
"""

from __future__ import annotations

import errno
import io
import logging
import mimetypes
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from s3transfer.copies import CopySubmissionTask
from s3transfer.exceptions import CancelledError as S3TransferCancelledError
from s3transfer.futures import NonThreadedExecutor
from s3transfer.manager import TransferConfig as S3TransferConfig
from s3transfer.manager import TransferManager
from s3transfer.upload import UploadSubmissionTask

from boto3_s3 import crtsupport, requestparams
from boto3_s3.exceptions import (
    AccessDeniedError,
    Boto3S3Error,
    CancelledError,
    ConfigurationError,
    ValidationError,
)
from boto3_s3.localstorage import LocalStorage, translate_os_error
from boto3_s3.s3storage import s3_errors, translate_boto_error
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
    from boto3.session import Session
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
    ``dest_path`` are native local paths; bucket/key pairs are S3 sides.
    ``head`` carries the single-source HeadObject payload so a single-file
    copy never heads the source twice; ``mtime`` is the source LastModified a
    download stamps onto the local file; ``src_display`` / ``dest_display``
    are the rendered endpoints reported on ``OpResult``.

    ``size`` is the transfer byte count, provided to s3transfer to skip its
    size probe; a copy also reads it for the copy-props multipart decision, so
    a copy item must carry it - an under-threshold value routes metadata/tags
    down the single-part path. ``etag`` is the source object's ETag held
    unquoted (``S3Storage`` strips the surrounding quotes); the engine
    re-quotes it when it provides it to s3transfer.
    """

    compare_key: str
    size: int | None = None
    src_bucket: str | None = None
    src_key: str | None = None
    src_path: str | None = None
    # The source listing entry, set by every listing-driven route (upload,
    # download, copy, and their open-route variants); ``mv`` unlinks an upload
    # source through its logical ``/``-key, distinct from ``src_path`` (the path
    # s3transfer reads). None only on the stream routes, which list nothing.
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
    # response_checksum_validation ("when_supported"), the aws stream wire
    # shape; a "when_required" client takes s3transfer's first-chunk GET
    # probe instead.
    src_fileobj: Any = None
    dest_fileobj: Any = None
    # A download admitted by the --case-conflict gate carries the callback that
    # drops its lower-cased key from the gate's in-flight set (aws-cli's
    # CaseConflictCleanupSubscriber wiring); _submit_download fires it on the
    # transfer's terminal. None unless the gate admitted this item.
    case_conflict_cleanup: Callable[[], None] | None = None


def _is_precondition_failed(exc: BaseException) -> bool:
    """A botocore ClientError carrying S3's conditional-write rejection."""
    response: Any = getattr(exc, "response", None)
    try:
        return bool(response["Error"]["Code"] == "PreconditionFailed")
    except (TypeError, KeyError):
        return False


def _is_cancellation(exc: BaseException) -> bool:
    """A future outcome meaning "revoked", not "failed".

    Classic s3transfer settles cancelled futures with its own
    ``CancelledError`` (``FatalError`` on aws's fatal path is a subclass); the
    CRT data plane surfaces awscrt's ``AWS_ERROR_S3_CANCELED`` instead, so
    that is matched by the error's ``name`` without importing awscrt. The
    library's own `CancelledError` is included for symmetry (a subscriber
    re-raising a translated cancellation).
    """
    if isinstance(exc, (CancelledError, S3TransferCancelledError)):
        return True
    return getattr(exc, "name", None) == "AWS_ERROR_S3_CANCELED"


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
    # 1.35.16+ pins s3transfer 0.10.x). Drop this getattr guard once the
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


# Back-compat (supported floor botocore 1.31, docs/overview.md section 2):
# IfNoneMatch reached the S3 write ops only in later botocore - PutObject and
# CompleteMultipartUpload in 1.35.16, CopyObject in 1.41.0. Below those,
# --no-overwrite is rejected up front (here for the library, and in the CLI for
# rc 252) instead of failing deep in botocore with an opaque "Unknown parameter
# in input: IfNoneMatch". Drop this gate once the botocore floor reaches them.
_CONDITIONAL_WRITE_MIN_BOTOCORE = {"upload": "1.35.16", "copy": "1.41.0"}

# Uploads additionally need s3transfer's create-multipart blocklist
# (`UploadSubmissionTask.CREATE_MULTIPART_BLOCKLIST`, s3transfer 0.11.0): older
# s3transfer hands the *full* extra_args to CreateMultipartUpload, whose model
# has no IfNoneMatch -> ParamValidationError on any multipart-threshold upload.
# The combination is real, not theoretical: boto3 1.35.16+ pins s3transfer
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
    members = getattr(client.meta.service_model.operation_model(op).input_shape, "members", {})
    if "IfNoneMatch" not in members:
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


# Feature-level degradation (docs/overview.md section 2): S3 object
# annotations reached botocore's S3 model in 1.43.31 and upstream s3transfer's
# copy handling in 0.19. Both are introspected by member presence
# (version-agnostic); the versions below only name the hint in the refusal
# message for `copy_props=ALL`.
_ANNOTATIONS_MIN_BOTOCORE = "1.43.31"
_ANNOTATIONS_MIN_S3TRANSFER = "0.19.0"


def _copy_annotations_param_supported(client: S3Client) -> bool:
    """Whether the installed botocore's CopyObject can carry `AnnotationDirective`."""
    members = getattr(
        client.meta.service_model.operation_model("CopyObject").input_shape, "members", {}
    )
    return "AnnotationDirective" in members


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

    `CopyPropsMode.ALL` copies S3 object annotations: the botocore S3 model
    must know them on CopyObject, and the multipart carryover rides
    s3transfer's own `AnnotationDirective` handling (>= 0.19). Both are
    introspected directly (version-agnostic); the message names the minimum
    versions as a hint. Returns ``None`` when the mode is supported.
    """
    if not _copy_annotations_param_supported(client):
        return (
            f"copy_props=ALL requires botocore >= {_ANNOTATIONS_MIN_BOTOCORE} "
            f"(S3 object annotations); the installed botocore is {version('botocore')}."
        )
    if not _annotation_directive_blacklisted():
        return (
            f"copy_props=ALL requires s3transfer >= {_ANNOTATIONS_MIN_S3TRANSFER} "
            "(annotation carryover on multipart copies); the installed "
            f"s3transfer is {version('s3transfer')}."
        )
    return None


def _set_file_utime(path: str, timestamp: float) -> None:
    """Set a file's atime/mtime (aws-cli's ``set_file_utime``).

    A permission failure (EPERM - typically a file owned by another user) is
    re-worded the way aws words it, so the resulting warning carries
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


def _guess_content_type(path: str) -> str | None:
    """``mimetypes`` guess with aws-cli's Windows-registry guard.

    ``guess_type`` can raise ``UnicodeDecodeError`` on Windows when a
    registry MIME entry is in an undecodable encoding (bpo-9291); strict
    (IANA-only) matching is kept deliberately, like aws.
    """
    try:
        return mimetypes.guess_type(path)[0]
    except UnicodeDecodeError:
        return None


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
    to the read slot, so ``Transferrer._submit_download`` admits the source
    key only as it queues the download (``expect``): a read on a key not yet
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
    (docs/s3.md).

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
    """The op-level warning sink: a message body -> a WARNED ``OpResult``.

    A single place that turns a warning into an ``OpResult`` and reports it, so
    both a backend enumeration (``ScanOptions.on_warning``) and the transfer
    engine funnel their warnings through the same emitter instead of the walk
    reaching into the transfer engine for one. Warnings count independently of
    files (``warned > 0`` alone maps to exit code 2, aws rc model); the count is
    thread-safe because a walk worker and a transfer worker can warn at once.
    The transfer run's ``Transferrer`` builds one from its result context
    and exposes it as ``Transferrer.warner``.
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

    def warn(self, body: str, *, key: str = "") -> None:
        """Record a warning (aws-cli message body, no ``warning: `` prefix)."""
        with self._lock:
            self._warned += 1
        if self._on_result is not None:
            self._on_result(
                OpResult(
                    transfer_type=self._transfer_type,
                    compare_key=key,
                    outcome=OpOutcome.WARNED,
                    error=Boto3S3Error(body, operation=self._operation),
                    src_storage=self._src_storage,
                    dest_storage=self._dest_storage,
                )
            )


class Transferrer:
    """Submit transfer items of one kind and aggregate their outcomes.

    Use as a context manager around the submission loop: ``__exit__`` shuts
    the manager down - waiting for in-flight transfers on a clean exit, and
    **cancelling accepted work on any exception** except a graceful cancel
    (aws's manager context cancels everything still pending when a fatal
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
    the completion win; docs/opresult.md). The rollup counters are
    approximate while transfers are in flight and exact after the `with`
    block exits.

    ``client`` is the manager-owning side: the destination client for uploads
    and copies, the source client for downloads; ``source_client`` (copies
    only) serves the CopySource reads - HeadObject, GetObjectTagging, and
    s3transfer's own size probe. ``src_storage`` / ``dest_storage`` are the run's
    two resolved side ``Storage`` objects (``plan.src`` / ``plan.dest``), retained
    so a completion can resolve or report either endpoint alongside its listing
    entries. ``src_storage`` doubles as ``mv``'s upload-source delete handle: an
    upload source - a local file or a custom backend - is removed through its
    ``Storage.delete``, while a download / copy S3 source is removed with a
    DeleteObject instead (so the handle is consulted only on the upload route).

    ``is_move`` turns the run into ``mv``: every record reports
    ``TransferType.MOVE`` while ``transfer_type`` keeps routing the bytes (aws-cli
    ``operation_name`` vs ``transfer_type='move'``), and each successful
    transfer deletes its source - ``Storage.delete`` for an upload (local or
    custom backend), a per-object DeleteObject on the owning side's client
    otherwise.
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
        crt_endpoint: str | None = None,
        session: Session | None = None,
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
        # a completion can surface them with the listing entries; _src_storage is
        # also mv's upload-source delete handle (consulted only on the upload
        # route - a download / copy S3 source is removed with a DeleteObject).
        self._src_storage = src_storage
        self._dest_storage = dest_storage
        self._transfer_config = transfer_config
        self._options: TransferOptions = options if options is not None else TransferOptions()
        self._operation = operation
        # Library-side --no-overwrite gate (the CLI rejects earlier with rc 252):
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
        # The copy-props mode is interpreted once here so a bad value fails at
        # construction rather than at the first submit. None means unspecified;
        # the check is is-None, not falsy, so a present-but-empty value from an
        # untyped caller ("") hits the invalid-value error below instead of
        # silently running with the default.
        # copy_props=ALL gate: annotations need a capable SDK; every other
        # mode degrades silently on an old one (docs/transfer.md section 4).
        self._copy_props = CopyPropsMode.DEFAULT
        self._annotation_copy_mode = AnnotationCopyMode.PRELOAD_MEMORY
        if transfer_type is TransferType.COPY:
            copy_props = self._options.get("copy_props")
            if copy_props is None:
                copy_props = CopyPropsMode.DEFAULT
            try:
                self._copy_props = CopyPropsMode(copy_props)
            except ValueError as exc:
                # A bad value must not leak the enum's raw ValueError past the
                # public API (the exception model keeps every error in the
                # Boto3S3Error family, docs/exceptions.md); the CLI never gets
                # here, having validated copy_props against its choices.
                raise ValidationError(
                    f"Invalid copy_props value: {copy_props!r}", operation=operation
                ) from exc
            annotation_copy_mode = self._options.get("annotation_copy_mode")
            if annotation_copy_mode is None:
                annotation_copy_mode = AnnotationCopyMode.PRELOAD_MEMORY
            try:
                self._annotation_copy_mode = AnnotationCopyMode(annotation_copy_mode)
            except ValueError as exc:
                raise ValidationError(
                    f"Invalid annotation_copy_mode value: {annotation_copy_mode!r}",
                    operation=operation,
                ) from exc
            # An explicit metadata_directive disables the whole copy-props chain
            # (aws-cli's s3handler.CopyRequestSubmitter), so ALL never reaches the
            # annotations path there - don't refuse the combination for a feature
            # that won't run.
            if self._copy_props is CopyPropsMode.ALL and not self._options.get(
                "metadata_directive"
            ):
                reason = annotations_copy_unsupported_reason(client)
                if reason is not None:
                    raise ConfigurationError(reason, operation=operation)
        self._on_progress = on_progress
        self._on_result = on_result
        self._cancel_token = cancel_token
        self._capture_response = capture_response
        # The caller's explicit endpoint (the CLI's --endpoint-url), threaded to
        # the CRT engine so it pins a custom endpoint the host heuristic would
        # miss (a VPC interface endpoint under an AWS domain); None = heuristic.
        self._crt_endpoint = crt_endpoint
        # The caller's boto3 session (S3.session), threaded to the CRT engine
        # so its request serializer reuses the warm session instead of paying
        # a fresh one per process (crtsupport._botocore_session); None = the
        # default-session/fresh fallback there.
        self._session = session
        self._manager: Any = None
        self._capture: _ResponseCapture | None = None
        self._lock = threading.Lock()
        self._futures_lock = threading.Lock()
        self._futures: set[Any] = set()
        self._shutdown_done = threading.Event()
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
            on_result=on_result,
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

        Any exception cancels the accepted transfers, like aws's manager
        context (its ``__exit__`` cancels with ``FatalError(str(exc))`` on a
        fatal and ``CancelledError`` on Ctrl-C; measured live: a fatal
        mid-listing leaves every queued transfer unrun). The one drain-on-
        exception case is a graceful token's `CancelledError`: graceful
        cancellation is *defined* as draining accepted work (exceptions.md),
        and the watcher thread keeps a mid-drain escalation to immediate
        effective. The cancelling shutdown is delegated to the manager's own
        ``__exit__`` - aws's actual path - because s3transfer's public
        ``shutdown()`` forwards its arguments shifted (``(cancel, cancel,
        cancel_msg)``), which would cancel a pending coordinator with the
        message as the exception *class*. ``__exit__`` exists on both engines;
        the classic one cancels with aws's exception shape
        (``FatalError(str(exc))``, Ctrl-C's plain ``CancelledError``), so a
        classic ``CANCELLED`` record names the fatal that revoked it, while
        the CRT manager cancels without the message and its ``CANCELLED``
        records carry awscrt's cancellation wording instead (still classified
        via ``AWS_ERROR_S3_CANCELED``).
        """
        if self._manager is None:
            return
        graceful_drain = (
            isinstance(exc, CancelledError)
            and self._cancel_token is not None
            and self._cancel_token.mode is CancelMode.GRACEFUL
        )
        cancel = exc is not None and not graceful_drain
        watcher: threading.Thread | None = None
        if self._cancel_token is not None and not cancel:
            watcher = threading.Thread(
                target=self._watch_for_immediate_cancel,
                name="boto3-s3-cancel",
                daemon=True,
            )
            watcher.start()
        try:
            if cancel:
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
                # feeding _store forever on a longer-lived client.
                self._capture.unregister(self._client)

    def _watch_for_immediate_cancel(self) -> None:
        """Cancel tracked futures if the token escalates while shutdown drains."""
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
        with self._futures_lock:
            self._futures.add(future)
        # A non-threaded manager may finish before submit() returns and before
        # _ForgetFuture can remove it. Re-check after insertion to close that race.
        if future.done():
            self._forget_future(future)

    def _forget_future(self, future: Any) -> None:
        with self._futures_lock:
            self._futures.discard(future)

    def _cancel_futures(self) -> None:
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
        """The run's warning sink - what a walk's ``ScanOptions.on_warning`` targets."""
        return self._warner

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def first_error(self) -> Boto3S3Error | None:
        return self._first_error

    # -- non-transfer outcomes ----------------------------------------------

    def warn(self, body: str, *, key: str = "") -> None:
        """Record a warning through the shared sink (``warner``).

        A transfer that succeeds but fails its mtime stamp produces both a
        SUCCEEDED and a WARNED record, and ``warned > 0`` alone maps to exit code
        2 (aws rc model). This engine's own warnings go here; a backend walk
        reports directly through ``warner``.
        """
        self._warner.warn(body, key=key)

    def skip(self, item: TransferItem) -> None:
        """Record a silent, non-warning skip (no exit-code effect)."""
        with self._lock:
            self._skipped += 1
        self._emit(self._result(item, OpOutcome.SKIPPED))

    def notice(self, body: str, *, key: str = "") -> None:
        """Emit display-only advisory text (no counts, no exit-code effect).

        aws prints the case-conflict messages straight to stderr, bypassing
        its result recorder - they look like warnings but never count as
        them. The full text (its ``warning: `` prefix included) rides on
        ``OpResult.error``.
        """
        self._emit(
            OpResult(
                transfer_type=self._result_transfer_type,
                compare_key=key,
                outcome=OpOutcome.NOTICE,
                error=Boto3S3Error(body, operation=self._operation),
                src_storage=self._src_storage,
                dest_storage=self._dest_storage,
            )
        )

    def dryrun(self, item: TransferItem) -> None:
        """Validate per-item request parameters, then report without an API call.

        aws-cli still runs its request-parameter mapper for a dry run. This is
        observable for options such as malformed `grants`, whose validation is
        deliberately deferred until an upload or copy item reaches the pipeline.
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

        Request-parameter mapping happens here, per item - so an invalid
        ``grants`` shape surfaces as an in-flight error like aws (rc 1 via
        the fatal-error path), never as an upfront usage error.
        """
        try:
            if self._transfer_type is TransferType.UPLOAD:
                future = self._submit_upload(item)
            elif self._transfer_type is TransferType.DOWNLOAD:
                future = self._submit_download(item)
            else:
                future = self._submit_copy(item)
            self._track_future(future)
        except BaseException:
            # The item may carry a stream handle - _cp_stream's already-open
            # fileobj, or the open route's deferred reader/writer. A submit
            # that raises before the manager accepted the work - the grants
            # ValidationError from param mapping, a manager-build failure -
            # means _CloseFileobj never runs, so release here (best-effort;
            # mirrors _CloseFileobj's failure branch, discard-preferring).
            self._close_item_fileobjs(item)
            raise

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
        # A directory source is handed through to fail like aws-cli ([Errno 21]
        # Is a directory, rc 1); botocore's default checksum wrapper would
        # otherwise open it and mask the read failure as an opaque rewind error,
        # so detect it and surface the OS error directly. A stream (src_fileobj)
        # is never a directory.
        if item.src_fileobj is None and item.src_path and os.path.isdir(item.src_path):
            self._record_failure(
                item, IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), item.src_path)
            )
            return None
        extra_args = requestparams.map_put_object_params(self._options, self._operation)
        if self._options.get("guess_mime_type", True) and "ContentType" not in extra_args:
            name = item.src_path or ""
            if not name and item.src_info is not None:
                # An open-route item has no local path but its entry does have a
                # filename: guess from the source key (the destination key for a
                # single "" source - same basename). A stream item carries no
                # src_info and stays guess-free (no filename, aws parity).
                name = item.src_info.key or item.dest_key or ""
            guessed = _guess_content_type(name)
            if guessed is not None:
                extra_args["ContentType"] = guessed
        subscribers = self._common_subscribers(item)
        if item.src_fileobj is not None:
            subscribers.append(_CloseFileobj(item.src_fileobj))
        if self._is_move:
            subscribers.append(self._delete_source_subscriber(item))
        subscribers.append(self._completion(item))
        subscribers.append(_ForgetFuture(self._forget_future))
        # Manager first: a stream run builds it (and the capture) at first submit.
        manager = self._get_manager()
        if self._capture is not None:
            # Admit the key: only submitted items' responses are recorded.
            self._capture.expect(item.dest_bucket, item.dest_key)
        return manager.upload(
            fileobj=item.src_fileobj if item.src_fileobj is not None else item.src_path,
            bucket=item.dest_bucket,
            key=item.dest_key,
            extra_args=extra_args,
            subscribers=subscribers,
        )

    def _submit_download(self, item: TransferItem) -> Any:
        """Map one download and order destination, durability, and completion hooks."""
        extra_args = requestparams.map_get_object_params(self._options)
        subscribers = self._common_subscribers(item)
        if item.dest_path is not None:
            subscribers.append(_DirectoryCreator())
        if item.dest_fileobj is not None:
            subscribers.append(_CloseFileobj(item.dest_fileobj))
        if item.case_conflict_cleanup is not None:
            subscribers.append(_CaseConflictCleanup(item.case_conflict_cleanup))
        # Before the mv source delete, like aws-cli registering
        # ProvideLastModifiedTimeSubscriber ahead of DeleteSourceObjectSubscriber:
        # a failed delete then still leaves the downloaded file carrying the
        # source mtime (a later sync compares equal instead of re-downloading).
        subscribers.append(_StampMtime(item, self._stamp_mtime))
        if self._is_move:
            # Durability barrier before the source delete (LocalStorage(fsync=True),
            # a library-only opt-in; off by default = aws parity). Only a real local
            # destination path can be fsynced - a stream / custom (open-route) dest
            # owns its own durability, so item.dest_fileobj downloads are excluded.
            if (
                item.dest_path is not None
                and isinstance(self._dest_storage, LocalStorage)
                and self._dest_storage.fsync
            ):
                subscribers.append(_FsyncDest(item.dest_path))
            subscribers.append(self._delete_source_subscriber(item))
        subscribers.append(self._completion(item))
        subscribers.append(_ForgetFuture(self._forget_future))
        # Manager first: a stream run builds it (and the capture) at first submit.
        manager = self._get_manager()
        if self._capture is not None:
            # Admit the key only now: a same-run content filter may have read
            # this source through the run's own client (a SyncPair content
            # update_filter via S3Storage.open(..., "rb")) before submit, and
            # that GetObject must neither occupy the first-stored-wins read
            # slot (the transfer's own GetObject is the item's read) nor sit
            # in the store forever for a pair the run never submits.
            self._capture.expect(item.src_bucket, item.src_key)
        return manager.download(
            bucket=item.src_bucket,
            key=item.src_key,
            fileobj=item.dest_fileobj if item.dest_fileobj is not None else item.dest_path,
            extra_args=extra_args,
            subscribers=subscribers,
        )

    def _submit_copy(self, item: TransferItem) -> Any:
        """Map one S3 copy, including copy-props and optional source deletion."""
        extra_args = requestparams.map_copy_object_params(self._options, self._operation)
        subscribers = self._common_subscribers(item)
        source_client: Any = self._source_client
        if not self._options.get("metadata_directive"):
            copy_props_subscribers, source_client = self._copy_props_subscribers(item)
            subscribers.extend(copy_props_subscribers)
        if self._is_move:
            subscribers.append(self._delete_source_subscriber(item))
        subscribers.append(self._completion(item))
        subscribers.append(_ForgetFuture(self._forget_future))
        # Manager first: a stream run builds it (and the capture) at first submit.
        manager = self._get_manager()
        if self._capture is not None:
            # Admit the key: only submitted items' responses are recorded.
            self._capture.expect(item.dest_bucket, item.dest_key)
        return manager.copy(
            copy_source={"Bucket": item.src_bucket, "Key": item.src_key},
            bucket=item.dest_bucket,
            key=item.dest_key,
            extra_args=extra_args,
            subscribers=subscribers,
            source_client=source_client,
        )

    def _common_subscribers(self, item: TransferItem) -> list[Any]:
        """Build the size, ETag, and progress hooks shared by all transfer routes."""
        # Size and etag are provided for every kind, aws-cli-style: under the
        # default response_checksum_validation ("when_supported") s3transfer
        # skips its pre-transfer HeadObject probe (downloads AND copies) only
        # when both are present (a "when_required" client skips the HEAD
        # regardless, probing with a first-chunk GET). S3-sourced items always
        # carry an etag; local sources have none, and uploads never probe.
        subscribers: list[Any] = []
        if item.size is not None:
            subscribers.append(_ProvideSize(item.size))
        if item.etag:
            subscribers.append(_ProvideETag(item.etag))
        if self._on_progress is not None:
            subscribers.append(
                _Progress(
                    self._result_transfer_type, item.compare_key, item.size, self._on_progress
                )
            )
        return subscribers

    def _completion(self, item: TransferItem) -> _Completion:
        """Bind an item's terminal callbacks into one s3transfer subscriber."""
        return _Completion(
            item,
            on_success=self._record_success,
            on_failure=self._record_failure,
        )

    def _delete_source_subscriber(self, item: TransferItem) -> _DeleteSource:
        """The per-item source deletion for ``mv`` (aws-cli DeleteSource* trio).

        An upload source - a local file or a custom (open-route) backend object -
        is removed through its own ``Storage.delete(info)``, keyed by the source
        listing entry (``src_info``); the delete maps the OS / backend error into
        the library taxonomy, preserving the message aws prints (``move failed:
        ... [Errno 13] ...``). S3 sources get a single DeleteObject - on the
        manager's client for downloads, the source-side client for copies - with
        ``RequestPayer`` forwarded like every other request.
        """
        if self._transfer_type is TransferType.UPLOAD:
            src_storage = self._src_storage
            info = item.src_info
            assert src_storage is not None and info is not None
            return _DeleteSource(lambda: src_storage.delete(info), capture=self._capture_response)
        client: Any = (
            self._client if self._transfer_type is TransferType.DOWNLOAD else self._source_client
        )
        bucket = item.src_bucket
        key = item.src_key
        params = requestparams.map_delete_object_params(self._options)

        def delete_s3_source() -> Any:
            # Pre-translate to the taxonomy carrying the *source* bucket/key, as
            # S3Storage.delete and the upload path's Storage.delete already do.
            # Otherwise _record_failure re-tags the raw ClientError with the copy
            # destination (dest_bucket/dest_key), mis-attributing a source-delete
            # failure to the object that copied fine.
            with s3_errors(operation=self._operation, bucket=bucket, key=key):
                return client.delete_object(Bucket=bucket, Key=key, **params)

        return _DeleteSource(delete_s3_source, capture=self._capture_response)

    def _copy_props_subscribers(self, item: TransferItem) -> tuple[list[Any], Any]:
        """Translate the selected copy-props mode into ordered request subscribers."""
        mode = self._copy_props
        exclude = _ExcludeAnnotationDirective(
            item, client=self._client, multipart_threshold=self._multipart_threshold
        )
        if mode is CopyPropsMode.NONE:
            return (
                [_ReplaceMetadataDirective(), _ReplaceTaggingDirective(), exclude],
                self._source_client,
            )
        metadata = _SetMetadataDirectiveProps(
            item,
            source_client=self._source_client,
            multipart_threshold=self._multipart_threshold,
            head_params=requestparams.map_head_object_params_with_copy_source_sse(self._options),
            operation=self._operation,
        )
        if mode is CopyPropsMode.METADATA_DIRECTIVE:
            return [metadata, _ReplaceTaggingDirective(), exclude], self._source_client
        tags = _SetTags(
            item,
            source_client=self._source_client,
            dest_client=self._client,
            multipart_threshold=self._multipart_threshold,
            options=self._options,
            operation=self._operation,
        )
        if mode is CopyPropsMode.DEFAULT:
            return [metadata, tags, exclude], self._source_client
        # CopyPropsMode.ALL - annotations carried instead of excluded (the
        # constructor already refused the mode on an incapable SDK).
        annotations = _SetAnnotations(
            item,
            source_client=self._source_client,
            multipart_threshold=self._multipart_threshold,
            mode=self._annotation_copy_mode,
            options=self._options,
            temp_dir=getattr(self._transfer_config, "annotation_temp_dir", None),
            operation=self._operation,
        )
        return [
            metadata,
            tags,
            annotations,
        ], annotations.source_client

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
            # Transfer-time breadcrumb: names the engine actually built for
            # this run (CRTTransferManager vs the classic TransferManager),
            # after any CRT->classic fallback. Lets --debug distinguish a real
            # CRT transfer from a silent classic fallback (docs/testing.md).
            logger.debug("transfer engine: %s", type(manager).__name__)
            if self._capture_response:
                # Registered once, before the first submit; drained per item by
                # _build_extra_info; removed in __exit__ after the manager shuts
                # down (capture forces the classic engine, so it is always live).
                self._capture = _ResponseCapture()
                self._capture.register(self._client)
            self._manager = manager
        return self._manager

    def _create_crt_manager(self) -> Any | None:
        """The boto3-faithful CRT attempt; ``None`` selects the classic engine.

        ``preferred_transfer_client`` is read with boto3's defaults (no config
        = ``'auto'``); a copy run is unconditionally classic - the CRT manager
        has no copy, the same rule boto3 and aws-cli apply to s3->s3.
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
        preferred = getattr(self._transfer_config, "preferred_transfer_client", None) or "auto"
        if str(preferred).lower() == "classic":
            return None
        if not crtsupport.should_use_crt(str(preferred)):
            return None
        _allow_if_none_match()  # the CRT manager aliases the classic arg lists
        _allow_inline_mpu_tagging()  # inert for CRT (no copy path) but keeps one table
        # The explicit endpoint pin belongs to the client built from it: the
        # run's client is the route-selected one (an S3Storage may carry its
        # own), and pinning the S3-level endpoint onto a storage-supplied
        # client would dial one endpoint with another's credentials and
        # region (the classic lane just uses that client). botocore stores
        # an explicit endpoint_url verbatim on meta, so equality identifies
        # a client this S3 built; any other client falls back to the
        # host heuristic on its *own* endpoint (`_derive_endpoint`).
        endpoint = self._crt_endpoint
        if endpoint is not None and self._client.meta.endpoint_url != endpoint:
            endpoint = None
        # Deferred: absent on floor boto3 (pre-CRT); reached only on the CRT lane.
        from boto3.exceptions import InvalidCrtTransferConfigError

        try:
            return crtsupport.create_crt_transfer_manager(
                self._client,
                self._transfer_config,
                endpoint=endpoint,
                session=self._session,
            )
        except InvalidCrtTransferConfigError as exc:
            # boto3's explicit-'crt' validation (classic-only TransferConfig
            # options set): a caller-argument problem, kept inside the taxonomy
            # like the copy_props ValueError above - docs/exceptions.md carves
            # out exactly one pass-through (MissingDependencyException), and
            # this is not it.
            raise ValidationError(str(exc), operation=self._operation) from exc

    def _create_classic_manager(self) -> Any:
        """Build the classic s3transfer manager, honoring threaded execution config."""
        _allow_if_none_match()
        _allow_inline_mpu_tagging()
        config: Any = self._transfer_config
        if config is None:
            config = S3TransferConfig()
        executor_cls = None
        if not getattr(config, "use_threads", True):
            executor_cls = NonThreadedExecutor
        # TransferManager registers handlers on self._client at construction and
        # leaves them there - shutdown() never removes them. Intentional and
        # boto3-faithful (boto3's own TransferManager does the same): we do not
        # restore the client, because it may be shared and unregistering could
        # disrupt a concurrent transfer. Run parallel operations with one client
        # per thread (docs/s3.md thread-safety note).
        return TransferManager(self._client, config=config, executor_cls=executor_cls)

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
            self._on_result(result)

    def _record_success(
        self,
        item: TransferItem,
        resolved_size: int | None = None,
        etag: str | None = None,
        delete_response: dict[str, Any] | None = None,
    ) -> None:
        """Count and emit a successful terminal result with captured response data."""
        # item.size is unset for an unknown-size transfer (a streaming download
        # lets s3transfer probe the object); fall back to the size s3transfer
        # resolved on the future so SUCCEEDED reports the real byte count.
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
        if self._transfer_type in (TransferType.UPLOAD, TransferType.COPY):
            # Also drain any read recorded for this key (a content filter's
            # GetObject of the pre-overwrite destination through this client):
            # it is not the item's transfer response, and must not linger.
            self._capture.pop_read(item.dest_bucket, item.dest_key)
            return self._capture.pop_write(item.dest_bucket, item.dest_key), None
        if self._transfer_type is TransferType.DOWNLOAD:
            return None, self._capture.pop_read(item.src_bucket, item.src_key)
        return None, None

    def _record_failure(self, item: TransferItem, exc: BaseException) -> None:
        """Classify a terminal exception as a cancellation, no-overwrite skip, or failure."""
        # Failed / skipped / cancelled items surface no captured response, but
        # the entry must still leave the store (see _drain_captured).
        self._drain_captured(item)
        if _is_cancellation(exc):
            # An accepted item revoked by the engine shutdown (a fatal
            # elsewhere, an immediate cancel, Ctrl-C): CANCELLED, not FAILED -
            # nothing is wrong with the item itself, and the run's outcome is
            # the exception the operation raises. Deliberately richer than
            # aws-cli, which drops cancelled items from output and counts;
            # first_error stays reserved for real failures (BatchError's
            # diagnostic sample).
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
        if _is_precondition_failed(exc):
            # --no-overwrite's IfNoneMatch rejection: the object already
            # exists, which is the asked-for outcome - a silent skip, not a
            # failure (aws-cli DoneResultSubscriber; rc stays 0).
            with self._lock:
                self._skipped += 1
            self._emit(self._result(item, OpOutcome.SKIPPED))
            return
        error = translate_boto_error(
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
        with self._lock:
            self._failed += 1
            if self._first_error is None:
                self._first_error = error
        self._emit(self._result(item, OpOutcome.FAILED, error=error))

    def _stamp_mtime(self, item: TransferItem) -> None:
        """Stamp the source LastModified onto the downloaded file.

        Runs where the download's subscribers do - an s3transfer worker, or
        the calling thread under ``use_threads=False`` - after a successful
        download. Failure
        does not undo the transfer (the bytes are on disk): it adds a WARNED
        record with aws's wording - the broad catch is deliberate, ``os.utime``
        can raise ``OverflowError`` / ``ValueError`` on Windows for timestamps
        outside ``localtime()``'s range besides the common ``OSError``.
        """
        if item.mtime is None or item.dest_path is None:
            return
        try:
            _set_file_utime(item.dest_path, item.mtime.timestamp())
        except Exception as exc:
            self.warn(
                f"Skipping file {item.dest_path}. Successfully Downloaded {item.dest_path} "
                f"but was unable to update the last modified time. {exc}",
                key=item.compare_key,
            )


# -- subscribers --------------------------------------------------------------
#
# Plain classes, not s3transfer.subscribers.BaseSubscriber subclasses:
# s3transfer resolves callbacks by getattr (duck typing), and inheriting would
# import the SDK at module load. Method signatures follow the subscriber
# protocol: on_queued(future), on_progress(future, bytes_transferred),
# on_done(future), all with **kwargs.


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
            future.meta.provide_object_etag(f'"{self._etag}"')


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


class _DirectoryCreator:
    """Create the download destination's parent directory (aws-cli subscriber)."""

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        directory = os.path.dirname(future.meta.call_args.fileobj)
        try:
            if not os.path.exists(directory):
                os.makedirs(directory)
        except FileExistsError:
            pass
        except OSError as exc:
            raise translate_os_error(
                exc,
                operation=None,
                key=None,
                message=f"Could not create directory {directory}: {exc}",
            ) from exc


class _FsyncDest:
    """Fsync a ``mv`` download's destination file before its S3 source is deleted.

    A library-only durability step, opt-in via ``LocalStorage(fsync=True)`` (off
    by default = aws parity). s3transfer finalizes a filename download with a
    temp-file write + ``os.rename`` and never fsyncs, so aws-cli / s3transfer
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

    def __init__(self, path: str) -> None:
        self._path = path

    def on_done(self, future: Any, **kwargs: Any) -> None:
        try:
            future.result()
        except Exception:
            return
        try:
            self._fsync()
        except OSError as exc:
            future.set_exception(
                translate_os_error(
                    exc,
                    operation=None,
                    key=None,
                    message=f"Failed to persist {self._path} to disk: {exc}",
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
        try:
            future.result()
        except Exception:
            return
        self._stamp(self._item)


class _DeleteSource:
    """Delete the transfer's source after it succeeds (mv; aws-cli port).

    Sits immediately before ``_Completion`` - the aws-cli slot between the
    route extras and the Done recorder - so a deletion failure can flip the
    already-settled future via ``set_exception`` (s3transfer accepts the
    post-done override) and ``_Completion`` then records aws's ``move
    failed`` outcome. A failed transfer leaves the source untouched.
    """

    def __init__(self, delete: Callable[[], Any], *, capture: bool = False) -> None:
        self._delete = delete
        self._capture = capture

    def on_done(self, future: Any, **kwargs: Any) -> None:
        try:
            future.result()
        except Exception:
            return
        try:
            response = self._delete()
        except Exception as exc:
            future.set_exception(exc)
            return
        # mv's S3 source removal returns a DeleteObject response; stash it (minus
        # ResponseMetadata) for _Completion to surface under extra_info["delete"].
        # A local source delete returns None (nothing to capture); a custom backend
        # may return its own Mapping response.
        if self._capture and isinstance(response, Mapping):
            captured = cast("Mapping[str, Any]", response)
            future.meta.user_context[_DELETE_RESPONSE_KEY] = strip_response_metadata(captured)


class _CloseFileobj:
    """Close the custom-backend fileobj an ``open``-routed item carries.

    The transfer owns every fileobj a ``Storage.open`` hands it (the file
    protocol the open route relies on): a real backend's ``close`` releases a
    reader or flushes a writer's buffered writes (``Storage.open``'s contract),
    while a caller-supplied stream (``IOStorage``) hands back a close-suppressing
    view, so this is a harmless flush there. Sits before ``_DeleteSource`` /
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
        try:
            future.result()
        except Exception:
            release = getattr(self._fileobj, "discard", None)
            try:
                release() if release is not None else self._fileobj.close()
            except Exception:
                pass
            return
        try:
            self._fileobj.close()
        except Exception as exc:
            future.set_exception(exc)


class _CaseConflictCleanup:
    """Drop an admitted download's lower-cased key from the gate's in-flight set.

    aws-cli's ``CaseConflictCleanupSubscriber``: the ``--case-conflict`` gate adds
    a key when it admits a download and this removes it on the transfer's terminal
    (success *or* failure, like ``on_done``), so the set tracks only downloads
    still in flight. Without it the set keeps every key ever admitted, and a later
    item whose name differs only by case is wrongly judged to conflict with an
    already-finished download (skipped, or - in ``error`` mode - a spurious
    failure) instead of being let through as aws does. Detecting an in-flight twin
    relies on the submit loop running ahead of completions (a non-blocking,
    threaded manager - aws runs at ``max_concurrent_requests=1`` and still detects;
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
        on_failure: Callable[[TransferItem, BaseException], None],
    ) -> None:
        self._item = item
        self._on_success = on_success
        self._on_failure = on_failure

    def on_done(self, future: Any, **kwargs: Any) -> None:
        try:
            future.result()
        except Exception as exc:
            self._on_failure(self._item, exc)
            return
        # s3transfer resolves the transfer size on the future (a HeadObject
        # probe for an unknown-size download); pass it so _record_success can
        # report real bytes when the item carried no size. meta.etag carries the
        # affected object's ETag on a copy / download (None on an upload); the
        # user_context slot carries mv's source-delete response (capture_response).
        self._on_success(
            self._item,
            getattr(future.meta, "size", None),
            getattr(future.meta, "etag", None),
            future.meta.user_context.get(_DELETE_RESPONSE_KEY),
        )


class _ForgetFuture:
    """Remove a settled top-level future from cancellation tracking."""

    def __init__(self, forget: Callable[[Any], None]) -> None:
        self._forget = forget

    def on_done(self, future: Any, **kwargs: Any) -> None:
        self._forget(future)


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
      docs/overview.md section 2);
    - on a multipart copy, an s3transfer without the directive in its create
      blacklist would forward it to CreateMultipartUpload (no such member)
      and fail - the multipart path does not carry annotations anyway, so the
      injection is skipped there too.
    """

    def __init__(self, item: TransferItem, *, client: Any, multipart_threshold: int) -> None:
        self._item = item
        self._client = client
        self._multipart_threshold = multipart_threshold

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        if not _copy_annotations_param_supported(self._client):
            return
        size = self._item.size or 0
        if size >= self._multipart_threshold and not _annotation_directive_blacklisted():
            return
        future.meta.call_args.extra_args["AnnotationDirective"] = "EXCLUDE"


class _SetAnnotations:
    """`copy-props all`: stage and carry S3 object annotations.

    A single-part CopyObject carries annotations server-side (the directive
    default is COPY), so nothing is sent - the same wire behavior as aws-cli.
    A multipart copy preloads every paginated source payload before submission
    in the memory and tempfile modes. A per-copy source-client adapter then
    serves those bytes to s3transfer's post-complete annotation writer, keeping
    its destination ETag/VersionId pinning and partial-write error handling.
    The deferred mode retains s3transfer's post-copy reads through
    `_PaginatingAnnotationClient`, which only completes the name listing
    (s3transfer's single-shot ``list_object_annotations`` would drop every
    page after the first). `Transferrer` refuses `copy_props=ALL` up
    front on an SDK that cannot honor the native write path.
    """

    def __init__(
        self,
        item: TransferItem,
        *,
        source_client: Any,
        multipart_threshold: int,
        mode: AnnotationCopyMode,
        options: TransferOptions,
        temp_dir: str | os.PathLike[str] | None,
        operation: str,
    ) -> None:
        self._item = item
        self._source_client = source_client
        self._multipart_threshold = multipart_threshold
        self._mode = mode
        self._options = options
        self._temp_dir = temp_dir
        self._operation = operation
        self._store: _MemoryAnnotationStore | _TempfileAnnotationStore | None = None
        self._preloaded_client = _PreloadedAnnotationClient(source_client, self)
        self._paginating_client = _PaginatingAnnotationClient(source_client, operation=operation)

    @property
    def source_client(self) -> Any:
        """The pagination-completing adapter for deferred reads, otherwise the cache adapter."""
        if self._mode is AnnotationCopyMode.DEFERRED:
            return self._paginating_client
        return self._preloaded_client

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        size = self._item.size or 0
        if size < self._multipart_threshold:
            return
        if self._mode is not AnnotationCopyMode.DEFERRED:
            self._preload()
        future.meta.call_args.extra_args["AnnotationDirective"] = "COPY"

    def on_done(self, future: Any, **kwargs: Any) -> None:
        self.close()

    def _preload(self) -> None:
        """Read every source annotation into the selected pre-copy store.

        List all pages and collect every name before fetching payloads, matching
        aws-cli's ordering. Source `VersionId` and request-payer parameters are
        pinned on the reads. Any failure closes and discards the partial store
        before the exception prevents multipart destination creation.
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
            # Pre-translate carrying the *source* bucket/key: a raw ClientError
            # from these source-side reads would be re-tagged with the copy
            # destination by _record_failure (see delete_s3_source).
            with s3_errors(
                operation=self._operation, bucket=self._item.src_bucket, key=self._item.src_key
            ):
                paginator = self._source_client.get_paginator("list_object_annotations")
                names: list[str] = []
                for page in paginator.paginate(
                    Bucket=self._item.src_bucket,
                    Key=self._item.src_key,
                    **list_params,
                ):
                    for annotation in page.get("Annotations", []):
                        names.append(annotation["AnnotationName"])
                for name in names:
                    response = self._source_client.get_object_annotation(
                        Bucket=self._item.src_bucket,
                        Key=self._item.src_key,
                        AnnotationName=name,
                        **get_params,
                    )
                    store.add(name, response["AnnotationPayload"].read())
        except BaseException:
            self.close()
            raise

    def annotation_names(self) -> list[str]:
        store = self._store
        assert store is not None
        return store.names()

    def annotation_payload(self, name: str) -> bytes:
        store = self._store
        assert store is not None
        return store.read(name)

    def close(self) -> None:
        store, self._store = self._store, None
        if store is not None:
            store.close()


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


class _PreloadedAnnotationClient:
    """Source-client adapter serving s3transfer's annotation reads from a preload."""

    def __init__(self, source_client: Any, preloader: _SetAnnotations) -> None:
        self._source_client = source_client
        self._preloader = preloader

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source_client, name)

    def list_object_annotations(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "Annotations": [{"AnnotationName": name} for name in self._preloader.annotation_names()]
        }

    def get_object_annotation(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["AnnotationName"]
        return {"AnnotationPayload": io.BytesIO(self._preloader.annotation_payload(name))}


class _PaginatingAnnotationClient:
    """Source-client adapter completing s3transfer's single-shot annotation list.

    The deferred mode keeps s3transfer's post-complete annotation reads on the
    source client, but s3transfer calls ``list_object_annotations`` once and
    never follows ``NextContinuationToken`` - a source with more annotations
    than one page would silently lose the tail. aws-cli's subscriber collects
    the names with a paginator; this adapter restores that by merging every
    page into the single response s3transfer expects. Names are small, so the
    merge does not defeat the mode's point (payload reads stay lazy per-name
    pass-throughs on the underlying client). Both intercepted reads
    pre-translate under the source bucket/key from the call's own params, so a
    deferred-read failure is attributed to the source like the preload path's
    (`_record_failure` would otherwise re-tag it with the copy destination).
    """

    def __init__(self, source_client: Any, *, operation: str) -> None:
        self._source_client = source_client
        self._operation = operation

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source_client, name)

    def list_object_annotations(self, **kwargs: Any) -> dict[str, Any]:
        with s3_errors(
            operation=self._operation, bucket=kwargs.get("Bucket"), key=kwargs.get("Key")
        ):
            annotations: list[dict[str, Any]] = []
            for page in self._source_client.get_paginator("list_object_annotations").paginate(
                **kwargs
            ):
                annotations.extend(page.get("Annotations", []))
            return {"Annotations": annotations}

    def get_object_annotation(self, **kwargs: Any) -> Any:
        with s3_errors(
            operation=self._operation, bucket=kwargs.get("Bucket"), key=kwargs.get("Key")
        ):
            return self._source_client.get_object_annotation(**kwargs)


class _SetMetadataDirectiveProps:
    """Carry source metadata into a multipart copy (aws subscriber port).

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
        multipart_threshold: int,
        head_params: dict[str, Any],
        operation: str,
    ) -> None:
        self._item = item
        self._source_client = source_client
        self._multipart_threshold = multipart_threshold
        self._head_params = head_params
        self._operation = operation

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        extra_args: dict[str, Any] = future.meta.call_args.extra_args
        size = self._item.size or 0
        is_multipart = size >= self._multipart_threshold
        has_explicit = any(prop in extra_args for prop in _METADATA_DIRECTIVE_PROPS)
        if not is_multipart and not has_explicit:
            return
        head = self._head_source()
        extra_args.setdefault("MetadataDirective", "REPLACE")
        for prop in _METADATA_DIRECTIVE_PROPS:
            if prop not in extra_args and prop in head:
                extra_args[prop] = head[prop]

    def _head_source(self) -> Mapping[str, Any]:
        if self._item.head is not None:
            return self._item.head
        # Pre-translate carrying the *source* bucket/key: a raw ClientError from
        # this source-side read would be re-tagged with the copy destination by
        # _record_failure (see delete_s3_source).
        with s3_errors(
            operation=self._operation, bucket=self._item.src_bucket, key=self._item.src_key
        ):
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
    (aws fails at create and keeps the source where tagging is denied).
    Removed EAFP-style: the table is process-shared, so two threads building
    their first manager concurrently could both pass a membership check and
    the loser's ``remove`` would raise; catching ``ValueError`` instead also
    keeps this a no-op on older s3transfer that never blacklisted it.
    """
    try:
        CopySubmissionTask.CREATE_MULTIPART_ARGS_BLACKLIST.remove("Tagging")
    except ValueError:
        pass


def _mpu_inline_tagging_supported() -> bool:
    """Whether s3transfer forwards an inline ``Tagging`` header to
    CreateMultipartUpload.

    `_allow_inline_mpu_tagging` makes this hold on every supported s3transfer
    at manager build; the probe stays as a guard so a future upstream that
    reshapes the table degrades to the post-copy PutObjectTagging fallback
    instead of silently dropping the header.
    """
    return "Tagging" not in CopySubmissionTask.CREATE_MULTIPART_ARGS_BLACKLIST


class _SetTags:
    """Carry source tags into a multipart copy (aws subscriber port).

    Single-part copies inherit tags via ``TaggingDirective=COPY``; multipart
    copies read GetObjectTagging from the source and either inline the
    percent-encoded set in the ``Tagging`` header on CreateMultipartUpload
    (<= ~2 KiB - `_allow_inline_mpu_tagging` keeps the header off upstream's
    create blacklist, like aws-cli's bundled table; the
    `_mpu_inline_tagging_supported` probe guards the alignment) or apply it
    with a post-copy PutObjectTagging - rolling the destination object back
    with a delete
    when that tagging write fails, then surfacing the tagging failure on the
    transfer future. If the rollback delete itself also fails, aws-cli lets
    that delete error escape the done callback (s3transfer swallows it) and
    records the transfer as a success; we mirror that, leaving the future
    settled (and, for ``mv``, letting the source delete proceed).
    """

    def __init__(
        self,
        item: TransferItem,
        *,
        source_client: Any,
        dest_client: Any,
        multipart_threshold: int,
        options: TransferOptions,
        operation: str,
    ) -> None:
        self._item = item
        self._source_client = source_client
        self._dest_client = dest_client
        self._multipart_threshold = multipart_threshold
        self._options = options
        self._operation = operation

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        extra_args: dict[str, Any] = future.meta.call_args.extra_args
        if "Tagging" in extra_args or "TaggingDirective" in extra_args:
            return
        size = self._item.size or 0
        if size < self._multipart_threshold:
            return
        tag_set = self._get_source_tags()
        if not tag_set:
            return
        header = "&".join(
            f"{quote(tag['Key'], safe='')}={quote(tag['Value'], safe='')}" for tag in tag_set
        )
        fits_inline = len(header.encode("utf-8")) <= _MAX_TAGGING_HEADER_SIZE
        if fits_inline and _mpu_inline_tagging_supported():
            extra_args["Tagging"] = header
        else:
            future.meta.user_context[_POST_TAGGING_KEY] = tag_set

    def on_done(self, future: Any, **kwargs: Any) -> None:
        tag_set = future.meta.user_context.get(_POST_TAGGING_KEY)
        if not tag_set:
            return
        try:
            future.result()
        except Exception:
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
            self._rollback(bucket, key)
            future.set_exception(exc)

    def _get_source_tags(self) -> list[dict[str, str]]:
        # Pre-translate carrying the *source* bucket/key: a raw ClientError from
        # this source-side read would be re-tagged with the copy destination by
        # _record_failure (see delete_s3_source).
        with s3_errors(
            operation=self._operation, bucket=self._item.src_bucket, key=self._item.src_key
        ):
            response = self._source_client.get_object_tagging(
                Bucket=self._item.src_bucket,
                Key=self._item.src_key,
                **requestparams.map_get_object_tagging_params(self._options),
            )
        return [{"Key": tag["Key"], "Value": tag["Value"]} for tag in response.get("TagSet", [])]

    def _rollback(self, bucket: str, key: str) -> None:
        # A raised delete error is left to escape on_done (matching aws-cli): the
        # already-swallowed on_done callback keeps the future settled as success.
        self._dest_client.delete_object(
            Bucket=bucket,
            Key=key,
            **requestparams.map_delete_object_params(self._options),
        )


__all__ = [
    "TransferItem",
    "Transferrer",
    "Warner",
    "annotations_copy_unsupported_reason",
    "conditional_write_unsupported_reason",
]
