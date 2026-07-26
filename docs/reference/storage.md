# Storage backends

`Storage` is what each side of an operation resolves to: a local path, an S3
bucket/prefix, a caller-supplied stream, or an application's own backend. This
page specifies the abstract contract, the capability declaration that gates it,
the four built-in implementations, and the override seams of the local
directory walk.

The walkthrough for writing a backend is
[`../library/custom-storage.md`](../library/custom-storage.md), the narrative
treatment of the stream wrappers is
[`../library/streams.md`](../library/streams.md), and the seam's rationale is
[`../../design/storage.md`](../../design/storage.md). Turning a path argument
into a `Storage` is `S3.resolve` ([`./s3.md`](./s3.md)).

Every symbol below is exported from the `boto3_s3` root. Each is also
importable from its own module: `Storage` / `Location` / `StorageCapability`
from `boto3_s3.storage`, `LocalStorage` / `LocalFileGenerator` / `WalkChild` /
`LoopDetector` from `boto3_s3.localstorage`, `S3Storage` from
`boto3_s3.s3storage`, `IOStorage` / `StdioStorage` from `boto3_s3.iostorage`.

## Storage

The abstract base every backend subclasses. It has four operations — `scan`
(enumerate), `get_fileinfo` (resolve a single entry), `open` (read or write one
object as a binary stream), `delete` — plus `as_text`, the canonical path-shape
token this location renders as.

**`capabilities` is the only declaration mechanism, and `as_text` is the only
abstract method.** `scan_pages`, `open`, `delete` and `get_fileinfo` all have
base implementations that raise `NotImplementedError` naming the capability
that would have promised them, so a subclass implements exactly what it
declared and nothing more. There is no registration step, no method-presence
probe, and no other way to tell the engine what a backend supports.

```python
class Storage(abc.ABC):
    scheme: ClassVar[str]
    sep: ClassVar[str] = "/"
    capabilities: ClassVar[StorageCapability] = StorageCapability(0)
    scan_options_type: ClassVar[type[ScanOptions]] = ScanOptions
    scan_pages_filters: ClassVar[bool] = False

    @abc.abstractmethod
    def as_text(self) -> str: ...

    def default_scan_options(self) -> ScanOptions: ...
    def scan(
        self,
        options: ScanOptions | None = None,
        *,
        cancel_token: CancelToken | None = None,
    ) -> Iterator[FileInfo]: ...
    def scan_pages(
        self, options: ScanOptions
    ) -> Iterator[Sequence[FileInfo]]: ...
    def open(
        self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None
    ) -> BinaryIO: ...
    def delete(self, info: FileInfo) -> Mapping[str, Any] | None: ...
    def get_fileinfo(
        self, key: str = "", *, on_warning: Callable[[str], None] | None = None
    ) -> FileInfo | None: ...
    def format(self, *, dir_op: bool) -> tuple[str, bool]: ...
    def validate(self) -> None: ...
    def supports(self, needed: StorageCapability) -> bool: ...
    def missing_capabilities(
        self, needed: StorageCapability
    ) -> StorageCapability: ...
    def __str__(self) -> str: ...
```

`scheme` is a display and classification label naming the storage family.
`"s3"` and `"local"` are the built-in pair; every other value marks a
non-built-in backend. It has no annotated default, so a concrete subclass sets
it. Transfer routing does not read it — the planner matches on concrete type
(`S3Storage` / `LocalStorage`, subclasses included), so a subclass of a
built-in keeps the built-in route whatever `scheme` says, and any other
`Storage` takes the `open` route whatever `scheme` says.

`sep` is the separator of this backend's path space as it appears in the roots
`format` returns. It is `"/"` for S3, the streams and custom backends, because
`FileInfo.key` and `FileInfo.compare_key` are `/`-separated by contract
([`./results.md`](./results.md)); `LocalStorage` overrides it with the host
`os.sep`.

