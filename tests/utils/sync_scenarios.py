"""Shared ``sync`` scenarios: golden replay and e2e parity.

sync reuses ``tests.utils.cp_scenarios.CpScenario`` and its helpers -
same workdir materialization, same remote seeding, same stdout
normalization - plus the ``local_mtimes`` field: sync's whole point is the
size+time judgment, so the at-both scenarios pin each side of the aws-cli
rules by stamping local files a day older or newer than the seeded objects:

- upload/copy: skip when the destination is at least as new;
- download: skip unless the LOCAL side is newer (the aws-cli asymmetry -
  ``--exact-timestamps`` tightens it to exact equality, and beats
  ``--size-only`` when both are given);
- ``--delete`` removes destination-only entries, except folder markers and
  anything the filters exclude (the destination-side pattern root).

The sync exit-code shape: usage errors (local-local, any ``-`` path) are
252, the missing local source is 255, a source *file* degrades to the walk
warning (rc 2), and a destination that exists as a file fails per item
(``[Errno 20]``, rc 1).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from tests.utils.cp_scenarios import (
    CpScenario,
    materialize_workdir,
    resolve_argv,
    seed_remote,
)
from tests.utils.harness import BUCKET_TOKEN

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["SCENARIOS", "CpScenario", "materialize_workdir", "resolve_argv", "seed_remote"]

_DAY = 86400

_SRC_TREE: Mapping[str, bytes] = {
    "src/a.txt": b"alpha\n",
    "src/b.bin": b"\x00\x01\x02",
    "src/sub/c.txt": b"gamma\n",
    "src/z.txt": b"zeta\n",
}

# The at-both upload matrix: same sizes where only time should decide.
_MIX_SRC: Mapping[str, bytes] = {
    "src/new.txt": b"brand new\n",
    "src/same.txt": b"AA\n",
    "src/stale.txt": b"BB\n",
    "src/bigger.txt": b"locally bigger\n",
}
_MIX_MTIMES: Mapping[str, int] = {
    "src/new.txt": -_DAY,
    "src/same.txt": -_DAY,  # dest newer -> skip
    "src/stale.txt": _DAY,  # dest older -> upload
    "src/bigger.txt": -_DAY,  # size differs -> upload regardless
}
_MIX_SEED: Mapping[str, bytes] = {
    "up/same.txt": b"xx\n",
    "up/stale.txt": b"yy\n",
    "up/bigger.txt": b"s\n",
}

# Destination-only entries for the --delete family. The folder marker and
# the prefix sibling pin the rules: markers are invisible to sync (never
# deleted), and "del-sibling" must not match "del/".
_DEL_SRC: Mapping[str, bytes] = {"src/keep.txt": b"K\n"}
_DEL_MTIMES: Mapping[str, int] = {"src/keep.txt": -_DAY}
_DEL_SEED: Mapping[str, bytes] = {
    "del/keep.txt": b"K\n",
    "del/extra.log": b"log\n",
    "del/extra.txt": b"extra\n",
    "del/marker/": b"",
    "del-sibling.txt": b"sibling\n",
}

_DL_TREE_SEED: Mapping[str, bytes] = {
    "d/a.txt": b"remote alpha\n",
    "d/b.bin": b"\x07\x08",
    "d/sub/c.txt": b"remote gamma\n",
    "d/marker/": b"",
    "d-sibling.txt": b"sibling\n",
}

# The at-both download matrix (note the inverted rule: local-older skips).
_DL_MIX_SRC: Mapping[str, bytes] = {
    "dest/same.txt": b"zz\n",
    "dest/touch.txt": b"qq\n",
    "dest/short.txt": b"s\n",
}
_DL_MIX_MTIMES: Mapping[str, int] = {
    "dest/same.txt": -_DAY,  # local older -> skip (aws-cli asymmetry)
    "dest/touch.txt": _DAY,  # local newer -> download
    "dest/short.txt": -_DAY,  # size differs -> download regardless
}
_DL_MIX_SEED: Mapping[str, bytes] = {
    "d/new.txt": b"fresh\n",
    "d/same.txt": b"xx\n",
    "d/short.txt": b"remote longer\n",
    "d/touch.txt": b"yy\n",
}

_CP_FRESH_SEED: Mapping[str, bytes] = {
    "cs/a.txt": b"copy alpha\n",
    "cs/marker/": b"",
    "cs/sub/b.txt": b"copy beta\n",
}
# Source prefix seeded before the destination prefix (dict order), so the
# identical pair's destination is at least as new -> skip.
_CP_SAME_SEED: Mapping[str, bytes] = {
    "cs/a.txt": b"same body\n",
    "cd/a.txt": b"same body\n",
}
_CP_DEL_SEED: Mapping[str, bytes] = {
    "cs/keep.txt": b"K\n",
    "cd/keep.txt": b"K\n",
    "cd/extra.txt": b"extra\n",
    "cd/marker/": b"",
}

SCENARIOS: tuple[CpScenario, ...] = (
    # -- uploads --------------------------------------------------------------
    CpScenario(
        name="sync_upload_fresh",
        argv=("sync", "src", f"s3://{BUCKET_TOKEN}/tree/"),
        local_src=_SRC_TREE,
    ),
    CpScenario(
        name="sync_upload_mixed_times",
        argv=("sync", "src", f"s3://{BUCKET_TOKEN}/up"),
        local_src=_MIX_SRC,
        local_mtimes=_MIX_MTIMES,
        seed=_MIX_SEED,
    ),
    CpScenario(
        name="sync_upload_size_only",
        argv=("sync", "src", f"s3://{BUCKET_TOKEN}/up", "--size-only"),
        # Same size, locally newer: the default would upload; --size-only
        # makes the run silent.
        local_src={"src/same.txt": b"AA\n"},
        local_mtimes={"src/same.txt": _DAY},
        seed={"up/same.txt": b"xx\n"},
    ),
    CpScenario(
        name="sync_upload_delete",
        argv=("sync", "src", f"s3://{BUCKET_TOKEN}/del", "--delete"),
        local_src=_DEL_SRC,
        local_mtimes=_DEL_MTIMES,
        seed=_DEL_SEED,
    ),
    CpScenario(
        name="sync_upload_delete_exclude",
        argv=("sync", "src", f"s3://{BUCKET_TOKEN}/del", "--delete", "--exclude", "*.log"),
        local_src=_DEL_SRC,
        local_mtimes=_DEL_MTIMES,
        seed=_DEL_SEED,
    ),
    CpScenario(
        name="sync_upload_delete_exclude_all",
        argv=("sync", "src", f"s3://{BUCKET_TOKEN}/del", "--delete", "--exclude", "*"),
        local_src=_DEL_SRC,
        local_mtimes=_DEL_MTIMES,
        seed=_DEL_SEED,
    ),
    CpScenario(
        name="sync_upload_delete_dryrun",
        argv=("sync", "src", f"s3://{BUCKET_TOKEN}/del", "--delete", "--dryrun"),
        local_src=_DEL_SRC,
        local_mtimes=_DEL_MTIMES,
        seed=_DEL_SEED,
    ),
    CpScenario(
        name="sync_upload_invalid_grants_dryrun",
        argv=(
            "sync",
            "src",
            f"s3://{BUCKET_TOKEN}/up",
            "--grants",
            "invalid",
            "--dryrun",
        ),
        local_src={"src/a.txt": b"alpha\n"},
        expected_stderr_tokens_ours=("grants should be of the form permission=principal",),
        expected_stderr_tokens_aws=("grants should be of the form permission=principal",),
        diff_only=True,
    ),
    CpScenario(
        name="sync_upload_no_overwrite",
        argv=("sync", "src", f"s3://{BUCKET_TOKEN}/no", "--no-overwrite"),
        # exists.txt differs in size and is locally newer - everything says
        # update - but --no-overwrite never touches an existing key.
        local_src={"src/exists.txt": b"locally much longer\n", "src/new.txt": b"n\n"},
        local_mtimes={"src/exists.txt": _DAY, "src/new.txt": -_DAY},
        seed={"no/exists.txt": b"s\n"},
        head_key="no/exists.txt",
        head_fields=("ContentLength",),
    ),
    CpScenario(
        name="sync_upload_quiet_delete",
        argv=("sync", "src", f"s3://{BUCKET_TOKEN}/del", "--delete", "--quiet"),
        local_src=_DEL_SRC,
        local_mtimes=_DEL_MTIMES,
        seed=_DEL_SEED,
    ),
    CpScenario(
        name="sync_upload_empty_dir",
        # dest/ is the harness's standing empty directory; an empty sync is a
        # silent rc-0 no-op.
        argv=("sync", "dest", f"s3://{BUCKET_TOKEN}/empty"),
    ),
    CpScenario(
        name="sync_upload_missing_source",
        argv=("sync", "src/none", f"s3://{BUCKET_TOKEN}/up"),
        expected_stderr_tokens_ours=("does not exist",),
        expected_stderr_tokens_aws=("does not exist",),
    ),
    CpScenario(
        name="sync_upload_source_is_a_file",
        argv=("sync", "src/a.txt", f"s3://{BUCKET_TOKEN}/up"),
        local_src={"src/a.txt": b"alpha\n"},
        expected_stderr_tokens_ours=("File does not exist.",),
        expected_stderr_tokens_aws=("File does not exist.",),
    ),
    # -- downloads ------------------------------------------------------------
    CpScenario(
        name="sync_download_fresh",
        argv=("sync", f"s3://{BUCKET_TOKEN}/d", "dest"),
        seed=_DL_TREE_SEED,
        capture_tree=True,
        mtime_key=("d/a.txt", "dest/a.txt"),
    ),
    CpScenario(
        name="sync_download_mixed_times",
        argv=("sync", f"s3://{BUCKET_TOKEN}/d", "dest"),
        local_src=_DL_MIX_SRC,
        local_mtimes=_DL_MIX_MTIMES,
        seed=_DL_MIX_SEED,
        capture_tree=True,
    ),
    CpScenario(
        name="sync_download_exact_timestamps",
        argv=("sync", f"s3://{BUCKET_TOKEN}/d", "dest", "--exact-timestamps"),
        # Same size, local older: the default skips; --exact-timestamps
        # downloads on any skew.
        local_src={"dest/same.txt": b"zz\n"},
        local_mtimes={"dest/same.txt": -_DAY},
        seed={"d/same.txt": b"xx\n"},
        capture_tree=True,
    ),
    CpScenario(
        name="sync_download_size_only",
        argv=("sync", f"s3://{BUCKET_TOKEN}/d", "dest", "--size-only"),
        # Same size, local newer: the default downloads; --size-only skips.
        local_src={"dest/touch.txt": b"qq\n"},
        local_mtimes={"dest/touch.txt": _DAY},
        seed={"d/touch.txt": b"yy\n"},
        capture_tree=True,
    ),
    CpScenario(
        name="sync_download_exact_timestamps_size_only",
        argv=(
            "sync",
            f"s3://{BUCKET_TOKEN}/d",
            "dest",
            "--exact-timestamps",
            "--size-only",
        ),
        # Both flags given, same size, local older: --size-only alone would skip
        # (equal size), but --exact-timestamps wins over --size-only, so the skew
        # triggers a download. The tree shows the remote body, distinguishing the
        # two strategies' order (aws registers SizeOnly before ExactTimestamps, so
        # the latter is applied last and wins).
        local_src={"dest/same.txt": b"zz\n"},
        local_mtimes={"dest/same.txt": -_DAY},
        seed={"d/same.txt": b"xx\n"},
        capture_tree=True,
    ),
    CpScenario(
        name="sync_download_delete",
        argv=("sync", f"s3://{BUCKET_TOKEN}/d", "dest", "--delete"),
        local_src={"dest/stale.txt": b"old\n", "dest/sub/stale2.txt": b"old2\n"},
        seed={"d/a.txt": b"remote alpha\n"},
        capture_tree=True,
    ),
    CpScenario(
        name="sync_download_delete_dryrun",
        argv=("sync", f"s3://{BUCKET_TOKEN}/d", "dest", "--delete", "--dryrun"),
        local_src={"dest/stale.txt": b"old\n"},
        seed={"d/a.txt": b"remote alpha\n"},
        capture_tree=True,
    ),
    CpScenario(
        name="sync_download_delete_exclude",
        argv=("sync", f"s3://{BUCKET_TOKEN}/d", "dest", "--delete", "--exclude", "*.log"),
        local_src={"dest/stale.txt": b"old\n", "dest/keep.log": b"log\n"},
        seed={"d/a.txt": b"remote alpha\n"},
        capture_tree=True,
    ),
    CpScenario(
        name="sync_download_empty_prefix_creates_dir",
        argv=("sync", f"s3://{BUCKET_TOKEN}/no-such-prefix", "dest/newdir"),
        capture_tree=True,
    ),
    CpScenario(
        name="sync_download_dest_is_a_file",
        argv=("sync", f"s3://{BUCKET_TOKEN}/d", "dest/afile.txt"),
        local_src={"dest/afile.txt": b"zz"},
        seed={"d/a.txt": b"remote alpha\n"},
        capture_tree=True,
        # The dest walk warns (file-as-directory), then each download fails
        # with ENOTDIR; failures win the rc (1). On Windows the failure is
        # ENOENT instead (file-as-directory opens answer [Errno 2] there),
        # and aws's warning line is not pinned - only its failure is.
        expected_stderr_tokens_ours=("File does not exist.", "Not a directory")
        if os.name != "nt"
        else ("File does not exist.", "[Errno 2]"),
        expected_stderr_tokens_aws=("File does not exist.", "Not a directory")
        if os.name != "nt"
        else ("[Errno 2]",),
    ),
    # -- copies ---------------------------------------------------------------
    CpScenario(
        name="sync_copy_fresh",
        argv=("sync", f"s3://{BUCKET_TOKEN}/cs", f"s3://{BUCKET_TOKEN}/cd"),
        seed=_CP_FRESH_SEED,
    ),
    CpScenario(
        name="sync_copy_identical",
        argv=("sync", f"s3://{BUCKET_TOKEN}/cs", f"s3://{BUCKET_TOKEN}/cd"),
        seed=_CP_SAME_SEED,
    ),
    CpScenario(
        name="sync_copy_delete",
        argv=("sync", f"s3://{BUCKET_TOKEN}/cs", f"s3://{BUCKET_TOKEN}/cd", "--delete"),
        seed=_CP_DEL_SEED,
    ),
    CpScenario(
        name="sync_copy_filters",
        argv=(
            "sync",
            f"s3://{BUCKET_TOKEN}/cs",
            f"s3://{BUCKET_TOKEN}/cd",
            "--exclude",
            "*",
            "--include",
            "*.txt",
        ),
        seed={"cs/a.txt": b"alpha\n", "cs/b.bin": b"\x00\x01"},
    ),
    CpScenario(
        name="sync_copy_invalid_grants_dryrun",
        argv=(
            "sync",
            f"s3://{BUCKET_TOKEN}/cs",
            f"s3://{BUCKET_TOKEN}/cd",
            "--grants",
            "invalid",
            "--dryrun",
        ),
        seed={"cs/a.txt": b"alpha\n"},
        expected_stderr_tokens_ours=("grants should be of the form permission=principal",),
        expected_stderr_tokens_aws=("grants should be of the form permission=principal",),
        diff_only=True,
    ),
    CpScenario(
        name="sync_copy_content_type",
        argv=(
            "sync",
            f"s3://{BUCKET_TOKEN}/cs",
            f"s3://{BUCKET_TOKEN}/cd",
            "--content-type",
            "text/css",
            "--metadata-directive",
            "REPLACE",
        ),
        seed={"cs/styled.txt": b"styled body\n"},
        head_key="cd/styled.txt",
        head_fields=("ContentType",),
    ),
    # -- usage errors ----------------------------------------------------------
    CpScenario(
        name="sync_local_to_local",
        argv=("sync", "src", "dest"),
        local_src={"src/a.txt": b"alpha\n"},
        expected_stderr_tokens_ours=("Error: Invalid argument type",),
        expected_stderr_tokens_aws=("Error: Invalid argument type",),
    ),
    CpScenario(
        name="sync_stream_source",
        argv=("sync", "-", f"s3://{BUCKET_TOKEN}/p"),
        expected_stderr_tokens_ours=(
            "Streaming currently is only compatible with non-recursive cp commands",
        ),
        expected_stderr_tokens_aws=(
            "Streaming currently is only compatible with non-recursive cp commands",
        ),
    ),
)
