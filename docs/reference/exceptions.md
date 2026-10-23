# Exceptions

Every failure the public API reports is a `Boto3S3Error` or one of its
subclasses; success is the absence of an exception. This page states each
class's contract: what it means, when the library raises it, which attributes
it carries, and what a caller may rely on in an `except` clause. The narrative
— worked handlers, the partial-failure model, what is safe to depend on across
releases — is in [`../library/errors.md`](../library/errors.md), and the
rationale behind the taxonomy is in
[`../../design/exceptions.md`](../../design/exceptions.md). Readers arriving
from the command will find its exit codes specified separately in
[`../cli/exit-codes.md`](../cli/exit-codes.md); they are a CLI concept and play
no part in the library contract below.

All ten classes are exported from the `boto3_s3` root and from
`boto3_s3.exceptions`.

```
Boto3S3Error
├── AccessDeniedError
├── NotFoundError
├── ValidationError
│   └── InvalidValueError
├── TransportError
├── ConfigurationError
│   └── InvalidConfigError
├── CancelledError
└── BatchError
```

## Boto3S3Error

The root of the hierarchy and the catch-all: `except Boto3S3Error` catches
every failure the public API reports as its own (the exceptions that stay
outside it are listed at the end of this section). It derives from `Exception`, not
`BaseException`, so a bare `except Exception` catches it too.

```python
class Boto3S3Error(Exception):
    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        bucket: str | None = None,
        key: str | None = None,
    ) -> None: ...
```

`operation` is the operation name the failure belongs to (`"cp"`, `"sync"`,
…). `None` is a legitimate value rather than a gap: it means no single
operation was in scope — while a client is being built, or when the caller
invokes a storage-level method directly — `validate`, `open`, `delete`,
`get_fileinfo`, a `scan` or `list_buckets` listing
([`storage.md`](./storage.md)), whose errors leave it unset because the
storage cannot know which operation is using it. The same call reached through an operation carries that operation's name
instead, whichever path the operation took: a `DeleteObject` the service denies
reads `"rm"` on the blind single-key path and on the batched path alike, and a
listing the service denies reads `"ls"`, `"rm"`, `"cp"`, `"mv"` or `"sync"`
after the operation that was reading it. `S3Storage.get_file` and `put_file`
are the exception among the storage-level methods: no operation ever runs
them, so they name their own failures `"get_file"` / `"put_file"`.

`bucket` and `key` are best-effort context for the failing entry. `key` names
that entry in the address space it came from, not necessarily an S3 key: a
locally-originating error puts a filesystem path there and leaves `bucket`
unset.

The message is available as `str(exc)` and is display only. It may change in
any release, so branch on the class and read the attributes; do not parse
it. What is stable is class
identity and the structured attributes — those two, and `BatchError`'s
counters.

No raise site constructs this class for a failure it can classify; a
classified failure is always raised as one of the subclasses below. Direct base
instances exist only where no classification applies:

- the S3 error translator's last-resort clause, for an exception no earlier
  clause claims — an error the CRT engine reports under a name it has no
  category for (a cancellation, an unknown error), or an `OSError` a request
  raises from a credential provider (a `credential_process` executable or a
  web identity token file that is missing), say;
- whatever else an S3 request raised from inside botocore that is none of
  botocore's own exceptions. The request failed all the same, so every
  request the library issues reports it as the base error with that
  exception on `__cause__` and its text — what the AWS CLI prints — as the
  message;
