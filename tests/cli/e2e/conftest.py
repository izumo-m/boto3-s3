"""Gate, probe, and fixtures for the e2e parity suite.

This suite runs the real ``aws`` binary and the installed ``boto3-s3``
console script as **external processes** against the same S3-compatible
endpoint the host shell is configured for - nothing is mocked. The intended
setup is the local dev stack::

    scripts/compose-up.sh
    scripts/install-awscli.sh
    source scripts/dev-env.sh
    uv run pytest tests/cli/e2e -n auto

``-n auto`` runs the scenarios on pytest-xdist workers, as many as
`pytest_xdist_auto_num_workers` allows; without ``-n`` they run in one process.
To run against **real AWS** instead, ``tests/run_e2e.sh`` creates a throwaway
bucket from ``BOTO3_S3_E2E_PROFILE`` / ``BOTO3_S3_E2E_REGION`` and force-removes
it (and its mb/rb siblings and the workers' own buckets) on exit.

The suite is opt-in, gated on ``BOTO3_S3_E2E_BUCKET``:

- env var unset, e2e collected alongside other suites -> every e2e item is
  skipped with a clear reason;
- env var unset, *only* e2e was requested -> the session aborts immediately
  with one message instead of N skips (under pytest-xdist "only e2e" is read
  off the command line's paths, so a ``--pyargs`` run skips instead);
- env var set -> the environment is probed once before any test runs (the
  ``aws`` binary present and on the vendored v2 pin; the bucket reachable and
  **empty**, checked via boto3) and the session aborts on any failure. The
  non-empty check is the safety guard against pointing the variable at a real,
  populated bucket. The probe runs at collection, or under pytest-xdist in the
  controller as the session starts (`pytest_sessionstart`); where the
  controller cannot, a worker probes and fails its e2e items instead of
  aborting.

Credentials, region and endpoint come from the env alone (``scripts/dev-env.sh``,
or ``tests/run_e2e.sh``, which exports a profile's credentials and drops
``AWS_PROFILE``); ``~`` points at a throwaway directory for the whole suite,
probe included, so neither tool reads the developer's ``~/.aws`` and the pinned
aws-cli does not write its session database there.
"""

from __future__ import annotations

import itertools
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.config import Config

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

BUCKET_ENV_VAR = "BOTO3_S3_E2E_BUCKET"

_E2E_BOTO3_CONFIG = Config(
    connect_timeout=3,
    read_timeout=15,
    tcp_keepalive=True,
    retries={"mode": "standard", "max_attempts": 5},
)

_E2E_DIR = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _moto_isolation() -> None:
    """Override the root ``tests/conftest.py`` autouse fixture with a no-op.

    The other suites force fake AWS credentials so moto never sees the host's
    real ones. e2e talks to a real endpoint and needs the host's credentials,
    region, and ``AWS_ENDPOINT_URL_S3`` intact, so the parent fixture is
    suppressed by this same-name no-op (pytest's closer-conftest-wins
    resolution). The home directory is isolated separately (`_e2e_home`).
    """
    return


@contextmanager
def _isolated_home() -> Generator[None, None, None]:
    """Point ``~`` at a throwaway directory for the duration.

    Both tools read ``~/.aws/config`` and ``~/.aws/credentials`` (and the
    pinned `aws` ``~/.aws/cli/alias``), and the pinned `aws` writes
    ``~/.aws/cli/cache/session.db`` on every command that builds a client
    (``--version`` alone does not). Against the developer's home, the host's
    settings would steer both sides of every comparison and each run would
    write into it. ``USERPROFILE`` is what Windows resolves ``~`` from, and
    ``AWS_CONFIG_FILE`` / ``AWS_SHARED_CREDENTIALS_FILE`` are dropped too: a
    shell that exports them would point both tools back at the developer's
    files (the CRT lane sets its own config file per invocation).
    """
    with (
        tempfile.TemporaryDirectory(prefix="e2e-home-") as home,
        pytest.MonkeyPatch.context() as mp,
    ):
        mp.setenv("HOME", home)
        mp.setenv("USERPROFILE", home)
        mp.delenv("AWS_CONFIG_FILE", raising=False)
        mp.delenv("AWS_SHARED_CREDENTIALS_FILE", raising=False)
        yield


