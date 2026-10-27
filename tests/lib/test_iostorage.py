"""The stream storages: a single stream presented as a ``Storage``.

Unit-level coverage of the ``open`` contract (``IOStorage``'s binary
pass-through, ``TextIOStorage``'s encode / decode, the caller's stream left
open), the bases a stream storage of your own derives from (``IOStorageBase``,
``TextIOStorageBase`` and the ``open_text`` stream the transfer closes), the
streams each class refuses, the unsupported container operations,
``StdioStorage``'s mode-driven choice of stdin / stdout, and what a stdout that
cannot take the bytes does to a streaming download.
"""

from __future__ import annotations

import errno
import io
import os
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Literal, TextIO

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3.exceptions import BatchError, Boto3S3Error, InvalidValueError, ValidationError
from boto3_s3.iostorage import (
    IOStorage,
    IOStorageBase,
    StdioStorage,
    TextIOStorage,
    TextIOStorageBase,
)
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import FileInfo, OpOutcome, OpResult, ScanOptions
from tests.utils.fakes3 import client_error, get_response, head_response
from tests.utils.recorder import make_recording_client, ops

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


class _ClaimsSeekableWriter:
    """A pipe-backed writer that reports ``seekable()`` True, like a Windows pipe."""

    def __init__(self, fd: int) -> None:
        self._raw = os.fdopen(fd, "wb")

    def write(self, data: bytes) -> int:
        return self._raw.write(data)

    def flush(self) -> None:
        self._raw.flush()

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        return 0

    def tell(self) -> int:
        return 0

    def fileno(self) -> int:
        return self._raw.fileno()

    def close(self) -> None:
        self._raw.close()


class _ClaimsSeekable:
    """A pipe-backed reader that reports ``seekable()`` True, like a Windows pipe."""

    def __init__(self, fd: int) -> None:
        self._raw = os.fdopen(fd, "rb")

    def read(self, amt: int | None = None) -> bytes:
        return self._raw.read() if amt is None else self._raw.read(amt)

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        return 0

    def tell(self) -> int:
        return 0

    def fileno(self) -> int:
        return self._raw.fileno()

    def close(self) -> None:
        self._raw.close()


