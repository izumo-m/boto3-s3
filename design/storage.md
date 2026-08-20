# Storage backends

`Storage` is the abstraction the `S3` operations resolve their sides into:
`S3.resolve` turns a path/URI argument into a `Storage`. The built-ins are
`S3Storage` (an S3 bucket/key), `LocalStorage` (a local path), and the
`IOStorage` / `StdioStorage` stream wrappers. A **custom subclass** is one more
`Storage`, so `cp` / `mv` / `sync` reach it through the same planning and
engine path as the built-ins - through this interface alone on the custom
side, while the built-in routes use their concrete classes' own API (section
2's `scheme` bullet).

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

So a custom backend takes part only in `cp` / `mv` / `sync`, and never in a
custom↔custom pair. The S3-only operations — `ls` / `rm` / `mb` / `rb` / `presign` /
`website` — require an actual `S3Storage` and are not part of this seam, and
neither is the single-request object transfer on `S3Storage` (section 6). (The
built-in `IOStorage` / `StdioStorage` stream wrappers use the very same seam: a
stream is a degenerate single-entry backend.)

## 2. The contract

Subclass `Storage`, set two class attributes, and implement the methods the
declared capabilities promise. Only `as_text` is abstract; `open` /
`scan_pages` / `delete` / `get_fileinfo` have base implementations that raise
`NotImplementedError` naming the missing capability, so a minimal backend
implements exactly what it declares. Every declared flag — including one
*implied* by a stronger flag (section 3's lattice: `SCAN` implies
`GET_FILEINFO`) — promises its matching method; the engine's gates check
declarations, not implementations, so they refuse an unsupported operation up
front only when the declaration is honest:

- **`scheme: ClassVar[str]`** — the backend's path-shape token, anything but
  `"s3"` / `"local"` (a declared classification label for embedders; the
  engine itself never reads it - results render through `as_text()`).
  Transfer *routing* does not read it either: the planner
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
  stays unimplemented, since every S3 write on the *transfer lanes* rides
  `s3transfer` and the one write outside them (`put_file`, section 6) is a
  whole-file call rather than a stream.
