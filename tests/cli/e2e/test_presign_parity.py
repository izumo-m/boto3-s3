"""Differential parity: ``boto3-s3 presign`` vs the real ``aws s3 presign``.

The ls-suite contract (read-only, one shared seeding, golden persist/check
before the live diff) plus a presign-only third layer: for ``fetch=True``
scenarios both sides' URLs are actually GET - the endpoint accepting our
signature like it accepts aws's is parity the URL string alone cannot
prove. Status must match; on 200 the bodies must match too (404 bodies
carry per-request IDs and are not compared).
"""

from __future__ import annotations

import difflib
from typing import Any

import pytest

from tests.utils.golden import (
    Golden,
    assert_matches_golden,
    detect_aws_version,
    load_golden,
    update_goldens_enabled,
    write_golden,
)
from tests.utils.harness import (
    assert_stderr_tokens,
    delete_keys,
    fetch_url,
    normalize_presign_stdout,
    run_aws_subprocess,
    run_cli_subprocess,
    seed_bucket,
)
from tests.utils.presign_scenarios import SCENARIOS, PresignScenario, resolve_argv


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_presign_parity(scenario: PresignScenario, bucket: str, s3_client: Any) -> None:
    seed_bucket(s3_client, bucket, scenario.seed)
    try:
        argv = resolve_argv(scenario, bucket)
        aws_result = run_aws_subprocess(argv)
        ours_result = run_cli_subprocess(argv)
        aws_lines = normalize_presign_stdout(aws_result.stdout, bucket=bucket)
        ours_lines = normalize_presign_stdout(ours_result.stdout, bucket=bucket)

        if update_goldens_enabled():
            write_golden(
                "presign",
                Golden(
                    scenario=scenario.name,
                    argv=list(scenario.argv),
                    rc=aws_result.rc,
                    stdout_lines=aws_lines,
                    aws_version=detect_aws_version(),
                ),
            )
        else:
            assert_matches_golden(
                load_golden("presign", scenario.name),
                rc=aws_result.rc,
                stdout_lines=aws_lines,
                side="aws",
                compare_stdout=scenario.compare_stdout,
            )

        assert ours_result.rc == aws_result.rc, (
            f"[{scenario.name}] exit-code parity broken (charter, docs/overview.md section 3):\n"
            f"  ours rc={ours_result.rc} stderr={ours_result.stderr.strip()!r}\n"
            f"  aws  rc={aws_result.rc} stderr={aws_result.stderr.strip()!r}"
        )
        if scenario.compare_stdout and ours_lines != aws_lines:
            diff = "\n".join(
                difflib.unified_diff(
                    aws_lines, ours_lines, fromfile="aws", tofile="ours", lineterm=""
                )
            )
            pytest.fail(f"[{scenario.name}] stdout parity broken:\n{diff}")
        assert_stderr_tokens(
            scenario.expected_stderr_tokens_ours,
            ours_result.stderr,
            side="ours",
            scenario=scenario.name,
        )
        assert_stderr_tokens(
            scenario.expected_stderr_tokens_aws,
            aws_result.stderr,
            side="aws",
            scenario=scenario.name,
        )

        if scenario.fetch:
            aws_status, aws_body = fetch_url(aws_result.stdout.strip())
            ours_status, ours_body = fetch_url(ours_result.stdout.strip())
            assert ours_status == aws_status, (
                f"[{scenario.name}] fetch parity broken: the endpoint answered "
                f"{ours_status} for our URL but {aws_status} for aws's"
            )
            if aws_status == 200:
                assert ours_body == aws_body, f"[{scenario.name}] fetched bodies differ"
    finally:
        delete_keys(s3_client, bucket, *scenario.seed)
