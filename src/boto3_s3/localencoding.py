"""The text encoding aws-cli uses in the same environment: its local file names and text.

design/overview.md section 3 ("Text encodings"): which encoding turns bytes
into text depends on the Python version and its environment - Python 3.15
turns UTF-8 mode on by default, ``PYTHONUTF8=1`` does on any version - while
``aws`` decides it the same way whatever Python runs (its frozen interpreter
ignores ``PYTHONUTF8``). `S3.from_aws_cli` names local files in
`filename_encoding`; the ``boto3-s3`` command uses `text_encoding` wherever
it converts text too. Measured against the pinned ``aws`` on Linux
(``C.UTF-8``, ``C``, ``POSIX``, ``ja_JP.EUC-JP``) and Windows (cp932) under
Python 3.10, 3.14 and 3.15.

The standard library only: the command reads this at startup.
"""

from __future__ import annotations

import codecs
import locale
import sys


def text_encoding() -> str:
    """The encoding ``aws`` uses here for text: its output and ``file://``.

    aws-cli's ``compat.getpreferredencoding`` without the
    ``AWS_CLI_FILE_ENCODING`` override (that one is ``file://``'s alone): UTF-8
    when the ``LC_CTYPE`` locale is ``C`` or ``POSIX`` (aws-cli applies PEP 540
    itself, its frozen build lacking the interpreter's), otherwise the
    locale's own encoding - read past Python's UTF-8 mode, which ``aws``
    never runs in (what ``locale.getencoding()`` returns on 3.11+: the
    ``LC_CTYPE`` codeset, UTF-8 when it names none). On Windows the ANSI code
    page (``GetACP``), what ``aws``'s interpreter reads as the locale encoding
    there.
    """
    if sys.platform == "win32":
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetACP()}"
    if locale.setlocale(locale.LC_CTYPE) in ("C", "POSIX"):
        return "UTF-8"
    # locale.getencoding() (3.11+) on every supported version: the codeset,
    # UTF-8 where the locale names none.
    return locale.nl_langinfo(locale.CODESET) or "utf-8"


def codec_name(encoding: str | None) -> str | None:
    """``encoding``'s canonical codec name, or ``None`` for one Python lacks."""
    if encoding is None:
        return None
    try:
        return codecs.lookup(encoding).name
    except LookupError:
        return None


def filename_encoding() -> str | None:
    """The encoding ``aws`` names and creates local files with here, or ``None``.

    `text_encoding` where Python's file system encoding follows the locale
    (Linux and the other non-Apple POSIX systems). ``None`` - Python's own
    choice, which is ``aws``'s there - on Windows, whose names are Unicode,
    and on macOS, where Python (``aws``'s included) always reads them as
    UTF-8; and for an encoding Python has no codec for.
    """
    if sys.platform in ("win32", "darwin"):
        return None
    encoding = text_encoding()
    return encoding if codec_name(encoding) is not None else None


__all__ = ["codec_name", "filename_encoding", "text_encoding"]
