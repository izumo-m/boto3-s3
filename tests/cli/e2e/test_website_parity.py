"""Differential parity: ``boto3-s3 website`` vs the real ``aws s3 website``.

The ls-suite contract without seeding (website never touches objects).
Teardown deletes the website configuration best-effort, so a successful put
does not leave website hosting on the shared bucket.
"""

from __future__ import annotations

import contextlib
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
    normalize_rm_stdout,
    run_aws_subprocess,
    run_cli_subprocess,
)
from tests.utils.website_scenarios import SCENARIOS, WebsiteScenario, resolve_argv


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_website_parity(scenario: WebsiteScenario, bucket: str, s3_client: Any) -> None:
    try:
        argv = resolve_argv(scenario, bucket)
        aws_result = run_aws_subprocess(argv)
        ours_result = run_cli_subprocess(argv)
        aws_lines = normalize_rm_stdout(aws_result.stdout, bucket=bucket)
        ours_lines = normalize_rm_stdout(ours_result.stdout, bucket=bucket)

        if not scenario.diff_only and not scenario.frozen_golden:
            if update_goldens_enabled():
                write_golden(
                    "website",
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
                    load_golden("website", scenario.name),
                    rc=aws_result.rc,
                    stdout_lines=aws_lines,
                    side="aws",
                    compare_stdout=scenario.compare_stdout,
                )

        assert ours_result.rc == aws_result.rc, (
            f"[{scenario.name}] exit-code parity broken (charter, design/overview.md section 3):\n"
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
        # A successful put would leave website hosting on the shared bucket.
        with contextlib.suppress(Exception):
            s3_client.delete_bucket_website(Bucket=bucket)
