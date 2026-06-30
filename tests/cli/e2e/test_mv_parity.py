"""Differential parity: ``boto3-s3 mv`` vs the real ``aws s3 mv``.

The cp parity harness (see test_cp_parity.py) with mv's two twists: the
local **source** tree is captured after every run (what the move deleted -
or kept on dryrun/filter/no-overwrite/failure - is mv's defining end
state), and the download-mtime expectation is read from the object *before*
the run, because a successful move deletes the source key the cp harness
would have re-read afterwards. Moves are doubly destructive, but both
sides start from scratch anyway: ``materialize_workdir`` rebuilds ``src/``
per side and the bucket is wiped and reseeded between the sides.
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
    head_object_fields,
    normalize_cp_stdout,
    remaining_keys,
    run_aws_subprocess_with_stdin,
    run_cli_subprocess_with_stdin,
)
from tests.utils.mv_scenarios import (
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
    src_tree: list[str]
    head: dict[str, Any] | None


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
    expected_mtime = None
    if scenario.mtime_key is not None:
        # Read the expectation up front: a successful move deletes the key.
        key, _rel_path = scenario.mtime_key
        expected_mtime = s3_client.head_object(Bucket=bucket, Key=key)["LastModified"]
    result = runner(resolve_argv(scenario, bucket), cwd=str(workdir), stdin_payload=scenario.stdin)
    head = None
    if scenario.head_key is not None:
        head = head_object_fields(s3_client, bucket, scenario.head_key, scenario.head_fields)
    state = _SideState(
        result=result,
        lines=normalize_cp_stdout(result.stdout, bucket=bucket),
        remaining=remaining_keys(s3_client, bucket),
        tree=capture_local_tree(str(workdir / "dest")) if scenario.capture_tree else None,
        src_tree=capture_local_tree(str(workdir / "src")),
        head=head,
    )
    if expected_mtime is not None and scenario.mtime_key is not None:
        stamped = os.stat(workdir / scenario.mtime_key[1]).st_mtime
        assert abs(stamped - expected_mtime.timestamp()) < 2, (
            f"[{scenario.name}] moved-file mtime {stamped} != LastModified {expected_mtime}"
        )
    return state


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_mv_parity(scenario: CpScenario, bucket: str, s3_client: Any, tmp_path: Path) -> None:
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
                    "mv",
                    Golden(
                        scenario=scenario.name,
                        argv=list(scenario.argv),
                        rc=aws.result.rc,
                        stdout_lines=aws.lines,
                        aws_version=detect_aws_version(),
                        remaining_keys=aws.remaining,
                        local_tree=aws.tree,
                        head_fields=aws.head,
                        src_tree=aws.src_tree,
                    ),
                )
            else:
                assert_matches_golden(
                    load_golden("mv", scenario.name),
                    rc=aws.result.rc,
                    stdout_lines=aws.lines,
                    side="aws",
                    compare_stdout=scenario.compare_stdout,
                    remaining_keys=aws.remaining,
                    local_tree=aws.tree,
                    head_fields=aws.head,
                    src_tree=aws.src_tree,
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
        assert ours.src_tree == aws.src_tree, (
            f"[{scenario.name}] source end-state parity broken:\n"
            f"  ours: {ours.src_tree!r}\n  aws:  {aws.src_tree!r}"
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
        )
        assert_stderr_tokens(
            scenario.expected_stderr_tokens_aws,
            aws.result.stderr,
            side="aws",
            scenario=scenario.name,
        )
    finally:
        delete_under(s3_client, bucket, "")
