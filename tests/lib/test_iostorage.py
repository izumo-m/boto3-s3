"""``IOStorage`` / ``StdioStorage``: a single stream presented as a ``Storage``.

Unit-level coverage of the ``open`` contract (binary pass-through, text
encode/decode, the caller's stream left open), the unsupported container
operations, ``StdioStorage``'s mode-driven choice of stdin / stdout, and what a
stdout that cannot take the bytes does to a streaming download.
"""

from __future__ import annotations

import errno
import io
import tempfile
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3.exceptions import BatchError, Boto3S3Error, ValidationError
from boto3_s3.iostorage import IOStorage, StdioStorage
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import FileInfo, OpOutcome, OpResult, ScanOptions
from tests.utils.fakes3 import get_response, head_response
from tests.utils.recorder import make_recording_client

_SYNC = TransferConfig(use_threads=False)


class TestBinaryPassthrough:
    def test_open_presents_a_close_suppressing_view(self) -> None:
        # A binary stream reaches s3transfer through a view that delegates I/O
        # and seekability (BytesIO is seekable -> multipart / retry stay
        # available) but whose close() never closes the caller's stream - the
        # transfer closes every fileobj open() returns (transfer._CloseFileobj).
        buf = io.BytesIO(b"data")
        reader = IOStorage(buf).open("k", "rb")
        assert reader is not buf
        assert reader.read() == b"data"
        assert reader.seekable()
        reader.close()
        assert not buf.closed

    def test_write_view_does_not_close_the_caller_stream(self) -> None:
        buf = io.BytesIO()
        writer = IOStorage(buf).open("k", "wb")
        writer.write(b"ab")
        writer.close()
        assert not buf.closed
        assert buf.getvalue() == b"ab"

    def test_key_and_size_are_ignored(self) -> None:
        buf = io.BytesIO(b"x")
        assert IOStorage(buf).open("anything", "rb", size=999).read() == b"x"


class TestTextAdapter:
    def test_encodes_on_read(self) -> None:
        reader = IOStorage(io.StringIO("héllo")).open("k", "rb")
        assert reader.read() == "héllo".encode()

    def test_encodes_on_read_in_chunks(self) -> None:
        reader = IOStorage(io.StringIO("abcdef")).open("k", "rb")
        assert reader.read(3) + reader.read(3) == b"abcdef"

    def test_decodes_on_write(self) -> None:
        sink = io.StringIO()
        writer = IOStorage(sink).open("k", "wb")
        writer.write("café".encode())
        assert sink.getvalue() == "café"

    def test_custom_encoding(self) -> None:
        reader = IOStorage(io.StringIO("café"), encoding="latin-1").open("k", "rb")
        assert reader.read() == "café".encode("latin-1")

    def test_stateful_codec_streams_as_one_encoder_across_chunks(self) -> None:
        # utf-16 prefixes a BOM and fixes its endianness once. A per-chunk
        # str.encode would re-emit the BOM on every read and corrupt the upload;
        # one incremental encoder spans every read, so the chunked bytes equal a
        # single encode of the whole string (BOM once) and round-trip.
        text = "a" * 70000  # exceeds _READ_CHUNK, so read() loops over chunks
        reader = IOStorage(io.StringIO(text), encoding="utf-16").open("k", "rb")
        data = b""
        while chunk := reader.read(8192):
            data += chunk
        assert data == text.encode("utf-16")
        assert data.decode("utf-16") == text

    def test_stateful_codec_read_to_eof_matches_one_encode(self) -> None:
        # The amt=None path (read to EOF) is the same single-encoder stream.
        text = "smørrebrød"
        reader = IOStorage(io.StringIO(text), encoding="utf-16").open("k", "rb")
        assert reader.read() == text.encode("utf-16")

    def test_non_textiobase_text_stream_is_wrapped(self) -> None:
        # The constructor accepts any IO[str], but a text-mode
        # SpooledTemporaryFile (like codecs.open's StreamReaderWriter) reads
        # str without deriving from io.TextIOBase. Recognized by its encoding
        # attribute, it is still adapted to bytes rather than passed through raw.
        with tempfile.SpooledTemporaryFile(max_size=1024, mode="w+", encoding="utf-8") as sp:
            sp.write("café")
            sp.seek(0)
            reader = IOStorage(sp).open("k", "rb")
            assert reader.read() == "café".encode()

    def test_write_adapter_does_not_close_the_caller_stream(self) -> None:
        sink = io.StringIO()
        writer = IOStorage(sink).open("k", "wb")
        writer.write(b"ab")
        writer.close()
        assert not sink.closed
        assert sink.getvalue() == "ab"


