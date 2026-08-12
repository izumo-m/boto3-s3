# Differences from `aws s3`

Read this before you switch a script over. Most `aws s3` invocations behave
identically; the handful that do not are listed below, and two of them are
silent — one on each side.

For what each option does, run `boto3-s3 <subcommand> help`; every option is an
`aws s3` option and is described there. For exit codes see
[`exit-codes.md`](./exit-codes.md), and for the configuration files, environment
variables and `[s3]` tuning keys the command reads see
[`configuration.md`](./configuration.md).

## 1. What you can rely on

Under the same arguments and configuration you get **the same resulting S3
state, the same returned values, the same error conditions, and the same exit
code**, bar the entries in section 2 that say outright that the run comes out
differently. Those are a corrupted ranged download, a transfer whose connection
dies below the HTTP layer, a download body cut mid-stream, a plain-HTTP
endpoint taken from the environment under the CRT engine, the `PYTHON*`
environment variables, and three failures the interpreter decides rather than
either tool: a stdout that cannot take a streamed object, an error report that
cannot be written at all, and a standard stream that cannot be set up at all —
that last one settling before either tool's first line of code runs. A mismatch
anywhere else is a bug worth reporting.

**Do not parse the console output.** Not because the wording differs: result
lines, error text and warnings are aws's own, with this command's name
substituted for `aws` — the error prefix is `boto3-s3:`, not `aws:`, and usage
lines read `boto3-s3 <subcommand>` where aws's read `aws s3 <subcommand>`. That
is exactly what makes parsing fragile: the wording is aws's to change, and it
does change from one `aws` release to the next. Ten of section 2's entries
cover the text that differs on purpose — the progress display, help pages and
`--debug` traces, a `rm` that cannot reach its credentials under the CRT
engine, the closing line of a Ctrl-C `aws` cannot attribute to a cancelled
classic transfer, the failure line of a directory copied without
`--recursive`, the invalid-bucket-name reports this command writes itself,
the `--version` line, two argument-parsing corners, the history warning `aws`
writes and this command has not, and the messages the installed botocore
itself writes. The interactive prompt
(`--cli-auto-prompt`) is outside parity altogether, its output included. And
the ordering of concurrent output is not reproducible on either tool (below).

Because the text is aws's, so are its envelopes: parameter-validation errors
use `An error occurred (ParamValidation): <message>`, and so do the
exit-code 253 reports this command shares with `aws` — unresolved credentials,
an unresolved region, and an unusable `cli_timestamp_format` (`An error
occurred (Configuration): ...`); the 253 an uninstalled dependency produces is
this command's own and has no envelope
(see [`exit-codes.md`](./exit-codes.md)). The credentials and region reports
also keep
aws's hint verbatim, which is why the credentials error tells you to run
`aws login`: this command has no login subcommand of its own, and both tools
read the same credentials and config files, so configuring credentials with
`aws` — or by any other means — is what fixes the run either way. The
unresolved-region report points at `aws configure` for the same reason.
`aws` drops the envelope when the profile you named does not exist in any
config file, keeping the exit code, and so does this command. Where the two
part is `--cli-error-format` and `AWS_CLI_ERROR_FORMAT`: `aws` acts on them,
this command accepts every value and ignores it. Set either to `enhanced` in
that dropped-envelope situation and `aws` puts the envelope back, where here
it stays dropped; set either to `legacy` and `aws` drops the envelope
everywhere, where here the report is unchanged; and `json`, `yaml`, `text` and
`table` re-render the report itself on `aws` — into a JSON object, a YAML
mapping, a tab-separated line, an ASCII table — where here it keeps its usual
one-line form. Whichever of those values is set, the exit code is the same on
both tools.

**Ordering**, as promised above: with concurrent transfers, result lines — and
`delete:` lines against transfer lines — interleave freely, on either tool.

## 2. Behavior differences

The first two can leave you with something wrong without saying so, one on each
side: a corrupted ranged download is a silent success here, and a transfer
whose connection dies is a silent success on `aws`. The rest are visible, or
make no difference to the result — except the ones that say outright that the
run comes out differently, listed in section 1.

