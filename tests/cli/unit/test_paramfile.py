"""Unit tests for boto3_s3_cli.paramfile's text-encoding resolution.

Pins the aws-cli ``compat.getpreferredencoding`` port: ``AWS_CLI_FILE_ENCODING``
wins, a ``C`` / ``POSIX`` ``LC_CTYPE`` reads as UTF-8 (verified against the
pinned aws-cli under ``LC_ALL=C``), anything else falls to the locale default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boto3_s3_cli import paramfile


class TestTextEncoding:
    def test_env_var_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", "latin-1")
        assert paramfile._text_encoding() == "latin-1"

    def test_empty_env_var_is_present_and_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Present-wins like aws: an empty AWS_CLI_FILE_ENCODING reaches open()
        # verbatim and fails there as an unknown codec, never reads as unset.
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", "")
        assert paramfile._text_encoding() == ""

    @pytest.mark.parametrize("lc_ctype", ["C", "POSIX"])
    def test_c_locale_reads_as_utf8(self, monkeypatch: pytest.MonkeyPatch, lc_ctype: str) -> None:
        # aws implements PEP 540's C/POSIX -> UTF-8 coercion itself (its
        # frozen build lacks the interpreter's); the port matches it where
        # PYTHONCOERCECLOCALE=0 would otherwise leave open() on ASCII.
        monkeypatch.delenv("AWS_CLI_FILE_ENCODING", raising=False)
        monkeypatch.setattr(paramfile.locale, "setlocale", lambda category: lc_ctype)
        assert paramfile._text_encoding() == "UTF-8"

    def test_normal_locale_falls_to_preferred(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWS_CLI_FILE_ENCODING", raising=False)
        monkeypatch.setattr(paramfile.locale, "setlocale", lambda category: "en_US.UTF-8")
        monkeypatch.setattr(paramfile.locale, "getpreferredencoding", lambda: "utf-8-sentinel")
        assert paramfile._text_encoding() == "utf-8-sentinel"

    def test_utf8_paramfile_readable_under_c_locale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The end-to-end shape of the divergence: non-ASCII UTF-8 content read
        # through file:// under LC_CTYPE=C decodes like aws (rc 0 measured on
        # the pinned aws-cli) instead of failing the ASCII locale default.
        ref = tmp_path / "val.txt"
        ref.write_text("こんにちは", encoding="utf-8")
        monkeypatch.delenv("AWS_CLI_FILE_ENCODING", raising=False)
        monkeypatch.setattr(paramfile.locale, "setlocale", lambda category: "C")
        loaded = paramfile.read_text_paramfile(f"file://{ref}", name="--metadata", operation="cp")
        assert loaded == "こんにちは"
