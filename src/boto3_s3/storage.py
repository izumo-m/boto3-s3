"""Storage component: the abstract contract and the ``Location`` type.

``Storage`` is the abstract extension surface for one side of a data location.
It has four operations - ``scan`` (enumerate), ``get_fileinfo`` (resolve a
single entry), ``open`` (read/write a single object as a binary stream),
``delete`` - plus ``as_text`` (its canonical aws-cli path-shape token,
the inverse of ``S3.resolve``) and the ``Location`` type.
Built-in implementations are ``S3Storage`` and ``LocalStorage``; turning a
``Location`` (string / path) into a concrete ``Storage`` is ``S3.resolve``'s
job, the customization seam for adding URL schemes.

Design intent (the extension goal): an application adds a data source (e.g. an
HTTP backend) by subclassing ``Storage`` and implementing ``scan_pages`` /
``get_fileinfo`` / ``open`` / ``delete``, so it can act as one side of a ``cp`` /
``mv`` / ``sync`` transfer - the other side always S3 - with ``open`` the generic
stream the transfer engine reads/writes for a non-built-in side, ``scan``
(provided concretely on top of ``scan_pages``) enumerating it as a source,
``get_fileinfo`` resolving a single source entry, and
``delete`` removing entries for ``mv`` / ``sync --delete``. The S3-only
operations (``ls`` / ``rm`` / ``mb`` / ``rb`` / ``presign`` / ``website``) are
**not** part of this seam: each needs an S3 bucket and accepts only an
``S3Storage`` (a subclass customizing S3 behavior included), never an arbitrary
``Storage``.

Current state (kept accurate because this docstring is the design record): the
built-in ``S3Storage`` <-> ``LocalStorage`` pairs transfer through ``s3transfer``
directly off ``S3Storage``'s client/bucket and ``LocalStorage``'s path
(``transfer.py``); ``S3._run_transfer`` (``s3.py``) routes those. A custom
backend (any non-built-in ``scheme``) - and the ``IOStorage`` / ``StdioStorage``
stream wrappers (``iostorage.py``) - instead ride the **open route**: ``cp`` /
``mv`` / ``sync`` move the non-built-in side's bytes through its ``Storage.open``
(``opens3`` uploads each ``open("rb")`` to S3, ``s3open`` downloads each S3
object into an ``open("wb")`` whose ``close`` flushes it) while the S3 side rides
``s3transfer``; the custom side is capability-checked up front
(``Storage.capabilities``), an ``mv`` removes a custom source through its own
``delete``, and ``sync`` works when the custom side declares ``SORTABLE_SCAN`` (its
merge-join needs both listings byte-ordered) (transfer.md section 12).
``S3Storage.open`` implements ``"rb"`` only (a ``GetObject`` read convenience);
its ``"wb"`` stays unimplemented, since the S3 side of a transfer always rides
``s3transfer``. None of this affects CLI / ``aws s3`` parity (the CLI only ever
pairs a built-in with a stdio stream).
"""

from __future__ import annotations

import abc
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from enum import Flag, auto
from typing import Any, BinaryIO, ClassVar, Literal

from boto3_s3.concurrency import prefetch
from boto3_s3.types import CancelToken, FileInfo, ScanOptions


def sieve_pages(
    pages: Iterator[Sequence[FileInfo]], keep: Callable[[FileInfo], bool]
) -> Iterator[list[FileInfo]]:
    """Apply ``keep`` to each page; drop pages sieved empty (a ``scan_pages`` helper).

    The building block a ``Storage.scan_pages`` implementation uses to honor
    ``options.filter`` when it cannot push the predicate to the source: wrap the
    raw page stream with it. Lazy, so ``prefetch``'s worker drives it - the
    predicate runs on the worker thread and excluded entries (and pages emptied
    by it) never enter the hand-off queue.
    """
    for page in pages:
        kept = [info for info in page if keep(info)]
        if kept:
            yield kept


