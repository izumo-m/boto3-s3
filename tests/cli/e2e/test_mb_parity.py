"""Differential parity: ``boto3-s3 mb`` vs the real ``aws s3 mb``.

The rm-suite contract applied to bucket lifecycle: each side gets a sibling
bucket of its own in the same start state (absent, or pre-created), runs,
and is compared on rc (unconditionally - exit-code charter,
design/overview.md section 3), sorted stdout (each side's bucket name
masked), the end state ``(bucket_exists, remaining_keys)``, the tag set
where the scenario asks, and stable stderr tokens. Goldens record the aws-cli
side, end state included.
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
    capture_bucket_tags,
    create_bucket_in_region,
    normalize_rm_stdout,
    run_aws_subprocess,
    run_cli_subprocess,
    settle_bucket_existence,
)
from tests.utils.mb_scenarios import SCENARIOS, MbScenario, resolve_argv


def _prepare(s3_client: Any, bucket: str, scenario: MbScenario) -> None:
    """Put a fresh sibling bucket (endpoint-fidelity T4) into the start state."""
    if scenario.pre_create:
        create_bucket_in_region(s3_client, bucket)


def _settle(s3_client: Any, bucket: str, rc: int) -> None:
    """After a successful mb, wait out real S3's create lag (endpoint-fidelity T4)."""
    if rc == 0:
        settle_bucket_existence(s3_client, bucket, exists=True)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_mb_parity(scenario: MbScenario, mb_buckets: tuple[str, str], s3_client: Any) -> None:
    aws_bucket, ours_bucket = mb_buckets

    _prepare(s3_client, aws_bucket, scenario)
    aws_result = run_aws_subprocess(resolve_argv(scenario, aws_bucket))
    _settle(s3_client, aws_bucket, aws_result.rc)
    aws_exists, aws_remaining = capture_bucket_state(s3_client, aws_bucket)
    aws_tags = capture_bucket_tags(s3_client, aws_bucket) if scenario.capture_tags else None

    _prepare(s3_client, ours_bucket, scenario)
    ours_result = run_cli_subprocess(resolve_argv(scenario, ours_bucket))
    _settle(s3_client, ours_bucket, ours_result.rc)
    ours_exists, ours_remaining = capture_bucket_state(s3_client, ours_bucket)
    ours_tags = capture_bucket_tags(s3_client, ours_bucket) if scenario.capture_tags else None

    aws_lines = normalize_rm_stdout(aws_result.stdout, bucket=aws_bucket)
    ours_lines = normalize_rm_stdout(ours_result.stdout, bucket=ours_bucket)

    if not scenario.diff_only and not scenario.frozen_golden:
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
        f"[{scenario.name}] exit-code parity broken (charter, design/overview.md section 3):\n"
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
    assert ours_tags == aws_tags, (
        f"[{scenario.name}] bucket-tag parity broken:\n  ours: {ours_tags!r}\n  aws:  {aws_tags!r}"
    )
    assert_stderr_tokens(
        scenario.expected_stderr_tokens_ours,
        ours_result.stderr,
        side="ours",
        scenario=scenario.name,
        require_empty=scenario.stderr_exact_empty,
    )
    assert_stderr_tokens(
        scenario.expected_stderr_tokens_aws,
        aws_result.stderr,
        side="aws",
        scenario=scenario.name,
        require_empty=scenario.stderr_exact_empty,
    )
