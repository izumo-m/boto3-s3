"""Local filesystem storage backend: ``LocalStorage``, ``LocalFileGenerator``, the walk.

:class:`LocalFileGenerator` is the customizable local directory walk - boto3-s3's
counterpart to aws-cli's ``FileGenerator`` (aws-cli's
awscli/customizations/s3/filegenerator.py), scoped to the local filesystem (the
S3 listing is :class:`~boto3_s3.s3storage.S3Storage`'s). :meth:`~LocalFileGenerator.list_files`
reproduces aws-cli's ``list_files`` observable behaviour: the same depth-first
traversal whose per-directory sort key appends ``os.sep`` to directory names and
compares with separators normalized to ``/`` - so the stream comes out in S3's
UTF-8 byte order (``foo.txt`` before ``foo/bar``), which sync's merge-join relies
on - and the same skip-with-warning rules (nonexistent / special / unreadable
files, broken symlinks, the invalid-timestamp epoch fallback). aws-cli's Python-2
byte-filename decoding warning has no Python-3 equivalent (a ``str`` directory
scan always yields ``str``) and is not ported.

Two library-extension scan knobs widen what the stream *contains* (both default
off = exact ``aws s3`` parity, which lists files only): ``return_directories``
returns every directory as its own ``DIRECTORY``-kind entry at its byte-order
position - the walk root included, leading the stream at ``compare_key == ""`` -
and ``return_symlinks`` returns each symlink as an unfollowed, vetting-free leaf
(the link's own lstat, ``is_symlink=True``; a broken link is an entry too).
They are orthogonal to ``follow_symlinks``, which keeps meaning *traversal*
only: following while returning descends a link's directory target right after
the link's own leaf, at the cost of one extra followed stat. All three ride
``LocalScanOptions`` (seeded from the ``LocalStorage`` constructor), so
``scan()``'s output shape is readable off its options.

An app customizes the walk by subclassing :class:`LocalFileGenerator`, overriding
its **public** methods, and passing an instance to
``LocalStorage(path, walker=MyWalker())`` - no protected-method surgery on
``LocalStorage`` itself. The method names track aws-cli where a counterpart
exists (``list_files`` / ``should_ignore_file`` / ``triggers_warning`` /
``normalize_sort`` / :func:`is_special_file` / :func:`is_readable` /
:func:`get_file_stat`); the pieces with no aws-cli counterpart - the
``os.scandir`` engine and its extensions - keep boto3-s3 names
(:meth:`~LocalFileGenerator.scan_children` / :meth:`~LocalFileGenerator.classify_child`
/ :meth:`~LocalFileGenerator.dir_child` / :class:`WalkChild` /
``have_dir_fd`` / ``detect_symlink_loops`` / :class:`LoopDetector`).

Performance (the reason for the ``os.scandir`` engine, not aws-cli's ``listdir``
+ per-name ``os.path.isdir`` / ``os.stat`` restat): ``os.scandir`` carries each
entry's type (``d_type``) so ``is_symlink`` costs no syscall, and caches one
``stat`` per entry that the vetting battery, the file/dir classification, and the
``LocalFileInfo`` build all read through
:meth:`~LocalFileGenerator.entry_stat_result` (one accessor, so overriding it -
e.g. to lstat - re-points the whole walk at once). Where the platform supports it
(:data:`LocalFileGenerator.have_dir_fd`, i.e. POSIX), the directory is opened
once and scanned through its file descriptor, so every per-entry ``stat`` /
readability probe is ``dir_fd``-relative (``fstatat`` - no kernel path re-walk);
on Windows those APIs are absent, and the same code path falls back to a
path-based scan whose ``FindNextFile`` data already supplies the attributes for
free. The net effect is one ``stat`` per surviving entry (zero for a plain
directory) plus aws-cli's one readability ``open`` per entry, versus the ~5
restats per entry a naive port makes. Every surviving :class:`LocalFileInfo`
carries that followed ``stat`` as :attr:`~boto3_s3.types.LocalFileInfo.stat_result`
and its ``d_type`` symlink flag as
:attr:`~boto3_s3.types.LocalFileInfo.is_symlink` - both already in hand from the
vetting battery, so a ``filter`` / ``on_result`` callback reads them free, and
because a ``stat_result`` is a plain value (not a ``dir_fd``-relative
``os.DirEntry``) the fast path keeps scanning through the directory fd.

The override seams, finest first - extend at the smallest layer that fits:

- :meth:`LocalFileGenerator.should_ignore_entry` - the special-file + readability
  battery on one entry's stat (the DirEntry form of ``triggers_warning``; the
  symlink-skip and "does not exist" cases are :meth:`~LocalFileGenerator.classify_child`'s);
- :meth:`LocalFileGenerator.entry_stat_result` - the one stat snapshot per entry
  (``classify_child`` takes it once and threads it to vetting / kind / size /
  mtime / loop key / ``stat_result``; override to lstat for a non-following /
  backup walk);
- :meth:`LocalFileGenerator.stat_info` - one file entry's ``LocalFileInfo`` from
  that stat (aws-cli ``_safely_get_file_stats``), and
  :meth:`LocalFileGenerator.dir_child` its directory counterpart;
- :meth:`LocalFileGenerator.classify_child` - one ``os.DirEntry`` -> a
  :class:`WalkChild` or a skip, the natural per-entry override point (owns the
  single stat: the no-follow / "does not exist" skips and the kind decision);
- :meth:`LocalFileGenerator.finalize_children` - a directory's ``compare_key``-
  stamped children as a whole, before the walk consumes them (default: just
  :meth:`~LocalFileGenerator.normalize_sort`; override to prune / harvest /
  strip - dropping a directory prunes its subtree);
- :meth:`LocalFileGenerator.scan_children` - one directory enumerated, its children
  ``compare_key``-stamped, then ``finalize_children``\\ d (override to change *how*
  a directory is read, reusing ``classify_child`` / ``dir_child`` / the two above);
- :meth:`LocalFileGenerator.walk_dir` - the depth-first recursion yielding one
  page per directory file-run (override to prune a subtree or otherwise
  customize how the tree is descended);
- :meth:`LocalFileGenerator.list_file_pages` - the entry point: root vetting,
  then ``walk_dir`` (which stamps ``compare_key``) with ``ScanOptions.filter``, in
  ``os.scandir``-aligned pages (what ``Storage.scan_pages`` returns);
- :meth:`LocalFileGenerator.list_files` - that flattened to a flat per-file
  stream (aws-cli's ``FileGenerator.list_files``).

The module-level helpers are public where an override needs them
(:func:`is_special_file` / :func:`is_readable` / :func:`get_file_stat`, and
:class:`LoopDetector` / :class:`WalkChild`); ``LocalStorage.get_fileinfo`` (the
single-path point op) reuses them too.
"""

from __future__ import annotations

import itertools
import os
import stat as stat_module
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, ClassVar, Literal, NamedTuple, cast

from typing_extensions import override

from boto3_s3.exceptions import (
    AccessDeniedError,
    Boto3S3Error,
    NotFoundError,
    TransportError,
)
from boto3_s3.storage import Storage, StorageCapability
from boto3_s3.types import FileInfo, FileKind, LocalFileInfo, LocalScanOptions, ScanOptions

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from typing import BinaryIO

# aws-cli EPOCH_TIME: the stamp used when a file's mtime cannot be represented.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_native_path(key: str) -> str:
    """Native form of a ``/``-separated ``FileInfo`` key.

    Lossless because no supported filesystem allows ``/`` inside a file name
    (it is the separator on POSIX and rejected on Windows).
    """
    return key.replace("/", os.sep)


