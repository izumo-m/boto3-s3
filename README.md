# boto3-s3

[![PyPI](https://img.shields.io/pypi/v/boto3-s3)](https://pypi.org/project/boto3-s3/)
[![Python versions](https://img.shields.io/pypi/pyversions/boto3-s3)](https://pypi.org/project/boto3-s3/)
[![License](https://img.shields.io/pypi/l/boto3-s3)](https://github.com/izumo-m/boto3-s3/blob/main/LICENSE)
[![CI](https://github.com/izumo-m/boto3-s3/actions/workflows/ci.yml/badge.svg)](https://github.com/izumo-m/boto3-s3/actions/workflows/ci.yml)

A Python library for running every `aws s3` operation in-process, with an
`aws s3 sync`-compatible synchronization pipeline at its core. Applications
that currently shell out to `aws s3 sync` can make the same default
size/timestamp decisions, transfers, filters, and deletions through `S3.sync`
without starting a CLI process. Every subcommand — `cp` / `ls` / `mb` / `mv` /
`presign` / `rb` / `rm` / `sync` / `website` — has a corresponding Python API.

The library provides the building blocks for `aws s3` compatibility and is
occasionally more permissive for Python callers. The companion CLI is the layer
that applies the command's strict argument validation and exit-code behavior.

Each command is a method on a single `S3` object, taking ordinary keyword
arguments; bring a boto3 client when you need a specific profile, region, or
endpoint.

**Status:** beta (pre-1.0), preparing for 1.0 — all subcommands are implemented;
the public API may still change.
**Python:** 3.10+ · **License:** Apache-2.0

Two packages:

- **`boto3-s3`** — the library. Run `aws s3`-equivalent operations from Python
  with your own boto3 clients and credentials.
- **[`boto3-s3-cli`](https://pypi.org/project/boto3-s3-cli/)** — the `boto3-s3`
  command, a drop-in for `aws s3`.

## Why

Much of `aws s3` is easy to do straight from boto3 — a one-off `cp` or `rm` is a
few lines, and recursive copies or multipart via `s3transfer` take only a bit
more effort. What's genuinely hard is preserving the command's path and naming
rules, recursive include/exclude semantics, and especially the decisions made
by `aws s3 sync`. Reimplementing those rules in each application invites subtle
drift; shelling out keeps the rules but leaves the application parsing process
output. boto3-s3 brings those command semantics into a direct Python API.

- **An in-process replacement for `aws s3 sync`.** Mirror local trees and buckets
  in any direction — upload, download, or S3-to-S3 — with the same default
  size/timestamp comparison, include/exclude behavior, deletion, and dry-run.
- **Every `aws s3` command.** `cp` / `ls` / `mb` / `mv` / `presign` / `rb` /
  `rm` / `website` complete the set.
- **A library, not a CLI wrapper.** Runs in-process: no `subprocess`, no scraping
  stdout, no `aws` on `PATH`. You pass boto3 clients directly and get structured,
  per-item results back — not text to parse.
- **Small, pure-Python packaging.** boto3-s3 can reuse a compatible existing
  boto3 / botocore / s3transfer installation, including a Lambda runtime SDK
  when it satisfies the supported version floor.
- **The established AWS transfer engines.** Byte transfers use `s3transfer` or
  the optional CRT engine, retaining their multipart and concurrency machinery
  instead of introducing another byte-transfer implementation.
- **Familiar behavior.** Path rules, options, and default sync decisions follow
  `aws s3`; the CLI additionally owns strict validation and exit-code parity.

## Install

```bash
pip install boto3-s3          # the library
pip install boto3-s3-cli      # the `boto3-s3` command (also installs boto3-s3)
```

Optional extra — the AWS Common Runtime (CRT) transfer engine and CRT-family
checksums:

```bash
pip install "boto3-s3[crt]"
```

## Quick start

Create the `S3` object once — it holds no connection of its own and needs no
cleanup — then call what you need. Its own state is safe to share, but parallel
operations need separate, prebuilt clients — see
[running operations across threads](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/s3-object.md#3-running-operations-across-threads).

```python
import boto3_s3
from boto3_s3 import S3

# boto3_s3.session(): a boto3 Session whose clients parse listing timestamps
# at C speed. A bare S3() works too, with plain boto3.client("s3") semantics.
s3 = S3(session=boto3_s3.session())

# Sync a directory tree up to S3, removing remote extras (mirror).
s3.sync("./site", "s3://my-bucket/site/", delete_filter=True)

# Copy a single object up or down.
s3.cp("./report.csv", "s3://my-bucket/report.csv")
s3.cp("s3://my-bucket/report.csv", "./report.csv")

# List objects; each result is a FileInfo (key, size, …).
s3.ls(
    "s3://my-bucket/site/",
    recursive=True,
    on_entry=lambda info: print(info.key, info.size),
)

# Delete everything under a prefix.
s3.rm("s3://my-bucket/tmp/", recursive=True)

# A presigned URL (no request is sent).
url = s3.presign("s3://my-bucket/report.csv", expires_in=900)
```

For `cp` / `mv` / `sync` the **direction is inferred from the two endpoints**:
local-to-S3 is an upload, S3-to-local a download, S3-to-S3 a copy. A
local-to-local pair is rejected, like `aws s3`.

## Sync

`sync` is the heart of the library — an in-process replacement for
`aws s3 sync`, in every direction, making the same default decisions.

```python
s3.sync("./site", "s3://my-bucket/site/")        # upload
s3.sync("s3://my-bucket/site/", "./site")        # download
s3.sync("s3://src/data/", "s3://dest/data/")     # S3-to-S3

s3.sync("./site", "s3://my-bucket/site/", delete_filter=True)   # aws's --delete
```

It asks one question per entry, and each has its own argument: create an entry
that is new (`create_filter`), overwrite one present on both sides
(`update_filter`), delete one the source no longer has (`delete_filter`). The
defaults are exactly `aws s3 sync`, and `create_filter` is a knob aws does not
expose.

Unlike `aws s3 sync`, updates can be decided by **content** rather than size and
timestamp — either against S3's ETag or against the checksum S3 already stores:

```python
from boto3_s3.checksumcompare import ChecksumComparison

s3.sync(src, dest, update_filter=ChecksumComparison(s3, src, dest))
```

Because it runs in-process, results come back as objects rather than console
output to parse:

```python
from boto3_s3 import OpOutcome, TransferType

uploaded = []

def track(r):
    if r.transfer_type is TransferType.UPLOAD and r.outcome is OpOutcome.SUCCEEDED:
        uploaded.append(r.compare_key)

s3.sync("./site", "s3://my-bucket/site/", delete_filter=True, on_result=track)
print(f"{len(uploaded)} files uploaded")
```

See the [sync guide](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/sync.md)
for the three decisions and the default rule's one asymmetry, and
[deciding by content](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/sync-content.md)
for choosing between the two strategies and running their decisions in
parallel.

## Operations

`S3` is the entry point: create one with `s3 = S3()`, then call the methods
below — each mirrors an `aws s3` subcommand.

| Method | What it does |
| --- | --- |
| `ls(target, *, on_entry, recursive, …)` | List objects and common prefixes — or, at the bare service root, every bucket. Delivers ordered `FileInfo` entries to `on_entry`. |
| `cp(src, dest, *, recursive, filter, dryrun, …)` | Copy bytes: upload, download, or S3-to-S3. Either side may be a stream. |
| `mv(src, dest, *, recursive, …)` | `cp`, then delete each source once its copy succeeds. |
| `sync(src, dest, *, filter, create_filter, update_filter, delete_filter, …)` | Recursively synchronize `src` into `dest`. |
| `rm(target, *, recursive, filter, dryrun, …)` | Delete objects: a single key, a recursive prefix, or the folder-marker sweep. |
| `mb(target, *, tags)` | Create the bucket of `target`. |
| `rb(target)` | Delete the (empty) bucket of `target`. |
| `presign(target, *, expires_in=3600, method="get_object")` | Return a presigned URL. No request is sent. |
| `website(target, *, index_document, error_document)` | Set the bucket website configuration. |

Each takes the `aws s3` transfer options as snake_case keyword arguments. The
direction of `cp` / `mv` / `sync` is inferred from the two endpoints.

## Configuring the client

A bare `"s3://..."` string uses the client the `S3` instance builds from its own
defaults. Give the object a session for a specific profile, region or endpoint,
and every bare string inherits it:

```python
import boto3_s3
from boto3_s3 import S3

s3 = S3(session=boto3_s3.session(profile_name="prod", region_name="eu-west-1"))
s3.cp("./artifact.tar.gz", "s3://prod-bucket/artifacts/")
```

`boto3_s3.session(**kwargs)` is a drop-in `boto3.Session` whose clients parse S3
response timestamps at C speed — severalfold faster on a large `ls` / `sync` /
`rm`, with no aws-cli equivalent. A plain `boto3.Session` works identically
apart from that.

When one operation needs **two** clients — a cross-account S3-to-S3 copy — wrap
each URL in an `S3Storage` carrying its own client. The same object configures
how a side is read (`page_size`, `follow_symlinks`, …). An S3-compatible
endpoint such as MinIO is just a differently-built client.

## Documentation

The [user guide](https://github.com/izumo-m/boto3-s3/blob/main/docs/README.md) covers both packages.

| | |
| --- | --- |
| [The `S3` object](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/s3-object.md) | creating it, choosing clients, threads, subclassing |
| [Results](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/results.md) | `on_result`, progress, dry runs, cancellation |
| [Errors](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/errors.md) | the exception hierarchy and partial failure |
| [Sync](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/sync.md) · [by content](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/sync-content.md) | the three decisions, and content comparison |
| [Filtering](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/filters.md) | `filter=`, glob patterns, which key they match |
| [Transfer options](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/transfer-options.md) | the `cp` / `mv` / `sync` options and multipart tuning |
| [Streams](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/streams.md) | `IOStorage` / `StdioStorage` |
| [`S3Deleter`](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/deleter.md) | driving batch deletion yourself |
| [Custom backends](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/custom-storage.md) | a `Storage` as one side of a transfer |
| [Logging](https://github.com/izumo-m/boto3-s3/blob/main/docs/library/logging.md) | debug output and credential masking |

Debug logs are masked by default: `set_stream_logger` mirrors
`boto3.set_stream_logger` but redacts signatures, session tokens and keys.

For the `boto3-s3` command, see its
[guide](https://github.com/izumo-m/boto3-s3/blob/main/docs/cli/README.md) —
what differs from `aws s3`, the exit codes, and the configuration it reads.

The design documents behind all of this are indexed in
[`design/overview.md`](https://github.com/izumo-m/boto3-s3/blob/main/design/overview.md).

## Compatibility

- **Python:** 3.10 and later.
- **OS:** Linux, macOS, Windows (path-separator and case-sensitivity behavior is
  matched to `aws s3` on each).
- **AWS SDK:** `boto3` >= 1.28, `botocore` >= 1.31, `s3transfer` >= 0.6.2 —
  roughly three years old. Rather than emulate a newer S3 model on an older SDK,
  a feature that needs one is simply unavailable below it. Which feature needs
  which version, and how an unavailable one behaves, is in
  [`docs/compatibility.md`](https://github.com/izumo-m/boto3-s3/blob/main/docs/compatibility.md).

## Contributing

Bug reports, questions, and ideas are welcome on the
[issue tracker](https://github.com/izumo-m/boto3-s3/issues). To work on the code,
[`CONTRIBUTING.md`](https://github.com/izumo-m/boto3-s3/blob/main/CONTRIBUTING.md)
covers local setup (uv), the test suite, and the coding and commit conventions.
Report security vulnerabilities privately as described in
[`SECURITY.md`](https://github.com/izumo-m/boto3-s3/blob/main/SECURITY.md),
not on the public issue tracker.

## License

Apache-2.0. See
[`LICENSE`](https://github.com/izumo-m/boto3-s3/blob/main/LICENSE).

Source and issues: <https://github.com/izumo-m/boto3-s3>.
