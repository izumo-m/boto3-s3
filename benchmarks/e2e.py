"""E2E benchmark mode: both CLIs run as subprocesses against a local S3 endpoint.

This is the only mode where the pinned ``aws`` binary can be compared
against ``boto3-s3`` - both are timed as whole processes (startup included;
the report subtracts the ``startup_minimal`` probe, see docs/benchmark.md)
against the same MinIO endpoint, interleaved A/B so host drift cancels in
the ratio.

The mode requires the standard dev stack (``scripts/compose-up.sh``,
``scripts/install-awscli.sh``, ``source scripts/minio-env.sh``) and owns the
dedicated bucket `BUCKET`, created at run start and force-deleted at exit;
the e2e test suite's bucket (``boto3-s3-e2e``, contractually empty) is never
touched.
"""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks import workload
from benchmarks.core import BenchmarkError, ScenarioResult, Side, VerificationError, round_order

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from tests.utils.harness import CliResult

BUCKET = "boto3-s3-bench"

ENGINES = ("classic", "crt")

_SUBPROCESS_TIMEOUT = 300.0

_MB = 1024 * 1024

_SETUP_HINT = (
    "the E2E mode needs the MinIO stack, the pinned aws binary, and its env:\n"
    "  scripts/compose-up.sh\n"
    "  scripts/install-awscli.sh\n"
    "  source scripts/minio-env.sh\n"
    "then rerun via `uv run python -m benchmarks run`"
)

# Set BOTO3_S3_BENCH_ALLOW_REMOTE=1 to accept a non-local endpoint. Off by
# default: the harness creates/purges its bucket and moves hundreds of MB,
# which must not silently land on (and bill against) real AWS.
_ALLOW_REMOTE_ENV = "BOTO3_S3_BENCH_ALLOW_REMOTE"

_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclass
class E2EEnv:
    """Everything a scenario callback needs for one engine's run."""

    client: Any
    bucket: str
    engine: str
    workdir: Path
    ours_exe: str
    aws_exe: str
    overlay: dict[str, str]
    counter: Iterator[int] = field(default_factory=itertools.count)

    def unique(self, side: Side) -> str:
        """A per-invocation token for write destinations that must not collide."""
        return f"{side.value}-{next(self.counter):04d}"

    def s3_prefix(self, scenario: str) -> str:
        return f"{self.engine}/{scenario}/"

    def s3_url(self, key_or_prefix: str) -> str:
        return f"s3://{self.bucket}/{key_or_prefix}"

    def dir_for(self, scenario: str) -> Path:
        path = self.workdir / scenario
        path.mkdir(parents=True, exist_ok=True)
        return path


@dataclass(frozen=True, kw_only=True)
class E2EScenario:
    """One timed command shape.

    `setup` runs once per engine; `reset` runs before *every* invocation
    (warmup included) and is where destructive scenarios re-seed and write
    scenarios purge the previous invocation's output, keeping MinIO's
    tmpfs-backed storage bounded. `verify` runs on both warmup results only -
    it guards against a run that silently did no work, which would otherwise
    record fake-fast timings. `top_level` marks argv for the program itself
    (``--version``), where aws takes no ``s3`` token.
    """

    name: str
    dimensions: Mapping[str, str]
    make_argv: Callable[[E2EEnv, Side], list[str]]
    setup: Callable[[E2EEnv], None] | None = None
    reset: Callable[[E2EEnv], None] | None = None
    verify: Callable[[E2EEnv, Side, CliResult], None] | None = None
    samples: int = 5
    crt_capable: bool = False
    top_level: bool = False
    ok_rcs: frozenset[int] = frozenset({0})


