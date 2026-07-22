"""In-process benchmark mode: the CLI runs in this process against stubbed S3.

Network and server time are out of the measured path entirely (see
`stubs.py`); what remains is boto3-s3's own work - enumeration, compare,
transfer orchestration - plus botocore's per-request serialization, signing,
and parsing, and the local filesystem I/O the scenarios deliberately keep
(reading upload sources, walking sync trees). Imports are warm after the
first invocation, so process startup is not part of these numbers; the E2E
mode covers that separately.

CRT scenarios cannot run here: the CRT engine bypasses botocore's HTTP layer,
so the ``before-send`` stub never sees its requests. The engine is pinned to
classic via ``AWS_CONFIG_FILE``.
"""

from __future__ import annotations

import contextlib
import gc
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks import stubs, workload
from benchmarks.core import BenchmarkError, ScenarioResult, Side, VerificationError

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping

    from boto3_s3_cli.commands.base import Context
    from tests.utils.harness import CliResult

# A name no real endpoint resolves; the responder answers before any connect.
BUCKET = "bench-stub"
_ENDPOINT = "http://bench-stub.invalid:9"

_MB = 1024 * 1024


@dataclass(frozen=True)
class Prepared:
    """One scenario, ready to time: fixed argv, injected context, warmup check."""

    argv: list[str]
    ctx: Context | None
    verify: Callable[[CliResult], None]


@dataclass(frozen=True)
class InProcScenario:
    name: str
    dimensions: Mapping[str, str]
    prepare: Callable[[Path], Prepared]
    samples: int = 10
    warmups: int = 2


@contextlib.contextmanager
def pinned_environment(workdir: Path) -> Generator[None]:
    """Deterministic process env for the in-process runs, restored on exit.

    Mirrors the test suite's ``_moto_isolation``: static fake credentials, a
    fixed region, an ``AWS_CONFIG_FILE`` pinning the classic engine, and no
    profile/endpoint leakage from the host shell (the MinIO env may or may
    not be sourced). Restoring matters because an ``--mode all`` run executes
    the E2E mode in this same process and needs the host env back.
    """
    config = workdir / "aws-config"
    config.write_text("[default]\ns3 =\n  preferred_transfer_client = classic\n")
    overrides = {
        "AWS_ACCESS_KEY_ID": "bench",
        "AWS_SECRET_ACCESS_KEY": "bench",
        "AWS_SESSION_TOKEN": "bench",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "AWS_CONFIG_FILE": str(config),
        "AWS_SHARED_CREDENTIALS_FILE": str(workdir / "credentials-absent"),
        "AWS_CLI_AUTO_PROMPT": "off",
    }
    removals = ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3")
    saved: dict[str, str | None] = {}
    for key, value in overrides.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    for key in removals:
        saved[key] = os.environ.get(key)
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _make_context(responder: stubs.S3Responder) -> Context:
    """A CLI `Context` whose client factory yields responder-backed clients.

    One session is shared (its loader cache keeps repeat client builds cheap,
    as in any long-lived process); a fresh client is built per CLI invocation,
    like the production ``build_client`` path the injection replaces. The
    session is the library's tuned `boto3_s3.session()` - the production CLI
    installs the same fast timestamp parser on every session it builds, so
    the injected context must measure the same configuration.
    """
    import boto3_s3
    from boto3_s3_cli.commands.base import Context

    session = boto3_s3.session()

    def factory(_args: Any) -> Any:
        client = session.client(
            "s3",
            region_name="us-east-1",
            endpoint_url=_ENDPOINT,
            aws_access_key_id="bench",
            aws_secret_access_key="bench",
        )
        responder.register(client)
        return client

    return Context(client_factory=factory)


def _require_rc(result: CliResult, name: str, stage: str) -> None:
    if result.rc != 0:
        raise BenchmarkError(f"[{name}] {stage} exited rc={result.rc}\nstderr:\n{result.stderr}")


