"""A local location at the filesystem root, its walk staged.

aws-cli walks a recursive local side from ``local_format``'s ``abspath`` plus a
separator, which doubles a filesystem root's own (``//bin/x``, ``D:\\\\x``), and
joins ``--exclude`` / ``--include`` patterns onto ``abspath(path)``, which
already ends in it - so a filter there matches each entry with a leading
``/``. Walking the real root is out of the question in a test, so this storage
is the root by its path and hands out staged entries keyed the way the
library's walk keys them there.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from typing import ClassVar

from boto3_s3.localstorage import LocalStorage
from boto3_s3.types import FileKind, LocalFileInfo, ScanOptions
from tests.utils.fakes3 import MTIME


class RootTree(LocalStorage):
    """`LocalStorage` at the filesystem root whose walk yields ``relatives``.

    ``Storage.scan`` applies the scan's filter (``scan_pages_filters`` is
    off), so what a test's filter sees is what the operation hands it.
    """

    scan_pages_filters: ClassVar[bool] = False

    def __init__(self, *relatives: str) -> None:
        super().__init__(os.path.abspath(os.sep))
        self._relatives = sorted(relatives)

    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[LocalFileInfo]]:
        del options
        top = self.abspath + os.sep
        yield [
            LocalFileInfo(
                key=(top + relative.replace("/", os.sep)).replace(os.sep, "/"),
                compare_key=relative,
                kind=FileKind.FILE,
                size=1,
                mtime=MTIME,
            )
            for relative in self._relatives
        ]
