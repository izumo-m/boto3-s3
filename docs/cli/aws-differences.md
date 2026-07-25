# Differences from `aws s3`

Read this before you switch a script over. Most `aws s3` invocations behave
identically; the handful that do not are listed below, and one of them is
silent.

For what each option does, run `boto3-s3 <command> --help`; every option is an
`aws s3` option and is described there. For exit codes see
[`exit-codes.md`](./exit-codes.md), and for the configuration files, environment
variables and `[s3]` tuning keys the command reads see
[`configuration.md`](./configuration.md).

## 1. What you can rely on

Under the same arguments and configuration you get **the same resulting S3
state, the same returned values, the same error conditions, and the same exit
code**. A mismatch in any of those is a bug worth reporting.

**Do not parse the console output.** Error and warning wording, progress lines,
`--debug` traces and `--help` text differ from `aws` and change between
releases (the error prefix is `boto3-s3:`, not `aws:`). One exception is worth
knowing: parameter-validation errors do use aws's envelope,
`An error occurred (ParamValidation): <message>`, and `--cli-error-format` does
not change it.

Ordering is not reproducible either: with concurrent transfers, result lines —
and `delete:` lines against transfer lines — interleave freely, on either tool.

## 2. Behavior differences

Only the first can leave you with something wrong without saying so. The rest
are visible, or make no difference to the result.

- **Corrupted ranged downloads are not detected by the classic engine.** For a
  single-object download at or above the multipart threshold, `aws` verifies the
  reassembled object and fails on a mismatch; here such a corruption — one that
  got past TLS and TCP integrity — would be reported as a success. **If that
  matters to you, install the `crt` extra and set
  `preferred_transfer_client = crt`**, which validates exactly as `aws` does.
  Downloads below the threshold are verified on both tools, so only large
  single-object downloads are affected.
- **Default checksum algorithm.** Without `--checksum-algorithm`, uploads are
  integrity-checked with `CRC32`; `aws` v2 uses `CRC64NVME`. Both are valid and
  neither changes the result or the exit code. An explicit
  `--checksum-algorithm` makes the two agree.
- **`-h` / `--help`.** An option here; `aws` offers `help` as a subcommand.
  `boto3-s3 <command> help` also works.
- **Output back-pressure.** `aws` queues result lines without limit, so a stalled
  reader grows memory. Here the queue is bounded: a reader that falls far enough
  behind slows the transfer instead. No result line is ever dropped.
- **Progress repaint rate.** `aws` repaints on every transferred chunk; here
  repaints are floored at 0.1 s, or `--progress-frequency` when that is larger.
  The numbers painted are exact either way — only the cadence differs.
- **Interactive prompt.** `--cli-auto-prompt` needs the `autoprompt` extra, and
  its completions are not the same as aws's: values of every option that has a
  fixed choice list are completed (`aws` omits some), bucket and key names are
  not completed from the server, and shell completion is not provided. Nothing
  about the prompt is covered by parity.

Differences that depend on which dependencies are installed — the CRT engine,
CRT-family checksums, conditional writes, and more — are in
[`compatibility.md`](../compatibility.md).

## 3. Options that do nothing

`--output`, `--query`, `--no-paginate`, `--no-cli-pager`, `--color`,
`--cli-error-format` and `--cli-binary-format` are accepted for compatibility
and have no effect — `--help` groups them under `recognized but ignored`. Where
an option has a fixed choice list the value is still validated, so an invalid
one is still an error, and `--query` is still compiled as a JMESPath expression
and rejected if malformed.

## 4. Options limited by one direction

`cp` / `mv` / `sync` accept the same options on every route, but not every
option applies to every route, and — following `aws` — the treatment is not
uniform. These are rejected outright on the wrong route:

| Option | Valid route |
| --- | --- |
| `--checksum-algorithm` | upload, or S3-to-S3 copy |
| `--checksum-mode` | download |
| `--sse-c-copy-source`, `--sse-c-copy-source-key` | S3-to-S3 copy only |

Every other direction-specific option is accepted anywhere and simply has no
effect off its route. The write-side options in particular — `--acl`,
`--storage-class`, the content headers, `--metadata`, `--grants`, the SSE
family — are silently ignored on a download rather than rejected.

## 5. Filtering on Windows

`--exclude` / `--include` patterns match case-insensitively on Windows, matching
`aws`. A backslash in a pattern is treated as a separator there, so
`logs\*.txt` matches `logs/x.txt`; on Linux and macOS a backslash stays a
literal character. The `boto3-s3` Python library does not apply the
case-insensitive rule — that tightening belongs to this command.
