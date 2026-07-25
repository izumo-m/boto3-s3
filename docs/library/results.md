# Results, progress and cancellation

`cp` / `mv` / `rm` / `sync` act on many items, so they do not return a list.
Each item's outcome is handed to an `on_result` callback while the run proceeds,
and the call raises at the end if anything failed.

```python
from boto3_s3 import OpOutcome, TransferType

uploaded = []

def track(r):
    if r.transfer_type is TransferType.UPLOAD and r.outcome is OpOutcome.SUCCEEDED:
        uploaded.append(r.compare_key)

s3.sync("./site", "s3://my-bucket/site/", delete_filter=True, on_result=track)
```

## 1. When `on_result` fires

**Every item that reaches the operation produces exactly one terminal record** —
`SUCCEEDED`, `FAILED`, `SKIPPED`, `DRYRUN` or `CANCELLED`. An item that never
reaches it produces nothing: one excluded by a filter during enumeration, or one
never enumerated because the run ended first.

`WARNED` and `NOTICE` records sit **outside** that rule. They are advisories, not
tied to an item — a directory-walk warning belongs to no transfer at all, and a
notice can precede the same item's real outcome. So do not assume one callback
per key.

The callback may run **on a worker thread**, and several may run at once:
transfers report from `s3transfer`'s threads and batched deletes from the
deleter's, while dry runs, single-key `rm` and local deletes report inline on
your own thread. Keep it fast, keep it thread-safe, and do not let it raise.

Counts always agree with the records — the totals in the final error are the
per-outcome record counts, not a separate tally.

## 2. What a record carries

| field | what it holds |
| --- | --- |
| `transfer_type` | `upload` / `download` / `copy` / `move` / `delete` |
| `outcome` | `SUCCEEDED` / `FAILED` / `WARNED` / `SKIPPED` / `DRYRUN` / `NOTICE` / `CANCELLED` |
| `compare_key` | the item's identity relative to the operation's root, `/`-separated — the same key space filters match against |
| `bytes_transferred` | bytes moved on success; 0 for a delete, an advisory, and every non-success record |
| `error` | the failure on `FAILED`, the advisory text on `WARNED` / `NOTICE` |
| `src` / `dest` | display endpoints, for a `verb: src to dest` line |
| `src_info` / `dest_info` | the listing entries (`FileInfo`) |
| `src_storage` / `dest_storage` | the backends behind each side |
| `extra_info` | S3 response metadata (below) |

`bytes_transferred` is 0 on any non-success record even when bytes really
landed — an `mv` whose copy succeeded but whose source delete failed reports 0.

### Reading `src` and `dest`

The three `src_*` fields describe one object and the three `dest_*` fields the
other, and they always agree within a side. The **source** side is the object
being acted on — including the object a delete removes, which fills `src_*` and
leaves `dest_*` empty.

`dest_info` is populated only by `sync`, where it is the pre-existing object the
decision compared against. `cp` and `mv` never list the destination, so they
carry `dest` and `dest_storage` but no `dest_info`.

Because `src_storage` and `src_info.key` agree, they are enough to re-reach the
object:

```python
from boto3_s3 import S3Storage

storage, info = r.src_storage, r.src_info
if isinstance(storage, S3Storage) and info is not None:
    head = storage.get_client().head_object(Bucket=storage.bucket, Key=info.key)
```

Narrow with `isinstance` first: the backend is not always S3 — an upload's
source is local, and `sync --delete` onto a local destination deletes there. And
pick a side that still exists: a delete record's `src_info` names the object the
run just removed, so a HEAD on it can only 404.

A streaming `cp` lists nothing, so both `src_info` and `dest_info` are `None`
and the stream endpoint displays as `-`.

## 3. Response metadata

`extra_info` carries the affected object's S3 response metadata. By default that
is just the ETag, in S3's raw quoted form:

- **copy** and **download** — the source object's ETag.
- **upload** — `None`. `s3transfer` discards the write response, so the written
  object's ETag is not available by default.
- **delete** and advisories — `None`.

Pass **`capture_response=True`** to `cp` / `mv` / `rm` / `sync` to get the full
responses instead, on each **successful** record, keyed by role — `"write"` for
an upload or copy, `"read"` for a download, `"delete"` for a removal. An `mv`
between two S3 locations carries both `"write"` and `"delete"`. Failed and
cancelled records keep `extra_info=None`.

Two things to know before turning it on. It **forces the classic transfer
engine**, because the capture rides botocore's event stream and the CRT data
plane bypasses it. And it registers handlers on the transfer's client for the
duration, so do not run a capturing operation with a client that is being used
concurrently elsewhere.

## 4. Progress and dry runs

- **`on_progress(TransferProgress)`** reports byte-level progress for
  `cp` / `mv` / `sync`. `rm` moves no bytes and has none.
- **`on_entry(FileInfo)`** is how `ls` delivers entries — it is required, and
  entries arrive in listing order on your own thread. `ls` blocks until listing
  and cleanup finish.
- **`dryrun=True`** reports everything the run would have done, as `DRYRUN`
  records, without any mutating call. Enumeration still happens, so the listing
  cost is real.

## 5. Cancelling

Pass a `CancelToken` to `ls` / `cp` / `mv` / `rm` / `sync` and call `cancel()`
from anywhere, including from inside `on_result`:

```python
from boto3_s3 import CancelToken, CancelMode

token = CancelToken()
s3.sync("./site", "s3://bucket/site/", cancel_token=token)
...
token.cancel()                          # graceful: drain what was accepted
token.cancel(mode=CancelMode.IMMEDIATE) # also try to stop what is in flight
```

**Graceful** (the default) is a drain: the operation stops taking new work,
finishes what it already accepted, reclaims its workers, and raises
`CancelledError`. Because accepted items run to completion, no `CANCELLED`
records appear.

**Immediate** additionally asks pending and in-flight work to stop. Then the
records resolve like this:

- accepted but not yet running, or abandoned mid-flight — one `CANCELLED`
  record whose `error` names the cause;
- in flight and completing anyway — its **real** outcome. A running request
  cannot be safely interrupted, and if its bytes landed, that is the truth;
- never accepted — no record at all.

Cancellation is monotonic and idempotent: an immediate request upgrades a
graceful one, and nothing ever downgrades it.

A run that a cancellation actually cut short **always ends by raising** — the
triggering error, or `CancelledError`. It never ends with the partial-failure
error described in [`errors.md`](./errors.md), and `CANCELLED` records are
counted in neither.
