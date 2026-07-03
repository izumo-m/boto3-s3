"""Shared ``mb`` scenarios: the single source for golden replay and e2e parity.

Same contract as ``rm_scenarios`` with the bucket-lifecycle extras:

- the scenario bucket is a **sibling** of the suite's main bucket (the e2e
  main bucket must stay existing and empty; the functional suite derives a
  sibling of ``FUNCTIONAL_BUCKET``), pre-created per scenario when
  ``pre_create`` asks for it and force-deleted between runs;
- the end state is ``(bucket_exists, remaining_keys)`` captured by
  ``harness.capture_bucket_state`` - goldens record both;
- stdout shares rm's sorted normalization (mb lines carry no timestamps).

Charter note (docs/overview.md section 3): the exit code is compared for *every*
scenario, unconditionally. mb's shape: usage errors (no scheme, a ``--x-s3``
directory bucket) are 252; everything after the operation starts is rc 1
(``make_bucket failed:``), never 254.
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.utils.harness import BUCKET_TOKEN
from tests.utils.scenario import BaseScenario, resolve_argv

__all__ = ["SCENARIOS", "MbScenario", "resolve_argv"]


@dataclass(frozen=True)
class MbScenario(BaseScenario):
    """One ``mb`` invocation against a fixed sibling-bucket start state."""

    # True => the scenario bucket already exists when the command runs.
    pre_create: bool = False


SCENARIOS: tuple[MbScenario, ...] = (
    MbScenario("mb_basic", ("mb", f"s3://{BUCKET_TOKEN}")),
    # --- usage errors (rc 252) -----------------------------------------------
    MbScenario(
        "mb_no_scheme",
        ("mb", BUCKET_TOKEN),
        expected_stderr_tokens_ours=("Invalid argument type",),
        expected_stderr_tokens_aws=("Invalid argument type",),
    ),
    MbScenario(
        "mb_express_suffix",
        ("mb", f"s3://{BUCKET_TOKEN}--x-s3"),
        expected_stderr_tokens_ours=("directory bucket",),
        expected_stderr_tokens_aws=("directory bucket",),
    ),
    # --- post-start errors (rc 1, never 254) ----------------------------------
    # diff_only: moto mirrors real S3's us-east-1 quirk (re-creating a bucket
    # you own succeeds), while MinIO answers BucketAlreadyOwnedByYou -> rc 1
    # on both live sides; a golden would freeze the MinIO-specific rc.
    MbScenario(
        "mb_existing",
        ("mb", f"s3://{BUCKET_TOKEN}"),
        pre_create=True,
        diff_only=True,
        expected_stderr_tokens_ours=("make_bucket failed",),
        expected_stderr_tokens_aws=("make_bucket failed",),
    ),
    # botocore client-side validation of Bucket="" -> rc 1 (same as rm).
    MbScenario(
        "mb_empty_uri",
        ("mb", "s3://"),
        expected_stderr_tokens_ours=("make_bucket failed", "Invalid bucket name"),
        expected_stderr_tokens_aws=("make_bucket failed", "Invalid bucket name"),
    ),
    # --- quirks ----------------------------------------------------------------
    # aws mb silently drops the key part (split_s3_bucket_key keeps the
    # bucket); the end state pins that the bucket exists with no objects.
    MbScenario("mb_key_dropped", ("mb", f"s3://{BUCKET_TOKEN}/some/key")),
    MbScenario("mb_tags", ("mb", f"s3://{BUCKET_TOKEN}", "--tags", "Key1", "Value1")),
)