- **`scan_pages(options) -> Iterator[Sequence[FileInfo]]`** — enumerate the
  container one page of `FileInfo` at a time. Callers consume it through the
  concrete `scan(options, *, cancel_token=None)` wrapper, which flattens the
  pages and overlaps them with a background prefetch worker; `cancel_token`
  stops the prefetch producer before its next page pull.

  **`ScanOptions(read_ahead=False)`** drops that overlap: each page is pulled on
  the calling thread when the consumer reaches it, and `options.filter` and
  `options.on_warning` then run there too. The cancel point is the same one (a
  page pull), and the stream of entries is unchanged - only the timing and the
  thread differ. Its reason is a consumer that mutates what it is enumerating:
  `sync --delete` walking a **local destination** deletes the orphans the
  merge-join hands it while the same walk is still running, so a page read ahead
  could describe a file the run has since removed. Nothing else asks for it -
  `sync` narrows the option for a `LocalStorage` destination of a deleting run
  only - and a backend that must never list ahead of its own `delete` can seed
  `read_ahead=False` in its `default_scan_options`, which the operations narrow
  but never widen.

  **A producer that fails part-way through a page** should yield what it has
  already built and only then raise: `scan`'s prefetch re-raises a producer
  error after the chunks it had queued are consumed, so the entries ahead of
  the failure reach the consumer first - the entry-by-entry order aws-cli dies
  in, where a page is the smallest chunk this interface can deliver. The
  built-in S3 backend is what needs it: it reads the elements a listing entry
  must carry by subscript, in aws-cli's own order (`Key` -> `LastModified` ->
  `Size` for an object, `Prefix` for a common prefix, `CreationDate` -> `Name`
  for a bucket), so an entry missing one raises `KeyError` naming the element
  instead of being silently dropped - the object listing emitting the part of
  the page it had converted, the bucket listing being streamed entry by entry
  to begin with.

  `options.filter` (the `--exclude`/`--include` predicate) is applied by
  **`scan()` as a safety net** by default, so a `scan_pages` that forgets it
  cannot silently leak excluded entries into `--exclude`/`--include` or, on a
  `sync --delete` destination, into deletion. A backend that filters at its
  source applies it in `scan_pages` and declares
  **`scan_pages_filters = True`** to skip the redundant re-filter
  (`storage.sieve_pages` is the helper; a custom
  `LocalFileGenerator.finalize_children` override prunes by its *own* rules -
  it never sees `options.filter` - so it does not qualify by itself). Honor
  `options.sort` when `SORTABLE_SCAN` is declared (byte order for the
  recursive listing `sync` performs; section 3).

  `options` carries the operation-inherent knobs the caller decides per
  invocation (`recursive` / `sort` / `filter` / `on_warning`, and
  `S3ScanOptions`'s internal `prefix`). The **source-config** knobs — how a
  particular source is read — are configured on the storage constructor and
  seeded into every scan by `default_scan_options()` (see below): a
  `ScanOptions` subclass still carries them to `scan_pages`, but the caller
  does not pass them per operation (`S3ScanOptions` = the `ListObjectsV2`
  knobs `page_size` / `fetch_owner`, plus the operation-set `request_payer` /
  `prefix` / `include_common_prefixes`; `LocalScanOptions` = `follow_symlinks` /
  `detect_symlink_loops` / `enumerate_all_entries`, plus the internal `storage`
  back-reference the walk stamps on each entry). A subclass keeps one backend's
  knobs from leaking into another's; the built-ins reject a foreign options
  type, and a custom backend reads its own knobs from its own subclass or from
  its instance state, taking the common base otherwise.

  The local complete-entry setting (`enumerate_all_entries`) widens candidates
  before filtering: it includes the root, directories, symlinks, special
  files, and metadata-readable entries whose content is unreadable. High-level
  operations preserve it; a caller that opts in must filter out entries its
  transfer cannot consume, or accept the operation's normal failure, blocking,
  device-side-effect, and deletion behavior. The default `False` keeps aws-cli
  transfer enumeration.

  The S3 side has one widening of the same kind, `include_common_prefixes`:
  emit the `CommonPrefixes` of a **recursive** page as `DIRECTORY` entries
  (a non-recursive page always emits them — its `Delimiter` is what asks for
  them). Only `S3.ls` sets it, because aws-cli's display prints a page's common
  prefixes whether or not `--recursive` was given, while its transfer-side
  generator reads `Contents` only; a listing without a `Delimiter` gets no
  prefixes from a conforming service, so the setting is visible only against
  one that returns them anyway. The transfers keep the default `False`: a
  directory record is not transferable, and a page's prefixes arrive ahead of
  its objects, which would break the `compare_key` byte order `sync`'s
  merge-join asserts on (section 3).

  `LocalStorage` also takes one **destination-side** constructor knob that is
  *not* a scan source-config: `fsync` (default off = aws parity), a library
  extension the transfer engine reads off the destination to make an `mv`
  download durable before deleting the S3 source (transfer.md section 11).
- **`get_fileinfo(key="", *, on_warning=None) -> FileInfo | None`**
  — the single-entry counterpart of `scan` (a single source, or an existence
  check). `key=""` is the location itself; `None` means "no entry to hand the
  engine" (`LocalStorage`: an absent path, a `follow_symlinks=False` symlink,
  or a special / unreadable file after the aws-style warn-and-skip - but a
  directory *is* returned, with no type check, and fails later at `open` the
  way aws fails). Whether a symlink is followed is `LocalStorage`'s own
  `follow_symlinks` config, read from the storage like every scan (not a
  parameter here). `enumerate_all_entries` does not apply: the point query
  always uses those single-file rules.
