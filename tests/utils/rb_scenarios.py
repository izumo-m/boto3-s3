"""Shared ``rb`` scenarios: the single source for golden replay and e2e parity.

Same contract as ``mb_scenarios`` (sibling bucket, ``pre_create`` start
state, ``capture_bucket_state`` end state, rm's sorted stdout normalization)
plus a ``seed`` layout for the non-empty cases.

Charter note (docs/overview.md section 3): the exit code is compared for *every*
scenario, unconditionally. rb's shape: usage errors (no scheme, a key part)
are 252; a ``--force`` whose inner rm fails is **255** (aws's RuntimeError
into the general handler); everything after delete_bucket starts is rc 1
(``remove_bucket failed:``), never 254.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.utils.harness import BUCKET_TOKEN
from tests.utils.scenario import BaseScenario, resolve_argv

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["SCENARIOS", "RbScenario", "resolve_argv"]

_TREE: Mapping[str, int] = {
    "data/a.txt": 3,
    "data/sub/b.txt": 5,
}


@dataclass(frozen=True)
class RbScenario(BaseScenario):
    """One ``rb`` invocation against a fixed sibling-bucket start state."""

    # True => the scenario bucket already exists when the command runs.
    pre_create: bool = False
    # Objects put into the pre-created bucket (ignored without pre_create).
    seed: Mapping[str, int] = field(default_factory=dict)


SCENARIOS: tuple[RbScenario, ...] = (
    RbScenario("rb_basic", ("rb", f"s3://{BUCKET_TOKEN}"), pre_create=True),
    # A trailing-slash-only URI has an empty key and passes aws's key check.
    RbScenario("rb_trailing_slash", ("rb", f"s3://{BUCKET_TOKEN}/"), pre_create=True),
    # --- usage errors (rc 252) -----------------------------------------------
    RbScenario(
        "rb_no_scheme",
        ("rb", BUCKET_TOKEN),
        pre_create=True,
        expected_stderr_tokens_ours=("Invalid argument type",),
        expected_stderr_tokens_aws=("Invalid argument type",),
    ),
    RbScenario(
        "rb_with_key",
        ("rb", f"s3://{BUCKET_TOKEN}/key"),
        pre_create=True,
        expected_stderr_tokens_ours=("valid bucket name only",),
        expected_stderr_tokens_aws=("valid bucket name only",),
    ),
    # --- post-start errors (rc 1, never 254) ----------------------------------
    RbScenario(
        "rb_nonexistent",
        ("rb", f"s3://{BUCKET_TOKEN}"),
        expected_stderr_tokens_ours=("remove_bucket failed", "NoSuchBucket"),
        expected_stderr_tokens_aws=("remove_bucket failed", "NoSuchBucket"),
    ),
    RbScenario(
        "rb_non_empty",
        ("rb", f"s3://{BUCKET_TOKEN}"),
        pre_create=True,
        seed=_TREE,
        expected_stderr_tokens_ours=("remove_bucket failed", "BucketNotEmpty"),
        expected_stderr_tokens_aws=("remove_bucket failed", "BucketNotEmpty"),
    ),
    # --- --force (inner rm --recursive, then the bucket delete) ----------------
    RbScenario("rb_force_empty", ("rb", f"s3://{BUCKET_TOKEN}", "--force"), pre_create=True),
    RbScenario(
        "rb_force_non_empty",
        ("rb", f"s3://{BUCKET_TOKEN}", "--force"),
        pre_create=True,
        seed=_TREE,
    ),
    # The inner rm fails (NoSuchBucket on the listing) -> rc 255 with aws's
    # fixed sentence; the bucket delete is never attempted.
    RbScenario(
        "rb_force_nonexistent",
        ("rb", f"s3://{BUCKET_TOKEN}", "--force"),
        expected_stderr_tokens_ours=("remove_bucket failed", "NoSuchBucket"),
        expected_stderr_tokens_aws=("remove_bucket failed",),
    ),
)
