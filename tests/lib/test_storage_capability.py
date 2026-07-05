"""Unit tests for ``StorageCapability`` and ``Storage.capabilities``.

The capability vocabulary is the structural pre-check a transfer runs on a custom
(``open``-routed) side: a declarative, class-level statement of which contract
methods a backend actually implements, distinct from runtime permission. These
goldens pin the ``auto()`` bit layout, the reading lattice
(``SORTABLE_SCAN`` -> ``SCAN`` -> ``GET_FILEINFO``), the fail-closed default, and
each built-in's honest declaration - notably that ``S3Storage`` resolves a single
object yet declares no ``OPEN_*`` because ``open`` is unimplemented.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import BinaryIO, ClassVar, Literal

from typing_extensions import override

from boto3_s3 import (
    IOStorage,
    LocalStorage,
    S3Storage,
    StdioStorage,
    Storage,
    StorageCapability,
)
from boto3_s3.types import FileInfo, ScanOptions

C = StorageCapability


class _Stub(Storage):
    """A minimal concrete Storage for default / lattice tests (methods are inert)."""

    @override
    def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
        raise NotImplementedError

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        raise NotImplementedError

    @override
    def delete(self, info: FileInfo) -> None:
        raise NotImplementedError

    @override
    def get_fileinfo(
        self,
        key: str = "",
        *,
        follow_symlinks: bool = True,
        on_warning: Callable[[str], None] | None = None,
    ) -> FileInfo | None:
        return None

    @override
    def as_text(self) -> str:
        return "stub"


class TestCustomBackendScanOptions:
    """A custom backend scans arg-less without overriding ``default_scan_options``:
    it either takes the base ``ScanOptions`` (nothing to declare) or names its own
    type with the one-line ``scan_options_type`` class attribute."""

    def test_base_options_backend_scans_argless_with_no_declaration(self) -> None:
        # Reads the base ScanOptions -> no scan_options_type, no override needed.
        class Plain(_Stub):
            @override
            def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
                yield [FileInfo(key="a", compare_key="a")]

        storage = Plain()
        assert isinstance(storage.default_scan_options(), ScanOptions)
        assert [i.key for i in storage.scan()] == ["a"]  # arg-less scan() just works

    def test_own_type_backend_needs_only_the_class_attr(self) -> None:
        # Requires its own ScanOptions subclass and rejects a foreign one, yet sets
        # only scan_options_type - arg-less scan() builds it, no method override.
        @dataclass(frozen=True, slots=True, kw_only=True)
        class MyScanOptions(ScanOptions):
            depth: int = 3

        class Strict(_Stub):
            scan_options_type: ClassVar[type[ScanOptions]] = MyScanOptions

            @override
            def scan_pages(self, options: ScanOptions) -> Iterator[Sequence[FileInfo]]:
                if not isinstance(options, MyScanOptions):
                    raise TypeError("Strict requires MyScanOptions")
                yield [FileInfo(key=f"d{options.depth}", compare_key="x")]

        storage = Strict()
        assert isinstance(storage.default_scan_options(), MyScanOptions)
        assert [i.key for i in storage.scan()] == ["d3"]  # arg-less, no TypeError

    def test_transfer_source_receives_the_backends_own_type(self) -> None:
        # The transfer engine (walk_source_scan_options), not only arg-less scan(),
        # hands a custom source backend its own scan_options_type - else a backend
        # that requires its subclass would TypeError mid-transfer.
        from boto3_s3.producers import walk_source_scan_options

        @dataclass(frozen=True, slots=True, kw_only=True)
        class MyScanOptions(ScanOptions):
            depth: int = 7

        class Strict(_Stub):
            scan_options_type: ClassVar[type[ScanOptions]] = MyScanOptions

        opts = walk_source_scan_options(
            Strict(),
            recursive=True,
            follow_symlinks=True,
            detect_symlink_loops=False,
            on_warning=None,
            item_filter=None,
        )
        assert isinstance(opts, MyScanOptions)  # own type, not a base ScanOptions
        assert opts.recursive is True  # common knob overlaid
        assert opts.depth == 7  # own default kept

        # A base-options backend still gets a plain ScanOptions.
        base_opts = walk_source_scan_options(
            _Stub(),
            recursive=False,
            follow_symlinks=True,
            detect_symlink_loops=False,
            on_warning=None,
            item_filter=None,
        )
        assert type(base_opts) is ScanOptions


class TestAutoBitLayout:
    def test_members_are_successive_powers_of_two(self) -> None:
        # auto() on a Flag assigns distinct single bits, in declaration order.
        assert [c.value for c in C] == [1, 2, 4, 8, 16, 32]


class TestBuiltinDeclarations:
    def test_s3_declares_no_open(self) -> None:
        # open() is unimplemented, so honesty requires omitting OPEN_*.
        assert S3Storage.capabilities == C.GET_FILEINFO | C.SCAN | C.SORTABLE_SCAN | C.DELETE

    def test_local_is_fully_capable(self) -> None:
        assert LocalStorage.capabilities == (
            C.OPEN_READ | C.OPEN_WRITE | C.GET_FILEINFO | C.SCAN | C.SORTABLE_SCAN | C.DELETE
        )

    def test_iostorage_is_byte_io_only(self) -> None:
        assert IOStorage.capabilities == C.OPEN_READ | C.OPEN_WRITE

    def test_stdio_inherits_the_io_pair(self) -> None:
        assert StdioStorage.capabilities == C.OPEN_READ | C.OPEN_WRITE


class TestSupports:
    def test_present_capability(self) -> None:
        assert S3Storage("s3://b/k").supports(C.SCAN | C.DELETE)

    def test_absent_capability(self) -> None:
        assert not S3Storage("s3://b/k").supports(C.OPEN_READ)

    def test_io_has_open_but_not_scan(self) -> None:
        s = IOStorage(io.BytesIO())
        assert s.supports(C.OPEN_READ | C.OPEN_WRITE)
        assert not s.supports(C.SCAN)
        assert not s.supports(C.GET_FILEINFO)


class TestLattice:
    def test_sortable_scan_implies_scan_and_get_fileinfo(self) -> None:
        class _SortedOnly(_Stub):
            capabilities = C.SORTABLE_SCAN

        s = _SortedOnly()
        assert s.supports(C.SCAN)
        assert s.supports(C.GET_FILEINFO)
        assert s.supports(C.SORTABLE_SCAN | C.SCAN | C.GET_FILEINFO)

    def test_scan_implies_get_fileinfo_but_not_sorted(self) -> None:
        class _ScanOnly(_Stub):
            capabilities = C.SCAN

        s = _ScanOnly()
        assert s.supports(C.GET_FILEINFO)
        assert not s.supports(C.SORTABLE_SCAN)


class TestMissingCapabilities:
    def test_names_the_gap(self) -> None:
        assert S3Storage("s3://b/k").missing_capabilities(C.OPEN_READ | C.SCAN) == C.OPEN_READ

    def test_empty_when_all_present(self) -> None:
        assert LocalStorage(".").missing_capabilities(C.OPEN_WRITE | C.DELETE) == C(0)


class TestFailClosedDefault:
    def test_base_default_declares_nothing(self) -> None:
        assert Storage.capabilities == C(0)

    def test_a_backend_that_forgets_supports_nothing(self) -> None:
        assert _Stub().capabilities == C(0)
        assert not _Stub().supports(C.OPEN_READ)
        assert _Stub().missing_capabilities(C.OPEN_READ) == C.OPEN_READ
