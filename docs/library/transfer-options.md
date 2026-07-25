# Transfer options

`cp` / `mv` / `sync` take the `aws s3` transfer options as snake_case keyword
arguments, plus a few the command has no flag for.

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

Grouped by what they control:

- **Metadata and headers** — `metadata`, `metadata_directive`, `copy_props`,
  `cache_control`, `content_type`, `content_disposition`, `content_encoding`,
  `content_language`, `expires`, `website_redirect`, `guess_mime_type`
- **Access and storage class** — `acl`, `grants`, `storage_class`,
  `request_payer`
- **Encryption** — `sse`, `sse_kms_key_id`, `sse_c`, `sse_c_key`, and the
  copy-source pair `sse_c_copy_source` / `sse_c_copy_source_key`
- **Integrity and write control** — `checksum_algorithm`, `checksum_mode`,
  `no_overwrite`, `case_conflict`
- **Archived objects** — `force_glacier_transfer`, `ignore_glacier_warnings`
- **Library-only** — `annotation_copy_mode`

An option that does not apply to the direction being run is simply ignored
rather than rejected — passing `acl` on a download changes nothing. The
exceptions are `checksum_algorithm`, `checksum_mode` and the SSE-C copy-source
pair, which are rejected on the wrong route.

## 1. Multipart and concurrency

Thresholds, part sizes, concurrency, bandwidth and the transfer engine are a
`TransferConfig`, not individual options:

```python
from boto3_s3 import TransferConfig

s3.cp(src, dest, transfer_config=TransferConfig(multipart_chunksize=16 * 1024**2))
```

Its defaults match `aws s3`: an 8 MiB threshold, 8 MiB parts, 10 concurrent
requests. Set it once on the `S3` object to apply everywhere, and per call to
override.

`preferred_transfer_client` selects the engine — `"auto"` (the default),
`"classic"`, or `"crt"`. S3-to-S3 copies always use the classic engine, since
the CRT engine has no copy operation. See
[`compatibility.md`](../compatibility.md) for what the CRT engine needs.

## 2. Refusing to overwrite

`no_overwrite=True` skips anything already present at the destination, silently
and successfully — a skip, not a failure.

How it is enforced depends on direction. Uploads and copies attach a conditional
header, so the check happens at S3 and is free of races. Downloads check whether
the local file exists before starting.

In `sync` it is decision-only: it never sends the conditional header, which
means it also works on older SDKs where `cp` would be refused up front. See
[`compatibility.md`](../compatibility.md).

## 3. Copying properties between S3 objects

A single-request copy carries metadata and tags across natively, but **a
multipart copy does not**. `copy_props` says what to do about that:

| value | what carries over |
| --- | --- |
| `none` | nothing |
| `metadata-directive` | the metadata properties, not tags |
| `default` | metadata and tags |
| `all` | metadata, tags, and object annotations |

Two behaviors worth knowing. Specifying an explicit property such as
`content_type` makes even a single-request copy replace rather than copy the
rest, matching `aws s3`. And specifying `metadata_directive` yourself disables
the whole mechanism.

Under `default` and `all`, tags too large for the request header are applied
after the copy succeeds. If that call fails, the destination is deleted on a
best-effort basis and the item is reported as failed. There is one corner where
that cleanup itself fails: the item is then reported **successful** with the
destination left carrying no tags, which is `aws s3`'s behavior as well.

`annotation_copy_mode` tunes how a multipart copy under `copy_props="all"`
stages annotations — in memory by default, matching the command's failure
timing; a temporary file, or deferred reads for lower overhead. The `boto3-s3`
command deliberately exposes no flag for it.

## 4. Archived objects

An archived object that has not been restored is skipped with a warning on
downloads and copies. `force_glacier_transfer=True` attempts it anyway, which
means S3 rejects an unrestored object — the same as `aws s3`.
`ignore_glacier_warnings=True` skips silently instead.

**Restored objects are skipped too, during recursive transfers.** Restore status
does not appear in a listing, and a recursive run decides from the listing, so
`force_glacier_transfer` is the only way through. `aws s3` behaves identically.

## 5. Case conflicts

On a case-insensitive destination — Windows, or a macOS volume — two keys
differing only in case collide. `case_conflict` chooses what happens: `ignore`
(the default), `skip`, `warn`, or `error`, which fails the run.

## 6. Warnings are counted separately from failures

A warning is not a failure, and the two are counted independently. A download
that transferred fine but could not stamp the file's modification time produces
**two** records: a success and a warning.

That distinction is what `BatchError` reports back, and what the command turns
into its exit codes. Local directory-walk problems — an unreadable file, a
broken symlink, a special file, an invalid timestamp — arrive as warnings on the
run rather than aborting it.

## 7. Options that live on the storage, not the call

How a local directory is walked is configured on `LocalStorage`, not per
operation:

```python
from boto3_s3 import LocalStorage

s3.sync(LocalStorage("./site", follow_symlinks=False), "s3://bucket/site/")
```

`follow_symlinks` matches `aws s3`. `detect_symlink_loops` is an addition, off
by default to preserve that parity: without it a symlink cycle descends until
the operating system stops it, exactly as `aws s3` does; with it, a directory
that resolves to one of its own ancestors is skipped with a warning.

Listing behavior is likewise configured on `S3Storage` — `page_size` and
`fetch_owner`. See [`s3-object.md`](./s3-object.md).
