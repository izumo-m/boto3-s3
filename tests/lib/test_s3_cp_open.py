"""``S3.cp`` over the open route: a custom ``Storage`` backend paired with S3.

A non-built-in backend (``scheme`` other than ``"s3"`` / ``"local"``) moves its
bytes through ``Storage.open`` instead of ``s3transfer``: ``opens3`` uploads each
``open("rb")`` to an S3 destination, ``s3open`` downloads each S3 object into an
``open("wb")`` whose ``close`` commits the write. An in-memory
``dict[str, bytes]`` backend (``_MemStorage``) stands in for a real one (e.g. an
HTTP backend); its writer commits **only on ``close``**, so these tests also pin
that the transfer closes every fileobj it opens (``transfer._CloseFileobj``) -
without that close the store would stay empty.

The S3 side rides the recording client (``make_recording_client``), so the genuine
HeadObject / GetObject / PutObject / ListObjectsV2 wire shape is exercised while
the custom side is pure Python.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3 import GlobFilter
from boto3_s3.exceptions import (
    BatchError,
    CancelledError,
    NotFoundError,
    ValidationError,
)
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.storage import Storage, StorageCapability
from boto3_s3.types import CancelToken, FileInfo, FileKind, OpOutcome, OpResult, TransferType
from tests.utils.fakes3 import MTIME, client_error, get_response, head_response
from tests.utils.recorder import make_recording_client, ops

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from typing import BinaryIO, Literal

    from boto3_s3.types import ScanOptions

_SYNC = TransferConfig(use_threads=False)


def _basename(location: str) -> str:
    """The last path segment of a ``mem://...`` location (its single-object name)."""
    return location.rstrip("/").rsplit("/", 1)[-1]


