# boto3-s3-cli

An `aws s3`-compatible command line, built on the
[`boto3-s3`](https://pypi.org/project/boto3-s3/) library.

```bash
pip install boto3-s3-cli
boto3-s3 sync ./build s3://my-bucket/build/ --delete
```

**Status:** early development (pre-1.0). **Python:** 3.10+ · **License:** Apache-2.0

## What it is

`boto3-s3-cli` installs one command, **`boto3-s3`**, a drop-in for `aws s3` with
every subcommand:

```
cp   ls   mb   mv   presign   rb   rm   sync   website
```

It takes the same arguments and global options as `aws s3`, reads the same
`~/.aws` configuration, and returns the same exit codes — so existing commands
and scripts keep working: just swap `aws s3` for `boto3-s3`. Argument handling,
configuration, and exit codes are tested for parity with `aws s3`.

## Footprint & startup

`boto3-s3-cli` is an ordinary Python package: it runs on the interpreter you
already have and reuses your boto3 / botocore install rather than bundling its
own. aws-cli v2 ships as a self-contained install — its own embedded Python plus a
private copy of the SDK — and pays that bootstrap on every invocation.

The startup gap is large. Against a local MinIO, listing a missing bucket (so the
request itself is negligible) and printing `--version`, on one Linux machine:

| Command                  | aws-cli v2 | boto3-s3-cli |
| ------------------------ | ---------- | ------------ |
| `ls` of a missing bucket | ~670 ms    | ~250 ms      |
| `--version`              | ~580 ms    | ~65 ms       |

Same result, same exit code — boto3-s3-cli just skips the bundle's bootstrap and
lazy-loads only what the subcommand needs. (Figures are from one environment and
will vary; the ratio is what's representative.)

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
`--ca-bundle`, and the timeout flags. A global flag may appear **before or
after** the subcommand:

```bash
boto3-s3 --profile prod --region eu-west-1 ls s3://my-bucket
boto3-s3 ls s3://my-bucket --profile prod --region eu-west-1
```

Transfer tuning is read from the profile's `[s3]` section in `~/.aws/config`
(`max_concurrent_requests`, `multipart_threshold`, `multipart_chunksize`,
`max_queue_size`, `max_bandwidth`, `io_chunksize`, `preferred_transfer_client`),
exactly as `aws s3` reads it.

`--debug` turns on wire-level logging with credentials (signatures, access-key
ids, session tokens) masked by default.

Run `boto3-s3 <command> --help` for a subcommand's full option list.

## Exit codes

For scripting, `boto3-s3` returns the same codes as `aws s3`:

| Code | Meaning |
| --- | --- |
| `0` | Success. |
| `1` | A transfer or delete failed, or a lookup matched nothing (e.g. `ls` of a key/prefix that doesn't exist). |
| `2` | Completed with warnings only (e.g. a skipped Glacier object). |
| `252` | Usage / argument error. |
| `253` | Configuration error (credentials, region). |
| `254` | Server-side error. |
| `255` | Other general error (e.g. a non-integer value for a numeric option). |

## License

Apache-2.0. See
[`LICENSE`](https://github.com/izumo-m/boto3-s3/blob/main/LICENSE) in the main
repository.

Main project (source, issues): <https://github.com/izumo-m/boto3-s3>.