class TestNonSeekableSource:
    """A source that cannot be positioned is read through a read-only view.

    The CRT lane hands the fileobj to botocore, whose content-length probe
    calls ``tell`` / ``seek`` whenever both exist: a POSIX pipe answers
    ``OSError(ESPIPE)`` (the upload failed ``[Errno 29] Illegal seek``), and a
    Windows pipe answers with meaningless positions and stored a truncated
    object as a success. Hiding everything but ``read`` - stdin's view, aws-cli's
    ``NonSeekableStream`` - sends both engines down their buffered path.
    """

    def test_a_pipe_gets_the_read_only_view(self) -> None:
        r, w = os.pipe()
        os.write(w, b"piped")
        os.close(w)
        with os.fdopen(r, "rb") as pipe:
            reader = IOStorage(pipe).open("k", "rb")
            assert not hasattr(reader, "seek")
            assert not hasattr(reader, "tell")
            assert reader.read() == b"piped"
            reader.close()
            assert not pipe.closed

    def test_a_fifo_that_claims_seekable_still_gets_the_view(self) -> None:
        # Windows pipes report seekable() True; the descriptor's type decides.
        r, w = os.pipe()
        os.write(w, b"claimed")
        os.close(w)
        stream = _ClaimsSeekable(r)
        try:
            reader = IOStorage(stream).open("k", "rb")  # pyright: ignore[reportArgumentType]
            assert not hasattr(reader, "seek")
            assert reader.read() == b"claimed"
        finally:
            stream.close()

    def test_a_regular_file_keeps_the_pass_through_view(self, tmp_path: Path) -> None:
        path = tmp_path / "f.bin"
        path.write_bytes(b"regular")
        with path.open("rb") as handle:
            reader = IOStorage(handle).open("k", "rb")
            assert reader.seekable()
            assert reader.tell() == 0

    def test_a_memory_stream_keeps_the_pass_through_view(self) -> None:
        # BytesIO: seekable, and fileno() raises UnsupportedOperation.
        reader = IOStorage(io.BytesIO(b"mem")).open("k", "rb")
        assert reader.seekable()

    def test_a_closed_stream_is_left_to_the_transfer(self) -> None:
        # seekable() raises ValueError on a closed stream: a probe that cannot
        # answer adds no failure and no view of its own, so the transfer meets
        # the stream as it would without the probe.
        buf = io.BytesIO(b"x")
        buf.close()
        reader = IOStorage(buf).open("k", "rb")
        assert hasattr(reader, "seek")

    def test_a_tarfile_member_keeps_the_pass_through_view(self, tmp_path: Path) -> None:
        # A tarfile member reads through a BufferedReader whose `fileno` is
        # forwarded to a wrapped object that has none (AttributeError) while
        # its `seekable` answers True: it uploaded on both engines before the
        # check existed and must still reach the engine untouched.
        import tarfile

        payload = os.urandom(70000)
        archive = tmp_path / "a.tar"
        with tarfile.open(archive, "w") as tar:
            info = tarfile.TarInfo("member.bin")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        with tarfile.open(archive) as tar:
            member = tar.extractfile("member.bin")
            assert member is not None
            with pytest.raises(AttributeError):
                member.fileno()
            reader = IOStorage(member).open("k", "rb")
            assert hasattr(reader, "seek")
            assert reader.read() == payload

    def test_a_download_into_a_gzip_over_a_write_only_sink_succeeds(self) -> None:
        # The append cue asks a destination for its descriptor: a GzipFile
        # over a write-only sink forwards `fileno` to an object that has none
        # (AttributeError), which escaped `open` and failed the cp before any
        # byte landed - the sibling of the upload-side check.
        import gzip

        class _Sink:
            def __init__(self) -> None:
                self.chunks: list[bytes] = []

            def write(self, data: bytes) -> int:
                self.chunks.append(bytes(data))
                return len(data)

            def flush(self) -> None:
                pass

        sink = _Sink()
        with gzip.GzipFile(fileobj=sink, mode="wb") as gz:  # pyright: ignore[reportArgumentType]
            with pytest.raises(AttributeError):
                gz.fileno()
            client, _ = make_recording_client([head_response(), get_response()])
            results: list[OpResult] = []
            S3().cp(
                S3Storage("s3://b/d/a.txt", client=client),
                IOStorage(gz),
                transfer_config=_SYNC,
                on_result=results.append,
            )
            assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]
        assert gzip.decompress(b"".join(sink.chunks)) == b"payload"

    def test_a_tarfile_member_uploads_on_the_classic_engine(self, tmp_path: Path) -> None:
        import tarfile

        archive = tmp_path / "a.tar"
        with tarfile.open(archive, "w") as tar:
            info = tarfile.TarInfo("member.bin")
            info.size = 5
            tar.addfile(info, io.BytesIO(b"tarry"))
        client, calls = make_recording_client([{"ETag": '"e"'}])
        results: list[OpResult] = []
        with tarfile.open(archive) as tar:
            member = tar.extractfile("member.bin")
            assert member is not None
            S3().cp(
                IOStorage(member),
                S3Storage("s3://b/k", client=client),
                transfer_config=_SYNC,
                on_result=results.append,
            )
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]
        assert [c.operation for c in calls] == ["PutObject"]

    def test_a_pipe_uploads_on_the_classic_engine(self) -> None:
        r, w = os.pipe()
        os.write(w, b"from a pipe")
        os.close(w)
        client, calls = make_recording_client(
            [{"ETag": '"e"'}]  # one PutObject
        )
        results: list[OpResult] = []
        with os.fdopen(r, "rb") as pipe:
            S3().cp(
                IOStorage(pipe),
                S3Storage("s3://b/k", client=client),
                transfer_config=_SYNC,
                on_result=results.append,
            )
        # The recorder never reads the body, so no bytes are counted; the
        # outcome and the single PutObject are the point (the CRT lane's
        # behaviour needs a live engine: tests/cli/e2e).
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]
        assert [c.operation for c in calls] == ["PutObject"]


