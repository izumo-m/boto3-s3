# Recorded benchmark results

A curated log of officially recorded baseline runs (newest first). The raw
sample data lives in the git-ignored `benchmarks/results/` JSONL files on the
measuring host; this file preserves the headline numbers with their exact
revision and environment so they survive across hosts and cleanups. Metric
definitions (net, ratio, flags) are in [design/benchmark.md](../design/benchmark.md).

## 2026-09-06 - interpreter step: the local lane moves to Python 3.14

- Commit: `9d352af` (`feat(bench): run the local lane on Python 3.14 in its
  own environment`), clean working tree. This entry records the interpreter
  change, not a code change: the same tree was measured on both interpreters
  back to back, and the 3.14 numbers are the baseline every later run
  compares against. Every entry below this one was measured on 3.10.
- Runs, in this order within one session: on 3.10 (the development
  environment) `python -m benchmarks run --mode e2e --engine both` and
  `--mode inprocess`; then `source scripts/bench-env.sh` and the same two
  runs on 3.14 with `--baseline last`, so each 3.14 run reports against its
  3.10 twin.
- Results files: `20260906-041410_e2e_9d352af9b2.jsonl` and
  `20260906-041606_inprocess_9d352af9b2.jsonl` (3.10),
  `20260906-042305_e2e_9d352af9b2.jsonl` and
  `20260906-042445_inprocess_9d352af9b2.jsonl` (3.14).

Environment:

- Machine: Intel Core Ultra 5 225H (14 cores), 15 GiB RAM; Ubuntu 26.04 LTS
  on WSL2 (kernel 6.18.33.1-microsoft-standard-WSL2). The recorded
  specification matches the 2026-07-14 entry, but whether it is the same
  physical machine is not established, and the absolute levels here sit
  about 2x that entry's on both sides. Per "Reading the numbers", only the
  ratios and this entry's same-day pair carry meaning.
- Endpoint: MinIO `pgsty/minio:latest` in Docker, tmpfs-backed,
  `http://127.0.0.1:9000`; local trees on ext4 under /tmp.
- Python 3.10.20 (uv-managed, `.venv`) and 3.14.7 (uv-managed,
  `.venv-bench`); boto3-s3 0.11.0 / boto3-s3-cli 0.8.0; boto3/botocore
  1.43.44, s3transfer 0.19.2, awscrt 0.32.2 - identical in both environments
  (one lockfile).
- aws side: pinned aws-cli 2.36.40 (`exe/x86_64.ubuntu.26`, bundling
  Python 3.14.6), the same binary on both runs.

### E2E on Python 3.10 (medians in seconds; ratio = net ours / net aws)

Startup probes (raw): `--version` ours 0.063 / aws 0.524 (classic),
0.062 / 0.463 (crt); `startup_minimal` ours 0.405 / aws 0.765 (classic),
0.287 / 0.559 (crt).

| scenario | engine | ours raw | ours net | aws raw | aws net | ratio |
|---|---|---|---|---|---|---|
| ls_recursive_10k | classic | 0.917 | 0.512 | 2.517 | 1.752 | 0.29 |
| sync_noop_10k | classic | 0.756 | 0.351 | 2.300 | 1.535 | 0.23 |
| cp_upload_small_1k | classic | 5.679 | 5.274 | 5.990 | 5.226 | 1.01 |
| cp_download_small_1k | classic | 3.809 | 3.403 | 4.891 | 4.127 | 0.82 |
| cp_upload_large (64MB) | classic | 0.552 | 0.147 | 0.929 | 0.164 | 0.89 |
| cp_download_large (64MB) | classic | 0.468 | 0.062 | 0.885 | 0.121 | 0.52 |
| rm_recursive_2k | classic | 0.748 | 0.343 | 7.000 | 6.235 | 0.06 |
| cp_upload_small_1k | crt | 1.713 | 1.426 | 2.796 | 2.237 | 0.64 |
| cp_download_small_1k | crt | 2.082 | 1.795 | 3.425 | 2.867 | 0.63 |
| cp_upload_large (64MB) | crt | 0.498 | 0.211 | 0.741 | 0.182 | 1.16 |
| cp_download_large (64MB) | crt | 0.451 | 0.164 | 0.748 | 0.189 | 0.87 |

### E2E on Python 3.14 (same layout; "vs 3.10" is the change in ratio)

