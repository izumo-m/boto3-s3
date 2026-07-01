"""The ``s3transfer``-backed transfer engine: ``TransferItem`` + ``Transferrer``.

One ``Transferrer`` serves one ``cp`` / ``mv`` / ``sync`` run, whose items all
share a single :class:`TransferType` (a run moves bytes in one direction). It owns
the ``s3transfer.manager.TransferManager`` - built via :meth:`Transferrer.prepare`
before a listing-driven run starts enumerating (client-event registration must
happen-before the scan prefetch worker's traffic on the same client), at the
first :meth:`Transferrer.submit` for a stream run, and never for a dry run (no
thread pools spun up; the ``s3transfer`` module itself is imported
earlier regardless, by ``boto3`` when a client is built) - and bridges
completions to the library's result model:
per-item ``OpResult`` records to ``on_result`` (from s3transfer worker
threads - keep callbacks fast and non-raising, the ``S3Deleter`` contract),
lock-guarded rollup counters for ``BatchError``, byte progress to
``on_progress``.

Engine choices (parity-driven):

- The engine follows ``TransferConfig.preferred_transfer_client`` with
  boto3's own semantics (``'auto'`` default; ``'auto'``/'``crt``' resolve
  through ``crtsupport``, docs/crt.md): the classic
  ``s3transfer.manager.TransferManager`` unless the CRT manager is selected,
  and unconditionally classic for a copy run - the CRT manager has no copy,
  the same rule boto3 and aws-cli apply to s3->s3. The public
  ``transfer_config`` type is :class:`boto3_s3.transferconfig.TransferConfig`
  (boto3's subclass plus CRT tuning fields; defaults match aws-cli: 8 MiB
  threshold/chunk, 10-way concurrency), and classic honors
  ``use_threads=False`` the way boto3 does - via the ``NonThreadedExecutor``
  (the CRT manager ignores the threading knobs, also like boto3).
- Backpressure is ``TransferConfig.max_request_queue_size``'s bounded
  semaphore: a full queue blocks the submitting thread, exactly the
  mechanism aws-cli's handler leans on. No extra submission window.
- Subscribers are plain duck-typed classes (s3transfer resolves callbacks
  with ``getattr``), keeping this module SDK-free at import time
  (docs/imports.md).
- The copy-props subscriber chain reproduces aws-cli's: a single-part
  CopyObject carries metadata and tags natively (directive defaults), while
  a multipart copy loses them - so metadata is re-injected from a source
  HeadObject (the cached single-source response when available) and tags via
  GetObjectTagging, inlined under S3's ~2 KiB ``Tagging`` header budget or
  applied by a post-copy PutObjectTagging whose failure rolls back the
  destination object.
"""

from __future__ import annotations

