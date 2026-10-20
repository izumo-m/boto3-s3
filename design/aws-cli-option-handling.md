# aws-cli Option Handling

This document describes how `boto3-s3` (library) and `boto3-s3-cli` (CLI)
treat each `aws s3` option, including the ones that are recognized but do
nothing and the ones that are not supported at all.

The policy is uniform across all nine `aws s3` subcommands because they
share the same command machinery (see section 2.1).

Reference points:

- Reference CLI: **the pinned aws-cli** (AWS CLI v2). `aws s3` semantics
  rarely shift within the v2 line.
- The effective (functional) options and their mapping to S3 API
  parameters are derived from aws-cli's
  `awscli/customizations/s3/`, the canonical reference for
  compatibility.

---

## 1. The two layers

Options are handled at two different layers, and "invalid" means
different things at each:

- **Library (`boto3-s3`)** sees only the *effective* options, as
  snake_case method parameters (e.g. `storage_class`, `sse`). It never
  sees presentation/transport CLI options. The only library-level
  "does not apply" case is an option that is valid only on certain
  transfer directions (section 4).
- **CLI (`boto3-s3-cli`)** is the surface that accepts the full
  `aws s3` flag set, as `aws s3` does. It maps the effective flags down to the
  library and is responsible for the options that have *no effect*
  (section 2) or are *not supported* (section 3).

## 2. Recognized-and-ignored (no-op) options - CLI

Several `aws s3` global options have **no observable effect** on any
`aws s3` subcommand's outcome (`--cli-error-format` changes only how aws
renders an error report, below). As aws-cli does, `boto3-s3-cli` **accepts them after
parsing and then ignores them** (validating `choices` where aws-cli
does, so an invalid value still errors the same way). Passing them
changes nothing about the operation.

| Option | Handling |
|---|---|
| `--output <fmt>` | Accept & ignore. Validates `choices`. |
| `--query <jmespath>` | Accept, but validate: aws compiles the value as JMESPath at parse time and rejects a bad expression (rc 252) before dispatch. The *result* is still ignored - there is no response payload to apply it to (section 2.1). |
| `--no-paginate` | Accept & ignore. |
| `--no-cli-pager` | Accept & ignore. |
| `--color {on,off,auto}` | Accept & ignore. Validates `choices`. |
| `--cli-error-format {legacy,json,yaml,text,table,enhanced}` | Accept & ignore. Validates `choices`. |
| `--cli-binary-format {base64,raw-in-base64-out}` | Accept & ignore. Validates `choices`. |

### 2.1 Why these are no-ops

