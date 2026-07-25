# Which `boto3` version each feature needs

**`boto3` 1.43.31 or newer has everything.** Four of the entries below
additionally need the `crt` extra.

If you cannot upgrade, the table says what each feature needs; below the version
listed, that feature is not supported. Check what you have with
`boto3-s3 --version`, or `python -c "import boto3; print(boto3.__version__)"`.

**Upgrading `boto3` is the one move that matters.** It pins `botocore` and
`s3transfer` to matching releases, so its version settles all three; raising
either of the other two on its own is not a supported move. There is no upper
bound, so a newer `boto3` is picked up as it is.

## 1. Features by required version

| Feature | Needs |
| --- | --- |
| the CRT transfer engine | boto3 >= 1.29.7, plus the `crt` extra |
| S3 Express directory buckets (a bucket name ending `--x-s3`) | boto3 >= 1.33.2 |
| `ls --bucket-name-prefix` / `--bucket-region` | boto3 >= 1.35.42 |
| `no_overwrite` / `--no-overwrite` on upload | boto3 >= 1.36.0 |
| `checksum_algorithm` = `CRC64NVME` | boto3 >= 1.36.0, plus the `crt` extra |
| `mb --tags` (`CreateBucketConfiguration.Tags`) | boto3 >= 1.39.2 |
| `no_overwrite` / `--no-overwrite` on copy | boto3 >= 1.41.0 |
| `[s3]` tuning reaching the CRT transfer manager | boto3 >= 1.42.0 |
| `mb` on an account-regional bucket (a name ending `-an`) | boto3 >= 1.42.67 |
| `checksum_algorithm` = `MD5` / `SHA512` | boto3 >= 1.42.94 |
| `checksum_algorithm` = the `XXHASH` family | boto3 >= 1.42.94, plus the `crt` extra |
| S3 object annotations (GA 2026-06) | boto3 >= 1.43.31 |
| `copy_props=ALL` / `--copy-props all` | boto3 >= 1.43.31 |
| `checksum_algorithm` = `CRC32C` | any supported boto3, plus the `crt` extra |
| the source ETag in `OpResult.extra_info` on a copy or download | boto3 >= 1.43.31 |

`CRC32`, `SHA1` and `SHA256` are available on every supported installation and
are not listed.

## 2. awscrt

`awscrt` is not a default dependency but an opt-in extra (`crt`). It gates two
independent things: the CRT transfer engine, and the checksum algorithms
botocore can only compute through it — **`CRC32C`, `CRC64NVME`, and the
`XXHASH` family**. The classic engine needs it for those too. `CRC32`, `SHA1`,
`SHA256` and `SHA512` are pure Python and never need it.

Missing awscrt fails only the features that need it, and nothing else changes.
The `boto3-s3` command reports 253 for those; `aws` v2 bundles awscrt, so the
situation cannot arise there (see [`exit-codes.md`](./cli/exit-codes.md)).

## 3. Three `[s3]` keys that never take effect

`should_stream`, `disk_throughput` and `direct_io` are accepted and validated
but do nothing, at every `boto3` version. See
[`cli/configuration.md`](./cli/configuration.md).

