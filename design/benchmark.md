# Performance benchmarks

The suite under `benchmarks/` exists to detect performance regressions
against the goal stated in [overview.md](overview.md) section 1: performance
equal to or better than `aws s3`. It is a local, manually run harness - it is
not part of pytest (`testpaths` stays `tests`), not run in CI (shared runners
make wall-clock thresholds meaningless), and nothing in it ships with either
package.

Two comparison axes, by design:

1. **Same-run differential against the pinned `aws` binary** (E2E mode). Both
   CLIs run back-to-back against the same local MinIO endpoint inside one
   run, so host noise hits both sides and cancels in the ratio. This is the
   primary regression signal on a noisy host (WSL2).
2. **History against boto3-s3's own past runs.** Every run stores a JSONL
   file under `benchmarks/results/` (git-ignored); `--baseline` compares the
   current run against a stored one.

## Quick start

```
scripts/compose-up.sh                  # MinIO stack (same as the e2e suite)
source scripts/bench-env.sh            # the lane's interpreter + environment
uv sync --all-packages --locked        # provisions .venv-bench on first use
scripts/install-awscli.sh              # pinned aws -> .venv-bench/bin/aws
source scripts/minio-env.sh            # endpoint + credentials env
uv run python -m benchmarks run        # both modes, classic engine
```

Useful variants:

```
uv run python -m benchmarks run --mode inprocess        # no docker needed
uv run python -m benchmarks run --engine both           # adds the CRT lane
uv run python -m benchmarks run --baseline last         # flag regressions vs the previous run
uv run python -m benchmarks run --quick --samples 2     # harness smoke test (shrunken corpora)
uv run python -m benchmarks run --large-transfer-mb 1024 # size the single-object transfers
uv run python -m benchmarks report [FILE] --baseline REV
uv run python -m benchmarks list
```

Exit codes: 0 clean, 1 when a regression flag fired, 2 on harness or
environment errors. Missing MinIO *variables* fail fast with the setup
commands; the endpoint itself is not probed, so an unreachable stack surfaces
only when the first S3 call fails.

## Interpreter

The lane runs on Python 3.14, the interpreter the pinned aws-cli bundles
(`aws --version` prints it). With both sides on one interpreter the E2E
differential compares two tools; boto3-s3 on the 3.10 floor against aws on
3.14 would fold the interpreter generations into every ratio. Development
stays on the floor (`.python-version`; CONTRIBUTING.md), so the lane has an
environment of its own: `scripts/bench-env.sh` points uv at `.venv-bench`,
requests 3.14 over the `.python-version` pin, and restricts the interpreter
to uv-managed builds so its build source is the one every other lane uses (a
distro `python3.14` would be a second variable). `BOTO3_S3_BENCH_PYTHON=3.12`
selects another version for a one-off run in its own `.venv-bench-3.12`. The
results meta records the exact version that ran, and a report whose baseline
ran on a different minor says so in its header: the cross-run deltas then
include the interpreter change, and only the same-run E2E ratio remains a
like-for-like number. The 2026-09-06 entry in
[benchmarks/RESULTS.md](../benchmarks/RESULTS.md) records the step itself -
one tree measured on 3.10 and 3.14 back to back - so the history stays
readable across it.

## Modes

**E2E** runs both CLIs as subprocesses against MinIO and measures wall-clock
per invocation, warmup discarded, then N rounds with the side order
alternating every round (drift cancels in the median ratio). It owns the
dedicated bucket `boto3-s3-bench` - created at run start, force-deleted on
exit (on real S3 the name gets a per-run suffix, since that namespace is
global); the e2e test suite's `boto3-s3-e2e` (contractually empty) is never
touched. Destinations are purged between invocations so MinIO's tmpfs stays
bounded. By default the endpoint must be the local MinIO stack;
`BOTO3_S3_BENCH_ALLOW_REMOTE=1` (exactly `1`) opts into a non-local endpoint,
or into real S3 with no endpoint override at all (how the EC2 lane runs,
below). A real-S3 run takes its credentials from the environment or an
instance role, never from `AWS_PROFILE`: both CLIs run with `AWS_CONFIG_FILE`
pointed at the per-engine config, which hides every profile from them, so the
harness refuses the combination up front instead of failing on the first
invocation. Each engine's work tree is removed before the next engine seeds
its own, and a scenario that hits a local I/O error (a full work tree) is
skipped and recorded like any other failed scenario.