All nine `aws s3` subcommands are `BasicCommand` subclasses
(aws-cli's `awscli/customizations/s3/subcommands.py`:
`S3Command(BasicCommand)` -> `ListCommand` / `WebsiteCommand` /
`PresignCommand` / `MbCommand` / `RbCommand`, and
`S3TransferCommand` -> `CpCommand` / `MvCommand` / `RmCommand` /
`SyncCommand`). A `BasicCommand` returns an **integer exit code** from
`_run_main`, not a response payload
(aws-cli's `awscli/customizations/commands.py`). The CLI
driver's response-formatter pipeline therefore never runs, so
`--output`, `--query`, `--no-paginate`, and `--no-cli-pager` have
nothing to act on.

`--query` is a partial exception: aws-cli's `globalargs` resolves (compiles)
the JMESPath expression at `top-level-args-parsed`, independent of the
response-formatter pipeline, so a malformed expression is still a usage error
(rc 252) even though the compiled value is never applied to anything.
`boto3-s3-cli`'s `globalargs.validate_query` matches this - the dispatcher
calls it once the command line is parsed (cli.md section 2).

`--color` is a no-op for a related reason: the parsed `color` global is
consulted only by other parts of aws-cli (the table formatter, the error
handler, the ECS monitor commands), never by `aws s3`; the `aws s3`
customization
(aws-cli's `awscli/customizations/s3/`) contains no color handling
and prints its `upload:` / `download:` / progress lines unconditionally.
`aws s3 ... --color on` therefore produces no color, and `boto3-s3-cli`
matches that.

`--cli-error-format` selects how aws-cli renders an error report. Error
reports are not a compatibility target ([`overview.md`](./overview.md)
section 3), so `boto3-s3-cli` accepts the flag and ignores it: its reports
keep one form whatever the value (a parameter-validation failure reads
`An error occurred (ParamValidation): <message>`).

`--cli-binary-format base64` (the default) base64-decodes only the parameters
the *API operation model* declares as a `blob` shape:
`base64_decode_input_blobs` walks the operation's `input_shape` and
`Base64DecodeVisitor` decodes a member only when `shape.type_name == 'blob'`
(aws-cli's `awscli/customizations/binaryformat.py`). The `aws s3`
SSE-C keys reach S3 as the `SSECustomerKey` / `CopySourceSSECustomerKey`
request parameters, and both are **`string` shapes in the S3 model**, not
`blob` shapes - so they are never base64-decoded regardless of the setting. The
only transformation applied to `--sse-c-key` / `--sse-c-copy-source-key` is
paramfile loading (`file://` text / `fileb://` bytes), a separate mechanism.
(Their CLI argument definitions carry `cli_type_name: 'blob'` and a help text
that says the key "should **not** be base64 encoded" - `subcommands.py`:
`SSE_C_KEY`, `SSE_C_COPY_SOURCE_KEY` - but `cli_type_name` governs
help/validation, not the model-shape-driven decode above.) `boto3-s3-cli`
therefore hands the library the raw string (or the bytes read from a `fileb://`
path); the library accepts a raw `bytes` key and does no base64 step of its own.

aws-cli registers a global URI paramfile handler on every `aws s3` argument -
positional and optional alike - so `file://` (text) and `fileb://` (binary)
load from disk for any *single-string* arg, not just the SSE-C keys, all
resolved at parse time before the command runs. (A list-valued argument - the
`--exclude` / `--include` filter accumulation, `mb`'s two-token `--tags` - is
not expanded: aws's paramfile hook rewrites only a plain string value, and
boto3-s3-cli keeps those verbatim too.) `boto3-s3-cli` matches this on the positional
too: `ls` / `rm`'s `<S3Uri>` positional (dest `paths`, aws's own `cli_name`
for it) and `mb` / `rb` / `presign`'s (dest `path`) are `file://`-expanded
before dispatch (`expand_positional_paramfile`, `commands/base.py`; a
`fileb://` there, and either prefix on `website`'s, is refused - below), and
so are the free-value options `--bucket-name-prefix`
/ `--bucket-region` (`ls`) and `--index-document` / `--error-document`
(`website`). On the transfer family (`cp` / `mv` / `sync`) the same expansion
covers the free-string transfer options (`--content-type`,
`--website-redirect`, `--cache-control`, `--content-disposition`,
`--content-encoding`, `--content-language`, `--expires`, `--source-region`,
`--sse-kms-key-id`, and `--expected-size` - a plain string in aws's table, so
a readable `fileb://` is rejected with the bytes 252 while its `int()` runs
only on the stream route at submit), the string-typed integer options
(`--page-size` / `--progress-frequency` / `--expires-in`, expanded before
their `int()` coercions - cli.md section 5.7), and `--metadata` (resolved
after both integer coercions, before its shorthand parse; the shorthand also
accepts aws's `key@=file://...` operator). The choices-validated options
(`--acl`, `--storage-class`, `--sse`, ...) cannot carry a `file://` value -
argparse rejects it as an invalid choice first - matching the practical aws
result. A load failure on any of these - a missing file, or a binary file via
the text `file://` prefix - is a usage error (rc 252) with aws's wording
(`Error parsing parameter '<name>': Unable to load paramfile ...`; `<name>` is
the positional's `paths`/`path` or the option's `--flag` form).

`fileb://` is not a free pass-through wherever it is accepted, though: only
the two SSE-C key blobs (`--sse-c-key`, `--sse-c-copy-source-key`) take the
loaded *bytes* verbatim into the request (a `string`-shaped request parameter
untouched, as above). Every other free-string option in the list above
still runs a
`fileb://` value through the same string-typed parameter check aws applies:
`--content-type fileb://x` loads the bytes and then fails botocore's
client-side check for a `string`-typed parameter (`Invalid type for
parameter ...`, rc 252) rather than passing them through unchanged.

Four paramfile uses are refused at 252 without reading the file, because aws
mishandles them - aws bugs this CLI does not copy
([`aws-differences.md`](../docs/cli/aws-differences.md) section 2):

- **A `fileb://` path positional** (`ls`, `rm`, `mb`, `rb`, `presign`,
  `website`). aws loads the bytes into the path and each command breaks its
  own way: `mb` / `rb` / `presign` reject them as a string parameter (252),
  `ls` and `website` crash in path handling (255), and `rm` decodes them
  back into a path and deletes it.
- **A `file://` path positional on `website`.** aws indexes the loaded text
  as though it were still a one-element list, so its first character
  becomes the bucket: a file holding `s3://mybucket` configures bucket `s`.
- **A whole-value `--metadata fileb://...`.** aws loads the bytes and then
  crashes indexing them in its shorthand parser (255).
- **A lone `--grants file://...` or `fileb://...`.** aws unwraps the
  one-element list before its paramfile hook, so its grant parser walks the
  loaded text character by character (bytes int by int) and fails in flight,
  or sends no grant at all for an empty file. With more than one `--grants`
  value nothing is loaded on either tool.

The `key@=...` shorthand form reports the same way: a reference it cannot
load - missing, or binary through the text `file://` - is the option's
`Error parsing parameter '<name>'` usage error (rc 252), and a `fileb://` one
that *does* load is refused for being bytes, a map value being text (rc 252).
aws wraps a paramfile failure into that form only in the `load-cli-arg`
handler it registers for *named* arguments, and its shorthand parser calls the
loader itself, so there the load failure reaches the general handler as rc 255
with the bare `Unable to load paramfile ...` line. That is its call path, not
a meaning: a file named on the command line that cannot be read is a
command-line error wherever it is named, so `shorthand.py` loads inside the
option's `paramfile.named_argument` scope too (an exit code in an error path,
not recorded as a difference - overview.md section 3).

aws's grammar also builds lists (`k=a,b`, `k=[a,b]`) and nested maps
(`k={a=b}`) and arms the operator inside them; a map of strings holds
neither, so aws parses them and then rejects them with botocore's type
report. `shorthand.py` refuses them where they start instead, with the
parser's own 252, and accepts exactly the values aws accepts.

## 3. Auto-prompt / completion UI

There is no interactive auto-prompt: the CLI is a by-product of the library,
and an interactive front-end has no place in either. `--cli-auto-prompt` and
`--no-cli-auto-prompt` are **accepted and ignored**, so a command line that
carries one still runs as typed, and `AWS_CLI_AUTO_PROMPT` and the profile's
`cli_auto_prompt` are not read. Shell completion (the `aws_completer`
equivalent) is not provided either. Recorded for users in
[`aws-differences.md`](../docs/cli/aws-differences.md).

## 4. Direction-specific options

Some effective options apply only to certain transfer directions -
upload (local->S3, `U`), download (S3->local, `D`), or copy (S3->S3, `C`).
How aws-cli treats one supplied on a route where it does not apply is **not
uniform**: some combinations are rejected as a usage error, others are silently
dropped. `boto3-s3-cli` and `boto3-s3` mirror aws-cli case by case.

**Case (a): rejected with a usage error (rc 252).** A few flags are
path-format-validated by aws-cli before any transfer starts, so the wrong route
hard-fails. `boto3-s3-cli` mirrors each rejection (same message, rc 252):

| Flag | Allowed route(s) | Wrong-route behavior |
|---|---|---|
| `--checksum-algorithm` | `<LocalPath> <S3Uri>` or `<S3Uri> <S3Uri>` | rc 252 usage error |
| `--checksum-mode` | `<S3Uri> <LocalPath>` | rc 252 usage error |
| `--sse-c-copy-source` / `--sse-c-copy-source-key` | `<S3Uri> <S3Uri>` (copy only) | rc 252 usage error |
| `--no-overwrite` | any route but a download to stdout (`cp <S3Uri> -`) | rc 252 usage error |

These mirror aws-cli `CommandParameters._validate_path_args` (checksum
pairing, in aws-cli's `awscli/customizations/s3/subcommands.py`),
`_validate_sse_c_copy_source_for_paths` (copy-source scope, same file) and
`_validate_no_overwrite_for_download_streaming` (same file).
The CLI ports are `validate_checksum_paths_type`
(in `cli/src/boto3_s3_cli/commands/transferargs.py`), the **copy-source
scope branch** of `validate_sse_c_pairing` (same file) and `cp`'s
streaming-path check (`commands/cp.py`). The rest of
`validate_sse_c_pairing` (mirroring aws-cli `_validate_sse_c_arg`)
enforces that `--sse-c` / `--sse-c-key` (and the copy-source pair) are supplied
together; that pairing check is route-independent and belongs to neither
case (a) nor case (b).

**Case (b): silently ignored, not rejected.** Every other direction-specific
option is accepted on any route and simply has no effect when the route does
not use it. `metadata_directive` / `copy_props` are copy-only and
`case_conflict` download-only; `guess_mime_type` is an internal flag carried on
every route but only consulted to infer `ContentType` on the upload path;
`--expected-size` is consulted only on the streaming upload. The same rule
covers the write-side request options on a download - `--acl`,
`--storage-class`, the content headers, `--metadata`, the SSE family,
`--grants` - because the `GetObject` mapping emits only SSE-C / request-payer /
checksum-mode: either the corresponding S3 request parameter is never emitted,
or the route-specific logic just does not run. aws-cli applies the shared
transfer arguments uniformly and acts only on the route-relevant ones.
`boto3-s3` does the same - the internal snake->PascalCase translation only
emits the parameters that are valid for the chosen direction - with one
exception: ignoring an option is not accepting a malformed one, so a
`--grants` value that is not `<permission>=<grantee>` with a known permission
is refused before the run (252) on every route, a download included, where
aws never parses it on a download (and on an upload or copy finds it only at
the first item).

The full direction matrix follows aws-cli; the canonical reference is
aws-cli's `awscli/customizations/s3/`.

## 5. Effective options -> where they go

For completeness, the options that **do** have an effect:

- **Connection / auth** (`--profile`, `--region`, `--endpoint-url`,
  `--no-verify-ssl`, `--ca-bundle`, `--no-sign-request`,
  `--cli-read-timeout`, `--cli-connect-timeout`): `boto3-s3-cli` turns
  these into the boto3 client/session it hands to the library, which
  takes a ready boto3 client (e.g. via `S3Storage`) rather than
  rebuilding connection settings itself.
- **Object / transfer shaping** (`--acl`, `--storage-class`, `--sse`,
  `--metadata`, `--content-type`, ...): snake_case method parameters,
  translated internally to S3 API PascalCase (see the design record).
- **`--debug`**: the library emits via the standard `logging` module
  under the `boto3_s3` logger hierarchy; `boto3-s3-cli` wires a stderr
  handler. The library itself attaches only a `NullHandler` to the
  `boto3_s3` package logger, and a stream handler when the caller asks for
  one through `set_stream_logger`.
- **`--version`**: prints a single aws v2-style version line
  (`boto3-s3-cli/<v> boto3-s3/<v> boto3/<v> botocore/<v> Python/<v>
  <System>/<release>`) and exits; handled entirely in `boto3-s3-cli`, with no
  library involvement.

## 6. Console output: the compatibility target and its recorded deviations

Console output is held to **functional compatibility**
([`overview.md`](./overview.md) section 3): under the same arguments and
conditions a successful run writes what `aws s3` writes, after the
normalization of the output comparison criterion ([`testing.md`](./testing.md)
section 9). A difference there that survives the normalization is a functional
difference - a bug, or one of the deliberate deviations recorded below. In an
error report aws's output is a reference rather than a target: a difference in
an error's details (its wording, which of two mistakes is reported, the order
of the reports) is neither a bug nor a deviation.

The program name and the command hierarchy are **class 1** of that
normalization rather than an exemption from it. The root tokens `aws` /
`boto3-s3` and everything derived from them - usage lines, the `aws: [ERROR]:`
/ `boto3-s3: [ERROR]:` prefixes, aws's `<command> <subcommand>` level collapsed
onto our single one - are rewritten on aws's side before the comparison, and
the surrounding message is then compared in full.

### Recorded deviations

These are the places `boto3-s3-cli` deliberately displays something other than
what `aws` displays. **The list is exhaustive for the display layer's
functional differences**: nothing here licenses an unrecorded one, and a new
one is added by the same explicit decision a new normalization rule takes.
Those a user sees on a successful run (the pipeline's five, the help pages
and `--debug` traces, the `--version` line, the missing `cli_history`
warning, and the `--dryrun` line of a directory-source copy below) are
carried in [`aws-differences.md`](../docs/cli/aws-differences.md) section 2
as well. The rest are kept here only as implementation notes: an interrupted
`mv`'s record, and the last, which no real run reaches.

The first five are the output pipeline's (`progress.py`; the rendering thread
itself mirrors aws-cli's `ResultProcessor`):

- **Bounded printer queue.** aws-cli feeds its printer thread through an
  unbounded queue: transfers are never slowed by console output, but a
  stalled consumer (say, `sync` piped into a pager that is never advanced)
  grows the result backlog in memory without limit. `boto3-s3-cli` bounds the
  queue (10,000 records): a consumer that falls that far behind
  back-pressures the transfer - the workers block on the queue - instead of
  growing memory. Result lines are never dropped either way.
- **Progress repaint floor.** aws-cli renders every progress record (one per
  transferred chunk). `boto3-s3-cli` floors repaints at 0.1 s (or
  `--progress-frequency` when higher), so repaint records enter the queue at
  a chunk-independent rate. Display cadence only; the painted numbers are
  exact snapshots.
- **No `~total (calculating...)` marker.** aws paints that marker on the
  progress line while its enumeration is still running. The library has no
  enumeration-finished signal to drive it, so the total is painted plain
  ([`cli.md`](./cli.md) section 5.7).
- **Progress is painted only from byte progress.** aws repaints the meter
  after every result line it prints as well (`_redisplay_progress`), so a run
  that never reports a byte - every transfer failing before its first chunk,
  or a `sync --delete` that only deletes - still shows a meter there and none
  here. The printer thread renders snapshots taken on the worker side at
  progress time, so painting on a result would mean reading the live counters
  from the rendering thread; taken together with the repaint floor above, the
  cadence is this pipeline's rather than aws's.
- **Deletions stay out of the meter's byte totals.** aws queues a
  `sync --delete` deletion like a transfer, so an S3-side deletion's size
  joins the expected bytes from the start — and only the expected: no bytes
  are ever counted transferred for it, so aws's byte meter ends short of its
  denominator (a local-side deletion queues with size 0 and moves neither).
  The library's delete lane reports only terminals, so here a deletion joins
  the file count at its own completion and its size joins neither byte
  total: the byte meter reads done over expected for the real transfers
  alone, and the two meters disagree mid-run and at the end.

One more is in the result text itself rather than in its rendering:

- **An interrupted `mv`'s record.** aws can delete sources for which it
  printed no `move:` line (measured); this command prints the line for every
  source it deletes.

The rest sit outside the pipeline:

- **Help pages** are rendered from this CLI's own argparse parsers, while aws
  renders its own documentation tree, so the two are laid out differently. The
  options, their values and their meanings are the same - that is what the
  page promises, not its typography.
- **`--debug` traces** are the installed boto3 / botocore's, not aws's
  bundled ones, and this project additionally masks credentials that aws
  prints in full - a safety feature beyond aws-cli, taken deliberately because
  security comes before compatibility ([`masking.md`](./masking.md)).
- **The `--version` line** names the four packages that decide this command's
  behavior (`boto3-s3-cli/<v> boto3-s3/<v> boto3/<v> botocore/<v>`) where aws
  names one, and it carries no `exe/<machine>` install-source token. This is
  the program identifying itself, but the shape of the line differs beyond a
  token rewrite, so it is recorded here rather than folded into the
  program-identity mapping.
- **The `--dryrun` line of a non-recursive directory source** names the
  source as given (`(dryrun) upload: ./d to s3://bkt/d`) where aws threads
  its trailing-separator form through its engines
  (`(dryrun) upload: d/ to s3://bkt/`). The source keeps its given form so
  that the run without `--dryrun` fails the item with the operating
  system's `[Errno 21]` (rc 1) (decided 2026-08-01).
- **No `cli_history` warning.** This command has no history mechanism, so it
  never writes aws's `Warning: Unable to record CLI history. ...` line.
- **`--human-readable` past EiB.** aws's suffix loop falls through above EiB
  and renders `None`; `human_readable_size` keeps counting in EiB instead
  (`output.py`). Unreachable in practice - a total below 1 EiB never gets
  there - which is why this one is not in the user-facing list.

One further difference comes from the **parser** rather than the display
layer: an ambiguous abbreviation used as an option value is rejected on
Python 3.10 / 3.11, recorded with its rationale in [`cli.md`](./cli.md)
section 2.
