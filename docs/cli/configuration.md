# Configuration

Where credentials, region and transfer tuning come from — the `~/.aws` files and
the environment. All of it resolves exactly as it does for `aws s3`, so an
environment already set up for `aws` needs no changes.

For what an individual option does, run `boto3-s3 <subcommand> help`.

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
`AWS_CA_BUNDLE`, `AWS_ENDPOINT_URL_S3` and the rest. The proxy variables
(`HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`) work the same way, down to the wire:
an HTTPS proxy is opened with the same `CONNECT` request `aws` sends, on
whichever Python this command runs.

On top of those, the command reads `AWS_REGION`, `AWS_DEFAULT_REGION`,
`AWS_PROFILE`, `AWS_DEFAULT_PROFILE`, `AWS_CONFIG_FILE`, `AWS_RETRY_MODE`,
`AWS_MAX_ATTEMPTS`, `AWS_CLI_AUTO_PROMPT`, `AWS_CLI_FILE_ENCODING`,
`AWS_CLI_OUTPUT_ENCODING` and `AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS` itself.

The two encoding variables name codecs. `AWS_CLI_FILE_ENCODING` is the codec a
`file://` paramfile is read with (section 7); `AWS_CLI_OUTPUT_ENCODING` is the
one an error report is written with, and reaches nothing else — result lines
and warnings keep the stream's own codec, as they do under `aws`. A codec
Python does not know is a configuration error, exit code 255, on either
variable. With `AWS_CLI_OUTPUT_ENCODING` unset, `PYTHONUTF8=1` selects UTF-8
for that same report — aws's own compatibility fallback, matched here.

Two of those parse loosely rather than strictly.
`AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS` is honored only when it is literally
`true` (case-insensitively), and `AWS_CLI_AUTO_PROMPT` accepts `on` and
`on-partial` — anything else counts as off.

Three variables the installed SDK would otherwise act on are ignored here,
because `aws` cannot see them either: `SSLKEYLOGFILE` (its frozen interpreter
ignores the environment for that), `BOTO_DISABLE_CRT` (a switch its bundled SDK
does not have) and `AWS_S3_US_EAST_1_REGIONAL_ENDPOINT`, along with the
matching `us_east_1_regional_endpoint` config key (dropped from its bundled SDK
— us-east-1 is regional there, always). Setting any of them changes nothing
here, an unusable value included.

`AWS_PAGER` and the `cli_pager` config key change nothing on either tool's `s3`
commands: `aws` routes only the structured output of its API commands through a
pager, and the `s3` commands produce none. Setting one is inert here for the
same reason it is inert there, which is why `--no-cli-pager` is among the
options accepted and ignored
(see [`aws-differences.md`](./aws-differences.md)).

One section of `~/.aws/config` is not read at all: `[plugins]`, from which
`aws` imports and initializes aws-cli plugins on every invocation. This command
has no plugin mechanism, so the section is inert — including the case where an
entry cannot be imported, which stops `aws` before it does anything and does not
stop this command (see [`aws-differences.md`](./aws-differences.md)).

## 3. Transfer tuning: the `[s3]` section

`cp` / `mv` / `sync` read transfer settings from the profile's `[s3]` section in
`~/.aws/config`, exactly as `aws s3` reads it.

`rm` reads and validates the same section, and builds the transfer engine
`preferred_transfer_client` names, because `aws s3 rm` does — so an unusable
value fails identically. But only that one key reaches anything here: this
command's deletes never ride the transfer engine (see
[`aws-differences.md`](./aws-differences.md)), so the numeric keys below tune
nothing about `rm`, where under `aws` they do.

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
`--quiet` does not hide it — the report comes from the error handler, not from
the result lines.

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

## 4. `cli_timestamp_format`

The profile key `cli_timestamp_format` takes `wire` or `iso8601`, exactly as
`aws` accepts it. Neither value changes anything this command prints — the
listing timestamps of `ls` are rendered the same way either way, as they are
under `aws s3`.

What it does do is fail the run when the value is neither: exit code 253, ahead
of the command itself — a `help` page included. That matches `aws`, where the
check happens as the session comes up.

The value is read from the selected profile only — a `[profile x]` setting
applies under `--profile x` or `AWS_PROFILE=x`, not otherwise — and the shared
credentials file is read for it too, winning over `~/.aws/config`.

## 5. Aliases: `~/.aws/cli/alias`

The `[command s3]` section of `~/.aws/cli/alias` — the same file, at the same
fixed path, that `aws` reads (`AWS_CONFIG_FILE` does not move it) — declares
extra subcommands:

```ini
[command s3]
lsr = ls --recursive
recent = !sh -c 'boto3-s3 ls "$1" | sort | tail' sh
```

`boto3-s3 lsr s3://bucket` then runs `ls --recursive s3://bucket`, and
`boto3-s3 recent s3://bucket` runs the shell command with `s3://bucket`
appended. Because this CLI *is* `aws s3`, `[command s3]` is the section that
applies; `[toplevel]`, whose entries name services, has no counterpart here and
is ignored.

The section header must be spelled `command` followed by a single ASCII space,
as `aws` requires. Extra spaces are fine (`[command  s3]` is the same section),
but any other whitespace — a tab, a space before `command` — makes the section
declare nothing at all, on either tool.

- A value that starts with `!` is a **shell command line**. The invocation's
  remaining arguments are appended (quoted, so a space inside one is safe) and
  the command's exit status becomes this command's.
- Any other value is **CLI arguments**. They are split with shell quoting rules
  and placed ahead of what was typed, then parsed again — so an alias may
  expand to another alias, and a global option in the value (say `--region`)
  applies to the run, overriding one typed on the command line. `--debug` and
  `--profile` are refused there, as they are under `aws`.
- An alias named after a built-in subcommand **replaces** it and proxies to it,
  dropping the first word of the expansion: `ls = ls --recursive` makes every
  `ls` recursive.

A file that is not valid INI aborts the run with exit code 255 before anything
else, `--version` and `help` included — again matching `aws`.

## 6. Cached temporary credentials

Credentials fetched for an `assume_role` / web-identity / SSO profile are cached
in `~/.aws/cli/cache`, aws's own directory and file format, so consecutive
commands do not repeat the `AssumeRole` call and an `mfa_serial` profile asks
for a code once rather than on every invocation. The cache is shared with
`aws`: either command reuses what the other fetched.

## 7. Reading a value from a file

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
