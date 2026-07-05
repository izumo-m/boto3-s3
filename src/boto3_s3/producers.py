"""The per-info transfer producers and gates cp / mv / sync share.

The "producer" half of the transfer design (docs/transfer.md): turn a
:class:`~boto3_s3.transferplan.TransferPlan` plus listing entries into
:class:`~boto3_s3.transfer.TransferItem` objects, running the aws-cli item
gates on
the way - the case-conflict gate, the glacier gate, the parent-reference and
oversize warnings, and the open-route capability checks. Everything here is a
plain function over the plan and the entry: no ``S3`` instance state is read
(these grew up as ``S3`` methods that only ever called their siblings), so the
orchestrator (:mod:`boto3_s3.s3`) calls them as ``producers.upload_items(...)``
and the producer/orchestrator boundary is physical, not just conceptual.

Kept out of :mod:`boto3_s3.transfer` on purpose: the engine module is
deliberately blind to ``transferplan`` / the storage backends, while the
producers need all of them. Like the planner, importing this module reaches
``botocore.exceptions`` through the backends (import contract item 3).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from boto3_s3 import requestparams, transferplan
from boto3_s3.exceptions import NotFoundError, ValidationError
from boto3_s3.localstorage import LocalStorage, to_native_path
from boto3_s3.s3storage import S3Storage, s3_errors
from boto3_s3.storage import Storage, StorageCapability
from boto3_s3.transfer import TransferItem, Transferrer
from boto3_s3.types import (
    CaseConflictMode,
    FileFilter,
    FileInfo,
    S3FileInfo,
    ScanOptions,
    TransferOptions,
    TransferType,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from boto3_s3.comparator import SyncPair

# S3's multipart ceiling: 10000 parts x 5 GiB. aws warns (without skipping)
# for larger uploads; the size string is aws-cli's rendered constant
# (human_readable_size(MAX_UPLOAD_SIZE)).
_MAX_UPLOAD_SIZE = 5 * 1024**3 * 10000
_MAX_UPLOAD_SIZE_TEXT = "48.8 TiB"

# Storage classes the glacier gate blocks unless restored or forced.
_GLACIER_STORAGE_CLASSES = ("GLACIER", "DEEP_ARCHIVE")


def _ckey(info: FileInfo) -> str:
    """The producer-stamped ``compare_key``, narrowed to ``str``.

    Every ``Storage.scan`` (listing) and ``Storage.get_fileinfo`` (single) entry
    carries it (the single-object HEAD path stamps it too), so a transfer reads it
    instead of re-deriving the root-relative key. ``None`` would mean a producer
    skipped the stamp - a bug.
    """
    key = info.compare_key
    assert key is not None, "compare_key must be stamped before transfer"
    return key


def open_side_display(storage: Storage, key: str) -> str:
    """A display string for the custom (``open``-routed) side of a transfer.

    The analog of the local path / ``s3://`` URI the built-in routes render:
    the backend's own ``as_text()`` location token, with the entry's relative
    ``key`` appended when it addresses a child (``""`` is the location itself).
    """
    text = storage.as_text()
    return f"{text.rstrip('/')}/{key}" if key else text


def _glacier_blocked(info: FileInfo, *, options: TransferOptions) -> bool:
    """Whether the aws-cli glacier gate skips this source object.

    GLACIER / DEEP_ARCHIVE sources block downloads and copies unless restored
    (``Restore`` carries ``ongoing-request="false"``) or forced; an upload
    never reaches this gate. Only the single-object path has a HeadObject to
    read ``Restore`` from - a recursive listing has none, so restored objects
    still skip there (aws-cli-faithful; ``fileinfo.is_glacier_compatible``).
    """
    if options.get("force_glacier_transfer"):
        return False
    if not isinstance(info, S3FileInfo) or info.storage_class not in _GLACIER_STORAGE_CLASSES:
        return False
    restore = ""
    if info.head is not None:
        restore = str(info.head.get("Restore", ""))
    return 'ongoing-request="false"' not in restore


def _glacier_warning(src_display: str, transfer_type: TransferType) -> str:
    """aws-cli's glacier skip message (``s3handler._warn_glacier``)."""
    op = transfer_type.value
    return (
        f"Skipping file {src_display}. Object is of storage class GLACIER. "
        f"Unable to perform {op} operations on GLACIER objects. You must "
        "restore the object to be able to perform the operation. See aws "
        f"s3 {op} help for additional parameter options to ignore or force "
        "these transfers."
    )


