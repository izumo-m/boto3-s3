"""Storage component: the abstract contract and the ``Location`` type.

``Storage`` is the abstract extension surface for one side of a data location.
It has three operations - :meth:`scan` (enumerate), :meth:`open` (read/write a
single object as a binary stream), :meth:`delete` - plus :meth:`as_text` (its
canonical aws-cli path-shape token, the inverse of :meth:`S3.resolve`) and the
``Location`` type.
Built-in implementations are ``S3Storage`` and ``LocalStorage``; turning a
``Location`` (string / path) into a concrete ``Storage`` is :meth:`S3.resolve`'s
job, the customization seam for adding URL schemes.

Design intent (the extension goal): an application adds a data source (e.g. an
HTTP backend) by subclassing ``Storage`` and implementing ``scan`` / ``open`` /
``delete``, so it can act as one side of a ``cp`` / ``mv`` / ``sync`` transfer -
the other side always S3 - with ``open`` the generic stream the transfer engine
reads/writes for a non-built-in side, ``scan`` enumerating it as a source, and
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
``mv`` move the non-built-in side's bytes through its ``Storage.open``
(``opens3`` uploads each ``open("rb")`` to S3, ``s3open`` downloads each S3
object into an ``open("wb")`` whose ``close`` commits it) while the S3 side rides
``s3transfer``; the custom side is capability-checked up front
(``Storage.capabilities``), and an ``mv`` removes a custom source through its own
``delete`` (transfer.md section 12). ``sync`` over a custom backend is not wired
yet. ``S3Storage.open`` stays unimplemented by design - the S3 side always rides
``s3transfer``, never ``open``. None of this affects CLI / ``aws s3`` parity (the
CLI only ever pairs a built-in with a stdio stream).
"""

from __future__ import annotations

import abc
import os
from collections.abc import Callable, Iterator, Sequence
from enum import Flag, auto
from typing import BinaryIO, ClassVar, Literal

from boto3_s3.concurrency import prefetch
from boto3_s3.types import FileInfo, ScanOptions


def _sieve_pages(
    pages: Iterator[Sequence[FileInfo]], keep: Callable[[FileInfo], bool]
) -> Iterator[list[FileInfo]]:
    """Apply ``keep`` to each page; drop pages sieved empty.

    Lazily wraps a :meth:`Storage.scan_pages` producer: :func:`prefetch`'s
    worker drives it, so the predicate runs on the worker thread and excluded
    entries - and emptied pages - never enter the hand-off queue.
    """
    for page in pages:
        kept = [info for info in page if keep(info)]
        if kept:
            yield kept


class StorageCapability(Flag):
    """Which transfer operations a :class:`Storage` *kind* actually implements.

    A declarative, class-level mirror of the contract methods, checked **before**
    a transfer so an unsupported pairing fails with a clear error instead of a
    deep ``NotImplementedError`` mid-flight. This is *capability* - whether the
    Storage kind implements the operation at all - **not** *permission* (whether a
    particular target is writable / reachable right now); a denied write or a
    missing object stays an execution-time error (an S3 ``403``, a local
    ``EACCES`` / ``ENOENT``), reported per item like aws-cli, never pre-screened.

    The members mirror the methods one-to-one, because support genuinely differs
    per kind (``S3Storage`` resolves one object via ``get_fileinfo`` yet does not
    implement ``open``; a single-URL backend reads one object but cannot
    enumerate):

    - ``OPEN_READ`` / ``OPEN_WRITE`` - ``open(key, "rb")`` / ``open(key, "wb")``
    - ``GET_FILEINFO`` - ``get_fileinfo(key)``, resolving a single entry
    - ``SCAN`` - ``scan`` / ``scan_pages``, enumerating a container
    - ``SORTED_SCAN`` - ``scan`` yields keys in UTF-8 byte order when asked
      (``ScanOptions(sort=True)``); ``sync``'s merge-join needs it on both sides,
      and gates on this capability for a custom side
    - ``DELETE`` - ``delete(key)`` (``rm`` / ``mv`` source / ``sync --delete``)

    The reading members form a lattice (expanded by :func:`_implied`):
    ``SORTED_SCAN`` implies ``SCAN`` implies ``GET_FILEINFO`` (ordered enumeration
    implies enumeration implies single-entry resolution), so a backend need only
    declare its strongest one.
    """

    OPEN_READ = auto()
    OPEN_WRITE = auto()
    GET_FILEINFO = auto()
    SCAN = auto()
    SORTED_SCAN = auto()
    DELETE = auto()


