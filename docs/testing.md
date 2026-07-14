# Testing

This document describes how the test suite is organized, what runs by default,
and how aws-cli parity is enforced. The exit-code charter this enforces lives
in [`overview.md`](./overview.md) section 3.

## 1. Tiers

| location | what | mechanism | runs |
|---|---|---|---|
| `tests/lib/` | `boto3-s3` library unit tests | hand-rolled fakes | always |
| `tests/cli/awscli/` | ports of aws-cli's own functional tests (one file per subcommand, diffable against aws-cli's `tests/functional/s3/`) | canned-response recording client (`tests/utils/recorder.py`) | always |
| `tests/cli/unit/` | `boto3-s3-cli`'s own unit tests (everything the ports don't cover) | fake clients via `Context` injection | always |
| `tests/cli/functional/` | golden replay: the CLI on moto must reproduce what aws-cli did on a real endpoint | in-process `moto.mock_aws` | always |
| `tests/cli/e2e/` | differential parity: `boto3-s3` vs the real `aws` binary against the same live endpoint, plus golden capture | subprocesses against MinIO / real S3 | opt-in (`BOTO3_S3_E2E_BUCKET`) |

Directory = provenance (awscli port vs own), subdirectory = mechanism
(stub / moto / live server). `uv run pytest` with no setup runs everything
except e2e (skipped with a reason). The `ci` GitHub Actions workflow runs the
quality gates and package builds on Linux, then runs this default suite on
Linux and macOS at the Python 3.10 floor, plus Python 3.14 on Linux and Windows.
It also downgrades to the declared boto3 / botocore / s3transfer floors and
runs the library and CLI compatibility-seam tests whose expected request
models are stable at that SDK generation. It needs no Docker because e2e self-skips without
`BOTO3_S3_E2E_BUCKET`.

