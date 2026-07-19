"""Unit tests for boto3_s3_cli.filters (--exclude/--include compilation).

The CLI layer owns aws-cli's exact filter semantics: every pattern is joined
onto both sides' bases (aws's ``rootdir``) and fnmatched against the entry's
full path, with the globsieve compare_key engine used only when provably
equivalent (``_needs_joined``). It also owns OS-dependent parity: aws-cli
matches with ``fnmatch.fnmatch`` (normcases both sides), so the filter is
case-insensitive on Windows and case-sensitive on POSIX; the folding is gated
on ``os.name``, which these tests simulate. Filesystem-shaped paths are
derived through the host ``os.path`` so the suite passes on both OS families.
"""

from __future__ import annotations

import os

import pytest

from boto3_s3 import FileInfo, GlobPattern, LocalStorage, S3Storage
from boto3_s3.types import FileFilter
from boto3_s3_cli import filters

_PATTERNS = [GlobPattern.exclude("*"), GlobPattern.include("*.TXT")]


def _compile(patterns: list[GlobPattern]) -> FileFilter:
    """Compile against plain, non-nested bases (the equivalence fast path)."""
    keep = filters.compile_filter(
        patterns, src=LocalStorage("/src"), dest=S3Storage("s3://bucket"), dir_op=True
    )
    assert keep is not None
    return keep


def _keeps(file_filter: FileFilter, compare_key: str, full_key: str | None = None) -> bool:
    # A hand-built FileInfo without a stamped storage: the joined engine then
    # matches the bare key, the delegated engine the compare_key.
    key = full_key if full_key is not None else compare_key
    return file_filter(FileInfo(key=key, compare_key=compare_key))


class TestCaseFolding:
    def test_posix_filter_is_case_sensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(filters.os, "name", "posix")
        keep = _compile(_PATTERNS)
        assert _keeps(keep, "a.TXT") is True
        assert _keeps(keep, "a.txt") is False  # byte-exact on POSIX

    def test_windows_filter_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws normcases both sides on Windows, so `*.TXT` matches `a.txt`.
        monkeypatch.setattr(filters.os, "name", "nt")
        keep = _compile(_PATTERNS)
        assert _keeps(keep, "a.txt") is True
        assert _keeps(keep, "a.TXT") is True
        assert _keeps(keep, "a.log") is False


class TestAbsolutePatterns:
    """An absolute pattern replaces the base in the join (``os.path.join``), so
    it matches full paths - and never an s3 entry, whose ``bucket/key`` path
    carries no anchor. That is what lets a source-anchored pattern prune only
    ``sync``'s local side."""

    def test_absolute_pattern_matches_full_key_not_compare_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(filters.os, "name", "posix")
        base = os.path.abspath("/data/src")
        dest = S3Storage("s3://dst")
        keep = filters.compile_filter(
            [GlobPattern.exclude(os.path.join(base, "keep", "*"))],
            src=LocalStorage("/data/src"),
            dest=dest,
            dir_op=True,
        )
        assert keep is not None
        folded = base.replace(os.sep, "/")
        # local source: full path under the anchored base -> excluded
        assert keep(FileInfo(key=f"{folded}/keep/a", compare_key="keep/a")) is False
        # s3 destination: same compare_key, anchorless bucket/key path ->
        # visible, so a source-anchored --exclude does not protect the dest
        # from --delete
        assert keep(FileInfo(key="keep/a", compare_key="keep/a", storage=dest)) is True

    def test_windows_absolute_pattern_is_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(filters.os, "name", "nt")
        base = os.path.abspath("/Data/Src")
        keep = filters.compile_filter(
            [GlobPattern.exclude(os.path.join(base, "Keep", "*"))],
            src=LocalStorage("/Data/Src"),
            dest=S3Storage("s3://dst"),
            dir_op=True,
        )
        assert keep is not None
        folded = base.replace(os.sep, "/").lower()
        assert _keeps(keep, "keep/a", f"{folded}/keep/a") is False  # case-folded -> excluded


class TestJoinedParity:
    """The two aws behaviors that need the joined engine (both verified against
    aws 2.36.1 in the task #184 probes)."""

    def test_glob_chars_in_base_defeat_exclude(self) -> None:
        # aws joins 'bucket/prefix[1]/' + '*' and fnmatches the full path; the
        # '[1]' is a character class there, so the joined pattern cannot match
        # the literal 'prefix[1]/' entries and the exclude never fires: the rm
        # deletes everything despite --exclude '*'.
        target = S3Storage("s3://bucket/prefix[1]/")
        keep = filters.compile_filter(
            [GlobPattern.exclude("*")], src=target, dest=target, dir_op=True
        )
        assert keep is not None
        info = FileInfo(key="prefix[1]/a.txt", compare_key="a.txt", storage=target)
        assert keep(info) is True  # included = still deleted, like aws

    def test_plain_base_exclude_still_excludes(self) -> None:
        target = S3Storage("s3://bucket/plain/")
        keep = filters.compile_filter(
            [GlobPattern.exclude("*")], src=target, dest=target, dir_op=True
        )
        assert keep is not None
        info = FileInfo(key="plain/a.txt", compare_key="a.txt", storage=target)
        assert keep(info) is False

    def test_nested_s3_bases_cross_apply(self) -> None:
        # cp s3://bucket/data s3://bucket/data/backup --exclude 'x*': the
        # dest-joined 'bucket/data/backup/x*' matches the *source* entry
        # data/backup/x1.txt, so aws excludes it even though its compare_key
        # 'backup/x1.txt' does not match 'x*'.
        src = S3Storage("s3://bucket/data")
        dest = S3Storage("s3://bucket/data/backup")
        keep = filters.compile_filter([GlobPattern.exclude("x*")], src=src, dest=dest, dir_op=True)
        assert keep is not None
        crossed = FileInfo(key="data/backup/x1.txt", compare_key="backup/x1.txt", storage=src)
        plain = FileInfo(key="data/y.txt", compare_key="y.txt", storage=src)
        assert keep(crossed) is False
        assert keep(plain) is True


