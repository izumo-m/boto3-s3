# S3Deleter - asynchronous batch deletion

`S3Deleter` is a building block that batches the deletion of S3 objects. It is
the foundation of `S3.rm` and `S3.sync(delete=True)`, and it can also be used
directly. It accumulates full keys in a buffer and, each time the buffer fills
(default 1000 = the `DeleteObjects` limit), hands one `DeleteObjects` request to
a background worker, so the caller can keep iterating `S3Storage.scan()` while
deletion proceeds in the background (a pipeline of one in-flight batch plus one
buffer under construction).

dryrun is the responsibility of the orchestrator layer (`S3.rm` and friends); it
never reaches the deleter.

## 1. API

```python
from boto3_s3 import S3Deleter, ScanOptions

with S3Deleter(storage, on_result=cb) as deleter:
    for info in storage.scan(ScanOptions(recursive=True)):
        deleter.submit(info.key)
```

Iterate scan in **recursive** mode (a non-recursive scan also yields DIRECTORY
(`CommonPrefixes`) entries, so submitting them as-is would "delete" prefixes
that are not objects).

| Argument / method | Description |
|---|---|
| `S3Deleter(storage, *, request_payer=None, on_result=None, batch_size=1000, operation="delete")` | From `storage` (an `S3Storage`; anything else raises `ValidationError`) it uses **only the client and bucket** (it ignores the key/prefix part). The client is resolved eagerly at construction time so that failures surface on the caller's thread. Keep `storage` (and the client it holds) open until the deleter's `close()` (do not call `storage.close()` first). `batch_size` is 1-1000 (`ValueError`). `operation` is the operation tag attached to exceptions (`rm` / `sync` put their own name here). |
| `submit(key)` | Accumulates one **full object key** into the buffer. Auto-flushes when `batch_size` is reached. An empty key raises `ValidationError` (rejected up front, because a single empty key would break the entire batch). If an auto-flush re-raises a worker exception from a previous batch, the key has still been accumulated (do not re-submit it after catching). Duplicate keys within the same batch pass through (dedup is the caller's responsibility). |
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
  dispatched key (`kind=OpKind.DELETE`, `bytes_transferred=0`, in submit order
  within a batch; it is not emitted for keys in a discarded buffer). The callback
  must finish quickly and must not raise. If it does raise: records up to that
  point are counted, the rest of the same batch remain undelivered, and the
  exception is re-raised to the caller on the next **non-empty** `flush()` or on
  `close()`.
- The worker is non-daemon. If you fail to close it, interpreter shutdown blocks
  until the in-flight batch completes, so using the context manager is
  recommended.

## 3. Error model

Success and failure are reconstructed from the `Quiet=True` response: failures
come from `Errors[]`, and successes are synthesized as "the submitted keys minus
the keys in `Errors[]`" (to reduce the response payload).

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
  does not match the submitted key - e.g., newline normalization by the XML
  parser): logs a WARNING and skips it. Owing to how the `Quiet=True` synthesis
  works, the key in question may be recorded as a success (a known limitation of
  the synthesis; the policy is not to silently flip it to success).
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
  (`DeleteObjects`). This implementation's batching is an **accepted wire-level
  deviation** that is observationally equivalent for ordinary keys: deleting a
  nonexistent key is treated as "success" on both sides (just like the 204 of
  `DeleteObject`, `DeleteObjects` returns no error either), and per-key success
  and failure are preserved. The known difference is a key that cannot be carried
  in the XML body of `DeleteObjects` (control characters and the like): aws-cli's
  per-key path can delete it, but in this implementation that batch fails at the
  request level.
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

The sync engine and deletion on the local side (both implemented, but in the
sync orchestrator rather than in this deleter module - `_SyncDeletes` drives an
`S3Deleter` for an S3 dest and a synchronous `os.remove` for a local dest; see
[`sync.md`](./sync.md) section 2 / 5), a `Deleter` ABC / `Storage.deleter()`
factory (considered, but not adopted: sync uses `S3Deleter` directly, and the
`Storage` ABC keeps deletion as the plain per-key `delete`), dryrun (the responsibility
of the orchestrator layer - `S3.rm` handles it before reaching the deleter),
the cancel token, and deletion by `VersionId`.

The wiring of `S3.rm` / CLI `rm` and the per-key `S3Storage.delete(key)` (a
blind `DeleteObject` used by the single-shot path of a non-recursive rm) are
implemented - see [`cli.md`](./cli.md) section 5.2 / [`globsieve.md`](./globsieve.md).