@pytest.fixture(scope="package", autouse=True)
def _e2e_home(request: pytest.FixtureRequest) -> Iterator[None]:
    """The suite-wide `_isolated_home` (package scope: it ends with the suite).

    It also fails every e2e item with the reason a worker's probe failed
    (`pytest_collection_modifyitems`): every e2e item uses it.
    """
    failure = request.config.stash.get(_PROBE_FAILURE_STASH, None)
    if failure is not None:
        pytest.fail(failure, pytrace=False)
    with _isolated_home():
        yield


def _is_e2e_item(item: Any) -> bool:
    try:
        return Path(str(item.fspath)).resolve().is_relative_to(_E2E_DIR)
    except (AttributeError, ValueError):
        return False


# Set once the xdist controller has run the gate and the probe
# (`pytest_sessionstart`): in its stash, and passed on in each worker's
# workerinput, so the workers do not repeat them.
_PROBED_STASH = pytest.StashKey[bool]()
_PROBED_KEY = "boto3_s3_e2e_probed"
# A worker's own probe failure, which its e2e items fail with (`_e2e_home`).
_PROBE_FAILURE_STASH = pytest.StashKey[str]()


def _is_xdist_controller(config: pytest.Config) -> bool:
    return config.pluginmanager.has_plugin("dsession")


