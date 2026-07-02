"""Shared ``cp`` scenarios: the single source for golden replay and e2e parity.

Same contract as the rm/ls scenario tables, extended for transfers: each
scenario fixes the **local** source tree (``local_src``, materialized under a
per-run workdir the CLI runs *in* - argv and result lines then carry
cwd-stable relative paths, no path masking needed), the remote layout
(``seed`` / ``seed_kwargs``), and the argv. End states are compared three
ways: the bucket (``remaining_keys``), the local destination tree
(``capture_tree`` -> ``harness.capture_local_tree`` of ``dest/``), and one
probe object's HeadObject fields (``head_key`` / ``head_fields``); the
download mtime stamp (``mtime_key``) is asserted live per side, not via
goldens. stdout is normalized by ``harness.normalize_cp_stdout`` (progress
segments masked, result lines sorted).

Charter note (docs/overview.md section 3): the exit code is compared for *every*
scenario, unconditionally. ``diff_only`` marks endpoint-relative outcomes
(``--sse`` / ``--storage-class GLACIER`` are rejected by MinIO and accepted
by real S3 - both CLIs always agree, which is exactly the charter assertion;
the rc-1-on-MinIO shape must not be frozen into a golden). The cp exit-code
shape: in-pipeline errors are rc 1 (``... failed:`` / ``fatal error:``),
warnings-only runs are rc 2, the missing local source is 255.

Streaming scenarios (``-``) carry ``stdin`` payloads; their replay uses the
binary-capable in-process runner (``harness.run_cli_in_process_streaming``)
and the e2e diff feeds both subprocesses the same bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tests.utils.harness import BUCKET_TOKEN, seed_bucket
from tests.utils.scenario import BaseScenario, resolve_argv

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "SCENARIOS",
    "CpScenario",
    "materialize_workdir",
    "resolve_argv",
    "seed_remote",
]

_MB = 1024 * 1024

# Local source trees (workdir-relative). "src" is the directory form; single
# files address "src/<name>" directly. A "dest/" directory is always created
# by materialize_workdir so existing-directory destination semantics hold.
_SRC_SINGLE: Mapping[str, bytes] = {"src/a.txt": b"upload single body\n"}
_SRC_JSON: Mapping[str, bytes] = {"src/data.json": b'{"k": 1}\n'}
_SRC_TREE: Mapping[str, bytes] = {
    "src/a.txt": b"alpha\n",
    "src/b.bin": b"\x00\x01\x02",
    "src/sub/c.txt": b"gamma\n",
    "src/sub/deep/d.txt": b"delta\n",
    "src/z.txt": b"zeta\n",
}
_SRC_BIG: Mapping[str, bytes] = {"src/big.bin": b"m" * (9 * _MB)}

# Remote layouts. The marker key and the prefix sibling pin the recursive
# rules (markers never transfer; "d-sibling" must not match "d/").
_SEED_TREE: Mapping[str, bytes] = {
    "d/a.txt": b"remote alpha\n",
    "d/b.bin": b"\x07\x08",
    "d/sub/c.txt": b"remote gamma\n",
    "d/marker/": b"",
    "d-sibling.txt": b"sibling\n",
}
_SEED_SINGLE: Mapping[str, bytes] = {"d/a.txt": b"download body\n"}
_SEED_PAGED: Mapping[str, bytes] = {f"pg/k{i:02d}": b"p" for i in range(5)}
_SEED_CASE: Mapping[str, bytes] = {"cc/A.txt": b"upper", "cc/a.txt": b"lower"}
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


@dataclass(frozen=True)
class CpScenario(BaseScenario):
    """One ``cp`` invocation against fixed local and remote layouts.

    ``diff_only`` here marks rc-equality-only scenarios whose outcome is
    endpoint-relative (no golden).
    """

    local_src: Mapping[str, bytes] = field(default_factory=dict)
    seed: Mapping[str, bytes] = field(default_factory=dict)
    seed_kwargs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # Golden-record the dest/ tree (downloads; uploads leave it empty anyway).
    capture_tree: bool = False
    # Golden-record HeadObject fields of one probe key.
    head_key: str | None = None
    head_fields: tuple[str, ...] = ()
    # Live per-side assertion: downloaded file mtime == object LastModified.
    mtime_key: tuple[str, str] | None = None  # (s3 key, workdir-relative path)
    # Deterministic local-vs-remote ordering for sync's time judgments:
    # workdir-relative path -> offset in seconds from "now" applied with
    # os.utime after materialization. Remote seeds land at ~now, so a
    # -86400 file is strictly older and a +86400 one strictly newer than
    # its seeded counterpart, regardless of run timing.
    local_mtimes: Mapping[str, int] = field(default_factory=dict)
    # Bytes fed to the CLI's stdin (the '-' upload scenarios).
    stdin: bytes | None = None


def materialize_workdir(workdir: Any, scenario: CpScenario) -> None:
    """Create the scenario's local source tree (plus the standing ``dest/``)."""
    import os
    import time

    (workdir / "dest").mkdir(parents=True, exist_ok=True)
    for rel, body in scenario.local_src.items():
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    now = time.time()
    for rel, offset in scenario.local_mtimes.items():
        os.utime(workdir / rel, (now + offset, now + offset))


