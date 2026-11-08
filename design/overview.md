# boto3-s3 Project Overview

This document is the **authoritative reference (source of truth)** for the
project's purpose, its scope, and what comes first when a specification is
decided. The specifications themselves - what the library and the command
do, and how they are built - are written in `design/` and `docs/` and follow
from it; [`README.md`](./README.md) lists the design documents. When the
policy changes, update this document first. It names no part of the code and
settles no single feature: those change with the specifications, and this
does not.

For term definitions, see [`glossary.md`](./glossary.md).

---

## 1. Purpose

Make the `aws s3` command a Python library: every subcommand runs in-process,
keeps the command's path rules, filters and `sync` decisions, and transfers
through the engines `aws s3` itself uses (`s3transfer` and the CRT). Other
projects offer `aws s3 sync` from Python, but they run the `aws` command
underneath, implement only part of it, or lack `s3transfer` and CRT support.
Running the `aws` command is not always an option either: aws-cli is a large
package, which can make it unsuitable for a container such as a Lambda
function.

The project ships two packages.

- **`boto3-s3`** - the library, which is the product. It provides `aws s3`'s
  operations through its own Python API - an independent implementation, not a
  boto3 client wrapper, though it uses boto3 / botocore / s3transfer
  underneath - and **may behave more permissively than aws-cli** for
  convenience.
- **`boto3-s3-cli`** - a by-product: the `boto3-s3` command, built on the
  library with `aws s3`'s command line. Anyone who wants a CLI is served by
  aws-cli itself. This layer owns what only a command line has: strict
  argument validation, result lines and exit codes. Its e2e suite is also
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

- **OS and Python**: Linux, macOS and **Windows**, on the Python versions each
  package's `pyproject.toml` declares.
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
  aws-cli v2. Neither the library nor the CLI signs with it on its own, and
  nothing is shaped to keep it working. A client the caller configures for it
  is neither refused nor supported: whether it works is the caller's concern.

## 3. Design policy

- **Functional compatibility**: in the normal path, each library operation
  does what its `aws s3` subcommand does under the corresponding arguments and
  the same conditions: the same objects and files are transferred, deleted or
  created, with the same content, properties and request headers; `sync`
  makes the same decisions; an option means the same thing. The CLI does the
  same, and a successful run prints what `aws s3` prints. In the
  semi-abnormal and abnormal paths, aws-cli's behavior is the reference
  rather than a target to match in full. What decides the functional outcome
  is matched: a run carries on past a per-item failure where `aws s3` carries
  on, and stops before it starts where `aws s3` stops. Warnings and error
  messages follow aws-cli as **Messages** (below) sets out. A functional
  difference is either a bug or a deliberate deviation written down in
  [`aws-differences.md`](../docs/cli/aws-differences.md); a difference the
  Messages rules leave uncompared is neither, and is not written down.
  The CRT transfer engine is held to aws-cli's CRT mode on the same terms
  ([`crt.md`](./crt.md)).
- **Compatibility target**: the pinned aws-cli release the code is ported
  against, together with the boto3 / botocore / s3transfer / awscrt releases
  of the same period. Older aws-cli releases are not a target. Two version
  differences stand between this project and that release: aws-cli bundles
  its own Python and its own copies of botocore and s3transfer, which differ
  from the Python and the PyPI releases this project runs on, and the library
  also runs on SDKs as old as the floor and on every supported Python
  (section 2). Four rules apply, by where a difference comes from:
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
     same way. A difference in what an operation returns to the caller is
     matched however the clients were assembled; one in how the clients send
     their requests is matched under aws-cli's assembly, which the caller
     asks the library for and the CLI is built on, while clients assembled
     otherwise keep boto3's defaults ([`s3.md`](./s3.md) section 2 has which
     is which).
  3. A difference that comes only from an SDK older than the pinned period,
     down to the floor, is not matched: it is neither a bug nor a deviation to
     write down, and nothing compensates for it.
  4. A difference that comes from how aws-cli is packaged and started,
     rather than from its own code, is not considered: its interpreter is
     frozen into the distribution, runs isolated from the user's
     environment, and receives the command line from a launcher of its own.
     The library and the CLI run on the Python the user installed and take
     what that Python takes from the environment and the command line.
     Such a difference is neither a bug nor written down.

  The rules for errors below still hold on every supported version.
