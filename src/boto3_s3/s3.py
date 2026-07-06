"""The boto3-s3 ``S3``: entry point for ``aws s3``-style operations.

``S3`` holds no connection of its own; it is a pure orchestrator. Every
``Location`` argument resolves to a ``Storage``: a local path becomes a
``LocalStorage`` and an ``"s3://..."`` string becomes an ``S3Storage``. The
boto3 client lives on the ``S3Storage``; when its client is omitted it falls
back to ``boto3.client("s3")``. To target a custom endpoint / profile / region
(e.g. MinIO) or a second account for S3-to-S3, pass an explicit
``S3Storage(url, client=...)`` instead of a bare string.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack
from queue import Queue
from typing import TYPE_CHECKING, Any, Concatenate, Literal, ParamSpec, TypeVar

from typing_extensions import Unpack

from boto3_s3 import producers, transferplan
from boto3_s3.awsclicompare import AwsCliComparison
from boto3_s3.comparator import (
    Comparator,
    PairFilter,
    ParallelCompare,
    SyncPair,
)
from boto3_s3.deleter import S3Deleter
from boto3_s3.exceptions import (
    BatchError,
    Boto3S3Error,
    CancelledError,
    NotFoundError,
    ValidationError,
)
from boto3_s3.iostorage import IOStorage
from boto3_s3.localstorage import LocalStorage, to_native_path, translate_os_error
from boto3_s3.s3storage import S3Storage, s3_errors, translate_boto_error
from boto3_s3.storage import Location, Storage
from boto3_s3.transfer import TransferItem, Transferrer
from boto3_s3.types import (
    CancelToken,
    FileFilter,
    FileInfo,
    OpOutcome,
    OpResult,
    ProgressCallback,
    ResultCallback,
    S3FileInfo,
    S3ScanOptions,
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
    """The prefix root an ``rm`` of ``key`` operates under (aws-cli parity).

    A recursive target is normalized to end with ``/`` (aws-cli
    ``FileFormat.s3_format``): ``rm s3://b/data --recursive`` lists under
    ``data/`` and does not touch ``data-sibling.txt``. A non-recursive key
    roots at its parent "directory", and a bucket-root target at ``""``
    (aws-cli ``filters._get_s3_root``). ``--exclude`` / ``--include``
    patterns resolve relative to this root, and the recursive listing uses
    it as the ``Prefix``. Always empty or ``/``-terminated.
    """
    if recursive:
        return f"{key}/" if key and not key.endswith("/") else key
    if not key or key.endswith("/"):
        return key
    head, sep, _tail = key.rpartition("/")
    return f"{head}/" if sep else ""


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
                key=info.key,
                outcome=outcome,
                error=error,
                src=f"s3://{storage.bucket}/{info.key}",
                src_info=info,
                src_storage=storage,
                extra_info=extra_info,
            )
        )


def _is_folder_marker(info: FileInfo) -> bool:
    """A zero-byte ``/``-terminated key - the manual-folder convention."""
    return info.size == 0 and info.key.endswith("/")


def _copy_all(_pair: SyncPair) -> bool:
    """A pair judgment that always copies (``create_filter=True`` every new entry,
    ``update_filter=True`` every update)."""
    return True


def _copy_none(_pair: SyncPair) -> bool:
    """A pair judgment that never copies (``create_filter=False`` no new entry,
    ``update_filter=False`` no update - additive-only, existing left as-is)."""
    return False


def _create_via_filter(keep: FileFilter) -> PairFilter:
    """``create_filter`` as a ``FileFilter``: copy a new (source-only) pair iff the
    source entry is kept (matched like ``delete_filter`` / ``rm``)."""

    def decide(pair: SyncPair) -> bool:
        assert pair.src is not None  # a source-only (new) pair always has a source
        return keep(pair.src)

    return decide


def _compare_workers(transfer_config: TransferConfig | None) -> int:
    """A ``ParallelCompare``'s default thread count: the transfer config's
    ``max_concurrency`` when explicitly set, else boto3's transfer default of 10
    (boto3 sets the attribute dynamically, so it is read defensively)."""
    configured: object = getattr(transfer_config, "max_concurrency", None)
    return configured if isinstance(configured, int) and configured >= 1 else 10


def _run_sync_pairs(
    pairs: Iterator[SyncPair],
    *,
    create_decide: PairFilter,
    update_decide: PairFilter,
    submit_copy: Callable[[SyncPair], None],
    submit_delete: Callable[[SyncPair], None],
    no_overwrite: bool,
    delete_on: bool,
    cancel_token: CancelToken | None,
    workers: int | None,
) -> None:
    """Drive ``S3.sync``'s pair loop - serial, or pooled when ``workers``.

    Each pair lands in exactly one lane by which sides it has: a source-only
    (new) pair goes to ``create_decide``, a both-sides (update) pair to
    ``update_decide``, a destination-only (orphan) pair to the delete lane
    (``delete_on``). Only the update decision (``update_decide``, where a
    content compare does its I/O) is parallelized; the new and delete lanes stay
    on the calling thread in compare-key order, so the case-conflict gate keeps
    its deterministic "first key wins" order. ``submit_copy`` / ``submit_delete``
    (and the gate they reach) therefore only ever run on this thread; the pool
    runs nothing but ``update_decide``. A ``update_decide`` exception
    propagates here (aborting the sync) when its result is consumed;
    ``cancel_token`` is polled between pairs. Behaviour matches the serial path
    bar ordering of the pooled pairs.
    """

    def cancelled() -> bool:
        return cancel_token is not None and cancel_token.cancelled

    if workers is None:
        for pair in pairs:
            if cancelled():
                raise CancelledError("sync was cancelled", operation="sync")
            if pair.src is not None:
                if pair.dest is None:
                    if create_decide(pair):
                        submit_copy(pair)
                elif not no_overwrite and update_decide(pair):
                    submit_copy(pair)
            elif delete_on:
                submit_delete(pair)
        return

    done: Queue[tuple[SyncPair, Future[bool]]] = Queue()
    with ThreadPoolExecutor(workers, thread_name_prefix="boto3-s3-compare") as pool:

        def dispatch() -> bool:
            # Pull pairs (compare-key order) until an update pair needs a pooled
            # decision; handle new (source-only) and delete pairs inline, in order.
            for pair in pairs:
                if cancelled():
                    raise CancelledError("sync was cancelled", operation="sync")
                if pair.src is not None:
                    if pair.dest is not None:
                        # Update (both sides): the content compare does I/O, so
                        # pool it - unless no_overwrite already rules it out.
                        if no_overwrite:
                            continue
                        pool.submit(update_decide, pair).add_done_callback(
                            lambda f, p=pair: done.put((p, f))
                        )
                        return True
                    # New (source-only): the create lane does no I/O, so decide
                    # inline and keep the case-conflict gate in order.
                    if create_decide(pair):
                        submit_copy(pair)
                elif delete_on:
                    submit_delete(pair)
            return False

        inflight = 0
        while inflight < workers and dispatch():
            inflight += 1
        while inflight:
            if cancelled():
                raise CancelledError("sync was cancelled", operation="sync")
            pair, future = done.get()
            inflight -= 1
            if future.result():
                submit_copy(pair)
            if dispatch():
                inflight += 1


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
    batches through :class:`S3Deleter` (the ``rm`` machinery - a wire-level
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
        capture_response: bool = False,
    ) -> None:
        self._dest = dest_storage
        self._request_payer = request_payer
        self._dryrun = dryrun
        self._on_result = on_result
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

    def submit(self, pair: SyncPair) -> None:
        info = pair.dest
        assert info is not None  # the delete lane runs only on destination-only pairs
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
            else producers.open_side_display(dest, pair.key)
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
                    key=info.key,
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
    so it needs no cleanup. Each ``Location`` argument is resolved by
    :meth:`resolve` - an ``"s3://..."`` string becomes an ``S3Storage`` carrying
    a client from :meth:`client`, anything else a ``LocalStorage``. A ``Storage``
    instance passed in is used verbatim; only bare ``"s3://"`` strings inherit
    these defaults.

    :meth:`client` and :meth:`resolve` are the customization seams - subclass and
    override ``client`` to change credentials / session / endpoint (or return a
    test double), or ``resolve`` to interpret new URL schemes (e.g. ``http://``).
    A second account for S3-to-S3, or a per-location client, is expressed by an
    explicit ``S3Storage(url, client=...)``. Debug-log secret masking is
    configured via :func:`boto3_s3.set_stream_logger`. The instance's own state
    is thread-safe to share, but its clients need care: ``client()`` is not safe
    to call concurrently (boto3's session construction path), and boto3-s3 uses
    ``s3transfer``, whose per-transfer setup is not thread-safe on a shared
    client. To run operations in parallel across threads, build one client per
    thread sequentially, up front, then give each thread its own
    ``S3Storage(url, client=...)`` - never share a client, never build clients
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

    def client(self) -> S3Client:
        """Build a boto3 S3 client from this instance's defaults (the factory seam).

        A fresh client each call, owned by the caller - ``S3`` keeps no
        connection, hence no ``close()``. Built from ``session`` (or boto3's
        default session) with ``endpoint_url`` / ``config`` applied. Override to
        change credentials, reuse a cached client, or return a test double; reuse
        one explicitly via ``S3Storage(url, client=s3.client())``. A failed build
        raises the translated ``Boto3S3Error`` - ``ConfigurationError`` for
        unresolvable credentials / region, its ``InvalidConfigError``
        refinement for a set-but-unusable ``AWS_PROFILE`` or partial
        credentials - never the raw botocore error (docs/exceptions.md
        section 1).
        """
        # operation=None: no subcommand is in scope at build time.
        with s3_errors(operation=None):
            if self._session is not None:
                return self._session.client(
                    "s3", endpoint_url=self._endpoint_url, config=self._config
                )
            # Deferred: only this SDK-touching seam loads boto3 (import contract,
            # docs/imports.md); constructing an `S3` stays SDK-free.
            import boto3

            return boto3.client("s3", endpoint_url=self._endpoint_url, config=self._config)

    def resolve(self, loc: Location) -> Storage:
        """Resolve a ``Location`` to a ``Storage`` (the URL-interpretation seam).

        A ``Storage`` instance is returned unchanged; an ``"s3://..."`` string
        becomes an ``S3Storage`` carrying :meth:`client`; anything else is a
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

        Returns an :class:`~boto3_s3.awsconfig.AwsConfig` reader over the config
        file resolved for this ``S3``'s ``session`` (or a default
        ``AWS_PROFILE``-aware session when none was given - the same resolution
        :meth:`client` uses). Use it to surface profile settings the application
        did not set itself - e.g. the ``[s3]`` transfer tuning::

            chunksize = s3.aws_config().get_size("s3.multipart_chunksize", 8 * 1024**2)

        This is a building block offered **explicitly**: ``S3``'s own operations
        (``cp`` / ``sync`` / ...) never read the config file on their own, so they
        carry no hidden dependence on the ambient ``~/.aws/config``. The reader is
        memoized per instance (the file is parsed once). Matching ``aws s3``'s
        ``[s3]`` semantics on top of these values (the defaults table, validation,
        the engine decision) is the CLI distribution's job, not the library's.
        """
        # Deferred: the reader's botocore touch (and the boto3 default session)
        # load only when a caller actually asks (import contract, docs/imports.md).
        from boto3_s3.awsconfig import AwsConfig

        if self._aws_config is None:
            self._aws_config = AwsConfig.from_session(self._session)
        return self._aws_config

    # -- listing ----------------------------------------------------------

    def ls(
        self,
        target: Location = "s3://",
        *,
        recursive: bool = False,
        page_size: int = 1000,
        request_payer: str | None = None,
        bucket_name_prefix: str | None = None,
        bucket_region: str | None = None,
    ) -> Iterator[FileInfo]:
        """List objects and common prefixes under an S3 ``target``, or all buckets.

        ``ls`` is S3-only (aws-cli parity): ``target`` is an ``"s3://..."`` URI
        string (the ``s3://`` scheme is optional) or an ``S3Storage``. A target
        with no bucket - the default bare ``"s3://"`` - is the service root:
        ``ls`` dispatches to :meth:`S3Storage.list_buckets` (aws-cli's ``ls``
        splits the bucket listing from the object listing the same way), yielding
        one ``BUCKET``-kind entry per bucket (``mtime`` = creation date).
        ``bucket_name_prefix`` / ``bucket_region`` filter *that* bucket listing and
        are meaningless for an object listing; conversely ``recursive`` /
        ``request_payer`` are meaningless at the service root (both ignored, like
        aws-cli). A non-S3 ``Location`` raises ``ValidationError``. The target is
        validated eagerly; iteration is lazy.
        """
        storage = self._resolve_s3_target(target, operation="ls")
        if not storage.bucket:
            return storage.list_buckets(
                page_size=page_size, name_prefix=bucket_name_prefix, region=bucket_region
            )
        return storage.scan(
            S3ScanOptions(recursive=recursive, page_size=page_size, request_payer=request_payer)
        )

    def _resolve_s3_target(self, target: Location, *, operation: str) -> S3Storage:
        if isinstance(target, S3Storage):
            storage = target
        else:
            if isinstance(target, os.PathLike):
                # Honor the Location os.PathLike[str] contract, like resolve() does;
                # a non-S3 Storage (no __fspath__) still falls through to the raise.
                target = os.fspath(target)
            if not isinstance(target, str):
                raise ValidationError(
                    f"{operation} accepts an 's3://...' URI string or an S3Storage",
                    operation=operation,
                )
            # S3-only ops are lenient about the s3:// scheme (a bare "bucket/key", or
            # the "s3://" service root, both work); the client carries this S3's
            # defaults. Unlike resolve(), a non-s3 string is not a local fallback.
            storage = S3Storage(target, client=self.client())
        # Construction is permissive (non-raising); run the strict aws-cli checks
        # (unsupported ARN forms, key-without-bucket) before the op uses it.
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
        follow_symlinks: bool = True,
        detect_symlink_loops: bool = False,
        dryrun: bool = False,
        page_size: int = 1000,
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
        download, S3->S3 copy (local->local is rejected, like aws). Path
        shapes - what an existing-directory or trailing-slash destination
        means, which side's name wins, where ``--exclude`` / ``--include``
        patterns root - reproduce aws-cli's ``FileFormat`` rules
        (:mod:`boto3_s3.transferplan`); ``recursive`` is the aws-cli ``dir_op``.
        Bytes move through :class:`boto3_s3.transfer.Transferrer`
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
        ``copy_props``, and a single S3 source resolved by HeadObject - a
        404 raises ``NotFoundError`` with aws's rewritten ``Key "..." does
        not exist`` message. A keyless non-recursive S3 source matches
        nothing and transfers nothing (aws lists and discards; same outcome
        without the requests).

        ``filter`` keeps an item in the operation (rm's contract): a
        ``FileFilter`` predicate over the item's ``FileInfo``, whose
        ``compare_key`` is stamped to the source path relative to the transfer
        root - a :class:`~boto3_s3.globsieve.GlobFilter` matches that key (a
        relative pattern) or the full ``key`` (an absolute one) while a richer
        predicate can read size / mtime / storage_class.
        ``detect_symlink_loops`` (default ``False``; a library extension - ``aws
        s3`` has no such option, so off keeps parity) guards a recursive local
        walk against symlink cycles: with it (and ``follow_symlinks``) a directory
        resolving to one of its own ancestors is skipped with a warning instead of
        recursing until ``RecursionError`` - off costs no extra ``stat``.
        ``dryrun`` enumerates (listing and HeadObject still
        run) and reports ``OpOutcome.DRYRUN`` without transferring; warnings
        still apply. ``expected_size`` is the multipart sizing hint for a
        streaming upload (stdin -> S3): it is forwarded to the stream path
        (:meth:`_cp_stream` sets it as the upload item ``size``) and is ignored
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
            follow_symlinks=follow_symlinks,
            detect_symlink_loops=detect_symlink_loops,
            dryrun=dryrun,
            page_size=page_size,
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
        follow_symlinks: bool,
        detect_symlink_loops: bool,
        dryrun: bool,
        page_size: int,
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
        case_gate = producers.cp_case_gate(plan, recursive=recursive, options=options)
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
            capture_response=capture_response,
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
                    follow_symlinks=follow_symlinks,
                    detect_symlink_loops=detect_symlink_loops,
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
                    page_size=page_size,
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
                    follow_symlinks=follow_symlinks,
                    detect_symlink_loops=detect_symlink_loops,
                    item_filter=item_filter,
                )
            else:
                items = producers.s3_source_items(
                    plan,
                    src_s3,
                    transfer_type=transfer_type,
                    dest_bucket=dest_bucket,
                    transferrer=transferrer,
                    page_size=page_size,
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
                if cancel_token is not None and cancel_token.cancelled:
                    raise CancelledError(f"{operation} was cancelled", operation=operation)
                try:
                    item = next(item_iter)
                except StopIteration:
                    break
                if dryrun:
                    transferrer.dryrun(item)
                else:
                    transferrer.submit(item)
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
        transfer_config: TransferConfig | None,
        capture_response: bool = False,
        options: TransferOptions,
    ) -> None:
        """One streaming transfer: an ``IOStorage`` on exactly one side.

        The other side must be S3; its key is taken verbatim (the CLI owns aws's
        ``-``-basename naming quirk). The stream's fileobj comes from
        ``IOStorage.open`` and is handed straight to ``s3transfer``. Uploads honor
        ``expected_size`` as the multipart sizing hint (without it the engine
        buffers up to the threshold and decides); downloads provide neither size
        nor etag, so s3transfer probes the object with a HeadObject - the aws
        stream wire shape. Streams are single items: the glacier / parent-ref
        gates do not apply; displays render as ``-``.
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
        if src_is_stream:
            storage = self._resolve_s3_target(dest_storage, operation="cp")
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
            storage = self._resolve_s3_target(src_storage, operation="cp")
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
            capture_response=capture_response,
        )
        with transferrer:
            if dryrun:
                transferrer.dryrun(item)
            else:
                transferrer.submit(item)
        if transferrer.failed:
            raise BatchError(
                "1 of 1 transfers failed",
                succeeded=transferrer.succeeded,
                failed=transferrer.failed,
                warned=transferrer.warned,
                skipped=transferrer.skipped,
                operation="cp",
            ) from transferrer.first_error

    def mv(
        self,
        src: Location,
        dest: Location,
        *,
        recursive: bool = False,
        filter: FileFilter | None = None,
        follow_symlinks: bool = True,
        detect_symlink_loops: bool = False,
        dryrun: bool = False,
        page_size: int = 1000,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        transfer_config: TransferConfig | None = None,
        capture_response: bool = False,
        **options: Unpack[TransferOptions],
    ) -> None:
        """Move bytes with ``aws s3 mv`` semantics: ``cp``, then delete the source.

        Everything :meth:`cp` documents - routes, path shapes, filters,
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
        with :class:`boto3_s3.pathresolver.S3PathResolver` (the
        ``--validate-same-s3-paths`` machinery; bring your own s3control /
        sts clients) when that risk applies.
        """
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
            follow_symlinks=follow_symlinks,
            detect_symlink_loops=detect_symlink_loops,
            dryrun=dryrun,
            page_size=page_size,
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
        create_filter: bool | FileFilter = True,
        update_filter: bool | PairFilter | ParallelCompare | None = None,
        delete_filter: bool | FileFilter = False,
        follow_symlinks: bool = True,
        detect_symlink_loops: bool = False,
        dryrun: bool = False,
        page_size: int = 1000,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        transfer_config: TransferConfig | None = None,
        capture_response: bool = False,
        **options: Unpack[TransferOptions],
    ) -> None:
        """Recursively synchronize ``src`` into ``dest`` (``aws s3 sync``).

        Always recursive (no single-file sync, aws parity) over the three
        transfer routes; a local->local pair raises ``ValidationError``. The
        pipeline has two layers:

        - **Visibility** - both listings are pruned independently *before*
          the sides meet by the single ``filter``, a
          :data:`~boto3_s3.types.FileFilter` applied to each side's
          root-relative compare key - so it prunes the source and the
          destination **symmetrically** (matching aws, which evaluates
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
          compare key (:class:`~boto3_s3.comparator.Comparator`) and each
          :class:`~boto3_s3.comparator.SyncPair` lands in exactly one of three
          lanes by which sides it has, each a filter deciding whether to act:
          **create** a new entry, **overwrite** one on both sides, or **delete**
          an orphan. ``create_filter`` and ``delete_filter`` are the two
          membership knobs (create / delete) - duals of each other;
          ``update_filter`` is the overwrite judgment for the intersection. The
          aws equivalent is the default of all three:

          - ``create_filter`` - the **new** (source-only) entries: ``True``
            (default) copies every one, ``False`` none, a
            :data:`~boto3_s3.types.FileFilter` only those it keeps (matched
            against the source ``FileInfo`` / compare key, the same shape as
            ``rm``'s ``filter``). aws hard-codes "always create"
            (``file_not_at_dest``), so this knob has no aws counterpart.
          - ``update_filter`` - the **update** (both-sides) pairs: ``None``
            (default) is the aws-cli size + last-modified judgment, equivalently
            ``AwsCliComparison()`` - tune it with
            ``AwsCliComparison(size_only=...)`` / ``(exact_timestamps=...)``;
            ``True`` re-copies every one, ``False`` none (additive-only:
            existing destinations are left as-is); any
            :data:`~boto3_s3.comparator.PairFilter` is a custom strategy - the
            content building blocks ``EtagComparison`` / ``ChecksumComparison``
            (submodule imports) are drop-in replacements that compare by
            content. Wrapping any strategy in
            :class:`~boto3_s3.comparator.ParallelCompare` runs these update
            decisions on a thread pool - identical results, only faster (the
            wrapped strategy must be thread-safe). A custom strategy is only
            ever handed an update pair, so it never faces a ``None`` side.
          - ``delete_filter`` - the **orphan** (destination-only) entries:
            ``False`` (default) deletes nothing, ``True`` deletes every one
            (aws ``--delete``), a :data:`~boto3_s3.types.FileFilter` only those
            it keeps (same shape as ``rm``'s ``filter``).

          ``no_overwrite`` is an orthogonal write-guard on the update lane: an
          existing destination is never overwritten (new entries still copy),
          and sync keeps it decision-only - no ``IfNoneMatch`` on the wire.

        Transfers run on the engine with cp's gates (glacier, the
        parent-directory guard, the ``--case-conflict`` gate for downloads -
        applied only to pairs missing at the destination, the aws-cli slot).
        Deletions are dispatched as they stream: batched ``DeleteObjects``
        for an S3 destination (the ``rm`` machinery; ``request_payer``
        forwarded), a synchronous ``Storage.delete`` for a local one
        (``LocalStorage.delete``, an ``os.remove``). ``dryrun``
        reports every would-be transfer and deletion without any API call.
        Local listing warnings (unreadable / vanished / special files,
        invalid timestamps) surface from **both** sides as WARNED records,
        exactly like aws walking both trees.

        A missing local source directory raises (aws's ``The user-provided
        path ... does not exist.``); a local destination directory is
        created up front even when nothing transfers. Item failures -
        transfer and delete alike - aggregate into one ``BatchError`` with
        rollup counts (first failure as ``__cause__``); ``cancel_token``
        raises ``CancelledError`` between pairs.
        """
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
        # create_filter governs the new (source-only) pairs: True copies every new
        # entry, False none, a FileFilter only those it keeps (matched like rm).
        create_decide: PairFilter
        if create_filter is True:
            create_decide = _copy_all
        elif create_filter is False:
            create_decide = _copy_none
        else:
            create_decide = _create_via_filter(create_filter)
        # update_filter picks exactly one copy strategy for the update
        # (both-sides) pairs; update_filter=None is the aws-cli size +
        # last-modified default, equivalently AwsCliComparison().
        compare_workers: int | None = None
        update_decide: PairFilter
        if isinstance(update_filter, ParallelCompare):
            compare_workers = (
                update_filter.workers
                if update_filter.workers is not None
                else _compare_workers(transfer_config)
            )
            update_decide = update_filter.compare
        elif update_filter is None:
            update_decide = AwsCliComparison()
        elif update_filter is True:
            update_decide = _copy_all
        elif update_filter is False:
            update_decide = _copy_none
        else:
            update_decide = update_filter
        # delete_filter is False/True (no per-orphan filter) or a FileFilter that
        # narrows which destination-only orphans are deleted (matched like rm);
        # the producer-stamped compare_key lets it read the entry directly.
        delete_keep = delete_filter if not isinstance(delete_filter, bool) else None
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
            capture_response=capture_response,
        )
        deletes = _SyncDeletes(
            dest_storage,
            request_payer=options.get("request_payer"),
            dryrun=dryrun,
            on_result=on_result,
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
                follow_symlinks=follow_symlinks,
                detect_symlink_loops=detect_symlink_loops,
                page_size=page_size,
                options=options,
            )
            dest_entries = producers.sync_entries(
                dest_storage,
                root=plan.dest_root,
                item_filter=filter,
                transferrer=transferrer,
                follow_symlinks=follow_symlinks,
                detect_symlink_loops=detect_symlink_loops,
                page_size=page_size,
                options=options,
            )
            src_bucket = src_storage.bucket if isinstance(src_storage, S3Storage) else ""

            def submit_copy(pair: SyncPair) -> None:
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

            def submit_delete(pair: SyncPair) -> None:
                if delete_keep is not None:
                    assert pair.dest is not None
                    if not delete_keep(pair.dest):
                        return
                deletes.submit(pair)

            _run_sync_pairs(
                Comparator(transfer_type).compare(src_entries, dest_entries),
                create_decide=create_decide,
                update_decide=update_decide,
                submit_copy=submit_copy,
                submit_delete=submit_delete,
                no_overwrite=no_overwrite,
                delete_on=bool(delete_filter),
                cancel_token=cancel_token,
                workers=compare_workers,
            )

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
        page_size: int = 1000,
        request_payer: str | None = None,
        on_result: ResultCallback | None = None,
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
          ``DeleteObjects`` calls via :class:`S3Deleter` (a wire-level
          deviation from aws-cli's per-key ``DeleteObject``; docs/deleter.md).
        - **bucket root, non-recursive**: lists the whole bucket but deletes
          only zero-byte ``/``-terminated "folder marker" objects (any depth)
          - aws-cli's manual-folder sweep, not a full wipe.

        ``filter`` keeps an item in the deletion when it is *included*: a
        ``Callable[[FileInfo], bool]`` over the entry's ``FileInfo``. Its
        ``compare_key`` is stamped to the key relative to :func:`rm_filter_root`
        before the call, so a :class:`~boto3_s3.globsieve.GlobFilter` matches
        that key (the CLI maps ``--exclude`` / ``--include`` here) while a
        richer predicate can read ``size`` / ``mtime`` / ``storage_class`` (on
        the blind single-key path only ``key`` and ``compare_key`` are
        populated). On the enumerating paths it is evaluated page by page on
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
        directly.
        """
        storage = self._resolve_s3_target(target, operation="rm")
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
            return

        # Enumerating paths: full recursive delete, or the keyless
        # non-recursive folder-marker sweep. Both list without Delimiter.
        # S3ScanOptions.prefix re-anchors the listing at the normalized prefix on
        # the passed storage itself (client shared), so a custom S3Storage
        # subclass and its scan_pages override survive.
        options = S3ScanOptions(
            recursive=True,
            page_size=page_size,
            request_payer=request_payer,
            prefix=root,
            filter=self._rm_scan_filter(filter, sweep=not recursive),
        )

        if dryrun:
            for info in storage.scan(options):
                _emit_result(on_result, info=info, storage=storage, outcome=OpOutcome.DRYRUN)
            return

        with S3Deleter(
            storage,
            request_payer=request_payer,
            on_result=on_result,
            operation="rm",
            capture_response=capture_response,
        ) as deleter:
            for info in storage.scan(options):
                deleter.submit(info)
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

        The scan stamps each entry's root-relative ``compare_key`` (the listing is
        anchored at the prefix), so a glob/user filter reads it directly. The
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
        ``root`` = :func:`rm_filter_root`, what a glob filter matches) is stamped
        on the hand-built ``FileInfo``.
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
        ``S3Storage``; a key part is ignored like :meth:`mb` (aws-cli rejects
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
        :class:`ValidationError`. ``method`` selects the signed operation -
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
        client-side parameter validation -> :class:`ValidationError`. A
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
