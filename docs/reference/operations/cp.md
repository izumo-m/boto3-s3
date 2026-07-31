# cp

`cp` copies bytes in one direction between a local path, an S3 URI, a
caller-supplied stream and a custom `Storage` backend, with `aws s3 cp`
semantics. The contract `cp` shares with the other operations — accepted
location forms, the module-level wrappers, the shared `dryrun` and
cancellation parameters, and the end-of-run error model — is in
[`README.md`](./README.md); the narrative pages are
[`../../library/transfer-options.md`](../../library/transfer-options.md) for
the options a transfer takes,
[`../../library/streams.md`](../../library/streams.md) for stream endpoints and
[`../../library/results.md`](../../library/results.md) for the result stream.
The engine's design is
[`../../../design/transfer.md`](../../../design/transfer.md).

## S3.cp

Copies every enumerated source item to the destination and returns `None` when
each one succeeded. A call moves bytes in one direction only, chosen by the
resolved pair: local to S3 is an upload, S3 to local a download, S3 to S3 a
server-side copy, and a custom `Storage` paired with S3 transfers through that
backend's own `open` in whichever direction the pair implies. A stream endpoint
on one side is a single transfer of its own shape, described below. Path
shapes — what a trailing separator or an existing directory means, which side's
name the destination takes, which key a filter matches — reproduce the AWS
CLI's rules, and `recursive` is its `--recursive`.

Items are transferred as enumeration produces them and a failed item does not
stop the run: each item's outcome is delivered to `on_result` as it happens,
and the call raises `BatchError` at the end if at least one item failed.

```python
def cp(
    self,
    src: Location,
    dest: Location,
    *,
    recursive: bool = False,
    filter: FileFilter | None = None,
    dryrun: bool = False,
    expected_size: int | None = None,
    on_progress: ProgressCallback | None = None,
    on_result: ResultCallback | None = None,
    cancel_token: CancelToken | None = None,
    transfer_config: TransferConfig | None = None,
    capture_response: bool = False,
    **options: Unpack[TransferOptions],
) -> None: ...
```

### `src` and `dest`

