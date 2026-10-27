# boto3-s3 Project Overview (current state / source of truth)

This document is the **authoritative reference (source of truth)** for the
project's purpose, supported scope, and design policy, and it doubles as the
entry point (index) for `docs/`. When the policy changes, update this document
first. It states policy only - what would hold however the code were
written. Names in the code, the mechanisms behind them and the decisions
about one feature belong to the design documents and `docs/`, which hold
the examples too.

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
  at the floor. A feature whose absence would fail every item for the same
  reason, or quietly drop a safeguard, is checked against the installed SDK
  before the run starts; one whose failure is reported correctly where it
  happens is called without a check.
- **No SigV2**: S3's legacy Signature Version 2 is out of scope, as it is for
  aws-cli v2, whose bundled botocore has no S3 SigV2 signer left. Neither the
  library nor the CLI signs with it on its own, and nothing is shaped to keep
  it working. A client the caller configures for it (`signature_version='s3'`)
  is neither refused nor supported: whether it works is the caller's concern.

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
  stops before it starts where `aws s3` stops. Warnings and error messages
  follow aws-cli as **Messages** (below) sets out. A functional difference is
  either a bug or a deliberate deviation written down in
  [`aws-differences.md`](../docs/cli/aws-differences.md); a difference the
  Messages rules leave uncompared is neither, and is not written down.
  The CRT transfer engine is held to aws-cli's CRT mode on the same terms
  ([`crt.md`](./crt.md)). The CLI has no interactive auto-prompt.
- **Compatibility target**: the pinned aws-cli release the code is ported
  against, together with the boto3 / botocore / s3transfer / awscrt releases
  of the same period. Older aws-cli releases are not a target. Two version
  differences stand between this project and that release: aws-cli bundles
  its own Python and its own copies of botocore and s3transfer, which differ
  from the Python and the PyPI releases this project runs on, and the library
  also runs on SDKs as old as the floor and on every supported Python
  (section 2). Three rules apply, by where a difference comes from:
  1. A difference that comes from the Python version is matched to aws-cli
     where it can be absorbed simply, and left as it is otherwise. Simply
     means all three of: matching it changes nothing outside the library that
     the application did not ask for (no process-wide replacement that the
     application's other code would feel as a side effect of using the
     library), it needs no large mechanism, and it conflicts with no settled
     decision. A process-wide change the application asks for by calling a
     function meant for it, with its reach documented, is not a side effect.
     The CLI owns its process, so the first condition does not bind it. A
     difference left as it is is neither a bug nor a deviation to write down,
     so a behavior may depend on the Python version; where it is a default a
     caller may want fixed, the library lets the caller name the value, as it
     does for text encodings (below).
  2. A difference that comes from aws-cli's bundled botocore or s3transfer is
     matched where it changes a normal run's outcome (the request sent, its
     endpoint, signature and headers, the objects selected, the profile used,
     the retries made, the arguments accepted); a difference that shows only
     where aws-cli refuses the input, or where the run sets something
     aws-cli's bundled SDK does not read, is not compensated. The arguments
     accepted are those aws-cli accepts: a run aws-cli takes is taken the
     same way. Where it is matched depends on what it touches:
     - a value an operation returns to the caller (a presigned URL, its
       signature and its host) is matched however the clients were assembled;
     - how the clients send their requests (the endpoint they send to, the
       retry posture, the default checksum, the order the profile variables
       are read in) is matched only under aws-cli's assembly (below).
  3. A difference that comes only from an SDK older than the pinned period,
     down to the floor, is not matched: it is neither a bug nor a deviation to
     write down, and nothing compensates for it.

  The defaults of a client and its session that the caller does not supply
  (the region, the profile, the default checksum, the CRT client's trust
  source) follow how the clients were assembled: boto3's by default, and
  aws-cli's when the caller asks the library for aws-cli's assembly, the one
  the CLI is built on. A client the caller hands over keeps its own
  settings either way. A caller that assembles its own sets them as for any
  boto3 application. The rules for errors below still hold on every supported
  version.
