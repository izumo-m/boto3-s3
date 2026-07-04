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
``have_dir_fd`` / ``capture_entry`` / ``detect_loops`` / :class:`LoopDetector`).

Performance (the reason for the ``os.scandir`` engine, not aws-cli's ``listdir``
+ per-name ``os.path.isdir`` / ``os.stat`` restat): ``os.scandir`` carries each
entry's type (``d_type``) so ``is_dir`` / ``is_symlink`` cost no syscall, and
caches one followed ``stat`` per entry that the vetting battery and the
``LocalFileInfo`` build both read. Where the platform supports it
(:data:`LocalFileGenerator.have_dir_fd`, i.e. POSIX), the directory is opened
once and scanned through its file descriptor, so every per-entry ``stat`` /
readability probe is ``dir_fd``-relative (``fstatat`` - no kernel path re-walk);
on Windows those APIs are absent, and the same code path falls back to a
path-based scan whose ``FindNextFile`` data already supplies the attributes for
free. The net effect is one ``stat`` per surviving entry (zero for a plain
directory) plus aws-cli's one readability ``open`` per entry, versus the ~5
restats per entry a naive port makes. The opt-in ``ScanOptions.capture_entry``
also forces the path-based scan (a ``dir_fd`` entry breaks once the fd closes, so
an entry a callback keeps must be path-form) and stamps each
``LocalFileInfo.entry`` with its ``os.DirEntry``.

The override seams, finest first - extend at the smallest layer that fits:

- :meth:`LocalFileGenerator.should_ignore_entry` - the symlink-skip + warning
  battery on one ``os.DirEntry`` (aws-cli ``should_ignore_file``, DirEntry form);
- :meth:`LocalFileGenerator.stat_info` - one file entry's ``LocalFileInfo`` from
  its cached stat (aws-cli ``_safely_get_file_stats``), and
  :meth:`LocalFileGenerator.dir_child` its directory counterpart;
- :meth:`LocalFileGenerator.classify_child` - one ``os.DirEntry`` -> a
  :class:`WalkChild` or a skip, the natural per-entry override point;
- :meth:`LocalFileGenerator.scan_children` - one directory enumerated and sorted
  (override to change *how* a directory is read, reusing ``classify_child`` /
  ``dir_child`` / :meth:`~LocalFileGenerator.normalize_sort`);
- :meth:`LocalFileGenerator.list_files` - the recursion driver + root vetting.

