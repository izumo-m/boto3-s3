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

    ``FILE`` is a real object/file with content (``size`` / ``mtime`` populated).
    ``DIRECTORY`` is a prefix / sub-directory grouping with no backing object (an
    S3 common prefix or a local sub-directory; ``size`` / ``mtime`` may be
    ``None``). ``BUCKET`` is a top-level S3 bucket.
    """

    FILE = "file"
    DIRECTORY = "directory"
    BUCKET = "bucket"


@dataclass(slots=True, kw_only=True)
class FileInfo:
    """Cross-backend listing entry returned by ``Storage.scan`` / ``S3.ls``.

    One uniform type for every entry a backend can list: files, directories /
    prefixes, and buckets are distinguished by :attr:`kind`, not by separate
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
    is relativized to its scan root, so every backend must emit ``/``-separated
    keys regardless of host OS. ``size`` (bytes) and ``mtime`` (tz-aware UTC) are
    populated for ``FILE`` entries and may be ``None`` for directories /
    prefixes; ``BUCKET`` entries carry the bucket's ``CreationDate`` as ``mtime``
    (``size`` stays ``None``). Producers enforce these invariants - the field
    types alone do not.

    ``compare_key`` is the entry's key *relative to its scan root* - the
    ``--exclude`` / ``--include`` matching space, and the axis ``sync`` merge-joins
    on (two ``scan`` sides relativized to their roots share one ``compare_key``
    space, while ``key`` stays the full identifier the transfer / delete uses and
    differs per side). ``Storage.scan`` stamps it on every entry it yields, so a
    custom ``ScanOptions.filter`` predicate - notably
    :class:`~boto3_s3.globsieve.GlobFilter` - matches the root-relative key
    directly, without re-deriving it from ``key``. The name mirrors aws-cli's
    ``FileInfo.compare_key``. It is ``None`` only on a ``FileInfo`` built by hand
    rather than produced by ``scan``.
    """

    key: str
    kind: FileKind = FileKind.FILE
    size: int | None = None
    mtime: datetime | None = None
    compare_key: str | None = None


@dataclass(slots=True, kw_only=True)
class LocalFileInfo(FileInfo):
    """``FileInfo`` for a local filesystem entry.

    Its ``key`` is the **absolute** path with ``os.sep`` normalized to ``/``
    (``LocalStorage`` anchors every scan at the absolutized root); ``to_native_path``
    turns it back into a host path for I/O. ``key`` keeps the path *as walked* -
    a symlinked directory or file stays under its link name, never resolved to
    the target (``follow_symlinks=True`` only follows to descend / stat, matching
    aws-cli).

    ``stat_result`` is the entry's **followed** ``os.stat`` (the same one the
    walk's vetting battery computes - so carrying it costs no extra syscall), a
    plain immutable snapshot a ``filter`` or ``on_result`` callback can read
    (``st_mode`` / ``st_uid`` / ``st_size`` / ... - ``size`` and ``mtime`` are
    derived from it) without re-stat'ing. Because it is a value, not an
    ``os.DirEntry`` handle, the walk keeps its ``dir_fd`` fast path even while
    populating it. ``is_symlink`` records whether the entry itself was a symbolic
    link (from the walk's ``d_type`` / cached ``lstat`` - free); with
    ``follow_symlinks=True`` a link to a file still surfaces here as
    ``is_symlink=True`` while its ``stat_result`` describes the target.
    ``LocalStorage`` populates both on every entry it lists (the walk and the
    single-path ``get_fileinfo``); a ``FileInfo`` built by hand leaves them at
    their defaults.
    """

    stat_result: os.stat_result | None = None
    is_symlink: bool = False


@dataclass(slots=True, kw_only=True)
class S3FileInfo(FileInfo):
    """``FileInfo`` enriched with fields derived from an S3 object listing.

    ``etag`` is the dequoted ETag (surrounding ``"`` stripped) when populated.
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
    honors; backend-specific knobs live on subclasses (:class:`S3ScanOptions` /
    :class:`LocalScanOptions`) so one backend's options never leak into another's
    code. A backend builds its own subclass via
    :meth:`~boto3_s3.storage.Storage.default_scan_options` and the built-ins
    reject a foreign options type, so ``sync`` builds one subclass per side (an
    S3 side gets :class:`S3ScanOptions`, a local side :class:`LocalScanOptions`).
    A custom backend uses this base plus its own instance state, or defines its
    own ``ScanOptions`` subclass. (Bucket *listing* - the S3 service root - is a
    separate operation with its own params, ``S3Storage.list_buckets`` / ``S3.ls``,
    not a ``scan`` knob: ``scan`` enumerates openable entities.)

    ``filter`` is a per-entry predicate (``True`` keeps the entry) applied by
    each ``Storage.scan_pages`` producer, which returns already-filtered pages
    (``scan`` flattens + prefetches them, it no longer sieves). A backend may
    push the predicate to its source (a REST listing filtering server-side) or
    wrap its raw pages with ``storage.sieve_pages``; the built-ins run it on the
    prefetch worker, page by page. It carries the item filter of ``rm`` / ``cp``
    / ``mv``, and sync's *visibility* layer: each side's listing is pruned
    independently (its own ``--exclude`` / ``--include`` root) before the
    comparator pairs the streams - which is exactly why filtered-out destination
    entries are protected from ``--delete`` (an entry a producer does not return
    is simply absent, so it is not a delete candidate). Keep the predicate
    thread-safe and fast (it runs on the worker thread).

    ``sort`` requests entries in UTF-8 byte order of their ``compare_key``:
    ``sync`` sets it (its merge-join needs both sides ascending), while ``cp`` /
    ``mv`` / ``ls`` / ``rm`` leave it ``False`` (order is immaterial - each entry
    transfers / lists / deletes independently). A backend declaring
    :attr:`~boto3_s3.storage.StorageCapability.SORTED_SCAN` MUST honor
    ``sort=True``; the built-ins ignore the flag and always sort (S3's listing is
    byte-ordered, the local walk sorts for aws parity), so it costs nothing for
    them. A custom backend whose sort is expensive may stream natural order when
    ``sort=False`` and pay the sort only for ``sync``.

    ``on_warning`` receives the aws-cli-worded skip messages (a broken symlink,
    an unreadable or special file) a backend's enumeration emits; the transfer
    rolls them up (see ``on_warning`` wiring in producers). Common because any
    backend may warn - without it those entries are dropped silently.
    """

    recursive: bool = False
    sort: bool = False
    filter: Callable[[FileInfo], bool] | None = None
    on_warning: Callable[[str], None] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class S3ScanOptions(ScanOptions):
    """S3 ``ListObjectsV2`` knobs - the S3 backend's :class:`ScanOptions`.

    ``page_size`` / ``request_payer`` / ``fetch_owner`` are not validated here:
    like aws-cli, they pass through to the service, which decides (``page_size=0``
    lists nothing, negative values fail with ``InvalidArgument`` - the exit-code
    charter requires reproducing both). ``fetch_owner`` sends ``FetchOwner=True``
    to populate ``S3FileInfo.owner``.

    ``prefix`` overrides the listing anchor: the backend lists under it (as the
    ``Prefix``, relativizing ``compare_key`` to it) instead of the storage's own
    key. A transfer sets it when its normalized listing prefix differs from the
    raw source key (a recursive ``cp`` / ``mv`` / ``sync`` / ``rm`` source, where
    the plan appends a trailing ``/``), so the *passed* storage instance is
    scanned rather than rebuilt from a URI - a custom ``S3Storage`` subclass (and
    its ``scan_pages`` override) survives. ``None`` uses the storage's key.
    """

    page_size: int = 1000
    request_payer: str | None = None
    fetch_owner: bool = False
    prefix: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalScanOptions(ScanOptions):
    """Local-walk knobs - the ``LocalStorage`` backend's :class:`ScanOptions`.

    ``follow_symlinks=False`` skips symlinks. ``detect_symlink_loops`` (default
    ``False``, a library extension - ``aws s3`` has none, so off keeps parity)
    guards the recursive walk against symlink cycles: with it (and
    ``follow_symlinks``) a directory that resolves to one of its own ancestors is
    skipped with a ``Symbolic link loop detected`` warning (via ``on_warning``)
    instead of recursing until ``RecursionError``. Off is zero extra cost (no
    per-directory ``stat``).
    """

    follow_symlinks: bool = True
    detect_symlink_loops: bool = False


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
    real outcome.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WARNED = "warned"
    SKIPPED = "skipped"
    DRYRUN = "dryrun"
    NOTICE = "notice"


class CaseConflictMode(enum.Enum):
    """Policy for case-fold key collisions when writing to a local filesystem."""

    IGNORE = "ignore"
    SKIP = "skip"
    WARN = "warn"
    ERROR = "error"


class CopyPropsMode(enum.Enum):
    """Which source object properties to propagate on an S3-to-S3 copy."""

    NONE = "none"
    METADATA_DIRECTIVE = "metadata-directive"
    DEFAULT = "default"


@dataclass(slots=True, kw_only=True)
class TransferProgress:
    """Byte-level progress for one in-flight item, passed to ``on_progress``."""

    transfer_type: TransferType
    key: str
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
    """

    transfer_type: TransferType
    key: str
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


class CancelToken:
    """Thread-safe cancellation flag handed to a blocking operation.

    ``cancel()`` may be called from any thread or a signal handler; the
    operation observes it and raises ``CancelledError``.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class TransferOptions(TypedDict, total=False):
    """Object-shaping / S3-request options shared by ``cp`` / ``mv`` / ``sync``.

    Names are the snake_case form of the corresponding ``aws s3`` options; the
    library translates them to S3 API PascalCase internally. Options that do not
    apply to a given transfer direction are ignored (aws-cli parity).
    """

    acl: str
    grants: Sequence[str]
    storage_class: str
    sse: str
    sse_kms_key_id: str
    sse_c: str
    # str or raw bytes, like botocore's SSECustomerKey: aws passes the CLI
    # string through verbatim (only fileb:// loads bytes).
    sse_c_key: str | bytes
    sse_c_copy_source: str
    sse_c_copy_source_key: str | bytes
    metadata: Mapping[str, str]
    metadata_directive: str
    copy_props: CopyPropsMode
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
    # Conditional write (--no-overwrite): IfNoneMatch="*" on uploads/copies
    # (the server's PreconditionFailed becomes a silent skip), an existence
    # check before downloads.
    no_overwrite: bool


ProgressCallback = Callable[[TransferProgress], None]
ResultCallback = Callable[[OpResult], None]

# A per-entry filter used across operations (``rm`` / ``cp`` / ``mv`` / ``sync``
# visibility, and ``sync``'s ``delete`` lane): a predicate over the ``FileInfo``
# returning True to keep the entry. The operation stamps ``info.compare_key``
# (the key relative to its root, aws-cli --exclude/--include space) before
# consulting it, so a glob predicate matches ``compare_key`` while a richer
# predicate can decide on ``size`` / ``mtime`` / ``storage_class`` / ``key``.
# :class:`~boto3_s3.globsieve.GlobFilter` is the ready-made glob implementation.
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
    "CancelToken",
    "CaseConflictMode",
    "CopyPropsMode",
    "FileFilter",
    "FileInfo",
    "FileKind",
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
