"""Golden replay: ``boto3-s3 ls`` on moto must match what aws-cli did on S3.

Each scenario seeds moto with the exact layout the e2e suite seeded on
MinIO, runs the CLI in-process, and compares the normalized output against
the committed golden (captured from the real aws-cli by
``tests/cli/e2e/test_ls_parity.py`` under ``UPDATE_GOLDENS=1``).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.functional.conftest import FUNCTIONAL_BUCKET
from tests.utils.golden import assert_matches_golden, load_golden
from tests.utils.harness import (
    assert_stderr_tokens,
    normalize_ls_stdout,
    run_cli_in_process,
    seed_bucket,
)
from tests.utils.ls_scenarios import SCENARIOS, LsScenario, resolve_argv

_REPLAYABLE = [scenario for scenario in SCENARIOS if not scenario.diff_only]


@pytest.mark.parametrize("scenario", _REPLAYABLE, ids=lambda s: s.name)
def test_ls_matches_golden(scenario: LsScenario, moto_s3: Any) -> None:
    seed_bucket(moto_s3, FUNCTIONAL_BUCKET, scenario.seed)
    result = run_cli_in_process(resolve_argv(scenario, FUNCTIONAL_BUCKET))
    lines = normalize_ls_stdout(result.stdout, bucket=FUNCTIONAL_BUCKET)
    golden = load_golden("ls", scenario.name)
    assert_matches_golden(
        golden,
        rc=result.rc,
        stdout_lines=lines,
        side="ours",
        compare_stdout=scenario.compare_stdout,
    )
    assert_stderr_tokens(
        scenario.expected_stderr_tokens_ours, result.stderr, side="ours", scenario=scenario.name
    )