def check_environment() -> tuple[str, str]:
    """Resolve both executables and validate the endpoint env, or raise with guidance."""
    missing = [
        name
        for name in ("AWS_ENDPOINT_URL_S3", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        raise BenchmarkError(f"missing environment: {', '.join(missing)}; {_SETUP_HINT}")
    host = urllib.parse.urlsplit(os.environ["AWS_ENDPOINT_URL_S3"]).hostname
    if host not in _LOCAL_HOSTS and not os.environ.get(_ALLOW_REMOTE_ENV):
        raise BenchmarkError(
            f"AWS_ENDPOINT_URL_S3 points at non-local host {host!r}; benchmarks create and "
            f"purge bucket {BUCKET!r} and transfer hundreds of MB. Set {_ALLOW_REMOTE_ENV}=1 "
            "if that endpoint really is the intended target."
        )
    ours = shutil.which("boto3-s3")
    if ours is None:
        raise BenchmarkError(
            "boto3-s3 console script not on PATH; run via `uv run python -m benchmarks run`"
        )
    aws = shutil.which("aws")
    if aws is None:
        raise BenchmarkError(f"aws binary not on PATH; {_SETUP_HINT}")
    return ours, aws


def aws_version(aws_exe: str) -> str:
    proc = subprocess.run([aws_exe, "--version"], capture_output=True, text=True, timeout=30.0)
    return (proc.stdout or proc.stderr).strip()


def _build_client() -> Any:
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["AWS_ENDPOINT_URL_S3"],
        region_name=os.environ.get("AWS_REGION") or "us-east-1",
    )


def _write_engine_config(workroot: Path, engine: str) -> Path:
    """The per-engine AWS config both CLIs read via ``AWS_CONFIG_FILE``.

    Same mechanism as the e2e test suite: pinning ``preferred_transfer_client``
    explicitly keeps the host's ``~/.aws/config`` tuning out and makes the
    engine an explicit benchmark dimension for aws and boto3-s3 alike.
    """
    config = workroot / f"aws-config-{engine}"
    config.write_text(f"[default]\ns3 =\n  preferred_transfer_client = {engine}\n")
    return config


def _invoke(env: E2EEnv, scenario: E2EScenario, side: Side) -> tuple[float, CliResult]:
    """Run one side once, returning (wall-clock seconds, outcome)."""
    from tests.utils.harness import CliResult

    argv = scenario.make_argv(env, side)
    if env.engine == "crt" and not scenario.top_level:
        # aws's CRT client reads use_ssl from the CLI argument only, so an
        # env-only AWS_ENDPOINT_URL_S3 makes it dial TLS to MinIO's http
        # endpoint and fail (AWS_IO_SOCKET_CLOSED). Same workaround as the
        # e2e CRT parity lane: pass --endpoint-url explicitly, to both sides
        # for symmetry. The classic lane stays env-driven like the e2e suite.
        endpoint = os.environ.get("AWS_ENDPOINT_URL_S3")
        if endpoint:
            argv = [*argv, "--endpoint-url", endpoint]
    if side is Side.OURS:
        cmd = [env.ours_exe, *argv]
    elif scenario.top_level:
        cmd = [env.aws_exe, *argv]
    else:
        cmd = [env.aws_exe, "s3", *argv]
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT,
            env={**os.environ, **env.overlay},
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError(
            f"[{scenario.name}] {side.value} exceeded {_SUBPROCESS_TIMEOUT:.0f}s: {cmd}"
        ) from exc
    elapsed = time.perf_counter() - start
    return elapsed, CliResult(
        proc.returncode,
        proc.stdout.decode(errors="replace"),
        proc.stderr.decode(errors="replace"),
    )


def _require_rc(scenario: E2EScenario, side: Side, stage: str, result: CliResult) -> None:
    if result.rc not in scenario.ok_rcs:
        raise BenchmarkError(
            f"[{scenario.name}] {side.value} {stage} exited rc={result.rc}\n"
            f"stderr:\n{result.stderr}"
        )


def _scale(value: int, quick: bool, *, minimum: int = 10) -> int:
    return value if not quick else max(value // 100, minimum)


def build_scenarios(quick: bool) -> list[E2EScenario]:
    """The v1 E2E scenario set (sizes divided by ~100 under --quick)."""
    from tests.utils.harness import normalize_cp_stdout

    scenarios: list[E2EScenario] = []

    def version_verify(_env: E2EEnv, _side: Side, result: CliResult) -> None:
        if not result.stdout.strip():
            raise VerificationError("[startup_version] --version printed nothing")

    scenarios.append(
        E2EScenario(
            name="startup_version",
            dimensions={},
            top_level=True,
            make_argv=lambda _env, _side: ["--version"],
            verify=version_verify,
        )
    )

    def minimal_verify(_env: E2EEnv, side: Side, result: CliResult) -> None:
        if result.stdout.strip():
            raise VerificationError(
                f"[startup_minimal] {side.value} listed something under the empty prefix:\n"
                f"{result.stdout}"
            )

    scenarios.append(
        E2EScenario(
            name="startup_minimal",
            dimensions={},
            # Both CLIs exit 1 on an empty listing (aws parity); accept 0 too
            # so the probe never breaks on an endpoint that differs there.
            ok_rcs=frozenset({0, 1}),
            make_argv=lambda env, _side: ["ls", env.s3_url(env.s3_prefix("nothing"))],
            verify=minimal_verify,
        )
    )

    ls_count = _scale(10_000, quick)

    def ls_setup(env: E2EEnv) -> None:
        workload.seed_prefix(
            env.client, env.bucket, env.s3_prefix("ls") + "corpus/", ls_count, 1024
        )

    def ls_verify(_env: E2EEnv, side: Side, result: CliResult) -> None:
        lines = len(result.stdout.splitlines())
        if lines != ls_count:
            raise VerificationError(
                f"[ls_recursive] {side.value} listed {lines} lines, expected {ls_count}"
            )

    scenarios.append(
        E2EScenario(
            name="ls_recursive_10k",
            dimensions={"key_count": str(ls_count), "object_size": "1KB"},
            setup=ls_setup,
            make_argv=lambda env, _side: [
                "ls",
                "--recursive",
                env.s3_url(env.s3_prefix("ls") + "corpus/"),
            ],
            verify=ls_verify,
        )
    )

    sync_count = _scale(10_000, quick)

    def sync_setup(env: E2EEnv) -> None:
        tree = env.dir_for("sync_noop") / "tree"
        # Local tree first with mtimes a day in the past, seed after: every
        # remote LastModified is then strictly newer, so both CLIs judge the
        # sync a no-op (size-equal, local older).
        workload.generate_tree(tree, sync_count, 1024, mtime=time.time() - 86400)
        workload.seed_prefix(
            env.client, env.bucket, env.s3_prefix("sync_noop") + "corpus/", sync_count, 1024
        )

    def sync_verify(_env: E2EEnv, side: Side, result: CliResult) -> None:
        transfers = normalize_cp_stdout(result.stdout, bucket=BUCKET)
        if transfers:
            raise VerificationError(
                f"[sync_noop] {side.value} transferred {len(transfers)} files "
                f"(first: {transfers[0]}); the corpus is not in the no-op state"
            )

    scenarios.append(
        E2EScenario(
            name="sync_noop_10k",
            dimensions={"file_count": str(sync_count), "file_size": "1KB"},
            setup=sync_setup,
            make_argv=lambda env, _side: [
                "sync",
                str(env.dir_for("sync_noop") / "tree"),
                env.s3_url(env.s3_prefix("sync_noop") + "corpus/"),
            ],
            verify=sync_verify,
        )
    )

    cp_small_count = _scale(1_000, quick)

    def transfer_count_verify(name: str, expected: int) -> Callable[..., None]:
        def verify(_env: E2EEnv, side: Side, result: CliResult) -> None:
            transfers = normalize_cp_stdout(result.stdout, bucket=BUCKET)
            if len(transfers) != expected:
                raise VerificationError(
                    f"[{name}] {side.value} reported {len(transfers)} transfers, "
                    f"expected {expected}"
                )

        return verify

    def cp_up_small_setup(env: E2EEnv) -> None:
        workload.generate_tree(env.dir_for("cp_up_small") / "tree", cp_small_count, 4096)

    def cp_up_small_reset(env: E2EEnv) -> None:
        # Purge the previous invocation's output so MinIO's tmpfs stays bounded.
        from tests.utils.harness import delete_under

        delete_under(env.client, env.bucket, env.s3_prefix("cp_up_small"))

    scenarios.append(
        E2EScenario(
            name="cp_upload_small_1k",
            dimensions={"file_count": str(cp_small_count), "file_size": "4KB"},
            crt_capable=True,
            setup=cp_up_small_setup,
            reset=cp_up_small_reset,
            make_argv=lambda env, side: [
                "cp",
                "--recursive",
                str(env.dir_for("cp_up_small") / "tree"),
                env.s3_url(env.s3_prefix("cp_up_small") + env.unique(side) + "/"),
            ],
            verify=transfer_count_verify("cp_upload_small", cp_small_count),
        )
    )

    def cp_dl_small_setup(env: E2EEnv) -> None:
        workload.seed_prefix(
            env.client,
            env.bucket,
            env.s3_prefix("cp_dl_small") + "corpus/",
            cp_small_count,
            4096,
        )

    def cp_dl_small_reset(env: E2EEnv) -> None:
        shutil.rmtree(env.dir_for("cp_dl_small") / "dest", ignore_errors=True)

    scenarios.append(
        E2EScenario(
            name="cp_download_small_1k",
            dimensions={"file_count": str(cp_small_count), "file_size": "4KB"},
            crt_capable=True,
            setup=cp_dl_small_setup,
            reset=cp_dl_small_reset,
            make_argv=lambda env, side: [
                "cp",
                "--recursive",
                env.s3_url(env.s3_prefix("cp_dl_small") + "corpus/"),
                str(env.dir_for("cp_dl_small") / "dest" / env.unique(side)),
            ],
            verify=transfer_count_verify("cp_download_small", cp_small_count),
        )
    )

    # --quick keeps a just-past-threshold size (not /100) so the smoke run
    # still exercises multipart.
    big_size = 64 * _MB if not quick else 9 * _MB
    big_dim = f"{big_size // _MB}MB"

    def cp_up_large_setup(env: E2EEnv) -> None:
        workload.generate_file(env.dir_for("cp_up_large") / "big.bin", big_size)

    def cp_up_large_reset(env: E2EEnv) -> None:
        from tests.utils.harness import delete_under

        delete_under(env.client, env.bucket, env.s3_prefix("cp_up_large"))

    scenarios.append(
        E2EScenario(
            name="cp_upload_large",
            dimensions={"file_size": big_dim},
            crt_capable=True,
            setup=cp_up_large_setup,
            reset=cp_up_large_reset,
            make_argv=lambda env, side: [
                "cp",
                str(env.dir_for("cp_up_large") / "big.bin"),
                env.s3_url(env.s3_prefix("cp_up_large") + env.unique(side) + ".bin"),
            ],
            verify=transfer_count_verify("cp_upload_large", 1),
        )
    )

    def cp_dl_large_setup(env: E2EEnv) -> None:
        big = env.dir_for("cp_dl_large") / "big.bin"
        workload.generate_file(big, big_size)
        env.client.upload_file(str(big), env.bucket, env.s3_prefix("cp_dl_large") + "big.bin")

    def cp_dl_large_reset(env: E2EEnv) -> None:
        shutil.rmtree(env.dir_for("cp_dl_large") / "dest", ignore_errors=True)

    scenarios.append(
        E2EScenario(
            name="cp_download_large",
            dimensions={"file_size": big_dim},
            crt_capable=True,
            setup=cp_dl_large_setup,
            reset=cp_dl_large_reset,
            make_argv=lambda env, side: [
                "cp",
                env.s3_url(env.s3_prefix("cp_dl_large") + "big.bin"),
                str(env.dir_for("cp_dl_large") / "dest" / (env.unique(side) + ".bin")),
            ],
            verify=transfer_count_verify("cp_download_large", 1),
        )
    )

    rm_count = _scale(2_000, quick)

    def rm_reset(env: E2EEnv) -> None:
        from tests.utils.harness import delete_under

        prefix = env.s3_prefix("rm") + "corpus/"
        delete_under(env.client, env.bucket, prefix)
        workload.seed_prefix(env.client, env.bucket, prefix, rm_count, 1)

    def rm_verify(_env: E2EEnv, side: Side, result: CliResult) -> None:
        lines = [line for line in result.stdout.splitlines() if line.startswith("delete:")]
        if len(lines) != rm_count:
            raise VerificationError(
                f"[rm_recursive] {side.value} deleted {len(lines)} keys, expected {rm_count}"
            )

    scenarios.append(
        E2EScenario(
            name="rm_recursive_2k",
            dimensions={"key_count": str(rm_count)},
            samples=3,
            reset=rm_reset,
            make_argv=lambda env, _side: [
                "rm",
                "--recursive",
                env.s3_url(env.s3_prefix("rm") + "corpus/"),
            ],
            verify=rm_verify,
        )
    )

    return scenarios


def _run_scenario(
    env: E2EEnv,
    scenario: E2EScenario,
    *,
    samples_override: int | None,
    handicap: float,
    log: Callable[[str], None],
) -> ScenarioResult:
    if scenario.setup is not None:
        log(f"[e2e/{env.engine}] {scenario.name}: setup")
        scenario.setup(env)
    for side in (Side.OURS, Side.AWS):
        if scenario.reset is not None:
            scenario.reset(env)
        _elapsed, result = _invoke(env, scenario, side)
        _require_rc(scenario, side, "warmup", result)
        if scenario.verify is not None:
            scenario.verify(env, side, result)
    n = samples_override if samples_override is not None else scenario.samples
    log(f"[e2e/{env.engine}] {scenario.name}: timing {n} rounds x 2 sides")
    samples: dict[str, list[float]] = {Side.OURS.value: [], Side.AWS.value: []}
    order: list[str] = []
    for round_index in range(n):
        for side in round_order(round_index):
            if scenario.reset is not None:
                scenario.reset(env)
            elapsed, result = _invoke(env, scenario, side)
            _require_rc(scenario, side, f"round {round_index}", result)
            if side is Side.OURS:
                elapsed += handicap
            samples[side.value].append(elapsed)
            order.append(side.value)
    return ScenarioResult(
        scenario=scenario.name,
        mode="e2e",
        engine=env.engine,
        dimensions=dict(scenario.dimensions),
        samples=samples,
        order=order,
    )


def run_scenarios(
    scenarios: list[E2EScenario],
    *,
    engines: tuple[str, ...],
    samples_override: int | None = None,
    handicap: float = 0.0,
    log: Callable[[str], None],
) -> tuple[list[ScenarioResult], list[str]]:
    """Run the scenario set per engine against a freshly created `BUCKET`.

    The bucket is force-deleted first (a leftover from an aborted run must
    not skew seeding) and again in the finally, so no state survives the run.
    Non-``classic`` engines run only the ``crt_capable`` scenarios plus the
    startup probes their net adjustment needs.

    Returns ``(results, failures)``: one scenario failing (an unexpected rc,
    a verification mismatch) is logged and skipped rather than discarding
    every completed measurement with it - a multi-minute run must not be lost
    to one flaky lane. Environment/bucket errors still abort the whole run.
    """
    from tests.utils.harness import create_bucket_in_region, force_delete_bucket

    ours_exe, aws_exe = check_environment()
    client = _build_client()
    workroot = Path(tempfile.mkdtemp(prefix="boto3-s3-bench-e2e-"))
    results: list[ScenarioResult] = []
    failures: list[str] = []
    force_delete_bucket(client, BUCKET)
    create_bucket_in_region(client, BUCKET)
    try:
        for engine in engines:
            config = _write_engine_config(workroot, engine)
            overlay = {
                "AWS_CONFIG_FILE": str(config),
                "AWS_CLI_AUTO_PROMPT": "off",
                "AWS_PAGER": "",
            }
            env = E2EEnv(
                client=client,
                bucket=BUCKET,
                engine=engine,
                workdir=workroot / engine,
                ours_exe=ours_exe,
                aws_exe=aws_exe,
                overlay=overlay,
            )
            for scenario in scenarios:
                if engine != "classic" and not (
                    scenario.crt_capable or scenario.name.startswith("startup_")
                ):
                    continue
                try:
                    results.append(
                        _run_scenario(
                            env,
                            scenario,
                            samples_override=samples_override,
                            handicap=handicap,
                            log=log,
                        )
                    )
                except BenchmarkError as exc:
                    message = f"[e2e/{engine}] {scenario.name}: {exc}"
                    log(f"{message}\n[e2e/{engine}] {scenario.name}: scenario skipped")
                    failures.append(message)
    finally:
        force_delete_bucket(client, BUCKET)
        shutil.rmtree(workroot, ignore_errors=True)
    return results, failures
