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

from pathlib import Path

import pytest


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
    monkeypatch.setattr(awscrt.s3, "is_optimized_for_system", lambda: False)