import errno
import logging
import mimetypes
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from boto3_s3 import requestparams
from boto3_s3.exceptions import Boto3S3Error, CancelledError, ValidationError
from boto3_s3.s3storage import s3_errors, translate_boto_error
from boto3_s3.types import (
    CopyPropsMode,
    FileInfo,
    OpOutcome,
    OpResult,
    ProgressCallback,
    ResultCallback,
    TransferOptions,
    TransferProgress,
    TransferType,
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

# user_context slot handing an oversized TagSet from on_queued to on_done.
_POST_TAGGING_KEY = "boto3_s3_post_copy_tag_set"

# user_context slot handing mv's source DeleteObject response from _DeleteSource
# to _Completion for capture_response (extra_info["delete"]); an S3 source delete
# populates it, a local / custom-backend one returns None and leaves it unset.
_DELETE_RESPONSE_KEY = "boto3_s3_delete_response"

_DEFAULT_MULTIPART_THRESHOLD = 8 * 1024 * 1024


@dataclass(slots=True, kw_only=True)
class TransferItem:
    """One unit of work handed to :meth:`Transferrer.submit`.

    ``compare_key`` is the item's operation-relative name (``naming.item_paths``)
    - the identity under which results and progress are reported. The
    per-route fields are populated by the orchestrator: ``src_path`` /
    ``dest_path`` are native local paths; bucket/key pairs are S3 sides.
    ``head`` carries the single-source HeadObject payload so a single-file
    copy never heads the source twice; ``mtime`` is the source LastModified a
    download stamps onto the local file; ``src_display`` / ``dest_display``
    are the rendered endpoints reported on ``OpResult``.
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
    # the object itself (HeadObject) - exactly the aws stream wire shape.
    src_fileobj: Any = None
    dest_fileobj: Any = None
    # A download admitted by the --case-conflict gate carries the callback that
    # drops its casefolded key from the gate's in-flight set (aws-cli's
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
    from s3transfer import copies, upload
    from s3transfer.manager import TransferManager

    for allowed in (TransferManager.ALLOWED_UPLOAD_ARGS, TransferManager.ALLOWED_COPY_ARGS):
        if "IfNoneMatch" not in allowed:
            allowed.append("IfNoneMatch")
    upload_cls = upload.UploadSubmissionTask
    # Back-compat (supported floor s3transfer 0.6.2): UploadSubmissionTask grew
    # CREATE_MULTIPART_BLOCKLIST only in s3transfer 0.11.0. On older s3transfer
    # the create-multipart call is handed the full extra_args (no per-arg
    # blocklist to extend), so skip it when the attribute is absent. IfNoneMatch
    # itself needs a newer botocore S3 model, so --no-overwrite is unavailable on
    # those installs regardless. Drop this getattr guard once the s3transfer floor
    # is raised to >= 0.11.
    create_blocklist = getattr(upload_cls, "CREATE_MULTIPART_BLOCKLIST", None)
    if create_blocklist is not None and "IfNoneMatch" not in create_blocklist:
        create_blocklist.append("IfNoneMatch")
    if "IfNoneMatch" not in upload_cls.COMPLETE_MULTIPART_ARGS:
        upload_cls.COMPLETE_MULTIPART_ARGS.append("IfNoneMatch")
    copy_cls = copies.CopySubmissionTask
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


def conditional_write_unsupported_reason(client: S3Client, *, is_copy: bool) -> str | None:
    """Why the installed botocore cannot honor ``--no-overwrite`` here, or ``None``.

    ``--no-overwrite`` maps to a conditional write (``IfNoneMatch="*"``), applied
    to PutObject (and CompleteMultipartUpload) on uploads and CopyObject on
    copies. The client's S3 model must define that input member; it is
    introspected directly (version-agnostic), while the returned message names
    the minimum botocore as a hint. Returns ``None`` when the param is supported.
    """
    op = "CopyObject" if is_copy else "PutObject"
    members = getattr(client.meta.service_model.operation_model(op).input_shape, "members", {})
    if "IfNoneMatch" in members:
        return None
    from importlib.metadata import version

    minimum = _CONDITIONAL_WRITE_MIN_BOTOCORE["copy" if is_copy else "upload"]
    return (
        f"--no-overwrite requires botocore >= {minimum} for {op} "
        f"(S3 conditional writes); the installed botocore is {version('botocore')}."
    )


def _set_file_utime(path: str, timestamp: float) -> None:
    """Set a file's atime/mtime (aws-cli's ``set_file_utime``).

    A permission failure (EPERM - typically a file owned by another user) is
    re-worded the way aws words it, so the resulting warning carries the
    aws-cli's "attempting to modify the utime" text; every other error keeps
    its own message.
    """
    try:
        os.utime(path, (timestamp, timestamp))
    except OSError as exc:
        if exc.errno != errno.EPERM:
            raise
        raise Boto3S3Error(
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
    streaming ``Body`` and ``ResponseMetadata``) and files it under that key.
    ``_build_extra_info`` drains it per item. A multipart download issues many
    ranged ``GetObject`` calls, so the first stored wins and the range-specific
    fields are dropped, leaving the object-level metadata.

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
    """

    #: The terminal object-writing operations. The intermediate multipart calls
    #: (CreateMultipartUpload / UploadPart / UploadPartCopy) are deliberately not
    #: observed - only the response describing the finished object is wanted.
    _WRITE_OPS = ("PutObject", "CopyObject", "CompleteMultipartUpload")
    #: The object-reading operation (a download). HeadObject is not observed.
    _READ_OPS = ("GetObject",)

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()
        # Bind once so unregister removes the same objects register added.
        self._stash_handler = self._stash_key
        self._record_handler = self._record_response

    def _stash_key(self, params: Mapping[str, Any], context: dict[str, Any], **kwargs: Any) -> None:
        bucket = params.get("Bucket")
        key = params.get("Key")
        if bucket is not None and key is not None:
            context["boto3_s3_capture_key"] = (bucket, key)

    def _record_response(
        self, parsed: Mapping[str, Any], context: Mapping[str, Any], **kwargs: Any
    ) -> None:
        bucket_key = context.get("boto3_s3_capture_key")
        if bucket_key is None:
            return
        # Drop the streaming Body (a download response carries the object bytes)
        # and ResponseMetadata (HTTP transport internals).
        response = {k: v for k, v in parsed.items() if k not in ("Body", "ResponseMetadata")}
        if "ContentRange" in response:
            # A ranged (partial) GetObject from a multipart download: the
            # object-level fields (ETag / VersionId / Metadata / ...) describe the
            # object, but ContentRange / ContentLength describe one range - drop
            # them so the slot reads like a whole-object response.
            response.pop("ContentRange", None)
            response.pop("ContentLength", None)
        with self._lock:
            # First stored wins: a multipart download issues many ranged GETs for
            # the same key, all carrying the same object-level metadata.
            self._store.setdefault(bucket_key, response)

    def register(self, client: Any) -> None:
        events = client.meta.events
        for op in (*self._WRITE_OPS, *self._READ_OPS):
            events.register(f"before-parameter-build.s3.{op}", self._stash_handler)
            events.register(f"after-call.s3.{op}", self._record_handler)

    def unregister(self, client: Any) -> None:
        events = client.meta.events
        for op in (*self._WRITE_OPS, *self._READ_OPS):
            events.unregister(f"before-parameter-build.s3.{op}", self._stash_handler)
            events.unregister(f"after-call.s3.{op}", self._record_handler)

    def pop(self, bucket: str | None, key: str | None) -> dict[str, Any] | None:
        if bucket is None or key is None:
            return None
        with self._lock:
            return self._store.pop((bucket, key), None)


