"""Unit tests for boto3_s3_cli.paramfile: text encoding, and the error shape.

The encoding half pins the aws-cli ``compat.getpreferredencoding`` port:
``AWS_CLI_FILE_ENCODING`` wins, a ``C`` / ``POSIX`` ``LC_CTYPE`` reads as UTF-8
(verified against the pinned aws-cli under ``LC_ALL=C``), anything else falls to
the locale default. The error half pins which exit code a load failure carries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boto3_s3 import ValidationError
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
        loaded = paramfile.read_text_paramfile(f"file://{ref}", operation="cp")
        assert loaded == "こんにちは"


class TestBadCodecWording:
    """`AWS_CLI_FILE_ENCODING` failures word themselves like aws's build.

    Both failures reach the user as the interpreter's own `LookupError`
    through aws's general handler (rc 255, measured), so the wording is
    whatever Python produced - and Python changed it: up to 3.11 a codec that
    is not a text encoding carried a `; use codecs.open() ...` tail that
    aws's official distribution (3.14) does not print. Pinning the newer form
    keeps the report off the host's Python version.
    """

    def _load(self, tmp_path: Path) -> str:
        ref = tmp_path / "val.txt"
        ref.write_text("5\n")
        return paramfile.read_text_paramfile(f"file://{ref}", operation="ls")

    @pytest.mark.parametrize("codec", ["rot13", "base64"])
    def test_a_non_text_codec_drops_the_interpreter_hint(
        self, codec: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", codec)
        with pytest.raises(LookupError) as excinfo:
            self._load(tmp_path)
        assert str(excinfo.value) == f"'{codec}' is not a text encoding"

    @pytest.mark.parametrize("codec", ["bogus-codec", ""])
    def test_an_unknown_codec_is_left_verbatim(
        self, codec: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other LookupError `open` raises reads the same on every Python
        # (measured identical on the pinned aws-cli), so it must pass through
        # untouched - including the empty present-wins value.
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", codec)
        with pytest.raises(LookupError) as excinfo:
            self._load(tmp_path)
        assert str(excinfo.value) == f"unknown encoding: {codec}"

    def test_it_is_not_a_paramfile_load_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws's `get_file` catches only the decode and OS failures, so the
        # codec one escapes the named-argument boundary as well: rc 255 bare,
        # never the rc-252 `Error parsing parameter` form.
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", "rot13")
        with pytest.raises(LookupError) as excinfo:
            with paramfile.named_argument("--metadata", operation="cp"):
                self._load(tmp_path)
        assert not isinstance(excinfo.value, ValidationError)


class TestErrorShape:
    """Which exception a load failure becomes - aws's two paths.

    aws raises ``ResourceLoadingError`` inside ``get_paramfile`` and catches it
    only in the ``load-cli-arg`` handler it registers for *named* arguments, so
    the same missing file is a bare rc-255 report or the named argument's rc-252
    parse error depending on the caller (both measured on the pinned aws-cli via
    ``--metadata k@=file:///no/x`` and ``--metadata file:///no/x``).
    """

    def test_unwrapped_load_failure_is_the_general_error(self, tmp_path: Path) -> None:
        with pytest.raises(paramfile.ParamfileLoadError) as excinfo:
            paramfile.get_paramfile(f"file://{tmp_path}/no-such-file", operation="cp")
        assert str(excinfo.value).startswith("Unable to load paramfile file://")

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
