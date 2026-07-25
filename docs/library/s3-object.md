# The `S3` object

`S3` is the entry point. It holds optional defaults for building clients and
transfers, but no connection of its own — there is nothing to close, and one
instance can serve the whole application.

```python
from boto3_s3 import S3

S3().cp("local.txt", "s3://bucket/key")
```

## 1. Creating one

```python
S3(session=None, *, endpoint_url=None, config=None,
   transfer_config=None, wait_on_interrupt=True)
```

- **`session`** — a `boto3.Session`. Omit it for a default session.
  `boto3_s3.session(**kwargs)` is a drop-in replacement for `boto3.Session`
  whose clients parse S3 listing timestamps at C speed; on a large `ls`, `sync`
  or `rm` the difference is severalfold.
- **`endpoint_url`**, **`config`** — passed on when building clients, for an
  S3-compatible endpoint or a `botocore.config.Config`.
- **`transfer_config`** — the default `TransferConfig` for `cp` / `mv` / `sync`.
  Any call can override it.
- **`wait_on_interrupt`** — what Ctrl-C should cost. `True` (the default)
  re-raises `KeyboardInterrupt` only after every resource has been reclaimed, so
  the next operation still works. `False` treats Ctrl-C as fatal to the process
  and lets the unwind abandon an in-flight listing page. Either way the
  interrupt is re-raised, never swallowed, and only `KeyboardInterrupt` is
  affected — every other exception reclaims fully.

## 2. Which client a location uses

A path argument is a `str`, an `os.PathLike`, or a `Storage` object. Bare
strings inherit this `S3`'s defaults; a `Storage` you construct is used exactly
as given, with its own client.

| You pass | It becomes |
| --- | --- |
| `"s3://bucket/key"` | an `S3Storage` using this instance's client |
| `"./local/path"` | a `LocalStorage` |
| `"bucket/key"` to `ls` / `rm` / `mb` / `rb` / `presign` / `website` | an `S3Storage` — these accept a missing `s3://` prefix |
| `S3Storage(uri, client=...)` | itself, with the client you gave it |
| `IOStorage(stream)` / `StdioStorage()` | itself, as one side of a `cp` (the other side must be S3) |

For a specific profile, region or endpoint, configure the `S3` object once and
every bare `"s3://..."` string follows:

```python
import boto3_s3
from boto3_s3 import S3

s3 = S3(session=boto3_s3.session(profile_name="prod", region_name="eu-west-1"))
s3.cp("./artifact.tar.gz", "s3://prod-bucket/artifacts/")
```

When a **single operation needs two different clients** — a cross-account
S3-to-S3 copy is the clear case — the instance default cannot express it. Build
each client and wrap each URL:

```python
s3.cp(
    S3Storage("s3://src-bucket/data/", client=src_client),
    S3Storage("s3://dest-bucket/data/", client=dest_client),
    recursive=True,
)
```

An S3-compatible endpoint such as MinIO is just a differently-built client:
either give it to the `S3` object for every location, or pass
`S3Storage(uri, client=minio)` when only one side needs it.

### Tuning how a side is read

How a location is *scanned* is configured on the `Storage`, not per operation.
`LocalStorage(path, follow_symlinks=..., detect_symlink_loops=...,
enumerate_all_entries=...)` controls the local walk;
`S3Storage(uri, page_size=..., fetch_owner=...)` controls the listing. A bare
string gets the defaults, so pass a configured `Storage` when you need to change
them — there is no per-call `page_size=` argument.

## 3. Running operations across threads

The `S3` object's own state is safe to share. Its clients are not: neither
building a client concurrently nor sharing one across concurrent operations is
safe.

So: **build the clients sequentially, up front, then give each concurrent
operation its own** through `S3Storage`.

```python
import concurrent.futures
import boto3
from boto3_s3 import S3, S3Storage

s3 = S3()
session = boto3.Session(profile_name="prod")
jobs = [
    (path, S3Storage(f"s3://prod-bucket/{path.name}", client=session.client("s3")))
    for path in paths
]

with concurrent.futures.ThreadPoolExecutor() as pool:
    for path, dest in jobs:
        pool.submit(s3.cp, str(path), dest)
```

Do not use bare `"s3://..."` arguments for this: each operation would build its
own client, concurrently, which is the case that is not safe.

## 4. Module-level shortcuts

`boto3_s3.cp` / `ls` / `mv` / `rm` / `mb` / `rb` / `presign` / `sync` /
`website` are thin wrappers over a default `S3()`, for the zero-config case:

```python
import boto3_s3
boto3_s3.sync("./site", "s3://my-bucket/site/")
```

Each keeps its method's exact signature, so type checkers and editors behave the
same. They build a fresh `S3()` on every call — there is no shared instance — so
use `S3(session=...)` when you need configuration.

## 5. Extending it

Two methods are the supported override points:

```python
class MyS3(S3):
    def client(self):
        return my_session.client("s3", config=my_config)

    def resolve(self, loc):
        if isinstance(loc, str) and loc.startswith("http://"):
            return HttpStorage(loc)
        return super().resolve(loc)
```

- **`client()`** builds a fresh client each time, owned by the caller. Override
  it to change credentials, to return a test double, or to memoize. Note that a
  memoizing override makes your subclass own a connection, and with it the
  cleanup — the base class keeps none precisely so it stays lifecycle-free.
- **`resolve(loc)`** decides what a path argument means. Override it to add a
  scheme, deferring everything else to `super()`.

What can be substituted has limits. The S3-only operations
(`ls` / `rm` / `mb` / `rb` / `presign` / `website`) accept only an `S3Storage`,
since each needs a bucket and a client. A custom backend can be **one side of a
transfer, with the other side always S3**.

## 6. Reading the AWS config file

Operations never read the `[s3]` tuning section of `~/.aws/config` on their own:
transfer settings come from arguments, never from ambient configuration.
Credentials, region and profile still resolve through boto3's usual chain, which
does read the file. When you want a value from it yourself, ask:

```python
cfg = s3.aws_config()
cfg.get_size("s3.multipart_chunksize", 8 * 1024**2)   # "16MB" -> bytes
cfg.get_str("region", None)
cfg.profile("prod").get_int("s3.max_concurrent_requests", 10)
```

The active profile is the default context and the parsing is botocore's, so file
location, the nested `s3 =` subsection syntax and profile resolution match what
`aws configure get` sees — including values that live in `~/.aws/credentials`.
`services()`, `sso_session()` and `plugins()` select the other section kinds.

Getters are typed (`get_str` / `get_int` / `get_size` / `get_bool` / `get_rate`;
sizes and rates are 1024-based, matching `aws s3`) and take your own default.
A missing key returns that default; a value that will not convert raises
`InvalidConfigError`, which `except ConfigurationError` catches.

Interpreting the `[s3]` section the way `aws s3` does — its defaults, its
validation, the transfer-engine decision — is the CLI's job, not the library's.