The module-level helpers are public where an override needs them
(:func:`is_special_file` / :func:`is_readable` / :func:`get_file_stat` /
:func:`entry_stat`, and :class:`LoopDetector` / :class:`WalkChild`);
``LocalStorage.get_fileinfo`` (the single-path point op) reuses them too.
"""

from __future__ import annotations

import os
import stat as stat_module
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
from boto3_s3.types import FileInfo, FileKind, LocalFileInfo, ScanOptions

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from typing import BinaryIO

# aws-cli EPOCH_TIME: the stamp used when a file's mtime cannot be represented.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Entries per page yielded by scan_pages - the hand-off granularity of
# Storage.scan's prefetch worker (a stat batch, the local analog of one
# ListObjectsV2 page).
_LOCAL_PAGE_SIZE = 256


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


def get_file_stat(path: str) -> tuple[int, datetime | None]:
    """Size and tz-aware mtime for a path (aws-cli's ``get_file_stat``).

    ``OSError`` from ``os.stat`` propagates (the caller runs the warning
    battery); an unrepresentable timestamp returns ``None`` for the caller's
    epoch fallback. The instant is the file's mtime; it is represented in UTC
    per the ``FileInfo.mtime`` contract (aws-cli uses the local zone - same
    instant either way).
    """
    stats = os.stat(path)
    try:
        mtime: datetime | None = datetime.fromtimestamp(stats.st_mtime, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        mtime = None
    return stats.st_size, mtime


def entry_stat(entry: os.DirEntry[str]) -> tuple[int, datetime | None]:
    """Size and tz-aware mtime for a walk entry (the ``os.DirEntry`` fast path).

    The :class:`os.DirEntry` analog of :func:`get_file_stat`, reading the entry's
    followed stat (cached by the ``DirEntry`` - shared with the vetting battery,
    so one ``fstatat`` covers both). ``OSError`` propagates (the caller runs the
    warning battery); an unrepresentable timestamp returns ``None`` for the
    caller's epoch fallback.
    """
    st = entry.stat(follow_symlinks=True)
    try:
        mtime: datetime | None = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        mtime = None
    return st.st_size, mtime


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

    The class is stateless across a walk: the per-walk knobs (``follow_symlinks``
    / ``detect_loops`` / ``capture_entry`` / ``notify``) are passed to
    :meth:`list_files` (aws-cli carries them on the instance instead, but here one
    walker serves every scan the owning ``LocalStorage`` makes).
    """

    #: Whether this platform can scan a directory through its file descriptor and
    #: stat/open entries relative to it (fstatat / openat - no kernel path
    #: re-walk). True on POSIX, False on Windows (which lacks dir_fd / O_DIRECTORY
    #: but returns entry attributes inline from FindNextFile). A feature probe,
    #: not an os.name check, so any platform missing the APIs degrades correctly.
    have_dir_fd: ClassVar[bool] = (
        os.scandir in os.supports_fd
        and os.open in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
    )
    #: Flags for the one open() that turns a directory path into the fd we scan
    #: through (POSIX; O_DIRECTORY / O_NONBLOCK are absent and unused off it).
    dir_open_flags: ClassVar[int] = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    #: The stamp for an unrepresentable mtime (aws-cli's ``EPOCH_TIME``).
    EPOCH_TIME: ClassVar[datetime] = _EPOCH

    def list_files(
        self,
        root: str,
        *,
        follow_symlinks: bool = True,
        detect_loops: bool = False,
        capture_entry: bool = False,
        item_filter: Callable[[FileInfo], bool] | None = None,
        notify: Callable[[str], None] | None = None,
    ) -> Iterator[LocalFileInfo]:
        """Yield every file under ``root`` (recursively) in aws-cli byte order.

        aws-cli's ``FileGenerator.list_files`` (the ``dir_op=True`` branch): a
        depth-first walk whose per-directory sort appends ``os.sep`` to directory
        names, so the stream is S3's UTF-8 byte order (``foo.txt`` before
        ``foo/bar``). ``root`` is an absolute path with a trailing ``os.sep``;
        each yielded ``LocalFileInfo.key`` is the absolute path (``os.sep`` folded
        to ``/``) and ``compare_key`` is stamped here as the key relative to
        ``root`` (what ``item_filter`` matches).

        The root is vetted with :meth:`should_ignore_file` (a missing / special /
        unreadable root, or a no-follow symlink root, yields nothing, warning as
        aws-cli does); its children are vetted per entry in :meth:`scan_children`.
        ``follow_symlinks=False`` skips symlinks silently. ``detect_loops=True``
        (with ``follow_symlinks``) skips a directory that resolves to one of its
        own ancestors with a ``Symbolic link loop detected`` warning (a library
        extension, off = ``aws s3`` behaviour, no extra ``stat``).
        ``capture_entry=True`` stamps each ``LocalFileInfo.entry`` with its
        ``os.DirEntry`` (scanning by path so the entry stays usable afterwards).
        ``item_filter`` (the ``ScanOptions.filter`` predicate) drops entries it
        rejects - applied *after* the aws-cli vetting so an excluded file still
        emits the warnings aws-cli would (exit-code parity), and yielding only the
        survivors saves paging them downstream. Warnings carry the aws-cli message
        bodies and go to ``notify``.
        """
        emit: Callable[[str], None] = notify if notify is not None else (lambda body: None)
        if self.should_ignore_file(root, follow_symlinks=follow_symlinks, notify=emit):
            return
        # No detector unless asked and reachable (a cycle needs a followed
        # symlink); None then costs no per-directory stat.
        detector = LoopDetector(root) if detect_loops and follow_symlinks else None
        # root ends in os.sep, so the normalized root ends in "/" and the slice
        # never leaves a leading separator; compare_key is the key relative to root.
        strip = len(root.replace(os.sep, "/"))
        for info in self._descend(
            root,
            follow_symlinks=follow_symlinks,
            notify=emit,
            detector=detector,
            capture_entry=capture_entry,
        ):
            info.compare_key = info.key[strip:]
            if item_filter is None or item_filter(info):
                yield info

    def _descend(
        self,
        dir_path: str,
        *,
        follow_symlinks: bool,
        notify: Callable[[str], None],
        detector: LoopDetector | None,
        capture_entry: bool,
    ) -> Iterator[LocalFileInfo]:
        """The recursion behind :meth:`list_files` - files yielded, directories
        (``kind == DIRECTORY``) recursed into depth-first so their contents
        interleave in byte order. ``dir_path`` was already vetted (as the root, or
        as a child in its parent's :meth:`scan_children`)."""
        for sort_name, info, loop_key in self.scan_children(
            dir_path, follow_symlinks=follow_symlinks, notify=notify, capture_entry=capture_entry
        ):
            if info.kind != FileKind.DIRECTORY:
                yield info
                continue
            # A sub-directory: sort_name carries the trailing os.sep (the sort
            # suffix), so the child path and the loop warning read correctly.
            sub = os.path.join(dir_path, sort_name)
            if detector is not None:
                if detector.is_cycle_key(loop_key):
                    notify(f"Skipping file {sub.rstrip(os.sep)}. Symbolic link loop detected.")
                    continue
                try:
                    yield from self._descend(
                        sub,
                        follow_symlinks=follow_symlinks,
                        notify=notify,
                        detector=detector,
                        capture_entry=capture_entry,
                    )
                finally:
                    detector.leave()
            else:
                yield from self._descend(
                    sub,
                    follow_symlinks=follow_symlinks,
                    notify=notify,
                    detector=detector,
                    capture_entry=capture_entry,
                )

    def scan_children(
        self,
        dir_path: str,
        *,
        follow_symlinks: bool,
        notify: Callable[[str], None],
        capture_entry: bool,
    ) -> list[WalkChild]:
        """One directory's vetted children as sorted :class:`WalkChild`\\ s.

        The enumeration layer (no aws-cli counterpart - aws-cli uses ``listdir``):
        it scans ``dir_path`` once with :func:`os.scandir`, turns each entry into
        a :class:`WalkChild` via :meth:`classify_child` (skips return ``None``),
        and orders them with :meth:`normalize_sort`. A directory that cannot be
        opened or scanned (a race after its parent vetted it readable) yields an
        empty list.

        The default fast path scans through the directory's own fd where the
        platform allows (:data:`have_dir_fd`), so each entry's stat and
        readability probe are dir-relative (``fstatat`` - no path re-walk). With
        ``capture_entry`` the scan runs by path (no fd): a fd-relative entry
        breaks once the fd is closed, so an entry a callback may keep past the
        walk must be path-form (full ``.path``, re-stats on demand).

        Override this to change how a directory is enumerated - e.g. to inject
        synthetic entries or read a different source - reusing
        :meth:`classify_child` / :meth:`dir_child` / :meth:`normalize_sort` so the
        sort key and directory-info shape need not be reproduced. For a smaller
        change, override :meth:`classify_child` (or :meth:`should_ignore_entry` /
        :meth:`stat_info`) instead.
        """
        children: list[WalkChild] = []
        dir_fd: int | None = None
        # Capturing an entry a callback may keep forces path-form scanning (a
        # dir_fd entry breaks once the fd is closed); otherwise scan through the
        # fd for dir-relative stats where the platform supports it.
        use_fd = self.have_dir_fd and not capture_entry
        try:
            if use_fd:
                dir_fd = os.open(dir_path, self.dir_open_flags)
            scan_target: int | str = dir_fd if dir_fd is not None else dir_path
            with os.scandir(scan_target) as it:
                for entry in it:
                    child = self.classify_child(
                        entry,
                        os.path.join(dir_path, entry.name),
                        dir_fd,
                        follow_symlinks=follow_symlinks,
                        notify=notify,
                        capture_entry=capture_entry,
                    )
                    if child is not None:
                        children.append(child)
        except OSError:
            return []
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
        return self.normalize_sort(children)

    def classify_child(
        self,
        entry: os.DirEntry[str],
        full: str,
        dir_fd: int | None,
        *,
        follow_symlinks: bool,
        notify: Callable[[str], None],
        capture_entry: bool,
    ) -> WalkChild | None:
        """One ``os.DirEntry`` -> a :class:`WalkChild`, or ``None`` to skip it.

        The per-entry decision, the natural place to customize the walk without
        touching the scan loop: it vets via :meth:`should_ignore_entry`, then
        builds a file's :meth:`stat_info` info or a sub-directory's
        :meth:`dir_child`. ``full`` is the entry's absolute path, ``dir_fd`` the
        owning directory fd (``None`` off POSIX / when capturing). With
        ``capture_entry`` the file info also carries its ``os.DirEntry``.
        """
        if self.should_ignore_entry(
            entry, full, dir_fd, follow_symlinks=follow_symlinks, notify=notify
        ):
            return None
        if entry.is_dir(follow_symlinks=True):
            return self.dir_child(entry, full, capture_entry=capture_entry)
        info = self.stat_info(entry, full, notify)
        if info is None:
            return None
        if capture_entry:
            info.entry = entry
        return WalkChild(entry.name, info, None)

    def dir_child(self, entry: os.DirEntry[str], full: str, *, capture_entry: bool) -> WalkChild:
        """A sub-directory's :class:`WalkChild` - its ``DIRECTORY`` info and loop key.

        ``sort_name`` and the info key carry a trailing ``os.sep`` (so the
        directory sorts after a sibling file of the same stem); ``loop_key`` is
        ``(st_dev, st_ino)`` from the entry's cached stat (``None`` to fail open).
        """
        try:
            # Cached by should_ignore_entry's stat - no syscall here.
            st = entry.stat(follow_symlinks=True)
            loop_key = (st.st_dev, st.st_ino) if st.st_ino else None
        except OSError:
            loop_key = None
        info = LocalFileInfo(
            key=(full + os.sep).replace(os.sep, "/"),
            kind=FileKind.DIRECTORY,
            entry=entry if capture_entry else None,
        )
        return WalkChild(entry.name + os.sep, info, loop_key)

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
        *,
        follow_symlinks: bool,
        notify: Callable[[str], None],
    ) -> bool:
        """:meth:`should_ignore_file` on one ``os.DirEntry`` (the DirEntry fast path).

        ``is_symlink`` is the free ``d_type`` test, and the exists / special
        checks read the entry's followed stat (``os.DirEntry.stat`` - one
        ``fstatat`` through ``dir_fd`` where the platform allows, cached so the
        later ``is_dir`` / :meth:`stat_info` add no syscall). ``full`` is the
        entry's absolute path (warning wording and the info key); ``dir_fd`` is
        the owning directory fd (``None`` off POSIX) for the dir-relative
        readability probe. Same order / wording as :meth:`triggers_warning`.
        """
        if not follow_symlinks and entry.is_symlink():
            return True
        try:
            st = entry.stat(follow_symlinks=True)
        except OSError:
            notify(f"Skipping file {full}. File does not exist.")
            return True
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
        self, entry: os.DirEntry[str], full: str, notify: Callable[[str], None]
    ) -> LocalFileInfo | None:
        """One file entry's ``LocalFileInfo`` from its cached stat (aws-cli's
        ``_safely_get_file_stats``).

        ``entry`` has passed :meth:`should_ignore_entry`, so its stat is cached
        and :func:`entry_stat` adds no syscall. On a race (the entry vanished
        since vetting) the path-based :meth:`triggers_warning` runs, warning like
        aws-cli; an unrepresentable mtime keeps the file, stamped with
        :data:`EPOCH_TIME` (aws-cli's ``skip_file=False``).
        """
        try:
            size, mtime = entry_stat(entry)
        except (OSError, ValueError):
            self.triggers_warning(full, notify)
            return None
        if mtime is None:
            # skip_file=False in aws-cli: warn but keep the file, stamped epoch.
            notify("File has an invalid timestamp. Passing epoch time as timestamp.")
            mtime = self.EPOCH_TIME
        return LocalFileInfo(key=full.replace(os.sep, "/"), size=size, mtime=mtime)


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
    """

    scheme: ClassVar[str] = "local"
    #: Local roots and keys are host-native (:meth:`format` returns ``os.sep``
    #: forms), unlike every other backend's ``/`` space.
    sep: ClassVar[str] = os.sep
    #: The local filesystem supports every transfer operation: byte I/O both
    #: ways, single-entry stat, a sorted walk (so ``SORTED_SCAN``), and delete.
    capabilities: ClassVar[StorageCapability] = (
        StorageCapability.OPEN_READ
        | StorageCapability.OPEN_WRITE
        | StorageCapability.GET_FILEINFO
        | StorageCapability.SCAN
        | StorageCapability.SORTED_SCAN
        | StorageCapability.DELETE
    )

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
        self, path: str | os.PathLike[str], *, walker: LocalFileGenerator | None = None
    ) -> None:
        self._path = os.fspath(path)
        # Absolutize once, at construction (against the cwd then): every scan /
        # get_fileinfo anchors here so ``FileInfo.key`` comes out absolute, and
        # ``os.path.abspath`` calls ``os.getcwd()`` - too costly to repeat per
        # entry. Binding the cwd here also keeps a relative path resolving
        # consistently if the process later chdir's.
        self._abspath = os.path.abspath(self._path)
        #: The directory-walk strategy (the default fast walk, or an app's).
        self._walker = walker if walker is not None else LocalFileGenerator()

    @property
    def path(self) -> str:
        return self._path

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
    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
        """Yield entries under :attr:`path` one stat batch at a time.

        Recursive enumeration drives the walker's
        :meth:`~LocalFileGenerator.list_files` in aws-cli byte order;
        ``options.follow_symlinks`` / ``options.detect_symlink_loops`` /
        ``options.on_warning`` / ``options.capture_entry`` / ``options.filter``
        are threaded into it - a symlink skip, the cycle guard, the aws-cli-worded
        warning channel a transfer needs, the opt-in ``LocalFileInfo.entry``
        capture, and the per-entry filter (applied in the walk, after vetting -
        :meth:`Storage.scan_pages`'s "return filtered pages" contract).
        Non-recursive yields one level like the S3 backend (immediate entries in
        the same sort order, sub-directories as ``DIRECTORY``-kind infos whose key
        ends with ``/``), anchored at the absolutized path (``self._abspath``) so
        ``FileInfo.key`` is absolute. The S3 listing knobs on ``options``
        (``page_size`` / ``request_payer`` / ...) are ignored here (docs on
        ``ScanOptions``).
        """
        if options.recursive:
            yield from _paged(
                self._walker.list_files(
                    self._abspath + os.sep,
                    follow_symlinks=options.follow_symlinks,
                    detect_loops=options.detect_symlink_loops,
                    capture_entry=options.capture_entry,
                    item_filter=options.filter,
                    notify=options.on_warning,
                )
            )
            return
        yield from _paged(
            self._scan_one_level(
                self._abspath,
                follow_symlinks=options.follow_symlinks,
                on_warning=options.on_warning,
                capture_entry=options.capture_entry,
                item_filter=options.filter,
            )
        )

    def walk_local(
        self,
        *,
        follow_symlinks: bool = True,
        detect_loops: bool = False,
        on_warning: Callable[[str], None] | None = None,
        capture_entry: bool = False,
    ) -> Iterator[LocalFileInfo]:
        """Yield every file under :attr:`path` (recursively) in aws-cli's byte order.

        A thin wrapper over :meth:`self.walker.list_files <LocalFileGenerator.list_files>`:
        it anchors at the absolutized path with a trailing ``os.sep`` (so
        ``FileInfo.key`` is absolute, ``compare_key`` comes out relative to
        :attr:`path`, and a non-directory root degrades to a "does not exist"
        warning rather than a scan error). The knobs (``follow_symlinks`` /
        ``detect_loops`` / ``capture_entry`` / warnings) and the byte-order /
        warning semantics are the walker's - see
        :meth:`LocalFileGenerator.list_files`. This is the raw walk (no
        ``ScanOptions.filter``); ``scan`` applies the filter through
        :meth:`scan_pages`. A single source object goes through
        :meth:`get_fileinfo` instead, not this walk.
        """
        # local_format(dir_op=True) form; self._abspath is computed once at
        # construction. list_files stamps compare_key (key relative to root).
        yield from self._walker.list_files(
            self._abspath + os.sep,
            follow_symlinks=follow_symlinks,
            detect_loops=detect_loops,
            capture_entry=capture_entry,
            notify=on_warning,
        )

    def _scan_one_level(
        self,
        root: str,
        *,
        follow_symlinks: bool,
        on_warning: Callable[[str], None] | None,
        capture_entry: bool = False,
        item_filter: Callable[[FileInfo], bool] | None = None,
    ) -> Iterator[FileInfo]:
        notify: Callable[[str], None] = (
            on_warning if on_warning is not None else (lambda body: None)
        )
        if not os.path.isdir(root):
            info = self.get_fileinfo(follow_symlinks=follow_symlinks, on_warning=on_warning)
            if info is not None and (item_filter is None or item_filter(info)):
                yield info
            return
        for sort_name, info, _loop_key in self._walker.scan_children(
            root, follow_symlinks=follow_symlinks, notify=notify, capture_entry=capture_entry
        ):
            # One level down: the entry name itself is the root-relative key (a
            # directory's sort_name / key already carry a trailing separator).
            info.compare_key = sort_name.replace(os.sep, "/")
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
        """
        if not follow_symlinks and os.path.islink(path):
            return None
        try:
            size, mtime = get_file_stat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise translate_os_error(exc, operation="get_fileinfo", key=None) from exc
        if is_special_file(path):
            notify(
                f"Skipping file {path}. File is character special device, "
                "block special device, FIFO, or socket."
            )
            return None
        if not is_readable(path):
            notify(f"Skipping file {path}. File/Directory is not readable.")
            return None
        if mtime is None:
            notify("File has an invalid timestamp. Passing epoch time as timestamp.")
            mtime = _EPOCH
        return LocalFileInfo(key=path.replace(os.sep, "/"), size=size, mtime=mtime)

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
        follow_symlinks: bool = True,
        on_warning: Callable[[str], None] | None = None,
    ) -> LocalFileInfo | None:
        """Stat a single path (:meth:`Storage.get_fileinfo`).

        Anchored at the absolutized path (``self._abspath``, joined with ``key``
        for a child), so ``FileInfo.key`` is absolute; ``compare_key`` is its
        basename. As with :meth:`open`, ``key`` is joined under the location and
        a ``..`` / absolute ``key`` deliberately resolves outside it (a building
        block an app drives, not a confinement boundary - see :meth:`open`).
        """
        notify: Callable[[str], None] = (
            on_warning if on_warning is not None else (lambda body: None)
        )
        target = self._abspath
        if key:
            target = os.path.join(target, to_native_path(key))
        info = self._stat_one(target, follow_symlinks=follow_symlinks, notify=notify)
        if info is not None:
            info.compare_key = info.key.rsplit("/", 1)[-1]
        return info


def _paged(infos: Iterator[FileInfo]) -> Iterator[list[FileInfo]]:
    page: list[FileInfo] = []
    for info in infos:
        page.append(info)
        if len(page) >= _LOCAL_PAGE_SIZE:
            yield page
            page = []
    if page:
        yield page


__all__ = [
    "LocalFileGenerator",
    "LocalStorage",
    "LoopDetector",
    "WalkChild",
    "entry_stat",
    "get_file_stat",
    "is_readable",
    "is_special_file",
    "to_native_path",
    "translate_os_error",
]
