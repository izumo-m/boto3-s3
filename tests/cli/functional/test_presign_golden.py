"""Golden replay: ``boto3-s3 presign`` must reproduce what aws-cli emitted.

Presign is pure client-side computation, so unlike the other golden suites
this one needs no moto backend and no seeding - the in-process run only
consumes the fake credentials/region the root conftest exports, and the
normalizer (``harness.normalize_presign_stdout``) folds the remaining
environment differences away: the replay's virtual-host URL (no endpoint
override) canonicalizes to the capture's path-style (MinIO IP endpoint),
and the credential/date/signature values are masked.

``AWS_SESSION_TOKEN`` is removed per test: the root conftest exports one,
which would grow the URL an ``X-Amz-Security-Token`` parameter the
token-less MinIO capture does not have.
"""

from __future__ import annotations

import pytest

from tests.cli.functional.conftest import FUNCTIONAL_BUCKET
from tests.utils.golden import assert_matches_golden, load_golden
from tests.utils.harness import (
    assert_stderr_tokens,
    normalize_presign_stdout,
    run_cli_in_process,
)
from tests.utils.presign_scenarios import SCENARIOS, PresignScenario, resolve_argv


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_presign_matches_golden(scenario: PresignScenario, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    result = run_cli_in_process(resolve_argv(scenario, FUNCTIONAL_BUCKET))
    lines = normalize_presign_stdout(result.stdout, bucket=FUNCTIONAL_BUCKET)
    golden = load_golden("presign", scenario.name)
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