- **Corrupted ranged downloads are not detected by the classic engine.** For a
  single-object download at or above the multipart threshold, `aws` verifies the
  reassembled object and fails on a mismatch; here such a corruption — one that
  got past TLS and TCP integrity — would be reported as a success. **If that
  matters to you, install the `crt` extra and set
  `preferred_transfer_client = crt`**, which validates exactly as `aws` does.
  Downloads below the threshold are verified on both tools, so only large
  single-object downloads are affected.
- **`aws` loses a per-item failure that happened below the HTTP layer.** When
  the last attempt botocore makes for one item dies without a response — the
  connection closed, or the read timed out — `aws` records nothing at all: no
  `... failed:` line, the item counted neither transferred nor failed, and
  **exit code 0 although nothing was transferred**. Its per-item completion
  handler asks whether the exception is S3's conditional-write rejection by
  reading the response off it, which that family of botocore errors leaves
  empty; the `AttributeError` that follows is swallowed inside `s3transfer` and
  the failure never reaches the result queue (`aws --debug` prints it). Every
  route is affected — `cp`, `mv`, `sync` and `rm`, uploads, downloads, S3-to-S3
  copies and deletes, single-part and multipart — and in a recursive run only
  the affected item vanishes while the rest print normally. Here the same
  question is asked in a way that survives the missing response, so the item
  fails like any other: one `upload failed:` / `download failed:` /
  `copy failed:` / `move failed:` / `delete failed:` line and exit code 1. The
  number of attempts, and what is left behind (no partial download, an `mv`
  source kept, a multipart upload aborted), are the same on both tools. This is
  deliberately not mirrored: mirroring it would mean exiting 0 and printing
  nothing after a transfer that did not happen. It belongs to the same family
  as the rendering accidents below — the empty `fatal error:` line of a Ctrl-C,
  the CRT engine's `Unknown Error Code` — and is the only one of them you
  cannot see.
- **A download body cut mid-stream is retried here and not by `aws`.** The
  retryable set differs between the `s3transfer` installed from PyPI and the
  fork `aws` bundles: the installed botocore turns a connection broken in the
  middle of a response body into an error the installed `s3transfer` re-fetches
  the range for, while the bundled pair does not. So one transient break leaves
  this command at exit code 0 with the complete file, where `aws` exits 1 with
  the underlying `Connection broken: IncompleteRead(...)` on its
  `download failed:` line and nothing at the destination. A break that persists
  fails both — exit code 1, no file — but this command spends five attempts per
  range and ends with `Max Retries Exceeded`, where `aws` gives up on the first
  break and prints that same underlying error. This lives in the installed
  `botocore` / `s3transfer` rather than in either tool's own code — the same
  split as the default checksum algorithm below — so no option here changes it.
- **Default checksum algorithm.** Without `--checksum-algorithm`, uploads are
  integrity-checked with `CRC32`; `aws` v2 uses `CRC64NVME`. Both are valid and
  neither changes the result or the exit code. An explicit
  `--checksum-algorithm` makes the two agree.
- **Output back-pressure.** `aws` queues result lines without limit, so a stalled
  reader grows memory. Here the queue is bounded: a reader that falls far enough
  behind slows the transfer instead. No result line is ever dropped.
- **Progress display.** `aws` repaints on every transferred chunk and again
  after every line it prints; here repaints are floored at 0.1 s (or
  `--progress-frequency` when that is larger) and come only from byte
  progress, so a run that never moves a byte — every transfer failing early,
  or a `sync --delete` that only deletes — shows a meter on `aws` and none
  here. `aws` also paints a `~total (calculating...)` marker while it is still
  enumerating what to transfer; here the total is painted plain. And `aws`
  counts a `sync --delete` deletion into the meter's totals when it queues it,
  where here it joins them as it completes. The numbers painted are exact
  either way, and the final counts agree.
- **Help pages and `--debug` traces.** Help pages are laid out by this
  command's own parser, not by aws's documentation renderer; the options, their
  values and their meanings are the same, the typography is not. `--debug`
  traces come from the installed boto3/botocore rather than aws's bundled copy,
  and credentials appearing in them are masked here — `aws` prints them in
  full.