class Transferrer:
    """Submit transfer items of one kind and aggregate their outcomes.

    Use as a context manager around the submission loop: ``__exit__`` shuts
    the manager down - waiting for in-flight transfers on a clean exit *and*
    on an ordinary exception (aws stops submitting but lets submitted work
    finish), cancelling them only for :class:`CancelledError` or a
    non-``Exception`` interrupt (Ctrl-C). The rollup counters are approximate
    while transfers are in flight and exact after the ``with`` block exits.

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
        capture_response: bool = False,
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
                raise Boto3S3Error(reason, operation=operation)
        self._on_progress = on_progress
        self._on_result = on_result
        self._capture_response = capture_response
        self._manager: Any = None
        self._capture: _ResponseCapture | None = None
        self._lock = threading.Lock()
        self._succeeded = 0
        self._failed = 0
        self._warned = 0
        self._skipped = 0
        self._first_error: Boto3S3Error | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> Transferrer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        if self._manager is None:
            return
        cancel = exc is not None and (
            isinstance(exc, CancelledError) or not isinstance(exc, Exception)
        )
        try:
            self._manager.shutdown(cancel=cancel)
        finally:
            if self._capture is not None:
                # After shutdown every transfer is drained, so no request is
                # emitting on the client while the capture handlers are removed.
                # The finally covers shutdown itself raising (s3transfer re-raises
                # a KeyboardInterrupt arriving during its drain wait): better to
                # unregister with stragglers in flight than to leave the handlers
                # feeding _store forever on a longer-lived client.
                self._capture.unregister(self._client)

    # -- rollup (approximate until the with block exits) --------------------

    @property
    def succeeded(self) -> int:
        return self._succeeded

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def warned(self) -> int:
        return self._warned

    @property
    def skipped(self) -> int:
        return self._skipped

    @property
    def first_error(self) -> Boto3S3Error | None:
        return self._first_error

    # -- non-transfer outcomes ----------------------------------------------

    def warn(self, body: str, *, key: str = "") -> None:
        """Record a warning (aws-cli message body, no ``warning: `` prefix).

        Warnings count independently of files: a transfer that succeeds but
        fails its mtime stamp produces both a SUCCEEDED and a WARNED record,
        and ``warned > 0`` alone maps to exit code 2 (aws rc model).
        """
        with self._lock:
            self._warned += 1
        self._emit(
            OpResult(
                transfer_type=self._result_transfer_type,
                key=key,
                outcome=OpOutcome.WARNED,
                error=Boto3S3Error(body, operation=self._operation),
                src_storage=self._src_storage,
                dest_storage=self._dest_storage,
            )
        )

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
                key=key,
                outcome=OpOutcome.NOTICE,
                error=Boto3S3Error(body, operation=self._operation),
                src_storage=self._src_storage,
                dest_storage=self._dest_storage,
            )
        )

    def dryrun(self, item: TransferItem) -> None:
        """Report what a dry run would have transferred (no API calls)."""
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
                self._submit_upload(item)
            elif self._transfer_type is TransferType.DOWNLOAD:
                self._submit_download(item)
            else:
                self._submit_copy(item)
        except BaseException:
            # The producer may have opened the item's stream before handing it
            # over (the open routes / _cp_stream). A submit that raises before
            # the manager accepted the work - the grants ValidationError from
            # param mapping, a manager-build failure - means _CloseFileobj
            # never runs, so release the handle here (best-effort; mirrors
            # _CloseFileobj, which also closes on a failed transfer).
            self._close_item_fileobjs(item)
            raise

    @staticmethod
    def _close_item_fileobjs(item: TransferItem) -> None:
        for fileobj in (item.src_fileobj, item.dest_fileobj):
            if fileobj is None:
                continue
            try:
                fileobj.close()
            except Exception:
                logger.debug("closing a fileobj after a submit error failed", exc_info=True)

    def _submit_upload(self, item: TransferItem) -> None:
        # A directory source is handed through to fail like aws-cli ([Errno 21]
        # Is a directory, rc 1); botocore's default checksum wrapper would
        # otherwise open it and mask the read failure as an opaque rewind error,
        # so detect it and surface the OS error directly. A stream (src_fileobj)
        # is never a directory.
        if item.src_fileobj is None and item.src_path and os.path.isdir(item.src_path):
            self._record_failure(
                item, IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), item.src_path)
            )
            return
        extra_args = requestparams.map_put_object_params(self._options, self._operation)
        if self._options.get("guess_mime_type", True) and "ContentType" not in extra_args:
            guessed = _guess_content_type(item.src_path or "")
            if guessed is not None:
                extra_args["ContentType"] = guessed
        subscribers = self._common_subscribers(item)
        if item.src_fileobj is not None:
            subscribers.append(_CloseFileobj(item.src_fileobj))
        if self._is_move:
            subscribers.append(self._delete_source_subscriber(item))
        subscribers.append(self._completion(item))
        self._get_manager().upload(
            fileobj=item.src_fileobj if item.src_fileobj is not None else item.src_path,
            bucket=item.dest_bucket,
            key=item.dest_key,
            extra_args=extra_args,
            subscribers=subscribers,
        )

    def _submit_download(self, item: TransferItem) -> None:
        extra_args = requestparams.map_get_object_params(self._options)
        subscribers = self._common_subscribers(item)
        if item.dest_path is not None:
            subscribers.append(_DirectoryCreator())
        if item.dest_fileobj is not None:
            subscribers.append(_CloseFileobj(item.dest_fileobj))
        if item.case_conflict_cleanup is not None:
            subscribers.append(_CaseConflictCleanup(item.case_conflict_cleanup))
        if self._is_move:
            subscribers.append(self._delete_source_subscriber(item))
        subscribers.append(self._completion(item, post_success=self._stamp_mtime))
        self._get_manager().download(
            bucket=item.src_bucket,
            key=item.src_key,
            fileobj=item.dest_fileobj if item.dest_fileobj is not None else item.dest_path,
            extra_args=extra_args,
            subscribers=subscribers,
        )

    def _submit_copy(self, item: TransferItem) -> None:
        extra_args = requestparams.map_copy_object_params(self._options, self._operation)
        subscribers = self._common_subscribers(item)
        if not self._options.get("metadata_directive"):
            subscribers.extend(self._copy_props_subscribers(item))
        if self._is_move:
            subscribers.append(self._delete_source_subscriber(item))
        subscribers.append(self._completion(item))
        self._get_manager().copy(
            copy_source={"Bucket": item.src_bucket, "Key": item.src_key},
            bucket=item.dest_bucket,
            key=item.dest_key,
            extra_args=extra_args,
            subscribers=subscribers,
            source_client=self._source_client,
        )

    def _common_subscribers(self, item: TransferItem) -> list[Any]:
        # Size and etag are provided for every kind, aws-cli-style: s3transfer
        # skips its pre-transfer HeadObject probe (downloads AND copies) only
        # when both are present. S3-sourced items always carry an etag; local
        # sources have none, and uploads never probe.
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

    def _completion(
        self, item: TransferItem, *, post_success: Callable[[TransferItem], None] | None = None
    ) -> _Completion:
        return _Completion(
            item,
            on_success=self._record_success,
            on_failure=self._record_failure,
            post_success=post_success,
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

    def _copy_props_subscribers(self, item: TransferItem) -> list[Any]:
        mode = CopyPropsMode(self._options.get("copy_props", CopyPropsMode.DEFAULT))
        if mode is CopyPropsMode.NONE:
            return [_ReplaceMetadataDirective(), _ReplaceTaggingDirective()]
        metadata = _SetMetadataDirectiveProps(
            item,
            source_client=self._source_client,
            multipart_threshold=self._multipart_threshold,
            head_params=requestparams.map_head_object_params_with_copy_source_sse(self._options),
        )
        if mode is CopyPropsMode.METADATA_DIRECTIVE:
            return [metadata, _ReplaceTaggingDirective()]
        tags = _SetTags(
            item,
            source_client=self._source_client,
            dest_client=self._client,
            multipart_threshold=self._multipart_threshold,
            options=self._options,
        )
        return [metadata, tags]

    # -- engine internals ----------------------------------------------------

    def _get_manager(self) -> Any:
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
        from boto3_s3 import crtsupport

        if not crtsupport.should_use_crt(str(preferred)):
            return None
        _allow_if_none_match()  # the CRT manager aliases the classic arg lists
        return crtsupport.create_crt_transfer_manager(self._client, self._transfer_config)

    def _create_classic_manager(self) -> Any:
        from s3transfer.manager import TransferConfig as S3TransferConfig
        from s3transfer.manager import TransferManager

        _allow_if_none_match()
        config: Any = self._transfer_config
        if config is None:
            config = S3TransferConfig()
        executor_cls = None
        if not getattr(config, "use_threads", True):
            from s3transfer.futures import NonThreadedExecutor

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
        return OpResult(
            transfer_type=self._result_transfer_type,
            key=item.compare_key,
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
        boto3-s3 hands s3transfer for a copy and a download (the same as the written
        object's except for a multipart copy); an upload has none, so ``extra_info``
        is then ``None``. With ``capture_response`` a
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
            return self._capture.pop(item.dest_bucket, item.dest_key), None
        if self._transfer_type is TransferType.DOWNLOAD:
            return None, self._capture.pop(item.src_bucket, item.src_key)
        return None, None

    def _record_failure(self, item: TransferItem, exc: BaseException) -> None:
        # Failed / skipped items surface no captured response, but the entry
        # must still leave the store (see _drain_captured).
        self._drain_captured(item)
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
        with self._lock:
            self._failed += 1
            if self._first_error is None:
                self._first_error = error
        self._emit(self._result(item, OpOutcome.FAILED, error=error))

    def _stamp_mtime(self, item: TransferItem) -> None:
        """Stamp the source LastModified onto the downloaded file.

        Runs on the s3transfer worker after a successful download. Failure
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
    """Pre-populate ``TransferFuture.meta.size`` (skips s3transfer's probe)."""

    def __init__(self, size: int) -> None:
        self._size = size

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        # The CRT manager's meta cannot take a provided size; guard like
        # aws-cli's ProvideSizeSubscriber (the CRT probes sizes itself).
        if hasattr(future.meta, "provide_transfer_size"):
            future.meta.provide_transfer_size(self._size)


