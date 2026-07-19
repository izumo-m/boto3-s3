"""Core typed value objects shared across boto3-s3 operations."""

from __future__ import annotations

import enum
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from typing_extensions import TypedDict

if TYPE_CHECKING:
    from boto3_s3.exceptions import Boto3S3Error
    from boto3_s3.storage import Storage


class FileKind(enum.Enum):
    """What a listing entry is. Backends map their native kinds onto these.

    ``FILE`` is an S3 object or a non-directory local entry. Under a local
    complete-entry enumeration it can therefore be a symlink, FIFO, socket, or
    device and does not by itself guarantee transferable content; inspect
    ``LocalFileInfo.stat_result.st_mode`` for the precise native kind.
    ``DIRECTORY`` is an S3 common-prefix grouping or a local directory;
    ``size`` / ``mtime`` may be ``None``. ``BUCKET`` is a top-level S3 bucket.
    """

    FILE = "file"
    DIRECTORY = "directory"
    BUCKET = "bucket"


@dataclass(slots=True, kw_only=True)
class FileInfo:
    """Cross-backend entry produced by `Storage` or delivered by `S3.ls`.

    One uniform type for every entry a backend can list: files, directories /
    prefixes, and buckets are distinguished by ``kind``, not by separate
    classes - so a backend never widens ``scan``'s return type as it gains kinds.
    Backend-specific detail lives on subclasses (``S3FileInfo`` / ``LocalFileInfo``);
    local-only notions such as symlinks are attributes of ``LocalFileInfo`` rather
    than distinct types.

    ``key`` is the full, ``/``-separated identifier: an S3 object key, prefix, or
    bucket name, or - for a ``LocalFileInfo`` - the **absolute filesystem path**
    (``os.sep`` normalized to ``/``; ``to_native_path`` inverts it). It is never
    a relative path and never platform ``os.sep``. It doubles as the
    cross-backend merge/sort key: two ``Storage.scan`` streams
    ordered by ``key`` can be merge-joined (the basis of ``sync``) once each side
    is expressed as ``compare_key``, so every backend must emit ``/``-separated
    keys regardless of host OS. ``size`` (bytes) and ``mtime`` (tz-aware UTC) are
    populated for ``FILE`` entries and may be ``None`` for directories /
    prefixes; ``BUCKET`` entries carry the bucket's ``CreationDate`` as ``mtime``
    (``size`` stays ``None``). Producers enforce these invariants - the field
    types alone do not.

    ``compare_key`` is the relative form of the entry's key: for a local scan,
    the key relative to the directory being enumerated; for an S3 object listing,
    the entry key with the ``ListObjectsV2`` ``Prefix`` removed. A bucket listing
    uses the bucket name unchanged. It is the ``--exclude`` / ``--include``
    matching space and the axis ``sync`` merge-joins on (two
    ``scan`` sides expressed as ``compare_key`` share one key space, while ``key``
    stays the full identifier the transfer / delete uses and differs per side).
    Each backend's ``scan_pages`` stamps it on every entry it
    yields (a contract of the backend's listing, not of the base ``Storage.scan``),
    so a custom ``ScanOptions.filter`` predicate - notably ``GlobFilter`` - matches
    it directly without re-deriving it from ``key``. The built-in
    ``get_fileinfo`` methods use the basename for a single entry. The name mirrors
    aws-cli's ``FileInfo.compare_key``; a hand-built ``FileInfo`` may leave it as
    ``None``.

    ``storage`` is the backend the entry was listed from - the ``Storage`` whose
    ``scan`` (or single-key path) produced it - stamped by the producer during the
    scan, before any filter runs (aws-cli carries the analogous ``client`` /
    ``source_client`` on its own ``FileInfo``). It lets a ``ScanOptions.filter``
    predicate reach the
    backend behind the entry (e.g. a ``HeadObject`` for a tag the listing omits),
    and it is the handle ``sync`` reads through ``SyncPair.src`` /
    ``SyncPair.dest`` to open a pair's non-S3 side for a
    content compare (so ``pair.src.storage`` replaces a separately threaded
    backend). ``None`` on a hand-built ``FileInfo``; ``Storage.scan`` sets it
    as a safety net for a backend whose ``scan_pages`` did not.
    """

    key: str
    kind: FileKind = FileKind.FILE
    size: int | None = None
    mtime: datetime | None = None
    compare_key: str | None = None
    storage: Storage | None = None


