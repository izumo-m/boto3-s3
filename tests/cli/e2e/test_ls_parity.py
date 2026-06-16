"""Differential parity: ``boto3-s3 ls`` vs the real ``aws s3 ls``.

For each scenario both CLIs run as subprocesses against the same live
endpoint and the same seeded keys (``ls`` is read-only, so the sides share
one seeding). Two layers of assertions:

1. **Golden persist/check** - under ``UPDATE_GOLDENS=1`` the aws capture is
   written to ``tests/cli/goldens/ls/``; otherwise the live aws output must
   still match the committed golden (detects aws-cli drift after upgrades).
   Runs *before* the diff so the aws truth is recorded even while our side
   is divergent.
2. **Live diff** - exit codes must match for every scenario, unconditionally
   (exit-code charter, docs/overview.md section 3); normalized stdout must match
   unless the scenario opted out (``compare_stdout=False``); stderr is probed
   for stable tokens only.
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
    normalize_ls_stdout,
    run_aws_subprocess,
    run_cli_subprocess,
    seed_bucket,
)
from tests.utils.ls_scenarios import SCENARIOS, LsScenario, resolve_argv


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_ls_parity(scenario: LsScenario, bucket: str, s3_client: Any) -> None:
    seed_bucket(s3_client, bucket, scenario.seed)
    try:
        argv = resolve_argv(scenario, bucket)
        aws_result = run_aws_subprocess(argv)
        ours_result = run_cli_subprocess(argv)
        aws_lines = normalize_ls_stdout(aws_result.stdout, bucket=bucket)
        ours_lines = normalize_ls_stdout(ours_result.stdout, bucket=bucket)

        if not scenario.diff_only:
            if update_goldens_enabled():
                write_golden(
                    "ls",
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
                    load_golden("ls", scenario.name),
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
    finally:
        delete_keys(s3_client, bucket, *scenario.seed)
