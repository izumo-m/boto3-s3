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
both in the library**.

```
config resolution = aws-cli-faithful (transferconfig.py)
  S3.resolve_transfer_config reads [s3] preferred_transfer_client (the
  caller's TransferConfig wins) and settles it to 'classic' / 'crt' via
  the aws-cli TransferManagerFactory decision tree. Every cp / mv / sync /
  rm runs it, and the CLI calls the same method.

engine seam = boto3-faithful (Transferrer, crtsupport.py)
  A config that arrives unresolved - a plain boto3 TransferConfig, or one
  handed to Transferrer directly - has its preferred_transfer_client
  ('auto' | 'classic' | 'crt') read with the same semantics as boto3.
```

- **The library ports aws-cli's decision tree** (section 4,
  `resolve_transfer_client`). It applies the unconditional classic for
  `s3s3` and the `is_optimized_for_system` + process-lock decision for
  `auto`, then places the settled value on
  `TransferConfig.preferred_transfer_client`. Settling `auto` rather than
  passing it through keeps the engine seam's boto3-style `auto` decision from
  running a second time, and makes the resolution own the degradation when
  awscrt is absent (section 6). The CLI has no decision of its own.
- **The engine seam interprets an unresolved config with boto3's rules**
  (section 3, boto3's `create_transfer_manager`), so a plain boto3
  `TransferConfig(preferred_transfer_client="crt")` selects CRT with the same
  feel as boto3.

**The process lock name is `'boto3-s3'` for both the library and the CLI.** The
s3transfer lock is an arbitration to keep multiple processes of the same
application from standing up a CRT client simultaneously (boto3 uses `'boto3'`,
aws-cli uses `'aws-cli'`); it is not observable in a single process's output or
return code, so it has no bearing on compatibility. Namespacing it by one's own
product name is the convention of both. s3transfer stores the lock in a module
global created once on the first `acquire_crt_s3_process_lock` and returns that
stored object on every later call (the `name` argument is consulted only at
first creation, not re-checked). So the lock the resolution acquires for
`auto` comes back as the same object from the engine's re-acquisition call
regardless of the name passed, and the two-stage acquisition reconciles
naturally.

## 2. Components

| module | role |
|---|---|
| `transferconfig.py` | `TransferConfig` = a subclass of boto3's `TransferConfig`. Adds the CRT tuning field that boto3 lacks (`target_bandwidth`) plus the classic multipart-copy's `annotation_temp_dir` (transfer.md section 4), and records which settings the caller gave. A plain boto3 config is also accepted (`crtsupport.py` reads `target_bandwidth` via `getattr` with a default of None, so a plain boto3 config works too). Also the port of aws-cli's own `transferconfig.py`: `RuntimeConfig`, reading `[s3]`, the decision tree (`resolve_transfer_client`) and the effective config (`resolve_transfer_config`) - section 4. |
| `crtsupport.py` | A faithful port of boto3's `boto3/crt.py` plus improvements. `should_use_crt` (whether to attempt CRT given `preferred`), `create_crt_transfer_manager` (the process-singleton CRT client + serializer, lock, compatibility check), `create_crt_request_sender` (the same engine as a sender of single requests, for the deletes - section 6), `is_optimized_for_system` / `acquire_process_lock` (the building blocks for `resolve_transfer_client`'s decision tree). It does not pull in awscrt / s3transfer.crt at import time. |
| `crtrequest.py` | `CrtRequestSender`: one botocore-serialized S3 request through the engine's CRT client, for the operation `CRTTransferManager` has no route for (the deleter's `DeleteObjects`) and, so one route reads every delete's answer, for the single-key `DeleteObject`s too (section 6). Also free of awscrt at import time. |
| `Transferrer._get_manager` in `transfer.py` | The engine seam. COPY is unconditionally classic, and `capture_response=True` forces classic before any config is read (transfer.md section 2). Otherwise it reads `preferred_transfer_client`, attempts CRT (classic fallback if None), and the rest takes the conventional classic path. |

## 3. The library layer (boto3-faithful)

A library `TransferConfig` reaches the engine already resolved (section 4,
`resolve_transfer_config`): its `preferred_transfer_client` is `'classic'` or
`'crt'`. A plain boto3 config, used as it is, and a config the caller hands a
`Transferrer` directly are resolved by `Transferrer._get_manager`
(transfer.md section 2) - `preferred_transfer_client` defaulting to `'auto'`,
config=None read as `'auto'` - with the same rules as boto3.

- **COPY (s3->s3) is unconditionally classic** - specified with the rest of the
  engine resolution in [`transfer.md`](./transfer.md) section 2. The boto3
  fidelity behind it: `CRTTransferManager` has only upload / download / delete,
  and boto3 drops to `preferred_transfer_client='classic'` for a copy in
  `inject.py` ("copy is not supported in the CRT").
