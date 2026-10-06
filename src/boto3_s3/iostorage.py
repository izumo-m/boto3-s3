"""``boto3_s3.iostorage``: a single in-hand stream presented as a ``Storage``.

``IOStorage`` adapts one caller-supplied file-like object into the ``Storage``
contract so it can be one side of a ``cp`` transfer - the building block behind
``cp(s3_uri, IOStorage(buf))`` / ``cp(IOStorage(buf), s3_uri)``. It is a single
endpoint, not a container: only ``open`` is meaningful;
``scan_pages`` / ``delete`` / ``get_fileinfo`` raise. The S3 side still rides ``s3transfer`` off its
client/bucket; this side hands ``s3transfer`` the fileobj that ``open`` returns.

The s3transfer boundary is always **bytes** (like botocore's ``StreamingBody``):
``IOStorage(binary_stream)`` presents the stream through a close-suppressing
view, while ``IOStorage(text_stream)`` wraps it with an incremental codec
(``encoding``, default utf-8) - encode on read (upload), decode on write
(download). A binary stream in append mode (``open(p, "ab")``, a ``>>``
redirected stdout's ``.buffer``) is written through a write-only view instead:
it reports ``seekable()`` and takes ``seek``, but every write lands at its end,
so s3transfer's positioned, parallel download would append the ranged parts in
completion order - the view hides ``seek`` and the download runs in order.
(The descriptor's flag can be read on POSIX only; on Windows the ``mode``
string is the whole cue - `_is_append_mode`.)
The transfer ``close``s every fileobj ``open`` returns (the open
route flushes a real backend's writer that way), so each ``IOStorage`` *writer*
view absorbs that ``close`` into a flush and each reader view's ``close`` is a
no-op (nothing to release): the caller's stream is **never closed** by
``IOStorage``
(it owns only the thin view / codec adapter).

``StdioStorage`` is the convenience for the process's stdio: as a source it reads
``sys.stdin`` (forced non-seekable, so s3transfer takes its buffered upload path -
Windows stdin reports a false ``seekable()``), as a destination it writes
``sys.stdout``; both via ``.buffer`` (binary). Its stdout writer buffers nothing
of its own and swallows the transfer's ``close`` outright, exactly like the
writer aws hands s3transfer: whatever the process stream buffered is left for
the interpreter to flush when it exits.

Like its peers it is imported by submodule path and imports no AWS SDK module, so
``import boto3_s3.iostorage`` stays SDK-free.
"""

from __future__ import annotations

import codecs
import io
import os
import stat
import sys
from typing import IO, TYPE_CHECKING, Any, ClassVar, Literal, cast

from typing_extensions import override

from boto3_s3.exceptions import InvalidValueError, ValidationError
from boto3_s3.storage import Storage, StorageCapability

if TYPE_CHECKING:
    from typing import BinaryIO

try:
    import fcntl
except ImportError:  # Windows: no descriptor flags; the open mode is the only cue
    fcntl = None


_READ_CHUNK = 64 * 1024


class _Uncloseable:
    """A pass-through binary view whose ``close`` never closes the wrapped stream.

    The open route lets the transfer ``close`` every fileobj ``open`` returns -
    that ``close`` is how a real backend flushes a write. IOStorage never owns
    the caller's stream, so it hands back this view instead of the raw stream:
    ``close`` flushes any buffered bytes (so a stdout download is visible) but
    leaves the underlying stream open for the caller. Every other attribute
    (``read`` / ``write`` / ``seek`` / ``seekable`` ...) delegates unchanged, so
    s3transfer drives it exactly like the wrapped stream.
    """

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def close(self) -> None:
        flush = getattr(self._raw, "flush", None)
        if flush is not None:
            flush()


class _WriteOnly:
    """Write-only binary view of process stdout (aws-cli's ``StdoutBytesWriter``).

    Exposing only ``write`` forces s3transfer's non-seekable download path,
    which orders ranged chunks before writing them out. A redirected stdout
    can report ``seekable()`` (a regular file) while ``>>`` opened it
    ``O_APPEND``, where every write lands at the end regardless of the seeked
    position - a seek-based parallel download would then interleave chunks in
    completion order. aws always streams stdout sequentially, so this view
    does too.

    The process stream is read per write, like aws's ``bytes_print``, and
    nothing is held from ``open`` time: a process without a stdout therefore
    fails on the ``write`` itself, from inside the transfer, which reports it
    as that item's failure the way aws does - not as a precondition of
    starting one. ``close`` neither flushes nor closes: aws's writer has no
    ``close`` at all and a non-seekable download's final task is a no-op, so
    the bytes the process stream buffered wait for the interpreter's flush at
    exit. A stdout that cannot take them then fails the *process* on that
    shutdown flush rather than the transfer, and the caller's stdout is left
    open either way.
    """

    def write(self, data: Any) -> int:
        stdout: Any = sys.stdout
        return getattr(stdout, "buffer", stdout).write(data)

    def close(self) -> None:
        pass