class TestCallerStreamPosition:
    def test_download_leaves_the_stream_unrewound_at_the_write_end(self) -> None:
        # design/storage.md: the caller's stream is neither closed nor rewound -
        # after a download it sits at the end of the written bytes, so the
        # caller can keep appending (or must seek(0) themselves to read back).
        buf = io.BytesIO()
        client, _ = make_recording_client([head_response(), get_response()])
        S3().cp(
            S3Storage("s3://b/d/a.txt", client=client),
            IOStorage(buf),
            transfer_config=TransferConfig(use_threads=False),
        )
        assert not buf.closed
        assert buf.tell() == len(b"payload")
        assert buf.getvalue() == b"payload"


class TestUnsupportedContainerOps:
    def test_scan_pages_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            IOStorage(io.BytesIO()).scan_pages(ScanOptions())

    def test_delete_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            IOStorage(io.BytesIO()).delete(FileInfo(key="k"))

    def test_get_fileinfo_raises(self) -> None:
        with pytest.raises(NotImplementedError):
            IOStorage(io.BytesIO()).get_fileinfo()


class _Stdio:
    """A minimal stdin/stdout stand-in exposing a binary ``.buffer``."""

    def __init__(self, payload: bytes = b"") -> None:
        self.buffer = io.BytesIO(payload)


class _RecordingBuffer(io.BytesIO):
    """A ``.buffer`` that records the ``flush`` / ``close`` calls it receives."""

    def __init__(self) -> None:
        super().__init__()
        self.flushes = 0
        self.closes = 0

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.closes += 1


class _FailingBuffer(io.BytesIO):
    """A ``.buffer`` that raises ENOSPC on ``write``, on ``flush``, or on both."""

    def __init__(self, *, on_write: bool = False, on_flush: bool = False) -> None:
        super().__init__()
        self._on_write = on_write
        self._on_flush = on_flush
        self.flushes = 0

    @staticmethod
    def _enospc() -> OSError:
        return OSError(errno.ENOSPC, "No space left on device")

    def write(self, data: Any) -> int:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self._on_write:
            raise self._enospc()
        return super().write(data)

    def flush(self) -> None:
        self.flushes += 1
        if self._on_flush:
            raise self._enospc()


class _BufferStdio:
    """A stdout stand-in wrapping a prepared ``.buffer``."""

    def __init__(self, buffer: io.BytesIO) -> None:
        self.buffer = buffer