Startup probes (raw): `--version` ours 0.088 / aws 0.437 (classic),
0.149 / 0.682 (crt); `startup_minimal` ours 0.359 / aws 0.653 (classic),
0.291 / 0.548 (crt).

| scenario | engine | ours raw | ours net | aws raw | aws net | ratio | vs 3.10 |
|---|---|---|---|---|---|---|---|
| ls_recursive_10k | classic | 0.668 | 0.309 | 1.982 | 1.329 | 0.23 | -21% |
| sync_noop_10k | classic | 0.765 | 0.406 | 2.505 | 1.852 | 0.22 | -4% |
| cp_upload_small_1k | classic | 4.904 | 4.545 | 5.981 | 5.328 | 0.85 | -16% |
| cp_download_small_1k | classic | 3.396 | 3.037 | 4.725 | 4.073 | 0.75 | -10% |
| cp_upload_large (64MB) | classic | 0.537 | 0.178 | 0.894 | 0.241 | 0.74 | -17% |
| cp_download_large (64MB) | classic | 0.541 | 0.182 | 0.868 | 0.215 | 0.84 | +63% |
| rm_recursive_2k | classic | 0.635 | 0.276 | 5.199 | 4.546 | 0.06 | +10% |
| cp_upload_small_1k | crt | 1.292 | 1.001 | 2.193 | 1.646 | 0.61 | -5% |
| cp_download_small_1k | crt | 1.565 | 1.274 | 2.758 | 2.211 | 0.58 | -8% |
| cp_upload_large (64MB) | crt | 0.342 | 0.051 | 0.495 | - | - | - |
| cp_download_large (64MB) | crt | 0.337 | 0.045 | 0.501 | - | - | - |

### In-process (classic; medians in seconds, ± spread)

| scenario | 3.10 median | spread | 3.14 median | spread | change |
|---|---|---|---|---|---|
| inproc_dispatch | 0.002 | ±0.001 | 0.003 | ±0.000 | +31% |
| inproc_ls_100k | 3.808 | ±0.518 | 2.901 | ±0.348 | -24% |
| inproc_sync_noop_20k | 0.798 | ±0.055 | 0.665 | ±0.020 | -17% |
| inproc_rm_recursive_20k | 0.853 | ±0.062 | 0.704 | ±0.059 | -17% |
| inproc_cp_upload_small_2k | 3.582 | ±0.305 | 3.612 | ±0.652 | +1% |
| inproc_cp_upload_64mb | 0.408 | ±0.026 | 0.113 | ±0.004 | -72% |

Notes:

- The step: the enumeration and compare lanes gain 17-24% in-process on
  3.14 and the E2E ratios move with them (ls 0.29 -> 0.23, small-file cp
  1.01 -> 0.85 upload and 0.82 -> 0.75 download, 64MB classic upload
  0.89 -> 0.74). The largest single move is the in-process 64MB upload
  (0.408 -> 0.113); its E2E counterpart moved less because the socket send
  the stub skips dominates there. Not investigated further. Small-file cp
  in-process is flat (+1%) inside a ±0.65 s spread.
- `--version` is the one thing that got slower, and it is the stdlib: a
  paired 30-round probe after the runs put `boto3-s3 --version` at 56 ms
  min / 77 ms median on 3.10 versus 77 / 99 on 3.14, with the bare
  interpreter (`python -I -c pass`) at 12 / 18 versus 15 / 20. The added
  cost is `_colorize` (about 7 ms cumulative under `-X importtime`, half of
  it `dataclasses`): on 3.14 argparse's `HelpFormatter.__init__` imports it
  whenever a parser gains an argument, whatever the color setting, and
  `logging` reaches it too through `traceback`. Keeping `logging` off the
  path (the commit after this entry) took the `--version` median from 54
  to 53 ms on 3.14 and 43 to 41 ms on 3.10 in a paired A/B; the rest is
  argparse's and stays. `startup_minimal`, the pre-work constant a real
  command pays, went the other way (0.405 -> 0.359 classic, 0.287 -> 0.291
  crt): the dispatch floor grew by 10-20 ms, real startup did not.
