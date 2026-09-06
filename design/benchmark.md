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
`BOTO3_S3_BENCH_ALLOW_REMOTE=1` opts into a non-local endpoint, or into real S3
with no endpoint override at all (how the EC2 lane runs, below).

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

## Scenarios (v1, default scale)

| E2E scenario | Command | Scale | Engines |
|---|---|---|---|
| `startup_version` | `--version` | - | classic+crt |
| `startup_minimal` | `ls` (empty prefix) | - | classic+crt |
| `ls_recursive_10k` | `ls --recursive` | 10k keys x 1KB | classic |
| `sync_noop_10k` | `sync` (nothing to do) | 10k files x 1KB | classic |
| `cp_upload_small_1k` / `cp_download_small_1k` | `cp --recursive` | 1000 x 4KB | classic+crt |
| `cp_upload_large` / `cp_download_large` | `cp` | 1 x 64MB (multipart) | classic+crt |
| `rm_recursive_2k` | `rm --recursive` | 2000 keys, reseeded before every invocation (warmup and each timed side) | classic |

| In-process scenario | Scale |
|---|---|
| `inproc_dispatch` (`--version`) | parse+dispatch floor |
| `inproc_ls_100k` | 100 pages x 1000 keys |
| `inproc_sync_noop_20k` | 20k local files + matching listing |
| `inproc_rm_recursive_20k` | 20k keys |
| `inproc_cp_upload_small_2k` | 2000 x 1B |
| `inproc_cp_upload_64mb` | 64MB multipart |

Every scenario verifies its warmup output (transfer-line counts, listing
sizes, no-op emptiness) before anything is timed - a scenario that silently
did no work is skipped and recorded as a verification failure (exit 2 at the
end; completed scenarios keep their numbers) instead of contributing
fake-fast ones. `sync` no-op corpora are
seeded *after* the local tree with past mtimes, so both CLIs deterministically
judge "nothing to transfer".

## Results files and baselines

`benchmarks/results/{utc}_{mode}_{gitrev}[-dirty].jsonl`: line 1 is a `meta`
record (git revision + dirty flag, Python/boto3/botocore/s3transfer/awscrt
versions, `aws --version` on an E2E run - the in-process meta stores none -
platform, run options), each further line one
scenario's samples per side plus the recorded execution order (the A/B
interleaving is auditable). Results are host-specific timings and stay out
of git.

`--baseline` accepts `last` (newest stored run of the same mode), a results
file path, or a git-revision prefix matched against stored filenames. Rows
whose workload dimensions differ from the baseline's (e.g. comparing against
a `--quick` run) are not compared.

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
python -m benchmarks ec2 cleanup              # terminate anything a crash left tagged
```

The launcher orchestrates; the instance does the work. `run` resolves the
instance type's architecture from EC2 and the matching latest Amazon Linux
2023 AMI from its public SSM parameter, hands the instance a `git archive HEAD`
of the tree over S3 (a dirty tree is refused, so a baseline is attributable to
a commit), waits for a completion marker, pulls the results into
`benchmarks/results/`, and terminates the instance. The instance provisions the
same way the local lane does - the pinned `aws`, `uv sync --locked`, the
chosen Python - and runs both modes against real S3.

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
  same 3.14 as the local lane.
- **Cost, and not leaking an instance.** A run is ~30-40 minutes, well under
  US$1 of on-demand instance time plus S3 request charges. The one real risk
  is a leaked instance, so it has three independent deaths: a timed `shutdown`
  armed before any work (`--max-minutes`, default 60), `terminate` as the
  shutdown behavior, and the launcher's own terminate on the way out. `cleanup`
  terminates anything a crashed launcher left tagged.
- **A clean shell.** The launcher refuses to start if the shell is configured
  for MinIO (`AWS_ENDPOINT_URL_S3` or the dev static key), because those would
  redirect its S3 calls. Run it from a shell that has not sourced
  `scripts/minio-env.sh`.

Record the results the same way as the local lane (an entry in RESULTS.md with
the instance type, AMI, region, and Python version in its environment block).
Cross-run `--baseline` comparison stays like-for-like only within one instance
type and architecture; treat an x86-64 and a Graviton run as two baselines to
read side by side, not as a regression pair.

## Reading the numbers on this host

- Prefer the E2E ratio and the in-process medians; raw E2E medians drift
  with host load. Close the browser/IDE storms before a run you intend to
  keep as a baseline.
- Record baselines from a clean checkout (the filename carries `-dirty`
  otherwise) so a stored run is attributable to a revision.
- `--quick` validates the harness end-to-end in about a minute; its
  timings are dominated by startup and prove nothing about performance.
