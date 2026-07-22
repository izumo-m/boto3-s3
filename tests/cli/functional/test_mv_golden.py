"""``boto3-s3 mv`` functional coverage: golden replay on moto + glacier extras.

The cp replay (see test_cp_golden.py) plus mv's source-side end state: the
``src/`` tree is captured after every run and compared against the golden's
``src_tree``, and the download-mtime expectation is read before the run
(the move deletes the source key). ``TestMvGlacierOnMoto`` pins the
glacier-gated shapes MinIO cannot host - crucially that a gated or failed
move never deletes the source object.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.cli.functional.conftest import FUNCTIONAL_BUCKET
from tests.utils.golden import assert_matches_golden, load_golden
from tests.utils.harness import (
    assert_stderr_tokens,
    capture_local_tree,
    had_progress_lines,
    head_object_fields,
    normalize_cp_stdout,
    remaining_keys,
    run_cli_in_process,
)
from tests.utils.mv_scenarios import (
    SCENARIOS,
    CpScenario,
    materialize_workdir,
    resolve_argv,
    seed_remote,
)

_REPLAYABLE = [scenario for scenario in SCENARIOS if not scenario.diff_only]


@pytest.mark.parametrize("scenario", _REPLAYABLE, ids=lambda s: s.name)
def test_mv_matches_golden(
    scenario: CpScenario, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialize_workdir(tmp_path, scenario)
    seed_remote(moto_s3, FUNCTIONAL_BUCKET, scenario)
    expected_mtime = None
    if scenario.mtime_key is not None:
        key, _rel_path = scenario.mtime_key
        expected_mtime = moto_s3.head_object(Bucket=FUNCTIONAL_BUCKET, Key=key)["LastModified"]
    monkeypatch.chdir(tmp_path)
    result = run_cli_in_process(resolve_argv(scenario, FUNCTIONAL_BUCKET))
    lines = normalize_cp_stdout(result.stdout, bucket=FUNCTIONAL_BUCKET)
    head_fields = None
    if scenario.head_key is not None:
        head_fields = head_object_fields(
            moto_s3, FUNCTIONAL_BUCKET, scenario.head_key, scenario.head_fields
        )
    assert_matches_golden(
        load_golden("mv", scenario.name),
        rc=result.rc,
        stdout_lines=lines,
        side="ours",
        compare_stdout=scenario.compare_stdout,
        remaining_keys=remaining_keys(moto_s3, FUNCTIONAL_BUCKET),
        local_tree=capture_local_tree(str(tmp_path / "dest")) if scenario.capture_tree else None,
        head_fields=head_fields,
        progress=(had_progress_lines(result.stdout) if scenario.compare_progress else None),
        src_tree=capture_local_tree(str(tmp_path / "src")),
    )
    assert_stderr_tokens(
        scenario.expected_stderr_tokens_ours,
        result.stderr,
        side="ours",
        scenario=scenario.name,
        require_empty=scenario.stderr_exact_empty,
    )
    if expected_mtime is not None and scenario.mtime_key is not None:
        stamped = os.stat(tmp_path / scenario.mtime_key[1]).st_mtime
        assert abs(stamped - expected_mtime.timestamp()) < 2, (
            f"moved-file mtime {stamped} != object LastModified {expected_mtime}"
        )


class TestMvGlacierOnMoto:
    """The glacier gate on a move: the source must survive every shape."""

    def _seed_glacier(self, moto_s3: Any) -> None:
        moto_s3.put_object(
            Bucket=FUNCTIONAL_BUCKET, Key="cold/x.bin", Body=b"frozen", StorageClass="GLACIER"
        )

    def _remaining(self, moto_s3: Any) -> list[str]:
        return remaining_keys(moto_s3, FUNCTIONAL_BUCKET)

    def test_download_warns_skips_and_keeps_the_object(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_glacier(moto_s3)
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(["mv", f"s3://{FUNCTIONAL_BUCKET}/cold/x.bin", "out.bin"])
        assert result.rc == 2
        assert "Object is of storage class GLACIER." in result.stderr
        # The wording stays route-shaped ("download"), not "move".
        assert "Unable to perform download operations" in result.stderr
        assert not (tmp_path / "out.bin").exists()
        assert self._remaining(moto_s3) == ["cold/x.bin:6:?"]  # GLACIER body unreadable

    def test_force_glacier_transfer_fails_without_deleting(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gate lets it through, S3 refuses the GET (InvalidObjectState),
        # and the failed move leaves the source object in place.
        self._seed_glacier(moto_s3)
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(
            ["mv", f"s3://{FUNCTIONAL_BUCKET}/cold/x.bin", "out.bin", "--force-glacier-transfer"]
        )
        assert result.rc == 1
        assert "move failed" in result.stderr
        assert "InvalidObjectState" in result.stderr
        assert self._remaining(moto_s3) == ["cold/x.bin:6:?"]  # GLACIER body unreadable

    def test_restored_object_moves_without_force(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_glacier(moto_s3)
        moto_s3.restore_object(
            Bucket=FUNCTIONAL_BUCKET, Key="cold/x.bin", RestoreRequest={"Days": 1}
        )
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(["mv", f"s3://{FUNCTIONAL_BUCKET}/cold/x.bin", "out.bin"])
        assert result.rc == 0, result.stderr
        assert (tmp_path / "out.bin").read_bytes() == b"frozen"
        assert self._remaining(moto_s3) == []

    def test_ignore_glacier_warnings_skips_silently_and_keeps_the_object(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_glacier(moto_s3)
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(
            ["mv", f"s3://{FUNCTIONAL_BUCKET}/cold/x.bin", "out.bin", "--ignore-glacier-warnings"]
        )
        assert result.rc == 0
        assert (result.stdout, result.stderr) == ("", "")  # a silent skip is silent on both
        assert not (tmp_path / "out.bin").exists()
        assert self._remaining(moto_s3) == ["cold/x.bin:6:?"]  # GLACIER body unreadable
