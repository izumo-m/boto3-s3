"""Local filesystem storage backend: ``LocalStorage`` and the aws-cli-order walk.

:meth:`LocalStorage.walk_local` reproduces aws-cli's ``FileGenerator.list_files``
(aws-cli's awscli/customizations/s3/filegenerator.py) observable behaviour: the
same depth-first traversal whose per-directory sort key appends ``os.sep`` to
directory names and compares with separators normalized to ``/`` - so the stream
comes out in S3's UTF-8 byte order (``foo.txt`` before ``foo/bar``), which sync's
merge-join later relies on - and the same skip-with-warning rules (nonexistent /
special / unreadable files, broken symlinks, the invalid-timestamp epoch
fallback). :meth:`LocalStorage.scan_pages` wraps it for the ``Storage.scan`` seam
that cp / mv / sync enumerate through. aws-cli's Python-2 byte-filename decoding
warning has no Python-3 equivalent (a ``str`` directory scan always yields
``str``) and is not ported.

Unlike aws-cli's ``listdir`` + per-name ``os.path.isdir`` / ``os.stat`` restat
(which the parity tests still pin), the walk is built on :func:`os.scandir`, which
carries each entry's type (``d_type``) so ``is_dir`` / ``is_symlink`` cost no
syscall, and caches one followed ``stat`` per entry that the vetting battery and
the ``LocalFileInfo`` build both read. Where the platform supports it
(:data:`_HAVE_DIR_FD`, i.e. POSIX), the directory is opened once and scanned
through its file descriptor, so every per-entry ``stat`` / readability probe is
``dir_fd``-relative (``fstatat`` - no kernel path re-walk); on Windows, where
those APIs are absent, the same code path falls back to a path-based
:func:`os.scandir`, whose ``FindNextFile`` data already supplies the attributes
for free. The net effect is one ``stat`` per surviving entry (zero for a plain
directory) plus aws-cli's one readability ``open`` per entry, versus the ~5
restats per entry a naive port makes. The opt-in ``ScanOptions.capture_entry``
also forces the path-based scan (a ``dir_fd`` entry breaks once the fd closes, so
an entry a callback keeps must be path-form) and stamps each
``LocalFileInfo.entry`` with its ``os.DirEntry``.

The walk is a pipeline of **protected, overridable** ``LocalStorage`` methods -
:meth:`~LocalStorage._walk` (recursion), :meth:`~LocalStorage._scan_children`
(one directory's vetted, sorted children - shared with the one-level scan),
:meth:`~LocalStorage._should_ignore` (the symlink-skip + warning battery on one
``os.DirEntry``), :meth:`~LocalStorage._stat_info` (one walk entry's
``LocalFileInfo`` from the entry's cached stat), and
:meth:`~LocalStorage._stat_one` (a single *path*, for ``get_fileinfo``) - so a
subclass can extend it without re-implementing the whole walk. For example, a
Windows build that wants to follow Cygwin's ``!<symlink>`` files (which appear as
ordinary files to native Python) can override ``_should_ignore`` / ``_walk`` /
``_stat_info`` to resolve them; ``_should_ignore`` / ``_stat_info`` receive the
``os.DirEntry`` (plus its full path and the owning ``dir_fd``, ``None`` off
POSIX). The leaf helpers stay module-level for reuse from such overrides:
:func:`_entry_stat` reads a walk entry's size/mtime from its cached stat, and the
path-based :func:`_is_special_file` / :func:`_is_readable` / :func:`_file_stat` /
:func:`_stat_key` back ``get_fileinfo`` and the root vetting. The public
:class:`LoopDetector` is likewise module-level.
"""

from __future__ import annotations

import os
import stat as stat_module
from datetime import datetime, timezone
from typing import TYPE_CHECKING, ClassVar, Literal, cast

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

# Whether this platform can scan a directory through its file descriptor and
# stat/open entries relative to it (fstatat / openat - no kernel path re-walk).
# True on POSIX, False on Windows (which lacks dir_fd / O_DIRECTORY but returns
# entry attributes inline from FindNextFile, so the path-based fallback is just
# as cheap there). A feature probe, not an os.name check, so any platform
# missing the APIs degrades correctly.
_HAVE_DIR_FD = (
    os.scandir in os.supports_fd and os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")
)
# Flags for the one open() that turns a directory path into the fd we scan
# through. O_DIRECTORY / O_NONBLOCK are POSIX-only (guarded so the module still
# imports on Windows, where _HAVE_DIR_FD is False and these are never used).
_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NONBLOCK", 0)