- **Security before compatibility**: where matching aws-cli would expose a
  secret or silently turn a protection off, the library and the CLI do not
  match it. For example, credentials are masked in debug logs
  ([`masking.md`](./masking.md)), where aws-cli prints them; nor does a
  report print the bytes a `fileb://` value read, where aws-cli prints them
  when botocore refuses them for a text option - a binary file may hold a
  key or a certificate, and no option takes its bytes as text. Such a
  deviation is a functional difference and is written down in
  [`aws-differences.md`](../docs/cli/aws-differences.md). A `file://` value
  is not covered: it is the option's own text, written there by the user
  for that option, and a report shows it as aws-cli's does.
- **Messages**: a warning or an error message is compared with aws-cli's in
  two parts, so that the same difference is judged the same way wherever it
  is found. Messages are compared by running the pinned `aws` under the same
  conditions on the same OS.
  1. The part aws-cli's own code writes - the sentence around the report, the
     values it puts in (paths, URIs, keys, counts, option names), and whether
     a warning or an error is written at all - is written the same way,
     character for character. A difference is a bug. Ours may say it
     differently only where aws-cli's text says something untrue about the
     run (`Invalid JSON` for valid JSON of the wrong shape) or says nothing
     about what went wrong (a `fatal error: ` with nothing after it); a
     misspelling or an awkward phrase is not reason enough. A comment at that
     place in the code then gives aws-cli's text and the reason.
  2. The part the SDK, awscrt, Python, the OS or S3 writes - the text of an
     exception the report carries - is reported as they write it, never
     rewritten, and only its cause is matched: for an OS error, the number
     the text shows (`[Errno 13]`, `[WinError 5]`); for an S3 error, the
     error code and the operation (`An error occurred (AccessDenied) when
     calling the PutObject operation`); for any other exception, its type
     (for awscrt, the error name). With the same cause, the rest of the text -
     the file names an exception carries, its wording - may differ. A
     different cause is a bug, and so is a text of our own where aws-cli
     reports that part.
  3. A difference is fixed. Keeping one is discussed beforehand, and only
     where fixing it would cost function or security, need a large
     mechanism, or conflict with a settled decision; a difference kept that
     way is commented at its place in the code.
  4. The frame aws-cli writes around the report of an exit code
     (`An error occurred (ParamValidation): ` and `Error parsing parameter
     '--metadata': ` on a usage error) follows our exit code where the two
     differ. Whether the exit code may differ is decided by the Exit codes
     rule below, and the reason for a different one is commented at its
     place. The text inside the frame is compared by items 1 and 2.
  5. Not compared, and neither a bug nor written down:
     - a difference that comes only from an SDK, Python or OS version (the
       compatibility target rules above). This reaches only the match with
       aws-cli: the error rules below hold on every version, so a version
       that makes a report hide its cause or print a traceback is a bug like
       any other;
     - an error's **details**: which of two simultaneous mistakes is
       reported, how the one reported is written where the other changes
       it, and the order of the reports;
     - the report where aws-cli loses the failure or fails on a bug of its
       own, whose text is ours (the error rules below). A bug of its own is
       a traceback; a report carrying an exception of a type that marks a
       programming error (`AssertionError`, `AttributeError`, `IndexError`,
       `KeyError`, `NameError`, `TypeError`); or a report that fails to be
       written and leaves another error in its place. This leaves out the
       report only. Where the outcome differs as well - aws-cli stops on a
       bug of its own where ours runs on - that is a functional difference,
       written down in
       [`aws-differences.md`](../docs/cli/aws-differences.md) as one of
       aws-cli's bugs not copied;
     - a message that follows from a deliberate deviation written down in
       [`aws-differences.md`](../docs/cli/aws-differences.md);
     - a message with no aws-cli counterpart, such as a check only the
       library makes.
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
     exception, with the original kept as its cause - except a stream source
     opened before the transfer, as aws-cli opens standard input: one that
     cannot be opened stops the run before it starts (the errors rule 3), with
     the same classification. An interrupt (Ctrl-C, a
     request to exit) cancels the item and reaches the caller. An exception
     from a filter or a callback reaches the caller as it is: a filter decides
     on an item and a callback reports on one, so their failure is not the
     item's.
