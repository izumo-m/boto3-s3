"""``boto3_s3.iostorage``: a single stream presented as a ``Storage``.

A stream storage (`IOStorageBase`) is one side of a ``cp`` transfer whose
other side is S3 - the building block behind ``cp(s3_uri, IOStorage(buf))`` /
``cp(IOStorage(buf), s3_uri)``. It is a single endpoint, not a container: only
``open`` is meaningful; ``scan_pages`` / ``delete`` / ``get_fileinfo`` raise.
The S3 side still rides ``s3transfer`` off its client/bucket; this side hands
``s3transfer`` the fileobj that ``open`` returns.

The s3transfer boundary is always **bytes** (like botocore's ``StreamingBody``).
``IOStorage`` takes a binary stream and presents it through a close-suppressing
view - a read-only one for a source that cannot be positioned whatever it
reports (a pipe, a socket, a console, judged by its descriptor:
`_cannot_seek`; a Windows pipe answers ``seekable()`` True), so the engines
read it in order instead of probing it with ``seek``. A download never writes
the view directly: ``cp`` and ``mv`` write every stream destination through
the open route's deferred writer, which exposes only ``write``, so the stream
is written in order from where it stands and opened only by the first byte -
or, for an empty object, by the successful download's commit.

Text goes through ``TextIOStorageBase``: its subclass opens a text stream
(``open_text``), and ``open`` wraps it with an incremental codec (``encoding``,
default utf-8) - encode on read (upload), decode on write (download).
``TextIOStorage`` is the one for a text stream the caller hands over.

The transfer ``close``s every fileobj ``open`` returns (the open route flushes
a real backend's writer that way). A view over the caller's stream absorbs
that ``close`` into a flush, or ignores it (`_NonSeekable`), so the caller's
stream is **never closed** by ``IOStorage`` or ``TextIOStorage``; a codec
adapter closes the text stream ``open_text`` returned, which is the caller's
behind such a view, or one a subclass opened for the transfer.

``StdioStorage`` is the convenience for the process's stdio: as a source it reads
``sys.stdin`` (forced non-seekable, so s3transfer takes its buffered upload path -
Windows stdin reports a false ``seekable()``), as a destination it writes
``sys.stdout``; both via ``.buffer`` (binary). Its stdout writer buffers nothing
of its own, and it too absorbs the transfer's ``close`` into a flush of the
process stream, so a stdout that cannot take the bytes fails the item.

Like its peers it is imported by submodule path and imports no AWS SDK module, so
``import boto3_s3.iostorage`` stays SDK-free.
"""

from __future__ import annotations

