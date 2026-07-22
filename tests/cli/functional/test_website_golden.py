"""``boto3-s3 website`` functional coverage: golden replay + the moto success path.

Two halves, because MinIO cannot exercise the success path (testing.md section 7 -
it rejects every PutBucketWebsite with MalformedXML, the inverse of the
usual moto fidelity gap):

1. **Golden replay** for the client-side-error scenarios (rc 252, no server
   contact, so no moto backend is needed) - the only website goldens that
   exist.
2. **TestWebsiteOnMoto** - the success path verified directly against moto
   (full PutBucketWebsite support): rc 0, empty stdout,
   and the configuration read back via ``get_bucket_website``; plus
   NoSuchBucket -> rc **254**, pinning website's no-local-catch shape (the
   contrast with mb/rb's rc 1).
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.functional.conftest import FUNCTIONAL_BUCKET
from tests.utils.golden import assert_matches_golden, load_golden
from tests.utils.harness import (
    assert_stderr_tokens,
    normalize_rm_stdout,
    run_cli_in_process,
)
from tests.utils.website_scenarios import SCENARIOS, WebsiteScenario, resolve_argv

_REPLAYABLE = [scenario for scenario in SCENARIOS if not scenario.diff_only]


@pytest.mark.parametrize("scenario", _REPLAYABLE, ids=lambda s: s.name)
def test_website_matches_golden(scenario: WebsiteScenario) -> None:
    result = run_cli_in_process(resolve_argv(scenario, FUNCTIONAL_BUCKET))
    lines = normalize_rm_stdout(result.stdout, bucket=FUNCTIONAL_BUCKET)
    golden = load_golden("website", scenario.name)
    assert_matches_golden(
        golden,
        rc=result.rc,
        stdout_lines=lines,
        side="ours",
        compare_stdout=scenario.compare_stdout,
    )
    assert_stderr_tokens(
        scenario.expected_stderr_tokens_ours, result.stderr, side="ours", scenario=scenario.name
    )


def _run(argv: list[str]) -> Any:
    return run_cli_in_process(argv)


def _website_config(client: Any, bucket: str) -> dict[str, Any]:
    got = client.get_bucket_website(Bucket=bucket)
    return {k: v for k, v in got.items() if k != "ResponseMetadata"}


class TestWebsiteOnMoto:
    def test_index_document_round_trip(self, moto_s3: Any) -> None:
        result = _run(["website", f"s3://{FUNCTIONAL_BUCKET}", "--index-document", "index.html"])
        assert (result.rc, result.stdout) == (0, "")
        assert _website_config(moto_s3, FUNCTIONAL_BUCKET) == {
            "IndexDocument": {"Suffix": "index.html"}
        }

    def test_error_document_round_trip(self, moto_s3: Any) -> None:
        result = _run(["website", f"s3://{FUNCTIONAL_BUCKET}", "--error-document", "error.html"])
        assert (result.rc, result.stdout) == (0, "")
        assert _website_config(moto_s3, FUNCTIONAL_BUCKET) == {
            "ErrorDocument": {"Key": "error.html"}
        }

    def test_both_documents_round_trip(self, moto_s3: Any) -> None:
        result = _run(
            [
                "website",
                f"s3://{FUNCTIONAL_BUCKET}",
                "--index-document",
                "index.html",
                "--error-document",
                "error.html",
            ]
        )
        assert result.rc == 0
        assert _website_config(moto_s3, FUNCTIONAL_BUCKET) == {
            "IndexDocument": {"Suffix": "index.html"},
            "ErrorDocument": {"Key": "error.html"},
        }

    def test_no_scheme_and_trailing_slash_round_trip(self, moto_s3: Any) -> None:
        assert _run(["website", FUNCTIONAL_BUCKET, "--index-document", "a.html"]).rc == 0
        # Read back before the second run overwrites it: the no-scheme form
        # must have applied a real configuration, not no-op'd to rc 0.
        assert _website_config(moto_s3, FUNCTIONAL_BUCKET) == {
            "IndexDocument": {"Suffix": "a.html"}
        }
        assert _run(["website", f"s3://{FUNCTIONAL_BUCKET}/", "--index-document", "b.html"]).rc == 0
        assert _website_config(moto_s3, FUNCTIONAL_BUCKET) == {
            "IndexDocument": {"Suffix": "b.html"}
        }

    def test_nonexistent_bucket_is_254(self, moto_s3: Any) -> None:
        # No local catch (unlike mb/rb): the ClientError cause maps to 254.
        result = _run(["website", "s3://no-such-bucket", "--index-document", "index.html"])
        assert result.rc == 254
        assert "NoSuchBucket" in result.stderr
