"""``--exclude`` / ``--include`` handling shared by rm / cp / mv / sync.

aws-cli evaluates the two options as ONE ordered rule list (its
``AppendFilter`` action appends both to a shared ``filters`` dest) with
last-match-wins semantics, so the interleaved command-line order is
significant: ``--exclude '*' --include '*.txt'`` keeps only ``.txt`` while
the reverse keeps nothing. ``AppendFilterAction`` preserves that order.

``compile_filter`` is the CLI's equivalent of aws-cli's filter construction
(``create_filter`` + ``Filter``), and its contract is that pair's exact
semantics. Each side of the operation contributes a *base* - the path portion
aws-cli calls the ``rootdir``: ``bucket/key`` for an s3 side, the absolutized
path for a local side, cut back to the parent for a single-file operation
(``_storage_base``). Every pattern is joined onto BOTH bases with
``os.path.join``, and an entry is decided by fnmatching its full path against
the joined patterns in appearance order, last match winning. Two aws
behaviors follow from the joining and are reproduced deliberately (both
verified against aws 2.36.1): the base is glob-interpreted like the rest of
the joined pattern (a ``[1]`` in the operation path is a character class
there, which can defeat ``--exclude '*'``), and both sides' joined patterns
apply to every entry whichever side produced it (nested s3->s3 paths let one
side's pattern bite the other side's entries). Matching is host-aware exactly
like aws-cli: case-insensitive on Windows (its ``fnmatch`` normcases both
sides), byte-exact elsewhere.

As an optimization, ``compile_filter`` delegates to the ``boto3_s3.globsieve``
engine - a relative pattern matched against each entry's ``compare_key`` -
whenever that is provably the same decision function as the joined matching
(``_needs_joined``); the observable behavior is aws-cli's either way.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from boto3_s3 import GlobPattern, globsieve
from boto3_s3.globsieve import Matcher, PatternKind

if TYPE_CHECKING:
    from boto3_s3 import S3Storage
    from boto3_s3.types import FileFilter, FileInfo


class _CaseFoldMatcher:
    """Lower-case keys before matching - the Windows case-insensitive wrapper.

    aws-cli matches with ``fnmatch.fnmatch``, which ``os.path.normcase``s both
    the path and the pattern; on Windows that lower-cases, so the filter is
    case-insensitive there (overview.md section 3: case sensitivity is matched to
    aws-cli per OS). The library matchers stay byte-exact (the permissive
    building block); this CLI-layer wrapper folds both keys at match time while
    ``compile_filter`` lower-cases the patterns at compile time.
    """

    def __init__(self, matcher: Matcher) -> None:
        self._matcher = matcher

    def included(self, compare_key: str, full_key: str | None = None) -> bool:
        return self._matcher.included(
            compare_key.lower(), full_key.lower() if full_key is not None else None
        )


class AppendFilterAction(argparse.Action):
    """Append ``--exclude`` / ``--include`` to one ordered ``filters`` list.

    The aws-cli ``AppendFilter`` equivalent: both options share ``dest``
    (``filters``) so the rule order is exactly the command-line order.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        # The typeshed base signature; nargs is unspecified on both options,
        # so at runtime *values* is always a single str.
        kind = PatternKind.EXCLUDE if option_string == "--exclude" else PatternKind.INCLUDE
        items: list[GlobPattern] = getattr(namespace, self.dest, None) or []
        items.append(GlobPattern(kind, str(values)))
        setattr(namespace, self.dest, items)