def _is_special_mode(mode: int) -> bool:
    """Whether ``st_mode`` is a character/block device, FIFO, or socket.

    The mode-only core of :func:`is_special_file`, so a caller that already holds
    a ``stat`` (the walk, from ``os.DirEntry.stat``) checks the type without a
    second syscall.
    """
    return (
        stat_module.S_ISCHR(mode)
        or stat_module.S_ISBLK(mode)
        or stat_module.S_ISFIFO(mode)
        or stat_module.S_ISSOCK(mode)
    )


def is_special_file(path: str) -> bool:
    """Character/block device, FIFO, or socket (aws-cli's ``is_special_file``)."""
    return _is_special_mode(os.stat(path).st_mode)


def is_readable(path: str, is_dir: bool | None = None) -> bool:
    """Probe read access by performing a read operation (aws-cli's ``is_readable``).

    aws-cli deliberately opens/lists instead of ``os.access`` - the probe
    answers what the transfer itself will do. ``is_dir`` short-circuits the
    ``os.path.isdir`` when the caller already knows the type (the walk does,
    from ``d_type`` / the entry's cached stat).
    """
    if is_dir is None:
        is_dir = os.path.isdir(path)
    if is_dir:
        try:
            os.listdir(path)
        except OSError:
            return False
    else:
        try:
            with open(path, "rb"):
                pass
        except OSError:
            return False
    return True


