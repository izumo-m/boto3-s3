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
dies below the HTTP layer, a download body cut mid-stream, a listing an
S3-compatible endpoint returns unsorted, a recursive delete whose listing dies
part way, a plain-HTTP
endpoint taken from the environment under the CRT engine, an `aws` plugin the
config file declares, the modification time a download stamps for an object
older than the local zone's present rules, a `--copy-props all` copy whose
oversized tag write and its rollback both fail, the `PYTHON*` environment
variables, and three failures the interpreter decides rather than
either tool: a stdout that cannot take a streamed object, an error report that
cannot be written at all, and a standard stream that cannot be set up at all —
that last one settling before either tool's first line of code runs. A mismatch
anywhere else is a bug worth reporting.

**Do not parse the console output.** Not because the wording differs: result
lines, error text and warnings are aws's own, with this command's name
substituted for `aws` — the error prefix is `boto3-s3:`, not `aws:`, and usage
lines read `boto3-s3 <subcommand>` where aws's read `aws s3 <subcommand>`. That
is exactly what makes parsing fragile: the wording is aws's to change, and it
does change from one `aws` release to the next. Fourteen of section 2's entries
cover the text that differs on purpose — the progress display, help pages and
`--debug` traces, a `rm` that cannot reach its credentials under the CRT
engine, the failure lines of a batched delete, the closing line of a Ctrl-C
`aws` cannot attribute to a cancelled classic transfer, the `move:` lines an
interrupted `mv` on `aws` can leave out for sources it has already deleted, the
failure line of a
directory copied without `--recursive`, the invalid-bucket-name reports this
command writes itself, the `--version` line, two argument-parsing corners, the
history warning `aws` writes and this command has not, the messages the
installed botocore itself writes, the element a listing entry missing two
required ones is blamed on, and the request a failing stream download is
blamed on under a non-default checksum-validation setting. The interactive prompt
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
whose connection dies is a silent success on `aws`. One more case is silent on
`aws`'s side — an unsorted listing — but it takes an S3-compatible endpoint
that returns one to reach at all. The rest are visible, or
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
- **An unsorted listing stops `sync` here and not on `aws`.** `sync` pairs its
  two sides by walking both listings in key order — what real S3 and MinIO
  return, and what the local walk sorts to match. Against an S3-compatible
  endpoint whose `ListObjectsV2` hands back keys out of order, `aws` keeps
  merging and exits 0, pairing keys that do not belong together; with
  `--delete` that means deleting a key present on **both** sides and then
  copying it again (measured). Here the descent is caught as the stream is
  read, and the run ends with one line — `fatal error: source sync stream is
  not byte-ordered by compare_key (...)`, or `destination` for the other
  side — and exit code 1. Transfers and deletions already reported before that
  point stand on both tools; nothing past it happens here. This is deliberately
  not mirrored, for the same reason as the swallowed per-item failure above:
  mirroring would mean deleting data that is present on both sides. Reaching it
  takes such an endpoint — S3 Express directory buckets, whose listings promise
  no order, are refused by `sync` up front on both tools.
- **A download stamps an old object on a different second.** Both tools give
  the downloaded file the object's `LastModified`, and for every timestamp
  today's zone rules cover they agree to the second. They part on one old
  enough that the zone's rule for it differs from the rule in force now: `aws`
  converts the timestamp to local time and then re-reads those wall-clock
  fields with `time.mktime`, which resolves them under the zone's *historical*
  rule and so settles on a different instant than the object carries, while
  here the object's own instant is stamped. Under `TZ=Asia/Tokyo` an object
  whose `LastModified` is `1601-01-01T00:00:00Z` comes out stamped
  `1601-01-01 09:00:00 +0918` on `aws` and `1601-01-01 09:18:59 +0918` here —
  the latter being the moment the object names. Neither tool warns, the exit
  codes agree, and the bytes are identical. Real S3 never reports a
  `LastModified` that old, so reaching this at all takes an S3-compatible
  implementation that does.
