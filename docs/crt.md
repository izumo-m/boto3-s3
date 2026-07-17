# Design of the CRT transfer engine mode

This document is the established design for boto3-s3's equivalent of the
`aws s3` CRT transfer engine mode
(`preferred_transfer_client`). The core of the transfer side lives in
[`transfer.md`](./transfer.md), the CLI wiring in [`cli.md`](./cli.md) section 8, and
the tests in [`testing.md`](./testing.md). Behavior matches aws
2.35.18, cross-checked against MinIO and the aws-cli / boto3 /
s3transfer source.

## 1. The two-layer split of responsibilities

The decision of whether to use the CRT engine is split across **two layers,
library and CLI**.

```
library layer = boto3-faithful
  Transferrer resolves TransferConfig.preferred_transfer_client
  ('auto' | 'classic' | 'crt') with the same semantics as boto3.
  The decision machinery lives in crtsupport.py.

CLI layer = aws-cli-faithful
  Reads [s3] preferred_transfer_client, settles it to 'classic' / 'crt'
  via the aws-cli TransferManagerFactory decision tree, then hands it to
  the library.
```

- **The library has no decision tree of its own.** It only interprets
  `preferred_transfer_client` with the same rules as boto3's
  `create_transfer_manager` (section 3). An application that uses the library directly
  can select CRT with the same feel as boto3, e.g.
  `TransferConfig(preferred_transfer_client="crt")`.
- **The CLI ports aws-cli's decision tree** (section 4). The CLI reads the `[s3]`
  config, applies the unconditional classic for `s3s3`, and applies the
  `is_optimized_for_system` + process-lock decision for `auto`, then places the
  resolved, settled value on `TransferConfig.preferred_transfer_client` and
  hands it to the library. Settling `auto` rather than passing it through is so
  that it does not duplicate the library's boto3-style `auto` decision, and so
  that the CLI owns the degradation when awscrt is absent (section 6).

**The process lock name is `'boto3-s3'` for both the library and the CLI.** The
s3transfer lock is an arbitration to keep multiple processes of the same
application from standing up a CRT client simultaneously (boto3 uses `'boto3'`,
aws-cli uses `'aws-cli'`); it is not observable in a single process's output or
return code, so it is out of scope for parity. Namespacing it by one's own
product name is the convention of both. s3transfer stores the lock in a module
global created once on the first `acquire_crt_s3_process_lock` and returns that
stored object on every later call (the `name` argument is consulted only at
first creation, not re-checked). So the lock the CLI acquires when resolving
`auto` comes back as the same object from the library's re-acquisition call
regardless of the name passed, and the two-stage acquisition reconciles
naturally.

## 2. Components

| module | role |
|---|---|
| `transferconfig.py` | `TransferConfig` = a subclass of boto3's `TransferConfig`. Adds only the CRT tuning fields that boto3 lacks (`target_bandwidth` / `should_stream` / `disk_throughput` / `direct_io`). A plain boto3 config is also accepted (`crtsupport.py` reads the CRT fields via `getattr` with a default of None, so a plain boto3 config works too). |
| `crtsupport.py` | A faithful port of boto3's `boto3/crt.py` plus improvements. `should_use_crt` (whether to attempt CRT given `preferred`), `create_crt_transfer_manager` (the process-singleton CRT client + serializer, lock, compatibility check), `is_optimized_for_system` / `acquire_process_lock` (the building blocks for the CLI decision tree). It does not pull in awscrt / s3transfer.crt at import time. |
| `Transferrer._get_manager` in `transfer.py` | The engine seam. COPY is unconditionally classic. Otherwise it reads `preferred_transfer_client`, attempts CRT (classic fallback if None), and the rest takes the conventional classic path. |
| `runtimeconfig.py` (CLI) | A port of `RuntimeConfig` from aws-cli `transferconfig.py` + reading `[s3]` + the decision tree (`resolve_transfer_client`) + building the `TransferConfig` (`build_transfer_config`). |

## 3. The library layer (boto3-faithful)

`Transferrer._get_manager` (transfer.md section 2) resolves
`transfer_config.preferred_transfer_client` (default `'auto'`; config=None is
also read as `'auto'`) with the same rules as boto3.

