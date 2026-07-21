# Import discipline

Two narrow import guarantees, plus the export-surface contract. Nothing else
is constrained: `S3()` / `S3Storage` construction, normal command execution,
usage errors, and subcommand help may all import the AWS SDK freely.

## 1. Contract

1. `import boto3_s3` imports none of the AWS SDK modules (`boto3` / `botocore`
   / `s3transfer`) and executes none of the package's own submodules.
2. Pure-Python building blocks (`globsieve` / `types` / `exceptions`, and the
   other modules the contract test names) import without any SDK module.
3. `boto3-s3 --help` and `boto3-s3 --version` complete without importing any
   AWS SDK module, any per-command module (the `commands` package's shared
   `base` infrastructure may load), or any library module beyond the lazy
   `boto3_s3` root and its pure `exceptions`. Version tokens for installed
   SDK distributions come from package metadata.

## 2. Export surface

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

## 3. Implementation

- `boto3_s3/__init__.py` is a lazy re-export via **PEP 562** (module
  `__getattr__`): public names resolve on first access from `_EXPORT_HOMES`
  (name -> home module) and are cached into `globals()`; the type checker sees
  the real imports in the `TYPE_CHECKING` block, so consumers' types are
  unaffected. **`__all__`, the `TYPE_CHECKING` imports, and `_EXPORT_HOMES`
  must agree across all three** (the contract test resolves every `__all__`
  name and parses the `TYPE_CHECKING` block against `_EXPORT_HOMES`, so any
  drift fails). `__version__` likewise resolves on first access via
  `importlib.metadata`. Trade-off: a missing dependency's `ImportError`
  surfaces at first access rather than at import time.
- SDK-backed modules import the SDK at module top - `s3.py`, `s3storage.py`
  and `sessions.py` import `boto3` (which itself pulls in s3transfer via its
  `compat` module),
  and `transferconfig.py` (the re-export home for the public `TransferConfig`:
  boto3's subclass plus the CRT fields and `annotation_temp_dir`, crt.md
  section 2) imports `boto3.s3.transfer`. The bare-import contract holds because the lazy root
  defers those *module* loads, not because the modules defer the SDK -
  touching a root symbol homed in one of them pays the SDK import.
  `crtsupport.py` keeps all of awscrt / `s3transfer.crt` in-function, so the
  classic path works with awscrt absent (when awscrt *is* installed, botocore
  itself imports it eagerly - the classic path avoids requiring awscrt, not
  its import cost).
- The CLI satisfies the help/version contract with a lazy command table:
  stage 1's parser is built from `cli._COMMAND_TABLE` alone (subcommand ->
  defining module, class, one-line help), and command modules load only after
  the subcommand is determined. The CLI-side implementation points live in
  [`cli.md`](./cli.md) section 7.

## 4. Enforcement

`tests/lib/test_import_contract.py` pins the package-root and pure-module
rules. `tests/cli/unit/test_import_contract.py` pins the two top-level CLI
exits. Module-loading cases run in a fresh interpreter (a `python -c`
subprocess) so that the test runner's own imports do not mask a regression.