import abc
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
    from typing import BinaryIO, TextIO

    from _typeshed import SupportsRead, SupportsWrite

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
    completion order. aws-cli always streams stdout sequentially, so this view
    does too.

    The process stream is read per write, like aws-cli's ``bytes_print``, and
    nothing is held from ``open`` time: a process without a stdout therefore
    fails on the ``write`` itself, from inside the transfer, which reports it
    as that item's failure the way aws-cli does - not as a precondition of
    starting one. An empty object, which writes nothing, fails the same way
    at ``close``, as on aws.

    ``close`` flushes the process stream and never closes it. The transfer
    closes its writer as the item settles (`transfer._CloseFileobj`), so a
    stdout that cannot take the bytes it still buffers - a full disk, a
    reader that left the pipe - fails the item, and a ``mv`` keeps its
    source. aws-cli's writer has no ``close`` and leaves those bytes for the
    interpreter's flush at exit, where a failure can no longer reach the item.
    The bytes a failed flush refused stay in the process stream's buffer, and
    the interpreter tries them again at exit (exit status 120): the process's
    stdout is the application's, so what happens to them is too.
    """

    def write(self, data: Any) -> int:
        stdout: Any = sys.stdout
        return getattr(stdout, "buffer", stdout).write(data)

    def close(self) -> None:
        stdout: Any = sys.stdout
        if stdout is None:
            # No stdout fails the item whatever the object held: an empty one
            # writes nothing, so this is where it fails, with the error aws-cli's
            # writer meets on its write (a non-empty one already failed there).
            self.write(b"")
            return
        getattr(stdout, "buffer", stdout).flush()


def _cannot_seek(stream: Any) -> bool:
    """Whether a binary source cannot be positioned (`_NonSeekable`).

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

    A stream that cannot answer is left as it is: this probe adds no failure
    of its own, and the engines meet the stream as they would without it - a
    closed stream, or one whose ``seekable()`` raises (outside the stream
    contract), fails there. A stream without ``fileno``, or whose ``fileno``
    cannot answer (a ``BytesIO``, a wrapper with no descriptor behind it),
    has no descriptor to say otherwise.
    """
    seekable = getattr(stream, "seekable", None)
    if seekable is not None:
        try:
            if not seekable():
                return True
        except Exception:
            return False
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
    the encoder's pending tail (``final=True``). ``close`` closes the text
    stream: it is what `TextIOStorageBase.open_text` returned, which the
    transfer owns once it is open.
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
        # Gathered and joined once: growing one bytes object by each encoded
        # chunk copies it whole every time, quadratic in the 8 MiB the classic
        # engine asks for per part.
        parts = [self._buf]
        have = len(self._buf)
        while have < amt and not self._eof:
            chunk = self._encode(self._text.read(max(amt - have, _READ_CHUNK)))
            parts.append(chunk)
            have += len(chunk)
        data = b"".join(parts)
        self._buf = data[amt:]
        return data[:amt]

    def close(self) -> None:
        self._text.close()


class _DecodingWriter:
    """Binary writer that decodes to ``str`` and writes to a text stream.

    ``close`` writes the incremental decoder's remainder (none for a complete,
    valid object) and then closes the text stream, whatever that write did:
    the stream is what `TextIOStorageBase.open_text` returned, which the
    transfer owns once it is open.
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
        try:
            tail = self._decoder.decode(b"", final=True)
            if tail:
                self._text.write(tail)
        finally:
            self._text.close()


class IOStorageBase(Storage):
    """One stream endpoint as a ``Storage``: the base of the stream storages.

    A stream endpoint is a single object with no location: ``cp`` streams a
    single object to or from one (the other side must be S3), ``mv`` moves a
    single object onto one, and ``scan_pages`` / ``delete`` / ``get_fileinfo``
    raise. A subclass implements ``open``, the bytes the transfer reads
    (``"rb"``, an upload) or writes (``"wb"``, a download); the transfer closes
    what it returns once the item settles, as for any ``Storage.open``. ``key``
    and ``size`` carry nothing for a single endpoint (what an operation passes
    differs), so an implementation ignores them.

    When each ``open`` runs differs by direction. A download's is deferred to
    its first write (the open route's deferred writer, for ``cp`` and ``mv``
    alike): the stream is written in order, and a download that ends before
    its first byte - a missing object, a cancel - never opens it, while an
    empty object's successful download opens it at the commit. An upload's
    runs as ``cp`` starts, before the transfer, as aws-cli opens stdin: a
    source that cannot be opened stops the run with the library's exception
    (a family error as raised, any other as the base error with it as the
    cause). What an upload's ``open`` returns reaches the engines as it is,
    with none of `IOStorage`'s views: one that offers ``seek`` / ``tell`` and
    answers ``seekable()`` True is read at offsets on the classic engine and
    has its length probed through them on the CRT engine, so it must honour
    them; one that offers only ``read`` is read in order.

    `IOStorage` serves a binary stream it is given, `StdioStorage` the
    process's standard input and output, and `TextIOStorageBase` a text stream
    its subclass opens.

    ``capabilities`` is just the ``OPEN_*`` pair: a stream supports only byte
    I/O (both directions, chosen per ``open`` call), with no listing or deletion.
    """

    scheme: ClassVar[str] = "stream"
    capabilities: ClassVar[StorageCapability] = (
        StorageCapability.OPEN_READ | StorageCapability.OPEN_WRITE
    )

    @override
    @abc.abstractmethod
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        """The bytes of the stream, read for ``"rb"`` and written for ``"wb"``."""

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


