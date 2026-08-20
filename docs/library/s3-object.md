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
   transfer_config=None, reusable_after_interrupt=True,
   crt_allow_absent_credentials=False, crt_allow_lockless=False,
   crt_region=CLIENT_REGION, crt_sign_requests=None)
```

- **`session`** — a `boto3.Session`. Omit it for a default session.
  `boto3_s3.session(**kwargs)` is a drop-in replacement for `boto3.Session`
  whose clients parse S3 listing timestamps at C speed; on a large `ls`, `sync`
  or `rm` the difference is severalfold.
- **`endpoint_url`**, **`config`** — passed on when building clients, for an
  S3-compatible endpoint or a `botocore.config.Config`.
- **`transfer_config`** — the default `TransferConfig` for `cp` / `mv` / `sync`.
  Any call can override it.
- **`reusable_after_interrupt`** — how Ctrl-C is handled: clean up first, or exit fast.
  `True` (the default) re-raises `KeyboardInterrupt` only after every resource
  has been reclaimed, so the next operation still works. `False` treats Ctrl-C
  as fatal to the process and lets the unwind abandon an in-flight listing page.
  Either way the interrupt is re-raised, never swallowed — with one
  engine-imposed exception: pip s3transfer's CRT manager discards an interrupt
  that lands in its transfer drain, so a CRT run cut short there raises
  `BatchError` with the cancelled items counted as failures instead. Only
  `KeyboardInterrupt` is affected — every other exception reclaims fully.
- **`crt_allow_absent_credentials`** — whether the CRT transfer engine may be
  used by a client that resolved no credentials. `False` (the default) drops
  to the classic engine and reports `Unable to locate credentials`; `True`
  attempts the transfer and lets it fail inside the CRT credentials delegate,
  which is what `aws s3` does. Only reproducing aws's output needs it.
- **`crt_allow_lockless`** — what an explicit
  `preferred_transfer_client="crt"` does when another process of this
  application already holds the cross-process CRT slot. `False` (the default)
  silently selects the classic engine, boto3's rule; `True` builds the CRT
  client regardless, which is what `aws s3` does — so a construction-time
  failure surfaces under contention too. `"auto"` respects the lock either
  way. Only reproducing aws's behavior needs it.
- **`crt_region`** — where the CRT transfer engine takes its region from.
  `CLIENT_REGION` (the default) reads it off the client that was built, which
  is what boto3 does. Passing your own already-resolved region uses that value
  instead, `None` included — and `None` is the whole point: when nothing
  configures a region, botocore quietly gives the client the `aws-global`
  pseudo-region, while `aws s3` resolves its own chain to `None` and awscrt
  then refuses to build a client at all. Only reproducing that refusal needs
  it. Like the flag above it does nothing unless
  `TransferConfig.preferred_transfer_client` selects the CRT engine.
- **`crt_sign_requests`** — whether the CRT transfer engine signs its
  requests. `None` (the default) derives the answer from the built client,
  boto3's rule. An explicit `False` builds the CRT client with no credentials
  provider (nothing is resolved, every CRT request goes out anonymous);
  `True` forces a provider. Only reproducing `aws s3` needs it — aws's CRT
  factory decides from its own `sign_request` flag and never from the client,
  which is how `--no-sign-request --sse aws:kms` transfers anonymously on
  aws's CRT engine while its classic engine signs.

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
  memoizing override makes your subclass own a connection: closing it becomes
  your responsibility.
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

If you want `aws s3`'s own interpretation of `[s3]` — its defaults, its
validation, its engine choice — use the `boto3-s3` command.

## 7. One small file, one request

`cp` runs the transfer engine: s3transfer's futures and thread pool, and for a
download a `HeadObject` probe before the transfer — so fetching one small object
costs two requests plus the machinery. For the files an application round-trips
constantly — a state file, a config, a manifest — `S3Storage` has a pair of
methods that skip all of it and make **exactly one** S3 call:

```python
from boto3_s3 import S3Storage

state = S3Storage("s3://my-bucket/app/state.json")

info = state.get_file("state.json")     # one GetObject, straight to the file
print(info.size, info.etag, info.mtime)

# ... your program rewrites state.json ...

info = state.put_file("state.json")     # one PutObject, straight from the file
print(info.etag)
```

`key` addresses an entry beneath the location, exactly as `get_fileinfo`'s does,
so one `S3Storage` can serve a whole prefix:

```python
app = S3Storage("s3://my-bucket/app/")
app.get_file("./cache/manifest.json", key="manifest.json")
app.put_file("./cache/manifest.json", key="manifest.json")
```

Both return an `S3FileInfo` for the object — the full key, the size, the
dequoted ETag, and the whole response under `head` — so a download needs no
follow-up `HeadObject` for the object's mtime or storage class either. (An
upload's info describes what `PutObject` answered, which carries no timestamp.)

**The download is atomic.** The body streams into a temp file next to the
destination and is then renamed onto it, the same safety `cp`'s download lane
has: a failure or a broken connection leaves the previous file byte-for-byte
intact with no leftovers, readers never see a half-written file, a symlink at
the destination is replaced rather than written through, and missing parent
directories are created. One thing goes further than `cp`'s lane: an existing
file's permission bits survive the replacement, rather than the replaced file
coming back with whatever bits a fresh one gets.

What this pair deliberately does not do:

- **no multipart**, in either direction, and no threshold to cross. A file too
  large for a single `PutObject` fails with S3's own error.
- **no `Content-Type` guessing** on upload, and no other object shaping —
  `put_file` sends the bytes and nothing else.
- **no mtime stamping** on a download, and none of `cp`'s `aws s3` parity gates
  (glacier, `--no-overwrite`, case conflicts).

So reach for `cp` / `sync` for large objects, for whole trees, and whenever you
want the `aws s3` behavior; reach for these two when one small object is the
whole job. They are `S3Storage` methods, not `S3` operations: a custom backend
has nothing to implement for them.

Being `S3Storage` methods also means the location owns the client. The examples
above let it build a default one on first use — release it with `close()`, or
pass the client you want (`S3Storage(uri, client=s3.client())` reuses this `S3`
object's configuration).