def _glacier_gate(
    info: FileInfo,
    *,
    transfer_type: TransferType,
    options: TransferOptions,
    transferrer: Transferrer,
    compare_key: str,
    src_display: str,
) -> bool:
    """Run the glacier gate; ``True`` when it consumed the item.

    A blocked item is either silently skipped (``ignore_glacier_warnings``,
    counted) or warned away with aws's message - the caller drops it either way.
    """
    if not _glacier_blocked(info, options=options):
        return False
    if options.get("ignore_glacier_warnings"):
        transferrer.skip(TransferItem(compare_key=compare_key, src_display=src_display))
    else:
        transferrer.warn(_glacier_warning(src_display, transfer_type), key=compare_key)
    return True


class CaseConflictGate:
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

    def _admit(self, item: TransferItem, lowered: str) -> bool:
        """Admit a download: track it in-flight and arrange its cleanup.

        Mirrors aws-cli's ``CaseConflictSync``: the casefolded key joins the
        in-flight set, and the item carries the callback that removes it when the
        download finishes (wired as ``_CaseConflictCleanup`` in
        ``_submit_download``), so the set reflects only downloads still in flight -
        not every key ever admitted.
        """
        self._submitted.add(lowered)
        item.case_conflict_cleanup = lambda: self._submitted.discard(lowered)
        return False

    def blocks(self, item: TransferItem, transferrer: Transferrer) -> bool:
        if item.compare_key in self._dest_keys:
            return False  # exact-case match at the destination: always copy
        lowered = item.compare_key.lower()
        src = f"{item.src_bucket}/{item.src_key}"
        dest = os.path.abspath(item.dest_path or "")
        if lowered not in self._submitted and not os.path.exists(dest):
            return self._admit(item, lowered)
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
            return self._admit(item, lowered)
        # A precondition violation (the destination filesystem cannot hold
        # both names); raised in-pipeline, rc 1 either way.
        raise ValidationError(
            f"Failed to download {src} -> {dest} because a file whose name "
            "differs only by case either exists or is being downloaded.",
            operation=self._operation,
        )


def upload_items(
    plan: transferplan.TransferPlan,
    *,
    dest_bucket: str,
    transferrer: Transferrer,
    follow_symlinks: bool,
    detect_symlink_loops: bool,
    item_filter: FileFilter | None,
) -> Iterator[TransferItem]:
    """Materialize upload items from the local source (warnings -> rollup).

    A recursive source enumerates through ``plan.src.scan`` so a ``LocalStorage``
    subclass that overrides ``scan`` is honored; a single (non-dir_op) source is
    a point op walked directly (the local analog of ``head_single`` - no
    directory check, so a directory source becomes an item the engine fails
    with [Errno 21] Is a directory, rc 1, like aws-cli). The recursive listing
    stamps each entry's ``compare_key`` and applies ``item_filter`` inside the
    scan (``scan_pages``' contract); the single source, having no scan filter, is
    filtered here.
    """
    infos: Iterator[FileInfo]
    if plan.dir_op:
        # The recursive listing filters inside the scan (the scan_pages contract),
        # so a LocalStorage subclass can prune during the walk.
        infos = plan.src.scan(
            ScanOptions(
                recursive=True,
                follow_symlinks=follow_symlinks,
                detect_symlink_loops=detect_symlink_loops,
                on_warning=transferrer.warn,
                filter=item_filter,
            )
        )
    else:
        # Single source object: the get_fileinfo point op (the scan
        # counterpart). None = warned-away (special/unreadable) or absent.
        # get_fileinfo has no filter, so an excluded single source is dropped here.
        single = plan.src.get_fileinfo(follow_symlinks=follow_symlinks, on_warning=transferrer.warn)
        infos = iter([single] if single is not None else [])
        if item_filter is not None:
            infos = (info for info in infos if item_filter(info))
    for info in infos:
        yield upload_item_from_info(plan, info, dest_bucket=dest_bucket, transferrer=transferrer)


