"""The boto3-s3 ``S3``: entry point for ``aws s3``-style operations.

``S3`` holds no connection of its own; it is a pure orchestrator. Every
``Location`` argument resolves to a ``Storage``: a local path becomes a
``LocalStorage`` and an ``"s3://..."`` string becomes an ``S3Storage``. The
boto3 client lives on the ``S3Storage``; when its client is omitted it falls
back to ``boto3.client("s3")``. To target a custom endpoint / profile / region
(e.g. MinIO) or a second account for S3-to-S3, pass an explicit
``S3Storage(uri, client=...)`` instead of a bare string.
"""

from __future__ import annotations

import functools
import inspect
import os
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Executor, Future
from contextlib import ExitStack
from dataclasses import dataclass, replace
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any, Concatenate, Generic, Literal, ParamSpec, TypeVar, cast

from typing_extensions import Unpack

from boto3_s3 import producers, transferplan
from boto3_s3.awsclicompare import AwsCliComparison
from boto3_s3.comparator import (
    Comparator,
    DestOnlyPair,
    MergedPair,
    PairFilter,
    ParallelFilter,
    SrcOnlyPair,
    SyncPair,
)
from boto3_s3.deleter import S3Deleter
from boto3_s3.exceptions import (
    BatchError,
    Boto3S3Error,
    CancelledError,
    InvalidConfigError,
    NotFoundError,
    ValidationError,
)
from boto3_s3.iostorage import IOStorage
from boto3_s3.localstorage import LocalStorage, to_native_path, translate_os_error
from boto3_s3.s3storage import S3Storage, s3_errors, translate_boto_error
from boto3_s3.storage import Location, Storage
from boto3_s3.transfer import TransferItem, Transferrer
from boto3_s3.types import (
    CancelMode,
    CancelToken,
    FileFilter,
    FileInfo,
    ListingCallback,
    OpOutcome,
    OpResult,
    ProgressCallback,
    ResultCallback,
    S3FileInfo,
    TransferOptions,
    TransferType,
    strip_response_metadata,
)

if TYPE_CHECKING:
    # Annotation-only: importing it for real would drag boto3 + s3transfer into
    # every `boto3_s3` import (import contract, docs/imports.md).
    from boto3 import Session
    from boto3.s3.transfer import TransferConfig
    from botocore.config import Config
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import WebsiteConfigurationTypeDef

    # Local + SDK-free, but kept annotation-only so `aws_config()` stays an
    # opt-in module load (the reader's botocore touch is deferred into it).
    from boto3_s3.awsconfig import AwsConfig


def rm_filter_root(key: str, *, recursive: bool) -> str:
    """The filter prefix an ``rm`` of ``key`` operates under (aws-cli parity).

    A recursive target is normalized to end with ``/`` (aws-cli
    ``FileFormat.s3_format``): ``rm s3://b/data --recursive`` lists under
    ``data/`` and does not touch ``data-sibling.txt``. A non-recursive key uses
    its parent directory, and a bucket-root target uses ``""``
    (aws-cli ``filters._get_s3_root``). ``--exclude`` / ``--include``
    patterns resolve relative to this prefix, and the recursive listing uses
    it as the ``Prefix``. Always empty or ``/``-terminated.
    """
    if recursive:
        return f"{key}/" if key and not key.endswith("/") else key
    if not key or key.endswith("/"):
        return key
    head, sep, _tail = key.rpartition("/")
    return f"{head}/" if sep else ""


# Every key TransferOptions accepts (the TypedDict is total=False, so the
# two frozensets union to the full key set regardless of future totality).
_TRANSFER_OPTION_KEYS = frozenset(TransferOptions.__required_keys__) | frozenset(
    TransferOptions.__optional_keys__
)


def _validate_transfer_options(options: TransferOptions, *, operation: str) -> None:
    """Reject unknown ``**options`` keys eagerly (pre-pipeline `ValidationError`).

    ``Unpack[TransferOptions]`` already enforces the key set for type-checked
    callers, but an unchecked caller's typo (``dry_run`` for ``dryrun``) would
    otherwise be silently ignored - and on ``mv`` still delete the source.
    """
    unknown = sorted(set(options) - _TRANSFER_OPTION_KEYS)
    if unknown:
        raise ValidationError(
            f"Unknown transfer option(s): {', '.join(unknown)}", operation=operation
        )


def _emit_result(
    on_result: ResultCallback | None,
    *,
    info: FileInfo,
    storage: S3Storage,
    outcome: OpOutcome,
    error: Boto3S3Error | None = None,
    extra_info: Mapping[str, Any] | None = None,
) -> None:
    if on_result is not None:
        on_result(
            OpResult(
                transfer_type=TransferType.DELETE,
                compare_key=info.compare_key or info.key,
                outcome=outcome,
                error=error,
                src=f"s3://{storage.bucket}/{info.key}",
                src_info=info,
                src_storage=storage,
                extra_info=extra_info,
            )
        )


def _raise_if_cancelled(cancel_token: CancelToken | None, operation: str) -> None:
    if cancel_token is not None and cancel_token.cancelled:
        raise CancelledError(f"{operation} was cancelled", operation=operation)


def _is_folder_marker(info: FileInfo) -> bool:
    """A zero-byte ``/``-terminated key - the manual-folder convention."""
    return info.size == 0 and info.key.endswith("/")


def _always(_pair: object) -> bool:
    """A lane decision that always acts (``create_filter=True`` copies every new
    entry, ``update_filter=True`` every update, ``delete_filter=True`` every orphan)."""
    return True


def _never(_pair: object) -> bool:
    """A lane decision that never acts (``create_filter=False`` no new entry,
    ``update_filter=False`` no update - additive-only, existing left as-is)."""
    return False


def _create_via_filter(keep: FileFilter) -> Callable[[SrcOnlyPair], bool]:
    """``create_filter`` as a ``FileFilter``: copy a new (``SrcOnlyPair``) entry iff
    its source entry is kept (matched like ``rm``)."""

    def decide(pair: SrcOnlyPair) -> bool:
        return keep(pair.src)

    return decide


def _delete_via_filter(keep: FileFilter) -> Callable[[DestOnlyPair], bool]:
    """``delete_filter`` as a ``FileFilter``: delete an orphan (``DestOnlyPair``)
    iff its destination entry is kept (matched like ``rm``)."""

    def decide(pair: DestOnlyPair) -> bool:
        return keep(pair.dest)

    return decide


_PairT = TypeVar("_PairT")


def _resolve_side_lane(
    flt: bool | FileFilter | ParallelFilter[FileInfo],
    wrap: Callable[[FileFilter], Callable[[_PairT], bool]],
) -> tuple[Callable[[_PairT], bool], Executor | None]:
    """Resolve a create / delete lane's filter to ``(decide, executor)``.

    ``True`` acts on every pair, ``False`` on none; a ``FileFilter`` is turned by
    ``wrap`` (``_create_via_filter`` / ``_delete_via_filter``) into a decision
    over the lane's pair shape; a ``ParallelFilter`` unwraps to that same filter
    plus the caller's pool (the executor its ``decide`` should run on).
    """
    if flt is True:
        return _always, None
    if flt is False:
        return _never, None
    if isinstance(flt, ParallelFilter):
        pooled = cast("ParallelFilter[FileInfo]", flt)  # isinstance loses the type arg; pin it
        return wrap(pooled.decide), pooled.executor
    return wrap(flt), None


def _pool_window(executor: Executor) -> int:
    """The outstanding-decision cap for one ``ParallelFilter`` executor.

    ``S3.sync`` submits each pooled decision as a ``Future`` and keeps at most
    this many outstanding across the run (summed over distinct executors), so a
    huge listing is never materialized into futures all at once - the caller's
    pool never blocks on ``submit`` (its queue is unbounded), so holding the
    bound is ours. Matches the pool's worker count when discoverable
    (``ThreadPoolExecutor._max_workers``, universally present on 3.10+) so the
    pool stays fed, else a small constant. Throughput-only: it bounds outstanding
    decisions, never correctness.
    """
    workers: object = getattr(executor, "_max_workers", None)
    return workers if isinstance(workers, int) and workers >= 1 else 16


@dataclass(frozen=True, slots=True)
class _Lane(Generic[_PairT]):
    """One of ``S3.sync``'s three pair lanes: how to decide and act on it.

    ``_PairT`` is the lane's pair shape (``SrcOnlyPair`` / ``SyncPair`` /
    ``DestOnlyPair``). ``decide`` is the predicate selecting whether to act on a
    pair; ``submit`` performs it (``submit_copy`` for create / update,
    ``submit_delete`` for delete). ``executor`` is the caller's pool when the
    lane's filter was wrapped in ``ParallelFilter`` - then ``decide`` runs
    there and ``submit`` runs on the calling thread once the result is in; ``None``
    runs both inline in compare-key order.
    """

    decide: Callable[[_PairT], bool]
    submit: Callable[[_PairT], None]
    executor: Executor | None


