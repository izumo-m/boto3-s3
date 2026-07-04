"""``boto3_s3.localstorage``: the aws-cli-order walk and the Storage surface.

Pins aws-cli ``FileGenerator.list_files`` parity for ``LocalStorage.walk_local``:
the byte-order sort (``foo.txt`` before ``foo/bar`` - the appended-separator
trick), depth-first interleaving, the warn-and-skip battery with aws-cli wording,
the silent symlink skip, and the invalid-timestamp epoch fallback. The single-path
point op (``LocalStorage.get_fileinfo``) is covered separately, including the
deliberate absence of a directory check (aws fails later at open - rc 1
``[Errno 21]``) and the existence-check contract (absent -> ``None``, no warning).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from boto3_s3.exceptions import NotFoundError
from boto3_s3.localstorage import (
    LocalFileGenerator,
    LocalStorage,
    LoopDetector,
    WalkChild,
    to_native_path,
)
from boto3_s3.types import FileInfo, FileKind, LocalFileInfo, ScanOptions

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _keys(tmp_path: Path, **kwargs: object) -> list[str]:
    root = str(tmp_path)
    prefix = root.replace(os.sep, "/") + "/"
    out: list[str] = []
    for info in LocalStorage(root).walk_local(**kwargs):  # type: ignore[arg-type]
        assert info.key.startswith(prefix)
        rel = info.key[len(prefix) :]
        # walk_local stamps compare_key with this same root-relative key.
        assert info.compare_key == rel
        out.append(rel)
    return out


def _make_tree(tmp_path: Path, *names: str) -> None:
    for name in names:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 3)


class TestWalkOrder:
    def test_files_sort_before_their_namesake_directory(self, tmp_path: Path) -> None:
        # 'a.txt' < 'a/' in byte order because '.' (0x2E) < '/' (0x2F): the
        # aws-cli sorts directory names with the separator appended so the
        # local stream matches an S3 listing of the uploaded keys.
        _make_tree(tmp_path, "a/inner.txt", "a.txt", "b.txt")
        assert _keys(tmp_path) == ["a.txt", "a/inner.txt", "b.txt"]

    def test_depth_first_interleaving(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "z.txt", "sub/n.txt", "sub/deep/m.txt", "sub2/o.txt")
        assert _keys(tmp_path) == ["sub/deep/m.txt", "sub/n.txt", "sub2/o.txt", "z.txt"]


class TestWalkWarnings:
    @pytest.mark.skipif(_IS_ROOT, reason="root reads anything")
    def test_unreadable_file_warns_and_is_skipped(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "ok.txt", "secret.txt")
        (tmp_path / "secret.txt").chmod(0)
        warnings: list[str] = []
        try:
            assert _keys(tmp_path, on_warning=warnings.append) == ["ok.txt"]
        finally:
            (tmp_path / "secret.txt").chmod(0o644)
        assert warnings == [
            f"Skipping file {tmp_path / 'secret.txt'}. File/Directory is not readable."
        ]

    @pytest.mark.skipif(_IS_ROOT, reason="root reads anything")
    def test_unreadable_directory_warns_once_and_prunes(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "ok.txt", "locked/inner.txt")
        (tmp_path / "locked").chmod(0)
        warnings: list[str] = []
        try:
            assert _keys(tmp_path, on_warning=warnings.append) == ["ok.txt"]
        finally:
            (tmp_path / "locked").chmod(0o755)
        assert len(warnings) == 1 and "is not readable" in warnings[0]

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
    def test_special_file_warns_with_awscli_wording(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "ok.txt")
        os.mkfifo(tmp_path / "pipe")  # pyright: ignore[reportAttributeAccessIssue]
        warnings: list[str] = []
        assert _keys(tmp_path, on_warning=warnings.append) == ["ok.txt"]
        assert warnings == [
            f"Skipping file {tmp_path / 'pipe'}. File is character special device, "
            "block special device, FIFO, or socket."
        ]

    def test_invalid_timestamp_falls_back_to_epoch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_tree(tmp_path, "a.txt")

        class _BoomDatetime:
            @staticmethod
            def fromtimestamp(_ts: float, tz: object = None) -> datetime:
                raise OverflowError("timestamp out of range")

        monkeypatch.setattr("boto3_s3.localstorage.datetime", _BoomDatetime)
        warnings: list[str] = []
        infos = list(LocalStorage(str(tmp_path)).walk_local(on_warning=warnings.append))
        assert [info.mtime for info in infos] == [datetime(1970, 1, 1, tzinfo=timezone.utc)]
        assert warnings == ["File has an invalid timestamp. Passing epoch time as timestamp."]


class TestSymlinks:
    def test_followed_by_default(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "real/inner.txt")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real" / "inner.txt")
        assert _keys(tmp_path) == ["link.txt", "real/inner.txt"]

    def test_no_follow_skips_links_silently(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "real/inner.txt", "plain.txt")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real" / "inner.txt")
        (tmp_path / "linkdir").symlink_to(tmp_path / "real")
        warnings: list[str] = []
        keys = _keys(tmp_path, follow_symlinks=False, on_warning=warnings.append)
        assert keys == ["plain.txt", "real/inner.txt"]
        assert warnings == []

    def test_broken_link_warns_when_following(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "plain.txt")
        (tmp_path / "broken").symlink_to(tmp_path / "gone")
        warnings: list[str] = []
        assert _keys(tmp_path, on_warning=warnings.append) == ["plain.txt"]
        assert warnings == [f"Skipping file {tmp_path / 'broken'}. File does not exist."]


class TestSymlinkLoopDetection:
    """``detect_loops`` (library extension, default off): an ancestor-stack guard."""

    def test_loop_skipped_with_a_warning_when_enabled(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt")
        (tmp_path / "loop").symlink_to(tmp_path)  # a directory cycle: loop -> the root
        warnings: list[str] = []
        keys = _keys(tmp_path, detect_loops=True, on_warning=warnings.append)
        assert keys == ["a.txt"]  # the cycle subtree is skipped, no RecursionError
        assert warnings == [f"Skipping file {tmp_path / 'loop'}. Symbolic link loop detected."]

    def test_diamond_links_are_both_followed(self, tmp_path: Path) -> None:
        # An ancestor stack, not a global visited set: two links to the same
        # *non-ancestor* directory are both followed (like GNU `find -L`).
        _make_tree(tmp_path, "target/t.txt")
        (tmp_path / "link1").symlink_to(tmp_path / "target")
        (tmp_path / "link2").symlink_to(tmp_path / "target")
        warnings: list[str] = []
        keys = _keys(tmp_path, detect_loops=True, on_warning=warnings.append)
        assert keys == ["link1/t.txt", "link2/t.txt", "target/t.txt"]
        assert warnings == []

    def test_enabled_is_a_noop_on_a_loopless_tree(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a/b.txt", "c.txt")
        warnings: list[str] = []
        assert _keys(tmp_path, detect_loops=True, on_warning=warnings.append) == _keys(tmp_path)
        assert warnings == []

    def test_no_follow_symlinks_disables_detection(self, tmp_path: Path) -> None:
        # follow_symlinks=False cannot reach a cycle (symlinks are skipped), so
        # the detector stays inactive - the loop link is skipped silently.
        _make_tree(tmp_path, "a.txt")
        (tmp_path / "loop").symlink_to(tmp_path)
        warnings: list[str] = []
        keys = _keys(tmp_path, follow_symlinks=False, detect_loops=True, on_warning=warnings.append)
        assert keys == ["a.txt"]
        assert warnings == []

    def test_loop_detector_is_a_usable_public_guard(self, tmp_path: Path) -> None:
        # LoopDetector is public for a custom walk: is_cycle registers a fresh
        # directory (returns False) and flags re-entry of an ancestor (True).
        (tmp_path / "child").mkdir()
        detector = LoopDetector(str(tmp_path))
        assert detector.is_cycle(str(tmp_path / "child")) is False  # fresh -> registered
        detector.leave()
        assert detector.is_cycle(str(tmp_path)) is True  # the seeded root is an ancestor

    def test_early_close_of_walk_dir_leaves_the_detector_balanced(self, tmp_path: Path) -> None:
        # walk_dir is a public seam a caller may drive with its own LoopDetector.
        # The file-run is flushed *before* is_cycle_key pushes the next subdir, so
        # closing the generator on that page cannot strand a push ahead of the
        # try/finally that pops it.
        _make_tree(tmp_path, "a.txt", "sub/b.txt")
        walker = LocalFileGenerator()
        root = str(tmp_path) + os.sep
        detector = LoopDetector(root)
        baseline = list(detector._ancestors)  # pyright: ignore[reportPrivateUsage]  # just the root
        gen = walker.walk_dir(root, ScanOptions(), notify=lambda _b: None, detector=detector)
        first = next(gen)  # [a.txt], flushed before descending into sub/
        assert [os.path.basename(i.key) for i in first] == ["a.txt"]
        gen.close()  # abandon before sub/ is consumed
        # sub/ was never pushed (flush precedes the push), so no ancestor leaked.
        assert detector._ancestors == baseline  # pyright: ignore[reportPrivateUsage]


class TestWalkIsCustomizable:
    """The walk is a ``LocalFileGenerator`` an app subclasses and injects via
    ``LocalStorage(path, walker=...)`` - overriding its public methods.

    The extension point behind, e.g., resolving Cygwin ``!<symlink>`` files on a
    native-Python Windows build - the walker dispatches through ``self``.
    """

    def test_should_ignore_entry_override_is_honored_through_scan(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "keep.txt", "skip.tmp")

        class _NoTmp(LocalFileGenerator):
            def should_ignore_entry(
                self,
                entry: os.DirEntry[str],
                full: str,
                dir_fd: int | None,
                *,
                follow_symlinks: bool,
                notify: Callable[[str], None],
            ) -> bool:
                if full.endswith(".tmp"):
                    return True
                return super().should_ignore_entry(
                    entry, full, dir_fd, follow_symlinks=follow_symlinks, notify=notify
                )

        storage = LocalStorage(str(tmp_path), walker=_NoTmp())
        keys = [info.compare_key for info in storage.scan(ScanOptions(recursive=True))]
        assert keys == ["keep.txt"]  # the override pruned skip.tmp

    def test_stat_info_override_can_rewrite_entries(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt")

        class _Tagged(LocalFileGenerator):
            def stat_info(
                self, entry: os.DirEntry[str], full: str, notify: Callable[[str], None]
            ) -> LocalFileInfo | None:
                info = super().stat_info(entry, full, notify)
                if info is not None:
                    info.size = 999
                return info

        infos = list(LocalStorage(str(tmp_path), walker=_Tagged()).walk_local())
        assert [(info.compare_key, info.size) for info in infos] == [("a.txt", 999)]

    def test_classify_child_override_filters_per_entry(self, tmp_path: Path) -> None:
        # The per-entry seam: decide file/dir/skip without touching the scan loop.
        _make_tree(tmp_path, "keep.txt", "skip.me", "sub/keep2.txt")

        class _Skip(LocalFileGenerator):
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
                if entry.name.endswith(".me"):
                    return None  # skip decided here, not in should_ignore_entry
                return super().classify_child(
                    entry,
                    full,
                    dir_fd,
                    follow_symlinks=follow_symlinks,
                    notify=notify,
                    capture_entry=capture_entry,
                )

        keys = [
            info.compare_key for info in LocalStorage(str(tmp_path), walker=_Skip()).walk_local()
        ]
        assert keys == ["keep.txt", "sub/keep2.txt"]

    def test_scan_children_reimplemented_with_building_blocks(self, tmp_path: Path) -> None:
        # An app re-implements scan_children to inject a synthetic entry into
        # every directory, reusing the public building blocks (no duplicated scan
        # loop, sort key, or directory-info shape).
        _make_tree(tmp_path, "a.txt", "sub/b.txt")

        class _WithSynthetic(LocalFileGenerator):
            def scan_children(
                self,
                dir_path: str,
                *,
                follow_symlinks: bool,
                notify: Callable[[str], None],
                capture_entry: bool,
            ) -> list[WalkChild]:
                children = super().scan_children(
                    dir_path,
                    follow_symlinks=follow_symlinks,
                    notify=notify,
                    capture_entry=capture_entry,
                )
                extra = WalkChild(
                    "_synthetic",
                    LocalFileInfo(
                        key=(dir_path + "_synthetic").replace(os.sep, "/"),
                        size=0,
                        mtime=datetime(2020, 1, 1, tzinfo=timezone.utc),
                    ),
                    None,
                )
                return self.normalize_sort([*children, extra])

        walker = _WithSynthetic()
        keys = [
            info.compare_key for info in LocalStorage(str(tmp_path), walker=walker).walk_local()
        ]
        # '_' (0x5F) < 'a' (0x61) < 's' - the synthetic sorts first at each level.
        assert keys == ["_synthetic", "a.txt", "sub/_synthetic", "sub/b.txt"]

    def test_walk_dir_override_prunes_a_subtree(self, tmp_path: Path) -> None:
        # walk_dir is the public recursion seam: override it and return early for
        # a directory to prune its subtree, calling super() for the rest. It
        # recurses via self.walk_dir, so the override applies at every level (no
        # depth counter needed - an app that wants depth re-implements the loop).
        _make_tree(tmp_path, "a.txt", "keep/b.txt", "skip/c.txt", "keep/skip/d.txt")

        class _PruneSkip(LocalFileGenerator):
            def walk_dir(
                self,
                dir_path: str,
                options: ScanOptions,
                *,
                notify: Callable[[str], None],
                detector: LoopDetector | None,
            ) -> Iterator[list[LocalFileInfo]]:
                if os.path.basename(dir_path.rstrip(os.sep)) == "skip":
                    return  # do not descend into any 'skip' directory
                yield from super().walk_dir(dir_path, options, notify=notify, detector=detector)

        keys = [
            info.compare_key
            for info in LocalStorage(str(tmp_path), walker=_PruneSkip()).walk_local()
        ]
        # every 'skip/' subtree is pruned at any depth (top-level and nested)
        assert keys == ["a.txt", "keep/b.txt"]


class TestScanPagesAreScandirAligned:
    """``scan_pages`` hands off one page per directory file-run (one ``os.scandir``)."""

    def test_pages_fall_on_directory_boundaries(self, tmp_path: Path) -> None:
        # root=[a.txt, sub/, z.txt], sub=[b.txt]. Byte order interleaves sub's
        # page between root's two file-runs, so the pages are [a.txt], [sub/b.txt],
        # [z.txt] - not one arbitrary fixed-size batch.
        _make_tree(tmp_path, "a.txt", "z.txt", "sub/b.txt")
        pages = [
            [info.compare_key for info in page]
            for page in LocalStorage(str(tmp_path)).scan_pages(ScanOptions(recursive=True))
        ]
        assert pages == [["a.txt"], ["sub/b.txt"], ["z.txt"]]

    def test_files_of_one_directory_share_a_page(self, tmp_path: Path) -> None:
        # No sub-directory splits them, so a directory's files come out as one page.
        _make_tree(tmp_path, "a.txt", "b.txt", "c.txt")
        pages = [
            [info.compare_key for info in page]
            for page in LocalStorage(str(tmp_path)).scan_pages(ScanOptions(recursive=True))
        ]
        assert pages == [["a.txt", "b.txt", "c.txt"]]

    def test_non_recursive_level_is_one_page(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt", "sub/b.txt")
        pages = list(LocalStorage(str(tmp_path)).scan_pages(ScanOptions(recursive=False)))
        assert len(pages) == 1
        assert [info.compare_key for info in pages[0]] == ["a.txt", "sub/"]


class TestGetFileinfo:
    """``LocalStorage.get_fileinfo`` - the single-path point op (no walk)."""

    def test_single_file(self, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_bytes(b"12345")
        info = LocalStorage(str(target)).get_fileinfo()
        assert info is not None
        assert info.key == str(target).replace(os.sep, "/")  # absolutized
        assert info.compare_key == "a.txt"  # basename
        assert info.size == 5
        assert info.mtime is not None and info.mtime.tzinfo is not None

    def test_directory_is_returned_without_a_type_check(self, tmp_path: Path) -> None:
        # aws parity: no type check, so a directory source yields a FileInfo and
        # fails later at open ([Errno 21], rc 1).
        info = LocalStorage(str(tmp_path)).get_fileinfo()
        assert info is not None
        assert info.key == str(tmp_path).replace(os.sep, "/")

    def test_missing_returns_none_silently(self, tmp_path: Path) -> None:
        # Definitively absent -> None, no warning (the existence-check contract).
        warnings: list[str] = []
        info = LocalStorage(str(tmp_path / "nope")).get_fileinfo(on_warning=warnings.append)
        assert info is None
        assert warnings == []

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
    def test_special_file_warns_and_skips(self, tmp_path: Path) -> None:
        pipe = tmp_path / "pipe"
        os.mkfifo(pipe)  # pyright: ignore[reportAttributeAccessIssue]
        warnings: list[str] = []
        assert LocalStorage(str(pipe)).get_fileinfo(on_warning=warnings.append) is None
        assert warnings == [
            f"Skipping file {pipe}. File is character special device, "
            "block special device, FIFO, or socket."
        ]

    @pytest.mark.skipif(_IS_ROOT, reason="root reads anything")
    def test_unreadable_file_warns_and_skips(self, tmp_path: Path) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"x")
        secret.chmod(0)
        warnings: list[str] = []
        try:
            assert LocalStorage(str(secret)).get_fileinfo(on_warning=warnings.append) is None
        finally:
            secret.chmod(0o644)
        assert warnings == [f"Skipping file {secret}. File/Directory is not readable."]

    def test_no_follow_symlink_skips_silently(self, tmp_path: Path) -> None:
        target = tmp_path / "real.txt"
        target.write_bytes(b"x")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        warnings: list[str] = []
        info = LocalStorage(str(link)).get_fileinfo(
            follow_symlinks=False, on_warning=warnings.append
        )
        assert info is None
        assert warnings == []

    def test_child_key_joins_under_the_location(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "sub/f.txt")
        info = LocalStorage(str(tmp_path)).get_fileinfo("sub/f.txt")
        assert info is not None
        assert info.key == str(tmp_path / "sub" / "f.txt").replace(os.sep, "/")
        assert info.compare_key == "f.txt"

    def test_parent_reference_key_resolves_outside_the_location(self, tmp_path: Path) -> None:
        # No trust boundary in the API (the caller owns both the location and the
        # key), so a "../" key navigates the parent as an app expects - it is not
        # confined. This is by design; see LocalStorage.open's docstring.
        root = tmp_path / "root"
        root.mkdir()
        (tmp_path / "sibling.txt").write_bytes(b"x")
        info = LocalStorage(str(root)).get_fileinfo("../sibling.txt")
        assert info is not None
        assert info.size == 1
        assert info.compare_key == "sibling.txt"


class TestLocalStorageScan:
    def test_recursive_scan_streams_the_walk(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a/inner.txt", "a.txt")
        infos = list(LocalStorage(tmp_path).scan(ScanOptions(recursive=True)))
        prefix = str(tmp_path).replace(os.sep, "/")
        assert [info.key for info in infos] == [f"{prefix}/a.txt", f"{prefix}/a/inner.txt"]
        # scan stamps the root-relative compare_key on every entry.
        assert [info.compare_key for info in infos] == ["a.txt", "a/inner.txt"]

    def test_non_recursive_lists_one_level_with_directories(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a/inner.txt", "a.txt")
        infos = list(LocalStorage(tmp_path).scan())
        prefix = str(tmp_path).replace(os.sep, "/")
        assert [(info.key, info.kind, info.compare_key) for info in infos] == [
            (f"{prefix}/a.txt", FileKind.FILE, "a.txt"),
            (f"{prefix}/a/", FileKind.DIRECTORY, "a/"),
        ]

    def test_scan_filter_applies(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt", "b.bin")
        options = ScanOptions(recursive=True, filter=lambda info: info.key.endswith(".txt"))
        keys = [info.key for info in LocalStorage(tmp_path).scan(options)]
        assert keys == [str(tmp_path).replace(os.sep, "/") + "/a.txt"]


class TestCaptureEntry:
    """``capture_entry`` fills ``LocalFileInfo.entry`` with a usable ``os.DirEntry``."""

    def test_default_leaves_entry_none(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt", "sub/b.txt")
        infos = list(LocalStorage(str(tmp_path)).walk_local())
        assert infos and all(info.entry is None for info in infos)

    def test_capture_populates_and_matches_the_info(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt", "sub/b.txt")
        infos = list(LocalStorage(str(tmp_path)).walk_local(capture_entry=True))
        assert {info.compare_key for info in infos} == {"a.txt", "sub/b.txt"}
        for info in infos:
            assert info.entry is not None
            # the entry describes the same file the info does
            assert info.key.endswith(info.entry.name)
            assert info.entry.stat().st_size == info.size
            assert info.entry.is_dir() is False

    def test_captured_entry_is_usable_after_the_walk(self, tmp_path: Path) -> None:
        # The reason capture_entry scans by path, not a dir_fd: the entry must
        # survive being used once the walk has closed the directory. A symlink is
        # the case a fd-form entry would fail (uncached lstat -> Bad file
        # descriptor); the path-form entry re-stats on demand.
        _make_tree(tmp_path, "real.txt")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
        # Fully drain the walk first, then touch the retained entries.
        infos = list(LocalStorage(str(tmp_path)).walk_local(capture_entry=True))
        by_key = {info.compare_key: info for info in infos}
        link = by_key["link.txt"].entry
        assert link is not None
        assert os.path.isabs(link.path) and os.path.exists(link.path)  # full path, not basename
        assert link.is_symlink() is True
        assert link.stat(follow_symlinks=True).st_size == 3  # follows to real.txt (b"x"*3)
        assert link.stat(follow_symlinks=False).st_size != 3  # the link's own lstat, uncached

    def test_capture_through_scan_options_reaches_a_filter(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "keep.txt", "sub/inner.txt")
        seen: list[str] = []

        def use_entry(info: FileInfo) -> bool:
            # A filter that reuses the entry's cached stat (no fresh syscall).
            assert isinstance(info, LocalFileInfo)
            assert info.entry is not None
            seen.append(f"{info.compare_key}:{info.entry.stat().st_size}")
            return True

        options = ScanOptions(recursive=True, capture_entry=True, filter=use_entry)
        infos = list(LocalStorage(str(tmp_path)).scan(options))
        assert len(infos) == 2
        assert sorted(seen) == ["keep.txt:3", "sub/inner.txt:3"]

    def test_non_recursive_captures_entry_on_files_and_directories(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt", "sub/inner.txt")
        infos = list(LocalStorage(str(tmp_path)).scan(ScanOptions(capture_entry=True)))
        by_key = {info.compare_key: info for info in infos}
        assert by_key["a.txt"].entry is not None and by_key["a.txt"].entry.is_dir() is False
        # a directory entry from the one-level listing is captured too
        assert by_key["sub/"].kind is FileKind.DIRECTORY
        assert by_key["sub/"].entry is not None and by_key["sub/"].entry.is_dir() is True


class TestLocalStorageIO:
    def test_open_round_trip_creates_parents(self, tmp_path: Path) -> None:
        storage = LocalStorage(tmp_path)
        with storage.open("sub/deep/f.bin", "wb") as handle:
            handle.write(b"payload")
        with storage.open("sub/deep/f.bin", "rb") as handle:
            assert handle.read() == b"payload"

    def test_open_missing_for_read_raises_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(NotFoundError):
            LocalStorage(tmp_path).open("missing.bin", "rb")

    def test_open_empty_key_is_the_location_itself(self, tmp_path: Path) -> None:
        # key="" is the location itself (the get_fileinfo convention):
        # os.path.join(x, "") would append a trailing separator, making the
        # read fail ENOTDIR and the write makedirs a directory at the target
        # file's own path.
        target = tmp_path / "single.bin"
        storage = LocalStorage(str(target))
        with storage.open("", "wb") as handle:
            handle.write(b"payload")
        assert target.read_bytes() == b"payload"
        assert not target.is_dir()
        with storage.open("", "rb") as handle:
            assert handle.read() == b"payload"

    def test_open_relative_path_is_chdir_stable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # open anchors on the construction-time absolutized path like scan /
        # get_fileinfo: a later chdir must not move where keys resolve.
        (tmp_path / "root").mkdir()
        (tmp_path / "root" / "f.bin").write_bytes(b"payload")
        (tmp_path / "elsewhere").mkdir()
        monkeypatch.chdir(tmp_path)
        storage = LocalStorage("root")
        monkeypatch.chdir(tmp_path / "elsewhere")
        with storage.open("f.bin", "rb") as handle:
            assert handle.read() == b"payload"

    def test_delete(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt")
        storage = LocalStorage(tmp_path)
        info = storage.get_fileinfo("a.txt")
        assert info is not None
        storage.delete(info)
        assert not (tmp_path / "a.txt").exists()
        with pytest.raises(NotFoundError):
            storage.delete(info)


def test_to_native_path_round_trips() -> None:
    assert to_native_path("a/b/c.txt") == os.sep.join(["a", "b", "c.txt"])
