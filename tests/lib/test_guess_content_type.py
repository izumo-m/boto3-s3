"""The `Content-Type` an upload guesses: aws's table, not the host interpreter's.

aws-cli's official distribution is frozen against CPython 3.14, so that
interpreter's MIME table is the answer boto3-s3 has to reproduce on every
supported host - `boto3_s3.mimetable` carries it. The rows below are the
extensions where a host table disagrees with 3.14's in one direction or the
other, so at least one of them is load-bearing on any given interpreter.

Oracle: the type aws 2.36.1 actually stored, read back with HeadObject after
`cp` to MinIO (probes/wire/probe_content_type.py, and every candidate extension
at once in probes/fixA/p2-all-extension-content-type.py).
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

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
