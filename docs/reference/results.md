# Results, progress and cancellation

The per-item record `cp` / `mv` / `rm` / `sync` deliver to `on_result`, the
byte-level record `on_progress` receives, the listing entry types `ls` and
`Storage.scan` produce, the three callback aliases, and the cancellation token
the batch operations accept. The narrative — worked callbacks, reconciling
counts, when to cancel — is in [`../library/results.md`](../library/results.md);
the rationale for a single record type and the per-operation field table are in
[`../../design/opresult.md`](../../design/opresult.md).

Every symbol on this page is exported from the `boto3_s3` root and from
`boto3_s3.types`.

## OpResult

The per-item completion record passed to the `on_result` callback of `cp` /
`mv` / `rm` / `sync`. It is one type discriminated by `transfer_type` and
`outcome`, not a class per event kind: the caller already knows which operation
it invoked, so the record only states what happened to this item. Instances are
built by the library and handed to the callback; nothing in the public API
consumes a caller-built one.

Emission is not confined to the calling thread. Submitted transfers report from
the transfer engine's worker threads — including the warning a failed
post-download mtime stamp adds — batched deletes from the deleter's worker
thread, and a backend walk's enumeration warnings from the scan's prefetch
worker; a `sync` walks both sides through one shared warning sink, so its two
prefetch workers can report concurrently. The remaining records — dry runs,
skips, notices, the gate warnings raised while items are produced, the
single-key `rm`, and local or custom-backend deletes — are emitted inline on
the thread that called the operation. With `use_threads=False` in the
`TransferConfig` ([`options.md`](./options.md)) the transfer engine also reports
on the calling thread. The callback must therefore be fast, thread-safe, and
non-raising; see [`ResultCallback`](#resultcallback) for what a raising callback
costs.

Every item the operation acts on produces exactly one terminal record —
`SUCCEEDED`, `FAILED`, `SKIPPED`, `DRYRUN`, or `CANCELLED`. An item that never
reaches the operation produces nothing: one a filter excluded during
enumeration, one a `sync` decision declined to act on (`create_filter`,
`update_filter`, or `delete_filter`,
[`operations/sync.md`](./operations/sync.md)), or one never enumerated because
the run ended first. Two carve-outs exist, both of them items consumed before
submission or discarded after acceptance:

- an item a pre-transfer gate rejects with an advisory instead of an outcome:
  the glacier gate without `ignore_glacier_warnings` (one `WARNED` record), a
  download blocked by `case_conflict=CaseConflictMode.SKIP` (one `NOTICE`
  record), and a download whose compare key resolves above the destination
  directory (one `WARNED` record). Under `ignore_glacier_warnings` the glacier
  gate emits a terminal `SKIPPED` record instead. Both option keys are on
  `TransferOptions` ([`options.md`](./options.md));
- entries discarded from the deleter's unsent buffer — accepted but never
  dispatched — which produce no record at all. A cancellation discards them,
  and so does an exception that ends the run, such as a listing failure
  part-way through ([`../../design/deleter.md`](../../design/deleter.md)).

`WARNED` and `NOTICE` sit outside that rule in the other direction as well.
Being advisories rather than outcomes, they are not tied one-to-one to an item:
a directory-walk warning belongs to no item at all, a `NOTICE` may precede the
same item's real outcome, and a problem that does not consume the item — a
post-download mtime stamp that failed, a source that exceeds S3's upload size
limit — adds a `WARNED` record beside the item's own terminal one. A consumer
must not assume one callback per key.

The aggregate counts always agree with the records delivered: `BatchError`'s
`succeeded` / `failed` / `warned` / `skipped` are the counts of the records of
each outcome ([`exceptions.md`](./exceptions.md)).

```python
@dataclass(slots=True, kw_only=True)
class OpResult:
    transfer_type: TransferType
    compare_key: str
    outcome: OpOutcome
    bytes_transferred: int = 0
    error: Boto3S3Error | None = None
    src: str | None = None
    dest: str | None = None
    src_info: FileInfo | None = None
    dest_info: FileInfo | None = None
    src_storage: Storage | None = None
    dest_storage: Storage | None = None
    extra_info: Mapping[str, Any] | None = None
```

`transfer_type` is the verb the record reports under. An `mv` reports `MOVE` on
every record while still routing the bytes as an upload, download, or copy; a
deletion — `rm`, `sync`'s delete lane, and both dry-run forms of them — reports
`DELETE`. See [`TransferType`](#transfertype).

`compare_key` is the item's identity within the operation, and the token
`TransferProgress` reports against. It is the entry's compare key: on an S3
side, the entry key with the listing `Prefix` removed; on a local side, the path
relative to the directory being enumerated, `/`-separated on every host OS. For
`rm` the frame is the prefix the target resolves to (`rm_filter_root`,
[`operations/rm.md`](./operations/rm.md)); where a producer left the entry's
`compare_key` unset, a delete record falls back to the full key. A streaming
`cp` enumerates nothing, so its record carries the S3 side's full object key
here — the same shape as that fallback. It is the same key space the
operations' filters match against ([`filters.md`](./filters.md)). Outside those
two cases it is not the full object key; read `src_info.key` for that, on a
record that carries a source entry. Advisory records that belong to no item — a
directory-walk warning — carry `""`.

`outcome` is the record kind; see [`OpOutcome`](#opoutcome).

`bytes_transferred` is the byte count moved, on `SUCCEEDED` records only. It is
`0` on a delete record, on an advisory, and on every non-`SUCCEEDED` transfer
record — including a failure whose bytes really landed, such as an `mv` whose
transfer succeeded but whose source removal failed.

`error` carries a `Boto3S3Error` or one of its subclasses
([`exceptions.md`](./exceptions.md)), never a raw backend exception: every path
translates into the library taxonomy first, keeping the originating exception on
`__cause__` where there was one — a per-key `DeleteObjects` failure is
synthesized from the response body and has none. A `FAILED` record carries the
failure; a `CANCELLED` record a
`CancelledError`; a `WARNED` or `NOTICE` record carries its display text in a
plain `Boto3S3Error` used purely as a message envelope — that instance is never
raised, and the base class is used because no classification applies. A `WARNED`
body is bare text, while a `NOTICE` body already includes its own `warning: `
prefix. `None` on every other record.

`src` and `dest` are the rendered endpoints, for a `verb: src to dest` display
line: `s3://bucket/key` for an S3 side, the native path for a local side, `-`
for a stream side, and — for a custom (open-route) side — the backend's own
location token from `Storage.as_text` ([`storage.md`](./storage.md)) with the
entry's relative key appended. A delete record sets `src` only, since the
operation has one endpoint. Advisory records set neither.

`src_info` and `dest_info` are the operation's listing entries
([`FileInfo`](#fileinfo)). The source side is the object being acted on — a
transfer's source, or the object a delete removes. `dest_info` is populated only
by `sync`, where it is the pre-existing destination object the decision compared
against; `cp` and `mv` never list the destination, so it stays `None` for them.
A streaming `cp` lists nothing, so both are `None`. Advisory records carry
neither, and a glacier `SKIPPED` record carries `src_info` but no `dest` or
`dest_info` — its gate runs before the destination is derived.

`src_storage` and `dest_storage` are the run's two sides as `Storage` backends
([`storage.md`](./storage.md)). They are populated where the corresponding entry
is absent as well — a `cp` / `mv` destination, a streaming side, and every
warning and notice — which is why they exist beside `FileInfo.storage`. Where
`src_info` or `dest_info` is present, its `storage` is the same backend as the
matching field here. A delete record puts the deleted-from side on `src_storage`
(for `rm` the target bucket, for `sync`'s delete lane the run's destination) and
leaves `dest_storage` unset. `src_storage` together with `src_info.key`
re-reaches the object directly, without re-deriving anything.

`extra_info` is the affected object's S3 response metadata, and is populated on
`SUCCEEDED` records only — a `FAILED`, `CANCELLED`, `SKIPPED`, `DRYRUN`, or
advisory record carries `None`. By default it holds the ETag alone, as
`{"ETag": "\"...\""}` in S3's raw quoted form: for a copy and a download this is
the *source* object's ETag, which the library provides to the transfer engine up
front, so a multipart copy's destination ETag differs from it. An upload has no
source ETag and the `PutObject` response is discarded, so its `extra_info` is
`None`, as it is for a delete and for advisories. Only what the transfer engine
exposes is surfaced, so the ETag may also be `None` on an s3transfer that does
not provide it and under the CRT engine — a documented degradation
([`../../design/overview.md`](../../design/overview.md)).

Under `capture_response=True` (a parameter of `cp` / `mv` / `rm` / `sync`,
[`operations/README.md`](./operations/README.md)) each successful record instead
carries the full parsed S3 responses the item produced, keyed by role, with
`ResponseMetadata` removed; only the slots that apply are present.
`extra_info["write"]` is an upload's or copy's terminal write response —
whichever of `PutObject`, `CopyObject`, or `CompleteMultipartUpload` the engine
issued, so its shape varies — and `"ETag"` is promoted out of it, normalized,
so an upload carries an ETag too. `extra_info["read"]` is a download's source
`GetObject` response with the streaming `Body` dropped; for a multipart download
the first ranged call is kept and its range-specific fields removed, so the slot
reads like a whole-object response, and `"ETag"` is promoted from it as well.
`extra_info["delete"]` is a removed object's `DeleteObject`-shaped response,
present for an `mv`'s S3 source removal and for each object `rm` or `sync`'s
delete lane removes from S3; the batched wire form is reconstructed into that
single-object shape per key. A custom backend's slot is whatever `Mapping` its
`Storage.delete` returned, minus `ResponseMetadata` — only the built-in S3 paths
guarantee the `DeleteObject` shape, and a local unlink returns nothing, so it
has no slot. An `mv` between two S3 locations therefore carries both `"write"`
and `"delete"`. The exhaustive field-level specification of the three slots is
in [`../../design/opresult.md`](../../design/opresult.md).

## OpOutcome

What happened to the item the record describes. The enum is the discriminator
that replaces a per-event record hierarchy, so a consumer branches on it before
reading the outcome-specific fields.

```python
class OpOutcome(enum.Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WARNED = "warned"
    SKIPPED = "skipped"
    DRYRUN = "dryrun"
    NOTICE = "notice"
    CANCELLED = "cancelled"
```

`SUCCEEDED` — the item completed. It is the only outcome that carries a nonzero
`bytes_transferred` or a populated `extra_info`, and it feeds
`BatchError.succeeded`.

`FAILED` — the item failed; `error` carries the translated failure. It feeds
`BatchError.failed`, and failures aggregate rather than aborting the run at the
first one: a run with at least one such record ends by raising `BatchError` —
unless the run was cut short first, by a cancellation, which ends it in
`CancelledError`, or by an error that ends the run outright, such as a listing
failure part-way through, which propagates instead.

`WARNED` — an advisory that did not fail the run: an unreadable or special file,
a broken symlink, a directory-walk problem, a post-success mtime stamp that
failed, a glacier source skipped without `ignore_glacier_warnings`. `error`
carries the message body without a `warning: ` prefix. It feeds
`BatchError.warned` but never raises on its own, and it is a warning count
rather than an item count — an item that succeeded with a warning contributes to
two counters.

`SKIPPED` — an informational, non-warning skip decided by the operation layer:
a glacier source skipped under `ignore_glacier_warnings`, and the two
`no_overwrite` rejections — a download whose local destination already exists,
and an upload or copy S3 rejected on the precondition. It feeds
`BatchError.skipped` and never raises. Neither an entry a filter removed during
enumeration nor a pair a `sync` decision declined is a skip: neither reaches
the transfer, and neither produces a record at all.

`DRYRUN` — the item a `dryrun=True` run would have acted on. Enumeration and the
single-object `HeadObject` still run, so the listing cost is real, but the
mutating API call does not occur. It affects no `BatchError` counter.

`NOTICE` — display-only text carried on `error`, used where `aws s3` prints an
advisory straight to stderr without counting it as a warning (the case-conflict
messages). It affects no counter, and it may precede the same item's real
outcome, so a consumer must not assume one callback per key. The body already
carries its own `warning: ` prefix.

`CANCELLED` — an accepted item revoked before it could complete, because a fatal
error elsewhere in the run, an immediate cancellation, or Ctrl-C shut the engine
down. `error` is a [`CancelledError`](./exceptions.md#cancellederror) naming the
cause where the canceller supplied one; an immediate-mode escalation arriving
during a drain revokes without a message and those records carry the bare
`canceled`, while the CRT manager cancels without the message and its records
carry awscrt's cancellation wording. It is deliberately distinct from `FAILED`:
nothing is wrong with the item itself, so it is not counted as a failure, it
appears in no `BatchError` counter, and it is not sampled as `BatchError`'s
`__cause__`. A run that produced such records ends by raising — the fatal that
triggered the shutdown, `CancelledError`, or the `KeyboardInterrupt` that
interrupted it — and never with a `BatchError`: the run's outcome is carried by
that exception, not by these records. The per-item `CANCELLED` records are a
library surface with no `aws s3` counterpart: the AWS CLI reports one fatal line
and drops the cancelled items from its output and counts.

## TransferProgress

Byte-level progress for one in-flight item, passed to the `on_progress` callback
of `cp` / `mv` / `sync`. `rm` moves no bytes and has no `on_progress` parameter.

```python
@dataclass(slots=True, kw_only=True)
class TransferProgress:
    transfer_type: TransferType
    compare_key: str
    bytes_done: int
    bytes_total: int | None = None
```

`transfer_type` is the run's reporting verb, the same value the item's
`OpResult` carries — `MOVE` for every `mv` record. `compare_key` is the same
identity token as [`OpResult.compare_key`](#opresult), which is how a consumer
joins progress to the item's eventual outcome.

`bytes_done` is the absolute number of bytes transferred so far for this item,
not a delta: the library accumulates the transfer engine's per-chunk deltas. One
zero-byte record is emitted when the item is queued, so a consumer learns the
in-flight set before any bytes move. `bytes_done` can step backward when the
engine rewinds a mid-transfer retry with a negative delta.

`bytes_total` is the item's size when the operation knew it up front — from the
listing entry, or from `expected_size` on a streaming upload — and `None`
otherwise, as for a streaming download whose size the transfer engine probes
itself.

Records for one item are delivered in order: the accumulate-and-deliver step is
locked per item, so two multipart workers cannot deliver snapshots of the same
item out of order. Nothing orders records across items — callbacks for different
items run concurrently. Progress fires from the transfer engine's worker
threads, or on the calling thread under `use_threads=False`, so the callback
must be fast, thread-safe, and non-raising, exactly like `on_result`.

## TransferType

The kind of byte-moving operation a result or progress record describes.

```python
class TransferType(enum.Enum):
    UPLOAD = "upload"
    DOWNLOAD = "download"
    COPY = "copy"
    MOVE = "move"
    DELETE = "delete"
```

`UPLOAD`, `DOWNLOAD` and `COPY` are the three transfer routes: local to S3, S3
to local, and S3 to S3. The open route — pairing a custom `Storage` with S3
([`storage.md`](./storage.md)) — has exactly one S3 side, so it reports as
`UPLOAD` or `DOWNLOAD` according to the direction the custom side sits on, never
as `COPY`; it adds no fourth kind.

`MOVE` is a reporting kind only. An `mv` run still routes each item internally
as an upload, download, or copy, but every record it emits, result and progress
alike, says `move`. This mirrors how the AWS CLI relabels its transfer type for
`mv`.

`DELETE` is used by `rm`, by `sync`'s delete lane, and by the dry-run records of
both. An `mv`'s source removal is part of that item's move and produces no
separate `DELETE` record.

## FileInfo

The cross-backend listing entry: one uniform type for everything a backend can
list, produced by `Storage.scan` and delivered by `ls`. Files, directories and
prefixes, and buckets are distinguished by `kind` rather than by separate
classes, so a backend never widens `scan`'s return type as it gains kinds.
Backend-specific detail lives on the [`LocalFileInfo`](#localfileinfo) and
[`S3FileInfo`](#s3fileinfo) subclasses; a local-only notion such as "is a
symlink" is an attribute there rather than a distinct type.

```python
@dataclass(slots=True, kw_only=True)
class FileInfo:
    key: str
    kind: FileKind = FileKind.FILE
    size: int | None = None
    mtime: datetime | None = None
    compare_key: str | None = None
    storage: Storage | None = None
```

`key` is the full, `/`-separated identifier: an S3 object key, prefix, or bucket
name, or — on a `LocalFileInfo` — the absolute filesystem path with `os.sep`
normalized to `/`. It is never a relative path and never carries the platform
separator, so entries from two backends share one sort order; that is what lets
`sync` merge-join two scans. Any backend must emit `/`-separated keys regardless
of host OS.

`kind` says what the entry is; see [`FileKind`](#filekind).

`size` is the entry's size in bytes and `mtime` its tz-aware UTC modification
time. Both are populated for `FILE` entries. A `DIRECTORY` entry may leave
either `None` — an S3 common prefix leaves both — and a `BUCKET` entry carries
the bucket's creation date as `mtime` with `size` left `None`. These are
invariants the producers enforce, not the field types.

`compare_key` is the relative form of the entry's key, the key space filters
match against and the axis `sync` merge-joins on: for a local scan, the key
relative to the directory being enumerated; for an S3 object listing, the entry
key with the `ListObjectsV2` `Prefix` removed; for a bucket listing, the bucket
name unchanged. Each backend's `scan_pages` stamps it on every entry it yields —
a contract of the backend's listing rather than of the base `Storage.scan` — so
a `ScanOptions.filter` predicate, `GlobFilter` in particular, can match it
without re-deriving it from `key`. The built-in single-entry lookups use the
basename. A hand-built `FileInfo` may leave it `None`.

`storage` is the backend the entry was listed from, stamped by the producer
during the scan and before any filter runs. It lets a filter predicate reach
behind the entry — a `HeadObject` for a tag the listing omits — and it is the
handle `sync` reads through a pair's `src` / `dest` to open the non-S3 side for
a content compare ([`comparator.md`](./comparator.md)). It is `None` on a
hand-built `FileInfo`; `Storage.scan` fills it as a backstop for a backend whose
`scan_pages` did not.

The class is a mutable dataclass with `slots=True` and keyword-only fields.

## LocalFileInfo

[`FileInfo`](#fileinfo) for a local filesystem entry, produced by `LocalStorage`
([`storage.md`](./storage.md)).

```python
@dataclass(slots=True, kw_only=True)
class LocalFileInfo(FileInfo):
    stat_result: os.stat_result | None = None
    is_symlink: bool = False
```

Its inherited `key` is the absolute path with `os.sep` normalized to `/`, since
`LocalStorage` anchors every scan at its absolutized path;
`boto3_s3.localstorage.to_native_path` inverts that for host I/O. The key is the
path *as walked*: a symlinked directory or file stays under its link name and is
never resolved to its target, since `follow_symlinks=True` follows only in order
to descend or stat.

`stat_result` is the exact stat snapshot the producer used to classify the
entry: normally the followed `os.stat`, the entry's own `lstat` under
`follow_symlinks=False` in the complete-entry view, or an `lstat` fallback when
following a link failed. It is a plain immutable value — not an `os.DirEntry`
handle — that a filter or an `on_result` callback can read (`st_mode`, `st_uid`,
`st_size`, and the rest; `size` and `mtime` are derived from it) without
re-stat'ing.

`is_symlink` records whether the entry itself was a symbolic link, taken from
the walk's directory-entry type or its cached `lstat`. Under
`follow_symlinks=True` a link to a file still surfaces with `is_symlink=True`
while its `stat_result` describes the target.

Every `LocalFileInfo` produced by `LocalStorage` carries both fields; a value
built by hand may leave `stat_result` at `None`. The enumeration knobs that
decide which entries appear at all — `enumerate_all_entries`,
`follow_symlinks`, `detect_symlink_loops` — belong to `LocalScanOptions`
([`options.md`](./options.md)).

## S3FileInfo

[`FileInfo`](#fileinfo) enriched with fields derived from an S3 listing,
produced by `S3Storage` ([`storage.md`](./storage.md)) for object listings,
common prefixes, and the service-root bucket listing.

```python
@dataclass(slots=True, kw_only=True)
class S3FileInfo(FileInfo):
    etag: str | None = None
    storage_class: str | None = None
    owner: str | None = None
    head: Mapping[str, Any] | None = None
```

`etag` is the dequoted ETag — the surrounding `"` characters stripped — where
the listing supplied one. Note that `OpResult.extra_info` reports an ETag in
S3's raw quoted form instead.

`storage_class` is the object's storage class as the listing reported it. It is
what the glacier gate consults to skip `GLACIER` and `DEEP_ARCHIVE` sources on
`cp` / `mv` / `sync`, unless the transfer is forced, or unless a `HeadObject`
supplied a `Restore` state reporting the object restored. That second carve-out
reaches the single-object source path only: a recursive listing carries no
`Restore`, so under a recursive transfer a restored object still skips.

`owner` is the S3 canonical user ID, present only when the listing was made with
`fetch_owner=True` on the scan options ([`options.md`](./options.md)): a plain
`ListObjectsV2` omits the owner, and requesting it costs per-page latency. The
display name S3 returns alongside it is deliberately not surfaced, since S3
populates it only in `us-east-1`.

`head` is a cache slot for a `HeadObject` response payload, or any partial
mapping with the same keys, so a later stage can spare the per-object HEAD
round-trip it would otherwise make. The built-in single-entry lookups fill it
with the response they already made — `S3Storage.get_fileinfo`
([`storage.md`](./storage.md)), and the HEAD a non-recursive transfer makes on
its source — which is what lets the glacier gate read `Restore` there. A
listing leaves it `None`; a custom enumerator or a pre-fetch hook may fill it.

All four fields are optional, matching the AWS CLI's tolerance of their
absence.

## FileKind

What a listing entry is. Backends map their native kinds onto these three.

```python
class FileKind(enum.Enum):
    FILE = "file"
    DIRECTORY = "directory"
    BUCKET = "bucket"
```

`FILE` is an S3 object or a non-directory local entry. Under a local
complete-entry enumeration it can therefore be a symlink, FIFO, socket, or
device, so it does not by itself guarantee transferable content; inspect
[`LocalFileInfo.stat_result`](#localfileinfo)`.st_mode` for the precise native
kind.

`DIRECTORY` is an S3 common-prefix grouping or a local directory. `size` and
`mtime` may be `None`, and an S3 common prefix leaves both unset.

`BUCKET` is a top-level S3 bucket, produced only by the service-root bucket
listing that a bare `s3://` target gives `ls`
([`operations/ls.md`](./operations/ls.md)). A transfer scan never yields
buckets.

## ProgressCallback

```python
ProgressCallback = Callable[[TransferProgress], None]
```

The type of the `on_progress` parameter of `cp` / `mv` / `sync`. It is called
with one [`TransferProgress`](#transferprogress) and its return value is
ignored. Passing `None` — the default — disables progress entirely: the library
registers no progress hook at all, so nothing is accumulated. See
[`TransferProgress`](#transferprogress) for the ordering and threading
guarantees.

## ResultCallback

```python
ResultCallback = Callable[[OpResult], None]
```

The type of the `on_result` parameter of `cp` / `mv` / `rm` / `sync`, and of
`S3Deleter`'s own `on_result`. It is called with one
[`OpResult`](#opresult) and its return value is ignored. Passing `None` — the
default — means the per-item stream is not delivered; a run that had failures
still ends in `BatchError`, carrying the rollup counts
([`exceptions.md`](./exceptions.md)).

The callback may run on a worker thread, and several may run at once. It must
not raise: on the batched delete path the counters are updated before the
callback runs, so a raising callback leaves its record counted, abandons the
rest of that batch, and surfaces its exception from a later flush or from the
deleter's close. Cancelling from inside the callback is supported — see
[`CancelToken`](#canceltoken).

## ListingCallback

```python
ListingCallback = Callable[[FileInfo], None]
```

The type of `ls`'s `on_entry` parameter
([`operations/ls.md`](./operations/ls.md)). It is called with one
[`FileInfo`](#fileinfo) per entry and its return value is ignored. Unlike the
two callbacks above it is required, since `ls` returns nothing and delivering
entries is the whole operation.

Entries arrive in listing order, one at a time, on the thread that called `ls`,
and `ls` blocks until listing and cleanup have finished. The callback may cancel
the listing through the operation's `cancel_token`.

## CancelToken

A thread-safe, monotonically escalating cancellation request, accepted as the
`cancel_token` parameter of `ls` / `cp` / `mv` / `rm` / `sync`, of
`Storage.scan` ([`storage.md`](./storage.md)), and of `S3Deleter`. One token can
be passed to several calls. Every holder of the token observes the effective
`mode` and performs that shutdown policy; the operations `ls` / `cp` / `mv` /
`rm` / `sync` then raise [`CancelledError`](./exceptions.md#cancellederror)
after reclaiming their resources. That raise wins over the partial-failure
report: an operation whose token was cancelled ends in `CancelledError` and
never in `BatchError`, even where items failed and even where the cancellation
arrived too late to cut anything short. An operation handed a token that is
already cancelled raises before any of its side effects: it resolves and
validates its arguments, then stops before transferring or deleting anything.

`Storage.scan` and `S3Deleter` are the two holders that do not raise, being
building blocks the caller drives itself. A cancelled token stops the scan's
prefetch producer before its next page pull and the entry stream ends there;
the deleter stops dispatching, discards its unsent buffer, and closes without
flushing it. What to raise, if anything, is the caller's decision.

```python
class CancelToken:
    def __init__(self) -> None: ...

    def cancel(self, *, mode: CancelMode = CancelMode.GRACEFUL) -> None: ...

    @property
    def cancelled(self) -> bool: ...

    @property
    def mode(self) -> CancelMode | None: ...
```

The constructor takes no arguments and the token starts uncancelled. The state
is two monotone flags, so the object is safe to share across threads without
external synchronization.

What each mode guarantees depends on the lane the work is in, because a request
already issued to S3 cannot be aborted safely.

For the transfer lanes of `cp` / `mv` / `sync`, `GRACEFUL` is a drain: the
operation stops accepting new work and lets everything it already accepted run
to completion, so those items report their real outcomes and no `CANCELLED`
record arises. `IMMEDIATE` additionally cancels pending and in-flight transfers,
and then an accepted item that had not started — or was abandoned mid-flight —
reports `CANCELLED`, while an in-flight request that completes despite the
cancellation reports its real outcome, since its bytes really landed. Work never
accepted, because the submission loop stopped before reaching it, produces no
record either way. An escalation to `IMMEDIATE` that arrives while a graceful
drain is already running is honored.

For the delete lanes of `rm` and `sync`, `GRACEFUL` discards the deleter's
unsent buffer and drains the batch already dispatched; `IMMEDIATE` additionally
cancels a dispatched batch that has not started. A batch whose S3 request has
started always completes and delivers its per-key records first. Discarded
buffered entries produce no records
([`../../design/deleter.md`](../../design/deleter.md)).

For `ls`, both modes behave identically: cancellation stops entry delivery,
drops prefetched pages, waits for a page request already in progress, reclaims
the prefetch worker, and raises. The service-root bucket listing iterates its
pages directly, so cancellation there simply stops between pages.

For `sync`'s pair loop, the token is polled before each decision and each
action. `GRACEFUL` awaits the decisions already handed to a `ParallelFilter`
executor ([`comparator.md`](./comparator.md)); `IMMEDIATE` first cancels those
futures. The caller's executor is never shut down.

Under either mode, external I/O already running may still have to finish before
the operation can return.

### cancel(\*, mode=CancelMode.GRACEFUL)

Requests cancellation. It may be called from any thread, from inside an
`on_result`, `on_progress`, or `on_entry` callback, and from a signal handler —
the internal lock is reentrant precisely so a handler running on the main thread
can re-enter it. It never blocks on the operation and never raises.

`mode` is the shutdown policy to request; see [`CancelMode`](#cancelmode).
Escalation is monotone: a later `IMMEDIATE` upgrades an earlier `GRACEFUL`,
while a later `GRACEFUL` never downgrades an `IMMEDIATE`. Calling it repeatedly
is safe and has no effect beyond the escalation.

### cancelled

`True` once `cancel` has been called, `False` before that. It never returns to
`False`.

### mode

The effective mode: `None` while the token is uncancelled, then
`CancelMode.GRACEFUL` or `CancelMode.IMMEDIATE` reflecting the highest request
made so far. Reading it is atomic with respect to a concurrent `cancel`.

## CancelMode

How an operation shuts down after a [`CancelToken`](#canceltoken) request.

```python
class CancelMode(enum.Enum):
    GRACEFUL = "graceful"
    IMMEDIATE = "immediate"
```

`GRACEFUL` — the default of `CancelToken.cancel` — stops accepting new work and
drains the work the operation already accepted. Accepted items therefore report
their real outcomes and produce no `CANCELLED` records.

`IMMEDIATE` additionally asks each engine to cancel pending and in-flight work
wherever its implementation supports cancellation. What that yields per lane is
listed under [`CancelToken`](#canceltoken); external I/O already running may
still have to finish, and an operation is free to report the real outcome of a
request that completed despite the request to cancel.
