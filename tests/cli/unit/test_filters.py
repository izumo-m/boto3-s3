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
import sys
from typing import TYPE_CHECKING

import pytest

from boto3_s3 import FileInfo, GlobPattern, LocalStorage, S3Storage
from boto3_s3.types import FileFilter
from boto3_s3_cli import filters

if TYPE_CHECKING:
    from pathlib import Path

_PATTERNS = [GlobPattern.exclude("*"), GlobPattern.include("*.TXT")]


class _HostOs:
    """The real ``os`` module with ``name`` overridden."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attribute: str) -> object:
        return getattr(os, attribute)


def _host(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make the module under test see ``os.name == name``, and nothing else.

    The override goes on the module's own ``os`` reference rather than on the
    shared ``os`` module: pytest builds paths while it reports a failure, and
    with the process-wide ``os.name`` saying ``nt`` on a POSIX host that fails
    inside pytest instead of showing the assertion.
    """
    monkeypatch.setattr(filters, "os", _HostOs(name))


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
        _host(monkeypatch, "posix")
        keep = _compile(_PATTERNS)
        assert _keeps(keep, "a.TXT") is True
        assert _keeps(keep, "a.txt") is False  # byte-exact on POSIX

    def test_windows_filter_is_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws normcases both sides on Windows, so `*.TXT` matches `a.txt`.
        _host(monkeypatch, "nt")
        keep = _compile(_PATTERNS)
        assert _keeps(keep, "a.txt") is True
        assert _keeps(keep, "a.TXT") is True
        assert _keeps(keep, "a.log") is False


_BS = chr(92)  # a backslash, spelled so no escape rewriting can touch it


class TestWindowsSeparatorFolding:
    """On Windows a backslash and a slash are one separator, in keys as in patterns.

    aws-cli's ``fnmatch.fnmatch`` normcases the path and the pattern, and
    ``ntpath.normcase`` turns every ``/`` into a backslash. An S3 key can hold
    a literal backslash, so ``--exclude a/b`` excludes the key ``a<bs>b`` there
    - measured against aws.exe 2.36.40 by real deletion (``rm --recursive``
    keeps it), on both engines. Lower-casing alone left such keys unmatched:
    ``rm --exclude`` deleted what aws-cli keeps, and ``--exclude '*' --include
    'dir/*.txt'`` skipped what aws-cli deletes.
    """

    @pytest.fixture(autouse=True)
    def _windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _host(monkeypatch, "nt")

    def _rm(self, patterns: list[GlobPattern]) -> FileFilter:
        target = S3Storage("s3://bucket")
        keep = filters.compile_filter(patterns, src=target, dest=target, dir_op=True)
        assert keep is not None
        return keep

    @pytest.mark.parametrize("pattern", ["a/b", "a/*", "A/B", f"a{_BS}b", f"a{_BS}*"])
    @pytest.mark.parametrize("forced_joined", [False, True])
    def test_a_backslash_in_a_key_matches_a_separator(
        self, pattern: str, forced_joined: bool
    ) -> None:
        # An absolute pattern ahead of the rule sends the whole list to the
        # joined engine; without it the same list is delegated.
        lead = [GlobPattern.exclude("/zzz-nowhere/*")] if forced_joined else []
        keep = self._rm([*lead, GlobPattern.exclude(pattern)])
        target = S3Storage("s3://bucket")
        for key in (f"a{_BS}b", "a/b", f"A{_BS}B"):
            assert keep(FileInfo(key=key, compare_key=key, storage=target)) is False, key
        assert keep(FileInfo(key="ab", compare_key="ab", storage=target)) is True

    def test_include_reaches_a_backslash_key_too(self) -> None:
        keep = self._rm([GlobPattern.exclude("*"), GlobPattern.include("dir/*.txt")])
        target = S3Storage("s3://bucket")
        key = f"dir{_BS}x.txt"
        assert keep(FileInfo(key=key, compare_key=key, storage=target)) is True

    def test_posix_keeps_the_backslash_a_literal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _host(monkeypatch, "posix")
        keep = self._rm([GlobPattern.exclude("a/b")])
        target = S3Storage("s3://bucket")
        key = f"a{_BS}b"
        assert keep(FileInfo(key=key, compare_key=key, storage=target)) is True

    def test_a_class_spanning_the_slash_does_not_match_the_separator(self) -> None:
        # aws-cli matches in the backslash form, where the separator is 0x5C:
        # the range `+-9` holds `/` (0x2F) and not that. The delegated engine
        # works in `/` space and would match, so a `[` goes to the joined one.
        keep = self._rm([GlobPattern.exclude("a[+-9]b")])
        target = S3Storage("s3://bucket")
        assert keep(FileInfo(key="a/b", compare_key="a/b", storage=target)) is True
        assert keep(FileInfo(key="a5b", compare_key="a5b", storage=target)) is False

    def test_a_bracket_needs_the_joined_engine_only_when_folding(self) -> None:
        patterns = [GlobPattern.exclude("a[+-9]b")]
        bases = {"src_base": "bucket/", "dest_base": "bucket/", "both_s3": True, "dir_op": True}
        assert filters._needs_joined(patterns, **bases, fold=True) is True
        assert filters._needs_joined(patterns, **bases, fold=False) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own case mapping")
