# boto3-s3-cli design (the CLI layer)

`boto3-s3-cli` is the `aws s3`-compatible CLI distribution that sits on top of
the `boto3-s3` library. Behaving exactly like `aws s3` (strict argument
validation, the option system) is **this layer's responsibility**, while the
library provides the building blocks from which parity can be achieved. For the
responsibility split, see [`overview.md`](./overview.md) section 1; for the handling of
individual options, see
[`aws-cli-option-handling.md`](./aws-cli-option-handling.md).

This document records the solidified design for the implemented scope (currently
`cp` / `ls` / `mv` / `rm` / `mb` / `rb` / `presign` / `sync` / `website`). Only
solidified design is added here.

## 1. Entry point and dispatch

- The console script `boto3-s3` -> `boto3_s3_cli.cli:main`.
- `boto3-s3` is the top-level command equivalent to `aws s3`. Subcommands (`cp` /
  `ls` ...) are registered as argparse subparsers.
- `main(argv, *, ctx=None)` parses -> dispatches to the corresponding
  subcommand's `Command` instance -> returns the exit code (an int). argparse's
  `SystemExit` is also absorbed inside `main` and converted into an exit code, so
  `main` always returns an int. The exit codes for exceptions and usage errors
  are covered in section 6 (the implementation of the exit code parity charter).
- `ctx` (`Context`) is the injection point for runtime dependencies (section 3.1). When
  not supplied, the real one (the default `Context()`) is assembled. Tests pass a
  `Context` loaded with fakes.

## 2. Parser construction (standard argparse, no added dependencies)

- The common options are registered on **both** the "top parser (with the real
  defaults)" and the "parent of each subparser (`suppress_defaults=True`)".
  Suppressing the subparser-side defaults (`argparse.SUPPRESS`) keeps a value
  parsed before the subcommand from being overwritten by the subcommand's own
  unset default. This lets a global option be placed **either before or after**
  the subcommand (both `boto3-s3 --profile foo ls s3://b` and
  `boto3-s3 ls s3://b --profile foo` work, matching `aws s3 --profile foo ls ...`).
- To avoid touching `argparse._SubParsersAction` (private) in type annotations,
  the subparsers are created by `cli.py`, and each subcommand class adds its own
  arguments via `configure(parser)`.

## 3. Module layout

