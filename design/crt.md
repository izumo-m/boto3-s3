# Design of the CRT transfer engine mode

This document is the established design for boto3-s3's equivalent of the
`aws s3` CRT transfer engine mode
(`preferred_transfer_client`). The core of the transfer side lives in
[`transfer.md`](./transfer.md), the CLI wiring in [`cli.md`](./cli.md) section 8, and
the tests in [`testing.md`](./testing.md). Behavior matches the pinned
aws-cli, cross-checked against MinIO and the aws-cli / boto3 /
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
| `transferconfig.py` | `TransferConfig` = a subclass of boto3's `TransferConfig`. Adds the CRT tuning fields that boto3 lacks (`target_bandwidth` / `should_stream` / `disk_throughput` / `direct_io`) plus the classic multipart-copy's `annotation_temp_dir` (transfer.md section 4). A plain boto3 config is also accepted (`crtsupport.py` reads the CRT fields via `getattr` with a default of None, so a plain boto3 config works too). |
| `crtsupport.py` | A faithful port of boto3's `boto3/crt.py` plus improvements. `should_use_crt` (whether to attempt CRT given `preferred`), `create_crt_transfer_manager` (the process-singleton CRT client + serializer, lock, compatibility check), `is_optimized_for_system` / `acquire_process_lock` (the building blocks for the CLI decision tree). It does not pull in awscrt / s3transfer.crt at import time. |
| `Transferrer._get_manager` in `transfer.py` | The engine seam. COPY is unconditionally classic, and `capture_response=True` forces classic before any config is read (transfer.md section 2). Otherwise it reads `preferred_transfer_client`, attempts CRT (classic fallback if None), and the rest takes the conventional classic path. |
| `runtimeconfig.py` (CLI) | A port of `RuntimeConfig` from aws-cli `transferconfig.py` + reading `[s3]` + the decision tree (`resolve_transfer_client`) + building the `TransferConfig` (`build_transfer_config`). |

## 3. The library layer (boto3-faithful)

`Transferrer._get_manager` (transfer.md section 2) resolves
`transfer_config.preferred_transfer_client` (default `'auto'`; config=None is
also read as `'auto'`) with the same rules as boto3.

- **COPY (s3->s3) is unconditionally classic** - specified with the rest of the
  engine resolution in [`transfer.md`](./transfer.md) section 2. The boto3
  fidelity behind it: `CRTTransferManager` has only upload / download / delete,
  and boto3 drops to `preferred_transfer_client='classic'` for a copy in
  `inject.py` ("copy is not supported in the CRT").
