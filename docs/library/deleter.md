# `S3Deleter` — batch deletion

Delete many objects while you are still enumerating them. `S3Deleter` buffers
the keys you submit and hands each full batch to a background worker, so listing
and deleting overlap. `S3.rm` and `sync(delete_filter=True)` use it internally.

```python
from boto3_s3 import S3Deleter, S3ScanOptions

with S3Deleter(storage, on_result=cb) as deleter:
    for info in storage.scan(S3ScanOptions(recursive=True)):
        deleter.submit(info)
```

**Scan recursively.** A non-recursive scan also yields common-prefix entries,
which are not objects; submitting them would try to delete prefixes.

## 1. The API

```python
S3Deleter(storage, *, request_payer=None, on_result=None, cancel_token=None,
          batch_size=1000, operation="delete", capture_response=False,
          dryrun=False)
```

`storage` must be an `S3Storage`; anything else raises `ValidationError`. Only
its client and bucket are used for addressing — the key part is ignored — but
the object itself rides along on every result. Keep it open until the deleter is
closed. The client is built during construction, so a client that cannot be
built — an unknown profile, say — fails on your thread rather than in the
worker. Credentials are resolved by the first request, so missing credentials
arrive as per-key failures (`ConfigurationError`).

`batch_size` must be between 1 and 1000, S3's own limit for one batch request.

Deleting a specific `VersionId` is not provided, as `aws s3 rm` does not offer
it either.

| member | what it does |
| --- | --- |
| `submit(info)` | Buffers one entry; `info.key` is the full object key. Flushes automatically when the buffer reaches `batch_size`. An empty key raises `ValidationError`. Duplicate keys pass through — de-duplicating is yours to do. |
| `flush()` | Sends what is buffered. Each dispatch first waits for the previous batch, which is where back-pressure happens and where a worker error surfaces. |
| `close(*, flush=True)` | Flush, wait for the in-flight batch, stop the worker. Idempotent. Later `submit` / `flush` raise. With `flush=False`, buffered keys are discarded. |
| `succeeded` / `failed` / `first_error` | Counts and the first failure. Approximate while running, final after `close()`. |

Used as a context manager, exiting normally flushes; exiting because of an
exception discards the unsent buffer while still waiting for what is already in
flight.

**Close it.** Interpreter shutdown waits for the worker thread whichever
thread started it (a daemon thread included), so if you neither close the
deleter nor use the context manager, shutdown blocks until the in-flight batch
finishes.

### Rehearsing with `dryrun`

`dryrun=True` makes the deleter report what it would delete and delete nothing.
`submit` validates the entry as usual, then hands `on_result` one record with
outcome `DRYRUN` — the same fields a real deletion's record carries — and
returns. Nothing is buffered, no request is sent, and no worker thread is
created, so the counters stay at zero and `flush()` and `close()` have nothing
to do:

```python
from boto3_s3 import S3Deleter, S3ScanOptions

with S3Deleter(storage, on_result=cb, dryrun=rehearse) as deleter:
    for info in storage.scan(S3ScanOptions(recursive=True)):
        deleter.submit(info)   # rehearsing: cb sees DRYRUN, S3 sees nothing
```

In your own code the flag is the only difference between rehearsing and
deleting, so a `--dryrun`-style option costs one argument rather than a second
code path. At run time a rehearsal behaves differently in two ways: the record
is emitted on **your** thread, so an exception from `on_result` comes straight
back out of `submit`, and a cancelled `cancel_token` makes `submit` return
without a record.

`S3.rm(dryrun=True)` and `sync(dryrun=True, delete_filter=...)` do their own
rehearsing; this flag is for code driving the deleter directly.

## 2. Results

`on_result` receives one `OpResult` per dispatched key, with `transfer_type`
`delete` and `bytes_transferred` 0, in submission order within a batch. Keys
discarded without being sent produce no record.

