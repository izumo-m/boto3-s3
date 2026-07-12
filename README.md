# boto3-s3

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

**Status:** early development (pre-1.0) — all subcommands are implemented; the
public API may still change.
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
operations require separate, prebuilt clients as described below.

```python
from boto3_s3 import S3

s3 = S3()

# Sync a directory tree up to S3, removing remote extras (mirror).
s3.sync("./site", "s3://my-bucket/site/", delete_filter=True)

# Copy a single object up or down.
s3.cp("./report.csv", "s3://my-bucket/report.csv")
s3.cp("s3://my-bucket/report.csv", "./report.csv")

# List objects; each result is a FileInfo (key, size, …).
s3.ls(
    "s3://my-bucket/site/",
    recursive=True,
    on_result=lambda info: print(info.key, info.size),
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

`sync` is the heart of the library: its default pipeline is designed as an
in-process replacement for `aws s3 sync`, in every direction:

```python
s3.sync("./site", "s3://my-bucket/site/")       # upload
s3.sync("s3://my-bucket/site/", "./site")       # download
s3.sync("s3://src/data/", "s3://dest/data/")     # S3-to-S3
```

Unlike `aws s3 sync`, boto3-s3 can decide updates by content instead of size +
mtime. Pass `EtagComparison` as the update strategy to copy changed content and
skip unchanged content even when timestamps differ:

```python
from boto3_s3.etagcompare import EtagComparison

s3.sync(
    "./site",
    "s3://my-bucket/site/",
    update_filter=EtagComparison(s3),
)
```

For S3-to-S3 sync this compares the ETags already returned by the listings,
without reading object bodies. For upload and download it reads the non-S3 side
and reconstructs its S3-style ETag. When many existing files need that work,
run the comparison decisions concurrently on a caller-owned thread pool:

```python
from concurrent.futures import ThreadPoolExecutor

from boto3_s3 import ParallelFilter

with ThreadPoolExecutor(max_workers=16, thread_name_prefix="sync-etag") as pool:
    s3.sync(
        "./site",
        "s3://my-bucket/site/",
        update_filter=ParallelFilter(EtagComparison(s3), executor=pool),
    )
```

`ParallelFilter` parallelizes only the update decisions; transfers continue to
use the transfer engine's own concurrency. Multipart part size and server-side
encryption affect when ETags are content-comparable; see the
[ETag comparison details](https://github.com/izumo-m/boto3-s3/blob/main/docs/sync.md#8-etag-content-comparison-etagcomparison-opt-in).

`sync` makes one of three decisions per entry — the same three cases as
`aws s3 sync`, each with its own filter:

- **`create_filter`** — whether to **create** an entry that is new (in the source,
  not yet at the destination). `True` (default) creates every one; `False`
  creates none; a predicate creates only the ones it keeps. (aws always creates;
  this is the knob aws does not expose.)
- **`update_filter`** — for an entry **present on both sides**, whether to
  **overwrite** it: `None` (default) decides by size + mtime (equivalently
  `AwsCliComparison()`, tuned via `AwsCliComparison(size_only=True)` /
  `(exact_timestamps=True)`); `True` always, `False` never (leave existing
  as-is); or a content strategy `EtagComparison(s3)` /
  `ChecksumComparison(s3, src, dest)` (wrap any lane's filter in
  `ParallelFilter(fn, executor=pool)` to decide on a caller-supplied thread pool).
- **`delete_filter`** — whether to **delete** an orphan (at the destination, no
  longer in the source). `False` (default) keeps orphans; `True` deletes every
  one (`aws s3 sync --delete`); a predicate deletes only the ones it keeps. Items
  hidden by `filter` stay out of deletion too, exactly like `aws s3 sync`.

`create_filter` and `delete_filter` are the two membership knobs (create / delete);
`update_filter` is the overwrite judgment for the entries on both sides.
`filter=` is separate — it decides which entries are **visible** at all (pruning
both sides symmetrically, like `--exclude` / `--include`); **`dryrun=True`**
previews every transfer and deletion first.

```python
# Full mirror: create new, overwrite changed, delete removed.
s3.sync("./site", "s3://my-bucket/site/", delete_filter=True)

