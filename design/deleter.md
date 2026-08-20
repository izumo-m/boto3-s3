# S3Deleter - asynchronous batch deletion

`S3Deleter` is a building block that batches the deletion of S3 objects. It is
the foundation of `S3.rm` and `S3.sync(delete_filter=True)`, and it can also be used
directly. It accumulates listing entries (`FileInfo`) in a buffer and, each time
the buffer fills (default 1000 = the `DeleteObjects` limit), hands one
`DeleteObjects` request to a background worker. A key containing a character
that XML 1.0 cannot represent is removed with an individual `DeleteObject`
instead (the route aws-cli uses); compatible keys in the same buffer remain one
batch. The caller can therefore keep iterating `S3Storage.scan()` while deletion
proceeds in the background (a pipeline of one in-flight buffer plus one buffer
under construction).

`dryrun=True` turns the deleter into a rehearsal for a direct consumer:
`submit` validates its entry as usual and then emits one DRYRUN record inline,
buffering nothing and sending nothing to S3. The orchestrators (`S3.rm`,
`S3.sync(delete_filter=...)`) do not use it - they keep their own dryrun
branches upstream (section 5).

## 1. API

```python
from boto3_s3 import S3Deleter, S3ScanOptions

with S3Deleter(storage, on_result=cb) as deleter:
    for info in storage.scan(S3ScanOptions(recursive=True)):
        deleter.submit(info)
```

Iterate scan in **recursive** mode (a non-recursive scan also yields DIRECTORY
(`CommonPrefixes`) entries, so submitting them as-is would "delete" prefixes
that are not objects).

