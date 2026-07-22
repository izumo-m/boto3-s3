"""Builders for the content-comparison strategy tests (`test_etagcompare` /
`test_checksumcompare`): local/S3 `FileInfo` sides backed by real files,
assembled into update `SyncPair`s.

The local side carries a basename `compare_key` and, once paired, a
`LocalStorage` rooted at the file's parent - so a strategy that opens
`compare_key` against the pair's storage reaches the real bytes on disk.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from boto3_s3.comparator import SyncPair
from boto3_s3.localstorage import LocalStorage, to_native_path
from boto3_s3.types import FileInfo, LocalFileInfo

if TYPE_CHECKING:
    from pathlib import Path

    from boto3_s3.storage import Storage
    from boto3_s3.types import TransferType


def write_file(tmp_path: Path, data: bytes, name: str = "f") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def native_key(p: Path) -> str:
    """The `/`-separated FileInfo key for a real path (round-trips via
    `to_native_path`)."""
    return str(p).replace(os.sep, "/")


def local_info(key: str, *, size: int | None = None) -> LocalFileInfo:
    """A local side whose `compare_key` is the basename, matching the storage
    `make_pair` roots at the file's parent."""
    return LocalFileInfo(key=key, size=size, compare_key=key.rsplit("/", 1)[-1])


def _storage_for(info: FileInfo) -> Storage | None:
    """A LocalStorage rooted at a local side's parent dir, so `open(compare_key)`
    reaches the real file (the readable side the strategy hashes)."""
    if isinstance(info, LocalFileInfo):
        return LocalStorage(os.path.dirname(to_native_path(info.key)))
    return None


def make_pair(
    transfer_type: TransferType, *, src: FileInfo, dest: FileInfo, compare_key: str
) -> SyncPair:
    """An update pair over `src`/`dest`. The backend rides on each side's
    `FileInfo`: the strategy reads `pair.src.storage` / `pair.dest.storage` to
    open the readable (local) side."""
    src.storage = _storage_for(src)
    dest.storage = _storage_for(dest)
    return SyncPair(compare_key=compare_key, transfer_type=transfer_type, src=src, dest=dest)
