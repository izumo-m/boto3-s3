"""Unit tests for :mod:`boto3_s3.globsieve`.

Three layers:

  - **GlobPattern**: the dataclass and its factory shorthands.
  - **Set matchers**: each :class:`SetMatcher` returns the right
    membership answers in isolation.
  - **compile**: the macro-shape detector picks the right
    :class:`Matcher` backend, and the result agrees with a reference
    AWS-CLI-style :class:`Sequential` walk for any pattern list.
"""

from __future__ import annotations

import pytest

from boto3_s3 import globsieve
from boto3_s3.globsieve import (
    AlwaysExclude,
    AlwaysInclude,
    ExcludeOnly,
    GlobPattern,
    IncludeOnly,
    LiteralSet,
    PatternKind,
    PrefixSet,
    Sequential,
    SuffixSet,
    UnionRegex,
)

# ----- GlobPattern ---------------------------------------------------------


class TestGlobPattern:
    def test_factories_produce_expected_kinds(self) -> None:
        assert GlobPattern.include("*.txt") == GlobPattern(PatternKind.INCLUDE, "*.txt")
        assert GlobPattern.exclude("*.log") == GlobPattern(PatternKind.EXCLUDE, "*.log")

    def test_pattern_is_frozen(self) -> None:
        p = GlobPattern.include("*.txt")
        with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
            p.pattern = "*.log"  # type: ignore[misc]

    def test_pattern_equality(self) -> None:
        # Equality is by (kind, pattern); used by compile's
        # catch-all detection.
        assert GlobPattern.exclude("*") == GlobPattern(PatternKind.EXCLUDE, "*")
        assert GlobPattern.exclude("*") != GlobPattern.include("*")

    def test_pattern_kind_enum(self) -> None:
        # Members carry their CLI-token string value.
        assert PatternKind.INCLUDE.value == "include"
        assert PatternKind.EXCLUDE.value == "exclude"


# ----- Set matchers --------------------------------------------------------


class TestLiteralSet:
    def test_membership(self) -> None:
        s = LiteralSet(["foo.txt", "bar.txt"])
        assert s.matches("foo.txt") is True
        assert s.matches("bar.txt") is True
        assert s.matches("baz.txt") is False
        assert s.matches("foo") is False

    def test_empty_set_never_matches(self) -> None:
        s = LiteralSet([])
        assert s.matches("anything") is False


class TestSuffixSet:
    def test_endswith_any(self) -> None:
        s = SuffixSet([".txt", ".csv"])
        assert s.matches("foo.txt") is True
        assert s.matches("a/b/foo.csv") is True
        assert s.matches("foo.log") is False

    def test_empty_suffix_matches_anything(self) -> None:
        # endswith("") is always True; this is the natural reading of
        # ``*`` which compiles to a SuffixSet of [""] - but compile
        # routes a bare "*" through the catch-all pre-pass, so this
        # corner case usually doesn't arise.
        s = SuffixSet([""])
        assert s.matches("anything") is True


class TestPrefixSet:
    def test_startswith_any(self) -> None:
        s = PrefixSet(["logs/", "tmp/"])
        assert s.matches("logs/yesterday") is True
        assert s.matches("tmp/cache") is True
        assert s.matches("data/foo") is False


class TestUnionRegex:
    def test_matches_union_of_fnmatch_translations(self) -> None:
        m = UnionRegex(["**/foo*.tmp", "*.bak"])
        assert m.matches("dir/foo123.tmp") is True
        assert m.matches("save.bak") is True
        assert m.matches("save.txt") is False


# ----- compile: macro-shape detection --------------------------------------