One small group needs a **case-insensitive filesystem**: the `--case-conflict`
`*_with_existing_file` tests (aws-cli's `skip_if_case_sensitive`), where the
conflict is seen through `os.path.exists`. They take the `case_insensitive_workdir`
fixture (tests/conftest.py), which resolves a case-insensitive directory or skips:
they run as part of the normal suite on macOS / Windows (the tmp dir is already
case-insensitive) and self-skip on a case-sensitive Linux host. To run them on
Linux, set **`BOTO3_S3_PYTEST_CASE_INSENSITIVE_DIR`** to a case-insensitive
directory - under WSL2 a `/mnt/c/...` path works directly (the Windows drive is
case-insensitive), no Docker. Where no such path exists,
`tests/run_case_insensitive_fs.sh` mounts a FAT loopback image in a privileged
Docker container and points the env var at it (opt-in, like e2e; the default
`uv run pytest` needs no Docker and self-skips). The "two S3 twins in one listing"
case-conflict tests instead detect the conflict through the gate's in-flight set
and run everywhere, but only under a threaded submit (`max_concurrency=1`,
matching aws's `max_concurrent_requests = 1`) - a NonThreadedExecutor finishes
each twin before the next is judged (docs/transfer.md section "case-conflict
gate").

## 2. Exit-code charter enforcement

The e2e parity tests (`tests/cli/e2e/test_*_parity.py`) assert
`ours.rc == aws.rc` for **every** scenario, unconditionally - scenario flags
can relax stdout comparison and golden handling, but by design there is no
flag to relax the rc comparison. The scenario sets
(`tests/utils/<cmd>_scenarios.py`) are the charter's detection surface:
extend them whenever a subcommand or option is added, including error paths
(nonexistent bucket, out-of-range values). Note the per-command exit-code
shapes differ (docs/cli.md section 6): ls maps server errors to 254 while the
transfer family (rm / cp / mv / sync) reports every post-start error as rc 1,
with cp / mv / sync adding rc 2 for warnings-only runs (rm has no
warnings-only path).

Charter exceptions map naturally: extension options (e.g. `--help`) cannot
run on the aws side, so they are unit-tested instead of diffed.

`test_positional_fileb_bug_parity.py` is a dedicated drift tripwire rather
than a golden scenario. It feeds a readable binary positional to all six
single-path commands and asserts the explicit aws-cli bug-shaped exit codes
before comparing the two CLIs. If aws-cli repairs one of those inconsistent
paths, the aws-side assertion fails even if both implementations could
otherwise be changed together; review and remove the matching compatibility
branch instead of updating the expected code mechanically.

## 3. Golden contract

A golden (`tests/cli/goldens/<cmd>/<scenario>.json`) records what the real
aws-cli did for one scenario: `{scenario, argv, rc, stdout_lines,
aws_version}` - plus `remaining_keys` (the bucket end state) for destructive
commands. Committed to git.

- **Capture** - `UPDATE_GOLDENS=1 uv run pytest tests/cli/e2e` writes goldens
  from the aws capture. Regenerate only against a clean MinIO (tmpfs state)
  and review the diff before committing; `aws_version` gives provenance.
- **Drift check** - without `UPDATE_GOLDENS`, the e2e suite asserts the live
  aws output still matches the committed golden, so an aws-cli upgrade that
  changes behavior fails visibly.
- **Replay** - the functional suite seeds moto with the scenario's exact
  layout, runs the CLI in-process, and compares against the golden.

### Platform variants (Windows)

A transfer golden's local side is OS-dependent (result-line separators; the
file-vs-directory outcome of a trailing-`/` destination), so the cp/mv/sync
goldens (`WINDOWS_VARIANT_KINDS` in `tests/utils/golden.py`) may carry a
Windows twin next to the POSIX base: `<name>.windows.json`, captured from
the real `aws.exe`. Loading on Windows prefers the variant and falls back to
the base when absent - absence means the two captures are identical, which
holds for most scenarios. Capture on Windows writes only variants, and only
where the capture differs from the base on a compared field (a variant that
stops differing is pruned); base files are POSIX captures and are never
written from Windows - the platform-independent kinds (ls/rm/mb/rb/presign/
website, verified against `aws.exe` unchanged) skip Windows capture
entirely. One scenario has no Windows golden by design:
`cp_case_conflict_warn` (`undefined_on_case_insensitive_dest`) - aws's own
twin-download rename race makes its outcome undefined on a case-insensitive
destination (its warn text says as much), so the golden tiers stand down
there and e2e pins only ours' deterministic rc 0. Where aws itself is
nondeterministic no defined rc exists to match, so this does not breach the
exit-code charter.

### Endpoint policy (MinIO goldens, occasional real-AWS verification)

A golden is a **drift detector, not ground truth**: the truth the exit-code
charter binds to is the pinned aws binary's behavior on the *same endpoint*
as ours, which the parity tests compare directly and unconditionally. So
goldens are captured against MinIO (fast, free, resettable state), and the
suite is re-run occasionally against real AWS (`tests/run_e2e.sh`) using the
**same pinned aws-cli** (`scripts/install-awscli.sh`) - a golden mismatch
there then isolates the endpoint variable, never the aws-cli version.

When a real-AWS run diverges from a MinIO-captured golden, absorb the
difference in this order:

1. **Normalize** (preferred): a mechanical difference (request IDs, ETag
   values, region strings) is folded into the normalizers so both endpoints
   produce one canonical form - the golden loses no detection power.
2. **Accepted alternates**: a semantic difference where both shapes are
   legitimately aws-correct (e.g. a field only real S3 returns) may be
   allow-listed per scenario. Any such relaxation **must** come with a direct
   aws<->boto3-s3 same-run equality check on the relaxed field: comparing
   both sides against one golden is what transitively guaranteed their
   parity, and an allow-list breaks that transitivity (aws matching
   alternate X while ours matches alternate Y must still fail).
3. **`diff_only`** (last resort): a genuinely endpoint-relative outcome
   (MinIO accepts what S3 rejects, or vice versa) is not frozen into a
   golden at all - rc equality between the two CLIs remains asserted, per
   the charter.

None of these relax the rc comparison between the two CLIs (section 2).

Normalization (`tests/utils/harness.py`): for **ls**
(`normalize_ls_stdout`), leading timestamps are masked (`<TIMESTAMP>` -
capture and replay happen at different times/timezones) and the bucket name
becomes `<BUCKET>`; nothing else changes - lines are never sorted (ls order
is deterministic and parity-relevant) and never dropped. For **rm**
(`normalize_rm_stdout`) the lines are **sorted** instead: aws-cli emits
delete lines in parallel-completion order, which is nondeterministic run to
run (observed on MinIO), so the line *set* is the contract; the end-state
comparison (`remaining_keys`) pins down what sorting relaxes. Console output
is not byte-guaranteed
([`aws-cli-option-handling.md`](./aws-cli-option-handling.md)); stderr is
only checked for stable tokens, never compared byte-for-byte.

Destructive commands cannot share one seeding between the two e2e sides:
`test_rm_parity.py` seeds, runs aws, captures the end state, then resets and
re-seeds before the boto3-s3 run, and finally compares rc / sorted stdout /
end state. The functional replay verifies the moto end state against the
golden's `remaining_keys` too.

Bucket-lifecycle commands (**mb** / **rb**) extend the same model: their
goldens additionally record `bucket_exists` (the scenario bucket's existence
after the run; `remaining_keys` is `null` when it is gone), their stdout
shares rm's sorted normalization (no timestamps; `rb --force` interleaves
nondeterministically ordered delete lines), and they run against a
**sibling** bucket derived from the suite's main bucket (`-mb` / `-rb`
suffix) - the e2e main bucket must stay existing and empty, so the
`mb_bucket` / `rb_bucket` fixtures force-delete the sibling around each test
and `harness.force_delete_bucket` resets it between the aws and boto3-s3
runs. Sibling names must not end in `-an` (account-regional BucketNamespace
semantics are unverified against MinIO; covered by ports and unit tests
instead).

