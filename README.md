# boto3-s3

A complete library version of the `aws s3` command, with a faithful `aws s3 sync`
at its core. Every subcommand — `cp` / `ls` / `mb` / `mv` / `presign` / `rb` /
`rm` / `sync` / `website` — runs in-process from Python, not by shelling out to
the CLI. Byte transfers run on the same `s3transfer` engine as `aws s3`, so
transfer performance keeps pace with the command.

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
more effort. What's genuinely hard is matching `aws s3` faithfully: its path and
naming rules, recursive and include/exclude semantics, and above all a `sync` that
compares and transfers exactly like the command — fiddly to reproduce and easy to
get subtly wrong. The usual fallbacks are shelling out to the `aws s3` command and
parsing its output, or a partial reimplementation that drifts from it. boto3-s3 is
neither: it reproduces `aws s3`'s behavior across the whole command set — sync
included — as a library you call directly.

- **A faithful `aws s3 sync`.** Mirror local trees and buckets in any direction —
  upload, download, or S3-to-S3 — with `--delete`, the same size/timestamp
  comparison as aws-cli, include/exclude, and dry-run.
- **Every `aws s3` command.** `cp` / `ls` / `mb` / `mv` / `presign` / `rb` /
  `rm` / `website` complete the set.
- **A library, not a CLI wrapper.** Runs in-process: no `subprocess`, no scraping
  stdout, no `aws` on `PATH`. You pass boto3 clients directly and get structured,
  per-item results back — not text to parse.
- **Light enough for a Lambda.** The Python runtime already ships boto3 (and its
  `botocore` / `s3transfer` deps), so adding boto3-s3 costs well under a megabyte
  for the full `aws s3` feature set in-process — where bundling aws-cli (250 MB+)
  overruns the deployment size limit and shelling out isn't practical.
- **Transfer speed on par with aws-cli.** Byte transfers use the same engine as
  `aws s3` (`s3transfer`, or the optional CRT engine), so large transfers run at
  the same speed — no penalty for being a library.
- **Familiar behavior.** Path rules, options, and (for the CLI) exit codes
  follow `aws s3`, so what you know from the command carries over.

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

Create the `S3` object once — it holds no connection of its own (nothing to
close) and is safe to share across threads — then call what you need:

```python
from boto3_s3 import S3

s3 = S3()

# Sync a directory tree up to S3, removing remote extras (mirror).
s3.sync("./site", "s3://my-bucket/site/", delete=True)

# Copy a single object up or down.
s3.cp("./report.csv", "s3://my-bucket/report.csv")
s3.cp("s3://my-bucket/report.csv", "./report.csv")

# List objects lazily; each item is a FileInfo (key, size, …).
for info in s3.ls("s3://my-bucket/site/", recursive=True):
    print(info.key, info.size)

# Delete everything under a prefix.
s3.rm("s3://my-bucket/tmp/", recursive=True)

# A presigned URL (no request is sent).
url = s3.presign("s3://my-bucket/report.csv", expires_in=900)
```

For `cp` / `mv` / `sync` the **direction is inferred from the two endpoints**:
local-to-S3 is an upload, S3-to-local a download, S3-to-S3 a copy. A
local-to-local pair is rejected, like `aws s3`.

## Sync

`sync` is the heart of the library — a faithful re-creation of `aws s3 sync`,
callable from Python, in every direction:

```python
s3.sync("./site", "s3://my-bucket/site/")       # upload
s3.sync("s3://my-bucket/site/", "./site")       # download
s3.sync("s3://src/data/", "s3://dst/data/")     # S3-to-S3
```

It supports the flags you know from the command:

- **`delete=True`** — remove destination entries the source no longer has. Items
  hidden by a filter stay out of deletion too, exactly like `aws s3 sync`.
- **`compare=`** — how the source and destination are compared: `None` (default)
  uses size + mtime (equivalently `AwsCliComparison()`, tuned via
  `AwsCliComparison(size_only=True)` / `(exact_timestamps=True)`); `True` copies
  everything, `False` copies nothing; or pass a content strategy like
  `EtagComparison(s3)` / `ChecksumComparison(s3, src, dst)` (wrap either in
  `ParallelCompare(...)` to decide on a thread pool).
- **`filter=`** — include/exclude matching; **`dryrun=True`** to
  preview every transfer and deletion first.

The content strategies are opt-in submodule imports —
`from boto3_s3.etagcompare import EtagComparison` /
`from boto3_s3.checksumcompare import ChecksumComparison` (and
`from boto3_s3.awsclicompare import AwsCliComparison` to tune the default).
`ParallelCompare` imports from `boto3_s3` itself.

Because it runs in-process, sync hands back **structured results that `aws s3`
can't**: `on_result` fires once per item as the run proceeds, so you know exactly
what changed without parsing any output. Each result carries a `kind`
(upload / download / copy / delete) and an `outcome`
(succeeded / failed / warned / skipped):

```python
from boto3_s3 import OpKind, OpOutcome

uploaded = []

def track(r):
    if r.kind is OpKind.UPLOAD and r.outcome is OpOutcome.SUCCEEDED:
        uploaded.append(r.key)

s3.sync("./site", "s3://my-bucket/site/", delete=True, on_result=track)
print(f"{len(uploaded)} files uploaded")
```

## Operations

`S3` is the entry point: create one with `s3 = S3()`, then call the methods
below — each mirrors an `aws s3` subcommand.

