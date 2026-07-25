# Custom backends

`cp` / `mv` / `sync` are not limited to local paths and S3. A `Storage` subclass
— an HTTP service, an archive, an in-memory store — can be one side of a
transfer, and its bytes move through the same engine.

The built-in [`IOStorage` and `StdioStorage`](./streams.md) use this same seam; a
stream is just a backend holding a single entry.

## 1. What a custom backend can be

**One side of a transfer, with the other side always S3.** Two shapes:

- **custom → S3** — the backend is the source, and each entry is uploaded from
  the readable stream it opens.
- **S3 → custom** — the backend is the destination, and each object is
  downloaded into the writable stream it opens.

So a custom backend takes part in `cp`, `mv` and `sync` only. A
custom-to-custom pair is not supported, and the S3-only operations
(`ls` / `rm` / `mb` / `rb` / `presign` / `website`) require a real `S3Storage`
because each needs a bucket and a client.

## 2. What to implement

Subclass `Storage`, set two class attributes, and implement what you declared.
Only `as_text` is abstract — everything else has a base implementation that
raises a `NotImplementedError` naming the capability you did not declare, so a
minimal backend implements exactly what it promised.

- **`scheme`** — a label for your backend, anything other than `"s3"` or
  `"local"`.
- **`capabilities`** — the flags you actually support (below).
- **`as_text()`** — how this side appears in results and progress.
- **`open(key, mode, *, size=None)`** — per-object byte I/O. `"rb"` returns a
  readable stream, `"wb"` a writable one whose `close()` flushes. `size` is an
  optional length hint for writes.
- **`scan_pages(options)`** — enumerate the container one page of `FileInfo` at
  a time. Callers reach it through `scan()`, which flattens the pages and
  prefetches them in the background.
- **`get_fileinfo(key)`** — one entry, or `None` when it does not exist.
- **`delete(info)`** — remove one entry. Whatever mapping you return surfaces on
  the result record.

### Keys

Two key spaces meet here. `key` is yours — whatever addresses an entry in your
backend. `compare_key` is the operation-relative, `/`-separated identity that
globs match and that `sync` pairs the two sides on. **Your `scan_pages` must
stamp `compare_key` on every entry it yields.** Nothing downstream can recover
it if you do not.

### Filtering

`options.filter` is applied by `scan()` as a safety net, so forgetting it in
`scan_pages` cannot leak excluded entries into a `sync --delete` and destroy
data. If your backend can filter at its source, apply it in `scan_pages` and set
`scan_pages_filters = True` to skip the redundant second pass.

The predicate runs on the prefetch worker thread. Keep that in mind if your
implementation shares state.

## 3. Capabilities

The engine checks what you declared **before** starting, so an operation your
backend cannot support fails immediately with a clear message rather than
part-way through.

| flag | you must implement | needed when your side is |
| --- | --- | --- |
| `OPEN_READ` | `open(key, "rb")` | the source of a transfer to S3 |
| `OPEN_WRITE` | `open(key, "wb")` | the destination of a transfer from S3 |
| `GET_FILEINFO` | `get_fileinfo` | a single-entry source, or an existence check |
| `SCAN` | `scan_pages` | a recursive **source** |
| `SORTABLE_SCAN` | a byte-ordered recursive listing | **any** side of a `sync` |
| `DELETE` | `delete` | an `mv` source, or a `sync --delete` destination |

The reading flags form a lattice: `SORTABLE_SCAN` implies `SCAN` implies
`GET_FILEINFO`. Declaring the strongest one promises the weaker methods too.

**`SORTABLE_SCAN` is not optional for `sync`.** Its pairing walks both listings
in UTF-8 byte order; an unsorted listing manufactures pairs that do not exist
and, with `delete_filter`, deletes objects that were never orphans. `sync` is
the only order-sensitive consumer — recursive `cp` and `mv` take entries in
whatever order you yield them, so a plain `SCAN` backend needs no ordering
guarantee at all.

The gates are also callable directly, so an application can check before it
starts: `storage.supports(needed)` and `storage.missing_capabilities(needed)`,
the second naming what is absent.

## 4. Things worth knowing before you write one

- **Errors.** Map your backend's failures onto the library taxonomy described in
  [`errors.md`](./errors.md). An exception that is not already a `Boto3S3Error`
  is wrapped in the base class when it surfaces as a per-item failure, but one
  raised during enumeration propagates as-is. The library assumes a well-behaved
  backend.
- **Streams are opened late.** `open` is not called until bytes actually need to
  move, so a dry run never opens anything, and an item that fails before its
  first byte leaves your backend untouched.
- **The engine always closes what `open` returns**, and a failure while closing
  a write fails that item — flushing is part of the transfer.
- **Some gates do not apply** to a custom side. The case-conflict check and the
  parent-directory escape check are local-filesystem concerns, and
  `no_overwrite` does not perform an existence check against a custom
  destination during `cp` — it does still take effect in `sync`.
- **A zero-byte entry still produces an object.** Writing zero bytes is not the
  same as writing nothing at all.

## 5. A minimal example

```python
import io

from boto3_s3 import Storage, StorageCapability

class DictStorage(Storage):
    scheme = "dict"
    capabilities = StorageCapability.OPEN_READ | StorageCapability.OPEN_WRITE

    def __init__(self, data: dict[str, bytes]) -> None:
        self.data = data

    def as_text(self) -> str:
        return "dict://"

    def open(self, key, mode, *, size=None):
        if mode == "rb":
            return io.BytesIO(self.data[key])
        return _Writer(self.data, key)     # writes back on close()
```

Declaring only the two `OPEN_*` flags makes this usable as a single-entry `cp`
side. Add `SCAN` and `get_fileinfo` for recursive transfers, `SORTABLE_SCAN` for
`sync`, and `DELETE` to be an `mv` source.