For **website** only the client-side-error scenarios (rc 252, no server
contact) carry goldens - every server-reaching scenario is `diff_only`
because MinIO rejects the operation outright (section 7) while a moto replay
would succeed; the success path is verified directly on moto instead
(`test_website_golden.py::TestWebsiteOnMoto`, asserting the
`get_bucket_website` round trip). Website stdout is empty in every
outcome and shares rm's normalization.

For **cp** the scenario fixes the *local* tree too: each side of the e2e
diff gets its own workdir (materialized from `local_src`), the CLI runs
with `cwd` set there, and argv / result lines carry workdir-relative paths
- goldens need no path masking. `normalize_cp_stdout` keeps, per physical
line, only the segment after the last `\r` (aws repaints progress in place
with no isatty gate, so piped stdout carries `Completed ...` segments and
right-padding), drops those progress statements (time/speed-dependent), and
**sorts** the remaining result lines (parallel completion order - the rm
rationale). What sorting and masking relax, the end states pin: goldens
record `remaining_keys` (bucket), `local_tree` (`dest/` as
`relpath:size:sha256-prefix` entries), and `head_fields` (selected
HeadObject fields of one probe key - how ContentType/Metadata/StorageClass
passthrough and the copy-props chain are verified end-to-end). The download
mtime stamp is asserted live per side (file mtime == object LastModified),
never via goldens. Scenarios whose outcome is endpoint-relative are
`diff_only` (`--sse` and `--storage-class GLACIER` fail on MinIO and
succeed on real S3; both CLIs always agree); the glacier *gate* shapes run
on moto instead (`TestCpGlacierOnMoto` - including the restored-object
pass and force-glacier letting S3's InvalidObjectState through). The `-`
streaming scenarios carry a `stdin` payload: the e2e driver feeds it to
both CLIs through the stdin-capable subprocess runners, and the moto
replay routes any scenario with a payload or a `-` in its argv through
`run_cli_in_process_streaming`, whose buffer-backed `sys.stdin` /
`sys.stdout` shims let the in-process CLI read stdin uploads and write
raw download bytes (a plain `StringIO` has no `.buffer`). A streaming
download's golden `stdout_lines` are the object body itself - the
errors-only printer both CLIs force for streams keeps result/progress
lines out of it.

**mv** is cp's model plus the move's defining end state: goldens
additionally record `src_tree` - the local *source* tree after the run,
captured unconditionally by the mv runners (what the move deleted, or kept
on dryrun / filter / no-overwrite / failure; bucket-side source survival is
already pinned by `remaining_keys`). cp goldens predate the field and load
it as `None` (not compared). The download-mtime expectation is read from
the object *before* the run - a successful move deletes the key the cp
harness re-reads afterwards. Scenarios reuse `CpScenario` verbatim
(`tests/utils/mv_scenarios.py`); the `--validate-same-s3-paths` surface has
no e2e lane (MinIO hosts no access points) and lives in the awscli port
(per-service recording clients) and the unit tier, and the glacier-gated
move shapes run on moto (`TestMvGlacierOnMoto` - a gated or failed move
must never delete the source).

**sync** is cp's model with both end states active at once -
`remaining_keys` pins the bucket (uploads, copies, S3-side `--delete`) and
`local_tree` pins `dest/` (downloads, local-side `--delete`); `src_tree` stays
`None` (sync never mutates its source, so the inherited field is neither
captured nor compared). The new scenario knob is
`CpScenario.local_mtimes`: workdir-relative offsets (+/-1 day) applied with
`os.utime` after materialization, so the size+time judgments are
deterministic against objects seeded at ~now - identically on MinIO (e2e)
and moto (replay). The at-both matrices pin the aws-cli rules from both
directions (upload skips when the destination is newer; a same-size
download runs only when the *local* side is newer; `--size-only` /
`--exact-timestamps` variants, the latter winning when combined). The
s3->s3 at-both scenarios rely on seed order only for the skip direction
(destination seeded after source is at least as new); the copy-side time
rules need no sleeps because they match upload's, already pinned there.
Scenarios live in `tests/utils/sync_scenarios.py`; the glacier-gated sync
shapes run on moto (`TestSyncGlacierOnMoto` - including the
restored-object *skip*: sync judges from the listing, which carries no
`Restore` status, aws-cli-faithfully).

For **presign** the golden is one normalized URL line
(`normalize_presign_stdout`): virtual-host URLs are canonicalized to
path-style and the endpoint is masked (`<ENDPOINT>` - the functional replay
runs with no endpoint override while the capture used MinIO's IP endpoint),
and the time/credential-dependent query values are masked (`X-Amz-Date`,
`X-Amz-Signature`, and the access key + date inside `X-Amz-Credential`). The
credential scope's region/service segment is compared, not blanket-masked -
the **endpoint policy**, in two steps:

- **step 1** (golden stability, `presign_scope_mask_region` +
  `normalize_presign_stdout(mask_region=...)`): the scope region folds to
  `<REGION>` only when it equals the environment default (`AWS_REGION` /
  `AWS_DEFAULT_REGION`), so a golden captured against one e2e region replays
  against any other. An explicit `--region` that differs from the default is
  left raw - that region is the scenario's whole point, and when it happens to
  coincide with the default nothing is masked either, so the golden still
  reflects it.
- **step 2** (e2e-only, `presign_scope_region`): masking a golden's region is
  a golden-stability concession, not license to stop checking it - the e2e
  suite pulls the raw, unmasked scope region straight out of both sides'
  stdout from the *same run* and asserts aws's and ours are equal, so the
  region the two CLIs actually signed with stays under test even where step
  1's golden comparison would hide a divergence.

Parameter order, `X-Amz-Expires`, `X-Amz-SignedHeaders`, and the
key path are compared verbatim. The functional replay needs no moto backend
(presign never contacts a server) and deletes `AWS_SESSION_TOKEN` for the
run - the root conftest exports one, which would add an
`X-Amz-Security-Token` parameter the token-less MinIO capture lacks. The
e2e suite adds a **fetch layer** for time-stable scenarios: both sides'
URLs are actually GET and compared on status (and body when 200) - the
endpoint accepting our signature like it accepts aws's is parity the URL
string alone cannot prove.

## 4. MinIO dev stack and the e2e gate

```
scripts/compose-up.sh          # idempotent; waits for bucket init
scripts/install-awscli.sh      # idempotent; pinned aws-cli -> .venv/bin
source scripts/minio-env.sh    # exports AWS_* + BOTO3_S3_E2E_BUCKET (no side effects)
uv run pytest                  # full suite including e2e
scripts/compose.sh down        # tear down; wraps `docker compose -f scripts/compose.dev.yaml`
```

`scripts/compose.dev.yaml` pins the compose project name (`boto3-s3-dev`) so
`compose-up.sh` matches the exact container name, and its `mc-init` sidecar
creates both `test-bucket` (manual play) and `boto3-s3-e2e` (e2e suite).
The performance benchmarks ([benchmark.md](benchmark.md)) reuse this same
stack and pinned `aws` but manage their own bucket (`boto3-s3-bench`); they
never touch `boto3-s3-e2e`.
Only one stack can own ports 9000/9001 - any other stack on those ports
conflicts; `compose-up.sh` then fails loudly instead of mistaking it for ours.

The e2e diff is only meaningful when the live `aws` matches the version the
goldens were captured with: the differential compares both CLIs against that
capture, so an `aws` that adds or drops a flag (e.g. `--no-overwrite`, added
mid-2.3x) diverges spuriously. `scripts/install-awscli.sh` pins it to the
aws-cli source revision the library is ported against by keeping the release
zip's self-contained `dist/` and symlinking
`.venv/bin/aws` (it installs no Python package, so the env is untouched, and a
matching install is reused rather than re-downloaded).

e2e safety contract: at collection time the suite probes that the `aws`
binary exists and that the bucket is reachable and **empty** (refusing to run
against a populated bucket); each test cleans up exactly the keys it seeded,
and the `bucket` fixture asserts emptiness before and after every test.

**Payload-size budget** (the suite must stay cheap against real AWS): a
scenario uses the smallest payload that exercises its behavior - byte-scale
literals almost everywhere; the deliberate exceptions are the multipart
scenarios at **9 MiB** (just past the 8 MiB threshold; cp/mv/crt, ~7
scenarios) and `ls`'s 1 MiB human-readable probe. A full run therefore moves
on the order of **a few hundred MiB total** (each scenario seeds and runs
per CLI side, so a 9 MiB scenario moves up to ~36 MiB), with transient
storage peaking around ~20 MiB - far under the 1 GiB comfort line. The
ceiling is **10 GiB per full run**: a new scenario that needs an MB-scale
payload keeps it at the smallest size that triggers the behavior and states
why in the scenario table; anything approaching GB-scale needs an explicit
design discussion first.

The suites isolate in both directions: the root `tests/conftest.py` forces
fake credentials (and strips `AWS_PROFILE` / `AWS_ENDPOINT_URL*`) for
everything except e2e, which overrides that fixture with a no-op; test
fixtures build clients from fresh `boto3.session.Session()` objects because
the process-global default session caches the first credentials it resolves.
Two mechanisms pin classic, because the CLI tiers and the library-default
path resolve the engine differently. The same root fixture points
`AWS_CONFIG_FILE` at a session-scoped file pinning
`[s3] preferred_transfer_client = classic`: now that cp/mv/sync read the
profile's `[s3]` section (cli.md section 8), this stops the host config from
leaking tuning into the moto/recorder tiers and - on a CRT-optimized host -
keeps `auto` from silently resolving to an engine moto cannot intercept. That
config file only covers the CLI transfer tiers (`resolve_transfer_client`),
not the library's own default `auto` path, which consults
`awscrt.s3.is_optimized_for_system()` directly (transfer.py); a second
autouse fixture (`_pin_classic_engine`) monkeypatches that probe to `False`
so the in-process/library transfer tests stay deterministic on a
CRT-optimized host (`test_transferrer.py` depends on it).

The dev environment is always CRT-present (the dev dependency group installs
`botocore[crt]`), matching aws v2 - the condition every parity tier runs
under. The opposite - an install without awscrt - is pinned by
`tests/cli/functional/test_crt_optional.py`: botocore freezes `HAS_CRT` and
its checksum registry at import, so the test drives the CLI in a fresh
subprocess that blocks `awscrt` before botocore loads, asserting the
documented degradation (transfer.md section 9): plain transfers and presign stay
rc 0; only the CRT checksum family fails in-pipeline ("Missing Dependency",
rc 1).

The **CRT transfer engine** (crt.md) has its own e2e lane,
`tests/cli/e2e/test_crt_parity.py`, because the CRT manager bypasses
botocore's HTTP layer and cannot run on moto - only against a real endpoint.
Both CLIs run with a temp `AWS_CONFIG_FILE` selecting
`preferred_transfer_client = crt` (the harness subprocess runners gained an
`env` overlay for this), and the aws side gets an explicit `--endpoint-url`
(aws's CRT client reads `use_ssl` from the argument only, so an env-only
endpoint dials TLS to http MinIO and dies - our client derives it from the
resolved boto3 client and needs no flag). The lane is differential, no
goldens: the CRT manager's stdout is byte-identical to classic (aws-cli
`s3handler` has no CRT branch), so the signal is that our CRT mode agrees
with aws's CRT mode on rc / stdout / bucket state / local tree / download
mtime across upload, download, mv, and sync (single-part and 9 MiB
multipart). The same lane covers the missing CRT-configured deletion shapes:
single/recursive `rm` and upload/download `sync --delete`. Those cases assert
CLI-observable parity rather than transport identity: boto3-s3 deliberately
keeps `DeleteObject` / batched `S3Deleter` for the S3-side deletions instead of
aws-cli's per-key CRT DELETE requests, while download sync-delete removes local
files on both sides (deleter.md section 4). A separate `--debug` check
pins that our side actually engaged the CRT engine by asserting the
transfer-time breadcrumb
`Transferrer._get_manager` emits (`transfer engine: CRTTransferManager`),
which names the engine *after* any CRT->classic fallback - guarding against a
silent classic fallback (the `s3transfer.crt` throughput log fires at CRT
*client construction*, before the compatibility gate, so it cannot tell a
real CRT transfer from a fallback). A further case
(`test_crt_ignores_classic_only_config`) pins the charter: `[s3]` classic-only
keys (`io_chunksize` / `max_bandwidth`) under CRT exit rc 0 like aws, not a
traceback. The lane is gated by the e2e opt-in plus an `awscrt` import check.
The selection matrix and `[s3]`
parsing run as in-process units (`tests/cli/unit/test_engine_selection.py`,
`test_runtimeconfig.py`, `tests/lib/test_crtsupport.py`) with awscrt and the
process lock monkeypatched.

## 5. aws-cli test ports

Ports keep aws-cli test names, canned responses, and expectations verbatim
where possible (see the adaptation rules in each port's module docstring -
e.g. `tests/cli/awscli/test_ls_command.py`). Genuine divergences are to be marked
`xfail(strict=True)` with the open design question in the reason, never
silently rewritten (none currently; all ports match aws-cli).

Each ported test is traceable to its aws-cli original by a simple convention:
a test carrying **no** `# aws-cli:` comment shares its original's class and
method name (so the same name locates it in aws-cli's
`tests/functional/s3/test_<cmd>_command.py`). A `# aws-cli:` comment names a
divergent origin instead - placed above the test for a per-test difference (a
rename, a parametrized merge of several aws-cli tests, a method from a different
aws-cli class or file, or `none` for a boto3-s3 addition), or above a class when
a whole block was carved out of one aws-cli class under the same method names.
The comment references the aws-cli test by logical `Class.method` name - or just
the method name when the origin is in the same class - never a checkout path.

The presign port freezes botocore's signing clock through the
`get_current_datetime` seam - patched in both modules that bind it,
`botocore.auth` and (when awscrt is importable - the dev environment always
is, section 4) `botocore.crt.auth` - and keeps aws-cli's expected URLs -
frozen-time signatures included - bit-for-bit.
The cp port injects `Context.transfer_config` with `use_threads=False`
(boto3's NonThreadedExecutor path) so multipart call order is deterministic
against the positional canned list, and rewrites aws-cli's expected
default `ChecksumAlgorithm: 'CRC64NVME'` to the `'CRC32'` that pip s3transfer
injects on upload paths (both engines add a default integrity checksum;
they just pick different algorithms - an explicit `--checksum-algorithm`
makes them agree, and docs/transfer.md section 10 has the full wire-deviation
list). Its case-conflict classes guard the existing-local-file variants
with a live case-insensitivity probe of the test directory (aws-cli's
`skip_if_case_sensitive`), so they run on macOS/Windows-like filesystems
and skip on default Linux ones.

## 6. Import contract

`tests/lib/test_import_contract.py` pins the lazy package root and explicitly
pure modules. `tests/cli/unit/test_import_contract.py` pins the CLI's two narrow
guarantees: top-level `--help` and `--version` load no boto3 / botocore /
s3transfer module or command module. Normal dispatch, usage errors,
subcommand help, and `S3()` construction have no SDK-free contract.
Module-loading cases run in fresh interpreters (`python -c` subprocesses) so
imports already made by the test runner cannot mask a regression; a
resolve-every-symbol case guards the three-way `__all__` / `TYPE_CHECKING` /
`_EXPORT_HOMES` mirror in the lazy `__init__`.

## 7. Known limitations

- **xdist** (not currently configured): if parallel runs are added, the e2e
  tier shares one bucket with an empty-before/after invariant and would not be
  xdist-safe (the other tiers would be). Move to per-worker key prefixes if
  parallel e2e becomes necessary.
- **moto fidelity**: moto raises an internal `IndexError` for `MaxKeys=-1`
  instead of S3's `InvalidArgument`, so `ls_page_size_negative` and
  `rm_page_size_negative` are `diff_only` (e2e only). Likewise `mb_existing`:
  moto mirrors real S3's us-east-1 quirk (re-creating a bucket you own
  succeeds) while MinIO answers `BucketAlreadyOwnedByYou` -> rc 1 on both live
  sides, so a golden would freeze the MinIO-specific rc. Scenarios that hit
  moto gaps should be flagged the same way, not papered over.
- **MinIO fidelity (the inverse gap)**: MinIO rejects **every**
  PutBucketWebsite with `MalformedXML` (index-only, both
  documents, empty config, and nonexistent bucket alike - its
  GetBucketWebsite answers `NoSuchWebsiteConfiguration` and its
  DeleteBucketWebsite is a no-op success). Both CLIs exit 254 identically,
  so the e2e rc comparison still holds, but the website success path cannot
  run there: those scenarios are `diff_only` and the success path is
  verified on moto (which supports the operation fully). Against real S3
  the same scenarios exit 0 on both sides - rc parity is endpoint-relative
  by design.
- **MinIO: no S3 object annotations** (probed 2026-07 on both minio/minio
  and pgsty/minio): the `x-amz-object-annotation-directive` header is
  ignored (harmless - the EXCLUDE both CLIs now send on every copy rides
  through the copy lanes unchanged, goldens unaffected) and
  ListObjectAnnotations answers 500, so `--copy-props all` cannot be
  exercised end-to-end. Measured against aws 2.35.18: single-part `all`
  exits 0 on both sides (nothing annotation-related on the wire); the
  multipart carryover fails with rc 1 and **identical stderr** on both. The
  live-only `cp_copy_props_all_multipart` scenario pins the defining end state:
  both read annotations before CreateMultipartUpload and leave no destination.
  Successful annotation writes, pagination, the three library staging modes,
  and temporary-file cleanup are covered by `TestCopyPropsAllCpCommand` and
  the library transfer tests because MinIO cannot serve those operations.

## 8. Running the suite on Windows (WSL2 host)

Windows is a supported OS (overview.md section 2); the suite runs there on a
real Windows CPython using a host-installed `uv`, driven either from a native
Windows shell or from WSL2 through its interop. Two things make the run
representative:

- **Work from an NTFS copy, not the WSL tree.** A Windows process can read
  the repo through `\\wsl.localhost\...`, but that path serves the ext4
  filesystem over 9P - case-sensitive and slow, a hybrid no real Windows
  deployment has. Copy the working tree to an NTFS directory instead,
  excluding the platform-bound and derived trees:

      rsync -a --delete --exclude .git --exclude .venv --exclude '<aws-cli-source-dir>' \
        --exclude __pycache__ --exclude out --exclude .pytest_cache \
        --exclude .ruff_cache  <repo>/  /mnt/c/tmp/boto3-s3-wintest/

  Replace `<aws-cli-source-dir>` with that checkout's repository-relative path.
  The aws-cli source is reference-only for the tests - nothing imports from it -
  so excluding it keeps the copy to a few MiB.

- **Sync with `--all-packages`.** A bare `uv sync` installs only the root
  project; without the `cli` workspace member every `boto3_s3_cli` import
  fails at collection:

      cd /mnt/c/tmp/boto3-s3-wintest
      uv sync --all-packages
      uv run pytest -q

  `uv` provisions its managed CPython for the pinned `.python-version` (3.10,
  the support floor) - the host Python installation is not used.

Prerequisites: a Windows `uv` on `PATH`, and Windows **Developer Mode** (or
an elevated shell) because several `tests/lib` scenarios create symlinks.
Tests staged on chmod-revoked access skip themselves on Windows (the
`skip_if_chmod_is_inert` mark in `tests/utils/host.py`).

**Goldens on Windows.** The cp/mv/sync goldens resolve to their
`<name>.windows.json` variants (section 3, "Platform variants"); regenerating
them needs this Windows setup plus the e2e stack below - `UPDATE_GOLDENS=1`
from Windows writes only those variants and never touches the POSIX base
files, so it is safe to run. `cp_case_conflict_warn` deliberately has no
Windows golden and its functional replay self-skips on a case-insensitive
filesystem.

**e2e on Windows.** The differential machinery works unchanged against the
WSL2 MinIO stack: pin `aws.exe` at the version `scripts/install-awscli.sh`
pins (the reference aws-cli source version - the section 4 drift rationale
applies to this binary too), start the stack inside WSL2 (`scripts/compose-up.sh`), and
Windows reaches it on `127.0.0.1:9000` through WSL2's localhost forwarding.
`scripts\install-awscli.cmd` is `install-awscli.sh`'s Windows twin and needs
no admin rights: it extracts the version-pinned MSI's self-contained payload
with `msiexec /a` (an administrative extraction - no registry entries, no
system PATH edits, any installed AWS CLI stays untouched) into
`%LOCALAPPDATA%\boto3-s3\aws-cli\<version>` behind a stable `current`
junction. The NTFS test copy carries no aws-cli source checkout, so pass the
version explicitly there (on a full checkout the argument is optional):

    cmd.exe /c "scripts\install-awscli.cmd 2.35.18"

The MinIO variables must be set in the **Windows** process - WSLENV
propagation cannot be relied on - which is what `scripts/minio-env.cmd` (the
`minio-env.sh` twin; a runner, because cmd cannot `source`) is for. It also
prepends the pinned `aws.exe` to `PATH` when present, mirroring how
`.venv/bin/aws` shadows any system `aws` on Linux:

    cmd.exe /c "scripts\minio-env.cmd uv run pytest -q tests\cli\e2e"

The Linux-capture caveat above applies to the e2e golden drift checks of
cp/mv/sync the same way. The bucket-empty invariant (section 4) is shared:
never run the Windows and Linux e2e suites concurrently.
