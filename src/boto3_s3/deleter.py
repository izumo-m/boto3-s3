"""Async batched S3 object deletion: ``S3Deleter``.

``S3Deleter`` buffers listing entries (``FileInfo``) and dispatches them on a
single background worker thread. XML-compatible keys share one ``DeleteObjects``
call (up to ``S3_DELETE_BATCH`` keys); incompatible keys use ``DeleteObject``.
A caller iterating ``S3Storage.scan()`` therefore keeps scanning while the
previous buffer deletes. It is the building block for ``S3.rm`` and
``S3.sync(delete_filter=True)``, and is usable directly.

aws-cli note: ``aws s3 rm`` deletes one key per ``DeleteObject`` call and never
uses the batch API. The batched ``DeleteObjects`` here is a wire-level
deviation that is observably equivalent for ordinary keys (a nonexistent key
deletes "successfully" either way, and per-key success/failure is preserved via
``Quiet=True`` plus the response ``Errors[]``). Keys that cannot be represented
in the DeleteObjects XML 1.0 body fall back to per-key ``DeleteObject``, matching
aws-cli instead of failing their whole batch (control characters ride that
route fine; an unpaired surrogate cannot be carried by the HTTP layer on
either route - reachability of such keys is unverified, design/deleter.md).
User-facing lines such as ``delete: s3://...`` are the CLI layer's job, fed by
``on_result``; the library only emits ``logging`` diagnostics.

Threading contract: ``submit`` / ``flush`` / ``close`` belong to one caller
thread (single producer). ``on_result`` is invoked from the worker thread and
must be fast and must not raise (if it does, the exception surfaces at the
next non-empty ``flush()`` or at ``close()``). At most one batch is in flight:
a dispatch first waits for the previous batch - the backpressure point, and
where an unexpected worker exception re-raises on the caller thread. A
``dryrun=True`` deleter has no worker at all and reports each submission
inline on the caller thread.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import (
    CancelledError as FutureCancelledError,
)
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
)
from concurrent.futures import (
    TimeoutError as FutureTimeoutError,
)
from typing import TYPE_CHECKING, Any

from boto3_s3.exceptions import Boto3S3Error, ValidationError
from boto3_s3.s3storage import S3_CODE_CATEGORIES, S3Storage, s3_errors
from boto3_s3.types import (
    CancelMode,
    CancelToken,
    OpOutcome,
    OpResult,
    TransferType,
    strip_response_metadata,
)

if TYPE_CHECKING:
    from types import TracebackType

    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import (
        DeleteObjectsOutputTypeDef,
        ErrorTypeDef,
        ObjectIdentifierTypeDef,
    )

    from boto3_s3.types import FileInfo, ResultCallback

logger = logging.getLogger(__name__)

# AWS DeleteObjects per-call hard limit (and the default batch size).
S3_DELETE_BATCH = 1000


# The complement of XML 1.0's Char production: C0 controls other than
# TAB/LF/CR, surrogate code points, and the two terminal BMP noncharacters.
# A compiled class instead of a per-character Python loop: `_run_batch` runs
# this over every submitted key.
_XML_INCOMPATIBLE = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _delete_objects_compatible(key: str) -> bool:
    """Whether *key* can be serialized as XML 1.0 character data.

    ``DeleteObjects`` carries keys in an XML document. Botocore escapes CR/LF
    before sending that document, but XML 1.0 still forbids the remaining C0
    controls, surrogate code points, and the two terminal BMP noncharacters.
    ``DeleteObject`` carries the key in the URL instead and is the same route
    aws-cli uses for every key, so incompatible keys must take that path.
    """
    return _XML_INCOMPATIBLE.search(key) is None


class S3Deleter:
    """Buffer listing entries and delete them in batched ``DeleteObjects`` calls.

    ``submit`` takes a ``FileInfo``; its ``key`` is the
    FULL object key to delete, and the rest of the entry rides along untouched
    (a richer subtype - ``S3FileInfo`` with its ``etag`` - flows straight
    through). The target bucket and client come from ``storage`` - its key/prefix
    part is not consulted, and the client is held for the deleter's whole
    lifetime (do not ``storage.close()`` until this deleter is closed). The
    buffer auto-flushes at ``batch_size``; each flush batches its XML-compatible
    keys and sends each incompatible key through ``DeleteObject`` on the same
    worker while the caller keeps submitting. Duplicate keys within a batch are
    passed through as-is (dedup is the caller's concern).

    Per-key completion is reported through ``on_result`` - one ``OpResult`` per
    submitted entry, in submission order within a batch (entries abandoned by
    ``close(flush=False)``, by an error-path close, or by a close under a
    cancelled token get no result); rollup
    ``succeeded`` / ``failed`` / ``first_error`` are approximate while running
    and final after ``close``. The deleter never raises ``BatchError``
    itself - the caller builds one from the rollup (``first_error`` is the
    ``__cause__`` sample).

    ``dryrun=True`` turns the whole deleter into a rehearsal for a direct
    consumer: ``submit`` validates its entry exactly as a real run does and
    then emits one ``OpOutcome.DRYRUN`` record inline on the calling thread,
    buffering nothing and sending nothing to S3 (no worker is created at all,
    ``flush`` is a no-op, and the rollup counters stay 0 - a rehearsal has no
    successes). ``S3.rm`` and ``sync`` do not use it: they keep their own
    dryrun handling upstream (design/deleter.md section 5).

    The worker thread inherits daemon-ness from its creator (Python's
    ``ThreadPoolExecutor``), so from a normal non-daemon thread an unclosed
    deleter keeps the interpreter alive until the in-flight batch finishes -
    use the context manager. Use a recursive scan: a non-recursive scan also yields
    DIRECTORY (``CommonPrefixes``) entries, which are not object keys::

        with S3Deleter(storage, on_result=cb) as deleter:
            for info in storage.scan(S3ScanOptions(recursive=True)):
                deleter.submit(info)
    """

    def __init__(
        self,
        storage: S3Storage,
        *,
        request_payer: str | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        batch_size: int = S3_DELETE_BATCH,
        operation: str = "delete",
        capture_response: bool = False,
        dryrun: bool = False,
    ) -> None:
        # Runtime guard for untyped callers (e.g. S3.resolve routing a bare
        # "bucket/key" to LocalStorage): fail inside the taxonomy, not with an
        # AttributeError from duck-typing.
        if not isinstance(storage, S3Storage):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValidationError(
                f"S3Deleter requires an S3Storage target, got {type(storage).__name__}",
                operation=operation,
            )
        if not 1 <= batch_size <= S3_DELETE_BATCH:
            # Same taxonomy as the storage-type guard above: a caller-argument
            # problem raises ValidationError, not a bare ValueError.
            raise ValidationError(
                f"batch_size must be between 1 and {S3_DELETE_BATCH} (got {batch_size!r})",
                operation=operation,
            )
        # Eager: resolve the client and bucket now, so a bad storage fails on
        # the caller thread and the worker never triggers the lazy
        # (deliberately unlocked) client construction.
        # Retained whole (not just client/bucket) so a completion can surface the
        # backend handle alongside the deleted entry on its result.
        self._storage: S3Storage = storage
        self._client: S3Client = storage.get_client()
        self._bucket: str = storage.bucket
        self._request_payer = request_payer
        self._on_result = on_result
        self._cancel_token = cancel_token
        self._batch_size = batch_size
        self._operation = operation
        self._capture_response = capture_response
        self._dryrun = dryrun

        self._buffer: list[FileInfo] = []
        self._pending: Future[None] | None = None  # at most one in-flight batch
        self._closed = False

        # Rollup state: written only by the worker thread (single worker,
        # batches serialized); approximate while running, final after close().
        self._succeeded = 0
        self._failed = 0
        self._first_error: Boto3S3Error | None = None

        # Spawns its worker thread lazily on the first dispatch. Created last
        # so a constructor failure leaves nothing behind. A dryrun dispatches
        # nothing, so it builds no executor at all (None is also what narrows
        # the dispatch path off).
        self._executor: ThreadPoolExecutor | None = (
            None
            if dryrun
            else ThreadPoolExecutor(max_workers=1, thread_name_prefix="boto3-s3-deleter")
        )

    # -- rollup state ------------------------------------------------------

    @property
    def succeeded(self) -> int:
        """Keys deleted without error. Final after ``close``."""
        return self._succeeded

    @property
    def failed(self) -> int:
        """Keys that failed to delete. Final after ``close``."""
        return self._failed

    @property
    def first_error(self) -> Boto3S3Error | None:
        """The first per-key failure (a ``BatchError.__cause__`` sample)."""
        return self._first_error

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> S3Deleter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Flush on success, or abandon unsent entries while draining active work."""
        # A body exception abandons the unsent buffer but still waits for the
        # in-flight batch; a worker error raised here then propagates with the
        # body exception chained as __context__.
        self.close(flush=exc_type is None)

    def close(self, *, flush: bool = True) -> None:
        """Flush (unless ``flush=False`` or the token is cancelled), wait for
        in-flight work, shut down.

        Idempotent. Afterwards the rollup counters are final and ``submit`` /
        ``flush`` raise ``ValidationError``. An unexpected worker exception (or
        one raised by ``on_result``) re-raises here; the deleter still ends up
        closed and the worker shut down either way, and any entries left in the
        buffer by that re-raise, by ``flush=False``, or by a cancelled token
        are abandoned without results. Under ``dryrun`` there is nothing
        buffered and no worker, so it only marks the deleter closed.
        """
        if self._closed:
            return
        try:
            if flush and not self._cancelled():
                self.flush()
            self._wait_pending()
        finally:
            self._closed = True
            self._buffer = []  # non-empty only when flush=False or flush() raised
            if self._executor is not None:
                self._executor.shutdown(wait=True)

    # -- submission --------------------------------------------------------

    def submit(self, info: FileInfo) -> None:
        """Buffer one entry to delete; auto-flush when ``batch_size`` is reached.

        ``info.key`` is the full object key. An empty key is rejected up front:
        S3 requires keys of length >= 1, and one empty key would fail its entire
        batch. The entry stays buffered even when the auto-flush re-raises a
        previous batch's worker error - do not submit it again after catching
        that error.

        Under ``dryrun`` the same validation runs and the entry is then
        reported straight away: one ``OpOutcome.DRYRUN`` record on this
        thread, nothing buffered, nothing sent. A cancelled token suppresses
        that record (as a real ``flush`` stops dispatching once cancelled),
        and because the callback runs here rather than on the worker, an
        exception it raises propagates out of this call instead of being
        deferred to ``flush`` / ``close``.
        """
        self._ensure_open()
        if not info.key:
            raise ValidationError(
                "object key must not be empty", operation=self._operation, bucket=self._bucket
            )
        if self._dryrun:
            if not self._cancelled():
                self._emit(info, OpOutcome.DRYRUN)
            return
        self._buffer.append(info)
        if len(self._buffer) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """Hand the buffered entries to the worker, one batch per ``batch_size``.

        No-op when the buffer is empty, and always a no-op under ``dryrun``
        (which buffers nothing and has no worker). Each dispatch first waits
        for the previous batch - the backpressure point, and where an
        unexpected worker exception re-raises on the caller thread (the
        entries not yet dispatched then stay buffered; nothing is lost). The
        buffer only exceeds ``batch_size`` after such a re-raise; the loop
        re-chunks it so a single call never carries more than ``batch_size``
        entries.
        """
        self._ensure_open()
        if self._executor is None:  # dryrun: submit() already reported inline
            return
        while self._buffer:
            if self._cancelled():
                return
            self._wait_pending()
            if self._cancelled():
                return
            batch = self._buffer[: self._batch_size]
            del self._buffer[: self._batch_size]
            self._pending = self._executor.submit(self._run_batch, batch)

    # -- internals ---------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._closed:
            raise ValidationError(
                "deleter is closed", operation=self._operation, bucket=self._bucket
            )

    def _wait_pending(self) -> None:
        """Wait for the active batch while polling for immediate cancellation."""
        pending = self._pending
        if pending is None:
            return
        self._pending = None  # cleared first: a failed batch is reported once
        while True:
            if self._cancel_token is not None and self._cancel_token.mode is CancelMode.IMMEDIATE:
                pending.cancel()
            try:
                pending.result(timeout=0.1)
            except FutureTimeoutError:
                continue
            except FutureCancelledError:
                # Ours only when the future itself was cancelled (the
                # immediate-mode cancel() above beat the executor to it). A
                # CancelledError *raised by* a ran batch - an on_result
                # callback misusing the type - is a worker exception and must
                # honor the re-raise contract, not be mistaken for our cancel.
                if pending.cancelled():
                    return
                raise
            return

    def _cancelled(self) -> bool:
        return self._cancel_token is not None and self._cancel_token.cancelled

    def _run_batch(self, batch: list[FileInfo]) -> None:
        """Delete one batch, falling back for keys XML 1.0 cannot carry."""
        logger.debug("deleting %d object(s) from s3://%s", len(batch), self._bucket)
        batchable: list[tuple[int, FileInfo]] = []
        singles: list[tuple[int, FileInfo]] = []
        for index, info in enumerate(batch):
            (batchable if _delete_objects_compatible(info.key) else singles).append((index, info))
        errors: list[Boto3S3Error | None] = [None] * len(batch)
        deletes: list[dict[str, Any] | None] = [None] * len(batch)

        if batchable:
            self._run_delete_objects(batchable, errors, deletes)
        for index, info in singles:
            self._run_delete_object(index, info, errors, deletes)

        for index, info in enumerate(batch):
            self._record(info, errors[index], deletes[index])

    def _run_delete_objects(
        self,
        batch: list[tuple[int, FileInfo]],
        errors: list[Boto3S3Error | None],
        deletes: list[dict[str, Any] | None],
    ) -> None:
        """Delete the XML-compatible portion with one ``DeleteObjects`` request."""
        objects: list[ObjectIdentifierTypeDef] = [{"Key": info.key} for _, info in batch]
        # capture_response needs the per-key Deleted[] entries, which Quiet=True
        # suppresses; request the full (larger) response only then.
        quiet = not self._capture_response
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Delete": {"Objects": objects, "Quiet": quiet},
        }
        if self._request_payer is not None:
            kwargs["RequestPayer"] = self._request_payer
        failures: dict[str, Boto3S3Error]
        deleted: dict[str, dict[str, Any]] = {}
        try:
            with s3_errors(operation=self._operation, bucket=self._bucket):
                response = self._client.delete_objects(**kwargs)
        except Boto3S3Error as exc:
            # Request-level failure: every key in this batch failed with the
            # same translated cause; later batches still run. Anything
            # s3_errors does not translate (a programming error) propagates
            # and re-raises at the caller's next non-empty flush() or close().
            logger.debug("delete_objects failed for s3://%s: %s", self._bucket, exc)
            failures = {info.key: exc for _, info in batch}
        else:
            failures, unattributable = self._translate_errors(
                response.get("Errors", []), [info for _, info in batch]
            )
            if self._capture_response:
                deleted = self._delete_slots(response)
            if unattributable:
                self._fail_unconfirmed(batch, failures, deleted, unattributable)
        for index, info in batch:
            errors[index] = failures.get(info.key)
            deletes[index] = deleted.get(info.key)

    def _run_delete_object(
        self,
        index: int,
        info: FileInfo,
        errors: list[Boto3S3Error | None],
        deletes: list[dict[str, Any] | None],
    ) -> None:
        """Delete one XML-incompatible key through aws-cli's per-key route."""
        logger.debug(
            "deleting XML-incompatible key with DeleteObject: s3://%s/%s",
            self._bucket,
            info.key,
        )
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": info.key}
        if self._request_payer is not None:
            kwargs["RequestPayer"] = self._request_payer
        try:
            with s3_errors(operation=self._operation, bucket=self._bucket, key=info.key):
                response = self._client.delete_object(**kwargs)
        except Boto3S3Error as exc:
            errors[index] = exc
        else:
            if self._capture_response:
                deletes[index] = strip_response_metadata(response)

    def _delete_slots(self, response: DeleteObjectsOutputTypeDef) -> dict[str, dict[str, Any]]:
        """Per-key DeleteObject-shaped slots from a non-Quiet DeleteObjects response.

        capture_response reads ``Deleted[]`` (present only with ``Quiet=False``)
        into one slot per key: the entry minus its ``Key`` (already the result's
        key) plus the batch-wide ``RequestCharged`` - the shape a single
        DeleteObject would return, hiding the batch wire form (design/deleter.md).
        """
        charged = response.get("RequestCharged")
        slots: dict[str, dict[str, Any]] = {}
        for entry in response.get("Deleted", []):
            key = entry.get("Key")
            if key is None:
                continue
            slot: dict[str, Any] = {k: v for k, v in entry.items() if k != "Key"}
            # DeleteObjects reports a created delete marker's id under
            # DeleteMarkerVersionId; a single DeleteObject reports the same id
            # as VersionId. Rename so the slot really is DeleteObject-shaped
            # (we never delete by VersionId, so the slot cannot already have one).
            marker_version = slot.pop("DeleteMarkerVersionId", None)
            if marker_version is not None:
                slot.setdefault("VersionId", marker_version)
            if charged:
                slot["RequestCharged"] = charged
            slots[key] = slot
        return slots

    def _translate_errors(
        self, entries: list[ErrorTypeDef], batch: list[FileInfo]
    ) -> tuple[dict[str, Boto3S3Error], list[str]]:
        """Map the response ``Errors[]`` onto the submitted keys (worker thread).

        Quiet=True: the response lists failures only, so a submitted key absent
        from the mapping is provisionally a success. Returns those per-key
        failures plus one detail string per entry that cannot be attributed to
        a submitted key (no ``Key``, or a key spelled differently than we sent
        it); such an entry is logged as a warning and hands the caller the
        material for `_fail_unconfirmed`, which fails the rest of the batch
        closed rather than letting the absence-means-success synthesis invent
        a success for the key the entry was about.
        """
        if not entries:
            return {}, []
        submitted = {info.key for info in batch}
        failures: dict[str, Boto3S3Error] = {}
        unattributable: list[str] = []
        for err in entries:
            key = err.get("Key")
            # "Unknown" fallbacks mirror botocore's ClientError rendering, so a
            # synthesized per-key message reads like a real DeleteObject failure.
            code = err.get("Code", "Unknown")
            message = err.get("Message", "Unknown")
            if key is None or key not in submitted:
                logger.warning(
                    "unattributable DeleteObjects error for s3://%s: key=%r %s (%s)",
                    self._bucket,
                    key,
                    code,
                    message,
                )
                unattributable.append(f"key={key!r} {code} ({message})")
                continue
            logger.debug("delete failed for s3://%s/%s: %s (%s)", self._bucket, key, code, message)
            failures[key] = self._translate_key_error(code, message, key)
        return failures, unattributable

    def _fail_unconfirmed(
        self,
        batch: list[tuple[int, FileInfo]],
        failures: dict[str, Boto3S3Error],
        deleted: dict[str, dict[str, Any]],
        unattributable: list[str],
    ) -> None:
        """Fail every key an unattributable ``Errors[]`` entry left unconfirmed.

        An entry nobody can attribute means the response no longer says which
        submitted keys really went away, and under ``Quiet=True`` there is no
        positive per-key evidence at all - "everything not in ``Errors[]``
        succeeded" would report the very key that entry was about as deleted,
        licensing a caller to discard its own record of an object that may
        still exist. Fail closed instead: a key keeps its own attributable
        error (more informative), a ``Deleted[]`` entry (present only under
        ``capture_response``) is positive proof the key went away and stays a
        success, and every other key of the batch gets this synthesized
        failure. It carries the unattributable details so the failure line
        says why.
        """
        text = (
            "The DeleteObjects response carried an unattributable error entry "
            f"({'; '.join(unattributable)}), so this key's deletion cannot be confirmed"
        )
        for _, info in batch:
            if info.key in failures or info.key in deleted:
                continue
            failures[info.key] = Boto3S3Error(
                text, operation=self._operation, bucket=self._bucket, key=info.key
            )

    def _translate_key_error(self, code: str, message: str, key: str) -> Boto3S3Error:
        """Translate one per-key ``Errors[]`` entry into the exception taxonomy.

        Categories come from the table shared with the request-level path
        (``S3_CODE_CATEGORIES``), and the message mirrors botocore's
        ``ClientError`` str, so per-key and request-level failures read the
        same to callers (modulo botocore's retry-info suffix on a
        retries-exhausted request-level failure).
        """
        category = S3_CODE_CATEGORIES.get(code, Boto3S3Error)
        text = f"An error occurred ({code}) when calling the DeleteObjects operation: {message}"
        return category(text, operation=self._operation, bucket=self._bucket, key=key)

    def _record(
        self, info: FileInfo, error: Boto3S3Error | None, delete: dict[str, Any] | None = None
    ) -> None:
        """Update the rollup and dispatch one ``OpResult`` (worker thread).

        Counters update before ``on_result`` runs: if the callback raises, its
        record is already counted, the rest of the batch is abandoned, and the
        exception surfaces at the next non-empty ``flush()`` or at ``close()``.
        ``delete`` is the per-key response slot under ``capture_response`` (a
        successful delete only); it lands on ``extra_info["delete"]``.
        """
        if error is None:
            self._succeeded += 1
            outcome = OpOutcome.SUCCEEDED
        else:
            self._failed += 1
            if self._first_error is None:
                self._first_error = error
            outcome = OpOutcome.FAILED
        self._emit(info, outcome, error=error, delete=delete)

    def _emit(
        self,
        info: FileInfo,
        outcome: OpOutcome,
        *,
        error: Boto3S3Error | None = None,
        delete: dict[str, Any] | None = None,
    ) -> None:
        """Hand one ``OpResult`` to ``on_result``, without touching the rollup.

        Shared by the worker's completion records and by ``submit``'s inline
        DRYRUN record, so a rehearsal produces exactly the record shape a real
        delete would (design/opresult.md). It runs on whichever thread calls
        it, which is what makes the dryrun path's callback exception the
        caller's own.
        """
        if self._on_result is not None:
            self._on_result(
                OpResult(
                    transfer_type=TransferType.DELETE,
                    compare_key=info.compare_key if info.compare_key is not None else info.key,
                    outcome=outcome,
                    error=error,
                    src=f"s3://{self._bucket}/{info.key}",
                    src_info=info,
                    src_storage=self._storage,
                    extra_info={"delete": delete} if delete is not None else None,
                )
            )


__all__ = ["S3_DELETE_BATCH", "S3Deleter"]
