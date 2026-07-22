"""The benchmark runner's command line: ``run``, ``report``, and ``list``.

Entry point for ``uv run python -m benchmarks``. ``run`` executes the
selected modes, stores one JSONL results file per mode, prints the
comparison tables, and exits 1 when a regression flag fires (0 otherwise,
2 on harness/environment errors) - the same contract ``report`` follows
when re-rendering stored files.
"""

from __future__ import annotations

import argparse
import sys
from fnmatch import fnmatch
from pathlib import Path

from benchmarks import e2e, inprocess, report, results
from benchmarks.core import BenchmarkError
from benchmarks.report import STARTUP_PROBES


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks",
        description="boto3-s3 performance benchmarks (see docs/benchmark.md).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run benchmarks and store a results file per mode")
    run.add_argument("--mode", choices=["e2e", "inprocess", "all"], default="all")
    run.add_argument(
        "--scenario",
        metavar="GLOB",
        help="only scenarios matching this glob (E2E startup probes always run)",
    )
    run.add_argument("--engine", choices=["classic", "crt", "both"], default="classic")
    run.add_argument("--samples", type=int, help="override every scenario's sample count")
    run.add_argument(
        "--quick", action="store_true", help="~1/100 workload sizes: a harness smoke test"
    )
    run.add_argument(
        "--baseline",
        metavar="LAST|PATH|REV",
        help="compare against a stored run: 'last', a results file, or a git-rev prefix",
    )
    run.add_argument("--threshold", type=float, default=1.10)
    run.add_argument(
        "--no-adjust-startup",
        action="store_true",
        help="report raw wall-clock instead of startup-adjusted net times",
    )
    # Regression-flag self-test only: inflate every boto3-s3 sample by this
    # many milliseconds and check that the flag fires.
    run.add_argument("--self-test-handicap-ms", type=float, default=0.0, help=argparse.SUPPRESS)

    rep = sub.add_parser("report", help="re-render stored results files")
    rep.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="results files (default: the newest stored run of each mode)",
    )
    rep.add_argument("--baseline", metavar="LAST|PATH|REV")
    rep.add_argument("--threshold", type=float, default=1.10)
    rep.add_argument("--no-adjust-startup", action="store_true")

    sub.add_parser("list", help="list scenarios and stored results files")
    return parser


def _filter_names(names: list[str], pattern: str | None, *, keep: tuple[str, ...] = ()) -> set[str]:
    if pattern is None:
        return set(names)
    return {name for name in names if fnmatch(name, pattern) or name in keep}


def _report_one(path: Path, baseline_spec: str | None, *, adjust: bool, threshold: float) -> bool:
    current = results.load_run(path)
    mode = str(current[0].get("mode"))
    baseline = None
    if baseline_spec is not None:
        # An unresolvable baseline (e.g. --baseline last on the first run of a
        # mode) degrades to a baseline-less table; the measurements themselves
        # must never be discarded over it.
        try:
            baseline_path = results.resolve_baseline(baseline_spec, mode, exclude=path)
        except BenchmarkError as exc:
            _log(f"[{mode}] baseline unavailable: {exc}")
        else:
            baseline = results.load_run(baseline_path)
    text, flagged = report.render(current, baseline, adjust=adjust, threshold=threshold)
    print(text)
    print()
    return flagged


def _cmd_run(args: argparse.Namespace) -> int:
    modes = ["inprocess", "e2e"] if args.mode == "all" else [args.mode]
    engines = ("classic", "crt") if args.engine == "both" else (args.engine,)
    handicap = args.self_test_handicap_ms / 1000.0
    aws_ver: str | None = None
    if "e2e" in modes:
        # Fail fast on a missing MinIO stack before spending in-process time.
        _ours_exe, aws_exe = e2e.check_environment()
        aws_ver = e2e.aws_version(aws_exe)

    options: dict[str, object] = {
        "engines": list(engines),
        "samples": args.samples,
        "quick": args.quick,
        "scenario": args.scenario,
        "handicap_ms": args.self_test_handicap_ms,
    }

    written: list[Path] = []
    all_failures: list[str] = []
    any_flag = False
    for mode in modes:
        if mode == "inprocess":
            scenarios = inprocess.build_scenarios(args.quick)
            names = _filter_names([s.name for s in scenarios], args.scenario)
            selected = [s for s in scenarios if s.name in names]
            if not selected:
                _log(f"[inprocess] no scenario matches {args.scenario!r}; skipping mode")
                continue
            mode_results, failures = inprocess.run_scenarios(
                selected, samples_override=args.samples, handicap=handicap, log=_log
            )
            meta = results.collect_meta("inprocess", {**options, "failures": failures})
        else:
            scenarios = e2e.build_scenarios(args.quick)
            names = _filter_names([s.name for s in scenarios], args.scenario, keep=STARTUP_PROBES)
            selected = [s for s in scenarios if s.name in names]
            if all(s.name in STARTUP_PROBES for s in selected):
                _log(f"[e2e] no work scenario matches {args.scenario!r}; skipping mode")
                continue
            mode_results, failures = e2e.run_scenarios(
                selected,
                engines=engines,
                samples_override=args.samples,
                handicap=handicap,
                log=_log,
            )
            meta = results.collect_meta(
                "e2e", {**options, "failures": failures}, aws_version=aws_ver
            )
        all_failures.extend(failures)
        path = results.write_run(meta, mode_results)
        written.append(path)
        _log(f"[{mode}] results written to {path}")

    print()
    for path in written:
        flagged = _report_one(
            path,
            args.baseline,
            adjust=not args.no_adjust_startup,
            threshold=args.threshold,
        )
        any_flag = any_flag or flagged
    if all_failures:
        # Failed scenarios are absent from the tables above; repeat them last
        # so a long run cannot scroll a failure out of sight.
        _log(f"{len(all_failures)} scenario(s) failed and were skipped:")
        for failure in all_failures:
            _log(f"  {failure.splitlines()[0]}")
        return 2
    return 1 if any_flag else 0


def _cmd_report(args: argparse.Namespace) -> int:
    files = list(args.files)
    if not files:
        files = [runs[-1] for mode in ("inprocess", "e2e") if (runs := results.list_runs(mode))]
        if not files:
            raise BenchmarkError("no stored results files (run `python -m benchmarks run` first)")
    any_flag = False
    for path in files:
        flagged = _report_one(
            path,
            args.baseline,
            adjust=not args.no_adjust_startup,
            threshold=args.threshold,
        )
        any_flag = any_flag or flagged
    return 1 if any_flag else 0


def _cmd_list() -> int:
    print("e2e scenarios (default scale):")
    for scenario in e2e.build_scenarios(False):
        # Mirror the run filter: the startup probes ride every lane by name.
        crt = scenario.crt_capable or scenario.name.startswith("startup_")
        engines = "classic+crt" if crt else "classic"
        dims = ", ".join(f"{k}={v}" for k, v in scenario.dimensions.items()) or "-"
        print(f"  {scenario.name:24} [{engines}] {dims}")
    print("inprocess scenarios (default scale):")
    for scenario in inprocess.build_scenarios(False):
        dims = ", ".join(f"{k}={v}" for k, v in scenario.dimensions.items()) or "-"
        print(f"  {scenario.name:24} [classic] {dims}")
    runs = results.list_runs()
    if runs:
        print("stored results (newest last):")
        for path in runs[-10:]:
            print(f"  {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "report":
            return _cmd_report(args)
        return _cmd_list()
    except BenchmarkError as exc:
        print(f"benchmarks: error: {exc}", file=sys.stderr)
        return 2