**In-process** runs the CLI inside the runner process against stubbed S3:
a real boto3 client whose `before-send` event returns canned responses, so
request serialization, signing, and response parsing stay in the measured
path while the socket send is skipped. Every handler drains the request body,
keeping upload read/chunking cost in the measurement. What this mode times is
boto3-s3's own work - enumeration, compare, transfer orchestration, local
file I/O - deterministically (spreads are typically a few percent), which
makes it the sensitive lane for cross-run comparison. What it does not
include: process startup (imports are warm after the first invocation), the
production `build_client` path (the client is injected via `Context`), and
the network/server. Listing corpora are synthesized (100k keys costs
nothing to "seed"), so it scales past what E2E seeding affords.

The CRT engine is E2E-only: it bypasses botocore's HTTP layer, so the
`before-send` stub never sees its requests. Both modes pin
`preferred_transfer_client` through `AWS_CONFIG_FILE` (the test suite's
mechanism); `--engine both` adds a CRT pass over the transfer scenarios for
aws and boto3-s3 alike. On the CRT lane the harness passes `--endpoint-url`
explicitly to both sides - the same workaround as the e2e CRT parity tests,
because aws's CRT client reads `use_ssl` from the CLI argument only and an
env-only `AWS_ENDPOINT_URL_S3` makes it dial TLS to MinIO's http endpoint
(`AWS_IO_SOCKET_CLOSED`).

A scenario that fails (unexpected rc, verification mismatch) is skipped and
recorded - in the run summary, and under `failures` in the results file's
meta line - without discarding the completed scenarios; the run then exits 2.

## Startup adjustment (the default E2E comparison)

aws-cli v2 is a frozen binary whose startup costs hundreds of milliseconds
(measured here: ~0.3s vs our ~0.05s for `--version`). A raw wall-clock
comparison therefore flatters boto3-s3 on short scenarios and can hide a
real regression under the inherited startup advantage. The report subtracts
it by default:

- `startup_version` (`--version` both sides) tracks the dispatch floor.
- `startup_minimal` (`ls` of an empty prefix, both sides) measures the full
  pre-work constant: process start, imports, config/credential resolution,
  client build, one trivial request. This is the subtraction baseline -
  `--version` alone would miss lazily paid import cost (our `--version`
  deliberately never loads the SDK; see [imports.md](imports.md)).
- `net = median(work) - median(startup_minimal)` per side;
  `ratio = net_ours / net_aws`. Ratios and flags use net; raw medians stay in
  the table. `--no-adjust-startup` reverts to raw.
- The two startup-probe rows render `-` in the net columns (a probe has no
  net of its own; its tracking is the raw-median comparison below).
- The startup probes themselves are baseline-compared on raw medians:
  startup growth (import bloat) is its own regression class.

## Throughput and peak memory (E2E)

Two recorded axes sit next to the timing table; neither raises a flag (the
timing flags stay the regression gate).

- **Throughput.** Transfer scenarios record the bytes one invocation moves
  (`payload_bytes` in the results record); the report divides it by the *net*
  median, the same startup-adjusted figure the ratio uses, so it reads as the
  rate at which the tool moved bytes once it was ready to. On a wide link
  against real S3 this is the natural unit for the large-object rows, and it
  compares across instance types where seconds do not.
- **Peak RSS.** Every E2E invocation records its peak resident set size, per
  side. The project's "light" claim is otherwise unmeasured, and aws-cli v2 is
  a frozen bundle whose footprint is a real difference. The number is exact,
  not sampled: the CLI is started by a tiny intermediary (`python -I -S`, a
  few MiB) that forks it, times fork-to-reap, and reports the child's
  `ru_maxrss`. Linux floors a child's reported peak at the high-water mark of
  the address space it was forked from, so a CLI spawned straight from the
  boto3-laden harness could never report less than the harness's own ~65 MiB;
  the intermediary's few MiB are the actual floor. Its startup is outside the
  timed window, and the fork-to-reap edge is the exit itself, so E2E timings
  since this change are marginally tighter than the earlier `subprocess.run`
  ones - a few milliseconds off every raw median, on both sides alike, which
  the ratio does not see. Where `fork` does not exist (Windows) the RSS is
  simply absent.

## Scenarios (default scale)

