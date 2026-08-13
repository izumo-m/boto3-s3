# boto3-s3 Project Overview (current state / source of truth)

This document is the **authoritative reference (source of truth)** for the
project's purpose, supported scope, and design policy, and it doubles as the
entry point (index) for `docs/`. When the policy changes, update this document
first.

For term definitions, see [`glossary.md`](./glossary.md).

---

## 1. Purpose

Make the equivalent of all `aws s3` subcommands (`cp` / `ls` / `mb` / `mv` /
`presign` / `rb` / `rm` / `sync` / `website`) usable from Python. The project is
structured in two layers.

- **`boto3-s3`** - the library itself. The **building blocks** that provide
  `aws s3`-equivalent operations through its own Python API (the `S3` entry
  point, whose methods mirror the `aws s3` subcommands - it is an independent
  implementation, not a boto3 client wrapper, though it uses boto3 / botocore /
  s3transfer underneath). It is the foundation for assembling `aws s3`-compatible
  tools, and it **may behave more permissively than aws-cli** for convenience
  (e.g., the `S3Storage` constructor also accepts `"bucket/key"` with the
  `s3://` prefix omitted).
- **`boto3-s3-cli`** - the CLI distribution that sits on top of `boto3-s3`. It
  exposes the `aws s3`-compatible `boto3-s3` command. **Behaving exactly like
  `aws s3` (strict argument validation, etc.) is this layer's responsibility**
  (e.g., making `mb` / `rb` / `rm` reject an omitted `s3://` is done on the CLI
  side).

### Intended direction (important)

The goal is **"using the `boto3-s3` library, you can build a tool compatible
with the `aws s3` command."** This **does not mean that the library itself
behaves exactly identically to the `aws s3` command**. The library provides
permissive building blocks that can achieve parity, and the responsibility of
matching `aws s3` exactly (tightening) is borne by the CLI distribution
(`boto3-s3-cli`).

The aim is to deliver performance equal to or better than `aws s3` while
maintaining high functional compatibility (parity).

## 2. Supported scope

- **Python**: 3.10 and later.
- **OS**: Linux / macOS / **Windows**.
- **AWS SDK floor**: the oldest SDKs supported are roughly three years old. The
  numbers themselves live in each package's `pyproject.toml`, which is what
  enforces them and is where they are read from; this states only the policy
  behind them. A future release may raise the floor (when it does, the
  back-compat shims that carry a comment to that effect can be removed).
- **The installed SDK decides the feature set**: rather than emulate newer AWS
  behavior on an older SDK, features that depend on a newer S3 model are simply
  unavailable below the version that introduced them - on a par with the awscrt
  extra. Which feature needs which version, and how an unavailable one behaves,
  is recorded in [`compatibility.md`](../docs/compatibility.md). Everything else works
  at the floor.

## 3. Design policy

- **Parity first**: when there are multiple valid implementation options,
  **prefer the one with higher parity with aws-cli**. When aws-cli's behavior is
  ambiguous, read the aws-cli source as the primary
  source rather than third-party documentation.