class _ProvideETag:
    """Pre-populate ``TransferFuture.meta.etag`` (with size: skips the HEAD)."""

    def __init__(self, etag: str) -> None:
        self._etag = etag

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        if hasattr(future.meta, "provide_object_etag"):
            future.meta.provide_object_etag(f'"{self._etag}"')


class _Progress:
    """Accumulate per-chunk deltas into absolute ``TransferProgress`` records.

    s3transfer reports each chunk as a delta, possibly from several worker
    threads at once for multipart parts; the lock makes accumulate-and-read
    atomic so consumers see monotonic ``bytes_done``. A zero-byte record is
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
                key=self._key,
                bytes_done=bytes_done,
                bytes_total=self._size,
            )
        )

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        self._fire(0)

    def on_progress(self, future: Any, bytes_transferred: int, **kwargs: Any) -> None:
        with self._lock:
            self._done += bytes_transferred
            done = self._done
        self._fire(done)


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
            raise Boto3S3Error(f"Could not create directory {directory}: {exc}") from exc


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
            future.meta.user_context[_DELETE_RESPONSE_KEY] = {
                k: v for k, v in captured.items() if k != "ResponseMetadata"
            }


class _CloseFileobj:
    """Close the custom-backend fileobj an ``open``-routed item carries.

    The transfer owns every fileobj a ``Storage.open`` hands it (the file
    protocol the open route relies on): a real backend's ``close`` releases a
    reader or *commits* a writer (``Storage.open``'s contract), while a
    caller-supplied stream (``IOStorage``) hands back a close-suppressing view,
    so this is a harmless flush there. Sits before ``_DeleteSource`` /
    ``_Completion`` so a writer's commit failure flips the settled future to a
    failure - and, for ``mv``, leaves the source in place (``_DeleteSource``
    then sees the failure and skips its delete). A transfer that already failed
    still closes - to release the resource - but never lets a close error
    overwrite the original failure.
    """

    def __init__(self, fileobj: Any) -> None:
        self._fileobj = fileobj

    def on_done(self, future: Any, **kwargs: Any) -> None:
        try:
            future.result()
        except Exception:
            try:
                self._fileobj.close()
            except Exception:
                pass
            return
        try:
            self._fileobj.close()
        except Exception as exc:
            future.set_exception(exc)


class _CaseConflictCleanup:
    """Drop an admitted download's casefolded key from the gate's in-flight set.

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
    here is non-blocking - it returns or raises the stored outcome. Runs last
    in the subscriber list so copy-props' post-copy tagging (which may flip
    the future to an exception) has already settled. The outcome sinks are
    injected by ``Transferrer`` at submit time (its rollup recorders, plus
    the download mtime stamp as ``post_success``).
    """

    def __init__(
        self,
        item: TransferItem,
        *,
        on_success: Callable[[TransferItem, int | None, str | None, dict[str, Any] | None], None],
        on_failure: Callable[[TransferItem, BaseException], None],
        post_success: Callable[[TransferItem], None] | None = None,
    ) -> None:
        self._item = item
        self._on_success = on_success
        self._on_failure = on_failure
        self._post_success = post_success

    def on_done(self, future: Any, **kwargs: Any) -> None:
        try:
            future.result()
        except Exception as exc:
            self._on_failure(self._item, exc)
            return
        if self._post_success is not None:
            self._post_success(self._item)
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


class _ReplaceMetadataDirective:
    """``copy-props none``: propagate no metadata (directive REPLACE)."""

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        future.meta.call_args.extra_args.setdefault("MetadataDirective", "REPLACE")


class _ReplaceTaggingDirective:
    """``copy-props none`` / ``metadata-directive``: propagate no tags."""

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        future.meta.call_args.extra_args.setdefault("TaggingDirective", "REPLACE")


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
    """

    def __init__(
        self,
        item: TransferItem,
        *,
        source_client: Any,
        multipart_threshold: int,
        head_params: dict[str, Any],
    ) -> None:
        self._item = item
        self._source_client = source_client
        self._multipart_threshold = multipart_threshold
        self._head_params = head_params

    def on_queued(self, future: Any, **kwargs: Any) -> None:
        extra_args: dict[str, Any] = future.meta.call_args.extra_args
        size = self._item.size or 0
        is_multipart = size >= self._multipart_threshold
        has_explicit = any(prop in extra_args for prop in _METADATA_DIRECTIVE_PROPS)
        if not is_multipart and not has_explicit:
            return
        head = self._head_source()
        if has_explicit:
            extra_args.setdefault("MetadataDirective", "REPLACE")
        for prop in _METADATA_DIRECTIVE_PROPS:
            if prop not in extra_args and prop in head:
                extra_args[prop] = head[prop]

    def _head_source(self) -> Mapping[str, Any]:
        if self._item.head is not None:
            return self._item.head
        return self._source_client.head_object(
            Bucket=self._item.src_bucket, Key=self._item.src_key, **self._head_params
        )


