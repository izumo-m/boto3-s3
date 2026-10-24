"""``S3.sync``'s lanes: how each merge-joined pair is decided and acted on.

A ``sync`` run sorts every pair into one of three lanes - create (source
only), update (both sides) and delete (destination only) - by its type
(design/sync.md). This module resolves each lane's filter into a decision,
refuses the filter shapes and combinations a lane cannot run, drives the
pair loop with its pooled decisions, and dispatches the delete lane
(`SyncDeletes`). ``S3.sync`` builds the entry streams, the copy route and
the rollup around it; nothing here is used by another operation.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Executor, Future
from contextlib import ExitStack
from dataclasses import dataclass
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from boto3_s3 import crtsupport, producers
from boto3_s3.awsclicompare import AwsCliComparison
from boto3_s3.comparator import (
    DestOnlyPair,
    MergedPair,
    MergedPairFilter,
    PairFilter,
    ParallelFilter,
    SrcOnlyPair,
    SyncPair,
)
from boto3_s3.deleter import S3Deleter, delete_record
from boto3_s3.exceptions import Boto3S3Error, CancelledError, ValidationError
from boto3_s3.localstorage import LocalStorage, to_native_path
from boto3_s3.s3storage import S3Storage
from boto3_s3.sdkerrors import request_failure
from boto3_s3.storage import Storage
from boto3_s3.types import (
    CancelMode,
    CancelToken,
    FileFilter,
    FileInfo,
    OpOutcome,
    ResultCallback,
    strip_response_metadata,
)

if TYPE_CHECKING:
    # Used in annotations alone.
    from boto3.s3.transfer import TransferConfig


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


def _update_via_filter(decide: PairFilter) -> PairFilter:
    """``update_filter`` as a ``PairFilter``: it already decides over the update
    lane's ``SyncPair``, so it is used as it is."""
    return decide


_PairT = TypeVar("_PairT")
# The entry type a lane's filter takes: a FileInfo (create / delete) or the pair itself (update).
_E = TypeVar("_E")


@dataclass(frozen=True, slots=True)
class _LaneDecision(Generic[_PairT]):
    """One ``S3.sync`` lane's resolved filter.

    ``decide`` is the predicate over the lane's pair shape selecting whether to
    act on a pair; ``executor`` is the caller's pool when the filter was a
    ``ParallelFilter`` (else ``None``: decide inline on the calling thread).
    """

    decide: Callable[[_PairT], bool]
    executor: Executor | None


def _resolve_lane(
    flt: bool | Callable[[_E], bool] | ParallelFilter[_E],
    wrap: Callable[[Callable[[_E], bool]], Callable[[_PairT], bool]],
) -> _LaneDecision[_PairT]:
    """Resolve one lane's filter by the shapes every lane takes.

    ``True`` acts on every pair, ``False`` on none; a filter is turned by
    ``wrap`` (`_create_via_filter` / `_update_via_filter` /
    `_delete_via_filter`) into a decision over the lane's pair shape; a
    ``ParallelFilter`` unwraps to that same filter plus the caller's pool (the
    executor its ``decide`` should run on).
    """
    if flt is True:
        return _LaneDecision(_always, None)
    if flt is False:
        return _LaneDecision(_never, None)
    if isinstance(flt, ParallelFilter):
        pooled = cast("ParallelFilter[_E]", flt)  # isinstance loses the type arg; pin it
        return _LaneDecision(wrap(pooled.decide), pooled.executor)
    return _LaneDecision(wrap(flt), None)