def _implied(caps: StorageCapability) -> StorageCapability:
    """Expand :class:`StorageCapability`'s reading lattice for a containment test.

    ``SORTED_SCAN`` -> ``SCAN`` -> ``GET_FILEINFO``, so a backend that declares
    only its strongest reading capability still satisfies a check for a weaker
    one. Applied wherever capabilities are tested, never to the stored value.
    """
    if caps & StorageCapability.SORTED_SCAN:
        caps |= StorageCapability.SCAN
    if caps & StorageCapability.SCAN:
        caps |= StorageCapability.GET_FILEINFO
    return caps


class Storage(abc.ABC):
    """One side of a data location (local filesystem, an S3 bucket/prefix, ...).

    Implement :meth:`scan_pages`, :meth:`open`, :meth:`delete`,
    :meth:`get_fileinfo`, :meth:`as_text`, set the :attr:`scheme` discriminator,
    and declare :attr:`capabilities` (which of those operations actually work) to
    add a data source; :meth:`scan` (prefetch + flatten) is provided concretely
    on top of ``scan_pages``, :meth:`__str__` delegates to :meth:`as_text`, and
    :meth:`validate` is a no-op by default (override to reject a malformed
    location before an operation uses it). Built-in implementations are
    ``LocalStorage`` and ``S3Storage``.
    """

    #: Which storage family this is - the object-layer discriminator
    #: ``naming.plan_transfer`` reads instead of re-parsing a scheme string
    #: (importing the concrete classes here would be circular). ``"s3"`` and
    #: ``"local"`` are the built-in transferable pair; any other value is a
    #: non-built-in backend (a custom one, or a stdio stream). Each concrete
    #: Storage sets its own token.
    scheme: ClassVar[str]

    #: Which transfer operations this Storage *kind* implements
    #: (:class:`StorageCapability`): the structural, class-level contract a
    #: transfer pre-checks for a custom side - distinct from runtime *permission*
    #: (a denied write / missing target is an execution-time error, not modeled
    #: here). The default declares nothing (fail-closed); each concrete Storage
    #: overrides it, and a subclass may narrow or widen it.
    capabilities: ClassVar[StorageCapability] = StorageCapability(0)

    # Pages buffered ahead of the consumer by scan()'s prefetch worker (see
    # concurrency.prefetch). Subclasses may override to tune the buffer depth.
    _scan_prefetch_pages: ClassVar[int] = 4

    def scan(self, options: ScanOptions | None = None) -> Iterator[FileInfo]:
        """Yield the entries under this storage as a flat ``FileInfo`` stream.

        A concrete wrapper around :meth:`scan_pages`: it flattens the per-page
        producer and overlaps it with a background :func:`prefetch` worker, so the
        next page's I/O (an S3 ``ListObjectsV2`` round-trip, a local ``stat``
        batch) runs while the consumer handles the current page; a producer error
        surfaces on the consumer's pull. ``options.filter`` is applied here, page
        by page on that same worker - excluded entries (and pages sieved empty)
        never cross the hand-off queue, and the predicate runs off the consumer's
        thread. Pass ``options`` to control the walk (defaults to
        ``ScanOptions()``). To customize the entries, override :meth:`scan_pages`,
        not this method. Used by ``ls`` and the recursive forms of ``cp`` /
        ``rm`` / ``sync``.
        """
        opts = options if options is not None else ScanOptions()
        pages: Iterator[Sequence[FileInfo]] = self.scan_pages(opts)
        if opts.filter is not None:
            pages = _sieve_pages(pages, opts.filter)
        with prefetch(pages, queue_size=self._scan_prefetch_pages) as items:
            yield from items

    @abc.abstractmethod
    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
        """Yield entries one natural I/O page at a time - the producer and override seam.

        This is the abstract method every backend implements and the public point
        to override: :meth:`scan` wraps it with prefetch and flattens it. Override
        it (calling ``super().scan_pages(options)``) to filter or enrich entries a
        page at a time - the work then runs on the prefetch worker, so the
        customized stream keeps the page-ahead overlap, cheaper than
        re-implementing :meth:`scan`. Yield raw pages: ``options.filter`` is
        :meth:`scan`'s concern, applied centrally downstream of this producer
        (and of any override), so implementations never check it.

        ``options.recursive`` walks every entry beneath the prefix (all ``FILE``
        kind, no directory grouping); non-recursive yields the immediate entries
        plus one ``DIRECTORY``-kind ``FileInfo`` per sub-"directory" (S3
        ``Delimiter='/'``). ``options.sort`` requests UTF-8 byte order (by
        ``compare_key``): a backend declaring ``SORTED_SCAN`` MUST honor it, so
        two recursive streams can be merge-joined (what ``sync`` relies on) after
        each is relativized to its scan root; when ``sort`` is ``False`` (``cp`` /
        ``mv`` / ``ls`` / ``rm``) the backend may yield its cheaper natural order.
        The built-ins always sort - S3's listing is byte-ordered (preserved across
        pages), the local walk sorts for aws parity - so they ignore the flag.
        ``key`` itself is the full, ``/``-separated identifier, so relativizing to
        the scan root is the caller's job.
        """

    @abc.abstractmethod
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        """Open the object at ``key`` as a binary stream.

        ``"rb"`` returns a readable stream; ``"wb"`` a writable one whose
        ``close()`` commits the write. ``size`` is an optional total-length hint
        for writes (lets S3 choose single-part vs multipart up front). This is
        the generic per-object I/O primitive: built-in S3<->local transfers go
        through ``s3transfer`` instead, so ``open`` is the path a custom backend
        (and a stream wrapper) transfers through. ``cp`` / ``mv`` call it for the
        non-built-in side of an open-route transfer (``opens3`` / ``s3open``,
        transfer.md section 12), handing the returned fileobj to ``s3transfer``
        and ``close``-ing it when done - a ``"wb"`` ``close`` is the write's
        commit point. ``LocalStorage`` and the stream Storages (``IOStorage`` /
        ``StdioStorage``) implement it; ``S3Storage.open`` stays unimplemented by
        design (the S3 side always rides ``s3transfer``, never ``open``).
        """

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """Delete the object at ``key`` (for ``rm`` / ``mv`` source / ``sync --delete``)."""

    @abc.abstractmethod
    def get_fileinfo(
        self,
        key: str = "",
        *,
        follow_symlinks: bool = True,
        on_warning: Callable[[str], None] | None = None,
    ) -> FileInfo | None:
        """Return the ``FileInfo`` for a single entry, or ``None`` if it is absent.

        The single-entry counterpart to :meth:`scan` (which enumerates): ``cp`` /
        ``mv`` use it for a single source object, and an existence check (e.g.
        ``--no-overwrite``) reads it for ``None``. ``key`` is relative to this
        storage's location: ``key=""`` (the default) is the location itself (the
        single source/dest the storage points at), a non-empty ``key`` an entry
        beneath it. The outcomes are uniform across backends:

        - present and transferable -> a ``FileInfo`` whose ``compare_key`` is the
          entry's basename;
        - **definitively absent** (an S3 ``404``, a local ``ENOENT``) -> ``None``,
          no warning;
        - present but not a transferable regular file (a local special device /
          FIFO / socket, or one that fails the readability probe) -> a message to
          ``on_warning`` and ``None`` (aws-cli's warn-and-skip, exit code 2);
        - **existence cannot be determined** (a permission error reaching it, a
          transport / 5xx error) -> the error is raised.

        So ``None`` means "no transferable entry here"; the caller decides what
        that means (a single source raises its own "does not exist"; an existence
        check proceeds). ``follow_symlinks=False`` skips a symlink; ``on_warning``
        is the local-walk warning channel (ignored by S3).
        """

    @abc.abstractmethod
    def as_text(self) -> str:
        """Return this location's canonical aws-cli path-shape token.

        The inverse of :meth:`S3.resolve`: an ``S3Storage`` yields
        ``s3://bucket/key`` (a keyless location stays slashless, ``s3://bucket``),
        a ``LocalStorage`` its path as given, so ``S3.resolve(s.as_text())``
        round-trips a locatable Storage. This is the form ``naming.plan_transfer``
        consumes and ``aws s3`` displays. A stream endpoint has no location, so
        its token (``"-"``) is display-only - not round-trippable.
        """

    def __str__(self) -> str:
        """Delegate to :meth:`as_text` so ``str(storage)`` is its path-shape token."""
        return self.as_text()

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

        The capability lattice is applied first (see :class:`StorageCapability`),
        so e.g. a backend declaring ``SORTED_SCAN`` ``supports(SCAN)``. This is
        the structural pre-check a transfer runs on a custom (``open``-routed)
        side; a built-in S3 / local side rides ``s3transfer`` and is not gated.
        """
        have = _implied(self.capabilities)
        return (needed & have) == needed

    def missing_capabilities(self, needed: StorageCapability) -> StorageCapability:
        """The subset of ``needed`` this storage does not implement (empty if none).

        The companion to :meth:`supports` that names what is absent, for a clear
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
]
