"""The CLI's text encoding: the one aws-cli uses in the same environment.

design/overview.md section 3 ("Text encodings"): which encoding turns bytes
into text depends on the Python version and its environment - Python 3.15
turns UTF-8 mode on by default, ``PYTHONUTF8=1`` does on any version - while
``aws`` decides it the same way whatever Python runs this CLI (its frozen
interpreter ignores ``PYTHONUTF8``). So the CLI decides it as ``aws`` does
(`text_encoding`) and uses that wherever it converts: local file names
(`filename_encoding`, handed to the library's ``filename_encoding``), the
command line (`decode_argv`), standard output and standard error
(`configure_std_streams`, and `prepare_error_report` for the ``[ERROR]``
report), and ``file://`` text (``paramfile``). The config and credentials files
stay botocore's (docs/cli/aws-differences.md). Measured against the pinned
``aws`` on Linux (``C.UTF-8``, ``C``, ``POSIX``, ``ja_JP.EUC-JP``) and Windows
(cp932) under Python 3.10, 3.14 and 3.15.

Imported at startup, before the command line is parsed, so it stays light:
the standard library only, the library's ``LocalStorage`` loaded on first use
(design/imports.md).
"""

from __future__ import annotations

import contextlib
import functools
import locale
import os
import sys
from typing import TYPE_CHECKING, TextIO

# aws-cli's own encoding decisions, which the library's `S3.from_aws_cli`
# shares: `text_encoding` for text, `filename_encoding` for local names (and
# the command line). Light like this module: the standard library only.
from boto3_s3.localencoding import codec_name as _codec_name
from boto3_s3.localencoding import filename_encoding, text_encoding

if TYPE_CHECKING:
    from boto3_s3 import LocalStorage

# aws-cli's codec override for its error reports (`prepare_error_report`).
OUTPUT_ENCODING_ENV_VAR = "AWS_CLI_OUTPUT_ENCODING"

# The locales whose stdio Python writes with ``surrogateescape`` outside UTF-8
# mode: C and POSIX, and the targets of its C-locale coercion (CPython's
# ``_Py_IsLocaleCoercionTarget``).
_SURROGATEESCAPE_LOCALES = frozenset({"C", "POSIX", "C.UTF-8", "C.utf8", "UTF-8"})


def _converts(encoding: str | None) -> bool:
    """Whether names in ``encoding`` differ from Python's own (file system encoding)."""
    name = _codec_name(encoding)
    return name is not None and name != _codec_name(sys.getfilesystemencoding())


def decode_argv(args: list[str]) -> list[str]:
    """The command line as ``aws`` reads it: each argument decoded with `filename_encoding`.

    Python decodes the arguments with its file system encoding
    (``surrogateescape``), so ``os.fsencode`` gives each one's bytes back;
    an argument whose bytes read differently under the CLI's encoding is
    decoded again with it (``surrogateescape`` for bytes it cannot decode).
    ``aws``'s launcher refuses such bytes before Python starts (rc 255); here
    they are carried and fail where they are used. Unchanged where the two
    encodings agree, and on Windows and macOS.
    """
    encoding = filename_encoding()
    if encoding is None or not _converts(encoding):
        return list(args)
    decoded: list[str] = []
    for arg in args:
        try:
            raw = os.fsencode(arg)
        except UnicodeError:
            decoded.append(arg)
            continue
        decoded.append(raw.decode(encoding, "surrogateescape"))
    return decoded


def _is_windows_console(stream: TextIO) -> bool:
    """Whether ``stream`` writes to a Windows console (Unicode through its own layer).

    Asked of the stream itself, not of its raw layer: unbuffered stdio
    (``-u``, ``PYTHONUNBUFFERED``) has no ``raw`` to look at.
    """
    if sys.platform != "win32":
        return False
    try:
        return stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _reconfigure(stream: TextIO | None, encoding: str, errors: str) -> None:
    """Have ``stream`` write ``encoding`` with ``errors``, where it can.

    Left alone: no stream, a stream that cannot be reconfigured (a test
    double), a Windows console, and an encoding Python has no codec for.
    """
    if stream is None or _is_windows_console(stream):
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding=encoding, errors=errors)
    except (LookupError, ValueError, OSError):
        pass