def resolve_lanes(
    *,
    create_filter: bool | FileFilter | ParallelFilter[FileInfo],
    update_filter: bool | PairFilter | ParallelFilter[SyncPair] | None,
    delete_filter: bool | FileFilter | ParallelFilter[FileInfo],
    pair_filter: MergedPairFilter | None,
) -> tuple[_LaneDecision[SrcOnlyPair], _LaneDecision[SyncPair], _LaneDecision[DestOnlyPair] | None]:
    """``S3.sync``'s create, update and delete decisions; the delete one is ``None`` when off.

    Each lane's filter resolves by the shapes every lane takes
    (`_resolve_lane`), each lane keeping its own pair shape and one rule of
    its own (below); a ``pair_filter`` replaces all three.
    """
    if pair_filter is not None:
        # One callable in place of the three lanes: each lane's decide IS that
        # callable, since each hands it its own MergedPair shape, and no lane
        # gets a pool (the hook decides serially, in compare-key order, on the
        # calling thread). The delete lane is live either way - the callback
        # may delete - so sync prepares its machinery exactly as for a callable
        # delete_filter, including the destination walk's dropped read-ahead
        # (the delete decision is not None). The three lane filters are at
        # their defaults here (`reject_pair_filter_conflicts`), so none is
        # overridden.
        return (
            _LaneDecision(pair_filter, None),
            _LaneDecision(pair_filter, None),
            _LaneDecision(pair_filter, None),
        )
    # create_filter: True copies every new (source-only) entry, False none, a
    # FileFilter only those it keeps (matched like rm).
    create = _resolve_lane(create_filter, _create_via_filter)
    # update_filter picks exactly one copy strategy for the update
    # (both-sides) pairs; update_filter=None is the aws-cli size +
    # last-modified default, equivalently AwsCliComparison().
    update = _resolve_lane(
        AwsCliComparison() if update_filter is None else update_filter, _update_via_filter
    )
    # delete_filter: False = off, True deletes every orphan (destination-only),
    # a FileFilter only those it keeps (matched like rm); the producer-stamped
    # compare_key lets it read the entry directly.
    delete = None if delete_filter is False else _resolve_lane(delete_filter, _delete_via_filter)
    return create, update, delete


def reject_pair_filter_conflicts(
    pair_filter: object,
    *,
    create_filter: object,
    update_filter: object,
    delete_filter: object,
    no_overwrite: bool,
) -> None:
    """Refuse a ``S3.sync`` ``pair_filter`` paired with an option it cannot honor.

    ``pair_filter`` *replaces* the three lane filters, so a non-default
    ``create_filter`` / ``update_filter`` / ``delete_filter`` beside it has no
    lane left to govern; silently letting one win would hide which decision
    actually ran, so the combination is rejected instead (design/overview.md
    section 3, unsatisfiable option combinations). ``no_overwrite=True`` drops
    the update lane wholesale, which contradicts the hook's contract that every
    merged pair reaches the callback, so it is rejected on the same ground. A
    ``ParallelFilter`` is refused eagerly rather than left to fail as a
    ``TypeError`` mid-run: it is a value container, not a callable, and a
    serial decision in compare-key order is the whole point of this hook.

    Takes its arguments as ``object`` because this is the shape check itself -
    an unchecked caller reaches it with whatever it passed.
    """
    if isinstance(pair_filter, ParallelFilter):
        raise ValidationError(
            "sync: pair_filter cannot be a ParallelFilter - every merged pair is decided "
            "serially on the calling thread; wrap a lane filter instead",
            operation="sync",
        )
    conflicts = [
        name
        for name, value, default in (
            ("create_filter", create_filter, True),
            ("update_filter", update_filter, None),
            ("delete_filter", delete_filter, False),
        )
        if value is not default
    ]
    if conflicts:
        raise ValidationError(
            "sync: pair_filter replaces the lane filters, so it cannot be combined with "
            f"{', '.join(conflicts)}",
            operation="sync",
        )
    if no_overwrite:
        raise ValidationError(
            "sync: pair_filter cannot be combined with no_overwrite=True - no_overwrite drops "
            "the update lane, so not every merged pair would reach pair_filter",
            operation="sync",
        )


