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
from contextlib import ExitStack
from typing import TYPE_CHECKING, Any, BinaryIO, Concatenate, Literal, ParamSpec, TypeVar

from typing_extensions import Unpack

from boto3_s3 import naming, requestparams
from boto3_s3.comparator import Comparator, DefaultCopyFilter, PairFilter, SyncPair
from boto3_s3.deleter import S3Deleter
from boto3_s3.exceptions import (
    BatchError,
    Boto3S3Error,
    CancelledError,
    NotFoundError,
    ValidationError,
)
from boto3_s3.localstorage import LocalStorage, to_native_path, walk_local
from boto3_s3.s3storage import S3Storage, s3_errors
from boto3_s3.storage import Location, Storage
from boto3_s3.transfer import TransferItem, Transferrer
from boto3_s3.types import (
    CancelToken,
    CaseConflictMode,
    FileFilter,
    FileInfo,
    OpKind,
    OpOutcome,
    OpResult,
    ProgressCallback,
    ResultCallback,
    S3FileInfo,
    ScanOptions,
    TransferOptions,
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

# S3's multipart ceiling: 10000 parts x 5 GiB. aws warns (without skipping)
# for larger uploads; the size string is aws-cli's rendered constant
# (human_readable_size(MAX_UPLOAD_SIZE)).
_MAX_UPLOAD_SIZE = 5 * 1024**3 * 10000
_MAX_UPLOAD_SIZE_TEXT = "48.8 TiB"

_GLACIER_STORAGE_CLASSES = ("GLACIER", "DEEP_ARCHIVE")

# Internal predicate shape for cp's filter: (info, compare_key) -> keep?
_CpKeep = Callable[[FileInfo, str], bool]


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
    key: str,
    outcome: OpOutcome,
    error: BaseException | None = None,
) -> None:
    if on_result is not None:
        on_result(OpResult(kind=OpKind.DELETE, key=key, outcome=outcome, error=error))


def _is_folder_marker(info: FileInfo) -> bool:
    """A zero-byte ``/``-terminated key - the manual-folder convention."""
    return info.size == 0 and info.key.endswith("/")


def _cp_text(storage: Storage, *, operation: str = "cp") -> str:
    """The textual form ``naming.plan_transfer`` resolves (aws-cli path shapes).

    An ``S3Storage`` reconstructs its ``s3://bucket/key`` (the keyless form
    stays slashless so the bucket-root normalization applies, exactly like a
    raw ``s3://bucket`` argument); a ``LocalStorage`` contributes its path as
    given - the trailing-separator rule reads the raw form. Other ``Storage``
    subclasses are not transferable yet.
    """
    if isinstance(storage, S3Storage):
        if storage.key:
            return f"s3://{storage.bucket}/{storage.key}"
        return f"s3://{storage.bucket}"
    if isinstance(storage, LocalStorage):
        return storage.path
    raise ValidationError(
        f"{operation} supports s3:// URIs, local paths, S3Storage, and LocalStorage "
        f"(got {type(storage).__name__})",
        operation=operation,
    )


def _cp_keep(item_filter: FileFilter | None) -> _CpKeep | None:
    """Adapt a ``FileFilter`` to the internal ``(info, compare_key)`` test.

    The scan computes each entry's compare key (its key relative to the
    operation root - the space ``TransferPlan.filter_root``-translated patterns
    live in); this stamps it onto ``info.compare_key`` before consulting the
    predicate, so a glob filter matches the root-relative key while a richer
    predicate still sees the full ``FileInfo``.
    """
    if item_filter is None:
        return None
    predicate = item_filter

    def keep(info: FileInfo, compare_key: str) -> bool:
        info.compare_key = compare_key
        return predicate(info)

    return keep


def _copy_all(_pair: SyncPair) -> bool:
    """``copy_filter=True``: copy every source-present pair (cp-like)."""
    return True


def _copy_none(_pair: SyncPair) -> bool:
    """``copy_filter=False``: copy nothing (scan-only / delete-only sync)."""
    return False


def _glacier_blocked(info: FileInfo, *, kind: OpKind, options: TransferOptions) -> bool:
    """Whether the aws-cli glacier gate skips this source object.

    GLACIER / DEEP_ARCHIVE sources block downloads and copies unless restored
    (``Restore`` carries ``ongoing-request="false"``) or forced. Only the
    single-object path has a HeadObject to read ``Restore`` from - a
    recursive listing has none, so restored objects still skip there
    (aws-cli-faithful; ``fileinfo.is_glacier_compatible``).
    """
    if options.get("force_glacier_transfer"):
        return False
    if not isinstance(info, S3FileInfo) or info.storage_class not in _GLACIER_STORAGE_CLASSES:
        return False
    restore = ""
    if info.head is not None:
        restore = str(info.head.get("Restore", ""))
    del kind  # download and copy both gate; upload never reaches here
    return 'ongoing-request="false"' not in restore


def _glacier_warning(src_display: str, kind: OpKind) -> str:
    """aws-cli's glacier skip message (``s3handler._warn_glacier``)."""
    op = kind.value
    return (
        f"Skipping file {src_display}. Object is of storage class GLACIER. "
        f"Unable to perform {op} operations on GLACIER objects. You must "
        "restore the object to be able to perform the operation. See aws "
        f"s3 {op} help for additional parameter options to ignore or force "
        "these transfers."
    )


def _as_binary_stream(value: object, attr: str) -> Any | None:
    """The value itself when it is a caller-supplied binary stream, else None.

    Anything that is not a path-like ``Location`` and quacks like a binary
    stream (``read`` for sources, ``write`` for destinations) is treated as
    one - how ``cp`` accepts stdin/stdout and arbitrary file-likes.
    """
    if isinstance(value, (str, os.PathLike, Storage)):
        return None
    return value if hasattr(value, attr) else None


