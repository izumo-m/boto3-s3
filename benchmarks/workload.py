"""Synthetic workload generation shared by both benchmark modes.

The key layout is one deterministic function (`rel_paths`) consumed by local
tree generation, S3 seeding, and the in-process listing stub, so a scenario's
local side, remote side, and stubbed listings always describe the same file
set. Names are zero-padded, which keeps lexicographic S3 listing order equal
to generation order (the sync merge-join's sorted-input assumption).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

# 8 MiB write chunks for large-file generation: matches the transfer chunk
# size order of magnitude without holding a big buffer per call.
_WRITE_CHUNK = 8 * 1024 * 1024


def rel_paths(count: int, fanout: int = 256) -> list[str]:
    """The canonical relative paths of a *count*-file workload.

    Files are spread over subdirectories *fanout* at a time so a large corpus
    never puts tens of thousands of entries in one directory (which would
    skew local-enumeration timings toward one pathological directory shape).
    """
    return [f"d{i // fanout:04d}/f{i:06d}.bin" for i in range(count)]


def generate_tree(
    root: Path, count: int, size: int, *, fanout: int = 256, mtime: float | None = None
) -> None:
    """Materialize the `rel_paths` workload under *root*, *size* bytes per file.

    *mtime* (when given) is stamped on every file. Sync no-op scenarios pass a
    timestamp safely in the past: S3's LastModified is whole-second while
    local mtimes are sub-second, so a tree generated in the same second it is
    seeded could read as *newer* than its uploaded copy and turn the intended
    no-op into a full re-upload.
    """
    body = b"x" * size
    seen_dirs: set[Path] = set()
    for rel in rel_paths(count, fanout):
        path = root / rel
        parent = path.parent
        if parent not in seen_dirs:
            parent.mkdir(parents=True, exist_ok=True)
            seen_dirs.add(parent)
        path.write_bytes(body)
        if mtime is not None:
            os.utime(path, (mtime, mtime))


def generate_file(path: Path, size: int) -> None:
    """Write one *size*-byte file (the large-transfer payload)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    chunk = b"x" * min(size, _WRITE_CHUNK)
    with path.open("wb") as handle:
        remaining = size
        while remaining > 0:
            handle.write(chunk[: min(remaining, len(chunk))])
            remaining -= len(chunk)


def seed_prefix(
    client: Any,
    bucket: str,
    prefix: str,
    count: int,
    size: int,
    *,
    fanout: int = 256,
    workers: int = 32,
) -> None:
    """Seed ``prefix + rel_paths(...)`` objects of *size* bytes, in parallel.

    A plain serial put loop is too slow at benchmark corpus sizes (10k
    objects); a thread pool over the shared client keeps setup in seconds.
    Any put failure propagates - a partially seeded corpus must not go on to
    be measured.
    """
    body = b"x" * size
    keys = [prefix + rel for rel in rel_paths(count, fanout)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(client.put_object, Bucket=bucket, Key=key, Body=body) for key in keys
        ]
        for future in futures:
            future.result()


def keys_for(prefix: str, count: int, fanout: int = 256) -> Sequence[str]:
    """The full object keys of a seeded/stubbed corpus (prefix + canonical paths)."""
    return [prefix + rel for rel in rel_paths(count, fanout)]