def seed_remote(client: Any, bucket: str, scenario: CpScenario) -> None:
    """Apply the scenario's remote layout (plain bodies + kwargs entries)."""
    seed_bucket(client, bucket, scenario.seed)
    for key, kwargs in scenario.seed_kwargs.items():
        client.put_object(Bucket=bucket, Key=key, **kwargs)


SCENARIOS: tuple[CpScenario, ...] = (
    # -- uploads ------------------------------------------------------------
    CpScenario(
        name="cp_upload_single",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/key.txt"),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="cp_upload_single_prefix_slash",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/"),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="cp_upload_single_bucket_root",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}"),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="cp_upload_recursive",
        argv=("cp", "src", f"s3://{BUCKET_TOKEN}/tree/", "--recursive"),
        local_src=_SRC_TREE,
    ),
    CpScenario(
        name="cp_upload_recursive_no_slash_dest",
        argv=("cp", "src", f"s3://{BUCKET_TOKEN}/tree", "--recursive"),
        local_src=_SRC_TREE,
    ),
    CpScenario(
        name="cp_upload_filters",
        argv=(
            "cp",
            "src",
            f"s3://{BUCKET_TOKEN}/tree/",
            "--recursive",
            "--exclude",
            "*",
            "--include",
            "*.txt",
        ),
        local_src=_SRC_TREE,
    ),
    CpScenario(
        name="cp_upload_exclude_all",
        argv=("cp", "src", f"s3://{BUCKET_TOKEN}/tree/", "--recursive", "--exclude", "*"),
        local_src=_SRC_TREE,
    ),
    CpScenario(
        name="cp_upload_dryrun_recursive",
        argv=("cp", "src", f"s3://{BUCKET_TOKEN}/tree/", "--recursive", "--dryrun"),
        local_src=_SRC_TREE,
    ),
    CpScenario(
        name="cp_upload_quiet",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/key.txt", "--quiet"),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="cp_upload_only_show_errors",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/key.txt", "--only-show-errors"),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="cp_upload_no_progress",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/key.txt", "--no-progress"),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="cp_upload_content_type",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/key.txt",
            "--content-type",
            "application/x-probe",
        ),
        local_src=_SRC_SINGLE,
        head_key="up/key.txt",
        head_fields=("ContentType",),
    ),
    CpScenario(
        name="cp_upload_guess_mime",
        argv=("cp", "src/data.json", f"s3://{BUCKET_TOKEN}/up/data.json"),
        local_src=_SRC_JSON,
        head_key="up/data.json",
        head_fields=("ContentType",),
    ),
    CpScenario(
        # No head probe: the server-side default content type for an untyped
        # PUT is endpoint-specific (MinIO vs moto vs AWS).
        name="cp_upload_no_guess_mime",
        argv=(
            "cp",
            "src/data.json",
            f"s3://{BUCKET_TOKEN}/up/data.json",
            "--no-guess-mime-type",
        ),
        local_src=_SRC_JSON,
    ),
    CpScenario(
        name="cp_upload_metadata",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/key.txt",
            "--metadata",
            "k1=v1,k2=v2",
        ),
        local_src=_SRC_SINGLE,
        head_key="up/key.txt",
        head_fields=("Metadata",),
    ),
    CpScenario(
        # aws-cli's shorthand parser has no empty-key guard: "=bar" parses to
        # {"": "bar"} and the command proceeds (rc 0). Pinned via --dryrun so
        # the golden stays endpoint-independent - the layer under test is the
        # option parse, not the empty-metadata-key PutObject semantics.
        name="cp_upload_metadata_empty_key_dryrun",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/key.txt",
            "--metadata",
            "=bar",
            "--dryrun",
        ),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        # An empty key NOT followed by "=" is a shorthand syntax error on
        # both sides - rc 252 with aws's "Expected: '='" wording, before any
        # server contact.
        name="cp_upload_metadata_leading_comma",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/key.txt",
            "--metadata",
            ",foo=1",
        ),
        local_src=_SRC_SINGLE,
        expected_stderr_tokens_ours=("Expected: '=', received: ','",),
        expected_stderr_tokens_aws=("Expected: '=', received: ','",),
    ),
    CpScenario(
        name="cp_upload_cache_control",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/key.txt",
            "--cache-control",
            "max-age=60",
        ),
        local_src=_SRC_SINGLE,
        head_key="up/key.txt",
        head_fields=("CacheControl",),
    ),
    CpScenario(
        name="cp_upload_storage_class_rr",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/key.txt",
            "--storage-class",
            "REDUCED_REDUNDANCY",
        ),
        local_src=_SRC_SINGLE,
        head_key="up/key.txt",
        head_fields=("StorageClass",),
    ),
    CpScenario(
        # MinIO accepts the ACL form (rc 0); applied semantics are not
        # asserted - rc/stdout parity is the contract here.
        name="cp_upload_acl_public_read",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/key.txt", "--acl", "public-read"),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="cp_upload_grants_uri",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/key.txt",
            "--grants",
            "read=uri=http://acs.amazonaws.com/groups/global/AllUsers",
        ),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="cp_upload_multipart",
        argv=("cp", "src/big.bin", f"s3://{BUCKET_TOKEN}/up/big.bin"),
        local_src=_SRC_BIG,
    ),
    CpScenario(
        # aws checks the raw path up front (rc 255, their bare RuntimeError
        # class), before any client work.
        name="cp_upload_src_missing",
        argv=("cp", "src/nope.txt", f"s3://{BUCKET_TOKEN}/x"),
        expected_stderr_tokens_ours=("does not exist",),
        expected_stderr_tokens_aws=("does not exist",),
    ),
    CpScenario(
        # No directory check on the single path - the transfer fails
        # at open time on both sides (rc 1).
        name="cp_upload_dir_no_recursive",
        argv=("cp", "src", f"s3://{BUCKET_TOKEN}/x"),
        local_src=_SRC_SINGLE,
        expected_stderr_tokens_ours=("upload failed", "Is a directory"),
        expected_stderr_tokens_aws=("upload failed", "Is a directory"),
    ),
    CpScenario(
        name="cp_upload_dest_bucket_missing",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}-missing-cp/key.txt"),
        local_src=_SRC_SINGLE,
        expected_stderr_tokens_ours=("upload failed",),
        expected_stderr_tokens_aws=("upload failed",),
    ),
    CpScenario(
        # MinIO rejects SSE without KMS (rc 1), real S3 accepts (rc 0): the
        # unconditional rc comparison holds either way (website precedent).
        name="cp_upload_sse",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/key.txt", "--sse"),
        local_src=_SRC_SINGLE,
        diff_only=True,
    ),
    CpScenario(
        # MinIO: InvalidStorageClass (rc 1); real S3 accepts (rc 0).
        name="cp_upload_storage_class_glacier",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/key.txt",
            "--storage-class",
            "GLACIER",
        ),
        local_src=_SRC_SINGLE,
        diff_only=True,
    ),
    CpScenario(
        name="cp_locallocal",
        argv=("cp", "src/a.txt", "dest/b.txt"),
        local_src=_SRC_SINGLE,
        expected_stderr_tokens_ours=("Invalid argument type",),
        expected_stderr_tokens_aws=("Invalid argument type",),
    ),
    CpScenario(
        name="cp_page_size_nonint",
        argv=("cp", f"s3://{BUCKET_TOKEN}/pg/", "dest", "--recursive", "--page-size", "abc"),
        expected_stderr_tokens_ours=("invalid literal",),
        expected_stderr_tokens_aws=("invalid literal",),
    ),
    # -- downloads ----------------------------------------------------------
    CpScenario(
        name="cp_download_single",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/a.txt", "dest/out.bin"),
        seed=_SEED_SINGLE,
        capture_tree=True,
        mtime_key=("d/a.txt", "dest/out.bin"),
    ),
    CpScenario(
        name="cp_download_to_existing_dir",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/a.txt", "dest"),
        seed=_SEED_SINGLE,
        capture_tree=True,
    ),
    CpScenario(
        name="cp_download_trailing_sep_new_dir",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/a.txt", "dest/new/"),
        seed=_SEED_SINGLE,
        capture_tree=True,
    ),
    CpScenario(
        name="cp_download_recursive",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/", "dest", "--recursive"),
        seed=_SEED_TREE,
        capture_tree=True,
    ),
    CpScenario(
        name="cp_download_recursive_filters",
        argv=(
            "cp",
            f"s3://{BUCKET_TOKEN}/d/",
            "dest",
            "--recursive",
            "--exclude",
            "*",
            "--include",
            "*.txt",
        ),
        seed=_SEED_TREE,
        capture_tree=True,
    ),
    CpScenario(
        name="cp_download_missing_key",
        argv=("cp", f"s3://{BUCKET_TOKEN}/no-such-key", "dest/x"),
        expected_stderr_tokens_ours=("fatal error", 'Key "no-such-key" does not exist'),
        expected_stderr_tokens_aws=("fatal error", 'Key "no-such-key" does not exist'),
    ),
    CpScenario(
        # A trailing-slash key is a *single* path: HeadObject on "d/" -> 404
        # (the marker object is deliberately not seeded here).
        name="cp_download_prefix_slash_nonrecursive",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/", "dest/x"),
        seed=_SEED_SINGLE,
        expected_stderr_tokens_ours=("fatal error", 'Key "d/" does not exist'),
        expected_stderr_tokens_aws=("fatal error", 'Key "d/" does not exist'),
    ),
    CpScenario(
        name="cp_download_dryrun",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/a.txt", "dest/x", "--dryrun"),
        seed=_SEED_SINGLE,
        capture_tree=True,  # stays empty: HEAD only
    ),
    CpScenario(
        name="cp_download_page_size",
        argv=("cp", f"s3://{BUCKET_TOKEN}/pg/", "dest", "--recursive", "--page-size", "2"),
        seed=_SEED_PAGED,
        capture_tree=True,
    ),
    CpScenario(
        # MinIO answers InvalidArgument for MaxKeys=-1 (rm precedent); the
        # message text is endpoint-specific.
        name="cp_download_page_size_negative",
        argv=("cp", f"s3://{BUCKET_TOKEN}/pg/", "dest", "--recursive", "--page-size", "-1"),
        seed=_SEED_PAGED,
        diff_only=True,
    ),
    CpScenario(
        # `cp s3://bucket .`: aws lists and exact-matches nothing -> rc 0,
        # nothing transferred.
        name="cp_keyless_nonrecursive",
        argv=("cp", f"s3://{BUCKET_TOKEN}", "."),
        seed=_SEED_SINGLE,
    ),
    # -- s3 -> s3 copies ----------------------------------------------------
    CpScenario(
        name="cp_copy_single",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/a.txt", f"s3://{BUCKET_TOKEN}/cp/a.txt"),
        seed=_SEED_SINGLE,
    ),
    CpScenario(
        name="cp_copy_rename",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/a.txt", f"s3://{BUCKET_TOKEN}/cp/renamed.bin"),
        seed=_SEED_SINGLE,
    ),
    CpScenario(
        name="cp_copy_recursive",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/", f"s3://{BUCKET_TOKEN}/cp/", "--recursive"),
        seed=_SEED_TREE,
    ),
    CpScenario(
        name="cp_copy_props_default",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/meta.txt", f"s3://{BUCKET_TOKEN}/cp/meta.txt"),
        seed_kwargs=_SEED_META_KWARGS,
        head_key="cp/meta.txt",
        head_fields=("ContentType", "Metadata"),
    ),
    CpScenario(
        # Metadata is dropped under copy-props none; the resulting default
        # ContentType is endpoint-specific, so only Metadata is probed.
        name="cp_copy_props_none",
        argv=(
            "cp",
            f"s3://{BUCKET_TOKEN}/d/meta.txt",
            f"s3://{BUCKET_TOKEN}/cp/meta.txt",
            "--copy-props",
            "none",
        ),
        seed_kwargs=_SEED_META_KWARGS,
        head_key="cp/meta.txt",
        head_fields=("Metadata",),
    ),
    CpScenario(
        name="cp_copy_metadata_replace",
        argv=(
            "cp",
            f"s3://{BUCKET_TOKEN}/d/meta.txt",
            f"s3://{BUCKET_TOKEN}/cp/meta.txt",
            "--metadata",
            "fresh=yes",
        ),
        seed_kwargs=_SEED_META_KWARGS,
        head_key="cp/meta.txt",
        head_fields=("Metadata",),
    ),
    CpScenario(
        name="cp_copy_metadata_directive_copy",
        argv=(
            "cp",
            f"s3://{BUCKET_TOKEN}/d/meta.txt",
            f"s3://{BUCKET_TOKEN}/cp/meta.txt",
            "--metadata-directive",
            "COPY",
        ),
        seed_kwargs=_SEED_META_KWARGS,
        head_key="cp/meta.txt",
        head_fields=("ContentType", "Metadata"),
    ),
    CpScenario(
        # Multipart copy: the copy-props HEAD-injection path on a live
        # endpoint - the source ContentType/Metadata must survive.
        name="cp_copy_multipart_props",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/big.bin", f"s3://{BUCKET_TOKEN}/cp/big.bin"),
        seed_kwargs=_SEED_BIG_KWARGS,
        head_key="cp/big.bin",
        head_fields=("ContentType", "Metadata"),
    ),
    CpScenario(
        name="cp_copy_missing_key",
        argv=("cp", f"s3://{BUCKET_TOKEN}/no-such-key", f"s3://{BUCKET_TOKEN}/cp/x"),
        expected_stderr_tokens_ours=("fatal error", 'Key "no-such-key" does not exist'),
        expected_stderr_tokens_aws=("fatal error", 'Key "no-such-key" does not exist'),
    ),
    # -- streaming ------------------------------------------------------------
    CpScenario(
        name="cp_stream_upload",
        argv=("cp", "-", f"s3://{BUCKET_TOKEN}/stream.txt"),
        stdin=b"streamed golden body\n",
    ),
    CpScenario(
        # A destination taking the source's name appends the literal
        # '-' basename (the formatted abspath of '-').
        name="cp_stream_upload_prefix_dash",
        argv=("cp", "-", f"s3://{BUCKET_TOKEN}/pre/"),
        stdin=b"dash quirk\n",
    ),
    CpScenario(
        name="cp_stream_download",
        argv=("cp", f"s3://{BUCKET_TOKEN}/stream.txt", "-"),
        seed={"stream.txt": b"streamed body line\n"},
    ),
    CpScenario(
        name="cp_stream_expected_size",
        argv=("cp", "-", f"s3://{BUCKET_TOKEN}/stream.txt", "--expected-size", "21"),
        stdin=b"streamed golden body\n",
    ),
    CpScenario(
        # aws converts with a bare int() at submit time -> fatal rc 1.
        name="cp_stream_expected_size_nonint",
        argv=("cp", "-", f"s3://{BUCKET_TOKEN}/stream.txt", "--expected-size", "abc"),
        stdin=b"x",
        expected_stderr_tokens_ours=("fatal error", "invalid literal"),
        expected_stderr_tokens_aws=("fatal error", "invalid literal"),
    ),
    CpScenario(
        name="cp_stream_recursive",
        argv=("cp", "-", f"s3://{BUCKET_TOKEN}/x", "--recursive"),
        stdin=b"x",
        expected_stderr_tokens_ours=("only compatible with non-recursive cp commands",),
        expected_stderr_tokens_aws=("only compatible with non-recursive cp commands",),
    ),
    # -- no-overwrite ---------------------------------------------------------
    CpScenario(
        name="cp_no_overwrite_upload_new",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/key.txt", "--no-overwrite"),
        local_src=_SRC_SINGLE,
        head_key="up/key.txt",
        head_fields=("ContentLength",),
    ),
    CpScenario(
        # The seeded 9-byte object must survive (PreconditionFailed -> silent
        # skip, rc 0): the head probe pins the non-overwrite.
        name="cp_no_overwrite_upload_exists",
        argv=("cp", "src/a.txt", f"s3://{BUCKET_TOKEN}/up/key.txt", "--no-overwrite"),
        local_src=_SRC_SINGLE,
        seed={"up/key.txt": b"untouched"},
        head_key="up/key.txt",
        head_fields=("ContentLength",),
    ),
    CpScenario(
        name="cp_no_overwrite_download_exists",
        argv=("cp", f"s3://{BUCKET_TOKEN}/d/a.txt", "dest/out.bin", "--no-overwrite"),
        local_src={"dest/out.bin": b"keep the local bytes"},
        seed=_SEED_SINGLE,
        capture_tree=True,
    ),
    CpScenario(
        name="cp_no_overwrite_copy_exists",
        argv=(
            "cp",
            f"s3://{BUCKET_TOKEN}/d/a.txt",
            f"s3://{BUCKET_TOKEN}/cp/a.txt",
            "--no-overwrite",
        ),
        seed={"d/a.txt": b"download body\n", "cp/a.txt": b"old-copy"},
        head_key="cp/a.txt",
        head_fields=("ContentLength",),
    ),
    # -- checksum options -------------------------------------------------------
    CpScenario(
        name="cp_checksum_sha256",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/key.txt",
            "--checksum-algorithm",
            "SHA256",
        ),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="cp_checksum_crc32c",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/key.txt",
            "--checksum-algorithm",
            "CRC32C",
        ),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        # The CRT-backed algorithm family (needs awscrt - installed here by
        # the dev group's botocore[crt]; the dist leaves CRT to the opt-in
        # `crt` extra); aws accepts it against MinIO (rc 0).
        name="cp_checksum_xxhash64",
        argv=(
            "cp",
            "src/a.txt",
            f"s3://{BUCKET_TOKEN}/up/key.txt",
            "--checksum-algorithm",
            "XXHASH64",
        ),
        local_src=_SRC_SINGLE,
    ),
    CpScenario(
        name="cp_checksum_mode_download",
        argv=(
            "cp",
            f"s3://{BUCKET_TOKEN}/d/a.txt",
            "dest/out.bin",
            "--checksum-mode",
            "ENABLED",
        ),
        seed=_SEED_SINGLE,
        capture_tree=True,
    ),
    # -- case-conflict ----------------------------------------------------------
    CpScenario(
        # In-S3 collision (A.txt admitted first in byte order; a.txt skipped).
        # The existing-local-file variants need a case-insensitive filesystem
        # and live in the awscli port tier with the aws-cli's skip guard.
        name="cp_case_conflict_skip",
        argv=(
            "cp",
            f"s3://{BUCKET_TOKEN}/cc/",
            "dest",
            "--recursive",
            "--case-conflict",
            "skip",
        ),
        seed=_SEED_CASE,
        capture_tree=True,
        expected_stderr_tokens_ours=("warning: Skipping", "differs only by case"),
        expected_stderr_tokens_aws=("warning: Skipping", "differs only by case"),
    ),
    CpScenario(
        name="cp_case_conflict_warn",
        argv=(
            "cp",
            f"s3://{BUCKET_TOKEN}/cc/",
            "dest",
            "--recursive",
            "--case-conflict",
            "warn",
        ),
        seed=_SEED_CASE,
        capture_tree=True,
        expected_stderr_tokens_ours=("warning: Downloading", "differs only by case"),
        expected_stderr_tokens_aws=("warning: Downloading", "differs only by case"),
    ),
    CpScenario(
        name="cp_case_conflict_error",
        argv=(
            "cp",
            f"s3://{BUCKET_TOKEN}/cc/",
            "dest",
            "--recursive",
            "--case-conflict",
            "error",
        ),
        seed=_SEED_CASE,
        compare_stdout=False,  # one side may have started a download line
        expected_stderr_tokens_ours=("Failed to download", "differs only by case"),
        expected_stderr_tokens_aws=("Failed to download", "differs only by case"),
    ),
)


__all__ = [
    "SCENARIOS",
    "CpScenario",
    "materialize_workdir",
    "resolve_argv",
    "seed_remote",
]