- **`should_use_crt`** (a port of boto3's `_should_use_crt`): an explicit
  `'crt'` combined with a missing or too-old awscrt (below 0.19.18) raises a
  `MissingDependencyException`, as boto3 does; an explicit `'crt'` on an
  s3transfer below 0.8.0 (the supported floor predates it) also
  raises `MissingDependencyException` - a branch boto3 does not have
  (`'auto'` degrades to classic there instead); CRT is attempted
  when `(is_optimized and 'auto') or 'crt'`.
- **`create_crt_transfer_manager`** (a port of boto3's `get_crt_s3_client` +
  singleton): lazily creates the process-singleton CRT client + serializer. If
  the lock is held by another process, it returns `None` = classic fallback. A
  later client that falls outside the compatibility check (same region + same
  frozen credentials, **+ our extensions: same endpoint, same signing mode**
  [signed/unsigned]**, same TLS `verify`, same URL-shaping `Config` (the `s3`
  dict plus the top-level fips / dualstack flags)** - the CRT client bakes in
  the first client's `verify` and the shared serializer its
  `Config`, so a differing later client must not silently ride those) also
  drops to classic (the same shape as boto3's region/credentials-mismatch
  fallback). The endpoint and `Config` pins compare what botocore resolves,
  not the spelling: a missing `s3` dict, an empty one and one naming only
  defaults (`addressing_style='auto'`) are one shape, a flag left `None`
  reads as `False`, and an endpoint's scheme and host case and trailing
  slash are ignored as botocore ignores them when it joins the URL - taken
  verbatim, `Config()` against `Config(s3={})` or a trailing `/` sent the
  second client to classic for nothing (measured 2026-10-06), with the CRT
  engine's throughput silently lost. The credentials half of that check matches by object before it
  compares by value: a client whose credentials object is the one the
  singleton's delegate wraps - every client of one session shares it - has
  the same identity by construction and is admitted without resolving it,
  which keeps a refreshable credential unresolved until the delegate needs
  it (section 4, "Entering the CRT engine without credentials"); boto3's
  frozen comparison is kept for a client holding another object; a signing
  client that resolved no credentials at all is refused, boto3's rule. On an
  explicit `'crt'` it also
  ports boto3's `_validate_crt_transfer_config` (which rejects an explicit
  setting of a CRT-unsupported option). Whatever the construction raises is
  classified at the seams that call it (`transfer.crt_engine_errors`, used
  by the transfer engine and by `S3.materialize_crt_engine`): that validation
  error is a `ValidationError`, a CA bundle awscrt cannot read or parse is an
  `InvalidConfigError`, and a credential resolution's failure keeps its
  botocore category (section 6).
