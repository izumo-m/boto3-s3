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
import sys
from pathlib import Path

import pytest

from boto3_s3.exceptions import InvalidValueError, ValidationError
from boto3_s3.types import CaseConflictMode
from boto3_s3_cli.commands import transferargs
from tests.utils.fakemodel import model_only_client


def test_rejects_upload_on_old_botocore() -> None:
    client = model_only_client(set())
    with pytest.raises(ValidationError, match=r"1\.35\.2"):
        transferargs.validate_no_overwrite_supported(True, "locals3", client, operation="cp")


def test_rejects_copy_on_old_botocore() -> None:
    client = model_only_client({"PutObject"})  # upload ok, copy needs 1.40.61
    with pytest.raises(ValidationError, match=r"1\.40\.61"):
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


class TestBuildS3Storage:
    """The strict aws-cli validation plus its one bucket-less carve-out.

    A key with no bucket must reach the API the way aws lets it (the
    ``validate`` pass-through); every other malformed form must still be
    rejected here, ahead of the pipeline.
    """

    def test_bucketless_key_skips_validation(self) -> None:
        storage = transferargs.build_s3_storage("s3:///k", client=object())
        assert (storage.bucket, storage.key) == ("", "k")
        storage.validate()  # the carve-out: no up-front rejection

    def test_bucketless_key_schemeless_skips_validation(self) -> None:
        # presign takes the scheme-less spelling of the same URI; the split
        # must read it as S3Storage's own constructor does.
        storage = transferargs.build_s3_storage("/k", client=object())
        assert (storage.bucket, storage.key) == ("", "k")
        storage.validate()

    def test_service_root_is_not_the_carve_out(self) -> None:
        # No key, so nothing is carved out - and validate() accepts it.
        storage = transferargs.build_s3_storage("s3://", client=object())
        assert not isinstance(storage, transferargs._BucketlessS3Storage)
        assert (storage.bucket, storage.key) == ("", "")
        storage.validate()

    def test_bucket_and_key_is_validated_normally(self) -> None:
        storage = transferargs.build_s3_storage("b/k", client=object())
        assert not isinstance(storage, transferargs._BucketlessS3Storage)
        assert (storage.bucket, storage.key) == ("b", "k")

    def test_object_lambda_arn_is_still_rejected(self) -> None:
        arn = "arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/my-olap"
        with pytest.raises(ValidationError, match="S3 Object Lambda"):
            transferargs.build_s3_storage(f"s3://{arn}/k", client=object())

    def test_outposts_bucket_arn_is_still_rejected(self) -> None:
        arn = (
            "arn:aws:s3-outposts:us-west-2:123456789012:outpost/"
            "op-01234567890123456/bucket/my-bucket"
        )
        with pytest.raises(ValidationError, match="Outpost Bucket ARNs"):
            transferargs.build_s3_storage(f"s3://{arn}", client=object())

    def test_accesspoint_arn_stays_whole_in_the_bucket(self) -> None:
        arn = "arn:aws:s3:us-west-2:123456789012:accesspoint/endpoint"
        storage = transferargs.build_s3_storage(f"s3://{arn}", client=object())
        assert (storage.bucket, storage.key) == (arn, "")


_OBJECT_LAMBDA = "s3://arn:aws:s3-object-lambda:us-west-2:123456789012:accesspoint/olap"
_OUTPOSTS_BUCKET = (
    "s3://arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-01234567890123456/bucket/b"
)
_OUTPOSTS_AP = (
    "arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-01234567890123456/accesspoint/ap"
)


class TestNormalizeS3Path:
    """aws's ``_normalize_s3_trailing_slash`` on one argument."""

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("s3://b", "s3://b/"),
            ("s3://b/", "s3://b/"),
            ("s3://b/k", "s3://b/k"),
            ("s3://", "s3://"),
            ("s3:///k", "s3:///k"),
            ("local/dir", "local/dir"),
            ("-", "-"),
            (f"s3://{_OUTPOSTS_AP}", f"s3://{_OUTPOSTS_AP}/"),
            # The ":" closing the access point is eaten by the re-read, so the
            # appended "/" becomes the key.
            (f"s3://{_OUTPOSTS_AP}:", f"s3://{_OUTPOSTS_AP}:/"),
        ],
    )
    def test_keyless_paths_gain_the_bucket_root_slash(self, path: str, expected: str) -> None:
        assert transferargs.normalize_s3_path(path) == expected

    @pytest.mark.parametrize(
        ("path", "match"),
        [
            (f"{_OBJECT_LAMBDA}/k", "S3 Object Lambda"),
            (_OBJECT_LAMBDA, "S3 Object Lambda"),
            (f"{_OUTPOSTS_BUCKET}/k", "Outpost Bucket ARNs"),
        ],
    )
    def test_refused_arns_fail_here(self, path: str, match: str) -> None:
        with pytest.raises(ValidationError, match=match):
            transferargs.normalize_s3_path(path)


