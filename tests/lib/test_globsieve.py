r"""Unit tests for `boto3_s3.globsieve`: aws-cli's ``--exclude`` / ``--include`` engine.

The engine decides from an entry's R (``compare_key``), F (the full path aws
matches) and D (F's drive); these tests drive it through ``included`` on plain
strings and through the ``FileFilter`` call. The host model (POSIX or Windows
path rules and fold) is bound at compile time from ``globsieve._WINDOWS``,
which the ``windows`` / ``posix`` fixtures set, so the Windows rules run on
any host through the ``ntpath`` port. Expectations come from three places:
the cases measured against aws 2.36.40 (``TestMeasured``), the cases moved
from the CLI's former engine (case and separator folding, ``LCMapStringEx``),
and a fuzz against a verbatim port of aws-cli's ``filters.py``
(``TestAgainstAwsFilter``).
"""

from __future__ import annotations

import fnmatch
import json
import ntpath
import pickle
import posixpath
import random
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import FrozenInstanceError, dataclass
from typing import ClassVar

import pytest

import boto3_s3
from boto3_s3 import (
    Exclude,
    FileInfo,
    Include,
    LocalFileInfo,
    S3FileInfo,
    S3Storage,
    ValidationError,
    globsieve,
)
from boto3_s3.producers import single_object_filter_root

_BS = chr(92)  # a backslash, spelled so no escape rewriting can touch it


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compile with Windows' path rules and fold, whatever the host."""
    monkeypatch.setattr(globsieve, "_WINDOWS", True)


@pytest.fixture
def posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compile with POSIX path rules (byte-exact), whatever the host."""
    monkeypatch.setattr(globsieve, "_WINDOWS", False)


def _kept(rules: Iterable[Exclude | Include], relative: str | None, full: str | None) -> bool:
    return globsieve.compile(rules).included(relative, full)


# ----- rules and the compiled value ------------------------------------------------


class TestRules:
    def test_patterns_are_kept_in_order(self) -> None:
        assert Exclude("*", "a").patterns == ("*", "a")
        assert Include().patterns == ()

    def test_rules_are_frozen_values(self) -> None:
        rule = Exclude("*.tmp")
        with pytest.raises(FrozenInstanceError):
            rule.patterns = ("x",)  # type: ignore[misc]
        assert rule == Exclude("*.tmp")
        assert rule != Include("*.tmp")
        assert hash(rule) == hash(Exclude("*.tmp"))
        assert len({Exclude("a"), Exclude("a"), Include("a")}) == 2

    def test_repr_reads_like_the_call(self) -> None:
        assert repr(Exclude("*", "a.txt")) == "Exclude('*', 'a.txt')"
        assert repr(globsieve.compile([Include("x")])) == "GlobFilter([Include('x')])"

    def test_rules_pickle(self) -> None:
        rule = Include("*.css", "*.html")
        assert pickle.loads(pickle.dumps(rule)) == rule

    def test_compile_rejects_what_is_not_a_rule(self) -> None:
        with pytest.raises(ValidationError):
            globsieve.compile(["*.txt"])  # type: ignore[list-item]

    def test_rules_attribute_is_the_list_as_given(self) -> None:
        rules = [Exclude("*"), Include("*.txt", "*.md")]
        assert globsieve.compile(rules).rules == tuple(rules)


class TestDecision:
    """aws's ``Filter.call``: in order, the last matching pattern decides."""

    def test_no_rules_keep_everything(self) -> None:
        assert _kept([], "anything", "/x/anything") is True

    def test_last_match_wins(self) -> None:
        rules = [Exclude("*"), Include("*.txt")]
        assert _kept(rules, "a.txt", None) is True
        assert _kept(rules, "a.log", None) is False
        assert _kept([Include("*.txt"), Exclude("*")], "a.txt", None) is False

    def test_each_pattern_of_a_rule_is_a_rule_of_its_own(self) -> None:
        rules = [Exclude("*"), Include("*.tar.gz", "*.zip"), Exclude("old.*")]
        assert _kept(rules, "x.zip", None) is True
        assert _kept(rules, "x.tar.gz", None) is True
        assert _kept(rules, "old.zip", None) is False
        assert _kept(rules, "x.bin", None) is False

    def test_star_and_question_mark_cross_the_separator(self) -> None:
        assert _kept([Exclude("a*z")], "a/b/z", None) is False
        assert _kept([Exclude("a?z")], "a/z", None) is False
        assert _kept([Exclude("[!x]")], "/", None) is False

    def test_the_whole_path_must_match(self) -> None:
        assert _kept([Exclude("sub")], "sub/x", None) is True
        assert _kept([Exclude("sub/")], "sub/x", None) is True
        assert _kept([Exclude("sub/*")], "sub/x", None) is False

    @pytest.mark.usefixtures("posix")
    def test_a_relative_and_a_full_path_rule_order_by_position(self) -> None:
        later_full = [Exclude("*"), Include("/base/keep/*")]
        assert _kept(later_full, "keep/a", "/base/keep/a") is True
        assert _kept(later_full, "drop/a", "/base/drop/a") is False
        later_relative = [Include("/base/*"), Exclude("*")]
        assert _kept(later_relative, "keep/a", "/base/keep/a") is False
        earlier_full = [Exclude("/base/*"), Include("keep/*")]
        assert _kept(earlier_full, "keep/a", "/base/keep/a") is True
        assert _kept(earlier_full, "drop/a", "/base/drop/a") is False

    @pytest.mark.usefixtures("posix")
    def test_a_missing_value_skips_the_rules_that_need_it(self) -> None:
        assert _kept([Exclude("*")], None, "/base/x") is True
        assert _kept([Exclude("/base/*")], "x", None) is True
        assert _kept([Exclude("/base/*"), Exclude("x")], None, "/base/x") is False