- **`should_use_crt`** (a port of boto3's `_should_use_crt`): an explicit
  `'crt'` combined with a missing or too-old awscrt (below 0.19.18) raises a
  `MissingDependencyException` (boto3's wording); an explicit `'crt'` on an
  s3transfer below 0.8.0 (the supported floor predates it) also
  raises `MissingDependencyException` - a branch boto3 does not have, with its
  own wording (`'auto'` degrades to classic there instead); CRT is attempted
  when `(is_optimized and 'auto') or 'crt'`.
- **`create_crt_transfer_manager`** (a port of boto3's `get_crt_s3_client` +
  singleton): lazily creates the process-singleton CRT client + serializer. If
  the lock is held by another process, it returns `None` = classic fallback. A
  later client that falls outside the compatibility check (same region + same
  frozen credentials, **+ our extensions: same endpoint, same signing mode**
  [signed/unsigned]**, same TLS `verify`, same `Config.s3` shape** - the CRT
  client bakes in the first client's `verify` and the shared serializer its
  `Config`, so a differing later client must not silently ride those) also
  drops to classic (the same shape as boto3's region/credentials-mismatch
  fallback). The credentials half of that check carries one caller opt-in,
  `allow_absent_credentials` (section 4, "Entering the CRT engine without
  credentials"); it is off by default, so every library caller keeps boto3's
  rule. On an explicit `'crt'` it also ports
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
    dualstack host) is pinned rather than re-resolved to public S3. The pin
    applies only when the run's client was built with that endpoint
    (`client.meta.endpoint_url` equality, checked at the `Transferrer` seam):
    a storage-supplied client with its own endpoint falls back to the host
    heuristic on *its* endpoint instead of being dialed at the S3-level one
    with the wrong credentials. With no
    explicit endpoint (`endpoint=None` - the default when neither the CLI's
    `--endpoint-url` nor `S3(endpoint_url=...)` supplies one),
    `_derive_endpoint` falls back to the host form of `client.meta.endpoint_url`:
    `None` for an AWS-default form (boto3-faithful - botocore re-resolves it per
    request, under the caller's `Config`; see the `config` bullet below), the
    value itself for a custom host (MinIO, etc.). "AWS-default" is
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
  - verify = the client's TLS verification setting. Via the private botocore
    attribute `client._endpoint.http_session._verify` (a private dependency at
    the same level as `client._get_credentials()`). `False` and a CA-bundle
    path ride through; botocore's own default (`True`, or nothing set at all)
    maps to `None`, which is boto3-faithful (plain boto3 passes no `verify` to
    `create_s3_crt_client` either) but means the **platform** trust store,
    not the certifi bundle the classic engine uses. The CLI does not rely on
    this mapping: `clientfactory._resolve_verify` resolves an explicit CA file
    into every client it builds (`--no-verify-ssl` > `--ca-bundle` >
    `ca_bundle` config variable > `REQUESTS_CA_BUNDLE` > botocore's
    `get_cert_path(True)`), so both engines trust the same roots and every
    CLI client passes the verify half of the compatibility check below
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
  - config = the caller's `client.meta.config`, threaded into the serializer
    with the region and endpoint (the serializer merges
    `signature_version=UNSIGNED` on top). This keeps a `None` endpoint's
    per-request re-resolution under the caller's configuration rather than
    stock botocore's defaults: us-east-1 stays on the regional endpoint like
    the classic engine's `us_east_1_regional_endpoint` override (one Host
    across engines), and addressing-style and accelerate/dualstack settings
    carry over the same way

  - part_size = that value **only when `multipart_chunksize` is explicitly set**;
    `None` if unset (CRT dynamic). Determined via boto3's `UNSET_DEFAULT`
    sentinel (a faithful version of the same rule as the aws-cli factory)
  - target_throughput = `getattr(config, "target_bandwidth", None)`
  - the fio family (should_stream / disk_throughput / direct_io): no released
    pip s3transfer's `create_s3_crt_client` accepts `fio_options` (still absent
    at 0.19.0), so they are passed only when the signature check accepts them
    (forward-compatible; the fork bundled with aws-cli supports them)

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
(`runtimeconfig.py`). It reads the profile's `[s3]` through `S3.aws_config()`
(`load_scoped_s3_config` pulls the known keys; `AWS_CONFIG_FILE` / `--profile`
/ the nested `s3 =` INI are honored through that reader), converts sizes (`8MB`), rates (`100MB/s` /
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
| `preferred == 'crt'` and awscrt present (>= 0.19.18) and s3transfer >= 0.8.0 | `crt` (acquires the lock but ignores the result = same shape as aws-cli) |
| `preferred == 'crt'` and awscrt **absent or too old** | `ConfigurationError` (rc 253, section 6) |
| `preferred == 'crt'` and s3transfer **< 0.8.0** (the supported floor predates it) | `ConfigurationError` (rc 253, the same clean degradation) |
| `preferred == 'auto'` and `is_optimized_for_system()` and the lock is acquirable | `crt` (an s3transfer without the CRT surface silently resolves `classic`) |
| otherwise (`auto` with non-optimized / lock contention) | `classic` |

The CLI settles `auto` and places the resolved `'classic'` / `'crt'` onto
`TransferConfig.preferred_transfer_client` via `build_transfer_config`, then hands
it to the library. Streaming does not force classic (same as aws-cli - a stream
also follows `preferred`).

### Building the `TransferConfig` (`build_transfer_config`)

It passes **only the keys explicitly set** in `[s3]` to the `TransferConfig` ctor,
plus the always-passed, already-resolved `preferred_transfer_client` - and,
under CRT, one derived pin described below
(an unset tuning key stays at boto3's `UNSET_DEFAULT` sentinel = "part_size
only when `multipart_chunksize` is explicit" holds).

**The config is assembled per engine** (the same as aws-cli's factory
building the classic `TransferConfig` and the CRT client from separate sets of
keys).

- **Resolved to classic**: all keys to the ctor. Because `max_queue_size` is not
  in the boto3 ctor, it is attached afterward onto the `max_request_queue_size`
  attribute, and `max_in_memory_upload/download_chunks` is fixed at 6 (the value
  the aws-cli factory permanently installs for classic). `max_io_queue_size`
  (the download disk-writer's buffered-chunk cap) needs no such attachment: the
  library's `TransferConfig` already defaults to the 1000 aws runs at - its
  bundled s3transfer's own default, which no `[s3]` key maps to - where boto3's
  class alone would have dialed the same default down to 100.
- **Resolved to crt**: only the keys the CRT client actually reads
  (`multipart_chunksize` / `target_bandwidth` / `should_stream` /
  `disk_throughput` / `direct_io` = those that aws-cli `_create_crt_client`
  references), plus one derived pin: an explicit `multipart_chunksize` is also
  passed as the ctor's `multipart_threshold`, raised to S3's 5 MiB minimum part
  size. aws sends no per-request threshold on the CRT lane and aws-c-s3 falls
  back to `max(part size, 5 MiB)`, so that value *is* aws's effective threshold
  - while the installed s3transfer stamps the config's **resolved** threshold
  onto every CRT put, which unpinned would stamp the 8 MiB default and
  multipart a file aws single-puts. The floor is what keeps a chunksize below
  5 MiB from doing the same in the other direction (a 1 MiB chunksize leaves
  aws single-putting up to 5 MiB). With no explicit chunksize the stamped
  default equals aws-c-s3's default part size (the same effective cutoff), so
  no pin is needed; the `[s3]` `multipart_threshold` key itself stays ignored
  either way, like aws. The classic-only keys (`io_chunksize` / `max_bandwidth` /
  `multipart_threshold` / `max_concurrent_requests`) and the classic-only
  attributes (queue size, in-memory chunk cap) are
  **not passed**. This is to
  match aws-cli ignoring these on the CRT path, and to prevent the case where
  placing `io_chunksize` / `max_bandwidth` on a crt-preferred config gets rejected
  by boto3's `_validate_crt_transfer_config` and fails the run - the library
  translates the rejection to a `ValidationError`, which the engine
  materialization ahead of the run (below) surfaces at rc 252 - where aws is
  rc 0 (avoiding a charter violation; e2e:
  `test_crt_ignores_classic_only_config`).

cp / mv / sync **and rm** call `transferargs.resolve_transfer_config(ctx, s3,
paths_type=...)` - all four are `S3TransferCommand`s in aws, so all four read
`[s3]` (rm passes `paths_type="s3"`, which is not `s3s3` and therefore honors
`preferred` like any other route). The test-injected `ctx.transfer_config`
always takes precedence (preserving the existing determinization lever). The
reading of `[s3]` is placed **after** the usage (252) and source-absent (255)
validations (an invalid `[s3]` value loses to either) and **before** everything
the run itself does - for rm that includes the bucket-less enumerating
synthesis, which an invalid `[s3]` value therefore preempts (measured).

### Materializing the engine (`materialize_transfer_engine`)

aws builds the transfer manager immediately after that read
(`_get_transfer_manager`), before it decides anything about the run, so a CRT
selection pays the whole construction - the cross-process lock and
`create_s3_crt_client` - even for a `--dryrun` that transfers nothing. The
library builds its manager lazily, at the first submit, which would put those
failures inside the transfer span (rc 1) instead of ahead of it (rc 255). So
all four commands call `transferargs.materialize_transfer_engine` at aws's
slot; it delegates to `S3.materialize_crt_engine`, which builds the engine the
effective `TransferConfig` selects and drops it (a no-op when that engine is
classic; the CRT client is a process-wide singleton, so the transfer that
follows reuses whatever was built).

For rm this is construction without use: the deletes still never ride the
engine (section 6). aws's rm builds the same client and does use it, so paying
the construction is what makes the failure surfaces identical - the alternative,
synthesizing awscrt's assertion ourselves, would rest the parity on a
hand-written mirror of a third-party `assert`.

### Entering the CRT engine without credentials

aws-cli's factory builds the CRT credentials delegate from whatever the session
resolved - `session.get_credentials()` returning `None` included - and hands it
to the CRT client unexamined. The failure therefore happens at transfer time,
inside the delegate: the `NoCredentialsError` its Python callback raises cannot
cross the C stack, so the interpreter prints an `Exception ignored in:` block
and the request comes back as `AWS_AUTH_CREDENTIALS_PROVIDER_DELEGATE_FAILURE`
- one per-item `upload failed:` line, rc 1. boto3 instead refuses the CRT
engine to a client that resolved no credentials, and the library, being
boto3-faithful, falls back to classic and reports `Unable to locate
credentials`.

The split of section 1 decides where the difference is absorbed: **the library
keeps boto3's rule and the CLI opts out of it**, through
`S3(crt_allow_absent_credentials=True)` (`clientfactory.build_s3`), which
threads to `create_crt_transfer_manager(allow_absent_credentials=...)`. Two
properties make that opt-in narrow enough to live in the library:

- it relaxes exactly one branch of the compatibility check - a client with no
  credentials against a singleton whose delegate also resolves none. Every
  other identity mismatch (credentials that appeared, or disappeared, between
  two clients) and every other pin (region, endpoint, TLS `verify`,
  `Config.s3` shape) still selects classic, so a second client can never ride
  the first one's baked-in wiring;
- it is off by default, so nothing changes for a library caller.

The alternatives considered were both worse. Building the CRT manager on the
CLI side - a port of aws-cli's `_create_crt_transfer_manager` - would duplicate
the whole of `_initialize` (serializer, `verify`, part size, fio options) and
contradict section 1, where the CLI resolves the engine and the library builds
it. Carrying the flag on `TransferConfig` would reach the same code with no
plumbing, but that class is transfer *tuning*, and a compatibility-posture
boolean does not belong in it. `S3` already declares one such posture
(`wait_on_interrupt`), so it is where the second one goes.

Only uploads reach this surface - a local source or a stdin stream alike, both
measured byte-for-byte. A download or a sync fails earlier, at the botocore
listing / HeadObject call, with `fatal error: Unable to locate credentials` on
both tools.

### Where the CRT region comes from

boto3 takes the CRT client's region from the built client
(`client.meta.region_name`); aws-cli takes it from its own region chain
(`TransferManagerFactory._resolve_region`: `--region`, else the session's
config variable), which answers `None` when nothing is configured. The two
agree everywhere except that last case, where botocore has already handed the
*botocore* client S3's `aws-global` pseudo-region - so boto3's source can never
be `None`, and `awscrt.s3.S3Client`'s `assert isinstance(region, str)` is
unreachable.

Same split as the credentials posture above: the library keeps boto3's source
and the CLI declares its own, through `S3(crt_region=...)`
(`clientfactory.build_s3` passes `_resolve_region(args.region, session)`, the
identical chain every CLI client is built with), threaded to
`create_crt_transfer_manager(region=...)`. The parameter's default is the
sentinel `CLIENT_REGION`, because `None` is a *meaningful* declaration here -
"the chain resolved nothing" - and must be distinguishable from "not declared".
The declared value is also what the singleton's compatibility check pins, so
two clients declaring different regions never share one CRT client.

The failure that then arrives is awscrt's own bare `AssertionError`, which
carries no message. `main`'s dispatcher deliberately re-raises `AssertionError`
(an internal-invariant violation, and the test doubles' unexpected-call guards
depend on it), so the conversion to aws's rc-255 empty report is scoped to the
one call site where aws's behavior is known and measured -
`materialize_transfer_engine`. `cli._write_error` carries the matching
rendering: an empty detail prints `boto3-s3: [ERROR]:` with **no** trailing
space, aws's `format_error_message` branch.

### Building without the cross-process lock

aws-cli's factory acquires the cross-process CRT lock best-effort: under an
explicit `crt` preference it builds the CRT client whether or not the
acquisition succeeded, so every construction-time failure (a bad `--ca-bundle`,
the region assertion above) surfaces under contention exactly as it does
without it. boto3 instead answers a held lock with the silent classic
fallback - which would also swallow those failures and, on a dryrun, report
success where aws exits 255 (measured 2026-08-01). Same split as the two
postures above: the library keeps boto3's rule and the CLI opts out through
`S3(crt_allow_lockless=True)` (`clientfactory.build_s3`), threaded to
`create_crt_transfer_manager(allow_lockless=...)`. The opt-in is scoped to an
explicit `'crt'` preference - `auto` resolves classic under contention on aws
too - and to the lock alone: every identity pin of the compatibility check is
unchanged. The scoping survives construction: a singleton built under the
opt-in holds no lock, so a later lock-respecting request (an `auto`, or a
default-posture explicit `'crt'`) re-attempts the acquisition and rides the
singleton only once the lock is held - classic under live contention - rather
than inheriting the opt-in through the process singleton.

## 5. Charter treatment

The CRT mode is promoted, in the charter of [`overview.md`](./overview.md) section 3,
from "excluded because hard to realize" to a target that "**takes parity against
aws's CRT mode**" (the CRT transfer engine is removed from charter exception 2).
When the CRT stack is usable (awscrt present and s3transfer's CRT surface new
enough - section 6), the exit code and output of CRT mode must match those of
aws's CRT mode (enforced by the e2e CRT lane - testing.md).

## 6. Degradation and known differences (record)

- **Deletes never ride the CRT engine, but rm builds it**: under CRT
  configuration rm constructs the CRT client exactly as aws does - the same
  cross-process lock, the same `create_s3_crt_client` call, so its
  construction-time failures match - and then deletes through its established
  non-CRT routes regardless: single rm keeps its blind `DeleteObject`,
  recursive rm and S3-side
  `sync --delete` keep `S3Deleter`'s batched `DeleteObjects`, and local-side
  sync-delete keeps `os.remove`; none route through `CRTTransferManager.delete`.
  These are the accepted deletion paths documented in deleter.md section 4;
  the CRT e2e lane pins the charter-observable rc, output, and end states for
  single/recursive rm and both sync-delete directions. One consequence shows in
  the credentials-absent corner of section 4: with CRT configured, aws's `rm`
  fails inside the CRT credentials delegate (`delete failed: ...
  AWS_AUTH_CREDENTIALS_PROVIDER_DELEGATE_FAILURE`, unraisable block included)
  while ours fails on its `DeleteObject` (`delete failed: ... Unable to locate
  credentials`). Same rc 1; the wording follows from the routing decision, not
  from how credentials are handled. A user can observe it, so it is recorded
  in [`aws-differences.md`](../docs/cli/aws-differences.md) section 2 too
  (testing.md section 9's recording rule); the mechanism stays here.
- **CRT configured x no resolvable region**: aligned, no longer a divergence.
  aws's factory hands `create_s3_crt_client` whatever its region chain
  answered, unvalidated, so an unresolved region reaches
  `awscrt.s3.S3Client`'s `assert isinstance(region, str)`; the bare
  `AssertionError` reaches aws's general handler and prints an **empty** report
  (`aws: [ERROR]:`) at rc 255. It fires for every command whose architecture
  builds a transfer manager - `cp` / `mv` / `sync` **and** `rm` - independently
  of whether credentials exist, and `--dryrun` / `--quiet` do not save it. This
  CLI now reaches the same assertion: it declares aws's chain-resolved region
  (section 4, "Where the CRT region comes from") instead of the built client's
  `aws-global`, builds the engine at aws's slot (section 4, "Materializing the
  engine") so `rm` and the dryrun routes get there too, converts the bare
  `AssertionError` at that one call site, and renders the empty report with
  aws's byte shape.
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
- **Explicit crt x lock contention**: aws forces the CRT construction; boto3 -
  and the library default - silently falls back to classic. When the
  construction succeeds the two are indistinguishable in output and rc
  (proven; only throughput differs), but a construction-time failure surfaces
  only on aws's path - the fallback would run classic and, on a dryrun, exit 0
  where aws exits 255 (measured 2026-08-01, a held lock plus a bad
  `--ca-bundle`). The CLI therefore opts into aws's posture with
  `S3(crt_allow_lockless=True)` (section 4, "Building without the
  cross-process lock"); library callers keep boto3's fallback by default.
- **Ctrl-C and fatals on the CRT lane**: pip s3transfer's CRT manager
  discards a drain-time `KeyboardInterrupt` - its coordinator converts the
  interrupt into `cancel()`, the cancellation error replaces it, and its
  shutdown drops that too - where the classic manager re-raises it. aws is
  built to survive that loss: its per-item subscribers classify every
  non-`CancelledError` outcome as a failure, so a CRT run cut short - a
  drain-time Ctrl-C, a Ctrl-C in the submission window, a fatal folding the
  manager mid-run - prints `upload failed: ... AWS_ERROR_S3_CANCELED: Request
  successfully cancelled` per in-flight item at rc 1 (all three measured
  2026-08-01; aws's own classic lane prints `cancelled: ctrl-c received` or
  one `fatal error:` line instead - the two aws lanes differ). This library
  takes the same rule at the same place: every CRT cancellation classifies
  `FAILED` unless this run's `CancelToken` ordered it
  (`Transferrer._cancel_initiated` - the one revocation aws has no
  counterpart for, kept `CANCELLED`; "ordered" means the token's own
  `CancelledError` forced the fold or its escalation issued the cancel, so a
  fatal or interrupt folding the manager stays `FAILED` even with the token
  already cancelled - design/opresult.md), so the CLI streams aws's per-item
  lines and the classic lane is untouched. Two residuals: a library caller on the
  swallowed-interrupt window gets `BatchError`, not the `KeyboardInterrupt`
  ([`opresult.md`](./opresult.md)); and a submission-window Ctrl-C ends with
  the CLI's `cancelled: ctrl-c received` line where aws ends with an empty
  `fatal error:` line (its recorder renders the `KeyboardInterrupt`, whose
  `str()` is empty, as an error result) - the per-item lines and rc 1 match,
  and the closing line stays ours deliberately (decided 2026-08-01; recorded
  in docs/cli/aws-differences.md section 2).
- **fio_options**: unavailable on any pip s3transfer
  ([`compatibility.md`](../docs/compatibility.md)). `_add_fio_options` probes
  `create_s3_crt_client`'s signature rather than a version, so the keys start
  flowing the moment the parameter ships upstream.
- **TransferConfig on old s3transfer**: below s3transfer 0.16.0 the config
  cannot reach `CRTTransferManager` and is dropped with boto3's own warning
  (`configured values will be ignored`), boto3-faithfully; the CRT client itself
  still gets `part_size` / `target_throughput`, passed to
  `create_s3_crt_client` directly ([`compatibility.md`](../docs/compatibility.md) for
  what the caller sees). The gate is boto3's `TRANSFER_CONFIG_SUPPORTS_CRT` =
  `hasattr(TransferConfig, "UNSET_DEFAULT")`; drop the shim once the floor is
  past 0.16.
- **Empty / whitespace-only `verify`**: pip s3transfer >= 0.19.2 rejects an
  empty or whitespace-only `verify` string outright with `InvalidConfigError`,
  where aws's bundled fork reads the empty string as falsy and turns TLS
  verification off, letting the transfer proceed - measured 2026-08-02, a dead
  endpoint and `--ca-bundle ""`: aws 2.36.1 runs on to
  `AWS_IO_SOCKET_CONNECTION_REFUSED: socket connection refused` at rc 1, and
  so does this library once `_derive_verify` normalizes the empty string to
  `False`, uniformly across pip s3transfer versions (the classic lane needs no
  such normalization - botocore gives the empty string the same falsy read on
  both sides). A whitespace-only value stays through and still fails, since
  aws attempts it as a CA-bundle path too, but the shape now depends on the
  installed s3transfer: on 0.19.0 this library's `[Errno 2] No such file or
  directory: '   '` at rc 255 matches aws's own `[Errno 2] No such file or
  directory: '   '` at rc 255 exactly; on 0.19.2 the upfront check fires first
  and this library's `Invalid CA bundle: ...` at rc 255 diverges from aws's
  `[Errno 2] No such file or directory: '   '` at rc 255 - an engine-rooted
  divergence under `overview.md` section 3's third exception (measured
  2026-08-02).
- **Process-pinned singleton**: the region / credentials / endpoint of the first
  client to reach the CRT path monopolize the in-process CRT, and an incompatible
  second connection falls back to classic (identical behavior to boto3).
- **Download checksum validation**: both aws's CRT mode and ours pass
  `S3ChecksumConfig(validate_response=True)` on every GET, so response
  validation is the CRT client's own, identical by construction. The classic
  engine's ranged-download full-object combine validation (a feature of
  aws-cli's bundled s3transfer fork that pip s3transfer lacks) is the known
  divergence recorded in transfer.md section 10.
- **Cannot be verified under moto**: because CRT bypasses botocore's HTTP layer,
  actual verification is only on the e2e (MinIO) lane.