class TestWindowsCaseMapping:
    """The fold is Windows' locale-invariant lower-casing, not ``str.lower``.

    aws.exe bundles a Python whose ``ntpath.normcase`` calls ``LCMapStringEx``;
    that table is one-to-one, so it differs from Unicode's full case mapping
    for a few hundred code points. Measured against aws.exe 2.36.40: ``--exclude 'k*'``
    does not keep a key that starts with the Kelvin sign, ``--exclude 'i*'``
    not one that starts with U+0130, and ``str.lower`` would have kept both.
    """

    @pytest.mark.parametrize(
        ("text", "folded"),
        [
            ("ABC/Def", f"abc{_BS}def"),
            (chr(0x130) + "x", chr(0x130) + "x"),  # LATIN CAPITAL LETTER I WITH DOT ABOVE
            (chr(0x212A) + "x", chr(0x212A) + "x"),  # KELVIN SIGN
            (chr(0x1E9E) + "x", chr(0x1E9E) + "x"),  # LATIN CAPITAL LETTER SHARP S
            (chr(0x39F) + chr(0x3A3), chr(0x3BF) + chr(0x3C3)),  # no final sigma
            ("", ""),
        ],
    )
    def test_it_folds_like_the_operating_system(self, text: str, folded: str) -> None:
        assert filters._windows_normcase(text) == folded

    def test_the_direct_call_agrees_with_the_stdlib_where_the_stdlib_has_it(self) -> None:
        if sys.version_info < (3, 12):
            pytest.skip("ntpath.normcase lower-cases with str.lower before Python 3.12")
        import ntpath

        for code in (*range(0x20, 0x250), 0x130, 0x212A, 0x1E9E, 0x3A3, 0x10400, 0x1F600):
            text = "x" + chr(code) + "/y"
            assert filters._lcmap_lower(text.replace("/", _BS)) == ntpath.normcase(text), hex(code)