- Every flag the runs raised is a measurement artifact of that floor or of
  host noise: `startup_version` classic +40% is the 20 ms above; crt +142%
  is a burst (the five 3.14 samples were 0.069, 0.071, 0.149, 0.151 and
  0.179, and aws's own `--version` in the same window spanned 0.41-0.90);
  `cp_download_large` classic +63% and `rm_recursive_2k` +10% are
  noise-floor rows (nets of 0.06-0.18 s; the rm ratio is 0.06 on both
  interpreters and the flag is a quotient of two such numbers). The CRT
  large-file rows show `-` on 3.14 because aws's raw medians (0.495 /
  0.501) fell under its own `startup_minimal` (0.548), so no net exists;
  the 1.16 flag on the 3.10 CRT upload is the same noise-floor row.
  `inproc_dispatch` +31% is 0.002 -> 0.003 s, the `--version` floor at the
  resolution limit.

## 2026-07-21 - fast timestamp parsing

- Commit: `f48ca50` (`perf(deleter): check XML key compatibility with a
  compiled regex`), clean working tree. The headline change in the window is
  `abd1533` (`feat(lib,cli): parse S3 response timestamps at C speed`).
- Runs: `python -m benchmarks run --mode e2e --engine both --baseline last`,
  then `python -m benchmarks run --mode inprocess --baseline 82b977f`.
- Results files: `20260721-143940_e2e_f48ca50dae.jsonl`,
  `20260721-144205_inprocess_f48ca50dae.jsonl`.

Environment: as the 2026-07-14 entry, except aws-cli is now the pinned
2.36.1 (`exe/x86_64.ubuntu.26`) and the package version strings are
boto3-s3 0.7.0 / boto3-s3-cli 0.5.0 (the working tree at the commit above);
boto3/botocore 1.43.44, s3transfer 0.19.0, awscrt 0.32.2, Python 3.10.20.

### E2E (medians in seconds; ratio = net ours / net aws, < 1 is ours faster)

Startup probes (raw): `--version` ours 0.045 / aws 0.358 (classic),
0.045 / 0.370 (crt); `startup_minimal` ours 0.210 / aws 0.410 (classic),
0.224 / 0.440 (crt).

| scenario | engine | ours raw | ours net | aws raw | aws net | ratio |
|---|---|---|---|---|---|---|
| ls_recursive_10k | classic | 0.610 | 0.401 | 1.752 | 1.342 | 0.30 |
| sync_noop_10k | classic | 0.607 | 0.397 | 1.823 | 1.413 | 0.28 |
| cp_upload_small_1k | classic | 4.206 | 3.996 | 4.751 | 4.341 | 0.92 |
| cp_download_small_1k | classic | 3.035 | 2.825 | 3.589 | 3.179 | 0.89 |
| cp_upload_large (64MB) | classic | 0.550 | 0.341 | 0.953 | 0.543 | 0.63 |
| cp_download_large (64MB) | classic | 0.532 | 0.322 | 0.905 | 0.496 | 0.65 |
| rm_recursive_2k | classic | 0.616 | 0.406 | 5.766 | 5.356 | 0.08 |
| cp_upload_small_1k | crt | 1.249 | 1.025 | 2.039 | 1.599 | 0.64 |
| cp_download_small_1k | crt | 1.609 | 1.385 | 2.703 | 2.263 | 0.61 |
| cp_upload_large (64MB) | crt | 0.389 | 0.165 | 0.616 | 0.176 | 0.94 |
| cp_download_large (64MB) | crt | 0.333 | 0.109 | 0.542 | 0.102 | 1.06 |

### In-process (classic; medians in seconds, ± spread)

| scenario | median | spread | vs 2026-07-14 |
|---|---|---|---|
| inproc_dispatch | 0.002 | ±0.000 | -26% |
| inproc_ls_100k | 1.663 | ±0.032 | -66% |
| inproc_sync_noop_20k | 0.513 | ±0.064 | -57% |
| inproc_rm_recursive_20k | 0.619 | ±0.027 | -47% |
| inproc_cp_upload_small_2k | 2.616 | ±0.072 | +31% |
| inproc_cp_upload_64mb | 0.304 | ±0.029 | +47% |

Notes:

- The listing gains are the fast ISO 8601 timestamp parser (`abd1533`):
  the aws ratio on listing-heavy scenarios halved (ls 0.62 -> 0.30,
  sync_noop 0.58 -> 0.28) because aws-cli's bundled botocore still parses
  every `LastModified` through dateutil.
- The cp rows' higher absolute levels versus 2026-07-14 are host drift, not
  a code regression: the aws control moved in lockstep (E2E
  cp_upload_large raw: ours +58% / aws +54%; small-file cp: both ~+15%),
  and a 12-round in-process A/B of the tuned session versus a plain one
  read 0.997 on cp. The paired ratios are the trustworthy signal.