def _scale(value: int, quick: bool, *, minimum: int = 10) -> int:
    return value if not quick else max(value // 100, minimum)


def build_scenarios(quick: bool) -> list[InProcScenario]:
    """The v1 in-process scenario set (sizes divided by ~100 under --quick)."""
    from tests.utils.harness import normalize_cp_stdout

    scenarios: list[InProcScenario] = []

    def dispatch_prepare(_workdir: Path) -> Prepared:
        def verify(result: CliResult) -> None:
            if not result.stdout.strip():
                raise VerificationError("[inproc_dispatch] --version printed nothing")

        return Prepared(argv=["--version"], ctx=None, verify=verify)

    scenarios.append(
        InProcScenario(name="inproc_dispatch", dimensions={}, prepare=dispatch_prepare)
    )

    ls_count = _scale(100_000, quick)

    def ls_prepare(_workdir: Path) -> Prepared:
        corpus = stubs.ListingCorpus(bucket=BUCKET, prefix="corpus/", count=ls_count, size=16)
        ctx = _make_context(stubs.S3Responder(corpus=corpus))

        def verify(result: CliResult) -> None:
            lines = len(result.stdout.splitlines())
            if lines != ls_count:
                raise VerificationError(
                    f"[inproc_ls] expected {ls_count} listing lines, got {lines}"
                )

        return Prepared(
            argv=["ls", "--recursive", f"s3://{BUCKET}/corpus/"], ctx=ctx, verify=verify
        )

    scenarios.append(
        InProcScenario(
            name="inproc_ls_100k",
            dimensions={"key_count": str(ls_count)},
            prepare=ls_prepare,
        )
    )

    sync_count = _scale(20_000, quick)

    def sync_prepare(workdir: Path) -> Prepared:
        tree = workdir / "tree"
        workload.generate_tree(tree, sync_count, 16)
        corpus = stubs.ListingCorpus(bucket=BUCKET, prefix="corpus/", count=sync_count, size=16)
        ctx = _make_context(stubs.S3Responder(corpus=corpus))

        def verify(result: CliResult) -> None:
            transfers = normalize_cp_stdout(result.stdout, bucket=BUCKET)
            if transfers:
                raise VerificationError(
                    f"[inproc_sync_noop] expected no transfers, got {len(transfers)} "
                    f"(first: {transfers[0]})"
                )

        return Prepared(argv=["sync", str(tree), f"s3://{BUCKET}/corpus/"], ctx=ctx, verify=verify)

    scenarios.append(
        InProcScenario(
            name="inproc_sync_noop_20k",
            dimensions={"file_count": str(sync_count), "file_size": "16B"},
            prepare=sync_prepare,
        )
    )

    rm_count = _scale(20_000, quick)

    def rm_prepare(_workdir: Path) -> Prepared:
        corpus = stubs.ListingCorpus(bucket=BUCKET, prefix="corpus/", count=rm_count, size=16)
        ctx = _make_context(stubs.S3Responder(corpus=corpus))

        def verify(result: CliResult) -> None:
            lines = [line for line in result.stdout.splitlines() if line.startswith("delete:")]
            if len(lines) != rm_count:
                raise VerificationError(
                    f"[inproc_rm] expected {rm_count} delete lines, got {len(lines)}"
                )

        return Prepared(
            argv=["rm", "--recursive", f"s3://{BUCKET}/corpus/"], ctx=ctx, verify=verify
        )

    scenarios.append(
        InProcScenario(
            name="inproc_rm_recursive_20k",
            dimensions={"key_count": str(rm_count)},
            prepare=rm_prepare,
        )
    )

    cp_count = _scale(2_000, quick)

    def cp_small_prepare(workdir: Path) -> Prepared:
        tree = workdir / "tree"
        workload.generate_tree(tree, cp_count, 1)
        ctx = _make_context(stubs.S3Responder())

        def verify(result: CliResult) -> None:
            transfers = normalize_cp_stdout(result.stdout, bucket=BUCKET)
            if len(transfers) != cp_count:
                raise VerificationError(
                    f"[inproc_cp_small] expected {cp_count} upload lines, got {len(transfers)}"
                )

        return Prepared(
            argv=["cp", "--recursive", str(tree), f"s3://{BUCKET}/up/"], ctx=ctx, verify=verify
        )

    scenarios.append(
        InProcScenario(
            name="inproc_cp_upload_small_2k",
            dimensions={"file_count": str(cp_count), "file_size": "1B"},
            prepare=cp_small_prepare,
        )
    )

    # --quick still uses a just-past-threshold size (not /100) so the smoke
    # run exercises the same multipart path as the real one.
    big_size = 64 * _MB if not quick else 9 * _MB

    def cp_big_prepare(workdir: Path) -> Prepared:
        big = workdir / "big.bin"
        workload.generate_file(big, big_size)
        ctx = _make_context(stubs.S3Responder())

        def verify(result: CliResult) -> None:
            transfers = normalize_cp_stdout(result.stdout, bucket=BUCKET)
            if len(transfers) != 1:
                raise VerificationError(
                    f"[inproc_cp_big] expected 1 upload line, got {len(transfers)}"
                )

        return Prepared(argv=["cp", str(big), f"s3://{BUCKET}/up/big.bin"], ctx=ctx, verify=verify)

    scenarios.append(
        InProcScenario(
            name="inproc_cp_upload_64mb",
            dimensions={"file_size": f"{big_size // _MB}MB"},
            prepare=cp_big_prepare,
        )
    )

    return scenarios


def _run_scenario(
    scenario: InProcScenario,
    workroot: Path,
    *,
    samples_override: int | None,
    handicap: float,
    log: Callable[[str], None],
) -> ScenarioResult:
    from tests.utils.harness import run_cli_in_process

    log(f"[inprocess] {scenario.name}: preparing")
    scenario_dir = workroot / scenario.name
    scenario_dir.mkdir(parents=True, exist_ok=True)
    prepared = scenario.prepare(scenario_dir)
    for i in range(scenario.warmups):
        result = run_cli_in_process(prepared.argv, ctx=prepared.ctx)
        _require_rc(result, scenario.name, f"warmup {i}")
        if i == 0:
            prepared.verify(result)
    n = samples_override if samples_override is not None else scenario.samples
    log(f"[inprocess] {scenario.name}: timing {n} samples")
    samples: list[float] = []
    for _ in range(n):
        gc.collect()
        start = time.perf_counter()
        result = run_cli_in_process(prepared.argv, ctx=prepared.ctx)
        elapsed = time.perf_counter() - start
        _require_rc(result, scenario.name, "timed sample")
        samples.append(elapsed + handicap)
    return ScenarioResult(
        scenario=scenario.name,
        mode="inprocess",
        engine="classic",
        dimensions=dict(scenario.dimensions),
        samples={Side.OURS.value: samples},
        order=[],
    )


def run_scenarios(
    scenarios: list[InProcScenario],
    *,
    samples_override: int | None = None,
    handicap: float = 0.0,
    log: Callable[[str], None],
) -> tuple[list[ScenarioResult], list[str]]:
    """Time every scenario: warmups (first one verified), then gc-fenced samples.

    GC runs collected before each sample but stays enabled during it -
    allocation pressure is part of real-world behavior. *handicap* (seconds)
    is added to every sample; it exists only for the regression-flag
    self-test (`--self-test-handicap-ms`).

    Returns ``(results, failures)``: a failing scenario is logged and skipped
    so the rest of the run's measurements survive (same contract as the E2E
    runner).
    """
    workroot = Path(tempfile.mkdtemp(prefix="boto3-s3-bench-inproc-"))
    results: list[ScenarioResult] = []
    failures: list[str] = []
    try:
        with pinned_environment(workroot):
            for scenario in scenarios:
                try:
                    results.append(
                        _run_scenario(
                            scenario,
                            workroot,
                            samples_override=samples_override,
                            handicap=handicap,
                            log=log,
                        )
                    )
                except BenchmarkError as exc:
                    message = f"[inprocess] {scenario.name}: {exc}"
                    log(f"{message}\n[inprocess] {scenario.name}: scenario skipped")
                    failures.append(message)
    finally:
        shutil.rmtree(workroot, ignore_errors=True)
    return results, failures
