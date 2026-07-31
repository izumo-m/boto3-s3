"""Reference index coverage: ``docs/reference/`` indexes every root export.

The hand-written API reference states one coverage rule: every name importable
from the package root - the contents of ``boto3_s3.__all__`` - has an entry in
the symbol index of ``docs/reference/README.md``. Nothing in the export
machinery forces an author to write that entry, and a page rename leaves the
index pointing at a file that no longer exists, so both halves are pinned here:
the index lists exactly the root exports, and every relative link the page
makes resolves to a real file.

Link fragments (``page.md#anchor``) are not validated. The anchors were checked
against the pages' headings when the index was written; re-deriving them here
would pin a renderer's slugging rules rather than this repo's contract.
"""

from __future__ import annotations

import re
from pathlib import Path

import boto3_s3

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_DIR = REPO_ROOT / "docs" / "reference"
INDEX_PAGE = REFERENCE_DIR / "README.md"

# The heading the symbol index lives under; the entries below it are the
# coverage claim, while links elsewhere on the page point at whole pages.
SYMBOL_INDEX_HEADING = "## Symbol index"

# Inline ``[label](target)`` links, the only link form these pages use.
_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\((?P<target>[^)]+)\)")


def _index_text() -> str:
    return INDEX_PAGE.read_text(encoding="utf-8")


def _symbol_index_section(text: str) -> str:
    """The part of the index page under its symbol-index heading."""
    _, heading, rest = text.partition(SYMBOL_INDEX_HEADING)
    assert heading, f"{INDEX_PAGE} has no {SYMBOL_INDEX_HEADING!r} heading"
    return rest.split("\n## ")[0]


def _indexed_symbols() -> set[str]:
    """Symbol names linked from the index, taken from backticked link labels."""
    section = _symbol_index_section(_index_text())
    return {
        match.group("label").strip("`")
        for match in _LINK_RE.finditer(section)
        if match.group("label").startswith("`")
    }


class TestReferenceIndex:
    def test_every_root_export_is_indexed(self) -> None:
        # A new entry in __all__ without a reference entry fails here.
        missing = sorted(set(boto3_s3.__all__) - _indexed_symbols())
        assert not missing, f"not indexed in {INDEX_PAGE}: {missing}"

    def test_index_lists_only_root_exports(self) -> None:
        # The other direction: an entry left behind by a removed or renamed
        # export would send readers after a symbol they cannot import.
        stale = sorted(_indexed_symbols() - set(boto3_s3.__all__))
        assert not stale, f"indexed in {INDEX_PAGE} but not exported: {stale}"

    def test_every_relative_link_target_exists(self) -> None:
        for match in _LINK_RE.finditer(_index_text()):
            target = match.group("target")
            if target.startswith(("http://", "https://")):
                continue
            path = target.split("#", 1)[0]
            if not path:  # a fragment-only link stays on this page
                continue
            assert (REFERENCE_DIR / path).is_file(), f"{INDEX_PAGE} links to missing file {target}"