- **Deletes never ride the CRT engine.** With
  `preferred_transfer_client = crt`, `aws` routes each `rm` — and each S3-side
  `sync --delete` — through its CRT client, while here they keep their
  `DeleteObject` / batched `DeleteObjects` requests. The CRT client itself is
  still built: `rm` constructs it exactly as `aws` does, taking its own
  host-wide CRT slot exactly as `aws` takes its own (the two slots are separate
  and never contend), so a configuration the CRT refuses fails the same way —
  the client simply carries no deletes. One consequence: the numeric `[s3]`
  tuning keys shape `aws`'s deletes and not this command's, since only its
  transfers ride the engine those keys configure. Same objects deleted, same
  exit code; the one place it shows in the output is a failure the CRT reports
  differently, such as credentials that cannot be resolved: `aws` prints its
  CRT delegate's `AWS_AUTH_CREDENTIALS_PROVIDER_DELEGATE_FAILURE` (preceded by
  a Python `Exception ignored in:` block) where this command prints botocore's
  `Unable to locate credentials`. Uploads and downloads use the CRT engine on
  both tools.
- **A plain-HTTP endpoint given only by the environment still reaches the CRT
  engine.** Under `preferred_transfer_client = crt`, `aws` decides whether its
  CRT client speaks TLS from `--endpoint-url` alone, so an endpoint supplied
  by `AWS_ENDPOINT_URL_S3` instead leaves TLS on: a `http://` endpoint is
  dialed over TLS and the transfer dies with `AWS_IO_SOCKET_CLOSED`. Here the
  scheme is read off the endpoint the client actually resolved, environment
  variable included, so the same run transfers — the exit code and the
  resulting S3 state differ, in the direction of working. Pass
  `--endpoint-url` and the two agree again
  ([`../../design/crt.md`](../../design/crt.md) section 3 records the
  measurement).
- **Ctrl-C's closing line.** Both tools exit 1 on a Ctrl-C caught mid-run,
  and on the classic engine with transfers actually in flight both close
  with `cancelled: ctrl-c received`. `aws` produces that line only from a
  cancelled *classic* transfer future, so everywhere else it has one — the
  interrupt landed before the first transfer was submitted, or during a
  `--dryrun`, or after the last one finished, or mid-submission under
  `preferred_transfer_client = crt` — `aws` closes instead with a
  `fatal error:` line carrying no message: its recorder renders the
  `KeyboardInterrupt`, whose text is empty, as an error result padded with the
  blanks that erase the progress line. The empty line is a rendering accident,
  so this command keeps its uniform Ctrl-C ending instead of copying it. (An
  interrupt landing in the CRT engine's transfer drain is a different shape
  with no divergence at all: the CRT manager swallows it on both tools, which
  print their per-item failure lines and no closing line.)
- **Copying a directory without `--recursive`.** A `cp` or `mv` whose local
  source is a directory always fails — exit code 1 on both tools, the source
  left in place — and only the failed line's wording differs. `aws` threads
  the source through in its trailing-separator form: its line names the
  source `d/` and, on the classic engine, ends with `[Errno 21] Is a
  directory: '/path/to/d/'` when the destination carries a key — or, when the
  destination folds the key away as `s3://bkt/` does, with `Parameter
  validation failed:` and `Invalid length for parameter Key, value: 0, valid
  min length: 1`, the empty key its request reaches botocore with. Under the
  CRT engine it ends with `Unknown Error Code: Unknown Error Code` — the
  native error its renderer cannot translate. Here the directory is detected
  before submission, so the line names the source `./d` and ends with
  `[Errno 21] Is a directory: '/path/to/d'` on either engine and for either
  destination. The `Unknown Error Code` ending is a rendering accident, so
  this command keeps the errno report instead of copying it. Under `--dryrun`
  both tools instead exit 0 and preview the
  doomed upload, and the same threading difference shows in the previewed
  line: `aws` prints `(dryrun) upload: d/ to s3://bkt/` — the
  trailing-separator form, with the destination key folded away — where this
  command prints `(dryrun) upload: ./d to s3://bkt/d`.
- **Some invalid bucket names report one line.** For a name that reaches
  botocore's own check, both tools print botocore's full report, ending in the
  regex the name must match — `mb s3://in!valid` is byte for byte aws's. The
  report stops after `Invalid bucket name "<name>"` only on the URIs this
  command refuses up front, before a request is built: the ones naming no
  bucket at all (`mb s3://`, `mb s3:///key`, `rb s3://`, `rm s3://`) and a
  `website` URI carrying a key (`website s3://bkt/key`). The exit code is the
  same either way — 1 for `mb` / `rb` / `rm`, 252 for `website`.
