# Version-dependent feature availability

Which `boto3` release each feature needs. Below the version listed, that feature
is not supported.

**What you have** is printed by `boto3-s3 --version`, or from Python:

```python
import boto3
print(boto3.__version__)
```

**The short answer:** `boto3` 1.43.31 or newer has everything below. Four of the
entries additionally need the `crt` extra, and one waits on an s3transfer
release that has not happened yet.

boto3-s3 supports SDKs going back roughly three years, and the installed one
decides what is available. Rather than emulate newer AWS behavior on an older
SDK, a feature that depends on a newer S3 model is simply unavailable.

**Upgrading `boto3` is the one move that matters.** It pins `botocore` and
`s3transfer` to matching releases, so its version settles all three; raising
either of the other two on its own is not a supported move. boto3-s3 declares
only floors and no ceiling, and detects each capability instead of comparing
version numbers, so a newer SDK starts being used with no change here.

## 1. Features by required version

| Feature | Needs |
| --- | --- |
| the CRT transfer engine | boto3 >= 1.29.7, plus the `crt` extra |
| S3 Express directory buckets (`--x-s3`) | boto3 >= 1.33.2 |
| `ls --bucket-name-prefix` / `--bucket-region` | boto3 >= 1.35.42 |
| `no_overwrite` / `--no-overwrite` on upload | boto3 >= 1.36.0 |
| `checksum_algorithm` = `CRC64NVME` | boto3 >= 1.36.0, plus the `crt` extra |
| `mb --tags` (`CreateBucketConfiguration.Tags`) | boto3 >= 1.39.2 |
| `no_overwrite` / `--no-overwrite` on copy | boto3 >= 1.41.0 |
| `[s3]` tuning reaching the CRT transfer manager | boto3 >= 1.42.0 |
| the `-an` namespace bucket's `BucketNamespace` | boto3 >= 1.42.67 |
| `checksum_algorithm` = `MD5` / `SHA512` | boto3 >= 1.42.94 |
| `checksum_algorithm` = the `XXHASH` family | boto3 >= 1.42.94, plus the `crt` extra |
| S3 object annotations (GA 2026-06) | boto3 >= 1.43.31 |
| `copy_props=ALL` / `--copy-props all` | boto3 >= 1.43.31 |
| `checksum_algorithm` = `CRC32C` | any supported boto3, plus the `crt` extra |
| the source ETag in `OpResult.extra_info` | an s3transfer release that exposes it - none so far |

`CRC32`, `SHA1` and `SHA256` are available on every supported installation and
are not listed.

## 2. awscrt

`awscrt` is not a default dependency but an opt-in extra (`crt`). It gates two
independent things: the CRT transfer engine, and the checksum algorithms
botocore can only compute through it - **`CRC32C`, `CRC64NVME`, and the
`XXHASH` family**. The classic engine needs it for those too. `CRC32`, `SHA1`,
`SHA256` and `SHA512` are pure Python and never need it.

Missing awscrt fails only the features that need it, and that does not count as
an exit code mismatch (see [`overview.md`](../design/overview.md) section 3). The
exit code charter does apply once the CRT stack is usable.

## 3. Not a version gap: the aws-cli s3transfer fork

The `[s3]` file-I/O keys `should_stream` / `disk_throughput` / `direct_io` are
parsed and validated but have no effect, because no released pip `s3transfer`
takes them. aws-cli v2 is a self-contained distribution bundling a fork that
does, so `aws` honors these three where a pip install cannot. No newer pip
release fixes this; it needs the parameter to ship upstream, after which the
keys start working here with no change.

## 4. Where the mechanisms are documented

This document records availability. The design behind each gate lives with its
component: [`transfer.md`](../design/transfer.md) for conditional writes,
copy-props and checksums, [`crt.md`](../design/crt.md) for the CRT engine and its
degradation, and [`opresult.md`](../design/opresult.md) for what `extra_info`
carries.
