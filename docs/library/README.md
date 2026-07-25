# The `boto3-s3` library

Run every `aws s3` operation from Python, in your own process, with your own
boto3 clients. No `subprocess`, no `aws` on `PATH`, no console output to parse —
results come back as objects.

```bash
pip install boto3-s3
```

```python
import boto3_s3
from boto3_s3 import S3

s3 = S3(session=boto3_s3.session())

s3.sync("./site", "s3://my-bucket/site/", delete_filter=True)
s3.cp("./report.csv", "s3://my-bucket/report.csv")
s3.rm("s3://my-bucket/tmp/", recursive=True)
```

## 1. The shape of the API

Everything hangs off one object. `S3` has a method per `aws s3` subcommand —
`cp` / `ls` / `mb` / `mv` / `presign` / `rb` / `rm` / `sync` / `website` — taking
ordinary keyword arguments. It holds no connection and needs no cleanup, so a
single instance can be created once and kept.

Operations that act on many items do not return a list. They stream a record per
item to an `on_result` callback as the run proceeds, and raise at the end if
anything failed. That is the one structural difference from most Python APIs,
and [`results.md`](./results.md) covers it.

The library is deliberately **more permissive than `aws s3`**. It accepts input
the command would reject — `"bucket/key"` without the `s3://` prefix, for
instance. The strict validation and the exit codes belong to the
[`boto3-s3-cli`](../cli/README.md) command that sits on top of it. If you need
`aws s3`'s exact rejections, use the command; if you are writing an application,
the library's leniency is usually what you want.

## 2. Where to look

The pages build on each other in this order; the first three cover everything
every operation shares.

- [`s3-object.md`](./s3-object.md) — creating the `S3` object, choosing which
  client each location uses, running operations across threads, and extending it
  by subclassing.
- [`results.md`](./results.md) — `on_result`, the `OpResult` record, progress,
  dry runs, and cancellation.
- [`errors.md`](./errors.md) — the exception hierarchy, what is safe to depend
  on, and how a batch reports partial failure.
- [`sync.md`](./sync.md) — the three decisions `sync` makes, and the argument
  that controls each one.
- [`sync-content.md`](./sync-content.md) — deciding by content instead of size
  and mtime.
- [`filters.md`](./filters.md) — `filter=`, glob patterns, and which key they
  match against.
- [`transfer-options.md`](./transfer-options.md) — the `cp` / `mv` / `sync`
  options and multipart tuning.
- [`streams.md`](./streams.md) — `IOStorage` and `StdioStorage`.
- [`deleter.md`](./deleter.md) — driving batch deletion yourself with
  `S3Deleter`.
- [`custom-storage.md`](./custom-storage.md) — using a `Storage` as one side of
  a transfer.
- [`logging.md`](./logging.md) — debug output and credential masking.
- [`compatibility.md`](../compatibility.md) — which `boto3` release each feature
  needs.

The [README](../../README.md) introduces the project as a whole.