class _MemWriter:
    """A write buffer that commits to the store **only on** ``close``.

    ``Storage.open("wb").close()`` is what persists a write (the open-route
    contract), and ``s3transfer`` never closes a caller-supplied fileobj - so if
    the transfer failed to close this, ``store`` would stay empty. The download
    tests assert the committed bytes, which therefore pin that close.
    """

    def __init__(self, store: dict[str, bytes], key: str) -> None:
        self._store = store
        self._key = key
        self._buf = io.BytesIO()
        self.closed = False

    def write(self, data: bytes) -> int:
        return self._buf.write(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        if self.closed:
            return
        self._store[self._key] = self._buf.getvalue()
        self.closed = True


class _MemStorage(Storage):
    """An in-memory ``dict[str, bytes]`` as a custom (non-built-in) ``Storage``.

    ``scheme = "mem"`` routes ``cp`` through the open route against an S3 side.
    ``store`` is keyed by ``compare_key`` relative to this storage's location: a
    single source / destination uses the key ``""`` (the location itself), a
    recursive one the relative keys. ``capabilities`` declares the full open-route
    contract; the narrower subclasses below drive the capability gate.

    ``opens`` records every ``(key, mode)`` passed to ``open()`` - it lets a
    dry-run test assert the backend is never opened (no read/write side
    effect). ``deletes`` records every key passed to ``delete()`` - it lets an
    mv test assert the source was removed through ``Storage.delete`` (not
    ``os.remove`` of an empty path).
    """

    scheme = "mem"
    capabilities = (
        StorageCapability.OPEN_READ
        | StorageCapability.OPEN_WRITE
        | StorageCapability.SORTABLE_SCAN
        | StorageCapability.DELETE
    )

    def __init__(self, store: dict[str, bytes], *, location: str = "mem://data") -> None:
        self._store = store
        self._location = location
        self.opens: list[tuple[str, str]] = []
        self.deletes: list[str] = []

    def as_text(self) -> str:
        return self._location

    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        self.opens.append((key, mode))
        if mode == "rb":
            return io.BytesIO(self._store[key])  # type: ignore[return-value]
        return _MemWriter(self._store, key)  # type: ignore[return-value]

    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
        # scan_pages returns already-filtered pages (the contract): a real custom
        # backend would push options.filter to its source; this in-memory one sieves.
        infos = [
            FileInfo(key=key, kind=FileKind.FILE, size=len(data), mtime=MTIME, compare_key=key)
            for key, data in sorted(self._store.items())
        ]
        if options.filter is not None:
            infos = [info for info in infos if options.filter(info)]
        if infos:
            yield infos

    def get_fileinfo(
        self,
        key: str = "",
        *,
        on_warning: Callable[[str], None] | None = None,
    ) -> FileInfo | None:
        if key not in self._store:
            return None
        return FileInfo(
            key=key,
            kind=FileKind.FILE,
            size=len(self._store[key]),
            mtime=MTIME,
            compare_key=_basename(self._location),
        )

    def delete(self, info: FileInfo) -> None:
        self.deletes.append(info.key)
        del self._store[info.key]


class _ArrivalOrderMem(_MemStorage):
    """Yields entries in dict insertion order - deliberately unsorted - to pin
    that the non-sync consumers take the backend's arrival order (a plain
    ``SCAN`` side has no ordering guarantee; docs/storage.md section 3)."""

    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
        yield [
            FileInfo(key=key, kind=FileKind.FILE, size=len(data), mtime=MTIME, compare_key=key)
            for key, data in self._store.items()  # insertion order, NOT sorted
        ]


class _ReadOnlyMem(_MemStorage):
    """Declares only ``OPEN_READ`` - missing ``SCAN`` / ``GET_FILEINFO`` (opens3) and
    ``OPEN_WRITE`` (s3open), so it always trips the capability gate."""

    capabilities = StorageCapability.OPEN_READ


class _NoDeleteMem(_MemStorage):
    """Readable/writable but not deletable - an mv *source* trips the gate (DELETE)."""

    capabilities = (
        StorageCapability.OPEN_READ | StorageCapability.OPEN_WRITE | StorageCapability.SORTABLE_SCAN
    )


class _FailingWriter(_MemWriter):
    """A writer whose ``close`` (the commit) raises - the backend rejecting the write."""

    def close(self) -> None:
        self.closed = True
        raise RuntimeError("commit failed")


class _CommitFailMem(_MemStorage):
    """A backend whose ``open("wb")`` commit fails on ``close``."""

    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        if mode == "wb":
            return _FailingWriter(self._store, key)  # type: ignore[return-value]
        return super().open(key, mode, size=size)


class TestOpenUploadRoute:
    def test_single_upload_puts_the_object(self) -> None:
        src = _MemStorage({"": b"x" * 7}, location="mem://data/a.txt")
        client, calls = make_recording_client([{}])
        results: list[OpResult] = []
        S3().cp(
            src,
            S3Storage("s3://bucket/up/", client=client),
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert ops(calls) == ["PutObject"]
        assert calls[0].params["Bucket"] == "bucket"
        assert calls[0].params["Key"] == "up/a.txt"
        assert results[0].outcome is OpOutcome.SUCCEEDED
        # The custom side renders through its own as_text(), the S3 side as a URI.
        assert results[0].src == "mem://data/a.txt"
        assert results[0].dest == "s3://bucket/up/a.txt"

    def test_recursive_upload_opens_each_key_in_order(self) -> None:
        src = _MemStorage({"a.txt": b"x", "sub/b.txt": b"yy"}, location="mem://data/")
        client, calls = make_recording_client([{}, {}])
        S3().cp(
            src,
            S3Storage("s3://b/tree", client=client),
            recursive=True,
            transfer_config=_SYNC,
        )
        assert [call.params["Key"] for call in calls] == ["tree/a.txt", "tree/sub/b.txt"]

    def test_upload_guesses_content_type_from_the_entry_key(self) -> None:
        # Open-route uploads are shaped like local ones (docs/transfer.md
        # section 12): the default guess reads the entry's key, so a custom
        # backend's .jpg lands as image/jpeg without the backend doing
        # anything. Only a true stream (no filename) skips the guess.
        src = _MemStorage({"img/photo.jpg": b"x"}, location="mem://data/")
        client, calls = make_recording_client([{}])
        S3().cp(
            src,
            S3Storage("s3://b/tree", client=client),
            recursive=True,
            transfer_config=_SYNC,
        )
        assert calls[0].params["ContentType"] == "image/jpeg"

    def test_oversize_upload_warns_but_still_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The open-route mirror of the local route's oversize pre-warning
        # (aws-cli's _warn_if_too_large): warn but still attempt, rendering
        # through the open side's own display form.
        monkeypatch.setattr("boto3_s3.producers._MAX_UPLOAD_SIZE", 1)
        src = _MemStorage({"big.bin": b"xx"}, location="mem://data/")
        client, calls = make_recording_client([{}])
        results: list[OpResult] = []
        S3().cp(
            src,
            S3Storage("s3://b/tree", client=client),
            recursive=True,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert ops(calls) == ["PutObject"]  # warned, never skipped
        outcomes = [r.outcome for r in results]
        assert outcomes.count(OpOutcome.WARNED) == 1
        assert outcomes.count(OpOutcome.SUCCEEDED) == 1
        warned = next(r for r in results if r.outcome is OpOutcome.WARNED)
        assert "exceeds s3 upload limit of 48.8 TiB." in str(warned.error)
        assert "big.bin" in str(warned.error)

    def test_recursive_upload_takes_the_backend_arrival_order(self) -> None:
        # sync is the only order-sensitive consumer (docs/storage.md section 3):
        # a recursive cp consumes a custom backend's entries exactly as its
        # scan yields them - deliberately NOT sorted here - with no reordering
        # anywhere in scan()'s prefetch or the submit loop.
        store = {"z.txt": b"1", "a.txt": b"2", "m.txt": b"3"}  # insertion order
        src = _ArrivalOrderMem(store, location="mem://data/")
        client, calls = make_recording_client([{}, {}, {}])
        S3().cp(
            src,
            S3Storage("s3://b/tree", client=client),
            recursive=True,
            transfer_config=_SYNC,
        )
        assert [call.params["Key"] for call in calls] == [
            "tree/z.txt",
            "tree/a.txt",
            "tree/m.txt",
        ]

    def test_recursive_move_deletes_in_arrival_order_too(self) -> None:
        # mv rides the same enumeration: uploads and the per-item source
        # deletes both follow the backend's yield order.
        store = {"z.txt": b"1", "a.txt": b"2", "m.txt": b"3"}
        src = _ArrivalOrderMem(store, location="mem://data/")
        client, calls = make_recording_client([{}, {}, {}])
        S3().mv(
            src,
            S3Storage("s3://b/tree", client=client),
            recursive=True,
            transfer_config=_SYNC,
        )
        assert [call.params["Key"] for call in calls] == [
            "tree/z.txt",
            "tree/a.txt",
            "tree/m.txt",
        ]
        assert src.deletes == ["z.txt", "a.txt", "m.txt"]
        assert store == {}

    def test_cancel_does_not_open_the_next_source(self) -> None:
        # The submit loop checks the cancel token *before* pulling the next item,
        # so a run cancelled mid-stream never opens (and thus never leaks) the
        # fileobj of an item it will not submit. on_result cancels right after the
        # first upload commits; the second source must never be opened.
        src = _MemStorage({"a.txt": b"x", "b.txt": b"yy"}, location="mem://data/")
        token = CancelToken()
        client, _ = make_recording_client([{}, {}])
        with pytest.raises(CancelledError):
            S3().cp(
                src,
                S3Storage("s3://b/tree", client=client),
                recursive=True,
                transfer_config=_SYNC,
                cancel_token=token,
                on_result=lambda _result: token.cancel(),
            )
        assert src.opens == [("a.txt", "rb")]

    def test_glob_filter_matches_compare_keys(self) -> None:
        src = _MemStorage({"keep.txt": b"x", "drop.bin": b"y"}, location="mem://data/")
        keep = GlobFilter().exclude("*").include("*.txt").compile()
        client, calls = make_recording_client([{}])
        S3().cp(
            src,
            S3Storage("s3://b/t", client=client),
            recursive=True,
            filter=keep,
            transfer_config=_SYNC,
        )
        assert [call.params["Key"] for call in calls] == ["t/keep.txt"]

    def test_missing_single_source_raises_does_not_exist(self) -> None:
        # An unresolvable single source (get_fileinfo -> None) raises up front
        # like a missing local source - NotFoundError with no ClientError cause
        # (the general rc 255) - not a silent zero-item run.
        src = _MemStorage({}, location="mem://data/gone.txt")
        client, calls = make_recording_client([])
        with pytest.raises(NotFoundError) as excinfo:
            S3().cp(src, S3Storage("s3://b/k", client=client), transfer_config=_SYNC)
        assert excinfo.value.__cause__ is None
        assert str(excinfo.value) == "The user-provided path mem://data/gone.txt does not exist."
        assert calls == []

    def test_recursive_empty_source_transfers_nothing(self) -> None:
        # Unlike a single source, a recursive source is an enumeration: an empty
        # scan yields zero items (rc 0, like an empty S3 prefix) - it never raises.
        src = _MemStorage({}, location="mem://data/")
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        S3().cp(
            src,
            S3Storage("s3://b/t", client=client),
            recursive=True,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert calls == []
        assert results == []

    def test_dryrun_enumerates_but_never_opens_the_source(self) -> None:
        src = _MemStorage({"": b"x" * 7}, location="mem://data/a.txt")
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        S3().cp(
            src,
            S3Storage("s3://bucket/up/", client=client),
            dryrun=True,
            on_result=results.append,
        )
        assert calls == []
        assert [result.outcome for result in results] == [OpOutcome.DRYRUN]
        assert results[0].dest == "s3://bucket/up/a.txt"
        assert src.opens == []

    def test_missing_get_fileinfo_capability_is_rejected(self) -> None:
        # A single (non-recursive) source resolves through get_fileinfo; a backend
        # that only declares OPEN_READ cannot, and is rejected before any S3 call.
        src = _ReadOnlyMem({"": b"x"}, location="mem://data/a.txt")
        client, calls = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().cp(src, S3Storage("s3://b/k", client=client), transfer_config=_SYNC)
        assert "GET_FILEINFO" in str(excinfo.value)
        assert "mem://data/a.txt" in str(excinfo.value)
        assert calls == []

    def test_missing_scan_capability_is_rejected(self) -> None:
        # A recursive source enumerates through scan; OPEN_READ alone misses SCAN.
        src = _ReadOnlyMem({"a.txt": b"x"}, location="mem://data/")
        client, calls = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().cp(
                src,
                S3Storage("s3://b/t", client=client),
                recursive=True,
                transfer_config=_SYNC,
            )
        assert "SCAN" in str(excinfo.value)
        assert calls == []


class TestOpenDownloadRoute:
    def test_single_download_commits_on_close(self) -> None:
        # A non-"/"-terminated destination names a single object (the location
        # itself, key ""); the writer commits only when the transfer closes it.
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/out.bin")
        client, calls = make_recording_client([head_response(), get_response()])
        S3().cp(S3Storage("s3://b/d/a.txt", client=client), dest, transfer_config=_SYNC)
        assert ops(calls) == ["HeadObject", "GetObject"]
        assert store == {"": b"payload"}

    def test_single_download_into_a_directory_uses_the_basename(self) -> None:
        # A "/"-terminated destination adopts the source's name (use_src_name).
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/")
        client, calls = make_recording_client([head_response(), get_response()])
        S3().cp(S3Storage("s3://b/d/a.txt", client=client), dest, transfer_config=_SYNC)
        assert ops(calls) == ["HeadObject", "GetObject"]
        assert store == {"a.txt": b"payload"}

    def test_single_download_is_filtered_too(self) -> None:
        # The open route mirrors the built-in one: aws filters the
        # single-object case as well - the excluded object is headed but
        # never fetched, and the backend is never opened.
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/out.bin")
        drop = GlobFilter().exclude("*").compile()
        client, calls = make_recording_client([head_response()])
        S3().cp(
            S3Storage("s3://b/d/a.txt", client=client), dest, filter=drop, transfer_config=_SYNC
        )
        assert ops(calls) == ["HeadObject"]
        assert store == {}
        assert dest.opens == []

    def test_recursive_download_writes_each_relative_key(self) -> None:
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/")
        listing = {
            "Contents": [
                {"Key": "pre/a.txt", "Size": 3, "LastModified": MTIME, "ETag": '"a"'},
                {"Key": "pre/sub/b.txt", "Size": 3, "LastModified": MTIME, "ETag": '"b"'},
            ]
        }
        client, calls = make_recording_client([listing, get_response(b"AAA"), get_response(b"BBB")])
        S3().cp(
            S3Storage("s3://b/pre", client=client),
            dest,
            recursive=True,
            transfer_config=_SYNC,
        )
        assert ops(calls) == ["ListObjectsV2", "GetObject", "GetObject"]
        assert store == {"a.txt": b"AAA", "sub/b.txt": b"BBB"}

    def test_keyless_non_recursive_source_writes_nothing(self) -> None:
        # `cp s3://bucket mem-dest`: aws lists the bucket and exact-matches nothing.
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/out.bin")
        client, calls = make_recording_client([])
        results: list[OpResult] = []
        S3().cp(S3Storage("s3://bucket", client=client), dest, on_result=results.append)
        assert calls == []
        assert results == []
        assert store == {}

    def test_glacier_source_skips_and_commits_nothing(self) -> None:
        # The source-side glacier gate still runs for a custom destination.
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/out.bin")
        client, calls = make_recording_client([head_response(StorageClass="GLACIER")])
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/cold", client=client),
            dest,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert ops(calls) == ["HeadObject"]
        assert [result.outcome for result in results] == [OpOutcome.WARNED]
        assert store == {}

    def test_dryrun_heads_but_never_opens_the_backend(self) -> None:
        # A dry run enumerates (HeadObject) but must not open the destination
        # for writing - opening "wb" on a real backend is itself a side effect.
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/out.bin")
        client, calls = make_recording_client([head_response()])
        results: list[OpResult] = []
        S3().cp(
            S3Storage("s3://b/d/a.txt", client=client),
            dest,
            dryrun=True,
            on_result=results.append,
        )
        assert ops(calls) == ["HeadObject"]
        assert [result.outcome for result in results] == [OpOutcome.DRYRUN]
        assert dest.opens == []
        assert store == {}

    def test_missing_open_write_capability_is_rejected(self) -> None:
        dest = _ReadOnlyMem({}, location="mem://data/out.bin")
        client, calls = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().cp(S3Storage("s3://b/d/a.txt", client=client), dest, transfer_config=_SYNC)
        assert "OPEN_WRITE" in str(excinfo.value)
        assert calls == []

    def test_commit_failure_surfaces_as_a_failed_transfer(self) -> None:
        # The bytes move (HeadObject + GetObject), but the backend rejects the
        # write on close: _CloseFileobj flips the settled future to a failure.
        store: dict[str, bytes] = {}
        dest = _CommitFailMem(store, location="mem://data/out.bin")
        client, calls = make_recording_client([head_response(), get_response()])
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            S3().cp(
                S3Storage("s3://b/d/a.txt", client=client),
                dest,
                transfer_config=_SYNC,
                on_result=results.append,
            )
        assert ops(calls) == ["HeadObject", "GetObject"]
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        assert "commit failed" in str(results[0].error)


class TestMoveRoute:
    """``mv`` over a custom backend: transfer, then delete the source.

    For ``opens3`` (custom source) the source is removed through its own
    ``Storage.delete`` after each successful upload; for ``s3open`` (custom
    destination) the source is S3, deleted with ``DeleteObject`` like any other
    download mv. Both report ``TransferType.MOVE``. A failed transfer keeps its source
    (``_CloseFileobj`` sits before the source delete, so a failed commit blocks
    it too).
    """

    def test_move_from_a_custom_source_uploads_then_deletes(self) -> None:
        src = _MemStorage({"": b"x" * 7}, location="mem://data/a.txt")
        client, calls = make_recording_client([{}])
        results: list[OpResult] = []
        S3().mv(
            src,
            S3Storage("s3://bucket/up/", client=client),
            transfer_config=_SYNC,
            on_result=results.append,
        )
        assert ops(calls) == ["PutObject"]
        assert calls[0].params["Key"] == "up/a.txt"
        # The single source's open key is "" (the location itself); delete uses it.
        assert src.deletes == [""]
        assert src._store == {}
        assert [(r.transfer_type, r.outcome) for r in results] == [
            (TransferType.MOVE, OpOutcome.SUCCEEDED)
        ]

    def test_recursive_move_from_a_custom_source_deletes_each_key(self) -> None:
        src = _MemStorage({"a.txt": b"x", "sub/b.txt": b"yy"}, location="mem://data/")
        client, calls = make_recording_client([{}, {}])
        S3().mv(
            src,
            S3Storage("s3://b/tree", client=client),
            recursive=True,
            transfer_config=_SYNC,
        )
        assert [call.params["Key"] for call in calls] == ["tree/a.txt", "tree/sub/b.txt"]
        # Each recursive item's open key is its compare_key; all are deleted.
        assert src.deletes == ["a.txt", "sub/b.txt"]
        assert src._store == {}

    def test_move_into_a_custom_destination_deletes_the_s3_source(self) -> None:
        store: dict[str, bytes] = {}
        dest = _MemStorage(store, location="mem://data/out.bin")
        client, calls = make_recording_client([head_response(), get_response(), {}])
        results: list[OpResult] = []
        S3().mv(
            S3Storage("s3://b/d/a.txt", client=client),
            dest,
            transfer_config=_SYNC,
            on_result=results.append,
        )
        # Download into the custom dest, then DeleteObject the S3 source.
        assert ops(calls) == ["HeadObject", "GetObject", "DeleteObject"]
        assert calls[2].params == {"Bucket": "b", "Key": "d/a.txt"}
        assert store == {"": b"payload"}
        assert [(r.transfer_type, r.outcome) for r in results] == [
            (TransferType.MOVE, OpOutcome.SUCCEEDED)
        ]

    def test_move_from_a_source_without_delete_is_rejected(self) -> None:
        src = _NoDeleteMem({"": b"x"}, location="mem://data/a.txt")
        client, calls = make_recording_client([])
        with pytest.raises(ValidationError) as excinfo:
            S3().mv(src, S3Storage("s3://b/k", client=client), transfer_config=_SYNC)
        assert "DELETE" in str(excinfo.value)
        assert calls == []

    def test_failed_upload_keeps_the_custom_source(self) -> None:
        # The source delete must not run when the transfer failed: the bytes
        # never arrived, so mv keeps the only copy.
        src = _MemStorage({"": b"x" * 7}, location="mem://data/a.txt")
        client, _ = make_recording_client([client_error("NoSuchBucket", 404, "PutObject")])
        with pytest.raises(BatchError):
            S3().mv(src, S3Storage("s3://gone/up/", client=client), transfer_config=_SYNC)
        assert src.deletes == []
        assert src._store == {"": b"x" * 7}