class TestStreamDestinations:
    """A download writes every stream destination in order, opened by its first byte.

    ``cp`` and ``mv`` hand the destination to the engine through the open
    route's deferred writer, which offers ``write`` only: the engine writes in
    order from where the stream stands, whatever the stream claims - an
    append-mode file (whose positioned writes would land in completion order;
    measured: a 40 MiB object into ``open(p, "ab")`` came back scrambled), a
    pipe that answers ``seekable()`` True, a stream already holding bytes.
    """

    @staticmethod
    def _download(dest: IOStorageBase) -> list[OpResult]:
        client, _calls = make_recording_client([head_response(), get_response()])
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/d/a.txt", client=client),
            dest,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        return results

    def test_a_download_lands_after_the_existing_bytes(self, tmp_path: Path) -> None:
        target = tmp_path / "out.bin"
        target.write_bytes(b"HEAD")
        with target.open("ab") as fh:
            self._download(IOStorage(fh))
            assert not fh.closed
        assert target.read_bytes() == b"HEADpayload"

    def test_a_download_starts_where_the_stream_stands(self) -> None:
        # Positioned writes would put the object at offset 0, over "HEAD".
        buf = io.BytesIO()
        buf.write(b"HEAD")
        self._download(IOStorage(buf))
        assert buf.getvalue() == b"HEADpayload"

    def test_a_pipe_that_claims_seekable_is_written_in_order(self) -> None:
        r, w = os.pipe()
        stream = _ClaimsSeekableWriter(w)
        try:
            results = self._download(IOStorage(stream))  # pyright: ignore[reportArgumentType]
        finally:
            stream.close()
        with os.fdopen(r, "rb") as pipe:
            assert pipe.read() == b"payload"
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]

    def test_a_missing_object_leaves_the_destination_unopened(self) -> None:
        notes = _Notes()
        client, _calls = make_recording_client([client_error("404", 404, "HeadObject")])
        with pytest.raises(BatchError):
            S3().cp(S3Storage("s3://b/d/a.txt", client=client), notes, transfer_config=_SYNC)
        assert notes.opened == []


class TestTextIOStorage:
    def test_encodes_on_read(self) -> None:
        reader = TextIOStorage(io.StringIO("héllo")).open("k", "rb")
        assert reader.read() == "héllo".encode()

    def test_encodes_on_read_in_chunks(self) -> None:
        reader = TextIOStorage(io.StringIO("abcdef")).open("k", "rb")
        assert reader.read(3) + reader.read(3) == b"abcdef"

    def test_a_read_smaller_than_a_character_keeps_the_rest(self) -> None:
        # One character encodes to several bytes: what a read leaves over is
        # the next read's start.
        reader = TextIOStorage(io.StringIO("héllo")).open("k", "rb")
        data = b""
        while chunk := reader.read(1):
            data += chunk
        assert data == "héllo".encode()

    def test_decodes_on_write(self) -> None:
        sink = io.StringIO()
        writer = TextIOStorage(sink).open("k", "wb")
        writer.write("café".encode())
        writer.close()
        assert sink.getvalue() == "café"

    def test_an_unknown_encoding_is_rejected_at_construction(self) -> None:
        # Before this check the name reached codecs only from the first open:
        # cp's eager stream route let the bare LookupError escape the taxonomy
        # while mv's lazy open route reported it as the item's failure.
        with pytest.raises(InvalidValueError, match="nope") as excinfo:
            TextIOStorage(io.StringIO(), encoding="nope")
        assert isinstance(excinfo.value.__cause__, LookupError)
        with pytest.raises(InvalidValueError):
            TextIOStorage(io.StringIO(), encoding="")

    def test_custom_encoding(self) -> None:
        reader = TextIOStorage(io.StringIO("café"), encoding="latin-1").open("k", "rb")
        assert reader.read() == "café".encode("latin-1")

    def test_stateful_codec_streams_as_one_encoder_across_chunks(self) -> None:
        # utf-16 prefixes a BOM and fixes its endianness once. A per-chunk
        # str.encode would re-emit the BOM on every read and corrupt the upload;
        # one incremental encoder spans every read, so the chunked bytes equal a
        # single encode of the whole string (BOM once) and round-trip.
        text = "a" * 70000  # exceeds _READ_CHUNK, so read() loops over chunks
        reader = TextIOStorage(io.StringIO(text), encoding="utf-16").open("k", "rb")
        data = b""
        while chunk := reader.read(8192):
            data += chunk
        assert data == text.encode("utf-16")
        assert data.decode("utf-16") == text

    def test_stateful_codec_read_to_eof_matches_one_encode(self) -> None:
        # The amt=None path (read to EOF) is the same single-encoder stream.
        text = "smørrebrød"
        reader = TextIOStorage(io.StringIO(text), encoding="utf-16").open("k", "rb")
        assert reader.read() == text.encode("utf-16")

    def test_a_text_stream_of_its_own_kind_is_carried(self) -> None:
        # Neither an io.TextIOBase nor carrying `encoding`: a stream the old
        # guess took for binary. Declared as text, it is carried as text.
        class _Text:
            def __init__(self, text: str) -> None:
                self._source = io.StringIO(text)

            def read(self, amt: int = -1) -> str:
                return self._source.read(amt)

        reader = TextIOStorage(_Text("héllo")).open("k", "rb")
        assert reader.read() == "héllo".encode()

    def test_a_text_mode_spooled_temporary_file_is_carried(self) -> None:
        with tempfile.SpooledTemporaryFile(max_size=1024, mode="w+", encoding="utf-8") as sp:
            sp.write("café")
            sp.seek(0)
            reader = TextIOStorage(sp).open("k", "rb")
            assert reader.read() == "café".encode()

    def test_neither_adapter_closes_the_caller_stream(self) -> None:
        sink = io.StringIO()
        writer = TextIOStorage(sink).open("k", "wb")
        writer.write(b"ab")
        writer.close()
        assert not sink.closed
        assert sink.getvalue() == "ab"
        source = io.StringIO("ab")
        reader = TextIOStorage(source).open("k", "rb")
        assert reader.read() == b"ab"
        reader.close()
        assert not source.closed

    def test_neither_adapter_seeks(self) -> None:
        # Read and written in order: an upload takes the engines' buffered
        # path, a download lands in order.
        storage = TextIOStorage(io.StringIO("x"))
        for mode in ("rb", "wb"):
            fileobj = storage.open("k", mode)
            assert not hasattr(fileobj, "seek")
            assert not hasattr(fileobj, "seekable")


