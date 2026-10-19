"""Shared ``website`` scenarios: the single source for golden replay and e2e parity.

Same contract as ``ls_scenarios``. Real S3, moto, and the dev stack all
support PutBucketWebsite, so the server-reaching scenarios carry goldens
like any other; the functional replay runs them on moto. The exception is a
configuration without an IndexDocument (endpoint-fidelity T8): real S3 and
the dev stack reject it, moto stores it, so those two scenarios are
``diff_only`` and rely on the unconditional rc comparison.

No scenario touches objects, so there is no ``seed``; the e2e test calls
``delete_bucket_website`` afterwards so a successful put does not leave
website hosting on the shared bucket.

Charter note (design/overview.md section 3): the exit code is compared for *every*
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
    # --- server-reaching -------------------------------------------------------
    WebsiteScenario(
        "website_index",
        ("website", f"s3://{BUCKET_TOKEN}", "--index-document", "index.html"),
    ),
    # endpoint-fidelity T8: no IndexDocument - rejected by the dev stack
    # (254) and, as documented, by real S3 (unverified for this shape);
    # stored by moto (0).
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
    ),
    # endpoint-fidelity T8: the empty configuration goes out for the
    # *server* to reject, never client-side - 254 on real S3 and the dev
    # stack, while moto stores it (0).
    WebsiteScenario("website_no_options", ("website", f"s3://{BUCKET_TOKEN}"), diff_only=True),
    WebsiteScenario(
        "website_nonexistent_bucket",
        ("website", f"s3://{BUCKET_TOKEN}-no-such", "--index-document", "index.html"),
    ),
    WebsiteScenario(
        "website_no_scheme",
        ("website", BUCKET_TOKEN, "--index-document", "index.html"),
    ),
    WebsiteScenario(
        "website_trailing_slash",
        ("website", f"s3://{BUCKET_TOKEN}/", "--index-document", "index.html"),
    ),
    # --- client-side errors (rc 252, no server contact): golden-backed ------
    # aws folds the key into the Bucket parameter; botocore's name regex
    # rejects it.
    WebsiteScenario(
        "website_with_key",
        ("website", f"s3://{BUCKET_TOKEN}/some/key", "--index-document", "index.html"),
        expected_stderr_tokens_ours=("Invalid bucket name",),
        expected_stderr_tokens_aws=("Invalid bucket name",),
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
