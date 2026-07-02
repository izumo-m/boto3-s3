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
    turns it back into a host path for I/O.

    ``entry`` is the same object ``os.scandir()`` yields when the producer had
    one, so callers can reuse its ``stat()`` cache and other methods without
    additional syscalls. Producers without a ``DirEntry`` at hand - the walk
    root, or the ``os.listdir``-based aws-cli-order walk - leave it ``None``.
    """

    entry: os.DirEntry[str] | None = None


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
    """Bundle of ``Storage.scan`` / ``Storage.scan_pages`` knobs, passed as one value.

    Carrying a single immutable object - rather than re-threading keyword
    arguments through ``scan`` -> ``scan_pages`` and every override - keeps the
    enumeration seam tidy and lets ``sync`` build one set of options and apply it
    to both sides. ``page_size`` / ``request_payer`` / ``fetch_owner`` are S3
    listing knobs ignored by non-S3 backends. ``bucket_name_prefix`` /
    ``bucket_region`` filter an S3 *bucket* listing (``ListBuckets`` ``Prefix`` /
    ``BucketRegion``, scanned from the service root) and are ignored for object
    listings - mirroring how inapplicable knobs are silently ignored elsewhere
    (aws-cli parity). Values are not validated here: like aws-cli, they pass
    through to the service, which decides (``page_size=0`` lists nothing,
    negative values fail the call with ``InvalidArgument`` - the exit-code
    charter requires reproducing both). ``follow_symlinks`` /
    ``detect_symlink_loops`` / ``on_warning`` are local-walk knobs (ignored by
    S3, like the listing knobs above): ``follow_symlinks=False`` skips symlinks;
    ``on_warning`` receives the aws-cli-worded skip messages (a broken symlink,
    an unreadable or special file) that the transfer rolls up - without it those
    entries are dropped silently. ``detect_symlink_loops`` (default ``False``, a
    library extension - ``aws s3`` has none, so off keeps parity) guards the
    recursive walk against symlink cycles: with it (and ``follow_symlinks``) a
    directory that resolves to one of its own ancestors is skipped with a
    ``Symbolic link loop detected`` warning instead of recursing until
    ``RecursionError``. Off is zero extra cost (no per-directory ``stat``).

    ``filter`` is a per-entry predicate (``True`` keeps the entry) applied by
    ``scan`` itself - page by page on the prefetch worker, before pages cross
    the hand-off queue - so ``scan_pages`` implementations and overrides keep
    yielding raw pages and never handle it. It carries the item filter of
    ``rm`` / ``cp`` / ``mv`` below the flatten loop, and sync's *visibility*
    layer: each side's listing is pruned independently here (its own
    ``--exclude`` / ``--include`` root) before the comparator pairs the
    streams - which is exactly why filtered-out destination entries are
    protected from ``--delete``. The predicate runs on the worker thread:
    keep it thread-safe and fast.

    ``sort`` requests entries in UTF-8 byte order of their ``compare_key``:
    ``sync`` sets it (its merge-join needs both sides ascending), while ``cp`` /
    ``mv`` / ``ls`` / ``rm`` leave it ``False`` (order is immaterial - each entry
    transfers / lists / deletes independently). A backend declaring
    :attr:`~boto3_s3.storage.StorageCapability.SORTED_SCAN` MUST honor
    ``sort=True``; the built-ins ignore the flag and always sort (S3's listing is
    byte-ordered, the local walk sorts for aws parity), so it costs nothing for
    them. A custom backend whose sort is expensive may stream natural order when
    ``sort=False`` and pay the sort only for ``sync``.
    """

    recursive: bool = False
    sort: bool = False
    page_size: int = 1000
    request_payer: str | None = None
    fetch_owner: bool = False
    bucket_name_prefix: str | None = None
    bucket_region: str | None = None
    filter: Callable[[FileInfo], bool] | None = None
    follow_symlinks: bool = True
    detect_symlink_loops: bool = False
    on_warning: Callable[[str], None] | None = None


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
    "OpOutcome",
    "OpResult",
    "ProgressCallback",
    "ResultCallback",
    "S3FileInfo",
    "ScanOptions",
    "TransferOptions",
    "TransferProgress",
    "TransferType",
]