class TestMismatchedStreams:
    """Python's own streams of the other kind are refused at construction."""

    @pytest.mark.parametrize("stream", [io.StringIO(), io.TextIOWrapper(io.BytesIO())])
    def test_io_storage_refuses_a_text_stream(self, stream: Any) -> None:
        with pytest.raises(ValidationError, match="use TextIOStorage for a text stream"):
            IOStorage(stream)

    def test_io_storage_refuses_a_text_mode_file(self, tmp_path: Path) -> None:
        with (tmp_path / "f.txt").open("w") as handle, pytest.raises(ValidationError):
            IOStorage(handle)

    @pytest.mark.parametrize(
        "stream", [io.BytesIO(), io.BufferedReader(io.BytesIO()), io.FileIO(os.devnull)]
    )
    def test_text_io_storage_refuses_a_binary_stream(self, stream: Any) -> None:
        with pytest.raises(ValidationError, match="use IOStorage for a binary stream"):
            TextIOStorage(stream)
        stream.close()

    def test_text_io_storage_refuses_a_binary_mode_file(self, tmp_path: Path) -> None:
        with (tmp_path / "f.bin").open("wb") as handle, pytest.raises(ValidationError):
            TextIOStorage(handle)


class _Note(io.StringIO):
    """A ``StringIO`` that keeps what it held when it was closed."""

    value: str | None = None

    def close(self) -> None:
        if not self.closed:
            self.value = self.getvalue()
        super().close()


class _Notes(TextIOStorageBase):
    """A text endpoint opening a fresh stream per transfer, recording each one."""

    def __init__(self, text: str = "", *, encoding: str = "utf-8") -> None:
        super().__init__(encoding=encoding)
        self.text = text
        self.opened: list[tuple[str, str, _Note]] = []

    def open_text(self, key: str, mode: Literal["r", "w"]) -> TextIO:
        stream = _Note(self.text if mode == "r" else "")
        self.opened.append((key, mode, stream))
        return stream