def upload_item_from_info(
    plan: transferplan.TransferPlan,
    info: FileInfo,
    *,
    dest_bucket: str,
    transferrer: Transferrer,
) -> TransferItem:
    """One upload item from a walk entry (the oversize warning included)."""
    native = to_native_path(info.key)
    compare_key = _ckey(info)
    dest = transferplan.dest_for(plan, compare_key)
    if info.size is not None and info.size > _MAX_UPLOAD_SIZE:
        # aws-cli's _warn_if_too_large: warn (rendered relative) but
        # still attempt, so S3's own EntityTooLarge stays visible.
        transferrer.warn(
            f"File {LocalStorage.relative_path(native)} exceeds s3 upload limit of "
            f"{_MAX_UPLOAD_SIZE_TEXT}.",
            key=compare_key,
        )
    return TransferItem(
        compare_key=compare_key,
        size=info.size,
        mtime=info.mtime,
        src_path=native,
        src_info=info,
        dest_bucket=dest_bucket,
        dest_key=dest[len(dest_bucket) + 1 :],
        src_display=native,
        dest_display=f"s3://{dest}",
    )


def s3_source_items(
    plan: transferplan.TransferPlan,
    src_storage: S3Storage,
    *,
    transfer_type: TransferType,
    dest_bucket: str,
    transferrer: Transferrer,
    page_size: int,
    item_filter: FileFilter | None,
    options: TransferOptions,
    case_gate: CaseConflictGate | None = None,
    operation: str,
) -> Iterator[TransferItem]:
    """Materialize download/copy items from an S3 source, gates applied."""
    src_bucket = src_storage.bucket
    if plan.dir_op:
        infos: Iterator[FileInfo] = scan_s3_source(
            src_storage,
            key_prefix=plan.src_root[len(src_bucket) + 1 :],
            page_size=page_size,
            item_filter=item_filter,
            options=options,
        )
    elif src_storage.bucket and not src_storage.key:
        # Keyless non-recursive source (`cp s3://bucket .`): aws lists the
        # bucket and exact-matches nothing -> zero items, rc 0. Same
        # outcome here without issuing the listing. A bucketless service root
        # (`cp s3://`) is NOT this case: it falls to head_single, whose empty
        # Bucket hits botocore's Invalid-bucket ParamValidation like aws.
        return
    else:
        infos = head_single(
            src_storage, transfer_type=transfer_type, options=options, operation=operation
        )
        if item_filter is not None:
            # aws applies --exclude/--include to the single-object routes too
            # (its filter pipeline stage runs regardless of dir_op); the
            # recursive branch filters inside the scan above. An excluded mv
            # source must not transfer - and must not be deleted.
            infos = (info for info in infos if item_filter(info))

    for info in infos:
        if transfer_type is TransferType.DOWNLOAD:
            item = download_item_from_info(
                plan,
                info,
                src_bucket=src_bucket,
                transferrer=transferrer,
                options=options,
                case_gate=case_gate,
            )
        else:
            item = copy_item_from_info(
                plan,
                info,
                src_bucket=src_bucket,
                dest_bucket=dest_bucket,
                transferrer=transferrer,
                options=options,
            )
        if item is not None:
            yield item


def download_item_from_info(
    plan: transferplan.TransferPlan,
    info: FileInfo,
    *,
    src_bucket: str,
    transferrer: Transferrer,
    options: TransferOptions,
    case_gate: CaseConflictGate | None,
) -> TransferItem | None:
    """One download item from a listing entry, or ``None`` once a gate
    consumed it (the gate emits its own warn/skip/notice record)."""
    src_path = f"{src_bucket}/{info.key}"
    compare_key = _ckey(info)
    dest = transferplan.dest_for(plan, compare_key)
    src_display = f"s3://{src_path}"
    is_s3_info = isinstance(info, S3FileInfo)
    item = TransferItem(
        compare_key=compare_key,
        size=info.size,
        etag=info.etag if is_s3_info else None,
        mtime=info.mtime,
        head=info.head if is_s3_info else None,
        src_bucket=src_bucket,
        src_key=info.key,
        src_info=info,
        dest_path=dest,
        src_display=src_display,
        dest_display=dest,
    )
    # The comparator-equivalent gate runs before the submitter
    # warning handlers (aws-cli instruction order).
    if case_gate is not None and case_gate.blocks(item, transferrer):
        return None
    if _glacier_gate(
        info,
        transfer_type=TransferType.DOWNLOAD,
        options=options,
        transferrer=transferrer,
        compare_key=compare_key,
        src_display=src_display,
    ):
        return None
    # Anchor with "./" before normpath (aws-cli's _warn_parent_reference): a
    # compare_key that relativizes to a leading slash (e.g. "/../secret" from
    # an S3 key "data//../secret") must still be caught - bare
    # normpath("/../secret") == "/secret" would slip the "..".
    if os.path.normpath("." + os.sep + compare_key).startswith(".." + os.sep):
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