class TestFileFilter:
    """The call form reads R from ``compare_key`` and F from the record's kind."""

    @pytest.mark.usefixtures("posix")
    def test_a_local_entry_offers_its_key_as_the_full_path(self) -> None:
        keep = globsieve.compile([Exclude("/data/src/keep/*")])
        assert keep(LocalFileInfo(key="/data/src/keep/a", compare_key="keep/a")) is False
        assert keep(LocalFileInfo(key="/other/keep/a", compare_key="keep/a")) is True

    @pytest.mark.usefixtures("posix")
    def test_an_s3_entry_offers_bucket_and_key(self) -> None:
        storage = S3Storage("s3://bkt/pre/")
        keep = globsieve.compile([Exclude("*:*"), Exclude("/bkt/*")])
        assert keep(S3FileInfo(key="pre/a:b", compare_key="a:b", storage=storage)) is False
        # A root-anchored pattern never matches bucket/key, which has no root.
        assert keep(S3FileInfo(key="pre/x", compare_key="x", storage=storage)) is True

    @pytest.mark.usefixtures("windows")
    def test_an_s3_entry_matches_a_glob_drive_pattern_on_windows(self) -> None:
        # aws joins `*:*` onto the S3 base as another drive: the pattern as
        # written, matched against bucket/key.
        storage = S3Storage("s3://bkt/pre/")
        keep = globsieve.compile([Exclude("*:*")])
        assert keep(S3FileInfo(key="pre/a:b", compare_key="a:b", storage=storage)) is False
        assert keep(S3FileInfo(key="pre/ab", compare_key="ab", storage=storage)) is True

    @pytest.mark.usefixtures("posix")
    def test_other_entries_have_no_full_path(self) -> None:
        keep = globsieve.compile([Exclude("/data/*"), Exclude("*.tmp")])
        assert keep(FileInfo(key="/data/x", compare_key="x")) is True
        assert keep(FileInfo(key="/data/x.tmp", compare_key="x.tmp")) is False
        assert keep(S3FileInfo(key="/data/x", compare_key="x")) is True  # no storage

    @pytest.mark.usefixtures("windows")
    def test_other_entries_see_drive_forms_only_as_full_paths(self) -> None:
        # No F means no D: `C:x` is another drive's pattern and `\x` a rooted
        # one, both matched against F, so neither applies.
        keep = globsieve.compile([Exclude("C:x"), Exclude(f"{_BS}x")])
        assert keep(FileInfo(key="C:/b/x", compare_key="x")) is True
        assert keep(LocalFileInfo(key="C:/b/x", compare_key="x")) is False

    def test_an_entry_without_a_compare_key_meets_only_full_path_rules(self) -> None:
        keep = globsieve.compile([Exclude("*")])
        assert keep(LocalFileInfo(key="/x", compare_key=None)) is True


class TestPickle:
    """A compiled filter crosses into a process pool and keeps its host model."""

    _ENTRIES = (
        ("a.TXT", "/b/a.TXT"),
        ("a.txt", "/b/a.txt"),
        (f"a{_BS}b", f"/b/a{_BS}b"),
        ("x", "C:/b/x"),
        ("x", None),
    )

    @pytest.mark.parametrize("host", ["posix", "windows"])
    def test_round_trip_decides_the_same(self, host: str, request: pytest.FixtureRequest) -> None:
        request.getfixturevalue(host)
        keep = globsieve.compile(
            [Exclude("*"), Include("*.txt", "a/b"), Exclude("C:x"), Include(f"{_BS}zzz")]
        )
        expected = [keep.included(r, f) for r, f in self._ENTRIES]
        # Unpickled under the other model still decides by the original one.
        request.getfixturevalue("posix" if host == "windows" else "windows")
        again = pickle.loads(pickle.dumps(keep))
        assert again.rules == keep.rules
        assert [again.included(r, f) for r, f in self._ENTRIES] == expected


class TestPublicSurface:
    def test_all_is_the_documented_surface(self) -> None:
        assert set(globsieve.__all__) == {"Exclude", "GlobFilter", "Include", "compile", "normcase"}
        for name in globsieve.__all__:
            assert hasattr(globsieve, name), name

    def test_the_rules_and_the_filter_are_importable_from_the_package_root(self) -> None:
        for name in ("Exclude", "Include", "GlobFilter"):
            assert name in boto3_s3.__all__
            assert getattr(boto3_s3, name) is getattr(globsieve, name)


# ----- normcase and the Windows fold -------------------------------------------------


class TestNormcase:
    @pytest.mark.usefixtures("posix")
    def test_identity_on_posix(self) -> None:
        assert globsieve.normcase(f"A/B{_BS}C") == f"A/B{_BS}C"

    @pytest.mark.usefixtures("windows")
    def test_windows_lowers_and_turns_slashes_into_backslashes(self) -> None:
        assert globsieve.normcase(f"A/B{_BS}C") == f"a{_BS}b{_BS}c"


class TestCaseFolding:
    _RULES = (Exclude("*"), Include("*.TXT"))

    @pytest.mark.usefixtures("posix")
    def test_posix_filter_is_case_sensitive(self) -> None:
        assert _kept(self._RULES, "a.TXT", None) is True
        assert _kept(self._RULES, "a.txt", None) is False

    @pytest.mark.usefixtures("windows")
    def test_windows_filter_is_case_insensitive(self) -> None:
        # aws normcases both sides on Windows, so `*.TXT` matches `a.txt`.
        assert _kept(self._RULES, "a.txt", None) is True
        assert _kept(self._RULES, "a.TXT", None) is True
        assert _kept(self._RULES, "a.log", None) is False

    @pytest.mark.usefixtures("windows")
    def test_windows_absolute_pattern_is_case_insensitive(self) -> None:
        rules = [Exclude(f"C:{_BS}Data{_BS}Src{_BS}Keep{_BS}*")]
        assert _kept(rules, "keep/a", "c:/data/src/keep/a") is False