class TestStreamStorageBases:
    def test_the_bases_cannot_be_instantiated(self) -> None:
        # The method a subclass must write is abstract: one that forgets it
        # is refused by Python itself when it is instantiated.
        with pytest.raises(TypeError):
            IOStorageBase()  # pyright: ignore[reportAbstractUsage]
        with pytest.raises(TypeError):
            TextIOStorageBase()  # pyright: ignore[reportAbstractUsage]

    def test_every_stream_storage_is_an_io_storage_base(self) -> None:
        for storage in (
            IOStorage(io.BytesIO()),
            TextIOStorage(io.StringIO()),
            StdioStorage(),
            _Notes(),
        ):
            assert isinstance(storage, IOStorageBase)
        assert not isinstance(StdioStorage(), IOStorage)
        assert not isinstance(TextIOStorage(io.StringIO()), IOStorage)

    def test_open_text_gets_the_key_and_a_text_mode(self) -> None:
        notes = _Notes("x")
        notes.open("a/k", "rb")
        notes.open("a/k", "wb")
        assert [(key, mode) for key, mode, _ in notes.opened] == [("a/k", "r"), ("a/k", "w")]

    def test_each_open_starts_a_fresh_codec(self) -> None:
        # Two transfers through one storage each get utf-16's BOM: the codec
        # state of the first never reaches the second.
        notes = _Notes("ab", encoding="utf-16")
        first = notes.open("k", "rb").read()
        second = notes.open("k", "rb").read()
        assert first == second == "ab".encode("utf-16")

    def test_the_adapters_close_what_open_text_returned(self) -> None:
        notes = _Notes("ab")
        reader = notes.open("k", "rb")
        reader.read()
        reader.close()
        writer = notes.open("k", "wb")
        writer.write(b"cd")
        writer.close()
        assert [stream.closed for _, _, stream in notes.opened] == [True, True]

    def test_a_failed_tail_still_closes_the_stream(self) -> None:
        # An object that ends inside a character: the decoder's final flush
        # raises, and the stream open_text returned is closed all the same.
        notes = _Notes()
        writer = notes.open("k", "wb")
        writer.write("é".encode()[:1])
        with pytest.raises(UnicodeDecodeError):
            writer.close()
        assert notes.opened[0][2].closed

    def test_a_download_closes_the_stream_once_the_item_settles(self) -> None:
        notes = _Notes()
        client, _ = make_recording_client([head_response(), get_response()])
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/d/a.txt", client=client),
            notes,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]
        assert [mode for _, mode, _ in notes.opened] == ["w"]
        assert notes.opened[0][2].value == "payload"

    def test_an_upload_closes_the_stream_once_the_item_settles(self) -> None:
        notes = _Notes("hello")
        client, calls = make_recording_client([{"ETag": '"e"'}])
        S3().cp(notes, S3Storage("s3://b/k", client=client), transfer_config=_SYNC)
        assert ops(calls) == ["PutObject"]
        assert [mode for _, mode, _ in notes.opened] == ["r"]
        assert notes.opened[0][2].closed

    def test_a_run_failing_before_the_transfer_closes_the_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # cp opens a stream source before it builds the transfer: a failure
        # in between (the S3 side's client) closes what the open returned.
        def broken(self: S3Storage) -> Any:
            raise Boto3S3Error("no client")

        monkeypatch.setattr(S3Storage, "get_client", broken)
        notes = _Notes("hello")
        with pytest.raises(Boto3S3Error, match="no client"):
            S3().cp(notes, S3Storage("s3://b/k"), transfer_config=_SYNC)
        assert [mode for _, mode, _ in notes.opened] == ["r"]
        assert notes.opened[0][2].closed

    def test_a_run_failing_before_the_transfer_never_opens_the_destination(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The destination opens at the first write, which never comes: the
        # deferred writer is discarded, not closed (a close would open it).
        def broken(self: S3Storage) -> Any:
            raise Boto3S3Error("no client")

        monkeypatch.setattr(S3Storage, "get_client", broken)
        notes = _Notes()
        with pytest.raises(Boto3S3Error, match="no client"):
            S3().cp(S3Storage("s3://b/k"), notes, transfer_config=_SYNC)
        assert notes.opened == []

    def test_a_source_that_cannot_be_opened_stops_the_run(self) -> None:
        # Raised from cp before the transfer, as aws-cli's missing stdin:
        # the library's base error, the source's own on __cause__.
        class _Missing(TextIOStorageBase):
            def open_text(self, key: str, mode: Literal["r", "w"]) -> TextIO:
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", "x.txt")

        client, calls = make_recording_client([])
        results: list[OpResult] = []
        with pytest.raises(Boto3S3Error) as excinfo:
            S3().cp(
                _Missing(),
                S3Storage("s3://b/k", client=client),
                transfer_config=_SYNC,
                on_result=results.append,
            )
        assert type(excinfo.value) is Boto3S3Error
        assert isinstance(excinfo.value.__cause__, FileNotFoundError)
        assert excinfo.value.operation == "cp"
        assert (results, calls) == ([], [])

    def test_an_interrupt_from_opening_the_source_passes_as_it_is(self) -> None:
        class _Interrupted(IOStorageBase):
            def open(
                self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None
            ) -> BinaryIO:
                raise KeyboardInterrupt

        client, calls = make_recording_client([])
        with pytest.raises(KeyboardInterrupt):
            S3().cp(_Interrupted(), S3Storage("s3://b/k", client=client), transfer_config=_SYNC)
        assert calls == []


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
        with pytest.raises(ValidationError) as excinfo:
            StdioStorage().open("k", "rb")
        # A directly invoked storage method names no operation: the storage
        # cannot know which one (cp streams, mv moves onto a stream), so an
        # operation-driven run stamps its own name instead.
        assert excinfo.value.operation is None

    def test_write_reads_stdout_per_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli's bytes_print dereferences sys.stdout on every write, so nothing
        # is captured at open time: a stdout swapped after the open (a
        # redirect_stdout around a library call) receives the bytes.
        first, second = _Stdio(), _Stdio()
        monkeypatch.setattr("sys.stdout", first)
        writer = StdioStorage().open("k", "wb")
        monkeypatch.setattr("sys.stdout", second)
        writer.write(b"hi")
        assert (first.buffer.getvalue(), second.buffer.getvalue()) == (b"", b"hi")

    def test_close_flushes_but_never_closes_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The transfer closes its writer as the item settles: the view flushes
        # what the process stream still buffers, so a stdout that cannot take
        # it fails the item, and leaves the process's stdout open.
        buffer = _RecordingBuffer()
        monkeypatch.setattr("sys.stdout", _BufferStdio(buffer))
        writer = StdioStorage().open("k", "wb")
        writer.write(b"hi")
        writer.close()
        assert (buffer.flushes, buffer.closes) == (1, 0)
        assert buffer.getvalue() == b"hi"


class TestStreamDownloadWithABrokenStdout:
    """A stdout that cannot take a streaming download's bytes fails the item.

    Whether the bytes are refused by a ``write`` or by the flush the writer's
    ``close`` makes as the item settles, the item is FAILED with the stream's
    error as the cause - never a success whose bytes are still waiting in the
    process stream's buffer.
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

    def test_a_failing_flush_fails_the_item(self, monkeypatch: pytest.MonkeyPatch) -> None:
        buffer = _FailingBuffer(on_flush=True)
        monkeypatch.setattr("sys.stdout", _BufferStdio(buffer))
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            self._download(StdioStorage(), results)
        assert buffer.flushes == 1
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert isinstance(error, Boto3S3Error)
        assert isinstance(error.__cause__, OSError)
        assert error.__cause__.errno == errno.ENOSPC
        assert str(error) == str(error.__cause__)  # the cause shows in the message

    def test_a_failing_flush_keeps_a_move_s_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The flush runs before the source delete, so a move whose bytes stdout
        # refused deletes nothing.
        monkeypatch.setattr("sys.stdout", _BufferStdio(_FailingBuffer(on_flush=True)))
        client, calls = make_recording_client([head_response(), get_response()])
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            S3().mv(
                S3Storage("s3://b/d/a.txt", client=client),
                StdioStorage(),
                transfer_config=_SYNC,
                on_result=results.append,
            )
        assert ops(calls) == ["HeadObject", "GetObject"]  # and no DeleteObject
        assert [result.outcome for result in results] == [OpOutcome.FAILED]

    def test_a_failing_write_fails_the_item(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdout", _BufferStdio(_FailingBuffer(on_write=True)))
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            self._download(StdioStorage(), results)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert isinstance(error, Boto3S3Error)
        assert isinstance(error.__cause__, OSError)
        assert str(error) == str(error.__cause__)  # the cause shows in the message

    def test_an_empty_object_without_a_stdout_fails_the_item(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing is written, but there is nowhere it could have gone: the
        # writer's close fails the item with the error a write meets, as aws-cli's
        # writer fails it (measured, rc 1 with the same line).
        monkeypatch.setattr("sys.stdout", None)
        client, _calls = make_recording_client([head_response(ContentLength=0), get_response(b"")])
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            S3().cp(
                S3Storage("s3://b/d/empty.txt", client=client),
                StdioStorage(),
                transfer_config=_SYNC,
                on_result=results.append,
            )
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert isinstance(error, Boto3S3Error)
        assert isinstance(error.__cause__, AttributeError)

    def test_no_stdout_at_all_fails_the_item(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli checks stdin before starting a transfer but never stdout, so this
        # is a per-item failure rather than a run-killing precondition (measured,
        # rc 1): the writer's own write fails, the open having succeeded.
        monkeypatch.setattr("sys.stdout", None)
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            self._download(StdioStorage(), results)
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert isinstance(error, Boto3S3Error)
        assert str(error) == str(error.__cause__)  # the cause shows in the message
        # The failing write is what the item recorded: an in-pipeline failure
        # carrying the run's own operation and the item's coordinates, not a
        # pre-flight ValidationError raised before anything was submitted.
        assert isinstance(error.__cause__, AttributeError)
        assert (error.operation, error.bucket, error.key) == ("cp", "b", "d/a.txt")