def copy_item_from_info(
    plan: transferplan.TransferPlan,
    info: FileInfo,
    *,
    src_bucket: str,
    dest_bucket: str,
    transferrer: Transferrer,
    options: TransferOptions,
) -> TransferItem | None:
    """One S3-to-S3 copy item from a listing entry, or ``None`` when the
    glacier gate consumed it."""
    src_path = f"{src_bucket}/{info.key}"
    compare_key = _ckey(info)
    dest = transferplan.dest_for(plan, compare_key)
    src_display = f"s3://{src_path}"
    is_s3_info = isinstance(info, S3FileInfo)
    if _glacier_gate(
        info,
        transfer_type=TransferType.COPY,
        options=options,
        transferrer=transferrer,
        compare_key=compare_key,
        src_display=src_display,
    ):
        return None
    return TransferItem(
        compare_key=compare_key,
        size=info.size,
        etag=info.etag if is_s3_info else None,
        mtime=info.mtime,
        head=info.head if is_s3_info else None,
        src_bucket=src_bucket,
        src_key=info.key,
        src_info=info,
        dest_bucket=dest_bucket,
        dest_key=dest[len(dest_bucket) + 1 :],
        src_display=src_display,
        dest_display=f"s3://{dest}",
    )


def scan_s3_source(
    storage: S3Storage,
    *,
    key_prefix: str,
    page_size: int,
    item_filter: FileFilter | None,
    options: TransferOptions,
) -> Iterator[FileInfo]:
    """A recursive object listing anchored at the '/'-normalized ``key_prefix``.

    The shared transfer-side enumeration: cp/mv scan their source here,
    and sync scans whichever of its sides is S3 (the destination too).
    Folder markers never surface; the scan stamps each entry's prefix-relative
    ``compare_key``, which ``item_filter`` matches against. The passed ``storage``
    instance is scanned directly (``ScanOptions.prefix`` re-anchors the listing at
    the normalized ``key_prefix``), so a custom ``S3Storage`` subclass survives.
    """

    def scan_filter(info: FileInfo) -> bool:
        # Zero-byte '/'-terminated "folder marker" objects never transfer
        # (aws-cli filegenerator); the user filter prunes what remains.
        if info.size == 0 and info.key.endswith("/"):
            return False
        if item_filter is None:
            return True
        return item_filter(info)

    scan_options = ScanOptions(
        recursive=True,
        page_size=page_size,
        request_payer=options.get("request_payer"),
        prefix=key_prefix,
        filter=scan_filter,
    )
    return storage.scan(scan_options)


