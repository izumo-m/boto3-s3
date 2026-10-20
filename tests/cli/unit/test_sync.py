"""Unit tests for the ``boto3-s3 sync`` subcommand (dispatch + exit codes).

The rc shape (design/cli.md section 6): the local-local pair, any ``-`` path,
the checksum/path-format pairing, and an
S3 Express directory bucket on either side are all 252; a missing local
source is 255; ``--recursive`` and
``--expected-size`` do not exist on sync (Unknown options, 252). The output
shapes pin the delete line (one endpoint, no ``to`` clause), its dryrun and
``--quiet`` forms, and the both-roots filter compilation that shields
excluded destination objects from ``--delete``.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context
from tests.utils.fakes3 import MTIME, listing
from tests.utils.harness import built_client_ctx
from tests.utils.host import skip_if_chmod_is_inert
from tests.utils.recorder import make_recording_client, ops

_SERIAL = TransferConfig(use_threads=False)


def _ctx(client: Any) -> Context:
    return Context(client_factory=lambda _args: client, transfer_config=_SERIAL)


class TestUsageErrors:
    def test_local_to_local_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["sync", "a", "b"], ctx=built_client_ctx())
        assert rc == 252
        assert "Invalid argument type" in capsys.readouterr().err

    @pytest.mark.parametrize("argv", [["sync", "-", "s3://b/p"], ["sync", "s3://b/p", "-"]])
    def test_any_stream_path_is_252(
        self, argv: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(argv, ctx=built_client_ctx())
        assert rc == 252
        assert "Streaming" in capsys.readouterr().err

    def test_checksum_pairing_is_checked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The pairing rule itself is pinned in test_cp; this is sync's wiring.
        rc = cli.main(
            ["sync", "s3://b/p", str(tmp_path), "--checksum-algorithm", "CRC32"],
            ctx=built_client_ctx(),
        )
        assert rc == 252
        assert "checksum-algorithm" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "paths",
        [["src", "s3://b/up"], ["s3://b/cs", "s3://b/cd"]],
        ids=["upload", "copy"],
    )
    def test_a_malformed_grant_is_refused_before_the_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        paths: list[str],
    ) -> None:
        # Before any listing, --dryrun or not: aws finds it only as it maps
        # the first upload or copy, a fatal error at rc 1 (and rc 0 when
        # nothing needs transferring).
        monkeypatch.chdir(tmp_path)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.txt").write_bytes(b"x")
        rc = cli.main(["sync", *paths, "--grants", "invalid", "--dryrun"], ctx=built_client_ctx())
        assert rc == 252
        assert "--grants" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "argv",
        [
            ["sync", "s3://mybucket--use1-az4--x-s3/p", "s3://plain/p"],
            ["sync", "s3://plain/p", "s3://mybucket--use1-az4--x-s3/p"],
            ["sync", "s3://mybucket--use1-az4--x-s3", "dl"],
            # An access point of a directory bucket: aws runs it (its test is
            # --x-s3 only) and --delete then loses objects; refused here.
            ["sync", "s3://myap--use1-az4--xa-s3/p", "s3://plain/p"],
            ["sync", "s3://myap--use1-az4--xa-s3", "dl"],
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
        rc = cli.main(argv, ctx=built_client_ctx())
        assert rc == 252
        assert "directory bucket" in capsys.readouterr().err

    @pytest.mark.parametrize("flag", [["--recursive"], ["--expected-size", "5"]])
    def test_cp_only_options_are_unknown_here(
        self, flag: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["sync", "s3://b/p", "s3://b/q", *flag], ctx=built_client_ctx())
        assert rc == 252
        assert "Unknown options" in capsys.readouterr().err


class TestGeneralErrors:
    def test_missing_local_source_is_255(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "nope"
        rc = cli.main(["sync", str(missing), "s3://b/p"], ctx=built_client_ctx())
        assert rc == 255
        assert str(missing) in capsys.readouterr().err

    def test_dest_dir_creation_failure_is_255(self, tmp_path: Path) -> None:
        # aws creates the s3local dest dir during validation (before run); an
        # OSError there is rc 255, not the transfer pipeline's rc 1. The dest's
        # parent is a regular file, so makedirs raises NotADirectoryError with
        # the built client still untouched.
        afile = tmp_path / "afile"
        afile.write_bytes(b"x")
        dest = afile / "sub"
        rc = cli.main(["sync", "s3://b/pre", str(dest)], ctx=built_client_ctx())
        assert rc == 255

    @pytest.mark.parametrize(
        "argv",
        [
            ["sync", "{src}", "s3://b/p", "--delete"],
            ["cp", "{src}", "s3://b/p", "--recursive"],
            ["mv", "{src}", "s3://b/p", "--recursive"],
        ],
        ids=["sync", "cp", "mv"],
    )
    def test_a_file_as_a_directory_source_stops_the_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
    ) -> None:
        # The walk would skip the root, leaving an empty source - aws warns
        # (rc 2) and a sync --delete then empties the destination. Here the
        # run stops before it starts, like a missing source: nothing listed,
        # sent or deleted.
        src = tmp_path / "afile.txt"
        src.write_bytes(b"x")
        rc = cli.main([a.format(src=src) for a in argv], ctx=built_client_ctx())
        assert rc == 255
        err = capsys.readouterr().err
        assert f"The user-provided path {src} cannot be walked as a directory." in err
        assert "not a directory" in err

    def test_a_symlink_root_not_followed_stops_the_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws skips the root in silence (rc 0) and a --delete empties the
        # destination; here the run stops before anything is deleted.
        real = tmp_path / "real"
        real.mkdir()
        (real / "a.txt").write_bytes(b"x")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        rc = cli.main(
            ["sync", str(link), "s3://b/p", "--delete", "--no-follow-symlinks"],
            ctx=built_client_ctx(),
        )
        assert rc == 255
        assert "symbolic link" in capsys.readouterr().err

    def test_a_source_found_only_through_a_symlinked_dotdot_stops_the_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `link/../sub` exists to the kernel (link -> real/sub), but the walk
        # reads the lexical absolute form, `<tmp>/sub`, which names nothing.
        (tmp_path / "real" / "sub").mkdir(parents=True)
        (tmp_path / "link").symlink_to(tmp_path / "real" / "sub", target_is_directory=True)
        src = os.path.join(str(tmp_path / "link"), os.pardir, "sub")
        rc = cli.main(["sync", src, "s3://b/p", "--delete"], ctx=built_client_ctx())
        assert rc == 255
        assert f"It does not exist as {tmp_path / 'sub'}." in capsys.readouterr().err

    def test_a_followed_symlink_root_is_walked(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        (real / "a.txt").write_bytes(b"x")
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        client, calls = make_recording_client([listing(), {}])
        rc = cli.main(["sync", str(link), "s3://b/p", "--no-progress"], ctx=_ctx(client))
        assert rc == 0
        assert ops(calls) == ["ListObjectsV2", "PutObject"]

    @skip_if_chmod_is_inert
    def test_an_unreadable_directory_source_stops_the_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "locked"
        src.mkdir()
        src.chmod(0)
        try:
            rc = cli.main(["sync", str(src), "s3://b/p", "--delete"], ctx=built_client_ctx())
        finally:
            src.chmod(0o700)
        assert rc == 255
        assert "cannot be walked as a directory" in capsys.readouterr().err

    def test_a_refused_run_leaves_no_destination_directory(self, tmp_path: Path) -> None:
        # aws creates the destination in the middle of its validation and
        # leaves it behind when a later check refuses the run (here an SSE-C
        # pairing a download cannot use); it is created only once every check
        # has passed.
        dest = tmp_path / "out"
        rc = cli.main(
            ["sync", "s3://b/pre", str(dest), "--sse-c-copy-source", "AES256"],
            ctx=built_client_ctx(),
        )
        assert rc == 252
        assert not dest.exists()

    @pytest.mark.parametrize("bucket", ["b--use1-az4--x-s3", "ap--use1-az4--xa-s3"])
    def test_a_directory_bucket_refusal_leaves_no_destination_directory(
        self, tmp_path: Path, bucket: str
    ) -> None:
        dest = tmp_path / "out"
        rc = cli.main(["sync", f"s3://{bucket}/pre", str(dest)], ctx=built_client_ctx())
        assert rc == 252
        assert not dest.exists()


class TestOutputShapes:
    def test_upload_line_and_rc(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"x")
        client, calls = make_recording_client([listing(), {}])
        rc = cli.main(["sync", str(src), "s3://b/p", "--no-progress"], ctx=_ctx(client))
        assert rc == 0
        assert ops(calls) == ["ListObjectsV2", "PutObject"]
        assert "upload: " in capsys.readouterr().out

    def test_s3_delete_line_has_no_to_clause(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client([listing(("p/extra.txt", 2)), {}])
        rc = cli.main(["sync", str(src), "s3://b/p", "--delete", "--no-progress"], ctx=_ctx(client))
        assert rc == 0
        assert ops(calls) == ["ListObjectsV2", "DeleteObjects"]
        assert "delete: s3://b/p/extra.txt\n" in capsys.readouterr().out

    def test_local_delete_line_is_cwd_relative(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        out = tmp_path / "dl"
        out.mkdir()
        (out / "stale.txt").write_bytes(b"x")
        client, _calls = make_recording_client([listing()])
        rc = cli.main(["sync", "s3://b/p", "dl", "--delete", "--no-progress"], ctx=_ctx(client))
        assert rc == 0
        assert f"delete: dl{os.sep}stale.txt\n" in capsys.readouterr().out
        assert not (out / "stale.txt").exists()

    def test_dryrun_delete_line(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client([listing(("p/extra.txt", 2))])
        rc = cli.main(
            ["sync", str(src), "s3://b/p", "--delete", "--dryrun", "--no-progress"],
            ctx=_ctx(client),
        )
        assert rc == 0
        assert ops(calls) == ["ListObjectsV2"]
        assert "(dryrun) delete: s3://b/p/extra.txt\n" in capsys.readouterr().out

    def test_quiet_silences_delete_lines(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "src"
        src.mkdir()
        client, _calls = make_recording_client([listing(("p/extra.txt", 2)), {}])
        rc = cli.main(["sync", str(src), "s3://b/p", "--delete", "--quiet"], ctx=_ctx(client))
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_excluded_dest_objects_survive_delete(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The single source-root filter is applied to the destination stream
        # too (design/sync.md section 1), shielding excluded objects from --delete.
        # For this relative pattern that matches aws-cli's per-root filtering
        # (design/sync.md section 7 records the absolute-pattern edge as the only
        # divergence).
        src = tmp_path / "src"
        src.mkdir()
        client, calls = make_recording_client([listing(("p/extra.log", 2), ("p/extra.txt", 2)), {}])
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
        newer = (MTIME + timedelta(days=1)).timestamp()
        os.utime(target, (newer, newer))
        client, calls = make_recording_client([listing(("p/same.txt", 2))])
        rc = cli.main(
            ["sync", str(src), "s3://b/p", "--size-only", "--no-progress"], ctx=_ctx(client)
        )
        assert rc == 0
        assert ops(calls) == ["ListObjectsV2"]
        assert capsys.readouterr().out == ""
