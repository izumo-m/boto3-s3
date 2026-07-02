"""Shared ``ls`` scenarios: the single source for golden replay and e2e parity.

Each scenario fixes the bucket layout (``seed``) and the CLI argv, so the
functional (moto) and e2e (MinIO / real S3) suites exercise **identical
inputs** - that identity is what makes "golden captured from aws-cli" a valid
expectation for the moto replay.

Charter note (docs/overview.md section 3): the exit code is compared for *every*
scenario, unconditionally - there is deliberately no flag to relax it.
``compare_stdout`` / ``diff_only`` only relax stdout and golden handling.

Not represented here:

- extension options absent from ``aws s3`` (e.g. ``--help``): they cannot run
  on the aws side, which is exactly the charter's exception 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.utils.harness import BUCKET_TOKEN
from tests.utils.scenario import BaseScenario, resolve_argv

__all__ = ["SCENARIOS", "LsScenario", "resolve_argv"]

if TYPE_CHECKING:
    from collections.abc import Mapping

# Object tree exercising basenames, nested prefixes, and sizes (tree A).
_TREE: Mapping[str, int] = {
    "data/a.txt": 3,
    "data/b.txt": 100,
    "data/sub/inner.txt": 10,
    "data/sub2/x.bin": 20,
}

# Sizes spanning aws-cli's human-readable buckets: 1 Byte / 1.0 KiB / 1.0 MiB
# / 2.5 KiB (fractional). Larger units (GiB+) are unit-tested; seeding real
# multi-GiB bodies is not worth the e2e cost.
_HUMAN_READABLE: Mapping[str, int] = {
    "hr/one": 1,
    "hr/kib": 1024,
    "hr/mib": 1024 * 1024,
    "hr/frac": 2560,
}

# Flat keys only: with no CommonPrefixes in play, page boundaries cannot
# reorder the PRE/object interleave, so moto/MinIO/S3 paginate identically.
_PAGED: Mapping[str, int] = {f"pg/k{i:02d}": 1 for i in range(12)}


@dataclass(frozen=True)
class LsScenario(BaseScenario):
    """One ``ls`` invocation against a fixed bucket layout.

    ``diff_only`` here covers output that depends on server-global state the
    scenario cannot pin (e.g. the account/instance bucket list).
    """

    seed: Mapping[str, int] = field(default_factory=dict)


SCENARIOS: tuple[LsScenario, ...] = (
    # PRE lines come first within a page (aws-cli ListCommand._display_page;
    # boto3-s3 S3Storage._page_to_infos), then objects with basenames.
    LsScenario("ls_basic", ("ls", f"s3://{BUCKET_TOKEN}/data/"), _TREE),
    LsScenario("ls_recursive", ("ls", f"s3://{BUCKET_TOKEN}/data/", "--recursive"), _TREE),
    LsScenario(
        "ls_human_readable", ("ls", f"s3://{BUCKET_TOKEN}/hr/", "--human-readable"), _HUMAN_READABLE
    ),
    LsScenario("ls_summarize", ("ls", f"s3://{BUCKET_TOKEN}/data/", "--summarize"), _TREE),
    LsScenario(
        "ls_summarize_human",
        ("ls", f"s3://{BUCKET_TOKEN}/data/", "--summarize", "--human-readable"),
        _TREE,
    ),
    LsScenario("ls_page_size", ("ls", f"s3://{BUCKET_TOKEN}/pg/", "--page-size", "5"), _PAGED),
    # Key given but nothing matches -> rc 1 (aws _check_no_objects).
    LsScenario("ls_empty_prefix", ("ls", f"s3://{BUCKET_TOKEN}/no-such-prefix/")),
    # Bucket given without key -> rc 0 even when empty.
    LsScenario("ls_bucket_empty", ("ls", f"s3://{BUCKET_TOKEN}")),
    # All-buckets listing reflects whatever buckets the endpoint has, which
    # the scenario cannot pin -> live diff only.
    LsScenario("ls_all_buckets", ("ls",), diff_only=True),
    # Server-side ClientError -> rc 254 on both sides (docs/cli.md section 6). The
    # "-no-such" suffix guarantees absence on MinIO/moto; on real S3 a
    # stranger could own the name (AccessDenied instead), in which case the
    # NoSuchBucket tokens fail visibly rather than silently passing.
    LsScenario(
        "ls_nonexistent_bucket",
        ("ls", f"s3://{BUCKET_TOKEN}-no-such/"),
        expected_stderr_tokens_ours=("boto3-s3:", "NoSuchBucket"),
        expected_stderr_tokens_aws=("NoSuchBucket",),
    ),
    # Out-of-range page sizes pass through to the server with no client-side
    # validation, matching aws-cli (verified against MinIO: aws-cli sends
    # MaxKeys=0). Zero keys come back -> rc 1 with empty stdout.
    LsScenario("ls_page_size_zero", ("ls", f"s3://{BUCKET_TOKEN}/pg/", "--page-size", "0"), _PAGED),
    # Negative page size -> server InvalidArgument -> rc 254 on both sides.
    # diff_only: moto raises an internal IndexError for MaxKeys=-1 instead of
    # InvalidArgument (fidelity gap), so the scenario cannot replay there.
    LsScenario(
        "ls_page_size_negative",
        ("ls", f"s3://{BUCKET_TOKEN}/pg/", "--page-size", "-1"),
        _PAGED,
        diff_only=True,
        expected_stderr_tokens_ours=("InvalidArgument",),
        expected_stderr_tokens_aws=("InvalidArgument",),
    ),
    # A non-integer page size dies in the CLI's own int() conversion -> rc
    # 255 on both sides (aws's bare int() escapes to its *general* handler,
    # not the 252 usage path). No server contact, so it
    # replays on moto unchanged.
    LsScenario(
        "ls_page_size_nonint",
        ("ls", f"s3://{BUCKET_TOKEN}/pg/", "--page-size", "abc"),
        expected_stderr_tokens_ours=("invalid literal",),
        expected_stderr_tokens_aws=("invalid literal",),
    ),
    # Access-point ARN bucket: botocore (driving both tools) injects the AP
    # name as a host prefix onto the endpoint, so against MinIO and real S3
    # alike the request fails - but the *same way* on both sides; a mis-split
    # ARN would surface as an rc mismatch here. The failure mode itself is
    # endpoint-dependent (connect error vs AccessDenied) -> live diff only,
    # no stderr tokens.
    LsScenario(
        "ls_accesspoint_arn",
        ("ls", "s3://arn:aws:s3:us-west-2:123456789012:accesspoint/endpoint"),
        diff_only=True,
    ),
    # ARN resource types `aws s3` rejects at parse time -> rc 252 on both
    # sides, no server contact (so these replay on moto unchanged).
    LsScenario(
        "ls_object_lambda_arn",
        ("ls", "s3://arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-olap"),
        expected_stderr_tokens_ours=("boto3-s3:", "S3 Object Lambda", "s3api"),
        expected_stderr_tokens_aws=("S3 Object Lambda", "s3api"),
    ),
    LsScenario(
        "ls_outpost_bucket_arn",
        (
            "ls",
            "s3://arn:aws:s3-outposts:us-west-2:123456789012:"
            "outpost/op-01234567890123456/bucket/my-bucket",
        ),
        expected_stderr_tokens_ours=("boto3-s3:", "Outpost Bucket", "s3control"),
        expected_stderr_tokens_aws=("Outpost Bucket", "s3control"),
    ),
)
