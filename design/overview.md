# boto3-s3 Project Overview (current state / source of truth)

This document is the **authoritative reference (source of truth)** for the
project's purpose, supported scope, and design policy, and it doubles as the
entry point (index) for `docs/`. When the policy changes, update this document
first.

For term definitions, see [`glossary.md`](./glossary.md).

---

## 1. Purpose

Make the `aws s3` command a Python library: every subcommand (`cp` / `ls` /
`mb` / `mv` / `presign` / `rb` / `rm` / `sync` / `website`) runs in-process,
keeps the command's path rules, filters and `sync` decisions, and transfers
through the engines `aws s3` itself uses (`s3transfer` and the CRT). Other
projects offer `aws s3 sync` from Python, but they run the `aws` command
underneath, implement only part of it, or lack `s3transfer` and CRT support.
Running the `aws` command is not always an option either: aws-cli is a large
package, which can make it unsuitable for a container such as a Lambda
function.

The project ships two packages.

- **`boto3-s3`** - the library, which is the product. It provides `aws s3`'s
  operations through its own Python API (one entry point whose methods mirror
  the `aws s3` subcommands - it is an independent implementation, not a boto3
  client wrapper, though it uses boto3 / botocore / s3transfer underneath). It
  **may behave more permissively than aws-cli** for convenience (e.g.,
  accepting an S3 path with the `s3://` prefix omitted).
- **`boto3-s3-cli`** - a by-product: the `boto3-s3` command, built on the
  library with `aws s3`'s command line. Anyone who wants a CLI is served by
  aws-cli itself. This layer owns what only a command line has: strict
  argument validation (e.g., rejecting an S3 path without `s3://` where
  `aws s3` rejects it), result lines and exit codes. Its e2e suite is also
  where the library is compared with `aws s3` command for command.

### Intended direction (important)

The goal is **`aws s3` as a library**: what an application would get by
running `aws s3`, it gets by calling `boto3-s3`. The library is held to
`aws s3`'s function (section 3) for every input `aws s3` accepts, and may
accept more (a superset). It does not reproduce the command line itself:
narrowing the inputs to exactly `aws s3`'s, and printing and exiting as
`aws s3` does, are the CLI's job.

The aim is to deliver performance equal to or better than `aws s3` while
maintaining high functional compatibility.

## 2. Supported scope

- **Python**: 3.10 and later.
- **OS**: Linux / macOS / **Windows**.
- **AWS SDK floor**: the oldest SDKs supported are roughly three years old. The
  numbers themselves live in each package's `pyproject.toml`, which is what
  enforces them and is where they are read from; this states only the policy
  behind them.
- **The installed SDK decides the feature set**: rather than emulate newer AWS
  behavior on an older SDK, features that depend on a newer S3 model are simply
  unavailable below the version that introduced them - on a par with the awscrt
  extra. Which feature needs which version, and how an unavailable one behaves,
  is recorded in [`compatibility.md`](../docs/compatibility.md). Everything else works
  at the floor.

## 3. Design policy