def add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the ``--exclude`` / ``--include`` pair (rm / cp / mv / sync).

    One shared ordered ``filters`` dest: the interleaved order carries
    aws-cli's last-match-wins semantics (this module's docstring).
    """
    parser.add_argument("--exclude", action=AppendFilterAction, dest="filters", metavar="PATTERN")
    parser.add_argument("--include", action=AppendFilterAction, dest="filters", metavar="PATTERN")


def _storage_base(storage: object, dir_op: bool) -> str:
    """The base one side contributes for pattern joining (aws-cli's ``rootdir``).

    The equivalent of aws-cli's ``_get_s3_root`` / ``_get_local_root``, read off
    the already-built ``Storage`` instead of re-parsing the command-line string
    (same result: ``S3Storage`` splits its URL with the same ``find_bucket_key``
    port aws-cli uses, and ``LocalStorage.path`` keeps the raw constructor
    form). An s3 side is ``bucket/key``; a local side the absolutized path. A
    single-file operation (``dir_op`` false) uses the parent: everything through
    the final ``/`` on s3 (a ``/``-terminated key stays whole),
    ``os.path.dirname`` locally.
    """
    from boto3_s3 import LocalStorage, S3Storage

    if isinstance(storage, S3Storage):
        key = storage.key
        if not dir_op and not key.endswith("/"):
            key = "/".join(key.split("/")[:-1])
        return f"{storage.bucket}/{key}"
    if isinstance(storage, LocalStorage):
        if dir_op:
            return os.path.abspath(storage.path)
        return os.path.abspath(os.path.dirname(storage.path))
    raise TypeError(f"unsupported filter location: {type(storage).__name__}")


_GLOB_MAGIC = ("*", "?", "[")


def _needs_joined(
    patterns: Sequence[GlobPattern], *, src_base: str, dest_base: str, both_s3: bool
) -> bool:
    """Whether only the joined matching reproduces aws-cli for this command.

    The globsieve delegation (a relative pattern against ``compare_key``) is the
    same decision function as the joined matching exactly when the joined
    pattern's base part consumes an entry's own base literally, and nothing
    else. Each condition below breaks that proof, so any of them routes to
    ``_JoinedFilter``. That engine is correct for every input - a condition may
    fire spuriously without changing behavior, only speed - so the conditions
    err toward firing:

    - a base contains a glob metacharacter: the base part of the joined pattern
      then matches glob-wise, not literally (``[1]`` in the operation path is a
      character class, and an unclosed ``[`` can even form one with pattern
      text, so the mere presence of ``[`` counts);
    - a pattern is root-anchored (absolute): ``os.path.join`` replaces the base,
      so the pattern matches full paths, which ``compare_key`` cannot express;
    - (Windows) a pattern carries a drive: ``ntpath.join`` merges or keeps it
      against the base's drive, which needs the real base;
    - both sides are s3 and one base sits under the other: that side's entries
      also start with the other side's base, so the other side's joined patterns
      can match them across sides (equal bases - rm's ``dest = src`` - are fine:
      the two joined forms coincide).
    """
    if any(c in base for base in (src_base, dest_base) for c in _GLOB_MAGIC):
        return True
    for p in patterns:
        if globsieve.is_anchored(p.pattern):
            return True
        if os.sep == "\\" and os.path.splitdrive(p.pattern)[0]:
            return True
    if both_s3:
        src_dir = src_base if src_base.endswith("/") else f"{src_base}/"
        dest_dir = dest_base if dest_base.endswith("/") else f"{dest_base}/"
        if src_dir != dest_dir and (src_dir.startswith(dest_dir) or dest_dir.startswith(src_dir)):
            return True
    return False


class _JoinedFilter:
    """aws-cli's ``Filter.call`` reproduced: joined patterns against full paths.

    Construction joins every pattern onto both bases with ``os.path.join``
    (exactly aws: an absolute pattern replaces the base, a Windows drive merges
    against the base's) and translates each joined form into one precompiled
    regex per rule - aws re-fnmatches per entry; compiling once is the same
    match, cheaper. The two sides share one rule slot as an alternation: aws
    consults the src-joined then the dst-joined form, but both carry the rule's
    one include/exclude kind, so the pair collapses without changing the
    last-match-wins outcome.

    Matching happens in the ``/``-folded key space the CLI already uses (aws's
    per-side ``os.sep`` rewrite, collapsed the way ``globsieve`` does), against
    the entry's full path in aws's form: ``bucket/key`` for an s3 entry (the
    bucket read from its stamped ``storage``), the absolute ``/``-folded path
    for a local one. On Windows the joined pattern and the key are both
    lower-cased (aws normcases). A hand-built ``FileInfo`` without a stamped
    ``storage`` falls back to its bare ``key``.
    """

    def __init__(self, patterns: Sequence[GlobPattern], bases: Sequence[str], fold: bool) -> None:
        from boto3_s3 import S3Storage

        self._s3_storage: type[S3Storage] = S3Storage
        self._fold = fold
        items: list[tuple[PatternKind, re.Pattern[str]]] = []
        for p in patterns:
            branches: list[str] = []
            for base in dict.fromkeys(bases):
                joined = os.path.join(base, p.pattern).replace(os.sep, "/")
                if fold:
                    joined = joined.lower()
                branches.append(f"(?:{fnmatch.translate(joined)})")
            items.append((p.kind, re.compile("|".join(branches))))
        self._items = items

    def __call__(self, info: FileInfo) -> bool:
        storage = info.storage
        if isinstance(storage, self._s3_storage):
            path = f"{storage.bucket}/{info.key}"
        else:
            path = info.key
        if self._fold:
            path = path.lower()
        included = True
        for kind, regex in self._items:
            if regex.match(path) is not None:
                included = kind is PatternKind.INCLUDE
        return included


def compile_filter(
    patterns: list[GlobPattern] | None, *, src: object, dest: object, dir_op: bool
) -> FileFilter | None:
    """Compile ordered ``--exclude`` / ``--include`` patterns into a ``FileFilter``.

    The ``create_filter`` equivalent: ``src`` / ``dest`` are the operation's
    already-built ``S3Storage`` / ``LocalStorage`` sides (``rm`` passes its one
    target twice, as aws sets ``dest = src`` for the single-path commands) and
    ``dir_op`` is the recursive / sync form; each side's base is derived from
    that pair (``_storage_base``). The semantics are the module docstring's
    joined matching; whether ``_JoinedFilter`` runs it directly or the globsieve
    engine reproduces it is invisible (``_needs_joined``). No patterns means no
    filter, returned as ``None``.
    """
    if not patterns:
        return None
    from boto3_s3 import S3Storage  # deferred like the storages the caller just built

    src_base = _storage_base(src, dir_op)
    dest_base = _storage_base(dest, dir_op)
    fold = os.name == "nt"
    if _needs_joined(
        patterns,
        src_base=src_base,
        dest_base=dest_base,
        both_s3=isinstance(src, S3Storage) and isinstance(dest, S3Storage),
    ):
        return _JoinedFilter(patterns, (src_base, dest_base), fold)
    # On Windows aws-cli's fnmatch normcases both the pattern and the key, so the
    # filter is case-insensitive. Lower-case the patterns at compile time and the
    # keys at match time (via _CaseFoldMatcher) to reproduce that; on POSIX
    # os.name != "nt", so matching stays byte-exact.
    if fold:
        patterns = [GlobPattern(p.kind, p.pattern.lower()) for p in patterns]
    matcher = globsieve.compile(patterns)
    return _as_file_filter(_CaseFoldMatcher(matcher) if fold else matcher)


def _as_file_filter(matcher: Matcher) -> FileFilter:
    """Wrap a compiled globsieve matcher as the ``FileFilter`` the operations consume.

    The delegation target of ``compile_filter``'s equivalence fast path: only
    relative patterns reach it (``_needs_joined`` routes anchored ones to
    ``_JoinedFilter``), and a relative pattern matches the ``info.compare_key``
    that ``Storage.scan`` stamps on each entry. ``info.key`` is still passed as
    the ``Matcher`` protocol's ``full_key`` (inert for relative-only matchers).
    ``compare_key`` is always set in a filter context; ``None`` would mean the
    filter was misapplied, so fail loudly.
    """

    def keep(info: FileInfo) -> bool:
        key = info.compare_key
        if key is None:
            raise ValueError("filter consulted without a stamped compare_key")
        return matcher.included(key, info.key)

    return keep