def reject_lane_filter_shapes(
    *, create_filter: object, update_filter: object, delete_filter: object
) -> None:
    """Refuse a ``S3.sync`` lane filter that is none of the shapes a lane can run.

    A lane takes ``True`` / ``False``, a callable, or a ``ParallelFilter``
    (``update_filter`` also ``None``, the aws-cli judgment). Anything else -
    ``None`` on ``create_filter`` / ``delete_filter``, where ``update_filter``'s
    default invites the slip, or an integer - is an unchecked caller's error
    that would otherwise pass the ``is True`` / ``is False`` tests, run both
    listings, submit transfers and only then fail as a mid-run ``TypeError``
    when the lane first calls it; refuse it up front instead, the same
    reasoning as `reject_pair_filter_conflicts` and the unknown-option
    check. A ``ParallelFilter`` is held to the same test one level down: the
    ``decide`` it wraps must be callable. Takes ``object`` because this is
    the shape check itself.
    """
    for name, value, allows_none in (
        ("create_filter", create_filter, False),
        ("update_filter", update_filter, True),
        ("delete_filter", delete_filter, False),
    ):
        if value is None and allows_none:
            continue
        if isinstance(value, ParallelFilter):
            # The container is a shape; what it wraps is called on the pool and
            # must be callable for the same reason a bare filter must.
            decide = cast("ParallelFilter[Any]", value).decide
            if not callable(decide):
                raise ValidationError(
                    f"sync: {name}'s ParallelFilter must wrap a callable, not {decide!r}",
                    operation="sync",
                )
            continue
        if isinstance(value, bool) or callable(value):
            continue
        shapes = "None, True, False, a callable, or a ParallelFilter"
        if not allows_none:
            shapes = "True, False, a callable, or a ParallelFilter"
        raise ValidationError(f"sync: {name} must be {shapes}, not {value!r}", operation="sync")


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
class Lane(Generic[_PairT]):
    """One of ``S3.sync``'s three pair lanes: how to decide and act on it.

    ``_PairT`` is the lane's pair shape (``SrcOnlyPair`` / ``SyncPair`` /
    ``DestOnlyPair``). ``decide`` is the predicate selecting whether to act on a
    pair; ``submit`` performs it (``submit_copy`` for create / update,
    ``SyncDeletes.submit`` for delete). ``executor`` is the caller's pool when
    the lane's filter was wrapped in ``ParallelFilter`` - then ``decide`` runs
    there and ``submit`` runs on the calling thread once the result is in; ``None``
    runs both inline in compare-key order.
    """

    decide: Callable[[_PairT], bool]
    submit: Callable[[_PairT], None]
    executor: Executor | None


def run_sync_pairs(
    pairs: Iterator[MergedPair],
    *,
    create_lane: Lane[SrcOnlyPair],
    update_lane: Lane[SyncPair] | None,
    delete_lane: Lane[DestOnlyPair] | None,
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
        """Optionally cancel queued decisions, then await every accepted one.

        Re-observes the token each pass: an upgrade to `IMMEDIATE` arriving
        while a graceful settle is already waiting must still cancel the
        queued decisions (`CancelToken`'s escalation contract) - a decision
        that can only complete through `cancel()`, such as one queued on an
        executor that will never run it, would otherwise pin this loop
        forever. `cancel` never downgrades, matching the token's monotonicity.
        """
        remaining = pending
        while remaining:
            cancel = cancel or (
                cancel_token is not None and cancel_token.mode is CancelMode.IMMEDIATE
            )
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

    def handle(lane: Lane[_PairT] | None, pair: _PairT) -> bool:
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


class SyncDeletes:
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
        transfer_config: TransferConfig | None = None,
        crt: crtsupport.CrtPostures | None = None,
    ) -> None:
        self._dest = dest_storage
        # What the deleter's client is chosen by (S3Deleter): the run's
        # TransferConfig and the S3 instance's CRT postures.
        self._transfer_config = transfer_config
        self._crt = crt if crt is not None else crtsupport.CrtPostures()
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
                        transfer_config=self._transfer_config,
                        session=self._crt.session,
                        crt_endpoint=self._crt.endpoint,
                        crt=self._crt.crt,
                    )
                )
            self._deleter.submit(info)
            return
        # Local or custom (s3open) destination: remove the orphan synchronously
        # through the backend's own Storage.delete(info), sharing the local
        # counters. A backend error is mapped into the library taxonomy (the same
        # translate the byte-moving delete path uses), so every result carries a
        # Boto3S3Error and the message `aws` prints survives; the catch is broad.
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
        """Map a synchronous-delete error into the taxonomy and count it.

        The same capture as the blind single-key ``rm`` (`request_failure`):
        a pass-through family error - a ``LocalStorage.delete`` that raised
        unnamed - is stamped with this run's name and keeps its own cause; a
        non-family exception becomes the base error carrying it as
        ``__cause__`` (exceptions.md section 2.1).
        """
        error = request_failure(exc, operation="sync", key=info.key)
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
                delete_record(
                    info,
                    outcome,
                    src=src,
                    storage=self._dest,
                    error=error,
                    extra_info=extra_info,
                )
            )


__all__: list[str] = []