class _CaseConflictGate:
    """The ``--case-conflict`` decision for recursive downloads.

    aws builds this from sync machinery (a destination listing paired by a
    comparator): a source file whose compare key exists at the destination
    with the **exact same case** always transfers (``AlwaysSync`` - cp
    overwrites); everything else runs the conflict check
    (``CaseConflictSync``): a conflict exists when another file with the
    same casefolded name was already admitted in this run, or the
    destination path exists (only possible on a case-insensitive
    filesystem, where a case-variant satisfies ``os.path.exists``). The
    messages are aws's verbatim, emitted as uncounted NOTICE records (aws
    prints them straight to stderr, bypassing its warned count).
    """

    _DOC_URI = "https://docs.aws.amazon.com/cli/latest/topic/s3-case-insensitivity.html"

    def __init__(
        self, mode: CaseConflictMode, dest_keys: set[str], *, operation: str = "cp"
    ) -> None:
        self._mode = mode
        self._dest_keys = dest_keys
        self._operation = operation
        self._submitted: set[str] = set()

    def blocks(self, item: TransferItem, transferrer: Transferrer) -> bool:
        if item.compare_key in self._dest_keys:
            return False  # exact-case match at the destination: always copy
        lowered = item.compare_key.lower()
        src = f"{item.src_bucket}/{item.src_key}"
        dest = os.path.abspath(item.dst_path or "")
        if lowered not in self._submitted and not os.path.exists(dest):
            self._submitted.add(lowered)
            return False
        if self._mode is CaseConflictMode.SKIP:
            transferrer.notice(
                f"warning: Skipping {src} -> {dest} because a file whose name "
                "differs only by case either exists or is being downloaded.",
                key=item.compare_key,
            )
            return True
        if self._mode is CaseConflictMode.WARN:
            transferrer.notice(
                f"warning: Downloading {src} -> {dest} despite a file whose "
                "name differs only by case either existing or being "
                "downloaded. This behavior is not defined on case-insensitive "
                "filesystems and may result in overwriting existing files or "
                "race conditions between concurrent downloads. For more "
                f"information, see {self._DOC_URI}.",
                key=item.compare_key,
            )
            self._submitted.add(lowered)
            return False
        raise Boto3S3Error(
            f"Failed to download {src} -> {dest} because a file whose name "
            "differs only by case either exists or is being downloaded.",
            operation=self._operation,
        )