class _SetTags:
    """Carry source tags into a multipart copy (aws subscriber port).

    Single-part copies inherit tags via ``TaggingDirective=COPY``; multipart
    copies read GetObjectTagging from the source and either inline the
    percent-encoded set in the ``Tagging`` header (<= ~2 KiB) or apply it with
    a post-copy PutObjectTagging - rolling the destination object back with a
    best-effort delete when that tagging write fails, and surfacing the
    failure on the transfer future.
    """

    def __init__(
        self,
        item: TransferItem,
        *,
        source_client: Any,
        dest_client: Any,
        multipart_threshold: int,
        options: TransferOptions,
    ) -> None:
        self._item = item
        self._source_client = source_client
        self._dest_client = dest_client
        self._multipart_threshold = multipart_threshold
        self._options = options

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
        if len(header.encode("utf-8")) <= _MAX_TAGGING_HEADER_SIZE:
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
        response = self._source_client.get_object_tagging(
            Bucket=self._item.src_bucket,
            Key=self._item.src_key,
            **requestparams.map_get_object_tagging_params(self._options),
        )
        return [{"Key": tag["Key"], "Value": tag["Value"]} for tag in response.get("TagSet", [])]

    def _rollback(self, bucket: str, key: str) -> None:
        try:
            self._dest_client.delete_object(
                Bucket=bucket,
                Key=key,
                **requestparams.map_delete_object_params(self._options),
            )
        except Exception:
            pass


__all__ = ["TransferItem", "Transferrer"]