- **The `--version` line.** `aws` names itself and its interpreter; this
  command names the four packages that decide its behavior —
  `boto3-s3-cli/<v> boto3-s3/<v> boto3/<v> botocore/<v> Python/<v>
  <System>/<release>` — and has no `exe/<machine>` install-source token to
  report. Anything keying on the `aws-cli/<version>` token will not match.
- **Two argument-parsing corners.** When an abbreviated option is ambiguous,
  the candidates are listed in a different order than `aws` lists them; and on
  Python 3.10 and 3.11 only, a value that itself ambiguously abbreviates one of
  the command's options (`--exclude --ss`) is rejected here where `aws` takes
  it as the value. Both affect the error text, not which options exist;
  [`../../design/cli.md`](../../design/cli.md) section 2 records why.
- **Interactive prompt.** `--cli-auto-prompt` needs the `autoprompt` extra, and
  its completions are not the same as aws's: values of every option that has a
  fixed choice list are completed (`aws` omits some), bucket and key names are
  not completed from the server, and shell completion is not provided. Nothing
  about the prompt is covered by parity.
- **`cli_history` is not read.** With `cli_history = enabled` in the selected
  profile, `aws` records the run in a history database, and when it cannot
  open that database it writes one line to stderr ahead of everything else —
  `Warning: Unable to record CLI history. Check file permissions for <path>` —
  without changing the exit code. This command has no history mechanism, so it
  records nothing and warns about nothing; the rest of the run is unchanged.
- **User-Agent.** Requests identify themselves as the installed
  `Boto3`/`Botocore`, not as `aws-cli`, and carry none of aws's command
  metadata (`md/command#s3.ls` and the like). Visible only to the server and
  in `--debug` traces; anything keying on the aws-cli User-Agent (bucket
  policies, access-log analytics) will classify these requests differently.
- **Messages botocore itself writes can be worded differently.** Where a report
  comes from the SDK rather than from either tool, the sentence is the
  installed botocore's, not the one in aws's bundled copy — the two are
  separate codebases. The one reachable instance is a rejected `max_attempts`,
  which `aws` ends "greater than or equal to one." and this command ends
  "greater than or equal to 1."; the exit code is the same either way. Give an
  out-of-range `max_attempts` (`0`, `-1`) and an invalid `retry_mode` in the
  same run and the two even report different halves of the problem — `aws` the
  range, this command the mode — because each validates them in a different
  order; either one alone is reported identically. The order is left as it is,
  since the range message can never match byte for byte anyway. The
  invalid-bucket-name report above belongs to the same SDK-owned family: the
  two copies word it identically today, and it is because that wording is
  theirs to change that the reports this command writes itself stop before the
  regex tail.
- **`AWS_DEFAULTS_MODE` is honored.** The installed botocore implements
  defaults modes; `aws` v2's bundled botocore ignores the variable entirely.
  Setting it changes retry/timeout defaults here where `aws` would not, and an
  invalid value is an error here (`aws` runs as if it were unset).
- **`sts_regional_endpoints` is validated.** The installed botocore still
  validates this config key (and `AWS_STS_REGIONAL_ENDPOINTS`); `aws` v2's
  bundled botocore dropped it. An invalid value is an error here (exit
  code 255) where `aws` runs as if it were unset.
- **Where a failing stdout stops a stream download.** `cp s3://bkt/k -` hands
  the object's bytes to the process's stdout and never flushes them, exactly as
  `aws` does, so a stdout that cannot take them fails in one of two places:
  inside the transfer, as a `download failed:` line and exit code 1, or at
  interpreter shutdown when the buffered bytes are finally flushed, which ends
  the *process* at 120 with no failure line at all. Which one you get depends on
  whether the object outgrows the interpreter's own stdout buffer, and that size
  belongs to the interpreter rather than to either tool: `aws`'s frozen 3.14
  buffers about 128 KiB against a `/dev/full` stdout, where a host CPython 3.10
  gives it 4096 bytes. Objects between those two sizes therefore report the
  failure and exit 1 here while `aws` reports nothing and exits 120; smaller and
  larger ones agree, and so does every size once the two buffers are the same
  (measured). A closed pipe and a stdout opened read-only fail in those same two
  places, by the same rule.
