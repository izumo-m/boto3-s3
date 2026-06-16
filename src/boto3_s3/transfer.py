"""The ``s3transfer``-backed transfer engine: ``TransferItem`` + ``Transferrer``.

One ``Transferrer`` serves one ``cp`` / ``mv`` / ``sync`` run, whose items all
share a single :class:`OpKind` (a run moves bytes in one direction). It owns
the ``s3transfer.manager.TransferManager`` - built lazily on the first
:meth:`Transferrer.submit`, so dry runs and all-skipped runs never instantiate
it (no thread pools spun up; the ``s3transfer`` module itself is imported
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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from boto3_s3 import requestparams
from boto3_s3.exceptions import Boto3S3Error, CancelledError, ValidationError
from boto3_s3.s3storage import translate_boto_error
from boto3_s3.types import (
    CopyPropsMode,
    OpKind,
    OpOutcome,
    OpResult,
    ProgressCallback,
    ResultCallback,
    TransferOptions,
    TransferProgress,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from boto3.s3.transfer import TransferConfig
    from mypy_boto3_s3 import S3Client

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

_DEFAULT_MULTIPART_THRESHOLD = 8 * 1024 * 1024


@dataclass(slots=True, kw_only=True)
class TransferItem:
    """One unit of work handed to :meth:`Transferrer.submit`.

    ``compare_key`` is the item's operation-relative name (``naming.item_paths``)
    - the identity under which results and progress are reported. The
    per-route fields are populated by the orchestrator: ``src_path`` /
    ``dst_path`` are native local paths; bucket/key pairs are S3 sides.
    ``head`` carries the single-source HeadObject payload so a single-file
    copy never heads the source twice; ``mtime`` is the source LastModified a
    download stamps onto the local file; ``src_display`` / ``dst_display``
    are the rendered endpoints reported on ``OpResult``.
    """

    compare_key: str
    size: int | None = None
    src_bucket: str | None = None
    src_key: str | None = None
    src_path: str | None = None
    dst_bucket: str | None = None
    dst_key: str | None = None
    dst_path: str | None = None
    etag: str | None = None
    mtime: datetime | None = None
    head: Mapping[str, Any] | None = None
    src_display: str = ""
    dst_display: str = ""
    # Streaming sides (cp with stdin/stdout, or any caller-supplied binary
    # stream): a readable object replaces src_path on uploads, a writable one
    # replaces dst_path on downloads. Streams get no directory creation and
    # no mtime stamp, and a download without size+etag lets s3transfer probe
    # the object itself (HeadObject) - exactly the aws stream wire shape.
    src_fileobj: Any = None
    dst_fileobj: Any = None


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
    s3transfer's own size probe.

    ``is_move`` turns the run into ``mv``: every record reports
    ``OpKind.MOVE`` while ``kind`` keeps routing the bytes (aws-cli
    ``operation_name`` vs ``transfer_type='move'``), and each successful
    transfer deletes its source - ``os.remove`` for uploads, a per-object
    DeleteObject on the owning side's client otherwise.
    """

    def __init__(
        self,
        kind: OpKind,
        client: S3Client,
        *,
        source_client: S3Client | None = None,
        transfer_config: TransferConfig | None = None,
        options: TransferOptions | None = None,
        operation: str = "cp",
        is_move: bool = False,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
    ) -> None:
        if kind not in (OpKind.UPLOAD, OpKind.DOWNLOAD, OpKind.COPY):
            raise ValidationError(f"Transferrer does not handle {kind!r}", operation=operation)
        self._kind = kind
        self._result_kind = OpKind.MOVE if is_move else kind
        self._is_move = is_move
        self._client = client
        self._source_client = source_client if source_client is not None else client
        self._transfer_config = transfer_config
        self._options: TransferOptions = options if options is not None else TransferOptions()
        self._operation = operation
        # Library-side --no-overwrite gate (the CLI rejects earlier with rc 252):
        # only uploads/copies carry IfNoneMatch, and an old botocore lacks it.
        if self._options.get("no_overwrite") and kind in (OpKind.UPLOAD, OpKind.COPY):
            reason = conditional_write_unsupported_reason(client, is_copy=kind is OpKind.COPY)
            if reason is not None:
                raise Boto3S3Error(reason, operation=operation)
        self._on_progress = on_progress
        self._on_result = on_result
        self._manager: Any = None
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
        self._manager.shutdown(cancel=cancel)

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
                kind=self._result_kind,
                key=key,
                outcome=OpOutcome.WARNED,
                error=Boto3S3Error(body, operation=self._operation),
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
                kind=self._result_kind,
                key=key,
                outcome=OpOutcome.NOTICE,
                error=Boto3S3Error(body, operation=self._operation),
            )
        )

    def dryrun(self, item: TransferItem) -> None:
        """Report what a dry run would have transferred (no API calls)."""
        self._emit(self._result(item, OpOutcome.DRYRUN))

    # -- submission ----------------------------------------------------------

    def submit(self, item: TransferItem) -> None:
        """Queue one transfer (blocks when the manager queue is full).

        Request-parameter mapping happens here, per item - so an invalid
        ``grants`` shape surfaces as an in-flight error like aws (rc 1 via
        the fatal-error path), never as an upfront usage error.
        """
        if self._kind is OpKind.UPLOAD:
            self._submit_upload(item)
        elif self._kind is OpKind.DOWNLOAD:
            self._submit_download(item)
        else:
            self._submit_copy(item)

    def _submit_upload(self, item: TransferItem) -> None:
        extra_args = requestparams.map_put_object_params(self._options, self._operation)
        if self._options.get("guess_mime_type", True) and "ContentType" not in extra_args:
            guessed = _guess_content_type(item.src_path or "")
            if guessed is not None:
                extra_args["ContentType"] = guessed
        subscribers = self._common_subscribers(item)
        if self._is_move:
            subscribers.append(self._delete_source_subscriber(item))
        subscribers.append(self._completion(item))
        self._get_manager().upload(
            fileobj=item.src_fileobj if item.src_fileobj is not None else item.src_path,
            bucket=item.dst_bucket,
            key=item.dst_key,
            extra_args=extra_args,
            subscribers=subscribers,
        )

    def _submit_download(self, item: TransferItem) -> None:
        extra_args = requestparams.map_get_object_params(self._options)
        subscribers = self._common_subscribers(item)
        if item.dst_path is not None:
            subscribers.append(_DirectoryCreator())
        if self._is_move:
            subscribers.append(self._delete_source_subscriber(item))
        subscribers.append(self._completion(item, post_success=self._stamp_mtime))
        self._get_manager().download(
            bucket=item.src_bucket,
            key=item.src_key,
            fileobj=item.dst_fileobj if item.dst_fileobj is not None else item.dst_path,
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
            bucket=item.dst_bucket,
            key=item.dst_key,
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
                _Progress(self._result_kind, item.compare_key, item.size, self._on_progress)
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

        Uploads remove the local file with a bare ``os.remove`` so a failure
        carries the OS's own wording (aws's ``move failed: ... [Errno 13]
        ...``); S3 sources get a single DeleteObject - on the manager's
        client for downloads, the source-side client for copies - with
        ``RequestPayer`` forwarded like every other request.
        """
        if self._kind is OpKind.UPLOAD:
            src_path = item.src_path or ""
            return _DeleteSource(lambda: os.remove(src_path))
        client: Any = self._client if self._kind is OpKind.DOWNLOAD else self._source_client
        bucket = item.src_bucket
        key = item.src_key
        params = requestparams.map_delete_object_params(self._options)
        return _DeleteSource(lambda: client.delete_object(Bucket=bucket, Key=key, **params))

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
            # Built lazily: the TransferManager (and its thread pools) is
            # created only when bytes actually move, so dryrun and all-skipped
            # runs never reach here (docs/imports.md). The s3transfer.manager
            # module itself is already imported (by boto3 at client build).
            manager = self._create_crt_manager()
            if manager is None:
                manager = self._create_classic_manager()
            # Transfer-time breadcrumb: names the engine actually built for
            # this run (CRTTransferManager vs the classic TransferManager),
            # after any CRT->classic fallback. Lets --debug distinguish a real
            # CRT transfer from a silent classic fallback (docs/testing.md).
            logger.debug("transfer engine: %s", type(manager).__name__)
            self._manager = manager
        return self._manager

    def _create_crt_manager(self) -> Any | None:
        """The boto3-faithful CRT attempt; ``None`` selects the classic engine.

        ``preferred_transfer_client`` is read with boto3's defaults (no config
        = ``'auto'``); a copy run is unconditionally classic - the CRT manager
        has no copy, the same rule boto3 and aws-cli apply to s3->s3.
        """
        if self._kind is OpKind.COPY:
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
        error: BaseException | None = None,
    ) -> OpResult:
        return OpResult(
            kind=self._result_kind,
            key=item.compare_key,
            outcome=outcome,
            bytes_transferred=bytes_transferred,
            error=error,
            src=item.src_display or None,
            dest=item.dst_display or None,
        )

    def _emit(self, result: OpResult) -> None:
        if self._on_result is not None:
            self._on_result(result)

    def _record_success(self, item: TransferItem, resolved_size: int | None = None) -> None:
        # item.size is unset for an unknown-size transfer (a streaming download
        # lets s3transfer probe the object); fall back to the size s3transfer
        # resolved on the future so SUCCEEDED reports the real byte count.
        size = item.size if item.size is not None else resolved_size
        with self._lock:
            self._succeeded += 1
        self._emit(self._result(item, OpOutcome.SUCCEEDED, bytes_transferred=size or 0))

    def _record_failure(self, item: TransferItem, exc: BaseException) -> None:
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
            bucket=item.dst_bucket or item.src_bucket,
            key=item.dst_key or item.src_key,
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
        if item.mtime is None or item.dst_path is None:
            return
        try:
            _set_file_utime(item.dst_path, item.mtime.timestamp())
        except Exception as exc:
            self.warn(
                f"Skipping file {item.dst_path}. Successfully Downloaded {item.dst_path} "
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
        self, kind: OpKind, key: str, size: int | None, callback: ProgressCallback
    ) -> None:
        self._kind = kind
        self._key = key
        self._size = size
        self._callback = callback
        self._lock = threading.Lock()
        self._done = 0

    def _fire(self, bytes_done: int) -> None:
        self._callback(
            TransferProgress(
                kind=self._kind, key=self._key, bytes_done=bytes_done, bytes_total=self._size
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

    def __init__(self, delete: Callable[[], None]) -> None:
        self._delete = delete

    def on_done(self, future: Any, **kwargs: Any) -> None:
        try:
            future.result()
        except Exception:
            return
        try:
            self._delete()
        except Exception as exc:
            future.set_exception(exc)


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
        on_success: Callable[[TransferItem, int | None], None],
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
        # report real bytes when the item carried no size.
        self._on_success(self._item, getattr(future.meta, "size", None))


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