class _SequentialWriter:
    """Write-only binary view of an append-mode stream (see `_is_append_mode`).

    Exposing only ``write`` takes s3transfer's non-seekable download path, which
    orders the ranged parts before writing them out, so the bytes land in
    object order at the stream's end - where an append-mode stream puts every
    write anyway. ``close`` flushes and leaves the caller's stream open, like
    `_Uncloseable`.
    """

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def write(self, data: Any) -> int:
        return self._raw.write(data)

    def close(self) -> None:
        flush = getattr(self._raw, "flush", None)
        if flush is not None:
            flush()


def _is_append_mode(stream: Any) -> bool:
    """Whether every write to ``stream`` lands at its end (``O_APPEND``).

    Such a stream still reports ``seekable()`` and honours ``seek`` / ``tell``,
    so through the plain pass-through view s3transfer would take its positioned,
    parallel download path and every ranged part would be appended in
    completion order - a corrupted object reported as a success. The cue is the
    open mode where the object carries one (``"a"`` in ``mode``:
    ``open(p, "ab")``), and otherwise the descriptor's flags where the platform
    can read them (``fcntl``'s ``F_GETFL``): a ``>>`` redirected stdout's
    ``.buffer`` has mode ``"wb"`` and only the flag reveals it. A stream with no
    descriptor (``BytesIO``) is not append-only. Windows has no ``fcntl``, so
    there the mode string is the whole cue: a descriptor opened ``O_APPEND``
    under another mode goes unrecognized (measured: its multipart download
    scrambles), and a ``cmd`` ``>>`` redirect is not append-only in the first
    place - the shell seeks to the end and the stream writes at offsets.
    """
    mode = getattr(stream, "mode", None)
    if isinstance(mode, str) and "a" in mode:
        return True
    if fcntl is None:
        return False
    fileno = getattr(stream, "fileno", None)
    if fileno is None:
        return False
    try:
        fd = fileno()
    except Exception:
        # io.UnsupportedOperation (a BytesIO), or a `fileno` forwarded to a
        # wrapped object that has none - a GzipFile over a write-only sink
        # raises AttributeError: no descriptor to read the flags from, so
        # not append-only, as every such stream was before this cue existed.
        return False
    try:
        return bool(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_APPEND)
    except OSError:
        return False


def _cannot_seek(stream: Any) -> bool:
    """Whether a binary source must be read through `_NonSeekable`.

    A stream that answers ``seekable()`` False - a POSIX pipe, a socket file
    - still carries ``seek`` and ``tell``, and the CRT lane hands the stream
    to botocore, whose content-length probe calls them when both exist and
    expects ``io.UnsupportedOperation`` where a pipe raises
    ``OSError(ESPIPE)``: the upload fails with ``[Errno 29] Illegal seek``
    (classic asks ``seekable()`` first and takes its buffered path). A
    Windows pipe is worse: it answers ``seekable()`` True and lets ``tell``
    and ``seek`` succeed with meaningless positions, so the probe computes a
    wrong length and the CRT lane stores a truncated object as a success
    (measured: 4176 bytes of a 9 MiB `subprocess` pipe) while classic fails.
    The descriptor's type settles both: a FIFO, a character device (a
    console) or a socket cannot be positioned whatever ``seekable()`` says,
    and ``os.fstat`` reports a Windows pipe as a FIFO too (measured).

    What cannot answer is handled by what the engines would do with it. A
    closed stream raises ``ValueError`` from ``seekable()`` and is left as
    it is: the transfer reports the closed stream as it already does, where
    the view would leave the CRT lane's failure wordless (measured). A
    stream whose ``seekable`` raises anything else cannot be asked twice -
    a stream-mode tarfile member's ``BufferedReader`` forwards ``seekable``
    to a wrapped object that has none (``AttributeError``), and s3transfer's
    own ``seekable()`` probe would fail the item the same way - so it is
    read through the view, which both engines then upload. A stream without
    ``fileno``, or whose ``fileno`` cannot answer (a ``BytesIO``'s
    ``io.UnsupportedOperation``; a regular tarfile member, whose wrapped
    object has ``seekable`` but no ``fileno``, ``AttributeError``), is left
    as it is: ``seekable()`` already answered True, and there is no
    descriptor to say otherwise.
    """
    seekable = getattr(stream, "seekable", None)
    if seekable is not None:
        try:
            if not seekable():
                return True
        except ValueError:
            return False
        except Exception:
            return True
    fileno = getattr(stream, "fileno", None)
    if fileno is None:
        return False
    try:
        mode = os.fstat(fileno()).st_mode
    except Exception:
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISCHR(mode) or stat.S_ISSOCK(mode)


