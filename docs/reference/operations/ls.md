# ls

`ls` enumerates the account's buckets, or the objects and common prefixes under
an S3 location. It is the one operation that reports through a listing callback
instead of the `OpResult` stream, so this page specifies that delivery contract
together with the two listing forms and their parameters. The callback and
entry types are specified in [`../results.md`](../results.md), the contract
every operation shares in [`README.md`](./README.md), and the narrative in
[`../../library/results.md`](../../library/results.md).

## S3.ls

Lists an S3 target, handing each entry to `on_entry` as it arrives, and returns
`None`. The call blocks until the listing and its cleanup have finished. `ls`
is S3-only: the target is an S3 location, never a local path and never a
non-S3 `Storage`.

Which of the two listings runs is decided by the target's bucket part. A target
with neither bucket nor key — the default bare `"s3://"` — is the service root
and lists the account's buckets, one `BUCKET`-kind entry each. A target that
names a bucket lists objects and common prefixes under its key, which is used
verbatim as the `ListObjectsV2` `Prefix`; a key without a bucket (`"s3:///k"`)
is rejected before either listing starts (see Raises). `aws s3 ls` splits the
two listings the same way, and each form ignores the other's parameters.

```python
def ls(
    self,
    target: Location = "s3://",
    *,
    on_entry: ListingCallback,
    recursive: bool = False,
    filter: FileFilter | None = None,
    request_payer: str | None = None,
    bucket_name_prefix: str | None = None,
    bucket_region: str | None = None,
    cancel_token: CancelToken | None = None,
) -> None: ...
```

### Target forms

`target` is an `S3Storage` or a `str` / `os.PathLike[str]` naming an S3
location. `Location`'s remaining member — a `Storage` that is not an
`S3Storage` — is rejected: none of the built-in backends implements
`__fspath__`, so a `LocalStorage`, `IOStorage`, or `StdioStorage` argument
falls into the `ValidationError` below rather than being listed.

An `S3Storage` is used as given, including a subclass, and keeps its own client
and its own listing config. A `PathLike` is expanded with `os.fspath`; the
resulting string is read as an S3 location whether or not it carries the
`s3://` scheme, so `"bucket/prefix"` and `"s3://bucket/prefix"` name the same
target and there is no local fallback for a scheme-less string. A string target
is wrapped in an `S3Storage` carrying this instance's `client()`, which is
therefore built before the first request goes out; an `S3Storage` you construct
yourself builds or reuses its own (see [`../storage.md`](../storage.md)).

The target's key is taken verbatim as the listing prefix: `ls` appends no
trailing `/` and normalizes nothing, so `"s3://bucket/pre"` covers every key
beginning with `pre` while `"s3://bucket/pre/"` covers only what lies under
that prefix. A key naming a single existing object is a prefix like any other —
`ls` issues no `HeadObject` and treats no target as a special case.

The resolved target is validated before the token is first consulted and before
any request is sent, so a malformed target raises even when `cancel_token` is
already cancelled.

### Entry delivery

`on_entry` is required and keyword-only; it has no default, so omitting it is a
`TypeError` from the call itself. It receives one `FileInfo` per entry, one
call at a time, **on the calling thread** — not on a worker thread, unlike the
transfer records the transferring operations emit from the engine's workers. It
therefore needs no locking of its own, but it sits in the delivery path: time
spent in it is time the listing is not consumed. An exception it raises
propagates out of `ls` unchanged, after the listing has been closed.

`ls` emits nothing to `on_result` — it has no such parameter, no `dryrun`, and
no `BatchError`: a listing has no per-item outcomes to report. Every entry the
listing returns that `filter` keeps reaches `on_entry`, zero-byte
`/`-terminated folder-marker keys included; only a cancellation, an error, or
an exception from `on_entry` or `filter` cuts the delivery short. Besides
`filter`, a listing narrows by a longer prefix, or by overriding
`S3Storage.scan_pages` on a subclass you pass as `target` — `ls` consumes that
producer, so what it omits or enriches is what `ls` delivers.

