"""CRT-engine parity: ``boto3-s3`` vs ``aws s3`` with ``preferred_transfer_client = crt``.

Both CLIs run against the same endpoint with a temporary
``AWS_CONFIG_FILE`` selecting the CRT transfer client. Upload / download / mv /
sync exercise the real ``CRTTransferManager``. The rm and upload-sync-delete
cases cover S3 deletion at the CLI boundary because boto3-s3's accepted
``DeleteObject`` / batched ``S3Deleter`` routes do not share aws-cli's per-key
CRT transport; download sync-delete covers the local deletion both CLIs
perform. The lane is differential - no goldens - and asserts that our
CRT-configured mode agrees with aws's on rc, stdout, the bucket end state, the
local destination and source trees, and the download mtime.

Gated twice: the e2e ``BOTO3_S3_E2E_BUCKET`` opt-in (conftest) and an
``awscrt`` import check here (the CRT manager needs it). Against a custom
endpoint (MinIO) the aws side needs an explicit ``--endpoint-url``: aws's CRT
client reads ``use_ssl`` from the CLI argument only, so an env-only
``AWS_ENDPOINT_URL_S3`` makes it dial TLS to an http endpoint and fail.
Our client derives the endpoint from the resolved boto3 client, so
it does not need the flag - we pass it to both sides for symmetry.
"""

from __future__ import annotations

import difflib
import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

from tests.utils.crt_scenarios import (
    SCENARIOS,
    CpScenario,
    materialize_workdir,
    resolve_argv,
    seed_remote,
)
from tests.utils.harness import (
    assert_stderr_tokens,
    capture_local_tree,
    delete_under,
    normalize_cp_stdout,
    remaining_keys,
    run_aws_subprocess_with_stdin,
    run_cli_subprocess_with_stdin,
)

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("awscrt") is None,
    reason="awscrt not installed; the CRT transfer engine is unavailable",
)


