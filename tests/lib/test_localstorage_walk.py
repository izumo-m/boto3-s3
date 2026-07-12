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
import stat
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from boto3_s3.exceptions import NotFoundError
from boto3_s3.globsieve import GlobFilter
from boto3_s3.localstorage import (
    LocalFileGenerator,
    LocalStorage,
    LoopDetector,
    WalkChild,
    to_native_path,
)
from boto3_s3.types import FileInfo, FileKind, LocalFileInfo, LocalScanOptions, ScanOptions
from tests.utils.host import skip_if_chmod_is_inert


def _keys(
    tmp_path: Path,
    on_warning: Callable[[str], None] | None = None,
    **config: bool,
) -> list[str]:
    # config holds the LocalStorage constructor's scan source-config
    # (follow_symlinks / detect_symlink_loops); walk_local reads it from the
    # storage, taking only the per-call on_warning overlay.
    root = str(tmp_path)
    prefix = root.replace(os.sep, "/") + "/"
    out: list[str] = []
    for info in LocalStorage(root, **config).walk_local(on_warning=on_warning):
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
    @skip_if_chmod_is_inert
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

    @skip_if_chmod_is_inert
    def test_unreadable_directory_warns_once_and_prunes(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "ok.txt", "locked/inner.txt")
        (tmp_path / "locked").chmod(0)
        warnings: list[str] = []
        try:
            assert _keys(tmp_path, on_warning=warnings.append) == ["ok.txt"]
        finally:
            (tmp_path / "locked").chmod(0o755)
        assert len(warnings) == 1 and "is not readable" in warnings[0]

    def test_symlink_cycle_descent_warns_like_aws(self, tmp_path: Path) -> None:
        # A directory cycle without detect_symlink_loops (the aws-parity mode):
        # the per-entry vetting is dir-relative (one link per resolution) so
        # every unrolled level is admitted, until the descent's full-path open
        # accumulates SYMLOOP_MAX links and the kernel stops it with ELOOP.
        # aws-cli vets children by full path, so os.path.exists() fails there
        # and it warns "File does not exist." and exits rc 2 - the walk must
        # emit that same warning, not prune the subtree silently (rc 0).
        _make_tree(tmp_path, "a/keep.txt")
        (tmp_path / "a" / "up").symlink_to(tmp_path)
        warnings: list[str] = []
        keys = _keys(tmp_path, on_warning=warnings.append)
        # The unrolled levels all surface their file before the descent fails.
        assert keys and all(key.endswith("keep.txt") for key in keys)
        if os.name == "nt":
            # No ELOOP on Windows: the descent dies on the path-length limit
            # instead, at a host-dependent depth with the not-readable wording -
            # pin only that it warns rather than pruning silently.
            assert warnings and all(w.startswith("Skipping file ") for w in warnings)
            return
        assert len(warnings) == 1
        assert warnings[0].startswith("Skipping file ")
        assert warnings[0].endswith(". File does not exist.")
        warned = warnings[0].removeprefix("Skipping file ").removesuffix(". File does not exist.")
        assert os.path.basename(warned) == "up"  # the cycle link the descent died on

    def test_symlink_leaf_at_the_cycle_boundary_warns_like_aws(self, tmp_path: Path) -> None:
        # A cycle whose boundary directory still holds a *symlink* file leaf: the
        # fast walk vets that leaf through the directory fd (its own single link,
        # re-anchored), so a dir-relative probe admits it - but the leaf's full path
        # crosses SYMLOOP_MAX and the transfer's full-path open would ELOOP (rc 1).
        # aws-cli stats every entry by full path, so it warn-skips the leaf ("File
        # does not exist.", rc 2). The walk must match: every admitted entry
        # resolves by full path, and the boundary link is warned away rather than
        # admitted to fail at open (a regular-file leaf, unlike this one, never
        # diverges - its full path resolves the same ancestor chain the directory
        # open already survived, so only a symlink leaf needs this fallback).
        _make_tree(tmp_path, "keep.txt")
        (tmp_path / "link").symlink_to(tmp_path / "keep.txt")  # a symlink file leaf
        (tmp_path / "loop").symlink_to(tmp_path)  # a directory cycle
        warnings: list[str] = []
        infos = list(LocalStorage(str(tmp_path)).walk_local(on_warning=warnings.append))
        # No admitted entry may fail its full-path resolution - that is the rc-1
        # leak the fd-relative vetting would otherwise let through.
        assert all(os.path.exists(to_native_path(info.key)) for info in infos)
        if os.name == "nt":
            # No ELOOP on Windows: the boundary is path length, at a host-dependent
            # depth - pin only that it warns, not admits an unopenable leaf.
            assert warnings and all(w.startswith("Skipping file ") for w in warnings)
            return
        leaf_warnings = [
            w
            for w in warnings
            if w.endswith(". File does not exist.")
            and os.path.basename(
                w.removeprefix("Skipping file ").removesuffix(". File does not exist.")
            )
            == "link"
        ]
        assert leaf_warnings, warnings  # the boundary symlink leaf, warned away by name

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
    """``detect_symlink_loops`` (library extension, default off): an ancestor-stack guard."""

    def test_loop_skipped_with_a_warning_when_enabled(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt")
        (tmp_path / "loop").symlink_to(tmp_path)  # a directory cycle: loop -> the root
        warnings: list[str] = []
        keys = _keys(tmp_path, detect_symlink_loops=True, on_warning=warnings.append)
        assert keys == ["a.txt"]  # the cycle subtree is skipped, no RecursionError
        assert warnings == [f"Skipping file {tmp_path / 'loop'}. Symbolic link loop detected."]

    def test_diamond_links_are_both_followed(self, tmp_path: Path) -> None:
        # An ancestor stack, not a global visited set: two links to the same
        # *non-ancestor* directory are both followed (like GNU `find -L`).
        _make_tree(tmp_path, "target/t.txt")
        (tmp_path / "link1").symlink_to(tmp_path / "target")
        (tmp_path / "link2").symlink_to(tmp_path / "target")
        warnings: list[str] = []
        keys = _keys(tmp_path, detect_symlink_loops=True, on_warning=warnings.append)
        assert keys == ["link1/t.txt", "link2/t.txt", "target/t.txt"]
        assert warnings == []

    def test_enabled_is_a_noop_on_a_loopless_tree(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a/b.txt", "c.txt")
        warnings: list[str] = []
        assert _keys(tmp_path, detect_symlink_loops=True, on_warning=warnings.append) == _keys(
            tmp_path
        )
        assert warnings == []

    def test_no_follow_symlinks_disables_detection(self, tmp_path: Path) -> None:
        # follow_symlinks=False cannot reach a cycle (symlinks are skipped), so
        # the detector stays inactive - the loop link is skipped silently.
        _make_tree(tmp_path, "a.txt")
        (tmp_path / "loop").symlink_to(tmp_path)
        warnings: list[str] = []
        keys = _keys(
            tmp_path,
            follow_symlinks=False,
            detect_symlink_loops=True,
            on_warning=warnings.append,
        )
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
        strip = len(root.replace(os.sep, "/"))
        gen = walker.walk_dir(
            root, LocalScanOptions(), strip=strip, notify=lambda _b: None, detector=detector
        )
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
                st: os.stat_result,
                *,
                notify: Callable[[str], None],
            ) -> bool:
                if full.endswith(".tmp"):
                    return True
                return super().should_ignore_entry(entry, full, dir_fd, st, notify=notify)

        storage = LocalStorage(str(tmp_path), walker=_NoTmp())
        keys = [info.compare_key for info in storage.scan(LocalScanOptions(recursive=True))]
        assert keys == ["keep.txt"]  # the override pruned skip.tmp

    def test_stat_info_override_can_rewrite_entries(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt")

        class _Tagged(LocalFileGenerator):
            def stat_info(
                self,
                entry: os.DirEntry[str],
                full: str,
                st: os.stat_result,
                notify: Callable[[str], None],
            ) -> LocalFileInfo:
                info = super().stat_info(entry, full, st, notify)
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
                options: LocalScanOptions,
                notify: Callable[[str], None],
            ) -> WalkChild | None:
                if entry.name.endswith(".me"):
                    return None  # skip decided here, not in should_ignore_entry
                return super().classify_child(entry, full, dir_fd, options=options, notify=notify)

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
                strip: int,
                options: LocalScanOptions,
                notify: Callable[[str], None],
                sym_depth: int = 0,
            ) -> list[WalkChild]:
                children = super().scan_children(
                    dir_path, strip=strip, options=options, notify=notify, sym_depth=sym_depth
                )
                # An injected child stamps its own compare_key (info.key[strip:]).
                key = (dir_path + "_synthetic").replace(os.sep, "/")
                extra = WalkChild(
                    "_synthetic",
                    LocalFileInfo(
                        key=key,
                        compare_key=key[strip:],
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
        # recurses via self.walk_dir, so the override applies at every level; the
        # followed-symlink depth counter is threaded through unchanged (an app that
        # wants its own depth logic re-implements the loop).
        _make_tree(tmp_path, "a.txt", "keep/b.txt", "skip/c.txt", "keep/skip/d.txt")

        class _PruneSkip(LocalFileGenerator):
            def walk_dir(
                self,
                dir_path: str,
                options: ScanOptions,
                *,
                strip: int,
                notify: Callable[[str], None],
                detector: LoopDetector | None,
                sym_depth: int = 0,
            ) -> Iterator[list[LocalFileInfo]]:
                if os.path.basename(dir_path.rstrip(os.sep)) == "skip":
                    return  # do not descend into any 'skip' directory
                yield from super().walk_dir(
                    dir_path,
                    options,
                    strip=strip,
                    notify=notify,
                    detector=detector,
                    sym_depth=sym_depth,
                )

        keys = [
            info.compare_key
            for info in LocalStorage(str(tmp_path), walker=_PruneSkip()).walk_local()
        ]
        # every 'skip/' subtree is pruned at any depth (top-level and nested)
        assert keys == ["a.txt", "keep/b.txt"]

    def test_finalize_children_is_the_only_customization_needed_for_backup_pruning(
        self, tmp_path: Path
    ) -> None:
        # Complete enumeration owns directories, lstat symlinks, and vetting.
        # A backup walker only prunes excluded directory children before descent.
        _make_tree(tmp_path, "keep.txt", "skip/inner.txt", "sub/nested.txt")
        (tmp_path / "link.txt").symlink_to(tmp_path / "keep.txt")

        class _ManifestWalker(LocalFileGenerator):
            def finalize_children(self, children: list[WalkChild]) -> list[WalkChild]:
                kept = [
                    child
                    for child in children
                    if child.info.compare_key is not None
                    and child.info.compare_key.rstrip("/") != "skip"
                ]
                return self.normalize_sort(kept)

        infos = list(
            LocalStorage(
                tmp_path,
                walker=_ManifestWalker(),
                follow_symlinks=False,
                enumerate_all_entries=True,
            ).walk_local()
        )
        assert [info.compare_key for info in infos] == [
            "",
            "keep.txt",
            "link.txt",
            "sub/",
            "sub/nested.txt",
        ]
        assert infos[2].is_symlink


class TestEnumerateAllEntries:
    """The complete filesystem-entry view selected before filtering."""

    def test_complete_view_surfaces_every_directory_and_the_root(self, tmp_path: Path) -> None:
        # Complete enumeration includes the walk root:
        # its record leads the stream at compare_key "" (sorts before every
        # child key). foo/ lands between foo.txt and foo/bar ('.' < '/'), and
        # an empty directory surfaces despite owning no files.
        _make_tree(tmp_path, "a.txt", "foo.txt", "foo/bar")
        (tmp_path / "empty").mkdir()

        infos = list(LocalStorage(str(tmp_path), enumerate_all_entries=True).walk_local())
        keys = [i.compare_key for i in infos]
        assert keys == ["", "a.txt", "empty/", "foo.txt", "foo/", "foo/bar"]
        kinds = {i.compare_key: i.kind for i in infos}
        assert kinds[""] is FileKind.DIRECTORY
        assert kinds["foo/"] is FileKind.DIRECTORY
        assert kinds["foo/bar"] is FileKind.FILE
        root = infos[0]
        assert root.stat_result is not None
        assert root.key.endswith("/")  # the walk anchor, separator-folded

    def test_complete_no_follow_view_keeps_links_as_lstat_leaves(self, tmp_path: Path) -> None:
        # Complete no-follow enumeration returns every link as an lstat-based leaf. The dir
        # link is not descended, the broken link is an entry rather than a
        # warning, and the walk warns about nothing.
        _make_tree(tmp_path, "real/inner.txt")
        (tmp_path / "link_dir").symlink_to(tmp_path / "real")
        (tmp_path / "link_broken").symlink_to(tmp_path / "nowhere")
        warnings: list[str] = []

        infos = list(
            LocalStorage(
                str(tmp_path), follow_symlinks=False, enumerate_all_entries=True
            ).walk_local(on_warning=warnings.append)
        )
        assert [(i.compare_key, i.is_symlink) for i in infos] == [
            ("", False),
            ("link_broken", True),
            ("link_dir", True),
            ("real/", False),
            ("real/inner.txt", False),
        ]
        assert warnings == []
        link = next(i for i in infos if i.compare_key == "link_dir")
        assert link.stat_result is not None
        assert stat.S_ISLNK(link.stat_result.st_mode)  # the link's own lstat

    def test_complete_followed_view_returns_one_target_entry_per_symlink(
        self, tmp_path: Path
    ) -> None:
        # Following selects the target view. A directory link is one DIRECTORY
        # entry and is descended; a file link is one FILE entry with the target
        # stat. Neither path also produces an lstat leaf.
        _make_tree(tmp_path, "real/inner.txt", "target.txt")
        (tmp_path / "dlink").symlink_to(tmp_path / "real")
        (tmp_path / "flink").symlink_to(tmp_path / "target.txt")

        infos = list(LocalStorage(str(tmp_path), enumerate_all_entries=True).walk_local())
        assert [(i.compare_key, i.kind, i.is_symlink) for i in infos] == [
            ("", FileKind.DIRECTORY, False),
            ("dlink/", FileKind.DIRECTORY, True),
            ("dlink/inner.txt", FileKind.FILE, False),
            ("flink", FileKind.FILE, True),
            ("real/", FileKind.DIRECTORY, False),
            ("real/inner.txt", FileKind.FILE, False),
            ("target.txt", FileKind.FILE, False),
        ]
        file_link = next(info for info in infos if info.compare_key == "flink")
        assert file_link.stat_result is not None
        assert stat.S_ISREG(file_link.stat_result.st_mode)

    def test_complete_followed_view_falls_back_to_lstat_with_warning(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken"
        broken.symlink_to(tmp_path / "gone")
        loop = tmp_path / "loop"
        loop.symlink_to(loop)
        warnings: list[str] = []

        infos = list(
            LocalStorage(tmp_path, enumerate_all_entries=True).walk_local(
                on_warning=warnings.append
            )
        )

        assert [(info.compare_key, info.is_symlink) for info in infos] == [
            ("", False),
            ("broken", True),
            ("loop", True),
        ]
        assert all(
            info.stat_result is not None and stat.S_ISLNK(info.stat_result.st_mode)
            for info in infos[1:]
        )
        assert set(warnings) == {
            f"Skipping file {broken}. File does not exist.",
            f"Skipping file {loop}. File does not exist.",
        }

    def test_complete_followed_view_reports_once_when_link_disappears_before_lstat(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "target.txt"
        target.write_bytes(b"x")
        link = tmp_path / "link"
        link.symlink_to(target)
        warnings: list[str] = []

        class _DisappearingLink(LocalFileGenerator):
            def entry_stat_result(self, entry: os.DirEntry[str]) -> os.stat_result | None:
                if entry.name == "link":
                    link.unlink()
                    return None
                return super().entry_stat_result(entry)

        infos = list(
            LocalStorage(
                tmp_path, walker=_DisappearingLink(), enumerate_all_entries=True
            ).walk_local(on_warning=warnings.append)
        )

        assert [info.compare_key for info in infos] == ["", "target.txt"]
        assert warnings == [f"Skipping file {link}. File does not exist."]

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
    def test_complete_view_includes_special_files_without_transfer_warnings(
        self, tmp_path: Path
    ) -> None:
        pipe = tmp_path / "pipe"
        os.mkfifo(pipe)  # pyright: ignore[reportAttributeAccessIssue]
        warnings: list[str] = []

        infos = list(
            LocalStorage(tmp_path, enumerate_all_entries=True).walk_local(
                on_warning=warnings.append
            )
        )

        assert [info.compare_key for info in infos] == ["", "pipe"]
        pipe_info = infos[1]
        assert pipe_info.kind is FileKind.FILE
        assert pipe_info.stat_result is not None
        assert stat.S_ISFIFO(pipe_info.stat_result.st_mode)
        assert warnings == []

    @skip_if_chmod_is_inert
    def test_complete_view_includes_unreadable_file_without_warning(self, tmp_path: Path) -> None:
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"secret")
        secret.chmod(0)
        warnings: list[str] = []
        try:
            infos = list(
                LocalStorage(tmp_path, enumerate_all_entries=True).walk_local(
                    on_warning=warnings.append
                )
            )
        finally:
            secret.chmod(0o644)

        assert [info.compare_key for info in infos] == ["", "secret.txt"]
        assert infos[1].stat_result is not None
        assert warnings == []

    @skip_if_chmod_is_inert
    def test_complete_view_keeps_unreadable_directory_record_when_descent_fails(
        self, tmp_path: Path
    ) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "hidden.txt").write_bytes(b"x")
        locked.chmod(0)
        warnings: list[str] = []
        try:
            infos = list(
                LocalStorage(tmp_path, enumerate_all_entries=True).walk_local(
                    on_warning=warnings.append
                )
            )
        finally:
            locked.chmod(0o755)

        assert [(info.compare_key, info.kind) for info in infos] == [
            ("", FileKind.DIRECTORY),
            ("locked/", FileKind.DIRECTORY),
        ]
        assert infos[1].stat_result is not None
        assert len(warnings) == 1 and "is not readable" in warnings[0]

    def test_root_record_and_the_scan_filter_on_its_empty_key(self, tmp_path: Path) -> None:
        # The root's compare_key is "": a glob '*' matches it (fnmatch), a
        # non-empty literal does not - so an exclude-everything filter drops
        # the root record too, while a targeted exclude leaves it standing.
        _make_tree(tmp_path, "a.txt")
        storage = LocalStorage(str(tmp_path))

        drop_all = GlobFilter().exclude("*").compile()
        opts = LocalScanOptions(recursive=True, enumerate_all_entries=True, filter=drop_all)
        assert list(storage.scan(opts)) == []

        drop_txt = GlobFilter().exclude("*.txt").compile()
        opts = LocalScanOptions(recursive=True, enumerate_all_entries=True, filter=drop_txt)
        assert [info.compare_key for info in storage.scan(opts)] == [""]

    def test_non_recursive_complete_view_returns_root_and_immediate_entries(
        self, tmp_path: Path
    ) -> None:
        _make_tree(tmp_path, "real/inner.txt", "target.txt")
        (tmp_path / "dlink").symlink_to(tmp_path / "real")
        storage = LocalStorage(str(tmp_path))

        followed = LocalScanOptions(recursive=False, enumerate_all_entries=True)
        assert [(i.compare_key, i.kind, i.is_symlink) for i in storage.scan(followed)] == [
            ("", FileKind.DIRECTORY, False),
            ("dlink/", FileKind.DIRECTORY, True),
            ("real/", FileKind.DIRECTORY, False),
            ("target.txt", FileKind.FILE, False),
        ]
        nofollow = LocalScanOptions(
            recursive=False, enumerate_all_entries=True, follow_symlinks=False
        )
        assert [(i.compare_key, i.kind, i.is_symlink) for i in storage.scan(nofollow)] == [
            ("", FileKind.DIRECTORY, False),
            ("dlink", FileKind.FILE, True),
            ("real/", FileKind.DIRECTORY, False),
            ("target.txt", FileKind.FILE, False),
        ]

    def test_non_recursive_root_record_obeys_the_scan_filter_on_its_empty_key(
        self, tmp_path: Path
    ) -> None:
        # Same empty-key filter contract as the recursive root: a glob '*'
        # matches "" so exclude-all drops the root too, a non-empty literal
        # does not so a targeted exclude leaves it standing.
        _make_tree(tmp_path, "a.txt")
        storage = LocalStorage(str(tmp_path))

        drop_all = GlobFilter().exclude("*").compile()
        opts = LocalScanOptions(recursive=False, enumerate_all_entries=True, filter=drop_all)
        assert list(storage.scan(opts)) == []

        drop_txt = GlobFilter().exclude("*.txt").compile()
        opts = LocalScanOptions(recursive=False, enumerate_all_entries=True, filter=drop_txt)
        assert [info.compare_key for info in storage.scan(opts)] == [""]

    def test_complete_leaf_root_is_the_only_entry_for_both_scan_shapes(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "one.txt"
        root.write_bytes(b"one")
        storage = LocalStorage(root)

        for recursive in (True, False):
            infos = list(
                storage.scan(LocalScanOptions(recursive=recursive, enumerate_all_entries=True))
            )
            assert [(i.compare_key, i.kind, i.size) for i in infos] == [("", FileKind.FILE, 3)]
            assert infos[0].stat_result is not None

    def test_complete_no_follow_symlink_root_is_an_lstat_leaf_for_both_scan_shapes(
        self, tmp_path: Path
    ) -> None:
        real = tmp_path / "real"
        real.mkdir()
        (real / "a.txt").write_bytes(b"x")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        storage = LocalStorage(str(link))

        warnings: list[str] = []
        for recursive in (True, False):
            opts = LocalScanOptions(
                recursive=recursive,
                follow_symlinks=False,
                enumerate_all_entries=True,
                on_warning=warnings.append,
            )
            infos = list(storage.scan(opts))
            assert [(i.compare_key, i.kind, i.is_symlink) for i in infos] == [
                ("", FileKind.FILE, True)
            ]
            assert infos[0].stat_result is not None
            assert stat.S_ISLNK(infos[0].stat_result.st_mode)
        assert warnings == []

    @skip_if_chmod_is_inert
    def test_unreadable_root_record_survives_failed_descent_for_both_scan_shapes(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "locked"
        root.mkdir()
        (root / "a.txt").write_bytes(b"x")
        root.chmod(0)
        outcomes: list[tuple[list[str], list[str]]] = []
        try:
            for recursive in (True, False):
                warnings: list[str] = []
                opts = LocalScanOptions(
                    recursive=recursive,
                    enumerate_all_entries=True,
                    on_warning=warnings.append,
                )
                keys = [i.compare_key for i in LocalStorage(str(root)).scan(opts)]
                outcomes.append((keys, warnings))
        finally:
            root.chmod(0o755)
        assert outcomes[0] == outcomes[1]
        assert outcomes[0][0] == [""]
        assert len(outcomes[0][1]) == 1 and "is not readable" in outcomes[0][1][0]


class TestScanPagesAreScandirAligned:
    """``scan_pages`` hands off one page per directory file-run (one ``os.scandir``)."""

    def test_pages_fall_on_directory_boundaries(self, tmp_path: Path) -> None:
        # root=[a.txt, sub/, z.txt], sub=[b.txt]. Byte order interleaves sub's
        # page between root's two file-runs, so the pages are [a.txt], [sub/b.txt],
        # [z.txt] - not one arbitrary fixed-size batch.
        _make_tree(tmp_path, "a.txt", "z.txt", "sub/b.txt")
        pages = [
            [info.compare_key for info in page]
            for page in LocalStorage(str(tmp_path)).scan_pages(LocalScanOptions(recursive=True))
        ]
        assert pages == [["a.txt"], ["sub/b.txt"], ["z.txt"]]

    def test_files_of_one_directory_share_a_page(self, tmp_path: Path) -> None:
        # No sub-directory splits them, so a directory's files come out as one page.
        _make_tree(tmp_path, "a.txt", "b.txt", "c.txt")
        pages = [
            [info.compare_key for info in page]
            for page in LocalStorage(str(tmp_path)).scan_pages(LocalScanOptions(recursive=True))
        ]
        assert pages == [["a.txt", "b.txt", "c.txt"]]

    def test_non_recursive_level_is_one_page(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt", "sub/b.txt")
        pages = list(LocalStorage(str(tmp_path)).scan_pages(LocalScanOptions(recursive=False)))
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

    def test_stats_once_and_reuses_the_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One os.stat, reused for size/mtime, the special-file check, and
        # stat_result - no re-stat, so no TOCTOU window and no snapshot mismatch
        # (os.path.islink is an lstat, not counted here).
        target = tmp_path / "a.txt"
        target.write_bytes(b"12345")
        storage = LocalStorage(str(target))  # abspath computed before we count
        real_stat = os.stat
        calls = 0

        def counting_stat(*args: object, **kwargs: object) -> os.stat_result:
            nonlocal calls
            calls += 1
            return real_stat(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "stat", counting_stat)
        info = storage.get_fileinfo()
        assert info is not None
        assert calls == 1  # a single content stat, reused
        assert info.stat_result is not None  # always populated (the reused snapshot)
        assert info.size == 5

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

    def test_path_through_a_file_returns_none(self, tmp_path: Path) -> None:
        # A path whose parent component is a regular file raises ENOTDIR from stat,
        # but the entry is just as definitively absent as ENOENT (os.path.exists is
        # False), so get_fileinfo returns None rather than raising a transport error.
        target = tmp_path / "file"
        target.write_bytes(b"x")
        warnings: list[str] = []
        info = LocalStorage(str(target / "child")).get_fileinfo(on_warning=warnings.append)
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

    @skip_if_chmod_is_inert
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
        # get_fileinfo keeps its transferable-entry contract. Complete enumeration
        # is scan-only and does not turn a no-follow point query into lstat.
        info = LocalStorage(
            str(link), follow_symlinks=False, enumerate_all_entries=True
        ).get_fileinfo(on_warning=warnings.append)
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
    def test_scan_rejects_a_foreign_scan_options(self, tmp_path: Path) -> None:
        # Mirror of the S3Storage guard: a bare ScanOptions is rejected rather
        # than silently walking with local defaults.
        with pytest.raises(TypeError, match="LocalScanOptions"):
            list(LocalStorage(str(tmp_path)).scan(ScanOptions(recursive=True)))

    def test_recursive_scan_streams_the_walk(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a/inner.txt", "a.txt")
        infos = list(LocalStorage(tmp_path).scan(LocalScanOptions(recursive=True)))
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
        options = LocalScanOptions(recursive=True, filter=lambda info: info.key.endswith(".txt"))
        keys = [info.key for info in LocalStorage(tmp_path).scan(options)]
        assert keys == [str(tmp_path).replace(os.sep, "/") + "/a.txt"]

    def test_default_scan_options_seeds_constructor_config(self) -> None:
        storage = LocalStorage(
            ".",
            follow_symlinks=False,
            detect_symlink_loops=True,
            enumerate_all_entries=True,
        )
        opts = storage.default_scan_options()
        assert opts.follow_symlinks is False
        assert opts.detect_symlink_loops is True
        assert opts.enumerate_all_entries is True

    def test_follow_symlinks_config_reaches_an_arg_less_scan(self, tmp_path: Path) -> None:
        # The walk knob configured on the storage flows into an arg-less scan()
        # (via default_scan_options), so a symlink is skipped without a per-call arg.
        _make_tree(tmp_path, "plain.txt")
        (tmp_path / "link.txt").symlink_to(tmp_path / "plain.txt")
        storage = LocalStorage(tmp_path, follow_symlinks=False)
        keys = sorted(info.compare_key for info in storage.scan())
        assert keys == ["plain.txt"]  # link.txt skipped, no options passed


class TestWalkerSharing:
    """One ``LocalFileGenerator`` may be shared across several ``LocalStorage``
    instances: the walker is stateless, and the producing storage is threaded
    per-walk via ``LocalScanOptions.storage`` (not a back-reference on the walker),
    so each scan stamps its OWN backend onto ``FileInfo.storage``."""

    def test_shared_walker_stamps_the_right_backend_per_storage(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "x.txt").write_bytes(b"1")
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "y.txt").write_bytes(b"2")
        walker = LocalFileGenerator()
        sa = LocalStorage(str(tmp_path / "a"), walker=walker)
        sb = LocalStorage(str(tmp_path / "b"), walker=walker)
        assert sa.walker is sb.walker is walker  # genuinely one shared walker
        # Recursive (the walker's own filter path): each scan stamps its own storage.
        a_infos = list(sa.scan(LocalScanOptions(recursive=True)))
        b_infos = list(sb.scan(LocalScanOptions(recursive=True)))
        assert a_infos and all(i.storage is sa for i in a_infos)
        assert b_infos and all(i.storage is sb for i in b_infos)
        # Non-recursive path too.
        assert all(i.storage is sa for i in sa.scan())
        assert all(i.storage is sb for i in sb.scan())

    def test_shared_walker_filter_sees_the_right_backend(self, tmp_path: Path) -> None:
        # storage is stamped BEFORE the visibility filter, so a predicate reaches
        # the correct backend even when the walker is shared.
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "x.txt").write_bytes(b"1")
        walker = LocalFileGenerator()
        sa = LocalStorage(str(tmp_path / "a"), walker=walker)
        LocalStorage(str(tmp_path), walker=walker)  # a later storage sharing the walker
        seen: list[object] = []

        def _spy(info: FileInfo) -> bool:
            seen.append(info.storage)
            return True

        list(sa.scan(LocalScanOptions(recursive=True, filter=_spy)))
        assert seen and all(s is sa for s in seen)


class TestStatResultAndSymlink:
    """Every listed ``LocalFileInfo`` carries its followed ``stat_result`` and
    ``is_symlink`` flag - no opt-in, and the walk keeps its ``dir_fd`` fast path
    (a ``stat_result`` is a plain value, not an fd-relative ``os.DirEntry``)."""

    def test_walk_populates_stat_result_and_flag_on_every_file(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt", "sub/b.txt")
        infos = list(LocalStorage(str(tmp_path)).walk_local())
        assert {info.compare_key for info in infos} == {"a.txt", "sub/b.txt"}
        for info in infos:
            assert info.stat_result is not None
            # the stat describes the same file the info does
            assert info.stat_result.st_size == info.size
            assert info.is_symlink is False

    def test_symlink_flag_true_with_followed_stat_and_link_key(self, tmp_path: Path) -> None:
        # follow_symlinks=True (default): a link to a file surfaces under its link
        # name (key not resolved to the target) with is_symlink=True, while its
        # stat_result describes the target it was followed to.
        _make_tree(tmp_path, "real.txt")  # 3 bytes
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
        by_key = {info.compare_key: info for info in LocalStorage(str(tmp_path)).walk_local()}
        assert by_key["real.txt"].is_symlink is False
        link = by_key["link.txt"]
        assert link.is_symlink is True
        assert link.key.endswith("/link.txt")  # kept the link name, not real.txt
        assert link.stat_result is not None
        assert link.stat_result.st_size == 3  # the followed target's size
        assert link.size == 3

    def test_filter_reads_stat_result_without_a_fresh_syscall(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "keep.txt", "sub/inner.txt")
        seen: list[str] = []

        def use_stat(info: FileInfo) -> bool:
            assert isinstance(info, LocalFileInfo)
            assert info.stat_result is not None
            seen.append(f"{info.compare_key}:{info.stat_result.st_size}")
            return True

        options = LocalScanOptions(recursive=True, filter=use_stat)
        infos = list(LocalStorage(str(tmp_path)).scan(options))
        assert len(infos) == 2
        assert sorted(seen) == ["keep.txt:3", "sub/inner.txt:3"]

    def test_non_recursive_populates_files_and_directories(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt", "sub/inner.txt")
        by_key = {
            info.compare_key: info for info in LocalStorage(str(tmp_path)).scan(LocalScanOptions())
        }
        assert isinstance(by_key["a.txt"], LocalFileInfo)
        assert by_key["a.txt"].stat_result is not None and by_key["a.txt"].is_symlink is False
        # a directory entry from the one-level listing carries the stat too
        assert by_key["sub/"].kind is FileKind.DIRECTORY
        assert isinstance(by_key["sub/"], LocalFileInfo)
        assert by_key["sub/"].stat_result is not None

    def test_single_path_get_fileinfo_populates_both(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "only.txt")
        info = LocalStorage(str(tmp_path / "only.txt")).get_fileinfo()
        assert isinstance(info, LocalFileInfo)
        assert info.stat_result is not None and info.stat_result.st_size == 3
        assert info.is_symlink is False

    def test_entry_stat_result_override_makes_the_walk_lstat_based(self, tmp_path: Path) -> None:
        # entry_stat_result is the walk's single stat: one override re-points the
        # whole walk at lstat, so symlinks surface as their own entries (not
        # followed) and a symlinked directory is not descended - a backup walk.
        _make_tree(tmp_path, "reg.txt", "realdir/inner.txt")
        (tmp_path / "linkfile").symlink_to(tmp_path / "reg.txt")
        # target_is_directory: Windows needs a directory-type symlink (no-op on POSIX).
        (tmp_path / "linkdir").symlink_to(tmp_path / "realdir", target_is_directory=True)

        class LstatWalker(LocalFileGenerator):
            def entry_stat_result(self, entry: os.DirEntry[str]) -> os.stat_result | None:
                try:
                    return entry.stat(follow_symlinks=False)  # lstat, not followed
                except OSError:
                    return None

        by_key = {
            info.compare_key: info
            for info in LocalStorage(str(tmp_path), walker=LstatWalker()).walk_local()
        }
        # the symlinked directory is its own entry, not descended into
        assert "linkdir" in by_key and "linkdir/inner.txt" not in by_key
        assert by_key["linkdir"].is_symlink is True
        # the symlink-to-file is its own lstat entry, not followed to reg.txt
        link = by_key["linkfile"]
        assert link.is_symlink is True
        assert link.stat_result is not None
        assert link.stat_result.st_size == os.lstat(tmp_path / "linkfile").st_size
        # real file / real directory are unaffected (still descended / followed)
        assert by_key["reg.txt"].is_symlink is False
        assert "realdir/inner.txt" in by_key


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
