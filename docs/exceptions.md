# boto3-s3 Exception Model (current state / source of truth)

This document is the **established current state (source of truth)** for the
exception design of `boto3-s3`. When a design decision changes, update this
document first.

Related: the entry point to the design as a whole is
[`overview.md`](./overview.md). The documentation for the public API is being
redesigned.

---

## 1. Policy

- **Every error from the public API is reported as a `Boto3S3Error`-family
  exception** (success = no exception, error = always an exception; no return
  codes or error-report objects).
- The hierarchy is **`Boto3S3Error` (root) + 6 categories + 2 refining
  subclasses**. Collapsing everything into a single `botocore`-style
  `ClientError` is **rejected** (because boto3-s3 spans both the local FS and
  S3, it preserves a cross-cutting classification in which "an S3 403 and a
  local `PermissionError` belong to the same category").
- **The root is never raised directly for a known failure** - every raise site
  uses a category (or refining) class, so `except Boto3S3Error` is purely the
  catch-all. Direct base instances appear only where no classification exists:
  the error translators' last-resort fallback (section 3) and the message
  envelope on WARNED / NOTICE `OpResult` records.
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
`-- CancelledError          # caller-initiated abort (CancelToken.cancel()).
                            #   Unrelated to the CancelledError of asyncio / concurrent.futures
```

The two **refining subclasses** exist for the CLI's exit-code parity: aws
reports those failures through its *general* exception handler (rc 255), while
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

- Programming bugs (`TypeError` / `AssertionError`, etc.) are not wrapped and
  pass through. `KeyboardInterrupt` / `SystemExit` also pass through.
- **Intentional pass-through exception**: using
  `TransferConfig.preferred_transfer_client="crt"` while awscrt is absent passes
  through the same `botocore.exceptions.MissingDependencyException` as boto3 does.
  This is a deliberate exception to the boundary conversion of section 1, kept to
  stay boto3-faithful ([`crt.md`](./crt.md) section 3 / section 6). The CLI
  distribution maps this situation to a `ConfigurationError` (rc 253) ahead of
  time to prevent a traceback (the decision tree of crt.md section 4), so this
  library exception never passes through the CLI.

## 3. backend / local -> category mapping (representative examples)

| Origin | Category |
|---|---|
| S3 403 / `AccessDenied` | `AccessDeniedError` |
| S3 404 / `NoSuchKey` / `NoSuchBucket` / `NoSuchVersion` / `NotFound` | `NotFoundError` |
| S3 `InternalError` / `SlowDown` / `ServiceUnavailable` / `RequestTimeout` (5xx / throttle) | `TransportError` |
| local `PermissionError` (incl. the post-download utime EPERM) | `AccessDeniedError` |
| local `FileNotFoundError` / a missing source path | `NotFoundError` |
| connection failure / timeout / other `OSError` (I/O, incl. a failed `makedirs`) | `TransportError` |
| `NoCredentialsError` / `NoRegionError` | `ConfigurationError` |
| `ProfileNotFound` / `PartialCredentialsError` / other config-flavored `BotoCoreError` at client construction | `InvalidConfigError` |
| an `[s3]` / config-file value that does not convert (`runtimeconfig` / `awsconfig`) | `InvalidConfigError` |
| a post-parse option-value conversion failure (`--page-size abc`, the CLI timeouts) | `InvalidValueError` |
| `ParamValidationError` / invalid argument / violated precondition (stdin absent, case-conflict `error` mode) | `ValidationError` |
| an SDK floor missing a capability (`no_overwrite` on an old botocore) | `ConfigurationError` |
| `CancelToken.cancel()` | `CancelledError` |

Local `OSError`s are converted by one shared translator
(`localstorage.translate_os_error`, the local mirror of
`s3storage.translate_boto_error`): `FileNotFoundError` -> `NotFoundError`,
`PermissionError` -> `AccessDeniedError`, everything else -> `TransportError`.

An S3 `ClientError` code is matched first against `S3_CODE_CATEGORIES`
(`s3storage.py`); a code not in the table falls back to HTTP-status widening:
403 -> `AccessDeniedError`, 404 -> `NotFoundError`, 5xx -> `TransportError`,
other 4xx -> `ValidationError`, otherwise the base `Boto3S3Error`. That last
fallback - and `translate_boto_error`'s final clause for an exception nothing
classifies - are the only places a direct base instance is created (section 1).

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
    total: int        # read-only @property: succeeded + failed + warned + skipped (items that reached the op layer; not a ctor argument)
    # __cause__ = the first failure (a diagnostic sample, not a list)
```

- The raise condition is **only when `failed > 0`** (Model 1). It does not raise
  for `warned`/`skipped` alone (`failed == 0`). In that case, obtain the counts
  through the `on_result` hook (for the exit code, see section 5).
- `BatchError` is also a subtype of `Boto3S3Error`, so it is caught by
  `except Boto3S3Error`.
- Single-item operations (`mb`/`rb`/`website`/`presign`/single `cp`) do not
  aggregate; they raise the corresponding category exception on the spot.
  **Exception: `rm` follows the batch model even for a single key**
  (`OpResult` FAILED + `BatchError(1 of 1)`). Because in aws-cli a one-off rm is
  also a "task" and yields `delete failed: ...` + rc 1, this shape keeps the CLI
  mapping uniform with the recursive case. Note that an error before item
  processing begins - such as a failure of the enumeration (scan) itself -
  **propagates as the category exception** (CLI rm maps both to rc 1, cli.md section 6).
- `OpOutcome.DRYRUN` is the report for an item that dryrun "would have deleted";
  no API call occurs and it does not affect rc (an informational value, like
  `SKIPPED`).

A note on `skipped`: it is an **informational value** whose collectability
varies with "at which level the skip happened." Op-layer skips (sync unchanged,
`--no-overwrite`) can be counted, but skips at the enumeration/filter level
(a symlink under `--no-follow-symlinks`, an `--exclude` exclusion) never reach
the op layer and are generally not counted.

## 5. exit code mapping (CLI)

This is a summary; the authoritative, per-subcommand exit-code mapping lives in
[`cli.md`](./cli.md) section 5 (each subcommand) and section 6 (the rc table and
the `exit_code_for` rules). The values match aws-cli v2
(aws-cli's `awscli/constants.py`).

| Situation | rc |
|---|---|
| Success (also `--help` / `--version` / `BrokenPipeError`) | 0 |
| Transfer family (`cp` / `mv` / `sync`) completed with warnings only, no failures | 2 |
| Usage error (unknown option / invalid choice; also the `--cli-auto-prompt` rejection) / client-side `ValidationError` | 252 |
| `ConfigurationError` (credentials / region unresolved; `[s3]` `preferred_transfer_client=crt` x absent awscrt) | 253 |
| Server error: a `Boto3S3Error` whose `__cause__` is a botocore `ClientError` | 254 |
| General error (incl. `TransportError`, `NotFoundError` without a `ClientError` cause, and the refining `InvalidValueError` / `InvalidConfigError`); the rm-stage failure of `rb --force` | 255 |

The mapping is `cli.exit_code_for`: it checks `__cause__` first, so any error
that reached the server (a botocore `ClientError`) is **254** regardless of the
library category - even one this taxonomy files under `ValidationError`. Only
when there is no `ClientError` cause does the library category decide - the
refining subclasses first (`InvalidValueError` / `InvalidConfigError` -> 255,
aws's general handler), then the parents (`ValidationError` -> 252,
`ConfigurationError` -> 253), everything else -> 255.

Two families override this with their own catch and so do **not** reach
`exit_code_for` (cli.md section 5 / section 6):

- **Transfer family (`cp` / `mv` / `sync`)**: every error after the operation
  starts is folded to **rc 1** (a `BatchError` after the per-item `... failed:`
  lines, or any other library error as one `fatal error:` line), even when
  server-derived. A clean run with only `warned > 0` is **rc 2**. Usage errors
  before the operation begins are still 252; `mv`'s `--validate-same-s3-paths`
  resolution failure reaches the server before the operation and stays **254**.
  `rm` also folds its errors (including a single-key `BatchError`) to **rc 1**.
- **`mb` / `rb`**: a local catch folds the API-call error to **rc 1** (not 254
  even when server-derived); client creation is outside the catch, so an
  unresolved credential / region is still 253. `rb --force`'s rm-stage failure is
  the documented **rc 255** exception.

`CancelledError` is **not** given a special exit code by the CLI (there is no rc
130 mapping for it). The CLI's only rc 130 is a `KeyboardInterrupt` / `EOFError`
raised inside the `--cli-auto-prompt` interactive session, which is outside the
exit-code charter.
