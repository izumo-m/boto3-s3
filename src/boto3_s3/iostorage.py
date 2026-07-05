"""``boto3_s3.iostorage``: a single in-hand stream presented as a ``Storage``.

``IOStorage`` adapts one caller-supplied file-like object into the ``Storage``
contract so it can be one side of a ``cp`` transfer - the building block behind
``cp(s3_uri, IOStorage(buf))`` / ``cp(IOStorage(buf), s3_uri)``. It is a single
endpoint, not a container: only :meth:`~IOStorage.open` is meaningful;
``scan_pages`` / ``delete`` raise. The S3 side still rides ``s3transfer`` off its
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
    from collections.abc import Callable, Iterator, Sequence
    from typing import BinaryIO

    from boto3_s3.types import FileInfo, ScanOptions

_NOT_A_CONTAINER = (
    "IOStorage wraps a single stream and supports open() only, not scan / delete / "
    "get_fileinfo (pass a path or an s3:// URI for ls / rm / recursive transfers)."
)

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
    """``str`` source presented as a non-seekable binary reader (encode on read)."""

    def __init__(self, text: IO[str], encoding: str) -> None:
        self._text = text
        self._encoding = encoding
        self._buf = b""

    def read(self, amt: int | None = None) -> bytes:
        if amt is None or amt < 0:
            out = self._buf + self._text.read().encode(self._encoding)
            self._buf = b""
            return out
        while len(self._buf) < amt:
            chunk = self._text.read(_READ_CHUNK)
            if not chunk:
                break
            self._buf += chunk.encode(self._encoding)
        out, self._buf = self._buf[:amt], self._buf[amt:]
        return out

    def close(self) -> None:
        # A reader over the caller's text stream: nothing to release, and the
        # caller's stream is never closed by IOStorage.
        pass


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
    A binary stream is used as-is; a text stream is wrapped with ``encoding``
    (default utf-8). The caller's stream is never closed by this class. As a single
    endpoint it has no listing: :meth:`scan_pages` / :meth:`delete` raise.
    """

    scheme: ClassVar[str] = "stdio"
    #: A single stream supports only byte I/O (both directions, chosen per
    #: ``open`` call); it has no listing or deletion, so just the ``OPEN_*`` pair.
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
        if isinstance(stream, io.TextIOBase):
            adapter = (
                _EncodingReader(stream, self._encoding)
                if mode == "rb"
                else _DecodingWriter(stream, self._encoding)
            )
            return cast("BinaryIO", adapter)
        return cast("BinaryIO", _Uncloseable(stream))

    @override
    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
        raise NotImplementedError(_NOT_A_CONTAINER)

    @override
    def delete(self, info: FileInfo) -> None:
        raise NotImplementedError(_NOT_A_CONTAINER)

    @override
    def get_fileinfo(
        self,
        key: str = "",
        *,
        follow_symlinks: bool = True,
        on_warning: Callable[[str], None] | None = None,
    ) -> FileInfo | None:
        raise NotImplementedError(_NOT_A_CONTAINER)

    @override
    def as_text(self) -> str:
        """Return the stdio token ``"-"`` (:meth:`Storage.as_text`, display-only).

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
    destination ``open("wb")`` writes ``sys.stdout.buffer``. The stream is chosen
    by ``mode`` at ``open`` time, so a single instance serves either direction and
    picks up a redirected ``sys.stdin`` / ``sys.stdout``.
    """

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
        return cast("BinaryIO", _Uncloseable(getattr(sys.stdout, "buffer", sys.stdout)))


__all__ = ["IOStorage", "StdioStorage"]
