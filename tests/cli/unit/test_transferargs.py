"""CLI-layer shared transfer-argument pieces (``transferargs``).

The ``--no-overwrite`` gate (``validate_no_overwrite_supported``): the
upload/copy parallel of the streaming rejection - on a botocore without S3
conditional writes it must reject ``--no-overwrite`` with a ``ValidationError``
(rc 252) for ``locals3`` / ``s3s3`` and stay out of the way for the routes that
never send ``IfNoneMatch`` (``s3local`` download, and ``sync``, which pops the
flag before transfer). Plus ``identify_type`` (aws-cli's
``FileFormat.identify_type``: string classification is the CLI's job) and the
``file://`` paramfile resolution.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from boto3_s3.exceptions import InvalidValueError, ValidationError
from boto3_s3_cli.commands import transferargs
from tests.utils.fakemodel import model_only_client


def test_rejects_upload_on_old_botocore() -> None:
    client = model_only_client(set())
    with pytest.raises(ValidationError, match=r"1\.35\.16"):
        transferargs.validate_no_overwrite_supported(True, "locals3", client, operation="cp")


def test_rejects_copy_on_old_botocore() -> None:
    client = model_only_client({"PutObject"})  # upload ok, copy needs 1.41.0
    with pytest.raises(ValidationError, match=r"1\.41\.0"):
        transferargs.validate_no_overwrite_supported(True, "s3s3", client, operation="cp")


def test_skips_download_route() -> None:
    # s3local never sends IfNoneMatch; must stay usable on an old botocore.
    transferargs.validate_no_overwrite_supported(
        True, "s3local", model_only_client(set()), operation="cp"
    )


def test_skips_when_flag_absent() -> None:
    transferargs.validate_no_overwrite_supported(
        False, "locals3", model_only_client(set()), operation="cp"
    )


def test_allows_upload_when_supported() -> None:
    client = model_only_client({"PutObject"})
    transferargs.validate_no_overwrite_supported(True, "locals3", client, operation="cp")


class TestIdentifyType:
    def test_s3_scheme(self) -> None:
        assert transferargs.identify_type("s3://bucket/key") == "s3"

    def test_bare_bucket_is_local(self) -> None:
        # Only the literal scheme marks S3 (aws identify_type); a bare
        # "bucket/key" is a local path for cp purposes.
        assert transferargs.identify_type("bucket/key") == "local"

    def test_scheme_is_case_sensitive(self) -> None:
        assert transferargs.identify_type("S3://bucket") == "local"


class TestParamfile:
    """aws applies file:// (text) / fileb:// (binary) paramfile resolution to s3 args."""

    def test_text_file_prefix_loads_contents(self, tmp_path: Path) -> None:
        p = tmp_path / "ct.txt"
        p.write_text("text/x-from-file")
        assert (
            transferargs.resolve_text_paramfile(f"file://{p}", "--content-type", operation="cp")
            == "text/x-from-file"
        )

    def test_no_prefix_passes_through_verbatim(self) -> None:
        # A non-prefixed value (e.g. an http:// website redirect) is untouched.
        assert (
            transferargs.resolve_text_paramfile(
                "http://example.com", "--website-redirect", operation="cp"
            )
            == "http://example.com"
        )

    def test_blob_file_prefix_loads_text(self, tmp_path: Path) -> None:
        p = tmp_path / "key.txt"
        p.write_text("0123456789abcdef0123456789abcdef")
        assert transferargs.blob_value(f"file://{p}", "--sse-c-key", operation="cp") == (
            "0123456789abcdef0123456789abcdef"
        )

    def test_blob_fileb_prefix_loads_bytes(self, tmp_path: Path) -> None:
        p = tmp_path / "key.bin"
        p.write_bytes(b"\x00\x01\x02binary-key")
        assert transferargs.blob_value(f"fileb://{p}", "--sse-c-key", operation="cp") == (
            b"\x00\x01\x02binary-key"
        )

    def test_missing_text_paramfile_is_252(self) -> None:
        with pytest.raises(ValidationError, match="Unable to load paramfile"):
            transferargs.resolve_text_paramfile(
                "file:///nonexistent/boto3_s3_xyz", "--content-type", operation="cp"
            )

    def test_fileb_on_a_string_option_is_a_param_validation(self, tmp_path: Path) -> None:
        # aws loads the fileb:// bytes and botocore rejects them for a
        # string-typed parameter (rc 252); the wording is byte-exact and
        # names the generic "input" parameter (measured against the pinned aws-cli).
        p = tmp_path / "ct.bin"
        p.write_bytes(b"hi\n")
        with pytest.raises(ValidationError) as excinfo:
            transferargs.resolve_text_paramfile(f"fileb://{p}", "--content-type", operation="cp")
        assert str(excinfo.value) == (
            "Parameter validation failed:\n"
            "Invalid type for parameter input, value: b'hi\\n', "
            "type: <class 'bytes'>, valid types: <class 'str'>"
        )

    def test_missing_fileb_on_a_string_option_is_the_load_252(self) -> None:
        with pytest.raises(ValidationError, match="Unable to load paramfile"):
            transferargs.resolve_text_paramfile(
                "fileb:///nonexistent/boto3_s3_xyz", "--content-type", operation="cp"
            )


