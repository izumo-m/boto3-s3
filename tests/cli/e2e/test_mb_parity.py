"""Differential parity: ``boto3-s3 mb`` vs the real ``aws s3 mb``.

The rm-suite contract applied to bucket lifecycle: each side starts from an
identical sibling-bucket state (``force_delete_bucket`` + optional
pre-create), runs, and is compared on rc (unconditionally - exit-code
charter, docs/overview.md section 3), sorted stdout, the end state
``(bucket_exists, remaining_keys)``, and stable stderr tokens. Goldens
record the aws side, end state included.
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
    capture_bucket_state,
    force_delete_bucket,
    normalize_rm_stdout,
    run_aws_subprocess,
    run_cli_subprocess,
)
from tests.utils.mb_scenarios import SCENARIOS, MbScenario, resolve_argv


def _reset(s3_client: Any, bucket: str, scenario: MbScenario) -> None:
    """Put the sibling bucket into the scenario's start state."""
    force_delete_bucket(s3_client, bucket)
    if scenario.pre_create:
        s3_client.create_bucket(Bucket=bucket)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_mb_parity(scenario: MbScenario, mb_bucket: str, s3_client: Any) -> None:
    argv = resolve_argv(scenario, mb_bucket)

    _reset(s3_client, mb_bucket, scenario)
    aws_result = run_aws_subprocess(argv)
    aws_exists, aws_remaining = capture_bucket_state(s3_client, mb_bucket)

    _reset(s3_client, mb_bucket, scenario)
    ours_result = run_cli_subprocess(argv)
    ours_exists, ours_remaining = capture_bucket_state(s3_client, mb_bucket)

    aws_lines = normalize_rm_stdout(aws_result.stdout, bucket=mb_bucket)
    ours_lines = normalize_rm_stdout(ours_result.stdout, bucket=mb_bucket)

    if not scenario.diff_only:
        if update_goldens_enabled():
            write_golden(
                "mb",
                Golden(
                    scenario=scenario.name,
                    argv=list(scenario.argv),
                    rc=aws_result.rc,
                    stdout_lines=aws_lines,
                    aws_version=detect_aws_version(),
                    remaining_keys=aws_remaining,
                    bucket_exists=aws_exists,
                ),
            )
        else:
            assert_matches_golden(
                load_golden("mb", scenario.name),
                rc=aws_result.rc,
                stdout_lines=aws_lines,
                side="aws",
                compare_stdout=scenario.compare_stdout,
                remaining_keys=aws_remaining,
                bucket_exists=aws_exists,
            )

    assert ours_result.rc == aws_result.rc, (
        f"[{scenario.name}] exit-code parity broken (charter, docs/overview.md section 3):\n"
        f"  ours rc={ours_result.rc} stderr={ours_result.stderr.strip()!r}\n"
        f"  aws  rc={aws_result.rc} stderr={aws_result.stderr.strip()!r}"
    )
    if scenario.compare_stdout and ours_lines != aws_lines:
        diff = "\n".join(
            difflib.unified_diff(aws_lines, ours_lines, fromfile="aws", tofile="ours", lineterm="")
        )
        pytest.fail(f"[{scenario.name}] stdout parity broken:\n{diff}")
    assert (ours_exists, ours_remaining) == (aws_exists, aws_remaining), (
        f"[{scenario.name}] end-state parity broken:\n"
        f"  ours: exists={ours_exists} keys={ours_remaining!r}\n"
        f"  aws:  exists={aws_exists} keys={aws_remaining!r}"
    )
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