- an exception a stream raises (an `IOStorage` or `StdioStorage` side) that is
  not already a `Boto3S3Error`, an `OSError` included — a download to a full
  disk, say; where the SDK reads the stream itself, the SDK's category applies
  instead ([`storage.md`](./storage.md#iostorage));
- the status-widening fallback for a `ClientError` whose error code is not in
  the category table and whose HTTP status is not one the widening rules cover
  (403, 404, 5xx, other 4xx);
- a per-key `DeleteObjects` failure whose code is not in the category table
  (such an entry carries no HTTP status to widen on);
- the guards that keep a server answering outside S3's API from making a
  run loop or grow without bound: a copy's source annotations past S3's
  limits (more than 1,000, or a payload over 1 MiB), a multi-region access
  point listing that repeats its `NextToken`
  ([`../../design/exceptions.md`](../../design/exceptions.md) section 3.1);
- the message envelope on `WARNED` and `NOTICE` `OpResult` records, where the
  instance is a carrier for display text and is never raised (see
  [`results.md`](./results.md)).

An S3 `ClientError` maps to a class by error code first; a code not in the
table falls back to its HTTP status — 403 to `AccessDeniedError`, 404 to
`NotFoundError`, 5xx to `TransportError`, other 4xx to `ValidationError`.

The original backend exception is preserved, never swallowed: an error raised
for a failed S3 request carries the originating botocore `ClientError` on
`__cause__`. Per-item failure records link the same way — the translation
stamps the original exception onto the record error's `__cause__`, and an error
that was already a `Boto3S3Error` passes through keeping whatever cause it had.
The exception-free paths are the deleter's per-key failures synthesized from a
`DeleteObjects` response body's error entries, and the guards' failures above,
with no exception object behind them, so their `__cause__` is `None` and only
the message says what happened.

Some exceptions stay outside the hierarchy by design. Programming bugs
(`TypeError`, `AssertionError`) propagate unwrapped on the synchronous paths —
the library's own code, that is: what botocore raises from inside a request is
the request failing, reported as above, with `AssertionError` alone left
untranslated there too (bar the CRT signer's refusal of a non-positive
`expires_in` in `presign`, an `InvalidValueError`). `KeyboardInterrupt` and
`SystemExit` always propagate. What a response outside S3's API leads to —
a missing element, a value the API does not define, a value that cannot be
read — is not specified: it may be any failure, the bare exception the
library's reading of the response raised included, or none
([`../../design/exceptions.md`](../../design/exceptions.md) section 3.1).
Selecting the CRT engine
explicitly with `preferred_transfer_client="crt"` while awscrt is absent (or
older than boto3's minimum) is a [`ConfigurationError`](#configurationerror)
when the library resolves the config (`S3.resolve_transfer_config`, which
every `cp` / `mv` / `sync` / `rm` calls); a plain boto3 `TransferConfig`, used
as it is, propagates botocore's `MissingDependencyException` instead, matching
what boto3 does — that pass-through is scoped to engine selection, and the same
exception surfacing inside a translated S3 call becomes a `ConfigurationError`.

An exception raised by a custom `Storage` backend that is not already a
`Boto3S3Error` is wrapped into this base class when it surfaces as a per-item
failure, and so is one raised while a `sync` content comparison
(`EtagComparison` / `ChecksumComparison`) reads that backend, which aborts the
`sync`; one raised during enumeration (`scan`) or elsewhere on the fatal path
propagates as-is (see [`storage.md`](./storage.md)). Attribution on a per-item
failure is filled in the same best-effort spirit: a family error raised
in-pipeline, from a backend's `open` or its reads and writes, arrives with
whatever its raiser attributed — often nothing — and the run fills in only what
is unset, never overwriting a value the raiser set itself. `bucket` and `key`
fill as a pair, so an error that named the failing entry in its own address
space keeps that naming whole.

## AccessDeniedError

The caller lacks permission for the resource. Raised for an S3 403 or the
`AccessDenied` error code, and for a local `PermissionError` on boto3-s3's
own paths — a storage's `open`, `delete` or `get_fileinfo`, a directory the
operation creates — and inside either transfer engine (a download into a
directory the process cannot write: the classic engine's `PermissionError`,
the CRT engine's `AWS_ERROR_NO_PERMISSION`), so one `except AccessDeniedError`
covers both sides of a transfer. The message is the one the AWS CLI prints.

```python
class AccessDeniedError(Boto3S3Error): ...
```

Adds no attributes and no constructor of its own. It has no subclasses in the
hierarchy.

## NotFoundError

The target resource does not exist. Raised for an S3 404 or the `NoSuchKey` /
`NoSuchBucket` / `NoSuchVersion` / `NotFound` error codes, for a local
`FileNotFoundError` (the CRT engine's `AWS_ERROR_FILE_INVALID_PATH`), and for a source that does not exist when an operation
checks it up front — a `cp` / `mv` / `sync` whose local source path is missing,
and a single-object `cp` / `mv` whose S3 source `HeadObject` answers 404, fail
this way before item processing begins, so the error propagates directly rather
than as a `BatchError`.

```python
class NotFoundError(Boto3S3Error): ...
```

Adds no attributes and no constructor of its own. It has no subclasses in the
hierarchy.

## ValidationError

A caller-supplied value, a precondition, or the coherence of the requested
state is invalid. Representative causes: an unrecognized key in a transfer
operation's `**options`; a malformed `s3://` URI, or a bucket name that is
empty where the operation requires one; a `mv` whose source and destination
name the same object; a stream used where the operation does not accept one
(both sides at once, a `mv` source, a recursive stream destination); an
unrecognized value for a mode option such as `copy_props`,
`annotation_copy_mode` or `case_conflict`, or for `presign`'s `method`; a
case-fold collision under `CaseConflictMode.ERROR`; and `stdin` unavailable
where a stream needs it (there is no `stdout` counterpart — matching the AWS
CLI, a missing one fails that item from inside the transfer instead); a local
path the operating system refuses before asking the filesystem (an embedded
NUL, a lone surrogate the filesystem encoding cannot carry), with the
`ValueError` on `__cause__`, where `LocalStorage` itself is asked about it
(`get_fileinfo`, `open`, a recursive download's destination directory) - as a
single transfer's local file it fails the item with the base `Boto3S3Error`,
and as an upload source it is a missing source (`NotFoundError`). It is also
the class for botocore's
`ParamValidationError` — client-side validation, so no request was sent — and
for an S3 4xx other than 403 and 404 whose code is not in the category table.

```python
class ValidationError(Boto3S3Error): ...
```

Adds no attributes and no constructor of its own. `except ValidationError` also
catches its `InvalidValueError` refinement.

## InvalidValueError

A caller-supplied value fails post-parse conversion or validation, as opposed
to being rejected while arguments are still being parsed. The distinction
exists so the `boto3-s3` command can reproduce the AWS CLI's own split between
those two failure modes; a library consumer can ignore it and catch
`ValidationError`.

```python
class InvalidValueError(ValidationError): ...
```

Adds no attributes and no constructor of its own. Inside `boto3_s3` these
paths raise it: [`presign`](./operations/presign.md) with a non-positive
`expires_in` the CRT signer refuses, an `IOStorage` given an unknown
`encoding`, and an [`S3`](./s3.md) or a `LocalStorage` given an unknown
`filename_encoding` ([`storage.md`](./storage.md)). Otherwise it is raised by the
`boto3-s3-cli` distribution (and is available to callers who want the same
refinement); a handler around library calls need only catch
`ValidationError`.

## TransportError

A network or local I/O failure. Raised for an S3 5xx or a throttling response
(`InternalError`, `SlowDown`, `ServiceUnavailable`, `RequestTimeout`), for
botocore's connection and HTTP-client failures (endpoint or connect failure,
proxy failure, read timeout, a dropped connection, a response body that ends
short of its `Content-Length`), and for a local `OSError` (not one a stream
raises, which is the base class; see above) that is neither
`FileNotFoundError` nor `PermissionError`, including a failed
directory creation and one raised inside the classic transfer engine (the
AWS CLI's message text kept as it is), and an `OSError` while a client is
built that is not a configured path failing (out of file descriptors, say). On the CRT engine the same events
arrive under awscrt's error names and take the same class: a socket, DNS or
TLS failure, a closed connection, S3's `SlowDown` and internal errors the
engine gave up retrying, a local file it cannot open, read or write.

```python
class TransportError(Boto3S3Error): ...
```

Adds no attributes and no constructor of its own. It has no subclasses in the
hierarchy.

## ConfigurationError

Required configuration is missing or unresolvable, or the environment lacks a
capability the request needs. Raised for botocore's `NoCredentialsError` and
`NoRegionError`; for an SDK floor shortfall, such as `no_overwrite` on a
botocore without conditional writes or `copy_props=ALL` on an SDK without the
annotations model; and for awscrt being absent on a path that translates that
absence, such as the SigV4a signing a multi-region access point target
requires. Configuration that is present but invalid is the `InvalidConfigError`
refinement instead.

```python
class ConfigurationError(Boto3S3Error): ...
```

Adds no attributes and no constructor of its own. `except ConfigurationError`
also catches `InvalidConfigError`.

Note the engine-selection carve-out described under `Boto3S3Error`: requesting
the CRT engine explicitly in a plain boto3 `TransferConfig` without a usable
awscrt installed propagates botocore's `MissingDependencyException` rather than
this class.

## InvalidConfigError

A configuration value is present but invalid or unusable. Raised for a config
or `[s3]` value that will not convert to the type the key requires (an integer,
a size, a rate), for a config key that names a section where a value is
expected, for botocore's `ProfileNotFound`, `PartialCredentialsError` and
`InvalidConfigError` (a `source_profile` loop, say), and for any other botocore
failure to read the config file while the library reads AWS configuration (a
`ConfigParseError`, say). It is also the class for a
malformed `endpoint_url` — one passed to `S3`, or one taken from
`AWS_ENDPOINT_URL` / `AWS_ENDPOINT_URL_S3` when a default client is built —
and for a malformed or empty region, both of which botocore rejects with a
`ValueError` that the client builders convert here, for a config value of
the wrong shape (`s3 = foo`, a value where botocore reads a section), on which
botocore's client build fails with an `AttributeError`, and for a
`credential_process` executable that cannot run while the client is built. A
CA bundle setting (`AWS_CA_BUNDLE`, a profile's `ca_bundle`,
`REQUESTS_CA_BUNDLE`, or a client's `verify`) that resolves to an empty or
whitespace-only string is refused with it too, rather than read as "TLS
verification off": by `S3.client()`, by `S3Storage.get_client()` for a client
it builds or is handed, by `S3PathResolver` for the clients it is given, and
by the CRT transfer engine, whatever the installed botocore does with the
value. A client the library builds resolves the setting the way the clients
botocore builds behind it for its credentials (an assumed role, SSO) do, so
its own value answers for them. A client you hand over (to `S3Storage`,
`S3PathResolver` or `materialize_crt_engine`) may carry an explicit `verify`
while those credential clients still resolve `AWS_CA_BUNDLE` first, so an
empty or whitespace-only `AWS_CA_BUNDLE` is refused beside it as well - more
strictly than the AWS CLI, which refuses it only when it builds a client that
reads the value (a credential client, the CRT engine's request serializer),
so a static credential on the classic engine passes there. Two cases stay outside, under a
botocore older than 1.43.54: an empty `REQUESTS_CA_BUNDLE` or an empty
profile `ca_bundle` still reaches those credential clients beside a
handed-over client with an explicit `verify`, since the client does not name
the session that decides whether they are read; build the client with
`S3.client()`, or use botocore 1.43.54 or later, to have them refused. The CRT transfer engine
raises it when it is built — at the first transfer, or from
[`materialize_crt_engine`](./s3.md#materialize_crt_engineclient--transfer_confignone)
— around a CA bundle it cannot read (a missing path, a directory, a file it
may not open) or cannot parse.

```python
class InvalidConfigError(ConfigurationError): ...
```

Adds no attributes and no constructor of its own. Catching the parent
`ConfigurationError` covers it; the separate class exists so the command can
keep the AWS CLI's distinction between unresolvable and unusable configuration.

## CancelledError

The operation was cancelled by the caller through a `CancelToken`. This is the
library's own class and is unrelated to the identically named exceptions in
`asyncio` and `concurrent.futures`; being a `Boto3S3Error`, it derives from
`Exception`, not `BaseException`.

```python
class CancelledError(Boto3S3Error): ...
```

Adds no attributes and no constructor of its own. It has no subclasses in the
hierarchy.

A run that a cancellation actually cut short ends by raising — either the error
that triggered the shutdown, or this class — and never with `BatchError`;
cancelled items are not counted as failures. The class also appears as
`OpResult.error` on `CANCELLED` records without being raised, for accepted
items revoked when the engine shut down: a fatal error elsewhere in the run,
an immediate cancellation, or Ctrl-C, on either transfer engine. The
cancellation modes and the resulting record shapes are in
[`results.md`](./results.md) and
[`../library/results.md`](../library/results.md).

## BatchError

The partial-failure carrier: raised once, at the end of a batch operation, when
at least one item failed. `cp` and `mv` with `recursive=True`, `rm`, and `sync`
attempt every item and report the outcome through this class rather than
aborting at the first failure.

```python
class BatchError(Boto3S3Error):
    def __init__(
        self,
        message: str,
        *,
        succeeded: int,
        failed: int,
        warned: int,
        skipped: int,
        operation: str | None = None,
    ) -> None: ...

    @property
    def total(self) -> int: ...
```

The raise condition is `failed > 0`. Warnings and skips alone do not raise; a
run that only warned or only skipped completes normally, and those counts are
available from the `on_result` stream. The batch shape holds regardless of item
count: a single-item `cp` / `mv` / `rm` whose one item failed also ends in
`BatchError`, reported as 1 of 1. It does not apply to `mb` / `rb` / `website`
/ `presign`, which act on one thing and raise the category exception directly,
nor to a failure that happens before item processing starts — a listing that
fails outright, or a `cp` whose source does not exist, propagates as
`NotFoundError` and the like.

`succeeded`, `failed`, `warned` and `skipped` are the run's rollup counts, and
they are the only per-run detail the exception carries: it holds no list of
failures, so memory is constant no matter how many items failed. Per-item
detail is delivered live through the `on_result` hook instead, and is the place
to collect it from ([`results.md`](./results.md)). On `sync`, `succeeded` and
`failed` are the sums of the transfer and the delete side, while `warned` and
`skipped` come from the transfer side only; on `rm`, both `warned` and
`skipped` are reported as `0`.

`warned` counts warnings rather than items, so an item that succeeded with a
warning — a download whose timestamp stamp failed, for instance — contributes
to two counters. `skipped` is informational: it counts the silent skips the
operation layer made — a `cp` / `mv` item `no_overwrite` declined to replace
(an existing local destination on a download, an `IfNoneMatch` rejection on an
upload or a copy), and a glacier source skipped under
`ignore_glacier_warnings`. That glacier gate is the only skip a `sync` run
reports: a pair the comparison finds already up to date produces no record and
no count at all, and so does one ruled out by `sync`'s `no_overwrite`, which
drops the whole update side instead of skipping pairs one by one. Entries a
filter dropped during enumeration are uncounted for the same reason — they
never reach the operation layer.

`total` is a read-only property returning `succeeded + failed + warned +
skipped`. Because of the `warned` semantics above it is a rollup sum, not an
exact item count.

`bucket` and `key` are not constructor parameters and are `None` on every
instance: the exception stands for a whole run rather than one entry.
`operation` carries the operation name as it does on any `Boto3S3Error`.

`__cause__` is a diagnostic sample, not a list: the first failure recorded by
the rollup, as a translated `Boto3S3Error` rather than a raw backend exception.
For `sync` it is sampled from the transfer rollup first and from the delete
rollup only when no transfer failed, regardless of which of the two happened
first chronologically. This makes the chain two levels deep, and the second
level is a guarantee: where a failure has an exception behind it — a botocore
`ClientError` for a request that reached the server — that original sits at
`__cause__.__cause__`. The documented exception is a per-key `DeleteObjects`
failure, which is synthesized from the response body with no exception object
behind it: its `__cause__` is `None`, and only its message carries the S3 error
code.

`BatchError` is a direct subclass of `Boto3S3Error` and of no category class,
so `except Boto3S3Error` covers it while a handler for a single category
(`except TransportError`, say) does not.