def head_single(
    src_storage: S3Storage,
    *,
    transfer_type: TransferType,
    options: TransferOptions,
    operation: str,
) -> Iterator[FileInfo]:
    """Resolve a single S3 source by HeadObject (aws-cli's `_list_single_object`).

    Any 404 is rewritten to aws's ``Key "..." does not exist`` message; a
    copy source is headed with the copy-source SSE-C parameters. Like aws-cli's
    filegenerator, the HEAD carries ``ChecksumMode=ENABLED`` when the client
    resolves checksum validation to ``when_supported`` (the botocore default).
    """
    key = src_storage.key
    if transfer_type is TransferType.COPY:
        params = requestparams.map_head_object_params_with_copy_source_sse(options)
    else:
        params = requestparams.map_head_object_params(options)
    client = src_storage.get_client()
    # aws-cli's filegenerator sends ChecksumMode=ENABLED on the single-source
    # HeadObject whenever the client resolves checksum validation to
    # 'when_supported' (the botocore default since checksums GA), so the HEAD
    # request matches aws even without an explicit --checksum-mode. setdefault
    # keeps an explicit mode; getattr guards botocore floors predating the knob.
    if getattr(client.meta.config, "response_checksum_validation", None) == "when_supported":
        params.setdefault("ChecksumMode", "ENABLED")
    try:
        with s3_errors(operation=operation, bucket=src_storage.bucket, key=key):
            head = client.head_object(Bucket=src_storage.bucket, Key=key, **params)
    except NotFoundError as exc:
        raise NotFoundError(
            "An error occurred (404) when calling the HeadObject operation: "
            f'Key "{key}" does not exist',
            operation=operation,
            bucket=src_storage.bucket,
            key=key,
        ) from exc
    etag = head.get("ETag")
    # A single (non-dir_op) source: the compare key is the key's basename,
    # matching transferplan.item_paths' single-item branch.
    yield S3FileInfo(
        key=key,
        size=head.get("ContentLength"),
        mtime=head.get("LastModified"),
        etag=etag.strip('"') if etag else None,
        storage_class=head.get("StorageClass"),
        head=head,
        compare_key=key.rsplit("/", 1)[-1],
    )


def require_open_capabilities(
    plan: transferplan.TransferPlan, *, recursive: bool, is_move: bool, operation: str
) -> None:
    """Reject an open-route custom side that lacks a contract method.

    The capability set is structural (class-level): ``opens3`` reads its
    custom source - a single object via ``get_fileinfo``, a recursive tree
    via ``scan`` - and opens each entry ``"rb"``, plus ``delete`` when it is
    an ``mv`` source (the source is removed after each transfer); ``s3open``
    opens its custom destination ``"wb"`` (an ``mv`` deletes the S3 source,
    not the custom side, so it needs nothing extra). A backend that declares
    less than the route asks of it is rejected here, before any bytes move,
    with the gap named (so a forgotten method is a clear ``ValidationError``,
    not a deep failure).
    """
    if plan.paths_type == "opens3":
        custom: Storage = plan.src
        needed = StorageCapability.OPEN_READ
        needed |= StorageCapability.SCAN if recursive else StorageCapability.GET_FILEINFO
        if is_move:
            needed |= StorageCapability.DELETE
    elif plan.paths_type == "s3open":
        custom = plan.dest
        needed = StorageCapability.OPEN_WRITE
    else:
        return
    _reject_missing_capabilities(custom, needed, operation=operation)


def require_open_sync_capabilities(
    plan: transferplan.TransferPlan, *, delete: bool, operation: str
) -> None:
    """Reject an open-route custom side that cannot back a ``sync``.

    ``sync`` merge-joins two byte-ordered listings, so a custom side must
    declare ``SORTED_SCAN`` (an unsorted side would manufacture phantom
    new/delete pairs - with ``--delete``, destination corruption). On top of
    that: an ``opens3`` source needs ``OPEN_READ``; an ``s3open`` destination
    needs ``OPEN_WRITE`` plus ``DELETE`` when ``delete`` removes orphans (the
    ``opens3`` orphans are S3, deleted without the custom side).
    """
    if plan.paths_type == "opens3":
        custom: Storage = plan.src
        needed = StorageCapability.SORTED_SCAN | StorageCapability.OPEN_READ
    elif plan.paths_type == "s3open":
        custom = plan.dest
        needed = StorageCapability.SORTED_SCAN | StorageCapability.OPEN_WRITE
        if delete:
            needed |= StorageCapability.DELETE
    else:
        return
    _reject_missing_capabilities(custom, needed, operation=operation)


def _reject_missing_capabilities(
    custom: Storage, needed: StorageCapability, *, operation: str
) -> None:
    """Raise a clear ``ValidationError`` naming the capabilities ``custom`` lacks."""
    missing = custom.missing_capabilities(needed)
    if missing:
        names = ", ".join(c.name for c in StorageCapability if c in missing and c.name)
        raise ValidationError(
            f"{operation}: the custom backend {custom.as_text()!r} cannot satisfy this "
            f"transfer (missing capability: {names})",
            operation=operation,
        )


