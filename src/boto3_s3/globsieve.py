"""Glob-pattern sieve - last-match-wins include/exclude pattern engine.

Semantics match aws-cli's ``--exclude`` / ``--include`` filters: patterns
evaluated in order, the last pattern that matches decides include vs.
exclude, and an unmatched key is included by default.

Usage (``re``-style)::

    from boto3_s3 import globsieve

    m = globsieve.compile([
        globsieve.GlobPattern.exclude("*"),
        globsieve.GlobPattern.include("*.txt"),
    ])
    m.included("foo.txt")  # True
    m.included("foo.log")  # False

:class:`GlobFilter` is the ergonomic front end built on this engine - a
chainable ``FileFilter`` (``GlobFilter().exclude("*").include("*.txt").compile()``)
passed straight to ``S3.cp`` / ``mv`` / ``rm`` / ``sync`` as ``filter=``; it is
sugar over the same :func:`compile`.

Specialization happens at compile time. :func:`compile` first detects
the *macro shape* of the pattern list (default-deny + includes,
default-allow + excludes, mixed) and picks one of the
:class:`Matcher` implementations. Within each set, the patterns are
partitioned by shape (literal, suffix, prefix, general fnmatch): a
uniform set uses that shape's dedicated :class:`SetMatcher`, and a mixed
set is folded into a :class:`CompositeSet` that ORs one matcher per shape.

Keys are matched verbatim. ``S3.rm`` feeds root-stripped keys (relative
to the operation's effective prefix root), so this module never needs to
know about buckets - ``fnmatch.fnmatch`` is greedy across ``/`` so the
verdict is identical whether the root prefix is included or stripped.
User patterns anchored at the root (aws-cli joins each pattern with the
source root) are converted to the same relative form by
:func:`translate_pattern_for_root`.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from boto3_s3.types import FileInfo

__all__ = [
    "AlwaysExclude",
    "AlwaysInclude",
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
    "translate_pattern_for_root",
]

# ----- pattern definition --------------------------------------------------


class PatternKind(Enum):
    """Whether a :class:`GlobPattern` includes or excludes matching keys."""

    INCLUDE = "include"
    EXCLUDE = "exclude"


@dataclass(frozen=True)
class GlobPattern:
    """One include/exclude rule.

    Use the :meth:`include` / :meth:`exclude` factories or build
    directly. Equality is by ``(kind, pattern)`` - :func:`compile`
    relies on this to recognize catch-all heads such as
    ``GlobPattern.exclude("*")``.
    """

    kind: PatternKind
    pattern: str

    @classmethod
    def include(cls, pattern: str) -> GlobPattern:
        return cls(PatternKind.INCLUDE, pattern)

    @classmethod
    def exclude(cls, pattern: str) -> GlobPattern:
        return cls(PatternKind.EXCLUDE, pattern)


# ----- pattern translation -------------------------------------------------


def translate_pattern_for_root(pattern: str, rootdir: str) -> str | None:
    """Translate a user-supplied glob into a relative-key-equivalent form.

    aws-cli's filter joins each pattern with the source rootdir
    (``os.path.join(rootdir, pattern)``) and matches the result against
    the absolute file path via ``fnmatch``. ``S3.rm`` feeds root-stripped
    relative keys to the matcher, so a user pattern like
    ``--exclude /abs/data/secret/*`` (which aws-cli turns into a literal
    absolute pattern) needs the rootdir prefix stripped to match the
    same set of keys against the relative form. Doing the translation
    once at compile time keeps the per-key fast paths
    (LiteralSet / SuffixSet / PrefixSet / UnionRegex) intact.

    Returns the translated pattern, or ``None`` if the pattern is
    anchored OUTSIDE ``rootdir`` and can therefore never match any
    key under it (caller should drop the entry from the pattern list).
    """
    # Normalise the host separator to ``/``: aws-cli matches a pattern against
    # the source path with ``pattern.replace(os.sep, '/')`` for an s3 source and
    # ``pattern.replace('/', os.sep)`` against os.sep paths for a local one
    # (aws-cli's filters._match_pattern). Folding ``os.sep`` -> ``/`` on both sides
    # reproduces both: on Windows it rewrites ``\`` like the local case, while on
    # POSIX (os.sep == '/') it is a no-op, so a literal backslash in an s3 key
    # survives instead of being wrongly rewritten to ``/``.
    pat = pattern.replace(os.sep, "/")
    root = rootdir.replace(os.sep, "/").rstrip("/")
    if not root:
        return pat

    # Mimic ``os.path.join`` semantics: an absolute right-hand side
    # discards the left-hand rootdir. POSIX-absolute (``/foo``) and
    # Windows drive-rooted (``C:/foo``) both qualify.
    if pat.startswith("/") or _has_drive(pat):
        joined = pat
    else:
        joined = f"{root}/{pat}"

    prefix = f"{root}/"
    if joined.startswith(prefix):
        return joined[len(prefix) :]
    # Anchored outside the rootdir: aws-cli's fnmatch against any file
    # under the rootdir would also miss, so the pattern entry is dead.
    return None


def _has_drive(p: str) -> bool:
    return len(p) >= 2 and p[1] == ":" and p[0].isalpha()


# ----- runtime protocols ---------------------------------------------------


class Matcher(Protocol):
    """Final include/exclude decision for a key.

    A single-method protocol (``included(key) -> bool``); the matchers
    :func:`compile` returns all satisfy it.
    """

    def included(self, key: str) -> bool: ...


class SetMatcher(Protocol):
    """Set-membership test for a uniformly-shaped pattern collection."""

    def matches(self, key: str) -> bool: ...


# ----- decision matchers ---------------------------------------------------


class AlwaysInclude:
    """No patterns - every key is included."""

    def included(self, key: str) -> bool:
        return True


class AlwaysExclude:
    """Catch-all exclude with no includes - nothing is included."""

    def included(self, key: str) -> bool:
        return False


class IncludeOnly:
    """Default-deny: include iff the key matches the wrapped set."""

    def __init__(self, set_matcher: SetMatcher) -> None:
        self.set_matcher = set_matcher

    def included(self, key: str) -> bool:
        return self.set_matcher.matches(key)


class ExcludeOnly:
    """Default-allow: include iff the key does NOT match the wrapped set."""

    def __init__(self, set_matcher: SetMatcher) -> None:
        self.set_matcher = set_matcher

    def included(self, key: str) -> bool:
        return not self.set_matcher.matches(key)


class Sequential:
    """Last-match-wins walk through ``(PatternKind, SetMatcher)`` pairs.

    Default to include. Each pair is consulted in order; a hit
    overwrites the running decision. This is the general-case fallback
    for mixed include/exclude orderings.
    """

    def __init__(self, items: Iterable[tuple[PatternKind, SetMatcher]]) -> None:
        self.items: tuple[tuple[PatternKind, SetMatcher], ...] = tuple(items)

    def included(self, key: str) -> bool:
        included = True
        for kind, sm in self.items:
            if sm.matches(key):
                included = kind is PatternKind.INCLUDE
        return included


# ----- set matchers --------------------------------------------------------


class LiteralSet:
    """Match keys equal to any of a fixed literal set."""

    def __init__(self, paths: Iterable[str]) -> None:
        self.paths: frozenset[str] = frozenset(paths)

    def matches(self, key: str) -> bool:
        return key in self.paths


class SuffixSet:
    """Match keys ending with any of a fixed suffix tuple.

    ``str.endswith`` accepts a tuple in C, so N suffixes test in one
    call.
    """

    def __init__(self, suffixes: Iterable[str]) -> None:
        self.suffixes: tuple[str, ...] = tuple(suffixes)

    def matches(self, key: str) -> bool:
        return key.endswith(self.suffixes)


class PrefixSet:
    """Match keys starting with any of a fixed prefix tuple."""

    def __init__(self, prefixes: Iterable[str]) -> None:
        self.prefixes: tuple[str, ...] = tuple(prefixes)

    def matches(self, key: str) -> bool:
        return key.startswith(self.prefixes)


class UnionRegex:
    """Match keys against the union of fnmatch patterns.

    Patterns are translated to regex via :func:`fnmatch.translate` and
    combined with alternation. ``fnmatch``'s ``*`` is greedy across
    ``/``, so general patterns match anywhere in the key.
    """

    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns: tuple[str, ...] = tuple(patterns)
        self._regex: re.Pattern[str] = re.compile(
            "|".join(f"(?:{fnmatch.translate(p)})" for p in self.patterns)
        )

    def matches(self, key: str) -> bool:
        return self._regex.match(key) is not None


class CompositeSet:
    """Union of shape-partitioned matchers, folded into one predicate.

    A heterogeneous set (e.g. ``*.elc`` + ``elpa/*`` + a literal) has no
    single uniform shape, but each *shape* still has a dedicated fast
    test. :func:`compile` partitions such a set into a literal frozenset,
    a suffix tuple, a prefix tuple, and a leftover :class:`UnionRegex`,
    and this matcher ORs them.

    The OR is folded into one closure built at construction (and bound to
    ``matches``) rather than a Python loop over sub-matcher objects: the
    per-key cost is then a few C-level membership tests (``in`` /
    ``str.startswith`` / ``str.endswith``, each consuming the whole tuple
    in a single call) with no per-sub method dispatch - which is what lets
    it beat the single ``UnionRegex`` it replaces. Prefixes are tested
    first since directory excludes (``dir/*``) dominate real exclude lists,
    so a matching key short-circuits soonest.
    """

    def __init__(
        self,
        literals: Iterable[str],
        suffixes: Iterable[str],
        prefixes: Iterable[str],
        general: Sequence[str],
    ) -> None:
        lits = frozenset(literals)
        sufs = tuple(suffixes)
        prefs = tuple(prefixes)
        # Bind the folded predicate as an instance attribute (not a method):
        # an instance-level callable is invoked directly, so the captured
        # tuples/frozenset are read as closure cells rather than via
        # ``self``-attribute lookups - measurably faster on the per-key path.
        matches: Callable[[str], bool]
        if general:
            regex_matches = UnionRegex(general).matches
            matches = lambda key: (  # noqa: E731
                key.startswith(prefs) or key.endswith(sufs) or key in lits or regex_matches(key)
            )
        else:
            matches = lambda key: (  # noqa: E731
                key.startswith(prefs) or key.endswith(sufs) or key in lits
            )
        self.matches = matches


class _NeverMatch:
    """Empty set - never matches anything (internal)."""

    def matches(self, key: str) -> bool:
        return False


# ----- compilation ---------------------------------------------------------


def compile(patterns: Iterable[GlobPattern]) -> Matcher:
    """Pick the fastest :class:`Matcher` backend for the pattern list.

    Macro shape detection:

    - empty list -> :class:`AlwaysInclude`
    - ``[exclude "*"]`` alone -> :class:`AlwaysExclude`
    - ``[exclude "*", include..., include...]`` -> :class:`IncludeOnly`
    - ``[include "*", exclude..., exclude...]`` -> :class:`ExcludeOnly`
    - all-exclude (no leading catch-all needed) -> :class:`ExcludeOnly`
    - mixed orderings -> :class:`Sequential`

    Within each set, the uniformly-shaped patterns are further
    specialized: literal-only sets use :class:`LiteralSet`, all-``*X``
    sets use :class:`SuffixSet`, all-``X*`` sets use :class:`PrefixSet`,
    and mixed shapes fall back to :class:`UnionRegex`.
    """
    pats = tuple(patterns)
    if not pats:
        return AlwaysInclude()

    head = pats[0]
    rest = pats[1:]

    is_catch_all_exclude = head == GlobPattern.exclude("*")
    is_catch_all_include = head == GlobPattern.include("*")

    if is_catch_all_exclude and not rest:
        return AlwaysExclude()

    if is_catch_all_exclude and all(p.kind is PatternKind.INCLUDE for p in rest):
        return IncludeOnly(_compile_set_matcher([p.pattern for p in rest]))

    if is_catch_all_include and all(p.kind is PatternKind.EXCLUDE for p in rest):
        return ExcludeOnly(_compile_set_matcher([p.pattern for p in rest]))

    if all(p.kind is PatternKind.EXCLUDE for p in pats):
        return ExcludeOnly(_compile_set_matcher([p.pattern for p in pats]))

    return Sequential((p.kind, _compile_one_set(p.pattern)) for p in pats)


def _compile_set_matcher(patterns: Sequence[str]) -> SetMatcher:
    """Pick the fastest :class:`SetMatcher` for a pattern list.

    Patterns are partitioned by shape (literal / ``*X`` suffix / ``X*``
    prefix / general fnmatch). A set that is uniformly one shape uses that
    shape's dedicated matcher (:class:`LiteralSet` / :class:`SuffixSet` /
    :class:`PrefixSet` / :class:`UnionRegex`). A mixed set - the common
    case for a real exclude list (``dir/*`` directory excludes alongside a
    ``*.ext`` suffix and a couple of literals) - is folded into a
    :class:`CompositeSet` that ORs one matcher per present shape, which is
    faster than collapsing everything into one regex.
    """
    literals: list[str] = []
    suffixes: list[str] = []
    prefixes: list[str] = []
    general: list[str] = []
    for p in patterns:
        if _is_literal(p):
            literals.append(p)
        elif _is_pure_suffix(p):
            suffixes.append(p[1:])
        elif _is_pure_prefix(p):
            prefixes.append(p[:-1])
        else:
            general.append(p)

    present = [bucket for bucket in (literals, suffixes, prefixes, general) if bucket]
    if not present:
        return _NeverMatch()
    if len(present) == 1:
        if literals:
            return LiteralSet(literals)
        if suffixes:
            return SuffixSet(suffixes)
        if prefixes:
            return PrefixSet(prefixes)
        return UnionRegex(general)
    return CompositeSet(literals, suffixes, prefixes, general)


def _compile_one_set(pattern: str) -> SetMatcher:
    """Specialize one pattern (used inside :class:`Sequential`)."""
    if _is_literal(pattern):
        return LiteralSet((pattern,))
    if _is_pure_suffix(pattern):
        return SuffixSet((pattern[1:],))
    if _is_pure_prefix(pattern):
        return PrefixSet((pattern[:-1],))
    return UnionRegex((pattern,))


# ----- shape predicates ----------------------------------------------------


_WILDCARDS = ("*", "?", "[")


def _is_literal(pattern: str) -> bool:
    return not any(c in pattern for c in _WILDCARDS)


def _is_pure_suffix(pattern: str) -> bool:
    """``*X`` with no other wildcards in X."""
    if not pattern.startswith("*"):
        return False
    return not any(c in pattern[1:] for c in _WILDCARDS)


def _is_pure_prefix(pattern: str) -> bool:
    """``X*`` with no other wildcards in X."""
    if not pattern.endswith("*"):
        return False
    return not any(c in pattern[:-1] for c in _WILDCARDS)


# ----- ergonomic filter ----------------------------------------------------


class GlobFilter:
    """Fluent ``--exclude`` / ``--include`` builder; itself a ``FileFilter``.

    The ergonomic front end for the engine above: accumulate ordered rules and
    pass the result straight to ``S3.cp`` / ``mv`` / ``rm`` / ``sync`` as
    ``filter=``. It is pure sugar over ``compile([GlobPattern...])`` - the same
    last-match-wins semantics, the same compile-time specialization - exposed as
    a chainable callable::

        from boto3_s3 import GlobFilter

        keep = GlobFilter().exclude("*").include("*.tar.gz").compile()
        s3.cp("./build", "s3://artifacts/", recursive=True, filter=keep)

    :meth:`exclude` / :meth:`include` each append one or more rules and return
    ``self`` so calls chain; finish with :meth:`compile`, which builds the
    underlying matcher eagerly and returns ``self`` - the recommended form, so
    the cost is paid once and the filter reuses cleanly across operations.
    :meth:`compile` is not mandatory: an un-compiled filter compiles lazily on
    first use and re-compiles after a later ``exclude`` / ``include`` (in
    ``sync`` both sides may then race to compile, which is harmless - the
    patterns are read-only and every compilation is equivalent).

    As a ``FileFilter`` it is invoked with a :class:`~boto3_s3.types.FileInfo`
    and matches its ``compare_key`` (the root-relative key the operation stamps
    before consulting the filter). Patterns are matched verbatim against that
    ``/``-form compare key, exactly like :func:`compile`; route them through
    :func:`translate_pattern_for_root` first if they need root anchoring or host
    separator folding.
    """

    __slots__ = ("_compiled", "_patterns")

    def __init__(self) -> None:
        self._patterns: list[GlobPattern] = []
        self._compiled: Matcher | None = None

    def exclude(self, *patterns: str) -> GlobFilter:
        """Append exclude rules (matching keys are dropped) and return ``self``."""
        self._patterns.extend(GlobPattern.exclude(p) for p in patterns)
        self._compiled = None
        return self

    def include(self, *patterns: str) -> GlobFilter:
        """Append include rules (matching keys are kept) and return ``self``."""
        self._patterns.extend(GlobPattern.include(p) for p in patterns)
        self._compiled = None
        return self

    def compile(self) -> GlobFilter:
        """Eagerly compile the accumulated rules and return ``self`` (no freeze)."""
        self._compiled = compile(self._patterns)
        return self

    def __call__(self, info: FileInfo) -> bool:
        key = info.compare_key
        if key is None:
            raise ValueError(
                "GlobFilter matches FileInfo.compare_key, which an operation "
                "stamps before consulting the filter; it is unset here. Apply "
                "the filter through S3.cp / mv / rm / sync rather than calling "
                "it directly."
            )
        compiled = self._compiled
        if compiled is None:
            compiled = self._compiled = compile(self._patterns)
        return compiled.included(key)
