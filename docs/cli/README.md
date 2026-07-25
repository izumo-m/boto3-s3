# The `boto3-s3` command

`boto3-s3-cli` installs one command, **`boto3-s3`**, built to be a
command-for-command replacement for `aws s3`. It implements every subcommand:

```
cp   ls   mb   mv   presign   rb   rm   sync   website
```

It takes the same arguments and global options, reads the same `~/.aws`
configuration, and returns the same exit codes. In most cases an existing script
works by replacing the `aws s3` prefix with `boto3-s3`.

It runs on your own Python interpreter and reuses your installed boto3, so it is
small enough to drop into a Lambda deployment package. The
[package page](https://pypi.org/project/boto3-s3-cli/) carries the size and
startup measurements.

## 1. Installing

```bash
pip install boto3-s3-cli
```

Two optional extras:

```bash
pip install "boto3-s3-cli[crt]"          # the AWS Common Runtime transfer engine
                                         # and the CRT-family checksums
pip install "boto3-s3-cli[autoprompt]"   # --cli-auto-prompt interactive completion
```

The `crt` extra does two things: it enables the CRT transfer engine, and it lets
the classic engine compute the CRT-family checksum algorithms (`CRC64NVME`,
`CRC32C`, the `XXHASH` family). Installing it therefore also changes which
engine runs by default, since `preferred_transfer_client = auto` can then
resolve to CRT.

## 2. Replacing `aws s3`

```bash
boto3-s3 cp ./report.csv s3://my-bucket/report.csv
boto3-s3 cp s3://my-bucket/report.csv ./report.csv
boto3-s3 cp ./build s3://my-bucket/build/ --recursive

boto3-s3 sync ./site s3://my-bucket/site/ --delete
boto3-s3 sync s3://my-bucket/site/ ./site --dryrun

boto3-s3 ls s3://my-bucket/build/ --recursive --human-readable --summarize
boto3-s3 rm s3://my-bucket/tmp/ --recursive
boto3-s3 presign s3://my-bucket/report.csv --expires-in 900
```

For `cp` / `mv` / `sync` the direction is inferred from the two paths, exactly as
`aws s3` infers it. Streaming through `-` works the same way:
`boto3-s3 cp - s3://bucket/key` uploads standard input, and
`boto3-s3 cp s3://bucket/key -` writes the object to standard output.

A global option may sit on either side of the subcommand:

```bash
boto3-s3 --profile prod --region eu-west-1 ls s3://my-bucket
boto3-s3 ls s3://my-bucket --profile prod --region eu-west-1
```

Before switching a script over, read
[`aws-differences.md`](./aws-differences.md) — the behavior differences are few
but some are silent.

## 3. Finding things

- **What an option does** — `boto3-s3 <command> --help`. Every option is
  described there, including which of them are accepted but ignored.
- **What an exit code means** — [`exit-codes.md`](./exit-codes.md).
- **Where this differs from `aws s3`** — [`aws-differences.md`](./aws-differences.md).
- **The configuration files, environment variables and `[s3]` tuning keys it
  reads** — [`configuration.md`](./configuration.md).
- **What changes with the installed dependencies** —
  [`compatibility.md`](../compatibility.md).

## 4. What is not part of the interface

Installing the package also makes a `boto3_s3_cli` Python package importable,
but that is an implementation detail with no API guarantee. The supported
interface is the `boto3-s3` command.

For a Python API, use the [`boto3-s3`](https://pypi.org/project/boto3-s3/)
library instead. It is not a wrapper around this command — the command is a
layer on top of the library — so calling it does not start a subprocess or
require this package at all.
