# Design documents

How the library and the command are built, and why. What comes first when a
specification is decided is in [`overview.md`](./overview.md), which these
documents follow; what users may rely on is in [`docs/`](../docs/README.md).
Where a design document describes behavior a user depends on, `docs/` states
the promise and the design document keeps the mechanism, each linking to the
other - [`cli.md`](./cli.md) section 6 and
[`docs/cli/exit-codes.md`](../docs/cli/exit-codes.md) are the worked example.

Because `design/` and `docs/` have files of the same name (`sync.md`,
`README.md`), a reference from code - where there is no relative path to
resolve against - must carry the directory: `design/sync.md section 10`, not
`sync.md section 10`. Markdown links inside each half resolve relatively and
need no prefix.

## Design

- [`overview.md`](./overview.md) - the purpose, the scope, and what comes
  first when a specification is decided.
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

The operation entry point is documented in [`s3.md`](./s3.md), the storage
abstraction and custom backends in [`storage.md`](./storage.md); each
operation's semantics live with its engine (`transfer.md` / `sync.md` /
`deleter.md` / `globsieve.md`).

## Developer runbooks

Neither design nor promises to users - how to work on the project. Read with
[`CONTRIBUTING.md`](../CONTRIBUTING.md), which is the entry point.

- [`testing.md`](./testing.md) - the test structure (the tiers, golden
  contracts, the e2e gate, the exit code comparison) and the output and
  message comparison criteria.
- [`endpoint-fidelity.md`](./endpoint-fidelity.md) - how the local test
  endpoint (the dev stack) answers differently from real S3, the
  test-side change made for each gap, and how that server was chosen.
- [`benchmark.md`](./benchmark.md) - the local performance benchmarks
  (E2E differential against the pinned aws-cli, in-process stubbed-S3
  timings, startup-adjusted comparison, regression flags).
- [`release.md`](./release.md) - how the two packages are published to PyPI
  (tag-driven GitHub Actions, Trusted Publishing, independent versioning)
  and how a release's version is chosen.