@dataclass(slots=True, kw_only=True)
class LocalFileInfo(FileInfo):
    """``FileInfo`` for a local filesystem entry.

    Its ``key`` is the **absolute** path with ``os.sep`` normalized to ``/``
    (``LocalStorage`` anchors every scan at its absolutized ``path``); ``to_native_path``
    turns it back into a host path for I/O. ``key`` keeps the path *as walked* -
    a symlinked directory or file stays under its link name, never resolved to
    the target (``follow_symlinks=True`` only follows to descend / stat, matching
    aws-cli).

    ``stat_result`` is the exact stat snapshot the local producer used to classify
    the entry: normally the followed ``os.stat``; the entry's own lstat when
    ``follow_symlinks=False`` under complete enumeration; or an lstat fallback
    when following a link failed. It is a plain immutable value a ``filter`` or
    ``on_result`` callback can read
    (``st_mode`` / ``st_uid`` / ``st_size`` / ... - ``size`` and ``mtime`` are
    derived from it) without re-stat'ing. Because it is a value, not an
    ``os.DirEntry`` handle, the walk keeps its ``dir_fd`` fast path even while
    populating it. ``is_symlink`` records whether the entry itself was a symbolic
    link (from the walk's ``d_type`` / cached ``lstat`` - free); with
    ``follow_symlinks=True`` a link to a file still surfaces here as
    ``is_symlink=True`` while its ``stat_result`` describes the target.
    Every ``LocalFileInfo`` produced by ``LocalStorage`` carries both fields; a
    value built by hand may leave ``stat_result`` at ``None``.
    """

    stat_result: os.stat_result | None = None
    is_symlink: bool = False


