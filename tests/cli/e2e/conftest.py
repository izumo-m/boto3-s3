"""Gate, probe, and fixtures for the e2e parity suite.

This suite runs the real ``aws`` binary and the installed ``boto3-s3``
console script as **external processes** against the same S3-compatible
endpoint the host shell is configured for - nothing is mocked. The intended
setup is the local MinIO stack::

    scripts/compose-up.sh
    source scripts/minio-env.sh
    uv run pytest tests/cli/e2e

To run against **real AWS** instead, ``tests/run_e2e.sh`` creates a throwaway
bucket from ``BOTO3_S3_E2E_PROFILE`` / ``BOTO3_S3_E2E_REGION`` and force-removes
it (and its mb/rb siblings) on exit.

The suite is opt-in, gated on ``BOTO3_S3_E2E_BUCKET``:

- env var unset, e2e collected alongside other suites -> every e2e item is
  skipped with a clear reason;
- env var unset, *only* e2e was requested -> the session aborts immediately
  with one message instead of N skips;
- env var set -> the environment is probed once at collection time (the ``aws``
  binary present; the bucket reachable and **empty**, checked via boto3) and the
  session aborts on any failure. The non-empty check is the safety guard against
  pointing the variable at a real, populated bucket.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from botocore.config import Config

if TYPE_CHECKING:
    from collections.abc import Iterator

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
    resolution).
    """
    return


def _is_e2e_item(item: Any) -> bool:
    try:
        return Path(str(item.fspath)).resolve().is_relative_to(_E2E_DIR)
    except (AttributeError, ValueError):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[Any]) -> None:
    if not items:
        return
    e2e_items = [item for item in items if _is_e2e_item(item)]
    if not e2e_items:
        return

    if not os.environ.get(BUCKET_ENV_VAR):
        if len(e2e_items) == len(items):
            # The user asked for the e2e suite explicitly but the env is not
            # configured - fail loudly with one message.
            pytest.exit(f"{BUCKET_ENV_VAR} env var is required for e2e tests", returncode=2)
        skip = pytest.mark.skip(reason=f"{BUCKET_ENV_VAR} not set; skipping e2e suite")
        for item in e2e_items:
            item.add_marker(skip)
        return

    _probe_e2e_environment_or_exit()


def _probe_e2e_environment_or_exit() -> None:
    """Probe the live environment once per session; abort on any failure.

    ``pytest.exit`` (rather than per-test fixture errors) keeps the failure to
    a single clear message. The parity tests drive the high-level ``aws s3``
    commands themselves, so the probe only checks that the ``aws`` binary is
    present; the bucket's reachability and the empty-bucket safety guard go
    through boto3 (the same client the per-test ``_assert_bucket_empty`` uses),
    not a low-level ``aws s3api`` call.
    """
    bucket = os.environ.get(BUCKET_ENV_VAR)
    assert bucket  # checked by the caller
    if shutil.which("aws") is None:
        pytest.exit("aws v2 binary not on PATH", returncode=2)
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


@pytest.fixture(scope="session")
def bucket_name() -> str:
    """The dedicated test bucket name from the env."""
    name = os.environ.get(BUCKET_ENV_VAR)
    assert name, f"{BUCKET_ENV_VAR} should have been verified at session start"
    return name


@pytest.fixture(scope="session")
def s3_client() -> Any:
    """An S3 client built from the host env (incl. ``AWS_ENDPOINT_URL_S3``).

    Built on a fresh ``Session``: the process-global default session caches
    whatever credentials it resolved first, which in a full run are the fake
    ones the other suites force via ``_moto_isolation``.
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
    """Yield the env-supplied test bucket; assert empty before and after.

    The fixture never deletes objects on its own. A non-empty pre-state means
    a previous test forgot to clean up; a non-empty post-state means *this*
    test did. Either fails immediately, so cleanup bugs surface where they
    happen instead of contaminating later tests.
    """
    _assert_bucket_empty(s3_client, bucket_name, when="before")
    yield bucket_name
    _assert_bucket_empty(s3_client, bucket_name, when="after")


def _sibling_bucket(s3_client: Any, name: str) -> Iterator[str]:
    """A bucket the lifecycle (mb/rb) tests own outright.

    The main bucket must stay existing and empty, so those suites work on a
    sibling derived from it (the ``BOTO3_S3_E2E_BUCKET`` opt-in covers the
    derived name). Force-deleted before the test (leftover from a crashed
    run) and after (never left behind). The suffixes must keep the name
    clear of ``-an`` (account-regional BucketNamespace semantics, unverified
    against MinIO) - scenarios append markers like ``--x-s3`` themselves
    when they test them.
    """
    from tests.utils.harness import force_delete_bucket

    force_delete_bucket(s3_client, name)
    yield name
    force_delete_bucket(s3_client, name)


@pytest.fixture
def mb_bucket(s3_client: Any, bucket_name: str) -> Iterator[str]:
    """The mb parity tests' sibling bucket (created by the command itself)."""
    yield from _sibling_bucket(s3_client, f"{bucket_name}-mb")


@pytest.fixture
def rb_bucket(s3_client: Any, bucket_name: str) -> Iterator[str]:
    """The rb parity tests' sibling bucket (pre-created per scenario)."""
    yield from _sibling_bucket(s3_client, f"{bucket_name}-rb")
