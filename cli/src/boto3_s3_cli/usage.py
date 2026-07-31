"""aws-parity usage / error strings shared across subcommands.

These strings are aws's own bytes: the output parity charter binds them, and
the parity tests compare them against what the pinned aws writes.
``invalid_bucket_name_message`` is the one exception - a simplified form, and
a **recorded deviation** from that charter rather than free text (its
docstring). Each wording has exactly one home here and the commands
interpolate only their own name or value.
"""

from __future__ import annotations

_TWO_PATH_FORMS = "<LocalPath> <S3Uri> or <S3Uri> <LocalPath> or <S3Uri> <S3Uri>"


def single_uri_usage(command: str) -> str:
    """aws's usage error for ``rm``'s single ``<S3Uri>`` - rc 252.

    Only the ``CommandParameters`` path (rm, and the transfer family via
    ``two_path_usage``) prepends the ``usage:`` line; ``mb`` / ``rb``
    raise the bare form (``bare_single_uri_usage``) - measured against
    the pinned aws-cli.
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
    only the leading ``Invalid bucket name "<name>"`` line, dropping the
    botocore-version-fragile regex tail. That truncation is a **recorded
    deviation** from the output parity charter, not a free choice
    (design/aws-cli-option-handling.md section 6,
    docs/cli/aws-differences.md section 2); the rc is unaffected
    (mb / rb 1, website 252).
    """
    return f'Parameter validation failed:\nInvalid bucket name "{name}"'
