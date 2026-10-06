"""The `Content-Type` an upload guesses: aws's table, not the host interpreter's.

aws-cli's official distribution is frozen against CPython 3.14, so that
interpreter's MIME table is the answer boto3-s3 has to reproduce on every
supported host - `boto3_s3.mimetable` carries it. The rows below are the
extensions where a host table disagrees with 3.14's in one direction or the
other, so at least one of them is load-bearing on any given interpreter.

Oracle: the type aws 2.36.40 actually stored, read back with HeadObject after
`cp` to MinIO (probes/wire/probe_content_type.py, and every candidate extension
at once in probes/fixA/p2-all-extension-content-type.py).
"""

from __future__ import annotations

import mimetypes
import ntpath
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from boto3_s3 import mimetable, transfer
from boto3_s3.transfer import _guess_content_type


class TestFrozenTable:
    """The frozen layers alone, host augmentation neutralized.

    `_mime_types` deliberately overlays the Windows registry and the host's
    mime.types on top of the frozen table (aws's lazy `mimetypes.init` does
    the same), so a host that happens to map one of these extensions - the
    GitHub Windows runner's registry maps `.cjs` - would flip the row without
    any parity being wrong. The fixture pins the cache to a db built from the
    frozen layers only; `TestHostOverlays` covers the augmentation.
    """

    @pytest.fixture(autouse=True)
    def _frozen_layers_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(transfer, "_mime_db", None)
        monkeypatch.setattr(mimetable, "KNOWNFILES", [])
        monkeypatch.setattr(
            mimetypes.MimeTypes, "read_windows_registry", lambda self, strict=True: None
        )

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # Absent from 3.10 / 3.11 / 3.12 / 3.13's table, present in 3.14's.
            ("f.php", "application/x-httpd-php"),
            ("f.weba", "audio/webm"),
            ("f.g3", "image/g3fax"),
            ("f.t38", "image/t38"),
            # The other direction: 3.15 added .cjs and re-typed .texinfo.
            ("f.cjs", None),
            ("f.texinfo", "application/x-texinfo"),
            # The multi-suffix and encoding maps ride along.
            ("f.tgz", "application/x-tar"),
            ("f.tar.gz", "application/x-tar"),
            ("f.svgz", "image/svg+xml"),
            # A plain one, and one nothing knows.
            ("f.txt", "text/plain"),
            ("f.unknown-ext-probe", None),
        ],
    )
    def test_guessed_type(self, name: str, expected: str | None) -> None:
        assert _guess_content_type(name) == expected


class _NtPathOs:
    """``transfer.os`` as a Windows host sees it: ``os.path`` is ``ntpath``, the rest real."""

    path = ntpath

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