class TestWindowsSeparatorFolding:
    """On Windows a backslash and a slash are one separator, in keys as in patterns.

    aws-cli's ``fnmatch.fnmatch`` normcases the path and the pattern, and
    ``ntpath.normcase`` turns every ``/`` into a backslash. An S3 key can hold
    a literal backslash, so ``--exclude a/b`` excludes the key ``a<bs>b`` there
    - measured against aws.exe 2.36.40 by real deletion (``rm --recursive``
    keeps it).
    """

    @pytest.fixture(autouse=True)
    def _windows(self, windows: None) -> None:
        del windows

    @staticmethod
    def _entry(key: str) -> S3FileInfo:
        # rm --recursive s3://bucket: the base is `bucket/`, R the whole key.
        return S3FileInfo(key=key, compare_key=key, storage=S3Storage("s3://bucket"))

    @pytest.mark.parametrize("pattern", ["a/b", "a/*", "A/B", f"a{_BS}b", f"a{_BS}*"])
    @pytest.mark.parametrize("with_full_path_rule", [False, True])
    def test_a_backslash_in_a_key_matches_a_separator(
        self, pattern: str, with_full_path_rule: bool
    ) -> None:
        # A rooted pattern ahead of the rule makes the filter build its plans
        # per drive and match F too; the verdict must not change.
        lead = [Exclude("/zzz-nowhere/*")] if with_full_path_rule else []
        keep = globsieve.compile([*lead, Exclude(pattern)])
        for key in (f"a{_BS}b", "a/b", f"A{_BS}B"):
            assert keep(self._entry(key)) is False, key
        assert keep(self._entry("ab")) is True

    def test_include_reaches_a_backslash_key_too(self) -> None:
        keep = globsieve.compile([Exclude("*"), Include("dir/*.txt")])
        assert keep(self._entry(f"dir{_BS}x.txt")) is True

    def test_posix_keeps_the_backslash_a_literal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(globsieve, "_WINDOWS", False)
        keep = globsieve.compile([Exclude("a/b")])
        assert keep(self._entry(f"a{_BS}b")) is True

    def test_a_class_spanning_the_slash_does_not_match_the_separator(self) -> None:
        # aws-cli matches in the backslash form, where the separator is 0x5C:
        # the range `+-9` holds `/` (0x2F) and not that.
        keep = globsieve.compile([Exclude("a[+-9]b")])
        assert keep(self._entry("a/b")) is True
        assert keep(self._entry("a5b")) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows' own case mapping")