- **`delete(info) -> Mapping | None`** — remove the entry `info` identifies, by
  `info.key`. Return the backend's delete response (surfaced under
  `OpResult.extra_info["delete"]` for `capture_response`) or `None` when there is
  none — a local unlink returns `None`, `S3Storage` returns its `DeleteObject`
  response.

A few more members come with working defaults a custom backend normally keeps:

- **`scan_options_type: ClassVar[type[ScanOptions]]`** — this backend's
  `ScanOptions` type (default `ScanOptions`). Arg-less `scan()` builds it (via
  `default_scan_options()`), so a backend whose `scan_pages` requires its own
  subclass still works with no options. `S3Storage` / `LocalStorage` set
  `S3ScanOptions` / `LocalScanOptions`; **a custom backend that defines its own
  subclass just sets this one class attribute — no method to override** — and one
  that takes the base `ScanOptions` sets nothing. A custom subclass must stay a
  `frozen=True, kw_only=True` dataclass and give **every added field a
  default**: the high-level operations overlay only the run-level knobs — the
  operation-inherent ones plus the application's Ctrl-C posture
  (`reusable_after_interrupt`, from `S3(reusable_after_interrupt=…)`) — via
  `dataclasses.replace(storage.default_scan_options(), …)`, and the
  base `default_scan_options()` constructs the type with no arguments.
- **`default_scan_options() -> ScanOptions`** — builds `scan_options_type` and is
  the single place a backend seeds the **source-config it holds on the instance**.
  The built-ins override it to inject their constructor knobs
  (`LocalStorage(follow_symlinks=…, detect_symlink_loops=…,
  enumerate_all_entries=…)`,
  `S3Storage(page_size=…, fetch_owner=…)`); a custom backend overrides it to seed
  its own instance state (or for any dynamic default). Every scan builds from it:
  the high-level `cp` / `sync` / `ls` / `rm` paths take
  `replace(storage.default_scan_options(), <operation-inherent knobs>)`, so a
  storage's source-config — and a custom `scan_options_type` subclass — flows
  through the operations, not only an arg-less `scan()`. This is how an app
  configures the walk / listing once on the storage rather than per call.
- **`ScanOptions.reusable_after_interrupt`** (not a `Storage` member) — the Ctrl-C
  exit policy of `scan()`'s background page worker. `True` (the default): the
  scan's teardown always waits for a page pull already in flight, so no worker
  survives the operation — required for an app that may catch
  `KeyboardInterrupt` and continue. `False`: a `KeyboardInterrupt` unwind
  abandons the daemon worker instead of waiting (an in-flight network pull can
  otherwise hold the exit for a full timeout) — only for an app that treats
  Ctrl-C as process-fatal. The application declares the posture once, on
  `S3(reusable_after_interrupt=…)`; every scan an operation starts receives it
  through this field, and only a direct `Storage.scan` caller sets it here
  itself. The CLI's `S3` declares `False`, matching aws's immediate death on
  Ctrl-C. It scopes to the interrupt alone: every other exit — exhaustion, an
  early break, `SystemExit` (`sys.exit()` requests an orderly termination),
  an ordinary exception — always waits (`concurrency.prefetch`). The interrupt
  does not always unwind *through* the prefetch context (it can land in a
  result callback or a submission wait, leaving the scan's teardown a plain
  close), so the operations mark the unwind
  (`concurrency.mark_interrupt_unwinding`, monotonic — the process is fatal by
  declaration) and every posture-`False` teardown from then on abandons its
  worker too.
- **`sep: ClassVar[str]`** — the separator of the backend's path space (`"/"`;
  only `LocalStorage` overrides with the host `os.sep`). Keep the default: the
  `FileInfo.key` / `compare_key` contract is `/`-separated.
- **`format(*, dir_op) -> (root, use_src_name)`** — how this side enters a
  transfer plan (the per-side half of aws-cli's `FileFormat.format`, resolved
  polymorphically; `S3Storage` / `LocalStorage` override it with aws's
  `s3_format` / `local_format` on their own held state). The default is the
  open-route rule. The root is `""`: a custom backend encapsulates its own
  location, its `open` receives `compare_key`, which addresses an entry relative
  to that location (`""` for the single location), and its `delete` receives the
  entry's `FileInfo` (keyed by `info.key`; section "Keys" below).
  `use_src_name` follows the S3 convention
  (`dir_op` or a trailing `/` on `as_text()`).
