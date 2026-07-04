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
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import pytest

from boto3_s3.exceptions import NotFoundError
from boto3_s3.localstorage import LocalStorage, LoopDetector, to_native_path
from boto3_s3.types import FileKind, LocalFileInfo, ScanOptions

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


class TestWalkIsOverridable:
    """The walk is a pipeline of protected methods a subclass can replace.

    The extension point behind, e.g., resolving Cygwin ``!<symlink>`` files on a
    native-Python Windows build - the engine dispatches through ``self``.
    """

    def test_should_ignore_override_is_honored_through_scan(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "keep.txt", "skip.tmp")

        class _NoTmp(LocalStorage):
            def _should_ignore(
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
                return super()._should_ignore(
                    entry, full, dir_fd, follow_symlinks=follow_symlinks, notify=notify
                )

        storage = _NoTmp(str(tmp_path))
        keys = [info.compare_key for info in storage.scan(ScanOptions(recursive=True))]
        assert keys == ["keep.txt"]  # the override pruned skip.tmp

    def test_stat_info_override_can_rewrite_entries(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt")

        class _Tagged(LocalStorage):
            def _stat_info(
                self, entry: os.DirEntry[str], full: str, notify: Callable[[str], None]
            ) -> LocalFileInfo | None:
                info = super()._stat_info(entry, full, notify)
                if info is not None:
                    info.size = 999
                return info

        infos = list(_Tagged(str(tmp_path)).walk_local())
        assert [(info.compare_key, info.size) for info in infos] == [("a.txt", 999)]


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
