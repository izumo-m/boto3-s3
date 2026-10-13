"""aws-parity usage / error strings shared across subcommands.

These strings are aws's own bytes: the output parity charter binds them, and
the parity tests compare them against what the pinned aws writes. Each
wording has exactly one home here and the commands interpolate only their own
name or value.
"""

from __future__ import annotations

import json
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


# json's own whitespace set (json.decoder.WHITESPACE_STR).
_JSON_WHITESPACE = " \t\n\r"


def json_error_message(exc: json.JSONDecodeError) -> str:
    """Render a decode failure the way the official aws build's json does.

    Python 3.13 gave the two trailing-comma shapes a message of their own,
    anchored on the comma instead of on the closing bracket. aws ships 3.14,
    so the newer text is the parity target on every interpreter this CLI runs
    on; the older ones are re-anchored here. Those two shapes are the whole
    difference (measured across 3.10 / 3.12 / 3.14 over a broken-JSON corpus).
    """
    if exc.msg == "Expecting property name enclosed in double quotes":
        closer, message = "}", "Illegal trailing comma before end of object"
    elif exc.msg == "Expecting value":
        closer, message = "]", "Illegal trailing comma before end of array"
    else:
        return str(exc)
    doc = exc.doc
    if exc.pos >= len(doc) or doc[exc.pos] != closer:
        return str(exc)
    comma = exc.pos - 1
    while comma >= 0 and doc[comma] in _JSON_WHITESPACE:
        comma -= 1
    if comma < 0 or doc[comma] != ",":
        return str(exc)
    lineno = doc.count("\n", 0, comma) + 1
    colno = comma - doc.rfind("\n", 0, comma)
    return f"{message}: line {lineno} column {colno} (char {comma})"


def exception_text(exc: BaseException) -> str:
    """An exception's text as aws would print it, in a report or a failure line.

    aws prints ``str(exc)``, so where the text is the interpreter's own - a
    ``json`` decode failure (a ``credential_process`` answer, say), an
    ``int()`` the SDK applies to a config value (``max_attempts``,
    ``metadata_service_timeout``) or to a response value - the wording is its
    bundled Python's. The two shapes an older host words differently are put
    in that wording (`json_error_message`, `int_error_message`); anything else
    is ``str(exc)``. An exception that carries its cause's text as its own -
    the library's error for a request that raised outside botocore's family,
    say - is rendered as that cause.
    """
    seen = {id(exc)}
    cause = exc.__cause__
    # A cause chain can loop (a plugin's ``raise e from e``): stop at the
    # first exception already visited.
    while cause is not None and id(cause) not in seen and str(exc) == str(cause):
        seen.add(id(cause))
        exc, cause = cause, cause.__cause__
    if isinstance(exc, json.JSONDecodeError):
        return json_error_message(exc)
    if isinstance(exc, ValueError):
        return int_error_message(exc)
    return str(exc)


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
