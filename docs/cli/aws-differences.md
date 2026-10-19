# Differences from `aws s3`

Read this before you switch a script over. This command aims at functional
compatibility with `aws s3`: the same objects and files transferred, deleted
or created, the same `sync` decisions, the same meaning for every option, and
on a successful run the same result lines. This file records where a run does
not come out that way — the deliberate deviations (batched deletes,
protections this command keeps on, `aws` bugs it does not copy) and the other
differences a run can show. It does not record a difference in an error's
details — its wording, which of two simultaneous mistakes is reported, the
order of the reports — nor one that comes only from the installed botocore /
s3transfer or the Python this command runs on differing from the copies
aws-cli bundles; neither is a bug
([`../../design/overview.md`](../../design/overview.md) section 3).

For what each option does, run `boto3-s3 <subcommand> help`; every option is an
`aws s3` option and is described there. For exit codes see
[`exit-codes.md`](./exit-codes.md), and for the configuration files, environment
variables and `[s3]` tuning keys the command reads see
[`configuration.md`](./configuration.md).

## 1. What you can rely on

Under the same arguments and configuration you get **the same resulting S3
state and local files, the same `sync` decisions, the same result lines on
success, the same conditions failing, and an exit code with the same
meaning**, bar the entries in section 2 that say outright that the run comes
out differently. Those are a recursive delete whose listing dies part way or
whose endpoint fails its batch requests, a Ctrl-C during deletes under the CRT
engine, a delete answered `412`, a CRT download that cannot put its file in
place, a `mv` between two spellings of one Outposts object, an S3 Express
access point, a plain-HTTP endpoint named anywhere but `--endpoint-url` under
the CRT engine, a `REQUESTS_CA_BUNDLE` the CRT engine cannot use, a
`[plugins]` section, an alias file, a `cli_history` directory `aws` cannot
create, the modification time a download stamps in some local zones (and the
`sync` runs that follow from it), a transfer or delete whose request dies
without any response, a multipart copy whose tag write and its cleanup both
fail, a help page that cannot be written, a
`file://` or `fileb://` reference `aws` mishandles, and the `PYTHON*`
environment variables. A difference of that kind anywhere else is a
bug worth reporting; an error report's wording, or which of two mistakes it
names, is not.

**Do not parse the console output.** Not because it differs on a successful
run: result lines are aws's own. That is exactly what makes
parsing fragile: the wording is aws's to change, and it does change from one
`aws` release to the next. Five of section 2's entries cover the text that
differs on purpose — the progress display, help pages and `--debug` traces,
the `--dryrun` line of a directory copied without `--recursive`, the
`--version` line, and the history warning `aws` writes and this command has
not. Error reports are not held to aws's wording at all. And the ordering of
concurrent output is not reproducible on either tool (below).

**Ordering**, as promised above: with concurrent transfers, result lines — and
`delete:` lines against transfer lines — interleave freely, on either tool.

One arrangement lets that same concurrency decide the result and not just the
order of the lines: **a `sync --delete` between two prefixes of one bucket, one
inside the other** (`sync s3://bkt/p/dir/ s3://bkt/p/ --delete`, or the other
way round). An object there is at once the source of a copy and a key the
destination has and the source does not, so the run both copies it and deletes
it, and which request reaches S3 first is a matter of timing on either tool.
In the form shown, `aws` sends the delete and the copy from separate worker
threads. For an object whose name sorts after the nested prefix the delete
goes out a few milliseconds first, and against a fast endpoint the object is
then usually lost (`copy failed: ... (NoSuchKey)`, exit code 1) — every run of
a batch on a local MinIO with only such names, about half of them once other
transfers ran alongside — while one whose name sorts before it is copied
first. This command sends its deletes in a batch once the listing is read and
usually keeps the copy, and sometimes does not; in the other form both tools
end either way from run to run. Neither outcome is
promised by either tool, so such a run is not something the two can be
compared on. Do not pass `--delete` when one side of a same-bucket sync
contains the other.

