"""Shared ``website`` scenarios: the single source for golden replay and e2e parity.

Same contract as ``ls_scenarios``, with an inverted twist (testing.md section 7):
**MinIO rejects every PutBucketWebsite with MalformedXML** while moto
supports the operation fully. Server-reaching
scenarios therefore cannot have goldens - against MinIO they exit 254 on
both sides, against real S3 the valid ones would exit 0 on both sides, and
a moto replay would succeed where the MinIO capture failed. They are
``diff_only=True`` with **no stderr tokens** (MalformedXML is
endpoint-specific), relying on the unconditional rc comparison; the
**success path is verified directly on moto** instead
(``tests/cli/functional/test_website_golden.py``). Only the client-side
errors (rc 252, no server contact) carry goldens.

No scenario touches objects, so there is no ``seed``; the e2e test calls
``delete_bucket_website`` best-effort afterwards for the real-S3 case
(against MinIO every put failed anyway, and its delete is a no-op).

Charter note (docs/overview.md section 3): the exit code is compared for *every*
scenario, unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass

from tests.utils.harness import BUCKET_TOKEN
from tests.utils.scenario import BaseScenario, resolve_argv

__all__ = ["SCENARIOS", "WebsiteScenario", "resolve_argv"]


@dataclass(frozen=True)
class WebsiteScenario(BaseScenario):
    """One ``website`` invocation (stdout is empty in every outcome)."""


SCENARIOS: tuple[WebsiteScenario, ...] = (
    # --- server-reaching: 254 on MinIO (MalformedXML), 0 on real S3 ----------
    WebsiteScenario(
        "website_index",
        ("website", f"s3://{BUCKET_TOKEN}", "--index-document", "index.html"),
        diff_only=True,
    ),
    WebsiteScenario(
        "website_error_only",
        ("website", f"s3://{BUCKET_TOKEN}", "--error-document", "error.html"),
        diff_only=True,
    ),
    WebsiteScenario(
        "website_both",
        (
            "website",
            f"s3://{BUCKET_TOKEN}",
            "--index-document",
            "index.html",
            "--error-document",
            "error.html",
        ),
        diff_only=True,
    ),
    # The empty configuration goes out for the *server* to reject - 254 on
    # MinIO and on real S3 alike (InvalidArgument there), never client-side.
    WebsiteScenario("website_no_options", ("website", f"s3://{BUCKET_TOKEN}"), diff_only=True),
    # MinIO checks the XML before bucket existence (MalformedXML), real S3
    # answers NoSuchBucket - 254 either way, tokens endpoint-specific.
    WebsiteScenario(
        "website_nonexistent_bucket",
        ("website", f"s3://{BUCKET_TOKEN}-no-such", "--index-document", "index.html"),
        diff_only=True,
    ),
    WebsiteScenario(
        "website_no_scheme",
        ("website", BUCKET_TOKEN, "--index-document", "index.html"),
        diff_only=True,
    ),
    WebsiteScenario(
        "website_trailing_slash",
        ("website", f"s3://{BUCKET_TOKEN}/", "--index-document", "index.html"),
        diff_only=True,
    ),
    # --- client-side errors (rc 252, no server contact): golden-backed ------
    # aws folds the key into the Bucket parameter; botocore's name regex
    # rejects it.
    WebsiteScenario(
        "website_with_key",
        ("website", f"s3://{BUCKET_TOKEN}/some/key", "--index-document", "index.html"),
        expected_stderr_tokens_ours=("Parameter validation failed",),
        expected_stderr_tokens_aws=("Parameter validation failed",),
    ),
    WebsiteScenario(
        "website_empty_uri",
        ("website", "s3://", "--index-document", "index.html"),
        expected_stderr_tokens_ours=("Parameter validation failed",),
        expected_stderr_tokens_aws=("Parameter validation failed",),
    ),
    WebsiteScenario(
        "website_extra_arg",
        ("website", f"s3://{BUCKET_TOKEN}", "extra-arg", "--index-document", "index.html"),
        expected_stderr_tokens_ours=("Unknown options",),
        expected_stderr_tokens_aws=("Unknown options",),
    ),
)