# Update-only mirror: refresh existing files and prune deleted ones,
# but never publish brand-new files.
s3.sync("./site", "s3://my-bucket/site/", create_filter=False, delete_filter=True)
```

The content strategies are opt-in submodule imports —
`from boto3_s3.etagcompare import EtagComparison` /
`from boto3_s3.checksumcompare import ChecksumComparison` (and
`from boto3_s3.awsclicompare import AwsCliComparison` to tune the default).
`ParallelFilter` imports from `boto3_s3` itself.

Because it runs in-process, sync provides **structured results without parsing
console output**. `on_result` receives terminal item outcomes as the run
proceeds, plus any additional warning or notice records. Each result carries a
`transfer_type` (upload / download / copy / delete) and an `outcome` (succeeded /
failed / warned / skipped / dryrun / notice):

```python
from boto3_s3 import TransferType, OpOutcome

uploaded = []

def track(r):
    if r.transfer_type is TransferType.UPLOAD and r.outcome is OpOutcome.SUCCEEDED:
        uploaded.append(r.key)

s3.sync("./site", "s3://my-bucket/site/", delete_filter=True, on_result=track)
print(f"{len(uploaded)} files uploaded")
```

## Operations

`S3` is the entry point: create one with `s3 = S3()`, then call the methods
below — each mirrors an `aws s3` subcommand.

| Method | `aws s3` | What it does |
| --- | --- | --- |
| `ls(target="s3://", *, on_result, recursive, request_payer, bucket_name_prefix, bucket_region, cancel_token)` | `ls` | List objects and common prefixes under an S3 target — or, at the bare service root, every bucket. Delivers ordered `FileInfo` entries to `on_result`. The listing page size is the `S3Storage`'s own `page_size` config. |
| `cp(src, dest, *, recursive, filter, dryrun, …, **options)` | `cp` | Copy bytes (upload / download / S3-to-S3 copy). `src` / `dest` may be a path/URI or a stream wrapped in `IOStorage` / `StdioStorage`. |
| `mv(src, dest, *, recursive, …, **options)` | `mv` | `cp`, then delete each source once its copy succeeds. |
| `sync(src, dest, *, filter, create_filter, update_filter, delete_filter, …, **options)` | `sync` | Recursively synchronize `src` into `dest`. |
| `rm(target, *, recursive, filter, dryrun, request_payer, …)` | `rm` | Delete objects (a single key, a recursive prefix, or the folder-marker sweep). |
| `mb(target, *, tags)` | `mb` | Create the bucket of `target`. |
| `rb(target)` | `rb` | Delete the (empty) bucket of `target`. |
| `presign(target, *, expires_in=3600, method="get_object")` | `presign` | Return a presigned URL. No request is sent. |
| `website(target, *, index_document, error_document)` | `website` | Set the bucket website configuration. |

## Choosing the client (profile, region, endpoint, cross-account)

A path argument is a `str`, an `os.PathLike`, or an `S3Storage`. A bare
`"s3://..."` string uses the client the `S3` instance builds from its own
defaults — `boto3.client("s3")` for a zero-config `S3()`.

For a specific **profile, region, or endpoint**, hand the `S3` object a
`boto3.Session` (and/or an `endpoint_url` / `config`); every bare `"s3://..."`
string then inherits it:

```python
import boto3
from boto3_s3 import S3, S3Storage

session = boto3.Session(profile_name="prod", region_name="eu-west-1")
s3 = S3(session=session)
s3.cp("./artifact.tar.gz", "s3://prod-bucket/artifacts/")
```

When a single operation needs **more than one client** — a cross-account
S3-to-S3 copy is the clearest case — the instance default can't express it.
Build each client yourself and wrap the URL in an `S3Storage`, which is used
verbatim with its own client:

```python
s3.cp(
    S3Storage("s3://src-bucket/data/", client=src_client),
    S3Storage("s3://dest-bucket/data/", client=dest_client),
    recursive=True,
)
```

An S3-compatible endpoint such as MinIO is just a differently-built client —
set it on the `S3` for every location, or pass a single
`S3Storage(url, client=minio)` when only one side needs it:

```python
minio = boto3.client(
    "s3",
    endpoint_url="http://localhost:9000",
    aws_access_key_id="minioadmin",
    aws_secret_access_key="minioadmin",
)
S3().ls(
    S3Storage("s3://bucket/", client=minio),
    on_result=lambda info: print(info.key),
)
```

The bucket part of an S3 URI may also be an access-point ARN (plain or
Outposts), passed through as the `Bucket`, like `aws s3`.

## Running operations across threads

An `S3` object's own state is safe to share across threads, but parallel
operations need care on two fronts: boto3 sessions are not safe for concurrent
client construction, and s3transfer's per-transfer setup is not safe on a
client shared by concurrent operations. Build the clients sequentially before
starting the workers, then give each concurrent operation its own client through
`S3Storage`:

```python
import concurrent.futures
import boto3
from boto3_s3 import S3, S3Storage