The same holds for **a `sync --delete` against an S3-compatible endpoint whose
listing is not in key order**: both tools pair such a listing the same way,
and that pairing can copy a key and delete it in one run, so whether the key
survives is timing on either tool (measured: lost in four of six runs on
each). Real S3's general purpose buckets and MinIO list in key order; an S3
Express directory bucket does not, and `sync` refuses it on both tools — and,
on this command only, an access point of one as well (section 2).

A second arrangement of the same kind is **a `sync --delete` download into a
case-insensitive directory** (the Windows and macOS defaults) **where a new
object and a file to be deleted differ only in case** — `A.txt` in the bucket,
`a.txt` on disk and not in the bucket. There the two names are one file, so
the run both writes it and removes it. Both tools remove a local file the
moment they reach it and download on worker threads. When the name to delete
sorts first (`A.txt` on disk, `a.txt` new), the delete is over before the
download is even started and the file is always there afterwards. When the
new name sorts first (`A.txt` new, `a.txt` on disk), the two race: a delete
that comes first removes the old file and the new one is then written, while
a download that finishes first has its file removed by the delete. On a
Windows host with only those two entries `aws` ended without the file in 6
runs of 60 and this command in 13 (a `download failed` line, exit code 1);
with 1500 other objects to download in between, every run of both ended
without the file and with exit code 0. Give the names one spelling before
syncing with `--delete` onto such a directory.

## 2. Behavior differences

These are visible, or make no difference to the result — except the ones
that say outright that the run comes out differently, listed in section 1.

- **A request that dies without any response fails its item here.** When a
  request's connection is closed before any response, or its read times out,
  once the client's retries are spent — or the HTTP layer refuses to send a
  header value (a line break in `--cache-control`) — `aws`'s completion
  handler fails on the missing response and loses the item: on the classic
  engine it prints no line for it and, if nothing else failed, exits 0,
  whether or not the request took effect (measured on `cp`, `mv`, recursive
  `cp` and `rm`). Under the CRT engine an `mv` download meets it in
  its source delete, a plain `DeleteObject`, and there `aws` writes an
  `Exception ignored in:` traceback ending in `AttributeError: 'NoneType'
  object has no attribute 'get'` and never exits (measured: still waiting
  after 90 seconds). This command reports such an item like any other
  failure — `upload failed:`, `move failed:`, `delete failed:` and so on,
  with the client's error — and exits 1; `rb --force` then stops with 255
  before removing the bucket. S3 may have acted on the request before the
  response was lost, so the object may have been written or deleted all the
  same. A request the CRT client sends itself — a CRT transfer, a delete
  under the CRT engine — is reported as a failure by both tools.
- **A multipart copy whose tag write and its cleanup both fail fails here.**
  An S3-to-S3 copy at or above the multipart threshold writes a tag set too
  large for its create request after the copy, and deletes the copied object
  again when that write fails. When the delete fails too, `aws` prints the
  copy's success line and exits 0, the copied object is left without its
  tags (under `--copy-props all`, with its annotations written), and a `mv`
  deletes its source. This command reports the item as failed (`copy
  failed:` / `move failed:` with the tagging error) and exits 1; the copied
  object is left without its tags or annotations, and a `mv` keeps its
  source.
