"""``boto3-s3 cp`` functional coverage: golden replay on moto + glacier extras.

The replay materializes each scenario's local tree under a per-test workdir,
chdirs into it (argv and result lines carry workdir-relative paths - the
goldens are cwd-stable by construction), seeds moto, runs the CLI
in-process, and compares everything the golden recorded: rc, masked/sorted
stdout, the bucket end state, the local destination tree, and the probe
object's HeadObject fields. The download mtime stamp is asserted live.
Streaming scenarios (``-``) run through the binary-capable in-process
runner, which feeds the scenario's stdin payload and captures raw stdout
bytes.

``TestCpGlacierOnMoto`` covers the glacier gate's success-and-skip shapes
that MinIO cannot host (no GLACIER storage-class semantics there - the
e2e tier carries only the diff-only storage-class scenario).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from tests.cli.functional.conftest import FUNCTIONAL_BUCKET
from tests.utils.cp_scenarios import (
    SCENARIOS,
    CpScenario,
    materialize_workdir,
    resolve_argv,
    seed_remote,
)
from tests.utils.golden import assert_matches_golden, load_golden
from tests.utils.harness import (
    assert_stderr_tokens,
    capture_local_tree,
    head_object_fields,
    normalize_cp_stdout,
    remaining_keys,
    run_cli_in_process,
    run_cli_in_process_streaming,
)

_REPLAYABLE = [scenario for scenario in SCENARIOS if not scenario.diff_only]


def _assert_mtime_stamped(client: Any, bucket: str, scenario: CpScenario, workdir: Path) -> None:
    if scenario.mtime_key is None:
        return
    key, rel_path = scenario.mtime_key
    last_modified = client.head_object(Bucket=bucket, Key=key)["LastModified"]
    stamped = os.stat(workdir / rel_path).st_mtime
    assert abs(stamped - last_modified.timestamp()) < 2, (
        f"downloaded file mtime {stamped} != object LastModified {last_modified}"
    )


@pytest.mark.parametrize("scenario", _REPLAYABLE, ids=lambda s: s.name)
def test_cp_matches_golden(
    scenario: CpScenario, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialize_workdir(tmp_path, scenario)
    seed_remote(moto_s3, FUNCTIONAL_BUCKET, scenario)
    monkeypatch.chdir(tmp_path)
    argv = resolve_argv(scenario, FUNCTIONAL_BUCKET)
    if scenario.stdin is not None or "-" in scenario.argv:
        result = run_cli_in_process_streaming(argv, stdin_payload=scenario.stdin)
    else:
        result = run_cli_in_process(argv)
    lines = normalize_cp_stdout(result.stdout, bucket=FUNCTIONAL_BUCKET)
    head_fields = None
    if scenario.head_key is not None:
        head_fields = head_object_fields(
            moto_s3, FUNCTIONAL_BUCKET, scenario.head_key, scenario.head_fields
        )
    assert_matches_golden(
        load_golden("cp", scenario.name),
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
    _assert_mtime_stamped(moto_s3, FUNCTIONAL_BUCKET, scenario, tmp_path)


class TestCpGlacierOnMoto:
    """The glacier gate's live shapes (aws-cli s3handler._warn_glacier)."""

    def _seed_glacier(self, moto_s3: Any) -> None:
        moto_s3.put_object(
            Bucket=FUNCTIONAL_BUCKET, Key="cold/x.bin", Body=b"frozen", StorageClass="GLACIER"
        )

    def test_download_warns_and_skips_rc_2(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_glacier(moto_s3)
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(["cp", f"s3://{FUNCTIONAL_BUCKET}/cold/x.bin", "out.bin"])
        assert result.rc == 2
        assert "warning:" in result.stderr
        assert "Object is of storage class GLACIER." in result.stderr
        assert not (tmp_path / "out.bin").exists()

    def test_force_glacier_transfer_attempts_the_get(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --force-glacier-transfer only bypasses the warning gate; S3 (and
        # moto, faithfully) still refuses the GET on an unrestored object -
        # the InvalidObjectState failure is the proof the gate let it through.
        self._seed_glacier(moto_s3)
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(
            ["cp", f"s3://{FUNCTIONAL_BUCKET}/cold/x.bin", "out.bin", "--force-glacier-transfer"]
        )
        assert result.rc == 1
        assert "download failed" in result.stderr
        assert "InvalidObjectState" in result.stderr

    def test_restored_object_downloads_without_force(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A restored object carries Restore: ongoing-request="false" on HEAD,
        # which the single-object gate reads (aws-cli _is_restored).
        self._seed_glacier(moto_s3)
        moto_s3.restore_object(
            Bucket=FUNCTIONAL_BUCKET, Key="cold/x.bin", RestoreRequest={"Days": 1}
        )
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(["cp", f"s3://{FUNCTIONAL_BUCKET}/cold/x.bin", "out.bin"])
        assert result.rc == 0, result.stderr
        assert (tmp_path / "out.bin").read_bytes() == b"frozen"

    def test_ignore_glacier_warnings_skips_silently(
        self, moto_s3: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_glacier(moto_s3)
        monkeypatch.chdir(tmp_path)
        result = run_cli_in_process(
            ["cp", f"s3://{FUNCTIONAL_BUCKET}/cold/x.bin", "out.bin", "--ignore-glacier-warnings"]
        )
        assert result.rc == 0
        assert result.stderr == ""
        assert not (tmp_path / "out.bin").exists()
