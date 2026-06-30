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
| `src_info` / `dst_info` | the operation's listing entries (`FileInfo`). |
| `src_storage` / `dst_storage` | the two sides' `Storage` backends. |
| `extra_info` | the result object's S3 response metadata (currently `{"ETag": ...}`). |

### The `src` / `dst` convention

The three `src_*` fields describe **one** object and the three `dst_*` fields
the **other**, so they always agree within a side:

- `src` (display) ↔ `src_info` (entry) ↔ `src_storage` (backend)
- `dest` (display) ↔ `dst_info` (entry) ↔ `dst_storage` (backend)

The **source side** is the object being acted on: the source of a transfer, or
the object a delete removes (aws models a delete as `src`=path, `dest`=None - so
a delete fills the `src_*` trio and leaves `dst_*` empty). The **destination
side** is where a transfer writes; `dst_info` is populated only by `sync` (the
pre-existing object the copy compared against) - `cp` / `mv` never list the
destination, so they carry `dest` / `dst_storage` but no `dst_info`.

So `src_storage` + `src_info.key` (or `dst_storage` + `dst_info.key`) re-reaches
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
| `dst_info` | — | update: the pre-existing dst / new: — | — | — |
| `src_storage` | source side | source side | the target bucket | run's source side |
| `dst_storage` | dest side | dest side | — | run's dest side |
| `extra_info` | copy / download: `{"ETag": …}` · upload: — | same | — | — |

A **stream** `cp` (one side is an `IOStorage`) lists nothing, so `src_info` /
`dst_info` are both `None` and the stream endpoint renders as `-`; the
`src_storage` / `dst_storage` are still the two sides.

## `extra_info` (result metadata)

`extra_info` is the affected object's S3 response metadata - currently the
ETag, as `{"ETag": "\"...\""}` (quoted, the raw S3 form):

- **copy** - the written object's ETag (the CopyObject response).
- **download** - the source object's ETag.
- **upload** - `None`: s3transfer discards the PutObject response, so the
  written object's ETag is not available here (docs/transfer.md). To capture an
  upload's ETag / VersionId, an opt-in HEAD or a botocore hook would be needed -
  not done by default.
- **delete** / **warning** - `None`.

The ETag comes from s3transfer's `future.meta.etag`; only what s3transfer
exposes is surfaced, so on an old s3transfer (or the CRT engine) it may be
`None` - a documented degradation, like the awscrt extra.

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
    etag = (r.extra_info or {}).get("ETag")  # copy / download; None on upload
    print("transferred", r.src_info.key if r.src_info else r.key, etag)
```

Re-reaching the result object (e.g. to HEAD it):

```python
storage, info = r.src_storage, r.src_info  # a delete's removed object
if storage is not None and info is not None:
    head = storage.get_client().head_object(Bucket=storage.bucket, Key=info.key)
```
