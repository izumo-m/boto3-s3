# Errors

Every failure the public API reports is a `Boto3S3Error` or one of its
subclasses. Success means no exception; there are no return codes and no
error-report objects to inspect.

```python
from boto3_s3 import Boto3S3Error

try:
    s3.sync("./site", "s3://my-bucket/site/")
except Boto3S3Error as exc:
    ...
```

Catching the root is a genuine catch-all: nothing raises it directly for a known
failure, so every real error is one of the categories below.

## 1. The hierarchy

```
Boto3S3Error                 the root; catch this to catch everything
├── AccessDeniedError        S3 403, or a local PermissionError
├── NotFoundError            S3 404, or a local FileNotFoundError
├── ValidationError          an invalid argument, precondition or state
│   └── InvalidValueError
├── TransportError           network or local I/O failure
├── ConfigurationError       credentials or region missing or unusable
│   └── InvalidConfigError
├── CancelledError           cancelled through a CancelToken
└── BatchError               a batch run's failure rollup
```

The classification cuts across S3 and the local filesystem on purpose: an S3 403
and a local `PermissionError` are the same category, because to your application
they usually mean the same thing.

Catch the parent — `ValidationError` also catches `InvalidValueError`, and
`ConfigurationError` also catches `InvalidConfigError`.

`CancelledError` here is the library's own, unrelated to the identically named
exceptions in `asyncio` and `concurrent.futures`.

Programming mistakes are not wrapped. A `TypeError` from your own callback, a
`KeyboardInterrupt`, a `SystemExit` — all pass straight through.

## 2. What you may depend on

Two things are stable across releases: **the class** and the **structured
attributes**.

Every `Boto3S3Error` carries `operation`, `bucket` and `key`. `BatchError` adds
its counters. Message strings are **display only** — they track `aws s3`'s
wording and change whenever parity requires it, so branch on the class and read
the attributes, and never parse `str(exc)`.

The context attributes are best-effort. `operation=None` is a legitimate value
rather than a gap: it means no single subcommand was in scope, which is the case
during client construction and in the shared listing path that every recursive
scan rides. And `key` names the failing entry in whichever address space it came
from, so a locally-originating error puts a filesystem path there, with no
`bucket`.

### Reaching the original exception

The backend exception is preserved, never swallowed. An error raised for a
failed S3 request carries the botocore `ClientError` on `__cause__`.

`BatchError` is two levels deep. Its `__cause__` is the **translated** first
failure — a `Boto3S3Error`, not the raw backend error — so when the server was
reached, the original `ClientError` sits at `__cause__.__cause__`. That is a
guarantee, with one exception: a per-key failure from a batched delete is
synthesized from the response body and has no exception behind it, so its
`__cause__` is `None` and only the message carries the code.

## 3. Where each error comes from

| Origin | Category |
| --- | --- |
| S3 403 / `AccessDenied`; a local `PermissionError` | `AccessDeniedError` |
| S3 404 / `NoSuchKey` / `NoSuchBucket`; a local `FileNotFoundError` or missing source | `NotFoundError` |
| S3 5xx or throttling (`InternalError`, `SlowDown`, `ServiceUnavailable`); a connection failure, a timeout, a local I/O `OSError` | `TransportError` |
| `NoCredentialsError` / `NoRegionError`; an SDK too old for a requested feature | `ConfigurationError` |
| an unusable profile, partial credentials, a config value that will not convert | `InvalidConfigError` |
| an invalid argument or a violated precondition | `ValidationError` |
| `CancelToken.cancel()` | `CancelledError` |

An S3 error code not in the table falls back to its HTTP status: 403 and 404 as
above, 5xx to `TransportError`, other 4xx to `ValidationError`.

## 4. When a batch partly fails

`cp -r` / `mv -r` / `rm -r` / `sync` **attempt every item**. If any of them
failed, the call raises `BatchError` once, at the end.

```python
from boto3_s3 import BatchError

try:
    s3.sync("./site", "s3://my-bucket/site/", on_result=track)
except BatchError as exc:
    print(f"{exc.failed} of {exc.total} failed, {exc.succeeded} succeeded")
```

It carries **counts only** — `succeeded`, `failed`, `warned`, `skipped`, and
`total` as their sum — never a list, so a million failures still cost nothing to
report. The per-item detail is the `on_result` stream described in
[`results.md`](./results.md); if you need it, collect it there.

Some details worth knowing:

- It is raised **only when `failed > 0`**. Warnings and skips alone do not
  raise; read their counts from `on_result`.
- `BatchError` is itself a `Boto3S3Error`, so a broad `except Boto3S3Error`
  already covers it.
- The batch shape applies **regardless of item count** — a single failing item
  still ends in `BatchError`, reported as 1 of 1.
- Cancelled items are never counted as failures, and a cancelled run raises
  something else entirely (see [`results.md`](./results.md)).
- `skipped` is informational. It counts skips the operation made — `sync`
  finding nothing changed, `no_overwrite` declining to replace — but not items
  dropped during enumeration by a filter, which never reach that layer.

Not every operation aggregates. `mb` / `rb` / `website` / `presign` act on one
thing and raise the category exception directly. So does a failure that happens
**before** item processing starts, such as a listing that fails outright or a
`cp` whose source does not exist — those propagate as `NotFoundError` and the
like, not as `BatchError`.
