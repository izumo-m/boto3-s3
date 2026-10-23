"""Local filesystem storage backend: ``LocalStorage``, ``LocalFileGenerator``, the walk.

``LocalFileGenerator`` is the customizable local directory walk - boto3-s3's
counterpart to aws-cli's ``FileGenerator`` (aws-cli's
awscli/customizations/s3/filegenerator.py), scoped to the local filesystem (the
S3 listing is ``S3Storage``'s). ``list_files``
reproduces aws-cli's ``list_files`` observable behaviour: the same depth-first
traversal whose per-directory sort key appends ``os.sep`` to directory names and
compares with separators normalized to ``/`` - so the stream comes out in S3's
UTF-8 byte order (``foo.txt`` before ``foo/bar``), which sync's merge-join relies
on - and the same skip-with-warning rules (nonexistent / special / unreadable
files, broken symlinks, the invalid-timestamp epoch fallback). aws-cli's Python-2
byte-filename decoding warning has no Python-3 equivalent (a ``str`` directory
scan always yields ``str``) and is not ported.

The library-extension ``enumerate_all_entries`` source setting selects a complete
filesystem-entry view before filtering. It returns the root, directories,
symlinks, special files, and metadata-readable entries whose content is
unreadable; the default ``False`` retains exact ``aws s3`` transfer enumeration.
``follow_symlinks`` independently selects one interpretation per symlink: follow
and return/descend the target, or return the link itself as an lstat-based leaf in
the complete view (the normal no-follow view skips it). A failed followed stat in
the complete view warns and falls back to lstat. These settings ride
``LocalScanOptions`` (seeded from the ``LocalStorage`` constructor), so every scan
and high-level operation preserves the configured candidate enumeration policy.

Names under a ``filename_encoding`` (``LocalStorage``, POSIX only): the storage
holds its local names as *text* - the name's bytes decoded with that encoding -
while Python's OS calls take and return the *OS form* (the bytes decoded with
the file system encoding). The conversion is made only where a name crosses
into or out of the OS, and not at all when the encoding is the file system
encoding (`_filename_codec`). The walk itself runs on the OS form: every path it
hands to the OS and every path in a warning it builds is that form, so the
override seams keep their plain ``os`` semantics. ``scan_children`` turns each
child's ``sort_name`` and ``key`` into text before ``compare_key`` is stamped
and the children are sorted - so the order is the text's (S3's key order) and
everything the walk emits is text - ``walk_dir`` descends a sub-directory by its
text name encoded back, and ``list_file_pages`` turns its warnings into text. The
walk finds the encoding through ``LocalScanOptions.storage``; a hand-built
options object without a storage walks in the OS form throughout.

An app customizes the walk by subclassing ``LocalFileGenerator``, overriding
its **public** methods, and passing an instance to
``LocalStorage(path, walker=MyWalker())`` - no protected-method surgery on
``LocalStorage`` itself. The method names track aws-cli where a counterpart
exists (``list_files`` / ``should_ignore_file`` / ``triggers_warning`` /
``normalize_sort`` / ``is_special_file`` / ``is_readable`` /
``get_file_stat``); the pieces with no aws-cli counterpart - the
``os.scandir`` engine and its extensions - keep boto3-s3 names
(``scan_children`` / ``classify_child``
/ ``dir_child`` / ``WalkChild`` /
``have_dir_fd`` / ``detect_symlink_loops`` / ``LoopDetector``).

Performance (the reason for the ``os.scandir`` engine, not aws-cli's ``listdir``
+ per-name ``os.path.isdir`` / ``os.stat`` restat): ``os.scandir`` carries each
entry's type (``d_type``) so ``is_symlink`` costs no syscall, and caches one
``stat`` per entry that the vetting battery, the file/dir classification, and the
``LocalFileInfo`` build all read through
``entry_stat_result`` (one accessor, so overriding it -
e.g. to lstat - re-points the whole scan at once; the yield-time re-stat is
``restat_leaf``'s). Where the platform supports it
(``LocalFileGenerator.have_dir_fd``, i.e. POSIX), the directory is opened
once and scanned through its file descriptor, so every per-entry ``stat`` /
readability probe is ``dir_fd``-relative (``fstatat`` - no kernel path re-walk);
on Windows those APIs are absent, and the same code path falls back to a
path-based scan whose ``FindNextFile`` data already supplies the attributes for
free. The net effect is one ``stat`` per surviving entry (directories
included - the kind is keyed on that same stat), plus aws-cli's one
readability ``open`` per entry and its one yield-time ``stat`` per leaf
(``restat_leaf``, which reports the leaf as it is at the moment it is emitted),
versus the ~5
restats per entry a naive port makes. Every surviving ``LocalFileInfo`` carries
the stat or lstat used to classify it as ``stat_result``
and its ``d_type`` symlink flag as
``is_symlink`` - both already in hand from the
vetting battery, so a ``filter`` / ``on_result`` callback reads them free, and
because a ``stat_result`` is a plain value (not a ``dir_fd``-relative
``os.DirEntry``) the fast path keeps scanning through the directory fd.

The override seams, finest first - extend at the smallest layer that fits:

- ``LocalFileGenerator.should_ignore_entry`` - the special-file + readability
  battery on one entry's stat (the DirEntry form of ``triggers_warning``; the
  symlink-skip and "does not exist" cases are ``classify_child``'s);
- ``LocalFileGenerator.entry_stat_result`` - the one stat snapshot per entry
  (``classify_child`` takes it once and threads it to vetting / kind / size /
  mtime / loop key / ``stat_result``; override to lstat for a non-following /
  backup walk);
- ``LocalFileGenerator.stat_info`` - one file entry's ``LocalFileInfo`` from
  that stat, ``LocalFileGenerator.dir_child`` its directory counterpart, and
  ``LocalFileGenerator.promoted_directory`` the directory record ``restat_leaf``
  builds for a leaf that has become one;
- ``LocalFileGenerator.classify_child`` - one ``os.DirEntry`` -> a
  ``WalkChild`` or a skip, the natural per-entry override point (owns the
  single stat: the no-follow / "does not exist" skips and the kind decision);
- ``LocalFileGenerator.finalize_children`` - a directory's ``compare_key``-
  stamped children as a whole, before the walk consumes them (default: just
  ``normalize_sort``; override to prune / harvest /
  strip - dropping a directory prunes its subtree);
- ``LocalFileGenerator.scan_children`` - one directory enumerated, its children
  ``compare_key``-stamped, then ``finalize_children``\\ d (override to change *how*
  a directory is read, reusing ``classify_child`` / ``dir_child`` / the two above);
- ``LocalFileGenerator.restat_leaf`` - the yield-time re-stat of one classified
  leaf: gone -> warn-skip, a directory now -> descend, otherwise a refreshed
  ``size`` / ``mtime`` (aws-cli's ``os.path.isdir`` +
  ``_safely_get_file_stats``, which run in its descent loop; override to
  ``return info`` in a walker whose children are not live filesystem paths);
- ``LocalFileGenerator.walk_dir`` - the depth-first recursion yielding one
  page per directory file-run (override to prune a subtree or otherwise
  customize how the tree is descended);
- ``LocalFileGenerator.list_file_pages`` - the entry point: root vetting,
  then ``walk_dir`` (which stamps ``compare_key``) with ``ScanOptions.filter``, in
  ``os.scandir``-aligned pages (what ``Storage.scan_pages`` returns);
- ``LocalFileGenerator.list_files`` - that flattened to a flat per-file
  stream (aws-cli's ``FileGenerator.list_files``).

The module-level helpers are public where an override needs them
(``is_special_file`` / ``is_readable`` / ``get_file_stat``, and
``LoopDetector`` / ``WalkChild``); ``LocalStorage.get_fileinfo`` (the
single-path point op) reuses them too.
"""

from __future__ import annotations

import codecs
import contextlib
import errno
import os
import stat as stat_module
import sys
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, ClassVar, Literal, NamedTuple, cast

from typing_extensions import override

from boto3_s3.exceptions import (
    AccessDeniedError,
    Boto3S3Error,
    InvalidValueError,
    NotFoundError,
    TransportError,
    ValidationError,
)
from boto3_s3.storage import Storage, StorageCapability
from boto3_s3.types import FileInfo, FileKind, LocalFileInfo, LocalScanOptions, ScanOptions

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from typing import BinaryIO

# aws-cli EPOCH_TIME: the stamp used when a file's mtime cannot be represented.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# aws-cli's warning for that fallback (skip_file=False: the file is kept).
_INVALID_TIMESTAMP = "File has an invalid timestamp. Passing epoch time as timestamp."


def _warn_unrepresentable_mtime(info: LocalFileInfo, notify: Callable[[str], None]) -> None:
    """Send aws-cli's invalid-timestamp warning for a final scan-time record.

    ``stat_info`` stamps a regular file's record silently, because the
    recursive walk re-stats the leaf at its turn (``restat_leaf``), which is
    where aws-cli takes the timestamp and warns. A record the walk keeps as
    built - a one-level scan's child - draws its warning here instead; a link's
    own stat (``S_IFLNK``) already warned in ``stat_info``, and a directory
    carries no timestamp.
    """
    st = info.stat_result
    if (
        info.kind is FileKind.FILE
        and st is not None
        and not stat_module.S_ISLNK(st.st_mode)
        and _size_mtime(st)[1] is None
    ):
        notify(_INVALID_TIMESTAMP)


def to_native_path(key: str) -> str:
    """Native form of a ``/``-separated ``FileInfo`` key.

    Lossless because no supported filesystem allows ``/`` inside a file name
    (it is the separator on POSIX and rejected on Windows). Only the separator
    changes: under a ``filename_encoding`` the result is still text, which
    `LocalStorage.os_path` turns into what the OS calls take.
    """
    return key.replace("/", os.sep)