- **Security before compatibility**: where matching aws-cli would expose a
  secret or silently turn a protection off, the library and the CLI do not
  match it. Such a deviation is a functional difference and is written down
  in [`aws-differences.md`](../docs/cli/aws-differences.md).
- **Messages**: a warning or an error message is compared with aws-cli's in
  two parts, so that the same difference is judged the same way wherever it
  is found. How each part is compared is set out in
  [`testing.md`](./testing.md) section 9.
  1. The part aws-cli's own code writes is written the same way, character
     for character. Ours may say it differently only where aws-cli's text says
     something untrue about the run or says nothing about what went wrong.
  2. The part the SDK, awscrt, Python, the OS or S3 writes is reported as they
     write it, never rewritten, and only its cause is matched.
  3. A difference is fixed. Keeping one is discussed beforehand, and only
     where fixing it would cost function or security, need a large
     mechanism, or conflict with a settled decision.
  4. Not compared, and neither a bug nor written down: a difference that
     comes only from an SDK, Python or OS version (the compatibility target
     rules above); an error's **details** - which of two simultaneous
     mistakes is reported, how the one reported is written where the other
     changes it, and the order of the reports; the report where aws-cli
     loses the failure or fails on a bug of its own, whose text is ours (the
     error rules below); a message that follows from a deliberate deviation;
     a message with no aws-cli counterpart.
- **Errors take the intended behavior, not aws-cli's**: how a failure is
  detected, reported and classified follows these rules rather than aws-cli's
  implementation.
  1. A failure is never reported as a success. An item that failed is a failed
     item, and the exit code says so, even where aws-cli loses the failure and
     reports success.
  2. A failure is reported so that its cause can be seen: not as a traceback,
     and with the original exception kept as its cause. This covers the reports
     the library and the CLI write, and it is met by catching exceptions in their
     own code. What awscrt or the interpreter writes by itself is left alone, as
     is an exception awscrt drops while it reads a request body to sign it
     ([`transfer.md`](./transfer.md) section 9), and no code exists only to
     change how a traceback reads.
  3. A per-item failure leaves the other items running; a failure the run
     cannot proceed past (configuration, credentials, a missing destination)
     stops it before it starts.
  4. The library's exceptions classify the failures within its own reach: the
     SDK, S3 and local files. How the caller's code fails is the next item.
  5. Once an operation's exception has reached the caller, `on_result` and
     `on_progress` are not called again.
- **Ctrl-C is not matched to aws-cli**: nothing is added only to make an
  interrupted run behave as aws-cli's does, in its output, its exit code or the
  requests it still sends, and a difference there is neither a bug nor a
  deviation to discuss. What an interrupt or a cancel does follows from these
  rules alone:
  1. An asynchronous exception leaves the `S3` unusable. It is an exception a
     signal raises wherever the program happens to be: a `KeyboardInterrupt`
     from Ctrl-C, or what a signal handler raises (a `SIGALRM` handler's, say).
     It can land anywhere, so no code can promise to clean up after it: the `S3`
     whose operation it interrupts is not used again (what that `S3` then does
     is not specified), and a new `S3` works. Every other end keeps the `S3`
     usable. A return and any other exception clean up before control goes back
     to the caller. A `CancelToken`'s immediate cancel does not wait for the
     requests already sent: nothing can cancel a request already out, so they and
     their cleanup go on behind the caller, and what they do can still change the
     result after the operation has returned. A graceful cancel finishes the work
     already accepted, as it is defined to. No cancel cuts short a request the
     operation's own thread is waiting on: the operation returns once that request
     does. On any early end, a listing page already being fetched is left to the
     scan's worker, which finishes and drops it behind the caller, so a `filter`
     call on it can come after the operation has raised. An application that
     wants to stop an operation and keep its `S3` cancels it through a
     `CancelToken`, whose `cancel` a signal handler may call.
  2. A `KeyboardInterrupt` the library sees is handed back at once, re-raised:
     nothing can cancel a request already out, and a server that does not answer
     holds it for a full timeout. What has not started never starts, and the
     cleanup goes on behind the caller, as far as the process lives. That is a
     best effort, not a guarantee: Python raises the interrupt only where the main
     thread runs Python code, so a wait the library does not own holds it until
     that wait ends, and code outside the library, Python's own included, can lose
     it or raise another exception in its place. The next Ctrl-C covers such a
     case, as far as that wait can be woken at all. One pressed while the
     operation cleans up after another exception can wait for the requests
     already out, as that cleanup does. The library cannot tell any other
     asynchronous exception from the same exception raised in place, so it
     handles it as it does that one; the previous rule still applies.
  3. Running the operation again on a new `S3` reaches a consistent result.
  4. The CLI ends its process with `os._exit`: one
     `cancelled: ctrl-c received` line on standard error and exit code 130. The
     cleanup is cut off there, so an unfinished multipart upload and a download's
     temporary file can stay behind, which the previous rule does not cover.
  5. That line and exit code are a best effort, not a guarantee: at worst, it is
     enough that the command stops, at the user's next Ctrl-C if need be (the
     second rule).