| module | role |
|---|---|
| `cli.py` | Builds the top parser, dispatches, wires `--debug`, maps exceptions to exit codes. `_COMMANDS` is the registry of subcommand classes |
| `globals.py` | Common option definitions (the parent) + `build_client(args) -> S3Client` (the connection/authentication layer, section 5) + `build_service_client(service, args, *, region=None)` (the s3control / sts client used by mv's path validation, section 5.8) |
| `commands/base.py` | The `Command` ABC + `Context` (the injection point for runtime dependencies, section 3.1) |
| `commands/<sub>.py` | The `Command` subclass for each subcommand (e.g., `LsCommand` in `ls.py`, `RmCommand` in `rm.py`) |
| `commands/transferargs.py` | The surface shared by cp / mv / sync: the declaration equivalent to aws-cli `TRANSFER_ARGS` (`--expected-size` is cp-only opt-in, `--recursive` is opt-out for sync), validation of the SSE-C pair / checksum path types / case-conflict / S3 Express, conversion to `TransferOptions`, the non-stream location wiring (including the `--source-region` clone), transfer config resolution (`resolve_transfer_config`, section 8), and the tail of exit-code derivation |
| `runtimeconfig.py` | The port of the aws-cli `[s3]` runtime config (`RuntimeConfig` / scoped reads / the transfer-engine decision tree / `TransferConfig` construction). section 8; design in [`crt.md`](./crt.md) |
| `filters.py` | The order-preserving action for `--exclude` / `--include` + `FileFilter` construction (`compile_for_root` / `build_filter`: compile a globsieve matcher and wrap it to match `FileInfo.compare_key`. rm uses a key-derived root, cp / mv use a naming-derived root, sync's single filter is compiled against the source root and applied to both sides) |
| `progress.py` | `TransferPrinter`: aws-compatible rendering of transfer result lines / progress (section 5.7-5.9. A lock-guarded aggregator called from worker threads. The verb is `OpKind.value` - mv is `move` on every path. A record with no `dest` is rendered with a single endpoint - sync's `delete:` lines) |
| `shorthand.py` | Parsing of map-type option values (`--metadata k=v,...` / JSON form) |
| `output.py` | `aws s3`-compatible output formatting (`ls` listing lines, `rm` delete lines. Kept as pure functions; not turned into a class) |
| `autoprompt/` | The completion engine for `--cli-auto-prompt` (a port of aws-cli's `autocomplete/` onto the `boto3-s3` surface = `model.py` / `parser.py` / `completers.py`, pure Python) + the prompt_toolkit implementation (`prompt.py`) + the injection ABC (`prompter.py`). An opt-in extra. Design in [`autoprompt.md`](./autoprompt.md) |

### 3.1 The subcommand interface (`Command`) and dependency injection (`Context`)

For testability (eliminating monkeypatch) and to prepare for a growing number of
subcommands (cp / sync ...), subcommands are classes and receive their
dependencies through a `Context`.

- `Command` is an ABC (the `name` / `help` ClassVars, `configure(parser)`,
  `run(args, ctx) -> int`). Adding a subcommand requires only one subclass plus
  its registration in `cli._COMMANDS`; no other wiring code is needed.
- Instances are **created anew** (per-run) at parser-build time and at dispatch
  time respectively. `run()` may keep its in-flight state (transfer counters,
  progress, etc.) in instance attributes, so calling `main()` multiple times in a
  single process does not carry state over (this corresponds to how aws-cli's
  `ListCommand._run_main` holds `self._total_objects` and the like).
- `Context` is the container for the runtime dependencies that `main()` resolves.
  Currently these are `client_factory` (`argparse.Namespace -> S3Client`, default
  `globals.build_client`), `service_client_factory`
  (`(service, args, *, region=None) -> client`, default
  `globals.build_service_client` - the injection point where mv's
  `--validate-same-s3-paths` creates the s3control / sts clients, section 5.8), and
  `transfer_config` (`TransferConfig | None`. Overrides the transfer engine's
  defaults - tests inject `use_threads=False` to make the multipart call order
  deterministic. **Only when it is `None`** do cp / mv / sync build the config
  from `[s3]` (section 8), so an injected value always takes precedence),
  `auto_prompter` (`AutoPrompter | None`. The backend for `--cli-auto-prompt`.
  Default `None` -> `main` lazily creates the prompt_toolkit implementation. Tests
  inject a fake that returns canned argv to verify re-dispatch without a tty -
  [`autoprompt.md`](./autoprompt.md) section 7). Tests inject a fake client via
  `cli.main(argv, ctx=Context(client_factory=<fake>))` (no monkeypatching of
  module attributes).

## 4. Common options

These implement the policy in
[`aws-cli-option-handling.md`](./aws-cli-option-handling.md).

- **Connection/authentication (effective, section 5)**: `--profile` ->
  `boto3.Session(profile_name=)`, `--region` / `--endpoint-url` -> client kwargs,
  `--no-verify-ssl` / `--ca-bundle` -> `verify`, `--no-sign-request` ->
  `Config(signature_version=UNSIGNED)`, `--cli-read-timeout` /
  `--cli-connect-timeout` -> `Config`. The assembled client is handed to the
  library via `S3Storage(url, client=...)` (the library does not rebuild the
  connection settings).
- **`build_client`'s alignment with aws v2**: `build_client`
  absorbs five differences between stock botocore and the botocore bundled with
  aws v2.
  1. **region resolution** - aws v2 resolves the region as `--region` >
     `AWS_REGION` > `AWS_DEFAULT_REGION` > the profile's config `region` > the EC2
     IMDS region (its `_construct_cli_region_chain`), with the env vars
     present-wins. Stock botocore never adopted `AWS_REGION` (its region env is
     `AWS_DEFAULT_REGION` alone) and keeps its `IMDSRegionProvider` for
     smart-defaults only, so a bare client would resolve a *different* region
     whenever `AWS_REGION` is the sole source, or on an EC2 host with no
     configured region. `build_client` / `build_service_client` rebuild that exact
     chain in `_resolve_region` (reusing botocore's own `EnvironmentProvider` /
     `ScopedConfigProvider` / `IMDSRegionProvider`); an empty `AWS_REGION=`
     therefore selects the empty region too -> the same `Invalid endpoint` failure
     as aws (rc 255), not a fall-through to `AWS_DEFAULT_REGION`. The library
     keeps stock botocore order on purpose (the boto3=library / aws=CLI split, as
     for the profile chain in item 5).
  2. **always-on SigV4** - stock botocore downgrades presigned URLs to SigV2 in
     regions that accept SigV2 (us-east-1), but SigV2 does not exist in aws v2's
     botocore. `Config(signature_version="s3v4")` is set permanently
     (`--no-sign-request` overrides it with UNSIGNED).
  3. **us-east-1 regional endpoint** - aws v2 resolves us-east-1 as regional
     (`s3.us-east-1.amazonaws.com`). `s3={"us_east_1_regional_endpoint":
     "regional"}` is set permanently.
  4. **a pure-Python pin for symmetric SigV4 signing** - when awscrt is
     importable (it can be pulled in via the opt-in `crt` extra - transfer.md section 9 -
     or via a co-installed package), stock botocore swaps `v4` / `v4-query` /
     `s3v4` / `s3v4-query` for the CRT signer, changing the parameter order of
     presigned URLs (`X-Amz-Expires` moves after `X-Amz-SignedHeaders`). The
     botocore bundled with aws v2 pins these four to pure-Python classes and lets
     CRT handle only the asymmetric SigV4a family - `build_client` restores the
     same table (an in-place update of `AUTH_TYPE_MAPS`; with awscrt absent it is
     a no-op that just re-sets the defaults). Asymmetric SigV4a (MRAP ARNs and the
     like) is a domain where botocore itself requires awscrt: with the `crt`
     extra, the CRT signer handles it just as in aws v2; without it, botocore
     raises `MissingDependencyException` (`Using S3 with an MRAP arn requires an
     additional dependency. ...`) (as the charter stipulates, parity applies only
     when awscrt is present - overview.md section 3).
  5. **profile env precedence** - aws v2 resolves the active profile as
     `--profile` > `AWS_PROFILE` > `AWS_DEFAULT_PROFILE` > `default` (its bundled
     botocore lists the env vars as `['AWS_PROFILE', 'AWS_DEFAULT_PROFILE']`),
     whereas stock botocore reverses the last two
     (`['AWS_DEFAULT_PROFILE', 'AWS_PROFILE']` - the long-standing botocore #1725),
     so a bare `boto3.Session(profile_name=None)` would pick a *different* profile
     when both env vars are set. `build_client` / `build_service_client` resolve it
     via `_resolve_profile` to restore aws's order - the first env var that is
     *present* wins, an empty value included (`AWS_PROFILE=` -> the empty profile ->
     ProfileNotFound, matching aws). This correction is the CLI layer's alone: the
     library (`S3.client`'s `boto3.client` fallback) stays boto3/botocore-faithful
     and keeps stock order on purpose - the same library=boto3 / CLI=aws split as
     [`crt.md`](./crt.md).
- **Recognized and ignored (no-op, section 2)**: `--output` / `--query` / `--no-paginate`
  / `--no-cli-pager` / `--color` / `--cli-error-format` / `--no-cli-auto-prompt`.
  They are accepted (the `choices` are validated) and have no effect on behavior.
- **auto-prompt (section 3, design in [`autoprompt.md`](./autoprompt.md))**:
  `--cli-auto-prompt` is an **opt-in extra**. If `prompt_toolkit` is present, it
  launches an interactive prompt with `aws s3`-faithful completion and
  re-dispatches the edited command. If absent, an explicit flag is rejected with a
  message and a non-zero code, while a config/env-driven request silently proceeds
  to normal dispatch (its rc is non-contractual). Mode resolution is done
  **before** argparse, using the raw argv + the env `AWS_CLI_AUTO_PROMPT` + the
  profile's `cli_auto_prompt` (an SDK-free read, env > config > off) (so that it
  can launch even with no subcommand). `on-partial` means "run -> if a usage error
  (252), prompt". `--no-cli-auto-prompt` is a no-op (section 2), but specifying it
  together with `--cli-auto-prompt` is a usage error.
- **`--debug`**: attaches a stderr handler to the `boto3_s3` / `botocore` /
  `boto3` / `s3transfer` loggers (via the library's `boto3_s3.set_stream_logger`,
  `mask_secrets=True`). Credentials (signatures, the access key id, the session
  token, proxy credentials) are masked by `SecretMaskingFilter` (design in
  [`masking.md`](./masking.md)). `urllib3` is excluded (because it does not emit
  credentials). The library itself does not attach a handler at import time.
- **`--version`**: prints a single line in aws-cli v2's User-Agent format and
  exits with 0 (in either position). `boto3-s3-cli/<v> boto3-s3/<v> boto3/<v>
  botocore/<v> Python/<v> <System>/<release>`. Both `boto3` and `botocore` are
  printed because the patch level can drift within boto3's pinned botocore range.
  Each distribution's version is computed individually via
  `importlib.metadata` (`unknown` when not installed). Because the standard
  version action wraps to the terminal width through the help formatter, a custom
  action that keeps it to a single line writes the output to stdout. The numbering
  policy is in [`overview.md`](./overview.md) section 3.
- **`--cli-binary-format`**: accepted for parity and the `choices` (`base64` /
  `raw-in-base64-out`) are validated, but currently no command consumes it - a
  no-op. The blob argument of `aws s3 cp` (`--sse-c-key`) is
  **passed through** regardless of this setting (no base64 decode; only
  `fileb://` reads raw bytes), so cp is treated the same way.

Together, these accept all of the **7 ls-specific options (plus the positional
`<S3Uri>`) + 19 global** options listed by `aws s3 ls help` (`--version` prints
and exits, the other effective ones are reflected in the client / output, and the
rest are recognized and ignored or rejected). The list has been reconciled against
aws-cli's `awscli/data/cli.json` and `ListCommand.ARG_TABLE` as the primary
sources.

## 5. Subcommand implementation

The bucket part of an S3 URI may also be an access point ARN (regular /
Outposts), and just like aws-cli's `find_bucket_key`, the entire ARN (including
the `/`-separated name) is passed as `Bucket`. S3 Object Lambda / Outposts
**bucket** ARNs are rejected by `S3Storage.validate()` (deferred from the
permissive construction), matching aws's rc 252. Both are implemented in the
library layer.

### 5.1 `ls`

Equivalent to `aws s3 ls [<S3Uri>]`. `<S3Uri>` may omit `s3://` (`S3Storage`
fills it in).

| flag | handling |
|---|---|
| `--recursive` | `S3.ls(recursive=True)` (recursive listing, no `Delimiter`. Ineffective in bucket listing = same as aws) |
| `--page-size N` | `S3.ls(page_size=N)`. No range validation (passed straight to the server as in aws: 0 yields 0 entries -> rc 1, a negative value yields `InvalidArgument` -> rc 254. Charter compliance) |
| `--request-payer [requester]` | `S3.ls(request_payer="requester")` (ineffective in bucket listing = same as aws) |
| `--human-readable` | Formats the size in base-2 (CLI side, the library is not involved) |
| `--summarize` | Total Objects / Total Size at the end (CLI side) |
| `--bucket-name-prefix PREFIX` | `Prefix` of `ListBuckets` for bucket listing (ineffective in object listing = same as aws) |
| `--bucket-region REGION` | `BucketRegion` of `ListBuckets` for bucket listing (same as above) |

When the target has no bucket name (`boto3-s3 ls` / `ls s3://`; as in aws, a
leftover key in `s3:///k` is also dropped), it lists **all buckets**: passing the
library's service root (`S3Storage("s3://")` = empty bucket) to `S3.ls` yields
entries of `FileKind.BUCKET` (`mtime` = CreationDate).

Output (follows `aws s3 ls`, though a byte-for-byte match of the console output
is not guaranteed):

- Objects: `YYYY-MM-DD HH:MM:SS` (local tz) + size (right-aligned 10) + name
  (non-recursive = basename / recursive = full key).
- Common prefixes (non-recursive only): `{'PRE':>30} <name>/`.
- Buckets: `YYYY-MM-DD HH:MM:SS` (CreationDate, local tz) + bucket name (no size
  column).
- Exit code: 1 when a key / prefix is specified and there are 0 entries, 0
  otherwise (aws's `_check_no_objects`. Bucket listing is always 0, even with 0
  entries or a non-matching `--bucket-name-prefix`).

### 5.2 `rm`

Equivalent to `aws s3 rm <S3Uri>`. As in aws, rm's path validation is strict: a
non-`s3://` path is rc 252 ("Invalid argument type"). The target has 3 forms
(determined from aws-cli `filegenerator.py` plus the real
aws-cli's behavior):

- **Key specified (non-recursive) = a single blind DeleteObject**. It neither
  lists nor does a HeadObject, and even a nonexistent key "succeeds" (rc 0 + a
  `delete:` line). A `dir/`-terminated key is a blind delete of that folder
  marker object (objects inside are untouched).
- **`--recursive`**: normalizes the prefix to end with `/` and lists (aws's
  `FileFormat.s3_format`. Because `data` is listed as `data/`, a sibling such as
  `data-sibling.txt` with the same string prefix is **not swept in**). Folder
  markers are also deletion targets. The deletion is a batch `DeleteObjects` by
  the library's `S3Deleter` (the wire deviation from aws's per-key `DeleteObject`
  is accepted, [`deleter.md`](./deleter.md) section 4).
- **No key (non-recursive) = folder-marker cleanup**: it lists everything but
  **deletes only the size-0, `/`-terminated markers** (at any depth). This is not
  a full wipe (a full wipe is `--recursive`).

| flag | handling |
|---|---|
| `--recursive` | The above (`S3.rm(recursive=True)`) |
| `--dryrun` | Calls no delete API, emitting only `(dryrun) delete:` lines (the recursive ListObjectsV2 still runs = a listing failure is fatal even under dryrun) |
| `--quiet` | **Suppresses all output** (not just success lines but also `delete failed:` / `fatal error:` lines. aws does not create the printer at all. The rc is unchanged) |
| `--only-show-errors` | Suppresses only success lines. **dryrun lines do appear** (an aws quirk: `OnlyShowErrorsResultPrinter` does not suppress dryrun) |
| `--exclude` / `--include` PATTERN | Evaluated in command-line appearance order, last wins (a shared dest of the same shape as aws's `AppendFilter`). The root is recursive = the normalized prefix / single = the parent of the key / bucket root = "" (`rm_filter_root`). `cli/src/boto3_s3_cli/filters.py` translates it into globsieve and passes it to `S3.rm(filter=)` |
| `--request-payer [requester]` | Applied to both ListObjectsV2 and DeleteObject(s) |
| `--page-size N` | No range validation (same policy as ls). However, when the server rejects the listing for a negative value, the exit code is **1 for rm** (fatal. Different from ls's 254 - section 6) |

Output: stdout `delete: s3://bucket/key` / `(dryrun) delete: ...`, stderr
`delete failed: s3://bucket/key <exc>` (a per-key failure) / `fatal error: <msg>`
(an error that halts execution). **The output order of the delete lines is
non-contractual** (aws is non-deterministic, ordered by the parallel completion
of transfer futures. Tests use sort normalization + a final-state comparison,
testing.md).

An empty bucket URI (`rm s3://` / `rm s3:///key`) is not a usage error but
**rc 1** (aws sends `Bucket=""` to the API and it fails botocore's client-side
validation). A recursive run with 0 matches is rc 0 and silent (unlike
ls's rc 1, there is no equivalent of `_check_no_objects`).

### 5.3 `mb`

Equivalent to `aws s3 mb <S3Uri>` (aws-cli `MbCommand`; a direct descendant of
`S3Command` that does not go through the transfer-family CommandArchitecture). A
non-`s3://` path is rc 252. **The key part is silently dropped** (same as aws's
`split_s3_bucket_key` - `mb s3://b/k` creates bucket `b`). A bucket name ending
in `--x-s3` (an S3 Express directory bucket) is rejected with rc 252.

Request shaping is done by the library's `S3.mb` in the same shape as aws-cli:
`LocationConstraint` is the client's region (not sent when it is `us-east-1`); if
the bucket name ends in `-an`, `BucketNamespace=account-regional`; tags go to
`CreateBucketConfiguration.Tags`.

| flag | handling |
|---|---|
| `--tags KEY VALUE` | Repeatable, appearance order preserved. **Duplicate keys are sent as-is too** (rejection is the server's responsibility = same as aws) |

Output: stdout `make_bucket: <bucket>` (the bucket name only) / stderr
`make_bucket failed: <path> <msg>` (the original path argument). **Any error
after the operation begins is uniformly rc 1** (both `BucketAlreadyOwnedByYou`
and a credential error at request time. aws locally catches every exception from
create_bucket - section 6). `mb s3://` / `mb s3:///k` is rc 1 by the equivalent of
client-side validation for `Bucket=""` (the same leading branch as rm).

### 5.4 `rb`

Equivalent to `aws s3 rb <S3Uri>` (aws-cli `RbCommand`). A non-`s3://` path is
rc 252. **A URI with a key is also rc 252** ("Please specify a valid bucket name
only." A bare trailing slash in `s3://b/` is allowed, treating the key as empty =
same as aws).

| flag | handling |
|---|---|
| `--force` | **Before** delete_bucket, it internally runs the entire `rm <S3Uri> --recursive` (delegating to `RmCommand` - `delete:` lines also appear the same as rm. aws likewise re-enters `RmCommand`). If rm's rc != 0, it emits the fixed text "remove_bucket failed: Unable to delete all objects in the bucket, bucket will not be deleted." to stderr and is **rc 255** (the path where aws's RuntimeError reaches the general handler; golden captured). It does not attempt to delete the bucket |

Output: stdout `remove_bucket: <bucket>` / stderr `remove_bucket failed: <path>
<msg>`. A delete_bucket failure (`BucketNotEmpty` / `NoSuchBucket` / the
`Bucket=""` of `rb s3://`) is uniformly **rc 1**.

### 5.5 `presign`

Equivalent to `aws s3 presign <S3Uri>` (aws-cli `PresignCommand`; a direct
descendant of `S3Command`). **It does not communicate with the server at all**
(only the local signature computation of `generate_presigned_url`). As in aws,
`s3://` is **optional** (the `bucket/key` form is also allowed - unlike mb / rb /
rm, there is no path-format check at all). The key is required: `presign s3://b`
/ `s3://b/` is botocore's client-side validation ("Invalid length for parameter
Key") -> rc 252.

| flag | handling |
|---|---|
| `--expires-in <seconds>` | Default 3600. **No range validation** (0 / a negative value / over 604800 are all signed as-is = same as aws. S3 rejects it only **when the URL is used**). A non-integer is rc **255** (the integer-conversion rule of section 6) |

Output: a single URL line to stdout. With `--no-sign-request`, a bare URL with no
query (matches aws). The signature format derives from the client
configuration, and `build_client`'s always-on SigV4 + us-east-1 regional (section 4)
makes it the same shape as aws v2 - because stock boto3 downgrades a us-east-1
presign to SigV2, the library layer (`S3.presign`) stays boto3-faithful and
enforcement is the CLI layer's responsibility.

rc forms: **0 / 252 / 253 / 255 only** (because the server is never reached, 1 /
254 cannot occur). Unlike mb / rb there is no local catch - botocore's
`ParamValidationError` becomes a `ValidationError` in the library and is 252 via
`main()`, a client-creation failure is 253, and a non-integer `--expires-in` is
255.

### 5.6 `website`

Equivalent to `aws s3 website <S3Uri>` (aws-cli `WebsiteCommand`; a direct
descendant of `S3Command`, **no local catch**). It calls PutBucketWebsite once
and, on success, **outputs nothing** and is rc 0. The only options are
`--index-document <suffix>` (-> `IndexDocument.Suffix`) and `--error-document
<key>` (-> `ErrorDocument.Key`). **If neither is specified, an empty
WebsiteConfiguration is sent as-is** (it passes client-side validation; rejection
is the server's responsibility = same as aws).

Path handling follows the same procedure as aws's `_get_bucket_name`: `s3://` is
optional -> strip **exactly one trailing slash** -> treat the whole remainder as
the bucket name. Because aws **keeps a key (`s3://b/some/key`) folded into the
bucket name** and lets botocore's name regex reject it (rc 252), the CLI side
reproduces the same shape (`storage.key` or the `/` left after stripping) with a
`ValidationError` (`s3://b//` is also 252). An accesspoint ARN passes through
entirely as `Bucket` (aws's `block_unsupported_resources` rejects only Object
Lambda / Outposts bucket ARNs = same as `S3Storage`'s parsing).

rc forms: **0 / 252 / 253 / 254 / 255** (because, unlike mb / rb, there is no
local catch: a server rejection - `NoSuchBucket`, an endpoint that does not
accept the configuration - is **254** derived from `ClientError`, while a
client-construction failure such as `ProfileNotFound` / `PartialCredentialsError`
is **255**. 1 cannot occur). Because
MinIO always rejects PutBucketWebsite with MalformedXML
([`testing.md`](./testing.md) section 7), the success-path verification is handled by
moto.

### 5.7 `cp`

Equivalent to `aws s3 cp <src> <dst>` (aws-cli `CpCommand`; transfer family =
`CommandArchitecture` + s3transfer). The engine design is in
[`transfer.md`](./transfer.md), the implementation in `commands/cp.py` +
`progress.py`. Routes are classified solely by the presence or absence of the
`s3://` prefix (upload / download / s3->s3 copy. local->local is a usage error
252). Path shapes - the meaning of an existing dir / a trailing-separator dest,
which of the two names to adopt, the bucket-root normalization of a keyless
`s3://bucket`, the filter root - are derived by `boto3_s3.naming` (a port of aws's
`FileFormat`), shared between the CLI and the library.

**The declaration surface is the full aws-cli ARG_TABLE**:
`--recursive` `--dryrun` `--quiet` `--only-show-errors` `--no-progress`
`--progress-frequency` `--progress-multiline` `--exclude/--include`
`--follow-symlinks/--no-follow-symlinks` `--no-guess-mime-type` `--content-type`
`--cache-control` `--content-disposition` `--content-encoding` `--content-language`
`--expires` `--metadata` (k=v,... / JSON) `--metadata-directive` `--copy-props`
`--acl` `--grants` `--storage-class` `--website-redirect` `--sse` `--sse-kms-key-id`
`--sse-c(-key)` `--sse-c-copy-source(-key)` (pair validation and the s3s3-only
restriction are 252 with aws's wording, **the value is passed through** - aws
does not base64-decode. Only `fileb://` reads raw bytes)
`--request-payer` `--source-region` (effective only for s3s3. The source client
swaps the region + discards `--endpoint-url` = aws-cli `ClientFactory`)
`--page-size`, streaming (`-`), `--expected-size`, `--no-overwrite`,
`--case-conflict`, `--checksum-mode`, `--checksum-algorithm`.

**streaming (`-`)**: src `-` = stdin upload, dst `-` = stdout download (passing
`sys.std{in,out}.buffer` to the library. [`transfer.md`](./transfer.md) section 6). In
the form where the dest adopts the source name, the literal `-` becomes the
basename, per aws's naming (`cp - s3://b/pre/` -> key `pre/-`); this is derived in
naming.py before `S3Storage` is assembled. A run involving a stream **forces the
errors-only printer** (as in aws - it does not mix success lines or progress into
the raw bytes of a download). Combining `--recursive` is 252 (`Streaming
currently is only compatible with non-recursive cp commands`); stdout download +
`--no-overwrite` is also 252 (`--no-overwrite parameter is not supported for
streaming downloads`). An absent stdin is an in-pipeline fatal (`fatal error:
stdin is required for this operation, but is not available`, rc 1).
`--expected-size` (a multipart design hint) is **converted with a bare `int()`
inside the S3().cp call, but only on the streaming-upload route** (`src == "-"`)
- the only route aws ever converts it on (`UploadStreamRequestSubmitter`); on
every other route the value is untouched and ignored, exactly like aws, so a
non-integer there is **rc 0** (not converted). On the stream route a non-integer
is, unlike the 255 of the other integer options, an in-pipeline fatal of **rc 1**
(aws does a bare `int()` at submit time, section 6). The
existence check for a single local src excludes `-`.

**`--no-overwrite`**: passed through to the library's `no_overwrite`
(transfer.md section 7). Both the server's PreconditionFailed and an existing download
dest are a **silent skip** (rc 0, no line emitted).

**`--case-conflict`** (`ignore` (default) / `skip` / `warn` / `error`): passed
through to the library's case-conflict gate (transfer.md section 8). The skip / warn
messages go to stderr as a **NOTICE**, ahead of the printer's `--quiet` gate
(reproducing aws's direct `uni_print`. Not counted as warned; rc 0). `error` is
an in-pipeline fatal (rc 1). **S3 Express** (a bucket name ending in `--x-s3`,
s3local recursive only) branches separately: `skip` / `error` is a usage error
252 (`` `<value>` is not a valid value for `--case-conflict` when operating on
S3 Express directory buckets. Valid values: `warn`, `ignore`.``), while `warn`
emits a permanent warning to stderr and is downgraded to `ignore` (aws-cli
`_handle_case_conflicts`).

**`--checksum-algorithm`** (aws-cli's 9 choices: CRC64NVME / CRC32 / SHA256 /
SHA1 / CRC32C / SHA512 / XXHASH64 / XXHASH3 / XXHASH128) /
**`--checksum-mode ENABLED`**:
passed through to transfer.md section 9. Computing the CRT-family algorithms requires
awscrt, but it is not in the default dependencies and is an opt-in extra (the
delegation chain `boto3-s3-cli[crt]` -> `boto3-s3[crt]` -> boto3's own
`boto3[crt]`). In an environment without awscrt, only the CRT-family values
become an in-pipeline failure (rc 1) and diverge from aws (v2 bundles awscrt),
but this is allowed because the charter stipulates that awscrt-dependent features
are subject to it only when awscrt is present (overview.md section 3, transfer.md section 9).
Signing stays pure-Python via the pin of section 4.

The validation order of `run()` (corresponding to aws's stages): integer
conversion (255) -> route type / streaming constraints (252) -> **checksum path
type** (`--checksum-algorithm` is locals3 / s3s3, `--checksum-mode` is s3local
only. `Expected <param> parameter to be used with one of following path formats:
...` 252 - shared with mv) -> **a nonexistent single local
src (255, before the client factory**, equivalent to aws's bare RuntimeError.
`-` is excluded. aws-cli `_validate_path_args` checks this right after the
checksum pairing and *before* SSE-C, so the 255 wins when both fail) -> the SSE-C pair / `--case-conflict` Express branch / `--metadata`
parsing / blob (252) -> client creation (253) -> `S3().cp(...)`.
**S3().cp is the in-pipeline boundary**: `BatchError` -> 1 (the `... failed:`
lines have already been emitted by on_result), any other library exception ->
a single `fatal error:` line + 1 (a single s3 src's HeadObject 404 = `Key "..."
does not exist`, a listing error, a malformed `--grants`, a non-integer
`--expected-size`, an absent stdin, and case-conflict `error` are also here), a
normal return is **2** if the warned count > 0, else 0.

**Output** (`TransferPrinter`, aws-cli `ResultPrinter` shape): success
`upload|download|copy: <src> to <dst>` (stdout. the local side is rendered
relative to cwd = aws-cli `relative_path`, the s3 side is `s3://...`), a `(dryrun) `
prefix, failure `<kind> failed: <src> to <dst> <err>` (stderr), warning `warning:
<body>` (stderr, the body assembled by the library with aws-cli wording). Progress
is `Completed <done>/<total> (<speed>/s) with <n> file(s) remaining`, overwritten
with `\r` (**no isatty gate** = mixed into a pipe too, as in aws. Goldens mask
it). The suppression matrix is the same shape as rm: `--quiet` = no printer at
all (**failures are silent too**, the rc is reflected), `--only-show-errors` =
suppresses only success/progress (dryrun appears. A run involving a stream forces
this), `--no-progress` = suppresses only progress. Only the **NOTICE**
(case-conflict's skip / warn messages) is outside the matrix: it goes to stderr
even under `--quiet` and is not counted (transfer.md section 8). aws's `~total
(calculating...)` display (the listing-incomplete marker) is not reproduced (the
console output is non-contractual, option-handling section 6).

rc forms: **0 / 1 / 2 / 252 / 253 / 255**. 254 cannot occur (the transfer family
folds every error after the start into 1). 255 is for the integer options + **a
nonexistent single local src** (aws's bare RuntimeError -> general handler).
The sources of a warning (rc 2): glacier skip, an mtime
stamp failure, an unreadable/special local file, a broken symlink, an invalid
mtime, a parent-ref escape, the pre-warning for a >48.8 TiB upload
([`transfer.md`](./transfer.md) section 8).

### 5.8 `mv`

Equivalent to `aws s3 mv <src> <dst>` (aws-cli `MvCommand`). The implementation is
`commands/mv.py` + `commands/transferargs.py` (shared with cp). **The transfer
surface is fully shared with cp (section 5.7)** - the declaration, validation, options
conversion, location wiring, output, and rc derivation all go through the same
code. This section records only the differences. The library side is `S3.mv`
(the cp pipeline + `is_move`, [`transfer.md`](./transfer.md) section 11).

**Differences in the declaration surface** (aws-cli ARG_TABLE: cp -
`EXPECTED_SIZE` + `VALIDATE_SAME_S3_PATHS`): `--expected-size` is not declared
(`Unknown options` 252). `--validate-same-s3-paths` is added. streaming is
**rejected at declaration**: if either src / dst is `-`, it is 252 (`Streaming
currently is only compatible with non-recursive cp commands` - the aws-cli wording
stays "cp commands" even for mv. `mv - -` hits the local->local usage error
first).

**mv-specific validation** (for s3s3, before the client factory = SDK not loaded):

1. **The same-path guard** (always): if the keyless-normalized URI (`s3://b` ->
   `s3://b/`) matches `naming.same_path` (an exact match, or a `/`-terminated dest
   + `basename(src)` concatenation equals src) -> 252 (`Cannot mv a file onto
   itself: <src> - <dst>`, displaying the normalized original URI). **`--recursive`
   is also subject to this** (`mv --recursive s3://b/d s3://b/` is 252 even when no
   key actually overlaps with itself - a faithful false positive of aws-cli.
   Confirmed by measurement).
2. **`--validate-same-s3-paths`** (the flag, or when the env
   `AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS` is the **string `true`** - aws-cli
   `ensure_boolean` treats anything other than `'true'` (a lowercased comparison)
   as false. `=1` is invalid): only when `naming.same_key` (a
   bucket-ignoring key comparison, including the `/`-anchored basename rule) is
   true, both sides are resolved to their real buckets with
   `boto3_s3.pathresolver.S3PathResolver` (access point ARN / alias / outposts
   ARN / MRAP ARN. A bare bucket passes through with no API call) and the
   same-path guard is reapplied to every combination - a match is 252 (the message
   uses the original URI). The clients go via `Context.service_client_factory`:
   the src-side s3control uses `--source-region` (when unspecified, the session
   default - it does not fall back to `--region`, like aws-cli's dead-default),
   the dst side uses `--region`, and sts has no region (a transcription of aws-cli
   `from_session`). An outposts **alias** is unresolvable, 252, and a missing MRAP
   alias is also 252 (the wording is verbatim from aws-cli). A ClientError from
   s3control / sts keeps `__cause__` and is **254** (aws is also 254 on
   a GetCallerIdentity failure).
3. **A warning** (validation off and same_key and either side is an access-point form
   = `pathresolver.has_underlying_s3_path`): the aws-cli-worded permanent warning
   (`warning: Provided s3 paths may resolve to same underlying s3 object(s) ...`)
   is written directly to stderr and execution **continues** (no rc effect,
   appears even under `--quiet` - outside the printer).

Checksum path-type validation (`--checksum-algorithm` is locals3 / s3s3,
`--checksum-mode` is s3local only. `Expected <param> parameter to be used with one
of following path formats: ...` 252) is shared by cp / mv (transferargs.py. mv's
probe revealed that it was unimplemented on the cp side, and it was added at the
same time).

**The source deletion** is the engine's job, not the CLI's (transfer.md section 11): for
each successful item, an upload deletes the source through its `Storage.delete`
(`LocalStorage.delete`, an `os.remove`, since the CLI's upload source is always
local), while a download / copy does a DeleteObject against the source-side
client (RequestPayer passed through). On a
dryrun / a filter exclusion / a skip (no-overwrite, glacier) / a transfer
failure, the source remains, and **a failure of the deletion itself makes that
item a `move failed:` (rc 1)** (the bytes have already arrived). An emptied local
dir is kept (same as aws).

**Output** uses the mechanism of section 5.7 with only the verb being `move` (success
`move: <src> to <dst>`, `(dryrun) move: ...`, failure `move failed: ... <err>`).
The wording of the glacier warning stays the route word ("Unable to perform
download operations ..." - because aws-cli uses operation_name. transfer.md
section 8).

rc forms: in addition to the same **0 / 1 / 2 / 252 / 253 / 255** as cp, only a
resolution-API failure of `--validate-same-s3-paths` can be **254** (it occurs
before the operation begins = outside the transfer-exception rule).

### 5.9 `sync`

Equivalent to `aws s3 sync <src> <dst>` (aws-cli `SyncCommand`). The
implementation is `commands/sync.py` + `commands/transferargs.py` (shared with
cp / mv). The transfer surface, output, and rc derivation are shared with cp
(section 5.7), and this section records only the differences. The library side is
`S3.sync` (a two-layer pipeline + a pure pairing comparator - design in
[`sync.md`](./sync.md)).

**Differences in the declaration surface** (aws-cli ARG_TABLE: `TRANSFER_ARGS` +
metadata / copy-props / metadata-directive / case-conflict / no-overwrite + the
strategy-derived `--delete` / `--size-only` / `--exact-timestamps`): **`--recursive`
and `--expected-size` are not declared** (`Unknown options` 252 - sync is always
recursive and has no streaming form). `add_transfer_arguments(include_recursive=False)`.

**Validation order** (before the client factory = SDK not loaded):

1. Integer-option conversion (255)
2. Route type: local->local is usage 252 (`usage: boto3-s3 sync <LocalPath>
   <S3Uri> or <S3Uri> <LocalPath> or <S3Uri> <S3Uri>` + `Error: Invalid argument
   type`)
3. If `-` is on either side, 252 (the wording stays "cp commands" - same as mv)
4. Checksum path type 252 (the cp / mv-shared `validate_checksum_paths_type` -
   aws-cli `_validate_path_args` applies to sync too)
5. A nonexistent locals3 src -> 255 (aws-cli `_validate_path_args` checks this
   right after the checksum pairing and *before* SSE-C and the directory-bucket
   check, so the 255 wins when more than one fails)
6. SSE-C pair 252
7. **An S3 Express directory bucket on either side is 252** (`Cannot use sync
   command with a directory bucket.` - aws-cli
   `_validate_not_s3_express_bucket_for_sync`. The `--x-s3` suffix decision =
   `transferargs.is_s3express_path`. A **local** dir of the same name passes)
8. case-conflict resolution (treated as `recursive=True` - sync has no flag)
9. options conversion (`no_overwrite` is passed through in options; `S3.sync`
   reads it as the write-guard and strips it before the engine - sync does not
   attach IfNoneMatch. sync.md section 3)

**The filter is compiled once**: the `--exclude` / `--include` sequence is turned
into a single `FileFilter` against the source root (`plan.filter_root`) and passed
to `S3.sync(filter=)`, which applies it symmetrically to both sides. Because the
same filter prunes the destination too, "what the filter excludes is also
excluded from `--delete`" falls out (sync.md section 1). A relative pattern is
root-independent so one compilation suffices; an absolute pattern is relativized
against the source only (sync.md section 7).

**Output**: the transfer lines are the same as section 5.7 (the verb is the route word
upload / download / copy). A deletion is `delete: <endpoint>` (**no `to` clause** -
the printer renders a record with no `OpResult.dest` as a single endpoint. local
is rendered relative to cwd) / `(dryrun) delete: ...`. The `--quiet` /
`--only-show-errors` matrix stays as the rules of section 5.7. The interleave order of
delete lines and transfer lines is non-deterministic in aws too (goldens use a
sorted comparison).

rc forms: the same **0 / 1 / 2 / 252 / 253 / 255** as cp (a deletion failure is
also aggregated into 1 by the transfer-exception rule. When src is a file, it is
2 from a walk warning; when dest is a file, it is 1 from a per-item `[Errno 20]` -
sync.md section 6).

## 6. Exit codes (the implementation of the exit code parity charter)

Following the charter in [`overview.md`](./overview.md) section 3, these match aws-cli
v2's convention (aws-cli's `awscli/constants.py`).

| code | condition | the name on the aws-cli side |
|---|---|---|
| 0 | Success. `--help` / `--version`, and `BrokenPipeError`, are also 0 | - |
| 1 | A subcommand-specific "no result" etc. (`ls` is a specified key / prefix with 0 entries), **all errors after the start of rm / cp / mv / sync / mb / rb** (below) | the convention of the S3-family commands / a task failure of the transfer family |
| 2 | **A transfer that completed with warnings only** (cp / mv / sync's glacier skip, an mtime stamp failure, an unreadable local file, etc. section 5.7) | a task warning of the transfer family |
| 252 | A usage error (an unknown option = `Unknown options: ...`, an invalid choice / value), a client-side `ValidationError`, a `--cli-auto-prompt` rejection | `PARAM_VALIDATION_ERROR_RC` |
| 253 | `ConfigurationError` (credentials / region unresolved, the degradation of `[s3] preferred_transfer_client=crt` x an absent awscrt section 8) | `CONFIGURATION_ERROR_RC` |
| 254 | A server-side error (a `Boto3S3Error` whose `__cause__` is a botocore `ClientError`) | `CLIENT_ERROR_RC` |
| 255 | Any other general error (including `TransportError`, a botocore client-construction error such as `ProfileNotFound`, and any otherwise-uncaught exception via `_dispatch`'s backstop), **a failure of the rm stage of `rb --force`** (section 5.4), **a non-integer value of an integer option** (below) | `GENERAL_ERROR_RC` |

The mapping is `cli.exit_code_for`. It prioritizes "**whether the server was
reached** (whether it derives from `ClientError`)" over the library's exception
classification: even if the server returns a 400 and the library classifies it as
`ValidationError`, the exit code is 254 (because aws-cli treats every error after
reaching the server uniformly as `CLIENT_ERROR_RC`). The **message wording** of a
usage error may stay as argparse's, except for an unknown option (`Unknown
options: ...` = the same shape as aws-cli) - what the charter requires is the exit
code, and a byte-for-byte match of the console output is not guaranteed.

**The exception rule for rm / cp / mv / sync (the transfer-family commands)**:
aws-cli's transfer family (rm / cp / mv / sync) aggregates errors after the start
of the operation as a task failure / fatal error and makes them **uniformly rc 1,
even when server-derived** (the rc computation of `CommandArchitecture.run`. Both
a listing failure with NoSuchBucket and the InvalidArgument of `--page-size -1`
are 1). Therefore `RmCommand.run` / `CpCommand.run` / `MvCommand.run` /
`SyncCommand.run` catch the library exceptions themselves and return 1 (a per-item
failure = `BatchError` -> the `... failed:` lines have already been emitted by
on_result, anything else = a `fatal error:` line), and do not let them flow to
`main()`'s `exit_code_for` (the 254 family). What becomes 252 is only a usage
error before the operation begins (a non-s3 path, an ARN rejection, cp / mv's
route type / SSE-C pair / mv's same-path guard, etc.). cp / mv additionally make
a warnings-only completion rc **2** (aws-cli `CommandArchitecture.run`'s `failed>0
-> 1, elif warned>0 -> 2`). Only mv's `--validate-same-s3-paths` can reach the
server before the operation begins, so its resolution-API failure is plainly
**254**, outside the exception rule (section 5.8).

**The exception rule for mb / rb (the `S3Command` family)**: aws-cli's mb / rb
catch the exceptions of the API call within the command and make them uniformly
rc 1 (they do not turn it into 254 even when server-derived). `MbCommand.run` /
`RbCommand.run` likewise return 1 with a local catch. Both build the client
**before** their path checks (mirroring aws's `S3Command._run_main`, which builds
the client in `super()._run_main()` ahead of the scheme / empty / `--x-s3` / key
checks), so a client-construction failure takes precedence over a path usage
error - e.g. `mb badpath --profile <bad>` is the construction error's 255, not
the scheme 252, exactly like aws. (This means an `mb` / `rb` path usage error
loads `boto3`; that is within the import contract, which keeps only the
pre-dispatch paths SDK-free - docs/imports.md section 2.) Client creation is
outside the local rc-1 catch: it translates botocore's construction-time errors
into the library taxonomy so they reach the exit-code mapping instead of escaping
as a traceback - `NoCredentialsError` /
`NoRegionError` -> `ConfigurationError` = 253 (aws's dedicated handlers); every
other `BotoCoreError` -> a base `Boto3S3Error` = 255 (aws's
`GeneralExceptionHandler`), including `ProfileNotFound` for a bad `--profile`
**and `PartialCredentialsError`** (e.g. an access key with no secret) - aws has
no handler for either, so both are 255, not 253. A schemeless
`--endpoint-url` is rejected up front as a usage error (252). As a final
backstop, `_dispatch` maps any non-`Boto3S3Error` exception that still escapes a
command to the same chain (credential/region 253, `ClientError` 254, else 255),
so no path crashes with a traceback + rc 1. The only exception is the rm-stage
failure of `rb --force` = rc 255 (section 5.4). Note that even among direct descendants of the same
`S3Command`, **website / presign have no local catch**: website's server
rejection is plainly 254 (section 5.6), and presign never reaches the server in the
first place (section 5.5).

**The conversion rule for integer options**: aws-cli converts integer-type
options (`--page-size` / `--expires-in` / `--progress-frequency`) with a bare
`int()`, and a failure (`ValueError`) reaches the general handler and becomes
**rc 255** (not 252; including that it fires **before** the path-format check).
Because argparse's `type=int` would turn the same error into
a usage error (252), it is not used; instead, each `run()` converts at the top via
`parse_integer_option` in `commands/base.py` (before the client factory = exits
255 with the SDK still unloaded). **The exception is cp's `--expected-size`**:
because aws does a bare `int()` at submit time (within the pipeline) and **only on
the streaming-upload route**, a non-integer there is not 255 but a `fatal error:`
of **rc 1**; off the stream route the value is ignored, so a non-integer is rc 0
(section 5.7).

## 7. Import discipline (startup cost)

Paths that end in `--help` / `--version` / a usage error import neither the AWS
SDK (boto3 / botocore / s3transfer) nor **`prompt_toolkit`** at all. The contract
and the implementation as a whole are in [`imports.md`](./imports.md); enforcement
is in `tests/cli/unit/test_import_contract.py` (the forbidden roots include
`prompt_toolkit`). The key points on the CLI side:

- Imports that reach the SDK go inside `build_client` and each `Command.run()`.
  They are not placed at the module top level where `configure()` runs (names
  derived from pure-Python `types` / `exceptions` / `globsieve` are allowed at the
  top level).
- The `--version` line is assembled when the action fires, and the boto3 /
  botocore versions are read from the distribution metadata (the package proper is
  not imported).
- The help choices / help text are a static mirror of aws-cli's static
  tables (the same idiom as the `cli.json` mirror of section 4). They are not taken
  dynamically from botocore's models.
- `runtimeconfig.py`'s top level is also pure Python (only `Boto3S3Error` /
  `ConfigurationError`). boto3 (the scoped config read), awscrt (the decision
  tree), and `TransferConfig` construction are imported inside functions, paid for
  only when a transfer path is reached.
- The `autoprompt` package and `prompt_toolkit` are imported only when
  `--cli-auto-prompt` fires (`cli.main`'s resolver only scans the raw argv and
  needs no import). The completion engine proper (`model` / `parser` /
  `completers`) is pure Python, and only `prompt.py` bundles `prompt_toolkit`
  ([`autoprompt.md`](./autoprompt.md) section 4).

## 8. Transfer-engine selection and the `[s3]` runtime config

cp / mv / sync read the transfer settings from the profile's `[s3]` section
(`~/.aws/config`), determine the transfer engine (classic / CRT), and hand it to
the library. The overall design and the library side (boto3-faithful) are in
[`crt.md`](./crt.md). The key points on the CLI side (aws-cli-faithful):

- **Reading and validating `[s3]`**: `runtimeconfig.load_scoped_s3_config` reads
  it with `session.get_scoped_config().get("s3", {})`, and
  `RuntimeConfig.build_config` converts sizes / rates / bools exactly as aws-cli
  `transferconfig.py`, resolves the `default` -> `classic` alias, and validates
  invalid values. An invalid value is a `Boto3S3Error` (rc 255 - aws-cli's
  `InvalidConfigError` is also 255 at the general handler. It is placed **after**
  the usage 252 / src-absent 255 validation: an invalid `[s3]` value loses to
  both). This also closes the existing gap where classic's
  `multipart_threshold` etc. did not take effect from the config.
- **The engine decision tree** (`resolve_transfer_client`, a port of aws-cli
  `TransferManagerFactory`): `s3s3` -> unconditionally classic; `preferred` is
  `classic` -> classic; `crt` -> crt if awscrt is present (if absent, a
  `ConfigurationError` rc 253 = a CLI-specific degradation); `auto` -> crt if
  `is_optimized_for_system()` and the lock can be acquired, otherwise classic.
  Streaming does not force classic.
- **`TransferConfig` construction** (`build_transfer_config`): pass only the keys
  explicitly present in `[s3]` to the ctor (an unset one keeps boto3's
  `UNSET_DEFAULT` sentinel = "a CRT part_size only when `multipart_chunksize` is
  explicit" holds). The config is built **per engine** (the same as the aws-cli
  factory): classic gets all keys + `max_request_queue_size` +
  `max_in_memory_*_chunks=6`, while crt gets only the keys the CRT client reads
  and does not pass classic-only keys (`io_chunksize` / `max_bandwidth`, etc.)
  (matching the fact that aws's CRT ignores them + so as not to die in boto3's CRT
  config validation, crt.md section 4). The resolved engine is placed in
  `preferred_transfer_client` (so the library does not re-resolve `auto`).
- **Wiring**: each `run()` calls `transferargs.resolve_transfer_config(args, ctx,
  paths_type=...)`. A test-injected `ctx.transfer_config` always takes precedence.
  `preferred_transfer_client` has no CLI option (config key only. Same as
  aws-cli).