@dataclass(slots=True, kw_only=True)
class S3FileInfo(FileInfo):
    """``FileInfo`` enriched with fields derived from an S3 object listing.

    ``etag`` is the dequoted ETag (surrounding ``"`` stripped) when populated.
    ``storage_class`` is the object's storage class from the listing
    (``ListObjectsV2``'s ``StorageClass``), consulted by the aws-cli glacier gate
    to skip ``GLACIER`` / ``DEEP_ARCHIVE`` sources on ``cp`` / ``mv`` / ``sync``
    unless forced or restored.
    ``owner`` is the S3 canonical user ID (``Owner["ID"]``), present only when
    the listing was made with ``fetch_owner=True`` - ``ListObjectsV2`` omits the
    owner otherwise, and ``FetchOwner`` adds per-page latency. ``DisplayName`` is
    deliberately not used (S3 returns it only in us-east-1). ``head`` is a cache
    slot for a ``HeadObject`` response payload (or any partial dict with the same
    keys), letting a custom enumerator or pre-fetch hook skip per-object HEAD
    round-trips. All four fields are optional because aws-cli tolerates their
    absence (parity).
    """

    etag: str | None = None
    storage_class: str | None = None
    owner: str | None = None
    head: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanOptions:
    """Backend-agnostic ``Storage.scan`` / ``Storage.scan_pages`` knobs, one value.

    Carrying a single immutable object - rather than re-threading keyword
    arguments through ``scan`` -> ``scan_pages`` and every override - keeps the
    enumeration seam tidy. This base holds only the knobs **every** backend
    honors; backend-specific knobs live on subclasses (``S3ScanOptions`` /
    ``LocalScanOptions``) so one backend's options never leak into another's
    code. A backend builds its own subclass via
    ``Storage.default_scan_options`` and the built-ins
    reject a foreign options type, so ``sync`` builds one subclass per side (an
    S3 side gets ``S3ScanOptions``, a local side ``LocalScanOptions``).
    A custom backend uses this base plus its own instance state, or defines its
    own ``ScanOptions`` subclass. (Bucket *listing* - the S3 service root - is a
    separate operation with its own params, ``S3Storage.list_buckets`` / ``S3.ls``,
    not a ``scan`` knob: ``scan`` enumerates openable entities.)

    ``filter`` is a per-entry predicate (``True`` keeps the entry) applied by
    each ``Storage.scan_pages`` producer, which returns already-filtered pages
    (``scan`` flattens + prefetches them, re-sieving only as a safety net for a
    backend that does not declare ``scan_pages_filters``). A backend may
    push the predicate to its source (a REST listing filtering server-side) or
    wrap its raw pages with ``storage.sieve_pages``; the built-ins run it on the
    prefetch worker, page by page. It carries the item filter of ``rm`` / ``cp``
    / ``mv``, and sync's *visibility* layer: each side's listing is pruned
    independently (its own ``--exclude`` / ``--include`` matching space) before the
    comparator pairs the streams - which is exactly why filtered-out destination
    entries are protected from ``--delete`` (an entry a producer does not return
    is simply absent, so it is not a delete candidate). Keep the predicate
    thread-safe and fast (it runs on the worker thread).

    ``sort`` requests entries in UTF-8 byte order of their ``compare_key``:
    ``sync`` sets it (its merge-join needs both sides ascending), while ``cp`` /
    ``mv`` / ``ls`` / ``rm`` leave it ``False`` (order is immaterial - each entry
    transfers / lists / deletes independently). A backend declaring
    ``StorageCapability.SORTABLE_SCAN`` MUST honor
    ``sort=True``; the built-ins ignore the flag and always sort (S3's listing is
    byte-ordered, the local walk sorts for aws parity), so it costs nothing for
    them. A custom backend whose sort is expensive may stream natural order when
    ``sort=False`` and pay the sort only for ``sync``.

    ``on_warning`` receives the aws-cli-worded skip messages (a broken symlink,
    an unreadable or special file) a backend's enumeration emits; the transfer
    rolls them up (see ``on_warning`` wiring in producers). Common because any
    backend may warn - without it those entries are dropped silently. Like
    ``filter`` it runs on the enumeration worker, not the calling thread; and
    because ``sync`` walks both sides through one shared sink, the two side-walks
    can invoke it concurrently - keep it thread-safe.
    """

    recursive: bool = False
    sort: bool = False
    filter: Callable[[FileInfo], bool] | None = None
    on_warning: Callable[[str], None] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class S3ScanOptions(ScanOptions):
    """S3 ``ListObjectsV2`` knobs - the S3 backend's ``ScanOptions``.

    ``page_size`` / ``request_payer`` / ``fetch_owner`` are not validated here:
    like aws-cli, they pass through to the service, which decides (``page_size=0``
    lists nothing, negative values fail with ``InvalidArgument`` - the exit-code
    charter requires reproducing both). The default ``None`` sends no
    ``MaxKeys`` at all, exactly like aws-cli's unset ``--page-size`` (the
    server pages at its own default, 1000). ``fetch_owner`` sends
    ``FetchOwner=True`` to populate ``S3FileInfo.owner``.

    ``prefix`` overrides the listing anchor: the backend lists under it (as the
    ``Prefix``, relativizing ``compare_key`` to it) instead of the storage's own
    key. A transfer sets it when its normalized listing prefix differs from the
    raw source key (a recursive ``cp`` / ``mv`` / ``sync`` / ``rm`` source, where
    the plan appends a trailing ``/``), so the *passed* storage instance is
    scanned rather than rebuilt from a URI - a custom ``S3Storage`` subclass (and
    its ``scan_pages`` override) survives. ``None`` uses the storage's key.
    """

    page_size: int | None = None
    request_payer: str | None = None
    fetch_owner: bool = False
    prefix: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalScanOptions(ScanOptions):
    """Local-walk knobs - the ``LocalStorage`` backend's ``ScanOptions``.

    ``enumerate_all_entries`` selects the candidate enumeration policy before
    ``ScanOptions.filter`` runs. ``False`` (default) is the aws-cli-compatible
    transfer view: recursively list transferable files, apply the special-file /
    readability battery, and skip no-follow symlinks. ``True`` is the complete
    filesystem-entry view: include the root, directories, symlinks, special
    files, and entries whose metadata is readable even when their content is not.
    A filter can still remove any candidate; filtering a directory record is too
    late to prune its children, while ``LocalFileGenerator.finalize_children`` can
    prune before descent. High-level operations preserve this source setting, so
    a caller that enables it is responsible for filtering out entries an intended
    transfer cannot consume.

    ``follow_symlinks`` selects the interpretation of a symlink. In the complete
    view, ``False`` returns the link itself as an lstat-based leaf, including a
    dangling link; ``True`` returns or descends its target as one entry at that
    key. If the followed stat fails, the complete view warns and falls back to
    the link's lstat. In the transfer view, ``False`` skips the link and ``True``
    retains the aws-cli-compatible followed behavior.

    ``detect_symlink_loops`` (default ``False``, a library extension -
    ``aws s3`` has none, so off keeps parity) guards the recursive walk
    against symlink cycles: with it (and ``follow_symlinks``) a directory that
    resolves to one of its own ancestors is skipped with a ``Symbolic link
    loop detected`` warning (via ``on_warning``) at the first re-entry, instead
    of descending ~SYMLOOP_MAX levels until the kernel's ELOOP ends the walk with
    a misleading ``File does not exist.`` warning (which is ``aws s3``'s
    behavior). Off is zero extra cost (no per-directory ``stat``).

    ``storage`` is not a user knob: ``LocalStorage`` threads its own instance here
    (``scan_pages`` / ``walk_local``) so the *shared, stateless*
    ``LocalFileGenerator`` can stamp each entry's
    ``FileInfo.storage`` before the visibility filter runs, without holding a
    back-reference to any one storage (which would misfire when one walker is
    shared across storages). ``None`` on a hand-built options object -
    ``Storage.scan``'s backstop fills ``FileInfo.storage`` afterwards instead.
    """

    follow_symlinks: bool = True
    detect_symlink_loops: bool = False
    enumerate_all_entries: bool = False
    storage: Storage | None = None