- **The `signal` module is a last resort**: it is highly OS-dependent and costs
  portability, so it is used only where no other way exists. Neither the library
  nor the CLI installs a signal handler; a gap one could close (the previous rule)
  is not reason enough.
- **A stalled output may stop the program** until it clears, even through a
  failure or a Ctrl-C.
- **A wait does not poll**: a thread waiting for another sleeps until woken, and
  does not wake by itself to look again. Whatever can make what it waits for
  come true wakes it, a `CancelToken`'s `cancel` included. Two exceptions: a
  wait on the main thread also wakes every second to let a Ctrl-C through, as
  Python cannot always wake a wait for one; and a library in use that offers no
  way to be woken.
- **A wait on another thread survives a Ctrl-C**: the calling thread waits on
  `concurrency.Wake` (a C lock underneath), which the other thread sends; not on
  `threading.Event`, `Condition` or `queue.Queue`, which a Ctrl-C landing inside
  can leave locked.
- **The caller's code is the caller's responsibility**: code the caller
  writes and hands to the library - a stream, a storage backend, a filter, a
  callback - is outside the library's reach. The library answers for its own
  code and for what it reaches through the SDK, S3, the local file system and
  Python's standard I/O streams (a file, a pipe, standard input and output, an
  in-memory buffer).
  1. Each place that takes the caller's code documents the contract it relies
     on. The library relies on that contract and neither checks it nor works
     around a breach: what code that breaks it leads to is not specified, and
     is not a library bug.
  2. The caller's code reports its own failures with the library's
     exceptions, and one it raises is recorded as it is. Any other exception
     from it is recorded with the original kept as its cause
     ([`exceptions.md`](./exceptions.md) section 1 has how each kind of code
     is recorded).
- **An argument of the wrong type is not specified**: the type annotations of
  the public API are the contract for its arguments, and a type checker is
  how a caller holds to them. What a value of another type leads to is not
  specified - it may fail, stop responding or appear to work - and is not a
  library bug.
  1. The library checks an argument only where the check is meaningful and
     certain: it never refuses a valid argument. A missing value where one is
     required, a combination of arguments that cannot work together, a value
     outside what the parameter defines, an object whose class makes it the
     other kind - those are checked, and refused with the library's
     exception.
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
  1. The library follows Python's defaults wherever it converts and lets the
     caller name the encoding instead, so an application can fix the one it
     relies on. An encoding the caller names is used as given: whether the OS
     can take names in it is the caller's concern.
  2. The CLI uses the encoding aws-cli uses in the same environment wherever
     it converts. A run that replaces `aws s3` with the CLI handles the same
     names and the same text.
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
- Changes that might break functional compatibility with aws-cli are not
  undertaken unilaterally; discuss them beforehand.

## 4. Documentation

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
keeps the mechanism, each linking to the other.