- **Deriving the connection parameters (a documented improvement over boto3)**:
  because the library uses a connection model in which the caller holds the
  client (the S3 connection model in [`overview.md`](./overview.md)), it derives
  the CRT wiring from the client.
  - region = `client.meta.region_name`
  - endpoint = the caller's explicit endpoint when one is threaded in, else the
    host heuristic. The CLI passes its `--endpoint-url` down (`S3(endpoint_url=)`
    -> `Transferrer(crt=)`'s `CrtPostures.endpoint` -> `create_crt_transfer_manager(endpoint=)`),
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
    resolved `client.meta.endpoint_url`. A `None` is resolved again by the
    serializer under its own session (below), so a client whose AWS endpoint
    came from another session than the `S3`'s would be addressed by whatever
    `endpoint_url` / `[services]` the `S3`'s session configures - a library
    corner only (the CLI has one session), not measured, since probes cannot
    lift the endpoint variables.
  - use_ssl = the endpoint's scheme is other than `http`
  - verify = the client's TLS verification setting. Via the private botocore
    attribute `client._endpoint.http_session._verify` (a private dependency at
    the same level as `client._get_credentials()`). `False` and a CA-bundle
    path ride through; botocore's own default (`True`, or nothing set at all)
    maps to `None`, which is boto3-faithful (plain boto3 passes no `verify` to
    `create_s3_crt_client` either) but means the **platform** trust store,
    not the certifi bundle the classic engine uses. A caller's
    `CrtClientOptions.verify` replaces the derivation. `S3.from_aws_cli` (the
    CLI's assembly) declares one the way aws-cli's CRT factory resolves it
    (`awsclisession.crt_verify`:
    `--no-verify-ssl` > `--ca-bundle` > `ca_bundle` config variable >
    botocore's bundled `cacert.pem`, never `REQUESTS_CA_BUNDLE`), apart from
    the botocore clients' own resolution, so its two engines trust what
    aws-cli's do
  - credentials = boto3's rule, from the client: no provider if
    `signature_version is UNSIGNED` (and then no credentials resolved at
    all), otherwise `BotocoreCRTCredentialsWrapper(client._get_credentials())`.
    No new singleton field encodes the mode: `_CrtS3Client.cred_wrapper`'s
    presence already does, so the compatibility pin rejects a later client
    signing the other way (classic fallback, never a wrong-mode transfer)
  - serializer session = the caller's session (`S3(session=)` ->
    `Transferrer(crt=)`'s `CrtPostures.session` -> `create_crt_transfer_manager(session=)`), falling
    back to boto3's default session when one exists, then to a fresh botocore
    session. Matches aws-cli, which hands its live CLI session to
    `BotocoreCRTRequestSerializer`; a fresh session re-parses the S3 service
    model and endpoint data on every process (~40 ms measured), which was the
    dominant fixed cost of the CRT lane versus aws-cli in the E2E benchmark
  - config = the caller's `client.meta.config`, threaded into the serializer
    with the region and endpoint (the serializer merges
    `signature_version=UNSIGNED` on top). This keeps a `None` endpoint's
    per-request re-resolution under the caller's configuration rather than
    stock botocore's defaults: addressing-style and accelerate/dualstack
    settings carry over that way, while us-east-1 stays on the regional
    endpoint through the session instead - the serializer builds its own client
    from the session it is handed, so the CLI's session-level
    `us_east_1_regional_endpoint` pin (cli.md section 4 item 3) reaches it as it
    reaches every other client (one Host across engines). Three parts of the
    Config are rewritten on the way (`_serializer_config`): the preliminary
    User-Agent botocore stores in it goes back to what the caller supplied
    (handed on, it read as a caller-supplied `user_agent` and froze the
    header in botocore's legacy form, dropping `app/<id>`); the app id is
    pinned to the client's resolved one, empty for none; and every `s3`
    key and fips / dualstack flag the client left out is pinned to
    botocore's default - otherwise the serializer's session, which is not the
    client's when the client came from another session, filled them from its
    own profile (its `addressing_style = virtual` put the CRT's requests on a
    virtual host where classic's went path-style, its `sdk_ua_app_id` named
    a client that has none; measured). What botocore takes from the session
    instead of the Config - the header's leading product name, a
    session-level `user_agent_extra` - stays the serializer session's, and a
    later client sharing the engine sends its CRT requests under the name of
    the client the engine was built for. The compatibility check below does
    not pin the User-Agent: a name is no reason to lose the engine (boto3's
    own check pins only region and credentials)

  - part_size = that value **only when `multipart_chunksize` is explicitly set**;
    `None` if unset (CRT dynamic). Determined via boto3's `UNSET_DEFAULT`
    sentinel (a faithful version of the same rule as the aws-cli factory)
  - target_throughput = `getattr(config, "target_bandwidth", None)`
  - the fio family (should_stream / disk_throughput / direct_io) is not
    passed: no released pip s3transfer's `create_s3_crt_client` accepts
    `fio_options` (still absent at 0.19.0; the fork bundled with aws-cli does)

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

## 4. The aws-cli-faithful layer (`[s3]` in the library, the rest in the CLI)

### `[s3]` runtime config (the library's `transferconfig.py`)

The `[s3]` handling below lives in the library, which applies it to every
`cp` / `mv` / `sync` / `rm` run (`S3.resolve_transfer_config`); the CLI only
calls the same method ahead of its materialization slot.

A port of `RuntimeConfig` from aws-cli `transferconfig.py`, under the same
module name. It reads the profile's `[s3]` through `S3.aws_config()`
(`load_scoped_s3_config` pulls the known keys; `AWS_CONFIG_FILE` / `--profile`
/ the nested `s3 =` INI are honored through that reader), converts sizes (`8MB`), rates (`100MB/s` /
`800Kb/s`), and bools, resolves the `default` -> `classic` alias, and validates
invalid values. An invalid value raises the library's `InvalidConfigError` -
aws-cli's class of the same name reaches the general handler with rc 255; ours
maps to 255 too (exceptions.md section 2).

### The decision tree (`resolve_transfer_client`)

A port of aws-cli `TransferManagerFactory._compute_transfer_client_type`.

| condition | result |
|---|---|
| an S3-to-S3 copy (`copy=True`) | `classic` (unconditional; CRT has no copy) |
| `preferred == 'classic'` | `classic` |
| `preferred == 'crt'` and awscrt present (>= 0.19.18) and s3transfer >= 0.8.0 | `crt` (acquires the lock but ignores the result = same shape as aws-cli) |
| `preferred == 'crt'` and awscrt **absent or too old** | `ConfigurationError` (rc 253, section 6) |
| `preferred == 'crt'` and s3transfer **< 0.8.0** (the supported floor predates it) | `ConfigurationError` (rc 253, the same clean degradation) |
| `preferred == 'auto'` and `is_optimized_for_system()` and the lock is acquirable | `crt` (an s3transfer without the CRT surface silently resolves `classic`) |
| otherwise (`auto` with non-optimized / lock contention) | `classic` |

The preference is the caller's when its `TransferConfig` gave one, else
`[s3]`'s. `resolve_transfer_config` settles `auto` and places the resolved
`'classic'` / `'crt'` onto the effective config's `preferred_transfer_client`.
Streaming does not force classic (same as aws-cli - a stream also follows
`preferred`).

### Building the effective `TransferConfig` (`resolve_transfer_config`)

It passes the settings the caller gave, and for the rest **only the keys
explicitly set** in `[s3]`, to the `TransferConfig` ctor, plus the
always-passed, already-resolved `preferred_transfer_client` - and, under CRT,
one derived pin described below (an unset tuning key stays at boto3's
`UNSET_DEFAULT` sentinel = "part_size only when `multipart_chunksize` is
explicit" holds). The result is a copy of the caller's config, so whatever the
caller set on it - in the constructor or as an attribute afterwards - carries
over and counts as given. Under a CRT engine the caller did not ask for (`auto`,
or `[s3]`'s choice) the caller's classic-only settings stay but are not held
to boto3's explicit-`crt` validation (`_crt_requested`): the CRT engine ignores
them, as boto3's `auto` and aws-cli's CRT manager do.

**The config is assembled per engine** (the same as aws-cli's factory
building the classic `TransferConfig` and the CRT client from separate sets of
keys).

- **Resolved to classic**: all keys to the ctor. Because `max_queue_size` is not
  in the boto3 ctor, it is attached afterward onto the `max_request_queue_size`
  attribute, and `max_in_memory_upload/download_chunks` is fixed at 6 (the value
  the aws-cli factory permanently installs for classic) unless the caller set
  them. `max_io_queue_size`
  (the download disk-writer's buffered-chunk cap) needs no such attachment: the
  library's `TransferConfig` already defaults to the 1000 aws-cli runs at - its
  bundled s3transfer's own default, which no `[s3]` key maps to - where boto3's
  class alone would have dialed the same default down to 100.
- **Resolved to crt**: only the keys the CRT client actually reads
  (`multipart_chunksize` / `target_bandwidth`; aws-cli's `_create_crt_client`
  also reads the fio family, which has no field here - below), plus one
  derived pin: an explicit `[s3]` `multipart_chunksize`
  the caller did not override is also
  passed as the ctor's `multipart_threshold`, raised to S3's 5 MiB minimum part
  size. aws-cli sends no per-request threshold on the CRT lane and aws-c-s3
  falls back to `max(part size, 5 MiB)`, so that value *is* aws-cli's effective
  threshold - while the installed s3transfer stamps the config's **resolved**
  threshold onto every CRT put, which unpinned would stamp the 8 MiB default and
  multipart a file aws-cli single-puts. The floor is what keeps a chunksize
  below 5 MiB from doing the same in the other direction (a 1 MiB chunksize
  leaves aws-cli single-putting up to 5 MiB). With no explicit chunksize the
  stamped default equals aws-c-s3's default part size (the same effective
  cutoff), so no pin is needed. One nuance behind that equality: aws-cli passes
  `part_size=None` and lets aws-c-s3 resolve its *dynamic* default, whose
  fallback constant is the same 8 MiB - measured 2026-08-04 (MinIO, explicit
  `crt`, no chunksize): both tools single-put an exactly-8-MiB file and
  multipart 9 MiB, so the cutoffs coincide here and aws-c-s3's comparison is
  strictly-greater. A host whose dynamic default resolved differently would part
  the two cutoffs; that residual comes only from the bundled-vs-pip engine, an
  SDK version difference (overview.md section 3), and is not matched. The `[s3]`
  `multipart_threshold` key itself stays ignored either way, like aws-cli. The
  classic-only keys (`io_chunksize` / `max_bandwidth` / `multipart_threshold` /
  `max_concurrent_requests`) and the classic-only attributes (queue size,
  in-memory chunk cap) are **not passed**. This is to match aws-cli ignoring
  these on the CRT path, and to prevent the case where placing `io_chunksize` /
  `max_bandwidth` on a crt-preferred config gets rejected by boto3's
  `_validate_crt_transfer_config` and fails the run - the library translates the
  rejection to a `ValidationError`, which the engine materialization ahead of
  the run (below) surfaces at rc 252 - where `aws` is rc 0 (e2e:
  `test_crt_ignores_classic_only_config`).

The library's cp / mv / sync **and rm** resolve the config at the start of
each run - all four are `S3TransferCommand`s in aws-cli, so all four read `[s3]`
(rm is never a copy, so it honors `preferred` like any other route). The CLI
calls the same method first (`transferargs.resolve_transfer_config`), so its
materialization below gets the same config; a test-injected
`ctx.transfer_config` supplies the settings it sets (the determinization
lever). In the CLI the reading of `[s3]` is placed **before** everything the
run itself does, so an invalid `[s3]` value stops every rm form (measured); a
path with no bucket is refused earlier still, before the client is built.

### Materializing the engine (`build_transfer_engine`)

aws-cli builds the transfer manager immediately after that read
(`_get_transfer_manager`), before it decides anything about the run, so a CRT
selection pays the whole construction - the cross-process lock and
`create_s3_crt_client` - even for a `--dryrun` that transfers nothing. The
library builds its manager only once a live run starts - `prepare()` just ahead
of enumeration, or the first submit on a stream route - and never for a
dryrun, which would put those failures inside the operation (rc 1), or skip
them, instead of ahead of it (rc 255). So all four commands call
`transferargs.build_transfer_engine` at aws-cli's slot; after the `[s3]` read it
delegates to `S3.materialize_crt_engine`, which builds the engine the
effective `TransferConfig` selects and drops it (a no-op when that engine is
classic; the CRT client is a process-wide singleton, so the transfer that
follows reuses whatever was built).

For rm this construction comes ahead of everything the run does: the deletes
ride the engine (section 6), but the library builds it later - the listing
routes (recursive, and the keyless folder-marker sweep) with their deleter,
just before the listing; the single-key route just before its request; a
dryrun never. Paying it at aws-cli's slot puts a construction failure (a CA
bundle the CRT cannot read) ahead of the run, where aws-cli's lands, and it is
where the CLI stops a CRT run that has no region ("Where the CRT region comes
from", below).

### Entering the CRT engine without credentials

aws-cli's factory builds the CRT credentials delegate from whatever the session
resolved - `session.get_credentials()` returning `None` included - and hands it
to the CRT client unexamined, so a credential-less upload fails inside the
delegate: one per-item `upload failed:` line, rc 1, after an `Exception ignored
in:` traceback block the interpreter prints because the callback's
`NoCredentialsError` cannot cross the C stack. boto3 instead refuses the CRT
engine to a client that resolved no credentials, and the library and the CLI
both keep that rule: the run falls back to classic and fails the same item at
rc 1 with botocore's `Unable to locate credentials`, and no traceback. The
outcome is aws-cli's; only the report differs (overview.md section 3).

The signing posture follows the client on both engines. aws-cli's CRT factory
attaches a credentials provider on its `sign_request` parameter alone, while
the per-client `Config(signature_version='s3v4')` its `ClientFactory` adds for
`--sse aws:kms` reaches its botocore client only, so aws-cli itself is
inconsistent across engines for `--no-sign-request --sse aws:kms` (anonymous on
CRT, signed on classic). That pin is not copied (cli.md section 4, item 2), so
the botocore client is UNSIGNED under `--no-sign-request` whatever `--sse`
says, and the CRT client, reading it, is anonymous too - aws-cli's CRT answer.

The alternatives considered were both worse. Building the CRT manager on the
CLI side - a port of aws-cli's `_create_crt_transfer_manager` - would duplicate
the whole of `_initialize` (serializer, `verify`, part size, fio options) and
contradict section 1, where the library both resolves the engine and builds
it. Carrying the posture on `TransferConfig` would reach the same code with no
plumbing, but that class is transfer *tuning*, and a compatibility posture
does not belong in it. `S3` already declares one such posture
(`reusable_after_interrupt`), so it is where the CRT postures go.

Only uploads and a single-key `rm` reach this surface - a local source or a
stdin stream alike, and the rm's one `DeleteObject`. A download, a sync or a recursive rm fails earlier, at the
botocore listing / HeadObject call, with `fatal error: Unable to locate
credentials` on both tools.

A credential that resolves *lazily* - an assumed role, an SSO token, a web
identity - is the same surface one step later: aws-cli's factory wraps the
session's credentials object unresolved, so a refresh that fails (the token
expired, STS unreachable or refusing) lands inside the delegate, per item
(`upload failed: ...` / `delete failed: ...
AWS_AUTH_CREDENTIALS_PROVIDER_DELEGATE_FAILURE`, rc 1),
a download or sync fails at its botocore call (`fatal error:`, rc 1), and a
`--dryrun` never resolves it at all (rc 0). boto3's compatibility check would
resolve it ahead of the transfer - `get_frozen_credentials()` on every
request - and the refresh failure would then fail the whole run before any
item, the dryrun included (measured 2026-10-06 on the pinned `aws`: rc 1 / 0
there against rc 254 / 255 here before the identity rule). The library
therefore matches the credentials half by object first: the singleton
records the credentials object its delegate wraps, and a client holding that
same object is admitted without resolving anything, which is every client of
one session, the CLI's included. A client holding another object still gets
boto3's value comparison, and a resolution that fails there surfaces through
the seam's classification (section 6) rather than as the bare botocore error.

### Where the CRT region comes from

The CRT client's region is the built client's (`client.meta.region_name`),
boto3's source. aws-cli takes it from its own region chain
(`TransferManagerFactory._resolve_region`: `--region`, else the session's
config variable). The two agree except in two cases:

- **Nothing configured.** aws-cli's chain answers `None`, which awscrt refuses
  (`assert isinstance(region, str)`), so aws-cli's CRT run stops before it
  starts - every command whose architecture builds a transfer manager, `rm` and
  `--dryrun` included. botocore instead hands the client a concrete region -
  `us-east-1` in stock botocore's default (legacy) us-east-1 endpoint mode, S3's
  `aws-global` pseudo-region in the regional mode the CLI pins (cli.md section 4
  item 3) - which the CRT would sign every request for. The CLI keeps aws-cli's
  outcome: its `S3` (`clientfactory.build_s3`) holds the chain's answer, and
  when that is `None` and the effective config selects the CRT engine, its
  `materialize_crt_engine` raises botocore's missing-region error as a
  `ConfigurationError` before anything is built - rc 253, the code for a region
  that cannot be resolved (aws-cli's 255 is its general handler's for the bare
  assertion). The classic engine needs no region on either tool. A library
  caller has no chain of its own and keeps boto3's behavior.
- **A FIPS pseudo-region** (`fips-us-east-1`, `us-east-1-fips`). botocore
  normalizes it on the client - the plain region with the FIPS endpoint - so
  the CRT signs for the plain region; aws-cli hands its CRT client the raw
  pseudo-region and signs for that, which S3 is expected to refuse per item
  (unmeasured). Recorded in
  [`aws-differences.md`](../docs/cli/aws-differences.md).

### Building without the cross-process lock

aws-cli documents an explicit `crt` preference as the way to make
concurrently running processes use the CRT client (its `s3` config topic,
`preferred_transfer_client`), and its factory builds the CRT client whether
or not it got the cross-process lock (a lock it does get still marks the slot
for `auto` runs). Clients built side by side only compete for throughput:
output and exit codes are those of a run that holds the lock (measured
2026-08-01). boto3 instead answers a held lock with the silent classic
fallback. Same split as the signing posture above: the library keeps boto3's rule and the CLI opts out through
`S3(crt=CrtClientOptions(allow_lockless=True))` (`clientfactory.build_s3`),
threaded to `create_crt_transfer_manager(crt=...)`. The opt-in is scoped to an
explicit `'crt'` preference - `auto` resolves classic under contention on `aws`
too - and to the lock alone: every identity pin of the compatibility check is
unchanged. The scoping survives construction: a singleton built under the
opt-in holds no lock, so a later lock-respecting request (an `auto`, or a
default-posture explicit `'crt'`) re-attempts the acquisition and rides the
singleton only once the lock is held - classic under live contention - rather
than inheriting the opt-in through the process singleton.

## 5. Compatibility treatment

Functional compatibility ([`overview.md`](./overview.md) section 3) holds the
CRT mode to **aws-cli's CRT mode**, on the same terms as the classic engine is
held to aws-cli's classic mode; the CRT transfer engine is not an exception.
When the CRT stack is usable (awscrt present and s3transfer's CRT surface new
enough - section 6), the normal path of CRT mode matches aws-cli's CRT mode, and
in an error path what decides the functional outcome matches while the
error's details need not (checked by the e2e CRT lane - testing.md).

## 6. Degradation and known differences (record)

- **Deletes ride the CRT engine, batched**: under CRT configuration aws-cli
  sends each `rm` / local-side `sync --delete` deletion through its CRT transfer
  manager, one `DeleteObject` per key (an S3-to-S3 sync is classic on both
  tools). `S3Deleter` keeps its batches and sends them on the same CRT client:
  `CRTTransferManager` has no `DeleteObjects` route, so `crtrequest`'s
  `CrtRequestSender` provides one the way s3transfer builds its own - an
  unsigned, non-sending botocore client serializes the request (keeping the
  checksum `DeleteObjects` requires, which s3transfer strips from its own
  bodiless requests), the CRT client signs and sends it as a `DEFAULT` meta
  request under the operation's name, and botocore's parser reads the answer
  (an HTTP error answer as the botocore error for its code, as s3transfer's
  `translate_crt_exception` does). The single-key rm and the deleter's per-key
  requests (keys XML cannot carry, re-sends) ride it as `DeleteObject`. The
  engine is resolved by `create_crt_request_sender`, which goes through
  `create_crt_transfer_manager` - same singleton, lock, pins, postures and
  construction failures - and serves a sender built once per engine from the
  serializer's own session and client arguments. What this buys is the CRT's
  retry policy (a request that dies without an answer is sent six times
  whatever `max_attempts` says, measured), its failure text (awscrt's own
  errors), and the `[s3]` tuning keys' reach. What batching leaves is one set of attempts per batch where aws-cli
  has one per key, so a batch request that fails as a whole is re-sent key by
  key on the same client (deleter.md section 3) - which can give a key more
  attempts than aws-cli does, never fewer - plus the batch differences
  deleter.md section 4 records for both engines.
- **A plain-HTTP endpoint named only by the environment**: aws-cli decides its
  CRT client's `use_ssl` from `--endpoint-url` alone, so an `http://` endpoint
  supplied through `AWS_ENDPOINT_URL_S3` is dialed over TLS and the transfer
  dies with `AWS_IO_SOCKET_CLOSED`, where `_derive_endpoint` reads the scheme
  off the endpoint the client actually resolved and the same run transfers.
  The user sees a different exit code and a different S3 state - a functional
  difference - so this one is recorded in
  [`aws-differences.md`](../docs/cli/aws-differences.md) section 2 too
  (overview.md section 3); the measurement and
  the mechanism stay in section 3.
- **CRT configured x no resolvable region**: aligned in outcome - the run
  stops before it starts, `rm` and `--dryrun` included - with the
  missing-region report aws-cli's NoRegion handler writes, at rc 253, where
  aws-cli prints an empty report at rc 255
  (section 4, "Where the CRT region comes from").
- **awscrt absent x explicit crt**: an area that cannot arise because aws-cli
  bundles awscrt. Our awscrt is an opt-in extra (`boto3-s3-cli[crt]` ->
  `boto3-s3[crt]` -> `boto3[crt]`, transfer.md section 9).
  - `resolve_transfer_client` stops it with a `ConfigurationError` (rc 253 in
    the CLI), for the library and the CLI alike: a feature that depends on the
    optional awscrt is unavailable without it (overview.md section 2).
  - A plain boto3 config, used as it is, is resolved with boto3's rule, which
    passes boto3's `MissingDependencyException` through, as boto3 does. This
    is a deliberate exception to the backend-exception translation at the
    library boundary (exceptions.md section 1) - to reproduce boto3's
    behavior for a boto3 config.
- **Construction failures are classified**: the engine is
  built by third-party code, and what it raises reaches the library's caller
  through `transfer.crt_engine_errors` - at the transfer engine's seam and at
  `S3.materialize_crt_engine` alike. A CA bundle awscrt cannot read
  (`FileNotFoundError` / `IsADirectoryError` / `PermissionError`, from its
  own `open` of the path) or cannot parse (its `RuntimeError`,
  `AWS_ERROR_INVALID_ARGUMENT`) is an `InvalidConfigError`; boto3's
  `InvalidCrtTransferConfigError` a `ValidationError`; the botocore family a
  credential resolution raises keeps its category (an `AccessDeniedError`
  for a refused `AssumeRole`, a `TransportError` for an unreachable STS).
  The CLI's rc 255 for an unusable bundle is unchanged: `InvalidConfigError`
  maps to aws-cli's general-handler code, and the text is the exception's own
  (measured 16 ways, 2026-10-06: `aws: [ERROR]: 34 (AWS_ERROR_INVALID_ARGUMENT)
  ...` on both).
  `InvalidCrtTransferConfigError` itself exists from boto3 1.42.0 only, so the
  seam looks it up rather than importing it: boto3 1.34 to 1.41 run the CRT
  engine without it (an unconditional import failed every CRT transfer there
  with `ImportError`, 2026-10-06). The engine's floor is boto3 1.34.6, the
  first release whose `crt` extra brings an awscrt at boto3's own minimum
  (the extra's awscrt follows botocore's patch release: 1.34.0 to 1.34.5
  pin 0.19.17, one below it), and the awscrt the extra brings before boto3
  1.40.12 dials the standard port only, so a custom-port endpoint is refused
  there (docs/compatibility.md records both).
- **Explicit crt x lock contention**: aws-cli forces the CRT construction, as
  its documentation promises for concurrently running processes; boto3 - and the
  library default - silently falls back to classic. The two are
  indistinguishable in output and rc (proven; only the engine and so the
  throughput differ). The CLI keeps aws-cli's promise with
  `CrtClientOptions(allow_lockless=True)` (section 4, "Building without the
  cross-process lock"); library callers keep boto3's fallback by default.
- **Ctrl-C and fatals on the CRT lane**: pip s3transfer's CRT manager
  discards a drain-time `KeyboardInterrupt` - its coordinator converts the
  interrupt into `cancel()`, the cancellation error replaces it, and its
  shutdown drops that too - where the classic manager re-raises it. aws-cli
  lives with that loss: its per-item subscribers classify every
  non-`CancelledError` outcome as a failure, so a CRT run cut short - a
  drain-time Ctrl-C, a Ctrl-C in the submission window, a fatal folding the
  manager mid-run - prints `upload failed: ... AWS_ERROR_S3_CANCELED: Request
  successfully cancelled` per in-flight item at rc 1 (all three measured
  2026-08-01; aws-cli's own classic lane prints `cancelled: ctrl-c received` or
  one `fatal error:` line instead - the two aws-cli lanes differ). This library
  follows its error rules instead (overview.md section 3: an interrupt
  cancels the item and reaches the caller), the same on both engines: a CRT
  cancellation the run ordered - its `CancelToken`, a fatal or a Ctrl-C
  folding the manager - classifies `CANCELLED`
  (`Transferrer._cancel_initiated`, marked before the cancel is issued), and
  the swallowed drain-time interrupt is caught where the manager issues its
  cancel: `Transferrer._guard_crt_cancel` wraps each CRT coordinator's
  `cancel` and reads the interrupt being handled (`sys.exc_info`) there,
  marks that cancel as the run's order, and `Transferrer.__exit__` re-raises
  the interrupt once the drain is over. Which transfers the interrupt cancels
  stays the manager's decision, as on `aws` (the one whose wait it landed in,
  or every one still running); the rest finish with their real outcomes. A
  CRT cancellation the run did not order stays `FAILED`. The CLI therefore
  ends a CRT run cut short like a classic one - `cancelled: ctrl-c received`
  or the fatal's line, without aws-cli's per-item failed lines - at the same rc
  1; what it reports there is an error detail (overview.md section 3). A
  drain-time interrupt that cancels nothing (the waited-on transfer, and every
  other, already finished) still reaches the caller here, where aws-cli, having
  dropped it, exits 0 (docs/cli/aws-differences.md).
- **fio_options**: unavailable on any pip s3transfer
  ([`compatibility.md`](../docs/compatibility.md)), so the three `[s3]` keys
  are accepted and validated and do nothing, and `TransferConfig` has no field
  for them.
- **TransferConfig on old s3transfer**: below s3transfer 0.16.0 the config
  cannot reach `CRTTransferManager` and is dropped with boto3's own warning
  (`configured values will be ignored`), boto3-faithfully; the CRT client itself
  still gets `target_throughput`, passed to `create_s3_crt_client` directly,
  while `part_size` stays `None` (the CRT's dynamic part size): an explicit
  `multipart_chunksize` is only detectable through the `UNSET_DEFAULT`
  machinery s3transfer 0.16.0 introduced
  ([`compatibility.md`](../docs/compatibility.md) for what the caller sees).
  The gate is boto3's `TRANSFER_CONFIG_SUPPORTS_CRT` =
  `hasattr(TransferConfig, "UNSET_DEFAULT")`.
- **Empty / whitespace-only `verify`**: aws-cli refuses such a value outright
  from 2.36.6 on (`Invalid CA bundle: ...` at rc 255, its bundled botocore and
  s3transfer fork both carrying the check), and so does the CLI - but at the
  botocore client build, which every command performs before any transfer
  engine exists ([`cli.md`](./cli.md) section 4, `awsclisession.reject_empty_ca_bundle`).
  So the CRT lane is never handed one from the CLI, whichever engine the run
  selects, and both tools refuse it before any transfer (measured
  2026-09-06 under `preferred_transfer_client = crt`: an empty `--ca-bundle`,
  a whitespace-only one, and an empty `AWS_CA_BUNDLE`). For a **library**
  caller that builds its own client, pip botocore >= 1.43.54 (and pip
  s3transfer >= 0.19.2 on the CRT lane) raise `InvalidConfigError` on the
  value, while older ones read the empty string as falsy and turn TLS
  verification off. The library refuses the value itself on every version,
  with botocore's own error (`s3storage.reject_empty_ca_bundle`):
  `S3Storage.get_client` for every operation, `S3.client()` and
  `S3PathResolver` for the clients they hand out or take, and
  `create_crt_transfer_manager` as its first step - before the cross-process
  lock and whatever state the singleton is in - for a client passed straight
  to `materialize_crt_engine` (the client-only check: the environment is
  `S3.materialize_crt_engine`'s, below). For a client the caller hands over
  (`S3Storage(client=...)`, `S3PathResolver`, `S3.materialize_crt_engine`) an
  empty `AWS_CA_BUNDLE` is refused too, because botocore's credential clients
  resolve it first whatever `verify` the client was given - stricter than
  aws-cli, which refuses it only when it builds a client that reads the value (a
  credential client, the CRT request serializer). `REQUESTS_CA_BUNDLE`
  and a profile's empty `ca_bundle` in that position stay out of reach: the
  client does not name the session that decides whether they are read.
  `S3.client()`'s clients carry `CA_BUNDLE_ENVIRONMENT_VETTED` (their own
  value answers for the environment), and so do the CLI's, which skip that guess
  since `awsclisession.check_every_client` already checks every client
  their sessions build at aws-cli's own point, so each run is refused or
  completes as aws-cli's does.
- **Process-pinned singleton**: the region / credentials / endpoint of the first
  client to reach the CRT path monopolize the in-process CRT, and an incompatible
  second connection falls back to classic (identical behavior to boto3).
- **Download checksum validation**: both aws-cli's CRT mode and ours pass
  `S3ChecksumConfig(validate_response=True)` on every GET, so response
  validation is the CRT client's own, identical by construction. The classic
  engine's ranged-download full-object combine validation (a feature of
  aws-cli's bundled s3transfer fork that pip s3transfer lacks) is the SDK
  version difference described in transfer.md section 10.
- **Upload checksum default**: pip s3transfer's CRT module stamps `CRC32` on
  an upload that names no checksum; aws-cli's bundled one stamps `CRC64NVME`
  (its bundled botocore's default). The library keeps boto3's (section 1):
  a library caller names `checksum_algorithm` itself. The CLI names aws-cli's
  value for both engines (`awsclisession.default_upload_algorithm`, cli.md section 5.7), which is
  where the two agree. The difference is not cosmetic on this lane:
  aws-checksums computes CRC32 in software on x86-64 (about 3 GiB/s against
  about 20 for CRC64NVME), which on a 4-vCPU instance pushing 800 MiB/s was
  10% of a 1 GB upload's wall time against real S3 (benchmarks/RESULTS.md,
  2026-09-06). A library caller on the CRT lane who wants that speed passes
  `checksum_algorithm="CRC64NVME"`.
- **Cannot be verified under moto**: because CRT bypasses botocore's HTTP layer,
  actual verification is only on the e2e (dev stack) lane.
