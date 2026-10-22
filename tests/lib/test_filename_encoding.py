"""``filename_encoding`` (``LocalStorage`` / ``S3``): local names in a named encoding.

The storage holds every local name as text decoded with the named encoding
and hands the OS that text encoded with it; ``None`` (the default) and the
file system encoding itself convert nothing, and Windows - where the OS names
files in Unicode - ignores it. The conversion is pinned here with an encoding
that is not the file system's, on names written as that encoding's bytes.
"""

from __future__ import annotations

import codecs
import os
import sys
from pathlib import Path

import pytest

from boto3_s3.exceptions import InvalidValueError
from boto3_s3.localstorage import LocalStorage
from boto3_s3.s3 import S3
from boto3_s3.s3storage import S3Storage
from boto3_s3.types import LocalScanOptions, OpResult
from tests.utils.fakes3 import get_response, head_response, listing
from tests.utils.recorder import make_recording_client, ops

_FS_ENCODING = codecs.lookup(sys.getfilesystemencoding()).name
# An encoding other than the file system's, so the storage converts.
_OTHER = "euc_jp" if _FS_ENCODING == "utf-8" else "utf-8"

needs_byte_names = pytest.mark.skipif(
    os.name == "nt" or sys.platform == "darwin",
    reason="the OS names files in Unicode (Windows) or UTF-8 only (macOS)",
)


def _native(path: Path, *parts: str) -> str:
    """The OS form of ``path`` joined with ``parts`` written in `_OTHER`."""
    return os.path.join(str(path), *(os.fsdecode(part.encode(_OTHER)) for part in parts))


def _tree(path: Path) -> None:
    for parts in (("plain.txt",), ("日本語.txt",), ("表", "ソ.txt")):
        target = _native(path, *parts)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(b"x" * len(parts))


class TestConstruction:
    def test_an_unknown_codec_is_refused(self) -> None:
        with pytest.raises(InvalidValueError):
            LocalStorage(".", filename_encoding="no-such-codec")
        with pytest.raises(InvalidValueError):
            S3(filename_encoding="no-such-codec")

    def test_the_file_system_encoding_converts_nothing(self) -> None:
        storage = LocalStorage(".", filename_encoding=sys.getfilesystemencoding())
        name = "\udce6x"
        assert storage.os_path(name) is name
        assert storage.text_path(name) is name

    def test_windows_ignores_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        storage = LocalStorage(".", filename_encoding=_OTHER)
        assert storage.filename_encoding == _OTHER
        assert storage.os_path("日本") == "日本"


@needs_byte_names
class TestConversion:
    def test_the_walk_lists_text_in_text_order(self, tmp_path: Path) -> None:
        _tree(tmp_path)
        storage = LocalStorage(str(tmp_path), filename_encoding=_OTHER)
        infos = list(storage.scan(LocalScanOptions(recursive=True)))
        # Code point order of the text (S3's key order), not of the OS form.
        assert [info.compare_key for info in infos] == ["plain.txt", "日本語.txt", "表/ソ.txt"]
        assert infos[1].key == str(tmp_path).replace(os.sep, "/") + "/日本語.txt"
        one_level = list(storage.scan(LocalScanOptions(recursive=False)))
        assert [info.compare_key for info in one_level] == ["plain.txt", "日本語.txt", "表/"]

    def test_single_entry_io_takes_text(self, tmp_path: Path) -> None:
        _tree(tmp_path)
        storage = LocalStorage(str(tmp_path), filename_encoding=_OTHER)
        info = storage.get_fileinfo("表/ソ.txt")
        assert info is not None
        assert (info.compare_key, info.size) == ("ソ.txt", 2)
        with storage.open("新/名.txt", "wb") as handle:
            handle.write(b"new")
        assert os.path.exists(_native(tmp_path, "新", "名.txt"))
        with storage.open("新/名.txt", "rb") as handle:
            assert handle.read() == b"new"
        storage.delete(info)
        assert not os.path.exists(_native(tmp_path, "表", "ソ.txt"))
        assert storage.text_path(storage.os_path(info.key)) == info.key

    def test_warnings_name_text(self, tmp_path: Path) -> None:
        os.symlink(_native(tmp_path, "無い"), _native(tmp_path, "壊れ"))
        warnings: list[str] = []
        storage = LocalStorage(str(tmp_path), filename_encoding=_OTHER)
        assert list(storage.walk_local(on_warning=warnings.append)) == []
        assert len(warnings) == 1
        assert "壊れ" in warnings[0]

    def test_a_name_that_does_not_encode_back_is_skipped(self, tmp_path: Path) -> None:
        # cp932 reads both byte pairs as U+2252, which encodes back to 81 e0:
        # the 87 90 file would be addressed as the other one.
        for raw in (b"\x87\x90.txt", b"\x81\xe0.txt"):
            with open(os.path.join(os.fsencode(tmp_path), raw), "wb") as handle:
                handle.write(raw)
        warnings: list[str] = []
        storage = LocalStorage(str(tmp_path), filename_encoding="cp932")
        infos = list(storage.scan(LocalScanOptions(recursive=True, on_warning=warnings.append)))
        assert [info.compare_key for info in infos] == ["≒.txt"]
        assert infos[0].size == len(b"\x81\xe0.txt")
        assert len(warnings) == 1
        assert "cp932" in warnings[0]

    def test_transfers_name_s3_keys_and_local_files_as_text(self, tmp_path: Path) -> None:
        _tree(tmp_path)
        client, calls = make_recording_client([{}, {}, {}])
        results: list[OpResult] = []
        s3 = S3(filename_encoding=_OTHER)
        s3.cp(
            str(tmp_path),
            S3Storage("s3://bucket/up/", client=client),
            recursive=True,
            on_result=results.append,
        )
        assert sorted(call.params["Key"] for call in calls) == [
            "up/plain.txt",
            "up/日本語.txt",
            "up/表/ソ.txt",
        ]
        assert sorted(result.src or "" for result in results)[1].endswith("日本語.txt")

        client, calls = make_recording_client([head_response(), get_response()])
        dest = tmp_path / "dl"
        s3.cp(S3Storage("s3://b/d/新.txt", client=client), str(dest) + os.sep)
        assert ops(calls) == ["HeadObject", "GetObject"]
        with open(_native(dest, "新.txt"), "rb") as handle:
            assert handle.read() == b"payload"

    def test_a_key_that_climbs_out_once_encoded_is_skipped(self, tmp_path: Path) -> None:
        # cp037 (EBCDIC) encodes ACK ACK BEL as b"../": text with no ".." that
        # would land outside the destination is refused like a "../" key.
        dest = tmp_path / "dl"
        client, calls = make_recording_client([listing(("d/\x06\x06\x07x", 1))])
        results: list[OpResult] = []
        S3(filename_encoding="cp037").cp(
            S3Storage("s3://b/d/", client=client),
            str(dest),
            recursive=True,
            on_result=results.append,
        )
        assert ops(calls) == ["ListObjectsV2"]
        assert [result.outcome.name for result in results] == ["WARNED"]
        assert "parent directory" in str(results[0].error)
        assert not os.path.exists(os.path.join(os.fsencode(tmp_path), b"\xa7"))
