"""E2E benchmark mode: both CLIs run as subprocesses against a local S3 endpoint.

This is the only mode where the pinned ``aws`` binary can be compared
against ``boto3-s3`` - both are timed as whole processes (startup included;
the report subtracts the ``startup_minimal`` probe, see design/benchmark.md)
against the same MinIO endpoint, interleaved A/B so host drift cancels in
the ratio.

The mode requires the standard dev stack (``scripts/compose-up.sh``,
``scripts/install-awscli.sh``, ``source scripts/minio-env.sh``) and owns the
dedicated bucket `BUCKET`, created at run start and force-deleted at exit;
the e2e test suite's bucket (``boto3-s3-e2e``, contractually empty) is never
touched.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks import awsenv, workload
from benchmarks.core import (
    DEFAULT_LARGE_MB,
    BenchmarkError,
    ScenarioResult,
    Side,
    VerificationError,
    round_order,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from tests.utils.harness import CliResult

BUCKET = "boto3-s3-bench"
# A caller-chosen bucket name for a real-S3 run (the EC2 launcher's way of
# knowing which bucket to remove if the run is cut short); must stay inside
# the `boto3-s3-bench-*` family.
BUCKET_ENV = "BOTO3_S3_BENCH_BUCKET"

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
    (``--version``), where aws takes no ``s3`` token. `payload_bytes` is what
    one invocation moves (transfer scenarios only); the report turns it into
    throughput.
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
    payload_bytes: int | None = None


def check_environment() -> tuple[str, str]:
    """Resolve both executables and validate the S3 target, or raise with guidance.

    Three shapes are accepted: local MinIO (an `AWS_ENDPOINT_URL_S3` on a local
    host plus the static dev keys), a deliberate non-local endpoint, and real
    S3 (no endpoint override, credentials from the default chain - a profile or
    an instance role). The last two both require the explicit
    `BOTO3_S3_BENCH_ALLOW_REMOTE` opt-in so a bare run cannot touch real AWS by
    accident.
    """
    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3")
    opted_in = awsenv.remote_opted_in()
    if not endpoint:
        if not opted_in:
            raise BenchmarkError(f"no AWS_ENDPOINT_URL_S3 set; {_SETUP_HINT}")
        _check_remote_credentials()
    elif awsenv.targeting_local_minio():
        missing = [
            name
            for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
            if not os.environ.get(name)
        ]
        if missing:
            raise BenchmarkError(f"missing environment: {', '.join(missing)}; {_SETUP_HINT}")
    elif not opted_in:
        host = awsenv.endpoint_host()
        raise BenchmarkError(
            f"AWS_ENDPOINT_URL_S3 points at non-local host {host!r}; benchmarks create and "
            f"purge a {BUCKET!r} bucket and transfer hundreds of MB. Set "
            f"{awsenv.ALLOW_REMOTE_ENV}=1 if that endpoint really is the intended target."
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


def _check_remote_credentials() -> None:
    """Fail fast when a real-S3 run has no region or no resolvable credentials.

    Better to stop here than after seeding a corpus: create_bucket_in_region
    needs the region, and every call needs credentials the default chain (env,
    profile, or instance role) can produce.
    """
    if awsenv.resolve_region() is None:
        raise BenchmarkError(
            "real-S3 run needs a region; set AWS_REGION or use a profile that has one"
        )
    if os.environ.get("AWS_PROFILE"):
        # Both CLIs run with AWS_CONFIG_FILE pointed at the per-engine config,
        # which hides every [profile ...] section from them; the harness's own
        # pre-flight would pass and the first CLI invocation would fail.
        raise BenchmarkError(
            "real-S3 run cannot use AWS_PROFILE: the harness pins AWS_CONFIG_FILE for both "
            "CLIs, so a profile is invisible to them. Export the credentials into the "
            "environment (e.g. `aws configure export-credentials --format env`) or run "
            "from an instance role."
        )
    import botocore.session

    session: Any = botocore.session.Session(profile=os.environ.get("AWS_PROFILE"))
    if session.get_credentials() is None:
        raise BenchmarkError(
            "real-S3 run found no credentials (checked env, profile, and instance role)"
        )


def aws_version(aws_exe: str) -> str:
    proc = subprocess.run([aws_exe, "--version"], capture_output=True, text=True, timeout=30.0)
    return (proc.stdout or proc.stderr).strip()


def _build_client() -> Any:
    import boto3

    # No endpoint override means real S3; MinIO reports us-east-1, so that is
    # the only place the default applies.
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3"),
        region_name=awsenv.resolve_region() or "us-east-1",
    )


def _bench_bucket() -> str:
    """The bucket the run owns: the fixed name on MinIO, a unique one on S3.

    On MinIO the name is private to the stack, so the documented
    ``boto3-s3-bench`` is fine and matches design/benchmark.md. Anywhere else -
    real S3 with or without an explicit endpoint - the namespace is global and
    the harness force-deletes whatever it creates, so a per-run suffix avoids
    colliding with anyone (including a previous run whose delete had not yet
    propagated).
    """
    if awsenv.targeting_local_minio():
        return BUCKET
    override = os.environ.get(BUCKET_ENV)
    if override:
        # The EC2 launcher names the run's bucket so it can remove exactly that
        # one if the run is cut short; it must stay in the family the instance
        # role is scoped to.
        if not override.startswith(BUCKET + "-"):
            raise BenchmarkError(f"{BUCKET_ENV} must start with {BUCKET + '-'!r}: {override!r}")
        return override
    import secrets

    return f"{BUCKET}-{secrets.token_hex(4)}"


def _write_engine_config(workroot: Path, engine: str) -> Path:
    """The per-engine AWS config both CLIs read via ``AWS_CONFIG_FILE``.

    Same mechanism as the e2e test suite: pinning ``preferred_transfer_client``
    explicitly keeps the host's ``~/.aws/config`` tuning out and makes the
    engine an explicit benchmark dimension for aws and boto3-s3 alike.
    """
    config = workroot / f"aws-config-{engine}"
    config.write_text(f"[default]\ns3 =\n  preferred_transfer_client = {engine}\n")
    return config


def _invoke(
    env: E2EEnv, scenario: E2EScenario, side: Side
) -> tuple[float, CliResult, float | None]:
    """Run one side once: (wall-clock seconds, outcome, peak RSS in bytes or None)."""
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
    try:
        return _run_child(cmd, {**os.environ, **env.overlay})
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError(
            f"[{scenario.name}] {side.value} exceeded {_SUBPROCESS_TIMEOUT:.0f}s: {cmd}"
        ) from exc


def _run_child(cmd: list[str], env: dict[str, str]) -> tuple[float, CliResult, float | None]:
    """Run one CLI process: wall-clock seconds, its outcome, its peak RSS in bytes.

    The child is started by a tiny intermediary (``python -I -S``, a few MiB)
    rather than by this harness directly, and the intermediary does the timing
    and the reaping. Two reasons. First, Linux floors a reaped child's
    ``ru_maxrss`` at the high-water mark of the address space it was forked
    from, so a CLI spawned straight from this boto3-laden process could never
    report a peak below the harness's own (~65 MiB) - the ours-side numbers
    would all read the same. Second, the intermediary's ``wait4`` returns that
    one child's rusage, which ``getrusage(RUSAGE_CHILDREN)`` (a running
    maximum over every child ever reaped) cannot. The intermediary's own
    startup sits outside the timed window, and its ``fork`` -> ``wait4`` edge
    is the exit itself, so the timing is if anything tighter than a
    ``subprocess.run`` from here. Output goes to temporary files so a chatty
    child (``ls`` of 10k keys) can never stall on a full pipe, and stdin is
    ``/dev/null`` so neither CLI can notice a terminal. On timeout the whole
    process group is killed so no orphan keeps transferring. Where
    ``fork`` does not exist (Windows) ``subprocess.run`` measures the time
    alone and the RSS is None.
    """
    import json
    import signal

    from tests.utils.harness import CliResult

    if not hasattr(os, "fork") or not hasattr(os, "killpg"):
        start = time.perf_counter()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=_SUBPROCESS_TIMEOUT,
            env=env,
        )
        elapsed = time.perf_counter() - start
        return (
            elapsed,
            CliResult(
                proc.returncode,
                proc.stdout.decode(errors="replace"),
                proc.stderr.decode(errors="replace"),
            ),
            None,
        )
    with (
        tempfile.TemporaryDirectory(prefix="boto3-s3-bench-run-") as tmp,
        tempfile.TemporaryFile() as out,
        tempfile.TemporaryFile() as err,
    ):
        report = Path(tmp) / "report.json"
        timed_out = threading.Event()
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _INTERMEDIARY, str(report), *cmd],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            env=env,
            start_new_session=True,
        )

        def _kill_group() -> None:
            timed_out.set()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

        timer = threading.Timer(_SUBPROCESS_TIMEOUT, _kill_group)
        timer.start()
        try:
            proc.wait()
        except BaseException:
            # Ctrl-C (or anything else) must not leave the CLI running on.
            _kill_group()
            raise
        finally:
            timer.cancel()
        if timed_out.is_set():
            raise subprocess.TimeoutExpired(cmd, _SUBPROCESS_TIMEOUT)
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode(errors="replace")
        stderr = err.read().decode(errors="replace")
        if not report.is_file():
            raise BenchmarkError(
                f"the timing intermediary produced no report for {cmd} "
                f"(rc={proc.returncode})\nstderr:\n{stderr}"
            )
        data = json.loads(report.read_text())
    rc = os.waitstatus_to_exitcode(int(data["status"]))
    return float(data["elapsed"]), CliResult(rc, stdout, stderr), _maxrss_bytes(data["maxrss"])


# The intermediary. It forks the CLI, times fork -> reap, and writes elapsed
# seconds, the raw wait status and ru_maxrss to the report path in argv[1].
# It imports only what it needs so its own address space stays small: that
# size is the floor below which the child's peak cannot be reported.
_INTERMEDIARY = """
import json, os, sys, time
report, cmd = sys.argv[1], sys.argv[2:]
start = time.perf_counter()
pid = os.fork()
if pid == 0:
    try:
        os.execvp(cmd[0], cmd)
    finally:
        os._exit(127)
