# Operations

The nine `aws s3` operations, one page each. Every page documents the `S3`
method as its primary entry — the method's own parameters, what it emits, and
what it raises. This page states what the nine share: the location forms they
accept, the module-level functions that wrap them, the parameters that recur
across them, and the end-of-run error model. The `S3` class itself — its
constructor, `client`, `resolve` and the subclassing contract — is in
[`../s3.md`](../s3.md); the narrative counterparts to this page are
[`../../library/README.md`](../../library/README.md) and
[`../../library/results.md`](../../library/results.md).

## The nine operations

- [`cp`](./cp.md) — copy bytes between a source and a destination.
- [`mv`](./mv.md) — copy, then delete each source whose transfer succeeded.
- [`sync`](./sync.md) — recursively bring a destination in line with a source:
  create new entries, overwrite changed ones, optionally delete orphans.
- [`ls`](./ls.md) — list objects and common prefixes under an S3 target, or
  list buckets.
- [`rm`](./rm.md) — delete objects under an S3 target.
- [`mb`](./mb.md) — create a bucket.
- [`rb`](./rb.md) — delete a bucket, which must already be empty.
- [`presign`](./presign.md) — compute a presigned URL for an object.
- [`website`](./website.md) — set a bucket's website configuration.

`cp` / `mv` / `sync` are the transfer operations: two locations, bytes moving
between them. The remaining six are S3-only: one target, read as S3. The split
decides how a location argument is interpreted.

## Location arguments

