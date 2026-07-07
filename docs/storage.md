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
  downloaded into its `open("wb")` (whose `close()` flushes the write).

So `cp` / `mv` / `sync` are the only operations a custom backend joins, and never
custom↔custom. The S3-only operations — `ls` / `rm` / `mb` / `rb` / `presign` /
`website` — require an actual `S3Storage` and are not part of this seam. (The
built-in `IOStorage` / `StdioStorage` stream wrappers use the very same seam: a
stream is a degenerate single-entry backend.)

## 2. The contract

Subclass `Storage`, set two class attributes, and implement the methods the
declared capabilities promise:

- **`scheme: ClassVar[str]`** — the backend's path-shape token, anything but
  `"s3"` / `"local"` (a display/classification label; result rendering uses
  it). Transfer *routing* does not read it: the planner
  (`transferplan._paths_type`) routes by concrete type — a structural match
  (`isinstance`) against `S3Storage` / `LocalStorage`, subclasses included —
  because the built-in routes reach into those classes' own API
  (`get_client`/`bucket`/`key`, `path`); every other `Storage` takes the
  `open` route regardless of its `scheme` string.
- **`capabilities: ClassVar[StorageCapability]`** — the flag set the backend
  actually supports (section 3).
- **`as_text() -> str`** (and `str(storage)`) — how this side renders in results
  / progress (its canonical path token).
- **`open(key, mode, *, size=None) -> BinaryIO`** — per-object byte I/O. `"rb"`
  returns a readable stream; `"wb"` a writable one whose `close()` flushes buffered
  writes (standard file semantics). `size` is an optional total-length hint for writes.
  `S3Storage` implements `"rb"` only (a `GetObject` read convenience, addressed by
  the object's full key — chiefly for a content-based `sync` filter); its `"wb"`
  stays unimplemented, since every S3 write rides `s3transfer`.
- **`scan_pages(options) -> Iterator[Sequence[FileInfo]]`** — enumerate the
  container one page of `FileInfo` at a time. `options.filter` (the
  `--exclude`/`--include` predicate) is applied by **`scan()` as a safety net** by
  default, so a `scan_pages` that forgets it cannot silently leak excluded entries
  into `--exclude`/`--include` or, on a `sync --delete` destination, into deletion.
  A backend that filters at its source, or prunes early (e.g. a custom
  `LocalFileGenerator.finalize_children` calling `options.filter`), applies it in
  `scan_pages` and declares **`scan_pages_filters = True`** to skip the redundant
  re-filter (`storage.sieve_pages` is the helper). Honour
  `options.sort` when `SORTABLE_SCAN` is declared. `options` carries the
  operation-inherent knobs the caller decides per invocation
  (`recursive` / `sort` / `filter` / `on_warning`, and `S3ScanOptions`'s internal
  `prefix`); the **source-config** knobs — how a particular source is read — are
  configured on the storage constructor and seeded into every scan by
  `default_scan_options()` (see below), so a `ScanOptions` subclass still carries
  them to `scan_pages` but the caller does not pass them per operation
  (`S3ScanOptions` = the `ListObjectsV2` knobs `page_size` / `fetch_owner`, plus
  the operation-set `request_payer` / `prefix`; `LocalScanOptions` =
  `follow_symlinks` / `detect_symlink_loops`). A subclass keeps one backend's knobs
  from leaking into another's; the built-ins reject a foreign options type, and a
  custom backend reads its own knobs from its own subclass or from its instance
  state, taking the common base otherwise.
- **`get_fileinfo(key="", *, on_warning=None) -> FileInfo | None`**
  — the single-entry counterpart of `scan` (a single source, or an existence
  check). `key=""` is the location itself; `None` means "no transferable entry
  here". Whether a symlink is followed is `LocalStorage`'s own `follow_symlinks`
  config, read from the storage like every scan (not a parameter here).
- **`delete(info) -> Mapping | None`** — remove the entry `info` identifies, by
  `info.key`. Return the backend's delete response (surfaced under
  `OpResult.extra_info["delete"]` for `capture_response`) or `None` when there is
  none — a local unlink returns `None`, `S3Storage` returns its `DeleteObject`
  response.

Three more members come with working defaults a custom backend normally keeps:

- **`scan_options_type: ClassVar[type[ScanOptions]]`** — this backend's
  `ScanOptions` type (default `ScanOptions`). Arg-less `scan()` builds it (via
  `default_scan_options()`), so a backend whose `scan_pages` requires its own
  subclass still works with no options. `S3Storage` / `LocalStorage` set
  `S3ScanOptions` / `LocalScanOptions`; **a custom backend that defines its own
  subclass just sets this one class attribute — no method to override** — and one
  that takes the base `ScanOptions` sets nothing.
- **`default_scan_options() -> ScanOptions`** — builds `scan_options_type` and is
  the single place a backend seeds the **source-config it holds on the instance**.
  The built-ins override it to inject their constructor knobs
  (`LocalStorage(follow_symlinks=…, detect_symlink_loops=…)`,
  `S3Storage(page_size=…, fetch_owner=…)`); a custom backend overrides it to seed
  its own instance state (or for any dynamic default). Every scan builds from it:
  the high-level `cp` / `sync` / `ls` / `rm` paths take
  `replace(storage.default_scan_options(), <operation-inherent knobs>)`, so a
  storage's source-config — and a custom `scan_options_type` subclass — flows
  through the operations, not only an arg-less `scan()`. This is how an app
  configures the walk / listing once on the storage rather than per call.
- **`sep: ClassVar[str]`** — the separator of the backend's path space (`"/"`;
  only `LocalStorage` overrides with the host `os.sep`). Keep the default: the
  `FileInfo.key` / `compare_key` contract is `/`-separated.
