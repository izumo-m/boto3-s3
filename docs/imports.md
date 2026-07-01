# Import discipline (lazy re-export and deferred SDK loading)

The design discipline that lets `boto3-s3` / `boto3-s3-cli` "pay import cost only
for the operations actually used." An application typically uses only one or two
operations, and the CLI runs only one subcommand per invocation, yet with eager
imports everyone would pay the full initialization cost of the AWS SDK (contrary
to overview.md's "performance equal to or better").

## 1. Background (fixed constraints)

- **`import boto3` pulls in s3transfer on its own**, because upstream's
  `boto3/compat.py` does `from s3transfer.manager import TransferConfig`. So as
  long as you create a client with boto3, importing s3transfer itself is
  unavoidable. All you can do is "defer it until the moment a client is actually
  created," and that is the upper bound of this discipline.
- Reference measurements (2026-06, WSL2): in the eager era, `import boto3_s3` took
  ~120ms (the boto3->botocore->s3transfer chain ~83ms, `importlib.metadata` ~20ms).
  After deferral it is ~5ms, and the CLI's `--help` went from ~120ms to ~30ms.
  The numbers are environment-dependent reference values; the normative spec is
  the contract in this document and the contract tests (section 6).

## 2. Contract

1. `import boto3_s3` imports none of the AWS SDK modules (`boto3` / `botocore` /
   `s3transfer`) and executes none of the package's own submodules.
2. Using pure-Python modules (`globsieve` / `types` / `exceptions`) incurs no SDK
   tax.
3. The SDK dependency permitted at the moment you touch `S3` / `S3Storage` extends
   only to `botocore.exceptions` (and the `botocore.vendored` exception shim it
   pulls in). The `boto3` / `s3transfer` / botocore client stack is not imported
   until the moment a default client is actually created (the fallback in
   `S3Storage.get_client`).
4. The CLI's **pre-dispatch** paths import no SDK module: `--help` / `--version`
   (argparse short-circuits during the parse) and the parse-time usage errors
   (invalid choice, unknown option, missing subcommand). Once a subcommand's
   `run()` is entered the SDK may load - `botocore` on the error path (the
   `exit_code_for` `ClientError`-cause check) and `boto3` when the command builds
   its client (`build_client`). `mb` / `rb` build the client up front, before
   their path checks, to match aws's client-before-path-validation ordering, so
   even a path usage error there loads `boto3`; this is within the contract
   (a subcommand is running) and is what the contract test enforces - it pins the
   SDK-free guarantee to the pre-dispatch paths only (section 6).

## 3. Library implementation

- `boto3_s3/__init__.py` is a lazy re-export via **PEP 562** (module
  `__getattr__`). Public names are resolved on first access from `_EXPORT_HOMES`
  (name -> home module) and cached into `globals()`. The type checker sees the real
  imports in the `TYPE_CHECKING` block, so consumers' types are unchanged from the
  eager era. **`__all__`, the `TYPE_CHECKING` imports, and `_EXPORT_HOMES` must
  agree across all three** (the contract test's all-symbols-resolve case detects
  any drift).
- `__version__` is likewise resolved on first access via `importlib.metadata` (the
  ~20ms deferral target).
- The `TransferConfig` in `s3.py` is an annotation-only import (`TYPE_CHECKING`).
  The re-export home for the public `TransferConfig` is `transferconfig.py`
  (boto3's subclass + the CRT fields, crt.md section 2), which imports
  `boto3.s3.transfer` at module top - since it is reachable only through the lazy
  re-export, the SDK-no-import contract of a bare `import boto3_s3` is preserved.
  `crtsupport.py` makes all of awscrt / `s3transfer.crt` into in-function imports,
  so the classic path and a bare import pull in no CRT dependency.
- `s3storage.py` does `import boto3` only inside the default-client fallback of
  `get_client`. `botocore.exceptions` is kept at top level because it is needed for
  exception translation (`s3_errors`) (a dependency permitted by contract item 3).
- Trade-off: an `ImportError` for a missing dependency surfaces "at first access"
  rather than "at import time." This is an intentional consequence of the deferral,
  and the contract tests continuously verify that the public surface is resolvable.

## 4. CLI implementation

- **Place imports that reach the SDK inside `build_client` and each
  `Command.run()`**, not at the module top level where `configure()` runs. Names
  from the pure-Python `types` / `exceptions` / `globsieve` may stay at top level
  (e.g., `OpOutcome` in `rm.py` - used by a module-level printer).
- The `--version` line is assembled when the action fires. The boto3 / botocore
  versions are read from distribution metadata (`importlib.metadata.version`); the
  package itself is not imported.
- The `--help` choices / help text are written as a **static mirror of aws-cli's
  static tables** (the same approach as `globals.py`'s `_OUTPUT_CHOICES`,
  which mirrors `cli.json`; the `aws s3` side likewise keeps all choices as static
  literals in aws-cli's `awscli/customizations/s3/subcommands.py`). They are
  not derived dynamically from botocore's service model - since aws-cli itself is
  static, going dynamic would skew parity whenever botocore updates ahead, so this
  is not a discipline for speed alone.

## 5. Discipline for the transfer subcommands (cp / mv / sync)

- Keep `S3` a thin orchestrator, and keep the transfer engine that uses
  s3transfer / `TransferManager` in a dedicated module (`boto3_s3/transfer.py`)
  that is **SDK-free at import**: the `s3transfer.manager` import is deferred
  into the functions that build the manager, which run only when cp / mv / sync
  actually submit work. Do not bring the s3transfer dependency into the rm / ls
  paths (the deleter and the scan / scan_pages path).
- The entry point's boto3 dependency is isolated in `S3.client()`: it imports
  boto3 only when called. Constructing an `S3` (with or without `session` /
  `endpoint_url` / `config`) is therefore SDK-free; boto3 loads when `client()`
  runs - i.e. when an operation builds a client, or `resolve()` turns an
  `"s3://..."` string into an `S3Storage`.
- Likewise, `Storage`'s methods (`scan_pages()` / `open()` / `delete()`) defer
  their heavy dependencies until the method is called.
- When you add a subcommand, add a "its `--help` is SDK-free" case to the contract
  tests.

## 6. Enforcement

`tests/lib/test_import_contract.py` and `tests/cli/unit/test_import_contract.py`
pin down contract section 2. Module-loading cases run in a fresh interpreter (a `python -c`
subprocess) so that the test runner's own imports do not mask a regression.