class _NonSeekable:
    """Read-only binary view that hides ``seek`` (aws-cli's ``NonSeekableStream``).

    Some streams that are not truly seekable still report ``seekable() == True``
    (Windows stdin, a Windows pipe); exposing only ``read`` forces the buffered
    non-seekable upload path on both engines - s3transfer's, and botocore's
    chunked body on the CRT lane. `StdioStorage` applies it to stdin
    unconditionally, `IOStorage` to any source `_cannot_seek` recognizes.
    """

    def __init__(self, fileobj: Any) -> None:
        self._fileobj = fileobj

    def read(self, amt: int | None = None) -> bytes:
        return self._fileobj.read() if amt is None else self._fileobj.read(amt)

    def close(self) -> None:
        # The transfer closes its source fileobj when done; this view reads the
        # caller's stream, which IOStorage never closes (nothing to release).
        pass


class _EncodingReader:
    """``str`` source presented as a non-seekable binary reader (encode on read).

    One incremental encoder spans every read (the encode mirror of
    ``_DecodingWriter``'s incremental decode), so a stateful codec encodes as a
    single stream - utf-16 emits its BOM once, not per chunk - and EOF flushes
    the encoder's pending tail (``final=True``).
    """

    def __init__(self, text: IO[str], encoding: str) -> None:
        self._text = text
        self._encoder = codecs.getincrementalencoder(encoding)()
        self._buf = b""
        self._eof = False

    def _encode(self, chunk: str) -> bytes:
        """Encode one source chunk; ``""`` (EOF) flushes the encoder's tail once."""
        if chunk:
            return self._encoder.encode(chunk)
        if self._eof:
            return b""
        self._eof = True
        return self._encoder.encode("", final=True)

    def read(self, amt: int | None = None) -> bytes:
        if amt is None or amt < 0:
            # Read to EOF: the rest of the text, then the encoder's final tail.
            out = self._buf + self._encode(self._text.read()) + self._encode("")
            self._buf = b""
            return out
        while len(self._buf) < amt and not self._eof:
            self._buf += self._encode(self._text.read(_READ_CHUNK))
        out, self._buf = self._buf[:amt], self._buf[amt:]
        return out

    def close(self) -> None:
        # A reader over the caller's text stream: nothing to release, and the
        # caller's stream is never closed by IOStorage.
        pass


def _is_text_stream(stream: IO[Any]) -> bool:
    """Whether ``stream`` trades in ``str`` (so ``open`` must adapt it to bytes).

    ``io.TextIOBase`` covers the stdlib's usual text streams (``TextIOWrapper``,
    ``StringIO``), but the constructor accepts any ``IO[str]`` - ``codecs.open``'s
    ``StreamReaderWriter`` or a text-mode ``SpooledTemporaryFile`` read ``str``
    without deriving from it. Those carry the text hallmark instead: an
    ``encoding`` attribute, which no binary stream has. Without this probe such a
    stream would pass through as "binary" and s3transfer would fail obscurely on
    its ``str`` chunks.
    """
    return isinstance(stream, io.TextIOBase) or hasattr(stream, "encoding")


class _DecodingWriter:
    """Binary writer that decodes to ``str`` and writes to a text stream.

    Never closes the underlying stream; ``close`` only flushes the incremental
    decoder's remainder (none for a complete, valid object).
    """

    def __init__(self, text: IO[str], encoding: str) -> None:
        self._text = text
        self._decoder = codecs.getincrementaldecoder(encoding)()

    def write(self, data: bytes) -> int:
        self._text.write(self._decoder.decode(data))
        return len(data)

    def flush(self) -> None:
        self._text.flush()

    def close(self) -> None:
        tail = self._decoder.decode(b"", final=True)
        if tail:
            self._text.write(tail)
        self._text.flush()


