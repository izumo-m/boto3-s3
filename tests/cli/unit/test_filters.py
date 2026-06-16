"""Unit tests for boto3_s3_cli.filters (--exclude/--include compilation).

The CLI layer owns OS-dependent filter parity: aws-cli matches with
``fnmatch.fnmatch`` (normcases both sides), so the filter is case-insensitive
on Windows and case-sensitive on POSIX. The folding is gated on ``os.name``;
these tests simulate each OS.
"""

from __future__ import annotations

import pytest

from boto3_s3 import GlobPattern
from boto3_s3_cli import filters

_PATTERNS = [GlobPattern.exclude("*"), GlobPattern.include("*.TXT")]


class TestCaseFolding:
    def test_posix_filter_is_case_sensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(filters.os, "name", "posix")
        matcher = filters.compile_for_root(_PATTERNS, root="")
        assert matcher is not None
        assert matcher.included("a.TXT") is True
        assert matcher.included("a.txt") is False  # byte-exact on POSIX

    def test_windows_filter_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws normcases both sides on Windows, so `*.TXT` matches `a.txt`.
        monkeypatch.setattr(filters.os, "name", "nt")
        matcher = filters.compile_for_root(_PATTERNS, root="")
        assert matcher is not None
        assert matcher.included("a.txt") is True
        assert matcher.included("a.TXT") is True
        assert matcher.included("a.log") is False