- **`validate() -> None`** — a public hook for deferred strict validation of
  the location, a no-op by default. Construction is permissive (a building
  block); an operation — or the CLI at its parity point — calls this to reject
  a malformed location loudly before use. `S3Storage` overrides it with the
  aws-cli-parity checks (unsupported ARN forms, a key with no bucket); a
  custom backend that can detect a malformed location overrides it likewise.
  Idempotent.

Errors raised from these should map to the library taxonomy
([`exceptions.md`](./exceptions.md)); the engine renders their message verbatim.

The contract is designed to evolve without breaking a shipped backend: an
existing method never grows a new parameter. New per-scan context arrives as a
defaulted field on the `ScanOptions` value objects, and anything else as a new
method with a non-abstract default (plus a new `StorageCapability` flag when
it gates an operation) — so a subclass written against today's surface keeps
working as the interface grows.

### Keys: `key` vs `compare_key`

A `FileInfo` carries two keys (see [`glossary.md`](./glossary.md)):

- **`key`** is the entry's full, `/`-separated identifier **in the backend's own
  address space** — what `delete(info)` acts on (the built-ins key on
  `info.key`). A backend chooses its own space (`S3Storage`'s `key` is the full
  bucket key, `LocalStorage`'s an absolute path). A typical custom backend uses
  keys relative to its location, so a recursive entry's `key` is its
  `compare_key` and the single location is `""`. `open`'s
  address argument is backend-specific: `S3Storage.open` takes the full bucket
  key, `LocalStorage.open` joins its key under the location (an absolute key
  resolves to itself), and a **custom** backend's `open` must resolve the
  `compare_key` the engine passes relative to its location (`""` for the single
  location — the open route and the content strategies both pass that space).
  For the typical custom backend (`key == compare_key`) the two spaces
  coincide, so `info.storage.open(info.key, "rb")` reads built-in and typical
  custom entries alike.
- **`compare_key`** is the relative form of the same entry: the space a
  relative glob pattern matches (a root-anchored pattern matches the full
  `key` instead - [`glossary.md`](./glossary.md)) and the axis `sync`
  merge-joins on.
  `scan` must stamp it on every entry. Omitting the stamp is a contract
  violation with undefined behavior: a dev (non-`-O`) run trips an `assert`
  before the transfer, while `-O` strips the check.

## 3. Capabilities

`StorageCapability` is a `Flag` the engine pre-checks **before** a transfer, so a
backend that lacks what an operation needs fails fast with a clear error instead
of deep inside the run:

| flag | method | needed for a custom side that is |
|---|---|---|
| `OPEN_READ` | `open(key, "rb")` | an `opens3` source |
| `OPEN_WRITE` | `open(key, "wb")` | an `s3open` destination |
| `GET_FILEINFO` | `get_fileinfo` | a single-entry source / existence check |
| `SCAN` | `scan` / `scan_pages` | a recursive (multi-entry) **source** |
| `SORTABLE_SCAN` | byte-ordered recursive `scan` (`ScanOptions(sort=True)`) | **any `sync`** side |
| `DELETE` | `delete(info)` | an `mv` source / a `sync --delete` destination |

The reading members form a lattice: `SORTABLE_SCAN` implies `SCAN` implies
`GET_FILEINFO`. `sync`'s merge-join walks both listings in UTF-8 byte order, so a
custom `sync` side **must** declare `SORTABLE_SCAN` — an unsorted listing would
manufacture phantom pairs and, with `--delete`, corrupt the destination. The
promise is checked as the merge consumes it rather than taken on trust: the
first strict descent on either side raises `ValidationError` and ends the run
(`comparator._byte_ordered`; the check is unconditional, so `python -O` keeps
it), which is also what an S3-compatible endpoint returning an unsorted
`ListObjectsV2` runs into.
**`sync` is the only order-sensitive consumer**: recursive `cp` / `mv` take the
backend's entries in whatever order `scan` yields them (they never pass
`ScanOptions(sort=True)`), so a plain `SCAN` side needs no ordering guarantee
at all. The byte-order promise is likewise scoped to the recursive listing
`sync` performs: a non-recursive (delimiter) listing keeps its native shape -
`S3Storage`'s pages emit a page's common prefixes before its objects, aws
`ls`'s order. A recursive custom *destination* of `cp` / `mv` needs no `SCAN`
either - only `sync` lists a destination. The exact per-route gates are in
[`sync.md`](./sync.md) / [`transfer.md`](./transfer.md).

