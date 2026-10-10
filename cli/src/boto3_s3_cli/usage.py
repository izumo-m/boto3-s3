"""aws-parity usage / error strings shared across subcommands.

These strings are aws's own bytes: the output parity charter binds them, and
the parity tests compare them against what the pinned aws writes. Each
wording has exactly one home here and the commands interpolate only their own
name or value.
"""

from __future__ import annotations

import re

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


# Python 3.10's int() names its digit limit without the unit 3.11 added.
_UNITLESS_DIGIT_LIMIT = re.compile(r"^Exceeds the limit \((\d+)\) for ")


def int_error_message(exc: ValueError) -> str:
    """A bare ``int()`` failure's text, worded as aws's interpreter words it.

    aws reports the ``ValueError`` of its bare ``int()`` conversions as the
    exception's own text, so the wording is its bundled Python's - 3.14. Of
    the interpreters this command runs on, only 3.10 words one of them
    differently: the digit limit, ``Exceeds the limit (4300) for ...`` where
    3.11 onward says ``Exceeds the limit (4300 digits) for ...``.
    """
    return _UNITLESS_DIGIT_LIMIT.sub(r"Exceeds the limit (\1 digits) for ", str(exc))


def invalid_bucket_name_message(name: str = "") -> str:
    """botocore's client-side bad-bucket-name report, for the names refused here.

    The commands refuse a few URIs before any request is built (``mb s3://`` /
    ``mb s3:///key`` / ``rb s3://`` / ``rm s3://``, and a ``website`` name the
    bucket/key split would disturb). aws sends those names to botocore, whose
    ``validate_bucket_name`` raises a ``ParamValidationError`` - ``"Parameter
    validation failed:"``, a newline, and ``Invalid bucket name "<name>": Bucket
    name must match the regex ...`` - and prints its str form whole. The report
    is built by that same botocore function, so it reads exactly as it does for
    a name that reaches botocore's own check (the installed botocore's wording).
    The rc is the caller's: ``mb`` / ``rb`` / ``rm`` 1, ``website`` 252.
    """
    from botocore.exceptions import ParamValidationError
    from botocore.handlers import validate_bucket_name

    try:
        validate_bucket_name({"Bucket": name})
    except ParamValidationError as exc:
        return str(exc)
    # A guard only: every name the callers pass fails both of botocore's
    # patterns.
    return f'Parameter validation failed:\nInvalid bucket name "{name}"'
