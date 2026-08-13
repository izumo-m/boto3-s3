"""Unit tests for boto3_s3_cli.s3errormsg (aws's `after-call.s3` rewriter).

The three rewrites and their wording are aws-cli's, ported verbatim; each case
below is a canned parsed response of the shape the real service returns.
"""

from __future__ import annotations

from typing import Any

from boto3_s3_cli import s3errormsg

_REDIRECT_MESSAGE = (
    "The bucket you are attempting to access must be addressed using the "
    "specified endpoint. Please send all future requests to this endpoint."
)


def _enhance(error: dict[str, Any]) -> str:
    parsed: dict[str, Any] = {"Error": error, "ResponseMetadata": {}}
    s3errormsg.enhance_error_msg(parsed=parsed)
    return parsed["Error"]["Message"]


def test_sigv4_required_message_is_replaced_wholesale() -> None:
    message = _enhance(
        {
            "Code": "InvalidRequest",
            "Message": (
                "The authorization mechanism you have provided is not supported. "
                "Please use AWS4-HMAC-SHA256."
            ),
        }
    )
    assert message == (
        "You are attempting to operate on a bucket in a region that requires "
        "Signature Version 4.  " + s3errormsg.REGION_ERROR_MSG
    )


def test_permanent_redirect_continues_the_message_with_the_endpoint() -> None:
    message = _enhance(
        {
            "Code": "PermanentRedirect",
            "Message": _REDIRECT_MESSAGE,
            "Endpoint": "bkt.s3.eu-west-1.amazonaws.com",
        }
    )
    # aws drops the message's trailing period so the endpoint reads as its
    # continuation, then puts the advice on its own line.
    assert message == (
        _REDIRECT_MESSAGE[:-1] + ": bkt.s3.eu-west-1.amazonaws.com\n" + s3errormsg.REGION_ERROR_MSG
    )


def test_kms_sigv4_message_keeps_its_text_and_gains_the_advice() -> None:
    original = (
        "Requests specifying Server Side Encryption with AWS KMS managed keys "
        "require AWS Signature Version 4."
    )
    assert _enhance({"Code": "InvalidArgument", "Message": original}) == (
        original + s3errormsg.ENABLE_SIGV4_MSG
    )


def test_the_branches_are_ordered_like_aws() -> None:
    # A PermanentRedirect whose message also mentions the signature version
    # takes the sigv4 branch: aws tests that one first.
    message = _enhance(
        {
            "Code": "PermanentRedirect",
            "Message": "Please use AWS4-HMAC-SHA256.",
            "Endpoint": "bkt.s3.eu-west-1.amazonaws.com",
        }
    )
    assert message.startswith("You are attempting to operate on a bucket in a region")


def test_an_unrelated_error_is_left_alone() -> None:
    assert _enhance({"Code": "NoSuchKey", "Message": "The specified key does not exist."}) == (
        "The specified key does not exist."
    )


def test_a_successful_response_is_left_alone() -> None:
    parsed: dict[str, Any] = {"Contents": [], "KeyCount": 0}
    s3errormsg.enhance_error_msg(parsed=parsed)
    assert parsed == {"Contents": [], "KeyCount": 0}


def test_a_missing_parse_is_tolerated() -> None:
    # botocore emits after-call with parsed=None when nothing was parsed.
    s3errormsg.enhance_error_msg(parsed=None)


def test_an_error_without_a_message_is_left_alone() -> None:
    parsed: dict[str, Any] = {"Error": {"Code": "404"}}
    s3errormsg.enhance_error_msg(parsed=parsed)
    assert parsed == {"Error": {"Code": "404"}}