Every location parameter is typed `Location`, which is
`str | os.PathLike[str] | Storage` ([`../storage.md`](../storage.md)). What a
given value resolves to, and which client the result carries, is specified once
under [Locations and clients](../s3.md#locations-and-clients); which half of
that contract applies follows the split above.

`cp`, `mv` and `sync` pass both `src` and `dest` through `S3.resolve`, so only
an `s3://` string is an S3 location and a scheme-less `"bucket/key"` is a local
relative path to them. `ls`, `rm`, `mb`, `rb`, `presign` and `website` resolve
their single `target` themselves and read it as S3 whether or not it carries
the scheme, with no local fallback.

Four consequences are the operations' own:

- Which source and destination pairings a transfer accepts, and which it
  rejects, is stated on each operation's page.
- The three transfer operations are the only callers of `resolve`, so a
  subclass that overrides it to add a scheme reaches them and none of the other
  six. Outside the operations,
  [`ChecksumComparison`](../comparator.md#checksumcomparison) also resolves the
  two locations it is constructed with through the `S3` it is given, so the
  override reaches that strategy too.
- The six S3-only operations reject a `Storage` that is not an `S3Storage` with
  `ValidationError` ([`../exceptions.md`](../exceptions.md)), since none of the
  library's `Storage` classes implements `__fspath__`. A custom backend
  ([`../../library/custom-storage.md`](../../library/custom-storage.md)) that
  does implement it is expanded like any other path-like value and read as an
  S3 target instead.
- Accepting the scheme-less form is deliberate leniency: the library is more
  permissive than `aws s3`, and the strict path rejections belong to the
  command layer ([`../../library/README.md`](../../library/README.md)).

Storage construction is permissive, so an operation validates the storage it is
about to use (`Storage.validate`, [`../storage.md`](../storage.md)): a
malformed `s3://` URI or an unsupported ARN form raises `ValidationError`
before any request is sent. A `cp` with a stream on one side is the carve-out —
only its S3 peer is validated, a stream endpoint carrying no location to check.
Requirements beyond validation, such as whether a bucket is required and
whether a key is used or ignored, are per-operation and stated on each page.

## Module-level functions

`boto3_s3.cp`, `ls`, `mv`, `rm`, `mb`, `rb`, `presign`, `sync` and `website`
are module-level functions wrapping the identically named `S3` methods. Each
one constructs a default `S3` and calls the method on it:

```python
boto3_s3.cp(*args, **kwargs)  ==  S3().cp(*args, **kwargs)
```

A wrapper takes exactly its method's parameters minus `self`, forwards them
unchanged, and returns what the method returns — `None` for all of them except
`presign`, which returns the URL string. Each operation page documents the
method; the wrapper adds nothing to the contract but the `S3()` it builds.

That `S3()` is constructed per call with no arguments: no `session`, no
`endpoint_url`, no `config`, no default `transfer_config`, and
`wait_on_interrupt=True`. Nothing is shared or memoized between calls, so each
call builds its own clients. Configuring any of that means constructing an `S3`
yourself and calling the method ([`../s3.md`](../s3.md)). Subclassing does not
reach these functions either — they instantiate `S3` itself, not the subclass.

Introspection reports the method's identity: `functools.wraps` copies
`__name__`, `__qualname__`, `__doc__` and `__module__`, and `__signature__` is
pinned to the method's signature with `self` removed, so `inspect.signature`
and `Signature.bind` see the documented parameters rather than the bound-method
signature `__wrapped__` would otherwise expose.

All nine are exported from `boto3_s3` and from `boto3_s3.s3`. The root
re-export is lazy: reading one of these names imports `boto3_s3.s3`, and with
it boto3.

## Shared parameters

### `on_result`

`cp`, `mv`, `rm` and `sync` take `on_result: ResultCallback | None = None` and
return `None`; the record stream is the result. The default discards records.
`ls` has no `on_result` — it delivers listing entries through its required
`on_entry: ListingCallback` instead — and `mb`, `rb`, `presign` and `website`
emit nothing.

Records arrive while the run proceeds, not at the end. An item the operation
acts on produces one terminal record — `SUCCEEDED`, `FAILED`, `SKIPPED`,
`DRYRUN` or `CANCELLED` — while `WARNED` and `NOTICE` records are advisories
outside that rule, so one key may produce more than one callback. An item that
never reaches the operation, because a filter dropped it during enumeration or
because the run ended first, produces nothing. Two carve-outs sit between the
two — an item a pre-transfer gate consumes with an advisory instead of an
outcome, and an entry discarded from a batched delete's unsent buffer — and
both are stated with `OpResult` in [`../results.md`](../results.md).

The callback runs on a worker thread for engine transfers and for batched
deletes, and on the scan's prefetch worker for the warnings a walk or listing
raises, which can therefore arrive alongside transfer records. Records emitted
inline — dry-run reports, a single-key `rm`, synchronous deletes — run on the
calling thread. Keep it fast, keep it thread-safe, and do not let it raise —
the library states no contract for a callback that raises. `OpResult`, the
outcome set, and which fields each record carries are specified in
[`../results.md`](../results.md).

### `dryrun`

`cp`, `mv`, `rm` and `sync` take `dryrun: bool = False`. The other five
operations have no such parameter.

`dryrun=True` makes the run issue no mutating API call: nothing is transferred
and nothing is deleted. A missing local destination directory is still created,
though — by a recursive `cp` or `mv`, and by `sync` — as it is on a live run.
Enumeration is unchanged — both sides' listings for `sync`, the recursive
listing for `rm`, the listing and the single-object HeadObject for `cp` and
`mv` — so the listing cost is real and a listing failure still surfaces. Every
item the run would have acted on is reported as an `OpOutcome.DRYRUN` record on
`on_result`, and the warnings a live run would emit still apply; an item one of
the transfer gates consumes keeps that gate's own record instead of becoming a
`DRYRUN` one.

### `cancel_token`

`ls`, `cp`, `mv`, `rm` and `sync` take
`cancel_token: CancelToken | None = None`; the single-call operations
(`mb`, `rb`, `presign`, `website`) take none. The default leaves the run
uncancellable.

The token is polled by the operation, and it may be cancelled from another
thread or from inside `on_result` / `on_entry`. A token that is already
cancelled when the call starts raises `CancelledError` before the operation's
own side effects — destination directory creation, a deferred client build, any
submission — though a bare `"s3://..."` argument's client is built during
resolution, which happens before that first poll.

If the token is cancelled at any point the call observes, the call ends by
raising `CancelledError` after the operation has reclaimed its workers; it does
not return normally, and it does not raise `BatchError`. `CancelToken`,
`CancelMode`, and what the two modes do to accepted and in-flight work are
specified in [`../results.md`](../results.md).

### The rest

Further parameter groups recur without being shared by all nine, and each is
documented where it is owned:

- `on_progress: ProgressCallback | None` on `cp` / `mv` / `sync` — byte-level
  progress ([`../results.md`](../results.md)). `rm` moves no bytes and has none.
- `capture_response: bool` on `cp` / `mv` / `rm` / `sync` — what a successful
  record's `extra_info` carries ([`../results.md`](../results.md)).
- `filter: FileFilter | None` on `cp` / `mv` / `rm` / `sync`
  ([`../filters.md`](../filters.md)), and, on `cp` / `mv` / `sync`, the
  `transfer_config` argument plus the `**options` transfer options
  ([`../options.md`](../options.md)). Each operation page states which key the
  filter matches against for that operation.
- `recursive: bool = False` on `cp` / `mv` / `rm` / `ls` — what it selects
  differs per operation, so each page states its own meaning. `sync` is always
  recursive and takes no such parameter.
- `request_payer: str | None = None` on `ls` and `rm`; on `cp` / `mv` / `sync`
  the same setting is the `request_payer` key of `**options`
  ([`../options.md`](../options.md)).

## The error model

The many-item operations — `cp`, `mv`, `rm` and `sync` — do not abort at the
first item failure. A failed item becomes a `FAILED` record on `on_result` and
the run continues through the remaining items. When at least one item failed,
the call raises `BatchError` once, at the end, after workers are reclaimed; it
carries the run's rollup counts and a sampled failure on `__cause__`. Warnings
and skips alone do not raise. The shape does not depend on item count: a
single-object `cp` or a single-key `rm` whose one item failed also ends in
`BatchError`, reported as 1 of 1.

A failure that happens before item processing begins raises its category
exception directly instead: an unknown key in `**options`
(`ValidationError`), a target that will not resolve or validate
(`ValidationError`), a client that cannot be built (`ConfigurationError` or its
`InvalidConfigError` refinement), a missing local source (`NotFoundError`,
worded as the AWS CLI words it), a listing rejected outright.

`mb`, `rb`, `presign` and `website` act on a single thing and raise their
category exception directly; they never raise `BatchError`. `ls` has no item
outcomes to aggregate either — a listing failure propagates as its category
exception.

Cancellation supersedes the batch raise, as described under `cancel_token`
above: a cancelled run raises `CancelledError`, never `BatchError`.

Errors originating in botocore or in the filesystem are translated into the
library's own hierarchy before they surface, with the original exception on
`__cause__` where one exists. The hierarchy, the per-class raise conditions and
`BatchError`'s counters are specified in
[`../exceptions.md`](../exceptions.md).
