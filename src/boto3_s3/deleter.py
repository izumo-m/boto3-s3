"""Async batched S3 object deletion: ``S3Deleter``.

``S3Deleter`` buffers listing entries (``FileInfo``) and deletes each batch with
one ``DeleteObjects`` call (up to ``S3_DELETE_BATCH`` keys) dispatched on a
single background worker thread, so a caller iterating ``S3Storage.scan()``
keeps scanning while the previous batch deletes. It is the building block for
``S3.rm`` and ``S3.sync(delete_filter=True)``, and is usable directly.

aws-cli note: ``aws s3 rm`` deletes one key per ``DeleteObject`` call and never
uses the batch API. The batched ``DeleteObjects`` here is an accepted
wire-level deviation that is observably equivalent for ordinary keys (a
nonexistent key deletes "successfully" either way, and per-key success/failure
is preserved via ``Quiet=True`` plus the response ``Errors[]``); the known gap
is keys the DeleteObjects XML body cannot carry (e.g. control characters),
which fail their whole batch here while aws-cli's per-key path deletes them.
User-facing lines such as ``delete: s3://...`` are the CLI layer's job, fed by
``on_result``; the library only emits ``logging`` diagnostics.

Threading contract: ``submit`` / ``flush`` / ``close`` belong to one caller
thread (single producer). ``on_result`` is invoked from the worker thread and
must be fast and must not raise (if it does, the exception surfaces at the
next non-empty ``flush()`` or at ``close()``). At most one batch is in flight:
a dispatch first waits for the previous batch - the backpressure point, and
where an unexpected worker exception re-raises on the caller thread.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from boto3_s3.exceptions import Boto3S3Error, ValidationError
from boto3_s3.s3storage import S3_CODE_CATEGORIES, S3Storage, s3_errors
from boto3_s3.types import OpOutcome, OpResult, TransferType

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


class S3Deleter:
    """Buffer listing entries and delete them in batched ``DeleteObjects`` calls.

    ``submit`` takes a ``FileInfo``; its ``key`` is the
    FULL object key to delete, and the rest of the entry rides along untouched
    (a richer subtype - ``S3FileInfo`` with its ``etag`` - flows straight
    through). The target bucket and client come from ``storage`` - its key/prefix
    part is not consulted, and the client is held for the deleter's whole
    lifetime (do not ``storage.close()`` until this deleter is closed). The
    buffer auto-flushes at ``batch_size``; each flush runs as one
    ``DeleteObjects`` call on the worker while the caller keeps submitting.
    Duplicate keys within a batch are passed through as-is (dedup is the caller's
    concern).

    Per-key completion is reported through ``on_result`` - one ``OpResult`` per
    submitted entry, in submission order within a batch (entries abandoned by
    ``close(flush=False)`` or by an error-path close get no result); rollup
    ``succeeded`` / ``failed`` / ``first_error`` are approximate while running
    and final after ``close``. The deleter never raises ``BatchError``
    itself - the caller builds one from the rollup (``first_error`` is the
    ``__cause__`` sample).

    The worker thread is non-daemon, so an unclosed deleter keeps the
    interpreter alive until the in-flight batch finishes - use the context
    manager. Use a recursive scan: a non-recursive scan also yields
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
        batch_size: int = S3_DELETE_BATCH,
        operation: str = "delete",
        capture_response: bool = False,
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
            raise ValueError(
                f"batch_size must be between 1 and {S3_DELETE_BATCH} (got {batch_size!r})"
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
        self._batch_size = batch_size
        self._operation = operation
        self._capture_response = capture_response

        self._buffer: list[FileInfo] = []
        self._pending: Future[None] | None = None  # at most one in-flight batch
        self._closed = False

        # Rollup state: written only by the worker thread (single worker,
        # batches serialized); approximate while running, final after close().
        self._succeeded = 0
        self._failed = 0
        self._first_error: Boto3S3Error | None = None

        # Spawns its worker thread lazily on the first dispatch. Created last
        # so a constructor failure leaves nothing behind.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="boto3-s3-deleter")

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
        # A body exception abandons the unsent buffer but still waits for the
        # in-flight batch; a worker error raised here then propagates with the
        # body exception chained as __context__.
        self.close(flush=exc_type is None)

    def close(self, *, flush: bool = True) -> None:
        """Flush (unless ``flush=False``), wait for in-flight work, shut down.

        Idempotent. Afterwards the rollup counters are final and ``submit`` /
        ``flush`` raise ``ValidationError``. An unexpected worker exception (or
        one raised by ``on_result``) re-raises here; the deleter still ends up
        closed and the worker shut down either way, and any entries left in the
        buffer by that re-raise (or by ``flush=False``) are abandoned without
        results.
        """
        if self._closed:
            return
        try:
            if flush:
                self.flush()
            self._wait_pending()
        finally:
            self._closed = True
            self._buffer = []  # non-empty only when flush=False or flush() raised
            self._executor.shutdown(wait=True)

    # -- submission --------------------------------------------------------

    def submit(self, info: FileInfo) -> None:
        """Buffer one entry to delete; auto-flush when ``batch_size`` is reached.

        ``info.key`` is the full object key. An empty key is rejected up front:
        S3 requires keys of length >= 1, and one empty key would fail its entire
        batch. The entry stays buffered even when the auto-flush re-raises a
        previous batch's worker error - do not submit it again after catching
        that error.
        """
        self._ensure_open()
        if not info.key:
            raise ValidationError(
                "object key must not be empty", operation=self._operation, bucket=self._bucket
            )
        self._buffer.append(info)
        if len(self._buffer) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """Hand the buffered entries to the worker, one batch per ``batch_size``.

        No-op when the buffer is empty. Each dispatch first waits for the
        previous batch - the backpressure point, and where an unexpected
        worker exception re-raises on the caller thread (the entries not yet
        dispatched then stay buffered; nothing is lost). The buffer only
        exceeds ``batch_size`` after such a re-raise; the loop re-chunks it so
        a single call never carries more than ``batch_size`` entries.
        """
        self._ensure_open()
        while self._buffer:
            self._wait_pending()
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
        pending = self._pending
        if pending is None:
            return
        self._pending = None  # cleared first: a failed batch is reported once
        pending.result()

    def _run_batch(self, batch: list[FileInfo]) -> None:
        """Delete one batch with a single ``DeleteObjects`` call (worker thread)."""
        logger.debug("deleting %d object(s) from s3://%s", len(batch), self._bucket)
        objects: list[ObjectIdentifierTypeDef] = [{"Key": info.key} for info in batch]
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
        # Intentional aws-cli wire-level deviation: recursive rm and sync --delete
        # use DeleteObjects batches for throughput instead of aws-cli's per-key
        # DeleteObject. This is accepted and documented in docs/deleter.md section 4.
        # Do not swap in per-key deletes for parity unless that design decision
        # changes; the known non-parity case is keys that cannot be represented in
        # the DeleteObjects XML body (e.g. some control characters).
        try:
            with s3_errors(operation=self._operation, bucket=self._bucket):
                response = self._client.delete_objects(**kwargs)
        except Boto3S3Error as exc:
            # Request-level failure: every key in this batch failed with the
            # same translated cause; later batches still run. Anything
            # s3_errors does not translate (a programming error) propagates
            # and re-raises at the caller's next non-empty flush() or close().
            logger.debug("delete_objects failed for s3://%s: %s", self._bucket, exc)
            failures = {info.key: exc for info in batch}
        else:
            failures = self._translate_errors(response.get("Errors", []), batch)
            if self._capture_response:
                deleted = self._delete_slots(response)
        for info in batch:
            self._record(info, failures.get(info.key), deleted.get(info.key))

    def _delete_slots(self, response: DeleteObjectsOutputTypeDef) -> dict[str, dict[str, Any]]:
        """Per-key DeleteObject-shaped slots from a non-Quiet DeleteObjects response.

        capture_response reads ``Deleted[]`` (present only with ``Quiet=False``)
        into one slot per key: the entry minus its ``Key`` (already the result's
        key) plus the batch-wide ``RequestCharged`` - the shape a single
        DeleteObject would return, hiding the batch wire form (docs/deleter.md).
        """
        charged = response.get("RequestCharged")
        slots: dict[str, dict[str, Any]] = {}
        for entry in response.get("Deleted", []):
            key = entry.get("Key")
            if key is None:
                continue
            slot: dict[str, Any] = {k: v for k, v in entry.items() if k != "Key"}
            if charged:
                slot["RequestCharged"] = charged
            slots[key] = slot
        return slots

    def _translate_errors(
        self, entries: list[ErrorTypeDef], batch: list[FileInfo]
    ) -> dict[str, Boto3S3Error]:
        """Map the response ``Errors[]`` onto the submitted keys (worker thread).

        Quiet=True: the response lists failures only, so a submitted key absent
        from the mapping is recorded as a success. An entry that cannot be
        attributed to a submitted key (no ``Key``, or a key spelled differently
        than we sent it - the XML parser normalizes line endings, for one) is
        logged as a warning rather than silently inverting into a success with
        no trace.
        """
        if not entries:
            return {}
        submitted = {info.key for info in batch}
        failures: dict[str, Boto3S3Error] = {}
        for err in entries:
            key = err.get("Key")
            code = err.get("Code", "Unknown")
            message = err.get("Message", "no message")
            if key is None or key not in submitted:
                logger.warning(
                    "unattributable DeleteObjects error for s3://%s: key=%r %s (%s)",
                    self._bucket,
                    key,
                    code,
                    message,
                )
                continue
            logger.debug("delete failed for s3://%s/%s: %s (%s)", self._bucket, key, code, message)
            failures[key] = self._translate_key_error(code, message, key)
        return failures

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
        if self._on_result is not None:
            self._on_result(
                OpResult(
                    transfer_type=TransferType.DELETE,
                    key=info.key,
                    outcome=outcome,
                    error=error,
                    src=f"s3://{self._bucket}/{info.key}",
                    src_info=info,
                    src_storage=self._storage,
                    extra_info={"delete": delete} if delete is not None else None,
                )
            )


__all__ = ["S3_DELETE_BATCH", "S3Deleter"]
