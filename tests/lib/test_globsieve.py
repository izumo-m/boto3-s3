"""Unit tests for `boto3_s3.globsieve`.

Three layers:

  - **GlobPattern**: the dataclass and its factory shorthands.
  - **Set matchers**: each `SetMatcher` returns the right
    membership answers in isolation.
  - **compile**: the macro-shape detector picks the right
    `Matcher` backend, and the result agrees with a reference
    AWS-CLI-style `Sequential` walk for any pattern list.
"""

from __future__ import annotations

import ntpath
import posixpath
from dataclasses import FrozenInstanceError

import pytest

from boto3_s3 import FileInfo, globsieve
from boto3_s3.globsieve import (
    AlwaysExclude,
    AlwaysInclude,
    CompositeSet,
    ExcludeOnly,
    GlobFilter,
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
        with pytest.raises(FrozenInstanceError):
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

    def test_empty_union_never_matches(self) -> None:
        # Like the other empty-set matchers (a zero-alternative join would
        # otherwise compile "" and match everything).
        m = UnionRegex([])
        assert m.matches("anything") is False
        assert m.matches("") is False


class TestCompositeSet:
    # CompositeSet takes pre-stripped buckets: literals verbatim, suffixes
    # without the leading ``*``, prefixes without the trailing ``*``, and
    # general patterns as raw fnmatch.
    def test_ors_every_present_shape(self) -> None:
        s = CompositeSet(
            literals=[".lock"],
            suffixes=[".elc"],  # from ``*.elc``
            prefixes=["elpa/"],  # from ``elpa/*``
            general=["**/foo*.bak"],
        )
        assert s.matches(".lock") is True  # literal
        assert s.matches("a/b.elc") is True  # suffix (greedy across /)
        assert s.matches("elpa/x/y.el") is True  # prefix
        assert s.matches("deep/dir/foo1.bak") is True  # general
        assert s.matches("init.el") is False  # none

    def test_works_without_a_general_bucket(self) -> None:
        # The common exclude-list shape: only literal/suffix/prefix, no regex.
        s = CompositeSet(literals=[".x"], suffixes=[".elc"], prefixes=["lib/"], general=[])
        assert s.matches(".x") is True
        assert s.matches("a.elc") is True
        assert s.matches("lib/native.so") is True
        assert s.matches("keep.txt") is False


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

    def test_mixed_shapes_partition_into_composite_set(self) -> None:
        # A suffix (``*.txt``) and a prefix (``logs/*``) together have no
        # single uniform shape, so they fold into a CompositeSet rather than
        # collapsing into one regex.
        m = globsieve.compile(
            [
                GlobPattern.exclude("*"),
                GlobPattern.include("*.txt"),
                GlobPattern.include("logs/*"),
            ]
        )
        assert isinstance(m, IncludeOnly)
        assert isinstance(m.set_matcher, CompositeSet)
        assert m.included("a.txt") is True
        assert m.included("logs/x") is True
        assert m.included("a.log") is False

    def test_only_general_shapes_stay_union_regex(self) -> None:
        # Patterns that are neither literal nor pure prefix/suffix remain a
        # single UnionRegex (one populated bucket, no composite).
        m = globsieve.compile(
            [
                GlobPattern.exclude("*"),
                GlobPattern.include("a*b.txt"),
                GlobPattern.include("c?d/*.log"),
            ]
        )
        assert isinstance(m, IncludeOnly)
        assert isinstance(m.set_matcher, UnionRegex)


# ----- public surface --------------------------------------------------------


class TestPublicSurface:
    def test_all_resolves_and_is_public(self) -> None:
        # The module is a documented submodule-path building block
        # (docs/globsieve.md): __all__ is its public contract.
        for name in globsieve.__all__:
            assert not name.startswith("_"), name
            assert hasattr(globsieve, name), name

    def test_all_matches_the_documented_surface(self) -> None:
        # Pin the exact surface: a symbol added to (or dropped from) the
        # module must be a deliberate __all__ / docs decision.
        assert set(globsieve.__all__) == {
            "AlwaysExclude",
            "AlwaysInclude",
            "Anchored",
            "CompositeSet",
            "ExcludeOnly",
            "GlobFilter",
            "GlobPattern",
            "IncludeOnly",
            "LiteralSet",
            "Matcher",
            "PatternKind",
            "PrefixSet",
            "Sequential",
            "SetMatcher",
            "SuffixSet",
            "UnionRegex",
            "compile",
            "compile_set_matcher",
            "is_anchored",
        }


class TestIsAnchored:
    def test_absolute_pattern_is_anchored(self) -> None:
        assert globsieve.is_anchored("/data/secret/*") is True

    def test_relative_patterns_are_not(self) -> None:
        assert globsieve.is_anchored("data/*") is False
        assert globsieve.is_anchored("*.txt") is False

    def test_windows_single_separator_keeps_join_semantics_after_python_313(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_isabs = ntpath.isabs

        def python_313_isabs(pattern: str) -> bool:
            if pattern.startswith(("/", "\\")) and not pattern.startswith(("//", "\\\\")):
                return False
            return real_isabs(pattern)

        monkeypatch.setattr(globsieve.os, "path", ntpath)
        monkeypatch.setattr(globsieve.os, "sep", "\\")
        monkeypatch.setattr(ntpath, "isabs", python_313_isabs)

        assert globsieve.is_anchored("/data/secret/*") is True
        assert globsieve.is_anchored("\\data\\secret\\*") is True
        assert globsieve.is_anchored("C:/data/secret/*") is True
        assert globsieve.is_anchored("data/secret/*") is False


class TestCompileSetMatcher:
    def test_uniform_shapes_pick_the_dedicated_matcher(self) -> None:
        assert isinstance(globsieve.compile_set_matcher(["a.txt", "b.txt"]), LiteralSet)
        assert isinstance(globsieve.compile_set_matcher(["*.txt"]), SuffixSet)
        assert isinstance(globsieve.compile_set_matcher(["logs/*"]), PrefixSet)
        assert isinstance(globsieve.compile_set_matcher(["a*b"]), UnionRegex)

    def test_mixed_shapes_fold_into_composite(self) -> None:
        sm = globsieve.compile_set_matcher(["*.txt", "logs/*"])
        assert isinstance(sm, CompositeSet)
        assert sm.matches("a.txt") and sm.matches("logs/x")
        assert not sm.matches("a.log")

    def test_empty_list_never_matches(self) -> None:
        sm = globsieve.compile_set_matcher([])
        assert sm.matches("anything") is False
        assert sm.matches("") is False


# ----- semantic equivalence: specialized vs. naive Sequential --------------


class TestSemanticEquivalence:
    """Specialized matchers must agree with a naive last-match-wins walk.

    The naive walk uses `Sequential` with one `UnionRegex` per pattern
    (forced) - i.e. the same logic as the AWS CLI filter loop. We compare its
    verdict to the specialized `compile` result on a battery of keys.
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
            # Mixed-shape all-exclude list (the .emacs.d backup case): a
            # suffix + many ``dir/*`` prefixes + literals -> CompositeSet,
            # which must agree with the naive last-match-wins walk.
            (
                [
                    GlobPattern.exclude("*.elc"),
                    GlobPattern.exclude("elpa/*"),
                    GlobPattern.exclude("eln-cache/*"),
                    GlobPattern.exclude(".cache/*"),
                    GlobPattern.exclude(".persistent-scratch"),
                    GlobPattern.exclude(".lsp-session-v1"),
                ],
                [
                    "init.el",
                    "a/b.elc",
                    "elpa/magit/magit.el",
                    "eln-cache/x.eln",
                    ".cache/y",
                    ".persistent-scratch",
                    ".lsp-session-v1",
                    "elpa/",
                    "elpax",
                ],
            ),
            # Mixed shapes that also exercise the general (regex) bucket.
            (
                [
                    GlobPattern.exclude("*"),
                    GlobPattern.include("*.txt"),
                    GlobPattern.include("logs/*"),
                    GlobPattern.include("keep.me"),
                    GlobPattern.include("a*b.csv"),
                ],
                ["a.txt", "logs/x", "keep.me", "aXXb.csv", "ab.csv", "other.bin"],
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


class TestAnchored:
    """Root-anchored (absolute) patterns: matched against ``full_key`` by joining
    each pattern onto the entry's anchor with ``os.path.join``, the way aws-cli
    joins each pattern onto the per-side root (its ``filters._full_path_patterns``).
    """

    def test_relative_only_list_keeps_a_fast_path(self) -> None:
        # No anchored pattern -> a macro-shape fast path, full_key never read.
        m = globsieve.compile([GlobPattern.exclude("*"), GlobPattern.include("*.txt")])
        assert not isinstance(m, globsieve.Anchored)
        assert m.included("a.txt", "/anywhere/a.txt") is True
        assert m.included("a.log", "/anywhere/a.log") is False

    def test_anchored_pattern_matches_the_full_key(self) -> None:
        # An absolute pattern selects Anchored and matches the full key, not the
        # bare compare_key, so it only bites under its own root.
        m = globsieve.compile([GlobPattern.exclude("/data/src/keep/*")])
        assert isinstance(m, globsieve.Anchored)
        assert m.included("keep/a", "/data/src/keep/a") is False  # under root -> excluded
        assert m.included("keep/a", "/other/keep/a") is True  # different root -> visible

    def test_one_pattern_prunes_two_sides_independently(self) -> None:
        # --exclude '/data/src/keep/*' excludes the local source but not the
        # anchorless S3 destination, so --delete still removes it.
        m = globsieve.compile([GlobPattern.exclude("/data/src/keep/*")])
        assert m.included("keep/a", "/data/src/keep/a") is False  # source (local) excluded
        assert m.included("keep/a", "dst/keep/a") is True  # dest (s3 key) visible
        assert m.included("keep/a", None) is True  # no anchor (s3 listing) -> visible

    def test_wildcard_in_the_root_region_matches(self) -> None:
        # ``*`` is greedy across ``/``, so a wildcard in the anchored region
        # matches (the old strip-based translation wrongly dropped this).
        m = globsieve.compile([GlobPattern.exclude("/data/*/secret")])
        assert m.included("secret", "/data/src/secret") is False  # * == src -> excluded
        assert m.included("secret", "/data/a/b/secret") is False  # * == a/b -> excluded

    def test_relative_and_anchored_interleave_last_match_wins(self) -> None:
        m = globsieve.compile(
            [GlobPattern.exclude("*"), GlobPattern.include("/data/src/keep/*.txt")]
        )
        assert m.included("keep/a.txt", "/data/src/keep/a.txt") is True  # re-included
        assert m.included("keep/a.log", "/data/src/keep/a.log") is False  # stays excluded

    def test_windows_driveless_absolute_anchors_to_the_entry_drive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On Windows ``os.path.join`` lends the entry's own drive to a driveless
        # absolute pattern (``/data/...`` under ``C:\\...`` -> ``C:/data/...``),
        # exactly like aws-cli's join onto the (single, drive-bearing) root;
        # simulate it with ntpath on POSIX. In one scan every entry shares that
        # drive, so the pattern bites whichever drive the entry is on.
        monkeypatch.setattr(globsieve.os, "path", ntpath)
        monkeypatch.setattr(globsieve.os, "sep", "\\")
        m = globsieve.compile([GlobPattern.exclude("/data/src/keep/*")])
        assert isinstance(m, globsieve.Anchored)
        assert m.included("keep/a", "C:/data/src/keep/a") is False
        assert m.included("keep/a", "D:/data/src/keep/a") is False

    def test_windows_drive_qualified_pattern_only_matches_its_drive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A pattern that names its own drive matches only that drive (os.path.join
        # keeps the pattern's drive over the entry's), so a different-drive entry
        # is visible.
        monkeypatch.setattr(globsieve.os, "path", ntpath)
        monkeypatch.setattr(globsieve.os, "sep", "\\")
        m = globsieve.compile([GlobPattern.exclude("C:/data/src/keep/*")])
        assert m.included("keep/a", "C:/data/src/keep/a") is False  # same drive -> excluded
        assert m.included("keep/a", "D:/data/src/keep/a") is True  # other drive -> visible


class TestSeparatorNormalization:
    """A pattern's host separator folds to ``/`` at compile time so it matches the
    ``/``-folded ``compare_key`` (aws-cli's per-side ``replace`` in
    ``filters._match_pattern``, collapsed to one step because boto3-s3 matches in
    ``/`` space). Windows-only behavior, so the Windows cases simulate ``os.sep``.
    """

    def test_windows_backslash_relative_pattern_folds_and_matches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On Windows a relative '\'-separated --exclude matches the '/'-folded
        # compare_key (aws-cli folds it to the native sep and matches native paths).
        monkeypatch.setattr(globsieve.os, "path", ntpath)
        monkeypatch.setattr(globsieve.os, "sep", "\\")
        m = globsieve.compile(
            [GlobPattern.exclude("logs\\*.txt"), GlobPattern.exclude("a\\b\\c.log")]
        )
        assert m.included("logs/app.txt") is False  # dir/suffix pattern, folded
        assert m.included("a/b/c.log") is False  # literal pattern, folded
        assert m.included("logs/app.log") is True  # neither pattern -> visible

    def test_posix_backslash_stays_a_literal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # On POSIX os.sep is '/', so folding is a no-op and '\' is a literal (not a
        # separator) - it does not match a '/'-separated key, like aws-cli on POSIX.
        # Simulated like the Windows cases above, so the test runs on any host.
        monkeypatch.setattr(globsieve.os, "path", posixpath)
        monkeypatch.setattr(globsieve.os, "sep", "/")
        m = globsieve.compile([GlobPattern.exclude("logs\\*.txt")])
        assert m.included("logs/app.txt") is True  # '\' literal -> no match -> visible


class TestDriveRelative:
    """A Windows drive-relative pattern (``C:foo``) anchors to the root the way
    aws-cli's ``os.path.join`` merges it: ``ntpath.join('C:\\root', 'C:foo') ==
    'C:\\root\\foo'``. globsieve strips the drive at compile and matches
    ``compare_key`` - not the never-match literal a raw ``C:foo``
    would be. Windows-only; on POSIX the colon is a valid filename character and
    the pattern stays literal.
    """

    def test_windows_drive_relative_folds_to_relative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(globsieve.os, "path", ntpath)
        monkeypatch.setattr(globsieve.os, "sep", "\\")
        m = globsieve.compile([GlobPattern.exclude("C:secret")])
        assert m.included("secret") is False  # drive dropped -> relative pattern excludes
        assert m.included("keep") is True
        # A drive-relative directory pattern folds its separator and anchors too.
        sub = globsieve.compile([GlobPattern.exclude("C:logs\\*")])
        assert sub.included("logs/app.txt") is False
        assert sub.included("data/app.txt") is True

    def test_windows_drive_absolute_still_routes_to_anchored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A drive-*absolute* C:/foo keeps its drive (is_anchored) and is not
        # stripped - it routes to Anchored and matches the full key, unchanged.
        monkeypatch.setattr(globsieve.os, "path", ntpath)
        monkeypatch.setattr(globsieve.os, "sep", "\\")
        m = globsieve.compile([GlobPattern.exclude("C:/data/keep/*")])
        assert isinstance(m, globsieve.Anchored)
        assert m.included("keep/a", "C:/data/keep/a") is False

    def test_posix_colon_pattern_stays_a_literal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # On POSIX splitdrive finds no drive (the colon is a valid character), so
        # ``C:foo`` is a literal that matches only ``C:foo`` - never stripped to
        # ``foo`` - aws-cli-faithful there too.
        monkeypatch.setattr(globsieve.os, "path", posixpath)
        monkeypatch.setattr(globsieve.os, "sep", "/")
        m = globsieve.compile([GlobPattern.exclude("C:foo")])
        assert m.included("C:foo") is False  # matched literally
        assert m.included("foo") is True  # not stripped to a relative pattern


class TestGlobFilter:
    """The fluent ``GlobFilter`` front end: a ``FileFilter`` matching ``compare_key``."""

    @staticmethod
    def _info(compare_key: str) -> FileInfo:
        return FileInfo(key=compare_key, compare_key=compare_key)

    def test_chained_rules_are_last_match_wins(self) -> None:
        keep = GlobFilter().exclude("*").include("*.txt")
        assert keep(self._info("a.txt")) is True
        assert keep(self._info("a.log")) is False

    def test_reverse_order_excludes_everything(self) -> None:
        keep = GlobFilter().include("*.txt").exclude("*")
        assert keep(self._info("a.txt")) is False

    def test_exclude_and_include_take_several_patterns(self) -> None:
        keep = GlobFilter().exclude("*").include("*.tar.gz", "*.zip")
        assert keep(self._info("x.tar.gz")) is True
        assert keep(self._info("x.zip")) is True
        assert keep(self._info("x.bin")) is False

    def test_empty_filter_keeps_everything(self) -> None:
        assert GlobFilter()(self._info("anything")) is True

    def test_agrees_with_compile(self) -> None:
        glob = GlobFilter().exclude("*").include("*.txt")
        ref = globsieve.compile([GlobPattern.exclude("*"), GlobPattern.include("*.txt")])
        for key in ("a.txt", "a.log", "sub/b.txt", ""):
            assert glob(self._info(key)) == ref.included(key)

    def test_builder_methods_return_self(self) -> None:
        f = GlobFilter()
        assert f.exclude("*") is f
        assert f.include("*.txt") is f
        assert f.compile() is f

    def test_compile_is_eager_but_not_frozen(self) -> None:
        # compile() forces compilation; a later rule re-dirties and the next
        # call recompiles, changing the verdict (no freeze).
        keep = GlobFilter().exclude("*").include("*.txt").compile()
        assert keep(self._info("a.log")) is False
        keep.include("*.log")
        assert keep(self._info("a.log")) is True

    def test_uncompiled_filter_compiles_on_first_use(self) -> None:
        # Passing a filter without calling compile() works: __call__ compiles lazily.
        assert GlobFilter().exclude("*.log")(self._info("a.log")) is False

    def test_missing_compare_key_raises(self) -> None:
        with pytest.raises(ValueError, match="compare_key"):
            GlobFilter().exclude("*")(FileInfo(key="a.txt"))