class TestAbsolutePatterns:
    """An absolute pattern replaces the base in the join (``os.path.join``), so
    it matches full paths - and never an s3 entry, whose ``bucket/key`` path
    carries no anchor. That is what lets a source-anchored pattern prune only
    ``sync``'s local side."""

    def test_absolute_pattern_matches_full_key_not_compare_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _host(monkeypatch, "posix")
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
        _host(monkeypatch, "nt")
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
    aws 2.36.40 in the task #184 probes)."""

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

    def test_single_object_empty_component_matches_like_aws(self) -> None:
        # rm s3://b/a//x --exclude '?x' (measured on aws 2.36.40, offline
        # dryrun): aws joins 'b/a/?x' and fnmatches the full path 'b/a//x',
        # where '?' crosses the second '/', so the object is excluded. The
        # basename compare_key 'x' cannot express that - the parent-derived
        # base plus basename reconstructs 'b/a/x' - hence every single-object
        # command routes to the joined engine.
        target = S3Storage("s3://b/a//x")
        keep = filters.compile_filter(
            [GlobPattern.exclude("?x")], src=target, dest=target, dir_op=False
        )
        assert keep is not None
        assert keep(FileInfo(key="a//x", compare_key="x", storage=target)) is False

    @pytest.mark.parametrize(
        ("source", "pattern", "kept"),
        [
            ("d", "d", True),
            ("d", "d/", False),
            ("d/", "d", True),
            ("d/", "d/", False),
            ("d/f", "f", False),
            ("d/f", "f/", True),
        ],
    )
    def test_single_local_source_matches_its_local_format_form(
        self, tmp_path: Path, source: str, pattern: str, kept: bool
    ) -> None:
        # aws fnmatches the joined pattern against the source in its
        # `local_format` form: `<abspath>/` for a directory (written with or
        # without the separator), `<abspath>` for a file. Measured on aws
        # 2.36.40: `cp d s3://b/k --exclude d` still previews and, live, fails
        # on the directory at rc 1, while `--exclude 'd/'` excludes it (rc 0);
        # boto3-s3 used to match the bare absolute path and flipped both.
        (tmp_path / "d").mkdir()
        (tmp_path / "d" / "f").write_bytes(b"x")
        src = LocalStorage(str(tmp_path / source))
        keep = filters.compile_filter(
            [GlobPattern.exclude(pattern)], src=src, dest=S3Storage("s3://b/k"), dir_op=False
        )
        assert keep is not None
        info = src.get_fileinfo()
        assert info is not None
        assert keep(info) is kept

    def test_single_object_plain_key_is_not_excluded(self) -> None:
        # The complement (same aws probe): rm s3://b/a/x --exclude '?x' still
        # deletes - the joined 'b/a/?x' needs two characters after 'b/a/'.
        target = S3Storage("s3://b/a/x")
        keep = filters.compile_filter(
            [GlobPattern.exclude("?x")], src=target, dest=target, dir_op=False
        )
        assert keep is not None
        assert keep(FileInfo(key="a/x", compare_key="x", storage=target)) is True


