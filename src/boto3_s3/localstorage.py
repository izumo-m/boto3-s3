"""Local filesystem storage backend: ``LocalStorage`` and the aws-cli-order walk.

:meth:`LocalStorage.walk_local` is a faithful port of aws-cli's
``FileGenerator.list_files`` (aws-cli's awscli/customizations/s3/filegenerator.py):
the same depth-first traversal whose per-directory sort key appends ``os.sep`` to
directory names and compares with separators normalized to ``/`` - so the stream
comes out in S3's UTF-8 byte order (``foo.txt`` before ``foo/bar``), which sync's
merge-join later relies on - and the same skip-with-warning rules (nonexistent /
special / unreadable files, broken symlinks, the invalid-timestamp epoch
fallback). :meth:`LocalStorage.scan_pages` wraps it for the ``Storage.scan`` seam
that cp / mv / sync enumerate through. aws-cli's Python-2 byte-filename decoding
warning has no Python-3 equivalent (``os.listdir`` of a ``str`` path always yields
``str``) and is not ported.

The walk is a pipeline of **protected, overridable** ``LocalStorage`` methods -
:meth:`~LocalStorage._walk` (recursion), :meth:`~LocalStorage._should_ignore` (the
symlink-skip + warning battery), :meth:`~LocalStorage._triggers_warning`,
:meth:`~LocalStorage._stat_info` (one walk entry's ``LocalFileInfo``), and
:meth:`~LocalStorage._stat_one` (a single path, for ``get_fileinfo``) - so a
subclass can extend it without re-implementing the whole walk. For example, a
Windows build that wants to follow Cygwin's ``!<symlink>`` files (which appear as
ordinary files to native Python) can override ``_should_ignore`` / ``_walk`` /
``_stat_info`` to resolve them. The leaf ``os.stat`` helpers (:func:`_is_special_file`
/ :func:`_is_readable` / :func:`_file_stat` / :func:`_stat_key`) and the public
:class:`LoopDetector` stay module-level for reuse from such overrides.
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


def to_native_path(key: str) -> str:
    """Native form of a ``/``-separated ``FileInfo`` key.

    Lossless because no supported filesystem allows ``/`` inside a file name
    (it is the separator on POSIX and rejected on Windows).
    """
    return key.replace("/", os.sep)


def _is_special_file(path: str) -> bool:
    """Character/block device, FIFO, or socket (aws-cli's ``is_special_file``)."""
    mode = os.stat(path).st_mode
    return (
        stat_module.S_ISCHR(mode)
        or stat_module.S_ISBLK(mode)
        or stat_module.S_ISFIFO(mode)
        or stat_module.S_ISSOCK(mode)
    )


def _is_readable(path: str) -> bool:
    """Probe read access by performing a read operation (aws-cli's ``is_readable``).

    aws-cli deliberately opens/lists instead of ``os.access`` - the probe
    answers what the transfer itself will do.
    """
    if os.path.isdir(path):
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
        key = _stat_key(path)
        if key is not None and key in self._ancestors:
            return True
        self._ancestors.append(key)  # None (fail-open) never matches an ancestor
        return False

    def leave(self) -> None:
        """Pop the directory pushed by the matching non-cycle :meth:`is_cycle`."""
        self._ancestors.pop()


def _translate_os_error(exc: OSError, *, operation: str, key: str | None) -> Boto3S3Error:
    """Map an ``OSError`` to the library taxonomy (mirror of ``s3_errors``)."""
    if isinstance(exc, FileNotFoundError):
        return NotFoundError(str(exc), operation=operation, key=key)
    if isinstance(exc, PermissionError):
        return AccessDeniedError(str(exc), operation=operation, key=key)
    return TransportError(str(exc), operation=operation, key=key)


class LocalStorage(Storage):
    """A local filesystem path as one side of a transfer.

    The recursive walk is split into protected, overridable methods (this
    module's docstring) so a subclass can extend it - e.g. to resolve Cygwin
    ``!<symlink>`` files on a native-Python Windows build.
    """

    scheme: ClassVar[str] = "local"
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

        The raw constructor form, verbatim - the trailing-separator rule that
        ``naming`` applies reads this unmodified token.
        """
        return self._path

    @override
    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
        """Yield entries under :attr:`path` one stat batch at a time.

        Recursive enumeration streams :meth:`walk_local` in aws-cli byte order;
        ``options.follow_symlinks`` / ``options.detect_symlink_loops`` /
        ``options.on_warning`` are threaded into it - a symlink skip, the cycle
        guard, and the aws-cli-worded warning channel a transfer needs.
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
                )
            )
            return
        yield from _paged(
            self._scan_one_level(
                self._abspath,
                follow_symlinks=options.follow_symlinks,
                on_warning=options.on_warning,
            )
        )

    def walk_local(
        self,
        *,
        follow_symlinks: bool = True,
        detect_loops: bool = False,
        on_warning: Callable[[str], None] | None = None,
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
        walk. Override the protected ``_walk`` / ``_should_ignore`` / ``_stat_info``
        below to customize the traversal (this module's docstring).
        """
        notify: Callable[[str], None] = (
            on_warning if on_warning is not None else (lambda body: None)
        )
        # Anchor at the absolutized path with a trailing separator (aws-cli's
        # local_format(dir_op=True) form): FileInfo.key comes out absolute, and a
        # non-directory root degrades to a "does not exist" warning rather than an
        # os.listdir error. self._abspath is computed once at construction.
        start = self._abspath + os.sep
        # No detector unless asked and reachable (a cycle needs a followed
        # symlink); None then costs no per-directory stat.
        detector = LoopDetector(start) if detect_loops and follow_symlinks else None
        # The trailing separator makes the normalized root end in "/", so the
        # slice below never leaves a leading separator.
        strip = len(start.replace(os.sep, "/"))
        for info in self._walk(
            start, follow_symlinks=follow_symlinks, notify=notify, detector=detector
        ):
            info.compare_key = info.key[strip:]
            yield info

    def _walk(
        self,
        path: str,
        *,
        follow_symlinks: bool,
        notify: Callable[[str], None],
        detector: LoopDetector | None,
    ) -> Iterator[LocalFileInfo]:
        """The recursive worker behind :meth:`walk_local` (an override seam)."""
        if self._should_ignore(path, follow_symlinks=follow_symlinks, notify=notify):
            return
        names: list[str] = []
        for name in os.listdir(path):
            entry_path = os.path.join(path, name)
            if self._should_ignore(entry_path, follow_symlinks=follow_symlinks, notify=notify):
                continue
            if os.path.isdir(entry_path):
                name += os.sep
            names.append(name)
        # str sorts by code point, which equals UTF-8 byte order (UTF-8 preserves
        # code-point order), so this is S3's exact key order, not an approximation.
        # The os.sep -> "/" fold puts a directory ("foo/") after a sibling file
        # ("foo.txt"), since "/" (0x2F) > "." (0x2E) - matching aws-cli.
        names.sort(key=lambda item: item.replace(os.sep, "/"))
        for name in names:
            entry_path = os.path.join(path, name)
            if os.path.isdir(entry_path):
                if detector is not None and detector.is_cycle(entry_path):
                    # entry_path carries the directory's trailing os.sep (the sort
                    # suffix); drop it so the warning path reads like the others.
                    notify(
                        f"Skipping file {entry_path.rstrip(os.sep)}. Symbolic link loop detected."
                    )
                    continue
                try:
                    yield from self._walk(
                        entry_path,
                        follow_symlinks=follow_symlinks,
                        notify=notify,
                        detector=detector,
                    )
                finally:
                    if detector is not None:
                        detector.leave()
            else:
                info = self._stat_info(entry_path, notify)
                if info is not None:
                    yield info

    def _should_ignore(
        self, path: str, *, follow_symlinks: bool, notify: Callable[[str], None]
    ) -> bool:
        """Silent symlink skip plus the warning battery (aws-cli's ``should_ignore_file``)."""
        if not follow_symlinks:
            probe = path
            if os.path.isdir(probe) and probe.endswith(os.sep):
                # A trailing separator must be removed to test the link itself.
                probe = probe[:-1]
            if os.path.islink(probe):
                return True
        return self._triggers_warning(path, notify)

    def _triggers_warning(self, path: str, notify: Callable[[str], None]) -> bool:
        """Warn-and-skip checks, aws-cli order and wording (``triggers_warning``)."""
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

    def _stat_info(self, path: str, notify: Callable[[str], None]) -> LocalFileInfo | None:
        """One walk entry, or ``None`` when the stat itself warned the file away."""
        try:
            size, mtime = _file_stat(path)
        except (OSError, ValueError):
            self._triggers_warning(path, notify)
            return None
        if mtime is None:
            # skip_file=False in aws-cli: warn but keep the file, stamped epoch.
            notify("File has an invalid timestamp. Passing epoch time as timestamp.")
            mtime = _EPOCH
        return LocalFileInfo(key=path.replace(os.sep, "/"), size=size, mtime=mtime)

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
            raise _translate_os_error(exc, operation="get_fileinfo", key=None) from exc
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
        self, root: str, *, follow_symlinks: bool, on_warning: Callable[[str], None] | None
    ) -> Iterator[FileInfo]:
        notify: Callable[[str], None] = (
            on_warning if on_warning is not None else (lambda body: None)
        )
        if not os.path.isdir(root):
            info = self.get_fileinfo(follow_symlinks=follow_symlinks, on_warning=on_warning)
            if info is not None:
                yield info
            return
        names: list[str] = []
        for name in os.listdir(root):
            entry_path = os.path.join(root, name)
            if self._should_ignore(entry_path, follow_symlinks=follow_symlinks, notify=notify):
                continue
            if os.path.isdir(entry_path):
                name += os.sep
            names.append(name)
        names.sort(key=lambda item: item.replace(os.sep, "/"))
        for name in names:
            entry_path = os.path.join(root, name)
            # One level down: the entry name itself is the root-relative key
            # (directory names already carry a trailing separator).
            compare_key = name.replace(os.sep, "/")
            if os.path.isdir(entry_path):
                yield LocalFileInfo(
                    key=entry_path.replace(os.sep, "/"),
                    kind=FileKind.DIRECTORY,
                    compare_key=compare_key,
                )
            else:
                info = self._stat_info(entry_path, notify)
                if info is not None:
                    info.compare_key = compare_key
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
        # location's keys resolve.
        target = os.path.join(self._abspath, to_native_path(key))
        try:
            if mode == "rb":
                return cast("BinaryIO", open(target, "rb"))
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            return cast("BinaryIO", open(target, "wb"))
        except OSError as exc:
            raise _translate_os_error(exc, operation="open", key=key) from exc

    @override
    def delete(self, info: FileInfo) -> None:
        """Remove the file at ``info.key`` (an absolute local path)."""
        target = to_native_path(info.key)
        try:
            os.remove(target)
        except OSError as exc:
            raise _translate_os_error(exc, operation="delete", key=info.key) from exc

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


__all__ = ["LocalStorage", "LoopDetector", "to_native_path"]
