"""``boto3-s3 sync`` functional coverage: golden replay on moto + glacier extras.

The cp replay (see test_cp_golden.py) over the sync scenario table:
``local_mtimes`` keeps the time judgments deterministic against moto's
"seeded at ~now" objects exactly as it does against MinIO in the e2e lane.
``TestSyncGlacierOnMoto`` pins the glacier-gated shapes MinIO cannot host -
a gated download warns (rc 2) without touching the local side, and
``--force-glacier-transfer`` lets S3's own ``InvalidObjectState`` surface
(rc 1).
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
    head_object_fields,
    normalize_cp_stdout,
    remaining_keys,
    run_cli_in_process,
)
from tests.utils.sync_scenarios import (
    SCENARIOS,
    CpScenario,
    materialize_workdir,
    resolve_argv,
    seed_remote,
)

_REPLAYABLE = [scenario for scenario in SCENARIOS if not scenario.diff_only]


@pytest.mark.parametrize("scenario", _REPLAYABLE, ids=lambda s: s.name)
def test_sync_matches_golden(
    scenario: CpScenario, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialize_workdir(tmp_path, scenario)
    seed_remote(moto_s3, FUNCTIONAL_BUCKET, scenario)
    monkeypatch.chdir(tmp_path)
    result = run_cli_in_process(resolve_argv(scenario, FUNCTIONAL_BUCKET))
    lines = normalize_cp_stdout(result.stdout, bucket=FUNCTIONAL_BUCKET)
    head_fields = None
    if scenario.head_key is not None:
        head_fields = head_object_fields(
            moto_s3, FUNCTIONAL_BUCKET, scenario.head_key, scenario.head_fields
        )
    assert_matches_golden(
        load_golden("sync", scenario.name),
        rc=result.rc,
        stdout_lines=lines,
        side="ours",
        compare_stdout=scenario.compare_stdout,
        remaining_keys=remaining_keys(moto_s3, FUNCTIONAL_BUCKET),
        local_tree=capture_local_tree(str(tmp_path / "dest")) if scenario.capture_tree else None,
        head_fields=head_fields,
    )
    assert_stderr_tokens(
        scenario.expected_stderr_tokens_ours, result.stderr, side="ours", scenario=scenario.name
    )
    if scenario.mtime_key is not None:
        key, rel_path = scenario.mtime_key
        expected = moto_s3.head_object(Bucket=FUNCTIONAL_BUCKET, Key=key)["LastModified"]
        stamped = os.stat(tmp_path / rel_path).st_mtime
        assert abs(stamped - expected.timestamp()) < 2, (
            f"synced-file mtime {stamped} != object LastModified {expected}"
        )


class TestSyncGlacierOnMoto:
    """The glacier gate on a sync download: skip with a warning, never fail."""

    def _seed_glacier(self, moto_s3: Any) -> None:
        moto_s3.put_object(
            Bucket=FUNCTIONAL_BUCKET, Key="cold/x.bin", Body=b"frozen", StorageClass="GLACIER"
        )

    def test_download_warns_and_skips(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_glacier(moto_s3)
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(["sync", f"s3://{FUNCTIONAL_BUCKET}/cold", "out"])
        assert result.rc == 2
        assert "Object is of storage class GLACIER." in result.stderr
        # The wording stays route-shaped ("download"), not "sync".
        assert "Unable to perform download operations" in result.stderr
        assert not (tmp_path / "out" / "x.bin").exists()

    def test_force_glacier_transfer_surfaces_the_get_failure(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The gate lets it through and S3 refuses the GET (InvalidObjectState).
        self._seed_glacier(moto_s3)
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(
            ["sync", f"s3://{FUNCTIONAL_BUCKET}/cold", "out", "--force-glacier-transfer"]
        )
        assert result.rc == 1
        assert "download failed" in result.stderr
        assert "InvalidObjectState" in result.stderr

    def test_restored_object_still_skips(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sync judges from the listing, which carries no Restore status -
        # aws-cli-faithful: a restored object still warns there (only cp/mv's
        # single-object HeadObject path can see the restoration; use
        # --force-glacier-transfer to sync restored objects).
        self._seed_glacier(moto_s3)
        moto_s3.restore_object(
            Bucket=FUNCTIONAL_BUCKET, Key="cold/x.bin", RestoreRequest={"Days": 1}
        )
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(["sync", f"s3://{FUNCTIONAL_BUCKET}/cold", "out"])
        assert result.rc == 2
        assert "Object is of storage class GLACIER." in result.stderr
        assert not (tmp_path / "out" / "x.bin").exists()

    def test_ignore_glacier_warnings_skips_silently(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_glacier(moto_s3)
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(
            ["sync", f"s3://{FUNCTIONAL_BUCKET}/cold", "out", "--ignore-glacier-warnings"]
        )
        assert result.rc == 0
        assert (result.stdout, result.stderr) == ("", "")  # a silent skip is silent on both
        assert not (tmp_path / "out" / "x.bin").exists()
