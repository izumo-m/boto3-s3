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
no `BatchError`: a listing has no per-item outcomes to report. It also filters
nothing. There is no `filter=` parameter, and every entry the listing returns
reaches `on_entry`, zero-byte `/`-terminated folder-marker keys included; only
a cancellation, an error, or an exception from `on_entry` cuts the delivery
short. Narrow a listing by choosing a longer prefix, by discarding entries
inside the callback, or by overriding `S3Storage.scan_pages` on a subclass you
pass as `target` — `ls` consumes that producer, so what it omits or enriches is
what `ls` delivers.

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
every key under the prefix arrives as a `FILE` entry and no `DIRECTORY` entries
are produced. It is ignored at the service root, where nothing is nested.

`request_payer` is passed through as the `ListObjectsV2` `RequestPayer` value
and is not validated by the library — the service decides. `None` sends the
parameter not at all. It is ignored at the service root.

`bucket_name_prefix` and `bucket_region` filter the bucket listing, mapping to
`ListBuckets`'s `Prefix` and `BucketRegion`. Each is sent only when truthy, so
`""` behaves as `None` does. They apply at the service root only and are
ignored when the target names a bucket. Both need a recent enough SDK
([`../../compatibility.md`](../../compatibility.md)) and fail in two distinct
ways below it: where botocore cannot paginate `ListBuckets` at all, `ls` issues
one unpaginated call and these two are never sent, so the unfiltered bucket
list arrives; where botocore paginates but predates the two input parameters,
the request is rejected client-side and `ls` raises `ValidationError`.

`cancel_token` accepts a `CancelToken` shared with anything else that holds it
(see [`../results.md`](../results.md)). It is consulted once before the listing
starts, again before and after every `on_entry` call, and — for an object
listing — by the page producer between page pulls, so cancelling from inside
the callback takes effect at the next entry boundary. A cancellation stops
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
`S3(wait_on_interrupt=...)` (see [`../s3.md`](../s3.md)).

### Ordering

`ls` sorts nothing and requests no sorting; entries reach `on_entry` in the
order the listing produces them, so what a caller may rely on is that order:

- A recursive object listing over a general-purpose bucket is ascending by
  `key` in UTF-8 byte order, preserved across pages — and therefore ascending
  by `compare_key` too, since the same prefix is removed from every entry. An
  S3 Express directory bucket is the carve-out: its listings carry no order
  guarantee at all.
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
received entries. Botocore errors from the object listing carry
`operation=None`, because that listing path is shared with the recursive forms
of `cp` / `mv` / `rm` / `sync`; the bucket listing stamps `operation="ls"`.

- [`ValidationError`](../exceptions.md#validationerror) — `target` is neither an
  `S3Storage` nor a `str` / `os.PathLike[str]`; it is an S3 Object Lambda or an
  Outposts bucket ARN, which these operations do not serve; it has a key but no
  bucket (`"s3:///k"`); botocore rejects a listing parameter client-side; or the
  service answers with a 4xx whose error code the category table does not name.
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
  [`InvalidConfigError`](../exceptions.md#invalidconfigerror) refinement — the
  client cannot be built: unresolvable credentials or region, a set-but-unusable
  profile, a malformed endpoint.
- [`Boto3S3Error`](../exceptions.md#boto3s3error) itself — a failure the
  translator cannot classify.

An exception raised by `on_entry` is not translated, wrapped, or suppressed.

## boto3_s3.ls

The module-level convenience wrapper over a default `S3()`: `boto3_s3.ls(...)`
builds a fresh `S3()` per call and forwards to `S3.ls` with that method's exact
signature minus `self`, so everything above applies unchanged — see
[`README.md`](./README.md).
