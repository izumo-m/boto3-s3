# Exit codes

`boto3-s3` uses the same exit codes as `aws s3`, with the same meanings, so a
script that already branches on `aws s3`'s codes keeps working unchanged. How
the command produces them is recorded in
[`../../design/cli.md`](../../design/cli.md) section 6.

## 1. The codes

| code | what it means |
| --- | --- |
| 0 | The command succeeded. `help` and `--version` also exit 0. |
| 1 | The operation failed after it started. |
| 2 | A transfer finished with warnings but no failures. |
| 130 | Interrupted with Ctrl-C outside a running transfer or delete. |
| 252 | The command line was rejected. |
| 253 | The environment cannot supply what the command needs. |
| 254 | A request reached S3 and S3 returned an error. |
| 255 | Any other error. |

### 0 — success

`help` and `--version` exit 0 as well. Output that cannot be written is not a
success: piping into a reader that closes early (`ls | head`) reports the
broken pipe like any other write failure — `boto3-s3: [ERROR]: [Errno 32]
Broken pipe`, exit code 255 — and so does a command whose work succeeded but
whose output stdout could not take (a full disk, a reader gone): the work is
done, the exit code is 255 with the error. With stdout closed (`1>&-`) nothing
is written and the code is the work's.

### 1 — the operation failed after it started

Once `cp` / `mv` / `rm` / `sync` / `mb` / `rb` has begun work, a failure ends
at 1 — **including errors S3 itself returned** — except a failed object
deletion under `rb --force`, which is 255 (section 2). The transfer commands
deliberately do not report 254 once running, matching `aws s3`. Per-item
failures were already printed as `... failed:` lines while the run proceeded;
a failure that stops the whole run prints one `fatal error:` line. A download
to stdout that stdout cannot take — full, or its reader gone, as in `cp
s3://bucket/key - | head -c1` on an object larger than the pipe holds — is
such a failure: `download failed: … [Errno 32] Broken pipe`.

Ctrl-C during a running transfer or delete is also 1, with a
`cancelled: ctrl-c received` line. `rb --force` is the
exception: Ctrl-C during its listing or its deletions is 255, the code of a
deletion that fails there (section 2), and during the bucket removal itself
130 (below).

`ls` uses 1 for its own "nothing to show" case: a key or prefix that matched no
entries.

### 2 — completed, with warnings

A transfer command reached the end with warnings but no failures: objects
skipped because they are archived, a local file that could not be read, a
modification time that could not be stamped onto a downloaded file. Everything
else transferred normally. Warnings count toward this code, failures do not —
if anything failed, the code is 1.

### 130 — Ctrl-C outside a running transfer or delete

Ctrl-C before a `cp` / `mv` / `rm` / `sync` run has started, or while `ls`,
`mb`, `website`, `presign` or `rb` is at work — a request already in flight
included, and for `rb --force` once its objects are deleted — exits 130. Once a transfer or delete is running,
Ctrl-C reports 1 instead (above). After the run has ended, while its result
lines are still being written (a slow terminal or pipe), Ctrl-C lets every
line out and then exits 130, as `aws` does; a second Ctrl-C stops the
remaining lines.

### 252 — the command line was rejected

An unknown option, an invalid choice or value, a path that is not a usable S3
URI, or an option that does not apply to the direction being run (for example
`--checksum-algorithm` on a download). No transfer request was sent.

### 253 — the environment cannot supply what is needed

Credentials or a region could not be resolved, or a requested feature needs a
dependency that is not installed — asking for the CRT transfer engine without
the `crt` extra is the common case — or one that is installed but too old to
carry it ([`compatibility.md`](../compatibility.md) lists what each feature
needs). Not every such case lands here:

- a CRT-only `--checksum-algorithm` without the extra fails per item (1);
- `--copy-props all`, a `--checksum-algorithm` newer than the SDK, or a `cp` /
  `mv` / `rm` on an S3 Express bucket or access point the SDK predates ends the
  run with one `fatal error:` line (1) — `sync`, and `--case-conflict skip` /
  `error` on a recursive download, refuse them at every SDK version (252);
- `mb` and `rb` report a too-old SDK — `mb`'s `--tags` or `-an` bucket name, an
  S3 Express bucket or access point — on their one `make_bucket failed:` /
  `remove_bucket failed:` line (1) like any of their failures, and `rb --force`
  as its failed delete (255);
- `--no-overwrite` on an upload or copy with a too-old SDK is rejected up front
  as a usage error (252).

