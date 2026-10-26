"""Usage and error strings shared across subcommands.

The usage errors and the frame of a parameter's parse error are aws-cli's
own text, the program name aside; the bucket-name report is this CLI's. Each
wording has one home here, and the commands interpolate only their own name,
value or detail.
"""

from __future__ import annotations

_TWO_PATH_FORMS = "<LocalPath> <S3Uri> or <S3Uri> <LocalPath> or <S3Uri> <S3Uri>"


def single_uri_usage(command: str) -> str:
    """aws-cli's usage error for ``rm``'s single ``<S3Uri>`` - rc 252.

    Only aws-cli's ``CommandParameters`` path (rm, and the transfer family via
    ``two_path_usage``) leads with the ``usage:`` line; ``mb`` / ``rb`` raise
    the bare form (``bare_single_uri_usage``).
    """
    return f"usage: boto3-s3 {command} <S3Uri>\nError: Invalid argument type"


def bare_single_uri_usage() -> str:
    """aws-cli's ``mb`` / ``rb`` path rejection: no ``usage:`` line - rc 252.

    aws-cli's ``MbCommand`` / ``RbCommand`` raise
    ``ParamValidationError("<S3Uri>\\nError: Invalid argument type")``
    directly, without the ``CommandParameters`` usage line rm gets.
    """
    return "<S3Uri>\nError: Invalid argument type"


def two_path_usage(command: str) -> str:
    """aws-cli's usage error for a two-path transfer command (cp / mv / sync) - rc 252."""
    return f"usage: boto3-s3 {command} {_TWO_PATH_FORMS}\nError: Invalid argument type"


def param_error(name: str, message: object) -> str:
    """aws-cli's ``ParamError`` text: an argument that failed to parse (252).

    ``Error parsing parameter '<name>': <message>`` (aws-cli's
    ``argprocess.ParamError``). *name* is the option's ``--flag`` form or the
    positional's name; *message* is the caller's own detail.
    """
    return f"Error parsing parameter '{name}': {message}"


def invalid_bucket_name_message() -> str:
    """The report for a path that names no bucket, refused before any request (252).

    ``s3://`` or ``s3:///k`` on the commands that refuse it where aws-cli
    carries it into the run (``base.refuse_bucketless``; the deviation is in
    docs/cli/aws-differences.md). ``presign`` and ``website``, where aws-cli
    lets botocore refuse the name, report botocore's text instead
    (``base.refuse_invalid_bucket_name``, and the library's ``S3.website``).
    """
    return 'Invalid bucket name ""'