class _FilenameCodec:
    """Local names between text in a named encoding and Python's OS form (POSIX).

    ``to_os`` encodes the text with ``encoding`` and decodes the bytes the way
    Python's own path calls do (``os.fsdecode``: the file system encoding,
    ``surrogateescape``), so handing the result to any ``os`` function reaches
    exactly those bytes; ``to_text`` is the reverse. Both carry undecodable
    bytes as ``surrogateescape`` surrogates. ``to_os`` raises
    ``UnicodeEncodeError`` (a ``ValueError``) for a character the encoding
    cannot represent - where Python's own call with the file system encoding
    would raise the same.
    """

    __slots__ = ("encoding",)

    def __init__(self, encoding: str) -> None:
        self.encoding = encoding

    def to_os(self, path: str) -> str:
        return os.fsdecode(path.encode(self.encoding, "surrogateescape"))

    def to_text(self, path: str) -> str:
        return os.fsencode(path).decode(self.encoding, "surrogateescape")

    def message(self, body: str) -> str:
        """A warning built from OS-form paths, as text (unchanged if it cannot be)."""
        try:
            return self.to_text(body)
        except UnicodeError:
            return body


def name_files_as_text(exc: BaseException, text_path: Callable[[str], str]) -> None:
    """Turn the file names an ``OSError`` carries into a local side's text, in place.

    The OS reports the names it was handed, in the OS form; ``str(exc)``
    formats them when a report is built, so this makes every report built
    from the error afterwards name the files the way the results do.
    ``text_path`` is the side's conversion (`LocalStorage.text_path`, or a
    codec's ``to_text``); a name it cannot convert (``UnicodeError``) stays
    as it is, and any other exception is left alone.
    """
    if not isinstance(exc, OSError):
        return
    for attr in ("filename", "filename2"):
        name = getattr(exc, attr)
        if isinstance(name, str):
            with contextlib.suppress(UnicodeError):
                setattr(exc, attr, text_path(name))


def _filename_codec(encoding: str | None) -> _FilenameCodec | None:
    """The conversion a ``filename_encoding`` asks for, or ``None`` for none.

    An unknown codec name is an ``InvalidValueError`` on every platform (as
    ``IOStorage(encoding=...)``). ``None`` - no conversion at all - for no
    encoding, on Windows (the OS names files in Unicode, so there is no byte
    form to choose), and for the file system encoding itself (compared by
    ``codecs.lookup`` name), so the common case costs nothing per name.
    """
    if encoding is None:
        return None
    try:
        name = codecs.lookup(encoding).name
    except LookupError as exc:
        raise InvalidValueError(str(exc)) from exc
    if sys.platform == "win32" or name == codecs.lookup(sys.getfilesystemencoding()).name:
        return None
    return _FilenameCodec(name)


def _walk_codec(options: LocalScanOptions) -> _FilenameCodec | None:
    """The filename conversion of the storage a walk runs for (``options.storage``)."""
    storage = options.storage
    if isinstance(storage, LocalStorage):
        return storage._codec  # pyright: ignore[reportPrivateUsage]
    return None


def _text_notify(notify: Callable[[str], None], codec: _FilenameCodec) -> Callable[[str], None]:
    """``notify`` for warnings that name OS-form paths, handing them on as text."""

    def text_notify(body: str) -> None:
        notify(codec.message(body))

    return text_notify


def _complete_root(
    walker: LocalFileGenerator,
    root: str,
    options: LocalScanOptions,
    notify: Callable[[str], None],
    codec: _FilenameCodec | None,
) -> LocalFileInfo | None:
    """The complete view's scanned-path entry as a scan hands it on, or ``None``.

    ``walker.root_info``'s record for the OS-form anchor ``root`` (so an
    override applies), its key turned into text and its producing storage
    (``options.storage``) stamped. One rule for the recursive walk
    (``list_file_pages``) and the one-level scan (``_scan_one_level``), so the
    two name the scanned path identically. ``codec`` is the scanning
    storage's conversion, ``None`` for none.
    """
    record = walker.root_info(root, options=options, notify=notify)
    if record is not None:
        if codec is not None:
            record.key = codec.to_text(record.key)
        record.storage = options.storage
    return record


def _is_special_mode(mode: int) -> bool:
    """Whether ``st_mode`` is a character/block device, FIFO, or socket.

    The mode-only core of ``is_special_file``, so a caller that already holds
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
    (``dir_fd is None``) it falls back to the path-based ``is_readable``
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

    An mtime UTC cannot hold (outside ``datetime``'s range, or outside the
    platform's ``gmtime`` - on Windows from about half a day before 1970 back,
    or after about 3001) returns
    ``None`` for mtime - the caller's cue for the epoch fallback. aws-cli
    renders the mtime in the host's local zone and falls back wherever that
    fails; one only the local zone cannot hold is read as its UTC value here,
    per the ``FileInfo.mtime`` contract (docs/cli/aws-differences.md).
    """
    try:
        mtime: datetime | None = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        mtime = None
    return st.st_size, mtime


def get_file_stat(path: str, st: os.stat_result | None = None) -> tuple[int, datetime | None]:
    """Size and tz-aware mtime for a path (aws-cli's ``get_file_stat``).

    ``st`` reuses a stat already taken for ``path`` (the single-path
    ``LocalStorage.get_fileinfo`` holds one snapshot and derives everything
    from it - no re-stat); with it ``path`` is unused. Without it,
    ``os.stat(path)`` is taken here and its ``OSError`` propagates (the caller
    runs the warning battery). An unrepresentable timestamp returns ``None`` for
    the caller's epoch fallback (see ``_size_mtime``).
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
    ``LocalFileGenerator.scan_children`` yields to the walk (part of the walk
    override contract).

    ``sort_name`` is the name the byte-order sort keys on - a plain file name, or
    a directory name with a trailing ``os.sep`` appended (so a directory sorts
    after a sibling file of the same stem, matching aws-cli). ``info`` is the
    entry's ``LocalFileInfo``: a ``FILE`` for a file, or a
    ``DIRECTORY``-kind info for a sub-directory (which the walk recurses into
    rather than yields). ``loop_key`` is a directory's ``(st_dev, st_ino)`` for
    cycle detection - ``None`` for a file, or to fail open when the identity is
    unknown.

    A subclass re-implementing ``scan_children`` builds these with
    ``LocalFileGenerator.classify_child`` (or
    ``dir_child``) and orders them with
    ``normalize_sort``, so it need not reproduce the sort
    key or the directory-info shape.
    """

    sort_name: str
    info: LocalFileInfo
    loop_key: tuple[int, int] | None


class LoopDetector:
    """Ancestor-stack guard against symbolic-link cycles in a recursive walk.

    A reusable building block for a custom recursive enumerator - an application
    walking its own backend, or driving ``LocalFileGenerator.list_files``'s
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
        pair that call with a ``leave`` once its descent returns.
        """
        return self.is_cycle_key(_stat_key(path))

    def is_cycle_key(self, key: tuple[int, int] | None) -> bool:
        """``is_cycle`` for a caller that already holds the ``(st_dev, st_ino)``.

        The walk captures the identity from the entry's cached stat, so it never
        restats just to check for a cycle. ``None`` (no stable identity) fails
        open - it is pushed but never matches an ancestor.
        """
        if key is not None and key in self._ancestors:
            return True
        self._ancestors.append(key)  # None (fail-open) never matches an ancestor
        return False

    def leave(self) -> None:
        """Pop the directory pushed by the matching non-cycle ``is_cycle``."""
        self._ancestors.pop()