class StorageCapability(Flag):
    """Which transfer operations a ``Storage`` *kind* actually implements.

    A declarative, class-level mirror of the contract methods, checked **before**
    a transfer so an unsupported pairing fails with a clear error instead of a
    deep ``NotImplementedError`` mid-flight. This is *capability* - whether the
    Storage kind implements the operation at all - **not** *permission* (whether a
    particular target is writable / reachable right now); a denied write or a
    missing object stays an execution-time error (an S3 ``403``, a local
    ``EACCES`` / ``ENOENT``), reported per item like aws-cli, never pre-screened.

    The members mirror the methods one-to-one, because support genuinely differs
    per kind (``S3Storage`` reads via ``open("rb")`` but does not write via
    ``open("wb")`` - S3 writes ride ``s3transfer``; a single-URL backend reads one
    object but cannot enumerate):

    - ``OPEN_READ`` / ``OPEN_WRITE`` - ``open(key, "rb")`` / ``open(key, "wb")``
    - ``GET_FILEINFO`` - ``get_fileinfo(key)``, resolving a single entry
    - ``SCAN`` - ``scan`` / ``scan_pages``, enumerating a container
    - ``SORTABLE_SCAN`` - ``scan`` yields keys in UTF-8 byte order when asked
      (``ScanOptions(sort=True)``); ``sync``'s merge-join needs it on both sides,
      and gates on this capability for a custom side
    - ``DELETE`` - ``delete(info)`` (``rm`` / ``mv`` source / ``sync --delete``)

    The reading members form a lattice (expanded by ``_implied``):
    ``SORTABLE_SCAN`` implies ``SCAN`` implies ``GET_FILEINFO`` (ordered enumeration
    implies enumeration implies single-entry resolution), so a backend need only
    declare its strongest one.
    """

    OPEN_READ = auto()
    OPEN_WRITE = auto()
    GET_FILEINFO = auto()
    SCAN = auto()
    SORTABLE_SCAN = auto()
    DELETE = auto()


def _implied(caps: StorageCapability) -> StorageCapability:
    """Expand ``StorageCapability``'s reading lattice for a containment test.

    ``SORTABLE_SCAN`` -> ``SCAN`` -> ``GET_FILEINFO``, so a backend that declares
    only its strongest reading capability still satisfies a check for a weaker
    one. Applied wherever capabilities are tested, never to the stored value.
    """
    if caps & StorageCapability.SORTABLE_SCAN:
        caps |= StorageCapability.SCAN
    if caps & StorageCapability.SCAN:
        caps |= StorageCapability.GET_FILEINFO
    return caps