class TransferType(enum.Enum):
    """Kind of byte-moving operation a result/progress record describes.

    ``MOVE`` is a reporting kind only: an ``mv`` run still routes each item
    as an upload/download/copy internally (the aws-cli ``operation_name``),
    but every record it emits says ``move`` - exactly aws-cli's
    ``transfer_type='move'`` relabeling.
    """

    UPLOAD = "upload"
    DOWNLOAD = "download"
    COPY = "copy"
    MOVE = "move"
    DELETE = "delete"


class OpOutcome(enum.Enum):
    """Per-item outcome. ``FAILED`` maps to CLI exit code 1, ``WARNED`` to 2.

    ``SKIPPED`` is an informational, non-warning skip (e.g. sync up-to-date,
    a ``no_overwrite`` rejection) and does not affect the exit code.
    ``DRYRUN`` reports an item a dry run *would* have acted on - no API call
    was made and the exit code is unaffected. ``NOTICE`` carries display-only
    text on ``error`` (aws prints some advisories - the case-conflict
    messages - straight to stderr without counting them as warnings); it
    never affects counts or the exit code, and may precede the same item's
    real outcome. ``CANCELLED`` reports an accepted item revoked before it
    could complete - a fatal error elsewhere in the run, an immediate cancel,
    or Ctrl-C shut the engine down; its ``error`` is a `CancelledError`
    naming the cause. It is distinct from ``FAILED``: nothing is wrong with
    the item itself, and the run's outcome is carried by the fatal /
    `CancelledError` the operation raises, not by these records (aws-cli
    surfaces only the one fatal line and drops its cancelled items from
    output and counts entirely).
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WARNED = "warned"
    SKIPPED = "skipped"
    DRYRUN = "dryrun"
    NOTICE = "notice"
    CANCELLED = "cancelled"


class CaseConflictMode(enum.Enum):
    """Policy for case-fold key collisions when writing to a local filesystem."""

    IGNORE = "ignore"
    SKIP = "skip"
    WARN = "warn"
    ERROR = "error"


class CopyPropsMode(enum.Enum):
    """Which source object properties to propagate on an S3-to-S3 copy.

    `ALL` additionally carries S3 object annotations and needs an SDK with the
    annotations model - botocore >= 1.43.31 and s3transfer >= 0.19; on an older
    SDK the transfer engine refuses it up front with a `ConfigurationError`
    (every other mode degrades silently there, docs/transfer.md section 4).
    Multipart annotation staging is selected separately by
    `AnnotationCopyMode`.
    """

    NONE = "none"
    METADATA_DIRECTIVE = "metadata-directive"
    DEFAULT = "default"
    ALL = "all"


class AnnotationCopyMode(enum.Enum):
    """How multipart `copy_props=all` stages source annotation payloads.

    `PRELOAD_MEMORY` matches aws-cli by reading every payload into memory
    before the multipart copy starts. `PRELOAD_TEMPFILE` preserves that
    failure timing while storing payloads in a temporary file instead.
    `DEFERRED` uses s3transfer's native post-copy reads, reducing pre-copy
    resource use but potentially leaving the destination when a read fails.
    """

    PRELOAD_MEMORY = "preload-memory"
    PRELOAD_TEMPFILE = "preload-tempfile"
    DEFERRED = "deferred"


@dataclass(slots=True, kw_only=True)
class TransferProgress:
    """Byte-level progress for one in-flight item, passed to ``on_progress``."""

    transfer_type: TransferType
    compare_key: str
    bytes_done: int
    bytes_total: int | None = None


@dataclass(slots=True, kw_only=True)
class OpResult:
    """Per-item completion record passed to the ``on_result`` callback.

    One record per item, emitted by ``cp`` / ``mv`` / ``rm`` / ``sync`` from a
    worker thread (keep the callback fast and non-raising). A single type keyed
    by ``transfer_type`` (the verb). The ``src_*`` trio describes the object
    acted on (a transfer's source, or a delete's removed object); the ``dest_*``
    trio the destination side. The fields, the ``src`` / ``dest`` convention, and
    which operation populates which field are documented in docs/opresult.md.

    ``dest_info`` is ``None`` for ``cp`` / ``mv``, which never list the
    destination; only ``sync`` populates it (the pre-existing object the copy
    compared against), while ``dest_storage`` still carries the destination
    backend in that case.
    """

    transfer_type: TransferType
    compare_key: str
    outcome: OpOutcome
    bytes_transferred: int = 0
    error: Boto3S3Error | None = None
    src: str | None = None
    dest: str | None = None
    src_info: FileInfo | None = None
    dest_info: FileInfo | None = None
    src_storage: Storage | None = None
    dest_storage: Storage | None = None
    extra_info: Mapping[str, Any] | None = None


class CancelMode(enum.Enum):
    """How an operation shuts down after a `CancelToken` request.

    `GRACEFUL` stops accepting new work and drains work already accepted by
    the operation. `IMMEDIATE` additionally asks each engine to cancel pending
    and in-flight work where its implementation supports cancellation; external
    I/O already running may still have to finish.
    """

    GRACEFUL = "graceful"
    IMMEDIATE = "immediate"


class CancelToken:
    """Thread-safe, monotonically escalating cancellation request.

    `cancel()` may be called from any thread or a signal handler. The default
    is graceful cancellation; a later `IMMEDIATE` request upgrades it, while a
    later graceful request never downgrades it. The operation observes the
    effective `mode`, performs that shutdown policy, and raises
    `CancelledError` after reclaiming its resources.
    """

    def __init__(self) -> None:
        # RLock, not Lock: cancel() is signal-handler-safe by contract, and a
        # CPython signal handler runs on the main thread - it must be able to
        # re-enter while that same thread holds the lock inside mode/cancel.
        # The monotonic check-then-set stays correct under such re-entry
        # because every write branch re-reads _mode. The cancelled flag is a
        # plain bool, not a threading.Event: Event.set() takes the Event's own
        # non-reentrant Condition lock, so a nested signal-handler cancel()
        # landing inside an outer set() would deadlock the main thread - and
        # nothing ever wait()s on the flag, so the Event bought nothing.
        self._lock = threading.RLock()
        self._mode: CancelMode | None = None
        self._cancelled = False

    def cancel(self, *, mode: CancelMode = CancelMode.GRACEFUL) -> None:
        with self._lock:
            if self._mode is CancelMode.IMMEDIATE:
                return
            if self._mode is None or mode is CancelMode.IMMEDIATE:
                self._mode = mode
                self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def mode(self) -> CancelMode | None:
        with self._lock:
            return self._mode


class TransferOptions(TypedDict, total=False):
    """Object-shaping / S3-request options shared by ``cp`` / ``mv`` / ``sync``.

    Names are the snake_case form of the corresponding ``aws s3`` options; the
    library translates them to S3 API PascalCase internally. Options that do not
    apply to a given transfer direction are ignored (aws-cli parity). An
    *unknown* key, by contrast, is rejected eagerly by ``cp`` / ``mv`` /
    ``sync`` (``ValidationError``): a typo'd option never passes silently.

    ``sse_c_key`` is a ``str`` or raw ``bytes``, like botocore's
    ``SSECustomerKey``: ``aws`` passes the CLI string through verbatim (only
    ``fileb://`` loads bytes). ``no_overwrite`` is the conditional write
    (``--no-overwrite``): ``IfNoneMatch="*"`` on uploads / copies (the server's
    ``PreconditionFailed`` becomes a silent skip), and an existence check before
    downloads. `annotation_copy_mode` affects only multipart S3-to-S3 copies
    under `copy_props=ALL`; it defaults to `PRELOAD_MEMORY` for aws-cli parity.
    """

    acl: str
    grants: Sequence[str]
    storage_class: str
    sse: str
    sse_kms_key_id: str
    sse_c: str
    sse_c_key: str | bytes
    sse_c_copy_source: str
    sse_c_copy_source_key: str | bytes
    metadata: Mapping[str, str]
    metadata_directive: str
    copy_props: CopyPropsMode
    annotation_copy_mode: AnnotationCopyMode
    cache_control: str
    content_type: str
    content_disposition: str
    content_encoding: str
    content_language: str
    expires: str
    website_redirect: str
    checksum_algorithm: str
    checksum_mode: str
    request_payer: str
    guess_mime_type: bool
    force_glacier_transfer: bool
    ignore_glacier_warnings: bool
    case_conflict: CaseConflictMode
    no_overwrite: bool


ProgressCallback = Callable[[TransferProgress], None]
ResultCallback = Callable[[OpResult], None]
ListingCallback = Callable[[FileInfo], None]

# A per-entry filter used across operations (``rm`` / ``cp`` / ``mv`` / ``sync``
# visibility, and ``sync``'s ``create_filter`` / ``delete_filter`` lanes): a
# predicate over the ``FileInfo`` returning True to keep the entry. The operation
# stamps ``info.compare_key`` (aws-cli's --exclude/--include matching space)
# before consulting it, so a glob predicate matches
# ``compare_key`` while a richer predicate can decide on ``size`` / ``mtime`` /
# ``storage_class`` / ``key``, or reach the entry's backend through ``info.storage``
# (e.g. a HeadObject for a tag the listing omits).
# ``GlobFilter`` is the ready-made glob implementation.
FileFilter = Callable[[FileInfo], bool]


def strip_response_metadata(
    response: Mapping[str, Any], *, drop_body: bool = False
) -> dict[str, Any]:
    """A parsed S3 response minus ``ResponseMetadata`` (HTTP transport internals).

    The wire-shape convention for everything surfaced under
    ``OpResult.extra_info``: response slots carry the operation's parsed fields
    only. ``drop_body`` additionally removes the streaming ``Body`` a
    ``GetObject`` response carries (the object bytes, never a metadata field).
    """
    dropped = ("Body", "ResponseMetadata") if drop_body else ("ResponseMetadata",)
    return {k: v for k, v in response.items() if k not in dropped}


__all__ = [
    "AnnotationCopyMode",
    "CancelMode",
    "CancelToken",
    "CaseConflictMode",
    "CopyPropsMode",
    "FileFilter",
    "FileInfo",
    "FileKind",
    "ListingCallback",
    "LocalFileInfo",
    "LocalScanOptions",
    "OpOutcome",
    "OpResult",
    "ProgressCallback",
    "ResultCallback",
    "S3FileInfo",
    "S3ScanOptions",
    "ScanOptions",
    "TransferOptions",
    "TransferProgress",
    "TransferType",
]