class TestCompileMacroShape:
    def test_empty_returns_always_include(self) -> None:
        m = globsieve.compile([])
        assert isinstance(m, AlwaysInclude)
        assert m.included("anything") is True

    def test_lone_catch_all_exclude_returns_always_exclude(self) -> None:
        m = globsieve.compile([GlobPattern.exclude("*")])
        assert isinstance(m, AlwaysExclude)
        assert m.included("anything") is False

    def test_deny_then_includes_returns_include_only(self) -> None:
        m = globsieve.compile(
            [
                GlobPattern.exclude("*"),
                GlobPattern.include("*.txt"),
                GlobPattern.include("*.csv"),
            ]
        )
        assert isinstance(m, IncludeOnly)
        # Underlying set should be specialized to SuffixSet (all *.X).
        assert isinstance(m.set_matcher, SuffixSet)
        assert m.included("foo.txt") is True
        assert m.included("foo.csv") is True
        assert m.included("foo.log") is False

    def test_allow_then_excludes_returns_exclude_only(self) -> None:
        m = globsieve.compile(
            [
                GlobPattern.include("*"),
                GlobPattern.exclude("*.tmp"),
                GlobPattern.exclude("*.bak"),
            ]
        )
        assert isinstance(m, ExcludeOnly)
        assert m.included("foo.txt") is True
        assert m.included("foo.tmp") is False
        assert m.included("foo.bak") is False

    def test_all_excludes_without_catch_all_head_returns_exclude_only(self) -> None:
        m = globsieve.compile(
            [
                GlobPattern.exclude("*.tmp"),
                GlobPattern.exclude("*.bak"),
            ]
        )
        assert isinstance(m, ExcludeOnly)
        assert m.included("a.txt") is True
        assert m.included("a.tmp") is False

    def test_mixed_ordering_returns_sequential(self) -> None:
        # last-match-wins matters here: include keep/* must override
        # the prior exclude *.tmp.
        m = globsieve.compile(
            [
                GlobPattern.exclude("*.tmp"),
                GlobPattern.include("keep/*.tmp"),
            ]
        )
        assert isinstance(m, Sequential)
        assert m.included("foo.tmp") is False
        assert m.included("keep/foo.tmp") is True
        assert m.included("foo.txt") is True


# ----- compile: per-set specialization -------------------------------------


class TestCompileSetSpecialization:
    def test_all_literal_set_picks_literal_set(self) -> None:
        m = globsieve.compile(
            [GlobPattern.exclude("*"), GlobPattern.include("a.txt"), GlobPattern.include("b.txt")]
        )
        assert isinstance(m, IncludeOnly)
        assert isinstance(m.set_matcher, LiteralSet)

    def test_all_suffix_picks_suffix_set(self) -> None:
        m = globsieve.compile(
            [GlobPattern.exclude("*"), GlobPattern.include("*.txt"), GlobPattern.include("*.csv")]
        )
        assert isinstance(m, IncludeOnly)
        assert isinstance(m.set_matcher, SuffixSet)
        assert m.set_matcher.suffixes == (".txt", ".csv")

    def test_all_prefix_picks_prefix_set(self) -> None:
        m = globsieve.compile(
            [GlobPattern.exclude("*"), GlobPattern.include("logs/*"), GlobPattern.include("tmp/*")]
        )
        assert isinstance(m, IncludeOnly)
        assert isinstance(m.set_matcher, PrefixSet)
        assert m.set_matcher.prefixes == ("logs/", "tmp/")

    def test_mixed_shapes_fall_back_to_union_regex(self) -> None:
        m = globsieve.compile(
            [
                GlobPattern.exclude("*"),
                GlobPattern.include("*.txt"),
                GlobPattern.include("logs/*"),
            ]
        )
        assert isinstance(m, IncludeOnly)
        assert isinstance(m.set_matcher, UnionRegex)


# ----- semantic equivalence: specialized vs. naive Sequential --------------


class TestSemanticEquivalence:
    """Specialized matchers must agree with a naive last-match-wins walk.

    The naive walk uses :class:`Sequential` with one
    :class:`UnionRegex` per pattern (forced) - i.e. the same logic as
    the AWS CLI filter loop. We compare its verdict to the specialized
    :func:`compile` result on a battery of keys.
    """

    @staticmethod
    def _naive(patterns: list[GlobPattern]) -> Sequential:
        return Sequential((p.kind, UnionRegex((p.pattern,))) for p in patterns)

    @pytest.mark.parametrize(
        "patterns,keys",
        [
            (
                [GlobPattern.exclude("*"), GlobPattern.include("*.txt")],
                ["a.txt", "a.log", "sub/b.txt", "sub/b.log", ""],
            ),
            (
                [GlobPattern.include("*"), GlobPattern.exclude("*.tmp")],
                ["a.txt", "a.tmp", "tmp/x", "x.tmp"],
            ),
            (
                [
                    GlobPattern.exclude("*.tmp"),
                    GlobPattern.exclude("*.bak"),
                    GlobPattern.include("keep/*"),
                ],
                ["a.tmp", "a.bak", "a.txt", "keep/a.tmp", "keep/a.bak"],
            ),
            (
                [
                    GlobPattern.exclude("*"),
                    GlobPattern.include("a.txt"),
                    GlobPattern.include("b.txt"),
                ],
                ["a.txt", "b.txt", "c.txt"],
            ),
            (
                [GlobPattern.exclude("logs/*"), GlobPattern.exclude("tmp/*")],
                ["logs/x", "tmp/y", "data/z"],
            ),
        ],
    )
    def test_compile_matches_naive(self, patterns: list[GlobPattern], keys: list[str]) -> None:
        compiled = globsieve.compile(patterns)
        naive = self._naive(patterns)
        for key in keys:
            assert compiled.included(key) == naive.included(key), (
                f"divergence on {key!r} with patterns={patterns!r}"
            )