class _SyncDeletes:
    """The deletion lane of one sync run (destination-only pairs).

    Wraps the two dispatch shapes behind one ``submit``: an S3 destination
    batches through :class:`S3Deleter` (the ``rm`` machinery - a wire-level
    deviation from aws-cli's per-key DeleteObject with the same end state),
    a local destination removes synchronously on the calling thread (aws-cli's
    ``LocalDeleteRequestSubmitter``). Dry runs emit DRYRUN records and touch
    nothing. The deleter is created lazily on the first S3 submit - a run
    with nothing to delete spawns no worker - and lands in the sync's
    ``ExitStack`` so an exception abandons the unflushed batch (aws-cli
    cancel behavior) while a clean exit flushes it.
    """

    def __init__(
        self,
        dst_s3: S3Storage | None,
        *,
        request_payer: str | None,
        dryrun: bool,
        on_result: ResultCallback | None,
    ) -> None:
        self._dst_s3 = dst_s3
        self._request_payer = request_payer
        self._dryrun = dryrun
        self._on_result = on_result
        self._stack: ExitStack | None = None
        self._deleter: S3Deleter | None = None
        self._local_succeeded = 0
        self._local_failed = 0
        self._local_first_error: BaseException | None = None

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
    def first_error(self) -> BaseException | None:
        if self._deleter is not None and self._deleter.first_error is not None:
            return self._deleter.first_error
        return self._local_first_error

    def submit(self, pair: SyncPair, plan: naming.TransferPlan) -> None:
        if self._dst_s3 is not None:
            key = plan.dst_root[len(self._dst_s3.bucket) + 1 :] + pair.key
            if self._dryrun:
                self._emit(key=key, outcome=OpOutcome.DRYRUN, src=self._display(key))
                return
            if self._deleter is None:
                assert self._stack is not None
                self._deleter = self._stack.enter_context(
                    S3Deleter(
                        self._dst_s3,
                        request_payer=self._request_payer,
                        on_result=self._relay,
                        operation="sync",
                    )
                )
            self._deleter.submit(key)
            return
        info = pair.dst
        assert info is not None
        native = to_native_path(info.key)
        if self._dryrun:
            self._emit(key=pair.key, outcome=OpOutcome.DRYRUN, src=native)
            return
        try:
            os.remove(native)
        except OSError as exc:
            self._local_failed += 1
            if self._local_first_error is None:
                self._local_first_error = exc
            self._emit(key=pair.key, outcome=OpOutcome.FAILED, src=native, error=exc)
            return
        self._local_succeeded += 1
        self._emit(key=pair.key, outcome=OpOutcome.SUCCEEDED, src=native)

    def _display(self, key: str) -> str:
        assert self._dst_s3 is not None
        return f"s3://{self._dst_s3.bucket}/{key}"

    def _relay(self, result: OpResult) -> None:
        # The deleter reports bare keys; re-emit with the rendered endpoint
        # so consumers print aws's `delete: s3://bucket/key` without
        # re-deriving it.
        self._emit(
            key=result.key,
            outcome=result.outcome,
            src=self._display(result.key),
            error=result.error,
        )

    def _emit(
        self,
        *,
        key: str,
        outcome: OpOutcome,
        src: str,
        error: BaseException | None = None,
    ) -> None:
        if self._on_result is not None:
            self._on_result(
                OpResult(kind=OpKind.DELETE, key=key, outcome=outcome, error=error, src=src)
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
    configured via :func:`boto3_s3.set_stream_logger`. The instance is safe to
    share across threads (immutable defaults plus one benign cache); building
    clients concurrently is bounded by boto3's own thread-safety, so for heavy
    parallelism reuse a prebuilt client via ``S3Storage(url, client=...)``.
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
        one explicitly via ``S3Storage(url, client=s3.client())``.
        """
        if self._session is not None:
            return self._session.client("s3", endpoint_url=self._endpoint_url, config=self._config)
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
        with no bucket - the default bare ``"s3://"`` - is the service root and
        yields one ``BUCKET``-kind entry per bucket (``mtime`` = creation date).
        ``bucket_name_prefix`` / ``bucket_region`` filter that bucket listing
        and are ignored for object listings; conversely ``recursive`` /
        ``request_payer`` are ignored at the service root (aws-cli parity). A
        non-S3 ``Location`` raises ``ValidationError``. The target is validated
        eagerly; iteration is lazy.
        """
        storage = self._resolve_s3_target(target, operation="ls")
        options = ScanOptions(
            recursive=recursive,
            page_size=page_size,
            request_payer=request_payer,
            bucket_name_prefix=bucket_name_prefix,
            bucket_region=bucket_region,
        )
        return storage.scan(options)

    def _resolve_s3_target(self, target: Location, *, operation: str) -> S3Storage:
        if isinstance(target, S3Storage):
            return target
        if not isinstance(target, str):
            raise ValidationError(
                f"{operation} accepts an 's3://...' URI string or an S3Storage",
                operation=operation,
            )
        # S3-only ops are lenient about the s3:// scheme (a bare "bucket/key", or
        # the "s3://" service root, both work); the client carries this S3's
        # defaults. Unlike resolve(), a non-s3 string is not a local fallback.
        return S3Storage(target, client=self.client())

    # -- byte transfer ----------------------------------------------------

    def cp(
        self,
        src: Location | BinaryIO,
        dst: Location | BinaryIO,
        *,
        recursive: bool = False,
        filter: FileFilter | None = None,
        follow_symlinks: bool = True,
        dryrun: bool = False,
        page_size: int = 1000,
        expected_size: int | None = None,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        transfer_config: TransferConfig | None = None,
        **options: Unpack[TransferOptions],
    ) -> None:
        """Copy bytes between ``src`` and ``dst`` with ``aws s3 cp`` semantics.

        The route follows the resolved pair: local->S3 upload, S3->local
        download, S3->S3 copy (local->local is rejected, like aws). Path
        shapes - what an existing-directory or trailing-slash destination
        means, which side's name wins, where ``--exclude`` / ``--include``
        patterns root - reproduce aws-cli's ``FileFormat`` rules
        (:mod:`boto3_s3.naming`); ``recursive`` is the aws-cli ``dir_op``.
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
        root (``TransferPlan.filter_root`` is the translation root for
        patterns) - a :class:`~boto3_s3.globsieve.GlobFilter` matches that key
        while a richer predicate can read size / mtime / storage_class.
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
        - raise directly: a missing local source raises the base
        ``Boto3S3Error`` with aws's wording (their pre-pipeline rc 255
        shape), not ``NotFoundError``.
        """
        if transfer_config is None:
            transfer_config = self._transfer_config
        src_stream = _as_binary_stream(src, "read")
        dst_stream = _as_binary_stream(dst, "write")
        if src_stream is not None or dst_stream is not None:
            self._cp_stream(
                src,
                dst,
                src_stream=src_stream,
                dst_stream=dst_stream,
                recursive=recursive,
                dryrun=dryrun,
                expected_size=expected_size,
                on_progress=on_progress,
                on_result=on_result,
                transfer_config=transfer_config,
                options=options,
            )
            return
        # The stream shapes were handled above; what remains is a Location.
        src_storage = self.resolve(src)  # type: ignore[arg-type]
        dst_storage = self.resolve(dst)  # type: ignore[arg-type]
        self._run_transfer(
            src_storage,
            dst_storage,
            operation="cp",
            is_move=False,
            recursive=recursive,
            item_filter=filter,
            follow_symlinks=follow_symlinks,
            dryrun=dryrun,
            page_size=page_size,
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            transfer_config=transfer_config,
            options=options,
        )

    def _run_transfer(
        self,
        src_storage: Storage,
        dst_storage: Storage,
        *,
        operation: str,
        is_move: bool,
        recursive: bool,
        item_filter: FileFilter | None,
        follow_symlinks: bool,
        dryrun: bool,
        page_size: int,
        on_progress: ProgressCallback | None,
        on_result: ResultCallback | None,
        cancel_token: CancelToken | None,
        transfer_config: TransferConfig | None,
        options: TransferOptions,
    ) -> None:
        """The shared cp/mv pipeline for parsed, non-stream locations.

        Route classification, the pre-batch checks, enumeration, the gates,
        and the submit loop - identical for both operations; ``mv`` differs
        only by what it validated beforehand and by ``is_move`` (the engine's
        delete-source + MOVE reporting).

        Built-in backends only: each route below asserts the concrete
        ``LocalStorage`` / ``S3Storage`` pair, because the engine reaches into
        ``S3Storage``'s client/bucket and ``LocalStorage``'s path directly
        (s3transfer). A custom ``Storage`` subclass cannot transfer yet - the
        generic ``open``-based path described in ``storage.py``'s module
        docstring is not wired. Enumeration / deletion (``ls`` / ``rm``) carry
        no such restriction.
        """
        plan = naming.plan_transfer(
            _cp_text(src_storage, operation=operation),
            _cp_text(dst_storage, operation=operation),
            recursive=recursive,
            operation=operation,
        )

        source_client = None
        src_s3: S3Storage | None = None
        dst_bucket = ""
        # Built-in backends only (see this method's docstring): the asserts pin
        # the concrete types the engine reaches into. Custom Storage transfer is
        # unimplemented - do not relax these without wiring an open()-based path
        # (storage.py) and implementing S3Storage.open.
        if plan.paths_type == "locals3":
            assert isinstance(src_storage, LocalStorage) and isinstance(dst_storage, S3Storage)
            # aws-cli's _validate_path_args: the raw user path, checked up front
            # (their bare RuntimeError -> rc 255; base category here, ditto).
            if not os.path.exists(src_storage.path):
                raise Boto3S3Error(
                    f"The user-provided path {src_storage.path} does not exist.",
                    operation=operation,
                )
            kind = OpKind.UPLOAD
            client = dst_storage.get_client()
            dst_bucket = dst_storage.bucket
        elif plan.paths_type == "s3local":
            assert isinstance(src_storage, S3Storage) and isinstance(dst_storage, LocalStorage)
            # aws-cli's _validate_path_args only creates the dest dir when it does
            # not already exist; check the raw user path (plan.dst_root carries a
            # trailing os.sep, so exists() is False for an existing *file*). An
            # existing-file dest then skips makedirs and fails per item like aws
            # (rc 1) instead of crashing up front; an empty listing transfers
            # nothing and exits 0.
            if recursive and not os.path.exists(dst_storage.path):
                try:
                    os.makedirs(plan.dst_root, exist_ok=True)
                except OSError as exc:
                    raise Boto3S3Error(str(exc), operation=operation) from exc
            kind = OpKind.DOWNLOAD
            client = src_storage.get_client()
            src_s3 = src_storage
        else:
            assert isinstance(src_storage, S3Storage) and isinstance(dst_storage, S3Storage)
            kind = OpKind.COPY
            client = dst_storage.get_client()
            source_client = src_storage.get_client()
            src_s3 = src_storage
            dst_bucket = dst_storage.bucket

        keep = _cp_keep(item_filter)
        case_gate = self._cp_case_gate(plan, kind=kind, recursive=recursive, options=options)
        transferrer = Transferrer(
            kind,
            client,
            source_client=source_client,
            transfer_config=transfer_config,
            options=options,
            operation=operation,
            is_move=is_move,
            on_progress=on_progress,
            on_result=on_result,
        )
        with transferrer:
            if src_s3 is None:
                items = self._cp_upload_items(
                    plan,
                    dst_bucket=dst_bucket,
                    transferrer=transferrer,
                    follow_symlinks=follow_symlinks,
                    keep=keep,
                )
            else:
                items = self._cp_s3_source_items(
                    plan,
                    src_s3,
                    kind=kind,
                    dst_bucket=dst_bucket,
                    transferrer=transferrer,
                    page_size=page_size,
                    keep=keep,
                    options=options,
                    case_gate=case_gate,
                    operation=operation,
                )
            for item in items:
                if cancel_token is not None and cancel_token.cancelled:
                    raise CancelledError(f"{operation} was cancelled", operation=operation)
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

    def _cp_upload_items(
        self,
        plan: naming.TransferPlan,
        *,
        dst_bucket: str,
        transferrer: Transferrer,
        follow_symlinks: bool,
        keep: _CpKeep | None,
    ) -> Iterator[TransferItem]:
        """Materialize upload items from the local walk (warnings -> rollup).

        No directory check on the single path, like aws: a directory source
        becomes an item whose open fails in flight ([Errno 21], rc 1).
        """
        infos = walk_local(
            plan.src_root,
            dir_op=plan.dir_op,
            follow_symlinks=follow_symlinks,
            on_warning=transferrer.warn,
        )
        for info in infos:
            if keep is not None:
                _dest, compare_key = naming.item_paths(plan, to_native_path(info.key))
                if not keep(info, compare_key):
                    continue
            yield self._upload_item_from_info(
                plan, info, dst_bucket=dst_bucket, transferrer=transferrer
            )

    def _upload_item_from_info(
        self,
        plan: naming.TransferPlan,
        info: FileInfo,
        *,
        dst_bucket: str,
        transferrer: Transferrer,
    ) -> TransferItem:
        """One upload item from a walk entry (the oversize warning included)."""
        native = to_native_path(info.key)
        dest, compare_key = naming.item_paths(plan, native)
        if info.size is not None and info.size > _MAX_UPLOAD_SIZE:
            # aws-cli's _warn_if_too_large: warn (rendered relative) but
            # still attempt, so S3's own EntityTooLarge stays visible.
            transferrer.warn(
                f"File {naming.relative_path(native)} exceeds s3 upload limit of "
                f"{_MAX_UPLOAD_SIZE_TEXT}.",
                key=compare_key,
            )
        return TransferItem(
            compare_key=compare_key,
            size=info.size,
            mtime=info.mtime,
            src_path=native,
            dst_bucket=dst_bucket,
            dst_key=dest[len(dst_bucket) + 1 :],
            src_display=native,
            dst_display=f"s3://{dest}",
        )

    def _cp_s3_source_items(
        self,
        plan: naming.TransferPlan,
        src_storage: S3Storage,
        *,
        kind: OpKind,
        dst_bucket: str,
        transferrer: Transferrer,
        page_size: int,
        keep: _CpKeep | None,
        options: TransferOptions,
        case_gate: _CaseConflictGate | None = None,
        operation: str = "cp",
    ) -> Iterator[TransferItem]:
        """Materialize download/copy items from an S3 source, gates applied."""
        bucket = src_storage.bucket
        if plan.dir_op:
            infos: Iterator[FileInfo] = self._cp_scan(
                src_storage,
                key_prefix=plan.src_root[len(bucket) + 1 :],
                page_size=page_size,
                keep=keep,
                options=options,
            )
        elif not src_storage.key:
            # Keyless non-recursive source (`cp s3://bucket .`): aws lists the
            # bucket and exact-matches nothing -> zero items, rc 0. Same
            # outcome here without issuing the listing.
            return
        else:
            infos = self._cp_head_single(
                src_storage, kind=kind, options=options, operation=operation
            )

        for info in infos:
            if kind is OpKind.DOWNLOAD:
                item = self._download_item_from_info(
                    plan,
                    info,
                    bucket=bucket,
                    transferrer=transferrer,
                    options=options,
                    case_gate=case_gate,
                )
            else:
                item = self._copy_item_from_info(
                    plan,
                    info,
                    bucket=bucket,
                    dst_bucket=dst_bucket,
                    transferrer=transferrer,
                    options=options,
                )
            if item is not None:
                yield item

    def _download_item_from_info(
        self,
        plan: naming.TransferPlan,
        info: FileInfo,
        *,
        bucket: str,
        transferrer: Transferrer,
        options: TransferOptions,
        case_gate: _CaseConflictGate | None,
    ) -> TransferItem | None:
        """One download item from a listing entry, or ``None`` once a gate
        consumed it (the gate emits its own warn/skip/notice record)."""
        src_path = f"{bucket}/{info.key}"
        dest, compare_key = naming.item_paths(plan, src_path)
        src_display = f"s3://{src_path}"
        is_s3_info = isinstance(info, S3FileInfo)
        item = TransferItem(
            compare_key=compare_key,
            size=info.size,
            etag=info.etag if is_s3_info else None,
            mtime=info.mtime,
            head=info.head if is_s3_info else None,
            src_bucket=bucket,
            src_key=info.key,
            dst_path=dest,
            src_display=src_display,
            dst_display=dest,
        )
        # The comparator-equivalent gate runs before the submitter
        # warning handlers (aws-cli instruction order).
        if case_gate is not None and case_gate.blocks(item, transferrer):
            return None
        if _glacier_blocked(info, kind=OpKind.DOWNLOAD, options=options):
            if options.get("ignore_glacier_warnings"):
                transferrer.skip(TransferItem(compare_key=compare_key, src_display=src_display))
            else:
                transferrer.warn(_glacier_warning(src_display, OpKind.DOWNLOAD), key=compare_key)
            return None
        if os.path.normpath(compare_key).startswith(".." + os.sep):
            transferrer.warn(
                f"Skipping file {compare_key}. File references a parent directory.",
                key=compare_key,
            )
            return None
        if options.get("no_overwrite") and os.path.exists(dest):
            # aws-cli's _warn_if_file_exists_with_no_overwrite: a silent
            # skip (debug-level only on the aws side; rc stays 0).
            transferrer.skip(item)
            return None
        return item

    def _copy_item_from_info(
        self,
        plan: naming.TransferPlan,
        info: FileInfo,
        *,
        bucket: str,
        dst_bucket: str,
        transferrer: Transferrer,
        options: TransferOptions,
    ) -> TransferItem | None:
        """One S3-to-S3 copy item from a listing entry, or ``None`` when the
        glacier gate consumed it."""
        src_path = f"{bucket}/{info.key}"
        dest, compare_key = naming.item_paths(plan, src_path)
        src_display = f"s3://{src_path}"
        is_s3_info = isinstance(info, S3FileInfo)
        if _glacier_blocked(info, kind=OpKind.COPY, options=options):
            if options.get("ignore_glacier_warnings"):
                transferrer.skip(TransferItem(compare_key=compare_key, src_display=src_display))
            else:
                transferrer.warn(_glacier_warning(src_display, OpKind.COPY), key=compare_key)
            return None
        return TransferItem(
            compare_key=compare_key,
            size=info.size,
            etag=info.etag if is_s3_info else None,
            mtime=info.mtime,
            head=info.head if is_s3_info else None,
            src_bucket=bucket,
            src_key=info.key,
            dst_bucket=dst_bucket,
            dst_key=dest[len(dst_bucket) + 1 :],
            src_display=src_display,
            dst_display=f"s3://{dest}",
        )

    def _cp_scan(
        self,
        storage: S3Storage,
        *,
        key_prefix: str,
        page_size: int,
        keep: _CpKeep | None,
        options: TransferOptions,
    ) -> Iterator[FileInfo]:
        """A recursive object listing anchored at the '/'-normalized ``key_prefix``.

        The shared transfer-side enumeration: cp/mv scan their source here,
        and sync scans whichever of its sides is S3 (the destination too).
        Folder markers never surface, and ``keep`` sees the prefix-relative
        key (the compare key).
        """
        bucket = storage.bucket
        list_storage = storage
        if key_prefix != storage.key:
            list_storage = S3Storage(f"s3://{bucket}/{key_prefix}", client=storage.get_client())

        def scan_filter(info: FileInfo) -> bool:
            # Zero-byte '/'-terminated "folder marker" objects never transfer
            # (aws-cli filegenerator); the user filter prunes what remains.
            if info.size == 0 and info.key.endswith("/"):
                return False
            if keep is None:
                return True
            return keep(info, info.key[len(key_prefix) :])

        scan_options = ScanOptions(
            recursive=True,
            page_size=page_size,
            request_payer=options.get("request_payer"),
            filter=scan_filter,
        )
        return list_storage.scan(scan_options)

    def _cp_head_single(
        self,
        src_storage: S3Storage,
        *,
        kind: OpKind,
        options: TransferOptions,
        operation: str = "cp",
    ) -> Iterator[FileInfo]:
        """Resolve a single S3 source by HeadObject (aws-cli's `_list_single_object`).

        Any 404 is rewritten to aws's ``Key "..." does not exist`` message; a
        copy source is headed with the copy-source SSE-C parameters.
        """
        key = src_storage.key
        if kind is OpKind.COPY:
            params = requestparams.map_head_object_params_with_copy_source_sse(options)
        else:
            params = requestparams.map_head_object_params(options)
        try:
            with s3_errors(operation=operation, bucket=src_storage.bucket, key=key):
                head = src_storage.get_client().head_object(
                    Bucket=src_storage.bucket, Key=key, **params
                )
        except NotFoundError as exc:
            raise NotFoundError(
                "An error occurred (404) when calling the HeadObject operation: "
                f'Key "{key}" does not exist',
                operation=operation,
                bucket=src_storage.bucket,
                key=key,
            ) from exc
        etag = head.get("ETag")
        yield S3FileInfo(
            key=key,
            size=head.get("ContentLength"),
            mtime=head.get("LastModified"),
            etag=etag.strip('"') if etag else None,
            storage_class=head.get("StorageClass"),
            head=head,
        )

    def _cp_stream(
        self,
        src: object,
        dst: object,
        *,
        src_stream: Any | None,
        dst_stream: Any | None,
        recursive: bool,
        dryrun: bool,
        expected_size: int | None,
        on_progress: ProgressCallback | None,
        on_result: ResultCallback | None,
        transfer_config: TransferConfig | None,
        options: TransferOptions,
    ) -> None:
        """One streaming transfer: a binary stream on exactly one side.

        The S3 side must resolve to an ``S3Storage`` whose key is taken
        verbatim (the CLI owns aws's ``-``-basename naming quirk). Uploads
        honor ``expected_size`` as the multipart sizing hint (without it the
        engine buffers up to the threshold and decides); downloads provide
        neither size nor etag, so s3transfer probes the object itself with a
        HeadObject - the aws stream wire shape. Streams are single items:
        gates (glacier, parent-ref) do not apply, exactly like aws's
        generator-less stream path; displays render as ``-``.
        """
        if recursive:
            raise ValidationError(
                "Streaming currently is only compatible with non-recursive cp commands",
                operation="cp",
            )
        if src_stream is not None and dst_stream is not None:
            raise ValidationError(
                "cp supports a stream on one side only (the other must be s3://)",
                operation="cp",
            )
        if src_stream is not None:
            storage = self._resolve_s3_target(dst, operation="cp")  # type: ignore[arg-type]
            kind = OpKind.UPLOAD
            item = TransferItem(
                compare_key=storage.key,
                size=expected_size,
                src_fileobj=src_stream,
                dst_bucket=storage.bucket,
                dst_key=storage.key,
                src_display="-",
                dst_display=f"s3://{storage.bucket}/{storage.key}",
            )
        else:
            storage = self._resolve_s3_target(src, operation="cp")  # type: ignore[arg-type]
            kind = OpKind.DOWNLOAD
            item = TransferItem(
                compare_key=storage.key,
                src_bucket=storage.bucket,
                src_key=storage.key,
                dst_fileobj=dst_stream,
                src_display=f"s3://{storage.bucket}/{storage.key}",
                dst_display="-",
            )
        transferrer = Transferrer(
            kind,
            storage.get_client(),
            transfer_config=transfer_config,
            options=options,
            operation="cp",
            on_progress=on_progress,
            on_result=on_result,
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

    def _cp_case_gate(
        self,
        plan: naming.TransferPlan,
        *,
        kind: OpKind,
        recursive: bool,
        options: TransferOptions,
    ) -> _CaseConflictGate | None:
        """Build the ``--case-conflict`` gate when it applies (aws-cli scope:
        recursive S3->local with a mode other than ``ignore``).

        The destination tree is enumerated up front into the exact-case
        membership set the gate's AlwaysSync arm consults - the observable
        equivalent of aws's reverse file generator + comparator.
        """
        mode = CaseConflictMode(options.get("case_conflict", CaseConflictMode.IGNORE))
        if kind is not OpKind.DOWNLOAD or not recursive or mode is CaseConflictMode.IGNORE:
            return None
        prefix = plan.dst_root.replace(os.sep, "/")
        dest_keys = {info.key[len(prefix) :] for info in walk_local(plan.dst_root, dir_op=True)}
        return _CaseConflictGate(mode, dest_keys)

    def mv(
        self,
        src: Location,
        dst: Location,
        *,
        recursive: bool = False,
        filter: FileFilter | None = None,
        follow_symlinks: bool = True,
        dryrun: bool = False,
        page_size: int = 1000,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        transfer_config: TransferConfig | None = None,
        **options: Unpack[TransferOptions],
    ) -> None:
        """Move bytes with ``aws s3 mv`` semantics: ``cp``, then delete the source.

        Everything :meth:`cp` documents - routes, path shapes, filters,
        gates, warnings, the ``BatchError`` aggregation - applies unchanged;
        the differences are mv's. Every result reports ``OpKind.MOVE``, and
        each item's source is deleted right after its transfer succeeds
        (``os.remove`` for uploads; one DeleteObject per object otherwise,
        ``request_payer`` forwarded). A failed, skipped, or dry-run item
        keeps its source; a deletion failure turns that item into the
        failure aws prints as ``move failed`` (the bytes already arrived).
        Filters prune both the transfer and the deletion. Streams are not a
        move source or destination (aws rejects ``-`` for mv; the CLI owns
        that exact error), and emptied local source directories are left
        behind like aws.

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
        dst_storage = self.resolve(dst)
        if isinstance(src_storage, S3Storage) and isinstance(dst_storage, S3Storage):
            src_text = naming.normalize_s3_uri(_cp_text(src_storage, operation="mv"))
            dst_text = naming.normalize_s3_uri(_cp_text(dst_storage, operation="mv"))
            if naming.same_path(src_text, dst_text):
                raise ValidationError(
                    f"Cannot mv a file onto itself: {src_text} - {dst_text}",
                    operation="mv",
                )
        self._run_transfer(
            src_storage,
            dst_storage,
            operation="mv",
            is_move=True,
            recursive=recursive,
            item_filter=filter,
            follow_symlinks=follow_symlinks,
            dryrun=dryrun,
            page_size=page_size,
            on_progress=on_progress,
            on_result=on_result,
            cancel_token=cancel_token,
            transfer_config=transfer_config,
            options=options,
        )

    def sync(
        self,
        src: Location,
        dst: Location,
        *,
        delete: bool | FileFilter = False,
        filter: FileFilter | None = None,
        copy_filter: bool | PairFilter | None = None,
        follow_symlinks: bool = True,
        dryrun: bool = False,
        page_size: int = 1000,
        on_progress: ProgressCallback | None = None,
        on_result: ResultCallback | None = None,
        cancel_token: CancelToken | None = None,
        transfer_config: TransferConfig | None = None,
        **options: Unpack[TransferOptions],
    ) -> None:
        """Recursively synchronize ``src`` into ``dst`` (``aws s3 sync``).

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
          cannot manufacture a phantom new/delete pair. Per-side or per-lane
          narrowing belongs in the pair layer below (``copy_filter`` /
          ``delete``).
        - **Pair decisions** - the surviving streams are merge-joined by
          compare key (:class:`~boto3_s3.comparator.Comparator`) and each
          :class:`~boto3_s3.comparator.SyncPair` is judged. ``copy_filter``
          decides source-present pairs: ``None`` (default) uses aws's stock
          judgment (:class:`~boto3_s3.comparator.DefaultCopyFilter`, which
          reads the direction from ``pair.kind``); ``True`` copies every
          source (cp-like); ``False`` copies nothing (scan-only, or a
          delete-only sync with ``delete``); any other
          :data:`~boto3_s3.comparator.PairFilter` is a custom decision. Tune
          or compose the default explicitly as ``DefaultCopyFilter(size_only=
          ..., exact_timestamps=..., no_overwrite=...)`` (e.g.
          ``any_of(DefaultCopyFilter(), ...)``). ``delete`` is the deletion
          lane in one value: ``False`` (default) deletes nothing, ``True``
          deletes every destination-only pair (aws ``--delete``), and a
          :data:`~boto3_s3.types.FileFilter` deletes only the orphans it
          keeps (matched against the orphan's ``FileInfo`` / compare key -
          the same shape as ``rm``'s ``filter``).

        Transfers run on the engine with cp's gates (glacier, the
        parent-directory guard, the ``--case-conflict`` gate for downloads -
        applied only to pairs missing at the destination, the aws-cli slot).
        Deletions are dispatched as they stream: batched ``DeleteObjects``
        for an S3 destination (the ``rm`` machinery; ``request_payer``
        forwarded), a synchronous ``os.remove`` for a local one. ``dryrun``
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
        dst_storage = self.resolve(dst)
        plan = naming.plan_transfer(
            _cp_text(src_storage, operation="sync"),
            _cp_text(dst_storage, operation="sync"),
            recursive=True,
            operation="sync",
        )

        source_client = None
        dst_bucket = ""
        if plan.paths_type == "locals3":
            assert isinstance(src_storage, LocalStorage) and isinstance(dst_storage, S3Storage)
            # aws-cli's _validate_path_args: the raw user path, checked up front.
            if not os.path.exists(src_storage.path):
                raise Boto3S3Error(
                    f"The user-provided path {src_storage.path} does not exist.",
                    operation="sync",
                )
            kind = OpKind.UPLOAD
            client = dst_storage.get_client()
            dst_bucket = dst_storage.bucket
        elif plan.paths_type == "s3local":
            assert isinstance(src_storage, S3Storage) and isinstance(dst_storage, LocalStorage)
            # aws-cli creates the destination directory during validation -
            # before any listing, so even an empty sync leaves it behind.
            # The bare exists() test (not exist_ok=True) is deliberate: a
            # destination that exists as a *file* passes here and fails per
            # item instead ([Errno 20], rc 1).
            if not os.path.exists(dst_storage.path):
                try:
                    os.makedirs(dst_storage.path)
                except OSError as exc:
                    raise Boto3S3Error(str(exc), operation="sync") from exc
            kind = OpKind.DOWNLOAD
            client = src_storage.get_client()
        else:
            assert isinstance(src_storage, S3Storage) and isinstance(dst_storage, S3Storage)
            kind = OpKind.COPY
            client = dst_storage.get_client()
            source_client = src_storage.get_client()
            dst_bucket = dst_storage.bucket

        if copy_filter is None:
            copy_filter = DefaultCopyFilter()
        elif copy_filter is True:
            copy_filter = _copy_all
        elif copy_filter is False:
            copy_filter = _copy_none
        keep = _cp_keep(filter)
        # delete is False/True (no per-orphan filter) or a FileFilter that
        # narrows which destination-only orphans are deleted (matched like rm).
        delete_keep = _cp_keep(delete) if not isinstance(delete, bool) else None
        case_gate = self._sync_case_gate(kind, options=options)

        transferrer = Transferrer(
            kind,
            client,
            source_client=source_client,
            transfer_config=transfer_config,
            options=options,
            operation="sync",
            on_progress=on_progress,
            on_result=on_result,
        )
        deletes = _SyncDeletes(
            dst_storage if isinstance(dst_storage, S3Storage) else None,
            request_payer=options.get("request_payer"),
            dryrun=dryrun,
            on_result=on_result,
        )
        with ExitStack() as stack:
            stack.enter_context(transferrer)
            deletes.open(stack)
            src_entries = self._sync_entries(
                src_storage,
                root=plan.src_root,
                keep=keep,
                transferrer=transferrer,
                follow_symlinks=follow_symlinks,
                page_size=page_size,
                options=options,
            )
            dst_entries = self._sync_entries(
                dst_storage,
                root=plan.dst_root,
                keep=keep,
                transferrer=transferrer,
                follow_symlinks=follow_symlinks,
                page_size=page_size,
                options=options,
            )
            for pair in Comparator(kind).compare(src_entries, dst_entries):
                if cancel_token is not None and cancel_token.cancelled:
                    raise CancelledError("sync was cancelled", operation="sync")
                if pair.src is not None:
                    if not copy_filter(pair):
                        continue
                    item = self._sync_transfer_item(
                        plan,
                        pair,
                        kind=kind,
                        src_bucket=src_storage.bucket if isinstance(src_storage, S3Storage) else "",
                        dst_bucket=dst_bucket,
                        transferrer=transferrer,
                        options=options,
                        case_gate=case_gate,
                    )
                    if item is None:
                        continue
                    if dryrun:
                        transferrer.dryrun(item)
                    else:
                        transferrer.submit(item)
                elif delete:  # on when delete is True or a FileFilter
                    if delete_keep is not None:
                        assert pair.dst is not None
                        if not delete_keep(pair.dst, pair.key):
                            continue
                    deletes.submit(pair, plan)

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

    def _sync_case_gate(
        self, kind: OpKind, *, options: TransferOptions
    ) -> _CaseConflictGate | None:
        """The ``--case-conflict`` gate for sync downloads.

        Unlike cp's (which pre-lists the destination for its exact-case
        AlwaysSync arm), sync already pairs against the destination listing:
        the gate is consulted only for pairs *missing* there, so the
        membership set is vacuously empty and only the submitted-set /
        ``os.path.exists`` conflict check remains - aws-cli's
        ``CaseConflictSync`` in the ``file_not_at_dest`` slot.
        """
        mode = CaseConflictMode(options.get("case_conflict", CaseConflictMode.IGNORE))
        if kind is not OpKind.DOWNLOAD or mode is CaseConflictMode.IGNORE:
            return None
        return _CaseConflictGate(mode, set(), operation="sync")

    def _sync_entries(
        self,
        storage: Storage,
        *,
        root: str,
        keep: _CpKeep | None,
        transferrer: Transferrer,
        follow_symlinks: bool,
        page_size: int,
        options: TransferOptions,
    ) -> Iterator[tuple[str, FileInfo]]:
        """One side's ``(compare_key, info)`` stream, visibility applied.

        A local side walks in aws-cli byte order with its warnings routed to
        the transfer rollup (both sides warn, aws parity); an S3 side is the
        shared anchored listing (folder markers dropped, ``request_payer`` /
        ``page_size`` forwarded - aws-cli maps them onto the destination
        listing too).
        """
        if isinstance(storage, S3Storage):
            key_prefix = root[len(storage.bucket) + 1 :]
            for info in self._cp_scan(
                storage, key_prefix=key_prefix, page_size=page_size, keep=keep, options=options
            ):
                yield info.key[len(key_prefix) :], info
            return
        prefix = root.replace(os.sep, "/")
        for info in walk_local(
            root, dir_op=True, follow_symlinks=follow_symlinks, on_warning=transferrer.warn
        ):
            compare_key = info.key[len(prefix) :]
            if keep is not None and not keep(info, compare_key):
                continue
            yield compare_key, info

    def _sync_transfer_item(
        self,
        plan: naming.TransferPlan,
        pair: SyncPair,
        *,
        kind: OpKind,
        src_bucket: str,
        dst_bucket: str,
        transferrer: Transferrer,
        options: TransferOptions,
        case_gate: _CaseConflictGate | None,
    ) -> TransferItem | None:
        """Build the transfer item for a copy-judged pair, cp's gates applied."""
        info = pair.src
        assert info is not None
        if kind is OpKind.UPLOAD:
            return self._upload_item_from_info(
                plan, info, dst_bucket=dst_bucket, transferrer=transferrer
            )
        if kind is OpKind.DOWNLOAD:
            # The case-conflict gate guards only pairs missing at the
            # destination (the aws-cli strategy slot); an exact-key update
            # never conflicts.
            gate = case_gate if pair.dst is None else None
            return self._download_item_from_info(
                plan,
                info,
                bucket=src_bucket,
                transferrer=transferrer,
                options=options,
                case_gate=gate,
            )
        return self._copy_item_from_info(
            plan,
            info,
            bucket=src_bucket,
            dst_bucket=dst_bucket,
            transferrer=transferrer,
            options=options,
        )

    # -- deletion ---------------------------------------------------------

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
            # aws-cli sends Bucket="" to the API and fails botocore's
            # client-side validation (general error, not usage error). rm has
            # no bucket-listing mode, so reject instead of letting scan's
            # service-root branch list buckets.
            raise ValidationError('Invalid bucket name "": rm requires a bucket', operation="rm")
        root = rm_filter_root(storage.key, recursive=recursive)
        decide = self._rm_decider(filter, strip=len(root))

        if not recursive and storage.key:
            self._rm_single(
                storage,
                decide,
                dryrun=dryrun,
                request_payer=request_payer,
                on_result=on_result,
            )
            return

        # Enumerating paths: full recursive delete, or the keyless
        # non-recursive folder-marker sweep. Both list without Delimiter.
        list_storage = storage
        if root != storage.key:
            # Re-anchor the listing at the normalized prefix; the client is
            # shared (and still owned by the original storage).
            list_storage = S3Storage(f"s3://{storage.bucket}/{root}", client=storage.get_client())
        options = ScanOptions(
            recursive=True,
            page_size=page_size,
            request_payer=request_payer,
            filter=self._rm_scan_filter(decide, sweep=not recursive),
        )

        if dryrun:
            for info in list_storage.scan(options):
                _emit_result(on_result, key=info.key, outcome=OpOutcome.DRYRUN)
            return

        with S3Deleter(
            storage, request_payer=request_payer, on_result=on_result, operation="rm"
        ) as deleter:
            for info in list_storage.scan(options):
                deleter.submit(info.key)
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
    def _rm_decider(
        item_filter: FileFilter | None, *, strip: int
    ) -> Callable[[FileInfo], bool] | None:
        """Wrap a ``FileFilter`` to stamp the compare key before each test.

        ``None`` means "keep everything" - kept as ``None`` (not a tautology
        lambda) so the unfiltered paths pay no per-object predicate call. Every
        candidate key starts with the root (the recursive listing uses it as
        Prefix; a single key starts with its own parent), so a plain slice
        yields the root-relative ``compare_key`` a glob filter matches against.
        """
        if item_filter is None:
            return None
        predicate = item_filter

        def decide(info: FileInfo) -> bool:
            info.compare_key = info.key[strip:]
            return predicate(info)

        return decide

    @staticmethod
    def _rm_scan_filter(
        decide: Callable[[FileInfo], bool] | None, *, sweep: bool
    ) -> Callable[[FileInfo], bool] | None:
        """The ``ScanOptions.filter`` for rm's enumerating paths.

        Composes the keyless-sweep marker test with ``decide``, marker first:
        the sweep selects only folder markers, and the user filter then prunes
        those candidates (aws-cli order). ``None`` when there is nothing to
        test. Evaluated page by page on the scan prefetch worker.
        """
        if not sweep:
            return decide
        if decide is None:
            return _is_folder_marker
        keep = decide
        return lambda info: _is_folder_marker(info) and keep(info)

    @staticmethod
    def _rm_single(
        storage: S3Storage,
        decide: Callable[[FileInfo], bool] | None,
        *,
        dryrun: bool,
        request_payer: str | None,
        on_result: ResultCallback | None,
    ) -> None:
        """The blind single-key path (no listing; aws ``_list_single_object``)."""
        key = storage.key
        if decide is not None and not decide(S3FileInfo(key=key)):
            return
        if dryrun:
            _emit_result(on_result, key=key, outcome=OpOutcome.DRYRUN)
            return
        try:
            storage.delete(key, request_payer=request_payer)
        except Boto3S3Error as exc:
            # The single key is still one batch item (aws counts it as a task
            # failure -> "delete failed:" + rc 1), so aggregate rather than
            # re-raising the category error.
            _emit_result(on_result, key=key, outcome=OpOutcome.FAILED, error=exc)
            raise BatchError(
                "1 of 1 deletes failed",
                succeeded=0,
                failed=1,
                warned=0,
                skipped=0,
                operation="rm",
            ) from exc
        _emit_result(on_result, key=key, outcome=OpOutcome.SUCCEEDED)

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
