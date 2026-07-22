"""The per-info transfer producers and gates cp / mv / sync share.

The "producer" half of the transfer design (docs/transfer.md): turn a
``TransferPlan`` plus listing entries into
``TransferItem`` objects, running the aws-cli item
gates on
the way - the case-conflict gate, the glacier gate, the parent-reference and
oversize warnings, and the open-route capability checks. Everything here is a
plain function over the plan and the entry: no ``S3`` instance state is read
(these grew up as ``S3`` methods that only ever called their siblings), so the
orchestrator (``boto3_s3.s3``) calls them as ``producers.upload_items(...)``
and the producer/orchestrator boundary is physical, not just conceptual.

Kept out of ``boto3_s3.transfer`` on purpose: the engine module is
deliberately blind to ``transferplan`` and touches the storage layer only at
named seams (the ``LocalStorage`` fsync opt-in probe, ``translate_os_error``),
while the producers need all of them. Like the planner, importing this module currently
reaches ``botocore.exceptions`` through the backends; that timing is not an
interface guarantee.
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING

from boto3_s3 import requestparams, transferplan
from boto3_s3.comparator import SrcOnlyPair, SyncPair
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
    from collections.abc import Callable, Generator, Iterator
    from typing import BinaryIO


def walk_source_scan_options(
    storage: Storage,
    *,
    recursive: bool,
    sort: bool = False,
    on_warning: Callable[[str], None] | None,
    item_filter: FileFilter | None,
    wait_on_interrupt: bool,
) -> ScanOptions:
    """Scan options for a walkable transfer source (upload / sync side).

    Built from the storage's own ``default_scan_options`` - which seeds the
    source-config held on the instance (``LocalStorage``'s ``follow_symlinks`` /
    ``detect_symlink_loops`` / ``enumerate_all_entries``, a
    custom backend's own knobs, all configured on the constructor) - with only
    the run-level knobs overlaid: the operation-inherent ones (``recursive`` /
    ``sort`` / ``on_warning`` / the item ``filter``) and the application's
    Ctrl-C posture (``wait_on_interrupt``, declared once on
    ``S3(wait_on_interrupt=...)``). So a ``LocalStorage`` subclass,
    or any custom source backend whose ``scan_pages`` requires its own
    ``ScanOptions`` subclass, is honored here exactly as an arg-less ``scan()``
    would honor it. An S3 source never comes here (it lists through
    ``scan_s3_source``).

    Source settings are never narrowed here. In particular,
    ``enumerate_all_entries=True`` lets the operation's item filter observe every
    native entry. The caller is responsible for filtering out directories,
    special files, or other entries the transfer route cannot consume; survivors
    reach the operation and may fail, block, have device side effects, or be
    deleted by ``mv`` / ``sync --delete`` according to that operation's normal
    behavior. The default ``False`` preserves aws-cli transfer enumeration.
    """
    options = replace(
        storage.default_scan_options(),
        recursive=recursive,
        sort=sort,
        on_warning=on_warning,
        filter=item_filter,
        wait_on_interrupt=wait_on_interrupt,
    )
    return options


# S3's multipart ceiling: 10000 parts x 5 GiB. aws warns (without skipping)
# for larger uploads; the size string is aws-cli's rendered constant
# (human_readable_size(MAX_UPLOAD_SIZE)).
_MAX_UPLOAD_SIZE = 5 * 1024**3 * 10000
_MAX_UPLOAD_SIZE_TEXT = "48.8 TiB"

# Storage classes the glacier gate blocks unless restored or forced.
_GLACIER_STORAGE_CLASSES = ("GLACIER", "DEEP_ARCHIVE")


def _compare_key(info: FileInfo) -> str:
    """The producer-stamped ``compare_key``, narrowed to ``str``.

    Every ``Storage.scan`` (listing) and ``Storage.get_fileinfo`` (single) entry
    carries it (the single-object HEAD path stamps it too), so a transfer reads it
    instead of re-deriving it. ``None`` would mean a producer skipped the stamp -
    a bug.
    """
    key = info.compare_key
    assert key is not None, "compare_key must be stamped before transfer"
    return key


def _single_source_info(
    plan: transferplan.TransferPlan, transferrer: Transferrer
) -> FileInfo | None:
    """Resolve the single (non-dir_op) source through ``get_fileinfo``.

    Stamps the producing backend as a safety net (mirroring ``Storage.scan``'s):
    the built-ins stamp their own ``get_fileinfo`` results, so this only fills a
    ``None`` left by a bespoke backend - keeping ``src_info.storage`` in
    agreement with ``OpResult.src_storage`` on the single-object routes too.
    """
    single = plan.src.get_fileinfo(on_warning=transferrer.warner.warn)
    if single is not None and single.storage is None:
        single.storage = plan.src
    return single


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
        # Carry the listing entry like the no_overwrite skip does, so an
        # on_result consumer reads the same record shape from both silent
        # skips (the destination fields stay unset - the gate runs before
        # the destination is derived).
        transferrer.skip(
            TransferItem(
                compare_key=compare_key,
                size=info.size,
                mtime=info.mtime,
                src_info=info,
                src_display=src_display,
            )
        )
    else:
        transferrer.warner.warn(_glacier_warning(src_display, transfer_type), key=compare_key)
    return True


class CaseConflictGate:
    """The ``--case-conflict`` decision for recursive downloads.

    aws builds this from sync machinery (a destination listing paired by a
    comparator): a source file whose compare key exists at the destination
    with the **exact same case** always transfers (``AlwaysSync`` - cp
    overwrites); everything else runs the conflict check
    (``CaseConflictSync``): a conflict exists when another file with the
    same lowercased name was already admitted in this run, or the
    destination path exists (on a case-insensitive filesystem a
    case-variant satisfies ``os.path.exists``; an exact-case file that
    appeared after the membership pre-scan trips it on any filesystem). The
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

        Mirrors aws-cli's ``CaseConflictSync``: the lowercased key joins the
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
    item_filter: FileFilter | None,
    wait_on_interrupt: bool,
) -> Generator[TransferItem, None, None]:
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
            walk_source_scan_options(
                plan.src,
                recursive=True,
                on_warning=transferrer.warner.warn,
                item_filter=item_filter,
                wait_on_interrupt=wait_on_interrupt,
            )
        )
    else:
        # Single source object: the get_fileinfo point op (the scan
        # counterpart). None = warned-away (special/unreadable) or absent.
        # get_fileinfo has no filter, so an excluded single source is dropped here.
        single = _single_source_info(plan, transferrer)
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
    compare_key = _compare_key(info)
    dest = transferplan.dest_for(plan, compare_key)
    if info.size is not None and info.size > _MAX_UPLOAD_SIZE:
        # aws-cli's _warn_if_too_large: warn (rendered relative) but
        # still attempt, so S3's own EntityTooLarge stays visible.
        transferrer.warner.warn(
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
    item_filter: FileFilter | None,
    options: TransferOptions,
    case_gate: CaseConflictGate | None = None,
    operation: str,
    wait_on_interrupt: bool,
) -> Generator[TransferItem, None, None]:
    """Materialize download/copy items from an S3 source, gates applied."""
    src_bucket = src_storage.bucket
    if plan.dir_op:
        infos: Iterator[FileInfo] = scan_s3_source(
            src_storage,
            key_prefix=plan.src_root[len(src_bucket) + 1 :],
            item_filter=item_filter,
            options=options,
            wait_on_interrupt=wait_on_interrupt,
        )
    elif src_storage.bucket and not src_storage.key:
        # Keyless non-recursive source (`cp s3://bucket .`): aws lists the
        # bucket and exact-matches nothing -> zero items, rc 0. Preserve the
        # listing because its failure (notably AccessDenied) is observable.
        # A bucketless service root (`cp s3://`) is NOT this case: it falls to
        # head_single, whose empty Bucket hits botocore's Invalid-bucket
        # ParamValidation like aws.
        scan_options = replace(
            src_storage.default_scan_options(),
            # BucketLister does not send a Delimiter on this aws-cli route,
            # even though the transfer itself is non-recursive.
            recursive=True,
            prefix="",
            request_payer=options.get("request_payer"),
            wait_on_interrupt=wait_on_interrupt,
        )
        infos = (
            info
            for info in src_storage.scan(scan_options)
            if f"{src_bucket}/{info.key}" == src_bucket
        )
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
    compare_key = _compare_key(info)
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
        transferrer.warner.warn(
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
    compare_key = _compare_key(info)
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
    item_filter: FileFilter | None,
    options: TransferOptions,
    wait_on_interrupt: bool,
) -> Iterator[FileInfo]:
    """A recursive object listing anchored at the '/'-normalized ``key_prefix``.

    The shared transfer-side enumeration: cp/mv scan their source here,
    and sync scans whichever of its sides is S3 (the destination too).
    Folder markers never surface; the scan stamps each entry's ``Prefix``-relative
    ``compare_key``, which ``item_filter`` matches against. Built from the passed
    ``storage``'s own ``default_scan_options`` (so its ``page_size`` / ``fetch_owner``
    config and a custom ``S3Storage`` subclass survive), with the run-level
    knobs overlaid - the ``prefix`` re-anchoring the listing at the normalized
    ``key_prefix``, ``request_payer`` from the transfer options, and the
    application's Ctrl-C posture (``wait_on_interrupt``).
    """

    def scan_filter(info: FileInfo) -> bool:
        # Zero-byte '/'-terminated "folder marker" objects never transfer
        # (aws-cli filegenerator); the user filter prunes what remains.
        if info.size == 0 and info.key.endswith("/"):
            return False
        if item_filter is None:
            return True
        return item_filter(info)

    scan_options = replace(
        storage.default_scan_options(),
        recursive=True,
        prefix=key_prefix,
        filter=scan_filter,
        request_payer=options.get("request_payer"),
        wait_on_interrupt=wait_on_interrupt,
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
        # Chain the originating ClientError directly: this aws-worded error
        # replaces the s3_errors translation wholesale, and the section 2.1
        # reachability guarantee (exceptions.md) promises the ClientError on
        # the *direct* `__cause__` of an error raised for a failed S3 request.
        raise NotFoundError(
            "An error occurred (404) when calling the HeadObject operation: "
            f'Key "{key}" does not exist',
            operation=operation,
            bucket=src_storage.bucket,
            key=key,
        ) from (exc.__cause__ or exc)
    etag = head.get("ETag")
    # A single (non-dir_op) source: the compare key is the key's basename,
    # matching transferplan.item_paths' single-item branch. storage stamps the
    # producing backend like every listing path, so src_info.storage agrees
    # with OpResult.src_storage and a filter can reach the backend.
    yield S3FileInfo(
        key=key,
        size=head.get("ContentLength"),
        mtime=head.get("LastModified"),
        etag=etag.strip('"') if etag else None,
        storage_class=head.get("StorageClass"),
        head=head,
        compare_key=key.rsplit("/", 1)[-1],
        storage=src_storage,
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
    declare ``SORTABLE_SCAN`` (an unsorted side would manufacture phantom
    new/delete pairs - with ``--delete``, destination corruption). On top of
    that: an ``opens3`` source needs ``OPEN_READ``; an ``s3open`` destination
    needs ``OPEN_WRITE`` plus ``DELETE`` when ``delete`` removes orphans (the
    ``opens3`` orphans are S3, deleted without the custom side).
    """
    if plan.paths_type == "opens3":
        custom: Storage = plan.src
        needed = StorageCapability.SORTABLE_SCAN | StorageCapability.OPEN_READ
    elif plan.paths_type == "s3open":
        custom = plan.dest
        needed = StorageCapability.SORTABLE_SCAN | StorageCapability.OPEN_WRITE
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
    item_filter: FileFilter | None,
    operation: str,
    dryrun: bool,
    wait_on_interrupt: bool,
) -> Generator[TransferItem, None, None]:
    """Upload items from a custom source: each entry's bytes via ``open("rb")``.

    The open-route mirror of ``upload_items`` - a recursive source
    enumerates through ``plan.src.scan``, a single source resolves through
    ``plan.src.get_fileinfo`` - but the source has no local path: the engine
    reads its ``Storage.open(key, "rb")``. The open key is the entry's
    ``compare_key`` for a recursive item, ``""`` (the location itself) for a
    single one; the producer stamps each ``compare_key``, from which the
    filter and the S3 destination key derive. A ``dryrun`` enumerates but
    does not ``open`` the source (no side effect on the backend).

    A single source the backend cannot resolve (``get_fileinfo`` -> ``None``)
    raises the same ``NotFoundError`` a missing local source does (aws's wording,
    no ``ClientError`` cause) - rather than transferring nothing - but from inside
    the generator (in-pipeline, as the items are drained), not a pre-flight check,
    so a CLI-type consumer maps it to a fatal error (rc 1), not the pre-check's
    rc 255. A recursive source is an enumeration: an empty ``scan`` yields zero
    items (rc 0), matching an empty S3 prefix listing.
    """
    if plan.dir_op:
        # The recursive listing filters inside the scan (the scan_pages contract),
        # so a custom source backend can push the predicate to its source.
        infos: Iterator[FileInfo] = plan.src.scan(
            walk_source_scan_options(
                plan.src,
                recursive=True,
                on_warning=transferrer.warner.warn,
                item_filter=item_filter,
                wait_on_interrupt=wait_on_interrupt,
            )
        )
    else:
        single = _single_source_info(plan, transferrer)
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
        yield open_upload_item(
            plan, info, dest_bucket=dest_bucket, transferrer=transferrer, dryrun=dryrun
        )


class _DeferredReader:
    """Lazy ``Storage.open(key, "rb")``: the backend opens on the first read.

    Built when the item is, opened when s3transfer actually starts consuming
    this item's bytes. Exposing only ``read`` routes s3transfer to its
    non-seekable upload path, which reads the source sequentially on the
    bounded submission stage (`max_in_memory_upload_chunks` supplies the
    backpressure), so a recursive run over a custom backend holds open
    handles only for the items being read (~submission concurrency) - not
    one per queued item, which crossed ``RLIMIT_NOFILE`` around a thousand
    queued entries. An open/read failure surfaces inside the transfer task
    as that item's per-item failure (the capability gate's documented
    contract: runtime errors stay per-item). ``close`` is idempotent and
    closes only what was opened - an item that never read (failed, cancelled)
    leaves the backend untouched; after ``close``, ``read`` returns ``b""``.
    """

    def __init__(self, storage: Storage, key: str, size: int | None) -> None:
        self._storage = storage
        self._key = key
        self._size = size
        self._fileobj: BinaryIO | None = None
        self._closed = False

    def read(self, amt: int | None = None) -> bytes:
        if self._closed:
            return b""
        if self._fileobj is None:
            self._fileobj = self._storage.open(self._key, "rb", size=self._size)
        return self._fileobj.read() if amt is None else self._fileobj.read(amt)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._fileobj is not None:
            self._fileobj.close()


class _DeferredWriter:
    """Lazy ``Storage.open(key, "wb")``: the backend opens on the first write.

    Built when the item is, opened when this item's first downloaded chunk
    actually arrives (one plain GetObject below the multipart threshold,
    ranged GETs above it). Exposing only ``write`` routes s3transfer to its
    non-seekable download path, which writes chunks strictly in order (the
    ordering stdout gets), so a custom destination receives one sequential
    stream and a queued-but-unstarted item holds no open writer. ``close``
    commits: a successful download that wrote nothing (a zero-byte object)
    still materializes the empty object by opening on close. ``discard`` -
    preferred by the failure-path close (`_CloseFileobj` / the submit-error
    cleanup) - closes only what was opened, so an item that failed or was
    cancelled before its first write leaves the backend untouched.
    """

    def __init__(self, storage: Storage, key: str, size: int | None) -> None:
        self._storage = storage
        self._key = key
        self._size = size
        self._fileobj: BinaryIO | None = None
        self._closed = False

    def _ensure_open(self) -> BinaryIO:
        if self._fileobj is None:
            self._fileobj = self._storage.open(self._key, "wb", size=self._size)
        return self._fileobj

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("write to a closed deferred writer")
        return self._ensure_open().write(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ensure_open().close()

    def discard(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._fileobj is not None:
            self._fileobj.close()


def open_upload_item(
    plan: transferplan.TransferPlan,
    info: FileInfo,
    *,
    dest_bucket: str,
    transferrer: Transferrer,
    dryrun: bool,
) -> TransferItem:
    """One upload item reading the custom source through ``Storage.open``.

    ``dryrun`` builds no reader at all; a live run hands the engine a
    `_DeferredReader`, so the backend opens only when the transfer actually
    starts reading - never at queue time.
    """
    compare_key = _compare_key(info)
    open_key = compare_key if plan.dir_op else ""
    dest = transferplan.dest_for(plan, compare_key)
    if info.size is not None and info.size > _MAX_UPLOAD_SIZE:
        # The open-route mirror of upload_item_from_info's oversize warning
        # (aws-cli's _warn_if_too_large): warn but still attempt, rendered
        # through the open side's own display form.
        transferrer.warner.warn(
            f"File {open_side_display(plan.src, open_key)} exceeds s3 upload limit of "
            f"{_MAX_UPLOAD_SIZE_TEXT}.",
            key=compare_key,
        )
    return TransferItem(
        compare_key=compare_key,
        size=info.size,
        mtime=info.mtime,
        # src_info carries the source listing entry so an mv can delete it via
        # the backend's Storage.delete(info), addressed by the entry's own full
        # info.key. The open key is separate: the relative compare_key for a
        # recursive item, "" (the location itself) for a single source; the
        # backend resolves each in its own key space.
        src_info=info,
        src_fileobj=None if dryrun else _DeferredReader(plan.src, open_key, info.size),
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
    item_filter: FileFilter | None,
    options: TransferOptions,
    operation: str,
    dryrun: bool,
    wait_on_interrupt: bool,
) -> Generator[TransferItem, None, None]:
    """Download items from an S3 source into a custom destination's ``open("wb")``.

    The open-route mirror of the S3-source half of ``s3_source_items``:
    the source is enumerated identically (``scan_s3_source`` for a recursive
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
            item_filter=item_filter,
            options=options,
            wait_on_interrupt=wait_on_interrupt,
        )
    elif src_storage.bucket and not src_storage.key:
        # Keyless non-recursive source (`cp s3://bucket custom`): mirror the
        # built-in download path (s3_source_items) - aws lists the bucket and
        # exact-matches nothing -> zero items, rc 0. The listing is issued,
        # not skipped, because its failure (notably NoSuchBucket/AccessDenied)
        # is observable. A bucketless service root (`cp s3:// custom`) instead
        # falls to head_single, whose empty Bucket hits botocore's
        # Invalid-bucket ParamValidation like aws.
        scan_options = replace(
            src_storage.default_scan_options(),
            # BucketLister does not send a Delimiter on this aws-cli route,
            # even though the transfer itself is non-recursive.
            recursive=True,
            prefix="",
            request_payer=options.get("request_payer"),
            wait_on_interrupt=wait_on_interrupt,
        )
        infos = (
            info
            for info in src_storage.scan(scan_options)
            if f"{src_bucket}/{info.key}" == src_bucket
        )
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

    ``dryrun`` builds no writer at all; a live run hands the engine a
    `_DeferredWriter`, so the backend opens only when the item's first chunk
    actually arrives - never at queue time, and never for an item that fails
    before its first write.
    """
    compare_key = _compare_key(info)
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
        dest_fileobj=None if dryrun else _DeferredWriter(plan.dest, open_key, info.size),
        src_display=src_display,
        dest_display=open_side_display(plan.dest, open_key),
    )


def cp_case_gate(
    plan: transferplan.TransferPlan,
    *,
    recursive: bool,
    options: TransferOptions,
    transferrer: Transferrer,
    item_filter: FileFilter | None,
    operation: str,
    wait_on_interrupt: bool,
) -> CaseConflictGate | None:
    """Build the ``--case-conflict`` gate when it applies (aws-cli scope:
    recursive S3->local with a mode other than ``ignore``).

    The destination tree is enumerated up front (through ``plan.dest.scan``)
    into the exact-case membership set the gate's AlwaysSync arm consults -
    the observable equivalent of aws's reverse file generator + comparator.
    That reverse enumeration is a full walk of the destination side: aws
    builds it with the run's ``follow_symlinks`` parameter, routes its
    warnings into the shared result queue (they count toward rc 2), and runs
    the ``--exclude`` / ``--include`` filters over it - so the membership scan
    here reads the destination storage's own source-config
    (``default_scan_options``), warns into the transfer rollup, and applies
    the run's ``item_filter``, exactly like a source walk. Each entry's
    stamped ``compare_key`` is the membership key. Scoped to
    ``s3local`` (a local-filesystem destination - the gate exists for
    case-insensitive filesystems but runs for every local destination
    without probing the filesystem's case behavior, matching aws); a custom
    ``s3open`` destination owns its own key space, so it never scans the
    custom side here.
    """
    mode = CaseConflictMode(options.get("case_conflict", CaseConflictMode.IGNORE))
    if plan.paths_type != "s3local" or not recursive or mode is CaseConflictMode.IGNORE:
        return None
    # paths_type == "s3local" guarantees a LocalStorage destination here.
    dest_keys = {
        _compare_key(info)
        for info in plan.dest.scan(
            walk_source_scan_options(
                plan.dest,
                recursive=True,
                on_warning=transferrer.warner.warn,
                item_filter=item_filter,
                wait_on_interrupt=wait_on_interrupt,
            )
        )
    }
    return CaseConflictGate(mode, dest_keys, operation=operation)


def sync_case_gate(
    transfer_type: TransferType, dest_storage: Storage, *, options: TransferOptions
) -> CaseConflictGate | None:
    """The ``--case-conflict`` gate for sync downloads to a local destination.

    Unlike cp's (which pre-lists the destination for its exact-case
    AlwaysSync arm), sync already pairs against the destination listing:
    the gate is consulted only for ``SrcOnlyPair``s (keys missing there), so
    the membership set is vacuously empty and only the submitted-set /
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
    options: TransferOptions,
    wait_on_interrupt: bool,
) -> Generator[tuple[str, FileInfo], None, None]:
    """One side's ``(compare_key, info)`` stream, visibility applied.

    A local side walks in aws-cli byte order with its warnings routed to
    the transfer rollup (both sides warn, aws parity); an S3 side is the
    shared anchored listing (folder markers dropped). Each side reads its own
    source-config from its storage (a local side's ``follow_symlinks``, an S3
    side's ``page_size`` / ``fetch_owner``) via ``default_scan_options``, so both
    the source and the destination listing honor the storage each was built with -
    aws-cli maps the same knobs onto the destination listing too. Both sides'
    producers stamp ``compare_key`` (the merge-join axis), so ``item_filter`` reads
    it and the pair key is taken from it.
    """
    if isinstance(storage, S3Storage):
        key_prefix = root[len(storage.bucket) + 1 :]
        for info in scan_s3_source(
            storage,
            key_prefix=key_prefix,
            item_filter=item_filter,
            options=options,
            wait_on_interrupt=wait_on_interrupt,
        ):
            yield _compare_key(info), info
        return
    for info in storage.scan(
        walk_source_scan_options(
            storage,
            recursive=True,
            sort=True,  # the merge-join needs both sides byte-ordered
            on_warning=transferrer.warner.warn,
            item_filter=item_filter,  # each side's visibility filter, applied in the scan
            wait_on_interrupt=wait_on_interrupt,
        )
    ):
        yield _compare_key(info), info


def sync_transfer_item(
    plan: transferplan.TransferPlan,
    pair: SrcOnlyPair | SyncPair,
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
    if plan.paths_type == "opens3":
        item = open_upload_item(
            plan, info, dest_bucket=dest_bucket, transferrer=transferrer, dryrun=dryrun
        )
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
        # The case-conflict gate guards only SrcOnlyPairs - keys missing at the
        # destination (the aws-cli strategy slot); an exact-key update
        # never conflicts.
        gate = case_gate if isinstance(pair, SrcOnlyPair) else None
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
        item.dest_info = pair.dest if isinstance(pair, SyncPair) else None
    return item


# Package-internal: the shared producer/gate helpers are consumed by s3.py
# only and carry no documented surface (docs/imports.md).
__all__: list[str] = []