@pytest.fixture(scope="session")
def crt_config_file(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A config file selecting the CRT transfer client for both CLIs."""
    config = tmp_path_factory.mktemp("crt-config") / "config"
    config.write_text("[default]\ns3 =\n  preferred_transfer_client = crt\n")
    return str(config)


def _crt_invocation(argv: list[str], crt_config_file: str) -> tuple[list[str], dict[str, str]]:
    """Append the endpoint (custom endpoints only) and the CRT config env."""
    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3")
    if endpoint:
        argv = [*argv, "--endpoint-url", endpoint]
    return argv, {"AWS_CONFIG_FILE": crt_config_file}


def _run_side(
    runner: Any,
    scenario: CpScenario,
    bucket: str,
    s3_client: Any,
    workdir: Path,
    crt_config_file: str,
) -> tuple[Any, list[str], list[str], list[str] | None, list[str]]:
    workdir.mkdir(parents=True, exist_ok=True)
    materialize_workdir(workdir, scenario)
    delete_under(s3_client, bucket, "")
    seed_remote(s3_client, bucket, scenario)
    argv, env = _crt_invocation(resolve_argv(scenario, bucket), crt_config_file)
    result = runner(argv, cwd=str(workdir), stdin_payload=scenario.stdin, env=env)
    lines = normalize_cp_stdout(result.stdout, bucket=bucket)
    remaining = remaining_keys(s3_client, bucket)
    tree = capture_local_tree(str(workdir / "dest")) if scenario.capture_tree else None
    # The source side (empty for downloads); an mv upload must empty it, which
    # only a live src/ capture can catch.
    src_tree = capture_local_tree(str(workdir / "src"))
    if scenario.mtime_key is not None:
        key, rel_path = scenario.mtime_key
        last_modified = s3_client.head_object(Bucket=bucket, Key=key)["LastModified"]
        stamped = os.stat(workdir / rel_path).st_mtime
        assert abs(stamped - last_modified.timestamp()) < 2, (
            f"[{scenario.name}] CRT download mtime {stamped} != LastModified {last_modified}"
        )
    return result, lines, remaining, tree, src_tree


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_crt_parity(
    scenario: CpScenario,
    bucket: str,
    s3_client: Any,
    tmp_path: Path,
    crt_config_file: str,
) -> None:
    try:
        aws_result, aws_lines, aws_remaining, aws_tree, aws_src = _run_side(
            run_aws_subprocess_with_stdin,
            scenario,
            bucket,
            s3_client,
            tmp_path / "aws",
            crt_config_file,
        )
        our_result, our_lines, our_remaining, our_tree, our_src = _run_side(
            run_cli_subprocess_with_stdin,
            scenario,
            bucket,
            s3_client,
            tmp_path / "ours",
            crt_config_file,
        )

        assert our_result.rc == aws_result.rc, (
            f"[{scenario.name}] exit-code parity broken under CRT (charter):\n"
            f"  ours rc={our_result.rc} stderr={our_result.stderr.strip()!r}\n"
            f"  aws  rc={aws_result.rc} stderr={aws_result.stderr.strip()!r}"
        )
        if scenario.compare_stdout and our_lines != aws_lines:
            diff = "\n".join(
                difflib.unified_diff(
                    aws_lines, our_lines, fromfile="aws", tofile="ours", lineterm=""
                )
            )
            pytest.fail(f"[{scenario.name}] CRT stdout parity broken:\n{diff}")
        assert our_remaining == aws_remaining, (
            f"[{scenario.name}] bucket end-state parity broken under CRT:\n"
            f"  ours: {our_remaining!r}\n  aws:  {aws_remaining!r}"
        )
        assert our_tree == aws_tree, (
            f"[{scenario.name}] local end-state parity broken under CRT:\n"
            f"  ours: {our_tree!r}\n  aws:  {aws_tree!r}"
        )
        assert our_src == aws_src, (
            f"[{scenario.name}] local source-tree parity broken under CRT:\n"
            f"  ours: {our_src!r}\n  aws:  {aws_src!r}"
        )
        assert_stderr_tokens(
            scenario.expected_stderr_tokens_ours,
            our_result.stderr,
            side="ours",
            scenario=scenario.name,
        )
    finally:
        delete_under(s3_client, bucket, "")


def test_our_cli_actually_engages_crt(
    bucket: str, s3_client: Any, tmp_path: Path, crt_config_file: str
) -> None:
    """Guard against a silent classic fallback: --debug shows the CRT engine.

    A differential pass alone cannot tell CRT from classic (the output is the
    same), so this pins that our side really built the CRT manager.

    The signal is the transfer-time breadcrumb ``Transferrer._get_manager``
    emits (``transfer engine: <class>``), which names the engine that actually
    transferred *after* any CRT->classic fallback. The ``s3transfer.crt``
    throughput log fires at CRT *client construction*, before the
    compatibility gate that can still fall back to classic, so it cannot tell a
    real CRT transfer from a fallback - this asserts the manager class instead.
    """
    source = tmp_path / "a.txt"
    source.write_bytes(b"engage crt\n")
    argv, env = _crt_invocation(
        ["cp", str(source), f"s3://{bucket}/crt-engage.txt", "--debug"], crt_config_file
    )
    try:
        result = run_cli_subprocess_with_stdin(argv, cwd=str(tmp_path), env=env)
        assert result.rc == 0, result.stderr
        assert "transfer engine: CRTTransferManager" in result.stderr, (
            "expected the CRT engine breadcrumb; our CLI may have fallen back to classic.\n"
            f"stderr:\n{result.stderr}"
        )
    finally:
        delete_under(s3_client, bucket, "")


def test_crt_ignores_classic_only_config(
    bucket: str, s3_client: Any, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """``[s3]`` classic-only knobs under CRT are ignored, not fatal (charter).

    aws-cli's CRT client never reads ``io_chunksize`` / ``max_bandwidth``, so a
    CRT transfer with them set in ``[s3]`` completes (rc 0). Our CLI must not
    forward them onto the crt-preferred ``TransferConfig`` either - doing so
    trips boto3's ``_validate_crt_transfer_config`` and would exit rc 1 with an
    uncaught traceback. Differential: both CLIs must agree on rc 0.
    """
    config = tmp_path_factory.mktemp("crt-classic-keys") / "config"
    config.write_text(
        "[default]\ns3 =\n"
        "  preferred_transfer_client = crt\n"
        "  io_chunksize = 1MB\n"
        "  max_bandwidth = 50MB/s\n"
    )
    source = tmp_path / "payload.bin"
    source.write_bytes(os.urandom(1024 * 1024))
    try:
        results = {}
        for side, runner in (
            ("ours", run_cli_subprocess_with_stdin),
            ("aws", run_aws_subprocess_with_stdin),
        ):
            argv, env = _crt_invocation(
                ["cp", str(source), f"s3://{bucket}/{side}-classic-keys.bin"], str(config)
            )
            results[side] = runner(argv, cwd=str(tmp_path), env=env)
        assert results["ours"].rc == 0, (
            f"classic-only [s3] keys must not be fatal under CRT (charter):\n"
            f"  stderr:\n{results['ours'].stderr}"
        )
        assert results["ours"].rc == results["aws"].rc, (
            f"rc parity under CRT broken: ours={results['ours'].rc} aws={results['aws'].rc}"
        )
    finally:
        delete_under(s3_client, bucket, "")