class TestWindowsCaseMapping:
    """The fold is Windows' locale-invariant lower-casing, not ``str.lower``.

    aws.exe bundles a Python whose ``ntpath.normcase`` calls ``LCMapStringEx``;
    that table is one-to-one, so it differs from Unicode's full case mapping
    for a few hundred code points. Measured against aws.exe 2.36.40:
    ``--exclude 'k*'`` does not keep a key that starts with the Kelvin sign,
    ``--exclude 'i*'`` not one that starts with U+0130, and ``str.lower`` would
    have kept both. Run on Python 3.10 (the direct call) and 3.14 (the stdlib).
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
        assert globsieve._windows_normcase(text) == folded
        assert globsieve.normcase(text) == folded

    def test_the_direct_call_agrees_with_the_stdlib_where_the_stdlib_has_it(self) -> None:
        if sys.version_info < (3, 12):
            pytest.skip("ntpath.normcase lower-cases with str.lower before Python 3.12")
        for code in (*range(0x20, 0x250), 0x130, 0x212A, 0x1E9E, 0x3A3, 0x10400, 0x1F600):
            text = "x" + chr(code) + "/y"
            assert globsieve._lcmap_lower(text.replace("/", _BS)) == ntpath.normcase(text), hex(
                code
            )

    def test_the_kelvin_sign_is_not_a_k(self) -> None:
        keep = globsieve.compile([Exclude("k*")])
        assert keep.included(chr(0x212A) + "x", None) is True
        assert keep.included("Kx", None) is False


# ----- splitroot and the forms of a pattern ------------------------------------------


_SPLITROOT_CASES: list[tuple[str, tuple[str, str, str]]] = [
    ("", ("", "", "")),
    ("x", ("", "", "x")),
    ("a/b", ("", "", "a/b")),
    (f"{_BS}x", ("", _BS, "x")),
    ("/x", ("", "/", "x")),
    ("C:", ("C:", "", "")),
    ("C:x", ("C:", "", "x")),
    (f"C:{_BS}x", ("C:", _BS, "x")),
    ("c:/x", ("c:", "/", "x")),
    ("*:*", ("*:", "", "*")),
    ("a:b.txt", ("a:", "", "b.txt")),
    ("ab:c", ("", "", "ab:c")),
    (f"{_BS}{_BS}srv{_BS}share{_BS}x", (f"{_BS}{_BS}srv{_BS}share", _BS, "x")),
    ("//srv/share/x", ("//srv/share", "/", "x")),
    (f"{_BS}{_BS}srv{_BS}share", (f"{_BS}{_BS}srv{_BS}share", "", "")),
    (f"{_BS}{_BS}srv", (f"{_BS}{_BS}srv", "", "")),
    (f"{_BS}{_BS}", (f"{_BS}{_BS}", "", "")),
    (f"{_BS}{_BS}?{_BS}C:{_BS}x", (f"{_BS}{_BS}?{_BS}C:", _BS, "x")),
    (
        f"{_BS}{_BS}?{_BS}UNC{_BS}srv{_BS}share{_BS}x",
        (f"{_BS}{_BS}?{_BS}UNC{_BS}srv{_BS}share", _BS, "x"),
    ),
    ("//?/unc/srv/share/x", ("//?/unc/srv/share", "/", "x")),
]


class TestSplitroot:
    @pytest.mark.parametrize(("path", "parts"), _SPLITROOT_CASES)
    def test_the_port_splits_like_python_314(self, path: str, parts: tuple[str, str, str]) -> None:
        assert globsieve._splitroot(path) == parts


def _python314() -> str | None:
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        found = subprocess.run(
            [uv, "python", "find", "3.14"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = found.stdout.strip()
    return path if found.returncode == 0 and path else None


_ROOT_SAMPLES = (
    "",
    "x",
    "a/b",
    "/",
    _BS,
    "//",
    f"{_BS}{_BS}",
    "///",
    "C:",
    "c:",
    "C:/",
    f"C:{_BS}",
    "*:",
    "?:",
    "//srv",
    "//srv/",
    "//srv/share",
    f"{_BS}{_BS}srv{_BS}share{_BS}",
    "//?/C:/",
    "//?/UNC/srv/share/",
    f"{_BS}{_BS}?{_BS}unc{_BS}srv",
    "//./dev/",
    ":",
    "a:",
)
_TAIL_SAMPLES = ("", "x", "*", "a/b", f"a{_BS}b", ":", "*:*")


class TestAgainstPython314:
    """The ports agree with Python 3.14's own ``ntpath``, where uv finds one."""

    def test_splitroot_and_join_ports(self) -> None:
        python = _python314()
        if python is None:
            pytest.skip("no Python 3.14 interpreter (uv python find 3.14)")
        paths = sorted({root + tail for root in _ROOT_SAMPLES for tail in _TAIL_SAMPLES})
        bases = [
            "C:\\base\\d",
            "c:\\Base",
            "C:\\",
            "\\\\srv\\share\\base",
            "\\\\srv\\share\\",
            "bkt/pre",
            "bkt/",
        ]
        script = (
            "import json, ntpath, sys\n"
            "paths, bases = json.load(sys.stdin)\n"
            "json.dump([[list(ntpath.splitroot(p)) for p in paths],"
            " [[ntpath.join(b, p) for p in paths] for b in bases],"
            " [ntpath.dirname(b + '\\\\x') for b in bases]], sys.stdout)\n"
        )
        result = subprocess.run(
            [python, "-I", "-c", script],
            input=json.dumps([paths, bases]),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        roots, joins, dirnames = json.loads(result.stdout)
        assert [list(globsieve._splitroot(p)) for p in paths] == roots
        assert [[_nt_join(b, p) for p in paths] for b in bases] == joins
        assert [_nt_dirname(b + "\\x") for b in bases] == dirnames


class TestAbsoluteForms:
    r"""How each form of pattern joins onto a base, as aws's ``os.path.join``.

    Entries: a local tree on drive C: (base ``C:\base\d``), one on a UNC share
    (``\\srv\share\base\d``), and an S3 prefix (``bkt/pre/``). Each row lists
    the entries the exclude removes; R is the part below the base.
    """

    _ENTRIES: ClassVar[dict[str, tuple[str | None, str]]] = {
        "c:x": ("x", "C:/base/d/x"),
        "c:sub/x": ("sub/x", "C:/base/d/sub/x"),
        "c:root": ("", "C:/base/d/"),
        "unc:x": ("x", "//srv/share/base/d/x"),
        "unc:sub/x": ("sub/x", "//srv/share/base/d/sub/x"),
        "unc:root": ("", "//srv/share/base/d/"),
        "s3:x": ("x", "bkt/pre/x"),
        "s3:a:b": ("a:b", "bkt/pre/a:b"),
        "s3:sub/x": ("sub/x", "bkt/pre/sub/x"),
    }

    @pytest.mark.usefixtures("windows")
    @pytest.mark.parametrize(
        ("pattern", "excluded"),
        [
            # relative: against R on every side
            ("x", {"c:x", "unc:x", "s3:x"}),
            ("sub/x", {"c:sub/x", "unc:sub/x", "s3:sub/x"}),
            ("", {"c:root", "unc:root"}),
            # drive-absolute and UNC: the join drops the base; against F
            (f"C:{_BS}base{_BS}d{_BS}x", {"c:x"}),
            ("c:/BASE/D/*", {"c:x", "c:sub/x", "c:root"}),
            (f"{_BS}{_BS}srv{_BS}share{_BS}base{_BS}d{_BS}*x", {"unc:x", "unc:sub/x"}),
            ("//SRV/Share/base/d/x", {"unc:x"}),
            (f"D:{_BS}base{_BS}d{_BS}x", set()),
            # root-relative: the base's drive (none on S3) + the pattern
            (f"{_BS}x", set()),
            (f"{_BS}*x", {"c:x", "c:sub/x", "unc:x", "unc:sub/x"}),
            ("/base/d/x", {"c:x", "unc:x"}),
            # drive-relative, the same drive (in any case): like a relative one
            ("C:x", {"c:x"}),
            ("c:sub/x", {"c:sub/x"}),
            ("C:", {"c:root"}),
            (f"{_BS}{_BS}srv{_BS}share", {"unc:root"}),
            (f"{_BS}{_BS}SRV{_BS}SHARE", {"unc:root"}),
            # drive-relative, another drive: as written against F
            ("D:x", set()),
            ("D:*", set()),
            ("//srv/share", set()),  # another spelling: ntpath.join sees another drive
            ("?:*x", {"c:x", "c:sub/x"}),
            ("*:*", {"c:x", "c:sub/x", "c:root", "s3:a:b"}),
            ("a:b", set()),  # on S3 a drive `a:`, not the key `a:b`
        ],
    )
    def test_each_form_matches_like_the_join(self, pattern: str, excluded: set[str]) -> None:
        keep = globsieve.compile([Exclude(pattern)])
        removed = {name for name, (r, f) in self._ENTRIES.items() if not keep.included(r, f)}
        assert removed == excluded

    @pytest.mark.usefixtures("posix")
    @pytest.mark.parametrize(
        ("pattern", "excluded"),
        [
            ("x", {"local:x", "s3:x"}),
            ("/base/d/x", {"local:x"}),
            ("/*x", {"local:x", "local:sub/x", "local:bs", "local:c"}),
            ("//base/d/x", set()),  # posixpath.join keeps the doubled root
            (f"{_BS}x", {"local:bs"}),  # a literal backslash, relative
            ("C:x", {"local:c"}),  # a literal file name, relative
        ],
    )
    def test_posix_has_one_absolute_form(self, pattern: str, excluded: set[str]) -> None:
        entries = {
            "local:x": ("x", "/base/d/x"),
            "local:sub/x": ("sub/x", "/base/d/sub/x"),
            "local:bs": (f"{_BS}x", f"/base/d/{_BS}x"),
            "local:c": ("C:x", "/base/d/C:x"),
            "s3:x": ("x", "bkt/pre/x"),
        }
        keep = globsieve.compile([Exclude(pattern)])
        removed = {name for name, (r, f) in entries.items() if not keep.included(r, f)}
        assert removed == excluded

    @pytest.mark.usefixtures("windows")
    def test_the_plan_follows_each_entrys_drive(self) -> None:
        # One filter, entries on two drives and on S3: `C:x` is relative on C:
        # only, and `\x` is rooted on each entry's own drive.
        keep = globsieve.compile([Exclude("C:x"), Exclude(f"{_BS}top")])
        assert keep.included("x", "C:/b/x") is False
        assert keep.included("x", "D:/b/x") is True
        assert keep.included("x", "bkt/x") is True
        assert keep.included("top", "D:/top") is False
        assert keep.included("top", "C:/top") is False
        assert keep.included("top", "bkt/top") is True


# ----- cases measured against aws 2.36.40 ------------------------------------------------


class TestMeasured:
    """Pinned outcomes of ``aws s3 cp --dryrun`` (2.36.40, Linux and Windows).

    Each entry is given as the library stamps it: R is aws's path below its
    base (``None`` where the entry is not under it), F the full path aws
    matches, ending in a separator for a directory source.
    """

    @pytest.mark.usefixtures("posix")
    @pytest.mark.parametrize(
        ("source", "patterns", "kept"),
        [
            # `cp d/ s3://b/k`: F /cwd/d/, base /cwd/d, R ""
            ("d/", ["d/"], True),
            ("d/", [""], False),
            ("d/", ["d"], True),
            ("d/", ["*"], False),
            ("d/", ["*", "+"], True),
            # `cp d s3://b/k`: F /cwd/d/, base /cwd, R "d/"
            ("d", ["d/"], False),
            ("d", ["d"], True),
            ("d", [""], True),
            # `cp .. s3://b/k` from /abs/c2/sub: F /abs/c2/, not under /abs/c2/sub
            ("..", ["*"], True),
            ("..", [".."], True),
            ("..", ["../"], True),
            ("..", ["../*"], True),
            ("..", ["/abs/c2"], True),
            ("..", ["/abs/c2/"], False),
            ("..", ["/*"], False),
            # `cp . s3://b/k` and `cp ../ s3://b/k` behave like `d/`
            (".", [""], False),
            (".", ["*"], False),
            ("../", [""], False),
            ("../", ["*"], False),
        ],
    )
    def test_linux_single_directory_sources(
        self, source: str, patterns: list[str], kept: bool
    ) -> None:
        stamped: dict[str, tuple[str | None, str]] = {
            "d/": ("", "/cwd/d/"),
            "d": ("d/", "/cwd/d/"),
            "..": (None, "/abs/c2/"),
            ".": ("", "/cwd/"),
            "../": ("", "/abs/c2/"),
        }
        relative, full = stamped[source]
        rules = [Include(p[1:]) if p.startswith("+") else Exclude(p) for p in patterns]
        assert _kept(rules, relative, full) is kept

    _BASE = "C:/Users/u/awsfp/d"
    _UNC = "//wsl.localhost/Ubuntu-26.04/home/u/uncsrc/d"
    _FILES = ("x", "a.txt", "sub/x", "sub/b.txt")

    @pytest.mark.usefixtures("windows")
    @pytest.mark.parametrize(
        ("pattern", "recursive", "single_kept"),
        [
            ("*:*", {"x", "a.txt", "sub/x", "sub/b.txt"}, False),
            ("x", {"x"}, False),
            (f"{_BS}x", set(), True),
            (f"{_BS}*x", {"x", "sub/x"}, False),
            (f"{_BS}Users{_BS}u{_BS}awsfp{_BS}d{_BS}x", {"x"}, False),
            ("C:x", {"x"}, False),
            ("c:x", {"x"}, False),
            (f"C:sub{_BS}x", {"sub/x"}, True),
            ("D:x", set(), True),
            ("D:*", set(), True),
            ("?:*x", {"x", "sub/x"}, False),
            (f"C:{_BS}Users{_BS}u{_BS}awsfp{_BS}d{_BS}x", {"x"}, False),
            (f"{_BS}{_BS}server{_BS}share{_BS}x", set(), True),
        ],
    )
    def test_windows_local_sources(
        self, pattern: str, recursive: set[str], single_kept: bool
    ) -> None:
        # `cp d s3://... --recursive` and the single `cp d\x s3://.../x`.
        keep = globsieve.compile([Exclude(pattern)])
        removed = {r for r in self._FILES if not keep.included(r, f"{self._BASE}/{r}")}
        assert removed == recursive
        assert keep.included("x", f"{self._BASE}/x") is single_kept

    @pytest.mark.usefixtures("windows")
    @pytest.mark.parametrize(
        ("pattern", "excluded"),
        [
            ("*:*", {"a:b.txt"}),
            ("x", {"x"}),
            (f"{_BS}x", set()),
            (f"{_BS}*x", set()),
            ("/*x", set()),
            ("C:x", set()),
            ("D:x", set()),
            ("?:*x", set()),
            (f"{_BS}{_BS}server{_BS}share{_BS}x", set()),
        ],
    )
    def test_windows_s3_source(self, pattern: str, excluded: set[str]) -> None:
        keys = ("x", "a.txt", "a:b.txt", "sub/x", "sub/b.txt")
        keep = globsieve.compile([Exclude(pattern)])
        removed = {k for k in keys if not keep.included(k, f"probe-filters/pfx/{k}")}
        assert removed == excluded

    @pytest.mark.usefixtures("windows")
    @pytest.mark.parametrize(
        ("pattern", "excluded"),
        [
            ("*:*", set()),
            ("x", {"x"}),
            (f"{_BS}x", set()),
            (f"{_BS}*x", {"x", "sub/x"}),
            (f"{_BS}{_BS}wsl.localhost{_BS}Ubuntu-26.04{_BS}*x", {"x", "sub/x"}),
            ("//wsl.localhost/Ubuntu-26.04/*x", {"x", "sub/x"}),
            (f"{_BS}{_BS}WSL.LOCALHOST{_BS}ubuntu-26.04{_BS}*x", {"x", "sub/x"}),
            ("C:x", set()),
            (f"{_BS}{_BS}server{_BS}share{_BS}x", set()),
            (
                f"{_BS}{_BS}wsl.localhost{_BS}Ubuntu-26.04{_BS}home{_BS}u{_BS}uncsrc{_BS}d{_BS}x",
                {"x"},
            ),
        ],
    )
    def test_windows_unc_source(self, pattern: str, excluded: set[str]) -> None:
        keep = globsieve.compile([Exclude(pattern)])
        removed = {r for r in self._FILES if not keep.included(r, f"{self._UNC}/{r}")}
        assert removed == excluded

    @pytest.mark.usefixtures("windows")
    @pytest.mark.parametrize(
        ("source", "excluded_by", "kept_by"),
        [
            # `cp d\ ...`: R "", F C:\cwd\d\ ; a bare `C:` is the empty pattern
            ("d\\", ["", "C:", "*"], ["d/", f"d{_BS}", f"C:d{_BS}", f"c:d{_BS}"]),
            # `cp d ...`: R "d/"
            ("d", ["d/", f"d{_BS}", f"C:d{_BS}", f"c:d{_BS}"], ["", "d", "C:"]),
            # `cp .. ...` from d\sub: F C:\cwd\d\, no R
            ("..", [f"{_BS}*", "*:*"], ["*"]),
        ],
    )
    def test_windows_single_directory_sources(
        self, source: str, excluded_by: list[str], kept_by: list[str]
    ) -> None:
        stamped: dict[str, tuple[str | None, str]] = {
            "d\\": ("", "C:/cwd/d/"),
            "d": ("d/", "C:/cwd/d/"),
            "..": (None, "C:/cwd/d/"),
        }
        relative, full = stamped[source]
        for pattern in excluded_by:
            assert _kept([Exclude(pattern)], relative, full) is False, pattern
        for pattern in kept_by:
            assert _kept([Exclude(pattern)], relative, full) is True, pattern


# ----- fuzz against aws-cli's filters.py ------------------------------------------------


def _nt_join(path: str, *paths: str) -> str:
    """Python 3.14's ``ntpath.join`` (str only), on the ``splitroot`` port."""
    sep = "\\"
    seps = "\\/"
    colon_seps = ":\\/"
    result_drive, result_root, result_path = globsieve._splitroot(path)
    for p in paths:
        p_drive, p_root, p_path = globsieve._splitroot(p)
        if p_root:
            if p_drive or not result_drive:
                result_drive = p_drive
            result_root = p_root
            result_path = p_path
            continue
        elif p_drive and p_drive != result_drive:
            if p_drive.lower() != result_drive.lower():
                result_drive = p_drive
                result_root = p_root
                result_path = p_path
                continue
            result_drive = p_drive
        if result_path and result_path[-1] not in seps:
            result_path = result_path + sep
        result_path = result_path + p_path
    if result_path and not result_root and result_drive and result_drive[-1] not in colon_seps:
        return result_drive + sep + result_path
    return result_drive + result_root + result_path


def _nt_dirname(p: str) -> str:
    """Python 3.14's ``ntpath.dirname`` (``split(p)[0]``), on the ``splitroot`` port."""
    seps = "\\/"
    d, r, p = globsieve._splitroot(p)
    i = len(p)
    while i and p[i - 1] not in seps:
        i -= 1
    head = p[:i]
    return d + r + head.rstrip(seps)


@dataclass(frozen=True)
class _Path:
    """The ``os.path`` aws-cli's filters.py runs on, for one host family.

    The bases fed to it are absolute and normalized, so ``abspath`` is the
    identity there; ``normcase`` is ``fnmatch.fnmatch``'s fold.
    """

    join: Callable[..., str]
    dirname: Callable[[str], str]
    normcase: Callable[[str], str]

    @staticmethod
    def abspath(path: str) -> str:
        return path


@dataclass(frozen=True)
class _Os:
    sep: str
    path: _Path
    windows: bool


_POSIX_OS = _Os("/", _Path(posixpath.join, posixpath.dirname, lambda s: s), windows=False)
_NT_OS = _Os("\\", _Path(_nt_join, _nt_dirname, ntpath.normcase), windows=True)


# aws-cli's filters.py, verbatim but for `os` (a parameter here) and logging.


def _aws_create_filter(parameters: dict[str, object], os: _Os) -> _AwsFilter:
    if parameters["filters"]:
        cli_filters = parameters["filters"]
        real_filters: list[tuple[str, str]] = []
        for filter_type, filter_pattern in cli_filters:  # type: ignore[attr-defined]
            real_filters.append((filter_type.lstrip("-"), filter_pattern))
        source_location = str(parameters["src"])
        if source_location.startswith("s3://"):
            src_rootdir = _aws_get_s3_root(source_location, bool(parameters["dir_op"]))
        else:
            src_rootdir = _aws_get_local_root(source_location, bool(parameters["dir_op"]), os)
        destination_location = str(parameters["dest"])
        if destination_location.startswith("s3://"):
            dst_rootdir = _aws_get_s3_root(destination_location, bool(parameters["dir_op"]))
        else:
            dst_rootdir = _aws_get_local_root(destination_location, bool(parameters["dir_op"]), os)
        return _AwsFilter(real_filters, src_rootdir, dst_rootdir, os)
    return _AwsFilter([], None, None, os)


def _aws_split_s3_bucket_key(s3_path: str) -> tuple[str, str]:
    if s3_path.startswith("s3://"):
        s3_path = s3_path[5:]
    s3_components = s3_path.split("/", 1)
    bucket = s3_components[0]
    s3_key = ""
    if len(s3_components) > 1:
        s3_key = s3_components[1]
    return bucket, s3_key


def _aws_get_s3_root(source_location: str, dir_op: bool) -> str:
    bucket, key = _aws_split_s3_bucket_key(source_location)
    if not dir_op and not key.endswith("/"):
        key = "/".join(key.split("/")[:-1])
    s3_path = "/".join([bucket, key])
    return s3_path


def _aws_get_local_root(source_location: str, dir_op: bool, os: _Os) -> str:
    if dir_op:
        rootdir = os.path.abspath(source_location)
    else:
        rootdir = os.path.abspath(os.path.dirname(source_location))
    return rootdir


@dataclass(frozen=True)
class _AwsFileInfo:
    src: str
    src_type: str


class _AwsFilter:
    def __init__(
        self, patterns: list[tuple[str, str]], rootdir: str | None, dst_rootdir: str | None, os: _Os
    ) -> None:
        self._os = os
        self.patterns = self._full_path_patterns(patterns, rootdir)
        self.dst_patterns = self._full_path_patterns(patterns, dst_rootdir)

    def _full_path_patterns(
        self, original_patterns: list[tuple[str, str]], rootdir: str | None
    ) -> list[tuple[str, str]]:
        full_patterns: list[tuple[str, str]] = []
        for pattern in original_patterns:
            full_patterns.append((pattern[0], self._os.path.join(rootdir, pattern[1])))
        return full_patterns

    def call(self, file_infos: Iterable[_AwsFileInfo]) -> Iterator[_AwsFileInfo]:
        for file_info in file_infos:
            file_status = (file_info, True)
            for pattern, dst_pattern in zip(self.patterns, self.dst_patterns):  # noqa: B905
                current_file_status = self._match_pattern(pattern, file_info)
                if current_file_status is not None:
                    file_status = current_file_status
                dst_current_file_status = self._match_pattern(dst_pattern, file_info)
                if dst_current_file_status is not None:
                    file_status = dst_current_file_status
            if file_status[1]:
                yield file_info

    def _match_pattern(
        self, pattern: tuple[str, str], file_info: _AwsFileInfo
    ) -> tuple[_AwsFileInfo, bool] | None:
        os = self._os
        file_status = None
        file_path = file_info.src
        pattern_type = pattern[0]
        if file_info.src_type == "local":
            path_pattern = pattern[1].replace("/", os.sep)
        else:
            path_pattern = pattern[1].replace(os.sep, "/")
        # fnmatch.fnmatch: both strings through os.path.normcase.
        is_match = fnmatch.fnmatchcase(os.path.normcase(file_path), os.path.normcase(path_pattern))
        if is_match and pattern_type == "include":
            file_status = (file_info, True)
        elif is_match and pattern_type == "exclude":
            file_status = (file_info, False)
        return file_status


@dataclass(frozen=True)
class _Entry:
    """One entry as aws sees it (``src`` / ``src_type``) and as the library stamps it."""

    aws: _AwsFileInfo
    relative: str
    full: str
    side: str  # the location the entry was listed from


_PATTERN_CHARS = ("*", "?", "[", "]", "!", "/", _BS, ":", ".", "a", "b", "x", "A", "-")
_NT_PREFIXES = (
    "/",
    _BS,
    "C:",
    "c:",
    "D:",
    f"C:{_BS}",
    "c:/",
    f"D:{_BS}",
    f"{_BS}{_BS}srv{_BS}share{_BS}",
    "//srv/share/",
    f"{_BS}{_BS}SRV{_BS}Share{_BS}",
    f"{_BS}{_BS}srv{_BS}share",
    "//srv/share",
    f"{_BS}{_BS}srv",
    f"{_BS}{_BS}",
    "?:",
    "*:",
    f"?:{_BS}",
    "[cd]:",
    f"{_BS}{_BS}?{_BS}C:{_BS}",
    f"{_BS}{_BS}?{_BS}UNC{_BS}srv{_BS}share{_BS}",
    f"{_BS}base{_BS}",
)
_POSIX_PREFIXES = ("/", "//", "///", "C:", _BS, "/base/")
_NT_LOCAL_BASES = (
    f"C:{_BS}base{_BS}d",
    f"c:{_BS}Base",
    f"C:{_BS}",
    f"{_BS}{_BS}srv{_BS}share{_BS}base",
    f"{_BS}{_BS}srv{_BS}share{_BS}",
)
_POSIX_LOCAL_BASES = ("/base/d", "/", "/srv/a b")
# (typed location, listing prefix): `s3://bkt` is normalized to `s3://bkt/`.
_S3_BASES = (("s3://bkt/pre", "pre/"), ("s3://bkt/", ""), ("s3://bkt/pre/", "pre/"))
_S3_OTHER = ("s3://other/dst", "dst/")
_NT_NAME_CHARS = ("a", "b", "x", "A", ".", "-", " ", "[", "]", "!")
_POSIX_NAME_CHARS = (*_NT_NAME_CHARS, _BS, ":", "*", "?")
_S3_KEY_CHARS = (*_POSIX_NAME_CHARS, "/")
# What a single key's parent - aws's base - is made of: no glob character,
# and no backslash, which ntpath.join would read as the base's separator.
_S3_PLAIN_CHARS = ("a", "b", "x", "A", ".", "-", " ", ":")


def _text(rng: random.Random, chars: tuple[str, ...], low: int, high: int) -> str:
    return "".join(rng.choice(chars) for _ in range(rng.randint(low, high)))


def _local_entries(rng: random.Random, os: _Os, base: str, count: int) -> list[_Entry]:
    name_chars = _NT_NAME_CHARS if os.windows else _POSIX_NAME_CHARS
    entries: list[_Entry] = []
    for _ in range(count):
        parts = [_text(rng, name_chars, 1, 3) for _ in range(rng.randint(1, 3))]
        relative = "/".join(parts)
        joint = "" if base.endswith(os.sep) else os.sep
        native = base + joint + os.sep.join(parts)
        full = native.replace(os.sep, "/")
        entries.append(_Entry(_AwsFileInfo(native, "local"), relative, full, base))
    return entries


def _s3_entries(rng: random.Random, location: str, prefix: str, count: int) -> list[_Entry]:
    bucket = location[5:].split("/", 1)[0]
    entries: list[_Entry] = []
    for _ in range(count):
        relative = _text(rng, _S3_KEY_CHARS, 1, 6)
        full = f"{bucket}/{prefix}{relative}"
        entries.append(_Entry(_AwsFileInfo(full, "s3"), relative, full, location))
    return entries


def _mutate(rng: random.Random, text: str, os: _Os) -> str:
    """Turn a path into a pattern that may still match it (or a neighbour)."""
    chars = list(text)
    for _ in range(rng.randint(0, 3)):
        if not chars:
            break
        at = rng.randrange(len(chars))
        roll = rng.random()
        if roll < 0.3:
            chars[at] = "?"
        elif roll < 0.55:
            del chars[at : at + rng.randint(0, 4)]
            chars.insert(at, "*")
        elif roll < 0.7:
            chars[at] = f"[{chars[at]}x]"
        elif roll < 0.8:
            chars[at] = "[!a]"
        elif roll < 0.9 and os.windows:
            chars[at] = chars[at].upper()
        else:
            chars[at] = rng.choice(_PATTERN_CHARS)
    pattern = "".join(chars)
    if os.windows and rng.random() < 0.3:
        pattern = pattern.replace(_BS, "/") if rng.random() < 0.5 else pattern.replace("/", _BS)
    return pattern


def _pattern(rng: random.Random, os: _Os, entries: list[_Entry]) -> str:
    roll = rng.random()
    if roll < 0.35 and entries:
        # From an entry's R: a relative pattern, or a drive form around it.
        pattern = _mutate(rng, rng.choice(entries).relative, os)
        if rng.random() < 0.3:
            pattern = rng.choice(_NT_PREFIXES if os.windows else _POSIX_PREFIXES) + pattern
        return pattern
    if roll < 0.55 and entries:
        # From an entry's full path: absolute, or with its drive or root cut off.
        full = rng.choice(entries).aws.src
        if os.windows:
            drive, root, rest = globsieve._splitroot(full)
            full = rng.choice((full, root + rest, drive + rest, drive + root + rest))
        return _mutate(rng, full, os)
    body = _text(rng, _PATTERN_CHARS, 0, 5)
    if roll < 0.7:
        return body
    if roll < 0.9:
        return rng.choice(_NT_PREFIXES if os.windows else _POSIX_PREFIXES) + body
    # An absolute pattern under a real base, sometimes respelled.
    base = rng.choice(_NT_LOCAL_BASES if os.windows else _POSIX_LOCAL_BASES)
    if os.windows and rng.random() < 0.5:
        base = base.upper().replace(_BS, "/")
    joint = "" if base.endswith(("/", _BS)) else os.sep
    return base + joint + body


def _drive_dependent(pattern: str, os: _Os) -> bool:
    if not os.windows:
        return False
    drive, root, _rest = globsieve._splitroot(pattern)
    return bool(root) != bool(drive)


@dataclass(frozen=True)
class _Operation:
    src: str
    dest: str
    dir_op: bool
    entries: list[_Entry]


def _operations(rng: random.Random, os: _Os) -> list[_Operation]:
    """One of each shape aws runs a filter in, over random bases and entries."""
    local_bases = _NT_LOCAL_BASES if os.windows else _POSIX_LOCAL_BASES
    local = rng.choice(local_bases)
    s3, prefix = rng.choice(_S3_BASES)
    other, other_prefix = _S3_OTHER
    local_side = _local_entries(rng, os, local, 4)
    s3_side = _s3_entries(rng, s3, prefix, 4)
    other_side = _s3_entries(rng, other, other_prefix, 2)
    # Single-object sources: a local file, an S3 key (its R is the key below
    # aws's parent, as the library's single-object view stamps it).
    file_name = _text(rng, _NT_NAME_CHARS, 1, 3)
    joint = "" if local.endswith(os.sep) else os.sep
    file_native = local + joint + file_name
    single_file = _Entry(
        _AwsFileInfo(file_native, "local"), file_name, file_native.replace(os.sep, "/"), "file"
    )
    components = [_text(rng, _S3_PLAIN_CHARS, 0, 2) for _ in range(rng.randint(0, 2))]
    if rng.random() < 0.2:
        # A folder marker: the key is its own base.
        key = "/".join([*components, _text(rng, _S3_PLAIN_CHARS, 1, 2)]) + "/"
    else:
        key = "/".join([*components, _text(rng, _POSIX_NAME_CHARS, 1, 3)])
    single_key = _Entry(
        _AwsFileInfo(f"bkt/{key}", "s3"),
        key[len(single_object_filter_root(key)) :],
        f"bkt/{key}",
        "key",
    )
    dest_file = local + joint + "k"
    return [
        _Operation(local, s3, True, local_side + s3_side),  # sync / cp --recursive up
        _Operation(s3, local, True, s3_side + local_side),  # sync / cp --recursive down
        _Operation(s3, other, True, s3_side + other_side),  # s3 -> s3
        _Operation(s3, s3, True, s3_side),  # rm --recursive
        _Operation(file_native, "s3://bkt/k", False, [single_file]),  # cp file s3://...
        _Operation(f"s3://bkt/{key}", dest_file, False, [single_key]),  # cp s3://... file
        _Operation(f"s3://bkt/{key}", f"s3://bkt/{key}", False, [single_key]),  # rm s3://...
    ]


class TestAgainstAwsFilter:
    """Seeded fuzz against aws-cli's ``Filter``, on both host models.

    The bases hold no glob characters and do not nest, so neither deliberate
    difference applies (a base taken literally, nested s3->s3 prefixes). Each
    entry's verdict must equal aws's with only its own side's joined
    patterns; with both sides' (aws applies the source's and the
    destination's to every entry), it must equal aws's too, except where a
    Windows rooted or drive-relative pattern joined onto the S3 side - which
    has no drive, so it keeps the pattern as written - also reaches the
    local side's entries. That cross-side reach is the same one the nested
    s3->s3 case shows, and it is not reproduced; the test counts it.
    """

    @pytest.mark.parametrize(("os_name", "seed"), [("posix", 20261020), ("nt", 20261021)])
    def test_verdicts_equal_aws(
        self, os_name: str, seed: int, request: pytest.FixtureRequest
    ) -> None:
        os = _NT_OS if os_name == "nt" else _POSIX_OS
        request.getfixturevalue("windows" if os.windows else "posix")
        rng = random.Random(seed)
        compared = crossed = 0
        for _ in range(1500):
            operations = _operations(rng, os)
            pool = [entry for op in operations for entry in op.entries]
            rules: list[Exclude | Include] = []
            flat: list[tuple[str, str]] = []
            for _ in range(rng.randint(1, 4)):
                patterns = tuple(_pattern(rng, os, pool) for _ in range(rng.choice((1, 1, 1, 2))))
                kind = "exclude" if rng.random() < 0.6 else "include"
                rules.append(Exclude(*patterns) if kind == "exclude" else Include(*patterns))
                flat.extend((f"--{kind}", p) for p in patterns)
            keep = globsieve.compile(rules)
            for op in operations:
                both = _aws_create_filter(
                    {"filters": flat, "src": op.src, "dest": op.dest, "dir_op": op.dir_op}, os
                )
                for entry in op.entries:
                    own_location = op.src if entry.side in ("file", "key") else entry.side
                    own = _aws_create_filter(
                        {
                            "filters": flat,
                            "src": own_location,
                            "dest": own_location,
                            "dir_op": op.dir_op,
                        },
                        os,
                    )
                    ours = keep.included(entry.relative, entry.full)
                    context = f"rules={rules!r} op={op.src!r}->{op.dest!r} entry={entry!r}"
                    assert ours is bool(list(own.call([entry.aws]))), context
                    aws = bool(list(both.call([entry.aws])))
                    compared += 1
                    if ours is not aws:
                        crossed += 1
                        assert entry.aws.src_type == "local", context
                        assert any(_drive_dependent(p, os) for _kind, p in flat), context
        assert compared > 30000
        # On POSIX an absolute pattern joins alike onto both bases, and a
        # relative one keeps its base's literal prefix: nothing crosses.
        if not os.windows:
            assert crossed == 0
