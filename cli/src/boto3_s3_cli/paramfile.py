"""aws-cli's local paramfile loaders (``file://`` text, ``fileb://`` binary).

The execution half of aws's ``paramfile.py`` (``get_paramfile`` over its
``LOCAL_PREFIX_MAP``), scoped to the local prefixes ``aws s3`` consumes -
the http/https fetchers are not ported. Two callers share it: the
transfer-argument resolution (``commands/transferargs.py``: the free-string
options, ``--metadata``'s pre-parse, the SSE-C blobs) and the shorthand
parser's ``@=`` operator (``shorthand.py``). Every load failure is a
:class:`~boto3_s3.exceptions.ValidationError` with aws's wording (rc 252).
"""

from __future__ import annotations

import os

from boto3_s3 import ValidationError


def read_text_paramfile(original: str, *, name: str, operation: str) -> str:
    """Load a ``file://`` reference as text (aws paramfile ``mode='r'``).

    Path expansion matches aws's ``get_file``: ``expandvars(expanduser(...))``
    (expanduser inner). The encoding honors ``AWS_CLI_FILE_ENCODING`` (aws's
    ``compat_open`` / ``getpreferredencoding``), falling back to the locale
    default (``open``'s default when ``encoding`` is ``None``).
    """
    path = os.path.expandvars(os.path.expanduser(original[len("file://") :]))
    encoding = os.environ.get("AWS_CLI_FILE_ENCODING")
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