class TestStdioStorage:
    def test_write_picks_stdout_buffer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = _Stdio()
        monkeypatch.setattr("sys.stdout", stdout)
        writer = StdioStorage().open("k", "wb")
        # A close-suppressing view over stdout.buffer: writes reach it, but the
        # transfer's close() must not close the process's stdout.
        writer.write(b"hi")
        writer.close()
        assert stdout.buffer.getvalue() == b"hi"
        assert not stdout.buffer.closed

    def test_write_view_is_write_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli's StdoutBytesWriter shape: exposing only write forces
        # s3transfer's non-seekable download path, which orders ranged chunks
        # before writing. A redirected stdout can report seekable (a regular
        # file) while `>>` opened it O_APPEND - every write then lands at the
        # end regardless of the seeked position, so a seek-based parallel
        # download would interleave chunks in completion order.
        stdout = _Stdio()
        assert stdout.buffer.seekable()  # the hazard: the raw stream IS seekable
        monkeypatch.setattr("sys.stdout", stdout)
        writer = StdioStorage().open("k", "wb")
        assert not hasattr(writer, "seek")
        assert not hasattr(writer, "seekable")

    def test_read_picks_stdin_buffer_forced_non_seekable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", _Stdio(b"hello"))
        reader = StdioStorage().open("k", "rb")
        assert reader.read() == b"hello"
        # Hiding seek forces s3transfer's buffered non-seekable upload path.
        assert not hasattr(reader, "seek")

    def test_read_without_stdin_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", None)
        # ValidationError: a runtime-state precondition (exceptions.md section 3).
        with pytest.raises(ValidationError, match="stdin is required") as excinfo:
            StdioStorage().open("k", "rb")
        # A directly invoked storage method names no operation: the storage
        # cannot know which one (cp streams, mv moves onto a stream), so an
        # operation-driven run stamps its own name instead.
        assert excinfo.value.operation is None

    def test_write_reads_stdout_per_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws's bytes_print dereferences sys.stdout on every write, so nothing
        # is captured at open time: a stdout swapped after the open (a
        # redirect_stdout around a library call) receives the bytes.
        first, second = _Stdio(), _Stdio()
        monkeypatch.setattr("sys.stdout", first)
        writer = StdioStorage().open("k", "wb")
        monkeypatch.setattr("sys.stdout", second)
        writer.write(b"hi")
        assert (first.buffer.getvalue(), second.buffer.getvalue()) == (b"", b"hi")

    def test_write_without_stdout_fails_on_the_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws has no stdout precondition (get_binary_stdout does not check,
        # unlike get_binary_stdin), and its writer dereferences the process
        # stream per write: the open succeeds and the missing stream surfaces as
        # the write's own AttributeError, whose text aws prints verbatim in the
        # item's failure line (measured: `aws s3 cp s3://b/k - 1>&-` reports
        # "download failed: s3://b/k to - 'NoneType' object has no attribute
        # 'write'").
        monkeypatch.setattr("sys.stdout", None)
        writer = StdioStorage().open("k", "wb")
        with pytest.raises(AttributeError) as excinfo:
            writer.write(b"hi")
        assert str(excinfo.value) == "'NoneType' object has no attribute 'write'"

    def test_close_neither_flushes_nor_closes_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The writer aws hands s3transfer has no close and no flush, and a
        # non-seekable download's final task is a no-op: whatever the process
        # stream buffered is the interpreter's to flush at exit. Flushing here
        # instead would turn a stdout that cannot take the bytes into a per-item
        # transfer failure, where aws lets the process's own shutdown flush fail.
        buffer = _RecordingBuffer()
        monkeypatch.setattr("sys.stdout", _BufferStdio(buffer))
        writer = StdioStorage().open("k", "wb")
        writer.write(b"hi")
        writer.close()
        assert (buffer.flushes, buffer.closes) == (0, 0)
        assert buffer.getvalue() == b"hi"


class TestStreamDownloadWithABrokenStdout:
    """Which stdout failures a streaming download owns, and which the process does.

    aws's stdout writer only writes: the transfer therefore fails an item when a
    *write* fails, and never learns about anything that only surfaces when the
    process stream's buffer is flushed - measured on the pinned aws, where a
    stdout of ``/dev/full`` gives one ``download failed: ... [Errno 28] No space
    left on device`` (rc 1) for an object too big for the buffer and a silent
    exit status 120 for one that fits.
    """

    @staticmethod
    def _download(storage: StdioStorage, results: list[OpResult]) -> None:
        client, _calls = make_recording_client([head_response(), get_response()])
        S3().cp(
            S3Storage("s3://b/d/a.txt", client=client),
            storage,
            transfer_config=_SYNC,
            on_result=results.append,
        )

    def test_a_flush_that_would_fail_is_never_attempted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        buffer = _FailingBuffer(on_flush=True)
        monkeypatch.setattr("sys.stdout", _BufferStdio(buffer))
        results: list[OpResult] = []
        self._download(StdioStorage(), results)
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert buffer.getvalue() == b"payload"
        assert buffer.flushes == 0

    def test_a_failing_write_fails_the_item(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdout", _BufferStdio(_FailingBuffer(on_write=True)))
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            self._download(StdioStorage(), results)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        # The text aws's own failure line carries for this errno.
        assert str(error) == "[Errno 28] No space left on device"
        assert isinstance(error, Boto3S3Error)
        assert isinstance(error.__cause__, OSError)

    def test_no_stdout_at_all_fails_the_item(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws checks stdin before starting a transfer but never stdout, so this
        # is a per-item failure rather than a run-killing precondition: the
        # failure line reads "download failed: <src> to - 'NoneType' object has
        # no attribute 'write'" (measured, rc 1).
        monkeypatch.setattr("sys.stdout", None)
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            self._download(StdioStorage(), results)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert isinstance(error, Boto3S3Error)
        assert str(error) == "'NoneType' object has no attribute 'write'"
        # The failing write is what the item recorded: an in-pipeline failure
        # carrying the run's own operation and the item's coordinates, not a
        # pre-flight ValidationError raised before anything was submitted.
        assert isinstance(error.__cause__, AttributeError)
        assert (error.operation, error.bucket, error.key) == ("cp", "b", "d/a.txt")
