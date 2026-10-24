"""Unit tests for boto3_s3_cli.paramfile: text encoding, and the error shape.

The encoding half pins the aws-cli ``compat.getpreferredencoding`` port:
``AWS_CLI_FILE_ENCODING`` wins, anything else falls to the CLI's text encoding
(``textencoding``; a ``C`` / ``POSIX`` ``LC_CTYPE`` reads as UTF-8, verified
against the pinned aws-cli under ``LC_ALL=C``). The error half pins which exit
code a load failure carries.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from boto3_s3 import ValidationError
from boto3_s3_cli import paramfile, textencoding


class TestTextEncoding:
    def test_env_var_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", "latin-1")
        assert paramfile._text_encoding() == "latin-1"

    def test_empty_env_var_is_present_and_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Present-wins like aws: an empty AWS_CLI_FILE_ENCODING reaches open()
        # verbatim and fails there as an unknown codec, never reads as unset.
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", "")
        assert paramfile._text_encoding() == ""

    def test_without_it_the_cli_text_encoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli's getpreferredencoding past its override: the encoding the CLI
        # writes its output in (textencoding's own tests pin how it is chosen).
        monkeypatch.delenv("AWS_CLI_FILE_ENCODING", raising=False)
        monkeypatch.setattr(textencoding, "text_encoding", lambda: "utf-8-sentinel")
        assert paramfile._text_encoding() == "utf-8-sentinel"

    @pytest.mark.skipif(sys.platform == "win32", reason="the C locale is POSIX's")
    def test_utf8_paramfile_readable_under_c_locale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The end-to-end shape of the divergence: non-ASCII UTF-8 content read
        # through file:// under LC_CTYPE=C decodes like aws-cli (rc 0 measured on
        # the pinned aws-cli) instead of failing the ASCII locale default.
        ref = tmp_path / "val.txt"
        ref.write_text("こんにちは", encoding="utf-8")
        monkeypatch.delenv("AWS_CLI_FILE_ENCODING", raising=False)
        monkeypatch.setattr(textencoding.locale, "setlocale", lambda category: "C")
        loaded = paramfile.read_text_paramfile(f"file://{ref}", operation="cp")
        assert loaded == "こんにちは"


class TestBadCodec:
    """An unusable `AWS_CLI_FILE_ENCODING` is the interpreter's own `LookupError`.

    It reaches the user through the general handler (rc 255).
    """

    def _load(self, tmp_path: Path) -> str:
        ref = tmp_path / "val.txt"
        ref.write_text("5\n")
        return paramfile.read_text_paramfile(f"file://{ref}", operation="ls")

    @pytest.mark.parametrize("codec", ["bogus-codec", ""])
    def test_an_unknown_codec_is_left_verbatim(
        self, codec: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Including the empty present-wins value.
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", codec)
        with pytest.raises(LookupError):
            self._load(tmp_path)

    def test_it_is_not_a_paramfile_load_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli's `get_file` catches only the decode and OS failures, so the
        # codec one escapes the named-argument boundary as well: rc 255 bare,
        # never the rc-252 `Error parsing parameter` form.
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", "rot13")
        with pytest.raises(LookupError) as excinfo:
            with paramfile.named_argument("--metadata", operation="cp"):
                self._load(tmp_path)
        assert not isinstance(excinfo.value, ValidationError)


class TestErrorShape:
    """A load failure is reported as the parse error of the argument naming it.

    Every caller loads inside `named_argument`, so a missing file is rc 252
    wherever it is named (aws-cli's shorthand ``@=`` alone reports it at 255).
    """

    def test_named_argument_wraps_it_as_the_parse_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as excinfo:
            with paramfile.named_argument("--metadata", operation="cp"):
                paramfile.get_paramfile(f"file://{tmp_path}/no-such-file", operation="cp")
        assert not isinstance(excinfo.value, paramfile.ParamfileLoadError)
        assert str(excinfo.value).startswith(
            "Error parsing parameter '--metadata': Unable to load paramfile file://"
        )

    def test_named_argument_passes_success_through(self, tmp_path: Path) -> None:
        ref = tmp_path / "val.txt"
        ref.write_text("ok")
        with paramfile.named_argument("--metadata", operation="cp"):
            assert paramfile.get_paramfile(f"file://{ref}", operation="cp") == "ok"
