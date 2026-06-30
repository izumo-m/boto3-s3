"""``--exclude`` / ``--include`` handling shared by rm / cp / mv / sync.

aws-cli evaluates the two options as ONE ordered rule list (its
``AppendFilter`` action appends both to a shared ``filters`` dest) with
last-match-wins semantics, so the interleaved command-line order is
significant: ``--exclude '*' --include '*.txt'`` keeps only ``.txt`` while
the reverse keeps nothing. :class:`AppendFilterAction` preserves that order;
:func:`compile_filter` compiles a :mod:`boto3_s3.globsieve` matcher and wraps
it as the ``FileFilter`` ``S3.rm`` / ``cp`` / ``mv`` / ``sync`` consume. The
matcher needs no root: a relative pattern matches ``info.compare_key`` and a
root-anchored (absolute) pattern is anchored against ``info.key`` (the full
path) at match time, which is what lets the one filter prune ``sync``'s two
sides per-side the way aws-cli's per-root joining does.
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
    building block); this CLI-layer wrapper folds both keys at match time while
    :func:`compile_filter` lower-cases the patterns at compile time.
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
        values: str | Any,
        option_string: str | None = None,
    ) -> None:
        kind = PatternKind.EXCLUDE if option_string == "--exclude" else PatternKind.INCLUDE
        items: list[GlobPattern] = getattr(namespace, self.dest, None) or []
        items.append(GlobPattern(kind, str(values)))
        setattr(namespace, self.dest, items)


def _as_file_filter(matcher: Matcher) -> FileFilter:
    """Wrap a compiled matcher as the ``FileFilter`` the operations consume.

    ``Storage.scan`` stamps ``info.compare_key`` (the root-relative key) on each
    entry; the wrapper passes it plus ``info.key`` (the full path, the anchor for
    an absolute pattern). ``compare_key`` is always set in a filter context;
    ``None`` would mean the filter was misapplied, so fail loudly.
    """

    def keep(info: FileInfo) -> bool:
        key = info.compare_key
        if key is None:
            raise ValueError("filter consulted without a stamped compare_key")
        return matcher.included(key, info.key)

    return keep


def compile_filter(patterns: list[GlobPattern] | None) -> FileFilter | None:
    """Compile ordered CLI ``--exclude`` / ``--include`` patterns into a ``FileFilter``.

    No root is needed: :mod:`boto3_s3.globsieve` matches a relative pattern
    against ``info.compare_key`` and a root-anchored (absolute) one against
    ``info.key`` (joined with the entry's drive / UNC anchor at match time the
    way aws-cli joins each pattern onto the per-side root). The same filter thus
    prunes both ``sync`` sides per-side, and ``rm`` / ``cp`` / ``mv`` (single
    root) need no special casing.
    """
    if not patterns:
        return None
    # On Windows aws-cli's fnmatch normcases both the pattern and the key, so the
    # filter is case-insensitive. Lower-case the patterns at compile time and the
    # keys at match time (via _CaseFoldMatcher) to reproduce that; on POSIX
    # os.name != "nt", so matching stays byte-exact.
    fold = os.name == "nt"
    if fold:
        patterns = [GlobPattern(p.kind, p.pattern.lower()) for p in patterns]
    matcher = globsieve.compile(patterns)
    return _as_file_filter(_CaseFoldMatcher(matcher) if fold else matcher)
