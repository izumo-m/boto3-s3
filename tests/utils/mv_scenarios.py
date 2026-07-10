"""Shared ``mv`` scenarios: golden replay and e2e parity (cp's contract + 1).

mv reuses ``tests.utils.cp_scenarios.CpScenario`` and its helpers
verbatim - same workdir materialization, same remote seeding, same stdout
normalization. The one addition is runner-side, not scenario-side: the
local **source** tree (``src/``) is captured after every run and recorded
as the golden's ``src_tree``, because what a move deleted (or left behind
on dryrun / filter / no-overwrite / failure) is mv's defining end state.
Bucket-side source survival is already pinned by ``remaining_keys``.

The mv exit-code shape: the
onto-itself family and any ``-`` path are 252, the missing local source is
255, in-pipeline errors are rc 1 (``move failed:`` / ``fatal error:``), and
``--validate-same-s3-paths`` adds nothing here - MinIO has no access
points, so that surface lives in the awscli port and the unit tier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

_MB = 1024 * 1024

_SRC_SINGLE: Mapping[str, bytes] = {"src/a.txt": b"move single body\n"}
_SRC_TREE: Mapping[str, bytes] = {
    "src/a.txt": b"alpha\n",
    "src/b.bin": b"\x00\x01\x02",
    "src/sub/c.txt": b"gamma\n",
    "src/sub/deep/d.txt": b"delta\n",
    "src/z.txt": b"zeta\n",
}
_SRC_BIG: Mapping[str, bytes] = {"src/big.bin": b"m" * (9 * _MB)}

_SEED_SINGLE: Mapping[str, bytes] = {"d/a.txt": b"download body\n"}
# The marker key and the prefix sibling pin the recursive rules: markers
# never transfer - so a move never deletes them - and "d-sibling" must not
# match "d/".
_SEED_TREE: Mapping[str, bytes] = {
    "d/a.txt": b"remote alpha\n",
    "d/b.bin": b"\x07\x08",
    "d/sub/c.txt": b"remote gamma\n",
    "d/marker/": b"",
    "d-sibling.txt": b"sibling\n",
}
_SEED_PAGED: Mapping[str, bytes] = {f"pg/k{i:02d}": b"p" for i in range(5)}
_SEED_META_KWARGS: Mapping[str, Mapping[str, Any]] = {
    "d/meta.txt": {
        "Body": b"styled body\n",
        "ContentType": "text/css",
        "Metadata": {"team": "blue"},
    }
}
_SEED_BIG_KWARGS: Mapping[str, Mapping[str, Any]] = {
    "d/big.bin": {
        "Body": b"M" * (9 * _MB),
        "ContentType": "text/css",
        "Metadata": {"team": "blue"},
    }
}

SCENARIOS: tuple[CpScenario, ...] = (
    # -- uploads (the source tree shrinks) -----------------------------------
    CpScenario(
        name="mv_upload_single",
        argv=("mv", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/key.txt"),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="mv_upload_recursive",
        argv=("mv", "src", f"s3://{BUCKET_TOKEN}/tree/", "--recursive"),
        local_src=_SRC_TREE,
    ),
    CpScenario(
        name="mv_upload_filters",
        argv=(
            "mv",
            "src",
            f"s3://{BUCKET_TOKEN}/tree/",
            "--recursive",
            "--exclude",
            "*",
            "--include",
            "*.txt",
        ),
        # Excluded files are neither moved nor deleted: src keeps b.bin.
        local_src=_SRC_TREE,
    ),
    CpScenario(
        name="mv_upload_dryrun_recursive",
        argv=("mv", "src", f"s3://{BUCKET_TOKEN}/tree/", "--recursive", "--dryrun"),
        local_src=_SRC_TREE,
    ),
    CpScenario(
        name="mv_upload_quiet",
        argv=("mv", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/key.txt", "--quiet"),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="mv_upload_multipart",
        argv=("mv", "src/big.bin", f"s3://{BUCKET_TOKEN}/up/big.bin"),
        local_src=_SRC_BIG,
    ),
    CpScenario(
        name="mv_upload_missing_source",
        argv=("mv", "src/none.txt", f"s3://{BUCKET_TOKEN}/up/key.txt"),
        local_src=_SRC_SINGLE,
        expected_stderr_tokens_ours=("does not exist",),
        expected_stderr_tokens_aws=("does not exist",),
    ),
    CpScenario(
        name="mv_upload_dir_without_recursive",
        # A directory source becomes one item whose open fails in flight
        # ([Errno 21]); the failed move leaves the tree alone.
        argv=("mv", "src", f"s3://{BUCKET_TOKEN}/up/key"),
        local_src=_SRC_SINGLE,
        expected_stderr_tokens_ours=("move failed:",),
        expected_stderr_tokens_aws=("move failed:",),
    ),
    # -- downloads (the bucket shrinks) ---------------------------------------
    CpScenario(
        name="mv_download_single",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d/a.txt", "dest/out.txt"),
        seed=_SEED_SINGLE,
        capture_tree=True,
        mtime_key=("d/a.txt", "dest/out.txt"),
    ),
    CpScenario(
        # aws filters a SINGLE-object mv too: the excluded source is neither
        # transferred nor deleted - the bucket end state (golden) proves the
        # object survived, and dest/ stays empty. Guards the single-source
        # filter lane (an excluded mv source being deleted was the hazard).
        name="mv_download_single_exclude_all",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d/a.txt", "dest/", "--exclude", "*"),
        seed=_SEED_SINGLE,
        capture_tree=True,
    ),
    CpScenario(
        name="mv_download_to_dir",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d/a.txt", "dest/"),
        seed=_SEED_SINGLE,
        capture_tree=True,
    ),
    CpScenario(
        name="mv_download_recursive",
        # The folder marker and the prefix sibling survive the move.
        argv=("mv", f"s3://{BUCKET_TOKEN}/d", "dest/", "--recursive"),
        seed=_SEED_TREE,
        capture_tree=True,
    ),
    CpScenario(
        name="mv_download_filters",
        argv=(
            "mv",
            f"s3://{BUCKET_TOKEN}/d",
            "dest/",
            "--recursive",
            "--exclude",
            "*",
            "--include",
            "*.txt",
        ),
        # Excluded keys are neither downloaded nor deleted.
        seed=_SEED_TREE,
        capture_tree=True,
    ),
    CpScenario(
        name="mv_download_dryrun_recursive",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d", "dest/", "--recursive", "--dryrun"),
        seed=_SEED_TREE,
        capture_tree=True,
    ),
    CpScenario(
        name="mv_download_missing_key",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d/none.txt", "dest/out.txt"),
        seed=_SEED_SINGLE,
        expected_stderr_tokens_ours=("fatal error",),
        expected_stderr_tokens_aws=("fatal error",),
    ),
    CpScenario(
        name="mv_download_page_size",
        argv=("mv", f"s3://{BUCKET_TOKEN}/pg", "dest/", "--recursive", "--page-size", "2"),
        seed=_SEED_PAGED,
        capture_tree=True,
    ),
    # -- s3 -> s3 moves --------------------------------------------------------
    CpScenario(
        name="mv_copy_single",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d/a.txt", f"s3://{BUCKET_TOKEN}/moved/a.txt"),
        seed=_SEED_SINGLE,
    ),
    CpScenario(
        name="mv_copy_recursive",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d", f"s3://{BUCKET_TOKEN}/moved/", "--recursive"),
        seed=_SEED_TREE,
    ),
    CpScenario(
        name="mv_copy_rename_props_default",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d/meta.txt", f"s3://{BUCKET_TOKEN}/moved/renamed.css"),
        seed_kwargs=_SEED_META_KWARGS,
        head_key="moved/renamed.css",
        head_fields=("ContentType", "Metadata"),
    ),
    CpScenario(
        name="mv_copy_multipart_props",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d/big.bin", f"s3://{BUCKET_TOKEN}/moved/big.bin"),
        seed_kwargs=_SEED_BIG_KWARGS,
        head_key="moved/big.bin",
        head_fields=("ContentType", "Metadata"),
    ),
    CpScenario(
        name="mv_copy_metadata_directive",
        argv=(
            "mv",
            f"s3://{BUCKET_TOKEN}/d/meta.txt",
            f"s3://{BUCKET_TOKEN}/moved/replaced.txt",
            "--metadata-directive",
            "REPLACE",
            "--content-type",
            "text/plain",
        ),
        seed_kwargs=_SEED_META_KWARGS,
        head_key="moved/replaced.txt",
        head_fields=("ContentType", "Metadata"),
    ),
    # -- the onto-itself family (252; the object survives) --------------------
    CpScenario(
        name="mv_onto_itself",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d/a.txt", f"s3://{BUCKET_TOKEN}/d/a.txt"),
        seed=_SEED_SINGLE,
        expected_stderr_tokens_ours=("Cannot mv a file onto itself",),
        expected_stderr_tokens_aws=("Cannot mv a file onto itself",),
    ),
    CpScenario(
        name="mv_onto_itself_implied",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d/a.txt", f"s3://{BUCKET_TOKEN}/d/"),
        seed=_SEED_SINGLE,
        expected_stderr_tokens_ours=("Cannot mv a file onto itself",),
        expected_stderr_tokens_aws=("Cannot mv a file onto itself",),
    ),
    CpScenario(
        name="mv_onto_itself_keyless",
        argv=("mv", f"s3://{BUCKET_TOKEN}/a.txt", f"s3://{BUCKET_TOKEN}"),
        seed={"a.txt": b"root body\n"},
        expected_stderr_tokens_ours=("Cannot mv a file onto itself",),
        expected_stderr_tokens_aws=("Cannot mv a file onto itself",),
    ),
    CpScenario(
        name="mv_onto_itself_recursive",
        # The aws-cli's basename false positive applies to --recursive too.
        argv=("mv", "--recursive", f"s3://{BUCKET_TOKEN}/d", f"s3://{BUCKET_TOKEN}/"),
        seed=_SEED_SINGLE,
        expected_stderr_tokens_ours=("Cannot mv a file onto itself",),
        expected_stderr_tokens_aws=("Cannot mv a file onto itself",),
    ),
    CpScenario(
        name="mv_stream_rejected",
        argv=("mv", "-", f"s3://{BUCKET_TOKEN}/k.txt"),
        expected_stderr_tokens_ours=(
            "Streaming currently is only compatible with non-recursive cp commands",
        ),
        expected_stderr_tokens_aws=(
            "Streaming currently is only compatible with non-recursive cp commands",
        ),
    ),
    CpScenario(
        name="mv_local_local",
        argv=("mv", "src/a.txt", "dest/b.txt"),
        local_src=_SRC_SINGLE,
        expected_stderr_tokens_ours=("Error: Invalid argument type",),
        expected_stderr_tokens_aws=("Error: Invalid argument type",),
    ),
    # -- no-overwrite (the source must survive a skipped move) ----------------
    CpScenario(
        name="mv_no_overwrite_upload_exists",
        argv=("mv", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/key.txt", "--no-overwrite"),
        local_src=_SRC_SINGLE,
        seed={"up/key.txt": b"already there\n"},
        head_key="up/key.txt",
        head_fields=("ContentLength",),
    ),
    CpScenario(
        name="mv_no_overwrite_download_exists",
        argv=("mv", f"s3://{BUCKET_TOKEN}/d/a.txt", "dest/out.txt", "--no-overwrite"),
        local_src={"dest/out.txt": b"existing local\n"},
        seed=_SEED_SINGLE,
        capture_tree=True,
    ),
    CpScenario(
        name="mv_no_overwrite_copy_exists",
        argv=(
            "mv",
            f"s3://{BUCKET_TOKEN}/d/a.txt",
            f"s3://{BUCKET_TOKEN}/moved/a.txt",
            "--no-overwrite",
        ),
        seed={"d/a.txt": b"download body\n", "moved/a.txt": b"already there\n"},
    ),
)
