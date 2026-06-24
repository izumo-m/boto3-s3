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
(``transfer.py``); ``S3._run_transfer`` (``s3.py``) routes those. A **stream**
side is an ``IOStorage`` / ``StdioStorage`` (``iostorage.py``): ``cp`` hands
``s3transfer`` the fileobj its ``open`` returns, so the ``open``-based transfer
path is wired for stream Storages. Still pending: ``S3Storage.open`` is not
implemented (``s3storage.py``), and ``_run_transfer`` still hard-asserts the two
built-in container types, so a ``Storage``-direct custom backend (e.g. an HTTP
one) cannot transfer yet. None of this affects CLI / ``aws s3`` parity (the CLI
only ever pairs a built-in with a stdio stream).
"""

from __future__ import annotations

import abc
import os
from collections.abc import Callable, Iterator, Sequence
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


class Storage(abc.ABC):
    """One side of a data location (local filesystem, an S3 bucket/prefix, ...).

    Implement :meth:`scan_pages`, :meth:`open`, :meth:`delete`, :meth:`as_text`,
    and set the :attr:`schema` discriminator to add a data source; :meth:`scan`
    (prefetch + flatten) is provided concretely on top of ``scan_pages``,
    :meth:`__str__` delegates to :meth:`as_text`, and :meth:`validate` is a no-op
    by default (override to reject a malformed location before an operation uses
    it). Built-in implementations are ``LocalStorage`` and ``S3Storage``.
    """

    #: Which built-in family this Storage belongs to - the object-layer
    #: discriminator ``naming.plan_transfer`` reads instead of re-parsing the
    #: scheme from a string. ``"s3"`` / ``"local"`` are the transferable container
    #: pair; ``"stream"`` is a stdio endpoint. Each concrete Storage sets it.
    schema: ClassVar[Literal["s3", "local", "stream"]]

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
        ``Delimiter='/'``). Entries come out in the backend's natural key order -
        for S3, UTF-8 lexicographic byte order, preserved across pages - so two
        recursive streams can be merge-joined on ``key`` (what ``sync`` relies on)
        after each is relativized to its scan root; ``key`` itself is the full,
        ``/``-separated identifier, so relativizing is the caller's job.
        """

    @abc.abstractmethod
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        """Open the object at ``key`` as a binary stream.

        ``"rb"`` returns a readable stream; ``"wb"`` a writable one whose
        ``close()`` commits the write. ``size`` is an optional total-length hint
        for writes (lets S3 choose single-part vs multipart up front). This is
        the generic per-object I/O primitive: built-in S3<->local transfers go
        through ``s3transfer`` instead, so ``open`` is the path intended for
        custom backends and for direct stream access. Current state: only
        ``LocalStorage`` implements it; ``S3Storage.open`` is not implemented
        yet and no transfer path calls ``open``, so that intended use is not
        wired (see this module's docstring and ``s3storage.py``).
        """

    @abc.abstractmethod
    def delete(self, key: str) -> None:
        """Delete the object at ``key`` (for ``rm`` / ``mv`` source / ``sync --delete``)."""

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


# A path argument accepted by the operation APIs: a string (``"s3://b/k"`` or a
# local path), any ``os.PathLike``, or a ``Storage`` instance (extension point).
Location = str | os.PathLike[str] | Storage


__all__ = [
    "Location",
    "Storage",
]
