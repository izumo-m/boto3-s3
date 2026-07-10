"""Port of aws-cli's functional presign tests to ``boto3-s3 presign``.

Provenance: aws-cli's ``tests/functional/s3/test_presign_command.py``
(aws-cli 2.35.18). Test names, argv, and the expected URLs -
the exact frozen-time signatures included - are kept verbatim so the file
stays diffable against the aws-cli original.

The behaviour under test is aws-cli's s3 command implementation in
``vendor/aws-cli/awscli/customizations/s3/`` - ``subcommands.py`` (``PresignCommand``).

A test carrying no ``# aws-cli:`` comment ports the aws-cli test of the same
class and method name. A ``# aws-cli:`` comment names a divergent origin
instead: above a test for a per-test difference (a rename, a parametrized
merge of several aws-cli tests, a method from a different aws-cli class or
file, or ``none`` for a boto3-s3 addition), or above a class when a whole
block was carved out of one aws-cli class under the same method names.

Adaptation rules (on top of the mb port's - see
``tests/cli/awscli/test_mb_command.py``):

- Time freezing: the aws-cli mocks ``time.time`` and
  ``datetime.datetime.utcnow``; runtime botocore signs through one seam,
  ``get_current_datetime``, so patching it reproduces the aws-cli's
  signatures bit-for-bit. With awscrt installed (the dev dependency group
  pulls in ``botocore[crt]``; the dist leaves CRT to the opt-in ``crt``
  extra) botocore swaps in its CRT signers, whose module holds
  a second binding of that seam - both are patched, honoring the CRT
  callers' ``remove_tzinfo=False`` (SigV4 itself is deterministic, so both
  signer families yield the aws-cli's exact signatures).
- The aws-cli signs with its harness credentials (``access_key`` /
  ``secret_key``) through aws v2's bundled botocore - SigV4-only and
  us-east-1-regional. The client factory rebuilds ``build_client``'s pinned
  base Config (s3v4 + regional us-east-1) with those fixed credentials, and
  the aws-cli's config-file fixtures (``enable_sigv4_from_config_file`` /
  ``enable_addressing_mode_in_config``) become client-Config equivalents.
"""

from __future__ import annotations

import contextlib
import datetime
from typing import Any
from unittest import mock
from urllib.parse import urlsplit

import boto3
from botocore.compat import HAS_CRT
from botocore.config import Config

from boto3_s3_cli.commands.base import Context
from tests.utils.harness import run_cli_in_process

# Value used to fix botocore's signing clock so we know the exact values of
# the signatures generated (the aws-cli's FROZEN_DATETIME).
FROZEN_DATETIME = datetime.datetime(2016, 8, 18, 14, 33, 3, 0)
DEFAULT_EXPIRES = 3600


def _frozen_clock(remove_tzinfo: bool = True) -> datetime.datetime:
    """``get_current_datetime`` replacement (CRT signers pass ``False``)."""
    if remove_tzinfo:
        return FROZEN_DATETIME
    return FROZEN_DATETIME.replace(tzinfo=datetime.timezone.utc)


# build_client's pinned base (clientfactory.py): SigV4 always, us-east-1 regional.
_BASE_S3: dict[str, Any] = {"us_east_1_regional_endpoint": "regional"}
_DEFAULT_CONFIG = Config(signature_version="s3v4", s3=_BASE_S3)
_PATH_STYLE_CONFIG = Config(signature_version="s3v4", s3={**_BASE_S3, "addressing_style": "path"})


def _get_presigned_url_for_cmd(argv: list[str], *, config: Config = _DEFAULT_CONFIG) -> str:
    """The port's ``get_presigned_url_for_cmd`` (frozen clock, fixed creds)."""

    def factory(args: Any) -> Any:
        session = boto3.session.Session(
            aws_access_key_id="access_key",
            aws_secret_access_key="secret_key",
            region_name=getattr(args, "region", None) or "us-east-1",
        )
        return session.client("s3", config=config)

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch("botocore.auth.get_current_datetime", side_effect=_frozen_clock)
        )
        if HAS_CRT:
            stack.enter_context(
                mock.patch("botocore.crt.auth.get_current_datetime", side_effect=_frozen_clock)
            )
        result = run_cli_in_process(argv, ctx=Context(client_factory=factory))
    assert result.rc == 0, result.stderr
    return result.stdout.strip()


def _parse_query_string(query_string: str) -> dict[str, str]:
    pairs = []
    for part in query_string.split("&"):
        pairs.append(part.split("=", 1))
    return dict(pairs)


def _assert_presigned_url_matches(actual_url: str, expected_match: dict[str, Any]) -> None:
    """Compare netloc/path/query against the aws-cli's expected dict.

    Like the aws-cli helper, the query string is compared as a dict because
    query-param ordering is not guaranteed; values stay percent-encoded.
    """
    parts = urlsplit(actual_url)
    assert parts.netloc == expected_match["hostname"]
    assert parts.path == expected_match["path"]
    assert _parse_query_string(parts.query) == expected_match["query_params"]


