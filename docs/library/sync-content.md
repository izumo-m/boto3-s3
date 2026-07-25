# Deciding updates by content

`sync`'s default rule compares size and modification time, which is what
`aws s3 sync` does. It is fast — the listings already carry both values — but
timestamps are not content: a file rewritten with identical bytes gets copied,
and a same-size object updated only on the S3 side is not downloaded at all.

Two opt-in strategies replace that judgment with a real content comparison. Both
are `update_filter=` values, imported by submodule path:

```python
from boto3_s3.etagcompare import EtagComparison
from boto3_s3.checksumcompare import ChecksumComparison
```

They **replace** the default rather than compose with it. Combining them with
size + mtime would defeat the point: the timestamp rule would still copy
everything it copies today, and the content check would only ever add more.

Both apply to entries present on both sides. New entries are still created and
orphans still deleted by their own filters.

## 1. Which one to use

| | `EtagComparison` | `ChecksumComparison` |
| --- | --- | --- |
| Compares | S3's ETag, reconstructed for the local side | the object's stored checksum, recomputed locally |
| Extra requests | none | one `GetObjectAttributes` per object consulted |
| Multipart | needs the part size to match | exact — the part boundaries come back from S3 |
| Encrypted objects (SSE-KMS / SSE-C / DSSE) | does not work | works |
| Objects uploaded without a checksum | works | falls back to copying |

Reach for `EtagComparison` when the objects are unencrypted and you know the
part size they were uploaded with. Reach for `ChecksumComparison` when either of
those does not hold, and accept the round-trip.

## 2. `EtagComparison`

```python
s3.sync(src, dest, update_filter=EtagComparison(s3))            # part size from the profile
s3.sync(src, dest, update_filter=EtagComparison())              # 8 MiB part size
s3.sync(src, dest, update_filter=EtagComparison(part_size=16 * 1024 * 1024))
```

For **S3-to-S3** it compares the two listings' ETags directly — no bytes are
read. For **upload and download** it reads the local file and reconstructs the
ETag S3 would have stored, then compares.

**`part_size` must match what the object was uploaded with.** If it does not,
every multipart object reconstructs to a different ETag and is copied every
time. Passing the `S3` object reads it from that instance's active profile
(`[s3] multipart_chunksize`, defaulting to 8 MiB); passing `part_size=`
explicitly wins over both. This is one of the very few places the library reads
your AWS config, and it does so only because you asked by passing `s3`.

`check_size` (on by default) treats a known size difference as differing before
any ETag work — a shortcut, and on S3-to-S3 also a guard against two different
objects sharing an ETag.

**Encrypted objects do not work here.** SSE-KMS, SSE-C and DSSE objects carry an
opaque ETag that is not an MD5, so on upload and download every one of them
reads as differing and is copied. Use the default rule or `ChecksumComparison`
against such buckets.

An indeterminate comparison always copies. The strategy never skips on a value
it could not verify.

## 3. `ChecksumComparison`

```python
s3.sync(src, dest, update_filter=ChecksumComparison(s3, src, dest))
```

It asks S3 for the checksum it already stores and recomputes the same algorithm
locally. Because the checksum is stored alongside the object, nothing needs to
be written first, it is independent of encryption, and for multipart objects the
exact part boundaries come back from S3 — nothing is guessed.

Pass the same `src` and `dest` you passed to `sync`. It needs them to reach each
S3 side's client and bucket, which a `FileInfo` alone does not carry. For a
cross-account S3-to-S3 sync, pass the same `S3Storage` objects.

**Cost.** One `GetObjectAttributes` per object consulted — the S3 side on upload
and download, both sides on S3-to-S3 — plus the local hash. Section 4 makes
these run concurrently.

**Anything indeterminate is copied**: an object with no stored checksum, a pair
whose two sides used different algorithms, an unknown algorithm, or a
`GetObjectAttributes` that fails with a client error such as a 404, a denied
permission, or an SSE-C object whose key was not supplied.

**One class of failure is not per-object.** A missing credential, an unreachable
endpoint or a timeout aborts the whole sync rather than being treated as
"differing", because silently copying everything would hide a broken
environment.

`check_size` behaves as it does for `EtagComparison`. `pure_max_size` matters
only without `awscrt`: the `CRC32C` and `CRC64NVME` algorithms then fall back to
a pure-Python implementation that is roughly a thousand times slower, so this
caps how large an object it will attempt — above the cap the object is treated
as indeterminate and copied. The default, `None`, never caps. Installing the
`crt` extra removes the concern.

## 4. Running the decisions in parallel

Both strategies do I/O per pair, and `sync` decides one entry at a time on the
calling thread. Wrap a filter in `ParallelFilter` to run that lane's decisions
on a thread pool you own:

```python
from concurrent.futures import ThreadPoolExecutor
from boto3_s3 import ParallelFilter

with ThreadPoolExecutor(16, thread_name_prefix="sync-cmp") as pool:
    s3.sync(
        src, dest,
        update_filter=ParallelFilter(ChecksumComparison(s3, src, dest), executor=pool),
        delete_filter=ParallelFilter(is_expired, executor=pool),
    )
```

It works on **any of the three lanes**, not just updates, and one pool can serve
several — pass the same executor to each.

**The pool is yours.** It is required, and `sync` neither creates nor shuts it
down. It must be thread-based; a `ProcessPoolExecutor` cannot work because
neither the predicate nor its client can be pickled. Whatever you wrap must be
thread-safe — both content strategies are.

Wrapping is a pure performance change: the same entries are acted on and the
same outcome is reached. Two visible side effects are worth knowing:

- **Order.** Pooled decisions are consumed as they finish, so entries are
  submitted out of key order. This is invisible for transfers and batched
  deletes, but records emitted on the deciding thread — dry-run records, and
  deletions against a local destination — arrive in completion order instead of
  key order.
- **Case conflicts.** Parallelizing `create_filter` makes which entry wins a
  case-insensitive collision non-deterministic, because that check depends on
  the order entries arrive in. The update lane never touches it.

If a decision raises, the sync aborts as it would serially: decisions not yet
started are cancelled, running ones are awaited, and the exception surfaces.
Outstanding decisions are always awaited before `sync` returns, and your
executor is never shut down.
