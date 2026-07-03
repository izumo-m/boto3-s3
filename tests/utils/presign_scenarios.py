"""Shared ``presign`` scenarios: the single source for golden replay and e2e parity.

Same contract as ``ls_scenarios``: each scenario fixes the bucket layout
(``seed``) and the CLI argv so the functional and e2e suites exercise
identical inputs. Presign-specific points:

- presign is **pure client-side computation** - no scenario contacts the
  server, every scenario replays in-process, and the rc shape is 0 / 252 /
  255 only (1 and 254 cannot happen; docs/cli.md section 6).
- stdout is one URL, normalized by ``harness.normalize_presign_stdout``
  (endpoint + time/credential-dependent query values masked; param order,
  Expires, scope region, and the key path stay).
- ``fetch=True`` scenarios additionally GET both sides' URLs in the e2e
  test and compare ``(status, body-on-200)`` - the functional proof that
  the endpoint accepts our signature, kept to scenarios whose outcome is
  time-stable (not the expires-edge ones, which would race the clock).

Charter note (docs/overview.md section 3): the exit code is compared for *every*
scenario, unconditionally.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.utils.harness import BUCKET_TOKEN
from tests.utils.scenario import BaseScenario, resolve_argv

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["SCENARIOS", "PresignScenario", "resolve_argv"]

# One small object; the fetch scenarios GET it through the presigned URL.
_SEEDED: Mapping[str, int] = {"presign/basic.txt": 7}


@dataclass(frozen=True)
class PresignScenario(BaseScenario):
    """One ``presign`` invocation against a fixed bucket layout.

    ``presign`` has no golden replay, so the base ``diff_only`` is inert here.
    """

    seed: Mapping[str, int] = field(default_factory=dict)
    # True => the e2e test GETs both sides' URLs and compares status (and
    # body when 200).
    fetch: bool = False


SCENARIOS: tuple[PresignScenario, ...] = (
    PresignScenario(
        "presign_basic",
        ("presign", f"s3://{BUCKET_TOKEN}/presign/basic.txt"),
        seed=_SEEDED,
        fetch=True,
    ),
    # The scheme is optional for presign (unlike mb/rb/rm).
    PresignScenario(
        "presign_no_scheme",
        ("presign", f"{BUCKET_TOKEN}/presign/basic.txt"),
        seed=_SEEDED,
    ),
    PresignScenario(
        "presign_custom_expiry",
        ("presign", f"s3://{BUCKET_TOKEN}/presign/basic.txt", "--expires-in", "120"),
        seed=_SEEDED,
        fetch=True,
    ),
    # --region must reach the credential scope (the normalizer keeps the
    # region segment). No fetch: MinIO rejects a scope signed for another
    # region, which is its policy rather than presign parity.
    PresignScenario(
        "presign_region",
        ("presign", f"s3://{BUCKET_TOKEN}/presign/basic.txt", "--region", "ap-northeast-1"),
    ),
    # URL generation never checks existence; the fetch pins 404 == 404.
    PresignScenario(
        "presign_nonexistent_key",
        ("presign", f"s3://{BUCKET_TOKEN}/presign/no-such-key.txt"),
        fetch=True,
    ),
    # --- client-side parameter validation (rc 252) --------------------------
    PresignScenario(
        "presign_bucket_only",
        ("presign", f"s3://{BUCKET_TOKEN}"),
        expected_stderr_tokens_ours=("Invalid length for parameter Key",),
        expected_stderr_tokens_aws=("Invalid length for parameter Key",),
    ),
    PresignScenario(
        "presign_trailing_slash",
        ("presign", f"s3://{BUCKET_TOKEN}/"),
        expected_stderr_tokens_ours=("Invalid length for parameter Key",),
        expected_stderr_tokens_aws=("Invalid length for parameter Key",),
    ),
    PresignScenario(
        "presign_empty_uri",
        ("presign", "s3://"),
        expected_stderr_tokens_ours=('Invalid bucket name ""',),
        expected_stderr_tokens_aws=('Invalid bucket name ""',),
    ),
    PresignScenario(
        "presign_extra_arg",
        ("presign", f"s3://{BUCKET_TOKEN}/presign/basic.txt", "extra-arg"),
        expected_stderr_tokens_ours=("Unknown options",),
        expected_stderr_tokens_aws=("Unknown options",),
    ),
    # --- expires-in edges: no range validation anywhere (rc 0) --------------
    PresignScenario(
        "presign_expires_zero",
        ("presign", f"s3://{BUCKET_TOKEN}/presign/basic.txt", "--expires-in", "0"),
    ),
    PresignScenario(
        "presign_expires_negative",
        ("presign", f"s3://{BUCKET_TOKEN}/presign/basic.txt", "--expires-in", "-1"),
    ),
    PresignScenario(
        "presign_expires_over_max",
        ("presign", f"s3://{BUCKET_TOKEN}/presign/basic.txt", "--expires-in", "604801"),
    ),
    # A non-integer dies in the CLI's own int() conversion -> rc 255 on both
    # sides (aws's bare int() escapes to its general handler).
    PresignScenario(
        "presign_expires_nonint",
        ("presign", f"s3://{BUCKET_TOKEN}/presign/basic.txt", "--expires-in", "abc"),
        expected_stderr_tokens_ours=("invalid literal",),
        expected_stderr_tokens_aws=("invalid literal",),
    ),
    # Head-order (docs/cli.md 5.7): the --endpoint-url scheme check (252)
    # beats the bare int() coercion (255).
    PresignScenario(
        "presign_endpoint_beats_expires",
        (
            "presign",
            f"s3://{BUCKET_TOKEN}/presign/basic.txt",
            "--expires-in",
            "abc",
            "--endpoint-url",
            "badurl",
        ),
        expected_stderr_tokens_ours=("scheme is missing",),
        expected_stderr_tokens_aws=("scheme is missing",),
    ),
    # UNSIGNED config -> the plain object URL, no query at all (rc 0).
    PresignScenario(
        "presign_no_sign_request",
        ("presign", f"s3://{BUCKET_TOKEN}/presign/basic.txt", "--no-sign-request"),
    ),
)
