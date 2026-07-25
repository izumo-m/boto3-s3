# Exit codes

`boto3-s3` uses the same exit codes as `aws s3`, with the same meanings, so a
script that already branches on `aws s3`'s codes keeps working unchanged. The
table below is the contract; the exceptions are listed at the end.

## 1. The codes

| code | what it means |
| --- | --- |
| 0 | The command succeeded. `--help` and `--version` also exit 0. |
| 1 | The operation failed after it started. |
| 2 | A transfer finished with warnings but no failures. |
| 130 | Interrupted with Ctrl-C before the operation started. |
| 252 | The command line was rejected. Nothing was sent. |
| 253 | The environment cannot supply what the command needs. |
| 254 | A request reached S3 and S3 returned an error. |
| 255 | Any other error. |

### 0 — success

`--help` and `--version` exit 0 as well, as does a run whose output pipe was
closed early by the reader (`| head`).

### 1 — the operation failed after it started

Once `cp` / `mv` / `rm` / `sync` / `mb` / `rb` has begun work, every failure ends
at 1 — **including errors S3 itself returned**. The transfer commands
deliberately do not report 254 once running, matching `aws s3`. Per-item
failures were already printed as `... failed:` lines while the run proceeded;
a failure that stops the whole run prints one `fatal error:` line.

Ctrl-C during a running transfer or delete is also 1, with a
`cancelled: ctrl-c received` line.

`ls` uses 1 for its own "nothing to show" case: a key or prefix that matched no
entries.

### 2 — completed, with warnings

A transfer command reached the end with warnings but no failures: objects
skipped because they are archived, a local file that could not be read, a
modification time that could not be stamped onto a downloaded file. Everything
else transferred normally. Warnings count toward this code, failures do not —
if anything failed, the code is 1.

### 130 — Ctrl-C before the operation started

Once a transfer or delete is running, Ctrl-C reports 1 instead (above). The
interactive prompt's own Ctrl-C and end-of-input also exit 130.

### 252 — the command line was rejected

An unknown option, an invalid choice or value, a path that is not a usable S3
URI, or an option that does not apply to the direction being run (for example
`--checksum-algorithm` on a download). The command never contacted S3. The
message uses the same envelope as `aws`:
`An error occurred (ParamValidation): <message>`.

### 253 — the environment cannot supply what is needed

Credentials or a region could not be resolved, or a requested feature needs a
dependency that is not installed — asking for the CRT transfer engine without
the `crt` extra is the common case.

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

## 2. When more than one thing is wrong

Which code wins is decided the same way `aws s3` decides it, not by a rule of
our own. Two consequences are easy to miss:

- `mb` and `rb` build their client before checking the path, so
  `boto3-s3 mb badpath --profile nosuch` exits 255 for the bad profile rather
  than 252 for the bad path.
- Options that take a number are converted before the path is checked, so a
  non-integer `--page-size` exits 255 even if the path is also wrong.

The full precedence specification — every command family's rule and the order
each check runs in — is in [`cli.md`](../../design/cli.md) section 6.

## 3. Where this differs from `aws s3`

Three cases where the codes are deliberately not identical:

- **`-h` / `--help`** is an option here; `aws` offers `help` as a subcommand
  instead. An extension that `aws s3` does not have cannot match it.
- **Features needing `awscrt`** exit 253 when the `crt` extra is not installed.
  `aws` v2 bundles awscrt, so this situation cannot arise there.
- **A corrupted ranged download** is reported as success by the classic
  transfer engine, where `aws` exits 1. `aws` validates the reassembled object
  against its full-object checksum using a variant of s3transfer that is not
  published; the CRT engine is unaffected.

[`compatibility.md`](../compatibility.md) covers what else changes with the
installed dependencies.