Both are `Location`s and both pass through `S3.resolve`, so an `s3://…` string
is an S3 location, every other string or `os.PathLike[str]` is a local path,
and a `Storage` instance is used as given — with its own client and its own
scan configuration ([`README.md`](./README.md#location-arguments),
[`../storage.md`](../storage.md)). The resolved storages are validated before
anything is listed or transferred; on the stream route only the S3 peer is, a
stream endpoint carrying no location to check.

Four pairings are accepted, and each of them has an
[`S3Storage`](../storage.md#s3storage) on at least one side: local with S3 in
either direction; S3 with S3; a custom `Storage` with S3 in either direction
(the open route, [`../storage.md`](../storage.md)); and a stream endpoint —
[`IOStorage`](../storage.md#iostorage) or
[`StdioStorage`](../storage.md#stdiostorage) — with S3 in either direction.
Every other combination raises `ValidationError`: local to local, a custom
backend paired with a local path or with another custom backend, a stream
paired with anything but S3, and a stream on both sides at once.

Whether the destination adopts each item's relative key, or stands alone as the
exact target, is decided per side. An S3 destination adopts it when
`recursive=True` or when its key ends in `/`; a keyless `s3://bucket`
normalizes to the bucket root and counts as the latter, so
`cp("./a.txt", "s3://bucket")` writes the key `a.txt`. A local destination
adopts it when `recursive=True`, when the path already exists as a directory,
or when it was written with a trailing separator. A custom destination adopts
it when `recursive=True` or when its `as_text()` ends in `/`. When the
destination does not adopt the name, the single item is written exactly at the
destination named.

A stream side is a single endpoint that is neither listed nor keyed: the S3
peer's key is used verbatim, both sides' displays render the stream as `-`, and
the AWS CLI's habit of turning a literal `-` into a basename belongs to the
command layer, not here.

### Single object versus recursive

`recursive=False` transfers at most one item, resolved once per source kind. A
local source is resolved by a single stat and is not checked for being a
directory, so a directory named as a non-recursive source becomes an item the
transfer fails with `[Errno 21] Is a directory` rather than an up-front
rejection. An S3 source with a non-empty key is resolved by one `HeadObject`,
and a 404 raises `NotFoundError` worded as the AWS CLI words it. An S3 source
with no key (`"s3://bucket"`) matches nothing and transfers nothing, but the
bucket listing is still issued, so a `NoSuchBucket` or a denied `ListBucket`
surfaces instead of a silent success. A custom source is resolved through its
`Storage.get_fileinfo`, and one it cannot resolve raises `NotFoundError` from
inside enumeration rather than transferring nothing.

`recursive=True` enumerates the source through its own `Storage.scan`: a local
walk, or an S3 listing under the source key normalized to a `/`-terminated
prefix — so `cp("s3://b/data", …, recursive=True)` lists under `data/` and
does not reach `data-sibling.txt`. Zero-byte `/`-terminated folder markers are
dropped from that listing, so they never transfer on this path. An enumeration
that yields nothing transfers nothing and does not raise. A recursive transfer
into a local destination that does not exist creates that directory before
enumerating; this pre-create is not gated on `dryrun`, so a dry run of such a
transfer still leaves the directory behind.

Listing and walk behavior is configured on the storage, not here: an
`S3Storage`'s `page_size` and `fetch_owner`, and a `LocalStorage`'s
`follow_symlinks`, `detect_symlink_loops` and `enumerate_all_entries`. Pass a
configured storage as `src` to change any of them
([`../storage.md`](../storage.md), [`../options.md`](../options.md)).

### Parameters

`recursive` selects the enumerated shape described above. It is the only way to
transfer more than one item, and with a stream endpoint on either side it
raises `ValidationError`.

`filter` decides which enumerated entries take part: a predicate over each
entry's `FileInfo` returning `True` to keep it
([`../filters.md`](../filters.md)). A rejected entry produces no `OpResult` at
all. A relative glob pattern is matched against the entry's stamped
`compare_key` — for a recursive local source the path relative to the directory
being enumerated, for a recursive S3 source the key with the listing's `Prefix`
removed, and on the single-object routes the last `/`-separated component of
the source key — while a root-anchored pattern is matched against the entry's
full `key` instead. The same predicate is also applied to the destination walk
the `case_conflict` gate performs, so a pattern that hides destination entries
changes what that gate considers already present. On the stream route there is
no listing to prune and `filter` is not consulted.

`dryrun=True` issues no mutating call ([`README.md`](./README.md#dryrun)). For
`cp` specifically: enumeration, the single-object `HeadObject` and the
`case_conflict` destination walk still run; the missing local destination
directory is still created; an upload's or copy's per-item request parameters
are still mapped, so a malformed `grants` entry is still rejected; no transfer
manager is built; and a stream endpoint is never opened, leaving a
side-effecting stream untouched. Items the gates consume — a glacier
skip, a parent-reference skip, a `no_overwrite` skip — keep their own record
instead of becoming `DRYRUN` records.

`expected_size` is the multipart sizing hint for a streaming upload, in bytes.
It reaches the engine only on that route; on every other route it is accepted
and ignored. Without it, an upload from a stream of unknown length buffers up
to the multipart threshold before deciding how to send the object.

`on_progress` receives byte-level
[`TransferProgress`](../results.md#transferprogress) updates for submitted
transfers, including one zero-byte notification when an item is queued
([`../results.md`](../results.md)). It is called from the engine's worker
threads and must be fast, thread-safe and non-raising.

`on_result` receives one [`OpResult`](../results.md#opresult) per event, as
described in the next section.

`cancel_token` accepts a [`CancelToken`](../results.md#canceltoken)
([`README.md`](./README.md#cancel_token)). `cp` polls it after resolution and
before its own side effects — the destination pre-create, the `case_conflict`
destination walk, a deferred client build — and again before pulling each item
from the enumeration, so a cancelled run stops listing promptly instead of
paging on. What each cancellation mode guarantees for the transfer lane — a
graceful drain, an immediate revocation — is specified with the token itself.
Under either mode the call ends by raising `CancelledError` once the engine and
the enumeration workers have been reclaimed.

`transfer_config` overrides, for this call only, the `TransferConfig` the `S3`
instance was constructed with; `None` uses that instance default, and a `None`
there means the library defaults
([`../options.md`](../options.md#transferconfig), [`../s3.md`](../s3.md)). It
carries the multipart thresholds and part sizes, the concurrency, the bandwidth
limit and the transfer engine selection.

`capture_response=True` adds the operation's own S3 responses to each
successful record's `extra_info` — `"write"` for an upload or copy, `"read"`
for a download — and forces the classic transfer engine, since the CRT data
plane does not expose them ([`../results.md`](../results.md)).

`**options` are the transfer options `cp` / `mv` / `sync` share — object
metadata and headers, ACLs and storage class, encryption, checksums,
`no_overwrite`, the glacier controls and `case_conflict`
([`../options.md`](../options.md#transferoptions)). An unknown key raises
`ValidationError` before anything is resolved, so a typo never passes silently;
an option that does not apply to the route being run is ignored.

### What is emitted to `on_result`

Every record carries `transfer_type` — `UPLOAD`, `DOWNLOAD` or `COPY`, the
route the run took. A record about an item carries that item's `compare_key`;
an advisory that belongs to no item — a walk warning — carries `""`, as
described with the warnings below. Transfer records also carry `src` and `dest`
as display strings (`s3://bucket/key`, a native local path, the backend's own
rendering for a custom side, or `-` for a stream), `src_info` as the source
listing or `HeadObject` entry, and `src_storage` / `dest_storage` as the
resolved sides. The archived-source `SKIPPED` record is the carve-out: it
carries `src` and `src_info` but no `dest`, its gate running before the
destination is derived. `dest_info` is `None` on every `cp` record: `cp` does
not pair against a destination listing, and the `case_conflict` gate's
destination walk feeds only that gate. On the stream route nothing is listed,
so `src_info` is `None` as well.

The outcomes `cp` emits:

- `SUCCEEDED` — the item transferred. `bytes_transferred` is the item's size,
  or the size the engine resolved when the item had none. `extra_info` carries
  `{"ETag": …}` for a download or a copy — the source object's ETag — and is
  `None` for an upload unless `capture_response=True` supplies it.
- `FAILED` — the item's transfer failed; `error` is the translated exception.
  These are the items `BatchError` counts.
- `SKIPPED` — a silent, non-warning skip: `no_overwrite` finding the local
  destination file already present, S3 answering a conditional write with
  `PreconditionFailed`, or an archived source under
  `ignore_glacier_warnings=True`.
- `WARNED` — an advisory that does not fail the run: local walk problems from
  the source walk or the `case_conflict` destination walk (unreadable, special
  or vanished files, broken symlinks, invalid timestamps), an archived source
  the gate below refuses on a download or copy, a download whose key resolves
  outside the destination directory, an upload above the S3 single-object size
  limit (warned, then attempted anyway), and a download that transferred but
  could not stamp the source's modification time — which produces a `WARNED`
  record beside its `SUCCEEDED` one. A warning record is message-only: `error`
  carries the text, the two `*_storage` fields are set, and `src` / `dest` /
  `src_info` are not. Its `compare_key` follows its origin: the engine's own
  per-item warnings (the archived source, the oversize upload, the parent
  reference, the failed mtime stamp) carry the item's key, while one raised by
  a source walk, by the single-object source resolution or by the
  `case_conflict` destination walk carries `""`.
- `NOTICE` — the `case_conflict` advisories under `CaseConflictMode.SKIP` and
  `CaseConflictMode.WARN`. Display-only, counted nowhere, and it may precede
  the same item's real outcome.
- `DRYRUN` — under `dryrun=True`, one per item that reached the submit step.
- `CANCELLED` — an accepted item revoked before it could complete, by an
  immediate cancellation, a fatal error elsewhere in the run, or `Ctrl-C`.

The archived-object gate behind that warning and its
`ignore_glacier_warnings` skip runs on downloads and copies only; an upload
never reaches it. A source whose storage class is `GLACIER` or `DEEP_ARCHIVE`
is refused unless `force_glacier_transfer=True` lets it through. The
restored-object carve-out is read from the single-object `HeadObject`'s
`Restore`, which a recursive listing does not carry, so under `recursive=True`
even a restored object is skipped
([`../options.md`](../options.md#transferoptions)).

Records for submitted transfers arrive on the engine's worker threads, and the
warnings an enumeration raises — the source walk or listing, and the
`case_conflict` destination walk — arrive on that scan's prefetch worker, so
they can interleave with transfer records. Dry-run records, skips, notices, the
warnings the single-object source resolution raises and the per-item warnings
the producers raise (the archived source, the oversize upload, the parent
reference) are emitted inline on the calling thread
([`README.md`](./README.md#on_result)).

### Raises

- [`ValidationError`](../exceptions.md#validationerror) — an unknown key in
  `**options` (checked first, before either location is resolved); a pairing
  with no S3 side, including local to local, a custom backend with a local path
  or with another custom backend, a stream whose peer is not S3, and a stream on
  both sides; `recursive=True` with a stream endpoint; `no_overwrite` with a
  stream destination; a location `Storage.validate` rejects, such as an
  unsupported ARN form or a key with no bucket; a custom backend that does not
  declare what its route needs (`OPEN_READ` plus `SCAN` or `GET_FILEINFO` for a
  custom source, `OPEN_WRITE` for a custom destination); an option value the
  request mapper rejects, such as a malformed `grants` entry or an unrecognized
  `copy_props` / `annotation_copy_mode`; an unrecognized `case_conflict` value,
  refused on every route including the streaming one, whose single item builds
  no gate; a `TransferConfig` carrying classic-only settings with
  `preferred_transfer_client="crt"`; a
  `StdioStorage` whose `stdin` / `stdout` is unavailable in this process; and a
  case-fold collision under `CaseConflictMode.ERROR`, raised from inside
  enumeration.
- [`NotFoundError`](../exceptions.md#notfounderror) — a local source path that
  does not exist, checked before any item work and worded as the AWS CLI words
  it; a single S3 source whose `HeadObject` answers 404; a single
  custom-backend source `get_fileinfo` cannot resolve.
- [`ConfigurationError`](../exceptions.md#configurationerror), or its
  [`InvalidConfigError`](../exceptions.md#invalidconfigerror) refinement —
  `no_overwrite` on an SDK without conditional writes, or `copy_props=ALL` on
  one without the annotations model, both raised as the engine is built and
  before any item is submitted; and credentials, region, profile or endpoint
  that will not resolve while a client is being built — during resolution for a
  bare `"s3://…"` argument, or on the deferred build of a caller-supplied
  `S3Storage` that carries none.
- The enumeration's own rejection, translated to its category and propagated:
  [`AccessDeniedError`](../exceptions.md#accessdeniederror),
  [`NotFoundError`](../exceptions.md#notfounderror) or
  [`TransportError`](../exceptions.md#transporterror) for a listing or a
  single-object `HeadObject` that fails, and the same three for a local
  destination directory that cannot be created.
  A listing that fails part-way leaves the records already delivered standing
  and raises instead of rolling up.
- [`BatchError`](../exceptions.md#batcherror) — at least one item failed.
  Raised once at the end, carrying the run's rollup counts and a sampled
  failure on `__cause__`. A single-object `cp` whose one item failed raises it
  too, reported as 1 of 1. Warnings and skips alone do not raise.
- [`CancelledError`](../exceptions.md#cancellederror) — the run was cancelled
  through `cancel_token`. It supersedes `BatchError`.
- Anything `filter` itself raises. A predicate's exception surfaces on the
  consuming side of the enumeration and aborts the run; the engine then cancels
  the transfers it had accepted. `on_result` and `on_progress` must not raise —
  the library states no contract for a callback that does
  ([`README.md`](./README.md#on_result)).

## boto3_s3.cp

`boto3_s3.cp(...)` is the module-level convenience wrapper: it runs the method
above on a freshly built default `S3()`, and its signature is `S3.cp`'s minus
`self`. See [`README.md`](./README.md#module-level-functions) for the shared
wrapper contract.