class IOStorage(IOStorageBase):
    """A binary stream you hand over, as a stream endpoint (`IOStorageBase`).

    ``cp("s3://b/k", IOStorage(io.BytesIO()))`` downloads into the stream,
    ``cp(IOStorage(buf), "s3://b/k")`` uploads from it, and
    ``mv("s3://b/k", IOStorage(buf))`` additionally deletes the S3 source after
    the bytes land. The stream trades in bytes; a text stream goes through
    `TextIOStorage`. One whose class makes it text - an ``io.TextIOBase``: a
    ``StringIO``, a text-mode file, ``sys.stdout`` - is refused here with
    ``ValidationError``; no other stream is checked, a text-mode
    ``NamedTemporaryFile`` / ``SpooledTemporaryFile`` or a ``codecs`` stream
    (none derives from an ``io`` class) included:
    a wrong kind is a wrong type, whose outcome is not specified (overview
    section 3).

    The stream is used as-is behind a close-suppressing view (`_Uncloseable`) -
    except a source that cannot be positioned (a pipe, a socket, a console -
    `_cannot_seek`), read through a read-only one (`_NonSeekable`) so both
    engines take their buffered upload path instead of probing it with
    ``seek`` / ``tell``. A download writes the stream in order through the
    transfer's deferred writer, whatever its kind. The caller's stream is
    never closed by this class.
    """

    def __init__(self, stream: SupportsRead[bytes] | SupportsWrite[bytes]) -> None:
        # Typed by what a transfer needs - a read for an upload, a write for a
        # download - so a wrapper exposing only one of them (the gzip example
        # in docs/library/streams.md) type-checks; the views probe the rest.
        if isinstance(stream, io.TextIOBase):
            raise ValidationError(
                "IOStorage takes a binary stream; use TextIOStorage for a text stream"
            )
        self._stream: IO[Any] = cast("IO[Any]", stream)

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        """Return the wrapped stream as a binary stream (``key`` / ``size`` ignored).

        A source that cannot be positioned is read through a read-only view.
        """
        stream = self._stream
        if mode == "rb" and _cannot_seek(stream):
            return cast("BinaryIO", _NonSeekable(stream))
        return cast("BinaryIO", _Uncloseable(stream))


class StdioStorage(IOStorageBase):
    """The process's stdio as a ``Storage``: ``sys.stdin`` to read, ``sys.stdout`` to write.

    A source ``open("rb")`` reads ``sys.stdin.buffer`` (forced non-seekable); a
    destination ``open("wb")`` writes ``sys.stdout.buffer`` through a
    write-only view (`_WriteOnly`, aws-cli's ``StdoutBytesWriter``), so a
    download always streams sequentially even when stdout is redirected to
    a seekable file; the transfer's ``close`` of that view flushes
    ``sys.stdout.buffer`` without closing it, so a stdout that cannot take
    the bytes fails the item. The direction is chosen
    by ``mode``, so a single instance serves either one and
    picks up a redirected ``sys.stdin`` / ``sys.stdout``.

    A process without a ``sys.stdin`` fails the ``open`` with
    ``ValidationError`` (aws-cli's ``StdinMissingError``, the same sentence), and
    the error carries no operation name - the operation that invoked the
    storage fills it in (a direct call leaves it ``None``). Stdout has no such
    check, again like aws: a missing one surfaces from the writer's first
    ``write`` (an empty object's ``close``), inside the transfer, as that
    item's failure.
    """

    scheme: ClassVar[str] = "stdio"

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
        # No stdout counterpart to the guard above: aws-cli checks stdin only, and
        # its stdout writer dereferences the process stream per write, so a
        # process without one fails the item instead of the run.
        return cast("BinaryIO", _WriteOnly())


