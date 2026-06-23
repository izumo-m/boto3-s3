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


def _keeps(file_filter: FileFilter, compare_key: str) -> bool:
    # The CLI filter matches FileInfo.compare_key (the stamped root-relative key).
    return file_filter(FileInfo(key=compare_key, compare_key=compare_key))


class TestCaseFolding:
    def test_posix_filter_is_case_sensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(filters.os, "name", "posix")
        keep = filters.compile_for_root(_PATTERNS, root="")
        assert keep is not None
        assert _keeps(keep, "a.TXT") is True
        assert _keeps(keep, "a.txt") is False  # byte-exact on POSIX

    def test_windows_filter_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws normcases both sides on Windows, so `*.TXT` matches `a.txt`.
        monkeypatch.setattr(filters.os, "name", "nt")
        keep = filters.compile_for_root(_PATTERNS, root="")
        assert keep is not None
        assert _keeps(keep, "a.txt") is True
        assert _keeps(keep, "a.TXT") is True
        assert _keeps(keep, "a.log") is False
