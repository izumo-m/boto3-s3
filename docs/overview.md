# boto3-s3 Project Overview (current state / source of truth)

This document is the **established current state (source of truth)** for the
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
- **AWS SDK floor**: `boto3` >= 1.28, `botocore` >= 1.31, `s3transfer` >= 0.6.2
  (a roughly 3-year window). These are the oldest versions the library and CLI
  are supported against; a future release may raise the floor (when it does,
  the back-compat shims that carry a comment to that effect can be removed).
- **Feature-level degradation on old SDKs**: rather than emulate newer AWS
  behavior, features that depend on a newer S3 model are simply unavailable
  below the SDK version that introduced them - on a par with the awscrt extra
  (transfer.md section 9 / crt.md section 6). Specifically, `--no-overwrite`
  (conditional writes / `IfNoneMatch`, GA 2024-11) needs a late-2024 botocore,
  and `--checksum-algorithm CRC64NVME` needs a botocore that ships that
  algorithm. The bucket-listing filters `ls --bucket-name-prefix` /
  `--bucket-region` (paginated `ListBuckets`, late 2024 / botocore 1.34.162)
  are likewise silently inert below that botocore - `ls` itself still works,
  falling back to an unpaginated `ListBuckets`. Everything else works at the
  floor.

## 3. Design policy

- **Parity first**: when there are multiple valid implementation options,
  **prefer the one with higher parity with aws-cli**. When aws-cli's behavior is
  ambiguous, read the aws-cli source as the primary
  source rather than third-party documentation.
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
  are only two exceptions.
  1. Extension options that do not exist in `aws s3` (e.g., the CLI's own
     `--help` / `-h` / `--version`, which aws-cli instead exposes as a `help`
     subcommand)
  2. When it depends on a feature that is hard to realize (e.g., the CLI's
     interactive UI)

  awscrt-dependent features (the CRT transfer engine, CRT-family checksums,
  SigV4a signing) are subject to this charter when awscrt is present (a mismatch
  is a bug). The **CRT transfer engine** (`[s3] preferred_transfer_client`)
  likewise takes parity against "aws's CRT mode" when awscrt is present (design
  in [`crt.md`](./crt.md); enforced by the e2e CRT lane). awscrt is not a
  default dependency but an opt-in extra (`crt`); on an installation without it,
  only the relevant features fail - this does not count as a mismatch
  (degradation is covered in transfer.md section 9 / crt.md section 6).
- **OS-dependent behavior**: host-OS-dependent behavior such as path separators
  and case sensitivity is matched to aws-cli on each supported OS.
- **Versioning**: `boto3-s3` and `boto3-s3-cli` are versioned independently.
- Changes that might break aws-cli compatibility are not undertaken
  unilaterally; discuss them beforehand.

## 4. Documentation index

`docs/` is the single source of truth for the design. Only solidified design is
written here.

- [`glossary.md`](./glossary.md) - glossary.
- [`exceptions.md`](./exceptions.md) - the exception model.
- [`s3.md`](./s3.md) - the design of the `S3` entry point (the `client` /
  `resolve` customization seams, resolution rules, module-level functions).
- [`deleter.md`](./deleter.md) - the design of `S3Deleter` (asynchronous batch
  deletion).
- [`globsieve.md`](./globsieve.md) - the glob filter engine and the filter
  contract of `S3.rm`.
- [`transfer.md`](./transfer.md) - the design of the transfer engine
  (`Transferrer` / `S3.cp` / `S3.mv`).
- [`crt.md`](./crt.md) - the design of the CRT transfer engine mode
  (`preferred_transfer_client`, library = boto3-faithful / CLI = aws-faithful).
- [`sync.md`](./sync.md) - the design of `S3.sync` (two-layer pipeline,
  comparator, the `compare` strategy axis).
- [`imports.md`](./imports.md) - import discipline (lazy re-export, lazy SDK
  loading, the contract for the CLI startup path).
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
- [`testing.md`](./testing.md) - the test structure (5 tiers, golden
  contracts, e2e gate, enforcement of the exit code charter).
- [`release.md`](./release.md) - how the two packages are published to PyPI
  (tag-driven GitHub Actions, Trusted Publishing, independent versioning).

The `S3` entry point itself is documented in [`s3.md`](./s3.md); each operation's
per-method semantics live with its engine (`transfer.md` / `sync.md` /
`deleter.md` / `globsieve.md`).