`capabilities` is the class-level flag set of transfer operations this storage
*kind* implements. The default declares nothing, so a subclass that forgets it
is refused every gated route rather than admitted by accident. A subclass may
narrow or widen an inherited set. Declaring a flag is a promise that the
matching method works; the gates check the declaration, never the
implementation, so a dishonest declaration surfaces as the base method's
`NotImplementedError` part-way through a run instead of as an up-front
rejection. See [Capability gates](#capability-gates) below for the per-route
requirements, and [`StorageCapability`](#storagecapability) for the members.

`scan_options_type` is the `ScanOptions` subclass this backend's `scan_pages`
expects ([`./options.md`](./options.md)). `default_scan_options` builds it, so
an arg-less `scan()` still produces the type the backend requires. A custom
backend that defines its own subclass sets this attribute and overrides
nothing; a backend that takes the base `ScanOptions` sets nothing.

`scan_pages_filters` declares that `scan_pages` already applies
`options.filter` itself. When it is `False` (the default), `scan` re-applies
the predicate after `scan_pages` as a safety net, so a backend that forgets to
filter cannot leak excluded entries into an include/exclude run or, on a `sync`
destination with deletes enabled, into deletion. Both built-ins set `True`.

### Capability gates

The gates apply to the **one non-built-in side** of a `cp`, `mv` or `sync`. A
pairing of two built-ins (`S3Storage` with `LocalStorage`, or `S3Storage` with
`S3Storage`) is not capability-checked at all, because those routes drive
`s3transfer` off the concrete classes rather than through `Storage.open`. The
S3-only operations — `ls`, `rm`, `mb`, `rb`, `presign`, `website` — accept only
an `S3Storage` and are not part of this seam, so they never consult
`capabilities` either.

A custom backend is one side of a transfer whose other side is an `S3Storage`;
pairing it with a local path, another custom backend or a stream is rejected
with `ValidationError` before any gate runs. So the routes below are the whole
set.

What each declaration buys, per route:

- **Custom source, S3 destination, `cp` / `mv`**: `OPEN_READ`, plus `SCAN` when
  the operation is recursive or `GET_FILEINFO` when it is not, plus `DELETE`
  when the operation is `mv` (a move removes the source it read).
- **S3 source, custom destination, `cp` / `mv`**: `OPEN_WRITE`. A `mv` adds
  nothing here — the source it deletes is the S3 side.
- **Custom source, `sync`**: `SORTABLE_SCAN` and `OPEN_READ`.
- **Custom destination, `sync`**: `SORTABLE_SCAN` and `OPEN_WRITE`, plus
  `DELETE` when the run enables deletes — a truthy `delete_filter`
  ([`./operations/sync.md`](./operations/sync.md)).

Omitting a flag the route needs raises `ValidationError`
([`./exceptions.md`](./exceptions.md)) before any bytes move, with the missing
flag names in the message. The reading lattice is applied first, so declaring
`SORTABLE_SCAN` satisfies a check for `SCAN` or `GET_FILEINFO`.

A capability is not a permission. Whether a particular object is readable,
writable or present right now is decided at execution time and reported per
item — an S3 `403`, a local `EACCES` or `ENOENT` — not by this pre-check. The
one pre-flight exception is the transfer commands' missing-local-source check,
which runs before items flow.

`cp` with an `IOStorage` on either side is diverted to its stream path before a
transfer plan exists, so no capability check runs on that route. A `mv` onto a
stream destination does go through the S3-to-custom route and is gated on
`OPEN_WRITE`, which `IOStorage` declares.

### scan(options=None, \*, cancel_token=None)

Yields the entries under this storage as one flat `FileInfo` stream. This is a
concrete wrapper around `scan_pages`, not an override point: it flattens the
per-page producer and overlaps it with a background prefetch worker, so the
next page's I/O runs while the consumer handles the current page. An error
raised by the producer surfaces on the consumer's pull.

`options` defaults to `default_scan_options()`. Anything a backend needs beyond
the base knobs must arrive on its own `ScanOptions` subclass; the built-ins
reject a foreign options type.

`cancel_token` ([`./results.md`](./results.md)) stops the prefetch producer
before its next page pull. Entries already yielded to the consumer are
unaffected.

Two things happen between the producer and the consumer. Each entry whose
`storage` is still `None` is stamped with this backend — a safety net for a
`scan_pages` that did not stamp it, applied ahead of the filter so a predicate
always sees the producing backend. Then, unless the class declares
`scan_pages_filters`, `options.filter` is applied page by page on the prefetch
worker; a page emptied by the predicate is dropped rather than yielded empty.

### scan_pages(options)

Yields entries one natural I/O page at a time. This is the method a listing
backend implements and the point to override — including by calling
`super().scan_pages(options)` and transforming its pages, which keeps the
page-ahead overlap that re-implementing `scan` would lose.

`options.filter` must be applied here, and the pages returned already filtered:
whatever this producer omits is simply absent downstream. A backend that cannot
push the predicate to its source wraps its raw pages with
`boto3_s3.storage.sieve_pages`; one that can translates the predicate into a
server-side query instead.

`options.recursive` normally walks every transferable entry beneath the
location, all of `FileKind.FILE` with no directory grouping; a backend-specific
source setting may widen that view, as `LocalScanOptions.enumerate_all_entries`
does. A non-recursive scan normally yields the immediate entries plus one
`FileKind.DIRECTORY` entry per sub-"directory".

`key` is the entry's full identifier in the backend's own address space; its
relative form must be stamped on every entry as `compare_key`
([`./results.md`](./results.md)). Nothing downstream can recover `compare_key`
if the producer does not stamp it.

Ordering is `options.sort`'s business, and only a `SORTABLE_SCAN` backend is
bound by it — see [Scan ordering](#scan-ordering) below.

The base implementation raises `NotImplementedError` naming
`StorageCapability.SCAN`.

### open(key, mode, \*, size=None)

Opens the object at `key` as a binary stream. `"rb"` returns a readable stream;
`"wb"` a writable one whose `close()` flushes any buffered writes, standard
file semantics. Whether a writing backend persists per write or defers to the
flush is its own choice.

`size` is an optional total-length hint. The transfer engine passes the entry's
size on both `"rb"` and `"wb"` opens; a backend may use it to pre-allocate or
to choose a write strategy up front, or ignore it.

What `key` means is backend-specific. `S3Storage.open` takes the object's full
bucket key, `LocalStorage.open` joins its argument under the location, and a
custom backend's `open` receives the entry's `compare_key` — `""` for the
single-entry location — because the default `format` gives a custom side an
empty root.

This is the generic per-object I/O primitive. Built-in S3-to-local transfers
ride `s3transfer` and never call it; it is the path a custom backend and the
stream wrappers transfer through. On that route `cp` / `mv` / `sync` call it
**lazily, when the engine first moves that entry's bytes** — not when the entry
is enumerated or queued — so an entry whose transfer never starts (failed,
cancelled, dry run) is never opened. The returned object is read sequentially
or written strictly in order, and closed when the transfer settles.

The base implementation raises `NotImplementedError` naming
`StorageCapability.OPEN_READ` / `OPEN_WRITE`.

### delete(info)

Removes the entry `info` identifies — the operation behind `rm`, a `mv`
source, and a `sync` destination orphan. `info` is a listing entry from `scan`
or `get_fileinfo`, or one built by hand; the backend locates it by `info.key`
in its own address space.

Returns the backend's delete response, which the operation surfaces under
`OpResult.extra_info["delete"]` when it runs with `capture_response=True`
([`./results.md`](./results.md)), or `None` when there is none. A local unlink
returns `None`; `S3Storage` returns its `DeleteObject` response.

The base implementation raises `NotImplementedError` naming
`StorageCapability.DELETE`.

### get_fileinfo(key="", \*, on_warning=None)

Returns the `FileInfo` for a single entry, or `None` when there is no
transferable entry there. This is the single-entry counterpart to `scan`: a
non-recursive `cp` / `mv` resolves its source with it, and an existence check
reads it for `None`.

`key` is relative to this storage's location. The default `""` is the location
itself — the single source or destination the storage points at — and a
non-empty `key` an entry beneath it.

`on_warning` is the enumeration warning channel, used by the local backend for
its skip messages and ignored by `S3Storage`.

The outcomes are uniform across backends:

- present — a `FileInfo` whose `compare_key` is the entry's basename, with
  `kind` reflecting what was found (a local directory resolves as a
  `DIRECTORY`-kind entry that a transfer later fails on, matching `aws s3`);
- definitively absent (an S3 `404`, a local `ENOENT` / `ENOTDIR`) — `None`,
  with no warning;
- present but not a transferable regular file (a local special device, FIFO or
  socket, or one that fails the readability probe) — a message to `on_warning`
  and `None`;
- existence cannot be determined (a permission error reaching it, a transport
  or 5xx error) — the error is raised.

So `None` means "no transferable entry here", and the caller decides what that
means: a single source raises its own "does not exist", an existence check
proceeds.

The base implementation raises `NotImplementedError` naming
`StorageCapability.GET_FILEINFO`.

### as_text()

Returns this location's canonical path-shape token — the only abstract method.
It is the inverse of `S3.resolve` for a locatable storage: an `S3Storage`
yields `s3://bucket/key` (a keyless location stays slashless, `s3://bucket`), a
`LocalStorage` its path as given, so `S3.resolve(s.as_text())` round-trips. A
stream endpoint has no location, so its token `"-"` is display-only and does
not round-trip. `__str__` delegates here, so `str(storage)` is the same token.

### format(\*, dir_op)

Returns `(root, use_src_name)` for this side of a transfer plan. `root` is what
item keys are resolved against; `use_src_name`, read from the *destination*
side only, is whether that side adopts the source's name. `dir_op` is the
operation's `recursive` flag.

The base implementation is the rule for a custom backend: `root` is `""`,
because such a backend encapsulates its own location and addresses entries by
their relative `compare_key`, and `use_src_name` is true when `dir_op` is set
or `as_text()` ends in `/`. `S3Storage` and `LocalStorage` override it with
their own held state.

### validate()

Runs deferred strict validation on this location. Construction is permissive,
so an operation — or the CLI at its parity point — calls this to reject a
malformed location loudly before use. The base implementation is a concrete
no-op; `S3Storage` overrides it. Idempotent.

When an operation runs the check, a `Boto3S3Error` raised here that names no
operation is stamped with that operation's name; a direct call leaves
`operation` unset, and an exception outside the hierarchy — a backend's own
`ValueError`, say — propagates untouched ([`exceptions.md`](./exceptions.md)).

### default_scan_options()

Builds this backend's own `ScanOptions` value. The base implementation
constructs `scan_options_type` with all field defaults.

This is the single place a backend seeds the scan **source config** it holds on
its instance — how this particular source is read, set once on the constructor.
`LocalStorage` seeds `follow_symlinks` / `detect_symlink_loops` /
`enumerate_all_entries`, `S3Storage` seeds `page_size` / `fetch_owner`. The
high-level operations build from this value and overlay only the run-level
knobs, so a storage's configuration reaches every operation, not just an
arg-less `scan()`.

### supports(needed)

Whether this storage declares every capability in `needed`. The reading lattice
is expanded before the test, so a backend declaring `SORTABLE_SCAN` supports
`SCAN`. This is the same test the transfer gates run; an application can call
it directly to check before it starts.

### missing_capabilities(needed)

The subset of `needed` this storage does not declare, empty when nothing is
missing. Lattice-expanded exactly as `supports` is. The rejection message the
gates raise is built from this.

### Scan ordering

`ScanOptions.sort` requests entries in UTF-8 byte order of their `compare_key`
([`./options.md`](./options.md)). **A backend declaring `SORTABLE_SCAN` must
honor `sort=True`; nothing else promises any order.**

`sync` is the only operation that sets `sort=True` — its merge-join walks both
listings in ascending key order ([`./comparator.md`](./comparator.md)) — and it
is also the only operation that gates on `SORTABLE_SCAN`. Recursive `cp`, `mv`,
`rm` and `ls` leave `sort` at `False`, so a custom storage consumed by them is
processed in whatever order it yields, and a backend whose sort is expensive
may stream its natural order for those and pay the sort only for `sync`.

The built-ins ignore the flag because they are ordered either way, with
carve-outs worth stating exactly. `LocalStorage` sorts regardless of `sort`:
its walk sorts every directory's children for `aws s3` parity, and its
non-recursive one-level scan runs the same sort — but that ordering is the
walker's, so an injected `walker` whose `finalize_children` re-orders or
prunes decides it instead. `S3Storage` relies on `ListObjectsV2`
returning keys in byte order, preserved across pages — but its non-recursive
listing emits each page's common prefixes ahead of that page's objects, which
is `aws s3 ls`'s shape rather than a merged byte order, and S3 Express
directory buckets guarantee no order at all, which is why `sync` rejects them.

## Location

The path argument type the operation APIs accept.

```python
Location = str | os.PathLike[str] | Storage
```

`S3.resolve` interprets a value of this type ([`./s3.md`](./s3.md)): a
`Storage` instance passes through unchanged, a string starting with `s3://`
becomes an `S3Storage` carrying the `S3`'s client, and anything else — a bare
string or an `os.PathLike` — becomes a `LocalStorage`. Passing a `Storage`
instance is how an application supplies a pre-configured built-in (a tuned
`LocalStorage`, an `S3Storage` with its own client) or a backend of its own;
overriding `resolve` is how it adds a URL scheme.

## StorageCapability

A `Flag` enumeration of the transfer operations a `Storage` *kind* implements.
It is the declarative mirror of the contract methods, checked before a transfer
so that an unsupported pairing fails with a clear message instead of a deep
`NotImplementedError` mid-flight.

```python
class StorageCapability(Flag):
    OPEN_READ = auto()
    OPEN_WRITE = auto()
    GET_FILEINFO = auto()
    SCAN = auto()
    SORTABLE_SCAN = auto()
    DELETE = auto()
```

The members mirror the methods one-to-one, because support genuinely differs
per kind — `S3Storage` reads through `open("rb")` but does not write through
`open("wb")`, and a single-URL backend can read one object without being able
to enumerate anything.

`OPEN_READ` and `OPEN_WRITE` promise `open(key, "rb")` and `open(key, "wb")`
respectively; a backend that implements one mode declares only that one.
`GET_FILEINFO` promises `get_fileinfo(key)`. `SCAN` promises `scan` /
`scan_pages`. `SORTABLE_SCAN` promises that `scan` yields entries in UTF-8 byte
order of their `compare_key` when `ScanOptions(sort=True)` asks for it.
`DELETE` promises `delete(info)`.

The three reading members form a lattice: `SORTABLE_SCAN` implies `SCAN`
implies `GET_FILEINFO`, since ordered enumeration implies enumeration implies
single-entry resolution. A backend need only declare its strongest reading
member. The expansion is applied wherever capabilities are tested — by
`supports`, by `missing_capabilities`, and so by every gate — and never to the
stored `capabilities` value, which reads back exactly as declared.

Which flags each route requires is in
[Capability gates](#capability-gates) above.

## LocalStorage

A local filesystem path as one side of a transfer. The recursive walk itself
lives in a composed [`LocalFileGenerator`](#localfilegenerator), so a custom
traversal is injected with `walker=` rather than by subclassing this class.

```python
class LocalStorage(Storage):
    scheme: ClassVar[str] = "local"
    sep: ClassVar[str] = os.sep
    capabilities: ClassVar[StorageCapability] = (
        StorageCapability.OPEN_READ
        | StorageCapability.OPEN_WRITE
        | StorageCapability.GET_FILEINFO
        | StorageCapability.SCAN
        | StorageCapability.SORTABLE_SCAN
        | StorageCapability.DELETE
    )
    scan_options_type: ClassVar[type[ScanOptions]] = LocalScanOptions
    scan_pages_filters: ClassVar[bool] = True

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        walker: LocalFileGenerator | None = None,
        follow_symlinks: bool = True,
        detect_symlink_loops: bool = False,
        enumerate_all_entries: bool = False,
        fsync: bool = False,
    ) -> None: ...
```

`capabilities` is the full set: the local filesystem supports byte I/O both
ways, single-entry stat, a sorted walk, and delete. `sep` is the host-native
separator, the one backend whose `format` roots are not `/`-separated.
`scan_pages_filters` is `True` because the walk applies `options.filter`
itself.

`path` is the location, taken as given; `os.PathLike` is accepted. It is
absolutized once, at construction, against the then-current working directory.
Every scan, `get_fileinfo`, `open` and transfer plan anchors on that absolute
form, so a relative `path` keeps meaning the same directory if the process
later changes directory, and `FileInfo.key` always comes out absolute.

`walker` replaces the default `LocalFileGenerator` with an application's
subclass. The walker is stateless across a walk, so one instance may be shared
by several `LocalStorage` instances.

`follow_symlinks`, `detect_symlink_loops` and `enumerate_all_entries` are this
source's scan configuration. They are held on the instance and seeded into
every scan through `default_scan_options`, so an operation inherits them
without being passed them; their per-entry meanings are on `LocalScanOptions`
([`./options.md`](./options.md)). `get_fileinfo` reads `follow_symlinks` only —
the enumeration and loop settings shape a walk and do not apply to resolving
one entry.

`fsync` is a destination-side durability knob, default off for `aws s3` parity.
When set, a `mv` whose download lands in this storage flushes the file to disk
before the S3 source is deleted, and on POSIX its parent directory as well — a
directory has no fsyncable handle on Windows, where the file flush alone is the
durability step. The transfer engine reads it off the destination side and only
on the `mv` download route.

`path`, `abspath`, `fsync` and `walker` are read-only properties exposing the
constructor input, the construction-time absolute form, the durability knob and
the walk strategy respectively.

### relative_path(filename, start=os.path.curdir)

A static method rendering a local path relative to `start` in the form `aws`
prints in transfer result lines and warnings: the directory part is
relativized and the basename joined back on, so an in-tree path always carries
a directory prefix (`./a.txt`, `../x/a.txt`). Where no relative path exists —
different Windows drives — the absolute path is returned rather than raising.

### scan_pages(options)

Yields entries under the location, paged on directory-read boundaries. Requires
a `LocalScanOptions`; a foreign `ScanOptions` raises `TypeError` rather than
silently walking with defaults. The passed options are copied with this storage
set as the producing backend before the walk starts, so entries are stamped
with it whatever the caller left there.

A recursive scan drives the walker's `list_file_pages`. Pages fall on directory
boundaries: a directory's sorted files accumulated so far are handed off just
before each descent into a sub-directory, so one `os.scandir` produces one page
per descent boundary — several for a directory with interleaved
sub-directories — and a page never spans two directory reads.

A non-recursive scan yields one level as a single page, in the same sort order,
with sub-directories surfacing as `DIRECTORY`-kind entries whose key ends in
`/` rather than being descended. A non-directory location is the single-entry
case, stat'd honoring the passed options' `follow_symlinks` with `compare_key`
set to the basename.

Under `enumerate_all_entries` both forms lead with the scanned path's own
record, whose `compare_key` is `""`; the recursive form then walks, the
non-recursive form emits the immediate entries without descending.

`FileInfo.key` is the absolute path with `os.sep` folded to `/`, and
`compare_key` is relative to the directory being enumerated. Every entry
carries the `stat_result` it was classified from and its `is_symlink` flag
([`./results.md`](./results.md)).

### walk_local(\*, on_warning=None)

Yields every file under the location recursively, as a flat stream, in the
walker's byte order. A thin wrapper over `self.walker.list_files` anchored at
the absolutized path with a trailing separator. It reads the storage's held
scan configuration through `default_scan_options` and overlays `on_warning`
per call. This is the raw walk: `ScanOptions.filter` is not applied, since no
options object is taken.

### default_scan_options()

Returns a `LocalScanOptions` seeded with the constructor's `follow_symlinks`,
`detect_symlink_loops` and `enumerate_all_entries`.

### open(key, mode, \*, size=None)

Opens `key` resolved against the absolutized location. `key=""` is the location
itself. `"wb"` creates missing parent directories first and writes **in place**
— no temporary file and rename, so an aborted write leaves a partial file. (The
built-in S3-to-local download route is atomic on failure through `s3transfer`'s
own temporary file; this method is the plain building block.) `size` is unused.

`key` is joined under the location with `os.path.join`, so a `..` or absolute
`key` deliberately resolves outside it: the caller owns both the location and
the key here, so there is no trust boundary to confine. The one untrusted case
— a remote S3 key steering a recursive download's local target — is guarded
separately, where the key arrives from the bucket.

Raises: `NotFoundError`, `AccessDeniedError` or `TransportError`
([`./exceptions.md`](./exceptions.md)), translated from the underlying
`OSError`. Errors raised later while reading or writing the returned file
object are the plain `OSError`s of an ordinary file.

### delete(info)

Unlinks the file at `info.key`, which for this backend is an absolute local
path. Returns `None` — there is no backend response to report. Raises the
translated `NotFoundError` / `AccessDeniedError` / `TransportError`.

### get_fileinfo(key="", \*, on_warning=None)

Stats a single path, anchored at the absolutized location and joined with `key`
for a child, following the same deliberate `..` / absolute-key resolution as
`open`. `FileInfo.key` is absolute and `compare_key` is its basename. Whether a
symlink is followed is the storage's own `follow_symlinks`, not a parameter.

One `os.stat` is taken and reused for the size, the mtime, the special-file
check and the returned `stat_result`, so the checks and the stored snapshot
cannot disagree.

Returns `None` for a definitively absent path (`ENOENT` / `ENOTDIR`, which
includes a broken symlink when following), for a symlink when
`follow_symlinks=False`, and — after warning through `on_warning` — for a
special or unreadable file. A directory is returned with `FileKind.DIRECTORY`
and no type check, and fails later at open the way `aws s3` fails. A stat error
that is not absence is raised, translated.

### as_text() / format(\*, dir_op)

`as_text` returns the constructor path verbatim, unmodified, because the
trailing-separator rule reads that raw token.

`format` returns the construction-time absolute path as the root, so the plan
and the walk agree even if the process changed directory since. An existing
directory, a `dir_op`, or a user-typed trailing `os.sep` each mean directory
semantics: the root gains a trailing `os.sep` and `use_src_name` is `True`.
Otherwise the bare absolute path is returned with `use_src_name` `False`.

## S3Storage

An `s3://bucket/prefix` location together with the boto3 S3 client used to
reach it.

```python
class S3Storage(Storage):
    scheme: ClassVar[str] = "s3"
    capabilities: ClassVar[StorageCapability] = (
        StorageCapability.GET_FILEINFO
        | StorageCapability.SCAN
        | StorageCapability.SORTABLE_SCAN
        | StorageCapability.OPEN_READ
        | StorageCapability.DELETE
    )
    scan_options_type: ClassVar[type[ScanOptions]] = S3ScanOptions
    scan_pages_filters: ClassVar[bool] = True

    def __init__(
        self,
        uri: str | os.PathLike[str],
        *,
        client: S3Client | None = None,
        page_size: int | None = None,
        fetch_owner: bool = False,
    ) -> None: ...
```

`capabilities` omits `OPEN_WRITE`: S3 resolves a single object with
`HeadObject`, enumerates with `ListObjectsV2`, reads with `GetObject` and
deletes with `DeleteObject`, but every S3 *write* rides `s3transfer` rather
than a writable stream. `scan_pages_filters` is `True` because `scan_pages`
sieves each page itself.

**`uri` is the identification contract.** The constructor stores the argument
text, prepending `s3://` when it is absent, and that stored string is what the
`uri` property returns and what `validate` inspects. The `s3://` scheme is
therefore optional here — `S3Storage("bucket/key")` reads exactly as
`S3Storage("s3://bucket/key")` — a deliberate library leniency that `S3.resolve`
does not share, since `resolve` must still route a bare `bucket/key` to local.
The stored text is split once into `bucket` and `key`, which are the properties
every operation reads and from which `as_text`, `format`, `normalized_uri` and
`same_path_as` are all derived. So `uri` is the input as accepted, scheme
filled in, while `as_text()` is the canonical rebuilt token; they differ when
the input carried a trailing slash on a keyless bucket.

An empty bucket part — a bare `"s3://"` — is the service root: `list_buckets`
lists the account's buckets there, while object listing or a transfer needs a
bucket. A key with no bucket (`"s3:///k"`) is rejected by `validate`, not by
the constructor.

The bucket part may be an access point ARN, plain or Outposts, which is kept
whole and passed as the `Bucket` parameter. S3 Object Lambda and Outposts
*bucket* ARNs are rejected by `validate`, the way `aws s3` rejects them.

`client` is the boto3 S3 client. When omitted, a default `boto3.client("s3")`
is built lazily on first use, owned by this instance and released by `close`.
The region is not derived from the URL; for a specific region, endpoint or
profile, pass a pre-built client.

`page_size` and `fetch_owner` are this source's listing configuration, held on
the instance and seeded into every scan through `default_scan_options`.
`page_size` sets the `ListObjectsV2` page size, and the default `None` sends no
`MaxKeys` at all, leaving the server to page at its own default.
`fetch_owner` populates each entry's owner.

`uri`, `bucket` and `key` are read-only properties; `key` may be empty.

Thread safety: a built or supplied client is safe to share across threads for
operation calls. Client *construction* is not, and is deliberately not locked
here. For concurrent use, build the client on the caller side and pass it in
rather than relying on the lazy default.

### same_path(src, dest)

A static method: whether `mv src dest` would move an object onto itself, for
two `s3://` URI strings the caller guarantees are an S3-to-S3 pair.

`True` when the two strings are equal, or when `dest` ends in `/` and joining
`basename(src)` onto it reproduces `src` exactly. `False` otherwise. The join
and basename are `os.path`'s, deliberately: those host semantics are the
contract, which is what makes the check behave identically to `aws s3`,
`ntpath`'s drive-relative reset included.

The rule is applied to recursive moves too, so `mv --recursive s3://b/p
s3://b/` is rejected even though no individual key would map onto itself. That
is a faithful false positive, matching `aws s3`.

### same_key(src, dest)

A static method: whether two `s3://` URI strings name the same *key*, ignoring
buckets. Each side is stripped of its scheme and split into bucket and key,
then the two key parts are compared with the `same_path` rule anchored at `/`
— each key is compared as `/{key}`. A keyless destination therefore matches any
source whose key is its own basename. This gates `mv`'s resolve-and-validate
work and its access point warning.

### same_path_as(dest)

The instance form of `same_path`, computed from held state rather than rebuilt
strings: `True` when the two storages' buckets are equal **and** their keys
satisfy the `same_path` rule anchored at `/`. With equal buckets the URI prefix
is inert, so this is equivalent to running `same_path` over the two
keyless-normalized URIs, with the `/`-anchoring preserving `os.path`'s
semantics exactly.

### split_bucket_key(path) / strip_scheme(path) / normalize_s3_uri(path)

Static string helpers over the S3 path grammar, with no client and no I/O.

`split_bucket_key` splits a scheme-less path into `(bucket, key)`. Access point
ARNs, plain and Outposts, are recognized first so an ARN whose name contains
`/` stays whole in `bucket`; otherwise the split is on the first `/`. Nothing
is validated and either part may be empty.

`strip_scheme` drops a leading `s3://` if present and returns the rest
unchanged otherwise. Composed with `split_bucket_key` it reproduces the AWS
CLI's whole bucket/key splitter, which is how `same_key` uses it.

`normalize_s3_uri` applies the keyless-bucket normalization with the scheme
kept: `s3://bucket`, including a keyless access point ARN, reads as the bucket
root `s3://bucket/`. A bare `s3://` is returned as is, and so is any string
that does not start with `s3://`. It is for raw strings; with an instance at
hand, `normalized_uri` derives the same form from held state.

### normalized_uri()

The keyless-normalized `s3://` URI derived from `bucket` and `key` with no
string round-trip: a keyless bucket reads as `s3://bucket/`, and the bare
service root stays `s3://`. This is the form `mv`'s same-path error message
shows.

### as_text() / format(\*, dir_op)

`as_text` rebuilds `s3://bucket/key` from the held bucket and key rather than
echoing the constructor input, so a keyless location normalizes to a slashless
`s3://bucket` and the service root to `s3://`.

`format` returns the scheme-less `bucket/key` as the root, with the same
keyless-bucket normalization (`s3://bucket` formats as `bucket/`); only the
bare service root formats empty. A `dir_op` root is `/`-terminated and
`use_src_name` is `True`; otherwise `use_src_name` is whether the root already
ends in `/`, and a `dir_op` on the service root formats it as `/`.

### validate()

Rejects the malformed forms deferred from construction, raising
`ValidationError` ([`./exceptions.md`](./exceptions.md)) for an S3 Object
Lambda ARN, an Outposts *bucket* ARN, or a key with no bucket. The library
calls it before an operation and the CLI at its parity point, so a malformed
location fails loudly instead of reaching the API as a cryptic botocore error.
Idempotent.

Each rejection carries what identifies the offending location. The two ARN
forms report the whole ARN as `bucket` and leave `key` unset — the whole ARN,
not this storage's `bucket`, which holds only the part before the first `/`
for an ARN family the splitters do not recognize. The bucket-less form
reports the orphaned key as `key`. `operation` is stamped by the operation that
ran the check and stays unset on a direct call.

### get_client()

Returns the boto3 S3 client, building a default one lazily when none was
supplied. Memoized: the first call builds or returns the client and every later
call returns the same instance. Deliberately not guarded by a lock.

Raises: `ConfigurationError` when credentials or region cannot be resolved,
`InvalidConfigError` for a set-but-unusable profile or a malformed environment
endpoint. A raw botocore error never escapes.

### close()

Closes the lazily-built default client if this instance owns one, and clears
it. A caller-supplied client is left alone.

### default_scan_options()

Returns an `S3ScanOptions` seeded with the constructor's `page_size` and
`fetch_owner`.

### scan_pages(options)

Yields one `list[S3FileInfo]` per `ListObjectsV2` page. Requires an
`S3ScanOptions`; a foreign `ScanOptions` raises `TypeError`.

This is object listing only — the openable-entity enumeration `scan` promises.
A recursive scan omits `Delimiter` and yields every object as a `FILE` entry; a
non-recursive scan sends `Delimiter='/'` and additionally emits one
`DIRECTORY`-kind entry per sub-"directory", ahead of that page's objects.
`FileInfo.key` is the full S3 key, or the prefix for a directory entry, and
`compare_key` is that key with the listing's `Prefix` removed.

`options.prefix` overrides the storage's own key as the listing anchor, driving
both the `Prefix` sent and the `compare_key` relativization, so a transfer
lists through the storage instance it was handed rather than rebuilding one.
`options.fetch_owner` sends `FetchOwner=True`. `options.request_payer`, when
set, is forwarded. `options.page_size` becomes the paginator's page size.

`options.filter` is applied here: the raw pages are sieved client-side before
being yielded, and a page the predicate empties is skipped rather than yielded
empty. This is also the override seam — filter or enrich each page, then defer
to `super().scan_pages(options)` — and the work runs on `scan`'s prefetch
worker.

The bare service root is not an object container: reaching object listing with
an empty bucket fails with botocore's `Invalid bucket name` validation,
matching `aws s3 cp` / `rm` / `sync` against `s3://`. Use `list_buckets`
instead.

Raises: botocore errors from the fetch are translated into the library taxonomy
([`./exceptions.md`](./exceptions.md)) and surface on the consumer's pull.

### list_buckets(\*, name_prefix=None, region=None)

Lists the account's buckets as `BUCKET`-kind `FileInfo`, each entry's `key` and
`compare_key` being the bucket name and its `mtime` the creation date. This is
the S3-only counterpart to `scan_pages`, kept separate because a bucket is a
container rather than an openable entity, so no transfer scan ever yields one.
`S3.ls` dispatches here at the service root.

`name_prefix` and `region` map to `ListBuckets`'s `Prefix` and `BucketRegion`,
omitted when falsy. The page size is the storage's own `page_size`.

Below the botocore version that added the `ListBuckets` paginator, this falls
back to a single unpaginated call in which those two filters are never sent and
are simply inert. On a botocore that paginates but predates the `Prefix` /
`BucketRegion` input parameters, passing either raises a botocore
`ParamValidationError` (see [`../compatibility.md`](../compatibility.md)).
Errors surface on the consumer's pull.

### open(key, mode, \*, size=None)

Opens the object at `key` — the object's **full** bucket key, exactly what its
`FileInfo.key` carries and what `delete` addresses — and implements `"rb"`
only.

`"rb"` performs a `GetObject` and returns botocore's streaming response body: a
read-only, forward-only stream with no `seek`, supporting the context-manager,
`read` and `close` protocol. It is a read convenience for building blocks over
S3, chiefly a content-based `sync` filter that must read an object's bytes
([`../library/sync-content.md`](../library/sync-content.md)). `size` is unused.

`request_payer` and an SSE-C key are not on the generic `open` signature, so a
requester-pays or SSE-C object must be read through the client directly.

`"wb"` raises `NotImplementedError`. Every S3 write rides `s3transfer` — the
built-in routes, the S3 side of an open-route transfer, and the stream path
alike — so the multipart upload a writable stream would need has no caller.

Raises: errors from the `GetObject` itself (a missing key, denied access) are
translated into the library taxonomy. An error raised *later* while reading the
streamed body — a read timeout, a broken stream — surfaces as botocore's,
untranslated, exactly as `LocalStorage.open`'s file object surfaces its read
errors.

### delete(info, \*, request_payer=None)

Deletes one object with a single blind `DeleteObject` call: no listing and no
`HeadObject`, so deleting a key that does not exist succeeds, matching `aws s3
rm`'s single-key path. The object is `info.key`, a full bucket key.

`request_payer` is an S3-specific keyword added on top of the cross-backend
`Storage.delete` signature.

Returns the `DeleteObject` response. `rm`'s blind single-key path surfaces it
under `extra_info["delete"]` when `capture_response` is set; `mv` and the
batched `rm` / `sync` lanes issue their own delete calls rather than this
method.

### get_fileinfo(key="", \*, on_warning=None)

Performs a `HeadObject` on a single key. `key=""` heads the storage's own key;
a non-empty `key` is joined beneath it with a `/` boundary inserted only when
the prefix does not already end in one, so a keyless or trailing-`/` prefix
needs none and a bare `prefix` still yields `prefix/key`.

The returned `S3FileInfo` carries the full `HeadObject` response as `head`, and
its `compare_key` is the key's basename.

`on_warning` does not apply to S3 and is ignored.

Raises: a `404` returns `None` rather than raising. Any other error — `403`,
transport, 5xx — is raised translated, because existence could not be
determined. This is the generic HEAD; the SSE-C-aware single-source HEAD lives
in the transfer engine.

## IOStorage

One caller-supplied stream presented as a `Storage`: a single `open`-able
endpoint, not a container.

```python
class IOStorage(Storage):
    scheme: ClassVar[str] = "stream"
    capabilities: ClassVar[StorageCapability] = (
        StorageCapability.OPEN_READ | StorageCapability.OPEN_WRITE
    )

    def __init__(
        self, stream: IO[bytes] | IO[str], *, encoding: str = "utf-8"
    ) -> None: ...
```

`capabilities` is just the `OPEN_*` pair: a single stream supports byte I/O in
both directions, chosen per `open` call, with no listing and no deletion.

`stream` is the caller's file-like object. A binary stream is used as it is,
behind a close-suppressing view. A text stream — recognized either as an
`io.TextIOBase` or by carrying an `encoding` attribute, which covers
`codecs.open`'s reader/writer and a text-mode `SpooledTemporaryFile` — is
wrapped with an incremental codec instead, encoding on read for an upload and
decoding on write for a download.

`encoding` is that codec's encoding and applies only to a text stream. A single
incremental encoder or decoder spans the whole transfer, so a stateful codec
behaves as one stream — utf-16 emits its BOM once, not per chunk.

**The caller's stream is never closed by this class, and never repositioned.**
The transfer closes every file object `open` returns, since that close is how a
real backend flushes a write; each view here absorbs that close into a flush of
the wrapped stream at most. Lifecycle and final position stay the caller's:
after a download the stream sits at the end of the written bytes.

Of the four contract operations only `open` is implemented. `get_fileinfo` and
`delete` keep `Storage`'s base implementations and raise `NotImplementedError`
when called; `scan` is a generator, so the same error from `scan_pages`
surfaces on its first pull rather than at the call.

Where a stream may appear is enforced by the operations, not by capabilities:
it is a side of a non-recursive `cp`, or the destination of a non-recursive
`mv`; it is never a `mv` source, never a recursive `mv` destination, never both
sides at once, and never an `ls` or `rm` target. `S3.cp` and `S3.mv` raise
`ValidationError` for those cases ([`./operations/cp.md`](./operations/cp.md),
[`./operations/mv.md`](./operations/mv.md)).

### open(key, mode, \*, size=None)

Returns the wrapped stream as a binary stream. `key` and `size` are ignored — a
single endpoint takes no key. `"rb"` yields a reader over the stream, `"wb"` a
writer into it; for a text stream those are the encoding and decoding adapters
built from `encoding`.

### as_text()

Returns `"-"`. A stream has no location, so this token is for display and error
messages only and is not round-trippable through `S3.resolve`. The default
`Storage.format` reads it: `"-"` has no trailing `/`, so a single move onto a
stream keeps its key.

## StdioStorage

The process's own standard streams as a `Storage` — `sys.stdin` to read,
`sys.stdout` to write. A subclass of `IOStorage` that holds no stream of its
own.

```python
class StdioStorage(IOStorage):
    scheme: ClassVar[str] = "stdio"

    def __init__(self) -> None: ...
```

The constructor takes no arguments. `capabilities` and `as_text` are
`IOStorage`'s, so this too declares `OPEN_READ | OPEN_WRITE` and renders as
`"-"`; `scheme`, the constructor and `open` are what differ.

### open(key, mode, \*, size=None)

Selects the process stream by `mode`, so one instance serves either direction
and picks up a `sys.stdin` / `sys.stdout` redirected after construction. Both
are used through their `.buffer` when they have one.

`"rb"` reads `sys.stdin` through a read-only view that hides `seek`, so
`s3transfer` takes its buffered upload path — some streams that are not truly
seekable report otherwise, Windows stdin among them.

`"wb"` writes `sys.stdout` through a write-only view, so a download always
streams sequentially even when stdout is redirected to a seekable file: a
`>>`-opened file lands every write at the end regardless of position, which a
seek-based parallel download would interleave.

Raises: `ValidationError` ([`./exceptions.md`](./exceptions.md)) when the
selected process stream is unavailable, raised here rather than letting a
transfer worker receive an unusable file object.

## LocalFileGenerator

The customizable local directory walk — this library's counterpart to the AWS
CLI's file generator, scoped to the local filesystem. It is a public extension
point: an application subclasses it, overrides the public methods it wants, and
injects an instance with `LocalStorage(path, walker=...)`. No surgery on
`LocalStorage` itself is needed or intended.

`list_files` reproduces the AWS CLI's observable behaviour: the same depth-first
traversal whose per-directory sort key appends `os.sep` to directory names and
compares with separators normalized to `/`, so the stream comes out in S3's
UTF-8 byte order (`foo.txt` before `foo/bar`), and the same skip-with-warning
rules for nonexistent, special and unreadable files, broken symlinks and the
invalid-timestamp epoch fallback.

The class is **stateless across a walk**. Every per-walk input — the symlink
policy, the warning channel, the producing storage — rides on the
`LocalScanOptions` passed in, never on the instance, so one walker may be
shared by several `LocalStorage` instances and each scan stamps its own
storage.

```python
class LocalFileGenerator:
    have_dir_fd: ClassVar[bool]
    dir_open_flags: ClassVar[int]
    EPOCH_TIME: ClassVar[datetime]

    def list_files(
        self, root: str, options: LocalScanOptions
    ) -> Iterator[LocalFileInfo]: ...
    def list_file_pages(
        self, root: str, options: LocalScanOptions
    ) -> Iterator[list[LocalFileInfo]]: ...
    def walk_dir(
        self,
        dir_path: str,
        options: LocalScanOptions,
        *,
        strip: int,
        notify: Callable[[str], None],
        detector: LoopDetector | None,
        sym_depth: int = 0,
    ) -> Iterator[list[LocalFileInfo]]: ...
    def root_info(
        self,
        root: str,
        *,
        options: LocalScanOptions,
        notify: Callable[[str], None],
    ) -> LocalFileInfo | None: ...
    def scan_children(
        self,
        dir_path: str,
        *,
        strip: int,
        options: LocalScanOptions,
        notify: Callable[[str], None],
        sym_depth: int = 0,
    ) -> list[WalkChild]: ...
    def crosses_full_path_boundary(
        self,
        info: LocalFileInfo,
        full: str,
        *,
        sym_depth: int,
        notify: Callable[[str], None],
    ) -> bool: ...
    def entry_stat_result(
        self, entry: os.DirEntry[str]
    ) -> os.stat_result | None: ...
    def classify_child(
        self,
        entry: os.DirEntry[str],
        full: str,
        dir_fd: int | None,
        *,
        options: LocalScanOptions,
        notify: Callable[[str], None],
    ) -> WalkChild | None: ...
    def dir_child(
        self, entry: os.DirEntry[str], full: str, st: os.stat_result
    ) -> WalkChild: ...
    def symlink_child(
        self,
        entry: os.DirEntry[str],
        full: str,
        *,
        notify: Callable[[str], None],
    ) -> WalkChild | None: ...
    def finalize_children(
        self, children: list[WalkChild]
    ) -> list[WalkChild]: ...
    def normalize_sort(self, children: list[WalkChild]) -> list[WalkChild]: ...
    def should_ignore_file(
        self, path: str, *, follow_symlinks: bool, notify: Callable[[str], None]
    ) -> bool: ...
    def triggers_warning(
        self, path: str, notify: Callable[[str], None]
    ) -> bool: ...
    def should_ignore_entry(
        self,
        entry: os.DirEntry[str],
        full: str,
        dir_fd: int | None,
        st: os.stat_result,
        *,
        notify: Callable[[str], None],
    ) -> bool: ...
    def stat_info(
        self,
        entry: os.DirEntry[str],
        full: str,
        st: os.stat_result,
        notify: Callable[[str], None],
    ) -> LocalFileInfo: ...
```

`have_dir_fd` is whether this platform can scan a directory through its file
descriptor and stat or open entries relative to it. It is a feature probe
rather than an `os.name` test, so any platform missing the APIs degrades
correctly; it is true on POSIX and false on Windows, whose directory scan
returns entry attributes inline instead. `dir_open_flags` are the flags for the
one `open()` that turns a directory path into that descriptor. `EPOCH_TIME` is
the timestamp stamped on a file whose mtime cannot be represented.

**The override seams, finest first.** Extend at the smallest layer that fits:
`should_ignore_entry` (one entry's vetting), `entry_stat_result` (the one stat
per entry), `stat_info` and `dir_child` (one entry's `LocalFileInfo`),
`classify_child` (one directory entry to a `WalkChild` or a skip),
`finalize_children` (a directory's children as a whole),
`scan_children` (how a directory is read), `walk_dir` (how the tree is
descended), `list_file_pages` (the paged entry point), `list_files` (the flat
one).

Two invariants an override must preserve. `compare_key` is stamped in
`scan_children`, before `finalize_children` runs, so any injected child needs
its own `compare_key` stamped as `info.key[strip:]`. `FileInfo.storage` is
*not* the walker's to set — `list_file_pages` stamps the producing backend on
each yielded page, before the visibility filter — so an override shapes the
subtree from `compare_key`, `key` and `kind`, not from a backend handle.

### list_files(root, options)

Yields every file under `root` recursively as a flat stream in byte order.
`list_file_pages` flattened; see it for the walk semantics.

### list_file_pages(root, options)

Yields the files under `root` recursively as byte-ordered pages — the paged
form that drives `LocalStorage.scan_pages`. One page per directory file run:
the sorted entries between two sub-directory descents. A directory's files can
only surface once its `os.scandir` has been read in full and sorted, and they
are handed off just before the next descent, so a page never spans two
directory reads and a consumer can overlap the next read. The pages concatenate
to `list_files`'s flat stream.

`root` is the absolute directory path **with a trailing `os.sep`**. Each
`LocalFileInfo.key` is the absolute path with `os.sep` folded to `/`, and
`compare_key` is relative to that directory — the axis `options.filter`
matches.

The fields of `options` this reads: `on_warning` (the warning channel, dropped
when `None`), `enumerate_all_entries`, `follow_symlinks`,
`detect_symlink_loops`, `filter`, and `storage`. Under
`enumerate_all_entries` the root leads in a one-record page and each descended
directory rides the page flushed before its children. `filter` is applied here,
on the yielded pages, *after* the vetting battery — so an excluded file still
emits the warnings the AWS CLI would in the normal view — and after the
producing backend is stamped, so a predicate can read `info.storage`. The root
itself is vetted with `should_ignore_file`; its children per entry in
`scan_children`.

`detect_symlink_loops` takes effect only together with `follow_symlinks`, since
a cycle requires a followed link; without both, no `LoopDetector` is built and
the walk pays no per-directory stat for it.

### walk_dir(dir_path, options, \*, strip, notify, detector, sym_depth=0)

The depth-first recursion behind `list_file_pages`, and the seam for changing
how the tree is descended. Each directory's `scan_children` is read and sorted
in full, its files collected into a run, and the run handed off as one page just
before descending into each sub-directory — via `self.walk_dir`, so an override
applies at every level.

`dir_path` has already been vetted, by `list_file_pages` for the root and by
the parent's `scan_children` for a child. `strip` is the prefix length of the
directory passed to `list_file_pages` and stays constant across the recursion;
`scan_children` uses it to stamp `compare_key`. `notify` is the warning sink.
`detector`, when present, guards symlink cycles: a sub-directory whose identity
matches an ancestor is skipped with a `Symbolic link loop detected` warning.
`sym_depth` is the number of followed symlinks between the walk root and
`dir_path`, threaded down so a leaf near the OS symlink limit can be re-vetted
by full path.

Override it and return early for a directory to prune its subtree, or
re-implement the loop to cap depth — preserving the depth-first byte order
`sync`'s merge-join relies on. `compare_key` stamping and `options.filter` are
not this method's job.

### root_info(root, \*, options, notify)

Returns the scanned path's own entry for the complete-enumeration view, or
`None` when it cannot be classified. `root` is the absolutized path being
scanned, with a trailing separator. Its `compare_key` is the empty string,
because the record represents the scanned path itself; that value sorts before
every child key, so the record leads the stream. Note that a glob filter sees
that `""` — a lone `*` matches it, a non-empty literal does not.

A directory key carries the trailing separator and is descended; any other
scanned path is the scan's sole leaf. `stat_result` is the followed stat, or
the lstat selected when symlinks are not followed or a follow failed, and
`is_symlink` reflects the scanned path itself.

### scan_children(dir_path, \*, strip, options, notify, sym_depth=0)

Returns one directory's vetted children as final `WalkChild` entries — the
enumeration layer, and the seam for changing *how* a directory is read.

It scans `dir_path` once with `os.scandir`, turns each entry into a `WalkChild`
through `classify_child` (a skip returns `None`), stamps each surviving child's
`compare_key` as `info.key[strip:]`, and hands the list to `finalize_children`,
which sorts it. Where the platform allows, the directory is opened once and
scanned through its own descriptor, so each entry's stat and readability probe
are directory-relative.

A path contributes at most one child, never two: a followed symlink describes
its target, and a non-followed symlink in the complete view describes the link
itself.

A directory that cannot be opened or scanned — a symlink cycle stopped by the
kernel, an over-long path, or a race after its parent vetted it readable — is
put through the `triggers_warning` battery and yields an empty list. If that
battery finds nothing wrong, the `OSError` propagates instead of pruning
silently. That handling is scoped to establishing the scan; a per-entry
`OSError` raised mid-scan propagates.

`sym_depth` lets a file leaf near the symlink-loop or path-length limit be
re-vetted by full path, so it warn-skips the way the AWS CLI's full-path stat
would rather than being admitted and failing at transfer time.

An override should reuse `classify_child`, `dir_child` and `normalize_sort`
rather than reproducing the sort key and directory-info shape, and must stamp
`compare_key` on any child it injects.

### crosses_full_path_boundary(info, full, \*, sym_depth, notify)

Whether a vetted file leaf must be dropped because a full-path stat would have
rejected it. The fast walk vets each entry through the owning directory's
descriptor, which re-anchors resolution and so hides both the ancestor symlink
chain and the full path's length; near either OS limit this re-runs the
full-path warning battery so the two agree. Returns `False` for a directory,
which is already covered by its own descent failing. `scan_children` consults
it only in the normal view — under `enumerate_all_entries` the vetting is
bypassed and this probe with it.

### entry_stat_result(entry)

The walk's **one** stat snapshot per entry, and a single-point override seam.
`classify_child` calls it once and threads the result through everything
downstream — the vetting, the file-versus-directory decision, the size and
mtime, the loop key, and the stored `stat_result`. The default reads the
directory entry's cache, one syscall for the whole entry, and returns the
**followed** stat, so the walk follows symlinks like `aws s3`.

Override it to return the link's own lstat and the walk turns lstat-based in
one place, still one syscall: a symlink then surfaces as its own entry, a
symlinked directory is not descended because its mode is not a directory, and
size, mtime and `stat_result` all describe the link. The readability probe in
`should_ignore_entry` still opens through the link, so pair the two overrides
to also admit broken links.

`None` — an `OSError` — makes the caller treat the entry as gone.

### classify_child(entry, full, dir\_fd, \*, options, notify)

Turns one directory entry into a `WalkChild`, or returns `None` to skip it —
the natural per-entry override point, and the owner of the single stat.

Symlinks are decided first, on a free type test: with symlinks not followed,
the complete view returns the link's own lstat leaf and the normal view skips
it silently. Then `entry_stat_result` is taken once. A `None` stat warns; the
complete view falls back to the link's lstat and the normal view skips. A stat
that itself reports a symlink — which only an lstat-style override produces —
is its own vetting-free leaf. Otherwise the normal view vets the entry through
`should_ignore_entry`, and the kind is keyed on that same stat: a directory
becomes `dir_child`, anything else `stat_info`. Keying on the stat rather than
a fresh directory probe is what lets an lstat override classify a symlinked
directory as a non-descended entry.

`full` is the entry's absolute path; `dir_fd` is the owning directory's
descriptor, or `None` where the platform has none.

### dir_child(entry, full, st)

Builds a sub-directory's `WalkChild`: a `DIRECTORY`-kind `LocalFileInfo`
carrying `st` and the entry's symlink flag, a `sort_name` ending in a trailing
`os.sep` so the directory sorts after a sibling file of the same stem, an info
key ending in `/` (the appended separator is folded with the rest of the path),
and a `loop_key` of `(st_dev, st_ino)` — `None` when the inode number is zero,
which some FAT, exFAT and FUSE volumes report, so loop detection fails open
there.

### symlink_child(entry, full, \*, notify)

Builds a symlink's own leaf for the complete no-follow or follow-failure view.
It is lstat-based, so size, mtime and `stat_result` describe the link rather
than its target, and vetting-free, since a link is a name plus a target with no
content to probe — a broken link is a returned entry like any other. The child
is never descended (`loop_key` is `None`). Returns `None` when the entry raced
away and even the lstat failed.

### finalize_children(children)

Produces a directory's final child list from its vetted, `compare_key`-stamped
children — the seam for shaping a directory as a whole before the walk consumes
it. The default is exactly `normalize_sort`.

The list arrives **unsorted**, so pruning here happens before the sort; call
`super().finalize_children` or `normalize_sort` for byte order. **Dropping a
`DIRECTORY` child prunes its whole subtree**, because the walk never descends
what this does not return — a saving `aws s3` cannot make, since it vets every
file, which is why this is the right seam for a non-parity backup walk and not
the default. A backup walker normally customizes only this method.

Note that a `finalize_children` override prunes by its own rules and never sees
`options.filter`, so it does not by itself justify setting
`scan_pages_filters`.

### normalize_sort(children)

Sorts children into the AWS CLI's byte order, in place, and returns the list.
The sort folds `os.sep` to `/`, so string order equals UTF-8 byte order — S3's
exact key order — and a directory `foo/` sorts after a sibling file `foo.txt`,
since `/` is `0x2F` and `.` is `0x2E`.

### should_ignore_file(path, \*, follow_symlinks, notify)

The path-based vetting used for the walk root, which has no directory entry to
read: a silent skip for a symlink when symlinks are not followed (with a
trailing separator stripped first so the link itself is tested), then the
`triggers_warning` battery. Returns whether the path is to be skipped. The
per-entry hot path uses `should_ignore_entry` instead.

### triggers_warning(path, notify)

The warn-and-skip battery on a path, in the AWS CLI's order and wording: the
path does not exist, then it is a character or block special device, FIFO or
socket, then it is not readable. Returns whether any check fired, having sent
that check's message to `notify`. Used for the root, for a directory whose scan
could not be established, and for the near-boundary re-vetting.

### should_ignore_entry(entry, full, dir\_fd, st, \*, notify)

The same battery on one entry's already-taken stat, the directory-entry form of
`triggers_warning`. It decides only whether a *present* entry is ignorable — a
special file, or one the process cannot read — since the no-follow skip and the
"does not exist" case belong to `classify_child`. The readability probe is
directory-relative where the platform allows.

One refinement on the probe's failure path: an entry whose full-path resolution
fails, because it exceeds the host's path limit or raced away, warns "File does
not exist." rather than "not readable", matching the AWS CLI, which vets by
full path and probes existence first. A genuinely unreadable present entry
still warns "not readable".

### stat_info(entry, full, st, notify)

Builds one file entry's `LocalFileInfo` from the stat `classify_child` already
took. It never fails: the race case was handled upstream, and an unrepresentable
mtime keeps the file, warns, and stamps `EPOCH_TIME`. The info carries `st` as
`stat_result` and the entry's symlink flag.

## WalkChild

One vetted child of a directory — the element `scan_children` yields to the
walk, and part of the walk override contract.

```python
class WalkChild(NamedTuple):
    sort_name: str
    info: LocalFileInfo
    loop_key: tuple[int, int] | None
```

`sort_name` is the name the byte-order sort keys on: a plain file name, or a
directory name with a trailing `os.sep` appended so that a directory sorts
after a sibling file of the same stem. `walk_dir` also joins it onto the parent
path to reach a sub-directory, so the separator it carries is load-bearing, not
decorative.

`info` is the entry's `LocalFileInfo` ([`./results.md`](./results.md)): a
`FILE`-kind entry for a file, or a `DIRECTORY`-kind one for a sub-directory,
which the walk recurses into rather than yields — unless
`enumerate_all_entries` is set, in which case the directory's own record is
emitted on the page flushed just before its descent.

`loop_key` is a directory's `(st_dev, st_ino)` identity for cycle detection. It
is `None` for a file, and also `None` when the identity is unknown, which makes
loop detection fail open for that subtree.

Being a `NamedTuple`, it unpacks positionally — `for sort_name, info, loop_key
in ...` is how the walk consumes it — and its fields are equally reachable by
name. A subclass re-implementing `scan_children` builds these through
`classify_child` or `dir_child` and orders them with `normalize_sort`, so it
need not reproduce the sort key or the directory-info shape.

## LoopDetector

An ancestor-stack guard against symbolic-link cycles in a recursive walk. It is
a reusable building block: `LocalFileGenerator`'s walk drives it under
`detect_symlink_loops`, and an application walking its own backend can use it
directly.

```python
class LoopDetector:
    def __init__(self, root: str) -> None: ...
    def is_cycle(self, path: str) -> bool: ...
    def is_cycle_key(self, key: tuple[int, int] | None) -> bool: ...
    def leave(self) -> None: ...
```

It tracks the `(st_dev, st_ino)` identity of every directory on the path from
the root down to the one being descended — an **ancestor stack**, not a global
visited set. A directory whose identity matches an ancestor is a cycle; a
legitimate diamond, two symlinks to the same external directory, is still
followed on both arms.

It **fails open**: a directory with no stable identity — an `os.stat` error, or
the zero inode number some FAT, exFAT and FUSE volumes report — never matches
an ancestor, so the walk keeps descending rather than risk a false positive.

The constructor seeds the stack with `root`'s identity.

The usage contract is that a non-cycle answer **pushes**, so it must be paired
with a `leave` once the descent returns:

```python
if detector.is_cycle(subdir):
    ...          # skip: descending would loop
else:
    try:
        ...      # recurse into subdir
    finally:
        detector.leave()
```

### is_cycle(path)

Whether descending `path` would re-enter an ancestor. Stats `path` for its
identity. On `False` the identity is pushed as the stack's new tip.

### is_cycle_key(key)

`is_cycle` for a caller that already holds the `(st_dev, st_ino)` pair, so a
walk that captured the identity from an entry's cached stat never restats just
to check. `None` fails open: it is pushed, and never matches an ancestor.

### leave()

Pops the directory pushed by the matching non-cycle `is_cycle` or
`is_cycle_key` call.
