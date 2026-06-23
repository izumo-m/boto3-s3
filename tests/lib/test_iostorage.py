"""``IOStorage`` / ``StdioStorage``: a single stream presented as a ``Storage``.

Unit-level coverage of the ``open`` contract (binary pass-through, text
encode/decode, the caller's stream left open), the unsupported container
operations, and ``StdioStorage``'s mode-driven choice of stdin / stdout.
"""

from __future__ import annotations

import io

import pytest

from boto3_s3.exceptions import Boto3S3Error
from boto3_s3.iostorage import IOStorage, StdioStorage
from boto3_s3.types import ScanOptions


class TestBinaryPassthrough:
    def test_open_returns_the_stream_unwrapped(self) -> None:
        # A binary stream is handed to s3transfer as-is, preserving its own
        # seekability (BytesIO is seekable -> multipart / retry stay available).
        buf = io.BytesIO(b"data")
        store = IOStorage(buf)
        assert store.open("k", "rb") is buf
        assert store.open("k", "wb") is buf

    def test_key_and_size_are_ignored(self) -> None:
        buf = io.BytesIO(b"x")
        assert IOStorage(buf).open("anything", "rb", size=999) is buf


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
            IOStorage(io.BytesIO()).delete("k")


class _Stdio:
    """A minimal stdin/stdout stand-in exposing a binary ``.buffer``."""

    def __init__(self, payload: bytes = b"") -> None:
        self.buffer = io.BytesIO(payload)


class TestStdioStorage:
    def test_write_picks_stdout_buffer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stdout = _Stdio()
        monkeypatch.setattr("sys.stdout", stdout)
        writer = StdioStorage().open("k", "wb")
        assert writer is stdout.buffer

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
        with pytest.raises(Boto3S3Error, match="stdin is required"):
            StdioStorage().open("k", "rb")
