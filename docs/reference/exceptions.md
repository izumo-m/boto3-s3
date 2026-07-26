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
every failure the public API reports. It derives from `Exception`, not
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
operation was in scope — while a client is being built, on the shared
object-listing path that backs every recursive scan, which `ls` / `rm` / `cp` /
`mv` / `sync` all ride, or when the caller invokes a storage-level method
directly, such as `Storage.validate` ([`storage.md`](./storage.md)), or `open`
on a storage that does not name its own errors — the stream wrappers, where
`LocalStorage` and `S3Storage` do name theirs. The same call reached through an
operation carries that operation's name instead.

`bucket` and `key` are best-effort context for the failing entry. `key` names
that entry in the address space it came from, not necessarily an S3 key: a
locally-originating error puts a filesystem path there and leaves `bucket`
unset.

The message is available as `str(exc)` and is display only. It tracks
`aws s3` wording and changes whenever parity requires, so branch on the class
and read the attributes; do not parse it. What is stable is class identity and
the structured attributes — those two, and `BatchError`'s counters.

No raise site constructs this class for a failure it can classify; a
classified failure is always raised as one of the subclasses below. Direct base
instances exist only where no classification applies:

- the S3 error translator's last-resort clause, for an exception no earlier
  clause claims — notably an `OSError` raised inside s3transfer's task
  execution, deliberately kept at the base rather than `TransportError` so the
  AWS CLI's message survives verbatim;
- the status-widening fallback for a `ClientError` whose error code is not in
  the category table and whose HTTP status is not one the widening rules cover
  (403, 404, 5xx, other 4xx);
- a per-key `DeleteObjects` failure whose code is not in the category table
  (such an entry carries no HTTP status to widen on);
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
The one exception-free path is a per-key `DeleteObjects` failure: it is
synthesized from the response body with no exception object behind it, so its
`__cause__` is `None` and only the message carries the S3 error code.

Some exceptions stay outside the hierarchy by design. Programming bugs
(`TypeError`, `AssertionError`) propagate unwrapped on the synchronous paths;
`KeyboardInterrupt` and `SystemExit` always propagate. Selecting the CRT engine
explicitly with `TransferConfig.preferred_transfer_client="crt"` while awscrt
is absent propagates botocore's `MissingDependencyException`, matching what
boto3 does — that pass-through is scoped to engine selection, and the same
exception surfacing inside a translated S3 call becomes a `ConfigurationError`.

An exception raised by a custom `Storage` backend that is not already a
`Boto3S3Error` is wrapped into this base class when it surfaces as a per-item
failure, but one raised during enumeration (`scan`) or on the fatal path
propagates as-is (see [`storage.md`](./storage.md)). Attribution on a per-item
failure is filled in the same best-effort spirit: a family error raised
in-pipeline, from a backend's `open` or its reads and writes, arrives with
whatever its raiser attributed — often nothing — and the run fills in only what
is unset, never overwriting a value the raiser set itself. `bucket` and `key`
fill as a pair, so an error that named the failing entry in its own address
space keeps that naming whole.

## AccessDeniedError

The caller lacks permission for the resource. Raised for an S3 403 or the
`AccessDenied` error code, and for a local `PermissionError` — one
`except AccessDeniedError` therefore covers both sides of a transfer.

```python
class AccessDeniedError(Boto3S3Error): ...
```

Adds no attributes and no constructor of its own. It has no subclasses in the
hierarchy.

## NotFoundError

The target resource does not exist. Raised for an S3 404 or the `NoSuchKey` /
`NoSuchBucket` / `NoSuchVersion` / `NotFound` error codes, for a local
`FileNotFoundError`, and for a source that does not exist when an operation
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
`annotation_copy_mode` or `case_conflict`; a case-fold collision under
`CaseConflictMode.ERROR`; and `stdin` or `stdout` unavailable where a stream
needs it. It is also the class for botocore's `ParamValidationError` —
client-side validation, so no request was sent — and for an S3 4xx other than
403 and 404 whose code is not in the category table.

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

Adds no attributes and no constructor of its own. No code path inside
`boto3_s3` raises it — it is raised by the `boto3-s3-cli` distribution (and is
available to callers who want the same refinement) — so a handler around
library calls need only catch `ValidationError`.

## TransportError

A network or local I/O failure. Raised for an S3 5xx or a throttling response
(`InternalError`, `SlowDown`, `ServiceUnavailable`, `RequestTimeout`), for
botocore's connection and HTTP-client failures (endpoint or connect failure,
proxy failure, read timeout, a dropped connection), and for a local `OSError`
that is neither `FileNotFoundError` nor `PermissionError` on boto3-s3's own
paths, including a failed directory creation. One carve-out: an `OSError`
surfacing from inside s3transfer's task execution stays at the base
`Boto3S3Error` rather than becoming a `TransportError`, so the AWS CLI's
message text rides through unchanged.

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
the CRT engine explicitly without awscrt installed propagates botocore's
`MissingDependencyException` rather than this class.

## InvalidConfigError

A configuration value is present but invalid or unusable. Raised for a config
or `[s3]` value that will not convert to the type the key requires (an integer,
a size, a rate), for a config key that names a section where a value is
expected, for botocore's `ProfileNotFound` and `PartialCredentialsError`, and
for any other botocore failure to read the config file while the library reads
AWS configuration (a `ConfigParseError`, say). It is also the class for a
malformed `endpoint_url` — one passed to `S3`, or one taken from
`AWS_ENDPOINT_URL` / `AWS_ENDPOINT_URL_S3` when a default client is built —
which botocore rejects with a plain `ValueError` that the client builders
convert here.

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
items revoked when the engine shut down: a fatal error elsewhere in the run, an
immediate cancellation, or Ctrl-C. The cancellation modes and the resulting
record shapes are in [`results.md`](./results.md) and
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
