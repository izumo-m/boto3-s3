"""Golden replay: ``boto3-s3 rb`` on moto must reproduce what aws-cli did.

Same contract as ``test_mb_golden``; rb scenarios additionally seed objects
into the pre-created sibling bucket for the non-empty cases.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.functional.conftest import FUNCTIONAL_BUCKET
from tests.utils.golden import assert_matches_golden, load_golden
from tests.utils.harness import (
    assert_stderr_tokens,
    capture_bucket_state,
    normalize_rm_stdout,
    run_cli_in_process,
    seed_bucket,
)
from tests.utils.rb_scenarios import SCENARIOS, RbScenario, resolve_argv

_REPLAYABLE = [scenario for scenario in SCENARIOS if not scenario.diff_only]

_RB_BUCKET = f"{FUNCTIONAL_BUCKET}-rb"


@pytest.mark.parametrize("scenario", _REPLAYABLE, ids=lambda s: s.name)
def test_rb_matches_golden(scenario: RbScenario, moto_s3: Any) -> None:
    if scenario.pre_create:
        moto_s3.create_bucket(Bucket=_RB_BUCKET)
        seed_bucket(moto_s3, _RB_BUCKET, scenario.seed)
    result = run_cli_in_process(resolve_argv(scenario, _RB_BUCKET))
    lines = normalize_rm_stdout(result.stdout, bucket=_RB_BUCKET)
    exists, remaining = capture_bucket_state(moto_s3, _RB_BUCKET)
    assert_matches_golden(
        load_golden("rb", scenario.name),
        rc=result.rc,
        stdout_lines=lines,
        side="ours",
        compare_stdout=scenario.compare_stdout,
        remaining_keys=remaining,
        bucket_exists=exists,
    )
    assert_stderr_tokens(
        scenario.expected_stderr_tokens_ours, result.stderr, side="ours", scenario=scenario.name
    )