| E2E scenario | Command | Scale | Engines |
|---|---|---|---|
| `startup_version` | `--version` | - | classic+crt |
| `startup_minimal` | `ls` (empty prefix) | - | classic+crt |
| `ls_recursive_10k` | `ls --recursive` | 10k keys x 1KB | classic |
| `sync_noop_10k` | `sync` (nothing to do) | 10k files x 1KB | classic |
| `sync_tiny` | `sync` of one new file | 1 x 11KB | classic+crt |
| `sync_changed_10k` | `sync` re-uploading a stale subset | 10k files x 1KB, 2k changed | classic+crt |
| `sync_delete_10k` | `sync --delete` removing remote extras | 8k kept + 2k extra keys | classic |
| `cp_upload_small_1k` / `cp_download_small_1k` | `cp --recursive` | 1000 x 4KB | classic+crt |
| `cp_upload_large` / `cp_download_large` | `cp` | 1 x 64MB (multipart; `--large-transfer-mb`) | classic+crt |
| `rm_recursive_2k` | `rm --recursive` | 2000 keys, reseeded before every invocation (warmup and each timed side) | classic |

| In-process scenario | Scale |
|---|---|
| `inproc_dispatch` (`--version`) | parse+dispatch floor |
| `inproc_ls_100k` | 100 pages x 1000 keys |
| `inproc_sync_noop_20k` | 20k local files + matching listing |
| `inproc_sync_changed_20k` | as above, 2k local files differing in size |
| `inproc_rm_recursive_20k` | 20k keys |
| `inproc_cp_upload_small_2k` | 2000 x 1B |
| `inproc_cp_upload_large` | 64MB multipart (`--large-transfer-mb`) |

Every scenario verifies its warmup output (transfer-line counts, listing
sizes, no-op emptiness) before anything is timed - a scenario that silently
did no work is skipped and recorded as a verification failure (exit 2 at the
end; completed scenarios keep their numbers) instead of contributing
fake-fast ones. `sync` no-op corpora are
seeded *after* the local tree with past mtimes, so both CLIs deterministically
judge "nothing to transfer".

The `sync` scenarios that do work stage their input deterministically before
*every* invocation, warmup included, so each one faces the same job.
`sync_tiny` is the README's one-file measurement: one new 11 KB file into an
empty prefix, startup-dominated by design - so the report treats it like the
startup probes (raw medians, cross-run flag on raw), since a ratio of two
tens-of-milliseconds nets would be noise. `sync_changed_10k` seeds the whole
corpus once at the local size and, before each invocation, rewrites the first
2k keys at a *different* size - a size mismatch forces the upload whatever the
timestamps say, and only the subset is rewritten, so the reset costs 2k puts,
not 10k; the untouched 8k compare equal (size) and older (local mtime a day
back), a no-op. `sync_delete_10k` keeps 8k matching keys and re-seeds 2k
extras under a sub-prefix no local directory matches, so `--delete` has
exactly those to remove and nothing to upload; the verify checks both counts.
The in-process `inproc_sync_changed_20k` gets the same shape from the stub:
2k local files differ in size from the pre-rendered listing.

The single-object rows measure two different things depending on the link.
Against MinIO on localhost a 64 MB object moves in a few hundredths of a
second, so `cp_upload_large` / `cp_download_large` there are the CPU cost of
multipart orchestration, not throughput - and on the CRT lane the aws side can
finish inside its own startup constant, which the report renders as `-`. For a
throughput figure the object has to be large enough to outlast startup by a
clear margin: `--large-transfer-mb` sizes both modes' large rows, and the EC2
lane defaults it to 1024 for that reason. Read the 64 MB rows as overhead
rows, not as the project's transfer speed.

## Results files and baselines

`benchmarks/results/{utc}_{mode}_{gitrev}[-dirty][.{lane}].jsonl`: line 1 is a
`meta` record (lane, git revision + dirty flag, Python/boto3/botocore/
s3transfer/awscrt versions, `aws --version` on an E2E run - the in-process
meta stores none - platform, run options), each further line one
scenario's samples per side plus the recorded execution order (the A/B
interleaving is auditable), and for E2E the per-side peak RSS samples
(`rss`, bytes) and the transfer scenarios' `payload_bytes`. Results are
host-specific timings and stay out of git.

`--baseline` accepts `last` (newest stored run of the same mode), a results
file path, or a git-revision prefix matched against stored filenames. Rows
whose workload dimensions differ from the baseline's (e.g. comparing against
a `--quick` run) are not compared.