| Argument / method | Description |
|---|---|
| `S3Deleter(storage, *, request_payer=None, on_result=None, cancel_token=None, batch_size=1000, operation="delete", capture_response=False, dryrun=False)` | From `storage` (an `S3Storage`; anything else raises `ValidationError`) it addresses requests with **only the client and bucket** (the key/prefix part is ignored for addressing; the `storage` itself is kept and rides each `OpResult.src_storage`). The client is resolved eagerly at construction time so that failures surface on the caller's thread. Keep `storage` (and the client it holds) open until the deleter's `close()` (do not call `storage.close()` first). `cancel_token` stops dispatching buffered batches; graceful mode drains the in-flight batch, while immediate mode also cancels it if it has not started (a running S3 request still finishes). `batch_size` is 1-1000 (out of range raises `ValidationError`); it bounds one worker dispatch and the XML-compatible subset's `DeleteObjects` call, while incompatible keys use `DeleteObject`. `operation` is the operation tag attached to exceptions (`rm` / `sync` put their own name here). `dryrun` makes the whole deleter a rehearsal: no worker is created, nothing is buffered or sent, and each `submit` reports its entry immediately (below). |
| `submit(info)` | Accumulates one listing entry (`FileInfo`) into the buffer; its `key` is the **full object key** to delete, and the rest of the entry (e.g. an `S3FileInfo.etag`) rides through to its `OpResult` untouched. Auto-flushes when `batch_size` is reached. An empty `info.key` raises `ValidationError` (rejected up front, because a single empty key would break the entire batch). If an auto-flush re-raises a worker exception from a previous batch, the entry has still been accumulated (do not re-submit it after catching). Duplicate keys within the same batch pass through (dedup is the caller's responsibility). |
| `flush()` | Splits the buffer into batches of `batch_size` and hands them to the worker (a complete no-op when empty). **Each dispatch first waits for the previous batch to complete** - this is the backpressure point, and the point where an unexpected worker exception is re-raised on the caller's thread (keys not yet dispatched remain intact in the buffer). After a re-raise it re-splits even if the buffer exceeds `batch_size`, so a single call never exceeds 1000 keys. |
| `close(*, flush=True)` | flush (with `flush=False` the remaining buffer is discarded) -> wait for in-flight -> stop the worker. Idempotent. Subsequent `submit` / `flush` raise `ValidationError`. A worker exception is re-raised here too, but it closes fully regardless. Keys left in the buffer by a re-raise or by `flush=False` are discarded **without an OpResult**. |
| context manager | `__exit__` is `close(flush=exc_type is None)` - on a body exception it discards the unsent buffer while still waiting for in-flight (the body exception is preserved via the `__context__` chain). |
| `succeeded` / `failed` / `first_error` | Aggregate counts and the first failure exception. Approximate while running; finalized after `close()`. A DRYRUN record is not a success, so a dryrun leaves all three at their initial values. |
| `dryrun=True` | `submit` runs the same up-front validation (closed check, empty-key `ValidationError`) and then emits **one `OpOutcome.DRYRUN` record inline on the calling thread**, with the same fields a real delete's record carries (`transfer_type=DELETE`, `compare_key` falling back to the key, `src`, `src_info`, `src_storage`; no `error`, no `extra_info`). Nothing is buffered, no request is issued, and no worker executor is created at all. A cancelled token suppresses the record, as a real `flush()` stops dispatching once cancelled. `flush()` is a no-op and `close()` only marks the deleter closed. |

`BatchError` is raised not by the deleter but by the caller (`S3.rm` and the
like). It is assembled from the counts, with `first_error` used as the sample
for `__cause__` ([`exceptions.md`](./exceptions.md) section 4 Model 1). The batch-limit
constant is `boto3_s3.deleter.S3_DELETE_BATCH`.

## 2. Concurrency model and the on_result contract

- There is a single worker (`ThreadPoolExecutor(max_workers=1)`, thread name
  `boto3-s3-deleter*`); its worker thread is spawned lazily on the first
  dispatch. `submit` / `flush` / `close` are contracted to be used from a single
  caller thread (single producer).
- `on_result` is called **from the worker thread**. One `OpResult` per
  dispatched key (`transfer_type=TransferType.DELETE`, `bytes_transferred=0`, in submit order
  within a batch; it is not emitted for keys in a discarded buffer). The callback
  must finish quickly and must not raise. If it does raise: records up to that
  point are counted, the rest of the same batch remain undelivered, and the
  exception is re-raised to the caller on the next **non-empty** `flush()` or on
  `close()`.
- A `dryrun=True` deleter has **no worker at all**: its DRYRUN records are
  emitted inline on the calling thread, which is the library-wide rule for
  non-submitting records ([`opresult.md`](./opresult.md)). An `on_result`
  exception there is the caller's own - it propagates straight out of
  `submit`, and is deliberately not turned into the worker path's deferred
  re-raise.
- The worker is non-daemon. If you fail to close it, interpreter shutdown blocks
  until the in-flight batch completes, so using the context manager is
  recommended.
- Cancellation never discards a *running* batch's results: a batch whose S3
  request has started completes and delivers its per-key results before
  shutdown returns. Unsent buffered entries are discarded without an
  `OpResult`, and immediate mode may also cancel a dispatched batch that has
  not started yet - its entries likewise produce no records.

## 3. Error model

For XML-compatible keys, success and failure are reconstructed from the
`Quiet=True` response: failures come from `Errors[]`, and successes are
synthesized as "the submitted keys minus the keys in `Errors[]`" (to reduce the
response payload) - unless an entry cannot be attributed to a submitted key at
all, which voids that synthesis for its batch (below). XML-incompatible keys
use `DeleteObject`, whose request success or translated exception directly
determines the per-key result. Results from both routes are emitted in original
submission order.

`capture_response=True` instead sends `Quiet=False`, so the response also lists
the successful `Deleted[]` entries; each is reconstructed into a per-key
`DeleteObject`-shaped slot (the entry minus its `Key`, plus the shared
`RequestCharged`) and attached to that key's `OpResult.extra_info["delete"]`.
The fallback route strips `ResponseMetadata` from its actual `DeleteObject`
response and uses the same slot, so the caller sees a single-object shape
regardless of the wire form (design/opresult.md). Failures are still read from
`Errors[]` as below. One limitation: when the same key was submitted more than
once in a batch, all of that key's `OpResult`s share a single slot (the
response's last entry for the key wins). `DeleteObjects` reports per key
spelling, so per-submission responses (e.g. two distinct delete markers on a
versioned bucket) cannot be mapped back to submission order.

- **per-key failure** (an `Errors[]` entry): the `Code` is translated into the
  taxonomy. The mapping table is **shared** with the request-level path
  (`s3storage.S3_CODE_CATEGORIES`; both paths produce the same classification).

  | Code | Exception |
  |---|---|
  | `AccessDenied` | `AccessDeniedError` |
  | `NoSuchBucket` / `NoSuchKey` / `NoSuchVersion` / `NotFound` | `NotFoundError` |
  | `InternalError` / `SlowDown` / `ServiceUnavailable` / `RequestTimeout` | `TransportError` |
  | other | `Boto3S3Error` |

  The message has the same shape as the str() of a botocore `ClientError`:
  `An error occurred ({Code}) when calling the DeleteObjects operation: {Message}`.
  The request-level path also uses the full `str(ClientError)`, so both paths
  read alike (the only difference is that the request-level path gains a retry
  suffix when retries are exhausted). It carries `operation` / `bucket` / `key`
  attributes.
- **an unattributable `Errors[]` entry** (a missing `Key`, or a spelling that
  does not match the submitted key): logs a WARNING (the trace stays) and
  **fails the rest of that batch closed**. Such an entry means the response no
  longer says which submitted keys really went away, so the synthesis above
  must not run: under `Quiet=True` it would report the very key the entry was
  about as a success, and a caller that treats a delete success as license to
  drop its own record of the object would discard the record of an object that
  may still exist. Every key of the batch that does not already carry an
  attributable error is therefore recorded as failed, with a plain
  `Boto3S3Error` carrying `operation` / `bucket` / `key` and the message
  `The DeleteObjects response carried an unattributable error entry
  (key=<key> <Code> (<Message>); ...), so this key's deletion cannot be
  confirmed` - one `key=... Code (Message)` detail per unattributable entry, so
  the CLI's `delete failed:` line says why. Keys with an attributable error keep
  it - it is the more informative one - and under `capture_response=True` a key
  listed in `Deleted[]` stays a success with its slot, since `Quiet=False` makes
  that entry positive per-key evidence. These failures count in `failed` and can
  be `first_error` like any other. Nothing here can fire against real S3, which
  answers only for the keys the request carried (and aws-cli never issues
  `DeleteObjects` at all, section 4), so this is a fail-closed defense rather
  than an observable behavior.
- **request-level failure** (the `delete_objects` call itself failing): records
  the `Boto3S3Error` raised by `s3storage.s3_errors` (which translates via
  `translate_boto_error`) as a failure for **every key** in that batch, and
  continues with subsequent batches
  (`NoSuchBucket` and the like fail across all batches alike and show up in the
  counts).
- **unexpected exceptions** (anything outside the boto family = a programming
  error): not turned into per-key results; passed straight through from the
  worker and re-raised to the caller on the next non-empty `flush()` or on
  `close()` (fails loudly).

## 4. aws-cli parity notes

- aws-cli uses only per-key `DeleteObject` and does not use the batch API
  (`DeleteObjects`). This implementation's batching is a wire-level deviation
  that is observationally equivalent for ordinary keys: deleting a nonexistent
  key is treated as "success" on both sides (`DeleteObject` returns 204 for a
  missing key, and `DeleteObjects` likewise reports no error), and per-key
  success and failure are preserved. The equivalence bounds the *success* path:
  when the producing listing/comparison dies mid-run (both sides exit nonzero),
  the partial S3 state differs - aws has already issued a per-key delete for
  everything enumerated, while the body exception here abandons the unsent
  buffer (up to `batch_size - 1` entries; `close(flush=False)`, consistent
  with the fatal-cancel contract in [`opresult.md`](./opresult.md)). A key containing XML 1.0-forbidden controls, surrogate code points,
  or `U+FFFE` / `U+FFFF` cannot be carried in a `DeleteObjects` body; it falls
  back to `DeleteObject`, preserving aws-cli behavior without sacrificing
  batching for the other keys.
- Failure messages are unified to the full `str(ClientError)`
  (`An error occurred (...) ...`), the same shape as the string aws-cli emits on
  a failure line (so the CLI layer can use it as-is when composing
  `delete failed: ...`). The shape is aws's; the bytes are not, and that is the
  visible edge of the batching above. A per-key line composed from a
  `DeleteObjects` `Errors[]` entry names the **plural** operation and carries no
  `(reached max retries: N)` suffix, where aws - issuing one `DeleteObject` per
  key - names the singular one and lets botocore append the suffix. The
  non-batched single-key delete keeps `DeleteObject` and matches aws byte for
  byte, so the difference is confined to the batched routes
  (`rm --recursive`, an S3-side `sync --delete`, `rb --force`). Recorded for
  the reader in [`aws-differences.md`](../docs/cli/aws-differences.md).
- User-facing output such as `delete: s3://...` / `delete failed: s3://... <error>` /
  `(dryrun) delete: ...` (the format of aws-cli's `results.py`) is the CLI layer's
  responsibility to assemble from `on_result`. The library does not print; it
  only emits the `boto3_s3.deleter` logger (debug: batch dispatch, request-level
  failures, per-key failures / warning: unattributable entries) - an intentional
  break from parity. The CLI's `--debug` picks up this logger.

## 5. Out of scope (outside this component)

Each of the following lives outside this component:

- **The sync engine and local-side deletion** - implemented, but in the sync
  orchestrator rather than in this deleter module: `_SyncDeletes` drives an
  `S3Deleter` for an S3 dest and a synchronous `os.remove` for a local dest
  (see [`sync.md`](./sync.md) section 2 / 5).
- **A `Deleter` ABC / `Storage.deleter()` factory** - considered but not
  adopted: sync uses `S3Deleter` directly, and the `Storage` ABC keeps deletion
  as the plain per-key `delete`.
- **The orchestrators' dryrun** - `S3.rm` and sync's `_SyncDeletes` keep their
  own dryrun branches and never build a `dryrun=True` deleter. Each already
  emits its DRYRUN record at a point the deleter cannot reach: `rm`'s blind
  single-key path builds its `FileInfo` without any deleter at all, and a
  local or custom (`s3open`) sync destination has no deleter either, so
  routing only the S3-destination case through this flag would leave two
  emission points instead of one. The CLI parity tests pin those records'
  ordering against real aws, so the flag is offered to direct consumers (who
  otherwise hand-roll the same record shape) and the orchestrators are left
  alone.
- **The `CancelToken` machinery** - shared infrastructure
  ([`exceptions.md`](./exceptions.md) section 3); how the deleter reacts to a
  token is described in sections 1 and 2 above.
- **Deletion by `VersionId`** - not provided.

The wiring of `S3.rm` / CLI `rm` and the single-object `S3Storage.delete(info)` (a
blind `DeleteObject` used by the single-shot path of a non-recursive rm) are
implemented - see [`cli.md`](./cli.md) section 5.2 / [`globsieve.md`](./globsieve.md).