- **`format(*, dir_op) -> (root, use_src_name)`** — how this side enters a
  transfer plan (the per-side half of aws-cli's `FileFormat.format`, resolved
  polymorphically; `S3Storage` / `LocalStorage` override it with aws's
  `s3_format` / `local_format` on their own held state). The default is the
  open-route rule: the root is `""` — a custom backend encapsulates its own
  location and its `open` / `delete` receive the scan-root-relative
  `compare_key` unprefixed — and `use_src_name` follows the S3 convention
  (`dir_op` or a trailing `/` on `as_text()`).

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
| `SORTABLE_SCAN` | byte-ordered `scan` (`ScanOptions(sort=True)`) | **any `sync`** side |
| `DELETE` | `delete(info)` | an `mv` source / a `sync --delete` destination |

The reading members form a lattice: `SORTABLE_SCAN` implies `SCAN` implies
`GET_FILEINFO`. `sync`'s merge-join walks both listings in UTF-8 byte order, so a
custom `sync` side **must** declare `SORTABLE_SCAN` — an unsorted listing would
manufacture phantom pairs and, with `--delete`, corrupt the destination.
**`sync` is the only order-sensitive consumer**: recursive `cp` / `mv` take the
backend's entries in whatever order `scan` yields them (they never pass
`ScanOptions(sort=True)`), so a plain `SCAN` side needs no ordering guarantee
at all. The exact per-route gates are in [`sync.md`](./sync.md) /
[`transfer.md`](./transfer.md).

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
        | StorageCapability.SORTABLE_SCAN | StorageCapability.DELETE
    )

    def __init__(self, store: dict[str, bytes], *, root: str = "dict://store"):
        self._store, self._root = store, root

    def as_text(self) -> str:            # how this side renders in results
        return self._root

    def open(self, key: str, mode: str, *, size: int | None = None) -> BinaryIO:
        return io.BytesIO(self._store[key]) if mode == "rb" else _Committing(self._store, key)

    def scan_pages(self, options: ScanOptions):
        infos = [FileInfo(key=k, size=len(v), compare_key=k)    # compare_key = root-relative
                 for k, v in sorted(self._store.items())]       # sorted -> byte order (sync)
        if options.filter is not None:                          # the scan_pages contract: return
            infos = [i for i in infos if options.filter(i)]     # filtered pages (or push the
        if infos:                                               # predicate to your source;
            yield infos                                         # storage.sieve_pages wraps raw)

    def get_fileinfo(self, key: str = "", *, on_warning=None):
        data = self._store.get(key)
        return None if data is None else FileInfo(key=key, size=len(data), compare_key=key)

    def delete(self, info: FileInfo) -> None:
        del self._store[info.key]

store = {"a.txt": b"hello", "b.txt": b"world"}
S3().sync(DictStorage(store), "s3://my-bucket/data/")   # custom -> S3 (upload)
S3().sync("s3://my-bucket/data/", DictStorage(store))   # S3 -> custom (download)
```

## 5. Streams: `IOStorage` and `StdioStorage`

`IOStorage` is a built-in `Storage` that presents **one caller-supplied stream**
as a single `open`-able endpoint, so a stream can be one side of a
non-recursive `cp` - or the destination of a non-recursive `mv` - (the other
side always S3) without a temp file:

```python
import gzip
import io

from boto3_s3 import S3, IOStorage

s3 = S3()

# upload from a stream (a binary file, or any readable)
with open("hello.txt", "rb") as f:
    s3.cp(IOStorage(f), "s3://bucket/hello.txt")

# upload from a text buffer (encoded with `encoding`, default utf-8)
s3.cp(IOStorage(io.StringIO("hello")), "s3://bucket/hello.txt")

# download into a buffer, then read it back: IOStorage does NOT reposition the
# stream, so rewind it yourself (or use getvalue())
buf = io.StringIO()
s3.cp("s3://bucket/hello.txt", IOStorage(buf))
buf.seek(0)
print(buf.read())            # or: print(buf.getvalue())

# download straight into a gzip writer - a non-seekable binary write stream
with gzip.open("hello.txt.gz", "wb") as f:
    s3.cp("s3://bucket/hello.txt", IOStorage(f))
```

The contract:

- **Bytes at the s3transfer boundary.** A **binary** stream (`io.BytesIO`, a file
  opened `"rb"` / `"wb"`, a `gzip` writer, a pipe) is used as-is; a **text**
  stream (`io.StringIO`, a file opened `"r"` / `"w"`) is wrapped with a codec
  (`IOStorage(stream, encoding="utf-8")`) — encode on upload, decode on download.
- **The caller owns the stream.** `IOStorage` **never closes** it and never
  rewinds it for you: lifecycle and final position are yours. After a download
  the stream sits at the end of the written bytes, so to read them back rewind it
  (`seek(0)`) or use `getvalue()`. A non-seekable sink works just as well — a
  `gzip` writer, `sys.stdout`, a pipe — there is nothing to rewind; the bytes
  land wherever the stream sends them (the `.gz` file, the console), and the
  caller's own `with` / `close` finalizes it.
- **A single endpoint, not a container.** Only `open` is meaningful; `scan` /
  `get_fileinfo` / `delete` raise, so a stream is a non-recursive `cp` side or
  a non-recursive `mv` **destination** only — the move writes the bytes to the
  stream and then deletes the S3 source. A stream is never a move *source* (a
  move deletes its source, which a stream cannot be) or a recursive move's
  destination (`S3.mv` rejects both with `ValidationError`), never `ls` / `rm`,
  and not stream↔stream.

`StdioStorage` is the convenience for the process's own stdio — `sys.stdin` as a
source, `sys.stdout` as a destination (both binary, via `.buffer`) — the
equivalent of `aws s3 cp - …` / `aws s3 cp … -`.
