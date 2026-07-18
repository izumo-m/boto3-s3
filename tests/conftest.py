"""Pytest config shared by every suite under ``tests/``.

Forces moto-friendly fake AWS credentials so the host's real
``~/.aws/credentials``, ``AWS_PROFILE``, or ``AWS_ENDPOINT_URL_S3`` cannot
leak into moto-backed or recorder-backed tests and trigger
``InvalidAccessKeyId`` (or, worse, target a real S3). The override is applied
per-test via ``monkeypatch``.

The e2e suite at ``tests/cli/e2e/`` talks to a **real** S3-compatible
endpoint (MinIO via ``scripts/minio-env.sh``, or real S3) and needs the host
env intact, so ``tests/cli/e2e/conftest.py`` overrides the ``_moto_isolation``
fixture defined here with a no-op of the same name (pytest's
closer-conftest-wins resolution).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.utils.host import is_case_insensitive

if TYPE_CHECKING:
    from collections.abc import Iterator

# Test-only knob: point this at a case-insensitive directory to run the
# ``--case-conflict`` ``*_with_existing_file`` tests on an otherwise
# case-sensitive host (see `case_insensitive_workdir`). The ``_PYTEST_`` infix
# marks it as test infrastructure, not a CLI/library setting; it is not
# ``_E2E_`` because these tests need no live endpoint.
CASE_INSENSITIVE_DIR_ENV = "BOTO3_S3_PYTEST_CASE_INSENSITIVE_DIR"


@pytest.fixture
def case_insensitive_workdir(tmp_path: Path) -> Iterator[Path]:
    """A writable directory on a CASE-INSENSITIVE filesystem, or skip the test.

    The ``--case-conflict`` ``*_with_existing_file`` tests detect the conflict
    through ``os.path.exists``, which only sees a case-variant when the
    destination is case-insensitive. Resolution order:

    1. ``$BOTO3_S3_PYTEST_CASE_INSENSITIVE_DIR`` - a case-insensitive directory
       to use; a unique subdirectory is created under it and removed. On a
       case-sensitive Linux host point it at e.g. ``/mnt/c/...`` under WSL2 (the
       mounted Windows drive is case-insensitive), or a ``ciopfs`` / ``vfat``
       mount. ``tests/run_case_insensitive_fs.sh`` sets it automatically.
    2. ``tmp_path`` when it is itself case-insensitive - macOS / Windows run
       these as part of the normal suite, no setup.
    3. Otherwise the test skips (a case-sensitive Linux host with nothing set).
    """
    base = os.environ.get(CASE_INSENSITIVE_DIR_ENV)
    if base:
        root = Path(base)
        root.mkdir(parents=True, exist_ok=True)
        if not is_case_insensitive(root):
            pytest.skip(f"{CASE_INSENSITIVE_DIR_ENV}={base} is not a case-insensitive directory")
        work = Path(tempfile.mkdtemp(prefix="boto3s3-cc-", dir=root))
        try:
            yield work
        finally:
            shutil.rmtree(work, ignore_errors=True)
    elif is_case_insensitive(tmp_path):
        yield tmp_path
    else:
        pytest.skip(
            f"requires a case-insensitive filesystem (set {CASE_INSENSITIVE_DIR_ENV} to one, "
            "e.g. /mnt/c/... on WSL2, or run on macOS / Windows)"
        )


@pytest.fixture(scope="session")
def _classic_aws_config(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A session config file pinning the transfer engine to classic.

    The CLI reads the profile's ``[s3]`` section (runtime config), so the
    host's ``~/.aws/config`` could otherwise leak tuning - or, on a
    CRT-optimized host, silently resolve ``auto`` to an engine moto cannot
    intercept. Subprocess CLI runs inherit this via ``AWS_CONFIG_FILE``.
    """
    config = tmp_path_factory.mktemp("aws-config") / "config"
    config.write_text("[default]\ns3 =\n  preferred_transfer_client = classic\n")
    return config


@pytest.fixture(autouse=True)
def _moto_isolation(monkeypatch: pytest.MonkeyPatch, _classic_aws_config: Path) -> None:
    """Force fake AWS credentials so moto never sees the host's real ones."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(_classic_aws_config))
    monkeypatch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE", str(_classic_aws_config.parent / "credentials-absent")
    )
    for leak in ("AWS_PROFILE", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"):
        monkeypatch.delenv(leak, raising=False)
    # A prior test's boto3.client() / session-less S3() installs a
    # process-global default session built under THAT test's env, and
    # AwsConfig.from_session(None) deliberately reuses the installed session
    # (the one the zero-config clients actually use). Reset it per test so a
    # config read never sees a stale session's cached AWS_CONFIG_FILE.
    import boto3

    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)


@pytest.fixture(autouse=True)
def _fail_on_recorder_exhaustion() -> Iterator[None]:
    """Fail any test whose recording client was called past its script.

    The recorder raises AssertionError at the call site, but on the transfer
    path that happens on an s3transfer worker thread where the engine folds it
    into an ordinary FAILED item - a ``pytest.raises(BatchError)`` test would
    then pass for the wrong reason. Draining the recorder's exhaustion log at
    teardown makes the harness bug loud wherever the AssertionError landed.
    """
    yield
    from tests.utils import recorder

    events, recorder.exhausted_calls[:] = list(recorder.exhausted_calls), []
    assert not events, f"recording client called past its scripted responses: {events}"


@pytest.fixture(autouse=True)
def _pin_classic_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the in-process 'auto' engine preference to classic.

    The library's default ``preferred_transfer_client='auto'`` consults
    ``awscrt.s3.is_optimized_for_system()`` (boto3 semantics), so on an
    optimized host every fake-client/moto transfer test would try to build a
    real CRT client. Pinning the probe keeps the suites deterministic; tests
    that exercise the CRT path monkeypatch their own answer, and the e2e CRT
    lane runs the CLI in a subprocess this fixture cannot reach.
    """
    try:
        import awscrt.s3
    except ImportError:  # pragma: no cover - awscrt is a dev dependency
        return
    # raising=False: an old awscrt (< 0.19.x) has no is_optimized_for_system;
    # the library never calls it there (has_minimum_crt_version gates first),
    # and erroring the autouse fixture would fail every test at the SDK floor.
    monkeypatch.setattr(awscrt.s3, "is_optimized_for_system", lambda: False, raising=False)
