"""Give better S3 error messages, the way ``aws s3`` does.

aws-cli hangs one handler off the ``after-call.s3`` event
(its ``s3errormsg`` customization) that rewrites three S3 error messages in
place, before anything renders them: the two "this bucket needs Signature
Version 4" shapes and the cross-region ``PermanentRedirect``. The rewrite is
observable on every report that carries a service message - the top-level
``[ERROR]`` line and the per-item ``upload failed:`` / ``download failed:``
lines alike - so the handler has to ride the S3 clients the CLI builds
(`clientfactory.build_client`). The library stays boto3-faithful and never
rewrites a service message.

The wording is aws-cli's, verbatim, including the two commands it advises
(``aws s3api get-bucket-location`` and ``aws configure set``) which have no
boto3-s3 counterpart: parity is a byte-identical report, the same rule the
credential and region hints in `cli` follow.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

REGION_ERROR_MSG = (
    "You can fix this issue by explicitly providing the correct region "
    "location using the --region argument, the AWS_DEFAULT_REGION "
    "environment variable, or the region variable in the AWS CLI "
    "configuration file.  You can get the bucket's location by "
    'running "aws s3api get-bucket-location --bucket BUCKET".'
)

ENABLE_SIGV4_MSG = (
    " You can enable AWS Signature Version 4 by running the command: \n"
    "aws configure set s3.signature_version s3v4"
)


def register(client: S3Client) -> None:
    """Attach the rewriter to one S3 client.

    aws registers the handler on its session, so every S3 client it creates
    carries it; registering per client is the same reach here, because
    `clientfactory.build_client` is the CLI's only S3 client builder.
    """
    client.meta.events.register("after-call.s3", enhance_error_msg)


def enhance_error_msg(parsed: dict[str, Any] | None, **kwargs: Any) -> None:
    """Rewrite the parsed error in place, if it is one of the three shapes."""
    if parsed is None or "Error" not in parsed:
        return
    if _is_sigv4_error_message(parsed):
        parsed["Error"]["Message"] = (
            "You are attempting to operate on a bucket in a region that requires "
            "Signature Version 4.  " + REGION_ERROR_MSG
        )
    elif _is_permanent_redirect_message(parsed):
        message: str = parsed["Error"]["Message"]
        # aws drops the message's final character - the period ending "Please
        # send all future requests to this endpoint." - so the endpoint reads
        # as its continuation.
        endpoint = parsed["Error"]["Endpoint"]
        parsed["Error"]["Message"] = f"{message[:-1]}: {endpoint}\n{REGION_ERROR_MSG}"
    elif _is_kms_sigv4_error_message(parsed):
        parsed["Error"]["Message"] += ENABLE_SIGV4_MSG


def _is_sigv4_error_message(parsed: dict[str, Any]) -> bool:
    return "Please use AWS4-HMAC-SHA256" in parsed.get("Error", {}).get("Message", "")


def _is_permanent_redirect_message(parsed: dict[str, Any]) -> bool:
    return parsed.get("Error", {}).get("Code", "") == "PermanentRedirect"


def _is_kms_sigv4_error_message(parsed: dict[str, Any]) -> bool:
    return "AWS KMS managed keys require AWS Signature Version 4" in parsed.get("Error", {}).get(
        "Message", ""
    )