class IOStorage(Storage):
    """One caller-supplied stream as a ``Storage`` (a single ``open``-able endpoint).

    Pass it to ``cp`` as a ``Location``: ``cp("s3://b/k", IOStorage(io.BytesIO()))``
    downloads into the stream, ``cp(IOStorage(buf), "s3://b/k")`` uploads from it.
    ``mv("s3://b/k", IOStorage(buf))`` additionally deletes the S3 source after
    the bytes land (a stream is never a move *source* - it cannot be deleted).
    A binary stream is used as-is behind a close-suppressing view
    (`_Uncloseable`) - except one in append mode, written through a write-only
    view (`_SequentialWriter`) so a multipart download lands in order rather
    than in completion order, and one that cannot be positioned (a pipe, a
    socket, a console - `_cannot_seek`), read through a read-only view
    (`_NonSeekable`) so both engines take their buffered upload path instead
    of probing it with ``seek`` / ``tell``; a text stream - recognized as an
    ``io.TextIOBase`` or by its ``encoding`` attribute (``codecs.open``'s
    ``StreamReaderWriter``, a text-mode ``SpooledTemporaryFile``) - is wrapped
    with ``encoding`` (default utf-8). The caller's stream is never closed by
    this class. As a single endpoint it has no listing: ``scan_pages`` /
    ``delete`` / ``get_fileinfo`` raise.

    ``capabilities`` is just the ``OPEN_*`` pair: a single stream supports only byte
    I/O (both directions, chosen per ``open`` call), with no listing or deletion.

    ``encoding`` is checked at construction (``codecs.lookup``): an unknown name
    is an ``InvalidValueError`` here rather than a bare ``LookupError`` from the
    first ``open`` - which the eager stream route of ``cp`` would let escape
    the library's taxonomy while the lazy open route of ``mv`` reported it as
    the item's failure.
    """

    scheme: ClassVar[str] = "stream"
    capabilities: ClassVar[StorageCapability] = (
        StorageCapability.OPEN_READ | StorageCapability.OPEN_WRITE
    )

    def __init__(self, stream: IO[bytes] | IO[str], *, encoding: str = "utf-8") -> None:
        self._stream: IO[Any] | None = stream
        try:
            codecs.lookup(encoding)
        except LookupError as exc:
            raise InvalidValueError(str(exc)) from exc
        self._encoding = encoding

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        """Return the wrapped stream as a binary stream (``key`` / ``size`` ignored).

        A single endpoint takes no key. A text stream is adapted to bytes via the
        configured ``encoding`` (encode on ``"rb"``, decode on ``"wb"``). A
        binary stream in append mode is written through a write-only view, so
        the download runs in order (`_is_append_mode`); one that cannot be
        positioned is read through a read-only view (`_cannot_seek`).
        """
        stream = self._stream
        assert stream is not None  # plain IOStorage always holds a stream
        if _is_text_stream(stream):
            adapter = (
                _EncodingReader(stream, self._encoding)
                if mode == "rb"
                else _DecodingWriter(stream, self._encoding)
            )
            return cast("BinaryIO", adapter)
        if mode == "wb" and _is_append_mode(stream):
            return cast("BinaryIO", _SequentialWriter(stream))
        if mode == "rb" and _cannot_seek(stream):
            return cast("BinaryIO", _NonSeekable(stream))
        return cast("BinaryIO", _Uncloseable(stream))

    @override
    def as_text(self) -> str:
        """Return the stdio token ``"-"`` (``Storage.as_text``, display-only).

        A stream has no location, so this token is for display / error messages
        only - never round-tripped. ``cp`` diverts a stream to its own path
        before any plan is built, but ``mv`` with a stream *destination* does
        ride ``transferplan.plan_transfer``: the stream folds into the custom
        (``s3open``) arm, where the default ``Storage.format`` reads this
        token (``"-"`` has no trailing ``/``, so a single move keeps its key).
        """
        return "-"


class StdioStorage(IOStorage):
    """The process's stdio as a ``Storage``: ``sys.stdin`` to read, ``sys.stdout`` to write.

    A source ``open("rb")`` reads ``sys.stdin.buffer`` (forced non-seekable); a
    destination ``open("wb")`` writes ``sys.stdout.buffer`` through a
    write-only view (`_WriteOnly`, aws's ``StdoutBytesWriter``), so a
    download always streams sequentially even when stdout is redirected to
    a seekable file. The direction is chosen
    by ``mode``, so a single instance serves either one and
    picks up a redirected ``sys.stdin`` / ``sys.stdout``.

    A process without a ``sys.stdin`` fails the ``open`` with
    ``ValidationError`` (aws's ``StdinMissingError``, the same sentence), and
    the error carries no operation name - the operation that invoked the
    storage fills it in (a direct call leaves it ``None``). Stdout has no such
    check, again like aws: a missing one surfaces from the writer's first
    ``write``, inside the transfer, as that item's failure.
    """

    scheme: ClassVar[str] = "stdio"

    def __init__(self) -> None:
        self._stream = None
        self._encoding = "utf-8"

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        if mode == "rb":
            stdin = sys.stdin
            if stdin is None:
                # A runtime-state precondition (no stdin in this process), so
                # ValidationError; raised in-pipeline, rc 1 either way. The
                # operation name is left unset: this storage does not know which
                # operation opened it, so the operation layer stamps its own
                # name (a streaming cp; mv rejects a stream source before any
                # open).
                raise ValidationError("stdin is required for this operation, but is not available.")
            return cast("BinaryIO", _NonSeekable(getattr(stdin, "buffer", stdin)))
        # No stdout counterpart to the guard above: aws checks stdin only, and
        # its stdout writer dereferences the process stream per write, so a
        # process without one fails the item instead of the run.
        return cast("BinaryIO", _WriteOnly())


__all__ = ["IOStorage", "StdioStorage"]