class LocalFileGenerator:
    """The customizable local directory walk (boto3-s3's aws-cli ``FileGenerator``).

    Subclass it, override the public methods below, and inject an instance via
    ``LocalStorage(path, walker=...)`` to change how directories are enumerated or
    entries vetted - see this module's docstring for the seam layering and the
    aws-cli name mapping. The default instance is the fast ``os.scandir`` walk.

    The class is **stateless across a walk**: the per-walk context
    (``follow_symlinks`` / ``detect_symlink_loops`` / ``on_warning`` / the producing
    ``storage``) rides on the ``LocalScanOptions`` passed to
    ``list_files``, not on the instance (aws-cli carries them on the instance
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
        (``foo.txt`` before ``foo/bar``). This is ``list_file_pages``
        flattened - see it for the walk semantics (the ``options`` fields read,
        vetting, ``compare_key`` stamping, and ``filter``). A single source object
        goes through ``get_fileinfo`` instead, not this walk.
        """
        for page in self.list_file_pages(root, options):
            yield from page

    def list_file_pages(
        self, root: str, options: LocalScanOptions
    ) -> Iterator[list[LocalFileInfo]]:
        """Yield the files under ``root`` (recursively) as byte-order pages.

        The paged form driving ``Storage.scan_pages``: one page per directory
        file-run - the sorted entries between two sub-directory descents. Under
        ``options.enumerate_all_entries`` the root leads in a one-record page and
        each descended directory rides the page before its children. A
        directory's files can only surface once its ``os.scandir`` has been read in
        full and sorted (the byte-order sort appends ``os.sep`` to directory names,
        so ``foo.txt`` precedes ``foo/bar``), and they are handed off just before
        the next descent's scandir. So a page never spans two directory reads
        (one scandir hands off a page per descent boundary - several for a
        directory with interleaved sub-directories), and a consumer -
        e.g. ``Storage.scan``'s prefetch worker - overlaps the next
        read on a network-mounted path, the reason the walk is paged at all. The
        pages concatenate to ``list_files``'s flat stream. The ``root`` parameter
        is the absolute directory path with a trailing ``os.sep``, in the OS
        form under a converting ``filename_encoding`` (module docstring); each
        ``LocalFileInfo.key`` is the absolute path (``os.sep`` folded to ``/``,
        as text) and ``compare_key`` is relative to that directory, stamped in
        ``scan_children`` (so a custom
        ``finalize_children`` can already filter on it) - the axis
        ``options.filter`` matches.

        Reads the local-walk fields of ``options`` (a ``LocalScanOptions``,
        this backend's option type, so a custom walker sees the full scan
        context):
        ``on_warning`` (warnings carry the aws-cli message bodies, dropped when
        ``None``); ``enumerate_all_entries`` (complete candidates before filtering,
        bypassing transferability vetting); ``follow_symlinks`` (target view when
        true, or an lstat leaf in the complete view / a silent skip otherwise);
        ``detect_symlink_loops`` (with ``follow_symlinks``, skips a directory that
        resolves to one of its own ancestors with a ``Symbolic link loop
        detected`` warning - a library extension, off = ``aws s3`` behaviour, no
        extra ``stat``); and ``filter`` (drops entries it rejects - applied
        *after* the aws-cli vetting so an excluded file still emits the warnings
        aws-cli would in the normal view, and yielding only survivors saves paging them downstream;
        the predicate can read each ``LocalFileInfo``'s ``stat_result`` /
        ``is_symlink``, stamped for free from the walk). The root is vetted with
        ``should_ignore_file``; its children per entry in
        ``scan_children``.
        """
        notify: Callable[[str], None] = (
            options.on_warning if options.on_warning is not None else (lambda body: None)
        )
        codec = _walk_codec(options)
        if codec is not None:
            # The walk names OS-form paths in its warnings (module docstring).
            notify = _text_notify(notify, codec)
        follow_symlinks = options.follow_symlinks
        if options.enumerate_all_entries:
            root_record = _complete_root(self, root, options, notify, codec)
            if root_record is None:
                return
            if options.filter is None or options.filter(root_record):
                yield [root_record]
            if root_record.kind is not FileKind.DIRECTORY:
                return
        elif self.should_ignore_file(root, follow_symlinks=follow_symlinks, notify=notify):
            return
        # No detector unless asked and reachable (a cycle needs a followed
        # symlink); None then costs no per-directory stat.
        detector = LoopDetector(root) if options.detect_symlink_loops and follow_symlinks else None
        # root ends in os.sep, so its normalized directory path ends in "/" and
        # the slice (in scan_children) never leaves a leading separator;
        # compare_key is relative to that directory - the text root's length,
        # as the keys it is cut from are text (scan_children).
        strip = len((root if codec is None else codec.to_text(root)).replace(os.sep, "/"))
        item_filter = options.filter
        storage = options.storage
        pages = self.walk_dir(root, options, strip=strip, notify=notify, detector=detector)
        for page in pages:
            # compare_key is already stamped (scan_children, before finalize_children);
            # here we stamp each entry's producing backend (options.storage, walk-
            # constant) before the visibility filter, so a predicate - and sync's
            # content compare - can reach it. Then the filter runs; no filter (or a
            # custom finalize_children that already pruned) -> the page passes
            # untouched. storage is None only for a hand-built options object
            # (Storage.scan fills FileInfo.storage afterwards).
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

        The recursion behind ``list_file_pages``: each directory's
        ``scan_children`` is read and sorted in full, then its files are
        collected into a run and handed off as one page just before descending
        into each sub-directory (``kind == DIRECTORY``) - depth-first via
        ``self.walk_dir`` (so an override applies at every level), the runs
        interleaving with the sub-directories' pages in byte order. ``dir_path``
        was already vetted (the root by ``list_file_pages``, a child in its
        parent's ``scan_children``); ``detector`` guards symlink cycles.
        ``strip`` is the prefix length of the directory passed to
        ``list_file_pages``; ``scan_children`` uses it to stamp ``compare_key``.
        It stays constant across the recursion.

        This is where recursion is customizable: override it and return early for a
        directory to prune its subtree (before ``super().walk_dir`` for the rest),
        or re-implement the loop to cap depth or change traversal - preserving the
        depth-first byte order that sync's merge-join relies on.
        ``options.enumerate_all_entries`` makes this loop also emit each descended
        directory's own record on the page flushed just before its descent.
        ``compare_key`` is stamped in ``scan_children``; ``ScanOptions.filter``
        is ``list_file_pages``'s job on the yielded pages, not this method's.

        Each leaf is re-stat'ed with ``restat_leaf`` just before its turn, the
        aws-cli semantics of a name that changed since its parent's scan: gone is
        warn-skipped, a directory by now is descended (with its own record ahead
        of it in the complete view) instead of submitted as a file, and a leaf
        that is still a leaf is emitted with the ``size`` / ``mtime`` it has at
        that moment - all on the bare name the leaf sorted under.
        """
        run: list[LocalFileInfo] = []
        # One join per directory rather than per child: dir_path is an absolute
        # directory path, so a child's path is this prefix plus its sort name.
        prefix = dir_path if dir_path.endswith(os.sep) else dir_path + os.sep
        codec = _walk_codec(options)
        for sort_name, info, loop_key in self.scan_children(
            dir_path, strip=strip, options=options, notify=notify
        ):
            # sort_name carries the trailing os.sep for a sub-directory (the sort
            # suffix) and is the bare name for a leaf, so this path is spelled the
            # way the child is addressed - which the loop warning and the warnings
            # raised by the descent keep. Under a converting filename encoding
            # the name is text (scan_children) and the path the OS form.
            sub = prefix + (sort_name if codec is None else codec.to_os(sort_name))
            if info.kind != FileKind.DIRECTORY:
                fresh = self.restat_leaf(sub, info, options=options, notify=notify)
                if fresh is None:
                    # Gone (warned there), or an entry this view does not emit.
                    continue
                if fresh.kind != FileKind.DIRECTORY:
                    run.append(fresh)
                    continue
                # The leaf became a directory between this parent's scan and its
                # turn here, so the record came back promoted: descend it, by the
                # bare name it sorted under (the separator was appended only to
                # names that were directories at scan time) - the subtree lands
                # where the leaf sorted, not where a directory of that name would
                # have. Its identity for the cycle guard comes from the same
                # yield-time stat, so the descent costs no further syscall.
                info = fresh
                promoted_stat = fresh.stat_result
                loop_key = (
                    (promoted_stat.st_dev, promoted_stat.st_ino)
                    if promoted_stat is not None and promoted_stat.st_ino
                    else None
                )
            # The directory's own record in the complete view rides the page
            # flushed before its descent: it sorts
            # after the collected files and before anything under sub. Appended
            # before the flush and the detector, so the record is emitted even
            # when the descent is then skipped as a cycle - like an unreadable
            # directory, the record survives and only the children are lost.
            if options.enumerate_all_entries:
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
                        sub,
                        options,
                        strip=strip,
                        notify=notify,
                        detector=detector,
                    )
                finally:
                    detector.leave()
            else:
                yield from self.walk_dir(
                    sub,
                    options,
                    strip=strip,
                    notify=notify,
                    detector=detector,
                )
        if run:
            yield run

    def restat_leaf(
        self,
        path: str,
        info: LocalFileInfo,
        *,
        options: LocalScanOptions,
        notify: Callable[[str], None],
    ) -> LocalFileInfo | None:
        """One classified leaf as it is at the moment it is emitted, or ``None`` to drop it.

        The walk's single re-stat of an already classified child, and the whole
        of what aws-cli does per name in its descent loop (``os.path.isdir``
        then ``_safely_get_file_stats``): ``scan_children`` classified ``path``
        as a leaf from the parent's ``os.scandir`` stat, and by the time
        ``walk_dir`` reaches it - after every earlier sibling's whole subtree has
        been read - the name may be gone, may have become a directory, or may
        hold different bytes. One ``os.stat`` decides all three, so this costs
        the syscall aws-cli spends here and no more:

        - it fails (``ENOENT`` and the rest): the ``triggers_warning`` battery
          runs on the path - normally ``File does not exist.``, rc 2 - and the
          leaf is dropped, like aws-cli's ``_safely_get_file_stats`` returning
          nothing. A battery that finds nothing (the race healed since the
          stat) warns with the stat's own error instead, where aws-cli drops
          the leaf silently. Without this the stale record was submitted and
          the transfer failed to open it (rc 1). The complete view first tries the
          link's own lstat (``_lstat_leaf``): a leaf that became a dangling
          link or a loop is kept as that lstat leaf, warned only when this
          walk follows links - ``classify_child``'s failed-follow rule, at
          the leaf's turn;
        - it says ``S_IFDIR``: the returned record is the promoted directory
          (built from this same stat, which is the **followed** one
          ``os.path.isdir`` takes), which ``walk_dir`` descends. Without this the
          swapped name was submitted as a file (rc 1) and its children never
          entered the stream, where aws-cli transfers them;
        - otherwise: ``info`` itself, with ``size`` / ``mtime`` / ``stat_result``
          refreshed from this stat, so what is transferred and what ``sync``
          compares are the file as of its turn - aws-cli takes every leaf's size
          and timestamp here, never at scan time. The unrepresentable-timestamp
          fallback stamps ``EPOCH_TIME`` as ``stat_info`` does and sends the one
          warning aws-cli sends for the file, here rather than at scan time, so
          a timestamp that became representable since the scan draws none
          (``stat_info`` stamps the scan-time record silently).

        ``is_symlink`` is left as classified: re-testing it would cost a second
        syscall per leaf, and aws-cli carries no such flag.

        A leaf whose classification stat is itself a symlink's (``S_IFLNK`` - the
        complete no-follow view's ``symlink_child``, or an lstat-style
        ``entry_stat_result`` override) is returned untouched: that walker keeps
        a link as its own leaf and never descends or follows it, so a followed
        re-stat would contradict its rule rather than report a change.

        Override it to ``return info`` in a walker whose ``scan_children`` yields
        entries that are not live filesystem paths (synthetic children, a virtual
        source): the re-stat cannot apply there and the syscall is saved.

        The reverse race - a directory that stopped being one - belongs to
        ``scan_children``, where the failing ``os.open`` / ``os.scandir`` runs the
        warning battery.
        """
        classified = info.stat_result
        if classified is not None and stat_module.S_ISLNK(classified.st_mode):
            return info
        try:
            st = os.stat(path)
        except (OSError, ValueError) as exc:
            if options.enumerate_all_entries:
                # The complete view's rule for a link whose follow fails
                # (classify_child's): warn when this walk follows links, keep
                # the entry as the link's own lstat leaf either way - a leaf
                # that became a dangling link or a loop since the scan is that
                # case at its turn. Gone altogether (no lstat either) drops it.
                fallback = self._lstat_leaf(path, info, options=options, notify=notify)
                if fallback is not None:
                    return fallback
            # aws-cli runs its battery on the path the stat failed on and skips
            # the leaf either way. A battery that finds nothing wrong - the race
            # healed between the two calls - still leaves no file stats, so the
            # leaf is skipped then too, but with the stat's own error as its
            # warning: aws-cli drops a file that exists without a word.
            if not self.triggers_warning(path, notify):
                reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
                notify(f"Skipping file {path}. {reason}.")
            return None
        if stat_module.S_ISDIR(st.st_mode):
            is_link = os.path.islink(path)
            if is_link and not options.follow_symlinks:
                # Now a link to a directory, and this walk does not follow links:
                # aws-cli descends (its isdir followed the link) and the
                # recursion's own should_ignore_file then drops it with no
                # warning, so the normal view emits nothing for the entry. The
                # complete view keeps the leaf record it already has rather than
                # lose the entry entirely.
                return info if options.enumerate_all_entries else None
            return self.promoted_directory(info, st, is_symlink=is_link)
        size, mtime = _size_mtime(st)
        if mtime is None:
            # aws-cli's one warning for the file, sent where aws-cli takes the
            # timestamp - at the leaf's turn, from this stat (the scan-time
            # record was stamped silently), so a timestamp that healed since
            # the scan draws none.
            notify(_INVALID_TIMESTAMP)
            mtime = self.EPOCH_TIME
        info.size = size
        info.mtime = mtime
        info.stat_result = st
        return info

    def _lstat_leaf(
        self,
        path: str,
        info: LocalFileInfo,
        *,
        options: LocalScanOptions,
        notify: Callable[[str], None],
    ) -> LocalFileInfo | None:
        """``info`` refreshed as the link's own lstat leaf, or ``None`` when even that is gone.

        The yield-time counterpart of ``symlink_child``'s fallback for the
        complete view: the followed stat failed, so the record describes the
        link itself (``size`` / ``mtime`` / ``stat_result`` from lstat,
        ``is_symlink`` set), warned "File does not exist." only when this walk
        follows links - a no-follow walk keeps a link silently. A path that is
        no link at all (the stat failed for another reason) is left to the
        caller's battery.
        """
        try:
            lst = os.lstat(path)
        except OSError:
            return None
        if not stat_module.S_ISLNK(lst.st_mode):
            return None
        if options.follow_symlinks:
            notify(f"Skipping file {path}. File does not exist.")
        size, mtime = _size_mtime(lst)
        if mtime is None:
            notify(_INVALID_TIMESTAMP)
            mtime = self.EPOCH_TIME
        info.size = size
        info.mtime = mtime
        info.stat_result = lst
        info.is_symlink = True
        return info

    def promoted_directory(
        self, info: LocalFileInfo, st: os.stat_result, *, is_symlink: bool
    ) -> LocalFileInfo:
        """The ``DIRECTORY`` record for a leaf found to be a directory at its turn.

        The yield-time counterpart of ``dir_child`` (the scan-time one), built by
        ``restat_leaf`` from the same re-stat: ``st`` is the followed stat
        aws-cli's ``os.path.isdir`` takes, and the key / ``compare_key`` are the
        leaf's plus the separator the sort appends to a directory - so the
        subtree lands where the leaf sorted rather than where a directory of that
        name would have. ``info`` is the leaf record the scan built and
        ``is_symlink`` whether the name is a link now (the walk knows only that
        it was not one at scan time).
        """
        compare_key = info.compare_key
        return LocalFileInfo(
            key=info.key + "/",
            kind=FileKind.DIRECTORY,
            stat_result=st,
            is_symlink=is_symlink,
            compare_key=None if compare_key is None else compare_key + "/",
        )

    def root_info(
        self,
        root: str,
        *,
        options: LocalScanOptions,
        notify: Callable[[str], None],
    ) -> LocalFileInfo | None:
        """The complete view's scanned-path entry, classified by symlink policy.

        ``root`` is the absolutized path being scanned, with a trailing separator
        (the ``list_file_pages`` anchor, or ``_scan_one_level``'s anchor for a
        non-recursive scan). Its ``compare_key`` is the empty string because the
        record represents the scanned path itself. That value sorts before every
        child key, so the record leads the stream; note glob filters see that
        ``""`` (a lone ``*`` matches it, a non-empty literal does not). A directory
        key carries the trailing separator and is descended; any other scanned
        path is the scan's sole leaf. ``stat_result`` is the followed stat or
        no-follow / failed-follow lstat selected by ``options``, with
        ``is_symlink`` reflecting the scanned path itself.
        """
        drive, _tail = os.path.splitdrive(root)
        path = root.rstrip(os.sep)
        if not path or path == drive:
            path = drive + os.sep
        follow_symlinks = options.follow_symlinks
        is_symlink = os.path.islink(path)
        try:
            st = os.stat(path, follow_symlinks=follow_symlinks)
        except OSError as exc:
            if not (options.enumerate_all_entries and is_symlink and follow_symlinks):
                if self.triggers_warning(path, notify):
                    return None
                codec = _walk_codec(options)
                if codec is not None:
                    name_files_as_text(exc, codec.to_text)
                raise
            notify(f"Skipping file {path}. File does not exist.")
            try:
                st = os.lstat(path)
            except OSError:
                return None
        is_directory = stat_module.S_ISDIR(st.st_mode)
        entry_path = path
        if is_directory and not entry_path.endswith(os.sep):
            entry_path += os.sep
        key = entry_path.replace(os.sep, "/")
        size: int | None = None
        mtime: datetime | None = None
        if not is_directory:
            size, mtime = _size_mtime(st)
            if mtime is None:
                notify(_INVALID_TIMESTAMP)
                mtime = self.EPOCH_TIME
        return LocalFileInfo(
            key=key,
            kind=FileKind.DIRECTORY if is_directory else FileKind.FILE,
            size=size,
            mtime=mtime,
            stat_result=st,
            is_symlink=is_symlink,
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
        """Return one directory's vetted children as final `WalkChild` entries.

        The enumeration layer (no aws-cli counterpart - aws-cli uses ``listdir``):
        it scans ``dir_path`` once with ``os.scandir``, turns each entry into
        a ``WalkChild`` via ``classify_child`` (skips return ``None``),
        stamps each child's ``compare_key`` (the key with the ``strip``-long
        directory prefix removed, aws-cli's ``src_path[len(src['path']):]``) -
        after turning each child's ``sort_name`` and ``key`` into text under a
        converting ``filename_encoding`` (module docstring) - then hands
        the list to
        ``finalize_children`` (which sorts, and is the override point for
        pruning / registration). ``options`` carries the per-walk context the
        classification reads (``follow_symlinks`` / ``enumerate_all_entries``).
        A path contributes one child: a followed symlink describes its target;
        a no-follow symlink in the complete view describes the link itself. A
        directory that cannot be opened or scanned - replaced, deleted or
        chmod'd away since its parent vetted it - is skipped through the
        ``triggers_warning`` battery on ``dir_path`` as passed, which is the
        aws-cli warning its own re-test before descending would emit, and yields
        an empty list (the walk goes on); if the battery sees nothing wrong, the
        ``OSError`` propagates instead. With symlinks not followed that re-test
        first strips the separator and tests the name itself, as aws-cli's
        ``should_ignore_file`` does at the top of each descent: a directory
        that became a symlink is skipped silently rather than followed, and
        the battery names the stripped path.

        The fast path scans through the directory's own fd where the platform
        allows (``have_dir_fd``), so each entry's stat and readability probe
        are dir-relative (``fstatat`` - no path re-walk); the followed stat each
        ``LocalFileInfo`` carries as ``stat_result`` is a plain value, so it
        keeps this fast path (unlike an ``os.DirEntry``, which would break once
        the fd closes).

        Override this to change how a directory is enumerated - e.g. to inject
        synthetic entries or read a different source - reusing
        ``classify_child`` / ``dir_child`` / ``normalize_sort`` so the
        sort key and directory-info shape need not be reproduced (an injected
        child still needs its ``compare_key`` stamped - ``info.key[strip:]``,
        and under a converting ``filename_encoding`` its name and key as text;
        ``FileInfo.storage`` need not be set - the caller stamps the producing
        backend after the walk yields a page). To prune / register / re-order after
        the fact, override the finer ``finalize_children`` instead; for a
        per-entry change, ``classify_child`` (or ``should_ignore_entry`` /
        ``stat_info``).
        """
        children: list[WalkChild] = []
        dir_fd: int | None = None
        use_fd = self.have_dir_fd
        # aws-cli's should_ignore_file runs again at the top of each descent,
        # and with symlinks not followed it strips the separator to test the
        # name itself: a directory that became a symlink since its parent's
        # scan is skipped silently - never followed - and its battery (below)
        # names the stripped path. The open through the separator-terminated
        # path would follow that link, so the two probes aws-cli spends here
        # (isdir + islink, only when not following) run first.
        probe = dir_path
        if not options.follow_symlinks and probe.endswith(os.sep) and os.path.isdir(probe):
            probe = probe[:-1]
            if os.path.islink(probe):
                return []
        try:
            if use_fd:
                dir_fd = os.open(dir_path, self.dir_open_flags)
            scan = os.scandir(dir_fd if dir_fd is not None else dir_path)
        except OSError as exc:
            # The directory stopped being an openable directory between its
            # parent's scan and this descent - replaced by a file, deleted, or
            # chmod'd away. aws-cli re-tests the child immediately before
            # recursing into it: no longer a directory falls to
            # ``_safely_get_file_stats``,
            # whose ``os.stat`` raises ``ENOTDIR`` / ``ENOENT`` and warns through
            # the battery, and still-a-directory-but-unreadable warns through the
            # ``should_ignore_file`` at the top of the recursion. Establishing
            # this scan *is* that re-test, so run the same battery on ``probe``
            # (``dir_path`` as given, or the separator-stripped form when
            # symlinks are not followed), then keep walking (a warning, rc 2)
            # instead of failing the run. If the probes
            # see nothing wrong (e.g. fd exhaustion resolved by the probe's
            # close), re-raise like aws-cli's ``listdir`` would - never prune
            # silently. This open is by full path, so a directory past the
            # kernel's symlink-loop or path-length limit - which the parent's
            # fd-relative vetting cannot see - is warned here, under the name
            # aws-cli's full-path listing warns it by (a leaf past it, by
            # ``restat_leaf``'s full-path stat at its turn). Scoped to the open/scandir
            # establishment only - a per-entry OSError (from a race mid-scan or
            # an override's classify_child) propagates rather than dropping the
            # directory.
            if dir_fd is not None:
                os.close(dir_fd)
            if exc.errno in (errno.ELOOP, errno.ENAMETOOLONG) and probe.endswith(os.sep):
                # aws-cli's parent listing already finds such a path not to
                # be a directory (its ``isdir`` fails the same way), so it is
                # never given the separator and is warned as a file.
                probe = probe[:-1]
            if not self.triggers_warning(probe, notify):
                codec = _walk_codec(options)
                if codec is not None:
                    name_files_as_text(exc, codec.to_text)
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
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
        codec = _walk_codec(options)
        if codec is not None:
            # The OS-form names become the storage's text here, before the key
            # is cut and the children sorted: the order is the text's. A name
            # whose text encodes back to other bytes (a codec that maps several
            # byte sequences to one character, as cp932 does) would address
            # another file, so it is skipped with a warning.
            converted: list[WalkChild] = []
            for child in children:
                os_key = child.info.key
                text_key = codec.to_text(os_key)
                if codec.to_os(text_key) != os_key:
                    notify(
                        f"Skipping file {os_key}. File name does not survive "
                        f"{codec.encoding} unchanged."
                    )
                    continue
                child.info.key = text_key
                converted.append(child._replace(sort_name=codec.to_text(child.sort_name)))
            children = converted
        for child in children:
            child.info.compare_key = child.info.key[strip:]
        return self.finalize_children(children)

    def entry_stat_result(self, entry: os.DirEntry[str]) -> os.stat_result | None:
        """The single stat snapshot for one entry (an override seam), or ``None``.

        The walk's **one** stat per entry: ``classify_child`` calls this once
        and threads the result through everything downstream - the special /
        readable vetting (``should_ignore_entry``), the file-vs-directory
        decision, the ``size`` / ``mtime`` (``stat_info``), the loop key
        (``dir_child``), and ``LocalFileInfo.stat_result``. The default
        reads the ``os.DirEntry``'s cache, one ``fstatat`` for the whole entry.

        Default: the **followed** stat (``follow_symlinks=True``), so the walk
        follows symlinks like ``aws s3``. Override to return the link's own lstat
        (``entry.stat(follow_symlinks=False)``) to turn the walk lstat-based in
        one place, still one syscall: then a symlink surfaces as its own entry
        instead of being followed, a symlinked directory is not descended (its
        ``st_mode`` is not ``S_IFDIR``), and ``size`` / ``mtime`` / ``stat_result``
        all describe the link - the building block for a backup-style walk. (The
        readability probe in ``should_ignore_entry`` still opens through the
        link, so pair this with a ``should_ignore_entry`` override to also admit
        broken links.) ``None`` (an ``OSError``) makes the caller treat the entry
        as gone (the "does not exist" skip).

        This is the scan's accessor. A leaf is re-stat'ed once more when its turn
        comes (``restat_leaf``, aws-cli's per-leaf stat), which leaves an
        ``S_IFLNK`` record alone and so keeps an lstat-based walk's rule; override
        that one too for a walk whose every stat must come from one place.
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
        """One ``os.DirEntry`` -> a ``WalkChild``, or ``None`` to skip it.

        The per-entry decision, and the walk's single stat: it takes the entry's
        ``entry_stat_result`` **once** and threads that one value through the
        rest (so nothing below re-stats or re-checks for ``None``). Symlinks are
        decided first on a free ``d_type`` test: no-follow returns an lstat leaf
        only in the complete view and otherwise skips it silently. A followed
        stat that comes back ``None`` warns; the complete view then falls back
        to the link's lstat (whose own timestamp warning still goes out), while
        the normal view skips it. A stat that
        says ``S_IFLNK`` (only an lstat-style override produces one) is its own
        vetting-free leaf, like ``symlink_child``; then
        the normal view's ``should_ignore_entry`` vets that valid stat, and the kind is keyed on
        its ``st_mode`` (``S_IFDIR`` = descend via ``dir_child``, else a file
        via ``stat_info``). Keying on that same stat - rather than a fresh
        ``entry.is_dir`` - keeps the whole entry on one stat and lets an lstat
        override classify a symlinked directory as a non-descended entry. ``full``
        is the entry's absolute path, ``dir_fd`` the owning directory fd (``None``
        off POSIX).
        """
        if entry.is_symlink():
            if options.enumerate_all_entries and not options.follow_symlinks:
                return self.symlink_child(entry, full, notify=notify)
            if not options.follow_symlinks:
                return None
        st = self.entry_stat_result(entry)
        if st is None:
            notify(f"Skipping file {full}. File does not exist.")
            if options.enumerate_all_entries and entry.is_symlink():
                # That warning is sent; symlink_child must not repeat it when
                # the lstat fails too. What the link's own stat has to say -
                # a timestamp the host cannot render - still goes out, as it
                # does for the same leaf built at its turn (`_lstat_leaf`).
                def own_stat_warnings(body: str) -> None:
                    if body == _INVALID_TIMESTAMP:
                        notify(body)

                return self.symlink_child(entry, full, notify=own_stat_warnings)
            return None
        if stat_module.S_ISLNK(st.st_mode):
            # Only an lstat-style entry_stat_result override produces this mode:
            # the link is its own leaf, vetting-free like symlink_child (a name
            # plus a target, no content to probe - the file probe would open the
            # target, which Windows refuses for a directory).
            return WalkChild(entry.name, self.stat_info(entry, full, st, notify), None)
        if not options.enumerate_all_entries and self.should_ignore_entry(
            entry, full, dir_fd, st, notify=notify
        ):
            return None
        if stat_module.S_ISDIR(st.st_mode):
            return self.dir_child(entry, full, st)
        return WalkChild(entry.name, self.stat_info(entry, full, st, notify), None)

    def dir_child(self, entry: os.DirEntry[str], full: str, st: os.stat_result) -> WalkChild:
        """A sub-directory's ``WalkChild`` - its ``DIRECTORY`` info and loop key.

        ``sort_name`` and the info key carry a trailing ``os.sep`` (so the
        directory sorts after a sibling file of the same stem); ``loop_key`` is
        ``(st_dev, st_ino)`` from ``st`` (``None`` to fail open on the ``st_ino ==
        0`` some FAT / exFAT / FUSE volumes report). ``st`` is the one stat
        ``classify_child`` took; the info carries it and the entry's
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
        """A symlink's own lstat-based leaf in the complete no-follow/fallback view.

        lstat-based - the info carries the link's own stat with
        ``is_symlink=True``, so ``size`` / ``mtime`` describe the link, not the
        target - and vetting-free: a symlink is a name plus a target with no
        content to probe, so the special/readability battery does not apply
        and a broken link is a returned entry like any other. The child itself
        is never descended (``loop_key=None``). ``None`` means the entry raced
        away and even lstat failed.
        """
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            notify(f"Skipping file {full}. File does not exist.")
            return None
        return WalkChild(entry.name, self.stat_info(entry, full, st, notify), None)

    def finalize_children(self, children: list[WalkChild]) -> list[WalkChild]:
        """Produce a directory's final child list from its vetted, ``compare_key``-
        stamped children (an override seam; default: just ``normalize_sort``).

        The last step of ``scan_children``, the seam for shaping a directory's
        children as a whole before the walk consumes them - each child's
        ``compare_key`` is already stamped, so an override can prune by it, harvest
        register entries, strip entries it does not want, or re-order. It receives the
        children *unsorted*, so pruning here happens before the sort; call
        ``super().finalize_children`` (or ``normalize_sort``) for aws-cli byte
        order. ``FileInfo.storage`` (the producing backend) is *not* set yet - the
        caller stamps it after the walk yields a page, before the visibility filter
        - so an override shapes the subtree from ``compare_key`` / ``key`` /
        ``kind``, not from the backend handle. Dropping a ``DIRECTORY`` child prunes
        its whole subtree (the walk
        never descends what this does not return) - a speedup ``aws s3`` cannot do
        (it vets every file), so it is the right place for a non-parity backup
        walk, not the default. ``enumerate_all_entries`` surfaces directories,
        symlinks, and other native entries without an override; a backup walker
        normally customizes only this method to prune excluded subtrees. The
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
        ``path`` is the absolutized walk ``root`` with a trailing ``os.sep``. The per-entry
        hot path uses the DirEntry form ``should_ignore_entry``.
        """
        if not follow_symlinks:
            if os.path.isdir(path) and path.endswith(os.sep):
                # A trailing separator must be removed to test the link itself.
                path = path[:-1]
            if os.path.islink(path):
                return True
        return self.triggers_warning(path, notify)

    def triggers_warning(self, path: str, notify: Callable[[str], None]) -> bool:
        """Warn-and-skip checks on a *path* (aws-cli's ``triggers_warning``).

        Path-based: the root vetting (``should_ignore_file``), a directory
        whose scandir establishment failed, and a leaf whose re-stat at its
        turn failed (``restat_leaf``). The per-entry hot path checks the same
        conditions from the ``DirEntry``'s cached stat in
        ``should_ignore_entry``; a mid-scan race (``entry_stat_result`` ->
        ``None``) is warned in ``classify_child`` directly.
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
        form of ``triggers_warning``).

        Runs on the valid stat ``classify_child`` already took (``st``) - the
        no-follow symlink skip and the "does not exist" / ``None`` case are
        classify_child's, so this only decides whether a *present* entry is
        ignorable: a character/block/FIFO/socket special file, or one the process
        cannot read. The readability probe is dir-relative (``openat`` through
        ``dir_fd`` where the platform allows, ``None`` off POSIX). ``full`` is the
        entry's absolute path, which the warnings name. The checks are
        ``triggers_warning``'s, with one refinement on the probe's failure path:
        aws-cli vets by full path and probes existence *first*, so an entry
        whose full-path resolution fails - over ``MAX_PATH`` on a Windows host
        without long-path support (the length is invisible to the scandir-cached
        stat above), or raced away since - is reported there as missing, never
        as unreadable. A full-path existence re-check on this rare branch does
        the same; a genuinely unreadable present entry is still reported as
        unreadable.
        """
        if _is_special_mode(st.st_mode):
            notify(
                f"Skipping file {full}. File is character special device, "
                "block special device, FIFO, or socket."
            )
            return True
        if not _is_readable_child(entry.name, full, dir_fd, is_dir=stat_module.S_ISDIR(st.st_mode)):
            if not os.path.exists(full):
                notify(f"Skipping file {full}. File does not exist.")
            else:
                notify(f"Skipping file {full}. File/Directory is not readable.")
            return True
        return False

    def stat_info(
        self, entry: os.DirEntry[str], full: str, st: os.stat_result, notify: Callable[[str], None]
    ) -> LocalFileInfo:
        """One file entry's ``LocalFileInfo`` from its stat, at scan time.

        ``st`` is the valid stat ``classify_child`` took (the race / ``None``
        case is handled there), so this never fails: an unrepresentable mtime
        keeps the file, stamped with ``EPOCH_TIME`` (aws-cli's
        ``skip_file=False``). The info carries ``st`` and the entry's
        ``is_symlink`` flag.

        A leaf's ``size`` / ``mtime`` / ``stat_result`` are then refreshed by
        ``restat_leaf`` when its turn comes (where aws-cli's
        ``_safely_get_file_stats`` takes them), so an override that rewrites
        those three - rather than adding to the record - pairs with that seam;
        everything else this stamps survives to the stream. The record built
        here is what a link leaf and a non-recursive scan keep as is.

        The invalid-timestamp warning is sent from here only for a link's own
        stat (``S_IFLNK``), which no re-stat follows; a regular file's record
        is stamped silently, and its warning is ``restat_leaf``'s (at the
        leaf's turn, where aws-cli warns) or, for a one-level scan that keeps
        this record, the scan's.
        """
        size, mtime = _size_mtime(st)
        if mtime is None:
            # skip_file=False in aws-cli: keep the file, stamped epoch.
            if stat_module.S_ISLNK(st.st_mode):
                notify(_INVALID_TIMESTAMP)
            mtime = self.EPOCH_TIME
        return LocalFileInfo(
            key=full.replace(os.sep, "/"),
            size=size,
            mtime=mtime,
            stat_result=st,
            is_symlink=entry.is_symlink(),
        )


def translate_os_error(
    exc: OSError | ValueError,
    *,
    operation: str | None,
    key: str | None,
    message: str | None = None,
) -> Boto3S3Error:
    """Map an ``OSError`` to the library taxonomy (the local mirror of
    ``sdkerrors.translate_boto_error``): missing path -> ``NotFoundError``,
    permission -> ``AccessDeniedError``, everything else -> ``TransportError``.
    A ``ValueError`` is a path the OS calls refuse before asking the
    filesystem - an embedded NUL, a lone surrogate the filesystem encoding
    cannot carry (``UnicodeEncodeError``) - and maps to ``ValidationError``:
    the caller supplied it.

    Shared by the local-filesystem failure paths that raise into the taxonomy
    (this backend's single-entry and I/O operations, the engines'
    ``makedirs``, the CLI's destination pre-creation). The walk's per-entry
    handling is separate: it warns-and-skips through the vetting battery, and
    an ``OSError`` its probes cannot attribute re-raises untranslated rather
    than dropping a directory silently. ``message`` overrides
    ``str(exc)`` when the caller carries a message of its own.
    """
    text = str(exc) if message is None else message
    if isinstance(exc, ValueError):
        return ValidationError(text, operation=operation, key=key)
    if isinstance(exc, FileNotFoundError):
        return NotFoundError(text, operation=operation, key=key)
    if isinstance(exc, PermissionError):
        return AccessDeniedError(text, operation=operation, key=key)
    return TransportError(text, operation=operation, key=key)


class LocalStorage(Storage):
    """A local filesystem path as one side of a transfer.

    The recursive walk lives in a composed ``LocalFileGenerator`` (default
    the fast ``os.scandir`` walk). Pass ``walker=`` a custom subclass to change
    the traversal - e.g. to resolve Cygwin ``!<symlink>`` files on a
    native-Python Windows build - without subclassing ``LocalStorage`` itself.

    ``filename_encoding`` names the encoding of the local names on POSIX
    (``None``, the default: Python's file system encoding). Every name the
    storage holds - ``path`` and ``abspath``, each ``FileInfo.key`` /
    ``compare_key`` it lists, the key an ``open`` / ``get_fileinfo`` /
    ``delete`` takes, and the paths in its warnings and errors - is then the
    name's bytes decoded with it (``surrogateescape`` for bytes it cannot
    decode), and the OS gets that text encoded with it. `os_path` and
    `text_path` convert between that text and the form Python's own OS calls
    take and return. On Windows the OS names files in Unicode and the setting
    has no effect; an unknown codec is an ``InvalidValueError``.

    Class attributes: ``sep`` is the host-native separator (``os.sep``;
    ``format`` returns ``os.sep`` forms, unlike every other backend's ``/``
    space). ``capabilities`` is the full set - the local filesystem supports every
    transfer operation: byte I/O both ways, single-entry stat, a sorted walk (so
    ``SORTABLE_SCAN``), and delete. ``scan_options_type`` is
    ``LocalScanOptions`` (arg-less ``scan()`` builds it, and ``scan_pages``
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
        enumerate_all_entries: bool = False,
        fsync: bool = False,
        filename_encoding: str | None = None,
    ) -> None:
        # The conversion between this storage's text names and the OS form
        # (None: none at all - see _filename_codec). Checked first, so an
        # unknown codec fails the construction.
        self._filename_encoding = filename_encoding
        self._codec = _filename_codec(filename_encoding)
        self._path = os.fspath(path)
        # Absolutize once, at construction (against the cwd then): every scan /
        # get_fileinfo anchors here so ``FileInfo.key`` comes out absolute, and
        # ``os.path.abspath`` calls ``os.getcwd()`` - too costly to repeat per
        # entry. Binding the cwd here also keeps a relative path resolving
        # consistently if the process later chdir's.
        # The same anchor without abspath's lexical normalization, for
        # ``exists``: ``..`` folded by abspath and ``..`` resolved by the
        # kernel name different files once a symlinked directory is involved.
        # An absolute path needs no anchor, and must not ask for one: getcwd
        # fails in a process whose working directory has been removed, where
        # abspath - which consults it for a relative path only - works.
        if self._codec is None:
            self._abspath = os.path.abspath(self._path)
            self._unnormalized = (
                self._path if os.path.isabs(self._path) else os.path.join(os.getcwd(), self._path)
            )
        else:
            # POSIX only (the codec is None on Windows), where abspath is the
            # normalized join onto the cwd - taken here as text, so the anchor
            # and the typed path share one form.
            self._unnormalized = (
                self._path
                if os.path.isabs(self._path)
                else os.path.join(self._codec.to_text(os.getcwd()), self._path)
            )
            self._abspath = os.path.normpath(self._unnormalized)
        # How this local source is read: symlink interpretation, whether every
        # metadata-readable native entry is enumerated before filtering, and
        # whether recursive descent guards against symlink cycles. Seeded into
        # every scan and high-level operation via default_scan_options. The
        # single-path get_fileinfo reads only follow_symlinks; enumeration / loop
        # settings shape a scan and do not apply to resolving one entry.
        self._follow_symlinks = follow_symlinks
        self._detect_symlink_loops = detect_symlink_loops
        self._enumerate_all_entries = enumerate_all_entries
        # A library-only durability knob for this local backend as a *destination*
        # (default off = aws parity). When set, a ``mv`` whose download lands here
        # fsyncs the file (and its parent directory) before deleting the S3 source,
        # closing the window where aws-cli / s3transfer delete the durable S3 copy
        # while the download is still only in the page cache. Read by the transfer
        # engine off the destination storage; see ``design/transfer.md`` section 11.
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
    def abspath(self) -> str:
        """The construction-time absolute form of `path`.

        The anchor every scan, `get_fileinfo`, and transfer plan resolves
        against (see `__init__`), so a relative `path` keeps meaning the same
        directory even if the process chdir's after construction. The
        operations' source existence check and destination directory creation
        ask `exists` / `ensure_directory` instead, which hand the path to the
        kernel as typed (anchored at the same working directory) rather than
        in this lexical form.
        """
        return self._abspath

    @property
    def filename_encoding(self) -> str | None:
        """The ``filename_encoding`` given at construction (see the class docstring)."""
        return self._filename_encoding

    def os_path(self, path: str) -> str:
        """``path``, a local path in this storage's text form, as the OS calls take it.

        The text form is what the storage holds and reports - `abspath`, a
        ``FileInfo.key`` (in either separator). Without a converting
        ``filename_encoding`` this returns ``path`` itself; with one, the text
        encoded with that encoding and decoded the way Python's own ``os``
        functions read a path, so ``os.stat(storage.os_path(key))`` reaches the
        file the key names. A character the encoding cannot represent raises
        ``UnicodeEncodeError`` (a ``ValueError``), as Python's own call does
        for one its file system encoding cannot represent.
        """
        codec = self._codec
        return path if codec is None else codec.to_os(path)

    def text_path(self, path: str) -> str:
        """``path``, as Python's OS calls return it, in this storage's text form.

        The reverse of `os_path`: a name from ``os.listdir`` or ``os.getcwd``
        becomes the text this storage would report for it. Without a
        converting ``filename_encoding`` this returns ``path`` itself.
        """
        codec = self._codec
        return path if codec is None else codec.to_text(path)

    def _os_root(self, path: str | None = None) -> str:
        """`abspath` (or the text ``path``) in the OS form: a walk's root, or `_stat_one`'s path.

        A character the ``filename_encoding`` cannot represent is the
        caller's path: the ``ValueError`` an OS call raises for a name its
        encoding cannot represent, a ``ValidationError`` (`_os_failure`).
        """
        text = self._abspath if path is None else path
        try:
            return self.os_path(text)
        except ValueError as exc:
            raise self._os_failure(exc, text) from exc

    def _single_filter_root(self) -> str:
        """aws-cli's ``_get_local_root`` for this location as a single (non-``dir_op``) source.

        The base aws joins ``--exclude`` / ``--include`` patterns onto:
        ``abspath(dirname(path))`` of the path as typed - so ``d`` roots at
        the working directory, while ``d/``, ``.`` and ``../`` root at the
        location itself and ``..`` at the working directory below it -
        anchored at the construction-time working directory like `abspath`.
        """
        return os.path.abspath(os.path.dirname(self._unnormalized))

    def exists(self) -> bool:
        """Whether the location exists, asked the way aws-cli's pre-check asks.

        aws-cli's ``_validate_path_args`` runs ``os.path.exists`` on the path
        as typed - the kernel's resolution - while its generator then looks
        for the lexical ``abspath`` form. The two differ once ``..`` follows a
        symlinked directory: ``lsub/../x`` is ``sub/x`` to the kernel and
        ``<cwd>/x`` to ``abspath``. aws-cli admits such a source and its
        generator warns "File does not exist." (rc 2). This asks the kernel,
        anchored at the construction-time working directory like `abspath`, so the
        operations admit what aws-cli admits (a recursive source is then
        vetted as a walk root by the operation) and the walk / `get_fileinfo`
        then answer for the lexical form. Windows has no such gap - it folds
        ``..`` out of a path before the filesystem sees it, so the typed path
        is already the lexical one - and there ``aws.exe`` refuses that
        source as missing up front (measured), as this does. An empty path
        does not exist (Python's own ``os.path.exists("")``; ``os.path.join``
        would make it the working directory).
        """
        if not self._path:
            return False
        try:
            target = self.os_path(self._unnormalized)
        except ValueError:
            # A name the filename encoding cannot represent names no file -
            # what os.path.exists answers for one the file system encoding
            # cannot represent.
            return False
        return os.path.exists(target)

    def ensure_directory(self) -> None:
        """Create the location as a directory unless it exists, as aws-cli's pre-check does.

        aws-cli's ``_validate_path_args`` creates a directory destination
        with a bare ``os.path.exists`` / ``os.makedirs`` on the path as typed,
        so this tests `exists` and creates the same typed, construction-anchored
        path - not the lexical ``abspath`` form the transfer then writes
        under. The typed path keeps what ``abspath`` folds away - a trailing
        separator, a ``..`` - and the kernel reads those: an existing *file*
        written ``out/`` does not exist as typed and fails the creation
        (written ``out`` it exists, and the transfer fails per item instead);
        ``missing/../out`` creates ``missing/`` on the way and then fails on
        an existing ``out``; and behind a symlinked directory ``..`` names the
        link target's parent, so a destination the kernel finds there is not
        created lexically (a ``sync`` then warns about the missing lexical
        directory) and one it does not find is created where the kernel
        resolves it. Each of these is aws-cli's own outcome, measured. (The
        ``..`` cases are POSIX's: Windows folds ``..`` before the filesystem
        sees the path, leaving the trailing separator the only difference.)
        An ``OSError`` (naming its files as text) or the ``ValueError`` of a
        name the ``filename_encoding`` cannot represent propagates for the
        caller to translate.
        """
        if not self.exists():
            try:
                os.makedirs(self.os_path(self._unnormalized))
            except OSError as exc:
                self._text_names(exc)
                raise

    @property
    def fsync(self) -> bool:
        """Whether a ``mv`` download into this destination fsyncs before deleting the source.

        The library-only durability knob from the constructor (default off). The
        transfer engine consults it only on the ``mv`` download route.
        """
        return self._fsync

    @property
    def walker(self) -> LocalFileGenerator:
        """The ``LocalFileGenerator`` driving this backend's directory walk."""
        return self._walker

    @override
    def validate(self) -> None:
        """Reject an empty path (``Storage.validate``).

        ``""`` is not a location: Python's own path functions treat it as a
        path that does not exist (``os.path.exists("")`` is ``False``), while
        ``os.path.abspath`` alone would silently make it the working
        directory - a ``sync`` with ``delete_filter`` onto ``""`` then deleted
        the working directory's files. aws-cli refuses it as a source and as a
        directory destination; the one place it means the working directory
        there, the destination of a single download, the CLI layer spells as
        ``"."`` itself. Construction stays permissive, like every backend's; the
        operations call this before use. ``os.PathLike`` never yields ``""``
        (``pathlib.Path("")`` is ``"."``).
        """
        if not self._path:
            raise ValidationError("A local path must not be empty.")

    @override
    def as_text(self) -> str:
        """Return the path as given (``Storage.as_text``).

        The raw constructor form, verbatim - the trailing-separator rule of
        ``format`` reads this unmodified token.
        """
        return self._path

    @override
    def format(self, *, dir_op: bool) -> tuple[str, bool]:
        """Format this local side; return ``(root, use_src_name)`` (``Storage.format``).

        aws-cli's ``FileFormat.local_format`` on the held state: the root is
        the construction-time ``_abspath`` (the same anchor ``scan`` walks
        from, so the plan and the walk agree even if the process chdir'd
        since), and the trailing-separator rule reads the *raw* constructor
        form (``abspath`` strips it). An existing directory, a ``dir_op``, or
        a user-typed trailing ``os.sep`` all mean directory semantics - the
        root gains a trailing ``os.sep`` and the destination side would take
        the source's name.
        """
        full_path = self._abspath
        if dir_op or self._is_directory(full_path):
            return full_path + os.sep, True
        if self._path.endswith(os.sep):
            return full_path + os.sep, True
        return full_path, False

    def _is_directory(self, path: str) -> bool:
        """``os.path.isdir`` for a text path (``False`` for one the encoding cannot carry)."""
        try:
            return os.path.isdir(self.os_path(path))
        except ValueError:
            return False

    @override
    def default_scan_options(self) -> LocalScanOptions:
        """The walk options seeded with this storage's held config
        (``Storage.default_scan_options``).

        ``follow_symlinks`` / ``detect_symlink_loops`` /
        ``enumerate_all_entries`` come from the constructor, so every scan reads the walk
        configured once on this ``LocalStorage``; an operation overlays only its
        own knobs (``recursive`` / ``sort`` / ``filter`` / ``on_warning``, plus
        the application's ``reusable_after_interrupt`` posture) onto this.
        The single-path ``get_fileinfo`` does not build these options - it reads
        ``follow_symlinks`` directly and ignores the enumeration / loop knobs.
        """
        return LocalScanOptions(
            follow_symlinks=self._follow_symlinks,
            detect_symlink_loops=self._detect_symlink_loops,
            enumerate_all_entries=self._enumerate_all_entries,
        )

    @override
    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[LocalFileInfo]]:
        """Yield entries under ``path``, paged on directory-read boundaries.

        Recursive enumeration drives the walker's
        ``list_file_pages`` in aws-cli byte order, whose
        pages fall on directory boundaries: a directory's sorted files
        accumulated so far are handed off just before each descent into a
        sub-directory, so one ``os.scandir`` yields one page per descent
        boundary (several for a directory with interleaved sub-directories)
        and a page never spans two directory reads - the unit a prefetch
        consumer overlaps.
        ``options.follow_symlinks`` / ``options.detect_symlink_loops`` /
        ``options.enumerate_all_entries`` /
        ``options.on_warning`` / ``options.filter`` are threaded into it - a
        symlink skip, the cycle guard, the aws-cli-worded warning channel a
        transfer needs, and the per-entry filter (applied in the walk, after
        vetting - ``Storage.scan_pages``'s "return filtered pages" contract).
        Every listed ``LocalFileInfo`` carries its classification ``stat_result`` and
        ``is_symlink`` flag for a filter / ``on_result`` to read.
        Non-recursive yields one level like the S3 backend (immediate entries in
        the same sort order, sub-directories as ``DIRECTORY``-kind infos whose key
        ends with ``/``) as a single page, anchored at the absolutized path
        (``self._abspath``) so ``FileInfo.key`` is absolute. Complete enumeration
        leads with the scanned directory itself (``compare_key == ""``) and
        includes every immediate metadata-readable entry without descending.

        Requires a ``LocalScanOptions`` (this backend's option type); a
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
            # The walk runs on the OS form and finds this storage's encoding
            # through options.storage (module docstring).
            yield from self._walker.list_file_pages(self._os_root() + os.sep, options)
            return
        page = list(self._scan_one_level(self._abspath, options))
        if page:
            yield page

    def walk_local(
        self,
        *,
        on_warning: Callable[[str], None] | None = None,
    ) -> Iterator[LocalFileInfo]:
        """Yield every file under ``path`` (recursively) in aws-cli's byte order.

        A thin wrapper over ``self.walker.list_files``:
        it anchors at the absolutized path with a trailing ``os.sep`` (so
        ``FileInfo.key`` is absolute, ``compare_key`` comes out relative to
        ``path``, and a non-directory root degrades to a "does not exist"
        warning rather than a scan error). The walk reads this storage's held
        source-config (``follow_symlinks`` / ``detect_symlink_loops`` /
        ``enumerate_all_entries`` from the constructor, via
        ``default_scan_options`` - the same walk ``scan``
        runs); ``on_warning`` is the per-call overlay, like an operation's. This
        is the raw walk (no ``ScanOptions.filter``); ``scan`` applies the filter
        through ``scan_pages``. The byte-order / warning semantics are the
        walker's - see ``LocalFileGenerator.list_files``. A single source
        object goes through ``get_fileinfo`` instead, not this walk.
        """
        # local_format(dir_op=True) form; self._abspath is computed once at
        # construction. list_files stamps compare_key (key relative to the
        # directory being enumerated) and, from options.storage below, each
        # entry's producing backend.
        yield from self._walker.list_files(
            self._os_root() + os.sep,
            replace(self.default_scan_options(), on_warning=on_warning, storage=self),
        )

    def _scan_one_level(self, root: str, options: LocalScanOptions) -> Iterator[LocalFileInfo]:
        """Yield immediate entries of scanned ``root`` for non-recursive scanning.

        ``root`` is the path being scanned. This is the one-level counterpart to
        the recursive walk: no descent, so a sub-directory surfaces as a single
        ``DIRECTORY`` entry (S3-style) rather than its files. A non-directory
        scanned path is the single-entry case - stat'd through ``_stat_one``
        honoring the passed ``follow_symlinks`` (not the storage's), with
        ``compare_key`` set to the basename, like ``get_fileinfo``. A directory is
        first vetted through ``should_ignore_file`` as in the recursive walk: a
        no-follow symlink is silently skipped and an unreadable or special path
        warns-and-skips. Complete enumeration instead classifies and emits the
        scanned path first, then its immediate children when it is a directory.
        Every entry has its ``compare_key`` stamped, its producing backend set
        (``options.storage``), and ``options.filter`` applied.

        ``root`` is text; the walker's methods get it in the OS form, and the
        warnings they build (which name that form) reach ``on_warning`` as
        text, like the recursive walk's (module docstring).
        """
        notify: Callable[[str], None] = (
            options.on_warning if options.on_warning is not None else (lambda body: None)
        )
        codec = self._codec
        walk_notify = notify if codec is None else _text_notify(notify, codec)
        os_root = self._os_root(root)
        item_filter = options.filter
        # The anchor (root + a separator), not the bare root, is what the root
        # step classifies or vets and what the children step scans: it names
        # each child the same way (os.path.join collapses a doubled separator)
        # and it is the path a failed scan warns about, so this one-level scan
        # and the recursive walk - which descends from that same anchor - name
        # the scanned directory identically.
        root_anchor = os.path.join(root, "")
        os_anchor = os.path.join(os_root, "")
        if options.enumerate_all_entries:
            root_record = _complete_root(self._walker, os_anchor, options, walk_notify, codec)
            if root_record is None:
                return
            if item_filter is None or item_filter(root_record):
                yield root_record
            if root_record.kind is not FileKind.DIRECTORY:
                return
        elif not os.path.isdir(os_root):
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
        elif self._walker.should_ignore_file(
            os_anchor, follow_symlinks=options.follow_symlinks, notify=walk_notify
        ):
            # Vet the root the way the recursive walk does (should_ignore_file on
            # the anchor, before any descent or scanned-path record): a no-follow
            # symlink root is silently skipped, and an unreadable / special root
            # warns-and-skips. So a one-level scan agrees with the recursive walk
            # instead of descending a link follow_symlinks=False forbids or
            # emitting a record the recursive walk's vetting would drop.
            return
        # scan_children stamps compare_key as info.key[strip:]; strip is the
        # normalized prefix length of the directory being enumerated
        # (root_anchor = root + a sep, the prefix of every child key). One level
        # down this equals the name.
        strip = len(root_anchor.replace(os.sep, "/"))
        for _sort_name, info, _loop_key in self._walker.scan_children(
            os_anchor, strip=strip, options=options, notify=walk_notify
        ):
            # scan_children stamps compare_key only; stamp the producing backend
            # here (options.storage == self, forced by scan_pages) before the filter.
            info.storage = options.storage
            # This record is kept as scanned (no yield-time re-stat), so its
            # timestamp warning is due here.
            _warn_unrepresentable_mtime(info, notify)
            if item_filter is None or item_filter(info):
                yield info

    def _stat_one(
        self, path: str, *, follow_symlinks: bool, notify: Callable[[str], None]
    ) -> LocalFileInfo | None:
        """One path's ``LocalFileInfo``, or ``None`` when there is no transferable entry.

        The local side of ``Storage.get_fileinfo``'s contract: a
        ``follow_symlinks=False`` symlink, or a definitively absent path (``ENOENT`` /
        ``ENOTDIR`` - including a broken symlink when following), is a silent ``None``; a special /
        unreadable file or directory warns via ``notify`` and returns ``None``
        (aws-cli's warn-and-skip); a regular file or directory returns a ``LocalFileInfo`` (no
        type check, so a directory is returned and fails later at open). A stat error
        other than absence (e.g. a permission error reaching the path) is raised -
        existence could not be determined. ``compare_key`` is the caller's to stamp.

        One ``os.stat`` is taken and **reused** for the size / mtime, the
        special-file mode check, and the info's ``stat_result`` - no re-stat, so
        the checks and the stored snapshot cannot disagree and no TOCTOU window
        opens between them (the walk's single-stat design via
        ``entry_stat_result`` / ``classify_child``). The info carries that
        one followed ``stat_result`` and the ``is_symlink`` flag.

        ``path`` is text: the OS calls take its OS form, while the key and the
        warnings keep the text.
        """
        native = self._os_root(path)
        is_symlink = os.path.islink(native)
        if not follow_symlinks and is_symlink:
            return None
        try:
            st = os.stat(native)  # the one snapshot, reused below (followed)
        except (FileNotFoundError, NotADirectoryError):
            # ENOENT and ENOTDIR (a path component along the way is a file, not a
            # directory) both mean the entry is definitively absent - os.path.exists
            # is False, aws-cli's own existence probe treats it as gone - so return
            # None per get_fileinfo's contract, not a raised transport error.
            return None
        except (OSError, ValueError) as exc:
            raise self._os_failure(exc, path) from exc
        if _is_special_mode(st.st_mode):
            notify(
                f"Skipping file {path}. File is character special device, "
                "block special device, FIFO, or socket."
            )
            return None
        if not is_readable(native, stat_module.S_ISDIR(st.st_mode)):
            notify(f"Skipping file {path}. File/Directory is not readable.")
            return None
        size, mtime = get_file_stat(native, st)
        if mtime is None:
            notify(_INVALID_TIMESTAMP)
            mtime = _EPOCH
        return LocalFileInfo(
            key=path.replace(os.sep, "/"),
            # A directory target carries its real kind (the FileInfo contract;
            # the transfer routes still hand it through to fail like aws).
            kind=FileKind.DIRECTORY if stat_module.S_ISDIR(st.st_mode) else FileKind.FILE,
            size=size,
            mtime=mtime,
            stat_result=st,
            is_symlink=is_symlink,
            storage=self,
        )

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        """Open ``key`` (resolved against the absolutized ``path``) as a binary stream.

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
        from the bucket (``producers.py``'s ``_warn_parent_reference`` port,
        warn-and-skip for ``aws s3`` parity).
        """
        # Anchor on the construction-time absolutized path like scan /
        # get_fileinfo, so a later chdir cannot move where a relative
        # location's keys resolve. key="" is the location itself (the
        # get_fileinfo convention) - os.path.join(x, "") would append a
        # trailing separator and, on "wb", makedirs a directory at the
        # target file's own path before the open fails.
        target = self._abspath if not key else os.path.join(self._abspath, to_native_path(key))
        try:
            native = self.os_path(target)
            if mode == "rb":
                return cast("BinaryIO", open(native, "rb"))
            parent = os.path.dirname(native)
            if parent:
                os.makedirs(parent, exist_ok=True)
            return cast("BinaryIO", open(native, "wb"))
        except (OSError, ValueError) as exc:
            raise self._os_failure(exc, target) from exc

    @override
    def delete(self, info: FileInfo) -> None:
        """Remove the file at ``info.key`` (an absolute local path, as text)."""
        try:
            os.remove(self.os_path(to_native_path(info.key)))
        except (OSError, ValueError) as exc:
            raise self._os_failure(exc, info.key) from exc

    def _text_names(self, exc: BaseException) -> None:
        """Name an OS error's files as this storage's text (no-op without a conversion)."""
        codec = self._codec
        if codec is not None:
            name_files_as_text(exc, codec.to_text)

    def _os_failure(self, exc: OSError | ValueError, path: str) -> Boto3S3Error:
        """This storage's error for an OS call's failure on the text ``path``.

        The one rule of the single-entry primitives (`_os_root` - so
        `_stat_one` - ``open`` and ``delete``), each of which raises the
        result ``from exc``: the files the error names are this storage's text
        (`_text_names`); ``operation`` is ``None``, as the storage cannot know
        which operation resolves, opens or deletes the path - the operation
        layer fills the name in (``attribute_failure``), so an mv's source
        delete and a sync's orphan removal each stamp their own; and ``key``
        names the path acted on, ``/``-separated, the entry's own address space
        (exceptions.md).
        """
        self._text_names(exc)
        return translate_os_error(exc, operation=None, key=path.replace(os.sep, "/"))

    @override
    def get_fileinfo(
        self,
        key: str = "",
        *,
        on_warning: Callable[[str], None] | None = None,
    ) -> LocalFileInfo | None:
        """Stat a single path (``Storage.get_fileinfo``).

        Anchored at the absolutized path (``self._abspath``, joined with ``key``
        for a child), so ``FileInfo.key`` is absolute; ``compare_key`` is its
        basename. As with ``open``, ``key`` is joined under the location and
        a ``..`` / absolute ``key`` deliberately resolves outside it (a building
        block an app drives, not a confinement boundary - see ``open``).
        Whether a symlink is followed is the storage's own ``follow_symlinks``
        config (constructor), like every scan built from
        ``default_scan_options`` (a caller-supplied ``LocalScanOptions``
        carries its own settings instead).
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