class TestNeedsJoined:
    """The equivalence proof conditions (the delegation gate)."""

    _RELATIVE = (GlobPattern.exclude("*.log"),)

    def test_plain_bases_delegate(self) -> None:
        assert (
            filters._needs_joined(
                self._RELATIVE, src_base="/src", dest_base="bucket/data", both_s3=False
            )
            is False
        )

    def test_equal_s3_bases_delegate(self) -> None:
        # rm: dest = src; the two joined forms coincide.
        assert (
            filters._needs_joined(
                self._RELATIVE, src_base="bucket/data", dest_base="bucket/data", both_s3=True
            )
            is False
        )

    @pytest.mark.parametrize("base", ["bucket/pre[1]", "bucket/pre*", "bucket/pre?"])
    def test_glob_metacharacter_in_a_base(self, base: str) -> None:
        assert (
            filters._needs_joined(
                self._RELATIVE, src_base=base, dest_base="bucket/plain", both_s3=True
            )
            is True
        )

    def test_absolute_pattern(self) -> None:
        assert (
            filters._needs_joined(
                [GlobPattern.exclude("/data/*")], src_base="/src", dest_base="b/d", both_s3=False
            )
            is True
        )

    def test_nested_s3_bases(self) -> None:
        assert (
            filters._needs_joined(
                self._RELATIVE, src_base="b/data", dest_base="b/data/backup", both_s3=True
            )
            is True
        )

    def test_sibling_s3_bases_delegate(self) -> None:
        # 'b/data' vs 'b/data2': not nested once '/'-terminated.
        assert (
            filters._needs_joined(
                self._RELATIVE, src_base="b/data", dest_base="b/data2", both_s3=True
            )
            is False
        )


class TestDelegationEquivalence:
    """On safe inputs the delegated engine must decide exactly like the joined
    reference engine - the proof ``_needs_joined`` relies on, checked directly."""

    def test_fast_and_joined_agree_on_safe_inputs(self) -> None:
        patterns = [
            GlobPattern.exclude("*"),
            GlobPattern.include("*.txt"),
            GlobPattern.exclude("logs/*"),
            GlobPattern.include("a?c"),
        ]
        src = S3Storage("s3://bucket/data/")
        dest = LocalStorage("/dest")
        src_base = filters._storage_base(src, True)
        dest_base = filters._storage_base(dest, True)
        assert (
            filters._needs_joined(patterns, src_base=src_base, dest_base=dest_base, both_s3=False)
            is False
        )
        fast = filters.compile_filter(patterns, src=src, dest=dest, dir_op=True)
        assert fast is not None
        joined = filters._JoinedFilter(patterns, (src_base, dest_base), fold=os.name == "nt")
        dest_folded = dest_base.replace(os.sep, "/")
        compare_keys = ["a.txt", "b.log", "logs/x.txt", "abc", "aXc", "nested/deep/f.txt"]
        for ck in compare_keys:
            s3_info = FileInfo(key=f"data/{ck}", compare_key=ck, storage=src)
            local_info = FileInfo(key=f"{dest_folded}/{ck}", compare_key=ck, storage=dest)
            assert fast(s3_info) is joined(s3_info), ck
            assert fast(local_info) is joined(local_info), ck


class TestStorageBase:
    """The rootdir derivation (aws's _get_s3_root / _get_local_root) off the
    built storages."""

    def test_s3_dir_op_keeps_the_key(self) -> None:
        assert filters._storage_base(S3Storage("s3://b/pre"), True) == "b/pre"
        assert filters._storage_base(S3Storage("s3://b/pre/"), True) == "b/pre/"

    def test_s3_single_file_uses_the_parent(self) -> None:
        assert filters._storage_base(S3Storage("s3://b/dir/f.txt"), False) == "b/dir"
        assert filters._storage_base(S3Storage("s3://b/f.txt"), False) == "b/"

    def test_s3_bucket_only(self) -> None:
        assert filters._storage_base(S3Storage("s3://b"), True) == "b/"

    def test_local_dir_op_absolutizes(self) -> None:
        assert filters._storage_base(LocalStorage("sub"), True) == os.path.abspath("sub")

    def test_local_single_file_uses_the_parent(self) -> None:
        target = os.path.join("sub", "f.txt")
        assert filters._storage_base(LocalStorage(target), False) == os.path.abspath("sub")
