"""aws-cli's local paramfile loaders (``file://`` text, ``fileb://`` binary).

The execution half of aws's ``paramfile.py`` (``get_paramfile`` over its
``LOCAL_PREFIX_MAP``) - aws v2 itself carries only the local file forms
there (its http/https fetchers are gone), so this is the whole surface.
Three callers share it: the
transfer-argument resolution (``commands/transferargs.py``: the free-string
options, ``--metadata``'s pre-parse, the SSE-C blobs), the shorthand
parser's ``@=`` operator (``shorthand.py``), and the plain-option expansions
(``commands/base.py``'s ``expand_option_paramfile`` and its integer wrapper
``expand_integer_paramfile``: ls / rm ``--page-size``,
presign ``--expires-in``). A bad ``AWS_CLI_FILE_ENCODING`` raises
``LookupError`` into the general handler (255), as in aws.

A read failure is a `ParamfileLoadError` naming no argument; every caller
loads inside `named_argument`, which reports it as the argument's parse error
(rc 252) - the port of aws's ``load-cli-arg`` handler (``URIArgumentHandler``),
which re-raises its ``ResourceLoadingError`` as the named argument's
``ParamError``. aws's shorthand parser alone calls ``get_paramfile`` outside
that handler, so its ``@=`` load failure reaches the general handler bare
(rc 255); here the shorthand's ``@=`` (``shorthand.py``) loads inside the
option's scope too, so a file named on the command line that cannot be read
is 252 wherever it is named (docs/cli/exit-codes.md).
"""

from __future__ import annotations

import contextlib
import locale
import os
from typing import TYPE_CHECKING

from boto3_s3 import InvalidValueError, ValidationError

if TYPE_CHECKING:
    from collections.abc import Generator


class ParamfileLoadError(InvalidValueError):
    """A paramfile reference that could not be loaded (aws's ``ResourceLoadingError``).

    The message names no argument: `named_argument`, around every load,
    converts it into the rc-252 ``Error parsing parameter '<name>'`` form.
    """


@contextlib.contextmanager
def named_argument(name: str, *, operation: str) -> Generator[None, None, None]:
    """Report a paramfile failure inside as *name*'s parse error (aws rc 252).

    The port of aws's ``URIArgumentHandler`` boundary: it wraps the load of
    every argument that has a CLI name to report - the options, the
    positionals, the whole-value form of a map option, and the values loaded
    from *inside* a map option's parsed value (the shorthand ``@=`` operator,
    reported as that option's error: `shorthand.parse_map_option` parses
    within this scope), where aws's shorthand parser loads outside its
    handler and reaches its general one (255).
    """
    try:
        yield
    except ParamfileLoadError as exc:
        raise ValidationError(
            f"Error parsing parameter '{name}': {exc}", operation=operation
        ) from exc


def _text_encoding() -> str:
    """The ``file://`` text encoding - aws's ``compat.getpreferredencoding``.

    ``AWS_CLI_FILE_ENCODING`` wins when present (even empty - the unknown
    codec fails in ``open``, present-wins like aws). Without it, a ``C`` /
    ``POSIX`` ``LC_CTYPE`` reads as UTF-8: aws implements PEP 540's
    locale coercion itself (its frozen build lacks the interpreter's), so a
    ``LC_ALL=C`` run decodes UTF-8 content where the plain locale default
    would be ASCII - matched here for the case where the interpreter's own
    coercion is disabled (``PYTHONCOERCECLOCALE=0``). Otherwise the locale
    default (``locale.getpreferredencoding``, like aws).
    """
    encoding = os.environ.get("AWS_CLI_FILE_ENCODING")
    if encoding is not None:
        return encoding
    if locale.setlocale(locale.LC_CTYPE) in ("C", "POSIX"):
        return "UTF-8"
    return locale.getpreferredencoding()


def read_text_paramfile(original: str, *, operation: str) -> str:
    """Load a ``file://`` reference as text (aws paramfile ``mode='r'``).

    Path expansion matches aws's ``get_file``: ``expandvars(expanduser(...))``
    (expanduser inner). The encoding is `_text_encoding` (aws's
    ``compat_open`` / ``getpreferredencoding``).
    """
    path = os.path.expandvars(os.path.expanduser(original[len("file://") :]))
    encoding = _text_encoding()
    try:
        with open(path, encoding=encoding) as handle:
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
            f"Unable to load paramfile {original}: {exc}", operation=operation
        ) from exc


def read_binary_paramfile(original: str, *, operation: str) -> bytes:
    """Load a ``fileb://`` reference as raw bytes (aws paramfile ``mode='rb'``).

    Path expansion matches aws's ``get_file``: ``expandvars(expanduser(...))``.
    """
    path = os.path.expandvars(os.path.expanduser(original[len("fileb://") :]))
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise ParamfileLoadError(
            f"Unable to load paramfile {original}: {exc}", operation=operation
        ) from exc


def get_paramfile(value: str, *, operation: str) -> str | bytes | None:
    """aws's ``get_paramfile``: load a prefixed reference, or ``None`` verbatim.

    ``file://`` loads text, ``fileb://`` raw bytes; a value with neither
    prefix returns ``None`` so the caller keeps it as-is (the
    "file-optional-values" of the shorthand ``@=`` grammar).
    """
    if value.startswith("file://"):
        return read_text_paramfile(value, operation=operation)
    if value.startswith("fileb://"):
        return read_binary_paramfile(value, operation=operation)
    return None


__all__ = [
    "ParamfileLoadError",
    "get_paramfile",
    "named_argument",
    "read_binary_paramfile",
    "read_text_paramfile",
]
