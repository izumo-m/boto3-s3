# Import discipline

This document defines two narrow import guarantees: importing the package root
does not eagerly resolve its public modules, and the CLI's top-level `--help` /
`--version` paths finish without importing the AWS SDK. It does **not** require
`S3()` construction, normal command execution, or usage-error paths to remain
SDK-free.

## 1. Background (fixed constraints)

- **`import boto3` pulls in s3transfer on its own**, because upstream's
  `boto3/compat.py` does `from s3transfer.manager import TransferConfig`. So as
  soon as an SDK-backed path imports boto3, importing s3transfer itself is
  unavoidable. No contract in this document postpones that cost beyond the
  top-level help/version decision.
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
3. `boto3-s3 --help` and `boto3-s3 --version` complete without importing any
   boto3 / botocore / s3transfer module or any subcommand's command module
   (the `commands` package's shared `base` infrastructure may load - the
   contract test carves exactly that out). Version tokens
   for installed SDK distributions come from package metadata. This guarantee
   is deliberately limited to these two top-level exits. Once normal dispatch
   begins - including usage-error handling or subcommand help - SDK imports are
   permitted.

Orthogonal to those import-cost guarantees, the **export surface** is
two-layered: the package root's `__all__` (the lazy re-export, section 3) and
each module's own `__all__` (the documented submodule surfaces - every module
defines one). A name outside a module's `__all__` is private even without a
leading underscore; the stability of a direct submodule import
(`from boto3_s3.etagcompare import EtagComparison`) is carried by that
module's `__all__`. The root re-exports symbols, never modules:
`from boto3_s3 import globsieve` still works, but through the import system's
submodule fallback, not as part of the root surface. The in-repo CLI is held
to exactly this contract - the "Library consumption contract" in
[`cli.md`](./cli.md) section 3, enforced by
`tests/cli/unit/test_library_surface.py`.

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
  `boto3.s3.transfer` at module top. That home module is reachable only through
  the lazy re-export, so the SDK-no-import contract of a bare `import boto3_s3`
  is preserved.
  `crtsupport.py` makes all of awscrt / `s3transfer.crt` into in-function imports,
  so the classic path and a bare import pull in no CRT dependency.
- `s3storage.py` currently defers its default-client boto3 import. This is an
  implementation detail rather than an interface contract; `S3` and
  `S3Storage` construction may import the SDK.
- Trade-off: an `ImportError` for a missing dependency surfaces "at first access"
  rather than "at import time." This is an intentional consequence of the deferral,
  and the contract tests continuously verify that the public surface is resolvable.

## 4. CLI implementation

- **The dispatch is a lazy command table** (`cli._COMMAND_TABLE`: subcommand ->
  defining module, class, one-line help). Stage 1's parser is built from the
  table alone, which is how the top-level help/version contract is satisfied.
  After those exits are ruled out, command modules and SDK modules may load at
  any point that preserves command behavior and error ordering. The full
  `build_parser()` remains the auto-prompt completion model's source of truth.
- The `--version` line is assembled when the action fires. The boto3 / botocore
  versions are read from distribution metadata (`importlib.metadata.version`); the
  package itself is not imported.
- The `--help` choices / help text are written as a **static mirror of aws-cli's
  static tables**, not derived dynamically from botocore's service model. This
  is the same approach as `globalargs.py`'s `_OUTPUT_CHOICES` (which mirrors
  `cli.json`), and it is what aws-cli itself does: its
  `awscli/customizations/s3/subcommands.py` keeps all choices as static
  literals. Because aws-cli is static, deriving the choices dynamically would
  skew parity whenever botocore updates ahead - so this is not a discipline for
  speed alone.

## 5. Discipline for the transfer subcommands (cp / mv / sync)

- Keep `S3` a thin orchestrator, and keep the transfer engine that uses
  s3transfer / `TransferManager` in a dedicated module (`boto3_s3/transfer.py`)
  so the transfer implementation remains local to transfer operations. Import
  timing inside a normally executing operation is not constrained here.
- When you add a subcommand, register it in `cli._COMMAND_TABLE` and add its
  help line to the drift test that pins the static table against the class.

## 6. Enforcement

`tests/lib/test_import_contract.py` pins the package-root and pure-module rules.
`tests/cli/unit/test_import_contract.py` pins the two top-level CLI exits.
Module-loading cases run in a fresh interpreter (a `python -c` subprocess) so
that the test runner's own imports do not mask a regression.