- **A download's modification time differs in some zones.** Both tools give
  the downloaded file the object's `LastModified`; here the object's own
  instant is stamped, always. `aws` converts the timestamp to local time and
  then re-reads those wall-clock fields with `time.mktime`, and that round trip
  lands elsewhere in three cases:
  - **A zone whose rules mark winter as the daylight-saving period**
    (`Europe/Dublin`, as the tz database models it): on every download route
    (`cp`, `mv` and `sync`, single or recursive, either engine) `aws` stamps
    every object an hour off, today's included — an hour late for a winter
    `LastModified`, an hour early for a summer one. Its `sync` then finds a
    winter-stamped local copy newer than the object and downloads it again on
    every run (with `--exact-timestamps`, a summer-stamped one too), and a
    `sync` in the other direction uploads the winter ones again; here a second
    `sync` transfers nothing.
  - **A timestamp old enough that the zone's rule for it differs from the
    rule in force now**: `aws` resolves the fields under the *historical* rule
    and settles on a different instant than the object carries. Under
    `TZ=Asia/Tokyo` an object whose `LastModified` is `1601-01-01T00:00:00Z`
    comes out stamped `1601-01-01 09:00:00 +0918` on `aws` and
    `1601-01-01 09:18:59 +0918` here — the latter being the moment the object
    names.
  - **A timestamp the zone's history cannot place** (under `TZ=Africa/Abidjan`,
    `1912-01-01T00:16:07Z`): `aws` stamps nothing, leaving the file with the
    time it was written, prints `warning: Skipping file <path>. Successfully
    Downloaded <path> but was unable to update the last modified time. mktime
    argument out of range` after the download line and exits 2; here the run
    stamps the object's instant and exits 0.

  The bytes are identical throughout. Real S3 never reports a `LastModified`
  as old as the last two cases need, so those take an S3-compatible
  implementation that does; the first needs only the zone.
- **Default checksum algorithm without awscrt.** Without `--checksum-algorithm`,
  the requests that carry a client-computed checksum use `CRC64NVME` here as on
  `aws` v2 — uploads, deletes, bucket configuration writes, the annotation
  writes a `--copy-props all` multipart copy sends — as long as the installed
  botocore can compute it, which takes awscrt (the `crt` extra). Without
  awscrt the installed botocore's own default stands instead (`CRC32` on
  current botocore; [`../compatibility.md`](../compatibility.md) for older
  ones); `aws` always bundles awscrt. None of these changes the result or the exit code;
  what differs is the checksum left on an uploaded object (a full-object
  CRC64NVME against a composite CRC32). An explicit `--checksum-algorithm`
  makes the two agree regardless. The batched deletes the CRT engine sends
  carry the installed botocore's own default even with awscrt; `aws` sends
  no such request — its deletes go one key at a time, with no
  checksum.
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
  full. A help page that cannot be written ends differently as well: `aws`
  hands its page to a pager process and exits 0 whatever becomes of it, while
  this command writes the page itself, so `help > /dev/full`, or a reader that
  closes at once, is exit code 120 here.
- **A delete answered `412 PreconditionFailed` fails here.** `aws` turns a
  `PreconditionFailed` answer into a silent skip whatever the request was - a
  rule meant for `--no-overwrite`'s conditional writes - so a `DeleteObject`
  answered that way prints nothing and does not count: `rm`, a `mv`'s source
  deletion and `sync --delete` end at exit code 0 with the object still there,
  and `rb --force` goes on to remove the bucket. This command reports it as a
  failed delete (`delete failed:` / `move failed:`, exit code 1; 255 for
  `rb --force`, whose bucket removal then does not run). S3 itself never
  answers an unconditional delete this way; only an S3-compatible endpoint
  can.
