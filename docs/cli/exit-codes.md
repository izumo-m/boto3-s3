# Exit codes

`boto3-s3` uses the same exit codes as `aws s3`, with the same meanings, so a
script that already branches on `aws s3`'s codes keeps working unchanged.

## 1. The codes

| code | what it means |
| --- | --- |
| 0 | The command succeeded. `help` and `--version` also exit 0. |
| 1 | The operation failed after it started. |
| 2 | A transfer finished with warnings but no failures. |
| 130 | Interrupted with Ctrl-C before the operation started. |
| 252 | The command line was rejected. |
| 253 | The environment cannot supply what the command needs. |
| 254 | A request reached S3 and S3 returned an error. |
| 255 | Any other error. |

### 0 — success

`help` and `--version` exit 0 as well. Piping into a reader that closes early
(`ls | head`) is a special case: the command itself succeeds, but the process
usually ends with 120, once Python fails to flush to the closed pipe. `aws` does
the same, so do not branch on it.

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
`--checksum-algorithm` on a download). No transfer request was sent. The
message uses the same envelope as `aws`:
`An error occurred (ParamValidation): <message>`.

### 253 — the environment cannot supply what is needed

Credentials or a region could not be resolved, or a requested feature needs a
dependency that is not installed — asking for the CRT transfer engine without
the `crt` extra is the common case — or one that is installed but too old to
carry it ([`compatibility.md`](../compatibility.md) lists what each feature
needs).

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

**Once a transfer command has started, every failure is 1.** `cp` / `mv` / `rm`
/ `sync` report 1 after they begin — including errors S3 itself returned. That
is why 254 is narrower than it looks. A usage error caught *before* the
operation starts is still 252.

**Before that, whether the request reached S3 decides the code.** Anything the
service answered is 254, whatever kind of error it is — a 400 from S3 is 254,
not 252, because the request went out. Errors that never left your machine are
classified by kind: 252, 253 or 255.

The orderings worth knowing, because the answer is not the one you would guess:

| You run | You get | Because |
| --- | --- | --- |
| `mb` / `rb` with both a bad path and a bad `--profile` | 255 | the profile error is detected before the path is validated |
| a non-integer `--page-size`, `--expires-in` or `--progress-frequency`, with a bad path too | 255 | the number is parsed before the path is validated |
| `cp --expected-size` with a non-integer | 1 when uploading a stream, 0 otherwise | the value is read only on the streaming route |
| `rb --force` whose object deletion fails | 255 | the bucket removal never runs |
| `mv --validate-same-s3-paths` whose lookup fails | 254 if the service answered, 255 if it could not be reached | the check calls out before the move begins |

All of this matches `aws s3`, including the orderings above.

## 3. Where this differs from `aws s3`

Two cases where the codes are deliberately not identical:

- **Features needing `awscrt`** exit 253 when the `crt` extra is not installed.
  `aws` v2 bundles awscrt, so this situation cannot arise there.
- **A corrupted ranged download** exits 0 here and 1 under `aws`. See
  [`aws-differences.md`](./aws-differences.md) for how to get that check back.

[`compatibility.md`](../compatibility.md) covers what else changes with the
installed dependencies.