- **Default checksum algorithm without awscrt.** Without `--checksum-algorithm`,
  the requests that carry a client-computed checksum use `CRC64NVME` here as on
  `aws` v2 — uploads, deletes, bucket configuration writes, the annotation
  writes a `--copy-props all` multipart copy sends — as long as the installed
  botocore can compute it, which takes awscrt (the `crt` extra). Without
  awscrt the installed botocore's own default, `CRC32`, is sent instead; `aws`
  always bundles awscrt. Both algorithms are valid and neither changes the
  result or the exit code; what differs is the checksum type left on an
  uploaded object (a full-object CRC64NVME against a composite CRC32). An
  explicit `--checksum-algorithm` makes the two agree regardless.
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
- **A batched delete names a different operation when a key fails.** Deletes go
  out in batches here — up to a thousand keys per `DeleteObjects` request —
  where `aws` sends one `DeleteObject` per key. For a run that finishes
  enumerating, the objects removed and the exit code are the same (a key that
  no longer exists is a success on both wire shapes); the per-key failure line
  is not. It reads `delete failed:
  s3://bkt/key An error occurred (AccessDenied) when calling the DeleteObjects
  operation: <message>` — the plural operation name, and no
  `(reached max retries: N)` suffix — because the line is composed from that
  batch response's own per-key error rather than written by botocore, where
  `aws`'s reads `... when calling the DeleteObject operation (reached max
  retries: 0): <message>`. Only the batching routes are affected —
  `rm --recursive`, an S3-side `sync --delete`, and `rb --force` — while a
  single `rm s3://bkt/key` still issues `DeleteObject` and its line is byte for
  byte aws's. So a script grepping for the singular name, or for the retry
  suffix, keeps matching on single deletes and quietly stops matching on
  recursive ones.

  Batching is also what makes those same three routes end differently when the
  **listing that feeds them dies part way** — the service failing mid-pagination,
  an entry the response cannot supply. Both tools stop and exit nonzero, but
  `aws` has already issued its own `DeleteObject` for every key enumerated so
  far, while here the keys still sitting in the unsent buffer are dropped
  without being deleted and without a record: **up to 999 objects that `aws`
  would have removed survive**. Re-running the command deletes them, and a run
  that enumerates to the end is unaffected.
- **A big tag set is written at a different point of a `--copy-props all`
  copy.** For an S3-to-S3 copy above the multipart threshold whose source tags
  do not fit the create call's header — roughly 2 KiB once percent-encoded —
  both tools fall back to a `PutObjectTagging` after the copy, and both carry
  the source's object annotations over. `aws` sends that tagging write first
  and the annotation writes after it; here the annotations ride the transfer
  library's own write path, which finishes before the tagging write goes out.
  The same requests are sent, and the console lines and the exit code agree. So
  does the destination whenever the tagging write succeeds, and whenever it
  fails and the rollback delete that follows it succeeds — both tools then
  leave no object at all. **They part when that rollback delete fails too**:
  both report the copy as a success (exit code 0, the destination left as the
  copy produced it, untagged), but the object `aws` leaves carries no
  annotations, never having got that far, while the one left here carries them.
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
- **An interrupted `mv` on `aws` can delete a source it never reported.** A
  recursive `mv` copies each object and then deletes its source, printing one
  `move:` line for the pair. Interrupt one and `aws` can finish with sources
  deleted for which no `move:` line was ever printed — measured, nine of them
  in each of four runs — while here every source that is deleted is reported.
  Neither tool deletes a source whose copy has not succeeded, and which objects
  a given interrupt catches is not reproducible on either tool; what differs is
  that aws's record can come out short of what it did, so a script reading its
  output to learn what moved would under-count. This belongs to the
  rendering accidents above, so this command keeps the complete record instead
  of copying it.
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
- **`~/.aws/cli/cache/session.db` is not written.** On the first client it
  builds, `aws` opens — creating the directory and the file if they are not
  there — an SQLite database at that path, and keeps a host id and a rolling
  session id in it; the session id then rides its User-Agent as a `sid/`
  component. This command has no such store: it creates nothing, reads nothing,
  and sends no `sid/` component (its User-Agent already differs, below). `aws`
  swallows every failure of that machinery, so a directory it cannot write
  changes nothing observable there either — the file simply stops being touched
  once a script switches over.