- **Symbol-name traceability against aws-cli**: parity work depends on reading
  this codebase side by side with the aws-cli source. When a symbol here (a
  class, function, or option/config key) corresponds to an aws-cli counterpart,
  keep the same name with the same meaning, or an extended form whose
  correspondence remains easy to trace (e.g., the CLI's
  `human_readable_to_int` keeps aws-cli's function name because it is a
  faithful port - same boundary behavior and failure wording, though the
  failure is re-raised as the taxonomy's `InvalidConfigError`). Renaming such a symbol to something unrelated - or reusing an
  aws-cli name for a different meaning - obscures the port; a change (including
  a refactoring) that would significantly break this correspondence is
  rejected.
- **Responsibility boundary for parity**: the layer that ultimately guarantees
  full compatibility with `aws s3` is the **CLI layer (`boto3-s3-cli`)**. The
  library layer only needs to provide building blocks from which a compatible
  tool can be built, and it may be a superset that is more permissive than
  aws-cli for convenience (the CLI layer narrows it just enough to match
  `aws s3`). "Parity first" is a tie-breaker for design decisions; it does not
  require the library API to behave identically to `aws s3` in every detail.
- **exit code parity (charter)**: the exit code of `boto3-s3 <subcommand>`
  **must match** that of `aws s3 <subcommand>` under the same arguments and the
  same conditions, **whether on success or error**. This holds for any arguments
  and values. A mismatch is a bug, and it must be detectable by e2e tests. There
  are only three exceptions.
  1. Extension options that do not exist in `aws s3`. The CLI declares none:
     its option surface is exactly `aws s3`'s, so this exception is currently
     unused. `--version` is *not* one of them - aws accepts it under any
     `aws s3` subcommand through its global argument handling (version line,
     rc 0) and the CLI matches that.
  2. When it depends on a feature that is hard to realize (e.g., the CLI's
     interactive UI)
  3. Behavior differences rooted in the transfer engine underneath: aws-cli
     bundles its own s3transfer fork while this project rides pip
     `s3transfer`, and an observable difference (exit code, output, requests
     sent, failure timing) that originates in a behavior difference between
     those two engines is outside both parity charters. A difference noticed
     is recorded as a known divergence in the design docs. Where the library
     can easily compensate - a default value, a public class attribute, a
     subscriber - it aims to behave like the bundled fork anyway; deciding
     not to compensate is legitimate and is recorded together with its
     reason.

  awscrt-dependent features (the CRT transfer engine, CRT-family checksums,
  SigV4a signing) are subject to this charter whenever the CRT stack is usable,
  and a mismatch there is a bug; the **CRT transfer engine**
  (`[s3] preferred_transfer_client`) takes parity against "aws's CRT mode" on
  the same terms (design in [`crt.md`](./crt.md); enforced by the e2e CRT lane).
  Where the stack is not usable only the relevant features fail, and that is not
  a mismatch. What "usable" requires is in
  [`compatibility.md`](../docs/compatibility.md).
- **output parity (charter)**: what `boto3-s3 <subcommand>` writes to stdout and
  stderr **must match** what `aws s3 <subcommand>` writes under the same
  arguments and the same conditions, byte for byte, after a defined
  normalization. That normalization tolerates only what follows from the two
  being different implementations of one command - program identity (the
  command's name and its command hierarchy), leakage of implementation
  internals (tracebacks, addresses, install paths), and run-to-run
  nondeterminism (execution and interleaving order included) - so a difference
  of those kinds is not a parity divergence. Everything the normalization does
  not cover is comparable surface, where a difference **is** a divergence: a
  bug, or a deliberate deviation that has been written down. The three classes,
  each with the test that decides membership, are defined in
  [`testing.md`](./testing.md) section 9. This charter has two exceptions,
  matching the exit code charter's second and third: an **interactive UI**
  (`--cli-auto-prompt`) is outside it, its console output and its completion
  candidates alike, and so is an observable difference rooted in the
  bundled-vs-pip `s3transfer` engine difference, recorded as a known
  divergence the same way.
- **OS-dependent behavior**: host-OS-dependent behavior such as path separators
  and case sensitivity is matched to aws-cli on each supported OS.
- **Unsatisfiable option combinations**: prefer making a mutually-exclusive or
  meaningless option combination **unrepresentable in the API** over policing it
  at runtime - e.g. `sync`'s size/timestamp tuners live inside `AwsCliComparison`
  (the `update_filter=None` default), so they cannot be paired with a content
  `update_filter=`. Where a combination cannot be designed away, raise vs ignore is
  decided per case and discussed beforehand (the library may fail loud where
  aws-cli is silent; the CLI keeps aws exit-code parity).
- **Versioning**: `boto3-s3` and `boto3-s3-cli` are versioned independently.
- Changes that might break aws-cli compatibility are not undertaken
  unilaterally; discuss them beforehand.

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
- [`compatibility.md`](../docs/compatibility.md) - which feature needs which
  `botocore` / `s3transfer` / `awscrt`, and how an unavailable one behaves.
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
- [`globsieve.md`](./globsieve.md) - the glob filter engine and the filter
  contract of `S3.rm`.
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
  (`set_stream_logger`, `SecretMaskingFilter`, parity of the replacement
  notation).
- [`aws-cli-option-handling.md`](./aws-cli-option-handling.md) - handling of
  `aws s3` options that are no-ops / unsupported.
- [`autoprompt.md`](./autoprompt.md) - the design of `--cli-auto-prompt`
  (prompt_toolkit opt-in extra; the completion engine is a port of aws-cli's
  `autocomplete/`; the exact-match baseline and the storage-class gap
  correction).
- [`cli.md`](./cli.md) - the design of the CLI layer (`boto3-s3-cli`).
  Implemented subcommands (currently `cp` / `ls` / `mv` / `rm` / `mb` / `rb` /
  `presign` / `sync` / `website` - all `aws s3` subcommands).
### Developer runbooks

Neither design nor promises to users - how to work on the project. Read with
[`CONTRIBUTING.md`](../CONTRIBUTING.md), which is the entry point.

- [`testing.md`](./testing.md) - the test structure (5 tiers, golden
  contracts, e2e gate, enforcement of the exit code charter) and the
  operational definition of the output parity criterion.
- [`benchmark.md`](./benchmark.md) - the local performance benchmarks
  (E2E differential against the pinned aws-cli, in-process stubbed-S3
  timings, startup-adjusted comparison, regression flags).
- [`release.md`](./release.md) - how the two packages are published to PyPI
  (tag-driven GitHub Actions, Trusted Publishing, independent versioning).

The `S3` entry point itself is documented in [`s3.md`](./s3.md), the `Storage`
abstraction and custom backends in [`storage.md`](./storage.md); each operation's
per-method semantics live with its engine (`transfer.md` / `sync.md` /
`deleter.md` / `globsieve.md`).