Of the S3 Express refusals, then, only `ls`, `presign` and `website` land here.
An unusable `cli_timestamp_format` in the profile lands here too: every
subcommand stops on it. `help` and `--version` do not read the config files,
so they still work, where `aws` fails them as well
([`configuration.md`](./configuration.md)).

Once a `cp` / `mv` / `sync` / `rm` run has started, a credential
failure is exit code 1 instead: a per-item
`upload failed:` / `move failed:` / `delete failed:` line when it lands on an
upload or a delete, a per-item `download failed:` line on a download to stdout
(`cp s3://… -`, which starts with no lookup of its own), and a whole-run
`fatal error:` line when it lands on the lookup a download to a file or an
S3-to-S3 copy starts with (the source's `HeadObject`) or on the S3 listing a
recursive run starts with (`sync`, `rm --recursive`). `aws` reports all three
the same way.

### 254 — S3 returned an error

The command was valid, a request went out, and the service rejected it. `ls`
and `website` report service errors this way. The transfer commands and
`mb` / `rb` fold service errors into 1 instead, as described above, so 254 is
narrower than it first looks.

### 255 — everything else

A network or local I/O failure, a missing local source, a `--profile` that does
not exist or is incomplete, a non-integer value for an option that takes a
number, or an unexpected internal error. `aws` documents 255 as a catch-all
whose specific cases may narrow over time; treat it the same way and do not
branch on it to identify a particular failure.

A config file that cannot be parsed is 255 as well — `~/.aws/config` (or
`AWS_CONFIG_FILE`) or `~/.aws/credentials` (or `AWS_SHARED_CREDENTIALS_FILE`)
— and so are an unusable `cli_binary_format` in the profile and an unusable
instance-metadata setting (`AWS_METADATA_SERVICE_TIMEOUT=abc`). Every
subcommand stops on them. `help` and `--version` do not read the config
files, so they still work, where `aws` fails them with 255 too.

## 2. Which code a failure gets

**Once a transfer command has started, every failure is 1.** `cp` / `mv` / `rm`
/ `sync` report 1 after they begin — including errors S3 itself returned. That
is why 254 is narrower than it looks. A usage error caught *before* the
operation starts is still 252. An item whose request fails before any
response arrives — its connection closed, its read timed out, a header value
the HTTP layer refuses to send (a line break in `--cache-control`, say) —
counts too: it prints its failure line and the run ends at 1, where `aws`
often prints nothing for it and ends at 0
([`aws-differences.md`](./aws-differences.md)). S3 may still have acted on
such a request before the response was lost.

**Before that, whether the request reached S3 decides the code.** Anything the
service answered is 254, whatever kind of error it is — a 400 from S3 is 254,
not 252, because the request went out. Errors that never left your machine are
classified by kind: 252, 253 or 255.

A few codes are not the ones you would guess:

| You run | You get | Because |
| --- | --- | --- |
| a non-integer `--page-size`, `--expires-in` or `--progress-frequency` | 255 | the number is converted after the command line is parsed, as on `aws s3` |
| `cp --expected-size` with a non-integer | 1 when uploading a stream (0 under `--dryrun`), 0 otherwise | the value is read only on the streaming route, and never on a dry run |
| `rb --force` whose object deletion fails | 255 | the bucket removal never runs |
| `mv --validate-same-s3-paths` whose lookup fails | 254 if the service answered, 255 if it could not be reached | the check calls out before the move begins |

Only what each code means is promised. When two things are wrong at once,
which one is reported - and so which code you get - may differ from
`aws s3`, so a script should not depend on it.

## 3. Where this differs from `aws s3`

Two cases where the codes are deliberately not identical:

- **The CRT transfer engine without the `crt` extra** exits 253; a CRT-only
  `--checksum-algorithm` without it fails per item at exit code 1 instead.
  `aws` v2 bundles awscrt, so neither situation can arise there.
- **A `file://` or `fileb://` reference that `aws` mishandles** exits 252:
  a `fileb://` path argument (on `aws`, 252, 255 or an `rm` that deletes
  the decoded path, by command), either prefix as `website`'s bucket, a
  whole `--metadata fileb://...` (255 on `aws`), and a single `--grants`
  value with either prefix (on `aws`, 1 in flight, or 0 for an empty file).

A few other paths settle on different codes too. Section 1 of
[`aws-differences.md`](./aws-differences.md) lists every run that comes out
differently, and its section 2 has each of them, with why it is not
mirrored.

[`compatibility.md`](../compatibility.md) covers what else changes with the
installed dependencies.
