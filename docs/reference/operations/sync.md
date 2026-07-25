# sync

`sync` mirrors a source tree into a destination tree, deciding per entry
whether to create it, overwrite it, or delete it. This page states the call
contract; the narrative — the three decisions, the default overwrite rule,
content-based comparison — is in
[`../../library/sync.md`](../../library/sync.md) and
[`../../library/sync-content.md`](../../library/sync-content.md), and the
pipeline's rationale in [`../../../design/sync.md`](../../../design/sync.md).
Contracts shared by every operation (path resolution, `dryrun`, cancellation,
the module-level function mirrors) are in [`./README.md`](./README.md).

## S3.sync

Recursively synchronizes `src` into `dest` and returns `None`; per-item
outcomes are reported through `on_result` and, if any item failed, rolled up
into a `BatchError` at the end. It is always recursive — there is no
single-entry form, and both sides are formatted as directory roots, so a
trailing separator on either argument changes nothing.

Four routes are accepted: local to S3 (upload), S3 to local (download), S3 to
S3 (copy), and the open route — a custom `Storage` on one side paired with S3
on the other. A local-to-local pair, a custom backend paired with anything but
S3, and a stream endpoint (`IOStorage` / `StdioStorage`, which declares no
scan capability) are each rejected with `ValidationError`.

The call runs two layers. First, visibility: each side's listing is enumerated
and pruned by the single `filter` before the sides meet. Second, pair
decisions: the surviving streams are merge-joined by compare key and every key
comes out as exactly one pair shape, which selects the lane that judges it —
`create_filter` for a key held only by the source, `update_filter` for a key
held by both sides, `delete_filter` for a key held only by the destination.
The pair shapes, the `Comparator` that produces them, and `ParallelFilter` are
specified in [`../comparator.md`](../comparator.md).

Throughout, the *compare key* is the key both the pairing and the filters
operate on: for a local side, the path relative to the directory being
enumerated; for an S3 side, the `Prefix`-relative key. It is `/`-separated on
every platform.

The defaults `create_filter=True`, `update_filter=None`, `delete_filter=False`
together reproduce `aws s3 sync`; `delete_filter=True` is its `--delete`.

```python
def sync(
    self,
    src: Location,
    dest: Location,
    *,
    filter: FileFilter | None = None,
    create_filter: bool | FileFilter | ParallelFilter[FileInfo] = True,
    update_filter: bool | PairFilter | ParallelFilter[SyncPair] | None = None,
    delete_filter: bool | FileFilter | ParallelFilter[FileInfo] = False,
    dryrun: bool = False,
    on_progress: ProgressCallback | None = None,
    on_result: ResultCallback | None = None,
    cancel_token: CancelToken | None = None,
    transfer_config: TransferConfig | None = None,
    capture_response: bool = False,
    **options: Unpack[TransferOptions],
) -> None: ...
```

### Parameters

`src` and `dest` are `Location`s resolved through `S3.resolve`
([`../s3.md`](../s3.md)): an `s3://` string becomes an `S3Storage` carrying
this instance's client, any other string or `os.PathLike` a `LocalStorage`,
and a `Storage` instance is used verbatim with its own client. Both are
treated as directories, so `s3://bucket/site` and `s3://bucket/site/` name the
same root here.

`filter` is the visibility layer, applied to **both** sides during
enumeration, before pairing. It is a `FileFilter`
([`../filters.md`](../filters.md)) receiving each entry's `FileInfo`, whose
`compare_key` is stamped by the producing scan; `None` (the default) keeps
everything. A predicate that reads only the compare key prunes the two sides
identically, so an entry it excludes is invisible on both sides and is
therefore neither transferred nor deleted; a predicate that reads side-specific
state prunes per side instead — notably a `GlobFilter` whose pattern is
root-anchored, which matches the entry's full key rather than its compare key.
Folder markers — zero-byte `/`-terminated objects — are dropped from an S3
side's listing before `filter` sees them, and the local walk does not produce
them, so `sync` neither transfers nor deletes markers. `filter` is evaluated
during enumeration, off the calling thread, and both sides are enumerated at
once, so keep it thread-safe and fast, as with `on_result`.

`create_filter` decides the keys held only by the source. `True` (the default)
creates every one, `False` none. A `FileFilter` creates only the entries it
keeps, receiving the source-side `FileInfo` — the same shape as `rm`'s
`filter`, not a pair. `aws s3` has no counterpart: it always creates.

