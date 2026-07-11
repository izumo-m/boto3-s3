"""Unit tests for the ``boto3-s3 sync`` subcommand (dispatch + exit codes).

The rc shape (docs/cli.md section 6): the local-local pair, any ``-`` path,
the checksum/path-format pairing, and an
S3 Express directory bucket on either side are all 252 before any client
factory runs; a missing local source is 255; ``--recursive`` and
``--expected-size`` do not exist on sync (Unknown options, 252). The output
shapes pin the delete line (one endpoint, no ``to`` clause), its dryrun and
``--quiet`` forms, and the both-roots filter compilation that shields
excluded destination objects from ``--delete``.
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context
from tests.utils.recorder import ApiCall, make_recording_client

_SERIAL = TransferConfig(use_threads=False)
_MTIME = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _failing_factory_ctx() -> Context:
    def factory(_args: argparse.Namespace) -> Any:
        raise AssertionError("client factory must not be called on this path")

    return Context(client_factory=factory)


def _ctx(client: Any) -> Context:
    return Context(client_factory=lambda _args: client, transfer_config=_SERIAL)


def _listing(*entries: tuple[str, int]) -> dict[str, Any]:
    return {
        "Contents": [
            {"Key": key, "Size": size, "LastModified": _MTIME, "ETag": '"e"'}
            for key, size in entries
        ]
    }


def _ops(calls: list[ApiCall]) -> list[str]:
    return [call.operation for call in calls]


class TestUsageErrors:
    def test_local_to_local_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["sync", "a", "b"], ctx=_failing_factory_ctx())
        assert rc == 252
        err = capsys.readouterr().err
        assert "usage: boto3-s3 sync" in err
        assert "Error: Invalid argument type" in err

    @pytest.mark.parametrize("argv", [["sync", "-", "s3://b/p"], ["sync", "s3://b/p", "-"]])
    def test_any_stream_path_is_252(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(argv, ctx=_failing_factory_ctx())
        assert rc == 252
        assert (
            "Streaming currently is only compatible with non-recursive cp commands"
            in capsys.readouterr().err
        )

    def test_two_streams_hit_the_path_type_error_first(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["sync", "-", "-"], ctx=_failing_factory_ctx())
        assert rc == 252
        assert "Error: Invalid argument type" in capsys.readouterr().err

    def test_checksum_algorithm_rejects_downloads(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(
            ["sync", "s3://b/p", str(tmp_path), "--checksum-algorithm", "CRC32"],
            ctx=_failing_factory_ctx(),
        )
        assert rc == 252
        err = capsys.readouterr().err
        assert "An error occurred (ParamValidation):" in err
        assert "Expected checksum-algorithm parameter" in err
        assert "Instead, received <S3Uri> <LocalPath>." in err

    def test_checksum_mode_rejects_uploads(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(
            ["sync", str(tmp_path), "s3://b/p", "--checksum-mode", "ENABLED"],
            ctx=_failing_factory_ctx(),
        )
        assert rc == 252
        assert "Expected checksum-mode parameter" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            ["sync", "s3://mybucket--use1-az4--x-s3/p", "s3://plain/p"],
            ["sync", "s3://plain/p", "s3://mybucket--use1-az4--x-s3/p"],
            ["sync", "s3://mybucket--use1-az4--x-s3", "dl"],
        ],
    )
    def test_directory_buckets_are_rejected(
        self,
        argv: list[str],
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # chdir: aws (and this CLI, faithfully) pre-creates a local dest dir
        # during validation, before the directory-bucket rejection - keep the
        # "dl" byproduct out of the repo working tree.
        monkeypatch.chdir(tmp_path)
        rc = cli.main(argv, ctx=_failing_factory_ctx())
        assert rc == 252
        assert "Cannot use sync command with a directory bucket." in capsys.readouterr().err

    @pytest.mark.parametrize("flag", [["--recursive"], ["--expected-size", "5"]])
    def test_cp_only_options_are_unknown_here(
        self, flag: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["sync", "s3://b/p", "s3://b/q", *flag], ctx=_failing_factory_ctx())
        assert rc == 252
        assert "Unknown options" in capsys.readouterr().err


class TestGeneralErrors:
    def test_missing_local_source_is_255_before_the_factory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "nope"
        rc = cli.main(["sync", str(missing), "s3://b/p"], ctx=_failing_factory_ctx())
        assert rc == 255
        assert f"The user-provided path {missing} does not exist." in capsys.readouterr().err

    def test_non_integer_page_size_is_255(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(
            ["sync", "s3://b/p", "s3://b/q", "--page-size", "abc"], ctx=_failing_factory_ctx()
        )
        assert rc == 255
        assert "invalid literal" in capsys.readouterr().err

    def test_dest_dir_creation_failure_is_255_before_the_factory(self, tmp_path: Path) -> None:
        # aws creates the s3local dest dir during validation (before run); an
        # OSError there is rc 255, not the transfer pipeline's rc 1. The dest's
        # parent is a regular file, so makedirs raises NotADirectoryError before
        # any client is built.
        afile = tmp_path / "afile"
        afile.write_bytes(b"x")
        dest = afile / "sub"
        rc = cli.main(["sync", "s3://b/pre", str(dest)], ctx=_failing_factory_ctx())
        assert rc == 255

    def test_dest_dir_creation_beats_sse_c_misuse(self, tmp_path: Path) -> None:
        # aws-cli _validate_path_args (dest-dir makedirs, 255) runs BEFORE
        # _validate_sse_c_args (252). When both fail, the 255 wins.
        afile = tmp_path / "afile"
        afile.write_bytes(b"x")
        dest = afile / "sub"
        rc = cli.main(
            ["sync", "s3://b/pre", str(dest), "--sse-c", "AES256"], ctx=_failing_factory_ctx()
        )
        assert rc == 255


class TestOutputShapes:
    def test_upload_dryrun_validates_grants_before_reporting(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"x")
        client, calls = make_recording_client([_listing()])

        rc = cli.main(
            [
                "sync",
                str(src),
                "s3://b/p",
                "--grants",
                "invalid",
                "--dryrun",
                "--no-progress",
            ],
            ctx=_ctx(client),
        )

        captured = capsys.readouterr()
        assert rc == 1
        assert captured.out == ""
        assert captured.err == "fatal error: grants should be of the form permission=principal\n"
        assert _ops(calls) == ["ListObjectsV2"]

    def test_copy_dryrun_validates_grants_before_reporting(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client, calls = make_recording_client([_listing(("src/a.txt", 1)), _listing()])

        rc = cli.main(
            [
                "sync",
                "s3://b/src",
                "s3://b/dest",
                "--grants",
                "invalid",
                "--dryrun",
                "--no-progress",
            ],
            ctx=_ctx(client),
        )

        captured = capsys.readouterr()
        assert rc == 1
        assert captured.out == ""
        assert captured.err == "fatal error: grants should be of the form permission=principal\n"
        assert _ops(calls) == ["ListObjectsV2", "ListObjectsV2"]

    def test_upload_line_and_rc(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"x")
        client, calls = make_recording_client([_listing(), {}])
        rc = cli.main(["sync", str(src), "s3://b/p", "--no-progress"], ctx=_ctx(client))
        assert rc == 0
        assert _ops(calls) == ["ListObjectsV2", "PutObject"]
        assert "upload: " in capsys.readouterr().out

    def test_s3_delete_line_has_no_to_clause(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client([_listing(("p/extra.txt", 2)), {}])
        rc = cli.main(["sync", str(src), "s3://b/p", "--delete", "--no-progress"], ctx=_ctx(client))
        assert rc == 0
        assert _ops(calls) == ["ListObjectsV2", "DeleteObjects"]
        assert "delete: s3://b/p/extra.txt\n" in capsys.readouterr().out

    def test_local_delete_line_is_cwd_relative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "dl"
        out.mkdir()
        (out / "stale.txt").write_bytes(b"x")
        client, _calls = make_recording_client([_listing()])
        rc = cli.main(["sync", "s3://b/p", "dl", "--delete", "--no-progress"], ctx=_ctx(client))
        assert rc == 0
        assert f"delete: dl{os.sep}stale.txt\n" in capsys.readouterr().out
        assert not (out / "stale.txt").exists()

    def test_dryrun_delete_line(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client([_listing(("p/extra.txt", 2))])
        rc = cli.main(
            ["sync", str(src), "s3://b/p", "--delete", "--dryrun", "--no-progress"],
            ctx=_ctx(client),
        )
        assert rc == 0
        assert _ops(calls) == ["ListObjectsV2"]
        assert "(dryrun) delete: s3://b/p/extra.txt\n" in capsys.readouterr().out

    def test_quiet_silences_delete_lines(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        client, _calls = make_recording_client([_listing(("p/extra.txt", 2)), {}])
        rc = cli.main(["sync", str(src), "s3://b/p", "--delete", "--quiet"], ctx=_ctx(client))
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_excluded_dest_objects_survive_delete(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The single source-root filter is applied to the destination stream
        # too (sync.md section 1), shielding excluded objects from --delete.
        # For this relative pattern that matches aws-cli's per-root filtering
        # (sync.md section 7 records the absolute-pattern edge as the only
        # divergence).
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client(
            [_listing(("p/extra.log", 2), ("p/extra.txt", 2)), {}]
        )
        rc = cli.main(
            ["sync", str(src), "s3://b/p", "--delete", "--exclude", "*.log", "--no-progress"],
            ctx=_ctx(client),
        )
        assert rc == 0
        keys = [entry["Key"] for entry in calls[1].params["Delete"]["Objects"]]
        assert keys == ["p/extra.txt"]
        assert "delete: s3://b/p/extra.log" not in capsys.readouterr().out

    def test_size_only_flag_reaches_the_judgment(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        target = src / "same.txt"
        target.write_bytes(b"xx")
        newer = (_MTIME + timedelta(days=1)).timestamp()
        os.utime(target, (newer, newer))
        client, calls = make_recording_client([_listing(("p/same.txt", 2))])
        rc = cli.main(
            ["sync", str(src), "s3://b/p", "--size-only", "--no-progress"], ctx=_ctx(client)
        )
        assert rc == 0
        assert _ops(calls) == ["ListObjectsV2"]
        assert capsys.readouterr().out == ""

    def test_source_file_warns_and_exits_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A FILE as the sync source degrades to the walk warning
        # ("File does not exist." with the trailing separator), rc 2.
        src = tmp_path / "afile.txt"
        src.write_bytes(b"x")
        client, _calls = make_recording_client([_listing()])
        rc = cli.main(["sync", str(src), "s3://b/p", "--no-progress"], ctx=_ctx(client))
        assert rc == 2
        err = capsys.readouterr().err
        assert f"warning: Skipping file {src}{os.sep}. File does not exist." in err