class TestNormalizedPathsAreUsed:
    """The commands build their locations from the normalized path, so an
    Outposts access point closed with ":" reads as the key "/" (measured:
    aws lists under "/" and heads "/")."""

    def test_rm_recursive_lists_under_the_key_slash(self) -> None:
        from tests.utils.harness import run_recorded

        result, calls = run_recorded(
            [{}], ["rm", f"s3://{_OUTPOSTS_AP}:", "--recursive", "--dryrun"]
        )
        assert result.rc == 0, result.stderr
        assert [(c.operation, c.params.get("Prefix")) for c in calls] == [("ListObjectsV2", "/")]

    def test_a_single_download_heads_the_key_slash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.utils.fakes3 import client_error
        from tests.utils.harness import run_recorded

        monkeypatch.chdir(tmp_path)
        result, calls = run_recorded(
            [client_error("404", 404, "HeadObject")],
            ["cp", f"s3://{_OUTPOSTS_AP}:", "out", "--dryrun"],
        )
        assert result.rc == 1
        assert [(c.operation, c.params.get("Key")) for c in calls] == [("HeadObject", "/")]


class TestRefusedArnsComeFirst:
    """aws refuses the unsupported ARNs right after the path-type check.

    Measured against the pinned aws: every other path check of cp / mv / sync
    / rm loses to it - the missing local source, the pre-created download
    directory, the stream rules, mv's same-path guard, the checksum and SSE-C
    pairings, sync's directory-bucket refusal - and so does rm's ``[s3]``
    config.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["cp", "nosuch", f"{_OBJECT_LAMBDA}/k"],
            ["mv", "nosuch", f"{_OUTPOSTS_BUCKET}/k"],
            ["sync", "nosuch", f"{_OBJECT_LAMBDA}/p"],
            ["cp", f"{_OBJECT_LAMBDA}/p", "out", "--recursive"],
            ["sync", f"{_OUTPOSTS_BUCKET}/p", "out"],
            ["mv", f"{_OBJECT_LAMBDA}/p", "out", "--recursive"],
            ["cp", "-", f"{_OBJECT_LAMBDA}/k", "--recursive"],
            ["cp", f"{_OBJECT_LAMBDA}/k", "-", "--no-overwrite"],
            ["mv", f"{_OBJECT_LAMBDA}/k", f"{_OBJECT_LAMBDA}/k"],
            ["cp", "f", f"{_OBJECT_LAMBDA}/k", "--checksum-mode", "ENABLED"],
            ["cp", "f", f"{_OBJECT_LAMBDA}/k", "--sse-c", "AES256"],
            ["sync", f"{_OBJECT_LAMBDA}/k", "s3://bkt--usw2-az1--x-s3/"],
            ["cp", f"{_OBJECT_LAMBDA}/k", f"{_OUTPOSTS_BUCKET}/k"],
        ],
    )
    def test_the_arn_refusal_wins(
        self, argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.utils.harness import client_ctx, run_cli_in_process

        monkeypatch.chdir(tmp_path)
        (tmp_path / "f").write_bytes(b"x")
        result = run_cli_in_process(argv, ctx=client_ctx(object()))
        assert result.rc == 252, result.stderr
        assert "S3 Object Lambda" in result.stderr or "Outpost Bucket ARNs" in result.stderr
        assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]

    @pytest.mark.parametrize("path", [f"{_OBJECT_LAMBDA}/k", f"{_OUTPOSTS_BUCKET}/k"])
    def test_rm_refuses_the_arn_before_reading_the_s3_config(
        self, path: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.utils.harness import client_ctx, run_cli_in_process

        config = tmp_path / "config"
        config.write_text("[default]\ns3 =\n  multipart_threshold = nope\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        result = run_cli_in_process(["rm", path, "--recursive"], ctx=client_ctx(object()))
        assert result.rc == 252, result.stderr
        assert "Invalid size value" not in result.stderr


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
    """A lone --grants file:// / fileb:// is refused unread (aws loads it and
    parses the text character by character - a bug not copied); a longer
    list or a prefix-less value stays a list."""

    @pytest.mark.parametrize("prefix", ["file://", "fileb://"])
    def test_single_element_paramfile_is_refused(self, prefix: str, tmp_path: Path) -> None:
        p = tmp_path / "g.txt"
        p.write_text("read=id=CANONICAL")
        args = argparse.Namespace(grants=[f"{prefix}{p}"])
        with pytest.raises(ValidationError, match="Error parsing parameter '--grants'"):
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
    parse; fileb:// is refused unread (252; aws crashes in its parser)."""

    def test_whole_value_file_prefix_feeds_the_shorthand(self, tmp_path: Path) -> None:
        p = tmp_path / "m.txt"
        p.write_text("k=v")
        args = argparse.Namespace(metadata=f"file://{p}")
        transferargs.resolve_metadata_option(args, operation="cp")
        assert args.metadata == {"k": "v"}

    @pytest.mark.parametrize("exists", [True, False])
    def test_whole_value_fileb_prefix_is_refused(self, exists: bool, tmp_path: Path) -> None:
        p = tmp_path / "m.bin"
        if exists:
            p.write_bytes(b"k=v")
        args = argparse.Namespace(metadata=f"fileb://{p}")
        with pytest.raises(ValidationError) as excinfo:
            transferargs.resolve_metadata_option(args, operation="cp")
        assert not isinstance(excinfo.value, InvalidValueError)
        assert "a fileb:// reference is not supported" in str(excinfo.value)

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