A run belongs to a *lane*: `local` (no suffix) or
`ec2-<instance type>-ubuntu<release>` for files the EC2 launcher brought back, and `last` / a revision prefix never
cross lanes - an EC2 file downloaded onto this host is another machine's
numbers and must not become a local baseline by being newest. An explicit
path is the one way to compare across lanes, and the report header then says
so. The EC2 instance runs from a `git archive` with no `.git`, so the
launcher passes the archived commit and the lane down as environment
(`BOTO3_S3_BENCH_GIT_REV`, `BOTO3_S3_BENCH_LANE`) and the meta records them
as if `git` had answered.

Officially recorded baselines - headline numbers with their revision and
machine environment - are kept in [benchmarks/RESULTS.md](../benchmarks/RESULTS.md),
which survives the git-ignored results directory.

Flag rules (`--threshold`, default 1.10): an E2E work scenario flags when
`(net ratio now) / (net ratio baseline)` exceeds the threshold - the aws side
is the same-run control, so this survives cross-run host noise; without a
baseline the ratio itself must exceed it. Startup probes and in-process
scenarios flag on `median now / median baseline`.

## Recording a baseline on EC2

The local lane runs against MinIO on `127.0.0.1`: no round-trip latency, no
TLS, unbounded bandwidth, static keys. That is the right *differential* - host
noise hits both CLIs and cancels in the ratio - but the wrong *absolute*
picture of where `aws s3` actually runs. The EC2 lane records a baseline in a
representative environment (a quiet, fixed-performance instance against real S3
in one region) whose "instance type + AMI + region" someone else can
reproduce. It is not a per-commit lane; run it when recording a baseline for
[RESULTS.md](../benchmarks/RESULTS.md).

```
python -m benchmarks ec2 setup-iam            # once, needs an administrator
python -m benchmarks ec2 run                  # provision, measure, retrieve, terminate
python -m benchmarks ec2 run --instance-type m7g.xlarge   # a Graviton run
python -m benchmarks ec2 run --ubuntu-release 24.04       # the previous LTS
python -m benchmarks ec2 run --on-demand      # when spot capacity is not there
python -m benchmarks ec2 cleanup              # the safety net for a launcher that died
```

The launcher orchestrates; the instance does the work. `run` resolves the
instance type's architecture and memory from EC2, the current Ubuntu LTS
AMI for that architecture from Canonical's public SSM parameter, and the
AMI's root device name; hands the instance a `git archive HEAD` of the tree
over S3 (a dirty tree is refused, so a baseline is attributable to a commit);
waits for a completion marker; pulls the results into `benchmarks/results/`;
and terminates the instance. The instance provisions the same way the local
lane does - the pinned `aws`, `uv sync --locked`, the chosen Python - and
runs both modes against real S3.

- **The image is Ubuntu LTS, 26.04 by default.** The local lane develops and
  measures on Ubuntu (WSL2), so the same distribution on EC2 keeps glibc and
  the kernel generation equal across lanes and leaves hardware, network, and
  real S3 as the differences between them; the interpreter (uv-managed) and
  both CLIs (self-contained) are the same either way. `--ubuntu-release` (or
  `BOTO3_S3_BENCH_UBUNTU_RELEASE`) selects another LTS Canonical publishes in
  the region. Before measuring, the instance silences Ubuntu's background
  work - the apt timers, unattended upgrades, snapd - so nothing competes
  for CPU or network mid-run.

- **Credentials.** The launcher uses the ambient profile (`~/.aws`,
  `AWS_PROFILE`, `--profile`) for its own EC2/S3/STS calls; the instance uses
  an IAM role, never a copied key. The role's only permission is S3 on the
  `boto3-s3-bench*` bucket family (the throwaway per-run bucket and the
  per-account hand-off bucket). `setup-iam` creates that role and instance
  profile; it is the one step that needs an administrator, so it is separate
  and run once.
- **Region.** Resolved as explicit `--region`, then `AWS_REGION`, then
  `AWS_DEFAULT_REGION`, then the profile - and passed explicitly to every
  client and to both CLIs on the instance, because this project's botocore
  floor does not itself read `AWS_REGION`.
