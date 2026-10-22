r"""aws-cli's ``--exclude`` / ``--include`` matching, compiled into a ``FileFilter``.

The rules are ``Exclude(*patterns)`` and ``Include(*patterns)`` values, and
``compile`` turns an ordered list of them into a ``GlobFilter``::

    from boto3_s3 import Exclude, Include, globsieve

    web = globsieve.compile([Exclude("*"), Include("*.html", "*.css")])
    s3.sync("./site", "s3://bkt/site", filter=web)
    s3.cp("./b", "s3://bkt/b/", recursive=True, filter=lambda f: web(f) and f.size < 10**8)

A ``GlobFilter`` decides like aws-cli's ``Filter``: the patterns apply in
order, the last one that matches decides, and an entry that no pattern
matches is included. A pattern is Python's ``fnmatch``: ``*`` and ``?`` match
``/`` too, ``[seq]`` is a character class, there is no escape character, and
the whole path must match.

aws joins every pattern onto its side's base (its ``rootdir``) with
``os.path.join`` and matches the entry's full path against the result. The
verdict depends on three values of the entry alone, so the compiled filter
needs no base and works for every operation, on both sides of a ``sync`` and
inside the caller's own predicate:

- R, the entry's path below aws's base: its ``compare_key``. The library
  stamps it so on every entry it hands to a filter.
- F, the full path aws matches: a ``LocalFileInfo``'s ``key`` (the absolute
  path), ``bucket/key`` for an ``S3FileInfo``, none for any other entry.
- D, the drive or UNC share of F in its native spelling (Windows only; ``""``
  on the S3 side).

At compile time each pattern is split into drive, root and rest with a port
of Python 3.14's ``ntpath.splitroot`` (the Python aws-cli bundles), so the
result does not depend on the Python running here. Where the join keeps the
base, the pattern is matched against R; where it drops the base, against F:

- relative (no drive, no root): against R. The base is taken literally,
  where aws reads glob characters in it (a deliberate difference);
- POSIX ``/x``, Windows ``C:\x`` and ``\\server\share\x``: against F;
- Windows ``\x`` (a root without a drive): ``D + \x`` against F;
- Windows ``C:x`` (a drive without a root): against R as ``x`` when the drive
  is D, compared case-insensitively as ``ntpath.join`` compares; otherwise as
  written against F (so on Windows ``*:*`` matches every drive-letter path,
  every S3 key that holds a ``:``, and every key under an access point or
  Outposts ARN, whose ``bucket/key`` starts with ``arn:`` - as on aws). A
  bare ``C:`` on drive C: is the empty pattern.

aws matches both strings through ``normcase``: byte-exact on POSIX, where
``\`` is an ordinary character; on Windows lower-cased by the operating
system's own table and with ``/`` turned into ``\``, so the two separators are
one, in an S3 key as in a pattern (``normcase``).

The rules of one kind of match are compiled into one regular expression,
latest rule first, so an entry costs one regex call (two when the list has a
pattern matched against F), plus one ``normcase`` call per string on Windows.
A ``GlobFilter`` is immutable once compiled, so one instance serves every
thread of an operation and any number of operations.

The module is stdlib-only apart from the pure ``boto3_s3.types`` and
``boto3_s3.exceptions``; everything in ``__all__`` is public, and
``Exclude`` / ``Include`` / ``GlobFilter`` are also exported from the package
root.
"""

from __future__ import annotations

import fnmatch
import ntpath
import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from boto3_s3.exceptions import ValidationError
from boto3_s3.types import LocalFileInfo, S3FileInfo

if TYPE_CHECKING:
    from boto3_s3.s3storage import S3Storage
    from boto3_s3.types import FileInfo

__all__ = [
    "Exclude",
    "GlobFilter",
    "Include",
    "compile",
    "normcase",
]

# The host model `compile` binds a filter to: Windows' path rules and fold, or
# POSIX's. Read when a filter is compiled (and by `normcase` when called), so a
# test can simulate the other family by replacing it.
_WINDOWS: bool = os.name == "nt"


# ----- rules ------------------------------------------------------------------