s3 = S3()
session = boto3.Session(profile_name="prod", region_name="eu-west-1")
jobs = [
    (
        path,
        S3Storage(
            f"s3://prod-bucket/{path.name}",
            client=session.client("s3"),
        ),
    )
    for path in paths
]

with concurrent.futures.ThreadPoolExecutor() as pool:
    for path, dest in jobs:
        pool.submit(s3.cp, str(path), dest)
```

Do not use bare `"s3://..."` arguments for this pattern: they call `client()`
inside each operation and would build clients concurrently.

## Options

`cp` / `mv` / `sync` take the `aws s3` transfer options as snake_case keyword
arguments plus documented library extensions, grouped here by what they control:

- **Metadata & headers:** `metadata`, `metadata_directive`, `copy_props`,
  `cache_control`, `content_type` / `content_disposition` / `content_encoding` /
  `content_language`, `expires`, `website_redirect`, `guess_mime_type`
- **Access & storage class:** `acl`, `grants`, `storage_class`, `request_payer`
- **Encryption:** `sse`, `sse_kms_key_id`, `sse_c` / `sse_c_key` (and the
  copy-source pair)
- **Integrity & write control:** `checksum_algorithm`, `checksum_mode`,
  `no_overwrite`, `case_conflict`
- **Glacier:** `force_glacier_transfer` / `ignore_glacier_warnings`
- **Annotation staging (library-only):** `annotation_copy_mode`

```python
s3.cp(
    "./photo.jpg",
    "s3://my-bucket/photo.jpg",
    storage_class="STANDARD_IA",
    content_type="image/jpeg",
    metadata={"reviewed": "yes"},
    acl="bucket-owner-full-control",
)
```

Multipart tuning (thresholds, concurrency, bandwidth, the classic/CRT engine
choice) is a `TransferConfig` passed as `transfer_config=`; its defaults match
`aws s3`. For multipart S3-to-S3 copies under `copy_props=ALL`, annotations are
preloaded in memory by default to match aws-cli's failure timing. Library
callers can select `AnnotationCopyMode.PRELOAD_TEMPFILE` (configured by
`TransferConfig(annotation_temp_dir=...)`) or the lower-overhead
`AnnotationCopyMode.DEFERRED`; the CLI deliberately exposes no corresponding
flag.

## Filtering

`cp` / `mv` / `rm` / `sync` take a `filter=` that decides which items stay in the
operation. The common form is an `aws s3`-style include/exclude matcher (last
match wins, default-include):

```python
from boto3_s3 import GlobFilter