def _is_readable_child(name: str, full: str, dir_fd: int | None, *, is_dir: bool) -> bool:
    """Readability probe for a walk entry (aws-cli's ``is_readable``, dir-relative).

    With a ``dir_fd`` (POSIX) the probe ``open`` is relative to it - the same
    ``openat`` the transfer/listdir would do, with no path re-walk. Off POSIX
    (``dir_fd is None``) it falls back to the path-based :func:`is_readable`
    with the type already known.
    """
    if dir_fd is None:
        return is_readable(full, is_dir)
    flags = os.O_RDONLY | (os.O_DIRECTORY if is_dir else 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError:
        return False
    os.close(fd)
    return True


def _size_mtime(st: os.stat_result) -> tuple[int, datetime | None]:
    """Size and tz-aware UTC mtime from a stat result (the shared derivation).

    An unrepresentable timestamp returns ``None`` for mtime - the caller's cue
    for the epoch fallback. Represented in UTC per the ``FileInfo.mtime``
    contract (aws-cli uses the local zone - same instant either way).
    """
    try:
        mtime: datetime | None = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        mtime = None
    return st.st_size, mtime


def get_file_stat(path: str, st: os.stat_result | None = None) -> tuple[int, datetime | None]:
    """Size and tz-aware mtime for a path (aws-cli's ``get_file_stat``).

    ``st`` reuses a stat already taken for ``path`` (the single-path
    :meth:`LocalStorage.get_fileinfo` holds one snapshot and derives everything
    from it - no re-stat); with it ``path`` is unused. Without it,
    ``os.stat(path)`` is taken here and its ``OSError`` propagates (the caller
    runs the warning battery). An unrepresentable timestamp returns ``None`` for
    the caller's epoch fallback (see :func:`_size_mtime`).
    """
    return _size_mtime(st if st is not None else os.stat(path))


def _stat_key(path: str) -> tuple[int, int] | None:
    """A directory's ``(st_dev, st_ino)`` identity, or ``None`` to fail open.

    ``None`` - an ``os.stat`` error, or the ``st_ino == 0`` some FAT / exFAT /
    FUSE volumes report - disables loop detection for that subtree (we keep
    descending rather than risk a false positive on a volume with no real
    inodes).
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


class WalkChild(NamedTuple):
    """One vetted child of a directory, the element
    :meth:`LocalFileGenerator.scan_children` yields to the walk (part of the walk
    override contract).

    ``sort_name`` is the name the byte-order sort keys on - a plain file name, or
    a directory name with a trailing ``os.sep`` appended (so a directory sorts
    after a sibling file of the same stem, matching aws-cli). ``info`` is the
    entry's :class:`~boto3_s3.types.LocalFileInfo`: a ``FILE`` for a file, or a
    ``DIRECTORY``-kind info for a sub-directory (which the walk recurses into
    rather than yields). ``loop_key`` is a directory's ``(st_dev, st_ino)`` for
    cycle detection - ``None`` for a file, or to fail open when the identity is
    unknown.

    A subclass re-implementing ``scan_children`` builds these with
    :meth:`LocalFileGenerator.classify_child` (or
    :meth:`~LocalFileGenerator.dir_child`) and orders them with
    :meth:`~LocalFileGenerator.normalize_sort`, so it need not reproduce the sort
    key or the directory-info shape.
    """

    sort_name: str
    info: LocalFileInfo
    loop_key: tuple[int, int] | None


class LoopDetector:
    """Ancestor-stack guard against symbolic-link cycles in a recursive walk.

    A reusable building block for a custom recursive enumerator - an application
    walking its own backend, or driving :meth:`LocalFileGenerator.list_files`'s
    lower layer. It tracks the ``(st_dev, st_ino)`` identity of every directory on
    the path from the root down to the one being descended (an *ancestor stack*,
    not a global visited set, so a legitimate diamond - two symlinks to the same
    external directory - is still followed on both arms, like GNU ``find -L`` /
    ``walkdir``). A directory whose identity matches an ancestor is a cycle.

    Seed it with the walk root, then for each subdirectory::

        if detector.is_cycle(subdir):
            ...  # skip: descending would loop
        else:
            try:
                ...  # recurse into subdir
            finally:
                detector.leave()

    It fails open: a directory with no stable identity (an ``os.stat`` error, or
    the ``st_ino == 0`` some FAT / exFAT / FUSE volumes report) never matches an
    ancestor, so the walk keeps descending rather than risk a false positive.
    """

    def __init__(self, root: str) -> None:
        self._ancestors: list[tuple[int, int] | None] = [_stat_key(root)]

    def is_cycle(self, path: str) -> bool:
        """Whether descending ``path`` would re-enter an ancestor (a cycle).

        On ``False`` (not a cycle) ``path`` is pushed as the stack's new tip -
        pair that call with a :meth:`leave` once its descent returns.
        """
        return self.is_cycle_key(_stat_key(path))

    def is_cycle_key(self, key: tuple[int, int] | None) -> bool:
        """:meth:`is_cycle` for a caller that already holds the ``(st_dev, st_ino)``.

        The walk captures the identity from the entry's cached stat, so it never
        restats just to check for a cycle. ``None`` (no stable identity) fails
        open - it is pushed but never matches an ancestor.
        """
        if key is not None and key in self._ancestors:
            return True
        self._ancestors.append(key)  # None (fail-open) never matches an ancestor
        return False

    def leave(self) -> None:
        """Pop the directory pushed by the matching non-cycle :meth:`is_cycle`."""
        self._ancestors.pop()


class LocalFileGenerator:
    """The customizable local directory walk (boto3-s3's aws-cli ``FileGenerator``).

    Subclass it, override the public methods below, and inject an instance via
    ``LocalStorage(path, walker=...)`` to change how directories are enumerated or
    entries vetted - see this module's docstring for the seam layering and the
    aws-cli name mapping. The default instance is the fast ``os.scandir`` walk.

    The class is **stateless across a walk**: the per-walk context
    (``follow_symlinks`` / ``detect_symlink_loops`` / ``on_warning`` / the producing
    ``storage``) rides on the :class:`~boto3_s3.types.LocalScanOptions` passed to
    :meth:`list_files`, not on the instance (aws-cli carries them on the instance
    instead). So **one walker can be shared across several ``LocalStorage``
    instances** - each scan stamps its own ``storage`` from the options.

    Class attributes: ``have_dir_fd`` - whether this platform can scan a directory
    through its file descriptor and stat/open entries relative to it (fstatat /
    openat - no kernel path re-walk); True on POSIX, False on Windows (which lacks
    dir_fd / O_DIRECTORY but returns entry attributes inline from FindNextFile). A
    feature probe, not an ``os.name`` check, so any platform missing the APIs
    degrades correctly. ``dir_open_flags`` are the flags for the one ``open()`` that
    turns a directory path into the fd we scan through (POSIX; O_DIRECTORY /
    O_NONBLOCK are absent and unused off it). ``EPOCH_TIME`` is the stamp for an
    unrepresentable mtime (aws-cli's ``EPOCH_TIME``).
    """

    have_dir_fd: ClassVar[bool] = (
        os.scandir in os.supports_fd
        and os.open in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
    )
    dir_open_flags: ClassVar[int] = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    EPOCH_TIME: ClassVar[datetime] = _EPOCH

    def list_files(self, root: str, options: LocalScanOptions) -> Iterator[LocalFileInfo]:
        """Yield every file under ``root`` (recursively) in aws-cli byte order.

        aws-cli's ``FileGenerator.list_files`` (the ``dir_op=True`` branch): the
        flat per-file stream, a depth-first walk in S3's UTF-8 byte order
        (``foo.txt`` before ``foo/bar``). This is :meth:`list_file_pages`
        flattened - see it for the walk semantics (the ``options`` fields read,
        vetting, ``compare_key`` stamping, and ``filter``). A single source object
        goes through :meth:`get_fileinfo` instead, not this walk.
        """
        for page in self.list_file_pages(root, options):
            yield from page

    def list_file_pages(
        self, root: str, options: LocalScanOptions
    ) -> Iterator[list[LocalFileInfo]]:
        """Yield the files under ``root`` (recursively) as byte-order pages.

        The paged form driving :meth:`Storage.scan_pages`: one page per directory
        file-run - the sorted files between two sub-directory descents, plus,
        under ``options.return_directories``, the descended directory's own
        record (and, first, a one-record page for the walk root's). A
        directory's files can only surface once its ``os.scandir`` has been read in
        full and sorted (the byte-order sort appends ``os.sep`` to directory names,
        so ``foo.txt`` precedes ``foo/bar``), and they are handed off just before
        the next descent's scandir. That aligns each page with one directory read,
        so a consumer - e.g. ``Storage.scan``'s prefetch worker - overlaps the next
        read on a network-mounted path, the reason the walk is paged at all. The
        pages concatenate to :meth:`list_files`'s flat stream. ``root`` is an
        absolute path with a trailing ``os.sep``; each ``LocalFileInfo.key`` is the
        absolute path (``os.sep`` folded to ``/``) and ``compare_key`` is the key
        relative to ``root``, stamped in :meth:`scan_children` (so a custom
        :meth:`finalize_children` can already filter on it) - the axis
        ``options.filter`` matches.

        Reads the local-walk fields of ``options`` (a :class:`LocalScanOptions`,
        this backend's option type, so a custom walker sees the full scan
        context):
        ``on_warning`` (warnings carry the aws-cli message bodies, dropped when
        ``None``); ``follow_symlinks`` (``False`` skips symlinks silently);
        ``return_directories`` (every directory's own record in-stream, the walk
        root's first at ``compare_key == ""`` - see ``root_info``);
        ``return_symlinks`` (symlinks as unfollowed, vetting-free leaves - see
        ``symlink_child``; combined with ``follow_symlinks`` a link's directory
        target is still descended, right after the link's own leaf);
        ``detect_symlink_loops`` (with ``follow_symlinks``, skips a directory that
        resolves to one of its own ancestors with a ``Symbolic link loop
        detected`` warning - a library extension, off = ``aws s3`` behaviour, no
        extra ``stat``); and ``filter`` (drops entries it rejects - applied
        *after* the aws-cli vetting so an excluded file still emits the warnings
        aws-cli would, and yielding only survivors saves paging them downstream;
        the predicate can read each ``LocalFileInfo``'s ``stat_result`` /
        ``is_symlink``, stamped for free from the walk). The root is vetted with
        :meth:`should_ignore_file`; its children per entry in
        :meth:`scan_children`.
        """
        notify: Callable[[str], None] = (
            options.on_warning if options.on_warning is not None else (lambda body: None)
        )
        follow_symlinks = options.follow_symlinks
        if self.should_ignore_file(root, follow_symlinks=follow_symlinks, notify=notify):
            return
        # No detector unless asked and reachable (a cycle needs a followed
        # symlink); None then costs no per-directory stat.
        detector = LoopDetector(root) if options.detect_symlink_loops and follow_symlinks else None
        # root ends in os.sep, so the normalized root ends in "/" and the slice
        # (in scan_children) never leaves a leading separator; compare_key is the
        # key relative to root.
        strip = len(root.replace(os.sep, "/"))
        item_filter = options.filter
        storage = options.storage
        pages = self.walk_dir(root, options, strip=strip, notify=notify, detector=detector)
        if options.return_directories:
            # "Return directories", strictly interpreted, includes the tree's
            # own root: its record leads the stream (compare_key "", which
            # sorts before every child key). Built after the root vetting; a
            # race that removed the root since returns None and drops it.
            root_record = self.root_info(root)
            if root_record is not None:
                pages = itertools.chain([[root_record]], pages)
        for page in pages:
            # compare_key is already stamped (scan_children, before finalize_children);
            # here we stamp each entry's producing backend (options.storage, walk-
            # constant) before the visibility filter, so a predicate - and sync's
            # content compare - can reach it. Then the filter runs; no filter (or a
            # custom finalize_children that already pruned) -> the page passes
            # untouched. storage is None only for a hand-built options object
            # (Storage.scan's backstop fills FileInfo.storage afterwards).
            if storage is not None:
                for info in page:
                    info.storage = storage
            if item_filter is None:
                yield page
                continue
            kept = [info for info in page if item_filter(info)]
            if kept:
                yield kept

    def walk_dir(
        self,
        dir_path: str,
        options: LocalScanOptions,
        *,
        strip: int,
        notify: Callable[[str], None],
        detector: LoopDetector | None,
    ) -> Iterator[list[LocalFileInfo]]:
        """Recursively yield the files under ``dir_path`` as byte-order pages (an override seam).

        The recursion behind :meth:`list_file_pages`: each directory's
        :meth:`scan_children` is read and sorted in full, then its files are
        collected into a run and handed off as one page just before descending
        into each sub-directory (``kind == DIRECTORY``) - depth-first via
        ``self.walk_dir`` (so an override applies at every level), the runs
        interleaving with the sub-directories' pages in byte order. ``dir_path``
        was already vetted (the root by :meth:`list_file_pages`, a child in its
        parent's :meth:`scan_children`); ``detector`` guards symlink cycles.
        ``strip`` is the root prefix length :meth:`scan_children` uses to stamp
        ``compare_key`` - constant across the recursion, threaded through unchanged.

        This is where recursion is customizable: override it and return early for a
        directory to prune its subtree (before ``super().walk_dir`` for the rest),
        or re-implement the loop to cap depth or change traversal - preserving the
        depth-first byte order that sync's merge-join relies on.
        ``options.return_directories`` makes this loop also emit each descended
        directory's own record, on the page flushed just before its descent.
        ``compare_key`` is stamped in :meth:`scan_children`; ``ScanOptions.filter``
        is :meth:`list_file_pages`'s job on the yielded pages, not this method's.
        """
        run: list[LocalFileInfo] = []
        for sort_name, info, loop_key in self.scan_children(
            dir_path, strip=strip, options=options, notify=notify
        ):
            if info.kind != FileKind.DIRECTORY:
                run.append(info)
                continue
            # A sub-directory: sort_name carries the trailing os.sep (the sort
            # suffix), so the child path and the loop warning read correctly.
            sub = os.path.join(dir_path, sort_name)
            # The directory's own record (options.return_directories; off =
            # aws parity) rides the page flushed before its descent: it sorts
            # after the collected files and before anything under sub. Appended
            # before the flush and the detector, so the record is emitted even
            # when the descent is then skipped as a cycle - like an unreadable
            # directory, the record survives and only the children are lost.
            if options.return_directories:
                run.append(info)
            # Descending runs sub's own scandir; hand off the files collected so
            # far first (they all sort before anything under sub) as this
            # directory's page, so a consumer overlaps that read. Flush before
            # touching the detector so an early generator close cannot strand a
            # push ahead of the try/finally that pops it (is_cycle_key pushes).
            if run:
                yield run
                run = []
            if detector is not None:
                if detector.is_cycle_key(loop_key):
                    notify(f"Skipping file {sub.rstrip(os.sep)}. Symbolic link loop detected.")
                    continue
                try:
                    yield from self.walk_dir(
                        sub, options, strip=strip, notify=notify, detector=detector
                    )
                finally:
                    detector.leave()
            else:
                yield from self.walk_dir(
                    sub, options, strip=strip, notify=notify, detector=detector
                )
        if run:
            yield run

    def root_info(self, root: str) -> LocalFileInfo | None:
        """The walk root's own ``DIRECTORY`` record (``options.return_directories``).

        ``root`` is the absolutized walk root with a trailing separator (the
        ``list_file_pages`` anchor, or ``_scan_one_level``'s for a non-recursive
        scan). Its ``compare_key`` is the empty string -
        the root relativized to itself - which sorts before every child key,
        so the record leads the stream; note glob filters see that ``""`` (a
        lone ``*`` matches it, a non-empty literal does not). The stat is
        taken after the root vetting passed; ``None`` (a race removed the root
        since) drops the record, as its children are about to be. The stat is
        the followed one - the walk is descending this directory - with
        ``is_symlink`` reflecting the root path itself.
        """
        try:
            st = os.stat(root)
        except OSError:
            return None
        return LocalFileInfo(
            key=root.replace(os.sep, "/"),
            kind=FileKind.DIRECTORY,
            stat_result=st,
            is_symlink=os.path.islink(root.rstrip(os.sep)),
            compare_key="",
        )

    def scan_children(
        self,
        dir_path: str,
        *,
        strip: int,
        options: LocalScanOptions,
        notify: Callable[[str], None],
    ) -> list[WalkChild]:
        """One directory's vetted children as final :class:`WalkChild`\\ s.

        The enumeration layer (no aws-cli counterpart - aws-cli uses ``listdir``):
        it scans ``dir_path`` once with :func:`os.scandir`, turns each entry into
        a :class:`WalkChild` via :meth:`classify_child` (skips return ``None``),
        stamps each child's ``compare_key`` (the key with the ``strip``-long root
        prefix removed, aws-cli's ``src_path[len(root):]``), then hands the list to
        :meth:`finalize_children` (which sorts, and is the override point for
        pruning / registration). ``options`` carries the per-walk context the
        classification reads (``follow_symlinks`` / ``return_symlinks``). Under
        ``return_symlinks`` *and* ``follow_symlinks``, a symlink to a directory
        contributes two children: the link's own leaf (sort key ``name``) and
        the target directory's descent (``name`` + separator, sorting right
        after it) - the extra followed stat is what decides that descent, and
        a broken link simply has nothing to descend. A directory that cannot be opened or scanned
        (a symlink cycle stopped by the kernel, an over-long path, or a race
        after its parent vetted it readable) is skipped through the
        :meth:`triggers_warning` battery - the aws-cli warning its full-path
        vetting would emit - and yields an empty list; if the battery sees
        nothing wrong, the ``OSError`` propagates instead.

        The fast path scans through the directory's own fd where the platform
        allows (:data:`have_dir_fd`), so each entry's stat and readability probe
        are dir-relative (``fstatat`` - no path re-walk); the followed stat each
        :class:`LocalFileInfo` carries as ``stat_result`` is a plain value, so it
        keeps this fast path (unlike an ``os.DirEntry``, which would break once
        the fd closes).

        Override this to change how a directory is enumerated - e.g. to inject
        synthetic entries or read a different source - reusing
        :meth:`classify_child` / :meth:`dir_child` / :meth:`normalize_sort` so the
        sort key and directory-info shape need not be reproduced (an injected
        child still needs its ``compare_key`` stamped - ``info.key[strip:]``;
        ``FileInfo.storage`` need not be set - the caller stamps the producing
        backend after the walk yields a page). To prune / register / re-order after
        the fact, override the finer :meth:`finalize_children` instead; for a
        per-entry change, :meth:`classify_child` (or :meth:`should_ignore_entry` /
        :meth:`stat_info`).
        """
        children: list[WalkChild] = []
        dir_fd: int | None = None
        use_fd = self.have_dir_fd
        try:
            if use_fd:
                dir_fd = os.open(dir_path, self.dir_open_flags)
            scan = os.scandir(dir_fd if dir_fd is not None else dir_path)
        except OSError:
            # The directory became unopenable even though its parent vetted it:
            # deterministically (a symlink cycle the kernel stops with ``ELOOP``
            # once a path accumulates SYMLOOP_MAX links - the per-entry vetting
            # is dir-relative and resolves one link at a time, so it admits each
            # level - or an over-long path), or by a race (deleted / chmod'd
            # since). aws-cli vets children by *full path*, so its battery fails
            # right here: run the same path battery for the same warning + rc 2
            # (``ELOOP`` fails ``os.path.exists`` -> "File does not exist.").
            # If the probes see nothing wrong (e.g. fd exhaustion resolved by the
            # probe's close), re-raise like aws-cli's ``listdir`` would - never
            # prune silently. Scoped to the open/scandir establishment only - a
            # per-entry OSError (from a race mid-scan or an override's
            # classify_child) propagates rather than dropping the directory.
            if dir_fd is not None:
                os.close(dir_fd)
            if not self.triggers_warning(dir_path.rstrip(os.sep), notify):
                raise
            return []
        try:
            with scan as it:
                for entry in it:
                    full = os.path.join(dir_path, entry.name)
                    child = self.classify_child(entry, full, dir_fd, options=options, notify=notify)
                    if child is None:
                        continue
                    children.append(child)
                    if (
                        options.return_symlinks
                        and options.follow_symlinks
                        and child.info.is_symlink
                        and child.info.kind is not FileKind.DIRECTORY
                    ):
                        # Returning AND following: the link's own leaf is
                        # above; when its target is a directory, descend it
                        # too - the extra followed stat is the cost this
                        # combination pays. A broken link: nothing to descend.
                        try:
                            followed = entry.stat()
                        except OSError:
                            continue
                        if stat_module.S_ISDIR(followed.st_mode):
                            children.append(self.dir_child(entry, full, followed))
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
        for child in children:
            child.info.compare_key = child.info.key[strip:]
        return self.finalize_children(children)

    def entry_stat_result(self, entry: os.DirEntry[str]) -> os.stat_result | None:
        """The single stat snapshot for one entry (an override seam), or ``None``.

        The walk's **one** stat per entry: :meth:`classify_child` calls this once
        and threads the result through everything downstream - the special /
        readable vetting (:meth:`should_ignore_entry`), the file-vs-directory
        decision, the ``size`` / ``mtime`` (:meth:`stat_info`), the loop key
        (:meth:`dir_child`), and :attr:`LocalFileInfo.stat_result`. The default
        reads the ``os.DirEntry``'s cache, one ``fstatat`` for the whole entry.

        Default: the **followed** stat (``follow_symlinks=True``), so the walk
        follows symlinks like ``aws s3``. Override to return the link's own lstat
        (``entry.stat(follow_symlinks=False)``) to turn the walk lstat-based in
        one place, still one syscall: then a symlink surfaces as its own entry
        instead of being followed, a symlinked directory is not descended (its
        ``st_mode`` is not ``S_IFDIR``), and ``size`` / ``mtime`` / ``stat_result``
        all describe the link - the building block for a backup-style walk. (The
        readability probe in :meth:`should_ignore_entry` still opens through the
        link, so pair this with a ``should_ignore_entry`` override to also admit
        broken links.) ``None`` (an ``OSError``) makes the caller treat the entry
        as gone (the "does not exist" skip).
        """
        try:
            return entry.stat(follow_symlinks=True)
        except OSError:
            return None

    def classify_child(
        self,
        entry: os.DirEntry[str],
        full: str,
        dir_fd: int | None,
        *,
        options: LocalScanOptions,
        notify: Callable[[str], None],
    ) -> WalkChild | None:
        """One ``os.DirEntry`` -> a :class:`WalkChild`, or ``None`` to skip it.

        The per-entry decision, and the walk's single stat: it takes the entry's
        :meth:`entry_stat_result` **once** and threads that one value through the
        rest (so nothing below re-stats or re-checks for ``None``). Symlinks are
        decided first, on a free ``d_type`` test before any stat: under
        ``options.return_symlinks`` the link is its own leaf (``symlink_child``,
        lstat-based, vetting-free; the follow-and-descend companion child is
        ``scan_children``'s); otherwise no-follow skips it silently. A stat
        that comes back ``None`` (a broken symlink followed, or the entry raced
        away between the scan and here) is the "does not exist" skip; then
        :meth:`should_ignore_entry` vets that valid stat, and the kind is keyed on
        its ``st_mode`` (``S_IFDIR`` = descend via :meth:`dir_child`, else a file
        via :meth:`stat_info`). Keying on that same stat - rather than a fresh
        ``entry.is_dir`` - keeps the whole entry on one stat and lets an lstat
        override classify a symlinked directory as a non-descended entry. ``full``
        is the entry's absolute path, ``dir_fd`` the owning directory fd (``None``
        off POSIX).
        """
        if entry.is_symlink():
            if options.return_symlinks:
                return self.symlink_child(entry, full, notify=notify)
            if not options.follow_symlinks:
                return None
        st = self.entry_stat_result(entry)
        if st is None:
            notify(f"Skipping file {full}. File does not exist.")
            return None
        if self.should_ignore_entry(entry, full, dir_fd, st, notify=notify):
            return None
        if stat_module.S_ISDIR(st.st_mode):
            return self.dir_child(entry, full, st)
        return WalkChild(entry.name, self.stat_info(entry, full, st, notify), None)

    def dir_child(self, entry: os.DirEntry[str], full: str, st: os.stat_result) -> WalkChild:
        """A sub-directory's :class:`WalkChild` - its ``DIRECTORY`` info and loop key.

        ``sort_name`` and the info key carry a trailing ``os.sep`` (so the
        directory sorts after a sibling file of the same stem); ``loop_key`` is
        ``(st_dev, st_ino)`` from ``st`` (``None`` to fail open on the ``st_ino ==
        0`` some FAT / exFAT / FUSE volumes report). ``st`` is the one stat
        :meth:`classify_child` took; the info carries it and the entry's
        ``is_symlink`` flag.
        """
        loop_key = (st.st_dev, st.st_ino) if st.st_ino else None
        info = LocalFileInfo(
            key=(full + os.sep).replace(os.sep, "/"),
            kind=FileKind.DIRECTORY,
            stat_result=st,
            is_symlink=entry.is_symlink(),
        )
        return WalkChild(entry.name + os.sep, info, loop_key)

    def symlink_child(
        self, entry: os.DirEntry[str], full: str, *, notify: Callable[[str], None]
    ) -> WalkChild | None:
        """A symlink's own :class:`WalkChild` (``options.return_symlinks``): the
        link as a leaf.

        lstat-based - the info carries the link's own stat with
        ``is_symlink=True``, so ``size`` / ``mtime`` describe the link, not the
        target - and vetting-free: a symlink is a name plus a target with no
        content to probe, so the special/readability battery does not apply
        and a broken link is a returned entry like any other. The child itself
        is never descended (``loop_key=None``); when the walk also follows
        (``follow_symlinks``), ``scan_children`` appends the target directory's
        descent as a separate child. ``None`` = the entry raced away (even
        lstat failed).
        """
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            notify(f"Skipping file {full}. File does not exist.")
            return None
        return WalkChild(entry.name, self.stat_info(entry, full, st, notify), None)

    def finalize_children(self, children: list[WalkChild]) -> list[WalkChild]:
        """Produce a directory's final child list from its vetted, ``compare_key``-
        stamped children (an override seam; default: just :meth:`normalize_sort`).

        The last step of :meth:`scan_children`, the seam for shaping a directory's
        children as a whole before the walk consumes them - each child's
        ``compare_key`` is already stamped, so an override can prune by it, harvest
        (register directories / symlinks a plain transfer would not surface),
        strip entries it does not want transferred, or re-order. It receives the
        children *unsorted*, so pruning here happens before the sort; call
        ``super().finalize_children`` (or :meth:`normalize_sort`) for aws-cli byte
        order. ``FileInfo.storage`` (the producing backend) is *not* set yet - the
        caller stamps it after the walk yields a page, before the visibility filter
        - so an override shapes the subtree from ``compare_key`` / ``key`` /
        ``kind``, not from the backend handle. Dropping a ``DIRECTORY`` child prunes
        its whole subtree (the walk
        never descends what this does not return) - a speedup ``aws s3`` cannot do
        (it vets every file), so it is the right place for a non-parity backup
        walk, not the default. To surface directories (or symlinks) as stream
        entries of their own, use the ``return_directories`` /
        ``return_symlinks`` scan options instead of harvesting here. The
        default returns the sorted list unchanged.
        """
        return self.normalize_sort(children)

    def normalize_sort(self, children: list[WalkChild]) -> list[WalkChild]:
        """Sort children into aws-cli byte order (aws-cli's ``normalize_sort``).

        The sort folds ``os.sep`` to ``/``: str sorts by code point = UTF-8 byte
        order (S3's exact key order), and the fold puts a directory (``foo/``)
        after a sibling file (``foo.txt``) since ``/`` (0x2F) > ``.`` (0x2E) -
        matching aws-cli. In place, then returned.
        """
        children.sort(key=lambda child: child.sort_name.replace(os.sep, "/"))
        return children

    def should_ignore_file(
        self, path: str, *, follow_symlinks: bool, notify: Callable[[str], None]
    ) -> bool:
        """Silent symlink skip plus the warning battery on a *path* (aws-cli's
        ``should_ignore_file``).

        Path-based, used for the walk root (which has no ``os.DirEntry``);
        ``path`` is the absolutized root with a trailing ``os.sep``. The per-entry
        hot path uses the DirEntry form :meth:`should_ignore_entry`.
        """
        if not follow_symlinks:
            probe = path
            if os.path.isdir(probe) and probe.endswith(os.sep):
                # A trailing separator must be removed to test the link itself.
                probe = probe[:-1]
            if os.path.islink(probe):
                return True
        return self.triggers_warning(path, notify)

    def triggers_warning(self, path: str, notify: Callable[[str], None]) -> bool:
        """Warn-and-skip checks on a *path*, aws-cli order and wording (``triggers_warning``).

        Path-based: the root vetting (:meth:`should_ignore_file`) and the
        :meth:`stat_info` race fallback. The per-entry hot path checks the same
        conditions from the ``DirEntry``'s cached stat in
        :meth:`should_ignore_entry`.
        """
        if not os.path.exists(path):
            notify(f"Skipping file {path}. File does not exist.")
            return True
        if is_special_file(path):
            notify(
                f"Skipping file {path}. File is character special device, "
                "block special device, FIFO, or socket."
            )
            return True
        if not is_readable(path):
            notify(f"Skipping file {path}. File/Directory is not readable.")
            return True
        return False

    def should_ignore_entry(
        self,
        entry: os.DirEntry[str],
        full: str,
        dir_fd: int | None,
        st: os.stat_result,
        *,
        notify: Callable[[str], None],
    ) -> bool:
        """The special-file + readability battery on one entry's stat (the DirEntry
        form of :meth:`triggers_warning`).

        Runs on the valid stat :meth:`classify_child` already took (``st``) - the
        no-follow symlink skip and the "does not exist" / ``None`` case are
        classify_child's, so this only decides whether a *present* entry is
        ignorable: a character/block/FIFO/socket special file, or one the process
        cannot read. The readability probe is dir-relative (``openat`` through
        ``dir_fd`` where the platform allows, ``None`` off POSIX). ``full`` is the
        entry's absolute path (warning wording). Same order / wording as
        :meth:`triggers_warning`.
        """
        if _is_special_mode(st.st_mode):
            notify(
                f"Skipping file {full}. File is character special device, "
                "block special device, FIFO, or socket."
            )
            return True
        if not _is_readable_child(entry.name, full, dir_fd, is_dir=stat_module.S_ISDIR(st.st_mode)):
            notify(f"Skipping file {full}. File/Directory is not readable.")
            return True
        return False

    def stat_info(
        self, entry: os.DirEntry[str], full: str, st: os.stat_result, notify: Callable[[str], None]
    ) -> LocalFileInfo:
        """One file entry's ``LocalFileInfo`` from its stat (aws-cli's
        ``_safely_get_file_stats``).

        ``st`` is the valid stat :meth:`classify_child` took (the race / ``None``
        case is handled there), so this never fails: an unrepresentable mtime
        keeps the file, stamped with :data:`EPOCH_TIME` (aws-cli's
        ``skip_file=False``). The info carries ``st`` and the entry's
        ``is_symlink`` flag.
        """
        size, mtime = _size_mtime(st)
        if mtime is None:
            # skip_file=False in aws-cli: warn but keep the file, stamped epoch.
            notify("File has an invalid timestamp. Passing epoch time as timestamp.")
            mtime = self.EPOCH_TIME
        return LocalFileInfo(
            key=full.replace(os.sep, "/"),
            size=size,
            mtime=mtime,
            stat_result=st,
            is_symlink=entry.is_symlink(),
        )


def translate_os_error(
    exc: OSError,
    *,
    operation: str | None,
    key: str | None,
    message: str | None = None,
) -> Boto3S3Error:
    """Map an ``OSError`` to the library taxonomy (the local mirror of
    ``s3storage.translate_boto_error``): missing path -> ``NotFoundError``,
    permission -> ``AccessDeniedError``, everything else -> ``TransportError``.

    Shared by every local-filesystem failure path (this backend, the engines'
    ``makedirs``, the CLI's destination pre-creation). ``message`` overrides
    ``str(exc)`` when the caller carries aws-cli wording of its own.
    """
    text = str(exc) if message is None else message
    if isinstance(exc, FileNotFoundError):
        return NotFoundError(text, operation=operation, key=key)
    if isinstance(exc, PermissionError):
        return AccessDeniedError(text, operation=operation, key=key)
    return TransportError(text, operation=operation, key=key)


class LocalStorage(Storage):
    """A local filesystem path as one side of a transfer.

    The recursive walk lives in a composed :class:`LocalFileGenerator` (default
    the fast ``os.scandir`` walk). Pass ``walker=`` a custom subclass to change
    the traversal - e.g. to resolve Cygwin ``!<symlink>`` files on a
    native-Python Windows build - without subclassing ``LocalStorage`` itself.

    Class attributes: ``sep`` is the host-native separator (``os.sep``;
    :meth:`format` returns ``os.sep`` forms, unlike every other backend's ``/``
    space). ``capabilities`` is the full set - the local filesystem supports every
    transfer operation: byte I/O both ways, single-entry stat, a sorted walk (so
    ``SORTABLE_SCAN``), and delete. ``scan_options_type`` is
    :class:`LocalScanOptions` (arg-less ``scan()`` builds it, and :meth:`scan_pages`
    requires it). ``scan_pages_filters`` is ``True`` - the walk applies
    ``options.filter`` (the default walker late, after vetting/warnings; a custom
    ``LocalFileGenerator`` possibly early), so ``scan`` does not re-apply it.
    """

    scheme: ClassVar[str] = "local"
    sep: ClassVar[str] = os.sep
    capabilities: ClassVar[StorageCapability] = (
        StorageCapability.OPEN_READ
        | StorageCapability.OPEN_WRITE
        | StorageCapability.GET_FILEINFO
        | StorageCapability.SCAN
        | StorageCapability.SORTABLE_SCAN
        | StorageCapability.DELETE
    )
    scan_options_type: ClassVar[type[ScanOptions]] = LocalScanOptions
    scan_pages_filters: ClassVar[bool] = True

    @staticmethod
    def relative_path(filename: str, start: str = os.path.curdir) -> str:
        """Render a local path relative to ``start`` (aws-cli's ``relative_path``).

        aws-cli splits first and joins the basename back on, so an in-tree
        path always carries a directory prefix (``./a.txt``, ``../x/a.txt``) -
        the form aws prints in transfer result lines and warnings. Where no
        relative path exists (different Windows drives), the absolute path is
        returned instead of raising.
        """
        try:
            dirname, basename = os.path.split(filename)
            relative_dir = os.path.relpath(dirname, start)
            return os.path.join(relative_dir, basename)
        except ValueError:
            return os.path.abspath(filename)

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        walker: LocalFileGenerator | None = None,
        follow_symlinks: bool = True,
        detect_symlink_loops: bool = False,
        return_directories: bool = False,
        return_symlinks: bool = False,
        fsync: bool = False,
    ) -> None:
        self._path = os.fspath(path)
        # Absolutize once, at construction (against the cwd then): every scan /
        # get_fileinfo anchors here so ``FileInfo.key`` comes out absolute, and
        # ``os.path.abspath`` calls ``os.getcwd()`` - too costly to repeat per
        # entry. Binding the cwd here also keeps a relative path resolving
        # consistently if the process later chdir's.
        self._abspath = os.path.abspath(self._path)
        # How this local source is read (the scan's source-config): whether
        # symlinks are followed, whether directories / symlinks appear in the
        # scan output themselves (return_*; defaults off = aws parity), and
        # whether the recursive walk guards against symlink cycles. Seeded into
        # every scan via default_scan_options (and read by the single-path
        # get_fileinfo), so an app configures the walk once here rather than
        # passing it through each operation.
        self._follow_symlinks = follow_symlinks
        self._detect_symlink_loops = detect_symlink_loops
        self._return_directories = return_directories
        self._return_symlinks = return_symlinks
        # A library-only durability knob for this local backend as a *destination*
        # (default off = aws parity). When set, a ``mv`` whose download lands here
        # fsyncs the file (and its parent directory) before deleting the S3 source,
        # closing the window where aws-cli / s3transfer delete the durable S3 copy
        # while the download is still only in the page cache. Read by the transfer
        # engine off the destination storage; see ``docs/transfer.md`` section 11.
        self._fsync = fsync
        # The directory-walk strategy (the default fast walk, or an app's). The
        # walker is stateless, so one instance may be shared across LocalStorage
        # instances: the producing storage is threaded per-walk via
        # LocalScanOptions.storage (scan_pages / walk_local below), not held on it.
        self._walker = walker if walker is not None else LocalFileGenerator()

    @property
    def path(self) -> str:
        return self._path

    @property
    def fsync(self) -> bool:
        """Whether a ``mv`` download into this destination fsyncs before deleting the source.

        The library-only durability knob from the constructor (default off). The
        transfer engine consults it only on the ``mv`` download route.
        """
        return self._fsync

    @property
    def walker(self) -> LocalFileGenerator:
        """The :class:`LocalFileGenerator` driving this backend's directory walk."""
        return self._walker

    @override
    def as_text(self) -> str:
        """Return the path as given (:meth:`Storage.as_text`).

        The raw constructor form, verbatim - the trailing-separator rule of
        :meth:`format` reads this unmodified token.
        """
        return self._path

    @override
    def format(self, *, dir_op: bool) -> tuple[str, bool]:
        """Format this local side; return ``(root, use_src_name)`` (:meth:`Storage.format`).

        aws-cli's ``FileFormat.local_format`` on the held state: the root is
        the construction-time :attr:`_abspath` (the same anchor ``scan`` walks
        from, so the plan and the walk agree even if the process chdir'd
        since), and the trailing-separator rule reads the *raw* constructor
        form (``abspath`` strips it). An existing directory, a ``dir_op``, or
        a user-typed trailing ``os.sep`` all mean directory semantics - the
        root gains a trailing ``os.sep`` and the destination side would take
        the source's name.
        """
        full_path = self._abspath
        if (os.path.exists(full_path) and os.path.isdir(full_path)) or dir_op:
            return full_path + os.sep, True
        if self._path.endswith(os.sep):
            return full_path + os.sep, True
        return full_path, False

    @override
    def default_scan_options(self) -> LocalScanOptions:
        """The walk options seeded with this storage's held config
        (:meth:`Storage.default_scan_options`).

        ``follow_symlinks`` / ``detect_symlink_loops`` / ``return_directories`` /
        ``return_symlinks`` come from the constructor,
        so every scan (and the single-path :meth:`get_fileinfo`) reads the walk
        configured once on this ``LocalStorage``; an operation overlays only its
        own knobs (``recursive`` / ``sort`` / ``filter`` / ``on_warning``) onto this.
        """
        return LocalScanOptions(
            follow_symlinks=self._follow_symlinks,
            detect_symlink_loops=self._detect_symlink_loops,
            return_directories=self._return_directories,
            return_symlinks=self._return_symlinks,
        )

    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
        """Yield entries under :attr:`path`, one directory read (``os.scandir``) per page.

        Recursive enumeration drives the walker's
        :meth:`~LocalFileGenerator.list_file_pages` in aws-cli byte order, whose
        pages fall on directory boundaries (a directory's sorted files handed off
        just before the walk descends into its next sub-directory) so a page maps
        to one scandir - the unit a prefetch consumer overlaps.
        ``options.follow_symlinks`` / ``options.detect_symlink_loops`` /
        ``options.on_warning`` / ``options.filter`` are threaded into it - a
        symlink skip, the cycle guard, the aws-cli-worded warning channel a
        transfer needs, and the per-entry filter (applied in the walk, after
        vetting - :meth:`Storage.scan_pages`'s "return filtered pages" contract).
        Every listed ``LocalFileInfo`` carries its followed ``stat_result`` and
        ``is_symlink`` flag for a filter / ``on_result`` to read.
        Non-recursive yields one level like the S3 backend (immediate entries in
        the same sort order, sub-directories as ``DIRECTORY``-kind infos whose key
        ends with ``/``) as a single page, anchored at the absolutized path
        (``self._abspath``) so ``FileInfo.key`` is absolute. Under
        ``return_directories`` that page also leads with the scanned root's own
        record (``compare_key == ""`` - see ``root_info``), as the recursive walk
        does; the S3 backend has no such knob, so this is a local-only extension.

        Requires a :class:`LocalScanOptions` (this backend's option type); a
        foreign ``ScanOptions`` is rejected rather than silently walking with
        defaults.
        """
        if not isinstance(options, LocalScanOptions):
            raise TypeError(
                f"LocalStorage.scan requires LocalScanOptions, got {type(options).__name__}"
            )
        # Thread this storage as the producing backend so the (shared, stateless)
        # walker stamps each entry's FileInfo.storage from options.storage before
        # the filter runs - overriding whatever a caller left there, since the
        # entries are this storage's. (walk_local sets it the same way.)
        options = replace(options, storage=self)
        if options.recursive:
            yield from self._walker.list_file_pages(self._abspath + os.sep, options)
            return
        page = list(self._scan_one_level(self._abspath, options))
        if page:
            yield page

    def walk_local(
        self,
        *,
        on_warning: Callable[[str], None] | None = None,
    ) -> Iterator[LocalFileInfo]:
        """Yield every file under :attr:`path` (recursively) in aws-cli's byte order.

        A thin wrapper over :meth:`self.walker.list_files <LocalFileGenerator.list_files>`:
        it anchors at the absolutized path with a trailing ``os.sep`` (so
        ``FileInfo.key`` is absolute, ``compare_key`` comes out relative to
        :attr:`path`, and a non-directory root degrades to a "does not exist"
        warning rather than a scan error). The walk reads this storage's held
        source-config (``follow_symlinks`` / ``detect_symlink_loops`` /
        ``return_directories`` / ``return_symlinks`` from the constructor, via
        :meth:`default_scan_options` - the same walk ``scan``
        runs); ``on_warning`` is the per-call overlay, like an operation's. This
        is the raw walk (no ``ScanOptions.filter``); ``scan`` applies the filter
        through :meth:`scan_pages`. The byte-order / warning semantics are the
        walker's - see :meth:`LocalFileGenerator.list_files`. A single source
        object goes through :meth:`get_fileinfo` instead, not this walk.
        """
        # local_format(dir_op=True) form; self._abspath is computed once at
        # construction. list_files stamps compare_key (key relative to root) and,
        # from options.storage below, each entry's producing backend.
        yield from self._walker.list_files(
            self._abspath + os.sep,
            replace(self.default_scan_options(), on_warning=on_warning, storage=self),
        )

    def _scan_one_level(self, root: str, options: LocalScanOptions) -> Iterator[FileInfo]:
        notify: Callable[[str], None] = (
            options.on_warning if options.on_warning is not None else (lambda body: None)
        )
        item_filter = options.filter
        if not os.path.isdir(root):
            # A single-file non-recursive scan honors the passed options'
            # follow_symlinks (like the directory branch below), so it stats
            # through _stat_one directly rather than get_fileinfo (which reads the
            # storage's own config); compare_key is the basename, as get_fileinfo
            # stamps for a single entry.
            info = self._stat_one(root, follow_symlinks=options.follow_symlinks, notify=notify)
            if info is not None:
                info.compare_key = info.key.rsplit("/", 1)[-1]
                if item_filter is None or item_filter(info):
                    yield info
            return
        if options.return_directories:
            # A one-level scan lists the immediate entries; strictly, "return
            # directories" also includes the scanned directory itself, so lead
            # with its record - the same root_info the recursive walk prepends
            # (compare_key "", key ending in "/"), anchored with the trailing
            # separator. None (a race removed the root) simply drops it, as its
            # children are about to be. Stamped and filtered like every entry.
            root_record = self._walker.root_info(os.path.join(root, ""))
            if root_record is not None:
                root_record.storage = options.storage
                if item_filter is None or item_filter(root_record):
                    yield root_record
        # scan_children stamps compare_key as info.key[strip:]; strip is the
        # normalized root-prefix length (os.path.join(root, "") = root + a sep, the
        # common prefix of every child key). One level down this equals the name.
        strip = len(os.path.join(root, "").replace(os.sep, "/"))
        for _sort_name, info, _loop_key in self._walker.scan_children(
            root, strip=strip, options=options, notify=notify
        ):
            # Under return_symlinks + follow_symlinks, scan_children appends a
            # symlinked directory's target as a second, DIRECTORY-kind child so
            # the recursive walk descends it (the link is already out as its own
            # leaf). A one-level scan does not descend, so that companion is not
            # an entry here - it would just duplicate the link's path in followed
            # form. Drop it; follow_symlinks is a traversal knob and must not add
            # an entry at one level. A plain followed dir-symlink (no
            # return_symlinks) still surfaces as its DIRECTORY entry, S3-style.
            if options.return_symlinks and info.kind is FileKind.DIRECTORY and info.is_symlink:
                continue
            # scan_children stamps compare_key only; stamp the producing backend
            # here (options.storage == self, forced by scan_pages) before the filter.
            info.storage = options.storage
            if item_filter is None or item_filter(info):
                yield info

    def _stat_one(
        self, path: str, *, follow_symlinks: bool, notify: Callable[[str], None]
    ) -> LocalFileInfo | None:
        """One path's ``LocalFileInfo``, or ``None`` when there is no transferable entry.

        The local side of :meth:`Storage.get_fileinfo`'s contract: a
        ``follow_symlinks=False`` symlink, or a definitively absent path (``ENOENT`` -
        including a broken symlink when following), is a silent ``None``; a special /
        unreadable file warns via ``notify`` and returns ``None`` (aws-cli's
        warn-and-skip); a regular file or directory returns a ``LocalFileInfo`` (no
        type check, so a directory is returned and fails later at open). A stat error
        other than absence (e.g. a permission error reaching the path) is raised -
        existence could not be determined. ``compare_key`` is the caller's to stamp.

        One ``os.stat`` is taken and **reused** for the size / mtime, the
        special-file mode check, and the info's ``stat_result`` - no re-stat, so
        the checks and the stored snapshot cannot disagree and no TOCTOU window
        opens between them (the walk's single-stat design via
        :meth:`entry_stat_result` / :meth:`classify_child`). The info carries that
        one followed ``stat_result`` and the ``is_symlink`` flag.
        """
        is_symlink = os.path.islink(path)
        if not follow_symlinks and is_symlink:
            return None
        try:
            st = os.stat(path)  # the one snapshot, reused below (followed)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise translate_os_error(exc, operation="get_fileinfo", key=None) from exc
        if _is_special_mode(st.st_mode):
            notify(
                f"Skipping file {path}. File is character special device, "
                "block special device, FIFO, or socket."
            )
            return None
        if not is_readable(path, stat_module.S_ISDIR(st.st_mode)):
            notify(f"Skipping file {path}. File/Directory is not readable.")
            return None
        size, mtime = get_file_stat(path, st)
        if mtime is None:
            notify("File has an invalid timestamp. Passing epoch time as timestamp.")
            mtime = _EPOCH
        return LocalFileInfo(
            key=path.replace(os.sep, "/"),
            size=size,
            mtime=mtime,
            stat_result=st,
            is_symlink=is_symlink,
            storage=self,
        )

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        """Open ``key`` (resolved against the absolutized :attr:`path`) as a binary stream.

        ``"wb"`` creates missing parent directories first and writes **in
        place** - no temp-file + rename, so an aborted write leaves a partial
        file (the built-in s3->local download route is atomic-on-failure via
        s3transfer's temp file instead; this building block keeps the plain
        open-write contract). ``size`` is unused
        locally. OS errors translate to the library taxonomy.

        ``key`` is joined under the location with ``os.path.join``, so a ``..``
        or absolute ``key`` resolves outside it (``"../sib"`` -> the parent's
        ``sib``). That is deliberate, not a hole: this is a building block whose
        caller owns both the location and the key, so there is no trust boundary
        to confine - ``..`` should navigate the parent rather than be rejected.
        The one genuinely untrusted path - a *remote* S3 key steering a recursive
        download's local target - is guarded separately, where the key arrives
        from the bucket (``s3.py``'s ``_warn_parent_reference`` port, warn-and-skip
        for ``aws s3`` parity).
        """
        # Anchor on the construction-time absolutized path like scan /
        # get_fileinfo, so a later chdir cannot move where a relative
        # location's keys resolve. key="" is the location itself (the
        # get_fileinfo convention) - os.path.join(x, "") would append a
        # trailing separator and, on "wb", makedirs a directory at the
        # target file's own path before the open fails.
        target = self._abspath if not key else os.path.join(self._abspath, to_native_path(key))
        try:
            if mode == "rb":
                return cast("BinaryIO", open(target, "rb"))
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            return cast("BinaryIO", open(target, "wb"))
        except OSError as exc:
            raise translate_os_error(exc, operation="open", key=key) from exc

    @override
    def delete(self, info: FileInfo) -> None:
        """Remove the file at ``info.key`` (an absolute local path)."""
        target = to_native_path(info.key)
        try:
            os.remove(target)
        except OSError as exc:
            raise translate_os_error(exc, operation="delete", key=info.key) from exc

    @override
    def get_fileinfo(
        self,
        key: str = "",
        *,
        on_warning: Callable[[str], None] | None = None,
    ) -> LocalFileInfo | None:
        """Stat a single path (:meth:`Storage.get_fileinfo`).

        Anchored at the absolutized path (``self._abspath``, joined with ``key``
        for a child), so ``FileInfo.key`` is absolute; ``compare_key`` is its
        basename. As with :meth:`open`, ``key`` is joined under the location and
        a ``..`` / absolute ``key`` deliberately resolves outside it (a building
        block an app drives, not a confinement boundary - see :meth:`open`).
        Whether a symlink is followed is the storage's own ``follow_symlinks``
        config (constructor), like every scan this backend makes.
        """
        notify: Callable[[str], None] = (
            on_warning if on_warning is not None else (lambda body: None)
        )
        target = self._abspath
        if key:
            target = os.path.join(target, to_native_path(key))
        info = self._stat_one(target, follow_symlinks=self._follow_symlinks, notify=notify)
        if info is not None:
            info.compare_key = info.key.rsplit("/", 1)[-1]
        return info


__all__ = [
    "LocalFileGenerator",
    "LocalStorage",
    "LoopDetector",
    "WalkChild",
    "get_file_stat",
    "is_readable",
    "is_special_file",
    "to_native_path",
    "translate_os_error",
]
