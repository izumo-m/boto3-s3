# boto3-s3-cli

[![PyPI](https://img.shields.io/pypi/v/boto3-s3-cli)](https://pypi.org/project/boto3-s3-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/boto3-s3-cli)](https://pypi.org/project/boto3-s3-cli/)
[![License](https://img.shields.io/pypi/l/boto3-s3-cli)](https://github.com/izumo-m/boto3-s3/blob/main/LICENSE)

An `aws s3`-compatible command line, built on the
[`boto3-s3`](https://pypi.org/project/boto3-s3/) library. The library is the
product and this command is a by-product of it: anyone who just wants the
`aws s3` command can use aws-cli itself. It can still help where aws-cli is
slow to start or too large to install (below).

```bash
pip install boto3-s3-cli
boto3-s3 sync ./build s3://my-bucket/build/ --delete
```

**Status:** beta (pre-1.0), preparing for 1.0. **Python:** 3.10+ · **License:** Apache-2.0

## Why use it

- **Fast startup.** In a representative Linux measurement, syncing a directory
  that contained one small file was about 1.7x faster than aws-cli v2.
- **Compact installation.** A default dependency-complete installation was
  about 31.5 MiB, compared with about 268 MiB for the corresponding aws-cli v2
  installation.
  More importantly, in Python and Lambda environments that already have boto3,
  adding `boto3-s3-cli` increased the installed package set by only about
  2.1 MiB in the measurement below.
- **Easy to add to AWS Lambda.** A ZIP-based Python Lambda function can include
  the package in its deployment ZIP or a Lambda layer. This avoids building or
  rebuilding a container image solely to add an `aws s3`-compatible command.

## What it is

`boto3-s3-cli` installs one command, **`boto3-s3`**, with every `aws s3`
subcommand:

```
cp   ls   mb   mv   presign   rb   rm   sync   website
```

It takes the `aws s3` arguments and global options, reads the same `~/.aws`
configuration, and does what `aws s3` does: the same transfers and `sync`
decisions, and on success the same result lines. Argument handling, resulting
S3 state, and exit codes are tested against `aws s3`. Exit codes carry the
meanings aws-cli gives them; the codes a script can branch on are listed in
[`docs/cli/exit-codes.md`](https://github.com/izumo-m/boto3-s3/blob/main/docs/cli/exit-codes.md).

Over the more permissive Python library, this CLI narrows the inputs to what
`aws s3` accepts and owns argument validation, output, and exit codes.
Compatibility is functional rather than full: when something fails, the
outcome follows `aws s3` (a failed item leaves the others running; a run that
cannot proceed stops before it starts). Messages `aws`'s own code writes read
the same, apart from a few `aws` words wrongly; an error's other details, such as which of two mistakes is reported
or the text botocore or Python writes, may differ. Deliberate
differences, such as batched deletes and credentials masked in debug output,
are listed in
[`docs/cli/aws-differences.md`](https://github.com/izumo-m/boto3-s3/blob/main/docs/cli/aws-differences.md);
there is no interactive auto-prompt.

## Packaging & startup

`boto3-s3-cli` is an ordinary Python package: it runs on the selected interpreter
and reuses a compatible boto3 / botocore installation rather than bundling its
own runtime. In contrast, the official aws-cli v2 installation is a
self-contained distribution that bundles its own runtime and dependencies
(installed once — it does not unpack itself on every invocation).

For a concrete comparison, the following measurement synced a directory
containing one 11 KiB file to an empty prefix of an S3 bucket in the same
region, from an m7i.xlarge EC2 instance (Ubuntu 26.04, Python 3.14). Each tool
ran as a fresh process 5 times, with a different destination for every run and
alternating execution order:

| Operation | `boto3-s3-cli` 0.8.0 | aws-cli 2.36.40 |
| --- | ---: | ---: |
| One-file `sync` (median) | 352 ms | 594 ms |

The measurement includes SDK setup, remote listing, and the actual upload; the
five runs spanned 339-363 ms and 582-600 ms respectively.

Installed package sizes were measured on an x86-64 Linux host with `du` using
Python 3.10 and the same pinned dependency versions, with bytecode caches
included:

| Installed packages | Size |
| --- | ---: |
| boto3 and its dependencies | 29.4 MiB |
| `boto3-s3-cli` and all default dependencies | 31.5 MiB |
| **Increase when boto3 is already installed** | **2.1 MiB** |
| aws-cli v2 self-contained installation | 268 MiB |

The two `boto3-s3` project wheels total about 430 KiB compressed. These figures
are illustrative rather than guarantees: startup time and size vary with the
Python version, platform, filesystem, dependency versions, installation method,
and S3 endpoint.

For Python Lambda functions deployed as ZIP archives, install the package and
its dependencies into the function archive or a
[Lambda layer](https://docs.aws.amazon.com/lambda/latest/dg/python-layers.html).
AWS recommends packaging all function dependencies, including boto3, to avoid
runtime dependency version mismatches. Even in that dependency-complete case,
the measured package set was about 31.5 MiB.

## Install

```bash
pip install boto3-s3-cli
```

Optional extra:

```bash
pip install "boto3-s3-cli[crt]"   # AWS Common Runtime transfer engine + CRT checksums
```

Installing the package also makes a `boto3_s3_cli` Python package importable,
but that is an implementation detail with no API guarantee: the supported
interface is the `boto3-s3` command. For a Python API, use the
[`boto3-s3`](https://pypi.org/project/boto3-s3/) library.

## Examples

```bash
# Copy (upload / download / S3-to-S3 — the route is inferred from the paths)
boto3-s3 cp ./report.csv s3://my-bucket/report.csv
boto3-s3 cp s3://my-bucket/report.csv ./report.csv
boto3-s3 cp ./build s3://my-bucket/build/ --recursive

# Sync a prefix onto disk, deleting local extras
boto3-s3 sync s3://my-bucket/site/ ./site --delete

# Include / exclude (last match wins)
boto3-s3 cp ./build s3://artifacts/ --recursive --exclude '*' --include '*.tar.gz'

# Preview without touching anything
boto3-s3 sync ./data s3://my-bucket/data/ --dryrun

# List
boto3-s3 ls s3://my-bucket/build/ --recursive --human-readable --summarize

# Delete everything under a prefix
boto3-s3 rm s3://my-bucket/tmp/ --recursive

# Buckets, a presigned URL, a static-site config
boto3-s3 mb s3://my-new-bucket
boto3-s3 presign s3://my-bucket/report.csv --expires-in 900
boto3-s3 website s3://my-bucket --index-document index.html
```

Streaming works through `-`, like `aws s3 cp`: `boto3-s3 cp - s3://b/key`
uploads stdin, and `boto3-s3 cp s3://b/key -` writes the object to stdout.

## Configuration

Credentials and connection settings come from the standard AWS sources, selected
with the same global flags as `aws s3` — `--profile`, `--region`,
`--endpoint-url` (e.g. for MinIO), `--no-sign-request`, `--no-verify-ssl`,
`--ca-bundle`, `--cli-read-timeout`, and `--cli-connect-timeout`. A global flag
may appear **before or after** the subcommand:

```bash
boto3-s3 --profile prod --region eu-west-1 ls s3://my-bucket
boto3-s3 ls s3://my-bucket --profile prod --region eu-west-1
```

Transfer tuning comes from the profile's `[s3]` section in `~/.aws/config`,
read as `aws s3` reads it (`max_concurrent_requests`, `multipart_threshold`,
`multipart_chunksize`, `max_queue_size`, `max_bandwidth`, `io_chunksize`,
`preferred_transfer_client`, plus the CRT-mode keys `target_bandwidth`,
`should_stream`, `disk_throughput`, and `direct_io`); botocore's own
`addressing_style`, `use_accelerate_endpoint`, `use_dualstack_endpoint` and
`payload_signing_enabled` are honored from the same section. One caveat:
`should_stream`, `disk_throughput` and `direct_io` are accepted and validated
but have no effect, because no released s3transfer takes the file-I/O options
that carry them (`aws` bundles a fork that does).

`--debug` turns on wire-level logging with credentials (signatures, access-key
ids, session tokens) masked by default.

What each key does, how the transfer engine is chosen, and the environment
variables read are in
[`configuration.md`](https://github.com/izumo-m/boto3-s3/blob/main/docs/cli/configuration.md).

Run `boto3-s3 <subcommand> help` for a subcommand's full option list.

## License

Apache-2.0. See
[`LICENSE`](https://github.com/izumo-m/boto3-s3/blob/main/LICENSE) in the main
repository.

Main project (source, issues): <https://github.com/izumo-m/boto3-s3>.
Report security vulnerabilities privately as described in
[`SECURITY.md`](https://github.com/izumo-m/boto3-s3/blob/main/SECURITY.md),
not on the public issue tracker.