- **Functional compatibility**: in the normal path, each library operation
  does what its `aws s3` subcommand does under the corresponding arguments and
  the same conditions: the same objects and files are transferred, deleted or
  created, with the same content, properties and request headers; `sync`
  makes the same decisions; an option means the same thing. The CLI does the
  same, and a successful run prints what `aws s3` prints (its result lines,
  `ls`'s listing). In the semi-abnormal and abnormal paths, aws-cli's behavior
  is the reference rather than a target to match in full. What decides the
  functional outcome is matched: a run carries on past a per-item failure
  where `aws s3` carries on (a `sync` that meets an error keeps going), and
  stops before it starts where `aws s3` stops. Messages follow aws-cli too:
  a warning or error message aws-cli's own code writes is written the same
  way, unless aws-cli's text is inappropriate or makes no sense, where ours
  may say it differently. Other details of a particular error are not
  matched: which of two simultaneous mistakes is reported, the order of the
  reports, and the text the SDK, Python or S3 writes, which is reported as
  they write it. A functional difference is either a bug or a deliberate
  deviation written down in
  [`aws-differences.md`](../docs/cli/aws-differences.md); a difference in an
  error's details is neither, and is not written down. One source of
  deliberate functional deviation is settled: S3 objects are deleted in
  batches ([`deleter.md`](./deleter.md)) where aws-cli deletes one key per
  request; batching is not to be traded back for per-key deletes. The CRT
  transfer engine is held to aws's CRT mode on the same terms
  ([`crt.md`](./crt.md)). The CLI has no interactive auto-prompt.
- **Compatibility target**: the pinned aws-cli release the code is ported
  against, together with the boto3 / botocore / s3transfer / awscrt releases
  of the same period. Older aws-cli releases are not a target. Two version
  differences stand between this project and that release: aws-cli bundles
  its own Python and its own copies of botocore and s3transfer, which differ
  from the Python and the PyPI releases this project runs on, and the library
  also runs on SDKs as old as the floor and on every supported Python
  (section 2). A behavior that differs only because of such a
  version difference is not matched: it is neither a bug nor a deviation to
  write down, and nothing compensates for it. There is one exception: where
  aws-cli's bundled botocore, s3transfer or Python differs from the one this
  project runs on in a way that changes a normal run's outcome (the request
  sent, its endpoint, signature and headers, the objects selected, the
  profile used, the retries made, the arguments accepted), the library and
  the CLI match aws-cli, each where the difference reaches it; a difference
  that shows only in an error or in an unusual environment is not
  compensated. The defaults of a client and its session that the caller does
  not supply (the region, the profile, the default checksum) follow boto3 in
  the library and aws-cli in the CLI: a library caller sets them as for any
  boto3 application. The rules for errors below still hold on every
  supported version.
- **Security before compatibility**: where matching aws-cli would expose a
  secret or silently turn a protection off, the library and the CLI do not
  match it. For example, credentials are masked in debug logs
  ([`masking.md`](./masking.md)), where aws-cli prints them. Such a deviation
  is a functional difference and is written down in
  [`aws-differences.md`](../docs/cli/aws-differences.md).
- **Errors take the intended behavior, not aws-cli's**: how a failure is
  detected, reported and classified follows these rules rather than aws-cli's
  implementation.
  1. A failure is never reported as a success. An item that failed is a failed
     item, and the exit code says so, even where aws-cli loses the failure and
     reports success.
  2. A failure is reported so that its cause can be seen: not as a traceback,
     and with the original exception kept as its cause.
  3. A per-item failure leaves the other items running; a failure the run
     cannot proceed past (configuration, credentials, a missing destination)
     stops it before it starts.
  4. The library's exceptions classify the failures within its own reach: the
     SDK, S3 and local files. How the caller's code fails is the next item.
- **The caller's code is the caller's responsibility**: code the caller
  writes and hands to the library - a stream, a storage backend, a filter, a
  callback - is outside the library's reach. The library answers for its own
  code and for what it reaches through the SDK, S3, the local file system and
  Python's standard I/O streams (a file, a pipe, standard input and output, an
  in-memory buffer).
  1. Each place that takes the caller's code documents the contract it relies
     on (the standard stream contract, the storage protocol, thread safety
     where the code runs on a worker thread). The library relies on that
     contract and neither checks it nor works around a breach: what code that
     breaks it leads to is not specified, and is not a library bug.
  2. The caller's code reports its own failures with the library's
     exceptions, and one it raises is recorded as it is. Any other exception
     from a stream or a storage backend fails that item as the library's base
     exception, with the original kept as its cause. An interrupt (Ctrl-C, a
     request to exit) cancels the item and reaches the caller. An exception
     from a filter or a callback reaches the caller as it is: a filter decides
     on an item and a callback reports on one, so their failure is not the
     item's.
