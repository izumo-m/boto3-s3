# Version-dependent feature availability

[`overview.md`](./overview.md) section 2 states the policy: the supported SDK
floor is roughly three years old, and the installed SDK decides which features
are available. This document is where that mapping lives - which feature needs
which `botocore` / `s3transfer` / `awscrt`, and what happens below it.

The rule behind every entry is the same: boto3-s3 does not emulate newer AWS
behavior on an older SDK. A feature that depends on a newer S3 model is simply
unavailable below the version that introduced it.

In practice the choice is coarser than the tables suggest. `boto3` pins
`s3transfer` to a single minor (1.43.44 requires `>=0.19.0,<0.20.0`) and
`botocore` to a matching release, so the installed boto3 fixes the other two;
raising s3transfer alone is not a supported move. boto3-s3 itself declares only
floors and no ceiling, and gates on capability rather than version number
(`hasattr` / signature checks), so a newer SDK starts being used with no code
change here.

## 1. How an unavailable feature behaves

Three shapes, chosen per feature so the caller is never silently wrong:

- **Refused up front** - a `ConfigurationError` (CLI: rc 252 / 253) before any
  request. Used when proceeding would silently write the wrong thing.
- **Clean `ValidationError`** - botocore rejects the request parameter
  client-side and the error is translated at the API boundary.
- **Silently inert** - the operation still works, the newer refinement just does
  not apply. Used only where the coarser result is still correct.

## 2. botocore-gated

| Feature | Needs | Below it |
| --- | --- | --- |
| `no_overwrite` / `--no-overwrite` on upload | botocore >= 1.35.16 (plus s3transfer, below) | refused up front |
| `no_overwrite` / `--no-overwrite` on copy | botocore >= 1.41.0 | refused up front |
| `checksum_algorithm` beyond `CRC32` / `SHA1` / `SHA256` | a botocore that registers it | unavailable; the CRT-backed ones need awscrt as well |
| `ls --bucket-name-prefix` / `--bucket-region` | paginated `ListBuckets` (botocore 1.34.162) | silently inert, on an unpaginated `ListBuckets` |
| the same two filters' `Prefix` / `BucketRegion` parameters | botocore >= 1.35.42 | `ValidationError` on 1.34.162 through 1.35.41 |
| `mb --tags` (`CreateBucketConfiguration.Tags`) and the `-an` namespace bucket's `BucketNamespace` | a botocore modelling them | `ValidationError` |
| S3 object annotations (GA 2026-06) | a botocore with the annotations model | copies stop sending `AnnotationDirective=EXCLUDE`, behaving like pre-annotations aws-cli |
| `copy_props=ALL` / `--copy-props all` | botocore >= 1.43.31 and s3transfer >= 0.19 | refused up front |
| S3 Express directory buckets (`--x-s3`) | a botocore with their endpoint rules and `v4-s3express` signing | requests against them fail |

## 3. s3transfer-gated

| Feature | Needs | Below it |
| --- | --- | --- |
| `no_overwrite` / `--no-overwrite` on upload | s3transfer >= 0.11.0 (its create-multipart blocklist) | refused up front |
| the source ETag in `OpResult.extra_info` | a future meta with `provide_object_etag` | `{"ETag": ...}` is `None` unless `capture_response=True` supplies it from the captured response; s3transfer's own `CopySourceIfMatch` consistency pin on copies is absent too |
| the CRT transfer engine at all | s3transfer >= 0.8.0 (the CRT lock and credentials surface) | an explicit `preferred_transfer_client = crt` is refused up front; `auto` falls back to classic silently |
| `[s3]` tuning reaching the CRT manager | s3transfer >= 0.16.0 (`CRTTransferManager`'s `config` kwarg) | manager-level tuning is dropped with boto3's own warning, while `part_size` and `target_throughput` still reach the CRT client directly |

## 4. awscrt

`awscrt` is not a default dependency but an opt-in extra (`crt`). It gates two
independent things - the CRT transfer engine, and the CRT-family checksum
algorithms, which the classic engine needs it for too. On an installation
without it only the relevant features fail, and that does not count as an exit
code mismatch (see [`overview.md`](./overview.md) section 3). The exit code
charter does apply once the CRT stack is usable: awscrt present and, for the
transfer engine, an s3transfer with the CRT surface.

## 5. Not a version gap: the aws-cli s3transfer fork

The `[s3]` file-I/O keys `should_stream` / `disk_throughput` / `direct_io` are
parsed and validated but have no effect, because no released pip `s3transfer`
takes them - `create_s3_crt_client` has no `fio_options` parameter, still absent
at 0.19.0. aws-cli v2 is a self-contained distribution bundling a fork that does
have it, so `aws` honors these three where a pip install cannot. No newer pip
release fixes this; it needs the parameter to ship upstream, after which the
keys start working here with no code change (the call site checks the
signature).

## 6. Where the mechanisms are documented

This document records availability. The design behind each gate lives with its
component: [`transfer.md`](./transfer.md) for conditional writes, copy-props and
checksums, [`crt.md`](./crt.md) for the CRT engine and its degradation, and
[`opresult.md`](./opresult.md) for what `extra_info` carries.