def open_upload_items(
    plan: transferplan.TransferPlan,
    *,
    dest_bucket: str,
    transferrer: Transferrer,
    follow_symlinks: bool,
    detect_symlink_loops: bool,
    item_filter: FileFilter | None,
    operation: str,
    dryrun: bool,
) -> Iterator[TransferItem]:
    """Upload items from a custom source: each entry's bytes via ``open("rb")``.

    The open-route mirror of ``_cp_upload_items`` - a recursive source
    enumerates through ``plan.src.scan``, a single source resolves through
    ``plan.src.get_fileinfo`` - but the source has no local path: the engine
    reads its ``Storage.open(key, "rb")``. The open key is the entry's
    ``compare_key`` for a recursive item, ``""`` (the location itself) for a
    single one; the producer stamps each ``compare_key``, from which the
    filter and the S3 destination key derive. A ``dryrun`` enumerates but
    does not ``open`` the source (no side effect on the backend).

    A single source the backend cannot resolve (``get_fileinfo`` -> ``None``)
    raises like a missing local source - ``NotFoundError``, aws's wording
    (rc 255: no ``ClientError`` cause) - rather than transferring nothing. A
    recursive source is an enumeration: an empty ``scan`` yields zero items
    (rc 0), matching an empty S3 prefix listing.
    """
    if plan.dir_op:
        # The recursive listing filters inside the scan (the scan_pages contract),
        # so a custom source backend can push the predicate to its source.
        infos: Iterator[FileInfo] = plan.src.scan(
            ScanOptions(
                recursive=True,
                follow_symlinks=follow_symlinks,
                detect_symlink_loops=detect_symlink_loops,
                on_warning=transferrer.warn,
                filter=item_filter,
            )
        )
    else:
        single = plan.src.get_fileinfo(follow_symlinks=follow_symlinks, on_warning=transferrer.warn)
        if single is None:
            raise NotFoundError(
                f"The user-provided path {plan.src.as_text()} does not exist.",
                operation=operation,
            )
        # get_fileinfo has no filter, so an excluded single source is dropped here.
        infos = iter([single])
        if item_filter is not None:
            infos = (info for info in infos if item_filter(info))
    for info in infos:
        yield open_upload_item(plan, info, dest_bucket=dest_bucket, dryrun=dryrun)


def open_upload_item(
    plan: transferplan.TransferPlan, info: FileInfo, *, dest_bucket: str, dryrun: bool
) -> TransferItem:
    """One upload item reading the custom source through ``Storage.open``.

    ``dryrun`` skips the ``open`` (the item is only reported, never
    submitted), so a dry run never reads from the backend.
    """
    compare_key = _ckey(info)
    open_key = compare_key if plan.dir_op else ""
    dest = transferplan.dest_for(plan, compare_key)
    return TransferItem(
        compare_key=compare_key,
        size=info.size,
        mtime=info.mtime,
        # src_info carries the source listing entry so an mv can delete it via the
        # backend's Storage.delete(info). info.key addresses the same object open_key
        # opens: for a recursive item info.key is the compare_key (the open key), for a
        # single source it is "" (the location). The backend resolves both in its own
        # key space.
        src_info=info,
        src_fileobj=None if dryrun else plan.src.open(open_key, "rb", size=info.size),
        dest_bucket=dest_bucket,
        dest_key=dest[len(dest_bucket) + 1 :],
        src_display=open_side_display(plan.src, open_key),
        dest_display=f"s3://{dest}",
    )