- **Responses outside S3's API are not specified**: the library relies on S3,
  or an S3-compatible server, answering as S3's API defines. What a response
  outside that definition leads to - a missing element, a value the API does
  not define, a value that cannot be read - is not specified: the run may
  fail, may go on and succeed, or may come out differently, and no code
  checks for such a response, works around it or classifies its failure, nor
  matches what aws-cli does with it. The error rules above apply to responses
  within the API. One exception is security: a server, faulty or hostile,
  must not be able to make a run loop or grow without bound, so that much is
  checked; the rules on secrets and protections above hold as well.
- **Text encodings**: which encoding turns bytes into text and back depends on
  the Python version and its environment, and only the application knows
  which one it was written for.
  1. The library follows Python's defaults wherever it converts - local file
     names, text streams - and lets the caller name the encoding instead, so
     an application can fix the one it relies on. An encoding the caller
     names for local file names is used as given: the OS gets the names in
     it. Whether the OS and its filesystem can take names in that encoding
     is the caller's concern; the library only refuses a codec Python does
     not know, and neither checks the encoding further nor works around what
     the OS makes of the names. The check that keeps a download inside its
     destination still runs on the names in the form the OS gets.
  2. The CLI uses the encoding aws-cli uses in the same environment wherever
     it converts: local file names, its command line, its output, and the
     files it reads values from. A run that replaces `aws s3` with the CLI
     handles the same names and the same text.
  3. The AWS config and credentials files are read as boto3 reads them, by
     the library and the CLI alike: a file boto3 refuses is refused, and one
     boto3 reads is read. Settings the library or the CLI ignores are exempt.
- **Exit codes** (the CLI): follow the meaning aws-cli gives each value
  ([`exit-codes.md`](../docs/cli/exit-codes.md)). Matching aws-cli's value in
  every corner (two mistakes at once, say) is not a goal.
- **Compatibility first**: when there are several valid implementation
  options, prefer the one closer to aws-cli's function. When aws-cli's behavior
  is ambiguous, measure it against the pinned `aws` and read the aws-cli source
  rather than third-party documentation.