class TestGrantsParamfile:
    """aws's URIArgumentHandler unwraps a length-1 --grants list, then applies
    the paramfile map; a longer list or a prefix-less value stays a list."""

    def test_single_element_file_prefix_is_unwrapped_and_loaded(self, tmp_path: Path) -> None:
        p = tmp_path / "g.txt"
        p.write_text("read=id=CANONICAL")
        args = argparse.Namespace(grants=[f"file://{p}"])
        transferargs._resolve_grants(args, operation="cp")
        assert args.grants == "read=id=CANONICAL"

    def test_single_element_fileb_prefix_loads_bytes(self, tmp_path: Path) -> None:
        p = tmp_path / "g.bin"
        p.write_bytes(b"read=id=X")
        args = argparse.Namespace(grants=[f"fileb://{p}"])
        transferargs._resolve_grants(args, operation="cp")
        assert args.grants == b"read=id=X"

    def test_single_element_missing_file_is_252(self) -> None:
        args = argparse.Namespace(grants=["file:///nonexistent/boto3_s3_grants"])
        with pytest.raises(ValidationError, match="Unable to load paramfile"):
            transferargs._resolve_grants(args, operation="cp")

    def test_single_element_without_prefix_stays_a_list(self) -> None:
        args = argparse.Namespace(grants=["read=id=CANONICAL"])
        transferargs._resolve_grants(args, operation="cp")
        assert args.grants == ["read=id=CANONICAL"]

    def test_multi_element_list_is_never_unwrapped(self, tmp_path: Path) -> None:
        # aws only unwraps a length-1 list, so a second element (even a file://)
        # keeps the whole thing a verbatim list.
        p = tmp_path / "g.txt"
        p.write_text("read=id=CANONICAL")
        args = argparse.Namespace(grants=["read=id=X", f"file://{p}"])
        transferargs._resolve_grants(args, operation="cp")
        assert args.grants == ["read=id=X", f"file://{p}"]

    def test_none_is_untouched(self) -> None:
        args = argparse.Namespace(grants=None)
        transferargs._resolve_grants(args, operation="cp")
        assert args.grants is None


class TestMetadataParamfile:
    """A whole-value --metadata paramfile: file:// text feeds the shorthand
    parse; fileb:// loads bytes and then aws crashes in its parser (rc 255)."""

    def test_whole_value_file_prefix_feeds_the_shorthand(self, tmp_path: Path) -> None:
        p = tmp_path / "m.txt"
        p.write_text("k=v")
        args = argparse.Namespace(metadata=f"file://{p}")
        transferargs.resolve_metadata_option(args, operation="cp")
        assert args.metadata == {"k": "v"}

    def test_whole_value_fileb_prefix_existing_is_255(self, tmp_path: Path) -> None:
        p = tmp_path / "m.bin"
        p.write_bytes(b"k=v")
        args = argparse.Namespace(metadata=f"fileb://{p}")
        with pytest.raises(InvalidValueError) as excinfo:
            transferargs.resolve_metadata_option(args, operation="cp")
        assert str(excinfo.value) == "'in <string>' requires string as left operand, not int"

    def test_whole_value_fileb_missing_is_the_load_252(self) -> None:
        args = argparse.Namespace(metadata="fileb:///nonexistent/boto3_s3_md")
        with pytest.raises(ValidationError, match="Unable to load paramfile"):
            transferargs.resolve_metadata_option(args, operation="cp")

    def test_binary_file_via_text_prefix_hints_fileb(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = tmp_path / "k.bin"
        p.write_bytes(b"\xff\xfe\x00\x01")  # not valid UTF-8
        # Pin the decode to UTF-8 (aws's compat_open knob): the locale default
        # elsewhere may be a codec that decodes any byte (cp1252 on Windows).
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", "utf-8")
        with pytest.raises(ValidationError) as excinfo:
            transferargs.blob_value(f"file://{p}", "--sse-c-key", operation="cp")
        # Byte-exact aws wording (paramfile.get_file -> ParamError): the
        # expanded path in parentheses, two spaces after "decoded.".
        assert str(excinfo.value) == (
            f"Error parsing parameter '--sse-c-key': Unable to load paramfile ({p}), "
            "text contents could not be decoded.  If this is a binary file, please use "
            "the fileb:// prefix instead of the file:// prefix."
        )

    def test_tilde_and_var_are_expanded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws expands ~ and $VARs in the paramfile path (expandvars(expanduser)).
        (tmp_path / "ct.txt").write_text("text/x-home")
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows expanduser reads this, not HOME
        monkeypatch.setenv("BOTO3_S3_TESTDIR", str(tmp_path))
        assert (
            transferargs.resolve_text_paramfile("file://~/ct.txt", "--content-type", operation="cp")
            == "text/x-home"
        )
        assert (
            transferargs.resolve_text_paramfile(
                "file://$BOTO3_S3_TESTDIR/ct.txt", "--content-type", operation="cp"
            )
            == "text/x-home"
        )

    def test_aws_cli_file_encoding_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws's compat_open uses AWS_CLI_FILE_ENCODING; a latin-1 file with a
        # non-ASCII byte decodes under that encoding (it would fail under UTF-8).
        p = tmp_path / "ct.txt"
        p.write_bytes("caf\xe9".encode("latin-1"))  # 0xe9, invalid UTF-8
        monkeypatch.setenv("AWS_CLI_FILE_ENCODING", "latin-1")
        assert (
            transferargs.resolve_text_paramfile(f"file://{p}", "--content-type", operation="cp")
            == "caf\xe9"
        )