def _run_sync_pairs(
    pairs: Iterator[MergedPair],
    *,
    create_lane: _Lane[SrcOnlyPair],
    update_lane: _Lane[SyncPair] | None,
    delete_lane: _Lane[DestOnlyPair] | None,
    cancel_token: CancelToken | None,
) -> None:
    """Drive ``S3.sync``'s pair loop, routing each pair to its lane.

    Each pair lands in exactly one lane by its type: a ``SrcOnlyPair`` (new)
    -> ``create_lane``, a ``SyncPair`` (update) -> ``update_lane``
    (``None`` when ``no_overwrite`` rules every update out), a ``DestOnlyPair``
    (orphan) -> ``delete_lane`` (``None`` when ``--delete`` is off). A lane
    carrying an ``executor`` runs its ``decide`` on that pool; the survivors'
    ``submit`` (and the gates it reaches - the case-conflict gate, glacier, etc.)
    always run on this calling thread. Pooled decisions are consumed in
    completion order, so a parallelized ``create_lane`` submits out of
    compare-key order and the case-conflict "first key wins" becomes
    non-deterministic (a library-only knob, no ``aws s3`` parity at stake). A
    ``decide`` exception propagates here (aborting the sync) when its result is
    consumed. Before returning from a decision, action, or pair-producer
    failure, decisions that have not started are cancelled and running decisions
    are awaited, so none outlive the operation.
    `cancel_token` is polled before decisions and actions. Graceful cancellation
    awaits outstanding decisions; immediate mode first calls `cancel()` on their
    futures. The caller's executor is never shut down.
    """

    pending: set[Future[bool]] = set()
    # A pooled decision's completion delivers its pre-bound submit (the lane's
    # ``submit`` closed over its pair), so the queue needs no pair/lane fields -
    # each lane keeps its own pair shape without a shared existential type.
    done: Queue[tuple[Callable[[], None], Future[bool]]] = Queue()

    def settle_pending(*, cancel: bool) -> None:
        """Optionally cancel queued decisions, then await every accepted one."""
        remaining = pending
        while remaining:
            if cancel:
                for future in remaining:
                    future.cancel()
            remaining = {future for future in remaining if not future.done()}
            if remaining:
                time.sleep(0.1)

    def stop_if_cancelled() -> None:
        """Apply the token mode to accepted decisions, then raise cancellation."""
        if cancel_token is None or not cancel_token.cancelled:
            return
        settle_pending(cancel=cancel_token.mode is CancelMode.IMMEDIATE)
        raise CancelledError("sync was cancelled", operation="sync")

    def handle(lane: _Lane[_PairT] | None, pair: _PairT) -> bool:
        """Decide-and-act on one routed pair; ``True`` iff a decision was pooled."""
        if lane is None:
            return False
        executor = lane.executor
        if executor is None:
            accepted = lane.decide(pair)
            stop_if_cancelled()
            if accepted:
                lane.submit(pair)
            return False
        future = executor.submit(lane.decide, pair)
        pending.add(future)
        submit = functools.partial(lane.submit, pair)
        future.add_done_callback(lambda f: done.put((submit, f)))
        return True

    def handle_pair(pair: MergedPair) -> bool:
        if isinstance(pair, SrcOnlyPair):
            return handle(create_lane, pair)
        if isinstance(pair, SyncPair):
            return handle(update_lane, pair)
        return handle(delete_lane, pair)

    stop_if_cancelled()

    # The outstanding-decision cap is the distinct pools' worker counts summed
    # (a pool shared across lanes counts once). It bounds total in-flight futures
    # so a huge listing is never materialized at once - a combined bound, not
    # per-pool isolation: a run of one lane's pairs can fill the cap on that
    # lane's pool while another pool sits idle for lack of its own work (harmless;
    # a pool's own submit queue absorbs the surplus). Empty when no lane is pooled
    # -> the serial path.
    pools = {
        lane.executor
        for lane in (create_lane, update_lane, delete_lane)
        if lane is not None and lane.executor is not None
    }
    if not pools:
        for pair in pairs:
            stop_if_cancelled()
            handle_pair(pair)
        return

    cap = sum(_pool_window(pool) for pool in pools)
    pairs_iter = iter(pairs)

    def dispatch() -> bool:
        # Advance the stream, handling non-pooled lanes inline in compare-key
        # order, until one pooled decision is submitted (return True) or the
        # stream is exhausted (return False).
        for pair in pairs_iter:
            stop_if_cancelled()
            if handle_pair(pair):
                return True
        return False

    try:
        inflight = 0
        while inflight < cap and dispatch():
            inflight += 1
        while inflight:
            stop_if_cancelled()
            try:
                submit, future = done.get(timeout=0.1)
            except Empty:
                continue
            pending.discard(future)
            inflight -= 1
            stop_if_cancelled()
            if future.result():
                submit()
            if dispatch():
                inflight += 1
    finally:
        # An exception from a decision, a lane action, or the pair producer must
        # not leave accepted predicates running after sync has cleaned up its
        # transfer manager and storages. Cancel work that has not started and
        # await the rest without shutting down the caller-owned executor. Polling
        # `done()` also handles a plain `Future` cancelled before a waiter was
        # registered. Results are not read, preserving the error that triggered
        # this cleanup.
        if pending:
            settle_pending(cancel=True)


def _check_local_source_exists(storage: LocalStorage, *, operation: str) -> None:
    """aws-cli's ``_validate_path_args`` missing-source check, run up front.

    (Their bare RuntimeError -> rc 255; ``NotFoundError`` without a
    ``ClientError`` cause maps the same.)
    """
    if not os.path.exists(storage.path):
        raise NotFoundError(
            f"The user-provided path {storage.path} does not exist.", operation=operation
        )


def _classify_transfer_route(
    plan: transferplan.TransferPlan, *, operation: str
) -> tuple[TransferType, S3Storage, S3Storage | None, str]:
    """Map a plan's route onto the engine wiring cp/mv and sync share.

    Returns ``(transfer_type, client_provider, source_client_provider,
    dest_bucket)``: the providers are the S3 side(s) whose ``get_client()``
    drives s3transfer (``source_client_provider`` only on the s3s3 route) -
    handed back unresolved so each caller keeps its established effect order
    (its destination-directory creation runs before any client is built).
    The built-in routes assert the concrete ``LocalStorage`` / ``S3Storage``
    pair, because the engine reaches into ``S3Storage``'s client/bucket and
    ``LocalStorage``'s path directly; an open route pairs a capability-checked
    custom backend with S3. The missing-local-source check runs here
    (identical in cp/mv and sync); the destination-directory creation does
    not - its guard shapes deliberately differ (cp's recursive-gated
    ``exist_ok=True`` vs sync's unconditional bare ``makedirs``) and stay with
    the callers.
    """
    src_storage = plan.src
    dest_storage = plan.dest
    if plan.paths_type == "locals3":
        assert isinstance(src_storage, LocalStorage) and isinstance(dest_storage, S3Storage)
        _check_local_source_exists(src_storage, operation=operation)
        return TransferType.UPLOAD, dest_storage, None, dest_storage.bucket
    if plan.paths_type == "s3local":
        assert isinstance(src_storage, S3Storage) and isinstance(dest_storage, LocalStorage)
        return TransferType.DOWNLOAD, src_storage, None, ""
    if plan.paths_type == "s3s3":
        assert isinstance(src_storage, S3Storage) and isinstance(dest_storage, S3Storage)
        return TransferType.COPY, dest_storage, src_storage, dest_storage.bucket
    if plan.paths_type == "opens3":
        # Custom source -> S3: upload each entry from its Storage.open("rb").
        assert isinstance(dest_storage, S3Storage)
        return TransferType.UPLOAD, dest_storage, None, dest_storage.bucket
    # s3open: S3 -> custom destination, download into its open("wb").
    assert isinstance(src_storage, S3Storage)
    return TransferType.DOWNLOAD, src_storage, None, ""