Entries from an object listing are `S3FileInfo` instances. `key` is the full S3
key, or the full prefix for a `DIRECTORY` entry, and `compare_key` is that key
with the listing `Prefix` removed. `size` and `mtime` are populated for `FILE`
entries and are `None` on `DIRECTORY` entries; `etag` and `storage_class` come
from the listing, while `owner` is populated only when the target `S3Storage`
was built with `fetch_owner=True`. Entries from the bucket listing carry the
bucket name as both `key` and `compare_key`, the bucket's `CreationDate` as
`mtime`, and no `size`. Both forms stamp `storage` with the listing backend.
The field contracts are in [`../results.md`](../results.md); because
`ListingCallback` is declared over `FileInfo`, narrow with `isinstance` before
reading the S3-only fields.

### Parameters

`recursive` selects the shape of an object listing. The default `False` lists
one level: `Delimiter='/'` is sent, so each sub-"directory" arrives as a single
`DIRECTORY`-kind entry instead of its contents. `True` sends no delimiter, so
every key under the prefix arrives as a `FILE` entry. `recursive` is ignored at
the service root, where nothing is nested.

`filter` decides which entries reach `on_entry`: a `FileFilter`
([`../filters.md`](../filters.md)), a predicate over the entry's `FileInfo`
returning `True` to deliver the entry and `False` to drop it silently. `None`
(the default) delivers every entry. It is offered every entry the listing
produces — the `DIRECTORY` entries of a non-recursive listing and the `BUCKET`
entries at the service root as well as `FILE` entries — and is shown the same
`FileInfo` that `on_entry` would receive. A relative
[`GlobFilter`](../filters.md#globfilter) pattern therefore matches the
`compare_key` described above: the key with the target's key removed as typed,
or the bucket name (`Include("prod-*")` after `Exclude("*")` keeps the buckets
whose names start with `prod-`). Two consequences follow:

- `Exclude("tmp/*")` also drops the common prefix `tmp/`, because `*` matches
  the empty string; `[Exclude("*"), Include("*.log")]` drops every `DIRECTORY`
  entry along with the other files. To keep the directories, wrap the
  compiled glob `keep`:
  `filter=lambda e: e.kind is FileKind.DIRECTORY or keep(e)`.
- `DIRECTORY` and `BUCKET` entries carry no `size`, and `DIRECTORY` entries no
  `mtime`, so a predicate that reads them must allow for `None`.

A recursive `rm`, `cp` or `mv` appends a `/` to a target key that lacks one
([`rm_filter_root`](./rm.md#rm_filter_root)), while `ls` takes the key as
typed. For a target that ends in `/`, a recursive `ls` therefore shows the
filter each key with the same `compare_key` those operations show it, so one
filter keeps the same keys in all four (`cp` and `mv` also skip zero-byte
`/`-terminated folder markers, which `ls` lists). For a target that does not,
`ls` also lists the keys that merely begin with it, and the `compare_key` of a
key below it starts with `/` (`s3://bucket/pre` offers `pre/a` as `/a`).

On an object listing the predicate runs page by page on the listing's prefetch
worker thread, as it does on the other operations' enumerating paths, so it
must be thread-safe, and a slow one throttles the listing; a target
`S3Storage` that seeds `read_ahead=False` in its `default_scan_options` runs
it on the calling thread instead. On the bucket listing, which has no worker,
it runs on the calling thread. An absolute pattern never matches a `BUCKET`
entry, which has no `bucket/key` path ([`../filters.md`](../filters.md#globfilter)).

`request_payer` is passed through as the `ListObjectsV2` `RequestPayer` value
and is not validated by the library — the service decides. `None` sends the
parameter not at all. It is ignored at the service root.

`bucket_name_prefix` and `bucket_region` filter the bucket listing, mapping to
`ListBuckets`'s `Prefix` and `BucketRegion`. Each is sent only when truthy, so
`""` behaves as `None` does. They apply at the service root only and are
ignored when the target names a bucket. Both need a recent enough SDK
([`../../compatibility.md`](../../compatibility.md)); requesting either on an
SDK below that raises `ValidationError` before any request, so a filtered
bucket listing either filters or fails.

`cancel_token` accepts a `CancelToken` shared with anything else that holds it
(see [`../results.md`](../results.md)). It is consulted once before the listing
starts, again before and after every `on_entry` call, by the page producer
between page pulls on an object listing, and on a filtered bucket listing
after `filter` has decided on each bucket — so cancelling from inside the callback
or the filter takes effect at the next entry boundary, even while `filter`
rejects every remaining entry. A cancellation stops
delivery, closes the listing, and then raises `CancelledError`; entries already
delivered stand, and a cancel that arrives after the last entry still ends the
call with `CancelledError` rather than a normal return. Both `CancelMode`
values have the same effect here, because a listing request already in flight
cannot be aborted safely: an object listing drops its prefetched pages and
waits for the page request in progress before reclaiming its prefetch worker,
and the bucket listing, which is iterated directly, simply stops between
entries.

Some listing knobs are not parameters of `ls`. The page size — for object
listings, and for bucket listings wherever `ListBuckets` paginates — and
`fetch_owner`, which reaches the object listing only, are configured on the
target `S3Storage` and seeded into the listings it produces, so pass a
configured `S3Storage` to tune them (see [`../storage.md`](../storage.md)). The
Ctrl-C posture of an object listing's page worker comes from
`S3(reusable_after_interrupt=...)` (see [`../s3.md`](../s3.md)).

### Ordering

`ls` sorts nothing and requests no sorting; entries reach `on_entry` in the
order the listing produces them, so what a caller may rely on is that order:

- A recursive object listing over a general-purpose bucket is ascending by
  `key` in UTF-8 byte order, preserved across pages — and therefore ascending
  by `compare_key` too, since the same prefix is removed from every entry. An
  S3 Express directory bucket is the carve-out: its listings carry no order
  guarantee at all, whether it is named directly or through one of its access
  points.
- A non-recursive object listing keeps the delimiter listing's native shape:
  each page's common prefixes are delivered ahead of that page's objects, which
  is `aws s3 ls`'s order. Across a multi-page listing the delivered stream is
  therefore grouped per page rather than globally ascending by key.
- The bucket listing is delivered in the order `ListBuckets` returns; `ls`
  makes no ordering promise there.
- An `S3Storage` subclass that overrides `scan_pages` decides the order of an
  object listing itself, and a custom order is not re-sorted by `ls`.

### Raises

An error raised while the listing runs surfaces on the pull that hits it, from
inside the delivery loop — so `ls` can raise after `on_entry` has already
received entries. Either listing's errors carry `operation="ls"`: the storage
raises them unnamed (the object listing is shared with the recursive forms of
`cp` / `mv` / `rm` / `sync`) and `ls` fills its own name in.

- [`ValidationError`](../exceptions.md#validationerror) — `target` is neither an
  `S3Storage` nor a `str` / `os.PathLike[str]`; it is an S3 Object Lambda or an
  Outposts bucket ARN, which these operations do not serve; it has a key but no
  bucket (`"s3:///k"`); botocore rejects a listing parameter client-side, such
  as a bucket filter (`bucket_name_prefix` / `bucket_region`) on an SDK whose
  `ListBuckets` lacks the matching input member; or the service answers with
  a 4xx whose error code the category table does not name.
- [`CancelledError`](../exceptions.md#cancellederror) — `cancel_token` was
  cancelled before or during the run.
- [`NotFoundError`](../exceptions.md#notfounderror) — the bucket does not exist
  (`NoSuchBucket`), or the response is another 404.
- [`AccessDeniedError`](../exceptions.md#accessdeniederror) — the listing is
  denied (`AccessDenied`, or another 403).
- [`TransportError`](../exceptions.md#transporterror) — a connection or
  timeout failure, a throttling or server-side code (`SlowDown`,
  `ServiceUnavailable`, `InternalError`, `RequestTimeout`), or another 5xx.
- [`ConfigurationError`](../exceptions.md#configurationerror), or its
  [`InvalidConfigError`](../exceptions.md#invalidconfigerror) refinement — a
  client that cannot be built: a set-but-unusable profile, partial
  credentials, a malformed region or endpoint; and missing credentials, which
  fail the listing's first request rather than the build. An S3 Express
  location the SDK predates raises `ConfigurationError` naming the version,
  before any request.
- [`Boto3S3Error`](../exceptions.md#boto3s3error) itself — a failure the
  translator cannot classify.

An exception raised by `on_entry` is not translated, wrapped, or suppressed.
Neither is one raised by `filter`, except for one detail: the predicate runs
inside the listing's pull, so a `Boto3S3Error` it lets out with `operation`
unset arrives with `operation="ls"`, the same exception object otherwise.

## boto3_s3.ls

The module-level convenience wrapper over a default `S3()`: `boto3_s3.ls(...)`
builds a fresh `S3()` per call and forwards to `S3.ls` with that method's exact
signature minus `self`, so everything above applies unchanged — see
[`README.md`](./README.md).
