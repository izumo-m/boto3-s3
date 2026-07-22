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
presign ``--expires-in``). Every file *read* failure is a
``ValidationError`` with aws's wording (rc 252); a bad
``AWS_CLI_FILE_ENCODING`` instead raises ``LookupError`` into the general
handler (255), as in aws.
"""

from __future__ import annotations

import locale
import os

from boto3_s3 import ValidationError


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


def read_text_paramfile(original: str, *, name: str, operation: str) -> str:
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
        # aws wording (paramfile.get_file): the decode-error message names the
        # EXPANDED path in parentheses; the OSError one names the full original.
        raise ValidationError(
            f"Error parsing parameter '{name}': Unable to load paramfile ({path}), "
            "text contents could not be decoded.  If this is a binary file, please use "
            "the fileb:// prefix instead of the file:// prefix.",
            operation=operation,
        ) from exc
    except OSError as exc:
        raise ValidationError(
            f"Error parsing parameter '{name}': Unable to load paramfile {original}: {exc}",
            operation=operation,
        ) from exc


def read_binary_paramfile(original: str, *, name: str, operation: str) -> bytes:
    """Load a ``fileb://`` reference as raw bytes (aws paramfile ``mode='rb'``).

    Path expansion matches aws's ``get_file``: ``expandvars(expanduser(...))``.
    """
    path = os.path.expandvars(os.path.expanduser(original[len("fileb://") :]))
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise ValidationError(
            f"Error parsing parameter '{name}': Unable to load paramfile {original}: {exc}",
            operation=operation,
        ) from exc


def get_paramfile(value: str, *, name: str, operation: str) -> str | bytes | None:
    """aws's ``get_paramfile``: load a prefixed reference, or ``None`` verbatim.

    ``file://`` loads text, ``fileb://`` raw bytes; a value with neither
    prefix returns ``None`` so the caller keeps it as-is (the
    "file-optional-values" of the shorthand ``@=`` grammar).
    """
    if value.startswith("file://"):
        return read_text_paramfile(value, name=name, operation=operation)
    if value.startswith("fileb://"):
        return read_binary_paramfile(value, name=name, operation=operation)
    return None


__all__ = ["get_paramfile", "read_binary_paramfile", "read_text_paramfile"]
