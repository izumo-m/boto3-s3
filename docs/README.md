# boto3-s3 user guide

What the two packages promise and how to use them. For how they are built and
why, see the design documents indexed in [`overview.md`](../design/overview.md)
section 4.

## The command

[`cli/README.md`](./cli/README.md) — installing `boto3-s3-cli`, replacing
`aws s3` with it, and where to look things up.

- [`cli/exit-codes.md`](./cli/exit-codes.md) — the codes a script can branch on.
- [`cli/aws-differences.md`](./cli/aws-differences.md) — what parity covers,
  where behavior differs from `aws s3`, and the configuration files and
  environment variables the command reads.

What each option does is not repeated here: every option is an `aws s3` option
and `boto3-s3 <command> --help` describes it.

## Both packages

- [`compatibility.md`](./compatibility.md) — which feature needs which
  `botocore` / `s3transfer` / `awscrt`, and how an unavailable one behaves.

## The library

[`library/README.md`](./library/README.md) — installing `boto3-s3`, the shape of
the API, and where to look.

- [`library/s3-object.md`](./library/s3-object.md) — creating the `S3` object,
  choosing which client each location uses, threads, and subclassing.
- [`library/results.md`](./library/results.md) — `on_result`, the `OpResult`
  record, progress, dry runs, and cancellation.
- [`library/errors.md`](./library/errors.md) — the exception hierarchy, what is
  safe to depend on, and partial failure.
- [`library/sync.md`](./library/sync.md) — the three decisions `sync` makes, the
  default rule and its asymmetry, and what happens before the scan.
- [`library/sync-content.md`](./library/sync-content.md) — deciding updates by
  content instead of timestamps, and running those decisions in parallel.
- [`library/filters.md`](./library/filters.md) — `filter=`, glob patterns, and
  which key they match.
- [`library/transfer-options.md`](./library/transfer-options.md) — the options
  `cp` / `mv` / `sync` take, multipart tuning, and how warnings are counted.
- [`library/streams.md`](./library/streams.md) — reading and writing objects
  through a stream instead of a file.
- [`library/deleter.md`](./library/deleter.md) — `S3Deleter`, for driving batch
  deletion yourself.
- [`library/custom-storage.md`](./library/custom-storage.md) — writing a backend
  that can be one side of a transfer.
- [`library/logging.md`](./library/logging.md) — debug logging, and what
  credential masking does and does not guarantee.