class TestAlgorithmPin:
    """The guess runs CPython 3.14's algorithm whatever interpreter hosts it.

    aws's bundled 3.14 splits a file path with the host's ``os.path`` and only
    a scheme-prefixed name with ``posixpath``; interpreters before 3.13 split
    every name with ``posixpath``, so on Windows a name that is all extension
    (``C:\\d\\.json``) kept ``.json`` and went up typed where aws sends it
    untyped (measured against aws.exe under Python 3.10, 8 of 12 names).
    """

    @pytest.fixture(autouse=True)
    def _frozen_layers_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(transfer, "_mime_db", None)
        monkeypatch.setattr(mimetable, "KNOWNFILES", [])
        monkeypatch.setattr(
            mimetypes.MimeTypes, "read_windows_registry", lambda self, strict=True: None
        )

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (r"C:\d\.json", None),
            (r"C:\d\.tar.gz", None),
            (r"C:\d\..txt", None),
            (r"C:\d\a.json", "application/json"),
            (r"C:\d\b.TGZ", "application/x-tar"),
            (r"\\srv\share\.txt", None),
            (r"\\srv\share\a.txt", "text/plain"),
            (r"C:\dir.d\x", None),
            (r"C:\dir.d\x.png", "image/png"),
        ],
    )
    def test_a_windows_path_splits_with_ntpath(
        self, monkeypatch: pytest.MonkeyPatch, name: str, expected: str | None
    ) -> None:
        monkeypatch.setattr(transfer, "os", _NtPathOs())
        assert _guess_content_type(name) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            # A scheme-like prefix: the URL branch, split with posixpath.
            ("notes:a.txt", "text/plain"),
            ("http://h/p.png?x=1", "image/png"),
            ("http://h/p.png#frag", "image/png"),
            # One character is a drive letter, not a scheme: the file branch.
            ("c:a.txt", "text/plain"),
            # data: URLs answer their media type.
            ("data:image/png;base64,xx", "image/png"),
            ("data:text/plain;charset=utf-8,abc", "text/plain"),
            ("data:,abc", "text/plain"),
            ("data:nocomma", None),
            # Plain paths, extension case, aliases and encodings.
            ("/tmp/a.b/c", None),
            ("f.JPG", "image/jpeg"),
            # An encoding suffix peels case-sensitively: ".GZ" is a type, ".gz" an encoding.
            ("f.tar.GZ", "application/gzip"),
            ("f.gz", None),
            ("f.svgz", "image/svg+xml"),
            (".bashrc", None),
            ("", None),
        ],
    )
    def test_the_url_and_path_branches(self, name: str, expected: str | None) -> None:
        assert _guess_content_type(name) == expected

    def test_a_name_urlparse_refuses_fails_the_item(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 3.14's guess_type parses the name as a URL first, and urlparse
        # refuses an unmatched bracket after a scheme-like prefix with
        # ValueError. A local path never starts that way, but an open-route
        # key can; inside aws's queued subscriber that is the item's failure,
        # so it is here too, not the run's.
        from boto3_s3 import S3, BatchError, OpResult, S3Storage
        from boto3_s3.transferconfig import TransferConfig
        from tests.utils.recorder import make_recording_client

        with pytest.raises(ValueError):
            _guess_content_type("scheme://[unmatched/a.txt")
        refused = ValueError("Invalid IPv6 URL")
        monkeypatch.setattr(
            transfer, "_guess_content_type", lambda _name: (_ for _ in ()).throw(refused)
        )
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("a")
        client, _ = make_recording_client([])
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            S3().cp(
                str(src),
                S3Storage("s3://b/p/", client=client),
                recursive=True,
                transfer_config=TransferConfig(use_threads=False),
                on_result=results.append,
            )
        assert len(results) == 1 and results[0].error is not None
        assert str(results[0].error) == "Invalid IPv6 URL"

    @pytest.mark.skipif(
        sys.version_info < (3, 13), reason="the host's guess_type is the older algorithm"
    )
    @pytest.mark.parametrize(
        "name",
        [
            "a.txt",
            "f.TGZ",
            "f.tar.gz",
            "f.gz",
            ".json",
            "..txt",
            "dir.d/x",
            "notes:a.txt",
            "http://h/p.png?x=1",
            "data:image/png;base64,xx",
            "data:,abc",
            "c:a.txt",
            "",
            "/abs/.hidden",
            "/abs/dir.d/.hidden.txt",
            "f.svgz",
            "f.Z",
            "f.bz2",
            "f.tbz2",
        ],
    )
    def test_matches_the_host_interpreter_from_3_13(self, name: str) -> None:
        # From 3.13 the stdlib runs the same algorithm, so it is the oracle.
        assert _guess_content_type(name) == transfer._mime_types().guess_type(name)[0]


class TestUnreadableOverlay:
    """A knownfiles entry that exists but cannot be read fails the item, not the run.

    aws guesses the type inside s3transfer's queued subscriber, so an
    `OSError` from `mimetypes.init` reading `/etc/mime.types` is that item's
    `upload failed: ... [Errno 13] Permission denied: '/etc/mime.types'` and
    the run goes on - every item fails the same way, and a sync still
    deletes. Raised out of the submit loop, it ended the run as one
    `fatal error:` and leaked a bare `PermissionError` from the library
    (measured against aws 2.36.40 in a mount namespace, 2026-10-06).
    """

    @pytest.fixture(autouse=True)
    def _unreadable_mime_types(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        overlay = tmp_path / "mime.types"
        overlay.write_text("text/x-probe probe\n")
        monkeypatch.setattr(transfer, "_mime_db", None)
        monkeypatch.setattr(mimetable, "KNOWNFILES", [str(overlay)])

        def refuse(self: mimetypes.MimeTypes, filename: str, strict: bool = True) -> None:
            raise PermissionError(13, "Permission denied", filename)

        monkeypatch.setattr(mimetypes.MimeTypes, "read", refuse)
        self.overlay = overlay

    def _tree(self, tmp_path: Path) -> Path:
        src = tmp_path / "src"
        src.mkdir(parents=True)
        (src / "a.txt").write_text("a")
        (src / "b.json").write_text("{}")
        return src

    def test_every_item_fails_with_the_os_error_and_the_run_goes_on(self, tmp_path: Path) -> None:
        from boto3_s3 import S3, BatchError, OpOutcome, OpResult, S3Storage
        from boto3_s3.transferconfig import TransferConfig
        from tests.utils.recorder import make_recording_client

        client, calls = make_recording_client([])
        results: list[OpResult] = []
        with pytest.raises(BatchError) as info:
            S3().cp(
                str(self._tree(tmp_path)),
                S3Storage("s3://b/p/", client=client),
                recursive=True,
                transfer_config=TransferConfig(use_threads=False),
                on_result=results.append,
            )
        assert info.value.failed == 2
        assert [r.outcome for r in results] == [OpOutcome.FAILED, OpOutcome.FAILED]
        for result in results:
            assert result.error is not None
            assert str(result.error) == f"[Errno 13] Permission denied: '{self.overlay}'"
        assert calls == []

    def test_an_explicit_content_type_or_no_guess_never_reads_the_overlay(
        self, tmp_path: Path
    ) -> None:
        from boto3_s3 import S3, OpOutcome, OpResult, S3Storage
        from boto3_s3.transferconfig import TransferConfig
        from tests.utils.recorder import make_recording_client

        for case, options in enumerate(({"content_type": "text/x-a"}, {"guess_mime_type": False})):
            client, calls = make_recording_client([{}, {}])
            results: list[OpResult] = []
            S3().cp(
                str(self._tree(tmp_path / f"case{case}")),
                S3Storage("s3://b/p/", client=client),
                recursive=True,
                transfer_config=TransferConfig(use_threads=False),
                on_result=results.append,
                **options,
            )
            assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED, OpOutcome.SUCCEEDED]
            assert [c.operation for c in calls] == ["PutObject", "PutObject"]

    def test_a_stream_source_fails_the_item_and_releases_nothing_twice(self) -> None:
        import io

        from boto3_s3 import S3, BatchError, IOStorage, OpResult, S3Storage
        from boto3_s3.transferconfig import TransferConfig
        from tests.utils.recorder import make_recording_client

        client, _ = make_recording_client([])
        buf = io.BytesIO(b"x")
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            S3().cp(
                IOStorage(buf),
                S3Storage("s3://b/k", client=client),
                transfer_config=TransferConfig(use_threads=False),
                on_result=results.append,
            )
        assert len(results) == 1 and results[0].error is not None
        assert not buf.closed


class TestHostOverlays:
    def test_a_local_mime_types_file_still_applies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws reads the host's mime.types on top of its frozen table, so pinning
        # the table must not cost that: a local file both adds extensions and
        # overrides the frozen answer.
        local = tmp_path / "mime.types"
        local.write_text(
            "application/x-boto3s3-probe   b3sprobe\ntext/x-boto3s3-override       php\n"
        )
        monkeypatch.setattr(transfer, "_mime_db", None)
        monkeypatch.setattr(mimetable, "KNOWNFILES", [str(tmp_path / "absent.types"), str(local)])
        assert _guess_content_type("f.b3sprobe") == "application/x-boto3s3-probe"
        assert _guess_content_type("f.php") == "text/x-boto3s3-override"
        # Untouched extensions keep the frozen answer.
        assert _guess_content_type("f.weba") == "audio/webm"
