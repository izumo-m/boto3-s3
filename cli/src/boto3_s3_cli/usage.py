"""aws-parity usage / error strings shared across subcommands.

These strings are part of the stderr contract (the parity tests compare them
token-for-token against aws), so each wording has exactly one home here and
the commands interpolate only their own name or value.
"""

from __future__ import annotations

_TWO_PATH_FORMS = "<LocalPath> <S3Uri> or <S3Uri> <LocalPath> or <S3Uri> <S3Uri>"


def single_uri_usage(command: str) -> str:
    """aws's usage error for ``rm``'s single ``<S3Uri>`` - rc 252.

    Only the ``CommandParameters`` path (rm, and the transfer family via
    :func:`two_path_usage`) prepends the ``usage:`` line; ``mb`` / ``rb``
    raise the bare form (:func:`bare_single_uri_usage`) - measured against
    aws 2.35.5.
    """
    return f"usage: boto3-s3 {command} <S3Uri>\nError: Invalid argument type"


def bare_single_uri_usage() -> str:
    """aws's ``mb`` / ``rb`` path rejection: no ``usage:`` prefix - rc 252.

    aws-cli's ``MbCommand`` / ``RbCommand`` raise
    ``ParamValidationError("<S3Uri>\\nError: Invalid argument type")`` directly,
    without the ``CommandParameters`` usage line rm gets.
    """
    return "<S3Uri>\nError: Invalid argument type"


def two_path_usage(command: str) -> str:
    """aws's usage error for a two-path transfer command (cp / mv / sync) - rc 252."""
    return f"usage: boto3-s3 {command} {_TWO_PATH_FORMS}\nError: Invalid argument type"


def invalid_bucket_name_message(name: str = "") -> str:
    """A simplified form of botocore's client-side bad-bucket-name rejection.

    botocore raises a ``ParamValidationError`` whose str form is ``"Parameter
    validation failed:"`` + newline + a report, and aws prints that report in
    full (it continues ``: Bucket name must match the regex ...``). We reproduce
    only the leading ``Invalid bucket name "<name>"`` line: the charter pins the
    rc (mb / rb 1, website 252), this stderr text is non-contractual, and the
    version-fragile regex tail is deliberately omitted.
    """
    return f'Parameter validation failed:\nInvalid bucket name "{name}"'