- **COPY (s3->s3) is unconditionally classic.** CRTTransferManager has only
  upload / download / delete and no copy. boto3 likewise drops to
  `preferred_transfer_client='classic'` for a copy in `inject.py` ("copy is not
  supported in the CRT"). Same rule as the aws-cli factory's
  `paths_type=='s3s3' -> classic`.
- **`should_use_crt`** (a port of boto3's `_should_use_crt`): an explicit
  `'crt'` combined with a missing or too-old awscrt (below 0.19.18) raises a
  `MissingDependencyException` (boto3's wording); an explicit `'crt'` on an
  s3transfer below 0.8.0 (no CRT surface at the supported floor 0.6.2) also
  raises `MissingDependencyException` - a branch boto3 does not have, with its
  own wording (`'auto'` degrades to classic there instead); CRT is attempted
  when `(is_optimized and 'auto') or 'crt'`.
- **`create_crt_transfer_manager`** (a port of boto3's `get_crt_s3_client` +
  singleton): lazily creates the process-singleton CRT client + serializer. If
  the lock is held by another process, it returns `None` = classic fallback. A
  later client that falls outside the compatibility check (same region + same
  frozen credentials, **+ our extension: same endpoint, same signing mode**
  [signed/unsigned]) also drops to classic (the same shape as boto3's
  region/credentials-mismatch fallback). On an explicit `'crt'` it also ports
  boto3's `_validate_crt_transfer_config` (which rejects an explicit setting of a
  CRT-unsupported option).
- **Deriving the connection parameters (a documented improvement over boto3)**:
  because the library uses a connection model in which the caller holds the
  client (the S3 connection model in [`overview.md`](./overview.md)), it derives
  the CRT wiring from the client.
  - region = `client.meta.region_name`
  - endpoint = the caller's explicit endpoint when one is threaded in, else the
    host heuristic. The CLI passes its `--endpoint-url` down (`S3(endpoint_url=)`
    -> `Transferrer(crt_endpoint=)` -> `create_crt_transfer_manager(endpoint=)`),
    and that value is honored verbatim - matching aws-cli, whose CRT serializer
    is handed `params['endpoint_url']` as-is, so a custom endpoint that sits
    under an AWS domain (a VPC interface endpoint, a directly-named FIPS /
    dualstack host) is pinned rather than re-resolved to public S3. With no
    explicit endpoint (`endpoint=None`, the default and every non-CLI caller),
    `_derive_endpoint` falls back to the host form of `client.meta.endpoint_url`:
    `None` for an AWS-default form (boto3-faithful - botocore re-resolves it per
    request), the value itself for a custom host (MinIO, etc.). "AWS-default" is
    recognized across every partition the installed botocore knows, not just
    the two commercial suffixes: `_aws_dns_suffixes` collects, once, every
    partition's `dnsSuffix` plus its dualstack/fips variant suffixes straight
    from botocore's own endpoint data - commercial (`amazonaws.com`,
    `api.aws`), china, gov, the eusc `amazonaws.eu`, and the iso partitions
    (`c2s.ic.gov`, `sc2s.sgov.gov`, `cloud.adc-e.uk`, `csp.hci.ic.gov`). A
    standard endpoint in any of them therefore re-resolves like boto3 instead
    of being needlessly pinned. The heuristic's residual limit is what that set cannot
    cover on its own: absent the explicit signal, a custom host *under* an AWS
    suffix would be dropped to `None`, which is exactly why the CLI threads
    `--endpoint-url` through. aws-cli itself has a known bug where, with only the
    env `AWS_ENDPOINT_URL_S3` set, it makes a TLS connection over http and dies
    with `AWS_IO_SOCKET_CLOSED`; we do not hit it because we derive from the
    resolved `client.meta.endpoint_url`.
  - use_ssl = the endpoint's scheme is other than `http`
  - verify = the client's TLS verification setting (`--no-verify-ssl` /
    `--ca-bundle`). Via the private botocore attribute
    `client._endpoint.http_session._verify` (a private dependency at the same
    level as `client._get_credentials()`)
  - credentials = no provider if `signature_version is UNSIGNED`
    (`--no-sign-request`), otherwise
    `BotocoreCRTCredentialsWrapper(client._get_credentials())`
  - serializer session = the caller's session (`S3(session=)` ->
    `Transferrer(session=)` -> `create_crt_transfer_manager(session=)`), falling
    back to boto3's default session when one exists, then to a fresh botocore
    session. Matches aws-cli, which hands its live CLI session to
    `BotocoreCRTRequestSerializer`; a fresh session re-parses the S3 service
    model and endpoint data on every process (~40 ms measured), which was the
    dominant fixed cost of the CRT lane versus aws in the E2E benchmark

  - part_size = that value **only when `multipart_chunksize` is explicitly set**;
    `None` if unset (CRT dynamic). Determined via boto3's `UNSET_DEFAULT`
    sentinel (a faithful version of the same rule as the aws-cli factory)
  - target_throughput = `getattr(config, "target_bandwidth", None)`
  - the fio family (should_stream / disk_throughput / direct_io): because pip
    s3transfer 0.17's `create_s3_crt_client` does not accept `fio_options`, they
    are passed only when the signature check accepts them (forward-compatible;
    the fork bundled with aws-cli supports them)