- **The instance type is the only knob for architecture.** `--instance-type`
  (or `BOTO3_S3_BENCH_INSTANCE_TYPE`) selects the machine; the architecture
  follows from it, so an x86-64 and a Graviton baseline differ by that one
  value. The default is `m7i.xlarge`: fixed-performance (never burstable -
  a `t`-family instance's CPU credits would corrupt the numbers), enough vCPU
  and RAM to keep the transfer pool, CRT's threads, and the harness from
  contending, and a work tree on tmpfs so EBS is off the measured path.
  `--python` (or `BOTO3_S3_BENCH_PYTHON`) picks the interpreter, default the
  same 3.14 as the local lane. `--large-transfer-mb` defaults to 1024 here
  (64 locally): on a 12.5 Gbps link a 64 MB object finishes inside the startup
  constant. The work tree is a tmpfs the launcher sizes for three payloads
  (upload source, download source, one download destination - the harness
  frees one engine's tree before the next seeds), checked against the memory
  EC2 reports for the instance type with 4 GiB kept back; a payload the
  instance cannot hold is refused before anything launches.
- **Spot by default.** The instance is a one-time spot request that
  terminates on interruption; the hardware is the same, a run this short is
  rarely reclaimed, and when it is the run is simply repeated. `--on-demand`
  pays the full price instead, and a spot launch that finds no capacity
  stops with that message rather than falling back on its own: paying
  on-demand is a choice.
- **Cost, and not leaking an instance.** A run is ~30-40 minutes, well under
  US$1 of instance time plus S3 request charges. The one real risk is a
  leaked instance, so it has three independent deaths: a timed `shutdown`
  armed before any work (`--max-minutes`, default 60), `terminate` as the
  shutdown behavior, and the launcher's own terminate on the way out.
- **Every ending is explained.** Provisioning runs under errexit with an ERR
  trap that names the failing line; an outside shutdown (a spot reclaim, the
  budget timer) arrives as SIGTERM and is recorded as such; and the EXIT trap
  uploads the log and a `DONE` marker through two PUT URLs the launcher
  presigned (valid past the budget), so reporting needs neither credentials
  nor a CLI on the instance and a failure before anything was provisioned
  still leaves a reason behind. The instance also syncs its log every minute,
  so a stop that leaves no time for the trap still leaves a recent record,
  and the launcher shows the log's latest line as progress while it waits.
  When the instance disappears without a marker the launcher reads EC2's stop
  reason. The last line the launcher prints is the verdict: completed, a
  benchmark exit code, the provisioning line that failed, a spot
  interruption, the budget exceeded, a service-side stop, or its own
  timeout - followed by the log tail when the run did not succeed.
- **Repeating a failed run is safe.** Everything a run creates is named by
  the run: the instance (tagged with the run id), its own bucket
  (`boto3-s3-bench-<run id>`, named by the launcher and handed to the
  harness), and the `<run id>/` prefix in the hand-off bucket. Each mode's
  results are uploaded as soon as that mode finishes, so an interruption in
  the second mode keeps the first mode's file. Whatever the ending, the
  launcher terminates the instance, removes the run's bucket if the harness
  did not, and deletes the code tarball (also when the launch itself
  failed); the log and results stay under the run prefix as the run's
  record. So the answer to a failed run is another `ec2 run`. `cleanup` is
  the safety net for a launcher that died before its own teardown: it
  terminates anything still tagged and force-deletes any `boto3-s3-bench-*`
  bucket left on real S3 (the persistent `boto3-s3-bench-boot-*` hand-off
  buckets excepted) - do not run it while another run is in flight.
- **What comes back.** The results files land in `benchmarks/results/` with
  the lane spelled in their name (`...bbbbbbbbbb.ec2-m7i.xlarge-ubuntu26.04.jsonl`),
  the archived commit as their revision, and the AMI id in the meta record;
  render them with `report`, compare two EC2 runs with `--baseline last` from
  either, and record the entry in RESULTS.md with the instance type, Ubuntu
  release and AMI, region, and Python version.
- **A clean shell.** The launcher refuses to start if the shell is configured
  for MinIO (`AWS_ENDPOINT_URL_S3` or the dev static key), because those would
  redirect its S3 calls. Run it from a shell that has not sourced
  `scripts/minio-env.sh`.

Cross-run `--baseline` comparison stays like-for-like only within one lane,
which is one instance type on one Ubuntu release; an x86-64 and a Graviton
run are two baselines to read side by side, not a regression pair, and the
lane scoping enforces that.

## Reading the numbers on this host

- Prefer the E2E ratio and the in-process medians; raw E2E medians drift
  with host load. Close the browser/IDE storms before a run you intend to
  keep as a baseline.
- Record baselines from a clean checkout (the filename carries `-dirty`
  otherwise) so a stored run is attributable to a revision.
- `--quick` validates the harness end-to-end in about a minute; its
  timings are dominated by startup and prove nothing about performance.
