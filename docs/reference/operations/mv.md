# mv

`mv` is `cp` plus a per-item source deletion: each source is removed once its
own transfer has succeeded, with `aws s3 mv` semantics. This page states what
`mv` adds to [`cp.md`](./cp.md) and what it rejects that `cp` accepts;
everything else about routes, path shapes, enumeration, gates, options and the
result stream is specified there. The shared operation contract is in
[`README.md`](./README.md), and the narrative pages are
[`../../library/transfer-options.md`](../../library/transfer-options.md),
[`../../library/streams.md`](../../library/streams.md) and
[`../../library/results.md`](../../library/results.md). The design, including
the ordering that makes the guarantees below hold, is
[`../../../design/transfer.md`](../../../design/transfer.md).

## S3.mv

Copies every enumerated source item to the destination, deletes each item's
source as soon as that item's transfer succeeds, and returns `None` when every
item succeeded. The route is chosen from the resolved pair as `cp` chooses it,
with one exception — a stream destination, which `mv` runs through the open
route ([The stream destination](#the-stream-destination)) — and the deletion is
per item: it follows each transfer individually, not the run as a whole.

```python
def mv(
    self,
    src: Location,
    dest: Location,
    *,
    recursive: bool = False,
    filter: FileFilter | None = None,
    dryrun: bool = False,
    on_progress: ProgressCallback | None = None,
    on_result: ResultCallback | None = None,
    cancel_token: CancelToken | None = None,
    transfer_config: TransferConfig | None = None,
    capture_response: bool = False,
    **options: Unpack[TransferOptions],
) -> None: ...
```

### What `mv` shares with `cp`

The parameters are `cp`'s minus `expected_size`, and each behaves as
[`cp.md`](./cp.md) specifies: `src` / `dest` resolution and the accepted
pairings, directory semantics and destination naming, single-object versus
recursive enumeration, `filter` and the key it matches, `dryrun`,
`on_progress`, `cancel_token`, `transfer_config`, `capture_response` and
`**options`. The gates are the same too — the archived-object gate, the
parent-directory guard, `no_overwrite`, `case_conflict` — as are the walk
warnings, the `BatchError` model and the per-item record stream.
`expected_size` is absent because it is a hint for uploading from a stream,
and a stream is never a move source.

`filter` therefore prunes the deletion as well as the transfer: an entry the
predicate rejects is not transferred, so its source is not removed either.

### The delete step

The source is deleted only after that item's own transfer has succeeded, and
never before. How the removal is issued follows the route:

- an upload's source — a local file, or an entry in a custom backend — is
  removed through that backend's own `Storage.delete`, addressed by the source
  listing entry;
- a download's S3 source is removed with one `DeleteObject` per object, on the
  transfer's client;
- an S3-to-S3 copy's source is removed with one `DeleteObject` per object, on
  the source side's client, so a two-account move deletes with the credentials
  that read.

`request_payer` rides the delete call like every other request. Deletions are
issued one object at a time; `mv` does not batch them the way `rm` and `sync`'s
orphan removal do.

The exclusions are the mirror of that guarantee. A source is kept when its item
failed, when the item was skipped (`no_overwrite` declining, an archived
source), when `filter` dropped it, and for every item under `dryrun=True`. A
source is also kept when the destination `LocalStorage` was constructed with
`fsync=True` and that barrier failed: it runs before the deletion, so a download
whose bytes are not yet durable does not lose its S3 copy. Folder markers are
dropped from a recursive S3 listing, so a recursive move neither transfers nor
deletes them; and emptied local source directories are left behind, since `mv`
removes objects and files, not directories.

If the delete itself fails, the item becomes a `FAILED` record even though its
bytes already arrived at the destination, and it counts toward `BatchError`
like any other failure. On a download, the source's modification time is
stamped onto the destination file before the deletion is attempted, so a failed
delete still leaves the stamped file behind.

### Reporting

Every record `mv` emits reports `transfer_type` as `TransferType.MOVE`,
whatever route carried the bytes — successes, failures, skips, warnings,
notices and dry runs alike. The routing kind still shows through in the
messages an archived-object warning carries.

The record fields are otherwise `cp`'s, with one carve-out on the stream
destination route described below. Under `capture_response=True` a
successful record carries both the transfer's own slot (`"write"` for an upload
or copy, `"read"` for a download) and a `"delete"` slot holding the source
removal's response where the backend produced one — an S3 `DeleteObject`
response, or whatever mapping a custom backend's `delete` returned. A local
file unlink returns nothing, so it contributes no slot
([`../results.md`](../results.md)).

### What `mv` rejects that `cp` accepts

Two checks are `mv`'s own, and each raises
[`ValidationError`](../exceptions.md#validationerror) before anything is
listed, transferred or deleted:

- **a stream source.** An [`IOStorage`](../storage.md#iostorage) or
  [`StdioStorage`](../storage.md#stdiostorage) as `src` is refused outright: a
  move deletes its source, and a stream is not something that can be deleted.
- **a move onto the same object.** When both sides resolve to S3, the two
  keyless-normalized URIs are compared: an exact match is refused, and so is a
  destination ending in `/` whose join with the source's basename reproduces
  the source. The check runs for `recursive=True` as well, which is the AWS
  CLI's own false positive — `mv("s3://b/p", "s3://b/", recursive=True)` is
  refused even though no key would map onto itself. The error names both URIs
  in normalized form, so a source `s3://b/k` and a destination `s3://b` are
  reported as `s3://b/k - s3://b/`.

`cp`'s two stream-destination guards are not among them: `mv` applies them at
its own entry point, in its own wording. `recursive=True` with a stream `dest`
is refused because a stream is a single endpoint, and `no_overwrite` with a
stream destination because there is no existing destination to check — which
for a move would mean deleting the source anyway. The four checks run in this
order: stream source, recursive stream destination, `no_overwrite` with a
stream destination, same-object move.

The same-object guard is textual. Two paths that reach the same underlying
bucket through an access point ARN, an access point alias or a multi-region
access point are not detected by it; resolve them yourself with
[`S3PathResolver`](../misc.md#s3pathresolver) beforehand when that risk
applies. The library never builds the `s3control` / `sts` clients such a
resolution needs.

### The stream destination

A stream may be the destination of a non-recursive move: the bytes land on the
stream, then the S3 source is deleted. Unlike `cp`, which routes any stream
side down its own single-item path, `mv` runs this through the open route — the
same route a custom backend takes — with three visible consequences. `filter`
is consulted for the single source entry, so a predicate can call the whole
move off, whereas on a streaming `cp` no filter is consulted at all. The
source-side archived-object gate applies, while the destination-side gates
(`case_conflict`, the parent-directory guard, `no_overwrite`'s existence check)
do not — a stream owns no key space to check them against. And the source is
resolved through a listing entry, so each record carries `src_info` (the
source's `HeadObject` entry), where a streaming `cp` lists nothing and carries
none.

A custom backend used as an `mv` source must additionally declare the `DELETE`
capability, on top of what `cp` requires of that route; a backend that does not
is rejected with `ValidationError` before any bytes move
([`../storage.md`](../storage.md)).

### Raises

Everything [`cp.md`](./cp.md#raises) lists, plus the two `mv`-specific
`ValidationError` cases above, `mv`'s own wording for `cp`'s two
stream-destination guards, and the extra `DELETE` capability requirement. A
source that could not be deleted is not a separate exception: it is that item's
`FAILED` record, aggregated into
[`BatchError`](../exceptions.md#batcherror) with the rest.

## boto3_s3.mv

`boto3_s3.mv(...)` is the module-level convenience wrapper: it runs the method
above on a freshly built default `S3()`, and its signature is `S3.mv`'s minus
`self`. See [`README.md`](./README.md#module-level-functions) for the shared
wrapper contract.