def open_download_items(
    plan: transferplan.TransferPlan,
    src_storage: S3Storage,
    *,
    transferrer: Transferrer,
    page_size: int,
    item_filter: FileFilter | None,
    options: TransferOptions,
    operation: str,
    dryrun: bool,
) -> Iterator[TransferItem]:
    """Download items from an S3 source into a custom destination's ``open("wb")``.

    The open-route mirror of the S3-source half of ``_cp_s3_source_items``:
    the source is enumerated identically (``_cp_scan`` for a recursive
    prefix, ``head_single`` for one object), but each entry is written
    through ``plan.dest.open(key, "wb")`` instead of to a local path. The
    destination's local-filesystem gates (case-conflict, parent-reference,
    ``no_overwrite``'s ``os.path.exists``) do not apply - the backend owns its
    own key space - so only the source-side glacier gate runs. A ``dryrun``
    enumerates but does not ``open`` the destination (no side effect on the
    backend).
    """
    src_bucket = src_storage.bucket
    if plan.dir_op:
        infos: Iterator[FileInfo] = scan_s3_source(
            src_storage,
            key_prefix=plan.src_root[len(src_bucket) + 1 :],
            page_size=page_size,
            item_filter=item_filter,
            options=options,
        )
    elif src_storage.bucket and not src_storage.key:
        # Keyless non-recursive source (`cp s3://bucket custom`): aws lists
        # the bucket and exact-matches nothing -> zero items (the built-in
        # download path's behavior, without issuing the listing). A bucketless
        # service root (`cp s3:// custom`) instead falls to head_single, whose
        # empty Bucket hits botocore's Invalid-bucket ParamValidation like aws.
        return
    else:
        infos = head_single(
            src_storage,
            transfer_type=TransferType.DOWNLOAD,
            options=options,
            operation=operation,
        )
        if item_filter is not None:
            # Mirror the built-in download route: aws filters the
            # single-object case too (the recursive branch filters inside
            # the scan above).
            infos = (info for info in infos if item_filter(info))
    for info in infos:
        item = open_download_item(
            plan,
            info,
            src_bucket=src_bucket,
            transferrer=transferrer,
            options=options,
            dryrun=dryrun,
        )
        if item is not None:
            yield item


def open_download_item(
    plan: transferplan.TransferPlan,
    info: FileInfo,
    *,
    src_bucket: str,
    transferrer: Transferrer,
    options: TransferOptions,
    dryrun: bool,
) -> TransferItem | None:
    """One download item writing the custom destination through ``Storage.open``,
    or ``None`` once the glacier gate consumed it.

    ``dryrun`` skips the destination ``open`` (the item is only reported,
    never submitted), so a dry run never opens the backend for writing.
    """
    compare_key = _ckey(info)
    open_key = transferplan.dest_for(plan, compare_key)
    src_display = f"s3://{src_bucket}/{info.key}"
    is_s3_info = isinstance(info, S3FileInfo)
    if _glacier_gate(
        info,
        transfer_type=TransferType.DOWNLOAD,
        options=options,
        transferrer=transferrer,
        compare_key=compare_key,
        src_display=src_display,
    ):
        return None
    return TransferItem(
        compare_key=compare_key,
        size=info.size,
        etag=info.etag if is_s3_info else None,
        mtime=info.mtime,
        head=info.head if is_s3_info else None,
        src_bucket=src_bucket,
        src_key=info.key,
        src_info=info,
        dest_fileobj=None if dryrun else plan.dest.open(open_key, "wb", size=info.size),
        src_display=src_display,
        dest_display=open_side_display(plan.dest, open_key),
    )


def cp_case_gate(
    plan: transferplan.TransferPlan,
    *,
    recursive: bool,
    options: TransferOptions,
) -> CaseConflictGate | None:
    """Build the ``--case-conflict`` gate when it applies (aws-cli scope:
    recursive S3->local with a mode other than ``ignore``).

    The destination tree is enumerated up front (through ``plan.dest.scan``)
    into the exact-case membership set the gate's AlwaysSync arm consults -
    the observable equivalent of aws's reverse file generator + comparator.
    Each entry's stamped ``compare_key`` is the root-relative membership key.
    Scoped to ``s3local`` (a case-insensitive *filesystem* destination); a
    custom ``s3open`` destination owns its own key space, so it never scans
    the custom side here.
    """
    mode = CaseConflictMode(options.get("case_conflict", CaseConflictMode.IGNORE))
    if plan.paths_type != "s3local" or not recursive or mode is CaseConflictMode.IGNORE:
        return None
    dest_keys = {_ckey(info) for info in plan.dest.scan(ScanOptions(recursive=True))}
    return CaseConflictGate(mode, dest_keys)