def _stdout_errors() -> str:
    """The error handler ``aws``'s interpreter gives its stdout here.

    Python's rule outside UTF-8 mode: ``surrogateescape`` on Windows and in
    the C / POSIX locales (and the targets of their coercion), ``strict``
    elsewhere - where an unencodable character fails the write and
    `output.uni_write` writes ``?`` instead, as ``aws``'s ``uni_print`` does.
    """
    if sys.platform == "win32":
        return "surrogateescape"
    if locale.setlocale(locale.LC_CTYPE) in _SURROGATEESCAPE_LOCALES:
        return "surrogateescape"
    return "strict"


def configure_std_streams() -> None:
    """Write standard output and standard error with `text_encoding`, as ``aws`` does.

    Only where it differs from the encoding or the error handler Python chose
    for the stream (UTF-8 mode, say): stdout takes the error handler of ``aws``'s
    interpreter (`_stdout_errors`), stderr ``backslashreplace``, as Python
    gives every stderr. A Windows console is left alone - it takes Unicode
    through its own layer - so only a pipe or a file is re-encoded there.
    Called once, at startup, before anything is written.
    """
    name = _codec_name(text_encoding())
    if name is None:
        return
    for stream, errors in ((sys.stdout, _stdout_errors()), (sys.stderr, "backslashreplace")):
        if stream is not None and (
            _codec_name(getattr(stream, "encoding", None)) != name
            or getattr(stream, "errors", None) != errors
        ):
            _reconfigure(stream, name, errors)


def prepare_error_report(stream: TextIO | None) -> None:
    """aws-cli's ``set_preferred_output_encoding``, before an ``[ERROR]`` report.

    ``aws`` re-encodes the stream it writes its top-level error reports to:
    with ``AWS_CLI_OUTPUT_ENCODING`` when it is set, else with UTF-8 when
    ``PYTHONUTF8`` is ``1`` (its frozen interpreter ignores that variable, so
    aws-cli reads it back for these reports alone). Nothing else ``aws``
    writes follows either variable (measured). An unknown codec is refused at
    startup (``cli._dispatch``) and left alone here; the handler is
    ``backslashreplace``, so the report is always written.
    """
    encoding = os.environ.get(OUTPUT_ENCODING_ENV_VAR)
    if encoding is None and os.environ.get("PYTHONUTF8") == "1":
        encoding = "UTF-8"
    if encoding is None or _codec_name(encoding) is None:
        return
    _reconfigure(stream, encoding, "backslashreplace")


@functools.cache
def _anchor(encoding: str | None) -> LocalStorage:
    """A ``LocalStorage`` carrying ``encoding``, for its conversions (`os_path`).

    Rooted at the filesystem root: an absolute path asks nothing of the
    working directory, which may be gone.
    """
    from boto3_s3 import LocalStorage

    return LocalStorage(os.sep, filename_encoding=encoding)


def os_path(path: str) -> str:
    """A local path from the command line, as Python's OS calls take it.

    ``LocalStorage.os_path`` under `filename_encoding`: the path itself where
    nothing converts.
    """
    encoding = filename_encoding()
    return path if not _converts(encoding) else _anchor(encoding).os_path(path)


def text_path(path: str) -> str:
    """A path as Python's OS calls return it, as the CLI's text (the reverse of `os_path`)."""
    encoding = filename_encoding()
    return path if not _converts(encoding) else _anchor(encoding).text_path(path)


def name_files_as_text(exc: OSError) -> OSError:
    """``exc`` with the file names it carries as the CLI's text, for its report.

    The OS reports the names it was handed, in its own form (`os_path`);
    ``str(exc)`` formats them when the report is built. Changed in place, and
    returned for use inside a message.
    """
    for attr in ("filename", "filename2"):
        name = getattr(exc, attr)
        if isinstance(name, str):
            with contextlib.suppress(UnicodeError):
                setattr(exc, attr, text_path(name))
    return exc


__all__ = [
    "OUTPUT_ENCODING_ENV_VAR",
    "configure_std_streams",
    "decode_argv",
    "filename_encoding",
    "name_files_as_text",
    "os_path",
    "prepare_error_report",
    "text_encoding",
    "text_path",
]
