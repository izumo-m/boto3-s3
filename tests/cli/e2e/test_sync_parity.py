"""Differential parity: ``boto3-s3 sync`` vs the real ``aws s3 sync``.

The cp parity harness with sync's twist: both end states matter at once -
``remaining_keys`` pins the bucket (uploads, copies, S3-side ``--delete``)
and ``local_tree`` pins ``dest/`` (downloads, local-side ``--delete``).
``CpScenario.local_mtimes`` makes the time judgments deterministic: local
files are stamped a day around "now" while the seeds land at ~now, so the
same skew holds for both sides regardless of run timing. The source tree
needs no capture - sync never mutates its source.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path
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
    CliResult,
    assert_stderr_tokens,
    capture_local_tree,
    delete_under,
    had_progress_lines,
    head_object_fields,
    normalize_cp_stdout,
    remaining_keys,
    run_aws_subprocess_with_stdin,
    run_cli_subprocess_with_stdin,
)
from tests.utils.sync_scenarios import (
    SCENARIOS,
    CpScenario,
    materialize_workdir,
    resolve_argv,
    seed_remote,
)


@dataclass
class _SideState:
    result: CliResult
    lines: list[str]
    remaining: list[str]
    tree: list[str] | None
    head: dict[str, Any] | None
    progress: bool | None


def _run_side(
    runner: Any,
    scenario: CpScenario,
    bucket: str,
    s3_client: Any,
    workdir: Path,
) -> _SideState:
    workdir.mkdir(parents=True, exist_ok=True)
    materialize_workdir(workdir, scenario)
    delete_under(s3_client, bucket, "")
    seed_remote(s3_client, bucket, scenario)
    result = runner(resolve_argv(scenario, bucket), cwd=str(workdir), stdin_payload=scenario.stdin)
    head = None
    if scenario.head_key is not None:
        head = head_object_fields(s3_client, bucket, scenario.head_key, scenario.head_fields)
    state = _SideState(
        result=result,
        lines=normalize_cp_stdout(result.stdout, bucket=bucket),
        remaining=remaining_keys(s3_client, bucket),
        tree=capture_local_tree(str(workdir / "dest")) if scenario.capture_tree else None,
        head=head,
        progress=had_progress_lines(result.stdout) if scenario.compare_progress else None,
    )
    if scenario.mtime_key is not None:
        key, rel_path = scenario.mtime_key
        expected = s3_client.head_object(Bucket=bucket, Key=key)["LastModified"]
        stamped = os.stat(workdir / rel_path).st_mtime
        assert abs(stamped - expected.timestamp()) < 2, (
            f"[{scenario.name}] synced-file mtime {stamped} != LastModified {expected}"
        )
    return state


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_sync_parity(scenario: CpScenario, bucket: str, s3_client: Any, tmp_path: Path) -> None:
    try:
        aws = _run_side(
            run_aws_subprocess_with_stdin, scenario, bucket, s3_client, tmp_path / "aws"
        )
        ours = _run_side(
            run_cli_subprocess_with_stdin, scenario, bucket, s3_client, tmp_path / "ours"
        )

        if not scenario.diff_only:
            if update_goldens_enabled():
                write_golden(
                    "sync",
                    Golden(
                        scenario=scenario.name,
                        argv=list(scenario.argv),
                        rc=aws.result.rc,
                        stdout_lines=aws.lines,
                        aws_version=detect_aws_version(),
                        remaining_keys=aws.remaining,
                        local_tree=aws.tree,
                        head_fields=aws.head,
                        progress=aws.progress,
                    ),
                )
            else:
                assert_matches_golden(
                    load_golden("sync", scenario.name),
                    rc=aws.result.rc,
                    stdout_lines=aws.lines,
                    side="aws",
                    compare_stdout=scenario.compare_stdout,
                    remaining_keys=aws.remaining,
                    local_tree=aws.tree,
                    head_fields=aws.head,
                    progress=aws.progress,
                )

        assert ours.result.rc == aws.result.rc, (
            f"[{scenario.name}] exit-code parity broken (charter, docs/overview.md section 3):\n"
            f"  ours rc={ours.result.rc} stderr={ours.result.stderr.strip()!r}\n"
            f"  aws  rc={aws.result.rc} stderr={aws.result.stderr.strip()!r}"
        )
        if scenario.compare_stdout and ours.lines != aws.lines:
            diff = "\n".join(
                difflib.unified_diff(
                    aws.lines, ours.lines, fromfile="aws", tofile="ours", lineterm=""
                )
            )
            pytest.fail(f"[{scenario.name}] stdout parity broken:\n{diff}")
        assert ours.remaining == aws.remaining, (
            f"[{scenario.name}] bucket end-state parity broken:\n"
            f"  ours: {ours.remaining!r}\n  aws:  {aws.remaining!r}"
        )
        assert ours.tree == aws.tree, (
            f"[{scenario.name}] local end-state parity broken:\n"
            f"  ours: {ours.tree!r}\n  aws:  {aws.tree!r}"
        )
        assert ours.head == aws.head, (
            f"[{scenario.name}] object-shape parity broken:\n"
            f"  ours: {ours.head!r}\n  aws:  {aws.head!r}"
        )
        assert_stderr_tokens(
            scenario.expected_stderr_tokens_ours,
            ours.result.stderr,
            side="ours",
            scenario=scenario.name,
            require_empty=scenario.stderr_exact_empty,
        )
        assert_stderr_tokens(
            scenario.expected_stderr_tokens_aws,
            aws.result.stderr,
            side="aws",
            scenario=scenario.name,
            require_empty=scenario.stderr_exact_empty,
        )
    finally:
        delete_under(s3_client, bucket, "")