**It is called from the worker thread.** Keep it fast and do not let it raise.
If it does raise, the records already delivered are counted, the rest of that
batch are not delivered, and the exception is re-raised to you on the next
non-empty `flush()` or on `close()`. (A `dryrun` deleter has no worker and
calls it on your own thread — see [Rehearsing with
`dryrun`](#rehearsing-with-dryrun) above.)

`submit` / `flush` / `close` are meant to be called from **one** thread.

Cancelling through `cancel_token` never discards a batch whose request has
already started — it completes and delivers its results. Buffered entries not
yet sent are dropped without records, and immediate mode may also cancel a
dispatched batch that has not begun.

Nothing is printed. Beyond the result records, the deleter logs to the
`boto3_s3.deleter` logger — batch dispatches and failures at debug level,
unattributable responses as warnings.

## 3. Failures

A per-key failure carries the taxonomy exception matching S3's error code:
`AccessDenied` becomes `AccessDeniedError`, the not-found family becomes
`NotFoundError`, and anything else the base `Boto3S3Error`. The message reads
like botocore's, so it can be printed as-is.

A key the batch response reports with a passing fault — `InternalError`,
`SlowDown`, `ServiceUnavailable`, `RequestTimeout` — is not failed on the spot:
it is sent again on its own, as a `DeleteObject`, so the client's retry policy
gets to work on it the way it does for every key `aws` deletes. Only if that
request still fails is the key recorded as failed, with that request's error.
A batch's re-sends go out up to ten at a time. Two things switch them off: a
client configured not to retry at all (a single attempt per request), and
abandoning the run — `close(flush=False)`, or an immediate-mode cancel — after
which the keys not yet re-sent are recorded with the batch's own error.

If the batch request itself fails, **every key in that batch** is recorded as
failed and the deleter continues with the following batches. So a wrong bucket
name fails everything, and the counts show it.

Success on the batch route is normally read as "not in the response's error
list". If the response carries an error the deleter cannot pin on any key it
sent — an entry with no key, or one naming a key it never submitted — that
reading is no longer safe, so **every key of that batch that the response did
not account for is recorded as failed**, with a message saying the deletion
could not be confirmed and quoting the offending entry. A key with an error of
its own keeps that error, and with `capture_response=True` a key the response
lists as deleted stays a success. Failing closed matters if a delete success
licenses you to drop your own record of the object: the object may still be
there. S3 answers only for the keys you sent, so in practice this never fires;
it is a guard, and it logs a warning as well.

An exception the delete request itself raises that is not a botocore error —
botocore choking on a malformed response, a redirect loop — fails the keys of
that request all the same, as a plain `Boto3S3Error` carrying the original as
`__cause__`. Anything outside that — a genuine programming error, or an
`on_result` callback that raises — is not turned into per-key results. It is
re-raised to you on the next non-empty `flush()` or `close()`.

`S3Deleter` never raises `BatchError`. If you want one, build it from
`succeeded` / `failed` after `close()`.

With `capture_response=True`, a successful key's `OpResult.extra_info` gains
a `"delete"` slot holding a single-object-shaped response, regardless of which
wire form was used. A success the batch response does not list under
`Deleted[]` — an endpoint that leaves a key out, or answers quietly although
asked not to — is still a success, with `extra_info` left at `None`, so read
the slot defensively. One more limitation: if the same key was submitted twice
in one batch, both records share a single slot.

## 4. Differences from `aws s3`

`aws s3` deletes one key per request. This batches them, and for ordinary keys
you cannot tell the difference: deleting a key that does not exist succeeds
either way, and per-key success and failure are preserved.

Two consequences of batching:

- **A run that dies mid-way leaves different state.** `aws` has already issued
  a delete for everything it enumerated; here the unsent buffer — up to
  `batch_size - 1` entries — is abandoned.
- **Keys XML cannot carry verbatim** (C0 control characters other than TAB
  and LF, surrogates, `U+FFFE` / `U+FFFF`) fall back to individual requests,
  which is what `aws` does for every key. The rest of the buffer stays batched.
  The individual requests of a batch go out up to ten at a time, and once the
  run is abandoned no further one is started.