| Method | `aws s3` | What it does |
| --- | --- | --- |
| `ls(target="s3://", *, recursive, page_size, request_payer, bucket_name_prefix, bucket_region)` | `ls` | List objects and common prefixes under an S3 target — or, at the bare service root, every bucket. Returns a lazy `Iterator[FileInfo]`. |
| `cp(src, dst, *, recursive, filter, dryrun, …, **options)` | `cp` | Copy bytes (upload / download / S3-to-S3 copy). `src` / `dst` may be a path/URI or a stream wrapped in `IOStorage` / `StdioStorage`. |
| `mv(src, dst, *, recursive, …, **options)` | `mv` | `cp`, then delete each source once its copy succeeds. |
| `sync(src, dst, *, delete, filter, compare, …, **options)` | `sync` | Recursively synchronize `src` into `dst`. |
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
    S3Storage("s3://dst-bucket/data/", client=dst_client),
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
for info in S3().ls(S3Storage("s3://bucket/", client=minio)):
    print(info.key)
```

The bucket part of an S3 URI may also be an access-point ARN (plain or
Outposts), passed through as the `Bucket`, like `aws s3`.

## Running operations across threads

An `S3` object carries only immutable defaults plus one benign, idempotent cache
(the `aws_config()` reader — concurrent first reads just recompute the same
value), so the object itself is safe to share across threads. The catch is
**building** a boto3 client: boto3 documents that a session is not thread-safe,
and creating clients concurrently from one session can resolve credentials more
than once and can fail while copying the session's shared event hooks. A bare
`"s3://..."` argument builds a fresh client on every call, so firing those
operations from many threads at once is exactly that unsupported pattern.

An already-built client is safe to *use* concurrently — which is why building one
up front and sharing it works. Pass a prebuilt client through
`S3Storage(url, client=client)`; the threaded calls then create nothing:

```python
import concurrent.futures
import boto3
from boto3_s3 import S3, S3Storage

s3 = S3()
client = boto3.Session(profile_name="prod", region_name="eu-west-1").client("s3")

with concurrent.futures.ThreadPoolExecutor() as pool:
    for path in paths:  # paths: an iterable of pathlib.Path
        dst = S3Storage(f"s3://prod-bucket/{path.name}", client=client)
        pool.submit(s3.cp, str(path), dst)
```

Alternatively, subclass `S3` and override `client()` to return a single
memoized client — then bare `"s3://..."` strings are concurrency-safe too.

## Options

`cp` / `mv` / `sync` take the `aws s3` transfer options as snake_case keyword
arguments, grouped here by what they control:

- **Metadata & headers:** `metadata`, `metadata_directive`, `copy_props`,
  `cache_control`, `content_type` / `content_disposition` / `content_encoding` /
  `content_language`, `expires`, `website_redirect`, `guess_mime_type`
- **Access & storage class:** `acl`, `grants`, `storage_class`, `request_payer`
- **Encryption:** `sse`, `sse_kms_key_id`, `sse_c` / `sse_c_key` (and the
  copy-source pair)
- **Integrity & write control:** `checksum_algorithm`, `checksum_mode`,
  `no_overwrite`, `case_conflict`
- **Glacier:** `force_glacier_transfer` / `ignore_glacier_warnings`

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
`aws s3`.

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
per-item gating. `sync` adds `compare=` and `delete=` (`True` to remove all
extras, or a filter to remove only matching ones). They answer different
questions: `filter=` decides **which items take part at all**, while `compare=`
then decides, for each matched source/destination pair, **whether it actually
needs copying** (by size + mtime, or by content with `EtagComparison` / `ChecksumComparison`).

## Progress, results, cancellation, dry run

Batch operations stream their per-item outcomes instead of returning a list:

- `on_result(OpResult)` — fires once per item as the run proceeds. Each
  `OpResult` carries a `kind` (upload / download / copy / delete) and an
  `outcome` (succeeded / failed / warned / skipped). It is called from worker
  threads, so keep it fast and non-raising.
- `on_progress(TransferProgress)` — byte-level transfer progress.
- `cancel_token` — a `CancelToken` whose `cancel()` cooperatively stops the run.
- `dryrun=True` — reports every would-be action without any mutating call.

## Custom backends

`cp` / `mv` / `sync` aren't limited to local paths and S3: a custom `Storage`
subclass — an HTTP service, an archive, an in-memory store — can be **one side of
a transfer, the other side always S3** (the built-in `IOStorage` /
`StdioStorage` stream wrappers are this same seam). A backend declares its
`capabilities`, which a transfer pre-checks, failing fast if it needs more.

See **[`docs/storage.md`](docs/storage.md)** for the `Storage` contract,
capabilities, and a worked example.

## Errors

Every failure is a `Boto3S3Error` subclass — catch the root to catch them all.

| Exception | Raised when |
| --- | --- |
| `Boto3S3Error` | Root of the hierarchy. Carries `operation` / `bucket` / `key`. |
| `ValidationError` | A supplied value, precondition, or path is invalid. |
| `ConfigurationError` | Credentials / region / profile / endpoint missing or unresolvable. |
| `NotFoundError` | The target does not exist (S3 404, local `FileNotFoundError`). |
| `AccessDeniedError` | Permission denied (S3 403, local `PermissionError`). |
| `TransportError` | Network or local I/O failure (connection, timeout, `OSError`). |
| `CancelledError` | Cancelled via `CancelToken`. |
| `BatchError` | Raised once at the end of a batch op (`cp -r` / `mv -r` / `rm -r` / `sync`) when at least one item failed. |

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
- **AWS SDK:** `boto3` >= 1.28, `botocore` >= 1.31, `s3transfer` >= 0.6.2. A few
  options need a newer SDK and are simply unavailable below it rather than
  emulated — conditional writes (`no_overwrite`), the `CRC64NVME` checksum, and
  the `ls` bucket-name / bucket-region filters. CRT features need the `crt`
  extra. Everything else works at the minimum.

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
