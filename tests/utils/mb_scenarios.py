"""Shared ``mb`` scenarios: the single source for golden replay and e2e parity.

Same contract as ``rm_scenarios`` with the bucket-lifecycle extras:

- the scenario bucket is a **sibling** of the suite's main bucket (the e2e
  main bucket must stay existing and empty; the functional suite derives a
  sibling of ``FUNCTIONAL_BUCKET``), pre-created per scenario when
  ``pre_create`` asks for it; the e2e suite gives each side a fresh one;
- the end state is ``(bucket_exists, remaining_keys)`` captured by
  ``harness.capture_bucket_state`` - goldens record both;
- stdout shares rm's sorted normalization (mb lines carry no timestamps).

Charter note (design/overview.md section 3): the exit code is compared for *every*
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
    # True => compare the bucket's tag set after the run between `aws` and
    # ours, live only. endpoint-fidelity T12: real S3 applies
    # CreateBucketConfiguration.Tags while moto and the dev stack ignore it,
    # so a golden of the tag set could not serve both a real-S3 capture and
    # the moto replay; the e2e comparison checks tag application only when
    # it runs on real S3.
    capture_tags: bool = False


SCENARIOS: tuple[MbScenario, ...] = (
    MbScenario("mb_basic", ("mb", f"s3://{BUCKET_TOKEN}")),
    # --- region-relative (rc 0 in us-east-1, rc 1 elsewhere) -----------------
    # endpoint-fidelity T11: re-creating a bucket you own succeeds in
    # us-east-1 (real S3, moto, the dev stack) and fails with
    # BucketAlreadyOwnedByYou elsewhere (real S3), so the outcome is
    # region-relative: no golden, and no stderr tokens either.
    MbScenario(
        "mb_existing",
        ("mb", f"s3://{BUCKET_TOKEN}"),
        pre_create=True,
        diff_only=True,
    ),
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
    # --- quirks ----------------------------------------------------------------
    # `aws s3 mb` silently drops the key part (split_s3_bucket_key keeps the
    # bucket); the end state pins that the bucket exists with no objects.
    MbScenario("mb_key_dropped", ("mb", f"s3://{BUCKET_TOKEN}/some/key")),
    # The tags ride CreateBucketConfiguration.Tags; capture_tags compares the
    # resulting tag set between `aws` and ours (endpoint-fidelity T12).
    MbScenario(
        "mb_tags",
        ("mb", f"s3://{BUCKET_TOKEN}", "--tags", "Key1", "Value1"),
        capture_tags=True,
    ),
)