_, status, usage = os.wait4(pid, 0)
elapsed = time.perf_counter() - start
with open(report, "w") as fh:
    json.dump({"elapsed": elapsed, "status": status, "maxrss": usage.ru_maxrss}, fh)
"""


def _maxrss_bytes(maxrss: float) -> float:
    """``ru_maxrss`` normalized to bytes: Linux reports KiB, macOS bytes."""
    value = float(maxrss)
    return value if sys.platform == "darwin" else value * 1024.0


def _require_rc(scenario: E2EScenario, side: Side, stage: str, result: CliResult) -> None:
    if result.rc not in scenario.ok_rcs:
        raise BenchmarkError(
            f"[{scenario.name}] {side.value} {stage} exited rc={result.rc}\n"
            f"stderr:\n{result.stderr}"
        )


def _scale(value: int, quick: bool, *, minimum: int = 10) -> int:
    return value if not quick else max(value // 100, minimum)


def build_scenarios(quick: bool, *, large_mb: int = DEFAULT_LARGE_MB) -> list[E2EScenario]:
    """The E2E scenario set (sizes divided by ~100 under --quick).

    *large_mb* sizes the single-object transfer scenarios. The 64 MB default
    suits the local MinIO lane; against real S3 on a wide NIC that finishes
    inside the startup constant, so the EC2 lane raises it
    (``--large-transfer-mb``).
    """
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

    def sync_verify(env: E2EEnv, side: Side, result: CliResult) -> None:
        transfers = normalize_cp_stdout(result.stdout, bucket=env.bucket)
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
        def verify(env: E2EEnv, side: Side, result: CliResult) -> None:
            transfers = normalize_cp_stdout(result.stdout, bucket=env.bucket)
            if len(transfers) != expected:
                raise VerificationError(
                    f"[{name}] {side.value} reported {len(transfers)} transfers, "
                    f"expected {expected}"
                )

        return verify

    # sync that does work. sync_noop above measures enumerate+compare with
    # nothing to do; these three cover the decisions that lead somewhere: one
    # new file (the README's one-file measurement, startup-dominated), a
    # corpus where a fixed subset must be re-uploaded, and --delete over a
    # corpus with remote-only extras. Deterministic re-staging keeps every
    # invocation, warmup included, facing the same work.
    tiny_size = 11 * 1024

    def sync_tiny_setup(env: E2EEnv) -> None:
        workload.generate_tree(env.dir_for("sync_tiny") / "tree", 1, tiny_size)

    def sync_tiny_reset(env: E2EEnv) -> None:
        from tests.utils.harness import delete_under

        delete_under(env.client, env.bucket, env.s3_prefix("sync_tiny"))

    scenarios.append(
        E2EScenario(
            name="sync_tiny",
            dimensions={"file_count": "1", "file_size": "11KB"},
            payload_bytes=tiny_size,
            crt_capable=True,
            setup=sync_tiny_setup,
            reset=sync_tiny_reset,
            make_argv=lambda env, side: [
                "sync",
                str(env.dir_for("sync_tiny") / "tree"),
                env.s3_url(env.s3_prefix("sync_tiny") + env.unique(side) + "/"),
            ],
            verify=transfer_count_verify("sync_tiny", 1),
        )
    )

    changed_total = _scale(10_000, quick)
    changed_count = _scale(2_000, quick)

    def sync_changed_setup(env: E2EEnv) -> None:
        # Local tree a day old, remote seeded now at the same size: the
        # untouched keys compare equal (size) and older (mtime) - a no-op.
        workload.generate_tree(
            env.dir_for("sync_changed") / "tree", changed_total, 1024, mtime=time.time() - 86400
        )
        workload.seed_prefix(
            env.client,
            env.bucket,
            env.s3_prefix("sync_changed") + "corpus/",
            changed_total,
            1024,
        )

    def sync_changed_reset(env: E2EEnv) -> None:
        # Re-stale the first `changed_count` keys by *size* (2 KB against a
        # 1 KB local file): a size mismatch forces the upload whatever the
        # timestamps say, and the previous invocation's uploads (which made
        # them 1 KB again) are undone. Only the subset is rewritten, so the
        # reset costs changed_count puts, not the whole corpus.
        workload.seed_prefix(
            env.client,
            env.bucket,
            env.s3_prefix("sync_changed") + "corpus/",
            changed_count,
            2048,
        )

    scenarios.append(
        E2EScenario(
            name="sync_changed_10k",
            dimensions={
                "file_count": str(changed_total),
                "changed_count": str(changed_count),
                "file_size": "1KB",
            },
            payload_bytes=changed_count * 1024,
            crt_capable=True,
            setup=sync_changed_setup,
            reset=sync_changed_reset,
            make_argv=lambda env, _side: [
                "sync",
                str(env.dir_for("sync_changed") / "tree"),
                env.s3_url(env.s3_prefix("sync_changed") + "corpus/"),
            ],
            verify=transfer_count_verify("sync_changed", changed_count),
        )
    )

    delete_kept = _scale(8_000, quick)
    delete_extra = _scale(2_000, quick)

    def sync_delete_setup(env: E2EEnv) -> None:
        workload.generate_tree(
            env.dir_for("sync_delete") / "tree", delete_kept, 1024, mtime=time.time() - 86400
        )
        workload.seed_prefix(
            env.client, env.bucket, env.s3_prefix("sync_delete") + "corpus/", delete_kept, 1024
        )

    def sync_delete_reset(env: E2EEnv) -> None:
        # The extras live under a sub-prefix no local directory matches, so
        # every one of them is remote-only; the previous invocation deleted
        # them and this puts them back.
        workload.seed_prefix(
            env.client,
            env.bucket,
            env.s3_prefix("sync_delete") + "corpus/extra/",
            delete_extra,
            1,
        )

    def sync_delete_verify(_env: E2EEnv, side: Side, result: CliResult) -> None:
        lines = result.stdout.splitlines()
        deletes = [line for line in lines if line.startswith("delete:")]
        uploads = [line for line in lines if line.startswith("upload:")]
        if len(deletes) != delete_extra or uploads:
            raise VerificationError(
                f"[sync_delete] {side.value} deleted {len(deletes)} keys (expected "
                f"{delete_extra}) and uploaded {len(uploads)} (expected 0)"
            )

    scenarios.append(
        E2EScenario(
            name="sync_delete_10k",
            dimensions={"file_count": str(delete_kept), "extra_count": str(delete_extra)},
            setup=sync_delete_setup,
            reset=sync_delete_reset,
            make_argv=lambda env, _side: [
                "sync",
                "--delete",
                str(env.dir_for("sync_delete") / "tree"),
                env.s3_url(env.s3_prefix("sync_delete") + "corpus/"),
            ],
            verify=sync_delete_verify,
        )
    )

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
            payload_bytes=cp_small_count * 4096,
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
            payload_bytes=cp_small_count * 4096,
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
    big_size = large_mb * _MB if not quick else 9 * _MB
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
            payload_bytes=big_size,
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
            payload_bytes=big_size,
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
        _elapsed, result, _peak = _invoke(env, scenario, side)
        _require_rc(scenario, side, "warmup", result)
        if scenario.verify is not None:
            scenario.verify(env, side, result)
    n = samples_override if samples_override is not None else scenario.samples
    log(f"[e2e/{env.engine}] {scenario.name}: timing {n} rounds x 2 sides")
    samples: dict[str, list[float]] = {Side.OURS.value: [], Side.AWS.value: []}
    rss: dict[str, list[float]] = {Side.OURS.value: [], Side.AWS.value: []}
    rss_available = True
    order: list[str] = []
    for round_index in range(n):
        for side in round_order(round_index):
            if scenario.reset is not None:
                scenario.reset(env)
            elapsed, result, peak = _invoke(env, scenario, side)
            _require_rc(scenario, side, f"round {round_index}", result)
            if side is Side.OURS:
                elapsed += handicap
            samples[side.value].append(elapsed)
            if peak is None:
                rss_available = False
            else:
                rss[side.value].append(peak)
            order.append(side.value)
    return ScenarioResult(
        scenario=scenario.name,
        mode="e2e",
        engine=env.engine,
        dimensions=dict(scenario.dimensions),
        samples=samples,
        order=order,
        rss=rss if rss_available else None,
        payload_bytes=scenario.payload_bytes,
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
    a verification mismatch, a local I/O error such as a full work tree) is
    logged and skipped rather than discarding every completed measurement
    with it - a multi-minute run must not be lost to one flaky lane.
    Environment/bucket errors still abort the whole run. Each engine's work
    tree is removed before the next engine seeds its own, so the large
    payloads of two engines never coexist: the EC2 lane's tmpfs is sized for
    one engine's.
    """
    from tests.utils.harness import create_bucket_in_region, force_delete_bucket

    ours_exe, aws_exe = check_environment()
    client = _build_client()
    bucket = _bench_bucket()
    region = awsenv.resolve_region()
    workroot = Path(tempfile.mkdtemp(prefix="boto3-s3-bench-e2e-"))
    results: list[ScenarioResult] = []
    failures: list[str] = []
    force_delete_bucket(client, bucket)
    create_bucket_in_region(client, bucket)
    try:
        for engine in engines:
            config = _write_engine_config(workroot, engine)
            overlay = {
                "AWS_CONFIG_FILE": str(config),
                "AWS_CLI_AUTO_PROMPT": "off",
                "AWS_PAGER": "",
                # The engine config replaces ~/.aws/config, so hand both CLIs
                # the region explicitly (real S3 needs it; harmless on MinIO).
                **({"AWS_REGION": region, "AWS_DEFAULT_REGION": region} if region else {}),
            }
            env = E2EEnv(
                client=client,
                bucket=bucket,
                engine=engine,
                workdir=workroot / engine,
                ours_exe=ours_exe,
                aws_exe=aws_exe,
                overlay=overlay,
            )
            try:
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
                    except (BenchmarkError, OSError) as exc:
                        message = f"[e2e/{engine}] {scenario.name}: {exc}"
                        log(f"{message}\n[e2e/{engine}] {scenario.name}: scenario skipped")
                        failures.append(message)
            finally:
                shutil.rmtree(env.workdir, ignore_errors=True)
    finally:
        force_delete_bucket(client, bucket)
        shutil.rmtree(workroot, ignore_errors=True)
    return results, failures