def _only_e2e_requested(config: pytest.Config) -> bool:
    """Whether every path the command line names is inside the e2e suite."""
    root = Path(str(config.invocation_params.dir))
    paths = [(root / arg.split("::", 1)[0]).resolve() for arg in config.args]
    return bool(paths) and all(path.is_relative_to(_E2E_DIR) for path in paths)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Under pytest-xdist, gate and probe once in the controller.

    The workers collect, not the controller, so `pytest_collection_modifyitems`
    runs on each of them - and a ``pytest.exit`` there reads to xdist as a
    worker going down: it starts another in its place, again and again, and
    the reason is never shown. Here the same checks run before any worker
    starts, and the run stops with its message. Which items were collected
    is not known yet, so "only e2e was asked for" is read off the command
    line's paths. This conftest is loaded this early only when the command
    line names a path inside the suite (``uv run pytest -n auto`` with the
    env set, the whole suite, does not); otherwise each worker probes, and
    fails its e2e items with the reason rather than exiting.
    """
    config = session.config
    if not _is_xdist_controller(config):
        return
    if not os.environ.get(BUCKET_ENV_VAR):
        if _only_e2e_requested(config):
            pytest.exit(f"{BUCKET_ENV_VAR} env var is required for e2e tests", returncode=2)
        return
    with _isolated_home():
        _probe_e2e_environment_or_exit(config)
    config.stash[_PROBED_STASH] = True


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    """Tell each worker the controller has already probed (`pytest_sessionstart`)."""
    if node.config.stash.get(_PROBED_STASH, False):
        node.workerinput[_PROBED_KEY] = True


def pytest_collection_modifyitems(config: pytest.Config, items: list[Any]) -> None:
    if not items:
        return
    e2e_items = [item for item in items if _is_e2e_item(item)]
    if not e2e_items:
        return

    worker_input: dict[str, Any] | None = getattr(config, "workerinput", None)
    if not os.environ.get(BUCKET_ENV_VAR):
        # On an xdist worker the controller has already stopped an e2e-only
        # run (`pytest_sessionstart`); a worker never exits (see there).
        if worker_input is None and len(e2e_items) == len(items):
            # The user asked for the e2e suite explicitly but the env is not
            # configured - fail loudly with one message.
            pytest.exit(f"{BUCKET_ENV_VAR} env var is required for e2e tests", returncode=2)
        skip = pytest.mark.skip(reason=f"{BUCKET_ENV_VAR} not set; skipping e2e suite")
        for item in e2e_items:
            item.add_marker(skip)
        return

    if worker_input is not None:
        if worker_input.get(_PROBED_KEY):
            return
        # The controller could not probe (`pytest_sessionstart`) and a
        # worker must not exit: record the reason for `_e2e_home` to fail
        # each e2e item with.
        try:
            with _isolated_home():
                _probe_e2e_environment_or_exit(config)
        except pytest.exit.Exception as exc:
            config.stash[_PROBE_FAILURE_STASH] = str(exc.msg)
        return
    with _isolated_home():
        _probe_e2e_environment_or_exit(config)


def _probe_e2e_environment_or_exit(config: pytest.Config) -> None:
    """Probe the live environment; ``pytest.exit`` on any failure.

    Run once per session - or, where the xdist controller cannot, once per
    worker, which turns the exit into its e2e items' failure
    (`pytest_collection_modifyitems`) - so a broken environment reads as one
    clear message rather than N test failures. The parity tests drive the high-level ``aws s3``
    commands themselves, so the probe only checks that the ``aws`` binary is
    present and on the vendored v2 pin; the bucket's reachability and the
    empty-bucket safety guard go through boto3 (the same client the per-test
    ``_assert_bucket_empty`` uses), not a low-level ``aws s3api`` call.
    """
    bucket = os.environ.get(BUCKET_ENV_VAR)
    assert bucket  # checked by the caller
    if shutil.which("aws") is None:
        pytest.exit("aws v2 binary not on PATH", returncode=2)
    _check_aws_version_or_exit(config)
    client = boto3.session.Session().client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3"),
        config=_E2E_BOTO3_CONFIG,
    )
    try:
        response = client.list_objects_v2(Bucket=bucket, MaxKeys=1)
    except Exception as exc:
        pytest.exit(f"bucket {bucket!r} not reachable via boto3: {exc}", returncode=2)
    # A populated bucket aborts the run before any test can touch data.
    if response.get("Contents"):
        pytest.exit(
            f"bucket {bucket!r} is not empty. e2e tests refuse to run against a "
            f"populated bucket - set {BUCKET_ENV_VAR} to a clean, dedicated test "
            "bucket and try again.",
            returncode=2,
        )


def _check_aws_version_or_exit(config: pytest.Config) -> None:
    """Gate the live ``aws`` on major 2 and the vendored pin.

    The parity goldens are captured with the aws-cli the library is ported
    against (the vendored submodule; ``scripts/install-awscli.sh`` installs it
    into ``.venv/bin``). A v1 binary, or a v2 off the pin, silently skews the
    comparison, so a wrong major aborts outright and a pinned-version drift
    aborts under ``UPDATE_GOLDENS`` (the capture must match the pin) and warns
    otherwise (a checked-out submodule is required for the pin check; without
    one only the major is enforced). The warning goes through
    ``issue_config_time_warning``: raised from the xdist controller's
    `pytest_sessionstart`, a plain ``warnings.warn`` is outside pytest's
    capture and never reaches the warnings summary.
    """
    from tests.utils.golden import (
        detect_aws_version,
        parse_aws_cli_version,
        pinned_aws_version,
        update_goldens_enabled,
    )

    reported = detect_aws_version()
    version = parse_aws_cli_version(reported)
    if version is None or version.split(".", 1)[0] != "2":
        pytest.exit(f"e2e requires an aws-cli v2 binary; found {reported!r}", returncode=2)
    pinned = pinned_aws_version()
    if pinned is not None and version != pinned:
        message = (
            f"live aws-cli {version} != the vendored pin {pinned} "
            "(scripts/install-awscli.sh); goldens are captured against the pin"
        )
        if update_goldens_enabled():
            pytest.exit(message, returncode=2)
        config.issue_config_time_warning(pytest.PytestWarning(message), stacklevel=2)


# The ceiling on `-n auto`'s workers. The scenarios spend their time in the
# CPU, starting and running the two CLIs, and on the dev stack the gain flattens
# early: on a 14-CPU laptop (Core Ultra 5 225H) the suite took 207 s in one
# process, 108 s on 4 workers, 97 s on 7 and 91 s on 10 or 14, while the CPU
# time the tests used grew with every worker (405 s on 4, 1061 s on 14). Each
# worker added about 230 MiB at its peak (pytest plus the CLI it runs).
# Measured 2026-10-25.
_MAX_AUTO_WORKERS = 4


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """How many workers ``-n auto`` starts for the e2e suite.

    ``BOTO3_S3_E2E_WORKERS`` when set; otherwise half the CPUs this process
    may use, at most `_MAX_AUTO_WORKERS` - not one per CPU, which would leave
    nothing for the endpoint (the dev stack runs on the same machine) or the
    rest of the machine; xdist's own ``PYTEST_XDIST_AUTO_NUM_WORKERS``, when
    set, in place of that default. Just one, whichever variable is set but
    ``BOTO3_S3_E2E_WORKERS``, on a host awscrt calls optimized for the CRT
    (some EC2 types): there both CLIs resolve ``auto`` to the CRT engine only
    when no other process of theirs holds its cross-process CRT lock, so
    parallel workers would make a scenario's engine a matter of timing. The
    check asks the venv's awscrt, whose list of such hosts can trail the one
    the pinned aws-cli bundles (aws-cli 2.36.40's names p5e / p5en / p6 types
    the venv's 0.32.2 does not). Only a run that names ``tests/cli/e2e``
    loads this conftest early enough for the hook to apply; ``-n auto`` over
    the whole suite gets xdist's own count.
    """
    explicit = os.environ.get("BOTO3_S3_E2E_WORKERS")
    if explicit:
        try:
            return max(1, int(explicit))
        except ValueError:
            raise pytest.UsageError(
                f"BOTO3_S3_E2E_WORKERS must be a whole number, not {explicit!r}"
            ) from None
    try:
        from awscrt.s3 import is_optimized_for_system
    except ImportError:
        pass
    else:
        if is_optimized_for_system():
            return 1
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    return max(1, min(_MAX_AUTO_WORKERS, (cpus or 2) // 2))


@pytest.fixture(scope="session")
def main_bucket_name() -> str:
    """The dedicated test bucket name from the env."""
    name = os.environ.get(BUCKET_ENV_VAR)
    assert name, f"{BUCKET_ENV_VAR} should have been verified at session start"
    return name


@pytest.fixture(scope="package")
def bucket_name(s3_client: Any, main_bucket_name: str) -> Iterator[str]:
    """The bucket this process's scenarios run in.

    In one process that is the env's bucket. On a pytest-xdist worker it is
    one of the worker's own, ``<main>-<worker id>`` (``-gw0``, ``-gw1``, ...):
    the scenarios need an empty bucket before and after each of them, and some
    work on the bucket root (``rm s3://<bucket> --recursive``,
    ``ls s3://<bucket>``), so workers can share neither the bucket nor split it
    by key prefix. The worker creates it as it enters this package and
    force-deletes it as it leaves (and before creating it, in case a crashed
    run left it) - without looking inside, as the mb/rb siblings are; the
    env's bucket is then only probed, as the empty-bucket guard.
    ``tests/run_e2e.sh`` sweeps ``<main>-*`` on exit, so a real-S3 run leaves
    none behind either. Two runs at once against one env bucket would delete
    each other's worker buckets, one more reason they must not overlap
    (design/testing.md). A worker that enters the package twice in one run -
    ``--dist worksteal``, or a crashed worker's items queued behind other
    suites' in a mixed run - re-creates the same name, which real S3 can
    refuse for a while after the delete (endpoint-fidelity T4); the default
    ``load`` scheduling of an e2e run never does.
    """
    from tests.utils.harness import create_bucket_in_region, force_delete_bucket

    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        yield main_bucket_name
        return
    name = f"{main_bucket_name}-{worker}"
    force_delete_bucket(s3_client, name)
    create_bucket_in_region(s3_client, name)
    try:
        yield name
    finally:
        force_delete_bucket(s3_client, name)


@pytest.fixture(scope="package")
def s3_client(_e2e_home: None) -> Any:
    """An S3 client built from the host env (incl. ``AWS_ENDPOINT_URL_S3``).

    Built on a fresh ``Session``: the process-global default session caches
    whatever credentials it resolved first, which in a full run are the fake
    ones the other suites force via ``_moto_isolation``. Package scope and the
    explicit `_e2e_home` dependency build it under the isolated home - a
    session-scoped fixture would be set up before the package-scoped one.
    """
    return boto3.session.Session().client(
        "s3",
        endpoint_url=os.environ.get("AWS_ENDPOINT_URL_S3"),
        config=_E2E_BOTO3_CONFIG,
    )


def _assert_bucket_empty(client: Any, bucket: str, *, when: str) -> None:
    response = client.list_objects_v2(Bucket=bucket, MaxKeys=5)
    contents = response.get("Contents", [])
    if contents:
        sample = ", ".join(obj["Key"] for obj in contents[:5])
        pytest.fail(
            f"bucket {bucket!r} is not empty {when} test (sample keys: {sample!r}). "
            "Each test must delete the keys it put - see tests/utils/harness.py::"
            "delete_keys / delete_under."
        )


@pytest.fixture
def bucket(s3_client: Any, bucket_name: str) -> Iterator[str]:
    """Yield this process's test bucket (`bucket_name`); assert empty before and after.

    The fixture never deletes objects on its own. A non-empty pre-state means
    a previous test forgot to clean up; a non-empty post-state means *this*
    test did. Either fails immediately, so cleanup bugs surface where they
    happen instead of contaminating later tests.
    """
    _assert_bucket_empty(s3_client, bucket_name, when="before")
    yield bucket_name
    _assert_bucket_empty(s3_client, bucket_name, when="after")


# Numbers the sibling-bucket pairs of one session, so no name repeats.
_SIBLING_SERIAL = itertools.count(1)


def _sibling_buckets(s3_client: Any, bucket_name: str, kind: str) -> Iterator[tuple[str, str]]:
    """Two buckets the lifecycle (mb/rb) tests own outright: aws-cli's, then ours.

    The test bucket (`bucket_name`: the env's, or a worker's own) must stay
    existing and empty, so those suites work on siblings derived from it (the
    ``BOTO3_S3_E2E_BUCKET`` opt-in covers the derived names):
    ``<bucket>-<kind><nnn>-aws`` and ``<bucket>-<kind><nnn>-ours``.
    endpoint-fidelity T4: every scenario and side gets a name of its own,
    because real S3 lags behind a delete and re-create under one name (a
    re-create can answer ``BucketAlreadyOwnedByYou``, a HeadBucket can miss
    the new bucket). Force-deleted before the test (leftover from a crashed
    run) and after (never left behind). endpoint-fidelity T3: the names stay
    clear of a trailing ``-an`` (account-regional BucketNamespace semantics,
    unverified against the dev stack) - scenarios append markers like
    ``--x-s3`` themselves when they test them.
    """
    from tests.utils.harness import force_delete_bucket

    serial = next(_SIBLING_SERIAL)
    aws_name = f"{bucket_name}-{kind}{serial:03d}-aws"
    ours_name = f"{bucket_name}-{kind}{serial:03d}-ours"
    force_delete_bucket(s3_client, aws_name)
    force_delete_bucket(s3_client, ours_name)
    try:
        yield (aws_name, ours_name)
    finally:
        try:
            force_delete_bucket(s3_client, aws_name)
        finally:
            force_delete_bucket(s3_client, ours_name)


@pytest.fixture
def mb_buckets(s3_client: Any, bucket_name: str) -> Iterator[tuple[str, str]]:
    """The mb parity tests' sibling buckets (created by the command itself)."""
    yield from _sibling_buckets(s3_client, bucket_name, "mb")


@pytest.fixture
def rb_buckets(s3_client: Any, bucket_name: str) -> Iterator[tuple[str, str]]:
    """The rb parity tests' sibling buckets (pre-created per scenario)."""
    yield from _sibling_buckets(s3_client, bucket_name, "rb")
