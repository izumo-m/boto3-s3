"""``--exclude`` / ``--include`` handling shared by rm / cp / mv / sync.

aws-cli evaluates the two options as ONE ordered rule list (its
``AppendFilter`` action appends both to a shared ``filters`` dest) with
last-match-wins semantics, so the interleaved command-line order is
significant: ``--exclude '*' --include '*.txt'`` keeps only ``.txt`` while
the reverse keeps nothing. :class:`AppendFilterAction` preserves that order;
:func:`build_filter` resolves the patterns against the operation's root the
way aws-cli's ``filters.py`` joins them, compiles a
:class:`boto3_s3.globsieve` matcher, and wraps it as the ``FileFilter``
``S3.rm`` / ``cp`` / ``mv`` / ``sync`` consume (matching ``info.compare_key``).
"""

from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING, Any

from boto3_s3 import GlobPattern, globsieve
from boto3_s3.globsieve import Matcher, PatternKind

if TYPE_CHECKING:
    from boto3_s3.types import FileFilter, FileInfo


class _CaseFoldMatcher:
    """Lower-case keys before matching - the Windows case-insensitive wrapper.

    aws-cli matches with ``fnmatch.fnmatch``, which ``os.path.normcase``s both
    the path and the pattern; on Windows that lower-cases, so the filter is
    case-insensitive there (overview.md section 3: case sensitivity is matched to
    aws-cli per OS). The library matchers stay byte-exact (the permissive
    building block); this CLI-layer wrapper folds the key at match time while
    :func:`compile_for_root` lower-cases the patterns at compile time.
    """

    def __init__(self, matcher: Matcher) -> None:
        self._matcher = matcher

    def included(self, key: str) -> bool:
        return self._matcher.included(key.lower())


class AppendFilterAction(argparse.Action):
    """Append ``--exclude`` / ``--include`` to one ordered ``filters`` list.

    The aws-cli ``AppendFilter`` equivalent: both options share ``dest``
    (``filters``) so the rule order is exactly the command-line order.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Any,
        option_string: str | None = None,
    ) -> None:
        kind = PatternKind.EXCLUDE if option_string == "--exclude" else PatternKind.INCLUDE
        items: list[GlobPattern] = getattr(namespace, self.dest, None) or []
        items.append(GlobPattern(kind, str(values)))
        setattr(namespace, self.dest, items)


def _as_file_filter(matcher: Matcher) -> FileFilter:
    """Wrap a compiled matcher as the ``FileFilter`` the operations consume.

    The operation stamps ``info.compare_key`` (the root-relative key) before
    consulting the filter, so the wrapper matches that key. It is always set in
    a filter context; ``None`` would mean the filter was misapplied, so fail
    loudly rather than silently matching the full key.
    """

    def keep(info: FileInfo) -> bool:
        key = info.compare_key
        if key is None:
            raise ValueError("filter consulted without a stamped compare_key")
        return matcher.included(key)

    return keep


def compile_for_root(patterns: list[GlobPattern] | None, *, root: str) -> FileFilter | None:
    """Compile ordered CLI patterns into a ``FileFilter`` rooted at ``root``.

    aws-cli joins each pattern onto the source root (``filters.create_filter``)
    and fnmatches the joined form against the full source path; the library
    matchers are fed root-relative keys instead, so each pattern is translated
    to the equivalent relative form - a pattern anchored outside the root can
    never match and is dropped, exactly like its aws-cli joined form never
    would. ``rm`` passes :func:`boto3_s3.rm_filter_root`; ``cp`` passes the
    plan's ``filter_root`` (local roots included - ``translate`` normalizes
    separators).
    """
    if not patterns:
        return None
    # On Windows aws-cli's fnmatch normcases both the pattern and the key, so the
    # filter is case-insensitive. Lower-case the patterns at compile time and the
    # keys at match time (via _CaseFoldMatcher) to reproduce that; on POSIX
    # os.name != "nt", so matching stays byte-exact.
    fold = os.name == "nt"
    translated: list[GlobPattern] = []
    for pattern in patterns:
        relative = globsieve.translate_pattern_for_root(pattern.pattern, root)
        if relative is not None:
            translated.append(GlobPattern(pattern.kind, relative.lower() if fold else relative))
    matcher = globsieve.compile(translated)
    return _as_file_filter(_CaseFoldMatcher(matcher) if fold else matcher)


def build_filter(
    patterns: list[GlobPattern] | None,
    *,
    key: str,
    recursive: bool,
) -> FileFilter | None:
    """Compile ordered CLI patterns into the ``FileFilter`` ``S3.rm`` consumes.

    The bucket segment cancels out of the relativization, so the root here is
    just :func:`rm_filter_root` of the key (see :func:`compile_for_root`).
    """
    if not patterns:
        return None
    # Deferred: rm_filter_root lives in boto3_s3.s3, whose import chain
    # reaches botocore; the parse path needs only the pure globsieve imports
    # above (import contract, docs/imports.md).
    from boto3_s3 import rm_filter_root

    return compile_for_root(patterns, root=rm_filter_root(key, recursive=recursive))
