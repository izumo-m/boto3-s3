"""``IOStorage`` / ``StdioStorage``: a single stream presented as a ``Storage``.

Unit-level coverage of the ``open`` contract (binary pass-through, text
encode/decode, the caller's stream left open), the unsupported container
operations, and ``StdioStorage``'s mode-driven choice of stdin / stdout.
"""

from __future__ import annotations

import io
import tempfile

import pytest

from boto3_s3.exceptions import ValidationError
from boto3_s3.iostorage import IOStorage, StdioStorage
from boto3_s3.types import FileInfo, ScanOptions


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
        with pytest.raises(ValidationError, match="stdin is required"):
            StdioStorage().open("k", "rb")
