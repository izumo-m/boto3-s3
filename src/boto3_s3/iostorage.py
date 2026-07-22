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
(download). The transfer ``close``s every fileobj ``open`` returns (the open
route flushes a real backend's writer that way), so each view here absorbs that
``close`` into a flush: the caller's stream is **never closed** by ``IOStorage``
(it owns only the thin view / codec adapter).

``StdioStorage`` is the convenience for the process's stdio: as a source it reads
``sys.stdin`` (forced non-seekable, so s3transfer takes its buffered upload path -
Windows stdin reports a false ``seekable()``), as a destination it writes
``sys.stdout``; both via ``.buffer`` (binary).

Like its peers it is imported by submodule path and imports no AWS SDK module, so
``import boto3_s3.iostorage`` stays SDK-free.
"""

from __future__ import annotations

import codecs
import io
import sys
from typing import IO, TYPE_CHECKING, Any, ClassVar, Literal, cast

from typing_extensions import override

from boto3_s3.exceptions import ValidationError
from boto3_s3.storage import Storage, StorageCapability

if TYPE_CHECKING:
    from typing import BinaryIO


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
    does too. ``close`` flushes and leaves the process stream open
    (``_Uncloseable``'s contract).
    """

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def write(self, data: Any) -> int:
        return self._raw.write(data)

    def flush(self) -> None:
        flush = getattr(self._raw, "flush", None)
        if flush is not None:
            flush()

    def close(self) -> None:
        self.flush()


class _NonSeekable:
    """Read-only binary view that hides ``seek`` (aws-cli's ``NonSeekableStream``).

    Some streams that are not truly seekable still report ``seekable() == True``
    (Windows stdin); exposing only ``read`` forces s3transfer's buffered
    non-seekable upload path.
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
    (`_Uncloseable`); a text stream - recognized as an
    ``io.TextIOBase`` or by its ``encoding`` attribute (``codecs.open``'s
    ``StreamReaderWriter``, a text-mode ``SpooledTemporaryFile``) - is wrapped
    with ``encoding`` (default utf-8). The caller's stream is never closed by
    this class. As a single endpoint it has no listing: ``scan_pages`` /
    ``delete`` / ``get_fileinfo`` raise.

    ``capabilities`` is just the ``OPEN_*`` pair: a single stream supports only byte
    I/O (both directions, chosen per ``open`` call), with no listing or deletion.
    """

    scheme: ClassVar[str] = "stream"
    capabilities: ClassVar[StorageCapability] = (
        StorageCapability.OPEN_READ | StorageCapability.OPEN_WRITE
    )

    def __init__(self, stream: IO[bytes] | IO[str], *, encoding: str = "utf-8") -> None:
        self._stream: IO[Any] | None = stream
        self._encoding = encoding

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        """Return the wrapped stream as a binary stream (``key`` / ``size`` ignored).

        A single endpoint takes no key. A text stream is adapted to bytes via the
        configured ``encoding`` (encode on ``"rb"``, decode on ``"wb"``).
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
    a seekable file. The stream is chosen
    by ``mode`` at ``open`` time, so a single instance serves either direction and
    picks up a redirected ``sys.stdin`` / ``sys.stdout``. If the selected process
    stream is unavailable, ``open`` raises ``ValidationError`` before a transfer
    worker can receive an unusable file object.
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
                # ValidationError; raised in-pipeline, rc 1 either way.
                raise ValidationError(
                    "stdin is required for this operation, but is not available.",
                    operation="cp",
                )
            return cast("BinaryIO", _NonSeekable(getattr(stdin, "buffer", stdin)))
        stdout = sys.stdout
        if stdout is None:
            raise ValidationError(
                "stdout is required for this operation, but is not available.",
                operation="cp",
            )
        return cast("BinaryIO", _WriteOnly(getattr(stdout, "buffer", stdout)))


__all__ = ["IOStorage", "StdioStorage"]