class TextIOStorageBase(IOStorageBase):
    """A stream endpoint that trades in text: the base for a text source or destination.

    A subclass implements `open_text`, the text stream the transfer reads
    (``"r"``, an upload) or writes (``"w"``, a download); ``open`` presents it
    as bytes in ``encoding`` (default utf-8): encoded as it is read, decoded as
    it is written, with a codec of its own each call, so a stateful encoding
    (utf-16's BOM) starts afresh for every transfer. The transfer closes what
    `open_text` returns once the item settles, as it closes what any
    ``Storage.open`` returns: a stream `open_text` opens itself is closed for
    it, and one it must not close goes back behind a view whose ``close`` only
    flushes, as `TextIOStorage` returns the caller's.

    The stream is read and written in order (neither adapter seeks), so an
    upload takes the engines' buffered path and a download lands in order.
    Text that does not convert fails the transfer, with the
    ``UnicodeDecodeError`` / ``UnicodeEncodeError`` on the item's error.

    ``encoding`` is checked at construction (``codecs.lookup``): an unknown name
    is an ``InvalidValueError`` here rather than a bare ``LookupError`` from the
    first ``open`` - which the eager stream route of ``cp`` would let escape
    the library's taxonomy while the lazy open route of ``mv`` reported it as
    the item's failure.
    """

    def __init__(self, *, encoding: str = "utf-8") -> None:
        try:
            codecs.lookup(encoding)
        except LookupError as exc:
            raise InvalidValueError(str(exc)) from exc
        self._encoding = encoding

    @abc.abstractmethod
    def open_text(self, key: str, mode: Literal["r", "w"]) -> TextIO:
        """The text stream to read for ``"r"`` or write for ``"w"``.

        ``key`` is ``open``'s, which carries nothing for a single endpoint.
        """

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        """`open_text`'s stream as bytes in ``encoding`` (``size`` ignored)."""
        if mode == "rb":
            return cast("BinaryIO", _EncodingReader(self.open_text(key, "r"), self._encoding))
        return cast("BinaryIO", _DecodingWriter(self.open_text(key, "w"), self._encoding))


class TextIOStorage(TextIOStorageBase):
    """A text stream you hand over, as a stream endpoint (`TextIOStorageBase`).

    ``cp(TextIOStorage(io.StringIO("hello")), "s3://b/k")`` uploads the text
    encoded in ``encoding`` (default utf-8), and
    ``cp("s3://b/k", TextIOStorage(sink))`` decodes the object into ``sink``.
    The stream trades in ``str``; a binary stream goes through `IOStorage`.
    One whose class makes it binary - an ``io.RawIOBase`` /
    ``io.BufferedIOBase``: a ``BytesIO``, a file opened ``"rb"`` / ``"wb"``,
    ``sys.stdout.buffer`` - is refused here with ``ValidationError``; no other
    stream is checked (a wrong kind is a wrong type, overview section 3).

    A text stream is never swapped for its ``.buffer``: text it has already
    read ahead would be skipped, and the encoding it was opened with ignored.
    The caller's stream is never closed by this class: `open_text` returns it
    behind a view whose ``close`` only flushes.
    """

    def __init__(
        self, stream: SupportsRead[str] | SupportsWrite[str], *, encoding: str = "utf-8"
    ) -> None:
        if isinstance(stream, (io.RawIOBase, io.BufferedIOBase)):
            raise ValidationError(
                "TextIOStorage takes a text stream; use IOStorage for a binary stream"
            )
        super().__init__(encoding=encoding)
        self._stream = stream

    @override
    def open_text(self, key: str, mode: Literal["r", "w"]) -> TextIO:
        """The caller's stream behind a view whose ``close`` only flushes (``key`` ignored)."""
        return cast("TextIO", _Uncloseable(self._stream))


__all__ = ["IOStorage", "IOStorageBase", "StdioStorage", "TextIOStorage", "TextIOStorageBase"]
