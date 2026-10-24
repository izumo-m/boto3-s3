"""aws-cli's local paramfile loaders (``file://`` text, ``fileb://`` binary).

The execution half of aws-cli's ``paramfile.py`` (``get_paramfile`` over its
``LOCAL_PREFIX_MAP``) - aws-cli v2 itself carries only the local file forms
there (its http/https fetchers are gone), so this is the whole surface - and
aws-cli's ``load-cli-arg`` handler (``URIArgumentHandler``) over it. The handler
is `load_argument`, which keeps the loaded value whatever its type (the SSE-C
blobs, the integer options); a string-typed argument loads through
`load_text_argument`, which refuses ``fileb://`` (the free-string options,
``--metadata``'s whole value, the path positionals). Their callers are
``commands/transferargs.py`` and ``commands/base.py``; the shorthand
parser's ``@=`` operator (``shorthand.py``) calls `get_paramfile` itself. A
bad ``AWS_CLI_FILE_ENCODING`` raises ``LookupError`` into the general
handler (255), as in aws-cli.

A read failure is a `ParamfileLoadError` naming no argument; every caller
loads inside `named_argument`, which reports it as the argument's parse error
(rc 252) - the port of aws-cli's handler, which re-raises its
``ResourceLoadingError`` as the named argument's ``ParamError``. aws-cli's
shorthand parser alone calls ``get_paramfile`` outside that handler, so its
``@=`` load failure reaches the general handler bare (rc 255); here the
shorthand's ``@=`` (``shorthand.py``) loads inside the option's scope too, so
a file named on the command line that cannot be read is 252 wherever it is
named (docs/cli/exit-codes.md).
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING

from boto3_s3 import InvalidValueError, ValidationError
from boto3_s3_cli import textencoding, usage

if TYPE_CHECKING:
    from collections.abc import Generator


class ParamfileLoadError(InvalidValueError):
    """A paramfile reference that could not be loaded (aws-cli's ``ResourceLoadingError``).

    The message names no argument: `named_argument`, around every load,
    converts it into the rc-252 ``Error parsing parameter '<name>'`` form.
    """


@contextlib.contextmanager
def named_argument(name: str, *, operation: str) -> Generator[None, None, None]:
    """Report a paramfile failure inside as *name*'s parse error (`aws` rc 252).

    The port of aws-cli's ``URIArgumentHandler`` boundary: it wraps the load of
    every argument that has a CLI name to report - the options, the
    positionals, the whole-value form of a map option, and the values loaded
    from *inside* a map option's parsed value (the shorthand ``@=`` operator,
    reported as that option's error: `shorthand.parse_map_option` parses
    within this scope), where aws-cli's shorthand parser loads outside its
    handler and reaches its general one (255).
    """
    try:
        yield
    except ParamfileLoadError as exc:
        raise ValidationError(usage.param_error(name, exc), operation=operation) from exc


def _text_encoding() -> str:
    """The ``file://`` text encoding - aws-cli's ``compat.getpreferredencoding``.

    ``AWS_CLI_FILE_ENCODING`` wins when present (even empty - the unknown
    codec fails in ``open``, present-wins like aws-cli). Without it, the CLI's
    text encoding (`textencoding.text_encoding`): a ``C`` / ``POSIX``
    ``LC_CTYPE`` reads as UTF-8, as aws-cli implements PEP 540's locale coercion
    itself, and otherwise the locale's own encoding whatever Python's UTF-8
    mode says - so a ``file://`` reads the same under Python 3.15, or
    ``PYTHONUTF8=1``, as under ``aws``.
    """
    encoding = os.environ.get("AWS_CLI_FILE_ENCODING")
    if encoding is not None:
        return encoding
    return textencoding.text_encoding()


def _local_path(reference: str) -> tuple[str, str]:
    """A paramfile reference's path, expanded as aws-cli's ``get_file`` expands it.

    ``expandvars(expanduser(...))`` (expanduser inner), on the path as the OS
    calls take it (`textencoding.os_path` - the command line is the CLI's
    text), so the variables and the home directory expand to the bytes the
    OS gave them. Returns ``(os_form, text)``: the first to open, the second
    to report.
    """
    native = os.path.expandvars(os.path.expanduser(textencoding.os_path(reference)))
    return native, textencoding.text_path(native)


def read_text_paramfile(original: str, *, operation: str) -> str:
    """Load a ``file://`` reference as text (aws-cli paramfile ``mode='r'``).

    Path expansion matches aws-cli's ``get_file``: ``expandvars(expanduser(...))``
    (expanduser inner). The encoding is `_text_encoding` (aws-cli's
    ``compat_open`` / ``getpreferredencoding``).
    """
    native, path = _local_path(original[len("file://") :])
    encoding = _text_encoding()
    try:
        with open(native, encoding=encoding) as handle:
            return handle.read()
    except UnicodeDecodeError as exc:
        # The decode-error message names the EXPANDED path in parentheses; the
        # OSError one names the full original.
        raise ParamfileLoadError(
            f"Unable to load paramfile ({path}), "
            "text contents could not be decoded.  If this is a binary file, please use "
            "the fileb:// prefix instead of the file:// prefix.",
            operation=operation,
        ) from exc
    except OSError as exc:
        raise ParamfileLoadError(
            f"Unable to load paramfile {original}: {textencoding.name_files_as_text(exc)}",
            operation=operation,
        ) from exc


def read_binary_paramfile(original: str, *, operation: str) -> bytes:
    """Load a ``fileb://`` reference as raw bytes (aws-cli paramfile ``mode='rb'``).

    Path expansion matches aws-cli's ``get_file``: ``expandvars(expanduser(...))``.
    """
    native, _path = _local_path(original[len("fileb://") :])
    try:
        with open(native, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise ParamfileLoadError(
            f"Unable to load paramfile {original}: {textencoding.name_files_as_text(exc)}",
            operation=operation,
        ) from exc


def get_paramfile(value: str, *, operation: str) -> str | bytes | None:
    """aws-cli's ``get_paramfile``: load a prefixed reference, or ``None`` verbatim.

    ``file://`` loads text, ``fileb://`` raw bytes; a value with neither
    prefix returns ``None`` so the caller keeps it as-is (the
    "file-optional-values" of the shorthand ``@=`` grammar).
    """
    if value.startswith("file://"):
        return read_text_paramfile(value, operation=operation)
    if value.startswith("fileb://"):
        return read_binary_paramfile(value, operation=operation)
    return None


def load_argument(value: str, name: str, *, operation: str) -> str | bytes:
    """aws-cli's ``URIArgumentHandler`` on one argument: load a reference, else keep it.

    aws-cli loads a ``file://`` / ``fileb://`` reference on every ``aws s3``
    argument at parse time - text or raw bytes, which the argument's own type
    then decides about - so a bad reference is the load 252 (`named_argument`,
    reported under *name*). A value without a prefix passes through verbatim.
    """
    with named_argument(name, operation=operation):
        loaded = get_paramfile(value, operation=operation)
    return value if loaded is None else loaded


def load_text_argument(value: str, name: str, *, operation: str) -> str:
    """`load_argument` for a string-typed argument: a ``fileb://`` is refused (252).

    A ``file://`` reference loads as text and a value without a prefix passes
    through verbatim. A ``fileb://`` reference is refused before the file is
    read: no bytes can be a string value. aws-cli loads them and then refuses them
    (an option, through botocore's type check) or mishandles them (a path
    positional, a whole ``--metadata`` value; docs/cli/aws-differences.md
    section 2). *name* is the argument name the report carries.
    """
    if value.startswith("fileb://"):
        raise ValidationError(
            usage.param_error(
                name,
                "a fileb:// reference is not supported, as the value cannot be bytes; "
                "use file:// to read it from a file",
            ),
            operation=operation,
        )
    if value.startswith("file://"):
        with named_argument(name, operation=operation):
            return read_text_paramfile(value, operation=operation)
    return value


__all__ = [
    "ParamfileLoadError",
    "get_paramfile",
    "load_argument",
    "load_text_argument",
    "named_argument",
    "read_binary_paramfile",
    "read_text_paramfile",
]
