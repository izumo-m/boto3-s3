# Recorded benchmark results

A curated log of officially recorded baseline runs (newest first). The raw
sample data lives in the git-ignored `benchmarks/results/` JSONL files on the
measuring host; this file preserves the headline numbers with their exact
revision and environment so they survive across hosts and cleanups. Metric
definitions (net, ratio, flags) are in [docs/benchmark.md](../docs/benchmark.md).

## 2026-07-14 - first official baseline

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
  (docs/benchmark.md "Reading the numbers").