- **`file://` and `fileb://` are refused where `aws` mishandles them.** Each
  of these exits 252 here, before anything is read or sent:
  - **A `fileb://` path argument** of `ls`, `rm`, `mb`, `rb`, `presign` or
    `website`. `aws` loads the bytes as the path, and each command breaks
    its own way: `mb`, `rb` and `presign` exit 252, `ls` and `website` crash
    at 255, and `rm` decodes the bytes back into a path and deletes it.
  - **A `file://` bucket for `website`.** `aws` takes the first character of
    the file as the bucket: a file holding `s3://mybucket` configures the
    website of bucket `s`. A `fileb://` one is refused too.
  - **A whole `--metadata fileb://...`.** `aws` crashes at 255; a map is
    text, so use `file://`.
  - **A single `--grants` value with either prefix.** `aws` loads the file
    and reads it as grants one character at a time, so the run fails at exit
    code 1, `--dryrun` included, while an empty file sends no grant and
    succeeds. Give each grant as an argument instead.

  Everywhere else the two prefixes work as on `aws`: a `file://` path
  argument (other than `website`'s) is read as the path, and option values
  load as before.
- **Deletes go out in batches.** Up to a thousand keys go out per
  `DeleteObjects` request here, where `aws` sends one `DeleteObject` per key.
  Batching is a settled design, and the differences it causes — this entry's,
  and the Ctrl-C during deletes under the CRT engine below — are accepted
  rather than open: where a batch can cheaply be brought closer to `aws`'s
  per-key outcome it is, by sending keys again one by one. For a run that
  finishes enumerating, the objects removed and the exit code are the same (a
  key that no longer exists is a success on both wire shapes) unless the
  endpoint keeps failing requests (below). A batch request that fails as a
  whole — a missing bucket, a refusal, a server error — has its keys sent
  again one by one (below). A key the batch reports with a fault the service
  asks to have retried — `InternalError`, `SlowDown`, `ServiceUnavailable`,
  `RequestTimeout` — is sent again as a `DeleteObject` of its own and retried
  as `aws` retries it. (With retries turned off — `AWS_MAX_ATTEMPTS=1` — on
  the classic engine it is not sent again and fails as it does on `aws`; the
  CRT client retries whatever that setting says, so under it the key is always
  sent again.) Only the batching routes are affected — `rm --recursive`, an
  S3-side `sync --delete`, and `rb --force` — while a single
  `rm s3://bkt/key` still issues `DeleteObject`.

  Batching is also what makes those same three routes end differently when the
  **listing that feeds them dies part way** — the service failing mid-pagination,
  an entry the response cannot supply. Both tools stop and exit nonzero, but
  `aws` has already issued its own `DeleteObject` for every key enumerated so
  far, while here the keys still sitting in the unsent buffer are dropped
  without being deleted and without a record: **up to 999 objects that `aws`
  would have removed survive**. Re-running the command deletes them, and a run
  that enumerates to the end is unaffected.

  A **Ctrl-C** while deletes are in flight shows it too. `aws` cancels its
  per-key delete futures and prints nothing after the interrupt (measured:
  ten deletes in flight, at most one line), while a batch request already out
  here cannot be cancelled: it completes, and the lines of the keys it carried
  — up to a thousand — print before `cancelled: ctrl-c received`, as do those
  of the per-key requests already out. A per-key request not yet started is
  not sent and prints nothing, as on `aws`. (Under the CRT engine see the
  entry below.)

  One batch request also has one set of attempts, where each of `aws`'s
  per-key requests has its own, so **an endpoint that fails requests** —
  dropping connections, answering with an error — can end differently too,
  even when it fails just one. A batch request that fails as a whole, whatever the
  failure, is sent again key by key, each key taking its own `DeleteObject`'s
  outcome, whatever the retry setting says (the batch was no key's own
  request, so even under `AWS_MAX_ATTEMPTS=1` each key still gets the one
  attempt `aws` gives it). A key can so get more attempts than `aws` gives
  it, never fewer, and an endpoint failing often enough to defeat `aws`'s
  per-key requests can leave this command deleting objects, with their
  `delete:` lines, that `aws` leaves in place: dropped connections make `aws`
  lose such a key without a line at exit code 0 on the classic engine
  (measured: six dropped requests for two keys) and report it at exit code 1
  under the CRT (measured: twelve), and error answers make it report the key
  at exit code 1 (measured on the classic engine: six `InternalError`
  answers for two keys, or a single 429 or 403, or a single 500 under
  `AWS_MAX_ATTEMPTS=1` — where three `InternalError` or `Throttling` answers
  leave both tools deleting both). A delete that keeps failing ends the same
  on both tools, bar one whose requests die without any response (the first
  entry of this section).

  Under the CRT engine (`preferred_transfer_client = crt`) the batches ride
  the CRT client, as `aws`'s per-key deletes ride its own — `rm` and a
  `sync --delete` from a local directory (an S3-to-S3 sync is on the classic
  client on both tools) — so the CRT's retry policy and the `[s3]` keys that
  tune the CRT client apply to them as they do on `aws`. The CRT re-sends a
  server error answer itself (a 500, a 503, a `RequestTimeout`), and once its
  attempts are spent the batch's keys are sent again key by key, as on the
  classic engine.
- **A plain-HTTP endpoint named anywhere but `--endpoint-url` still reaches
  the CRT engine.** Under `preferred_transfer_client = crt`, `aws` decides
  whether its CRT client speaks TLS from `--endpoint-url` alone, so an
  endpoint supplied any other way — `AWS_ENDPOINT_URL_S3` or
  `AWS_ENDPOINT_URL`, the profile's `endpoint_url`, a `[services]` block's
  `s3 = endpoint_url` — leaves TLS on: a `http://` endpoint is dialed over
  TLS and the transfer dies with `AWS_IO_SOCKET_CLOSED` (all four measured).
  Here the scheme is read off the endpoint the client actually resolved,
  whichever of those named it, so the same run transfers — the exit code and
  the resulting S3 state differ, in the direction of working. Pass
  `--endpoint-url` and the two agree again
  ([`../../design/crt.md`](../../design/crt.md) section 3 records the
  measurement).
- **Ctrl-C during deletes under the CRT engine.** For transfers the two
  agree: an interrupt landing in the CRT engine's transfer drain is swallowed
  by the CRT manager on both tools, which print their per-item failure lines
  and no closing line. For deletes `aws`'s CRT manager swallows it too and
  goes on sending every per-key delete already queued, then ends with the
  per-key lines — a request the interrupt cancelled fails with
  `AWS_ERROR_S3_CANCELED` — and no closing line, exit code 1 (measured:
  thirty keys, all deleted). This command's deletes
  are batched — an accepted difference (above) — and stop at the interrupt as
  on the classic engine: the
  requests already out finish, no further one starts, and the run closes with
  `cancelled: ctrl-c received`, exit code 1 — so objects `aws` would still
  have deleted survive. Re-running the command deletes them. A single-key `rm`
  sends one request, unbatched, and the same rule holds: the request out
  finishes and is reported (`delete: s3://…`) before that closing line, where
  `aws` cancels it and reports `delete failed: s3://… AWS_ERROR_S3_CANCELED:
  Request successfully cancelled` with no closing line, exit code 1 on both.
- **A CRT download that writes no file fails here.** Under
  `preferred_transfer_client = crt`, a download whose file cannot be put in
  place - a name longer than the filesystem allows, a directory already
  standing at the destination - is reported by the CRT engine as a success
  (`s3transfer` drops the failed rename of its temporary file). `aws` prints
  `download:` (with a warning that the modification time could not be set,
  and exit code 2, where the name is too long; exit code 0 otherwise), and on
  `mv` deletes the S3 object, so the object is gone and no file was written
  (measured). This
  command reports the item as `download failed:` / `move failed:` with the
  operating system's error, exits 1, and keeps the object - the classic
  engine's outcome on both tools.
- **A `mv` between two spellings of one Outposts object is refused here.** An
  Outposts access point's key can be set off from the ARN with `:` or with `/`,
  and a name the access-point pattern cuts short at a character a name may not
  hold reads as the shorter name and a key (`accesspoint/myap_x` is the access
  point `myap` and the key `_x`, also spelled `accesspoint/myap/_x`).
  `aws` compares its two arguments as text, takes
  `mv s3://<arn>:k s3://<arn>/k` (or the two spellings of a cut-short name) for
  a move between two objects and sends it — onto itself. This command recognizes the one object and stops: after the
  same "may resolve to same underlying s3 object(s)" warning `aws` prints, it
  ends with `fatal error: Cannot mv a file onto itself: ...` and exit code 1.
  (A `mv` whose two arguments are the same text is refused earlier, as a usage
  error with exit code 252, on both tools.)
- **An S3 Express access point is treated as its directory bucket by `sync`
  and `--case-conflict`, and as an access point alias by `mv`.** `aws`
  refuses `sync` on an S3 Express
  directory bucket (a name ending `--x-s3`), whose listing is in no particular
  order, and for the same reason refuses `--case-conflict skip` / `error` on a
  download from one and turns `warn` into a standing warning. It recognizes
  the bucket by the `--x-s3` suffix alone, so it runs both through an access
  point of a directory bucket (a name ending `--xa-s3`), whose listing is just
  as unordered. There `sync --delete` pairs keys wrongly and deletes
  destination objects the source also has (measured on real S3: of 20
  identical objects, `aws` left between 5 and 13, a different number each
  run), and `--case-conflict skip` skips
  objects that conflict with nothing. This command applies the directory
  bucket's rules to its access points too: `sync` exits 252 with `Cannot use
  sync command with a directory bucket.`, and `--case-conflict` takes the
  directory bucket's branch. The same gap reaches `mv`: `aws` neither warns
  about nor resolves an access point of a directory bucket, so moving its key
  onto the same key of the directory bucket copies the object onto itself and
  deletes it, silently. This command treats it like an access point alias:
  it prints `aws`'s "may resolve to same underlying s3 object(s)" warning,
  and under `--validate-same-s3-paths` resolves the access point and refuses
  the move with exit code 252.
- **Copying a directory without `--recursive`.** A `cp` or `mv` whose local
  source is a directory fails — exit code 1 on both tools, the source left in
  place — unless a filter excludes the source (`--exclude 'd/'`, its
  trailing-separator form, or `--exclude '*'`), when both tools do nothing and
  exit 0. Under `--dryrun` both tools instead exit 0 and preview the doomed
  upload, and the previewed line differs: `aws` threads the source through in
  its trailing-separator form and prints `(dryrun) upload: d/ to s3://bkt/` —
  with the destination key folded away — where this command prints
  `(dryrun) upload: ./d to s3://bkt/d`.
- **The `--version` line.** `aws` names itself and its interpreter; this
  command names the four packages that decide its behavior —
  `boto3-s3-cli/<v> boto3-s3/<v> boto3/<v> botocore/<v> Python/<v>
  <System>/<release>` — and has no `exe/<machine>` install-source token to
  report. Anything keying on the `aws-cli/<version>` token will not match.
- **No interactive prompt.** `--cli-auto-prompt` and `--no-cli-auto-prompt`
  are accepted and ignored, and `AWS_CLI_AUTO_PROMPT` and the profile's
  `cli_auto_prompt` are not read: the command runs as typed, where `aws`
  would prompt for the rest.
- **`cli_history` is not read.** With `cli_history = enabled` in the selected
  profile, `aws` records the run in a history database, and when it cannot
  open that database it writes one line to stderr ahead of everything else —
  `Warning: Unable to record CLI history. Check file permissions for <path>` —
  without changing the exit code. This command has no history mechanism, so it
  records nothing and warns about nothing; the rest of the run is unchanged.
  **When the database's directory cannot be created the run comes out
  differently**: `aws` stops before doing anything — exit code 255 with the
  file-system error (`[Errno 17] File exists: ...`,
  `[Errno 13] Permission denied: ...`), a `help` page included, `--version`
  excepted — while this command runs normally.
- **`~/.aws/cli/cache/session.db` is not written.** On the first client it
  builds, `aws` opens — creating the directory and the file if they are not
  there — an SQLite database at that path, and keeps a host id and a rolling
  session id in it; the session id then rides its User-Agent as a `sid/`
  component. This command has no such store: it creates nothing, reads nothing,
  and sends no `sid/` component (its User-Agent already differs, below). `aws`
  swallows every failure of that machinery, so a directory it cannot write
  changes nothing observable there either — the file simply stops being touched
  once a script switches over.
- **`~/.aws/cli/alias` is not read.** `aws` reads that file on every
  invocation: its `[command s3]` entries add or replace `aws s3` subcommands,
  and a `[toplevel]` `s3` entry replaces `aws s3` itself. This command applies
  none of them, and a file there is no error; a shell alias or function does
  the same job.
- **`[plugins]` is not read.** `aws` loads the plugins the config file's
  `[plugins]` section names (with `cli_legacy_plugin_path` set, every other
  entry is imported and its `awscli_initialize` called on every invocation),
  so their handlers run and a plugin that cannot be loaded stops `aws` before
  anything happens. This command loads none: the section is ignored, a broken
  entry is no error, and no plugin's handler runs.
- **User-Agent.** Requests identify themselves as the installed
  `Boto3`/`Botocore`, not as `aws-cli`, and carry none of aws's command
  metadata (`md/command#s3.ls` and the like). Visible only to the server and
  in `--debug` traces; anything keying on the aws-cli User-Agent (bucket
  policies, access-log analytics) will classify these requests differently.
- **The `PYTHON*` environment variables reach this command and not `aws`.**
  The official `aws` distribution is a frozen interpreter running in isolated
  mode, so the interpreter ignores that whole family; this command runs on
  your own Python, which does not. `PYTHONIOENCODING` therefore re-codes what
  is printed here while `aws` keeps its own codec (UTF-8 on Linux, the host
  code page on Windows): under `PYTHONIOENCODING=ascii` a `café.txt` result
  line comes out `caf?.txt`, and under `latin-1` it comes out in latin-1
  bytes. One member `aws`'s own code reads back: `PYTHONUTF8=1`, which it
  applies only to the streams it writes error reports on; here it is the
  interpreter's own UTF-8 mode and reaches every stream. `PYTHONUTF8=0` with
  `PYTHONCOERCECLOCALE=0` under a C locale goes the other way and makes a
  UTF-8 config file unreadable here — exit code 255 — where `aws` still reads
  it. The same packaging decides one thing more: an
  argument carrying a byte that is not valid UTF-8 is accepted here and the
  run proceeds, while `aws`'s bootloader rejects it before Python starts,
  exiting 255 with `Failed to set sys.argv: decoding error`. Leave the family
  unset and the two agree: under a plain C, ISO-8859-1 or ASCII locale, config
  reading, `file://` decoding, and the scanning and display of file names all
  match.
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
  That last one can change a run's outcome: under
  `preferred_transfer_client = crt`, a `REQUESTS_CA_BUNDLE` the CRT cannot
  read or use — a missing path, a file that is not a PEM bundle — fails the
  command here at exit code 255 with awscrt's or the operating system's own
  message, where `aws`, never reading it on that engine, runs the command as
  if the variable were unset (measured on `cp`, `rm`, `sync` and their
  dryruns).

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

## 4. Options limited by one direction

`cp` / `mv` / `sync` accept the same options on every route, but not every
option applies to every route, and — following `aws` — the treatment is not
uniform. These are rejected outright on the wrong route:

| Option | Valid route |
| --- | --- |
| `--checksum-algorithm` | upload, or S3-to-S3 copy |
| `--checksum-mode` | download |
| `--sse-c-copy-source`, `--sse-c-copy-source-key` | S3-to-S3 copy only |
| `--no-overwrite` | any route but a download to standard output (`cp s3://… -`) |

Every other direction-specific option is accepted anywhere and simply has no
effect off its route. The write-side options in particular — `--acl`,
`--storage-class`, the content headers, `--metadata`, `--grants`, the SSE
family — are silently ignored on a download rather than rejected.

## 5. Filtering on Windows

`--exclude` / `--include` patterns match case-insensitively on Windows, matching
`aws`. A backslash is treated as a separator there, in a pattern and in an S3
key alike: `logs\*.txt` matches `logs/x.txt`, and `logs/*` matches a key
spelled `logs\x.txt`. On Linux and macOS a backslash stays a literal
character. The `boto3-s3` Python library applies neither rule — that
tightening belongs to this command.
