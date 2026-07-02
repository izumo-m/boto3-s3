"""aws-parity usage / error strings shared across subcommands.

These strings are part of the stderr contract (the parity tests compare them
token-for-token against aws), so each wording has exactly one home here and
the commands interpolate only their own name or value.
"""

from __future__ import annotations

_TWO_PATH_FORMS = "<LocalPath> <S3Uri> or <S3Uri> <LocalPath> or <S3Uri> <S3Uri>"


def single_uri_usage(command: str) -> str:
    """aws's usage error for a single-``<S3Uri>`` command (rm / mb / rb) - rc 252."""
    return f"usage: boto3-s3 {command} <S3Uri>\nError: Invalid argument type"


def two_path_usage(command: str) -> str:
    """aws's usage error for a two-path transfer command (cp / mv / sync) - rc 252."""
    return f"usage: boto3-s3 {command} {_TWO_PATH_FORMS}\nError: Invalid argument type"


def invalid_bucket_name_message(name: str = "") -> str:
    """botocore's client-side rejection of an empty / malformed bucket name.

    The str form of botocore's ``ParamValidationError``: ``"Parameter
    validation failed:"`` + newline + report - the message aws prints when it
    sends the bad name through to the API layer.
    """
    return f'Parameter validation failed:\nInvalid bucket name "{name}"'