- **An argument of the wrong type is not specified**: the type annotations of
  the public API are the contract for its arguments, and a type checker is
  how a caller holds to them. What a value of another type leads to is not
  specified - it may fail, stop responding or appear to work - and is not a
  library bug.
  1. The library checks an argument only where the check is meaningful and
     certain: it never refuses a valid argument. A `None` where a value is
     required, a combination of arguments that cannot work together, a value
     outside what the parameter defines, an object whose class makes it the
     other kind (a text stream where a binary one is expected) - those
     are checked, and refused with the library's exception.
  2. It does not guess at a value from its attributes or its behavior, and a
     check it makes is a help, not part of the contract: it does not promise
     to catch every value of the wrong type.
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
     an application can fix the one it relies on. Under aws-cli's assembly,
     local file names take the encoding aws-cli uses in the same
     environment, as in the CLI (rule 2). An encoding the caller names for
     local file names is used as given: the OS gets the names in it.
     Whether the OS and its filesystem can take names in that encoding is
     the caller's concern; the library only refuses a codec Python does not
     know, and neither checks the encoding further nor works around what the
     OS makes of the names.
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
  rejected. Where the code is shaped differently from its counterpart
  (computing from held state what aws-cli re-parses from a string, say), its
  docstring or comment names the counterpart and says the two are equivalent.
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
  CLI follows aws-cli's exit code meanings).
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
- [`opresult.md`](./opresult.md) - the per-item result a run reports: its
  fields, which side is the source and which the destination, and which
  operation fills which.
- [`s3.md`](./s3.md) - the operation entry point (its customization seams,
  how a location is resolved, the module-level functions).
- [`storage.md`](./storage.md) - the storage abstraction and custom backends
  (the subclassing contract, capabilities, the open route).
- [`deleter.md`](./deleter.md) - asynchronous batch deletion.
- [`globsieve.md`](./globsieve.md) - aws-cli's `--exclude` / `--include`
  engine and what each operation shows a filter.
- [`transfer.md`](./transfer.md) - the transfer engine (`cp` and `mv`).
- [`crt.md`](./crt.md) - the CRT transfer engine mode.
- [`sync.md`](./sync.md) - `sync` (the two-layer pipeline, the comparator,
  the per-lane create / update / delete filters).
- [`imports.md`](./imports.md) - import discipline (lazy re-exports from the
  package root, and the CLI's help and version answered without the SDK).
- [`masking.md`](./masking.md) - credential masking for debug logs (what is
  masked, and the replacement notation).
- [`aws-cli-option-handling.md`](./aws-cli-option-handling.md) - handling of
  `aws s3` options that are no-ops / unsupported.
- [`cli.md`](./cli.md) - the CLI layer (`boto3-s3-cli`).
### Developer runbooks

Neither design nor promises to users - how to work on the project. Read with
[`CONTRIBUTING.md`](../CONTRIBUTING.md), which is the entry point.

- [`testing.md`](./testing.md) - the test structure (the tiers, golden
  contracts, the e2e gate, the exit code comparison) and the output
  comparison criterion.
- [`endpoint-fidelity.md`](./endpoint-fidelity.md) - how the local test
  endpoint (the dev stack) answers differently from real S3, the
  test-side change made for each gap, and how that server was chosen.
- [`benchmark.md`](./benchmark.md) - the local performance benchmarks
  (E2E differential against the pinned aws-cli, in-process stubbed-S3
  timings, startup-adjusted comparison, regression flags).
- [`release.md`](./release.md) - how the two packages are published to PyPI
  (tag-driven GitHub Actions, Trusted Publishing, independent versioning).

The operation entry point is documented in [`s3.md`](./s3.md), the storage
abstraction and custom backends in [`storage.md`](./storage.md); each
operation's semantics live with its engine (`transfer.md` / `sync.md` /
`deleter.md` / `globsieve.md`).
