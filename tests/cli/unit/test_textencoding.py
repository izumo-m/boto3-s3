"""Unit tests for boto3_s3_cli.textencoding: the encoding aws uses, and where it applies.

aws decides its text encoding from the locale whatever Python's UTF-8 mode
says (measured against the pinned aws-cli on Linux C.UTF-8 / C / POSIX /
ja_JP.EUC-JP and Windows cp932): UTF-8 under a C or POSIX ``LC_CTYPE``, the
locale's own encoding otherwise, the ANSI code page on Windows. These pin that
choice, the platforms where local names and the command line follow it, and
the stream setup that writes output in it.
"""

from __future__ import annotations

import ctypes
import io
import locale
import os
import sys
from types import SimpleNamespace

import pytest

from boto3_s3_cli import textencoding


def _stream(encoding: str, errors: str = "strict") -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors)


class TestTextEncoding:
    @pytest.mark.parametrize("lc_ctype", ["C", "POSIX"])
    def test_c_and_posix_read_as_utf8(self, monkeypatch: pytest.MonkeyPatch, lc_ctype: str) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(locale, "setlocale", lambda category: lc_ctype)
        assert textencoding.text_encoding() == "UTF-8"

    def test_otherwise_the_locales_own_encoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Read past UTF-8 mode: aws runs in the locale's encoding.
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(locale, "setlocale", lambda category: "ja_JP.EUC-JP")
        monkeypatch.setattr(locale, "nl_langinfo", lambda item: "EUC-JP", raising=False)
        monkeypatch.setattr(locale, "CODESET", 14, raising=False)  # absent on Windows
        assert textencoding.text_encoding() == "EUC-JP"

    def test_windows_takes_the_ansi_code_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kernel32 = SimpleNamespace(GetACP=lambda: 932)
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
        assert textencoding.text_encoding() == "cp932"


class TestFilenameEncoding:
    @pytest.mark.parametrize("platform", ["win32", "darwin"])
    def test_none_where_python_reads_names_as_aws_does(
        self, monkeypatch: pytest.MonkeyPatch, platform: str
    ) -> None:
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(textencoding, "text_encoding", lambda: "euc_jp")
        assert textencoding.filename_encoding() is None

    def test_the_text_encoding_on_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(textencoding, "text_encoding", lambda: "EUC-JP")
        assert textencoding.filename_encoding() == "EUC-JP"

    def test_none_for_a_codec_python_lacks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(textencoding, "text_encoding", lambda: "no-such-codec")
        assert textencoding.filename_encoding() is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows argv is Unicode: nothing to re-decode")
class TestDecodeArgv:
    def test_arguments_are_read_with_the_cli_encoding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = (
            "euc_jp" if sys.getfilesystemencoding().lower().replace("-", "") == "utf8" else "utf-8"
        )
        monkeypatch.setattr(textencoding, "filename_encoding", lambda: other)
        typed = os.fsdecode("s3://b/日本.txt".encode(other))
        assert textencoding.decode_argv(["ls", typed]) == ["ls", "s3://b/日本.txt"]

    def test_unchanged_without_a_filename_encoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(textencoding, "filename_encoding", lambda: None)
        assert textencoding.decode_argv(["\udce6x"]) == ["\udce6x"]


class TestStdStreams:
    def test_streams_take_the_cli_encoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out, err = _stream("utf-8", "surrogateescape"), _stream("utf-8", "backslashreplace")
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(textencoding, "text_encoding", lambda: "EUC-JP")
        monkeypatch.setattr(locale, "setlocale", lambda category: "ja_JP.EUC-JP")
        textencoding.configure_std_streams()
        # aws's stdout is strict there, so uni_write's '?' takes over.
        assert (out.encoding, out.errors) == ("euc_jp", "strict")
        assert (err.encoding, err.errors) == ("euc_jp", "backslashreplace")

    def test_a_stream_already_in_it_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        out = _stream("utf-8", "surrogateescape")
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", None)
        monkeypatch.setattr(textencoding, "text_encoding", lambda: "UTF-8")
        textencoding.configure_std_streams()
        assert (out.encoding, out.errors) == ("utf-8", "surrogateescape")


class TestErrorReport:
    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({"AWS_CLI_OUTPUT_ENCODING": "UTF-8"}, "UTF-8"),
            ({"AWS_CLI_OUTPUT_ENCODING": "latin-1", "PYTHONUTF8": "1"}, "latin-1"),
            ({"PYTHONUTF8": "1"}, "UTF-8"),
            ({"PYTHONUTF8": "0"}, "euc_jp"),
        ],
    )
    def test_aws_preferred_output_encoding(
        self, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
    ) -> None:
        monkeypatch.delenv("AWS_CLI_OUTPUT_ENCODING", raising=False)
        monkeypatch.delenv("PYTHONUTF8", raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        stream = _stream("euc_jp")
        textencoding.prepare_error_report(stream)
        assert stream.encoding == expected
