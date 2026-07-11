# boto3-s3-cli

An `aws s3`-compatible command line, built on the
[`boto3-s3`](https://pypi.org/project/boto3-s3/) library.

```bash
pip install boto3-s3-cli
boto3-s3 sync ./build s3://my-bucket/build/ --delete
```

**Status:** early development (pre-1.0). **Python:** 3.10+ · **License:** Apache-2.0

## What it is

`boto3-s3-cli` installs one command, **`boto3-s3`**, designed as a
command-for-command replacement for `aws s3` with every subcommand:

```
cp   ls   mb   mv   presign   rb   rm   sync   website
```

It takes the `aws s3` arguments and global options, reads the same `~/.aws`
configuration, and treats an exit-code mismatch as a bug. Existing commands and
scripts can generally replace the `aws s3` prefix with `boto3-s3`; argument
handling, resulting S3 state, and exit codes are tested against `aws s3`.

This CLI is the strict compatibility layer over the more permissive Python
library. It applies aws-compatible path validation, configuration resolution,
transfer defaults, output behavior, and error handling. Human-readable wording
is not guaranteed to be byte-for-byte identical, and the interactive UI and a
few deliberately cleaned-up aws-cli edge-case failures are documented
exceptions.

## Packaging & startup

`boto3-s3-cli` is an ordinary Python package: it runs on the selected interpreter
and reuses a compatible boto3 / botocore installation rather than bundling its
own runtime. Its startup path lazy-loads the AWS SDK and command modules so
`--help`, `--version`, and pre-dispatch usage errors avoid imports they do not
need. Actual startup time depends on the interpreter, installation method,
machine, and credential/configuration environment and should be measured where
the command will run.

## Install

```bash
pip install boto3-s3-cli
```

Optional extras:

```bash
pip install "boto3-s3-cli[crt]"          # AWS Common Runtime transfer engine + CRT checksums
pip install "boto3-s3-cli[autoprompt]"   # --cli-auto-prompt interactive completion
```

With the autoprompt extra, run `boto3-s3 --cli-auto-prompt` to complete
arguments interactively as you type.

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

Transfer tuning is read from the profile's `[s3]` section in `~/.aws/config`
(`max_concurrent_requests`, `multipart_threshold`, `multipart_chunksize`,
`max_queue_size`, `max_bandwidth`, `io_chunksize`, `preferred_transfer_client`,
plus the CRT-mode keys `target_bandwidth`, `should_stream`, `disk_throughput`,
and `direct_io`), exactly as `aws s3` reads it.

`--debug` turns on wire-level logging with credentials (signatures, access-key
ids, session tokens) masked by default.

Run `boto3-s3 <command> --help` for a subcommand's full option list.

## License

Apache-2.0. See
[`LICENSE`](https://github.com/izumo-m/boto3-s3/blob/main/LICENSE) in the main
repository.

Main project (source, issues): <https://github.com/izumo-m/boto3-s3>.