`update_filter` decides the keys held by both sides — whether the source is
copied over the destination. It selects exactly one strategy and does not
compose. `None` (the default) is the `aws s3` size and last-modified
judgment, equivalently
[`AwsCliComparison()`](../comparator.md#awsclicomparison), whose `size_only`
and `exact_timestamps` constructor arguments are where the `--size-only` /
`--exact-timestamps` tuning lives. `True` re-copies every such
key, `False` none — leaving existing destinations as they are. Any other value
is a `PairFilter`: a callable receiving the both-sides `SyncPair` (both
`pair.src` and `pair.dest` are always present) and returning whether to copy.
[`EtagComparison`](../comparator.md#etagcomparison) and
[`ChecksumComparison`](../comparator.md#checksumcomparison) are drop-in
strategies of this kind. Note that `None` and `False` are different values with
different meanings.

`delete_filter` decides the keys held only by the destination. `False` (the
default) switches the lane off entirely — no orphan is examined and, on an
open-route destination, no `DELETE` capability is required. `True` deletes
every orphan. A `FileFilter` deletes only the orphans it keeps, receiving the
destination-side `FileInfo`.

Any one of the three may instead be a `ParallelFilter` wrapping the same
predicate, which runs that lane's decisions on a caller-supplied thread pool —
`ParallelFilter[FileInfo]` for `create_filter` / `delete_filter`,
`ParallelFilter[SyncPair]` for `update_filter`. The lane still submits its
survivors on the calling thread. For a stateless predicate, wrapping does not
change which entries are acted on; it changes ordering, and parallelizing
`create_filter` makes the case-conflict gate's first-key-wins outcome
non-deterministic. A stateful predicate can observe the concurrency and can
therefore decide differently. The wrapper's contract, including pool
ownership, is in [`../comparator.md`](../comparator.md).

`dryrun` reports every transfer and deletion that would have happened as a
`DRYRUN` record and issues no mutating API call. Both sides are still
enumerated, a missing local destination directory is still created as it is on
a live run, and on the open route the backend's `Storage.open` is not called.

`on_progress`, `on_result` and `cancel_token` are the shared callback and
cancellation hooks specified in [`../results.md`](../results.md); what `sync`
puts on the stream is described below. Deletions report no byte progress.

`transfer_config` overrides the `TransferConfig` this `S3` instance was built
with, for this call only ([`../options.md`](../options.md)).

`capture_response` surfaces the underlying S3 responses on
`OpResult.extra_info` ([`../results.md`](../results.md)); for the delete side
see below.

`**options` are the shared `TransferOptions`
([`../options.md`](../options.md)); an unrecognized key raises
`ValidationError` before any listing. Several of them have `sync`-specific
behavior. `no_overwrite=True` drops the update lane entirely: a key present at
the destination is not overwritten and produces no record at all, while keys
held only by the source are still created — and because the guard is applied
as a decision, `sync` sends no conditional-write header, unlike `cp` and `mv`.
`request_payer` is forwarded to the delete side as well as to the transfers.
`case_conflict` builds its gate only for a download to a `LocalStorage`
destination, and the gate is consulted only for keys held only by the source,
since an exact-key update is not a conflict. `force_glacier_transfer` and
`ignore_glacier_warnings` act as they do for `cp`; because `sync` judges from
the listing, which carries no restore status, a restored archived object is
still gated.

### Order of validation and effects

The observable order before any transfer: unrecognized `**options` keys are
rejected; `src` and `dest` are resolved and validated; an already-cancelled
`cancel_token` raises; the route is classified, which for an upload checks
that the local source path exists; a missing local destination directory is
created; an S3 Express directory bucket on either side is rejected; a client
is built for an S3 side that still has none; an open-route side is checked for
the capabilities the route needs; an unrecognized `case_conflict` value is
rejected. The destination directory is therefore created even by a run that
transfers nothing, by one the directory-bucket check goes on to reject, and by
one the `case_conflict` check rejects. The check is a bare existence test,
so a destination that already exists as a file passes it and every item fails
afterwards. The source check is a bare existence test too: a local source that
is a file passes it, the directory-style walk then enumerates nothing and emits
one `WARNED` record, and the run completes with warnings rather than raising.

Only a caller-constructed `S3Storage` carrying no client reaches that late
client build. The client for a bare `s3://…` string argument is built during
resolution instead — before the cancel-token poll, the destination-directory
creation and the directory-bucket check — so credentials, region, profile or
endpoint that will not resolve raise there rather than at the position listed
above.

`sync` has no guard against syncing a path onto itself: every key simply pairs
with itself, so under the default decisions nothing is transferred and the run
succeeds.

### What it emits to on_result

Transfers emit the records specified in [`../results.md`](../results.md), with
`transfer_type` naming the route. `sync` is the only operation that populates
`dest_info`: on a key held by both sides it carries the destination listing
entry the decision compared against, and it is `None` for a key held only by
the source. Local-listing warnings — unreadable, vanished or special files,
unusable timestamps — are emitted as `WARNED` records from **both** sides,
including the destination-side walk of a download. In a `sync` run, `SKIPPED`
records come from the glacier gate under `ignore_glacier_warnings=True`; a key
the update lane declines to copy produces no record at all.

Each deletion emits one record with `transfer_type` set to
`TransferType.DELETE` and these fields:

- `compare_key` — the orphan's compare key, falling back to its full key if
  the producing scan left it unstamped.
- `outcome` — `SUCCEEDED`, `FAILED`, or `DRYRUN` under `dryrun`.
- `error` — the translated `Boto3S3Error` on a `FAILED` record, otherwise
  `None`.
- `src` — the removed entry's display form: `s3://bucket/key` for an S3
  destination, the native path for a local one, the backend's own rendering
  for a custom one.
- `src_info` — the destination listing entry that was removed.
- `src_storage` — the destination storage it was listed from.
- `extra_info` — under `capture_response` only, and only on a successful
  delete, `{"delete": ...}`: a `DeleteObject`-shaped response slot for an S3
  destination, and whatever the backend's own `Storage.delete` returned for a
  custom one. A local removal returns nothing, so it carries no slot.

The `dest` trio and `bytes_transferred` are unset on a delete record: the
entry has no second side.

Deletions against an S3 destination are batched, so their records arrive
together when a batch flushes, from the deleter's worker thread; deletions
against a local or custom destination happen synchronously and emit inline on
`sync`'s calling thread — when the lane is wrapped in `ParallelFilter`, only
the decision moves to the pool. The interleaving of delete records with
transfer records is not deterministic. A lane wrapped in `ParallelFilter`
consumes its decisions in completion order, so the records it emits inline —
dry-run records, and local or custom-destination deletions — arrive in that
order rather than in compare-key order.

### Raises

- [`ValidationError`](../exceptions.md#validationerror) — an unrecognized key
  in `**options`; a local-to-local pair, a custom backend paired with anything
  but S3, or a stream endpoint; an S3 Express directory bucket on either side,
  whose listings are not ordered the way the merge-join requires; a
  custom (open-route) side that does not declare the capabilities the route
  needs (sorted scanning, the route's read or write, and deletion when the
  delete lane is on for a custom destination); an unrecognized `copy_props` or
  `annotation_copy_mode` value on the S3-to-S3 route; an unrecognized
  `case_conflict` value, on any route and not only the download the gate
  covers; and a `TransferConfig` carrying classic-only settings with
  `preferred_transfer_client="crt"`,
  rejected when the transfer engine is built and therefore not under `dryrun`.
- [`NotFoundError`](../exceptions.md#notfounderror) — the local source path of
  an upload does not exist.
- [`ConfigurationError`](../exceptions.md#configurationerror), or its
  [`InvalidConfigError`](../exceptions.md#invalidconfigerror) refinement —
  credentials, region, profile or endpoint that will not resolve while a client
  is built for a bare `s3://…` argument, and `copy_props=ALL` on an SDK without
  the annotations model (S3-to-S3 route), the latter raised as the engine is
  built and before any item.
- [`AccessDeniedError`](../exceptions.md#accessdeniederror) or
  [`TransportError`](../exceptions.md#transporterror) — creating the local
  destination directory failed, mapped by the same rules as any other local
  failure.
- [`CancelledError`](../exceptions.md#cancellederror) — `cancel_token` was
  cancelled. Graceful cancellation stops taking new pair actions and drains
  the transfers and delete batches already accepted; immediate cancellation
  also asks their futures to cancel. Either way, decisions outstanding on a
  `ParallelFilter` pool are awaited first, and that pool is never shut down.
- [`BatchError`](../exceptions.md#batcherror) — at least one item failed. Its
  `succeeded` and `failed` are the sums of the transfer and the delete side,
  while `warned` and `skipped` come from the transfer side only. `__cause__`
  is sampled from the transfer rollup first and from the delete rollup only
  when no transfer failed.

A failure before item processing — a listing rejected outright, for instance —
propagates as its category exception rather than as a `BatchError`. An
exception raised by a lane's own predicate is not translated: it propagates
and aborts the run, after decisions that have not started are cancelled and
running ones are awaited.

## boto3_s3.sync

`boto3_s3.sync(...)` is the module-level convenience wrapper: it runs
`S3.sync` on a default `S3()` instance and takes the same arguments minus
`self` — see [`./README.md`](./README.md).
