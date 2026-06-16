"""``--debug`` wires the library's masked stream logger (docs/masking.md).

These tests pin the wiring end-to-end: the flag attaches a handler carrying a
``SecretMaskingFilter`` to each debug logger, and botocore-shaped DEBUG records
(the real leak: ``endpoint`` logs the signed ``AWSPreparedRequest`` repr,
``auth`` logs ``Signature:\\n<hex>``) reach stderr with every credential masked.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from boto3_s3.masking import SecretMaskingFilter
from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context

ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"  # last 4 == "MPLE"
SIGNATURE = "0123456789abcdef" * 4
SESSION_TOKEN = "FQoGZXIvYXdzEMPLELONGSESSIONTOKENvalue1234567890abcdef+/=="

_DEBUG_LOGGERS = ("boto3_s3", "botocore", "boto3", "s3transfer")


@pytest.fixture
def restore_debug_loggers() -> Iterator[None]:
    """Snapshot the debug loggers' handlers/levels and restore them afterwards."""
    snapshot = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).level)
        for name in _DEBUG_LOGGERS
    }
    try:
        yield
    finally:
        for name, (handlers, level) in snapshot.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.setLevel(level)


def _has_masking_handler(name: str) -> bool:
    return any(
        any(isinstance(f, SecretMaskingFilter) for f in handler.filters)
        for handler in logging.getLogger(name).handlers
    )


class _FakeS3Client:
    """Minimal client so ``ls`` completes without network (one empty page)."""

    def get_paginator(self, name: str) -> Any:
        class _P:
            def paginate(self, **_kw: Any) -> Any:
                return iter([{}])

        return _P()


def _fake_ctx() -> Context:
    return Context(client_factory=lambda _args: _FakeS3Client())


class TestDebugWiring:
    def test_flag_attaches_masking_handler_to_each_logger(
        self, restore_debug_loggers: None
    ) -> None:
        cli.main(["--debug", "ls", "s3://bucket/"], ctx=_fake_ctx())
        for name in _DEBUG_LOGGERS:
            assert _has_masking_handler(name), f"{name} has no masking handler"
            assert logging.getLogger(name).level == logging.DEBUG

    def test_no_flag_attaches_no_masking_handler(self, restore_debug_loggers: None) -> None:
        cli.main(["ls", "s3://bucket/"], ctx=_fake_ctx())
        for name in _DEBUG_LOGGERS:
            assert not _has_masking_handler(name)


class TestDebugOutputMasked:
    def test_botocore_records_reach_stderr_masked(
        self, restore_debug_loggers: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cli._enable_debug_logging()

        # The two real credential-bearing DEBUG lines botocore emits.
        prepared = (
            "<AWSPreparedRequest stream_output=False, method=PUT, url=https://b.s3/k, "
            "headers={'User-Agent': 'aws-cli', "
            f"'Authorization': 'AWS4-HMAC-SHA256 Credential={ACCESS_KEY_ID}"
            "/20260613/us-east-1/s3/aws4_request, SignedHeaders=host, "
            f"Signature={SIGNATURE}', 'X-Amz-Security-Token': '{SESSION_TOKEN}'}}>"
        )
        logging.getLogger("botocore.endpoint").debug("Sending http request: %s", prepared)
        logging.getLogger("botocore.auth").debug("Signature:\n%s", SIGNATURE)

        err = capsys.readouterr().err
        for secret in (ACCESS_KEY_ID, SIGNATURE, SESSION_TOKEN):
            assert secret not in err, f"raw {secret!r} leaked into --debug stderr"
        assert "Credential=***MPLE/20260613/us-east-1/s3/aws4_request" in err
        assert "Signature=***" in err
        assert "'X-Amz-Security-Token': '***'" in err

    def test_mask_secrets_can_be_disabled_at_the_library(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The library entry point still allows opting out (mask_secrets=False);
        # the CLI itself always passes True. Uses an isolated logger name.
        from boto3_s3 import set_stream_logger

        logger = logging.getLogger("test.boto3_s3.cli_optout")
        try:
            set_stream_logger("test.boto3_s3.cli_optout", mask_secrets=False)
            logger.debug("X-Amz-Signature=%s", SIGNATURE)
            assert f"X-Amz-Signature={SIGNATURE}" in capsys.readouterr().err
        finally:
            logger.handlers = []
            logger.setLevel(logging.NOTSET)
