"""Usage and error strings shared across subcommands.

Each wording has one home here, and the commands interpolate only their own
name or value.
"""

from __future__ import annotations

_TWO_PATH_FORMS = "<LocalPath> <S3Uri> or <S3Uri> <LocalPath> or <S3Uri> <S3Uri>"


def single_uri_usage(command: str) -> str:
    """The usage error for a single-``<S3Uri>`` command (rm, mb, rb) - rc 252."""
    return f"usage: boto3-s3 {command} <S3Uri>\nError: Invalid argument type"


def two_path_usage(command: str) -> str:
    """The usage error for a two-path transfer command (cp / mv / sync) - rc 252."""
    return f"usage: boto3-s3 {command} {_TWO_PATH_FORMS}\nError: Invalid argument type"


def invalid_bucket_name_message(name: str = "") -> str:
    """The report for a bucket name the commands refuse before any request.

    The commands refuse a few URIs before any request is built (``mb s3://`` /
    ``mb s3:///key`` / ``rb s3://`` / ``rb s3:///key`` / ``rm s3://``, and a
    ``website`` name the bucket/key split would disturb), where aws lets
    botocore refuse the name.
    The rc is the caller's: ``mb`` / ``rb`` / ``rm`` 1, ``website`` 252.
    """
    return f'Invalid bucket name "{name}"'