def to_native_path(key: str) -> str:
    """Native form of a ``/``-separated ``FileInfo`` key.

    Lossless because no supported filesystem allows ``/`` inside a file name
    (it is the separator on POSIX and rejected on Windows).
    """
    return key.replace("/", os.sep)


def _is_special_mode(mode: int) -> bool:
    """Whether ``st_mode`` is a character/block device, FIFO, or socket.

    The mode-only core of aws-cli's ``is_special_file``, so a caller that
    already holds a ``stat`` (the walk, from ``os.DirEntry.stat``) checks the
    type without a second syscall.
    """
    return (
        stat_module.S_ISCHR(mode)
        or stat_module.S_ISBLK(mode)
        or stat_module.S_ISFIFO(mode)
        or stat_module.S_ISSOCK(mode)
    )


def _is_special_file(path: str) -> bool:
    """Character/block device, FIFO, or socket (aws-cli's ``is_special_file``)."""
    return _is_special_mode(os.stat(path).st_mode)


def _is_readable(path: str, is_dir: bool | None = None) -> bool:
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
    (``dir_fd is None``) it falls back to the path-based :func:`_is_readable`
    with the type already known.
    """
    if dir_fd is None:
        return _is_readable(full, is_dir)
    flags = os.O_RDONLY | (os.O_DIRECTORY if is_dir else 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except OSError:
        return False
    os.close(fd)
    return True


def _file_stat(path: str) -> tuple[int, datetime | None]:
    """Size and tz-aware mtime (aws-cli's ``get_file_stat``).

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


def _entry_stat(entry: os.DirEntry[str]) -> tuple[int, datetime | None]:
    """Size and tz-aware mtime for a walk entry (aws-cli's ``get_file_stat``).

    The :class:`os.DirEntry` analog of :func:`_file_stat`, reading the entry's
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


class LoopDetector:
    """Ancestor-stack guard against symbolic-link cycles in a recursive walk.

    A reusable building block for a custom recursive enumerator - an application
    walking its own backend, or driving :meth:`LocalStorage._walk`'s lower layer.
    It tracks the ``(st_dev, st_ino)`` identity of every directory on the path
    from the root down to the one being descended (an *ancestor stack*, not a
    global visited set, so a legitimate diamond - two symlinks to the same
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

    The recursive walk is split into protected, overridable methods (this
    module's docstring) so a subclass can extend it - e.g. to resolve Cygwin
    ``!<symlink>`` files on a native-Python Windows build.
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

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = os.fspath(path)
        # Absolutize once, at construction (against the cwd then): every scan /
        # get_fileinfo anchors here so ``FileInfo.key`` comes out absolute, and
        # ``os.path.abspath`` calls ``os.getcwd()`` - too costly to repeat per
        # entry. Binding the cwd here also keeps a relative path resolving
        # consistently if the process later chdir's.
        self._abspath = os.path.abspath(self._path)

    @property
    def path(self) -> str:
        return self._path

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

        Recursive enumeration streams :meth:`walk_local` in aws-cli byte order;
        ``options.follow_symlinks`` / ``options.detect_symlink_loops`` /
        ``options.on_warning`` / ``options.capture_entry`` are threaded into it -
        a symlink skip, the cycle guard, the aws-cli-worded warning channel a
        transfer needs, and the opt-in ``LocalFileInfo.entry`` capture.
        Non-recursive yields one level like the S3 backend (immediate entries in
        the same sort order, sub-directories as ``DIRECTORY``-kind infos whose key
        ends with ``/``), anchored at the absolutized path (``self._abspath``) so
        ``FileInfo.key`` is absolute. The S3 listing knobs on ``options``
        (``page_size`` / ``request_payer`` / ...) are ignored here (docs on
        ``ScanOptions``).
        """
        if options.recursive:
            yield from _paged(
                self.walk_local(
                    follow_symlinks=options.follow_symlinks,
                    detect_loops=options.detect_symlink_loops,
                    on_warning=options.on_warning,
                    capture_entry=options.capture_entry,
                )
            )
            return
        yield from _paged(
            self._scan_one_level(
                self._abspath,
                follow_symlinks=options.follow_symlinks,
                on_warning=options.on_warning,
                capture_entry=options.capture_entry,
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

        Depth-first; each directory's entries are vetted (warned entries never
        reach the sort), directory names gain ``os.sep``, and the page sorts by
        ``name.replace(os.sep, '/')`` - S3's UTF-8 byte order (``foo.txt`` before
        ``foo/bar``), which sync's merge-join relies on. Warnings carry the
        aws-cli message bodies and go to ``on_warning`` (dropped when ``None``);
        each warned entry is skipped. ``follow_symlinks=False`` skips symlinks
        silently. ``detect_loops=True`` (with ``follow_symlinks``) skips a
        directory that resolves to one of its own ancestors with a ``Symbolic link
        loop detected`` warning, instead of recursing until ``RecursionError`` (a
        library extension, off by default = ``aws s3`` behavior; off costs no
        extra ``stat``). ``FileInfo.key`` is the absolute path with ``os.sep``
        normalized to ``/`` (:func:`to_native_path` inverts it); each entry's
        ``compare_key`` is stamped here as its key relative to :attr:`path`. A
        single source object goes through :meth:`get_fileinfo` instead, not this
        walk. ``capture_entry=True`` stamps each ``LocalFileInfo.entry`` with its
        ``os.DirEntry`` (for a ``filter`` / ``on_result`` that reuses the cached
        stat); it scans by path instead of a directory fd so the entry stays
        usable afterwards (docs on ``ScanOptions.capture_entry``). Override the
        protected ``_walk`` / ``_should_ignore`` / ``_stat_info`` below to
        customize the traversal (this module's docstring).
        """
        notify: Callable[[str], None] = (
            on_warning if on_warning is not None else (lambda body: None)
        )
        # Anchor at the absolutized path with a trailing separator (aws-cli's
        # local_format(dir_op=True) form): FileInfo.key comes out absolute, and a
        # non-directory root degrades to a "does not exist" warning rather than a
        # scan error. self._abspath is computed once at construction.
        start = self._abspath + os.sep
        # Vet the root (aws-cli should_ignore_file on the formatted root): the
        # per-entry vetting lives in each parent's _scan_children, but the root
        # has no parent. A missing / special / unreadable root degrades to the
        # matching warning and an empty stream; a no-follow symlink root is
        # skipped silently. Path-based here (no DirEntry for the root); the
        # per-entry hot path is DirEntry-based.
        if self._root_should_ignore(start, follow_symlinks=follow_symlinks, notify=notify):
            return
        # No detector unless asked and reachable (a cycle needs a followed
        # symlink); None then costs no per-directory stat.
        detector = LoopDetector(start) if detect_loops and follow_symlinks else None
        # The trailing separator makes the normalized root end in "/", so the
        # slice below never leaves a leading separator.
        strip = len(start.replace(os.sep, "/"))
        for info in self._walk(
            start,
            follow_symlinks=follow_symlinks,
            notify=notify,
            detector=detector,
            capture_entry=capture_entry,
        ):
            info.compare_key = info.key[strip:]
            yield info

    def _root_should_ignore(
        self, start: str, *, follow_symlinks: bool, notify: Callable[[str], None]
    ) -> bool:
        """Path-based should_ignore for the walk root (aws-cli's ``should_ignore_file``).

        The children are vetted per-``DirEntry`` in :meth:`_should_ignore`, but
        the root has no parent scan, so it is vetted here from its path (``start``
        is the absolutized root with a trailing ``os.sep``). Same order and
        wording as the per-entry battery.
        """
        if not follow_symlinks:
            probe = start
            if os.path.isdir(probe) and probe.endswith(os.sep):
                # A trailing separator must be removed to test the link itself.
                probe = probe[:-1]
            if os.path.islink(probe):
                return True
        return self._triggers_warning(start, notify)

    def _triggers_warning(self, path: str, notify: Callable[[str], None]) -> bool:
        """Warn-and-skip checks on a *path*, aws-cli order and wording (``triggers_warning``).

        Path-based: the root vetting (:meth:`_root_should_ignore`) and the
        :meth:`_stat_info` race fallback. The per-entry hot path checks the same
        conditions from the ``DirEntry``'s cached stat in :meth:`_should_ignore`.
        """
        if not os.path.exists(path):
            notify(f"Skipping file {path}. File does not exist.")
            return True
        if _is_special_file(path):
            notify(
                f"Skipping file {path}. File is character special device, "
                "block special device, FIFO, or socket."
            )
            return True
        if not _is_readable(path):
            notify(f"Skipping file {path}. File/Directory is not readable.")
            return True
        return False

    def _walk(
        self,
        dir_path: str,
        *,
        follow_symlinks: bool,
        notify: Callable[[str], None],
        detector: LoopDetector | None,
        capture_entry: bool,
    ) -> Iterator[LocalFileInfo]:
        """The recursive worker behind :meth:`walk_local` (an override seam).

        Iterates one directory's vetted, sorted children (:meth:`_scan_children`):
        files are yielded, sub-directories (``kind == DIRECTORY``) recursed into
        depth-first so their contents interleave in byte order. ``dir_path`` was
        already vetted - by its parent's scan, or, for the root, by
        :meth:`walk_local`.
        """
        for sort_name, info, loop_key in self._scan_children(
            dir_path, follow_symlinks=follow_symlinks, notify=notify, capture_entry=capture_entry
        ):
            if info.kind != FileKind.DIRECTORY:
                yield info
                continue
            # A sub-directory: sort_name carries the trailing os.sep (the sort
            # suffix), so the child path and the loop warning read correctly.
            child = os.path.join(dir_path, sort_name)
            if detector is not None:
                if detector.is_cycle_key(loop_key):
                    notify(f"Skipping file {child.rstrip(os.sep)}. Symbolic link loop detected.")
                    continue
                try:
                    yield from self._walk(
                        child,
                        follow_symlinks=follow_symlinks,
                        notify=notify,
                        detector=detector,
                        capture_entry=capture_entry,
                    )
                finally:
                    detector.leave()
            else:
                yield from self._walk(
                    child,
                    follow_symlinks=follow_symlinks,
                    notify=notify,
                    detector=detector,
                    capture_entry=capture_entry,
                )

    def _scan_children(
        self,
        dir_path: str,
        *,
        follow_symlinks: bool,
        notify: Callable[[str], None],
        capture_entry: bool,
    ) -> list[tuple[str, LocalFileInfo, tuple[int, int] | None]]:
        """One directory's vetted children in aws-cli sort order (an override seam).

        Scans ``dir_path`` once with :func:`os.scandir`. Each surviving child is a
        ``(sort_name, info, loop_key)`` triple: a file's ``info`` is its
        ``LocalFileInfo`` (``kind == FILE``); a sub-directory's is a
        ``DIRECTORY``-kind ``LocalFileInfo`` (``sort_name`` gains a trailing
        ``os.sep``, ``loop_key`` is its ``(st_dev, st_ino)`` for cycle detection
        or ``None`` to fail open). Entries :meth:`_should_ignore` skips never
        reach the list. The sort folds ``os.sep`` to ``/``: str sorts by code
        point = UTF-8 byte order (S3's exact key order), and the fold puts a
        directory (``foo/``) after a sibling file (``foo.txt``) since ``/``
        (0x2F) > ``.`` (0x2E) - matching aws-cli.

        The default fast path scans through the directory's own fd where the
        platform allows (:data:`_HAVE_DIR_FD`), so each entry's stat and
        readability probe are dir-relative (``fstatat`` - no path re-walk). With
        ``capture_entry`` each ``info`` also carries its ``os.DirEntry``, and the
        scan runs by path (no fd): a fd-relative entry breaks once the fd is
        closed, so an entry a callback may keep past the walk must be path-form
        (full ``.path``, re-stats on demand). A directory that cannot be opened or
        scanned (a race after its parent vetted it readable) yields an empty list.
        """
        kept: list[tuple[str, LocalFileInfo, tuple[int, int] | None]] = []
        dir_fd: int | None = None
        # Capturing an entry a callback may keep forces path-form scanning (a
        # dir_fd entry breaks once the fd is closed); otherwise scan through the
        # fd for dir-relative stats where the platform supports it.
        use_fd = _HAVE_DIR_FD and not capture_entry
        try:
            if use_fd:
                dir_fd = os.open(dir_path, _DIR_OPEN_FLAGS)
            scan_target: int | str = dir_fd if dir_fd is not None else dir_path
            with os.scandir(scan_target) as it:
                for entry in it:
                    full = os.path.join(dir_path, entry.name)
                    if self._should_ignore(
                        entry, full, dir_fd, follow_symlinks=follow_symlinks, notify=notify
                    ):
                        continue
                    if entry.is_dir(follow_symlinks=True):
                        try:
                            # Cached by _should_ignore's stat - no syscall here.
                            st = entry.stat(follow_symlinks=True)
                            loop_key = (st.st_dev, st.st_ino) if st.st_ino else None
                        except OSError:
                            loop_key = None
                        dir_info = LocalFileInfo(
                            key=(full + os.sep).replace(os.sep, "/"),
                            kind=FileKind.DIRECTORY,
                            entry=entry if capture_entry else None,
                        )
                        kept.append((entry.name + os.sep, dir_info, loop_key))
                    else:
                        child_info = self._stat_info(entry, full, notify)
                        if child_info is not None:
                            if capture_entry:
                                child_info.entry = entry
                            kept.append((entry.name, child_info, None))
        except OSError:
            return []
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
        kept.sort(key=lambda item: item[0].replace(os.sep, "/"))
        return kept

    def _should_ignore(
        self,
        entry: os.DirEntry[str],
        full: str,
        dir_fd: int | None,
        *,
        follow_symlinks: bool,
        notify: Callable[[str], None],
    ) -> bool:
        """Silent symlink skip plus the warning battery (aws-cli's ``should_ignore_file``).

        Works on one :class:`os.DirEntry`: ``is_symlink`` is the free ``d_type``
        test, and the exists / special checks read the entry's followed stat
        (``os.DirEntry.stat`` - one ``fstatat`` through ``dir_fd`` where the
        platform allows, cached so the later ``is_dir`` / :meth:`_stat_info` add
        no syscall). ``full`` is the entry's absolute path (warning wording and
        the info key); ``dir_fd`` is the owning directory fd (``None`` off POSIX)
        for the dir-relative readability probe.
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

    def _stat_info(
        self, entry: os.DirEntry[str], full: str, notify: Callable[[str], None]
    ) -> LocalFileInfo | None:
        """One walk entry's ``LocalFileInfo`` from its cached stat (an override seam).

        ``entry`` has passed :meth:`_should_ignore`, so its stat is cached and
        :func:`_entry_stat` adds no syscall. On a race (the entry vanished since
        vetting) the path-based :meth:`_triggers_warning` runs, warning like
        aws-cli; an unrepresentable mtime keeps the file, stamped with the epoch
        (aws-cli's ``skip_file=False``).
        """
        try:
            size, mtime = _entry_stat(entry)
        except (OSError, ValueError):
            self._triggers_warning(full, notify)
            return None
        if mtime is None:
            # skip_file=False in aws-cli: warn but keep the file, stamped epoch.
            notify("File has an invalid timestamp. Passing epoch time as timestamp.")
            mtime = _EPOCH
        return LocalFileInfo(key=full.replace(os.sep, "/"), size=size, mtime=mtime)

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
            size, mtime = _file_stat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise translate_os_error(exc, operation="get_fileinfo", key=None) from exc
        if _is_special_file(path):
            notify(
                f"Skipping file {path}. File is character special device, "
                "block special device, FIFO, or socket."
            )
            return None
        if not _is_readable(path):
            notify(f"Skipping file {path}. File/Directory is not readable.")
            return None
        if mtime is None:
            notify("File has an invalid timestamp. Passing epoch time as timestamp.")
            mtime = _EPOCH
        return LocalFileInfo(key=path.replace(os.sep, "/"), size=size, mtime=mtime)

    def _scan_one_level(
        self,
        root: str,
        *,
        follow_symlinks: bool,
        on_warning: Callable[[str], None] | None,
        capture_entry: bool = False,
    ) -> Iterator[FileInfo]:
        notify: Callable[[str], None] = (
            on_warning if on_warning is not None else (lambda body: None)
        )
        if not os.path.isdir(root):
            info = self.get_fileinfo(follow_symlinks=follow_symlinks, on_warning=on_warning)
            if info is not None:
                yield info
            return
        for sort_name, info, _loop_key in self._scan_children(
            root, follow_symlinks=follow_symlinks, notify=notify, capture_entry=capture_entry
        ):
            # One level down: the entry name itself is the root-relative key (a
            # directory's sort_name / key already carry a trailing separator).
            info.compare_key = sort_name.replace(os.sep, "/")
            yield info

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


__all__ = ["LocalStorage", "LoopDetector", "to_native_path", "translate_os_error"]
