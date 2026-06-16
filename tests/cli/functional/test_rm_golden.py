"""Golden replay: ``boto3-s3 rm`` on moto must reproduce what aws-cli did.

Same contract as ``test_ls_golden`` plus the destructive-command extras: the
stdout comparison is order-relaxed (sorted normalization), so the bucket end
state recorded in the golden (``remaining_keys``) is verified too.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.functional.conftest import FUNCTIONAL_BUCKET
from tests.utils.golden import assert_matches_golden, load_golden
from tests.utils.harness import (
    assert_stderr_tokens,
    normalize_rm_stdout,
    remaining_keys,
    run_cli_in_process,
    seed_bucket,
)
from tests.utils.rm_scenarios import SCENARIOS, RmScenario, resolve_argv

_REPLAYABLE = [scenario for scenario in SCENARIOS if not scenario.diff_only]


@pytest.mark.parametrize("scenario", _REPLAYABLE, ids=lambda s: s.name)
def test_rm_matches_golden(scenario: RmScenario, moto_s3: Any) -> None:
    seed_bucket(moto_s3, FUNCTIONAL_BUCKET, scenario.seed)
    result = run_cli_in_process(resolve_argv(scenario, FUNCTIONAL_BUCKET))
    lines = normalize_rm_stdout(result.stdout, bucket=FUNCTIONAL_BUCKET)
    assert_matches_golden(
        load_golden("rm", scenario.name),
        rc=result.rc,
        stdout_lines=lines,
        side="ours",
        compare_stdout=scenario.compare_stdout,
        remaining_keys=remaining_keys(moto_s3, FUNCTIONAL_BUCKET),
    )
    assert_stderr_tokens(
        scenario.expected_stderr_tokens_ours, result.stderr, side="ours", scenario=scenario.name
    )