### subscriber compatibility

CRTTransferManager's future meta differs subtly from classic. `_ProvideSize` /
`_ProvideETag` call `provide_transfer_size` / `provide_object_etag` only after a
`hasattr` guard (CRT meta has neither method, and CRT probes the size itself; the
same shape as the aws-cli `ProvideSizeSubscriber`). `_Progress` /
`_DirectoryCreator` / `_DeleteSource` / `_Completion`, and a download's parent-dir
creation and mtime stamping, work as-is under CRT too (byte-for-byte
download match and mtime stamping confirmed on MinIO). The copy-props subscribers
are copy-only = not on the CRT path. The allow-list addition for `IfNoneMatch`
(`_allow_if_none_match`) works as-is because `ALLOWED_UPLOAD_ARGS` is the **same
object** for CRT and classic (CRT's `--no-overwrite` 412 silent skip
confirmed).

## 4. The CLI layer (aws-cli-faithful)

### `[s3]` runtime config

A verbatim port of `RuntimeConfig` from aws-cli `transferconfig.py`
(`runtimeconfig.py`). It reads the profile's `[s3]` via
`session.get_scoped_config().get("s3", {})` (honoring `AWS_CONFIG_FILE` /
`--profile` / nested `s3 =` INI), converts sizes (`8MB`), rates (`100MB/s` /
`800Kb/s`), and bools, resolves the `default` -> `classic` alias, and validates
invalid values. The wording for invalid values is byte-for-byte, raised as the
library's `InvalidConfigError` - aws-cli's class of the same name reaches the
general handler with rc 255; ours maps to 255 too (exceptions.md section 2).

### The decision tree (`resolve_transfer_client`)

A port of aws-cli `TransferManagerFactory._compute_transfer_client_type`.

| condition | result |
|---|---|
| `paths_type == 's3s3'` | `classic` (unconditional; CRT has no copy) |
| `preferred == 'classic'` | `classic` |
| `preferred == 'crt'` and awscrt present and s3transfer >= 0.8.0 | `crt` (acquires the lock but ignores the result = same shape as aws-cli) |
| `preferred == 'crt'` and awscrt **absent** | `ConfigurationError` (rc 253, section 6) |
| `preferred == 'crt'` and s3transfer **< 0.8.0** (no CRT surface at the supported floor 0.6.2) | `ConfigurationError` (rc 253, the same clean degradation) |
| `preferred == 'auto'` and `is_optimized_for_system()` and the lock is acquirable | `crt` (an s3transfer without the CRT surface silently resolves `classic`) |
| otherwise (`auto` with non-optimized / lock contention) | `classic` |

The CLI settles `auto` and places the resolved `'classic'` / `'crt'` onto
`TransferConfig.preferred_transfer_client` via `build_transfer_config`, then hands
it to the library. Streaming does not force classic (same as aws-cli - a stream
also follows `preferred`).

### Building the `TransferConfig` (`build_transfer_config`)