class TestPresignCommand:
    def test_generates_a_url(self) -> None:
        stdout = _get_presigned_url_for_cmd(["presign", "s3://bucket/key"])
        _assert_presigned_url_matches(
            stdout,
            {
                "hostname": "bucket.s3.us-east-1.amazonaws.com",
                "path": "/key",
                "query_params": {
                    "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                    "X-Amz-Credential": ("access_key%2F20160818%2Fus-east-1%2Fs3%2Faws4_request"),
                    "X-Amz-Date": "20160818T143303Z",
                    "X-Amz-Expires": "3600",
                    "X-Amz-Signature": (
                        "1297528058f2c8b89cfa52c6a47d6c54890700a1da24702b06d53e774c0acc95"
                    ),
                    "X-Amz-SignedHeaders": "host",
                },
            },
        )

    def test_handles_non_dns_compatible_buckets(self) -> None:
        stdout = _get_presigned_url_for_cmd(["presign", "s3://bucket.dots/key"])
        _assert_presigned_url_matches(
            stdout,
            {
                "hostname": "s3.us-east-1.amazonaws.com",
                "path": "/bucket.dots/key",
                "query_params": {
                    "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                    "X-Amz-Credential": ("access_key%2F20160818%2Fus-east-1%2Fs3%2Faws4_request"),
                    "X-Amz-Date": "20160818T143303Z",
                    "X-Amz-Expires": "3600",
                    "X-Amz-Signature": (
                        "5a032639cabfe3db0b4b87ba3b12c29f5e42fe74cbba8a0eb69bfb30c6e2d277"
                    ),
                    "X-Amz-SignedHeaders": "host",
                },
            },
        )

    def test_handles_expires_in(self) -> None:
        expires_in = 1000
        stdout = _get_presigned_url_for_cmd(
            ["presign", "s3://bucket/key", "--expires-in", str(expires_in)]
        )
        _assert_presigned_url_matches(
            stdout,
            {
                "hostname": "bucket.s3.us-east-1.amazonaws.com",
                "path": "/key",
                "query_params": {
                    "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                    "X-Amz-Credential": ("access_key%2F20160818%2Fus-east-1%2Fs3%2Faws4_request"),
                    "X-Amz-Date": "20160818T143303Z",
                    "X-Amz-Expires": f"{expires_in}",
                    "X-Amz-Signature": (
                        "865fb61b021c3bf406c40d41353f584835fff1f158cf1b3e6ec06260ecbb8937"
                    ),
                    "X-Amz-SignedHeaders": "host",
                },
            },
        )

    def test_handles_sigv4(self) -> None:
        # aws-cli enables sigv4 via a config file; aws v2 (and build_client's
        # pinned base) already signs v4, so the default config is the
        # equivalent - the expectation is identical to test_generates_a_url.
        stdout = _get_presigned_url_for_cmd(
            ["presign", "s3://bucket/key"], config=Config(signature_version="s3v4", s3=_BASE_S3)
        )
        expected = {
            "hostname": "bucket.s3.us-east-1.amazonaws.com",
            "path": "/key",
            "query_params": {
                "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                "X-Amz-Credential": ("access_key%2F20160818%2Fus-east-1%2Fs3%2Faws4_request"),
                "X-Amz-Date": "20160818T143303Z",
                "X-Amz-Expires": "3600",
                "X-Amz-Signature": (
                    "1297528058f2c8b89cfa52c6a47d6c54890700a1da24702b06d53e774c0acc95"
                ),
                "X-Amz-SignedHeaders": "host",
            },
        }
        _assert_presigned_url_matches(stdout, expected)

    def test_s3_prefix_not_needed(self) -> None:
        # Consistent with the 'ls' command.
        stdout = _get_presigned_url_for_cmd(["presign", "bucket/key"])
        _assert_presigned_url_matches(
            stdout,
            {
                "hostname": "bucket.s3.us-east-1.amazonaws.com",
                "path": "/key",
                "query_params": {
                    "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                    "X-Amz-Credential": ("access_key%2F20160818%2Fus-east-1%2Fs3%2Faws4_request"),
                    "X-Amz-Date": "20160818T143303Z",
                    "X-Amz-Expires": "3600",
                    "X-Amz-Signature": (
                        "1297528058f2c8b89cfa52c6a47d6c54890700a1da24702b06d53e774c0acc95"
                    ),
                    "X-Amz-SignedHeaders": "host",
                },
            },
        )

    def test_can_support_addressing_mode_config(self) -> None:
        # aws-cli enables path-style addressing via a config file; the client
        # Config equivalent keeps the rest of the pinned base.
        stdout = _get_presigned_url_for_cmd(
            ["presign", "s3://bucket/key"], config=_PATH_STYLE_CONFIG
        )
        _assert_presigned_url_matches(
            stdout,
            {
                "hostname": "s3.us-east-1.amazonaws.com",
                "path": "/bucket/key",
                "query_params": {
                    "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                    "X-Amz-Credential": ("access_key%2F20160818%2Fus-east-1%2Fs3%2Faws4_request"),
                    "X-Amz-Date": "20160818T143303Z",
                    "X-Amz-Expires": "3600",
                    "X-Amz-Signature": (
                        "c6dab3560db76aded03e6268338ddb0a6dec00ebc82d6e7abdc305529fcaba74"
                    ),
                    "X-Amz-SignedHeaders": "host",
                },
            },
        )