class TestNeedsJoined:
    """The equivalence proof conditions (the delegation gate)."""

    _RELATIVE = (GlobPattern.exclude("*.log"),)

    def test_plain_bases_delegate(self) -> None:
        assert (
            filters._needs_joined(
                self._RELATIVE, src_base="/src", dest_base="bucket/data", both_s3=False, dir_op=True
            )
            is False
        )

    def test_single_object_command_needs_joined(self) -> None:
        # dir_op False: base + basename compare_key does not always
        # reconstruct the full path (an empty component before the basename
        # collapses in the join), and the listing is one entry anyway.
        assert (
            filters._needs_joined(
                self._RELATIVE,
                src_base="bucket/data",
                dest_base="bucket/data",
                both_s3=True,
                dir_op=False,
            )
            is True
        )

    def test_equal_s3_bases_delegate(self) -> None:
        # rm: dest = src; the two joined forms coincide.
        assert (
            filters._needs_joined(
                self._RELATIVE,
                src_base="bucket/data",
                dest_base="bucket/data",
                both_s3=True,
                dir_op=True,
            )
            is False
        )

    @pytest.mark.parametrize("base", ["bucket/pre[1]", "bucket/pre*", "bucket/pre?"])
    def test_glob_metacharacter_in_a_base(self, base: str) -> None:
        assert (
            filters._needs_joined(
                self._RELATIVE, src_base=base, dest_base="bucket/plain", both_s3=True, dir_op=True
            )
            is True
        )

    def test_absolute_pattern(self) -> None:
        assert (
            filters._needs_joined(
                [GlobPattern.exclude("/data/*")],
                src_base="/src",
                dest_base="b/d",
                both_s3=False,
                dir_op=True,
            )
            is True
        )

    def test_nested_s3_bases(self) -> None:
        assert (
            filters._needs_joined(
                self._RELATIVE,
                src_base="b/data",
                dest_base="b/data/backup",
                both_s3=True,
                dir_op=True,
            )
            is True
        )

    def test_sibling_s3_bases_delegate(self) -> None:
        # 'b/data' vs 'b/data2': not nested once '/'-terminated.
        assert (
            filters._needs_joined(
                self._RELATIVE, src_base="b/data", dest_base="b/data2", both_s3=True, dir_op=True
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
            filters._needs_joined(
                patterns, src_base=src_base, dest_base=dest_base, both_s3=False, dir_op=True
            )
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


class TestDelegationFuzz:
    """Seeded differential mini-fuzz over the delegated fast path.

    Every parameter set ``_needs_joined`` delegates must decide exactly like
    the joined reference engine (the equivalence proof, exercised mechanically
    rather than by hand-picked inputs - the hand-picked divergences in
    ``TestJoinedParity`` were each found only after shipping). A glob-rich
    alphabet with ``/`` in both patterns and keys covers empty components,
    trailing slashes, and character classes; 71k cases of the full-size fuzz
    (three shapes plus the Windows fold) ran divergence-free on 2026-07-22,
    and this bounded seeded slice keeps the property pinned.
    """

    _PATTERN_ALPHABET = ("a", "b", "/", "*", "?", "[", "]", "!", ".", "-")
    _KEY_ALPHABET = ("a", "b", "/", ".", "-")

    def test_fast_path_matches_joined_engine_on_random_inputs(self) -> None:
        import random

        rng = random.Random(20260722)
        src = S3Storage("s3://bucket/data")
        dest = LocalStorage("/destroot")
        src_base = filters._storage_base(src, True)
        dest_base = filters._storage_base(dest, True)
        # A local entry's stamped key is its absolute path with os.sep folded to
        # "/" (LocalFileInfo, localstorage.py), so it is drive-prefixed on
        # Windows, where _storage_base abspath'd "/destroot" to e.g.
        # "C:/destroot". Build the oracle's key off that same base so the joined
        # engine sees the full path the fast path's compare_key implies; a
        # hard-coded "/destroot/..." carries no drive and silently defeats the
        # dest-side join on Windows (the fast path stays right via compare_key).
        dest_key_base = dest_base.replace(os.sep, "/")
        compared = 0
        for _ in range(2000):
            patterns = [
                (GlobPattern.exclude if rng.random() < 0.6 else GlobPattern.include)(
                    "".join(rng.choice(self._PATTERN_ALPHABET) for _ in range(rng.randint(1, 6)))
                )
                for _ in range(rng.randint(1, 3))
            ]
            if filters._needs_joined(
                patterns, src_base=src_base, dest_base=dest_base, both_s3=False, dir_op=True
            ):
                continue
            compared += 1
            fast = filters.compile_filter(patterns, src=src, dest=dest, dir_op=True)
            assert fast is not None
            joined = filters._JoinedFilter(patterns, (src_base, dest_base), fold=os.name == "nt")
            for _ in range(20):
                rel = "".join(rng.choice(self._KEY_ALPHABET) for _ in range(rng.randint(0, 8)))
                s3_info = FileInfo(key=f"data/{rel}", compare_key=rel, storage=src)
                local_info = FileInfo(key=f"{dest_key_base}/{rel}", compare_key=rel, storage=dest)
                for info in (s3_info, local_info):
                    assert fast(info) is joined(info), (
                        f"patterns={[(p.kind.name, p.pattern) for p in patterns]} "
                        f"key={info.key!r} compare={rel!r}"
                    )
        assert compared > 500  # the delegated fast path was actually exercised


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
