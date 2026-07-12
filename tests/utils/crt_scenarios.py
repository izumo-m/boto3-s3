"""Scenarios for the CRT-engine parity lane (``tests/cli/e2e/test_crt_parity.py``).

A focused subset of the CRT-configured transfer surface - upload / download /
mv / sync / rm, including multipart and delete-bearing shapes - reusing the
``CpScenario`` shape and helpers. The lane runs
both CLIs with ``preferred_transfer_client = crt`` and asserts they agree, so
these scenarios carry no goldens (the CRT manager's stdout is identical to the
classic engine's - aws-cli ``s3handler`` has no CRT branch - and the value is
the live ours-vs-aws comparison under CRT mode).

The delete cases port the missing intent of aws-cli's CRT rm/sync tests at the
CLI-observable boundary. For S3 deletion, boto3-s3 deliberately keeps its
established ``DeleteObject`` / batched ``S3Deleter`` routes rather than
aws-cli's per-key CRT DELETE requests; download sync-delete removes local files
on both sides (docs/deleter.md). The contract here is rc, output, and end-state
parity rather than S3 transport identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tests.utils.cp_scenarios import (
    CpScenario,
    materialize_workdir,
    resolve_argv,
    seed_remote,
)
from tests.utils.harness import BUCKET_TOKEN

__all__ = [
    "SCENARIOS",
    "CpScenario",
    "materialize_workdir",
    "resolve_argv",
    "seed_remote",
]

_MB = 1024 * 1024

_SMALL_SRC: Mapping[str, bytes] = {"src/a.txt": b"crt single-part body\n"}
_BIG_SRC: Mapping[str, bytes] = {"src/big.bin": b"u" * (9 * _MB)}
_TREE_SRC: Mapping[str, bytes] = {
    "src/a.txt": b"alpha\n",
    "src/nested/b.txt": b"beta\n",
}

# Remote seeds for the download / mv-download scenarios (9 MiB exercises the
# CRT multipart download path).
_SMALL_SEED: Mapping[str, bytes] = {"down/a.txt": b"crt download body\n"}
_BIG_SEED_KWARGS: Mapping[str, Mapping[str, Any]] = {
    "down/big.bin": {"Body": b"d" * (9 * _MB)},
}
_TREE_SEED: Mapping[str, bytes] = {
    "down/a.txt": b"alpha\n",
    "down/nested/b.txt": b"beta\n",
}
_RM_SEED: Mapping[str, bytes] = {
    "remove/a.txt": b"delete alpha\n",
    "remove/nested/b.txt": b"delete beta\n",
    "keep.txt": b"keep\n",
}


SCENARIOS: tuple[CpScenario, ...] = (
    # -- uploads ------------------------------------------------------------
    CpScenario(
        name="crt_upload_single",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/a.txt"),
        local_src=_SMALL_SRC,
    ),
    CpScenario(
        name="crt_upload_multipart",
        argv=("cp", "src/big.bin", f"s3://{BUCKET_TOKEN}/up/big.bin"),
        local_src=_BIG_SRC,
    ),
    CpScenario(
        name="crt_upload_checksum",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/a.txt",
            "--checksum-algorithm",
            "SHA256",
        ),
        local_src=_SMALL_SRC,
    ),
    CpScenario(
        name="crt_upload_dryrun",
        argv=("cp", "src/big.bin", f"s3://{BUCKET_TOKEN}/up/big.bin", "--dryrun"),
        local_src=_BIG_SRC,
    ),
    CpScenario(
        name="crt_no_overwrite_skip",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}/exists.txt", "--no-overwrite"),
        local_src=_SMALL_SRC,
        seed={"exists.txt": b"already here\n"},
    ),
    # -- downloads ----------------------------------------------------------
    CpScenario(
        name="crt_download_single",
        argv=("cp", f"s3://{BUCKET_TOKEN}/down/a.txt", "dest/a.txt"),
        seed=_SMALL_SEED,
        capture_tree=True,
        mtime_key=("down/a.txt", "dest/a.txt"),
    ),
    CpScenario(
        name="crt_download_multipart",
        argv=("cp", f"s3://{BUCKET_TOKEN}/down/big.bin", "dest/big.bin"),
        seed_kwargs=_BIG_SEED_KWARGS,
        capture_tree=True,
        mtime_key=("down/big.bin", "dest/big.bin"),
    ),
    # -- mv -----------------------------------------------------------------
    CpScenario(
        name="crt_mv_upload",
        argv=("mv", "src/a.txt", f"s3://{BUCKET_TOKEN}/moved/a.txt"),
        local_src=_SMALL_SRC,
        capture_tree=True,  # the source side is emptied; dest/ stays empty
    ),
    CpScenario(
        name="crt_mv_download",
        argv=("mv", f"s3://{BUCKET_TOKEN}/down/a.txt", "dest/a.txt"),
        seed=_SMALL_SEED,
        capture_tree=True,
    ),
    # -- sync ---------------------------------------------------------------
    CpScenario(
        name="crt_sync_upload",
        argv=("sync", "src", f"s3://{BUCKET_TOKEN}/synced"),
        local_src=_TREE_SRC,
    ),
    CpScenario(
        name="crt_sync_download",
        argv=("sync", f"s3://{BUCKET_TOKEN}/down", "dest"),
        seed=_TREE_SEED,
        capture_tree=True,
    ),
    CpScenario(
        name="crt_sync_upload_with_delete",
        argv=("sync", "src", f"s3://{BUCKET_TOKEN}/synced", "--delete"),
        local_src=_TREE_SRC,
        seed={"synced/orphan.txt": b"delete remote orphan\n"},
    ),
    CpScenario(
        name="crt_sync_download_with_delete",
        argv=("sync", f"s3://{BUCKET_TOKEN}/down", "dest", "--delete"),
        local_src={"dest/orphan.txt": b"delete local orphan\n"},
        seed=_TREE_SEED,
        capture_tree=True,
    ),
    # -- rm -----------------------------------------------------------------
    CpScenario(
        name="crt_rm_single",
        argv=("rm", f"s3://{BUCKET_TOKEN}/remove/a.txt"),
        seed=_RM_SEED,
    ),
    CpScenario(
        name="crt_rm_recursive",
        argv=("rm", f"s3://{BUCKET_TOKEN}/remove", "--recursive"),
        seed=_RM_SEED,
    ),
)
