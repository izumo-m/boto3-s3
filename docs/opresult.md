# The `OpResult` record

`OpResult` is the per-item completion record passed to the `on_result`
callback of `cp` / `mv` / `rm` / `sync`. One record is emitted per item, from a
worker thread (s3transfer's for transfers, `S3Deleter`'s for batched deletes) -
so `on_result` must be fast and must not raise.

It is a **single type discriminated by `transfer_type`**, not a per-subcommand
hierarchy - mirroring aws-cli, whose every result is one `BaseResult` keyed by
its `transfer_type`. The caller already knows which subcommand it invoked, so
the record only needs to say *what happened to this item*.

## Fields

| field | meaning |
|---|---|
| `transfer_type` | the verb (aws-cli's `transfer_type`): `upload` / `download` / `copy` / `move` / `delete`. |
| `key` | the operation-relative identity - the transfer `compare_key`, or the deleted object's key. Also the token the CLI matches byte-progress against. |
| `outcome` | `SUCCEEDED` / `FAILED` / `WARNED` / `SKIPPED` / `DRYRUN` / `NOTICE`. |
| `bytes_transferred` | bytes moved (0 for a delete or an advisory). |
| `error` | a `Boto3S3Error` on a failure; the advisory text on `WARNED` / `NOTICE`. Always the library taxonomy (every path translates into it), never a raw exception. |
| `src` / `dest` | display endpoints (`s3://bucket/key` or a native path) for aws's `verb: src to dest` line. A single-endpoint op (delete) sets `src` only. |
| `src_info` / `dest_info` | the operation's listing entries (`FileInfo`). |
| `src_storage` / `dest_storage` | the two sides' `Storage` backends. |
| `extra_info` | the result object's S3 response metadata: `{"ETag": ...}` by default, plus the full write / delete responses under `"write"` / `"delete"` when the operation ran with `capture_response=True`. |

### The `src` / `dest` convention

The three `src_*` fields describe **one** object and the three `dest_*` fields
the **other**, so they always agree within a side:

- `src` (display) ↔ `src_info` (entry) ↔ `src_storage` (backend)
- `dest` (display) ↔ `dest_info` (entry) ↔ `dest_storage` (backend)

The **source side** is the object being acted on: the source of a transfer, or
the object a delete removes (aws models a delete as `src`=path, `dest`=None - so
a delete fills the `src_*` trio and leaves `dest_*` empty). The **destination
side** is where a transfer writes; `dest_info` is populated only by `sync` (the
pre-existing object the copy compared against) - `cp` / `mv` never list the
destination, so they carry `dest` / `dest_storage` but no `dest_info`.

So `src_storage` + `src_info.key` (or `dest_storage` + `dest_info.key`) re-reaches
the object directly - e.g. a HeadObject - without re-deriving anything.

## Which operation populates which field

| field | cp / mv | sync (copy) | rm / sync (delete) | warning · notice |
|---|---|---|---|---|
| `transfer_type` | upload / download / copy (mv → `move`) | upload / download / copy | `delete` | the run's verb |
| `key` | `compare_key` | `compare_key` | the object key | `compare_key` (often `""`) |
| `outcome` | SUCCEEDED / FAILED / SKIPPED / DRYRUN | same | SUCCEEDED / FAILED / DRYRUN | WARNED / NOTICE |
| `src` / `dest` | both | both | `src` only | — / — |
| `bytes_transferred` | bytes | bytes | 0 | 0 |
| `error` | on FAILED | on FAILED | on FAILED | the message body |
| `src_info` | the source entry | the source entry | the removed object | — |
| `dest_info` | — | update: the pre-existing dest / new: — | — | — |
| `src_storage` | source side | source side | the target bucket | run's source side |
| `dest_storage` | dest side | dest side | — | run's dest side |
| `extra_info` | copy / download: `{"ETag": …}` · upload: — | same | — | — |

A **stream** `cp` (one side is an `IOStorage`) lists nothing, so `src_info` /
`dest_info` are both `None` and the stream endpoint renders as `-`; the
`src_storage` / `dest_storage` are still the two sides.

## `extra_info` (result metadata)

`extra_info` is the affected object's S3 response metadata. By default it is just
the ETag, as `{"ETag": "\"...\""}` (quoted, the raw S3 form):

- **copy** - the written object's ETag (the CopyObject response).
- **download** - the source object's ETag.
- **upload** - `None`: s3transfer discards the PutObject response, so the
  written object's ETag is not available by default (docs/transfer.md).
- **delete** / **warning** - `None`.

The default ETag comes from s3transfer's `future.meta.etag`; only what s3transfer
exposes is surfaced, so on an old s3transfer (or the CRT engine) it may be `None`
- a documented degradation, like the awscrt extra.

### `capture_response` - the full S3 responses

`cp` / `mv` / `rm` / `sync` accept `capture_response=True`, which surfaces the
**full S3 responses** an operation produced, keyed by role - only the slots that
apply are present:

- **`extra_info["write"]`** - the transferred object's write response for an
  **upload** or **copy**: the `PutObject`, `CopyObject`, or
  `CompleteMultipartUpload` response (whichever s3transfer issued), with its
  `ResponseMetadata` dropped. Only the terminal write is captured (the
  intermediate multipart calls are not), so the shape varies by which write API
  ran (a single-part `PutObject` vs a multipart `CompleteMultipartUpload`, which
  also carries `Location` / `Bucket` / `Key`). `"ETag"` is promoted from it -
  normalized, since `CopyObject` nests it under `CopyObjectResult` - so an upload
  carries an ETag too.
- **`extra_info["delete"]`** - the removed object's `DeleteObject`-shaped
  response (`VersionId` / `DeleteMarker` / `DeleteMarkerVersionId` /
  `RequestCharged`, whichever apply): an `mv`'s S3 source removal, and each object
  `rm` / `sync --delete` removes. The batched path reconstructs one per key from
  its `DeleteObjects` `Deleted[]` entry plus the shared `RequestCharged`, so the
  caller sees the same single-object shape regardless of the batch wire form
  (docs/deleter.md); `rm`'s blind single-key path (a non-recursive exact key)
  carries the `Storage.delete` response directly.

So an `mv` of one S3 object to another carries both `"write"` (the copy) and
`"delete"` (the source removal); a `cp` upload carries `"write"`; an `rm` carries
`"delete"`.

The **`"write"`** slot rides the botocore client's event stream, which the CRT
data plane bypasses, so `capture_response=True` **forces the classic transfer
engine** (a library-only flag with no `aws s3` equivalent, so no parity impact).
It registers handlers on the transfer's client for the operation's span and
removes them after; run a capture operation with a client not used concurrently
elsewhere (docs/s3.md thread-safety note). The **`"delete"`** slot rides no
events - the delete calls are issued directly - so it works on any engine.

## `error`

`error` is always a `Boto3S3Error` (the library exception taxonomy) or `None` -
never a raw exception. A `FAILED` record carries the failure; a `WARNED` /
`NOTICE` record carries the advisory text (the CLI prints `warning: {error}`).

## Example

```python
from boto3_s3 import OpOutcome, OpResult, TransferType

def on_result(r: OpResult) -> None:
    if r.outcome is not OpOutcome.SUCCEEDED:
        return
    if r.transfer_type is TransferType.DELETE:
        print("removed", r.src_info.key if r.src_info else r.key)
        return
    etag = (r.extra_info or {}).get("ETag")  # copy / download; upload only under capture_response
    print("transferred", r.src_info.key if r.src_info else r.key, etag)
```

Re-reaching the result object (e.g. to HEAD it):

```python
storage, info = r.src_storage, r.src_info  # a delete's removed object
if storage is not None and info is not None:
    head = storage.get_client().head_object(Bucket=storage.bucket, Key=info.key)
```
