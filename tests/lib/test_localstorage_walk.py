"""``boto3_s3.localstorage``: the aws-cli-order walk and the Storage surface.

Pins aws-cli ``FileGenerator.list_files`` parity for the recursive ``walk_local``:
the byte-order sort (``foo.txt`` before ``foo/bar`` - the appended-separator
trick), depth-first interleaving, the warn-and-skip battery with aws-cli wording,
the silent symlink skip, and the invalid-timestamp epoch fallback. The single-path
point op (``LocalStorage.get_fileinfo``) is covered separately, including the
deliberate absence of a directory check (aws fails later at open - rc 1
``[Errno 21]``) and the existence-check contract (absent -> ``None``, no warning).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from boto3_s3.exceptions import NotFoundError
from boto3_s3.localstorage import LocalStorage, to_native_path, walk_local
from boto3_s3.types import FileKind, ScanOptions

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def _keys(tmp_path: Path, **kwargs: object) -> list[str]:
    root = str(tmp_path)
    prefix = root.replace(os.sep, "/") + "/"
    out: list[str] = []
    for info in walk_local(root, **kwargs):  # type: ignore[arg-type]
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
        infos = list(walk_local(str(tmp_path), on_warning=warnings.append))
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

    def test_delete(self, tmp_path: Path) -> None:
        _make_tree(tmp_path, "a.txt")
        storage = LocalStorage(tmp_path)
        storage.delete("a.txt")
        assert not (tmp_path / "a.txt").exists()
        with pytest.raises(NotFoundError):
            storage.delete("a.txt")


def test_to_native_path_round_trips() -> None:
    assert to_native_path("a/b/c.txt") == os.sep.join(["a", "b", "c.txt"])