class Storage(abc.ABC):
    """One side of a data location (local filesystem, an S3 bucket/prefix, ...).

    Implement ``scan_pages``, ``open``, ``delete``,
    ``get_fileinfo``, ``as_text``, set the ``scheme`` discriminator,
    and declare ``capabilities`` (which of those operations actually work) to
    add a data source; ``scan`` (prefetch + flatten) is provided concretely
    on top of ``scan_pages``, ``__str__`` delegates to ``as_text``, and
    ``validate`` is a no-op by default (override to reject a malformed
    location before an operation uses it). Built-in implementations are
    ``LocalStorage`` and ``S3Storage``.

    Class attributes (a concrete backend sets / overrides these):

    ``scheme`` - which storage family this is, a display/classification label
    (result rendering uses it). ``"s3"`` and ``"local"`` are the built-in pair; any
    other value is a non-built-in backend (a custom one, or a stdio stream).
    Transfer *routing* does not read it: the planner routes by concrete type (the
    structural match in ``transferplan._paths_type``). Each concrete Storage sets
    its own token.

    ``sep`` - the separator of this backend's path space, as it appears in
    formatted roots (``format``) and item keys. ``"/"`` for S3, streams, and
    custom backends (``FileInfo.key`` / ``compare_key`` are ``/``-separated by
    contract); ``LocalStorage`` overrides with the host ``os.sep``.

    ``capabilities`` - which transfer operations this Storage *kind* implements
    (``StorageCapability``): the structural, class-level contract a transfer
    pre-checks for a custom side - distinct from runtime *permission* (a denied
    write / missing target is an execution-time error, not modeled here). The
    default declares nothing (fail-closed); each concrete Storage overrides it, and
    a subclass may narrow or widen it.

    ``scan_options_type`` - this backend's ``ScanOptions``
    type. ``scan()`` with no options builds it (via ``default_scan_options``),
    so a backend whose ``scan_pages`` requires its own subclass still works
    arg-less. The base is a plain ``ScanOptions``; ``S3Storage``
    / ``LocalStorage`` set ``S3ScanOptions`` /
    ``LocalScanOptions``. A custom backend that defines its own
    subclass just sets this one attribute - no method to override. (A backend that
    takes the base ``ScanOptions`` needs nothing here.)

    ``scan_pages_filters`` - whether this backend's ``scan_pages`` already applies
    ``options.filter`` itself. When ``False`` (default), ``scan`` applies it as
    a safety net after ``scan_pages`` (on the prefetch worker), so a custom backend
    that forgets to filter cannot silently leak excluded entries into ``--exclude``
    / ``--include`` and, on a ``sync --delete`` destination, into deletion. The
    built-ins set ``True`` (their ``scan_pages`` filters: ``S3Storage`` sieves;
    ``LocalStorage``'s walk applies it - late, after the aws-cli vetting that still
    warns on excluded files, or early in a custom ``LocalFileGenerator``'s
    ``finalize_children``). A backend that filters at its source, or prunes early by
    calling ``options.filter`` itself, sets ``True`` to skip the redundant re-filter
    without re-implementing ``scan``.
    """

    scheme: ClassVar[str]

    sep: ClassVar[str] = "/"

    capabilities: ClassVar[StorageCapability] = StorageCapability(0)

    # Pages buffered ahead of the consumer by scan()'s prefetch worker (see
    # concurrency.prefetch). Subclasses may override to tune the buffer depth.
    _scan_prefetch_pages: ClassVar[int] = 4

    # Whether scan()'s context exit still waits for the prefetch worker when
    # the consumer is unwinding on a KeyboardInterrupt/SystemExit. True (the
    # default) keeps the no-surviving-worker contract for embedders; an app
    # that treats such an interrupt as process-fatal (the CLI, matching aws's
    # immediate death on Ctrl-C) sets False - via the built-ins' constructor
    # kwarg - so an in-flight page pull cannot delay the exit. Every other
    # exit always waits (concurrency.prefetch).
    scan_wait_on_interrupt: bool = True

    scan_options_type: ClassVar[type[ScanOptions]] = ScanOptions

    scan_pages_filters: ClassVar[bool] = False

    def default_scan_options(self) -> ScanOptions:
        """This backend's own ``ScanOptions``: used when ``scan()`` gets none,
        and the base an operation overlays its own knobs onto.

        The default builds ``scan_options_type`` with all field defaults. A
        backend that carries scan *source-config* on its instance - how this source
        is read, set once on the constructor (``LocalStorage``'s ``follow_symlinks``
        / ``detect_symlink_loops``, ``S3Storage``'s ``page_size`` / ``fetch_owner``)
        - overrides this to seed those held values, so every scan reflects the
        storage's configuration. The high-level ``cp`` / ``sync`` / ``ls`` / ``rm``
        paths build from this and overlay only the operation-inherent knobs
        (``recursive`` / ``sort`` / ``filter`` / ``on_warning`` / ``prefix``), which
        is what lets an app configure the walk through the storage rather than per
        call.
        """
        return self.scan_options_type()

    def scan(
        self,
        options: ScanOptions | None = None,
        *,
        cancel_token: CancelToken | None = None,
    ) -> Iterator[FileInfo]:
        """Yield the entries under this storage as a flat ``FileInfo`` stream.

        A concrete wrapper around ``scan_pages``: it flattens the per-page
        producer and overlaps it with a background ``prefetch`` worker, so the
        next page's I/O (an S3 ``ListObjectsV2`` round-trip, a local ``stat``
        batch) runs while the consumer handles the current page; a producer error
        surfaces on the consumer's pull. ``options.filter`` is applied here as a
        safety net (on the prefetch worker) **unless** the backend declares
        ``scan_pages_filters`` - so a custom backend that does not filter in
        ``scan_pages`` cannot silently leak excluded entries; a backend that
        filters at its source or prunes early declares the flag to skip the
        redundant re-filter. Pass ``options`` to control the walk (defaults to
        ``default_scan_options``). To customize the entries, override
        ``scan_pages``, not this method. Each yielded entry has its
        ``FileInfo.storage`` set to this backend as a safety net (only when a
        ``scan_pages`` left it ``None``); the built-ins stamp it during the scan so
        their filters see it too. Used by ``ls`` and the recursive forms of
        ``cp`` / ``mv`` / ``rm`` / ``sync``. `cancel_token` stops the prefetch
        producer before another page pull without changing entries already
        yielded to the consumer.
        """
        opts = options if options is not None else self.default_scan_options()
        pages = self.scan_pages(opts)
        if opts.filter is not None and not self.scan_pages_filters:
            pages = sieve_pages(pages, opts.filter)
        with prefetch(
            pages,
            queue_size=self._scan_prefetch_pages,
            cancel_token=cancel_token,
            wait_on_interrupt=self.scan_wait_on_interrupt,
        ) as items:
            for info in items:
                # Safety net: stamp the producing backend so a downstream consumer
                # (sync's content compare via pair.src.storage, an on_result
                # callback) can reach it even when a custom scan_pages did not set
                # it. The built-ins stamp it during the scan so their filters
                # already see it; this only fills a None left by a bespoke backend.
                if info.storage is None:
                    info.storage = self
                yield info

    @abc.abstractmethod
    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
        """Yield entries one natural I/O page at a time - the producer and override seam.

        This is the abstract method every backend implements and the public point
        to override: ``scan`` wraps it with prefetch and flattens it. Override
        it (calling ``super().scan_pages(options)``) to filter or enrich entries a
        page at a time - the work then runs on the prefetch worker, so the
        customized stream keeps the page-ahead overlap, cheaper than
        re-implementing ``scan``. **Apply ``options.filter`` here** and return
        already-filtered pages: whatever this producer omits is simply absent
        downstream (its ``sync`` effect is side-specific - the visibility layer in
        ``ScanOptions.filter`` / docs/globsieve.md). A backend that cannot push the
        predicate to its source wraps its raw pages with ``sieve_pages``; one
        that can (e.g. a REST listing) translates ``options.filter`` into a
        server-side query instead.

        ``options.recursive`` normally walks every transferable entry beneath the
        prefix (all ``FILE`` kind, no directory grouping); a backend-specific
        source setting may widen that view - local
        ``enumerate_all_entries=True`` includes the root, directories, symlinks,
        and special entries before filtering. Non-recursive normally yields the
        immediate entries plus one ``DIRECTORY``-kind ``FileInfo`` per
        sub-"directory" (S3 ``Delimiter='/'``). ``options.sort`` requests UTF-8 byte order (by
        ``compare_key``): a backend declaring ``SORTABLE_SCAN`` MUST honor it, so
        two recursive streams can be merge-joined (what ``sync`` relies on) after
        each is expressed as ``compare_key``; when ``sort`` is ``False``
        (``cp`` / ``mv`` / ``ls`` / ``rm``) the backend may yield its cheaper
        natural order.
        The built-ins always sort - S3's listing is byte-ordered (preserved across
        pages), the local walk sorts for aws parity - so they ignore the flag.
        ``key`` itself is the full, ``/``-separated identifier; stamp its relative
        form as ``compare_key`` on every entry this producer yields.
        """

    @abc.abstractmethod
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        """Open the object at ``key`` as a binary stream.

        ``"rb"`` returns a readable stream; ``"wb"`` a writable one whose
        ``close()`` flushes any buffered writes (standard file semantics).
        ``size`` is an optional total-length hint (the engine passes the entry's
        size on both ``"rb"`` and ``"wb"`` opens); a writing backend may use it to
        pre-allocate or choose its write strategy up front. This is
        the generic per-object I/O primitive: built-in S3<->local transfers go
        through ``s3transfer`` instead, so ``open`` is the path a custom backend
        (and a stream wrapper) transfers through. ``cp`` / ``mv`` call it for the
        non-built-in side of an open-route transfer (``opens3`` / ``s3open``,
        transfer.md section 12), handing the returned fileobj to ``s3transfer``
        and ``close``-ing it when done - a ``"wb"`` ``close`` flushes buffered
        writes like any file object (whether the backend persists per write or
        defers to the flush - write-through vs write-back - is its own choice).
        ``LocalStorage`` and the stream Storages (``IOStorage`` /
        ``StdioStorage``) implement both modes. ``S3Storage`` implements ``"rb"``
        only - a ``GetObject`` read convenience (chiefly for a content-based
        ``sync`` filter), addressed by the object's full key; its ``"wb"`` stays
        unimplemented, since every S3 write rides ``s3transfer`` rather than
        ``open``.
        """

    @abc.abstractmethod
    def delete(self, info: FileInfo) -> Mapping[str, Any] | None:
        """Delete the entry ``info`` identifies (``rm`` / ``mv`` source / ``sync --delete``).

        ``info`` is a listing entry (from ``scan`` / ``get_fileinfo``) or
        one built by hand; the backend locates the object by ``info.key`` in its
        own address space - a local absolute path, an S3 full key, or a custom
        backend's own key.

        May return the backend's delete response - surfaced under
        ``OpResult.extra_info["delete"]`` when the operation runs with
        ``capture_response=True`` - or ``None`` when there is none. A local unlink
        returns ``None``; ``S3Storage`` returns its ``DeleteObject`` response.
        """

    @abc.abstractmethod
    def get_fileinfo(
        self,
        key: str = "",
        *,
        on_warning: Callable[[str], None] | None = None,
    ) -> FileInfo | None:
        """Return the ``FileInfo`` for a single entry, or ``None`` if it is absent.

        The single-entry counterpart to ``scan`` (which enumerates): ``cp`` /
        ``mv`` use it for a single source object, and an existence check (e.g.
        ``--no-overwrite``) reads it for ``None``. ``key`` is relative to this
        storage's location: ``key=""`` (the default) is the location itself (the
        single source/dest the storage points at), a non-empty ``key`` an entry
        beneath it. The outcomes are uniform across backends:

        - present and transferable -> a ``FileInfo`` whose ``compare_key`` is the
          entry's basename;
        - **definitively absent** (an S3 ``404``, a local ``ENOENT`` / ``ENOTDIR``)
          -> ``None``, no warning;
        - present but not a transferable regular file (a local special device /
          FIFO / socket, or one that fails the readability probe) -> a message to
          ``on_warning`` and ``None`` (aws-cli's warn-and-skip, exit code 2);
        - **existence cannot be determined** (a permission error reaching it, a
          transport / 5xx error) -> the error is raised.

        So ``None`` means "no transferable entry here"; the caller decides what
        that means (a single source raises its own "does not exist"; an existence
        check proceeds). ``on_warning`` is the local-walk warning channel (ignored
        by S3). Whether a symlink is followed is the local backend's own
        construction-time config (``LocalStorage(follow_symlinks=...)``), read from
        the storage - not a parameter here.
        """

    @abc.abstractmethod
    def as_text(self) -> str:
        """Return this location's canonical aws-cli path-shape token.

        The inverse of ``S3.resolve``: an ``S3Storage`` yields
        ``s3://bucket/key`` (a keyless location stays slashless, ``s3://bucket``),
        a ``LocalStorage`` its path as given, so ``S3.resolve(s.as_text())``
        round-trips a locatable Storage. This is the form ``transferplan.plan_transfer``
        consumes and ``aws s3`` displays. A stream endpoint has no location, so
        its token (``"-"``) is display-only - not round-trippable.
        """

    def __str__(self) -> str:
        """Delegate to ``as_text`` so ``str(storage)`` is its path-shape token."""
        return self.as_text()

    def format(self, *, dir_op: bool) -> tuple[str, bool]:
        """Format this side of a transfer; return ``(root, use_src_name)``.

        The per-side half of aws-cli's ``FileFormat.format``, resolved
        polymorphically: ``S3Storage`` overrides with ``FileFormat.s3_format``,
        ``LocalStorage`` with ``FileFormat.local_format``, each reading its own
        held state. ``root`` is what item keys are resolved against
        (``transferplan.dest_for`` prefixes it to a ``compare_key`` on the side
        that adopts the source's name); ``use_src_name`` - read from the
        *destination* side - is whether it does.

        This default is the ``open``-route rule for a custom backend (aws-cli
        has no counterpart): the root is ``""`` because such a backend
        encapsulates its own location and addresses entries by their
        relative ``compare_key`` - its ``open`` / ``delete`` receive that key
        unprefixed. ``use_src_name`` mirrors the S3 rule: a
        ``dir_op`` or an explicit trailing ``/`` on ``as_text`` means the
        destination adopts the source's name.
        """
        return "", (dir_op or self.as_text().endswith("/"))

    def validate(self) -> None:  # noqa: B027 - deliberate concrete no-op; S3Storage overrides
        """Run any deferred strict validation on this location (no-op by default).

        Construction is permissive (a building block); an operation - or the CLI
        at its parity point - calls this to reject a malformed location loudly
        before use. ``S3Storage`` overrides it with the aws-cli-parity checks
        (unsupported ARN forms, a key with no bucket); built-ins that need none
        keep this no-op. Idempotent.
        """

    def supports(self, needed: StorageCapability) -> bool:
        """Whether this storage implements every capability in ``needed``.

        The capability lattice is applied first (see ``StorageCapability``),
        so e.g. a backend declaring ``SORTABLE_SCAN`` ``supports(SCAN)``. This is
        the structural pre-check a transfer runs on a custom (``open``-routed)
        side; a built-in S3 / local side rides ``s3transfer`` and is not gated.
        """
        have = _implied(self.capabilities)
        return (needed & have) == needed

    def missing_capabilities(self, needed: StorageCapability) -> StorageCapability:
        """The subset of ``needed`` this storage does not implement (empty if none).

        The companion to ``supports`` that names what is absent, for a clear
        rejection message; lattice-expanded the same way.
        """
        return needed & ~_implied(self.capabilities)


# A path argument accepted by the operation APIs: a string (``"s3://b/k"`` or a
# local path), any ``os.PathLike``, or a ``Storage`` instance (extension point).
Location = str | os.PathLike[str] | Storage


__all__ = [
    "Location",
    "Storage",
    "StorageCapability",
    "sieve_pages",
]