- **The `[plugins]` section is not read.** `aws` hands its merged
  configuration to a plugin loader before it parses anything: the
  `cli_legacy_plugin_path` entry is added to its import path, and every other
  entry of `[plugins]` is imported and its `awscli_initialize` called — on
  every invocation, `--version` included. This command has no plugin mechanism
  and never reads the section, so whatever a plugin was doing on your `aws`
  runs — registering handlers, auditing, extra output — simply does not
  happen. **When the plugin cannot be imported the run comes out
  differently**: `aws` refuses to start at all (exit code 255, `No module
  named '<name>'`, nothing done), while this command runs the operation and
  exits normally — so a `rm --recursive` that `aws` would never have begun
  deletes the objects here. This one is not a matter of effort: a plugin is
  written against `aws`'s own internals, so nothing outside that codebase can
  run one.
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
  "greater than or equal to 1."; the exit code is the same either way. It is
  the only one left because the order around it agrees: the retry settings are
  resolved and validated where `aws` resolves them, so a configuration carrying
  several mistakes at once — an out-of-range `max_attempts` (`0`, `-1`)
  together with an invalid `retry_mode`, or either of them beside a broken
  `[s3]` section — reports the same one of them on both tools. The
  invalid-bucket-name report above belongs to the same SDK-owned family: the
  two copies word it identically today, and it is because that wording is
  theirs to change that the reports this command writes itself stop before the
  regex tail.
- **A listing entry missing two required elements is blamed on a different
  one.** When a service returns an entry without `Key` *and* without
  `LastModified` or `Size`, `ls` stops and quotes the name of an element it
  could not read — and the two tools quote different names. `aws` reads them in
  one order while displaying a listing (`LastModified`, `Size`, `Key`) and in
  another while enumerating a transfer (`Key`, `LastModified`, `Size`); this
  command has a single listing converter, which follows the transfer order. So
  an entry missing `Key` and `LastModified` ends `'LastModified'` on `aws` and
  `'Key'` here. The exit code, the entries printed before the bad one and the
  stream they go to all agree, and `cp` / `mv` / `sync` / `rm` agree
  completely, their order being the shared one. An entry missing exactly one
  required element names that element on both tools.
- **`AWS_DEFAULTS_MODE` is read here and not by `aws`.** The installed botocore
  implements defaults modes; `aws` v2's bundled botocore ignores the variable —
  and the `defaults_mode` config key — entirely. A *valid* mode nonetheless
  changes nothing you can observe here, because every setting a mode vends is
  one this command already fixes to the same value: the connect timeout stays
  at 60 s (the session names it explicitly, so a mode's 3.1 s or 30 s never
  applies), the retry posture stays `standard` with 3 attempts, and both the
  `[s3]` section's `us_east_1_regional_endpoint` and the profile's
  `sts_regional_endpoints` are already `regional`. What does differ is an invalid or empty value: an error here
  (exit code 255) where `aws` runs as if the variable were unset.
- **`sts_regional_endpoints` is validated.** The installed botocore still
  validates this config key (and `AWS_STS_REGIONAL_ENDPOINTS`); `aws` v2's
  bundled botocore dropped it. An invalid value is an error here (exit
  code 255) where `aws` runs as if it were unset.
- **A failing stream download under `response_checksum_validation =
  when_required` is blamed on a different request.** With that config key (or
  `AWS_RESPONSE_CHECKSUM_VALIDATION=when_required` — not the default), the
  installed `s3transfer` skips the `HeadObject` it would otherwise send before
  a download and opens the object with a ranged `GetObject` straight away,
  where `aws`'s bundled copy still heads first. A download to a file is
  unaffected — both tools resolve the source with their own `HeadObject`
  before the transfer starts — and a stream download that succeeds is byte
  for byte the same. But `cp s3://bkt/k -` resolves nothing of its own, so
  when the object cannot be read the failure line names the request that
  failed: `download failed: s3://bkt/k to - An error occurred (NoSuchKey)
  when calling the GetObject operation: The specified key does not exist.`
  here, against `... An error occurred (404) when calling the HeadObject
  operation: Not Found` on `aws` — for a missing key, and for an SSE-C
  object read without its key. Exit code 1 on both, nothing written. This
  lives in the installed `s3transfer`, the same family as the mid-stream
  retry above, so no option here changes it.
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