The gates read the declaration through `supports(needed)` /
`missing_capabilities(needed)` — the lattice-expanded membership test and its
companion that names what is absent (for a clear rejection message). An
embedder can call the same pair for its own up-front check.

## 4. Example

A minimal in-memory backend — a `dict[str, bytes]` keyed by `compare_key`, so a
single entry uses `""`:

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
    scan_pages_filters: ClassVar[bool] = True    # scan_pages applies options.filter itself

    def __init__(self, store: dict[str, bytes], *, root: str = "dict://store"):
        self._store, self._root = store, root

    def as_text(self) -> str:            # how this side renders in results
        return self._root

    def open(self, key: str, mode: str, *, size: int | None = None) -> BinaryIO:
        return io.BytesIO(self._store[key]) if mode == "rb" else _Committing(self._store, key)

    def scan_pages(self, options: ScanOptions):
        infos = [FileInfo(key=k, size=len(v), compare_key=k)    # relative key
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
non-recursive `cp`, or the destination of a non-recursive `mv` (the other side
always S3), without a temp file:

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
- **A single endpoint, not a container.** Only `open` is meaningful; the
  inherited `get_fileinfo` / `delete` raise `NotImplementedError` on the call,
  and `scan` raises the same on its first iteration rather than on the call
  (it is a generator, so its body - and the `scan_pages` backstop inside it -
  runs only once the caller pulls). So a stream is a non-recursive `cp` side or
  a non-recursive `mv` **destination** only — the move writes the bytes to the
  stream and then deletes the S3 source. A stream is never a move *source* (a
  move deletes its source, which a stream cannot be) or a recursive move's
  destination (`S3.mv` rejects both with `ValidationError`), never `ls` / `rm`,
  and not stream↔stream.

`StdioStorage` is the convenience for the process's own stdio — `sys.stdin` as a
source, `sys.stdout` as a destination (both binary, via `.buffer`) — the
equivalent of `aws s3 cp - …` / `aws s3 cp … -`. Its stdout writer hands each
chunk straight to `sys.stdout.buffer` and never flushes it, aws's writer having
neither `flush` nor `close`, so a finished download can still be sitting in the
process stream's buffer until the interpreter's own flush at exit.

## 6. Single-request object transfer: `S3Storage.get_file` / `put_file`

`S3Storage.get_file(path, *, key="")` and `S3Storage.put_file(path, *, key="")`
move one whole local file with **exactly one S3 request** — a `GetObject` or a
`PutObject`, issued on the calling thread with no transfer engine underneath.
They are a deliberate **third lane**, beside the two above: the s3transfer
routes (`cp` / `mv` / `sync`, built-in pairs) and the open route (section 1).

**Why a lane at all.** The engine's mechanism is not free. A transfer builds
s3transfer's futures machinery and runs on its thread pool, and a *download*
resolves its source with a `HeadObject` first — the transfer route's own
single-source probe (aws-cli's shape, [`transfer.md`](./transfer.md)), and the
one s3transfer issues itself when no size is supplied — so one small object
fetched through `S3.cp` costs two requests plus the engine, against this lane's
one. For the objects an
application round-trips constantly — a state file, a config, a manifest, each
comfortably under the multipart threshold — that machinery, not the bytes, is
the cost. The evidence is an external consumer (s3bak) that already bypassed
`cp` for sub-threshold objects with direct client `GetObject` / `PutObject`
calls, and then had to re-implement s3transfer's local-write safety by hand —
sibling temp file, `os.replace`, preserving an existing destination's permission
bits, removing the temp file on failure. That safety is library work, so it
lives here.

Keep this apart from bulk throughput: the answer for **many** small files in one
`cp` / `sync` run is the CRT engine ([`crt.md`](./crt.md)). This lane is about a
**single** round-trip's mechanism cost, and it does nothing for a run of
thousands of items.

**The local write is atomic** (`get_file`, the shape s3bak proved). The sibling
temp file and the `os.replace` mirror the safety property s3transfer's own
download lane has; preserving the destination's mode is this lane's addition on
top, which s3transfer does not make — it leaves the replacement at whatever bits
its temp file was created with. In full:

- the body streams into a **sibling temp file** in the destination's own
  directory (so the commit stays inside one filesystem), created `O_EXCL` with
  a random name and mode `0o666` — the umask decides a *new* destination's bits,
  exactly as a plain `open(…, "wb")` would;
- an existing **regular** destination's permission bits are copied onto the temp
  file first: the atomic swap lands a new inode, so without that the file's mode
  would silently become the temp file's;
- `os.replace` commits, so a concurrent reader sees the whole old file or the
  whole new one, a **symlink** at the final path component is replaced rather
  than followed into its target, and a failed or truncated download leaves the
  previous destination byte-for-byte intact;
- the temp file is removed on the way out of **any** failure, `BaseException`
  included, so an interrupted download litters no more than a failed one;
- missing parent directories are created (`makedirs(exist_ok=True)`).

Every failure surfaces in the library taxonomy ([`exceptions.md`](./exceptions.md))
with the two families attributed correctly: the request and the reads of its
streamed body through the botocore translation (carrying `bucket` / `key`), the
local filesystem failures through `localstorage.translate_os_error` (carrying the
local path in `key`, no `bucket`). The split is load-bearing, not stylistic:
botocore's `ReadTimeoutError` *is* an `OSError`, so a single `except OSError`
spanning both would file a broken stream as a local failure.

**The boundary.** One request, and nothing more:

- no multipart, and the multipart threshold is never consulted. `put_file` sends
  one `PutObject` whatever the size, and an object too large for a single PUT
  fails with S3's own error; `get_file` streams the whole object in one
  `GetObject`. Multipart, parallelism and resumption remain `S3.cp`'s.
- no `HeadObject`, and no `ContentType` / MIME guessing on upload — guessing an
  upload's type from its name is the transfer lanes' aws-cli parity behavior
  (`guess_mime_type`), not this lane's.
- no local mtime stamping after a download, and no `reject_unrepresentable_stamp`
  guard: both exist for the transfer lanes' aws parity, and here the caller holds
  the returned `S3FileInfo` and decides.
- **`cp` / `mv` / `sync` do not use them**, so aws-cli parity is untouched: the
  transfer engine, its gates (glacier, `--no-overwrite`, case-conflict) and the
  CLI behave exactly as before.
- not part of the `Storage` SPI. These are `S3Storage`-specific building blocks
  like the S3-only operations, so `StorageCapability` and the `Storage` ABC gain
  nothing and a custom backend implements nothing new.

Both address the object through the same `""`-is-this-location /
join-under-the-prefix rule as `get_fileinfo`, and both return an `S3FileInfo`
filled as `get_fileinfo`'s is: full `key`, dequoted `etag`, basename
`compare_key`, `storage`. The `head` slot is where they differ — these two store
the response with its transport metadata stripped (`strip_response_metadata`,
the convention for every response slot the API surfaces; `get_file` drops the
streaming body with it), where `get_fileinfo` stores the `HeadObject` response
as it arrived.
