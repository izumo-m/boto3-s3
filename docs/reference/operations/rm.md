# rm

`rm` deletes S3 objects with `aws s3 rm` semantics: a blind single-key delete,
a recursive prefix delete, or the bucket-root folder-marker sweep, chosen by
the target's key and `recursive`. The contract shared by all nine operations —
accepted path forms, client selection, the module-level wrappers — is in
[`README.md`](./README.md); the narrative pages are
[`../../library/filters.md`](../../library/filters.md) for which entries take
part, [`../../library/results.md`](../../library/results.md) for the result
stream, and [`../../library/deleter.md`](../../library/deleter.md) for the
batching machinery.

## S3.rm

Deletes objects under `target` and returns `None` when every item succeeded.
The target's key and `recursive` select one of three shapes, reproducing the
three `aws s3 rm` takes:

- **a non-empty key, non-recursive** — one blind `DeleteObject` of exactly
  that key. There is no listing and no `HeadObject`, so deleting a key that
  does not exist succeeds (the service reports a missing key as deleted). A
  key ending in `/` deletes that "folder marker" object itself, not the
  objects under it.
- **recursive** — every object under the `/`-normalized prefix
  [`rm_filter_root`](#rm_filter_root) returns, folder markers included. Because
  that prefix is `/`-terminated, `rm("s3://b/data", recursive=True)` lists
  under `data/` and does not touch `data-sibling.txt`. With an empty key it is
  the whole bucket.
- **the bucket root, non-recursive** — lists the whole bucket but deletes only
  zero-byte `/`-terminated folder markers, at any depth. This is the AWS CLI's
  manual-folder sweep, not a full wipe.

Both enumerating shapes list recursively, without a delimiter, so every entry
that reaches a filter or a delete is an object — a common-prefix entry never
appears. Deletion overlaps enumeration: entries are handed to an
[`S3Deleter`](../misc.md#s3deleter), which deletes them in batches on a
background worker while the listing continues, sending a key that XML 1.0
cannot carry through a per-key request instead. The batching is a wire-level
difference from `aws s3 rm`, observably equivalent for ordinary keys; the
consequences are stated in
[`../../../design/deleter.md`](../../../design/deleter.md).

```python
def rm(
    self,
    target: Location,
    *,
    recursive: bool = False,
    filter: FileFilter | None = None,
    dryrun: bool = False,
    request_payer: str | None = None,
    on_result: ResultCallback | None = None,
    cancel_token: CancelToken | None = None,
    capture_response: bool = False,
) -> None: ...
```

`target` names what to delete: an `"s3://bucket/key"` string (the `s3://`
scheme is optional, so `"bucket/key"` works too), an `os.PathLike[str]`
yielding such a string, or an [`S3Storage`](../storage.md#s3storage). A string
or `os.PathLike` becomes an `S3Storage` carrying this instance's `client()`; an
`S3Storage` is used as given, with its own client — and, on the enumerating
shapes, with its own `page_size` and `fetch_owner` governing the listing
([`../options.md`](../options.md#s3scanoptions)). Any other `Storage` — a
`LocalStorage`, a custom backend — is rejected. Unlike `mb` and `rb`, `rm`
uses the key part as well as the bucket. Like them, it requires a bucket: the
bare service root `"s3://"` is rejected, because `rm` has no bucket-listing
mode.

`recursive` picks the recursive shape above. It is the only way to delete
arbitrary objects under a prefix; with a non-empty key and `recursive=False`
the call touches exactly the one key named. With an empty key `recursive=False`
is the folder-marker sweep, which also deletes many keys, but only zero-byte
`/`-terminated ones — not a bucket wipe.

`filter` decides which entries take part: a `FileFilter`, a predicate over the
entry's `FileInfo` returning `True` to delete the entry and `False` to leave it
alone. A rejected entry is dropped silently — no `OpResult` record, matching
`aws s3` — and `None` (the default) keeps everything enumerated, except on the
bucket-root sweep, where the folder-marker test still applies. The key a
pattern matches is the entry's `compare_key`: on the enumerating shapes the
scan stamps it `Prefix`-relative, and that `Prefix` is
[`rm_filter_root`](#rm_filter_root)'s return value; the blind single-key path
has no listing and stamps the same relativization by hand. A relative
[`GlobFilter`](../filters.md#globfilter) pattern therefore matches the key
relative to that root, while an absolute pattern matches the full `key` — and
since an S3 key normally has no leading `/`, an absolute pattern is inert here
unless a key literally begins with one. On the bucket-root sweep the
folder-marker test runs before `filter`, so the predicate is consulted only for
entries that are already zero-byte `/`-terminated keys.

What the predicate can read differs by shape. On the enumerating shapes the
`FileInfo` is a listing entry, so `size`, `mtime` and `storage_class` are
populated alongside `key`, `compare_key` and `storage`. On the blind
single-key path nothing is listed, so only `key`, `compare_key` and `storage`
are set and the other three are `None` — guard for that in a predicate that may
run there. Threading also differs: on the enumerating shapes the predicate runs
page by page on the scan's prefetch worker, overlapped with the listing I/O, so
it must be thread-safe and cheap; on the blind path it runs inline on the
calling thread.

`dryrun=True` reports every candidate as an `OpOutcome.DRYRUN` record and
issues no delete call. `filter` is applied first, so a rejected entry produces
no dry-run record either. Enumeration still happens on the two enumerating
shapes — the listing cost is real, and so is a listing failure; the blind
single-key path lists nothing, so a dry run there sends no request at all.

`request_payer` is the value sent as the S3 `RequestPayer` request field
(`"requester"` for a requester-pays bucket). It rides both the listing and the
delete calls. The library does not validate it — the service decides — and the
default `None` sends the field on neither.

`on_result` receives one [`OpResult`](../results.md#opresult) per item that
reaches the operation. `transfer_type` is `TransferType.DELETE`, `compare_key`
is the `rm_filter_root`-relative key described above, `src` is the
`s3://bucket/key` display form, and `src_info` / `src_storage` carry the entry
and the target storage; the `dest_*` fields are unset and `bytes_transferred`
is `0` — `rm` moves no bytes and takes no `on_progress`. The outcomes it emits
are `SUCCEEDED`, `FAILED` and `DRYRUN`: an entry `filter` rejected produces no
record, entries abandoned by a cancellation produce no record, and `rm` emits
no `CANCELLED`, `WARNED`, `SKIPPED` or `NOTICE` records at all. Records for
batched deletes arrive on the deleter's worker thread, in submission order
within a batch; the blind single-key path and every dry-run record are emitted
inline on the calling thread. The callback must be fast and must not raise.

`cancel_token` accepts a [`CancelToken`](../results.md#canceltoken), which may
be cancelled from `on_result` or from another thread. It is polled once the
target has been resolved (the shared `Storage.validate` step) and before `rm`'s
own bucket requirement is checked, so a token cancelled before the call stops
the run before anything is listed or deleted, and supersedes the `"s3://"`
rejection below. During a run it stops the listing prefetch producer between
page pulls and stops the deleter from dispatching further batches; what each
mode leaves buffered, dispatched or in flight is specified with the token
itself. Either mode ends by raising
[`CancelledError`](../exceptions.md#cancellederror) once the worker has been
reclaimed.

`capture_response=True` attaches the delete response to each successful
record's `extra_info` under the key `"delete"`, with `ResponseMetadata`
removed. Both routes produce the single-object `DeleteObject` shape — the
batched route reconstructs it from the batch response — so the wire form does
not show through. Failed and dry-run records keep `extra_info` at `None`,
which is also what every record carries when `capture_response` is `False`.

Raises:

- [`ValidationError`](../exceptions.md#validationerror) — `target` is neither
  a string, an `os.PathLike`, nor an `S3Storage`; the location is an
  unsupported ARN form or has a key but no bucket; or its bucket part is empty
  (`"s3://"`), which this operation requires. No request is sent in these
  cases.
- [`ConfigurationError`](../exceptions.md#configurationerror), or its
  [`InvalidConfigError`](../exceptions.md#invalidconfigerror) refinement —
  the client had to be built and credentials, region, profile or endpoint were
  unresolvable. Only when the target did not bring its own client.
- The listing's own rejection, translated to its category and propagated
  untouched: `NoSuchBucket` becomes
  [`NotFoundError`](../exceptions.md#notfounderror), a denied `ListBucket`
  becomes [`AccessDeniedError`](../exceptions.md#accessdeniederror), a 5xx or
  throttling response becomes
  [`TransportError`](../exceptions.md#transporterror). A listing that fails
  part-way through propagates the same way even though earlier batches already
  deleted objects — no rollup is raised in that case, and the records already
  delivered stand.
- [`BatchError`](../exceptions.md#batcherror) — at least one item's delete
  failed. Raised once, after enumeration has finished, carrying the run's
  `succeeded` and `failed` counts with `warned` and `skipped` both `0` (`rm`
  produces neither) and the first per-item failure on `__cause__`. The blind
  single-key path uses the same shape, reported as 1 of 1.
- [`CancelledError`](../exceptions.md#cancellederror) — the run was cancelled
  through `cancel_token`.
- Anything `on_result` itself raises. On the batched path it surfaces from
  `rm` at the next batch dispatch or when the deleter is closed, rather than at
  the callback, and the remaining records of that batch are not delivered.
- Anything `filter` itself raises. On the enumerating shapes the predicate's
  exception surfaces on the consuming side of the enumeration; on the blind
  single-key path it is raised inline. Either way the run is abandoned: the
  deleter's unsent buffer is discarded while the batch already in flight
  drains.

## boto3_s3.rm

`boto3_s3.rm(...)` is the module-level convenience wrapper: it runs the method
above on a freshly built default `S3()`, and its signature is `S3.rm`'s minus
`self`. See [`README.md`](./README.md) for the shared wrapper contract.

## rm_filter_root

Returns the prefix an `rm` of `key` operates under: the prefix a relative
filter pattern resolves against and, on the enumerating shapes, the `Prefix`
the listing is issued with. It is a pure function of its arguments — no
request, no client, no `S3` instance — so a caller can predict exactly which
key a [`GlobFilter`](../filters.md#globfilter) pattern will be matched against
before calling `rm`. It is exported from the `boto3_s3` root and from
`boto3_s3.s3`.

```python
def rm_filter_root(key: str, *, recursive: bool) -> str: ...
```

`key` is the target's S3 key alone — no bucket, no `s3://` scheme; it is what
`S3Storage.key` holds for the target `rm` was given. The bucket never takes
part in the result. `recursive` is the flag `rm` was called with. The three
cases are:

- `recursive=True` — the key normalized to a `/`-terminated form: `"data"` and
  `"data/"` both give `"data/"`, and `"data/sub"` gives `"data/sub/"`. An
  empty key gives `""`.
- `recursive=False` with a key not ending in `/` — everything through its last
  `/`, so `"data/a.txt"` gives `"data/"`, and `""` when the key contains no
  `/` at all, so `"a.txt"` gives `""`.
- `recursive=False` with a key that already ends in `/`, or an empty key — the
  key unchanged: `"data/"` gives `"data/"` and `""` gives `""`.

The result is therefore either empty or `/`-terminated; no other form is
returned.

An entry's `compare_key` is its full key with this prefix removed, which makes
two cases worth predicting. Under `rm("s3://b/data", recursive=True)` the root
is `"data/"`, so `data-sibling.txt` is outside the listing altogether and an
object at `data/app/x.txt` is matched as `app/x.txt`. Under the non-recursive
`rm("s3://b/data/")` the root is the target key itself, so the single entry's
`compare_key` is the empty string: a relative pattern has to match `""` to
reject it, while an absolute pattern still matches the full key `data/`.
