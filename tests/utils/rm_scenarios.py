"""Shared ``rm`` scenarios: the single source for golden replay and e2e parity.

Same contract as ``ls_scenarios``: each scenario fixes the bucket layout
(``seed``) and the CLI argv so the functional (moto) and e2e (MinIO / real
S3) suites exercise identical inputs. Two rm-specific differences:

- stdout is normalized **sorted** (``harness.normalize_rm_stdout``): aws's
  delete-line order is parallel-completion order, nondeterministic run to
  run. The bucket **end state** (``remaining_keys``) is captured in the
  golden to pin what sorting relaxes.
- rm is destructive, so the e2e diff re-seeds between the aws run and the
  ours run instead of sharing one seeding.

Charter note (docs/overview.md section 3): the exit code is compared for *every*
scenario, unconditionally - there is deliberately no flag to relax it. Note
the rm exit-code shape: errors after the operation starts are rc 1 (never
254; unlike ls), usage errors are 252.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.utils.harness import BUCKET_TOKEN
from tests.utils.scenario import BaseScenario, resolve_argv

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["SCENARIOS", "RmScenario", "resolve_argv"]

# Object tree with a prefix sibling: "data-sibling.txt" shares the string
# prefix "data" and pins aws's trailing-slash normalization for recursive
# targets (it must never be deleted by "rm s3://B/data --recursive").
_TREE: Mapping[str, int] = {
    "data/a.txt": 3,
    "data/b.txt": 100,
    "data/sub/inner.txt": 10,
    "data-sibling.txt": 5,
}

# Zero-byte "/"-terminated keys are S3 console-style folder markers; the
# keyless non-recursive rm sweeps exactly those (any depth) and nothing else.
_MARKERS: Mapping[str, int] = {
    "keep/x.txt": 4,
    "m1/": 0,
    "m2/sub/": 0,
}

# Mixed extensions under one prefix for --exclude/--include ordering.
_FILTERED: Mapping[str, int] = {
    "f/a.txt": 3,
    "f/b.bin": 4,
    "f/sub/c.txt": 5,
}

_PAGED: Mapping[str, int] = {f"pg/k{i:02d}": 1 for i in range(12)}


@dataclass(frozen=True)
class RmScenario(BaseScenario):
    """One ``rm`` invocation against a fixed bucket layout."""

    seed: Mapping[str, int] = field(default_factory=dict)


SCENARIOS: tuple[RmScenario, ...] = (
    # --- single blind path -------------------------------------------------
    RmScenario("rm_single_key", ("rm", f"s3://{BUCKET_TOKEN}/data/a.txt"), _TREE),
    # Blind DeleteObject: a nonexistent key still "succeeds" (rc 0 + line).
    RmScenario("rm_single_nonexistent", ("rm", f"s3://{BUCKET_TOKEN}/no-such-key")),
    RmScenario("rm_single_dryrun", ("rm", f"s3://{BUCKET_TOKEN}/data/a.txt", "--dryrun"), _TREE),
    RmScenario("rm_dryrun_nonexistent", ("rm", f"s3://{BUCKET_TOKEN}/no-such-key", "--dryrun")),
    # A trailing-slash key is a blind delete of the marker object itself -
    # here none exists, so the line prints and nothing changes.
    RmScenario("rm_prefix_no_marker", ("rm", f"s3://{BUCKET_TOKEN}/keep/"), _MARKERS),
    # --- recursive ----------------------------------------------------------
    RmScenario("rm_recursive", ("rm", f"s3://{BUCKET_TOKEN}/data/", "--recursive"), _TREE),
    # No trailing slash: the prefix is normalized to "data/", so the
    # "data-sibling.txt" string-prefix neighbor must survive.
    RmScenario("rm_recursive_no_slash", ("rm", f"s3://{BUCKET_TOKEN}/data", "--recursive"), _TREE),
    RmScenario(
        "rm_recursive_dryrun",
        ("rm", f"s3://{BUCKET_TOKEN}/data/", "--recursive", "--dryrun"),
        _TREE,
    ),
    RmScenario(
        "rm_recursive_zero_match", ("rm", f"s3://{BUCKET_TOKEN}/no-such-prefix/", "--recursive")
    ),
    RmScenario("rm_bucket_recursive", ("rm", f"s3://{BUCKET_TOKEN}", "--recursive"), _MARKERS),
    RmScenario(
        "rm_page_size",
        ("rm", f"s3://{BUCKET_TOKEN}/pg/", "--recursive", "--page-size", "5"),
        _PAGED,
    ),
    # MaxKeys=0 passes through (charter): zero keys come back -> rc 0, no-op.
    RmScenario(
        "rm_page_size_zero",
        ("rm", f"s3://{BUCKET_TOKEN}/pg/", "--recursive", "--page-size", "0"),
        _PAGED,
    ),
    # Negative page size kills the listing -> "fatal error" rc 1 on both
    # sides (NOT 254 - rm's error shape differs from ls). diff_only: moto
    # raises an internal IndexError for MaxKeys=-1 (same gap as ls).
    RmScenario(
        "rm_page_size_negative",
        ("rm", f"s3://{BUCKET_TOKEN}/pg/", "--recursive", "--page-size", "-1"),
        _PAGED,
        diff_only=True,
        expected_stderr_tokens_ours=("fatal error", "InvalidArgument"),
        expected_stderr_tokens_aws=("fatal error", "InvalidArgument"),
    ),
    # --- bucket-root folder-marker sweep -------------------------------------
    # Keyless non-recursive rm deletes only the zero-byte "/" markers (any
    # depth); keep/x.txt must survive.
    RmScenario("rm_bucket_root", ("rm", f"s3://{BUCKET_TOKEN}"), _MARKERS),
    RmScenario("rm_bucket_root_slash", ("rm", f"s3://{BUCKET_TOKEN}/"), _MARKERS),
    # --- filters --------------------------------------------------------------
    RmScenario(
        "rm_exclude_include",
        ("rm", f"s3://{BUCKET_TOKEN}/f/", "--recursive", "--exclude", "*", "--include", "*.txt"),
        _FILTERED,
    ),
    # Reverse order: last match wins -> everything excluded, silent rc 0.
    RmScenario(
        "rm_include_exclude_order",
        ("rm", f"s3://{BUCKET_TOKEN}/f/", "--recursive", "--include", "*.txt", "--exclude", "*"),
        _FILTERED,
    ),
    RmScenario(
        "rm_exclude_single_key",
        ("rm", f"s3://{BUCKET_TOKEN}/data/a.txt", "--exclude", "*"),
        _TREE,
    ),
    # The single key roots at its parent: the basename pattern must match.
    RmScenario(
        "rm_exclude_basename_single",
        ("rm", f"s3://{BUCKET_TOKEN}/data/a.txt", "--exclude", "a.*"),
        _TREE,
    ),
    # --- output suppression ----------------------------------------------------
    RmScenario("rm_quiet", ("rm", f"s3://{BUCKET_TOKEN}/data/", "--recursive", "--quiet"), _TREE),
    RmScenario(
        "rm_only_show_errors",
        ("rm", f"s3://{BUCKET_TOKEN}/data/", "--recursive", "--only-show-errors"),
        _TREE,
    ),
    # aws quirk: --only-show-errors does NOT suppress dryrun lines.
    RmScenario(
        "rm_only_show_errors_dryrun",
        ("rm", f"s3://{BUCKET_TOKEN}/data/a.txt", "--dryrun", "--only-show-errors"),
        _TREE,
    ),
    # --- error paths (rc 1, never 254 - unlike ls) ------------------------------
    RmScenario(
        "rm_nonexistent_bucket",
        ("rm", f"s3://{BUCKET_TOKEN}-no-such/key"),
        expected_stderr_tokens_ours=("delete failed", "NoSuchBucket"),
        expected_stderr_tokens_aws=("delete failed", "NoSuchBucket"),
    ),
    RmScenario(
        "rm_nonexistent_bucket_recursive",
        ("rm", f"s3://{BUCKET_TOKEN}-no-such/", "--recursive"),
        expected_stderr_tokens_ours=("fatal error", "NoSuchBucket"),
        expected_stderr_tokens_aws=("fatal error", "NoSuchBucket"),
    ),
    # botocore client-side validation of Bucket="" -> rc 1 (not 252).
    RmScenario(
        "rm_empty_bucket_uri",
        ("rm", "s3://"),
        expected_stderr_tokens_ours=("fatal error", "Invalid bucket name"),
        expected_stderr_tokens_aws=("fatal error", "Invalid bucket name"),
    ),
    RmScenario(
        "rm_empty_bucket_with_key",
        ("rm", "s3:///key"),
        expected_stderr_tokens_ours=("delete failed", "Invalid bucket name"),
        expected_stderr_tokens_aws=("delete failed", "Invalid bucket name"),
    ),
    # A non-integer page size dies in the CLI's own int() conversion -> rc
    # 255 on both sides (aws's bare int() escapes to its *general* handler,
    # converting at parse time - it even precedes the 252 path-type check).
    # No server contact, nothing seeded, nothing deleted.
    RmScenario(
        "rm_page_size_nonint",
        ("rm", f"s3://{BUCKET_TOKEN}/pg/", "--page-size", "abc"),
        expected_stderr_tokens_ours=("invalid literal",),
        expected_stderr_tokens_aws=("invalid literal",),
    ),
    # --- usage errors (rc 252) ---------------------------------------------------
    RmScenario(
        "rm_local_path",
        ("rm", "./not-an-s3-path"),
        expected_stderr_tokens_ours=("Invalid argument type",),
        expected_stderr_tokens_aws=("Invalid argument type",),
    ),
    RmScenario(
        "rm_object_lambda_arn",
        ("rm", "s3://arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-olap"),
        expected_stderr_tokens_ours=("S3 Object Lambda", "s3api"),
        expected_stderr_tokens_aws=("S3 Object Lambda", "s3api"),
    ),
    RmScenario(
        "rm_outpost_bucket_arn",
        (
            "rm",
            "s3://arn:aws:s3-outposts:us-west-2:123456789012:"
            "outpost/op-01234567890123456/bucket/my-bucket",
        ),
        expected_stderr_tokens_ours=("Outpost Bucket", "s3control"),
        expected_stderr_tokens_aws=("Outpost Bucket", "s3control"),
    ),
)