class TestExpressCaseConflictWarning:
    def test_warning_rides_uni_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The standing S3 Express warning has no trailing newline (aws's
        # measured byte shape), so it must ride the uni_print port - write plus
        # flush. A raw stderr.write sits in a block-buffered redirect until
        # process exit, inverting aws's output order.
        writes: list[tuple[object, str]] = []
        monkeypatch.setattr(
            transferargs, "uni_write", lambda stream, text: writes.append((stream, text))
        )
        args = argparse.Namespace(case_conflict="warn", recursive=True)
        mode = transferargs.resolve_case_conflict(
            args, "s3://b--use1-az4--x-s3/p/", "s3local", operation="cp"
        )
        assert mode is CaseConflictMode.IGNORE
        [(stream, text)] = writes
        assert stream is sys.stderr
        assert text.startswith("warning: Recursive copies/moves")
        assert not text.endswith("\n")

    @pytest.mark.parametrize("src", ["s3://b--use1-az4--x-s3/p/", "s3://ap--use1-az4--xa-s3/p/"])
    def test_directory_bucket_and_its_access_point_take_the_branch(
        self, src: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws takes this branch for --x-s3 only. An access point of a directory
        # bucket lists the same keys just as unordered, and under aws its keys
        # then pair wrongly - skip dropped objects that conflicted with nothing
        # (measured on real S3) - so the access point takes it here too.
        writes: list[str] = []
        monkeypatch.setattr(transferargs, "uni_write", lambda _stream, text: writes.append(text))
        for value in ("skip", "error"):
            args = argparse.Namespace(case_conflict=value, recursive=True)
            with pytest.raises(ValidationError, match="when operating on S3 Express"):
                transferargs.resolve_case_conflict(args, src, "s3local", operation="cp")
        args = argparse.Namespace(case_conflict="warn", recursive=True)
        mode = transferargs.resolve_case_conflict(args, src, "s3local", operation="cp")
        assert mode is CaseConflictMode.IGNORE
        assert len(writes) == 1

    @pytest.mark.parametrize("src", ["s3://plain/p/", "s3://ap-abc123-s3alias/p/"])
    def test_other_buckets_keep_the_mode(self, src: str) -> None:
        args = argparse.Namespace(case_conflict="skip", recursive=True)
        mode = transferargs.resolve_case_conflict(args, src, "s3local", operation="cp")
        assert mode is CaseConflictMode.SKIP


class TestBuildPrinter:
    """Every display flag reaches the printer it configures.

    The four flags share one construction site, so a wiring slip here silences
    or reshapes output for a whole command family while the printer's own tests
    stay green. ``--progress-multiline`` is the reason this class exists: its
    rendering is covered in ``test_progress.py``, but nothing pinned the flag
    that selects it.
    """

    @staticmethod
    def _args(**overrides: object) -> argparse.Namespace:
        defaults = {
            "quiet": False,
            "only_show_errors": False,
            "progress": True,
            "progress_multiline": False,
        }
        return argparse.Namespace(**{**defaults, **overrides})

    def test_defaults(self) -> None:
        printer = transferargs.build_printer(self._args(), 0)
        assert (printer._quiet, printer._only_show_errors) == (False, False)
        assert printer._show_progress
        assert not printer._multiline

    def test_progress_multiline_selects_the_multiline_renderer(self) -> None:
        printer = transferargs.build_printer(self._args(progress_multiline=True), 0)
        assert printer._multiline

    def test_no_progress_and_quiet_reach_the_printer(self) -> None:
        assert not transferargs.build_printer(self._args(progress=False), 0)._show_progress
        assert transferargs.build_printer(self._args(quiet=True), 0)._quiet

    def test_only_show_errors_ors_in_the_stream_rule(self) -> None:
        # cp's streaming download forces the errors-only printer even without
        # the flag, because the object bytes own stdout.
        assert transferargs.build_printer(self._args(), 0, only_show_errors=True)._only_show_errors
        assert transferargs.build_printer(self._args(only_show_errors=True), 0)._only_show_errors

    def test_progress_frequency_floors_at_the_repaint_minimum(self) -> None:
        # --progress-frequency 0 (aws's default) is raised to the repaint
        # floor; a larger value wins (aws-cli-option-handling.md section 6).
        assert transferargs.build_printer(self._args(), 0)._frequency == pytest.approx(0.1)
        assert transferargs.build_printer(self._args(), 5)._frequency == pytest.approx(5.0)
