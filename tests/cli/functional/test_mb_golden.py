"""Golden replay: ``boto3-s3 mb`` on moto must reproduce what aws-cli did.

Same contract as ``test_rm_golden`` plus the bucket-lifecycle extras: the
scenario bucket is a sibling of ``FUNCTIONAL_BUCKET`` (created by the command
under test, not the fixture) and the golden's end state is
``(bucket_exists, remaining_keys)``.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.functional.conftest import FUNCTIONAL_BUCKET
from tests.utils.golden import assert_matches_golden, load_golden
from tests.utils.harness import (
    assert_stderr_tokens,
    capture_bucket_state,
    capture_bucket_tags,
    normalize_rm_stdout,
    run_cli_in_process,
)
from tests.utils.mb_scenarios import SCENARIOS, MbScenario, resolve_argv

_REPLAYABLE = [scenario for scenario in SCENARIOS if not scenario.diff_only]

# Sibling of the fixture bucket: mb must be able to create it itself. Fresh
# per test - the moto backend lives only inside the fixture's mock_aws().
_MB_BUCKET = f"{FUNCTIONAL_BUCKET}-mb"


@pytest.mark.parametrize("scenario", _REPLAYABLE, ids=lambda s: s.name)
def test_mb_matches_golden(scenario: MbScenario, moto_s3: Any) -> None:
    if scenario.pre_create:
        moto_s3.create_bucket(Bucket=_MB_BUCKET)
    result = run_cli_in_process(resolve_argv(scenario, _MB_BUCKET))
    lines = normalize_rm_stdout(result.stdout, bucket=_MB_BUCKET)
    exists, remaining = capture_bucket_state(moto_s3, _MB_BUCKET)
    tags = capture_bucket_tags(moto_s3, _MB_BUCKET) if scenario.capture_tags else None
    assert_matches_golden(
        load_golden("mb", scenario.name),
        rc=result.rc,
        stdout_lines=lines,
        side="ours",
        compare_stdout=scenario.compare_stdout,
        remaining_keys=remaining,
        bucket_exists=exists,
        bucket_tags=tags,
    )
    assert_stderr_tokens(
        scenario.expected_stderr_tokens_ours, result.stderr, side="ours", scenario=scenario.name
    )