@dataclass(frozen=True, init=False, repr=False)
class _Rule:
    """One ``--exclude`` or ``--include`` option: its patterns, in order.

    ``patterns`` is the tuple the constructor was given. Each pattern is a rule
    of its own, at its own position in the list; a rule with no pattern
    changes nothing. A pattern that is not a ``str`` (a list passed whole, say)
    raises ``ValidationError`` here. Two values are equal when they are of
    one kind with the same patterns; they are hashable.
    """

    patterns: tuple[str, ...]

    def __init__(self, *patterns: str) -> None:
        for pattern in patterns:
            if not isinstance(pattern, str):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise ValidationError(
                    f"{type(self).__name__} takes patterns as strings, got {type(pattern).__name__}"
                )
        object.__setattr__(self, "patterns", patterns)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({', '.join(map(repr, self.patterns))})"


class Exclude(_Rule):
    """``--exclude``: an entry the last matching pattern belongs to is left out."""


class Include(_Rule):
    """``--include``: an entry the last matching pattern belongs to is kept."""


# ----- normcase -----------------------------------------------------------------


if sys.platform == "win32":
    import _winapi
    import ctypes
    import functools

    # Whether ``ntpath.normcase`` makes the system call itself: it does where
    # ``_winapi`` has it (3.11, and 3.10 from a patch release on), and an
    # early 3.10 release lower-cases with ``str.lower`` instead.
    _NTPATH_FOLDS = hasattr(_winapi, "LCMapStringEx")

    @functools.lru_cache(maxsize=1)
    def _lcmap_string_ex() -> Any:
        """``kernel32.LCMapStringEx``, bound once (the binding costs more than the call)."""
        function = ctypes.WinDLL("kernel32", use_last_error=True).LCMapStringEx
        function.argtypes = [
            ctypes.c_wchar_p,  # locale name
            ctypes.c_uint32,  # flags
            ctypes.c_wchar_p,  # source
            ctypes.c_int,  # source length, in UTF-16 units
            ctypes.c_wchar_p,  # destination
            ctypes.c_int,  # destination size
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        function.restype = ctypes.c_int
        return function

    def _lcmap_lower(text: str) -> str:
        """Windows' locale-invariant lower-casing (``LCMapStringEx``), through ctypes.

        What ``ntpath.normcase`` calls where ``_winapi`` has it. The mapping is the
        operating system's one-to-one table, not Unicode's full case mapping:
        it leaves U+0130, the Kelvin sign and U+1E9E alone and has no final
        sigma, where ``str.lower`` does otherwise (447 code points differ on
        Windows 11, measured) - so only the call itself reproduces it, and it
        equals ``ntpath.normcase`` on Python 3.14 for every code point. Being
        one-to-one, it keeps the length (no code point changed it on the host
        measured), so the destination is sized like the source and one call
        does it; should a Windows table ever answer otherwise, the size is
        asked for and the call repeated, the way CPython's own wrapper does.
        """
        if not text:
            return text
        lcmap = _lcmap_string_ex()
        invariant_locale, lowercase = "", 0x100  # LOCALE_NAME_INVARIANT, LCMAP_LOWERCASE
        units = len(text.encode("utf-16-le", "surrogatepass")) // 2
        size = units
        buffer = ctypes.create_unicode_buffer(size)
        written = lcmap(invariant_locale, lowercase, text, units, buffer, size, None, None, None)
        if written <= 0 and ctypes.get_last_error() == 122:  # ERROR_INSUFFICIENT_BUFFER
            size = lcmap(invariant_locale, lowercase, text, units, None, 0, None, None, None)
            if size > 0:
                buffer = ctypes.create_unicode_buffer(size)
                written = lcmap(
                    invariant_locale, lowercase, text, units, buffer, size, None, None, None
                )
        if written <= 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.wstring_at(buffer, written)


def _windows_normcase(text: str) -> str:
    r"""``ntpath.normcase`` as the Python aws-cli bundles runs it on Windows.

    aws-cli's ``fnmatch.fnmatch`` puts the path and the pattern through
    ``os.path.normcase`` before matching. On Windows that turns every ``/``
    into ``\`` - so the two are one separator, in an S3 key that holds a
    literal backslash as much as in a pattern - and lower-cases with Windows'
    own locale-invariant mapping (``LCMapStringEx``), which differs from
    ``str.lower`` for a few hundred code points. Measured against ``aws.exe``
    2.36.40 by real deletion: ``rm --recursive --exclude "a/b"`` keeps the key
    ``a\b``, and ``--exclude "k*"`` does not keep a key starting with the
    Kelvin sign.

    The stdlib function is that mapping wherever ``_winapi`` offers the call
    (Python 3.11, and 3.10 from a patch release on); an early 3.10 release
    still lower-cases with ``str.lower``, so the system call is made directly
    there (a version difference that changes which entries a normal run
    selects, so it is matched; design/overview.md section 3). Drop that branch
    once no supported release lacks the call. Off Windows this is only
    reached by tests that simulate the host, where ``ntpath.normcase`` falls
    back to ``str.lower`` itself. The engine relies on the fold working
    character by character, which the Windows table does.
    """
    if sys.platform == "win32" and not _NTPATH_FOLDS:
        text = text.replace("/", "\\")
        # The two mappings part only outside ASCII, and str.lower is far
        # cheaper than a system call per key.
        return text.lower() if text.isascii() else _lcmap_lower(text)
    return ntpath.normcase(text)


def normcase(text: str) -> str:
    r"""aws-cli's matching fold on this host: what its ``fnmatch`` does to both strings.

    The identity on POSIX. On Windows, ``/`` becomes ``\`` and the text is
    lower-cased with the operating system's locale-invariant table (see
    ``_windows_normcase``). A ``GlobFilter`` applies it itself; it is public
    for a caller that reasons about patterns and paths on its own, such as
    deciding that no pattern can reach below a directory.
    """
    return _windows_normcase(text) if _WINDOWS else text


# ----- splitroot ----------------------------------------------------------------


def _splitroot(path: str) -> tuple[str, str, str]:
    r"""Python 3.14's ``ntpath.splitroot``, ported so every host Python splits alike.

    Splits into drive, root and the rest, keeping the input's spelling:
    ``C:\x`` -> ``("C:", "\\", "x")``, ``C:x`` -> ``("C:", "", "x")``, ``\x``
    -> ``("", "\\", "x")``, ``\\server\share\x`` -> ``("\\\\server\\share",
    "\\", "x")``, and an incomplete UNC drive (``\\server``) is all drive.
    Both separators are recognised. Python 3.10 and 3.11 split some of these
    forms differently, and aws-cli joins patterns with the 3.14 rules.
    """
    normp = path.replace("/", "\\")
    if normp[:1] == "\\":
        if normp[1:2] == "\\":
            # UNC drives, e.g. \\server\share or \\?\UNC\server\share; device
            # drives, e.g. \\.\device or \\?\device.
            start = 8 if normp[:8].upper() == "\\\\?\\UNC\\" else 2
            index = normp.find("\\", start)
            if index == -1:
                return path, "", ""
            index2 = normp.find("\\", index + 1)
            if index2 == -1:
                return path, "", ""
            return path[:index2], path[index2 : index2 + 1], path[index2 + 1 :]
        # A root without a drive, e.g. \Windows.
        return "", path[:1], path[1:]
    if normp[1:2] == ":":
        if normp[2:3] == "\\":
            # An absolute drive-letter path, e.g. X:\Windows.
            return path[:2], path[2:3], path[3:]
        # A drive-relative path, e.g. X:Windows.
        return path[:2], "", path[2:]
    return "", "", path


# ----- compiled filter --------------------------------------------------------------


# How a pattern joins onto a base (see the module docstring).
_RELATIVE = 0  # against R
_FULL = 1  # the join drops the base: against F
_ROOTED = 2  # Windows `\x`: D + pattern, against F
_DRIVE = 3  # Windows `C:x`: against R on drive C:, else as written against F


class _Pattern(NamedTuple):
    """One pattern of the rule list, classified at compile time.

    ``position`` is its place in the flattened list (a later one wins),
    ``include`` its rule's kind. ``text`` is the pattern ready to translate -
    folded on Windows - for every form but ``_DRIVE``, which keeps its
    ``drive`` and ``rest`` as written: how it joins depends on D.
    """

    position: int
    include: bool
    form: int
    text: str
    drive: str = ""
    rest: str = ""


def _classify(
    position: int, include: bool, pattern: str, fold: Callable[[str], str] | None
) -> _Pattern:
    """How ``pattern`` joins onto a base on this host model (the module docstring's forms).

    ``fold`` is ``None`` for the POSIX model and the Windows fold otherwise.
    """
    if fold is None:
        # posixpath.join drops the base exactly for a leading "/".
        form = _FULL if pattern.startswith("/") else _RELATIVE
        return _Pattern(position, include, form, pattern)
    drive, root, rest = _splitroot(pattern)
    if not drive and not root:
        return _Pattern(position, include, _RELATIVE, fold(pattern))
    if drive and root:
        return _Pattern(position, include, _FULL, fold(pattern))
    if root:
        return _Pattern(position, include, _ROOTED, fold(pattern))
    return _Pattern(position, include, _DRIVE, "", drive, rest)


_Match = Callable[[str], "re.Match[str] | None"]


class _Plan(NamedTuple):
    """The regexes a filter runs for entries on one drive D.

    ``relative`` matches R and ``full`` matches F, each ``None`` when no rule
    takes that form. Each is the alternation of its rules, latest first, and
    every alternative ends in an empty marker group, so the match's
    ``lastindex`` names the rule that decided; ``*_rules`` maps it to that
    rule's ``(index, include)``. ``full_top`` is the latest full-path rule's
    index (-1 when there is none): a relative match at a later index makes
    the full-path regex unnecessary.
    """

    relative: _Match | None
    relative_rules: dict[int | None, tuple[int, bool]]
    full: _Match | None
    full_rules: dict[int | None, tuple[int, bool]]
    full_top: int


# Distinct drives whose plans a filter keeps. An operation meets one or two
# (each side's); a caller handing ``included`` paths from many UNC shares gets
# the rest built per call.
_PLAN_CACHE = 64


def _union(
    alternatives: list[tuple[int, bool, str]],
) -> tuple[_Match | None, dict[int | None, tuple[int, bool]]]:
    """Compile ``(index, include, regex)`` alternatives into one last-match-wins regex."""
    if not alternatives:
        return None, {}
    regex = re.compile(
        "|".join(f"(?:{body})(?P<r{index}>)" for index, _include, body in reversed(alternatives))
    )
    rules: dict[int | None, tuple[int, bool]] = {
        regex.groupindex[f"r{index}"]: (index, include) for index, include, _ in alternatives
    }
    return regex.match, rules


def _plan(patterns: tuple[_Pattern, ...], drive: str, fold: Callable[[str], str] | None) -> _Plan:
    """Resolve the drive-dependent forms for entries on ``drive`` and compile the plan."""
    relative: list[tuple[int, bool, str]] = []
    full: list[tuple[int, bool, str]] = []
    for p in patterns:
        if p.form == _RELATIVE:
            relative.append((p.position, p.include, fnmatch.translate(p.text)))
        elif p.form == _FULL:
            full.append((p.position, p.include, fnmatch.translate(p.text)))
        else:
            # Only Windows patterns reach here, so `fold` is the Windows fold.
            assert fold is not None
            if p.form == _ROOTED:
                # ntpath.join keeps the base's drive (none on the S3 side) and
                # takes the pattern's root and rest; the drive is the base's,
                # matched literally.
                full.append(
                    (p.position, p.include, re.escape(fold(drive)) + fnmatch.translate(p.text))
                )
            elif p.drive.lower() == drive.lower():
                # The same drive, as ntpath.join compares it: the pattern joins
                # like a relative one. Written in another case, the joined path
                # carries the pattern's spelling of the drive, which the fold
                # makes D's own - or, where Windows' table and str.lower part,
                # a prefix no entry under D has.
                if fold(p.drive) == fold(drive):
                    relative.append((p.position, p.include, fnmatch.translate(fold(p.rest))))
            else:
                # Another drive: the join ignores the base, adding a separator
                # between a UNC drive and the rest.
                sep = "\\" if p.rest and p.drive[-1] not in ":\\/" else ""
                joined = p.drive + sep + p.rest
                full.append((p.position, p.include, fnmatch.translate(fold(joined))))
    relative_match, relative_rules = _union(relative)
    full_match, full_rules = _union(full)
    full_top = full[-1][0] if full else -1
    return _Plan(relative_match, relative_rules, full_match, full_rules, full_top)


class GlobFilter:
    r"""A compiled ``--exclude`` / ``--include`` rule list; a ``FileFilter``.

    Built by ``compile``. Calling it with a ``FileInfo`` returns whether the
    entry is kept, deciding from its R, F and D as the module docstring
    describes: R is ``compare_key``, F is the ``key`` of a ``LocalFileInfo``
    or ``bucket/key`` of an ``S3FileInfo`` (read from its ``storage``), and
    any other entry - or an ``S3FileInfo`` without a ``storage`` - has no F,
    so only patterns matched against R apply to it. An entry whose
    ``compare_key`` is ``None`` is matched by the full-path patterns alone.
    Both values are what the library stamps on every entry it hands to a
    filter; on an entry the caller builds, they are the caller's to stamp.

    ``included(relative, full=None)`` is the same decision on plain strings:
    ``relative`` is R and ``full`` is F (a local absolute path, in either
    separator, or ``bucket/key``), ``None`` where there is none.

    ``rules`` is the rule list as given to ``compile``. The filter is
    immutable and safe to share between threads; it pickles by its rules (a
    ``ProcessPoolExecutor`` worker recompiles it for the same host model).
    """

    __slots__ = ("_fold", "_patterns", "_plans", "_rules", "_static", "_uses_full", "_windows")

    _fold: Callable[[str], str] | None
    _patterns: tuple[_Pattern, ...]
    _plans: dict[str, _Plan]
    _rules: tuple[Exclude | Include, ...]
    _static: _Plan | None
    _uses_full: bool
    _windows: bool

    def __init__(self, rules: Iterable[Exclude | Include]) -> None:
        self._setup(tuple(rules), _WINDOWS)

    def _setup(self, rules: tuple[Exclude | Include, ...], windows: bool) -> None:
        fold = _windows_normcase if windows else None
        patterns: list[_Pattern] = []
        for rule in rules:
            # Fail inside the taxonomy rather than with an AttributeError (a
            # bare string is the likely mistake).
            if not isinstance(rule, (Exclude, Include)):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise ValidationError(
                    f"a glob rule must be Exclude or Include, got {type(rule).__name__}"
                )
            include = isinstance(rule, Include)
            for pattern in rule.patterns:
                patterns.append(_classify(len(patterns), include, pattern, fold))
        self._rules = rules
        self._windows = windows
        self._fold = fold
        self._patterns = tuple(patterns)
        self._uses_full = any(p.form != _RELATIVE for p in patterns)
        # Without a drive-dependent form one plan serves every entry; with
        # one, plans are built per drive on first sight (the S3 side's "" up
        # front).
        drive_dependent = any(p.form in (_ROOTED, _DRIVE) for p in patterns)
        first = _plan(self._patterns, "", fold)
        self._static = None if drive_dependent else first
        self._plans = {"": first} if drive_dependent else {}

    @property
    def rules(self) -> tuple[Exclude | Include, ...]:
        return self._rules

    def __call__(self, info: FileInfo) -> bool:
        full: str | None = None
        if self._uses_full:
            if isinstance(info, LocalFileInfo):
                full = info.key
            elif isinstance(info, S3FileInfo) and info.storage is not None:
                full = f"{cast('S3Storage', info.storage).bucket}/{info.key}"
        return self.included(info.compare_key, full)

    def included(self, relative: str | None, full: str | None = None) -> bool:
        """Whether an entry with path ``relative`` below the base and full path ``full`` is kept."""
        plan = self._static
        if plan is None:
            drive = "" if full is None else _splitroot(full.replace("/", "\\"))[0]
            plan = self._plans.get(drive)
            if plan is None:
                plan = _plan(self._patterns, drive, self._fold)
                if len(self._plans) < _PLAN_CACHE:
                    self._plans[drive] = plan
        fold = self._fold
        best = -1
        verdict = True
        if relative is not None and plan.relative is not None:
            match = plan.relative(relative if fold is None else fold(relative))
            if match is not None:
                best, verdict = plan.relative_rules[match.lastindex]
        if full is not None and plan.full is not None and plan.full_top > best:
            match = plan.full(full if fold is None else fold(full))
            if match is not None:
                index, include = plan.full_rules[match.lastindex]
                if index > best:
                    verdict = include
        return verdict

    def __repr__(self) -> str:
        return f"GlobFilter({list(self._rules)!r})"

    def __reduce__(self) -> tuple[type[GlobFilter], tuple[object, ...], bool]:
        # Rebuilt from its rules, then held to the host model it was compiled
        # for (which differs from the unpickling host's only in a test).
        return GlobFilter, (self._rules,), self._windows

    def __setstate__(self, windows: bool) -> None:
        if windows != self._windows:
            self._setup(self._rules, windows)


def compile(rules: Iterable[Exclude | Include]) -> GlobFilter:
    """Compile an ordered list of ``Exclude`` / ``Include`` rules into a ``GlobFilter``.

    The ``re.compile`` of this module: the patterns are split and translated
    once, here, for this host's path rules (POSIX or Windows). A value that
    is not an ``Exclude`` or ``Include`` raises ``ValidationError``. An empty
    list compiles to a filter that keeps everything.
    """
    return GlobFilter(rules)
