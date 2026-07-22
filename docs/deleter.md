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

dryrun is the responsibility of the orchestrator layer (`S3.rm` and friends); it
never reaches the deleter.

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
| `S3Deleter(storage, *, request_payer=None, on_result=None, cancel_token=None, batch_size=1000, operation="delete", capture_response=False)` | From `storage` (an `S3Storage`; anything else raises `ValidationError`) it addresses requests with **only the client and bucket** (the key/prefix part is ignored for addressing; the `storage` itself is kept and rides each `OpResult.src_storage`). The client is resolved eagerly at construction time so that failures surface on the caller's thread. Keep `storage` (and the client it holds) open until the deleter's `close()` (do not call `storage.close()` first). `cancel_token` stops dispatching buffered batches; graceful mode drains the in-flight batch, while immediate mode also cancels it if it has not started (a running S3 request still finishes). `batch_size` is 1-1000 (out of range raises `ValidationError`); it bounds one worker dispatch and the XML-compatible subset's `DeleteObjects` call, while incompatible keys use `DeleteObject`. `operation` is the operation tag attached to exceptions (`rm` / `sync` put their own name here). |
| `submit(info)` | Accumulates one listing entry (`FileInfo`) into the buffer; its `key` is the **full object key** to delete, and the rest of the entry (e.g. an `S3FileInfo.etag`) rides through to its `OpResult` untouched. Auto-flushes when `batch_size` is reached. An empty `info.key` raises `ValidationError` (rejected up front, because a single empty key would break the entire batch). If an auto-flush re-raises a worker exception from a previous batch, the entry has still been accumulated (do not re-submit it after catching). Duplicate keys within the same batch pass through (dedup is the caller's responsibility). |
| `flush()` | Splits the buffer into batches of `batch_size` and hands them to the worker (a complete no-op when empty). **Each dispatch first waits for the previous batch to complete** - this is the backpressure point, and the point where an unexpected worker exception is re-raised on the caller's thread (keys not yet dispatched remain intact in the buffer). After a re-raise it re-splits even if the buffer exceeds `batch_size`, so a single call never exceeds 1000 keys. |
| `close(*, flush=True)` | flush (with `flush=False` the remaining buffer is discarded) -> wait for in-flight -> stop the worker. Idempotent. Subsequent `submit` / `flush` raise `ValidationError`. A worker exception is re-raised here too, but it closes fully regardless. Keys left in the buffer by a re-raise or by `flush=False` are discarded **without an OpResult**. |
| context manager | `__exit__` is `close(flush=exc_type is None)` - on a body exception it discards the unsent buffer while still waiting for in-flight (the body exception is preserved via the `__context__` chain). |
| `succeeded` / `failed` / `first_error` | Aggregate counts and the first failure exception. Approximate while running; finalized after `close()`. |

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
response payload). XML-incompatible keys use `DeleteObject`, whose request
success or translated exception directly determines the per-key result. Results
from both routes are emitted in original submission order.

`capture_response=True` instead sends `Quiet=False`, so the response also lists
the successful `Deleted[]` entries; each is reconstructed into a per-key
`DeleteObject`-shaped slot (the entry minus its `Key`, plus the shared
`RequestCharged`) and attached to that key's `OpResult.extra_info["delete"]`.
The fallback route strips `ResponseMetadata` from its actual `DeleteObject`
response and uses the same slot, so the caller sees a single-object shape
regardless of the wire form (docs/opresult.md). Failures are still read from
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
  does not match the submitted key): logs a WARNING and skips it. Owing to how
  the `Quiet=True` synthesis works, the key in question may still be recorded
  as a success - a known limitation of the synthesis; the WARNING exists so
  that flip at least leaves a trace.
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
  `delete failed: ...`).
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
- **dryrun** - the orchestrator layer's responsibility; `S3.rm` handles it
  before anything reaches the deleter.
- **The `CancelToken` machinery** - shared infrastructure
  ([`exceptions.md`](./exceptions.md) section 3); how the deleter reacts to a
  token is described in sections 1 and 2 above.
- **Deletion by `VersionId`** - not provided.

The wiring of `S3.rm` / CLI `rm` and the single-object `S3Storage.delete(info)` (a
blind `DeleteObject` used by the single-shot path of a non-recursive rm) are
implemented - see [`cli.md`](./cli.md) section 5.2 / [`globsieve.md`](./globsieve.md).