def sync_case_gate(
    transfer_type: TransferType, dest_storage: Storage, *, options: TransferOptions
) -> CaseConflictGate | None:
    """The ``--case-conflict`` gate for sync downloads to a local destination.

    Unlike cp's (which pre-lists the destination for its exact-case
    AlwaysSync arm), sync already pairs against the destination listing:
    the gate is consulted only for pairs *missing* there, so the
    membership set is vacuously empty and only the submitted-set /
    ``os.path.exists`` conflict check remains - aws-cli's
    ``CaseConflictSync`` in the ``file_not_at_dest`` slot. Scoped to a
    ``LocalStorage`` destination (a case-insensitive *filesystem*); a custom
    ``s3open`` destination owns its key space and runs no such check.
    """
    mode = CaseConflictMode(options.get("case_conflict", CaseConflictMode.IGNORE))
    if (
        transfer_type is not TransferType.DOWNLOAD
        or mode is CaseConflictMode.IGNORE
        or not isinstance(dest_storage, LocalStorage)
    ):
        return None
    return CaseConflictGate(mode, set(), operation="sync")


def sync_entries(
    storage: Storage,
    *,
    root: str,
    item_filter: FileFilter | None,
    transferrer: Transferrer,
    follow_symlinks: bool,
    detect_symlink_loops: bool,
    page_size: int,
    options: TransferOptions,
) -> Iterator[tuple[str, FileInfo]]:
    """One side's ``(compare_key, info)`` stream, visibility applied.

    A local side walks in aws-cli byte order with its warnings routed to
    the transfer rollup (both sides warn, aws parity); an S3 side is the
    shared anchored listing (folder markers dropped, ``request_payer`` /
    ``page_size`` forwarded - aws-cli maps them onto the destination
    listing too). Both sides' producers stamp ``compare_key`` (the merge-join
    axis), so ``item_filter`` reads it and the pair key is taken from it.
    """
    if isinstance(storage, S3Storage):
        key_prefix = root[len(storage.bucket) + 1 :]
        for info in scan_s3_source(
            storage,
            key_prefix=key_prefix,
            page_size=page_size,
            item_filter=item_filter,
            options=options,
        ):
            yield _ckey(info), info
        return
    for info in storage.scan(
        ScanOptions(
            recursive=True,
            sort=True,  # the merge-join needs both sides byte-ordered
            follow_symlinks=follow_symlinks,
            detect_symlink_loops=detect_symlink_loops,
            on_warning=transferrer.warn,
            filter=item_filter,  # each side's visibility filter, applied in the scan
        )
    ):
        yield _ckey(info), info


def sync_transfer_item(
    plan: transferplan.TransferPlan,
    pair: SyncPair,
    *,
    transfer_type: TransferType,
    src_bucket: str,
    dest_bucket: str,
    transferrer: Transferrer,
    options: TransferOptions,
    case_gate: CaseConflictGate | None,
    dryrun: bool,
) -> TransferItem | None:
    """Build the transfer item for a copy-judged pair, cp's gates applied.

    A custom-backend side (``opens3`` / ``s3open``) moves through the open
    builders (``Storage.open``); ``dryrun`` skips the ``open`` there so a
    dry run never touches the backend.
    """
    info = pair.src
    assert info is not None
    if plan.paths_type == "opens3":
        item = open_upload_item(plan, info, dest_bucket=dest_bucket, dryrun=dryrun)
    elif plan.paths_type == "s3open":
        item = open_download_item(
            plan,
            info,
            src_bucket=src_bucket,
            transferrer=transferrer,
            options=options,
            dryrun=dryrun,
        )
    elif transfer_type is TransferType.UPLOAD:
        item = upload_item_from_info(plan, info, dest_bucket=dest_bucket, transferrer=transferrer)
    elif transfer_type is TransferType.DOWNLOAD:
        # The case-conflict gate guards only pairs missing at the
        # destination (the aws-cli strategy slot); an exact-key update
        # never conflicts.
        gate = case_gate if pair.dest is None else None
        item = download_item_from_info(
            plan,
            info,
            src_bucket=src_bucket,
            transferrer=transferrer,
            options=options,
            case_gate=gate,
        )
    else:
        item = copy_item_from_info(
            plan,
            info,
            src_bucket=src_bucket,
            dest_bucket=dest_bucket,
            transferrer=transferrer,
            options=options,
        )
    # Stamp the comparison's destination entry (None for a new key) so a
    # completion can report both sides of the sync pair. A gate that consumed
    # the item returns None - nothing to stamp.
    if item is not None:
        item.dest_info = pair.dest
    return item


# -- deletion ---------------------------------------------------------