class _SyncDeletes:
    """The deletion lane of one sync run (destination-only pairs).

    Wraps the dispatch shapes behind one ``submit``: an S3 destination
    batches through ``S3Deleter`` (the ``rm`` machinery - a wire-level
    deviation from aws-cli's per-key DeleteObject with the same end state),
    a local destination removes synchronously on the calling thread (aws-cli's
    ``LocalDeleteRequestSubmitter``), and a custom (``s3open``) destination
    removes synchronously through its own ``Storage.delete``. Dry runs emit
    DRYRUN records and touch nothing. The deleter is created lazily on the first
    S3 submit - a run with nothing to delete spawns no worker - and lands in the
    sync's ``ExitStack`` so an exception abandons the unflushed batch (aws-cli
    cancel behavior) while a clean exit flushes it.
    """

    def __init__(
        self,
        dest_storage: Storage,
        *,
        request_payer: str | None,
        dryrun: bool,
        on_result: ResultCallback | None,
        cancel_token: CancelToken | None,
        capture_response: bool = False,
    ) -> None:
        self._dest = dest_storage
        self._request_payer = request_payer
        self._dryrun = dryrun
        self._on_result = on_result
        self._cancel_token = cancel_token
        self._capture_response = capture_response
        self._stack: ExitStack | None = None
        self._deleter: S3Deleter | None = None
        # Synchronous (non-batched) deletes - a local or custom dest's own
        # Storage.delete(info) - share one set of counters.
        self._local_succeeded = 0
        self._local_failed = 0
        self._local_first_error: Boto3S3Error | None = None

    def open(self, stack: ExitStack) -> None:
        self._stack = stack

    @property
    def succeeded(self) -> int:
        deleter = 0 if self._deleter is None else self._deleter.succeeded
        return deleter + self._local_succeeded

    @property
    def failed(self) -> int:
        deleter = 0 if self._deleter is None else self._deleter.failed
        return deleter + self._local_failed

    @property
    def first_error(self) -> Boto3S3Error | None:
        if self._deleter is not None and self._deleter.first_error is not None:
            return self._deleter.first_error
        return self._local_first_error

    def submit(self, pair: DestOnlyPair) -> None:
        """Dispatch one destination orphan through the S3 or backend delete route."""
        info = pair.dest
        dest = self._dest
        if isinstance(dest, S3Storage):
            if self._dryrun:
                self._emit(info=info, outcome=OpOutcome.DRYRUN, src=self._display(info.key))
                return
            if self._deleter is None:
                assert self._stack is not None
                self._deleter = self._stack.enter_context(
                    S3Deleter(
                        dest,
                        request_payer=self._request_payer,
                        on_result=self._on_result,
                        cancel_token=self._cancel_token,
                        operation="sync",
                        capture_response=self._capture_response,
                    )
                )
            self._deleter.submit(info)
            return
        # Local or custom (s3open) destination: remove the orphan synchronously
        # through the backend's own Storage.delete(info), sharing the local
        # counters. A backend error is mapped into the library taxonomy (the same
        # translate the byte-moving delete path uses), so every result carries a
        # Boto3S3Error and the message aws prints survives; the catch is broad.
        # Only the display form differs: a local orphan reads as a native path,
        # a custom one through the backend's own rendering.
        display = (
            to_native_path(info.key)
            if isinstance(dest, LocalStorage)
            else producers.open_side_display(dest, pair.compare_key)
        )
        if self._dryrun:
            self._emit(info=info, outcome=OpOutcome.DRYRUN, src=display)
            return
        try:
            response = dest.delete(info)
        except Exception as exc:
            self._emit(
                info=info, outcome=OpOutcome.FAILED, src=display, error=self._fail(exc, info)
            )
            return
        self._local_succeeded += 1
        self._emit(
            info=info,
            outcome=OpOutcome.SUCCEEDED,
            src=display,
            extra_info=self._delete_slot(response),
        )

    def _fail(self, exc: Exception, info: FileInfo) -> Boto3S3Error:
        """Map a synchronous-delete error into the taxonomy and count it."""
        error = translate_boto_error(exc, operation="sync", key=info.key)
        if error is not exc:
            # Same cause link as the transfer engine's _record_failure
            # (exceptions.md section 2.1); a pass-through keeps its own cause.
            error.__cause__ = exc
        self._local_failed += 1
        if self._local_first_error is None:
            self._local_first_error = error
        return error

    def _display(self, key: str) -> str:
        assert isinstance(self._dest, S3Storage)
        return f"s3://{self._dest.bucket}/{key}"

    def _delete_slot(self, response: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        """A ``capture_response`` ``{"delete": ...}`` from a backend delete response.

        A custom (open-route) destination's ``Storage.delete`` may return its
        delete response (a ``Mapping``, surfaced under ``extra_info["delete"]``);
        a ``LocalStorage`` unlink returns ``None``, so a local orphan removal has
        no slot.
        """
        if not self._capture_response or response is None:
            return None
        return {"delete": strip_response_metadata(response)}

    def _emit(
        self,
        *,
        info: FileInfo,
        outcome: OpOutcome,
        src: str,
        error: Boto3S3Error | None = None,
        extra_info: Mapping[str, Any] | None = None,
    ) -> None:
        if self._on_result is not None:
            self._on_result(
                OpResult(
                    transfer_type=TransferType.DELETE,
                    compare_key=info.compare_key or info.key,
                    outcome=outcome,
                    error=error,
                    src=src,
                    src_info=info,
                    src_storage=self._dest,
                    extra_info=extra_info,
                )
            )


def _bucket_tags(
    tags: Sequence[tuple[str, str]] | Mapping[str, str] | None,
) -> list[dict[str, str]]:
    """Normalize ``S3.mb``'s ``tags`` to the ``CreateBucketConfiguration.Tags`` shape.

    A sequence of pairs passes through in order, duplicates included - the
    server enforces key uniqueness, and the CLI's repeated ``--tags`` relies
    on that rejection for parity. A mapping is the convenience form.
    """
    if tags is None:
        return []
    pairs = tags.items() if isinstance(tags, Mapping) else tags
    return [{"Key": key, "Value": value} for key, value in pairs]


class S3:
    """Entry point for ``aws s3``-style operations (``cp`` / ``ls`` / ``mv`` / ...).

    ``S3`` is a stateless orchestrator: it carries optional defaults
    (``session`` / ``endpoint_url`` / ``config`` for building clients,
    ``transfer_config`` for ``cp`` / ``mv`` / ``sync``) but holds no connection,
    so it needs no cleanup. The transfer ops (``cp`` / ``mv`` / ``sync``) resolve
    each ``Location`` argument by `resolve` - an ``"s3://..."`` string becomes an
    ``S3Storage`` carrying a client from `client`, anything else a
    ``LocalStorage``. The S3-only ops (``ls`` / ``rm`` / ``mb`` / ``rb`` /
    ``presign`` / ``website``) resolve their single target more leniently and do
    not pass through `resolve`: the ``s3://`` scheme is optional and a non-s3
    string is never a local fallback. Either way a bare string inherits these
    defaults (its ``S3Storage`` carries `client`), while a ``Storage`` instance
    passed in is used verbatim (its own client).

    `client` and `resolve` are the customization seams - subclass and
    override ``client`` to change credentials / session / endpoint (or return a
    test double), or ``resolve`` to interpret new URL schemes (e.g. ``http://``).
    Overriding ``resolve`` reaches the transfer ops only; the S3-only ops bypass
    it (they always read their target as S3), so their endpoint / credentials are
    customized through ``client`` alone. A second account for S3-to-S3, or a
    per-location client, is expressed by an explicit ``S3Storage(uri,
    client=...)``. Debug-log secret masking is configured via
    `boto3_s3.set_stream_logger`. The instance's own state
    is thread-safe to share, but its clients need care: ``client()`` is not safe
    to call concurrently (boto3's session construction path), and boto3-s3 uses
    ``s3transfer``, whose per-transfer setup is not thread-safe on a shared
    client. To run operations in parallel across threads, build one client per
    thread sequentially, up front, then give each thread its own
    ``S3Storage(uri, client=...)`` - never share a client, never build clients
    concurrently.
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        endpoint_url: str | None = None,
        config: Config | None = None,
        transfer_config: TransferConfig | None = None,
    ) -> None:
        self._session = session
        self._endpoint_url = endpoint_url
        self._config = config
        self._transfer_config = transfer_config
        # Memoized AwsConfig (aws_config()): resolve+parse the config file once
        # per instance, since a sync filter may consult it per object. A benign,
        # idempotent cache - concurrent first calls recompute the same reader.
        self._aws_config: AwsConfig | None = None

    @property
    def session(self) -> Session | None:
        """The session that supplies this instance's default clients and AWS config."""
        return self._session

    def client(self) -> S3Client:
        """Build a boto3 S3 client from this instance's defaults (the factory seam).

        A fresh client each call, owned by the caller - ``S3`` keeps no
        connection, hence no ``close()``. Built from ``session`` (or boto3's
        default session) with ``endpoint_url`` / ``config`` applied. Override to
        change credentials, reuse a cached client, or return a test double; reuse
        one explicitly via ``S3Storage(uri, client=s3.client())``. A failed build
        raises the translated ``Boto3S3Error`` - ``ConfigurationError`` for
        unresolvable credentials / region, its ``InvalidConfigError``
        refinement for a set-but-unusable ``AWS_PROFILE``, partial
        credentials, or a malformed ``endpoint_url`` - never the raw botocore
        error (docs/exceptions.md section 1).
        """
        # operation=None: no subcommand is in scope at build time.
        with s3_errors(operation=None):
            try:
                if self._session is not None:
                    return self._session.client(
                        "s3", endpoint_url=self._endpoint_url, config=self._config
                    )
                # Import locally so callers that only use SDK-independent modules
                # do not pay for boto3. `S3` construction itself has no SDK-free
                # contract (docs/imports.md).
                import boto3

                return boto3.client("s3", endpoint_url=self._endpoint_url, config=self._config)
            except ValueError as exc:
                # botocore rejects a malformed endpoint_url with a plain
                # ValueError (not a BotoCoreError), which would leak raw
                # through s3_errors. A set-but-unusable setting is
                # InvalidConfigError's definition (docs/exceptions.md).
                raise InvalidConfigError(str(exc)) from exc

    def resolve(self, loc: Location) -> Storage:
        """Resolve a ``Location`` to a ``Storage`` (the URL-interpretation seam).

        A ``Storage`` instance is returned unchanged; an ``"s3://..."`` string
        becomes an ``S3Storage`` carrying `client`; anything else is a
        ``LocalStorage`` (aws-cli's rule: only ``s3://`` is special). Override to
        add schemes such as ``http://``, deferring the rest to
        ``super().resolve(loc)``.
        """
        if isinstance(loc, Storage):
            return loc
        text = os.fspath(loc)
        if text.startswith("s3://"):
            return S3Storage(text, client=self.client())
        return LocalStorage(text)

    def aws_config(self) -> AwsConfig:
        """Read this instance's AWS config file (``~/.aws/config``) - an explicit opt-in.

        Returns an `AwsConfig` reader over the config
        file resolved for this ``S3``'s ``session`` (or a default
        ``AWS_PROFILE``-aware session when none was given - the same resolution
        `client` uses). Use it to surface profile settings the application
        did not set itself - e.g. the ``[s3]`` transfer tuning::

            chunksize = s3.aws_config().get_size("s3.multipart_chunksize", 8 * 1024**2)

        This is a building block offered **explicitly**: ``S3``'s own operations
        (``cp`` / ``sync`` / ...) never read the config file on their own, so they
        carry no hidden dependence on the ambient ``~/.aws/config``. The reader is
        memoized per instance (the file is parsed once). Matching ``aws s3``'s
        ``[s3]`` semantics on top of these values (the defaults table, validation,
        the engine decision) is the CLI distribution's job, not the library's.
        """
        # Load the optional config reader only when a caller asks for it.
        from boto3_s3.awsconfig import AwsConfig

        if self._aws_config is None:
            self._aws_config = AwsConfig.from_session(self._session)
        return self._aws_config

    # -- listing ----------------------------------------------------------

    def ls(
        self,
        target: Location = "s3://",
        *,
        on_entry: ListingCallback,
        recursive: bool = False,
        request_payer: str | None = None,
        bucket_name_prefix: str | None = None,
        bucket_region: str | None = None,
        cancel_token: CancelToken | None = None,
    ) -> None:
        """List objects and common prefixes under an S3 ``target``, or all buckets.

        ``ls`` is S3-only (aws-cli parity): ``target`` is an ``"s3://..."`` URI
        string (the ``s3://`` scheme is optional) or an ``S3Storage``. A target
        with no bucket - the default bare ``"s3://"`` - is the service root:
        ``ls`` dispatches to `S3Storage.list_buckets` (aws-cli's ``ls``
        splits the bucket listing from the object listing the same way), yielding
        one ``BUCKET``-kind entry per bucket (``mtime`` = creation date).
        ``bucket_name_prefix`` / ``bucket_region`` filter *that* bucket listing and
        are meaningless for an object listing; conversely ``recursive`` /
        ``request_payer`` are meaningless at the service root (both ignored, like
        aws-cli). The listing page size (object and bucket listings alike) is the
        ``S3Storage``'s own ``page_size`` config (its constructor) - pass a
        configured ``S3Storage`` as ``target`` to tune it. A non-S3 ``Location``
        raises ``ValidationError``. The target is validated eagerly. Entries are
        delivered in listing order to `on_entry` on the calling thread.

        `cancel_token` may be cancelled from `on_entry` or another thread.
        Cancellation stops entry delivery, drops prefetched pages, waits for a
        page request already in progress, reclaims the prefetch worker, and then
        raises `CancelledError`. Both cancellation modes therefore have the
        same effect for listing: synchronous S3 page requests cannot be aborted
        safely once started.
        """
        storage = self._resolve_s3_target(target, operation="ls")
        _raise_if_cancelled(cancel_token, "ls")
        if not storage.bucket:
            items = storage.list_buckets(name_prefix=bucket_name_prefix, region=bucket_region)
        else:
            items = storage.scan(
                replace(
                    storage.default_scan_options(),
                    recursive=recursive,
                    request_payer=request_payer,
                ),
                cancel_token=cancel_token,
            )
        try:
            for info in items:
                if cancel_token is not None and cancel_token.cancelled:
                    break
                on_entry(info)
                if cancel_token is not None and cancel_token.cancelled:
                    break
        finally:
            close = getattr(items, "close", None)
            if close is not None:
                close()
        _raise_if_cancelled(cancel_token, "ls")

    def _resolve_s3_target(self, target: Location, *, operation: str) -> S3Storage:
        """Resolve the single ``Location`` of an S3-only op to a validated ``S3Storage``.

        The shared seam of ``ls`` / ``rm`` / ``mb`` / ``rb`` / ``presign`` /
        ``website``. An ``S3Storage`` is used verbatim; a ``PathLike`` is expanded
        via ``os.fspath`` (honoring the ``Location`` os.PathLike[str] contract,
        like `resolve` does), and any other non-string - a non-S3 ``Storage``,
        which carries no ``__fspath__`` - falls through to the ``ValidationError``.
        Unlike `resolve`, these ops are lenient about the ``s3://`` scheme (a bare
        ``"bucket/key"`` or the ``"s3://"`` service root both work) and a non-s3
        string is never a local fallback; the built ``S3Storage`` carries this
        ``S3``'s `client`. Construction is permissive (non-raising), so the strict
        aws-cli checks (unsupported ARN forms, a key without a bucket) run via
        ``validate`` before the op uses the storage.
        """
        if isinstance(target, S3Storage):
            storage = target
        else:
            if isinstance(target, os.PathLike):
                target = os.fspath(target)
            if not isinstance(target, str):
                raise ValidationError(
                    f"{operation} accepts an 's3://...' URI string or an S3Storage",
                    operation=operation,
                )
            storage = S3Storage(target, client=self.client())
        storage.validate()
        return storage

    # -- byte transfer ----------------------------------------------------

    def cp(
        self,
        src: Location,
        dest: Location,
        *,
        recursive: bool = False,
        filter: FileFilter | None = None,
        dryrun: bool = False,
        expected_size: int | None = None,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        transfer_config: TransferConfig | None = None,
        capture_response: bool = False,
        **options: Unpack[TransferOptions],
    ) -> None:
        """Copy bytes between ``src`` and ``dest`` with ``aws s3 cp`` semantics.

        The route follows the resolved pair: local->S3 upload, S3->local
        download, S3->S3 copy, or the open route pairing a custom ``Storage``
        with S3 (local->local is rejected, like aws). Path
        shapes - what an existing-directory or trailing-slash destination
        means, which side's name wins, where ``--exclude`` / ``--include``
        patterns root - reproduce aws-cli's ``FileFormat`` rules
        (`boto3_s3.transferplan`); ``recursive`` is the aws-cli ``dir_op``.
        Bytes move through `boto3_s3.transfer.Transferrer`
        (s3transfer; multipart per ``transfer_config``, defaults matching
        aws). Per-item parity behaviors carried over: local walk warnings
        (unreadable / special / broken-symlink files are skipped with a
        warning), GLACIER / DEEP_ARCHIVE sources skipped with a warning on
        download/copy unless forced (``ignore_glacier_warnings`` skips
        silently; a recursive listing carries no ``Restore``, so restored
        objects still skip there - aws-cli-faithful), parent-directory
        escapes blocked on download, the >48.8 TiB upload pre-warning,
        downloads stamping the source ``LastModified`` (a stamp failure is a
        warning, not an error), copy metadata/tags propagation per
        `copy_props` (including the library-only `annotation_copy_mode` for
        multipart `copy_props=ALL` staging), and a single S3 source resolved
        by HeadObject - a
        404 raises ``NotFoundError`` with aws's rewritten ``Key "..." does
        not exist`` message. A keyless non-recursive S3 source matches
        nothing and transfers nothing (aws lists and discards; same outcome
        without the requests).

        ``filter`` keeps an item in the operation (rm's contract): a
        ``FileFilter`` predicate over the item's ``FileInfo``, whose
        ``compare_key`` is stamped relative to the source directory or S3
        ``Prefix`` - a `GlobFilter` matches that key (a relative pattern) or the
        full ``key`` (an absolute one) while a richer
        predicate can read size / mtime / storage_class. On the stream route (an
        ``IOStorage`` on one side) there is no listing to prune, so ``filter``
        does not apply - aws-cli applies no filter to a stream either.
        How the local source is walked - whether symlinks are followed
        (``follow_symlinks``) and whether the recursive walk guards against symlink
        cycles (``detect_symlink_loops``, a library extension ``aws s3`` lacks),
        and whether every native entry becomes a filter candidate
        (``enumerate_all_entries``) - is
        configured on the ``LocalStorage`` itself (its constructor knobs), not on
        ``cp``: pass a configured ``LocalStorage`` as ``src`` to change it; a bare
        path string uses the defaults (follow symlinks, no cycle guard, normal
        transfer enumeration).
        The S3 listing page size is likewise the ``S3Storage``'s own ``page_size``.
        ``dryrun`` enumerates (listing and HeadObject still
        run) and reports ``OpOutcome.DRYRUN`` without transferring; warnings
        still apply. ``expected_size`` is the multipart sizing hint for a
        streaming upload (stdin -> S3): it is forwarded to the stream path
        (`_cp_stream` sets it as the upload item ``size``) and is ignored
        on the non-stream routes, exactly like aws's ``--expected-size`` (which
        only matters for a stdin upload above ~50 GB).

        Results stream to ``on_result`` from worker threads (fast,
        non-raising callbacks); failures aggregate into ``BatchError`` with
        the first failure as ``__cause__``, warnings alone do not raise (the
        CLI derives exit code 2 from its warned count). Failures *before*
        any item work - an unreadable destination, the missing-source check
        - raise directly: a missing local source raises ``NotFoundError``
        with aws's wording (their pre-pipeline rc 255 shape - no
        ``ClientError`` cause, so the CLI maps it to the general rc).
        """
        _validate_transfer_options(options, operation="cp")
        if transfer_config is None:
            transfer_config = self._transfer_config
        src_storage = self.resolve(src)
        dest_storage = self.resolve(dest)
        if isinstance(src_storage, IOStorage) or isinstance(dest_storage, IOStorage):
            self._cp_stream(
                src_storage,
                dest_storage,
                recursive=recursive,
                dryrun=dryrun,
                expected_size=expected_size,
                on_progress=on_progress,
                on_result=on_result,
                cancel_token=cancel_token,
                transfer_config=transfer_config,
                capture_response=capture_response,
                options=options,
            )
            return
        self._run_transfer(
            src_storage,
            dest_storage,
            operation="cp",
            is_move=False,
            recursive=recursive,
            item_filter=filter,
            dryrun=dryrun,
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            transfer_config=transfer_config,
            capture_response=capture_response,
            options=options,
        )

    def _run_transfer(
        self,
        src_storage: Storage,
        dest_storage: Storage,
        *,
        operation: str,
        is_move: bool,
        recursive: bool,
        item_filter: FileFilter | None,
        dryrun: bool,
        on_progress: ProgressCallback | None,
        on_result: ResultCallback | None,
        cancel_token: CancelToken | None,
        transfer_config: TransferConfig | None,
        capture_response: bool = False,
        options: TransferOptions,
    ) -> None:
        """The shared cp/mv pipeline for parsed, non-stream locations.

        Route classification, the pre-batch checks, enumeration, the gates,
        and the submit loop - identical for both operations; ``mv`` differs
        only by what it validated beforehand and by ``is_move`` (the engine's
        delete-source + MOVE reporting).

        The built-in routes assert the concrete ``LocalStorage`` / ``S3Storage``
        pair, because the engine reaches into ``S3Storage``'s client/bucket and
        ``LocalStorage``'s path directly (s3transfer). A custom ``Storage`` (any
        other ``scheme``) pairs with S3 through the ``open`` route (``opens3`` /
        ``s3open``): its bytes move via ``Storage.open`` while the S3 side rides
        s3transfer. The custom side is capability-checked up front - it must
        implement the contract methods the route uses - so a missing method
        surfaces as a clear ``ValidationError`` instead of failing deep in the
        engine.
        """
        src_storage.validate()
        dest_storage.validate()
        plan = transferplan.plan_transfer(
            src_storage, dest_storage, recursive=recursive, operation=operation
        )

        transfer_type, client_provider, source_provider, dest_bucket = _classify_transfer_route(
            plan, operation=operation
        )
        # aws-cli's _validate_path_args only creates the dest dir when it does
        # not already exist; check the raw user path (plan.dest_root carries a
        # trailing os.sep, so exists() is False for an existing *file*). An
        # existing-file dest then skips makedirs and fails per item like aws
        # (rc 1) instead of crashing up front; an empty listing transfers
        # nothing and exits 0. (sync's guard differs: unconditional, bare
        # makedirs.)
        if (
            recursive
            and isinstance(dest_storage, LocalStorage)
            and not os.path.exists(dest_storage.path)
        ):
            try:
                os.makedirs(plan.dest_root, exist_ok=True)
            except OSError as exc:
                raise translate_os_error(exc, operation=operation, key=None) from exc
        client = client_provider.get_client()
        source_client = source_provider.get_client() if source_provider is not None else None
        # The S3 listing source (s3local / s3s3 / s3open); None on the
        # local/custom-source upload routes.
        src_s3 = src_storage if isinstance(src_storage, S3Storage) else None

        producers.require_open_capabilities(
            plan, recursive=recursive, is_move=is_move, operation=operation
        )
        transferrer = Transferrer(
            transfer_type,
            client,
            source_client=source_client,
            # The run's two resolved sides, carried onto each result; src_storage
            # is also mv's upload-source delete handle (consulted only on the
            # upload route - a download/copy S3 source is removed via DeleteObject).
            src_storage=src_storage,
            dest_storage=dest_storage,
            transfer_config=transfer_config,
            options=options,
            operation=operation,
            is_move=is_move,
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            capture_response=capture_response,
            crt_endpoint=self._endpoint_url,
            session=self._session,
        )
        # After the Transferrer: the gate's destination membership scan warns
        # into the shared rollup (aws's reverse enumeration shares the result
        # queue) and applies the run's filter, like any side's walk.
        case_gate = producers.cp_case_gate(
            plan,
            recursive=recursive,
            options=options,
            transferrer=transferrer,
            item_filter=item_filter,
            operation=operation,
        )
        with transferrer:
            if not dryrun:
                # Build the manager (registers client-event handlers) before the
                # producers start their scan prefetch worker on this same client
                # (Transferrer.prepare).
                transferrer.prepare()
            if plan.paths_type == "opens3":
                items = producers.open_upload_items(
                    plan,
                    dest_bucket=dest_bucket,
                    transferrer=transferrer,
                    item_filter=item_filter,
                    operation=operation,
                    dryrun=dryrun,
                )
            elif plan.paths_type == "s3open":
                assert src_s3 is not None
                items = producers.open_download_items(
                    plan,
                    src_s3,
                    transferrer=transferrer,
                    item_filter=item_filter,
                    options=options,
                    operation=operation,
                    dryrun=dryrun,
                )
            elif src_s3 is None:
                items = producers.upload_items(
                    plan,
                    dest_bucket=dest_bucket,
                    transferrer=transferrer,
                    item_filter=item_filter,
                )
            else:
                items = producers.s3_source_items(
                    plan,
                    src_s3,
                    transfer_type=transfer_type,
                    dest_bucket=dest_bucket,
                    transferrer=transferrer,
                    item_filter=item_filter,
                    options=options,
                    case_gate=case_gate,
                    operation=operation,
                )
            # Check cancellation *before* pulling the next item, not after: the
            # open routes (_open_upload_item / _open_download_item) open the
            # backend fileobj as the generator yields, so materializing an item we
            # then discard on cancellation would leak that open fileobj. Pulling
            # only once the run is still live means a cancelled run never opens an
            # item it will not submit.
            item_iter = iter(items)
            while True:
                _raise_if_cancelled(cancel_token, operation)
                try:
                    item = next(item_iter)
                except StopIteration:
                    break
                if dryrun:
                    transferrer.dryrun(item)
                else:
                    transferrer.submit(item)
        _raise_if_cancelled(cancel_token, operation)
        if transferrer.failed:
            raise BatchError(
                f"{transferrer.failed} of "
                f"{transferrer.failed + transferrer.succeeded} transfers failed",
                succeeded=transferrer.succeeded,
                failed=transferrer.failed,
                warned=transferrer.warned,
                skipped=transferrer.skipped,
                operation=operation,
            ) from transferrer.first_error

    def _cp_stream(
        self,
        src_storage: Storage,
        dest_storage: Storage,
        *,
        recursive: bool,
        dryrun: bool,
        expected_size: int | None,
        on_progress: ProgressCallback | None,
        on_result: ResultCallback | None,
        cancel_token: CancelToken | None,
        transfer_config: TransferConfig | None,
        capture_response: bool = False,
        options: TransferOptions,
    ) -> None:
        """One streaming transfer: an ``IOStorage`` on exactly one side.

        The other side must be S3 (a non-S3 peer is the "stream on one side"
        error); its key is taken verbatim (the CLI owns aws's ``-``-basename
        naming quirk). The stream's fileobj comes from ``IOStorage.open`` and is
        handed straight to ``s3transfer``. Uploads honor ``expected_size`` as the
        multipart sizing hint (without it the engine buffers up to the threshold
        and decides); downloads provide neither size nor etag, so s3transfer
        probes the object with a HeadObject - the aws stream wire shape. Streams
        are single items: the glacier / parent-ref gates do not apply; displays
        render as ``-``. A ``cancel_token`` already cancelled raises
        ``CancelledError`` before the fileobj is opened or submitted, matching the
        non-stream route's pre-submit poll (a single item, so one check suffices).
        """
        src_is_stream = isinstance(src_storage, IOStorage)
        dest_is_stream = isinstance(dest_storage, IOStorage)
        if recursive:
            raise ValidationError(
                "Streaming currently is only compatible with non-recursive cp commands",
                operation="cp",
            )
        if src_is_stream and dest_is_stream:
            raise ValidationError(
                "cp supports a stream on one side only (the other must be s3://)",
                operation="cp",
            )
        if dest_is_stream and options.get("no_overwrite"):
            # A streaming download has no existing destination to guard, so
            # no_overwrite is meaningless here (aws-cli rejects it too); fail
            # loud rather than silently ignore. Uploads keep IfNoneMatch.
            raise ValidationError(
                "no_overwrite is not supported for streaming downloads",
                operation="cp",
            )
        # Poll once before opening the fileobj / submitting, like the
        # non-stream loop does before pulling each item - a pre-cancelled
        # run transfers nothing and leaves a side-effecting stream untouched.
        _raise_if_cancelled(cancel_token, "cp")
        if src_is_stream:
            storage = self._stream_s3_peer(dest_storage)
            transfer_type = TransferType.UPLOAD
            item = TransferItem(
                compare_key=storage.key,
                size=expected_size,
                # dryrun reports the item without submitting it, so skip the open -
                # matching the open routes (_open_upload_item / _open_download_item)
                # and keeping a side-effecting custom IOStorage untouched on a dry run.
                src_fileobj=None if dryrun else src_storage.open(storage.key, "rb"),
                dest_bucket=storage.bucket,
                dest_key=storage.key,
                src_display="-",
                dest_display=f"s3://{storage.bucket}/{storage.key}",
            )
        else:
            storage = self._stream_s3_peer(src_storage)
            transfer_type = TransferType.DOWNLOAD
            item = TransferItem(
                compare_key=storage.key,
                src_bucket=storage.bucket,
                src_key=storage.key,
                # See the upload branch: a dry run never opens the stream.
                dest_fileobj=None if dryrun else dest_storage.open(storage.key, "wb"),
                src_display=f"s3://{storage.bucket}/{storage.key}",
                dest_display="-",
            )
        transferrer = Transferrer(
            transfer_type,
            storage.get_client(),
            src_storage=src_storage,
            dest_storage=dest_storage,
            transfer_config=transfer_config,
            options=options,
            operation="cp",
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            capture_response=capture_response,
            crt_endpoint=self._endpoint_url,
            session=self._session,
        )
        with transferrer:
            if dryrun:
                transferrer.dryrun(item)
            else:
                transferrer.submit(item)
        _raise_if_cancelled(cancel_token, "cp")
        if transferrer.failed:
            raise BatchError(
                "1 of 1 transfers failed",
                succeeded=transferrer.succeeded,
                failed=transferrer.failed,
                warned=transferrer.warned,
                skipped=transferrer.skipped,
                operation="cp",
            ) from transferrer.first_error

    @staticmethod
    def _stream_s3_peer(peer: Storage) -> S3Storage:
        """The S3 side facing a stream in ``_cp_stream``, validated.

        ``cp`` already resolved both sides, so the stream's peer is a ``Storage``
        instance: an ``S3Storage`` is the only well-formed one (a local or other
        non-S3 peer is the "stream on one side" error, not the generic
        `_resolve_s3_target` message, which reads as if ``cp`` never takes a local
        path). Construction was permissive, so run the strict aws-cli checks
        before use.
        """
        if not isinstance(peer, S3Storage):
            raise ValidationError(
                "cp supports a stream on one side only (the other must be s3://)",
                operation="cp",
            )
        peer.validate()
        return peer

    def mv(
        self,
        src: Location,
        dest: Location,
        *,
        recursive: bool = False,
        filter: FileFilter | None = None,
        dryrun: bool = False,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        transfer_config: TransferConfig | None = None,
        capture_response: bool = False,
        **options: Unpack[TransferOptions],
    ) -> None:
        """Move bytes with ``aws s3 mv`` semantics: ``cp``, then delete the source.

        Everything `cp` documents - routes, path shapes, filters,
        gates, warnings, the ``BatchError`` aggregation - applies unchanged;
        the differences are mv's. Every result reports ``TransferType.MOVE``, and
        each item's source is deleted right after its transfer succeeds
        (``Storage.delete`` for uploads - a local file or a custom backend
        object; one DeleteObject per object otherwise, ``request_payer``
        forwarded). A failed, skipped, or dry-run item
        keeps its source; a deletion failure turns that item into the
        failure aws prints as ``move failed`` (the bytes already arrived).
        Filters prune both the transfer and the deletion. A stream
        (``IOStorage``) can be the destination of a single-object move -
        the bytes land on the stream, then the S3 source is deleted - but
        not a recursive one (a stream is a single endpoint) and never the
        source (a move deletes its source, which a stream cannot be); both
        raise ``ValidationError``. The CLI rejects ``-`` for mv on either
        side outright (aws parity; it owns that exact error text). Emptied
        local source directories are left behind like aws.

        Moving an object onto itself (same URI, or a ``/``-terminated
        destination plus the source's basename - checked for ``--recursive``
        too, aws-cli's faithful false positive) raises
        ``ValidationError`` with aws's message before anything runs. That
        guard is textual only: paths through access point ARNs or aliases
        can still land on the same underlying bucket. Resolve them first
        with `boto3_s3.pathresolver.S3PathResolver` (the
        ``--validate-same-s3-paths`` machinery; bring your own s3control /
        sts clients) when that risk applies.
        """
        _validate_transfer_options(options, operation="mv")
        if transfer_config is None:
            transfer_config = self._transfer_config
        src_storage = self.resolve(src)
        dest_storage = self.resolve(dest)
        if isinstance(src_storage, IOStorage):
            # A move deletes its source, which a stream cannot be (no delete).
            # The destination side is different: a single-object move onto a
            # stream is well-formed (bytes land on the stream, then the S3
            # source is deleted) and rides the s3open route below.
            raise ValidationError(
                "mv does not support a stream source: a move deletes its source, "
                "which a stream cannot be",
                operation="mv",
            )
        if isinstance(dest_storage, IOStorage) and recursive:
            # A stream is a single endpoint: a recursive move would concatenate
            # every object into it while deleting the sources.
            raise ValidationError(
                "mv supports a stream destination only for a single object (non-recursive)",
                operation="mv",
            )
        if isinstance(src_storage, S3Storage) and isinstance(dest_storage, S3Storage):
            if src_storage.same_path_as(dest_storage):
                # aws words the error with the keyless-normalized URIs
                # (`mv s3://b/k s3://b` reports `s3://b/`).
                raise ValidationError(
                    f"Cannot mv a file onto itself: {src_storage.normalized_uri()} "
                    f"- {dest_storage.normalized_uri()}",
                    operation="mv",
                )
        self._run_transfer(
            src_storage,
            dest_storage,
            operation="mv",
            is_move=True,
            recursive=recursive,
            item_filter=filter,
            dryrun=dryrun,
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            transfer_config=transfer_config,
            capture_response=capture_response,
            options=options,
        )

    def sync(
        self,
        src: Location,
        dest: Location,
        *,
        filter: FileFilter | None = None,
        create_filter: bool | FileFilter | ParallelFilter[FileInfo] = True,
        update_filter: bool | PairFilter | ParallelFilter[SyncPair] | None = None,
        delete_filter: bool | FileFilter | ParallelFilter[FileInfo] = False,
        dryrun: bool = False,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        transfer_config: TransferConfig | None = None,
        capture_response: bool = False,
        **options: Unpack[TransferOptions],
    ) -> None:
        """Recursively synchronize ``src`` into ``dest`` (``aws s3 sync``).

        Always recursive (no single-file sync, aws parity) over the three
        built-in transfer routes plus the open route (a custom ``Storage``
        paired with S3); a local->local pair raises ``ValidationError``. The
        pipeline has two layers:

        - **Visibility** - both listings are pruned independently *before*
          the sides meet by the single ``filter``, a
          `FileFilter` applied to each side's `compare_key` (directory-relative
          for local entries, ``Prefix``-relative for S3 entries) - so it prunes
          the source and destination **symmetrically** (matching aws, which evaluates
          ``--exclude`` / ``--include`` against both sides). Folder markers
          (zero-byte ``/``-terminated objects) never surface on either side -
          sync neither transfers nor deletes them. Because pruning is
          symmetric, an excluded key is invisible on *both* sides and is thus
          neither transferred nor deleted (aws's "files excluded by filters
          are excluded from deletion"); visibility is never one-sided, so it
          cannot manufacture a phantom new/delete pair. Per-lane narrowing
          belongs in the pair layer below (``create_filter`` / ``update_filter``
          / ``delete_filter``).
        - **Pair decisions** - the surviving streams are merge-joined by
          compare key (`Comparator`) and each resulting pair lands in exactly
          one of three lanes by its type (`MergedPair` - the shape says which
          sides hold the key), each a filter deciding whether to act:
          **create** a new entry, **overwrite** one on both sides, or **delete**
          an orphan. ``create_filter`` and ``delete_filter`` are the two
          membership knobs (create / delete) - duals of each other;
          ``update_filter`` is the overwrite judgment for the intersection. The
          aws equivalent is the default of all three:

          - ``create_filter`` - the **new** (`SrcOnlyPair`) entries: ``True``
            (default) copies every one, ``False`` none, a
            `FileFilter` only those it keeps (matched
            against the source ``FileInfo`` / compare key, the same shape as
            ``rm``'s ``filter``). aws hard-codes "always create"
            (``file_not_at_dest``), so this knob has no aws counterpart.
          - ``update_filter`` - the **update** (`SyncPair`) pairs: ``None``
            (default) is the aws-cli size + last-modified judgment, equivalently
            ``AwsCliComparison()`` - tune it with
            ``AwsCliComparison(size_only=...)`` / ``(exact_timestamps=...)``;
            ``True`` re-copies every one, ``False`` none (additive-only:
            existing destinations are left as-is); any
            `PairFilter` is a custom strategy - the
            content building blocks ``EtagComparison`` / ``ChecksumComparison``
            (submodule imports) are drop-in replacements that compare by
            content. A `SyncPair` always carries both sides, so a custom
            strategy reads ``pair.src`` / ``pair.dest`` directly.
          - ``delete_filter`` - the **orphan** (`DestOnlyPair`) entries:
            ``False`` (default) deletes nothing, ``True`` deletes every one
            (aws ``--delete``), a `FileFilter` only those
            it keeps (same shape as ``rm``'s ``filter``).

          Any lane's filter may be wrapped in
          `ParallelFilter` to run its per-entry
          decision on a caller-supplied thread pool (for a content
          ``update_filter=`` strategy, or a ``create`` / ``delete`` filter that
          reads bytes / tags / attributes) - identical results, only faster (the
          wrapped filter must be thread-safe). Parallelizing ``create_filter``
          makes the ``--case-conflict`` "first key wins" order non-deterministic.

          ``no_overwrite`` is an orthogonal write-guard on the update lane: an
          existing destination is never overwritten (new entries still copy),
          and sync keeps it decision-only - no ``IfNoneMatch`` on the wire.

        Transfers run on the engine with cp's gates (glacier, the
        parent-directory guard, the ``--case-conflict`` gate for downloads -
        applied only to `SrcOnlyPair`s, the aws-cli not-at-dest slot).
        Deletions are dispatched as they stream: batched ``DeleteObjects`` for
        an S3 destination, with XML-incompatible keys falling back to
        ``DeleteObject`` (the ``rm`` machinery; ``request_payer`` forwarded),
        a synchronous ``Storage.delete`` for a local one
        (``LocalStorage.delete``, an ``os.remove``) or a custom (open-route)
        one (through the backend's own ``delete``). ``dryrun``
        reports every would-be transfer and deletion without any API call.
        Local listing warnings (unreadable / vanished / special files,
        invalid timestamps) surface from **both** sides as WARNED records,
        exactly like aws walking both trees.

        A missing local source directory raises (aws's ``The user-provided
        path ... does not exist.``); a local destination directory is
        created up front even when nothing transfers. Item failures -
        transfer and delete alike - aggregate into one ``BatchError`` with
        rollup counts (first failure as ``__cause__``). `cancel_token` raises
        `CancelledError` after shutdown: graceful mode stops new pair actions
        and drains accepted transfers/deletes; immediate mode additionally
        requests best-effort future cancellation.
        """
        _validate_transfer_options(options, operation="sync")
        if transfer_config is None:
            transfer_config = self._transfer_config
        src_storage = self.resolve(src)
        dest_storage = self.resolve(dest)
        src_storage.validate()
        dest_storage.validate()
        plan = transferplan.plan_transfer(
            src_storage, dest_storage, recursive=True, operation="sync"
        )

        transfer_type, client_provider, source_provider, dest_bucket = _classify_transfer_route(
            plan, operation="sync"
        )
        # aws-cli creates the destination directory during validation - before
        # any listing, so even an empty sync leaves it behind. The bare
        # exists() test (not exist_ok=True) is deliberate: a destination that
        # exists as a *file* passes here and fails per item instead
        # ([Errno 20], rc 1). (cp's guard differs: recursive-gated, exist_ok.)
        if isinstance(dest_storage, LocalStorage) and not os.path.exists(dest_storage.path):
            try:
                os.makedirs(dest_storage.path)
            except OSError as exc:
                raise translate_os_error(exc, operation="sync", key=None) from exc
        client = client_provider.get_client()
        source_client = source_provider.get_client() if source_provider is not None else None
        # A custom side must support sorted enumeration (the merge-join) plus the
        # I/O the route uses; reject up front before any listing (gate below).
        producers.require_open_sync_capabilities(plan, delete=bool(delete_filter), operation="sync")

        # no_overwrite is an orthogonal write-guard (an option, so callers can
        # write ``sync(no_overwrite=True)``): strip it from the engine options
        # and apply it in the loop, keeping sync decision-only (no IfNoneMatch).
        no_overwrite = options.pop("no_overwrite", False)
        # Each lane resolves to (decide, executor): the predicate over the
        # lane's pair shape selecting whether to act, and the caller's pool when
        # the filter was a ParallelFilter (else None = decide inline on the
        # calling thread).
        # create_filter: True copies every new (source-only) entry, False none, a
        # FileFilter only those it keeps (matched like rm).
        create_decide, create_pool = _resolve_side_lane(create_filter, _create_via_filter)
        # update_filter picks exactly one copy strategy for the update
        # (both-sides) pairs; update_filter=None is the aws-cli size +
        # last-modified default, equivalently AwsCliComparison().
        update_pool: Executor | None = None
        update_decide: PairFilter
        if isinstance(update_filter, ParallelFilter):
            pooled = cast("ParallelFilter[SyncPair]", update_filter)  # isinstance loses type arg
            update_decide = pooled.decide
            update_pool = pooled.executor
        elif update_filter is None:
            update_decide = AwsCliComparison()
        elif update_filter is True:
            update_decide = _always
        elif update_filter is False:
            update_decide = _never
        else:
            update_decide = update_filter
        # delete_filter: False = off, True deletes every orphan (destination-only),
        # a FileFilter only those it keeps (matched like rm); the producer-stamped
        # compare_key lets it read the entry directly.
        delete_decide, delete_pool = (
            (None, None)
            if delete_filter is False
            else _resolve_side_lane(delete_filter, _delete_via_filter)
        )
        case_gate = producers.sync_case_gate(transfer_type, dest_storage, options=options)

        transferrer = Transferrer(
            transfer_type,
            client,
            source_client=source_client,
            src_storage=src_storage,
            dest_storage=dest_storage,
            transfer_config=transfer_config,
            options=options,
            operation="sync",
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            capture_response=capture_response,
            crt_endpoint=self._endpoint_url,
            session=self._session,
        )
        deletes = _SyncDeletes(
            dest_storage,
            request_payer=options.get("request_payer"),
            dryrun=dryrun,
            on_result=on_result,
            cancel_token=cancel_token,
            capture_response=capture_response,
        )
        with ExitStack() as stack:
            stack.enter_context(transferrer)
            if not dryrun:
                # Build the manager before either side's scan prefetch worker
                # starts on the manager's client (Transferrer.prepare).
                transferrer.prepare()
            deletes.open(stack)
            src_entries = producers.sync_entries(
                src_storage,
                root=plan.src_root,
                item_filter=filter,
                transferrer=transferrer,
                options=options,
            )
            dest_entries = producers.sync_entries(
                dest_storage,
                root=plan.dest_root,
                item_filter=filter,
                transferrer=transferrer,
                options=options,
            )
            src_bucket = src_storage.bucket if isinstance(src_storage, S3Storage) else ""

            def submit_copy(pair: SrcOnlyPair | SyncPair) -> None:
                item = producers.sync_transfer_item(
                    plan,
                    pair,
                    transfer_type=transfer_type,
                    src_bucket=src_bucket,
                    dest_bucket=dest_bucket,
                    transferrer=transferrer,
                    options=options,
                    case_gate=case_gate,
                    dryrun=dryrun,
                )
                if item is None:
                    return
                if dryrun:
                    transferrer.dryrun(item)
                else:
                    transferrer.submit(item)

            def submit_delete(pair: DestOnlyPair) -> None:
                deletes.submit(pair)

            # no_overwrite rules out every update up front (an existing
            # destination is never overwritten), so the update lane is dropped.
            _run_sync_pairs(
                Comparator(transfer_type).compare(src_entries, dest_entries),
                create_lane=_Lane(create_decide, submit_copy, create_pool),
                update_lane=(
                    None if no_overwrite else _Lane(update_decide, submit_copy, update_pool)
                ),
                delete_lane=(
                    None
                    if delete_decide is None
                    else _Lane(delete_decide, submit_delete, delete_pool)
                ),
                cancel_token=cancel_token,
            )

        _raise_if_cancelled(cancel_token, "sync")
        failed = transferrer.failed + deletes.failed
        if failed:
            succeeded = transferrer.succeeded + deletes.succeeded
            raise BatchError(
                f"{failed} of {failed + succeeded} operations failed",
                succeeded=succeeded,
                failed=failed,
                warned=transferrer.warned,
                skipped=transferrer.skipped,
                operation="sync",
            ) from (transferrer.first_error or deletes.first_error)

    def rm(
        self,
        target: Location,
        *,
        recursive: bool = False,
        filter: FileFilter | None = None,
        dryrun: bool = False,
        request_payer: str | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        capture_response: bool = False,
    ) -> None:
        """Delete objects under ``target`` with ``aws s3 rm`` semantics.

        Three target shapes (aws-cli parity, ``FileGenerator.list_objects``):

        - **key, non-recursive**: one *blind* ``DeleteObject`` of exactly that
          key - no listing, no HeadObject, and deleting a nonexistent key
          succeeds. A trailing-``/`` key deletes that "folder marker" object.
        - **recursive**: every object under the ``/``-normalized prefix
          (``"data"`` lists under ``"data/"``, so ``data-sibling.txt`` is not
          touched), folder markers included, deleted in batched
          ``DeleteObjects`` calls via `S3Deleter`; XML-incompatible keys fall
          back to aws-cli's per-key ``DeleteObject`` route (docs/deleter.md).
        - **bucket root, non-recursive**: lists the whole bucket but deletes
          only zero-byte ``/``-terminated "folder marker" objects (any depth)
          - aws-cli's manual-folder sweep, not a full wipe.

        ``filter`` keeps an item in the deletion when it is *included*: a
        ``Callable[[FileInfo], bool]`` over the entry's ``FileInfo``. Its
        ``compare_key`` is stamped to the key relative to the prefix returned by
        `rm_filter_root` before the call, so a `GlobFilter` matches
        that key (the CLI maps ``--exclude`` / ``--include`` here) while a
        richer predicate can read ``size`` / ``mtime`` / ``storage_class`` (on
        the blind single-key path ``size`` / ``mtime`` / ``storage_class`` are
        all ``None`` - only ``key``, ``compare_key`` and ``storage`` are set).
        On the enumerating paths it is evaluated page by page on
        the scan prefetch worker (via ``ScanOptions.filter``), overlapped with
        the listing I/O - keep the filter thread-safe and fast, like
        ``on_result``. ``dryrun`` enumerates (the recursive listing still
        runs) and reports candidates as ``OpOutcome.DRYRUN`` without any
        delete calls.

        Per-item completion is streamed to ``on_result`` (from the deleter's
        worker thread on the batched path - keep the callback fast and
        non-raising). Item failures are aggregated: ``BatchError`` with rollup
        counts (first failure as ``__cause__``) once at the end, exit-code
        model docs/exceptions.md section 4. Failures *before* any item work - e.g.
        the listing rejecting the bucket - raise their category error
        directly. `cancel_token` may be cancelled from `on_result`: graceful
        mode discards the unsent buffer and drains the in-flight delete batch;
        immediate mode also cancels an unstarted batch future. Both raise
        `CancelledError` after worker cleanup.
        """
        storage = self._resolve_s3_target(target, operation="rm")
        _raise_if_cancelled(cancel_token, "rm")
        if not storage.bucket:
            # rm has no bucket-listing mode (scan is object listing only), so a
            # bucketless service root cannot resolve to anything to delete. aws
            # sends Bucket="" to the API and fails botocore's client-side
            # validation (rc 1); reject up front the same way - a deterministic
            # library-level check that does not depend on the client validating.
            raise ValidationError('Invalid bucket name "": rm requires a bucket', operation="rm")
        root = rm_filter_root(storage.key, recursive=recursive)

        if not recursive and storage.key:
            self._rm_single(
                storage,
                filter,
                root=root,
                dryrun=dryrun,
                request_payer=request_payer,
                capture_response=capture_response,
                on_result=on_result,
            )
            _raise_if_cancelled(cancel_token, "rm")
            return

        # Enumerating paths: full recursive delete, or the keyless
        # non-recursive folder-marker sweep. Both list without Delimiter.
        # ScanOptions.prefix re-anchors the listing at the normalized prefix on
        # the passed storage itself (client shared), so a custom S3Storage
        # subclass and its scan_pages override survive; page_size / fetch_owner
        # come from the storage's own config via default_scan_options.
        options = replace(
            storage.default_scan_options(),
            recursive=True,
            request_payer=request_payer,
            prefix=root,
            filter=self._rm_scan_filter(filter, sweep=not recursive),
        )

        if dryrun:
            for info in storage.scan(options):
                _raise_if_cancelled(cancel_token, "rm")
                _emit_result(on_result, info=info, storage=storage, outcome=OpOutcome.DRYRUN)
                _raise_if_cancelled(cancel_token, "rm")
            _raise_if_cancelled(cancel_token, "rm")
            return

        with S3Deleter(
            storage,
            request_payer=request_payer,
            on_result=on_result,
            cancel_token=cancel_token,
            operation="rm",
            capture_response=capture_response,
        ) as deleter:
            for info in storage.scan(options):
                _raise_if_cancelled(cancel_token, "rm")
                deleter.submit(info)
            _raise_if_cancelled(cancel_token, "rm")
        _raise_if_cancelled(cancel_token, "rm")
        if deleter.failed:
            raise BatchError(
                f"{deleter.failed} of {deleter.failed + deleter.succeeded} deletes failed",
                succeeded=deleter.succeeded,
                failed=deleter.failed,
                warned=0,
                skipped=0,
                operation="rm",
            ) from deleter.first_error

    @staticmethod
    def _rm_scan_filter(item_filter: FileFilter | None, *, sweep: bool) -> FileFilter | None:
        """The ``ScanOptions.filter`` for rm's enumerating paths.

        The scan stamps each entry's ``Prefix``-relative ``compare_key``, so a
        glob/user filter reads it directly. The
        keyless sweep additionally restricts to folder markers (marker test first,
        then the user filter - aws-cli order). ``None`` when there is nothing to
        test, so unfiltered paths pay no per-object call. Evaluated page by page on
        the scan prefetch worker.
        """
        if not sweep:
            return item_filter
        if item_filter is None:
            return _is_folder_marker
        predicate = item_filter
        return lambda info: _is_folder_marker(info) and predicate(info)

    @staticmethod
    def _rm_single(
        storage: S3Storage,
        item_filter: FileFilter | None,
        *,
        root: str,
        dryrun: bool,
        request_payer: str | None,
        capture_response: bool,
        on_result: ResultCallback | None,
    ) -> None:
        """The blind single-key path (no listing; aws ``_list_single_object``).

        No scan runs here, so the entry's ``compare_key`` (its key relative to
        the prefix returned by `rm_filter_root`, what a glob filter matches) is
        stamped on the hand-built ``FileInfo``.
        """
        key = storage.key
        info = S3FileInfo(key=key, compare_key=key[len(root) :], storage=storage)
        if item_filter is not None and not item_filter(info):
            return
        if dryrun:
            _emit_result(on_result, info=info, storage=storage, outcome=OpOutcome.DRYRUN)
            return
        try:
            response = storage.delete(info, request_payer=request_payer)
        except Boto3S3Error as exc:
            # The single key is still one batch item (aws counts it as a task
            # failure -> "delete failed:" + rc 1), so aggregate rather than
            # re-raising the category error.
            _emit_result(on_result, info=info, storage=storage, outcome=OpOutcome.FAILED, error=exc)
            raise BatchError(
                "1 of 1 deletes failed",
                succeeded=0,
                failed=1,
                warned=0,
                skipped=0,
                operation="rm",
            ) from exc
        # capture_response surfaces the DeleteObject response (minus
        # ResponseMetadata) under extra_info["delete"], the same shape the batched
        # path reconstructs from a DeleteObjects entry.
        extra_info = {"delete": strip_response_metadata(response)} if capture_response else None
        _emit_result(
            on_result,
            info=info,
            storage=storage,
            outcome=OpOutcome.SUCCEEDED,
            extra_info=extra_info,
        )

    # -- bucket / signing -------------------------------------------------

    def mb(
        self,
        target: Location,
        *,
        tags: Sequence[tuple[str, str]] | Mapping[str, str] | None = None,
    ) -> None:
        """Create the bucket of ``target`` with ``aws s3 mb`` semantics.

        ``target`` is an ``"s3://bucket"`` URI string (scheme optional) or an
        ``S3Storage``; a key part is ignored - aws-cli's ``mb`` also keeps
        only the bucket of its path (the CLI layer owns aws's strict path
        checks). The request is shaped the way aws-cli shapes it:
        ``LocationConstraint`` is the client's region unless that is
        ``us-east-1`` (sending it for us-east-1 is an error), a bucket name
        ending in ``-an`` selects the account-regional bucket namespace, and
        ``tags`` become ``CreateBucketConfiguration.Tags``. ``tags`` is a
        mapping or a sequence of ``(key, value)`` pairs; pairs preserve order
        and duplicate keys (passed through for the server to reject, which is
        what the CLI's repeated ``--tags`` parity rests on). A single-call
        operation: failures raise their category error directly, never
        ``BatchError`` (docs/exceptions.md section 4).
        """
        storage = self._resolve_s3_target(target, operation="mb")
        bucket = storage.bucket
        if not bucket:
            raise ValidationError('Invalid bucket name "": mb requires a bucket', operation="mb")
        params: dict[str, Any] = {"Bucket": bucket}
        if bucket.endswith("-an"):
            # aws-cli is_account_regional_namespace_bucket: the -an suffix
            # selects the account-regional bucket namespace.
            params["BucketNamespace"] = "account-regional"
        config: dict[str, Any] = {}
        region = storage.get_client().meta.region_name
        if region != "us-east-1":
            config["LocationConstraint"] = region
        bucket_tags = _bucket_tags(tags)
        if bucket_tags:
            config["Tags"] = bucket_tags
        if config:
            params["CreateBucketConfiguration"] = config
        with s3_errors(operation="mb", bucket=bucket):
            storage.get_client().create_bucket(**params)

    def rb(self, target: Location) -> None:
        """Delete the (empty) bucket of ``target`` with ``aws s3 rb`` semantics.

        ``target`` is an ``"s3://bucket"`` URI string (scheme optional) or an
        ``S3Storage``; a key part is ignored like `mb` (aws-cli rejects
        it, but at the CLI layer). The bucket must already be empty - there
        is no ``force`` parameter: aws-cli composes ``rb --force`` from a
        full ``rm --recursive`` plus the bucket delete at the command layer,
        and callers compose ``S3.rm(target, recursive=True)`` + ``S3.rb``
        the same way. A single-call operation: failures (``BucketNotEmpty``,
        ``NoSuchBucket``, ...) raise their category error directly, never
        ``BatchError`` (docs/exceptions.md section 4).
        """
        storage = self._resolve_s3_target(target, operation="rb")
        bucket = storage.bucket
        if not bucket:
            raise ValidationError('Invalid bucket name "": rb requires a bucket', operation="rb")
        with s3_errors(operation="rb", bucket=bucket):
            storage.get_client().delete_bucket(Bucket=bucket)

    def presign(
        self,
        target: Location,
        *,
        expires_in: int = 3600,
        method: Literal["get_object", "put_object"] = "get_object",
    ) -> str:
        """Return a presigned URL for ``target`` with ``aws s3 presign`` semantics.

        ``target`` is an ``"s3://bucket/key"`` URI string (scheme optional -
        aws-cli's presign also takes ``bucket/key``) or an ``S3Storage``.
        Signing is pure client-side computation: no request is sent, so
        neither bucket nor key existence is checked, and ``expires_in`` is
        not range-validated (aws-cli passes any integer through; S3 enforces
        its 604800-second maximum only when the URL is *used*). An empty
        bucket or key fails botocore's client-side parameter validation ->
        `ValidationError`. ``method`` selects the signed operation -
        aws-cli only ever signs ``get_object``; ``put_object`` is this
        library's permissive superset.

        The signature version follows the client's configuration, exactly
        like boto3: a default client still downgrades presigned URLs to
        SigV2 in regions that accept it (e.g. us-east-1). For aws-cli v2's
        always-SigV4 URLs, build the client with
        ``Config(signature_version="s3v4")`` - the CLI layer does exactly
        that.
        """
        storage = self._resolve_s3_target(target, operation="presign")
        with s3_errors(operation="presign", bucket=storage.bucket, key=storage.key):
            return storage.get_client().generate_presigned_url(
                method,
                Params={"Bucket": storage.bucket, "Key": storage.key},
                ExpiresIn=expires_in,
            )

    def website(
        self,
        target: Location,
        *,
        index_document: str | None = None,
        error_document: str | None = None,
    ) -> None:
        """Set the bucket website configuration with ``aws s3 website`` semantics.

        ``target`` is an ``"s3://bucket"`` URI string (scheme optional) or an
        ``S3Storage``; a key part is ignored - the library is permissive, the
        CLI layer owns aws's strict path handling (aws passes the key *as
        part of the bucket name* and lets botocore reject it). The request is
        shaped the way aws-cli shapes it: ``--index-document`` becomes
        ``IndexDocument.Suffix``, ``--error-document`` becomes
        ``ErrorDocument.Key``, unset ones are omitted - and with neither set
        an **empty** configuration is sent, leaving the rejection to the
        server, exactly like aws. An empty bucket fails botocore's
        client-side parameter validation -> `ValidationError`. A
        single-call operation: failures raise their category error directly,
        never ``BatchError`` (docs/exceptions.md section 4).
        """
        storage = self._resolve_s3_target(target, operation="website")
        config: WebsiteConfigurationTypeDef = {}
        if index_document is not None:
            config["IndexDocument"] = {"Suffix": index_document}
        if error_document is not None:
            config["ErrorDocument"] = {"Key": error_document}
        with s3_errors(operation="website", bucket=storage.bucket):
            storage.get_client().put_bucket_website(
                Bucket=storage.bucket, WebsiteConfiguration=config
            )


# -- module-level convenience -------------------------------------------------
# Thin wrappers over a default ``S3()`` for the common zero-config case
# (``boto3_s3.cp(...)`` rather than ``S3().cp(...)``), in the spirit of requests'
# module-level functions. ``Concatenate[S3, _P]`` strips ``self`` so each wrapper
# keeps its method's exact signature for type checkers and IDEs - no
# ``*args, **kwargs`` erasure - with the method itself as the single source of truth.
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _delegate(method: Callable[Concatenate[S3, _P], _R]) -> Callable[_P, _R]:
    @functools.wraps(method)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        return method(S3(), *args, **kwargs)

    # ``functools.wraps`` sets ``__wrapped__``, which would make
    # ``inspect.signature`` report the method's signature *with* ``self``. Pin
    # the self-stripped signature explicitly (it wins over ``__wrapped__``) so
    # runtime introspection matches the documented contract - the method's
    # exact signature minus ``self`` (docs/s3.md) - for help generators and
    # ``Signature.bind`` consumers, not just for type checkers.
    method_signature = inspect.signature(method)
    wrapper.__signature__ = method_signature.replace(  # pyright: ignore[reportAttributeAccessIssue]
        parameters=list(method_signature.parameters.values())[1:]
    )
    return wrapper


cp = _delegate(S3.cp)
ls = _delegate(S3.ls)
mv = _delegate(S3.mv)
rm = _delegate(S3.rm)
mb = _delegate(S3.mb)
rb = _delegate(S3.rb)
presign = _delegate(S3.presign)
sync = _delegate(S3.sync)
website = _delegate(S3.website)


__all__ = [
    "S3",
    "cp",
    "ls",
    "mb",
    "mv",
    "presign",
    "rb",
    "rm",
    "rm_filter_root",
    "sync",
    "website",
]