- **When the error report itself cannot be written.** Both tools deliberately
  leave the report *about* a failed report unguarded. That is what replaces a
  run's own exit code with 255 on both when the report carries a character the
  output codec cannot encode (`AWS_CLI_OUTPUT_ENCODING`, whose codec is
  described in [`configuration.md`](./configuration.md)) — there the second
  report still gets written. With stdout and stderr both broken, though —
  `ls s3://bkt/ 2>&1 |` a reader that closes at once — there is nothing left to
  catch it: `aws` lets the failure escape and its interpreter exits **1**,
  while here the process ends at **120** on the shutdown flush that fails on
  the same stream. Neither tool has managed to write anything you can read, so
  only the code differs.
- **The `PYTHON*` environment variables reach this command and not `aws`.**
  The official `aws` distribution is a frozen interpreter running in isolated
  mode, so the interpreter ignores that whole family; this command runs on
  your own Python, which does not. `PYTHONIOENCODING` therefore re-codes what
  is printed here while `aws` keeps writing UTF-8 — under
  `PYTHONIOENCODING=ascii` a `café.txt` result line comes out `caf?.txt`, and
  under `latin-1` it comes out in latin-1 bytes. One member `aws`'s own code
  reads back: `PYTHONUTF8=1`, kept as a compatibility fallback for the streams
  it writes error reports on, and honored here the same way — so setting it
  brings an error report into agreement again, while result lines stay as they
  were. `PYTHONUTF8=0` with `PYTHONCOERCECLOCALE=0` under a C locale goes the
  other way and makes a UTF-8 config file unreadable here — exit code 255 —
  where `aws` still reads it. The same packaging decides one thing more: an
  argument carrying a byte that is not valid UTF-8 is accepted here and the
  run proceeds, while `aws`'s bootloader rejects it before Python starts,
  exiting 255 with `Failed to set sys.argv: decoding error`. Leave the family
  unset and the two agree: under a plain C, ISO-8859-1 or ASCII locale, config
  reading, `file://` decoding, and the scanning and display of file names all
  match. One last packaging split needs no environment at all: give the process
  a standard stream it cannot set up — `cp - s3://bkt/k < /some/directory` —
  and `aws`'s bootloader fails to start its embedded interpreter and exits 255,
  while here CPython dies in `init_sys_streams` and exits 1 with a
  `Fatal Python error:` block. No code from either tool has run at that point,
  and nothing is created either way.
- **Which CA certificates are trusted.** Both tools verify every TLS connection
  against an explicit CA file — never the operating system's trust store — but
  not the same file: `aws` uses the `cacert.pem` bundled in its own
  installation, while this command uses the bundle shipped with the installed
  botocore/certifi. Both are derived from Mozilla's CA list, so the trusted
  roots are the same in practice; they are separate snapshots and can differ in
  age, so a very recently added or removed root may be known to one and not the
  other. `--ca-bundle` and `AWS_CA_BUNDLE` override the file on both tools
  identically, and `--no-verify-ssl` disables verification on both;
  `REQUESTS_CA_BUNDLE` is honored here whichever transfer engine runs, while
  `aws` honors it on its classic engine only (its CRT transfers ignore it).

Differences that depend on which dependencies are installed — the CRT engine,
CRT-family checksums, conditional writes, and more — are in
[`compatibility.md`](../compatibility.md).

## 3. Options that do nothing

`--output`, `--query`, `--no-paginate`, `--no-cli-pager`, `--color`,
`--cli-error-format` and `--cli-binary-format` are accepted for compatibility
and have no effect — the help page groups them under `recognized but ignored`.
Where an option has a fixed choice list the value is still validated, so an
invalid one is still an error, and `--query` is still compiled as a JMESPath
expression and rejected if malformed.

Having no effect here is not the same as having no effect on `aws`:
`--cli-error-format` — and its `AWS_CLI_ERROR_FORMAT` spelling — does change
how `aws` renders an error report, so a script reading `aws`'s `json` or
`text` error output gets this command's usual one-line report instead
(section 1).

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
