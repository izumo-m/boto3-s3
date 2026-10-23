# boto3-s3 Exception Model (current state / source of truth)

This document is the **authoritative reference (source of truth)** for the
exception design of `boto3-s3`. When a design decision changes, update this
document first.

Related: the entry point to the design as a whole is
[`overview.md`](./overview.md).

---

## 1. Policy

- **Every error from the public API is reported as a `Boto3S3Error`-family
  exception** (success = no exception, error = always an exception; no return
  codes or error-report objects).
- The hierarchy is **`Boto3S3Error` (root) + 6 categories + 2 refining
  subclasses + the `BatchError` aggregate** (section 4). Collapsing everything into a single `botocore`-style
  `ClientError` is **rejected** (because boto3-s3 spans both the local FS and
  S3, it preserves a cross-cutting classification in which "an S3 403 and a
  local `PermissionError` belong to the same category").
- **The root is never raised directly for a known failure** - every raise site
  uses a category (or refining) class, so `except Boto3S3Error` is purely the
  catch-all. Direct base instances appear only where no classification exists:
  the error translators' last-resort fallback (section 3), the security
  guards against a server answering outside S3's API (section 3.1), and the
  message envelope on WARNED / NOTICE `OpResult` records.
- Backend exceptions (`botocore` / `OSError` / `urllib3`, etc.) are converted at
  the library boundary and the original is preserved on `__cause__` via
  `raise ... from <backend>` (never swallowed).

## 2. Hierarchy

```
Boto3S3Error                # root. The supertype of all library errors. Inherits Exception (not BaseException)
+-- AccessDeniedError       # S3 403 / local PermissionError
+-- NotFoundError           # S3 404 (NoSuchKey/NoSuchBucket) / local FileNotFoundError (e.g. a missing source path)
+-- ValidationError         # invalid argument / precondition / state
|   `-- InvalidValueError   # refinement: a value failing post-parse conversion (aws's bare int() -> its
|                           #   general handler, rc 255 - not the rc-252 usage path)
+-- TransportError          # network / local I/O failure (connection, timeout, OSError)
+-- ConfigurationError      # credentials / region missing or unresolvable (aws's dedicated handlers, rc 253)
|   `-- InvalidConfigError  # refinement: config present but invalid/unusable - aws-cli's InvalidConfigError
|                           #   counterpart (bad [s3] value, unusable profile, partial credentials; rc 255)
+-- CancelledError          # caller-initiated cancellation (CancelToken.cancel()).
|                           #   Unrelated to the CancelledError of asyncio / concurrent.futures
`-- BatchError              # aggregate: a batch operation's failure rollup (counts only; the sample failure rides on __cause__; section 4)
```

The two **refining subclasses** exist for the CLI's exit codes: aws reports
those failures through its *general* exception handler (rc 255), while
plain `ValidationError` / `ConfigurationError` map to the dedicated 252 / 253.
`exit_code_for` keys on the subclass before the parent (section 5). Library
consumers can ignore the distinction and catch the parent category.

Common fields of `Boto3S3Error` (shared across all categories):

```python
class Boto3S3Error(Exception):
    def __init__(self, message: str, *,
                 operation: str | None = None,   # subcommand name ("cp", etc.)
                 bucket: str | None = None,
                 key: str | None = None) -> None: ...
```

- Programming bugs (`TypeError` / `AssertionError`, etc.) are not wrapped on
  the synchronous paths - they pass through. Inside the asynchronous transfer
  engine every task exception must land in its item's record, so an
  unclassified one surfaces there wrapped in the base `Boto3S3Error` (the
  last-resort clause) instead. `KeyboardInterrupt` / `SystemExit` always pass
  through - including one the caller's own code raises on a transfer's worker
  thread (an event handler registered on the client, a stream the transfer
  reads or writes): the engine settles that item CANCELLED (a CRT upload's
  body, which awscrt reads, included) and re-raises the exception on the
  operation's thread
  (`transfer._TaskGuard`, `transfer._GuardedReader`). Whatever `on_result`
  raises reaches the caller the same way, from whichever thread the record
  settled on (`Transferrer._deliver`).
- **Intentional pass-through exception**: an explicit
  `preferred_transfer_client="crt"` while awscrt is absent (or older than
  boto3's minimum) is a `ConfigurationError` (rc 253 in the CLI) wherever the
  library resolves the config (`S3.resolve_transfer_config`, the decision tree
  of crt.md section 4), which every `cp` / `mv` / `sync` / `rm` and the CLI do.
  A config the library has not resolved - a plain boto3 `TransferConfig`, or
  one handed to `Transferrer` directly - is selected with boto3's rule and
  passes through the same `botocore.exceptions.MissingDependencyException` as
  boto3 does. This is a deliberate exception to the boundary conversion of
  section 1, kept to stay boto3-faithful ([`crt.md`](./crt.md) section 3 /
  section 6). The pass-through is scoped to
  that engine-selection path: the same exception surfacing inside a translated
  S3 call - SigV4a signing for an MRAP target without awscrt, say - converts to
  the plain `ConfigurationError` like the section 3 table's other environment
  errors.

### 2.1 The attribute contract (stable vs. display)

What a consumer may rely on across releases is **class identity** (the
hierarchy above) and the **structured attributes**: `operation` / `bucket` /
`key` on every `Boto3S3Error`, plus `BatchError`'s counters (`succeeded` /
`failed` / `warned` / `skipped` and the derived `total`, section 4).
**Message strings are display only**: they may change in any release, so
never parse `str(exc)` - branch on the class and read the attributes.

`__cause__` reachability: an error raised for a failed S3 request carries the
originating botocore `ClientError` on `__cause__` (the `raise ... from`
boundary conversion of section 1) - the CLI's rc-254 test reads exactly this
(section 5). Per-item failure records link the same way: the translation
stamps the original exception onto the record error's `__cause__` (a
pass-through `Boto3S3Error` keeps whatever cause it already has). `BatchError`
is two levels deep: its `__cause__` is the **translated** first failure (a
`Boto3S3Error`, section 4), never the raw backend exception, so an
exception-backed failure's original - a `ClientError` where the server was
reached - sits at `__cause__.__cause__`, and that is a guarantee. The
exception-free paths are the deleter's per-key synthesis from a
`DeleteObjects` response body (section 3) - an `Errors[]` entry - and the
security guards' failures (section 3.1), with no exception object behind them,
so their `__cause__` is `None` and only the message says what happened.

The context attributes are best-effort, and `operation=None` is a legitimate
value: it means no subcommand-scoped operation was in scope - client
construction, and a storage-level method the caller invokes itself -
`validate`, `open`, `delete`, `get_fileinfo`, the `scan` / `list_buckets`
listings - whose raise sites leave it unset because the storage cannot know
which operation is using it (the object listing backs `ls` / `rm` / `cp` /
`mv` / `sync` alike, so a name stamped there would mislabel four of them); an
operation making the same call stamps its own name, since only the operation
layer knows it (the single rule `sdkerrors.attribute_failure` applies for
every capture, so a `DeleteObject` denied under `rm` reads `"rm"` on the blind
single-key path and the batched path alike, and each operation wraps the pull
of its enumeration, so a listing failure reads the name of the run that was
listing). `S3Storage.get_file` / `put_file` are storage-level methods no
operation ever runs; nothing above them could name their failures, so they
stamp `"get_file"` / `"put_file"` themselves. A locally-originating error carries a filesystem path in `key`
(and no `bucket`) when set: the field names the failing entry in the backend's
own address space, not always an S3 key.

The engine fills the same three attributes on every family error it records as
a per-item failure: it supplies the run's `operation` and the item's `bucket` /
`key` where the error left them unset, and never overwrites what the raiser
authored. That is how an error raised in-pipeline - inside a backend's `open`
or its reads and writes, where only the location is known - reaches the caller
attributed, without the run's context having to be threaded into every raise
site. `bucket` and `key` fill **as a pair**: an error that set only `key` named
the entry in its own address space (a local path, `bucket` unset by contract),
and pairing this item's bucket with it would invent a mixed address. A
`BatchError` is skipped by that pair fill entirely - it stands for a whole run,
and its constructor (section 4) takes no `bucket` / `key`, so both are always
`None`. The fill is in place on the recorded object, so a raiser that reuses one
exception instance across items sees the first item's attribution on every later
record: attribution is best-effort, not per-record.

Custom backends: an exception a custom `Storage` raises that is not already a
`Boto3S3Error` is wrapped into the **base** `Boto3S3Error` when it surfaces as
a per-item failure (the translators' last-resort clause, section 3), and so is
one raised while a `sync` content comparison (`EtagComparison` /
`ChecksumComparison`) reads the custom side, which aborts the `sync`
(`comparator`'s `_read_readable`); one raised during enumeration (`scan`) or
elsewhere on the fatal path propagates **as-is**.
The library assumes a well-behaved backend that maps its own errors to this
taxonomy ([`storage.md`](./storage.md) section 2).

## 3. backend / local -> category mapping (representative examples)

| Origin | Category |
|---|---|
| S3 403 / `AccessDenied` | `AccessDeniedError` |
| S3 404 / `NoSuchKey` / `NoSuchBucket` / `NoSuchVersion` / `NotFound` | `NotFoundError` |
| S3 `InternalError` / `SlowDown` / `ServiceUnavailable` / `RequestTimeout` (5xx / throttle) | `TransportError` |
| local `PermissionError` | `AccessDeniedError` |
| local `FileNotFoundError` / a missing source path | `NotFoundError` |
| a local path the OS calls refuse before the filesystem is asked (an embedded NUL's `ValueError`, a lone surrogate's `UnicodeEncodeError`), on `LocalStorage`'s own calls (`get_fileinfo`, `open`, a recursive download's destination directory); a single transfer's local file fails the item with the base `Boto3S3Error`, and an upload source with such a name is a missing source (`NotFoundError`) | `ValidationError` |
| connection failure / timeout / a body ending short of its `Content-Length` (botocore's `IncompleteReadError` under urllib3 1.x, a broken stream under urllib3 2; both measured, 1.x at the SDK floor and on a current SDK; on the classic engine's download, s3transfer retries the read and raises `RetriesExceededError`, "Max Retries Exceeded", once its attempts run out); a local-I/O `OSError` caught on boto3-s3's own paths (incl. a failed `makedirs`) | `TransportError` |
| a transfer item's local file failing inside either engine - s3transfer's `OSError` (aws's message survives verbatim, e.g. `[Errno 21] Is a directory`), awscrt's `AWS_ERROR_NO_PERMISSION` / `AWS_ERROR_FILE_INVALID_PATH` / `AWS_ERROR_FILE_*_FAILURE` / `AWS_ERROR_NO_SPACE` (`transfer.Transferrer._local_failure`) | the local rows above, naming the file in `key` with no `bucket` |
| a CRT engine network or server failure, by awscrt's error name (`sdkerrors.crt_error_name`: an `AwsCrtError`'s, or the `RuntimeError` `<code> (<name>): ...` a request awscrt refuses synchronously raises): `AWS_IO_SOCKET_*` / `AWS_IO_DNS_*` / `AWS_IO_TLS_*`, a closed connection, a first-byte timeout, S3's `SlowDown` / internal error the CRT client gave up retrying | `TransportError` |
| `NoCredentialsError` / `NoRegionError` | `ConfigurationError` |
| `MissingDependencyException` from a request/signing path (awscrt absent where SigV4a is required - an MRAP target) | `ConfigurationError` |
| `ProfileNotFound` / `PartialCredentialsError` / botocore's own `InvalidConfigError` family (the library translator's list; the CLI's client factory goes further and maps every other construction-time `BotoCoreError` here too, while the library's general translator keeps unlisted ones at the base) | `InvalidConfigError` |
| an `[s3]` / config-file value that does not convert (`transferconfig` / `awsconfig`) | `InvalidConfigError` |
| a post-parse option-value conversion failure (`--page-size abc`, the CLI timeouts); the CRT signer's bare `AssertionError` on a non-positive `presign` expiry (`S3.presign`, the assertion kept as `__cause__`) | `InvalidValueError` |
| `ParamValidationError` / invalid argument / violated precondition (stdin absent, case-conflict `error` mode; an `ls` bucket filter on a botocore without the paginated `ListBuckets`, refused before any request) | `ValidationError` |
| a transfer argument s3transfer refuses synchronously at hand-over (`InvalidCrtTransferConfigError` at the manager build, the CRT engine's `ValueError` for a checksum algorithm awscrt cannot compute) | `ValidationError` |
| a CA bundle awscrt cannot read (`FileNotFoundError` / `IsADirectoryError` / `PermissionError`) or parse (its `RuntimeError`) while the CRT engine is built (`transfer.crt_engine_errors`, at the transfer engine's seam and `S3.materialize_crt_engine`) | `InvalidConfigError` |
| a credential the CRT engine's compatibility check resolves for a client whose credentials object is not the one the engine was built with (a refused `AssumeRole`, an unreachable STS); the engine's own client, and every client of the same session, is admitted without resolving | the request's category (`AccessDeniedError` / `TransportError` / ...), through `s3_errors` |
| an SDK floor missing a capability (`no_overwrite` on an old botocore) | `ConfigurationError` |
| `CancelToken.cancel()` | `CancelledError` |

`CancelToken.cancel()` defaults to `CancelMode.GRACEFUL`: operations stop
accepting new work, discard work not yet accepted, drain accepted work (a
delete lane instead discards the deleter's unsent buffer - deleter.md
section 2), reclaim their workers, and then raise `CancelledError`. Passing
`mode=CancelMode.IMMEDIATE` additionally requests best-effort cancellation of
pending and in-flight futures. Synchronous external I/O already running cannot
be killed safely and may still finish - such a completion reports its real
outcome, while a revoked accepted item reports one `CANCELLED` record
([`opresult.md`](./opresult.md), the `on_result` contract; a fatal error or
a Ctrl-C cancels the same way, on either transfer engine). Cancellation is
monotonic and idempotent:
an immediate request upgrades graceful cancellation, and later requests never
downgrade it. `on_result` may call `cancel()`; it only changes token state and
never shuts an engine down from the callback's worker thread.

Local `OSError`s are converted by one shared translator
(`localstorage.translate_os_error`, the local mirror of
`sdkerrors.translate_boto_error`; the transfer engine hands it the `OSError`s
of an item's local file): `FileNotFoundError` -> `NotFoundError`,
`PermissionError` -> `AccessDeniedError`, everything else -> `TransportError`,
and a path the OS calls refuse (`ValueError`) -> `ValidationError`.

One local failure never reaches a category at all: the post-download `utime`
stamp. `transfer.py` re-words its EPERM into aws's "attempting to modify the
utime" text, but the caller catches every exception from the stamp (Windows can
raise `OverflowError` / `ValueError` there too) and folds only the message into
a WARNED record, whose `error` is a base `Boto3S3Error` like every other
warning envelope. The category the re-wording built is therefore never
observable.

An S3 `ClientError` code is matched first against `S3_CODE_CATEGORIES`
(`sdkerrors.py`); a code not in the table falls back to HTTP-status widening:
403 -> `AccessDeniedError`, 404 -> `NotFoundError`, 5xx -> `TransportError`,
other 4xx -> `ValidationError`, otherwise the base `Boto3S3Error`. The error
*translation* creates a direct base instance in only three places:

- that final widening fallback;
- `translate_boto_error`'s last clause, for an exception no earlier clause
  claims - a CRT failure under a name the table above does not list (a
  cancellation, an unknown error), an `OSError` a request raised (a credential
  provider's: a missing `credential_process` executable or web identity token
  file - not the object's absence, so never `NotFoundError`), and whatever
  else a request raised from inside botocore outside botocore's own family
  (`sdkerrors.request_failure`). Every request point applies
  it - the per-item captures, the single-call operations, and the storage's
  own requests through `sdkerrors.s3_request` (the listing page by page, the
  single-object HEAD, `open` / `delete` / `get_file` / `put_file`) - so a
  failed request is a family error whatever botocore died of, with
  `AssertionError` alone passing through. The capture wraps the request
  only: what the library's own reading of the result raises keeps its type;
- the deleter's per-key `Errors[]` translation, whose entries carry a bare code
  with no HTTP status to widen on (an unknown code becomes the base category,
  deleter.md section 3).

Two kinds of direct-base site remain outside the translation: the security
guards' failures (section 3.1), and the message envelope on WARNED / NOTICE
`OpResult` records (`Warner.warn` / `Warner.notice` in `transfer.py` -
section 1).

### 3.1 Responses outside S3's API

The library relies on S3, or an S3-compatible server, answering as S3's API
defines ([`overview.md`](./overview.md) section 3). What a response outside
that definition leads to - a missing element, a value the API does not define
or of another type, a value that cannot be read - is not specified: the run
may fail, may go on and succeed, or may come out differently. No code checks
for such a response, works around it, gives its failure a class of its own,
or matches what aws-cli does with it. The table above and the rules of
section 1 are for responses within the API; a failure such a response causes
is whatever the code that met it raised (inside a request, the capture above
reports it as the base error; in the library's reading of a result, it is
the bare exception).

One exception is security: a server, faulty or hostile, must not be able to
make a run loop or grow without bound. These guards check that much. A guard
of the library's own fails with the base `Boto3S3Error`, no `__cause__`
behind it, attributed to the item or request it stops; what botocore refuses
arrives through the request capture like any other failed request:

- **Paginated listings** (`ListObjectsV2`, `ListBuckets`,
  `ListObjectAnnotations`) ride botocore's paginator, which refuses a
  continuation token repeated on the next page (`PaginationError`, reported
  by the request capture). Nothing more is refused: an endless listing from
  a server - ever-fresh tokens, or a cycle longer than one page - cannot be
  told from a large bucket, so only what botocore refuses is refused. The
  object listing streams its pages through a bounded queue, so memory stays
  bounded while it runs.
- **The multi-region access point lookup** (`S3PathResolver`, behind the
  CLI's `--validate-same-s3-paths`) pages `ListMultiRegionAccessPoints`
  itself, and stops on a `NextToken` equal to the previous one, botocore's
  paginator rule; a longer cycle is left running for the same reason as a
  listing's.
- **The part listing a COMPOSITE checksum comparison reads**
  (`checksumcompare._part_sizes`, `GetObjectAttributes`) is followed only
  while its marker moves past the page just read and stays below S3's
  10,000-part ceiling. A listing that does not is not followed; the
  comparison reads as indeterminate, which copies, like every other
  indeterminate comparison.
- **Annotation copying** (`copy_props=ALL` on a multipart copy) fails the item
  past S3's limits on one object's annotations: more than 1,000 names, or a
  payload over 1 MiB (read with a cap, never whole).
- **A region redirect that keeps redirecting** inside botocore ends in
  Python's `RecursionError`, which the request capture reports.

Left to the SDK or bounded elsewhere: botocore reads every non-streaming
reply body whole (as in any boto3 application; the library has no HTTP layer
of its own), and so does the CRT client for the delete requests the library
sends through it (`crtrequest`): it hands an answer's headers and body over
only once it holds all of the body (measured with awscrt 0.32.2), so no cap in
the library could stop that growth. A ranged download's request count follows
the size a listing or HEAD claims, while its memory stays bounded by
s3transfer's queues; and a download body that never ends runs until the disk
is full, which fails the item.

The rules on secrets and protections of overview.md section 3 hold for such
a response as for any other.

## 4. The batch aggregation exception `BatchError`

`cp -r` / `mv -r` / `rm -r` / `sync` handle many items. As in aws-cli, they
**attempt every item** and, if even one is `FAILED`, raise **`BatchError` once**
at the end. They **keep no breakdown list, only aggregate counts** (memory is
O(1) even with a million failures). Per-item detail is streamed in real time
through the `on_result` hook.

```python
class BatchError(Boto3S3Error):
    # ctor: BatchError(message, *, succeeded, failed, warned, skipped, operation=None)
    succeeded: int
    failed: int       # -> exit code 1
    warned: int       # -> exit code 2
    skipped: int      # informational (does not affect rc; op-layer skip is the main case. Skips at the enumeration/filter level are generally not included)
    total: int        # read-only @property: succeeded + failed + warned + skipped (a rollup sum, not an item count - `warned` counts warnings, walk warnings included; not a ctor argument)
    # __cause__ = the first failure (a diagnostic sample, not a list)
```

- The raise condition is **only when `failed > 0`**. It does not raise
  for `warned`/`skipped` alone (`failed == 0`). In that case, obtain the counts
  through the `on_result` hook (for the exit code, see section 5).
- `CANCELLED` records never reach a `BatchError`: a cancelling run ends by
  raising the fatal (or `CancelledError`) instead, and cancelled items are not
  failures - the engine's separate `cancelled` rollup counter carries them
  ([`opresult.md`](./opresult.md), the `on_result` contract), on either
  transfer engine: a drain-time Ctrl-C the CRT manager swallows is re-raised
  ([`crt.md`](./crt.md) section 6).
- `BatchError` is also a subtype of `Boto3S3Error`, so it is caught by
  `except Boto3S3Error`.
- `cp` / `mv` / `sync` / `rm` follow the batch model regardless of the item
  count: even a single-item failure is `OpResult` FAILED +
  `BatchError(1 of 1)`. Because in aws-cli a one-off transfer or rm is also a
  "task" and yields `... failed: ...` + rc 1, this shape keeps the CLI mapping
  uniform with the recursive case. Only `mb` / `rb` / `website` / `presign`
  are single-item operations that do not aggregate; they raise the
  corresponding category exception on the spot, and whatever else their one
  request raised from inside botocore (a refreshing `credential_process`
  whose program is gone) as the base `Boto3S3Error` carrying the original on
  `__cause__` - the same capture as a per-item failure
  (`sdkerrors.request_failure`), with `AssertionError` alone passing through.
  Note that an error before item
  processing begins - such as a failure of the enumeration (scan) itself, or
  `cp`'s missing-source check - **propagates as the category exception**, the
  listing's and the single-source HEAD's requests capturing what botocore
  raised outside its own family the same way (the
  CLI's transfer-family commands turn an enumeration failure into rc 1, while
  `cp`'s missing local source is rc 255 as on aws; cli.md section 6).
- `OpOutcome.DRYRUN` is the report for an item a dry run *would* have acted on
  (transferred or deleted); the item's mutating API call does not occur
  (enumeration and HeadObject still run) and it does not affect rc (an
  informational value, like `SKIPPED`).

A note on `skipped`: it is an **informational value** whose collectability
varies with "at which level the skip happened." Op-layer skips can be counted -
`cp` / `mv` declining to overwrite under `no_overwrite` (a download's
destination-existence check, an upload/copy's `IfNoneMatch` rejection) and a
glacier-blocked source passed over under `ignore_glacier_warnings` - but skips
at the enumeration/filter level (a symlink under `--no-follow-symlinks`, an
`--exclude` exclusion) never reach the op layer and are generally not counted.
`sync` counts only one kind of skip: a glacier-blocked source passed over under
`ignore_glacier_warnings`. A pair it finds up to date produces no record and no
counter at all, and its `no_overwrite` is applied by dropping the update lane
outright rather than by skipping pairs one at a time.

## 5. exit code mapping (CLI)

The CLI turns this hierarchy into aws-compatible exit codes. What the codes mean
and which one wins is specified in
[`exit-codes.md`](../docs/cli/exit-codes.md); how the mapping is built - and
which command families bypass `exit_code_for` with a local catch - is in
[`cli.md`](./cli.md) section 6.

Two consequences belong to this document, because they are properties of the
taxonomy rather than of the mapping:

- **The class does not decide alone.** `exit_code_for` inspects `__cause__`
  first, so anything that reached the server (a botocore `ClientError`) is 254
  whatever this taxonomy calls it - a server-side rejection filed here under
  `ValidationError` included. The category is consulted only when there is no
  `ClientError` cause, and then the refining subclasses are matched before their
  parents, which is why `InvalidValueError` and `InvalidConfigError` do not
  inherit their parents' codes.
- **`CancelledError` has no exit code of its own.** rc 130 is not a mapping from
  it: it comes from `main()`'s `KeyboardInterrupt` backstop, and only outside a
  running transfer - inside one, cancellation is reported as a failed run
  instead.
