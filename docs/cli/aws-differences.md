# Differences from `aws s3`, and configuration

`boto3-s3` aims to be a command-for-command replacement for `aws s3`. This page
records where that aim stops — what parity covers, where behavior deliberately
differs — and the configuration inputs that are not command-line options and so
have no `--help` entry.

For what each option does, run `boto3-s3 <command> --help`; every option is an
`aws s3` option and is described there. For exit codes see
[`exit-codes.md`](./exit-codes.md).

## 1. What parity covers

Parity is defined on **the resulting S3 state, the values returned, the error
conditions, and the exit code**, under the same arguments and configuration. An
exit-code mismatch is treated as a bug.

Parity is **not** defined on console output. Error and warning wording, progress
lines, `--debug` traces and `--help` text may differ from `aws`, and may change
between `boto3-s3-cli` releases. Do not parse them. Two specifics:

- The program name differs, so usage lines and the `boto3-s3: [ERROR]:` prefix
  differ from `aws: [ERROR]:` by construction.
- Parameter-validation errors do use aws's default envelope,
  `An error occurred (ParamValidation): <message>`. Selecting a different
  `--cli-error-format` does not change the rendering.

Ordering is not guaranteed either: with concurrent transfers, the order of
result lines — and of `delete:` lines against transfer lines — is not
reproducible, on either tool.

## 2. Behavior differences

- **Default checksum algorithm.** Without `--checksum-algorithm`, uploads are
  integrity-checked with `CRC32`; `aws` v2 uses `CRC64NVME`. Both are valid and
  neither changes the result or the exit code. An explicit
  `--checksum-algorithm` makes the two agree.
- **Corrupted ranged downloads are not detected.** For a single-object download
  at or above the multipart threshold, `aws` recombines a per-range checksum and
  fails the download if the assembled object does not match. That check lives in
  a variant of s3transfer that is not published, so a corruption that got past
  TLS and TCP integrity would be reported here as a success. Every neighbouring
  path is unaffected: non-ranged downloads are verified by botocore on both
  sides, and the CRT engine's own validation is identical by construction.
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

## 3. Configuration files and environment variables

Credentials and connection settings come from the standard AWS sources, chosen
with the same global flags as `aws s3`. A global flag may appear **before or
after** the subcommand.

Resolution order matches `aws` v2, which is not the same as plain boto3's — so a
Python script using boto3 directly may pick a different profile or region than
this command does from the same environment.

- **Region**: `--region`, then `AWS_REGION`, then `AWS_DEFAULT_REGION`, then the
  profile's `region`, then the EC2 instance metadata service. The environment
  variables win by being **present**, so `AWS_REGION=` selects the empty region
  rather than falling through.
- **Profile**: `--profile`, then `AWS_PROFILE`, then `AWS_DEFAULT_PROFILE`, then
  `default`. Present wins here too, so `AWS_PROFILE=` fails with a
  profile-not-found error.
- **Retries**: `standard` mode with 3 attempts, unless `AWS_RETRY_MODE` /
  `AWS_MAX_ATTEMPTS` or the profile supplies one. Only `standard` and `adaptive`
  are accepted; `legacy` is rejected, as `aws` v2 rejects it.

The environment variables read are `AWS_REGION`, `AWS_DEFAULT_REGION`,
`AWS_PROFILE`, `AWS_DEFAULT_PROFILE`, `AWS_CONFIG_FILE`, `AWS_RETRY_MODE`,
`AWS_MAX_ATTEMPTS`, `AWS_CLI_AUTO_PROMPT`, `AWS_CLI_FILE_ENCODING`, and
`AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS`. The last is honored only when it is
literally `true` (case-insensitively); `AWS_CLI_AUTO_PROMPT` accepts `on` and
`on-partial`, and anything else counts as off.

Transfer tuning comes from the profile's `[s3]` section in `~/.aws/config`; the
keys are listed in the package README, and three of them are accepted but inert
(see [`compatibility.md`](../compatibility.md)).

## 4. Reading a value from a file

Any option or path that takes a single string can be given as `file://path`
(read as text) or `fileb://path` (read as bytes), resolved before the command
runs. This covers the `<S3Uri>` positional of `ls` / `rm` / `website` / `mb` /
`rb` / `presign` and the free-string options of `cp` / `mv` / `sync`.

Two exclusions are worth knowing:

- **List-valued options are not expanded** — `--exclude`, `--include`, and
  `mb --tags` keep the text verbatim.
- **Options with a fixed choice list cannot use it**, because the value is
  rejected as an invalid choice first.

A file that cannot be read, or a binary file given to the text `file://` form,
is a usage error. `fileb://` yields raw bytes only for `--sse-c-key` and
`--sse-c-copy-source-key`; those two keys are never base64-decoded, matching
`aws`.

## 5. Options that do nothing

`--output`, `--query`, `--no-paginate`, `--no-cli-pager`, `--color`,
`--cli-error-format` and `--cli-binary-format` are accepted for compatibility
and have no effect — `--help` groups them under `recognized but ignored`. Where
an option has a fixed choice list the value is still validated, so an invalid
one is still an error, and `--query` is still compiled as a JMESPath expression
and rejected if malformed.

## 6. Options limited by one direction

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

## 7. Filtering on Windows

`--exclude` / `--include` patterns match case-insensitively on Windows, matching
`aws`. A backslash in a pattern is treated as a separator there, so
`logs\*.txt` matches `logs/x.txt`; on Linux and macOS a backslash stays a
literal character. The `boto3-s3` Python library does not apply the
case-insensitive rule — that tightening belongs to this command.
