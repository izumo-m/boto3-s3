"""Differential parity: ``boto3-s3 rm`` vs the real ``aws s3 rm``.

``rm`` is destructive, so unlike the ls suite the two sides cannot share one
seeding: the bucket is seeded, aws runs and its end state is captured, then
the bucket is reset and re-seeded for the boto3-s3 run. Assertion layers:

1. **Golden persist/check** - as in ls, plus the aws-side end state
   (``remaining_keys``) is recorded: the stdout normalization sorts delete
   lines (aws's output order is parallel-completion order, nondeterministic),
   and the end state pins what sorting relaxes.
2. **Live diff** - exit codes must match for every scenario, unconditionally
   (exit-code charter, docs/overview.md section 3; note rm's rc-1-on-error shape);
   sorted stdout must match unless ``compare_stdout=False``; the end states
   must match for every scenario; stderr is probed for stable tokens only.
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
    normalize_rm_stdout,
    remaining_keys,
    run_aws_subprocess,
    run_cli_subprocess,
    seed_bucket,
)
from tests.utils.rm_scenarios import SCENARIOS, RmScenario, resolve_argv


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_rm_parity(scenario: RmScenario, bucket: str, s3_client: Any) -> None:
    argv = resolve_argv(scenario, bucket)
    try:
        seed_bucket(s3_client, bucket, scenario.seed)
        aws_result = run_aws_subprocess(argv)
        aws_remaining = remaining_keys(s3_client, bucket)

        # Reset to the identical start state for the boto3-s3 side.
        delete_keys(s3_client, bucket, *scenario.seed)
        seed_bucket(s3_client, bucket, scenario.seed)
        ours_result = run_cli_subprocess(argv)
        ours_remaining = remaining_keys(s3_client, bucket)

        aws_lines = normalize_rm_stdout(aws_result.stdout, bucket=bucket)
        ours_lines = normalize_rm_stdout(ours_result.stdout, bucket=bucket)

        if not scenario.diff_only:
            if update_goldens_enabled():
                write_golden(
                    "rm",
                    Golden(
                        scenario=scenario.name,
                        argv=list(scenario.argv),
                        rc=aws_result.rc,
                        stdout_lines=aws_lines,
                        aws_version=detect_aws_version(),
                        remaining_keys=aws_remaining,
                    ),
                )
            else:
                assert_matches_golden(
                    load_golden("rm", scenario.name),
                    rc=aws_result.rc,
                    stdout_lines=aws_lines,
                    side="aws",
                    compare_stdout=scenario.compare_stdout,
                    remaining_keys=aws_remaining,
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
        assert ours_remaining == aws_remaining, (
            f"[{scenario.name}] end-state parity broken:\n"
            f"  ours left {ours_remaining!r}\n"
            f"  aws  left {aws_remaining!r}"
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
    finally:
        delete_keys(s3_client, bucket, *scenario.seed)
