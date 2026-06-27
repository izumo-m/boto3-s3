# Storage backends

`Storage` is the abstraction every `S3` operation works against: `S3.resolve`
turns a path/URI argument into a `Storage`, and the operations only ever talk to
that interface. The built-ins are `S3Storage` (an S3 bucket/key), `LocalStorage`
(a local path), and the `IOStorage` / `StdioStorage` stream wrappers. A **custom
subclass** is one more `Storage`, so `cp` / `mv` / `sync` reach it through the
same code path as the built-ins.

This document is the contract for writing a custom backend. The per-operation
mechanics live with each engine: the byte-transfer "open route" in
[`transfer.md`](./transfer.md) (section 12), the sorted merge-join in
[`sync.md`](./sync.md), and error mapping in [`exceptions.md`](./exceptions.md).

## 1. Scope: a custom side, the other side S3

A custom `Storage` can be **one side of a transfer; the other side is always
S3**. Its bytes move through `open()` while the S3 side keeps riding
`s3transfer`:

- **custom → S3** (`opens3`): the backend is the source; each entry is uploaded
  from its `open("rb")`.
- **S3 → custom** (`s3open`): the backend is the destination; each object is
  downloaded into its `open("wb")` (whose `close()` is the write's commit).

So `cp` / `mv` / `sync` are the only operations a custom backend joins, and never
custom↔custom. The S3-only operations — `ls` / `rm` / `mb` / `rb` / `presign` /
`website` — require an actual `S3Storage` and are not part of this seam. (The
built-in `IOStorage` / `StdioStorage` stream wrappers use the very same seam: a
stream is a degenerate single-entry backend.)

## 2. The contract

Subclass `Storage`, set two class attributes, and implement the methods the
declared capabilities promise:

- **`scheme: ClassVar[str]`** — the backend's path-shape token, anything but
  `"s3"` / `"local"` (it is how the engine and result rendering tell the sides
  apart).
- **`capabilities: ClassVar[StorageCapability]`** — the flag set the backend
  actually supports (section 3).
- **`as_text() -> str`** (and `str(storage)`) — how this side renders in results
  / progress (its canonical path token).
- **`open(key, mode, *, size=None) -> BinaryIO`** — per-object byte I/O. `"rb"`
  returns a readable stream; `"wb"` a writable one whose `close()` commits the
  write. `size` is an optional total-length hint for writes.
- **`scan_pages(options) -> Iterator[Sequence[FileInfo]]`** — enumerate the
  container one page of `FileInfo` at a time (the base `scan()` flattens it and
  applies `options.filter`). Honour `options.sort` when `SORTED_SCAN` is
  declared.
- **`get_fileinfo(key="", *, follow_symlinks=True, on_warning=None) -> FileInfo | None`**
  — the single-entry counterpart of `scan` (a single source, or an existence
  check). `key=""` is the location itself; `None` means "no transferable entry
  here".
- **`delete(info) -> None`** — remove the entry `info` identifies, by
  `info.key`.

Errors raised from these should map to the library taxonomy
([`exceptions.md`](./exceptions.md)); the engine renders their message verbatim.

### Keys: `key` vs `compare_key`

A `FileInfo` carries two keys (see [`glossary.md`](./glossary.md)):

- **`key`** is the entry's full, `/`-separated identifier **in the backend's own
  address space** — what `open` and `delete(info)` act on. A backend chooses its
  own space (`S3Storage`'s `key` is the full bucket key, `LocalStorage`'s an
  absolute path). A typical custom backend rooted at its location uses keys
  relative to that root, so a recursive entry's `key` is its `compare_key` and
  the single location is `""`.
- **`compare_key`** is the same entry **relative to the scan root**: the
  `--include` / `--exclude` matching space and the axis `sync` merge-joins on.
  `scan` must stamp it on every entry.

## 3. Capabilities

`StorageCapability` is a `Flag` the engine pre-checks **before** a transfer, so a
backend that lacks what an operation needs fails fast with a clear error instead
of deep inside the run:

| flag | method | needed for a custom side that is |
|---|---|---|
| `OPEN_READ` | `open(key, "rb")` | an `opens3` source |
| `OPEN_WRITE` | `open(key, "wb")` | an `s3open` destination |
| `GET_FILEINFO` | `get_fileinfo` | a single-entry source / existence check |
| `SCAN` | `scan` / `scan_pages` | a recursive (multi-entry) side |
| `SORTED_SCAN` | byte-ordered `scan` (`ScanOptions(sort=True)`) | **any `sync`** side |
| `DELETE` | `delete(info)` | an `mv` source / a `sync --delete` destination |

The reading members form a lattice: `SORTED_SCAN` implies `SCAN` implies
`GET_FILEINFO`. `sync`'s merge-join walks both listings in UTF-8 byte order, so a
custom `sync` side **must** declare `SORTED_SCAN` — an unsorted listing would
manufacture phantom pairs and, with `--delete`, corrupt the destination. The
exact per-route gates are in [`sync.md`](./sync.md) / [`transfer.md`](./transfer.md).

## 4. Example

A minimal in-memory backend — a `dict[str, bytes]` keyed by `compare_key`
(root-relative), so a single entry uses `""`:

```python
import io
from typing import BinaryIO, ClassVar

from boto3_s3 import S3, FileInfo, ScanOptions, Storage, StorageCapability

class _Committing(io.BytesIO):           # a "wb" handle whose close() commits the write
    def __init__(self, store, key):
        super().__init__()
        self._store, self._key = store, key
    def close(self):
        self._store[self._key] = self.getvalue()
        super().close()

class DictStorage(Storage):
    """A minimal in-memory backend: a dict of key -> bytes."""

    scheme: ClassVar[str] = "dict"
    capabilities: ClassVar[StorageCapability] = (
        StorageCapability.OPEN_READ | StorageCapability.OPEN_WRITE
        | StorageCapability.SORTED_SCAN | StorageCapability.DELETE
    )

    def __init__(self, store: dict[str, bytes], *, root: str = "dict://store"):
        self._store, self._root = store, root

    def as_text(self) -> str:            # how this side renders in results
        return self._root

    def open(self, key: str, mode: str, *, size: int | None = None) -> BinaryIO:
        return io.BytesIO(self._store[key]) if mode == "rb" else _Committing(self._store, key)

    def scan_pages(self, options: ScanOptions):
        yield [FileInfo(key=k, size=len(v), compare_key=k)      # compare_key = root-relative
               for k, v in sorted(self._store.items())]         # sorted -> byte order (sync)

    def get_fileinfo(self, key: str = "", *, follow_symlinks=True, on_warning=None):
        data = self._store.get(key)
        return None if data is None else FileInfo(key=key, size=len(data), compare_key=key)

    def delete(self, info: FileInfo) -> None:
        del self._store[info.key]

store = {"a.txt": b"hello", "b.txt": b"world"}
S3().sync(DictStorage(store), "s3://my-bucket/data/")   # custom -> S3 (upload)
S3().sync("s3://my-bucket/data/", DictStorage(store))   # S3 -> custom (download)
```
