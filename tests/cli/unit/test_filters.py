"""Unit tests for boto3_s3_cli.filters (--exclude/--include compilation).

The CLI layer owns OS-dependent filter parity: aws-cli matches with
``fnmatch.fnmatch`` (normcases both sides), so the filter is case-insensitive
on Windows and case-sensitive on POSIX. The folding is gated on ``os.name``;
these tests simulate each OS.
"""

from __future__ import annotations

import pytest

from boto3_s3 import FileInfo, GlobPattern
from boto3_s3.types import FileFilter
from boto3_s3_cli import filters

_PATTERNS = [GlobPattern.exclude("*"), GlobPattern.include("*.TXT")]


def _keeps(file_filter: FileFilter, compare_key: str, full_key: str | None = None) -> bool:
    # A relative pattern matches FileInfo.compare_key; an absolute one matches the
    # full FileInfo.key (defaults to compare_key for the relative-only tests).
    key = full_key if full_key is not None else compare_key
    return file_filter(FileInfo(key=key, compare_key=compare_key))


class TestCaseFolding:
    def test_posix_filter_is_case_sensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(filters.os, "name", "posix")
        keep = filters.compile_filter(_PATTERNS)
        assert keep is not None
        assert _keeps(keep, "a.TXT") is True
        assert _keeps(keep, "a.txt") is False  # byte-exact on POSIX

    def test_windows_filter_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws normcases both sides on Windows, so `*.TXT` matches `a.txt`.
        monkeypatch.setattr(filters.os, "name", "nt")
        keep = filters.compile_filter(_PATTERNS)
        assert keep is not None
        assert _keeps(keep, "a.txt") is True
        assert _keeps(keep, "a.TXT") is True
        assert _keeps(keep, "a.log") is False


class TestAnchoredFilter:
    """An absolute ``--exclude`` / ``--include`` anchors against ``info.key`` (the
    full path), so the one filter prunes ``sync``'s two sides per-side."""

    def test_absolute_pattern_matches_full_key_not_compare_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(filters.os, "name", "posix")
        keep = filters.compile_filter([GlobPattern.exclude("/data/src/keep/*")])
        assert keep is not None
        # local source: full key under the anchored root -> excluded
        assert _keeps(keep, "keep/a", "/data/src/keep/a") is False
        # s3 destination: same compare_key, anchorless key -> visible, so a
        # source-anchored --exclude does not protect the dest from --delete
        assert _keeps(keep, "keep/a", "dst/keep/a") is True

    def test_windows_absolute_pattern_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(filters.os, "name", "nt")
        keep = filters.compile_filter([GlobPattern.exclude("/Data/Src/Keep/*")])
        assert keep is not None
        assert _keeps(keep, "keep/a", "/data/src/keep/a") is False  # case-folded -> excluded