class TestTranslatePatternForRoot:
    """Pin :func:`translate_pattern_for_root` semantics.

    Mirrors aws-cli's filter behaviour
    (aws-cli's ``awscli/customizations/s3/filters.py:_full_path_patterns``)
    but emits a relative-key form so the existing fast-path matchers
    keep working against root-stripped keys.
    """

    def test_relative_pattern_passes_through_unchanged(self) -> None:
        # Plain relative patterns are already in relative-key form;
        # the rootdir prefix gets joined on then stripped, leaving
        # the original.
        assert globsieve.translate_pattern_for_root("*.log", "/abs/data") == "*.log"
        assert globsieve.translate_pattern_for_root("sub/*", "/abs/data") == "sub/*"
        assert globsieve.translate_pattern_for_root("**/*.py", "/abs/data") == "**/*.py"

    def test_absolute_pattern_under_root_is_stripped(self) -> None:
        # ``--exclude /abs/data/secret/*`` against rootdir ``/abs/data``:
        # the rootdir prefix is stripped to leave ``secret/*``.
        assert globsieve.translate_pattern_for_root("/abs/data/secret/*", "/abs/data") == "secret/*"

    def test_absolute_pattern_outside_root_returns_none(self) -> None:
        # ``/foreign/X/*`` cannot match any file under ``/abs/data``;
        # the translator drops the entry.
        assert globsieve.translate_pattern_for_root("/foreign/X/*", "/abs/data") is None

    def test_windows_backslash_pattern_is_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # On Windows (os.sep == "\\") backslash-separated patterns fold to
        # ``/`` so they match the always-``/`` relative-key form. Rootdir
        # ``C:\data`` + pattern ``sub\file.*`` -> relative ``sub/file.*``.
        # Folding is os.sep-gated (aws-cli filters._match_pattern s3/local
        # branches), so simulate Windows here; on POSIX a backslash is a legal
        # key/filename char and is preserved (test_posix_backslash_is_preserved).
        monkeypatch.setattr(globsieve.os, "sep", "\\")
        assert globsieve.translate_pattern_for_root("sub\\file.*", "C:\\data") == "sub/file.*"

    def test_windows_drive_pattern_under_drive_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Drive-rooted pattern ``C:\data\secret\*`` under rootdir ``C:\data``
        # strips to ``secret/*`` (Windows: os.sep == "\\").
        monkeypatch.setattr(globsieve.os, "sep", "\\")
        assert globsieve.translate_pattern_for_root("C:\\data\\secret\\*", "C:\\data") == "secret/*"

    def test_posix_backslash_is_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # On POSIX (os.sep == "/") a literal backslash is NOT folded: aws-cli's
        # s3 branch is ``pattern.replace(os.sep, "/")`` (a no-op on POSIX), and
        # an s3 key may legally contain a backslash, so it must survive for the
        # pattern to keep matching it (the POSIX-s3 parity fix).
        monkeypatch.setattr(globsieve.os, "sep", "/")
        assert globsieve.translate_pattern_for_root("a\\b.txt", "") == "a\\b.txt"
        assert globsieve.translate_pattern_for_root("a\\b.*", "data") == "a\\b.*"

    def test_absolute_pattern_equal_to_root_returns_none(self) -> None:
        # ``--exclude /abs/data`` (no trailing slash) cannot match any
        # key under the rootdir - there is no relative key that maps
        # to the rootdir itself.
        assert globsieve.translate_pattern_for_root("/abs/data", "/abs/data") is None

    def test_absolute_pattern_collapses_to_catchall(self) -> None:
        # ``--exclude /abs/data/*`` strips to ``*`` - the same outcome
        # as a plain ``--exclude *``. After translation it should be
        # eligible for the AlwaysExclude / IncludeOnly fast paths.
        assert globsieve.translate_pattern_for_root("/abs/data/*", "/abs/data") == "*"

    def test_empty_rootdir_passes_through(self) -> None:
        # No rootdir -> no anchoring; pattern passes through unchanged.
        assert globsieve.translate_pattern_for_root("/foo/*", "") == "/foo/*"
        assert globsieve.translate_pattern_for_root("*.log", "") == "*.log"

    def test_trailing_slash_in_rootdir_does_not_break_strip(self) -> None:
        # The translator must tolerate trailing separators on rootdir.
        assert globsieve.translate_pattern_for_root("/abs/data/sub/*", "/abs/data/") == "sub/*"