- **Symbol-name traceability against aws-cli**: compatibility work depends on
  reading this codebase side by side with the aws-cli source. When a symbol
  here (a class, function, or option/config key) corresponds to an aws-cli
  counterpart, keep the same name with the same meaning, or an extended form
  whose correspondence remains easy to trace (e.g., a function ported from
  aws-cli keeps aws-cli's name even where it re-raises its failure as the
  library's exception). Renaming such a symbol to something unrelated - or reusing an
  aws-cli name for a different meaning - obscures the port; a change (including
  a refactoring) that would significantly break this correspondence is
  rejected.
- **Responsibility boundary for compatibility**: functional compatibility is
  the **library's** responsibility, for every input `aws s3` accepts; the
  library may be a superset that is more permissive than aws-cli for
  convenience. The **CLI layer (`boto3-s3-cli`)** narrows the inputs just
  enough to match `aws s3`, and owns argument validation, output and exit
  codes. "Compatibility first" is a tie-breaker for design decisions; it does
  not require the library API to behave identically to `aws s3` in every
  detail.
- **OS-dependent behavior**: host-OS-dependent behavior such as path separators
  and case sensitivity is matched to aws-cli on each supported OS.
- **Unsatisfiable option combinations**: prefer making a mutually-exclusive or
  meaningless option combination **unrepresentable in the API** over policing it
  at runtime - e.g., options that only tune one of `sync`'s comparison methods
  belong to that method, so they cannot be paired with another. Where a
  combination cannot be designed away, raise vs ignore is decided per case and
  discussed beforehand (the library may fail loud where aws-cli is silent; the
  CLI follows aws's exit code meanings).
- **Versioning**: `boto3-s3` and `boto3-s3-cli` are versioned independently.
- Changes that might break functional compatibility with aws-cli are not
  undertaken unilaterally; discuss them beforehand.

## 4. Documentation index

Documentation is split by who it promises things to, and each half is the
single source of truth for its own half:

- **[`docs/`](../docs/README.md)** - what the library and the command promise,
  and how to use them. A change here is a change to what users may depend on, so
  it travels with a CHANGELOG line.
- **`design/`** - how it is built and why. Only solidified design is written
  here, and it may change without notice to users as long as the promises above
  hold.

The two must not restate each other. Where the design documents describe
behavior a user depends on, `docs/` states the promise and the design document
keeps the mechanism, each linking to the other - `design/cli.md` section 6 and
[`docs/cli/exit-codes.md`](../docs/cli/exit-codes.md) are the worked example.

Because the halves have files of the same name (`sync.md`, `README.md`), a
reference from code - where there is no relative path to resolve against - must
carry the directory: `design/sync.md section 10`, not `sync.md section 10`.
Markdown links inside each half resolve relatively and need no prefix.

### Design documents

- [`glossary.md`](./glossary.md) - glossary.
- [`compatibility.md`](../docs/compatibility.md) - which `boto3` version (and
  whether the `crt` extra) each feature needs, and how an unavailable one
  behaves.
- [`exceptions.md`](./exceptions.md) - the exception model.
- [`opresult.md`](./opresult.md) - the `OpResult` record (the `on_result`
  callback): the fields, the `src` / `dest` convention, and which operation
  populates which.
- [`s3.md`](./s3.md) - the design of the `S3` entry point (the `client` /
  `resolve` customization seams, resolution rules, module-level functions).
- [`storage.md`](./storage.md) - the `Storage` abstraction and custom backends
  (the subclassing contract, `StorageCapability`, the open route).
- [`deleter.md`](./deleter.md) - the design of `S3Deleter` (asynchronous batch
  deletion).
- [`globsieve.md`](./globsieve.md) - aws-cli's `--exclude` / `--include`
  engine and what each operation shows a filter.
- [`transfer.md`](./transfer.md) - the design of the transfer engine
  (`Transferrer` / `S3.cp` / `S3.mv`).
- [`crt.md`](./crt.md) - the design of the CRT transfer engine mode
  (`preferred_transfer_client`, library = boto3-faithful / CLI = aws-faithful).
- [`sync.md`](./sync.md) - the design of `S3.sync` (two-layer pipeline,
  comparator, the per-lane `create_filter` / `update_filter` / `delete_filter`
  axis).
- [`imports.md`](./imports.md) - import discipline (lazy root re-exports and the
  SDK-free top-level CLI help/version exits).
- [`masking.md`](./masking.md) - credential masking for debug logs
  (`set_stream_logger`, `SecretMaskingFilter`, the replacement notation).
- [`aws-cli-option-handling.md`](./aws-cli-option-handling.md) - handling of
  `aws s3` options that are no-ops / unsupported.
- [`cli.md`](./cli.md) - the design of the CLI layer (`boto3-s3-cli`).
  Implemented subcommands (currently `cp` / `ls` / `mv` / `rm` / `mb` / `rb` /
  `presign` / `sync` / `website` - all `aws s3` subcommands).
### Developer runbooks

Neither design nor promises to users - how to work on the project. Read with
[`CONTRIBUTING.md`](../CONTRIBUTING.md), which is the entry point.

- [`testing.md`](./testing.md) - the test structure (5 tiers, golden
  contracts, e2e gate, the exit code comparison) and the output comparison
  criterion.
- [`endpoint-fidelity.md`](./endpoint-fidelity.md) - how the local test
  endpoint (the dev stack, RustFS) answers differently from real S3, the
  test-side change made for each gap, and how that server was chosen.
- [`benchmark.md`](./benchmark.md) - the local performance benchmarks
  (E2E differential against the pinned aws-cli, in-process stubbed-S3
  timings, startup-adjusted comparison, regression flags).
- [`release.md`](./release.md) - how the two packages are published to PyPI
  (tag-driven GitHub Actions, Trusted Publishing, independent versioning).

The `S3` entry point itself is documented in [`s3.md`](./s3.md), the `Storage`
abstraction and custom backends in [`storage.md`](./storage.md); each operation's
per-method semantics live with its engine (`transfer.md` / `sync.md` /
`deleter.md` / `globsieve.md`).
