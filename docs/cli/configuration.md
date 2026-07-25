# Configuration

Where credentials, region and transfer tuning come from — the `~/.aws` files and
the environment. All of it resolves exactly as it does for `aws s3`, so an
environment already set up for `aws` needs no changes.

For what an individual option does, run `boto3-s3 <command> --help`.

The same global flags as `aws s3` override any of it, and a global flag may
appear **before or after** the subcommand.

## 1. Region, profile, and retries

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

## 2. Environment variables

Every standard AWS variable works, because credentials and endpoints resolve
through botocore exactly as they do for `aws` — `AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_SHARED_CREDENTIALS_FILE`,
`AWS_CA_BUNDLE`, `AWS_ENDPOINT_URL_S3` and the rest.

On top of those, the command reads `AWS_REGION`, `AWS_DEFAULT_REGION`,
`AWS_PROFILE`, `AWS_DEFAULT_PROFILE`, `AWS_CONFIG_FILE`, `AWS_RETRY_MODE`,
`AWS_MAX_ATTEMPTS`, `AWS_CLI_AUTO_PROMPT`, `AWS_CLI_FILE_ENCODING` and
`AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS` itself.

Two of those parse loosely rather than strictly.
`AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS` is honored only when it is literally
`true` (case-insensitively), and `AWS_CLI_AUTO_PROMPT` accepts `on` and
`on-partial` — anything else counts as off.

## 3. Transfer tuning: the `[s3]` section

`cp` / `mv` / `sync` read transfer settings from the profile's `[s3]` section in
`~/.aws/config`, exactly as `aws s3` reads it.

| Key | Effect |
| --- | --- |
| `max_concurrent_requests` | how many transfers run at once |
| `multipart_threshold` | the size at which an object is split |
| `multipart_chunksize` | the size of each part |
| `max_queue_size` | how many transfers may be queued ahead |
| `max_bandwidth` | an upper bound on transfer rate |
| `io_chunksize` | the read/write block size |
| `preferred_transfer_client` | which engine runs the transfer (below) |
| `target_bandwidth` | the CRT engine's throughput target |

botocore's own `addressing_style`, `use_accelerate_endpoint`,
`use_dualstack_endpoint` and `payload_signing_enabled` are honored from the same
section.

Three CRT-mode keys — `should_stream`, `disk_throughput` and `direct_io` — are
accepted and validated but **have no effect**, because no released `s3transfer`
takes the file-I/O options that carry them (`aws` bundles a fork that does).

An invalid value is a configuration error, exit code 255. It is reported after
usage errors, so a command that is also malformed reports that instead.

### Choosing the transfer engine

`preferred_transfer_client` takes `classic`, `crt`, or `auto` (`default` is an
alias for `classic`). There is no command-line option for it — the config key is
the only way to set it, as in `aws s3`.

- `classic` — the `s3transfer` engine.
- `crt` — the AWS Common Runtime engine. Requires the `crt` extra **and** an
  `s3transfer` new enough to expose it; if either is missing the command fails
  rather than falling back silently.
- `auto` — CRT when the `crt` extra is installed, your `s3transfer` supports it,
  and the machine is one the CRT is optimized for. Otherwise classic, and always
  quietly. Only one process per host drives the CRT engine at a time, so a
  concurrent run also falls back.

S3-to-S3 copies always use the classic engine, whatever this is set to.
Which features need which versions is in
[`compatibility.md`](../compatibility.md).

To see which engine a run actually used, pass `--debug`. Set
`preferred_transfer_client = crt` when you need to be sure: that form fails
rather than falling back.

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
