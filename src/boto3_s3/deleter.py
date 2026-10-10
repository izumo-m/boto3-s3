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
``Quiet=True`` plus the response ``Errors[]``). The requests ride the client
aws-cli's would: botocore's, or - when the run's ``TransferConfig`` selects the
CRT engine - the CRT client, whose retry policy and error text are aws-cli's
there (`crt_delete_sender`). Keys that cannot make the
DeleteObjects XML 1.0 round trip - characters the body cannot hold, and a
carriage return, which a response may hand back as a line feed - fall back to
per-key ``DeleteObject``, matching aws-cli instead of failing their whole batch
(control characters ride that route fine; an unpaired surrogate cannot be
carried by the HTTP layer on either route, and whether a listing can return
such a key at all is unverified).
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
from typing import TYPE_CHECKING, Any, cast

from boto3_s3 import crtsupport
from boto3_s3.crtrequest import died_without_answer
from boto3_s3.exceptions import Boto3S3Error, TransportError, ValidationError
from boto3_s3.s3storage import (
    S3_CODE_CATEGORIES,
    S3Storage,
    drop_like_aws,
    lost_like_aws,
    request_failure,
    s3_errors,
)
from boto3_s3.types import (
    CancelMode,
    CancelToken,
    OpOutcome,
    OpResult,
    TransferType,
    strip_response_metadata,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from boto3.s3.transfer import TransferConfig
    from boto3.session import Session
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import (
        DeleteObjectsOutputTypeDef,
        ErrorTypeDef,
        ObjectIdentifierTypeDef,
    )

    from boto3_s3.crtrequest import CrtRequestSender
    from boto3_s3.types import FileInfo, ResultCallback

logger = logging.getLogger(__name__)

# AWS DeleteObjects per-call hard limit (and the default batch size).
S3_DELETE_BATCH = 1000

# How many per-key DeleteObject requests re-send a batch's transient failures
# at once: aws-cli's default request concurrency, which is what sends its
# per-key deletes.
_RESEND_CONCURRENCY = 10


def _retries_disabled(client: Any) -> bool:
    """Whether ``client`` is configured for a single attempt per request.

    botocore records the resolved limit as ``total_max_attempts`` on the
    client's config (absent when left at the mode's default). One attempt is
    the "do not retry" setting - ``AWS_MAX_ATTEMPTS=1``, ``max_attempts=0`` -
    and re-sending a key would be exactly the retry it turns off. Read
    defensively: a client double may carry no config at all.
    """
    retries: Any = getattr(getattr(getattr(client, "meta", None), "config", None), "retries", None)
    return (
        isinstance(retries, dict) and cast("dict[str, Any]", retries).get("total_max_attempts") == 1
    )


# What a DeleteObjects round trip cannot carry verbatim. The complement of XML
# 1.0's Char production - C0 controls other than TAB/LF/CR, surrogate code
# points, and the two terminal BMP noncharacters - cannot be written at all;
# CR can be written (botocore sends it as a character reference) but not
# relied on coming back: an XML parser turns a literal CR into LF (end-of-line
# normalization), so a response naming the key with its CR unescaped is read
# as a different key. A compiled class instead of a per-character Python loop:
# `_run_batch` runs this over every submitted key.
_XML_INCOMPATIBLE = re.compile("[^\t\n\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def crt_delete_sender(
    client: S3Client,
    transfer_config: TransferConfig | None,
    *,
    operation: str,
    session: Session | None = None,
    crt_endpoint: str | None = None,
    crt_allow_absent_credentials: bool = False,
    crt_allow_lockless: bool = False,
    crt_region: crtsupport.CrtRegion = crtsupport.CLIENT_REGION,
    crt_sign_requests: bool | None = None,
) -> CrtRequestSender | None:
    """The CRT client a run's deletes ride, or ``None`` for botocore.

    aws-cli sends its deletes through the transfer manager it built for the
    run, so a run whose ``TransferConfig`` selects the CRT engine
    (`crtsupport.selects_crt`, boto3's rule - ``None`` is its ``'auto'``)
    deletes on the CRT client there: the CRT's retry policy applies (a request
    that dies without a response is sent up to six times whatever
    ``max_attempts`` says), and a failure that is not an HTTP answer is
    awscrt's own error. The engine is built here exactly as the transfer
    engine builds it - the same postures, the same classic fallback (``None``),
    construction failures classified at the same seam
    (`transfer.crt_engine_errors`) - and an explicit ``'crt'`` without a usable
    awscrt raises botocore's ``MissingDependencyException`` as a transfer's
    does. It does not know the route: aws-cli runs an S3-to-S3 operation on its
    classic client whatever the preference (``_compute_transfer_client_type``),
    which the caller applies by passing a classic config.
    """
    if not crtsupport.selects_crt(transfer_config):
        return None
    # Deferred: the transfer module is the engine's home and pulls in
    # s3transfer's manager, which a classic deleter never needs.
    from boto3_s3.transfer import crt_engine_errors

    with crt_engine_errors(operation):
        return crtsupport.create_crt_request_sender(
            client,
            transfer_config,
            endpoint=crtsupport.caller_endpoint(client, crt_endpoint),
            session=session,
            allow_absent_credentials=crt_allow_absent_credentials,
            allow_lockless=crt_allow_lockless,
            region=crt_region,
            sign_requests=crt_sign_requests,
        )


def _delete_objects_compatible(key: str) -> bool:
    """Whether *key* survives a ``DeleteObjects`` round trip as XML character data.

    ``DeleteObjects`` carries keys in an XML document, both ways. XML 1.0
    forbids the C0 controls other than TAB/LF/CR, surrogate code points, and
    the two terminal BMP noncharacters, so a key holding one cannot be sent.
    A carriage return can be sent - botocore escapes CR/LF before sending the
    document - but the response is the other half: its ``Errors[]`` names the
    failed keys, and a service writing the CR back unescaped has it normalized
    to LF by the parser. The failure then matches no submitted key (failing
    the whole batch closed), or matches a sibling key that differs only by
    that CR/LF - reporting the wrong one failed and the failed one deleted. So
    a key with a CR is not batched either. ``DeleteObject`` carries the key in
    the URL instead and is the route aws-cli uses for every key.
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
    keys and sends each incompatible key through a ``DeleteObject`` of its own
    (up to ten at a time) while the caller keeps submitting. Duplicate keys within a batch are
    passed through as-is (dedup is the caller's concern).

    Per-key completion is reported through ``on_result`` - one ``OpResult`` per
    submitted entry, in submission order within a batch (entries abandoned by
    ``close(flush=False)``, by an error-path close, or by a close under a
    cancelled token get no result, and so does a key whose own DeleteObject
    died without a response on botocore - `lost_like_aws`);
    rollup
    ``succeeded`` / ``failed`` / ``first_error`` are approximate while running
    and final after ``close``. The deleter never raises ``BatchError``
    itself - the caller builds one from the rollup (``first_error`` is the
    ``__cause__`` sample).

    ``transfer_config`` picks the client the requests ride, as it picks a
    transfer's engine: botocore's, or the CRT client when it selects the CRT
    engine (`crt_delete_sender`, built on the caller's thread during
    construction). ``session`` and the ``crt_*`` arguments are the CRT
    engine's postures, ``Transferrer``'s and ``S3``'s own; ``S3.rm`` and
    ``sync`` pass their instance's. A dryrun deleter builds no engine.

    ``dryrun=True`` turns the whole deleter into a rehearsal for a direct
    consumer: ``submit`` validates its entry exactly as a real run does and
    then emits one ``OpOutcome.DRYRUN`` record inline on the calling thread,
    buffering nothing and sending nothing to S3 (no worker is created at all,
    ``flush`` is a no-op, and the rollup counters stay 0 - a rehearsal has no
    successes). ``S3.rm`` and ``sync`` do not use it: they keep their own
    dryrun handling upstream (design/deleter.md section 5).

    The worker thread is started by the first dispatch (a
    ``ThreadPoolExecutor``'s), and interpreter shutdown joins it whichever
    thread started it - the executor registers that join itself, so a daemon
    caller earns no exemption - so an unclosed deleter holds shutdown until
    the in-flight batch finishes: use the context manager. Use a recursive
    scan: a non-recursive scan also yields
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
        transfer_config: TransferConfig | None = None,
        session: Session | None = None,
        crt_endpoint: str | None = None,
        crt_allow_absent_credentials: bool = False,
        crt_allow_lockless: bool = False,
        crt_region: crtsupport.CrtRegion = crtsupport.CLIENT_REGION,
        crt_sign_requests: bool | None = None,
    ) -> None:
        # Runtime guard for untyped callers (e.g. S3.resolve routing a bare
        # "bucket/key" to LocalStorage): fail inside the taxonomy, not with an
        # AttributeError from duck-typing.
        if not isinstance(storage, S3Storage):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValidationError(
                f"S3Deleter requires an S3Storage target, got {type(storage).__name__}",
                operation=operation,
            )
        if (
            not isinstance(batch_size, int)  # pyright: ignore[reportUnnecessaryIsInstance]
            or not 1 <= batch_size <= S3_DELETE_BATCH
        ):
            # Same taxonomy as the storage-type guard above: a caller-argument
            # problem raises ValidationError, not a bare ValueError. The type
            # is checked with the range because a float inside it (2.0, from
            # arithmetic) would pass here and only fail at the first flush,
            # as a TypeError slicing the buffer.
            raise ValidationError(
                f"batch_size must be an integer between 1 and {S3_DELETE_BATCH} "
                f"(got {batch_size!r})",
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
        # The CRT client these requests ride, None for botocore. A rehearsal
        # sends nothing, so it builds no engine.
        self._crt: CrtRequestSender | None = (
            None
            if dryrun
            else crt_delete_sender(
                self._client,
                transfer_config,
                operation=operation,
                session=session,
                crt_endpoint=crt_endpoint,
                crt_allow_absent_credentials=crt_allow_absent_credentials,
                crt_allow_lockless=crt_allow_lockless,
                crt_region=crt_region,
                crt_sign_requests=crt_sign_requests,
            )
        )
        # A transient per-key failure is re-sent unless the client's own
        # policy is not to retry (`_retries_disabled`). The CRT client retries
        # on its own policy whatever botocore's says, as it does for aws-cli's
        # per-key deletes, so its re-sends always go out.
        self._resend_transient = self._crt is not None or not _retries_disabled(self._client)
        # Set when the run is abandoned - close(flush=False) for anything but
        # a graceful cancel, or a close() that is itself interrupted - so a
        # batch in flight starts no further per-key request. Written on the
        # caller's thread, read on the workers'.
        self._abandoned = False

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
        if not flush and not self._graceful_cancel():
            # A graceful cancel is defined as draining what was accepted: rm
            # and sync leave the deleter through an exception then too (their
            # CancelledError), and the batch in flight must still send every
            # key it holds. Any other reason to skip the flush - an error, an
            # interrupt, the caller's own choice - abandons the run.
            self._abandoned = True
        try:
            if flush and not self._cancelled():
                self.flush()
            self._wait_pending()
        except BaseException:
            # Interrupted (Ctrl-C lands here when the last batch is the one in
            # flight) or re-raising a worker error: either way the run is
            # abandoned from this point, and the shutdown below must not wait
            # for re-sends that have not gone out.
            self._abandoned = True
            raise
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

    def _graceful_cancel(self) -> bool:
        token = self._cancel_token
        return token is not None and token.cancelled and token.mode is CancelMode.GRACEFUL

    def _run_batch(self, batch: list[FileInfo]) -> None:
        """Delete one batch, falling back for keys XML 1.0 cannot carry."""
        logger.debug("deleting %d object(s) from s3://%s", len(batch), self._bucket)
        batchable: list[tuple[int, FileInfo]] = []
        singles: list[tuple[int, FileInfo]] = []
        for index, info in enumerate(batch):
            (batchable if _delete_objects_compatible(info.key) else singles).append((index, info))
        errors: list[Boto3S3Error | None] = [None] * len(batch)
        deletes: list[dict[str, Any] | None] = [None] * len(batch)
        # A per-key request the run was abandoned before sending: no request,
        # so no record - the same as an entry still in the buffer.
        unsent: set[int] = set()
        # A key whose request died without a response, which aws-cli's own
        # per-key DeleteObject loses without a trace (`lost_like_aws`): no
        # record either, and no count.
        lost = [False] * len(batch)

        if batchable:
            self._run_delete_objects(batchable, errors, deletes, lost)
        if singles:
            self._send_singly(
                singles,
                errors,
                deletes,
                lost,
                purpose="deleting XML-incompatible key",
                not_sent=lambda index, _info: unsent.add(index),
            )

        for index, info in enumerate(batch):
            if index not in unsent and not lost[index]:
                self._record(info, errors[index], deletes[index])

    def _run_delete_objects(
        self,
        batch: list[tuple[int, FileInfo]],
        errors: list[Boto3S3Error | None],
        deletes: list[dict[str, Any] | None],
        lost: list[bool],
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
        transient: set[str] = set()
        try:
            with s3_errors(operation=self._operation, bucket=self._bucket):
                response = cast(
                    "DeleteObjectsOutputTypeDef",
                    self._crt.call("DeleteObjects", kwargs)
                    if self._crt is not None
                    else self._client.delete_objects(**kwargs),
                )
        except AssertionError:
            raise  # a test double's guard or an invariant, never a request outcome
        except Exception as exc:
            # Request-level failure: every key in this batch failed with the
            # same cause - translated by s3_errors, or wrapped by
            # request_failure when the request raised outside the boto family -
            # unless the cause says nothing final about the keys (below), in
            # which case each goes out on its own; later batches still run.
            # What the deleter's own code raises after the request (a
            # programming error) still propagates and re-raises at the
            # caller's next non-empty flush() or close().
            failure = request_failure(exc, operation=self._operation, bucket=self._bucket)
            logger.debug("delete_objects failed for s3://%s: %s", self._bucket, failure)
            if lost_like_aws(failure) or (self._crt is not None and died_without_answer(failure)):
                # Died without a response - on botocore, or on the CRT client
                # once its own retries were spent. That says nothing about the
                # per-key DeleteObject aws-cli sends for each of these keys -
                # a batch past the read timeout, an endpoint that drops only
                # the batch call, a flaky connection that ran through the one
                # request's attempts where each of aws-cli's requests has
                # attempts of its own - so each key goes out on that route of
                # the same client, where its own outcome (a delete, a failure,
                # or on botocore a loss) is decided as aws-cli's is. A key an
                # abandoned run leaves unsent gets no record, like aws-cli's
                # never-started future.
                self._send_singly(
                    batch,
                    errors,
                    deletes,
                    lost,
                    purpose="resending a DeleteObjects request that died without a response",
                    not_sent=lambda index, _info: lost.__setitem__(index, True),
                )
                return
            failures = {info.key: failure for _, info in batch}
            if isinstance(failure, TransportError):
                # The request as a whole met a fault the service asks to have
                # retried, or a connection that failed, and the client spent
                # its attempts on it: one request's attempts, where each of
                # aws-cli's per-key requests has its own (measured: a server
                # failing the first three deletes with InternalError leaves
                # aws deleting both keys at rc 0). So each key goes out again
                # on the per-key route, whatever the retry policy - the batch
                # was no key's own request - and takes that request's outcome;
                # a key the run is abandoned before re-sending keeps this
                # error.
                self._resend(batch, failures, errors, deletes, lost)
                return
        else:
            failures, unattributable = self._translate_errors(
                response.get("Errors", []), [info for _, info in batch]
            )
            if self._capture_response:
                deleted = self._delete_slots(response)
            # Read before the fail-closed synthesis below adds its own entries:
            # only a key the service itself reported a passing fault for.
            if self._resend_transient:
                transient = {
                    key for key, failure in failures.items() if isinstance(failure, TransportError)
                }
            if unattributable:
                self._fail_unconfirmed(batch, failures, deleted, unattributable)
        resend: list[tuple[int, FileInfo]] = []
        for index, info in batch:
            if info.key in transient:
                resend.append((index, info))
                continue
            errors[index] = failures.get(info.key)
            # A slot belongs to a success only: a response listing one key
            # under both Deleted[] and Errors[] is reported by its error.
            deletes[index] = None if errors[index] is not None else deleted.get(info.key)
        if resend:
            self._resend(resend, failures, errors, deletes, lost)

    def _resend(
        self,
        keys: list[tuple[int, FileInfo]],
        failures: dict[str, Boto3S3Error],
        errors: list[Boto3S3Error | None],
        deletes: list[dict[str, Any] | None],
        lost: list[bool],
    ) -> None:
        """Send the batch's transiently failed keys again, one ``DeleteObject`` each.

        Also the route of every key of a batch request that failed as a whole
        with a transient fault (`_run_delete_objects`), which is re-sent
        whatever the retry policy. Otherwise:
        the service answered each of these keys with a fault it asks the
        caller to retry (InternalError, SlowDown, ...). aws-cli, sending one
        DeleteObject per key, has its client - botocore, or the CRT - retry
        exactly that, so the key goes out again on the per-key route of the
        same client, where that client's own retry policy applies and the
        outcome - a delete, or the error once the attempts are spent - is the
        one aws-cli reports. The batch entry is not counted against that
        policy, so such a key gets one attempt more than aws-cli gives it -
        except under botocore with a no-retry policy, where nothing is re-sent
        at all (`_retries_disabled`; the CRT client retries whatever that
        policy says).

        A throttled endpoint can fail every key of a batch this way, so the
        re-sends go out side by side and stop being started once the run is
        abandoned (`_send_singly`): a key not re-sent keeps the error the
        batch reported for it.
        """

        def keep_batch_error(index: int, info: FileInfo) -> None:
            errors[index] = failures[info.key]

        self._send_singly(
            keys,
            errors,
            deletes,
            lost,
            purpose="retrying transient DeleteObjects failure",
            not_sent=keep_batch_error,
        )

    def _send_singly(
        self,
        entries: list[tuple[int, FileInfo]],
        errors: list[Boto3S3Error | None],
        deletes: list[dict[str, Any] | None],
        lost: list[bool],
        *,
        purpose: str,
        not_sent: Callable[[int, FileInfo], None],
    ) -> None:
        """Send each entry as a ``DeleteObject`` of its own, several at a time.

        The per-key route of a batch: the keys XML 1.0 cannot carry, and the
        re-sends of transient per-key failures. Either can be most of a
        batch - a prefix of keys holding a carriage return, an endpoint
        throttling every key - so the requests run `_RESEND_CONCURRENCY` at a
        time (aws-cli's own concurrency for its per-key deletes; each may sit
        through the client's backoff) instead of in line on the one worker.

        No further request is started once the run is abandoned -
        ``close(flush=False)`` for any reason but a graceful cancel (which
        drains the batch in flight, these requests included), a ``close()``
        that was itself interrupted, or the token cancelled in IMMEDIATE
        mode; ``not_sent`` is told about each
        entry left out, and the requests already out finish, as the batch
        request itself does. An unclosed deleter's last batch can get here
        while the interpreter is shutting down, when no thread can be started
        any more: the entries the pool refuses are sent in line, on the
        worker that shutdown is waiting for.
        """

        def send(entry: tuple[int, FileInfo]) -> None:
            index, info = entry
            if self._abandoned or (
                self._cancel_token is not None and self._cancel_token.mode is CancelMode.IMMEDIATE
            ):
                not_sent(index, info)
                return
            logger.debug("%s with DeleteObject: s3://%s/%s", purpose, self._bucket, info.key)
            self._run_delete_object(index, info, errors, deletes, lost)

        if len(entries) == 1:
            send(entries[0])
            return
        with ThreadPoolExecutor(
            max_workers=min(len(entries), _RESEND_CONCURRENCY),
            thread_name_prefix="boto3-s3-deleter-resend",
        ) as pool:
            started: list[Future[None]] = []
            refused: list[tuple[int, FileInfo]] = []
            for entry in entries:
                try:
                    started.append(pool.submit(send, entry))
                except RuntimeError:  # "cannot schedule new futures after interpreter shutdown"
                    refused.append(entry)
            for entry in refused:
                send(entry)
            # Read for their exceptions: an AssertionError from a request (a
            # test double's guard, an invariant) must reach the worker's
            # caller like one from the batch request.
            for future in started:
                future.result()

    def _run_delete_object(
        self,
        index: int,
        info: FileInfo,
        errors: list[Boto3S3Error | None],
        deletes: list[dict[str, Any] | None],
        lost: list[bool],
    ) -> None:
        """Delete one key through aws-cli's per-key route, on the run's client.

        Taken by a key the batch cannot carry, and by a key the batch response
        reported a transient fault for - the request the client's retry policy
        covers, which a per-key entry of a 200 response never reaches. A
        request that died without a response is lost as aws-cli loses it
        (`drop_like_aws`), which only a botocore failure can be.
        """
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": info.key}
        if self._request_payer is not None:
            kwargs["RequestPayer"] = self._request_payer
        try:
            with s3_errors(operation=self._operation, bucket=self._bucket, key=info.key):
                response = (
                    self._crt.call("DeleteObject", kwargs)
                    if self._crt is not None
                    else self._client.delete_object(**kwargs)
                )
        except AssertionError:
            raise  # as in _run_delete_objects
        except Exception as exc:
            failure = request_failure(
                exc, operation=self._operation, bucket=self._bucket, key=info.key
            )
            if drop_like_aws(failure):
                lost[index] = True
            else:
                errors[index] = failure
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