It passes **only the keys explicitly set** in `[s3]` to the `TransferConfig` ctor
(an unset key stays at boto3's `UNSET_DEFAULT` sentinel = "part_size only when
`multipart_chunksize` is explicit" holds).

**The config is assembled per engine** (the same as aws-cli's factory
building the classic `TransferConfig` and the CRT client from separate sets of
keys).

- **Resolved to classic**: all keys to the ctor. Because `max_queue_size` is not
  in the boto3 ctor, it is attached afterward onto the `max_request_queue_size`
  attribute, and `max_in_memory_upload/download_chunks` is fixed at 6 (the value
  the aws-cli factory permanently installs for classic).
- **Resolved to crt**: only the keys the CRT client actually reads
  (`multipart_chunksize` / `target_bandwidth` / `should_stream` /
  `disk_throughput` / `direct_io` = those that aws-cli `_create_crt_client`
  references). The classic-only keys (`io_chunksize` / `max_bandwidth` /
  `multipart_threshold` / `max_concurrent_requests`) and the classic-only
  attributes (queue size, in-memory chunk cap) are **not passed**. This is to
  match aws-cli ignoring these on the CRT path, and to prevent the case where
  placing `io_chunksize` / `max_bandwidth` on a crt-preferred config gets rejected
  by boto3's `_validate_crt_transfer_config` and turns into an rc 1 traceback
  (aws is rc 0) (avoiding a charter violation; e2e:
  `test_crt_ignores_classic_only_config`).

cp / mv / sync call `transferargs.resolve_transfer_config(args, ctx,
paths_type=...)`. The test-injected `ctx.transfer_config` always takes precedence
(preserving the existing determinization lever). The reading of `[s3]` is placed
**after** the usage (252) and source-absent (255) validations (an
invalid `[s3]` value loses to either).

## 5. Charter treatment

The CRT mode is promoted, in the charter of [`overview.md`](./overview.md) section 3,
from "excluded because hard to realize" to a target that "**takes parity against
aws's CRT mode**" (the CRT transfer engine is removed from charter exception 2).
When awscrt is present, the exit code and output of CRT mode must match those of
aws's CRT mode (enforced by the e2e CRT lane - testing.md).

## 6. Degradation and known differences (record)

- **Deletion stays on its established non-CRT routes**: under CRT configuration,
  single rm keeps its blind `DeleteObject`, recursive rm and S3-side
  `sync --delete` keep `S3Deleter`'s batched `DeleteObjects`, and local-side
  sync-delete keeps `os.remove`; none route through `CRTTransferManager.delete`.
  These are the accepted deletion paths documented in deleter.md section 4;
  the CRT e2e lane pins the charter-observable rc, output, and end states for
  single/recursive rm and both sync-delete directions.
- **awscrt absent x explicit crt**: an area that cannot arise because aws bundles
  awscrt. Our awscrt is an opt-in extra (`boto3-s3-cli[crt]` ->
  `boto3-s3[crt]` -> `boto3[crt]`, transfer.md section 9).
  - CLI: `resolve_transfer_client` stops it with a `ConfigurationError` (rc 253),
    preventing boto3's `MissingDependencyException` from slipping through `main`
    and dying in a traceback (a CLI-specific degradation, not counted as a
    mismatch).
  - Direct library use: it passes boto3's `MissingDependencyException` through, as
    boto3 does (faithful). This is a deliberate exception to the backend-exception
    translation at the library boundary (exceptions.md section 1) - to reproduce boto3's
    behavior.
- **Explicit crt x lock contention**: aws forces CRT; our library does boto3's
  faithful silent classic fallback. The output and rc are identical for both
  engines (proven), so no observable charter is broken - only throughput is
  affected.
- **fio_options**: not in pip s3transfer 0.17; only parsed and validated, with no
  effect (forward-compatible via the signature check).
- **TransferConfig on old s3transfer**: `CRTTransferManager` grew its `config`
  kwarg only in s3transfer 0.16.0, but the floor is 0.6.2 (overview.md section
  2). Below 0.16 the config cannot reach the manager, so boto3-s3 drops it and
  logs boto3's own warning (`configured values will be ignored`),
  boto3-faithfully. The CRT client itself still gets `part_size` /
  `target_throughput` (passed to `create_s3_crt_client` directly), so only the
  manager-level config is lost. The gate is boto3's `TRANSFER_CONFIG_SUPPORTS_CRT`
  = `hasattr(TransferConfig, "UNSET_DEFAULT")`; drop the shim once the floor is
  past 0.16.
- **Process-pinned singleton**: the region / credentials / endpoint of the first
  client to reach the CRT path monopolize the in-process CRT, and an incompatible
  second connection falls back to classic (identical behavior to boto3).
- **Cannot be verified under moto**: because CRT bypasses botocore's HTTP layer,
  actual verification is only on the e2e (MinIO) lane.