keep = GlobFilter().exclude("*").include("*.tar.gz").compile()
s3.cp("./build", "s3://artifacts/", recursive=True, filter=keep)
```

`filter=` also accepts a plain `Callable[[FileInfo], bool]` for arbitrary
per-item gating. `sync` adds one filter per decision — `create_filter=` (create a
new entry?), `update_filter=` (overwrite one on both sides?), `delete_filter=`
(delete an orphan?). They answer different questions from `filter=`: `filter=`
decides **which items take part at all** (visible on either side), while the
three lane filters decide **what to do** with the entries that do — create,
overwrite, delete.

## Progress, results, cancellation, dry run

Batch operations stream item records instead of returning a list:

- `on_result(OpResult)` — receives each terminal item outcome as the run
  proceeds. An item may also emit a `WARNED` or `NOTICE` record, so callers must
  not assume exactly one callback per key. Callbacks may come from the calling
  thread or worker threads, and may be concurrent; keep them thread-safe, fast,
  and non-raising.
- `on_progress(TransferProgress)` — byte-level transfer progress
  (`cp` / `mv` / `sync`; `rm` moves no bytes).
- `S3.ls(..., on_result=...)` — receives each listed `FileInfo`, in listing
  order on the calling thread.
- `cancel_token` — a `CancelToken` whose `cancel()` stops new work and drains
  accepted work (`ls` / `cp` / `mv` / `rm` / `sync`). Pass
  `mode=CancelMode.IMMEDIATE` to additionally request best-effort cancellation
  of pending and in-flight work. A later immediate request upgrades an earlier
  graceful request; cancellation never downgrades.
- `dryrun=True` — reports every would-be action without any mutating call.

## Custom backends

`cp` / `mv` / `sync` aren't limited to local paths and S3: a custom `Storage`
subclass — an HTTP service, an archive, an in-memory store — can be **one side of
a transfer, the other side always S3** (the built-in `IOStorage` /
`StdioStorage` stream wrappers are this same seam). A backend declares its
`capabilities`, which a transfer pre-checks, failing fast if it needs more.

See **[`docs/storage.md`](https://github.com/izumo-m/boto3-s3/blob/main/docs/storage.md)**
for the `Storage` contract, capabilities, and a worked example.

## Errors

Recognized operational failures use the `Boto3S3Error` hierarchy, so catching
the root handles the library's translated S3, filesystem, validation, and
configuration errors. Programming errors and a small number of documented
dependency pass-throughs are not wrapped.

| Exception | Raised when |
| --- | --- |
| `Boto3S3Error` | Root of the hierarchy. Carries `operation` / `bucket` / `key`. |
| `ValidationError` | A supplied value, precondition, or path is invalid. |
| `ConfigurationError` | Credentials / region / profile / endpoint missing or unresolvable. |
| `NotFoundError` | The target does not exist (S3 404, local `FileNotFoundError`). |
| `AccessDeniedError` | Permission denied (S3 403, local `PermissionError`). |
| `TransportError` | Network or local I/O failure (connection, timeout, `OSError`). |
| `CancelledError` | Cancelled via `CancelToken`. |
| `BatchError` | Raised once at the end of a `cp` / `mv` / `rm` / `sync` run when at least one item failed — single-item runs included; an error before the run starts (validation, resolution) raises its category error directly. |

`BatchError` carries summary counts (`succeeded` / `failed` / `warned` /
`skipped` / `total`); the per-item detail arrives live through `on_result`.

## Debug logging

`boto3` / `botocore` debug logs leak signed headers, signatures, and session
tokens. `set_stream_logger` mirrors `boto3.set_stream_logger` but redacts those
by default:

```python
from boto3_s3 import set_stream_logger

set_stream_logger("botocore")  # credentials masked unless mask_secrets=False
```

## Compatibility

- **Python:** 3.10 and later.
- **OS:** Linux, macOS, Windows (path-separator and case-sensitivity behavior is
  matched to `aws s3` on each).
- **AWS SDK:** `boto3` >= 1.28, `botocore` >= 1.31, `s3transfer` >= 0.6.2.
  Features introduced by newer S3 models degrade rather than being emulated.
  Notable examples include conditional writes (`no_overwrite`), `CRC64NVME`,
  paginated bucket filters, newer `mb` tags / account-regional namespace
  fields, S3 object annotations, `copy_props="all"`, and source-ETag response
  extras. CRT features need the `crt` extra. See
  [`docs/overview.md`](https://github.com/izumo-m/boto3-s3/blob/main/docs/overview.md#2-supported-scope)
  for the authoritative version requirements and degradation behavior.

## In short

Every `aws s3` operation as an in-process Python call — no `subprocess`, no
stdout to scrape, structured per-item results back.

## Contributing

Bug reports, questions, and ideas are welcome on the
[issue tracker](https://github.com/izumo-m/boto3-s3/issues). To work on the code,
[`CONTRIBUTING.md`](https://github.com/izumo-m/boto3-s3/blob/main/CONTRIBUTING.md)
covers local setup (uv), the test suite, and the coding and commit conventions.

## License

Apache-2.0. See
[`LICENSE`](https://github.com/izumo-m/boto3-s3/blob/main/LICENSE).

Source and issues: <https://github.com/izumo-m/boto3-s3>.