- The run's two flags are measurement artifacts of that drift:
  `startup_version` classic compares raw startup across hosts-states a week
  apart, and CRT `cp_download_large` nets (~0.06-0.11s) sit at the harness
  noise floor - the ratio reads 0.90 (2026-07-14), then 1.06 and 1.18 on two
  runs today: parity within noise, in a lane this cycle's changes do not
  touch (the CRT data plane bypasses the response parser).

- Commit: `82b977f` (`perf(crt): reuse the caller's session for CRT request
  serialization`), clean working tree.
- Run: `python -m benchmarks run --engine both`, default samples
  (5 per side E2E / 3 for rm / 10 in-process). No failures, no flags.
- Results files: `20260714-115742_inprocess_82b977fac0.jsonl`,
  `20260714-120254_e2e_82b977fac0.jsonl`.

Environment:

- Machine: Intel Core Ultra 5 225H (14 cores), 15 GiB RAM
- OS: Ubuntu 26.04 LTS on WSL2 (kernel 6.18.33.1-microsoft-standard-WSL2)
- Endpoint: MinIO `pgsty/minio:latest` in Docker, tmpfs-backed,
  `http://127.0.0.1:9000`; local trees on ext4 under /tmp
- Python 3.10.20; boto3-s3 0.6.0-dev / boto3-s3-cli 0.5.0-dev (the working
  tree at the commit above); boto3/botocore 1.43.44, s3transfer 0.19.0,
  awscrt 0.32.2
- aws side: pinned aws-cli 2.35.18 (`exe/x86_64.ubuntu.26`)

### E2E (medians in seconds; ratio = net ours / net aws, < 1 is ours faster)

Startup probes (raw): `--version` ours 0.038 / aws 0.291 (classic),
0.044 / 0.327 (crt); `startup_minimal` ours 0.196 / aws 0.403 (classic),
0.213 / 0.424 (crt).

| scenario | engine | ours raw | ours net | aws raw | aws net | ratio |
|---|---|---|---|---|---|---|
| ls_recursive_10k | classic | 0.882 | 0.686 | 1.513 | 1.110 | 0.62 |
| sync_noop_10k | classic | 0.933 | 0.737 | 1.676 | 1.273 | 0.58 |
| cp_upload_small_1k | classic | 3.693 | 3.497 | 4.144 | 3.742 | 0.93 |
| cp_download_small_1k | classic | 2.550 | 2.354 | 3.123 | 2.721 | 0.87 |
| cp_upload_large (64MB) | classic | 0.348 | 0.152 | 0.620 | 0.218 | 0.70 |
| cp_download_large (64MB) | classic | 0.318 | 0.122 | 0.580 | 0.177 | 0.69 |
| rm_recursive_2k | classic | 0.521 | 0.325 | 4.511 | 4.108 | 0.08 |
| cp_upload_small_1k | crt | 1.158 | 0.945 | 1.814 | 1.390 | 0.68 |
| cp_download_small_1k | crt | 1.499 | 1.287 | 2.248 | 1.825 | 0.71 |
| cp_upload_large (64MB) | crt | 0.339 | 0.126 | 0.549 | 0.125 | 1.01 |
| cp_download_large (64MB) | crt | 0.301 | 0.088 | 0.522 | 0.098 | 0.90 |

### In-process (classic; medians in seconds, ± spread)

| scenario | median | spread |
|---|---|---|
| inproc_dispatch | 0.002 | ±0.000 |
| inproc_ls_100k | 4.948 | ±0.135 |
| inproc_sync_noop_20k | 1.186 | ±0.024 |
| inproc_rm_recursive_20k | 1.176 | ±0.028 |
| inproc_cp_upload_small_2k | 2.003 | ±0.037 |
| inproc_cp_upload_64mb | 0.206 | ±0.009 |

Notes:

- Recorded immediately after the CRT session-reuse fix in the same commit;
  the pre-fix measurement (dirty tree on `64a5a84`) had the CRT large-file
  ratios at 1.41 (upload) / 1.12 (download), which that fix brought to
  1.01 / 0.90 here. Every scenario now meets the "equal to or better than
  aws s3" goal within noise.
- In-process absolute levels on this host vary by tens of percent between
  runs under different host load; within-run spreads are the stable signal
  (design/benchmark.md "Reading the numbers").
