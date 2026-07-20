"""Unit tests for the ``boto3-s3 cp`` subcommand (dispatch + exit codes).

The rc shape (docs/cli.md section 6): pre-pipeline errors
keep their class - usage errors 252 (path types, SSE-C pairing, streaming
with --recursive / --no-overwrite, ``--metadata`` parsing, blob decoding,
the S3 Express case-conflict rejection), the bare integer conversion and
the missing local source 255 (both before the client factory) - while
everything the transfer pipeline raises is rc 1 (``<kind> failed:`` per
item, one ``fatal error:`` for run-killers - a non-integer
``--expected-size`` and a missing stdin included), and a warnings-only run
exits 2. The case-conflict advisories are uncounted NOTICEs that print
even under --quiet (aws's direct stderr prints).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context
from tests.utils.host import skip_if_chmod_is_inert
from tests.utils.recorder import ApiCall, make_recording_client

_SYNC = TransferConfig(use_threads=False)
# The case-conflict gate detects a "two S3 twins" conflict via its in-flight set,
# which only holds while the first twin's download is still running. That needs a
# threaded (non-blocking) submit running ahead of completions - aws-cli's own
# tests use a single worker (max_concurrent_requests = 1). _SYNC completes each
# twin before the next is judged, emptying the set.
_CASE_CONFLICT_CONFIG = TransferConfig(max_concurrency=1)


def _client_error(code: str, status: int, operation: str) -> ClientError:
    response: Any = {
        "Error": {"Code": code, "Message": "stub"},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return ClientError(response, operation)


def _recording_ctx(
    responses: list[dict[str, Any] | Exception],
    *,
    transfer_config: TransferConfig = _SYNC,
) -> tuple[Context, list[ApiCall]]:
    client, calls = make_recording_client(responses)
    ctx = Context(client_factory=lambda _args: client, transfer_config=transfer_config)
    return ctx, calls


def _failing_factory_ctx() -> Context:
    def factory(_args: argparse.Namespace) -> Any:
        raise AssertionError("client factory must not be called on this path")

    return Context(client_factory=factory)


class TestUsageErrors:
    def test_annotation_copy_mode_is_not_a_cli_option(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(
            [
                "cp",
                "s3://src-b/key",
                "s3://dest-b/key",
                "--annotation-copy-mode",
                "deferred",
            ],
            ctx=_failing_factory_ctx(),
        )
        assert rc == 252
        assert "Unknown options: --annotation-copy-mode,deferred" in capsys.readouterr().err

    def test_local_to_local_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["cp", "a.txt", "b.txt"], ctx=_failing_factory_ctx())
        assert rc == 252
        assert "Error: Invalid argument type" in capsys.readouterr().err

    def test_streaming_with_recursive_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["cp", "-", "s3://b/k", "--recursive"], ctx=_failing_factory_ctx())
        assert rc == 252
        assert (
            "Streaming currently is only compatible with non-recursive cp commands"
            in capsys.readouterr().err
        )

    def test_streaming_download_rejects_no_overwrite(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["cp", "s3://b/k", "-", "--no-overwrite"], ctx=_failing_factory_ctx())
        assert rc == 252
        assert (
            "--no-overwrite parameter is not supported for streaming downloads"
            in capsys.readouterr().err
        )

    @pytest.mark.parametrize(
        ("argv_extra", "token"),
        [
            (["--sse-c"], "--sse-c-key must be specified as well"),
            (["--sse-c-key", "Zm9v"], "--sse-c must be specified as well"),
            (["--sse-c-copy-source"], "--sse-c-copy-source-key must be specified as well"),
        ],
    )
    def test_sse_c_pairing_is_252(
        self,
        argv_extra: list[str],
        token: str,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        rc = cli.main(["cp", str(src), "s3://b/k", *argv_extra], ctx=_failing_factory_ctx())
        assert rc == 252
        assert token in capsys.readouterr().err

    def test_sse_c_copy_source_requires_a_copy(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        rc = cli.main(
            ["cp", str(src), "s3://b/k", "--sse-c-copy-source", "--sse-c-copy-source-key", "Zm9v"],
            ctx=_failing_factory_ctx(),
        )
        assert rc == 252
        assert "--sse-c-copy-source is only supported for copy operations." in (
            capsys.readouterr().err
        )

    def test_malformed_metadata_is_252(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        rc = cli.main(["cp", str(src), "s3://b/k", "--metadata", "foo"], ctx=_failing_factory_ctx())
        assert rc == 252
        assert "Error parsing parameter '--metadata'" in capsys.readouterr().err

    def test_missing_fileb_blob_is_252(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        missing = tmp_path / "no.key"
        rc = cli.main(
            ["cp", str(src), "s3://b/k", "--sse-c", "--sse-c-key", f"fileb://{missing}"],
            ctx=_failing_factory_ctx(),
        )
        assert rc == 252
        assert "Unable to load paramfile" in capsys.readouterr().err

    def test_plain_sse_c_key_passes_through_verbatim(self, tmp_path: Path) -> None:
        # aws sends an arbitrary key string to the server untouched
        # (no base64 decoding) - a malformed key is never a usage error.
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        ctx, calls = _recording_ctx([{}])
        rc = cli.main(["cp", str(src), "s3://b/k", "--sse-c", "--sse-c-key", "foo"], ctx=ctx)
        assert rc == 0
        assert calls[0].params["SSECustomerAlgorithm"] == "AES256"
        assert calls[0].params["SSECustomerKey"] == "foo"

    def test_invalid_choice_is_252(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        assert cli.main(["cp", str(src), "s3://b/k", "--acl", "bogus"]) == 252

    def test_object_lambda_arn_is_252(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        arn = "arn:aws:s3-object-lambda:us-east-1:123456789012:accesspoint/ap"
        ctx, calls = _recording_ctx([])
        assert cli.main(["cp", str(src), f"s3://{arn}/k"], ctx=ctx) == 252
        assert calls == []


class TestGeneralErrors:
    def test_page_size_non_integer_is_255(self) -> None:
        rc = cli.main(["cp", "a", "s3://b/k", "--page-size", "abc"], ctx=_failing_factory_ctx())
        assert rc == 255

    def test_progress_frequency_non_integer_is_255(self) -> None:
        rc = cli.main(
            ["cp", "a", "s3://b/k", "--progress-frequency", "abc"], ctx=_failing_factory_ctx()
        )
        assert rc == 255

    def test_missing_local_source_is_255_before_the_factory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = str(tmp_path / "nope.txt")
        rc = cli.main(["cp", missing, "s3://b/k"], ctx=_failing_factory_ctx())
        assert rc == 255
        assert f"The user-provided path {missing} does not exist." in capsys.readouterr().err


class TestPipelineErrors:
    def test_single_source_404_is_a_fatal_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctx, _ = _recording_ctx([_client_error("404", 404, "HeadObject")])
        rc = cli.main(["cp", "s3://b/no-such", str(tmp_path / "x")], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err == (
            "fatal error: An error occurred (404) when calling the HeadObject operation: "
            'Key "no-such" does not exist\n'
        )

    def test_transfer_failure_is_rc_1_with_a_failed_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        ctx, _ = _recording_ctx([_client_error("NoSuchBucket", 404, "PutObject")])
        rc = cli.main(["cp", str(src), "s3://missing-b/k"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 1
        assert "upload failed:" in captured.err
        assert "An error occurred (NoSuchBucket)" in captured.err

    def test_bad_grants_shape_is_a_fatal_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        ctx, calls = _recording_ctx([])
        rc = cli.main(["cp", str(src), "s3://b/k", "--grants", "foo"], ctx=ctx)
        assert rc == 1
        assert capsys.readouterr().err == (
            "fatal error: grants should be of the form permission=principal\n"
        )
        assert calls == []


class TestWarnings:
    @skip_if_chmod_is_inert
    def test_unreadable_source_warns_rc_2(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "secret.txt"
        src.write_bytes(b"x")
        src.chmod(0)
        ctx, calls = _recording_ctx([])
        try:
            rc = cli.main(["cp", str(src), "s3://b/k"], ctx=ctx)
        finally:
            src.chmod(0o644)
        captured = capsys.readouterr()
        assert rc == 2
        assert f"warning: Skipping file {src}. File/Directory is not readable." in captured.err
        assert calls == []

    @skip_if_chmod_is_inert
    def test_quiet_keeps_rc_2_but_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "secret.txt"
        src.write_bytes(b"x")
        src.chmod(0)
        ctx, _ = _recording_ctx([])
        try:
            rc = cli.main(["cp", str(src), "s3://b/k", "--quiet"], ctx=ctx)
        finally:
            src.chmod(0o644)
        captured = capsys.readouterr()
        assert rc == 2
        assert captured.out == ""
        assert captured.err == ""


class TestSuccessShapes:
    def test_upload_prints_the_result_line_and_guesses_content_type(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        Path("a.txt").write_bytes(b"x")
        ctx, calls = _recording_ctx([{}])
        rc = cli.main(["cp", "a.txt", "s3://bucket/up/"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 0
        # aws-cli's relative_path joins with the native sep (".\\a.txt" on Windows).
        assert captured.out.endswith(f"upload: .{os.sep}a.txt to s3://bucket/up/a.txt\n")
        assert calls[0].params["Key"] == "up/a.txt"
        assert calls[0].params["ContentType"] == "text/plain"

    def test_dryrun_prints_without_calling(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        Path("a.txt").write_bytes(b"x")
        ctx, calls = _recording_ctx([])
        rc = cli.main(["cp", "a.txt", "s3://bucket/k", "--dryrun"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == f"(dryrun) upload: .{os.sep}a.txt to s3://bucket/k\n"
        assert calls == []

    def test_quiet_suppresses_success_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        Path("a.txt").write_bytes(b"x")
        ctx, _ = _recording_ctx([{}])
        rc = cli.main(["cp", "a.txt", "s3://bucket/k", "--quiet"], ctx=ctx)
        assert rc == 0
        assert capsys.readouterr().out == ""

    def test_request_options_flow_to_the_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        Path("a.txt").write_bytes(b"x")
        ctx, calls = _recording_ctx([{}])
        rc = cli.main(
            [
                "cp",
                "a.txt",
                "s3://bucket/k",
                "--storage-class",
                "STANDARD_IA",
                "--metadata",
                "k1=v1,k2=v2",
                "--content-type",
                "text/x-probe",
                "--no-guess-mime-type",
            ],
            ctx=ctx,
        )
        assert rc == 0
        params = calls[0].params
        assert params["StorageClass"] == "STANDARD_IA"
        assert params["Metadata"] == {"k1": "v1", "k2": "v2"}
        assert params["ContentType"] == "text/x-probe"

    def test_filters_root_at_the_local_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        Path("keep.txt").write_bytes(b"x")
        Path("drop.bin").write_bytes(b"x")
        ctx, calls = _recording_ctx([{}])
        rc = cli.main(
            ["cp", ".", "s3://bucket/t", "--recursive", "--exclude", "*", "--include", "*.txt"],
            ctx=ctx,
        )
        assert rc == 0
        assert [call.params["Key"] for call in calls] == ["t/keep.txt"]


class TestSourceRegionWiring:
    def _factory_recording_ctx(self) -> tuple[Context, list[argparse.Namespace]]:
        namespaces: list[argparse.Namespace] = []

        def factory(args: argparse.Namespace) -> Any:
            namespaces.append(args)
            # Generous identical canned lists: whichever of HeadObject /
            # CopyObject lands on this client finds a workable response
            # (leftovers are allowed by the recorder).
            client, _ = make_recording_client([{"ContentLength": 1, "ETag": '"e"'}, {}, {}])
            return client

        return Context(client_factory=factory, transfer_config=_SYNC), namespaces

    def test_source_region_builds_a_second_client_without_the_endpoint(self) -> None:
        ctx, namespaces = self._factory_recording_ctx()
        rc = cli.main(
            [
                "cp",
                "s3://src-b/k",
                "s3://dest-b/k2",
                "--source-region",
                "eu-west-3",
                "--region",
                "us-east-1",
                "--endpoint-url",
                "http://main-endpoint",
            ],
            ctx=ctx,
        )
        assert rc == 0
        assert len(namespaces) == 2
        main_args, source_args = namespaces
        assert (main_args.region, main_args.endpoint_url) == ("us-east-1", "http://main-endpoint")
        # aws-cli ClientFactory: --source-region replaces the region and drops
        # the --endpoint-url override for the source client.
        assert (source_args.region, source_args.endpoint_url) == ("eu-west-3", None)

    def test_without_source_region_one_client_serves_both_sides(self) -> None:
        ctx, namespaces = self._factory_recording_ctx()
        rc = cli.main(["cp", "s3://src-b/k", "s3://dest-b/k2"], ctx=ctx)
        assert rc == 0
        assert len(namespaces) == 1


class _StdinShim:
    def __init__(self, payload: bytes) -> None:
        import io

        self.buffer = io.BytesIO(payload)


class _StdoutShim:
    def __init__(self) -> None:
        import io

        self.buffer = io.BytesIO()
        self._text: list[str] = []

    def write(self, text: str) -> int:
        self._text.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self._text)


class TestStreaming:
    def test_stdin_upload_is_silent(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", _StdinShim(b"foo\n"))
        ctx, calls = _recording_ctx([{}])
        rc = cli.main(["cp", "-", "s3://bucket/streaming.txt"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.out == ""  # streams force the errors-only printer
        assert [(c.operation, c.params["Key"]) for c in calls] == [("PutObject", "streaming.txt")]

    def test_stdin_upload_to_prefix_appends_the_dash_basename(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws's stream destination naming appends the source's
        # basename - literally "-" - when the destination takes the name.
        monkeypatch.setattr("sys.stdin", _StdinShim(b"x"))
        ctx, calls = _recording_ctx([{}])
        rc = cli.main(["cp", "-", "s3://bucket/pre/"], ctx=ctx)
        assert rc == 0
        assert calls[0].params["Key"] == "pre/-"

    def test_stdin_upload_with_expected_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", _StdinShim(b"foo\n"))
        ctx, calls = _recording_ctx([{}])
        rc = cli.main(["cp", "-", "s3://bucket/k", "--expected-size", "4"], ctx=ctx)
        assert rc == 0
        assert [c.operation for c in calls] == ["PutObject"]

    def test_expected_size_non_integer_is_a_fatal_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws converts with a bare int() at submit time -> rc 1
        # ("fatal error: invalid literal ..."), not the parse-time 255.
        monkeypatch.setattr("sys.stdin", _StdinShim(b"x"))
        ctx, _ = _recording_ctx([])
        rc = cli.main(["cp", "-", "s3://bucket/k", "--expected-size", "abc"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 1
        assert "fatal error: invalid literal for int()" in captured.err

    def test_unexpected_pipeline_exception_is_a_fatal_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # aws wraps the whole pipeline in CommandResultRecorder.__exit__, which
        # converts ANY escaping exception into "fatal error: ..." rc 1 (e.g. a
        # RecursionError from a pathologically deep tree). The run-span catch
        # must be that broad - the dispatcher's 255 is for pre-pipeline errors.
        (tmp_path / "a.txt").write_bytes(b"x")

        def boom(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("maximum recursion depth exceeded")

        monkeypatch.setattr("boto3_s3.localstorage.LocalFileGenerator.scan_children", boom)
        ctx, _ = _recording_ctx([])
        rc = cli.main(["cp", str(tmp_path), "s3://bucket/pre/", "--recursive"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 1
        assert "fatal error: maximum recursion depth exceeded" in captured.err

    def test_mid_pipeline_ctrl_c_is_rc_1_cancelled_like_aws(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A Ctrl-C inside the pipeline span is aws's cancelled run - rc 1
        # with one `cancelled: ctrl-c received` line (measured mid-sync on
        # the pinned 2.36.1), never the dispatcher backstop's 130, which
        # stays for the pre-pipeline spans (test_exit_codes).
        (tmp_path / "a.txt").write_bytes(b"x")

        def interrupt(*_args: object, **_kwargs: object) -> object:
            raise KeyboardInterrupt

        monkeypatch.setattr("boto3_s3.localstorage.LocalFileGenerator.scan_children", interrupt)
        ctx, _ = _recording_ctx([])
        rc = cli.main(["cp", str(tmp_path), "s3://bucket/pre/", "--recursive"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err == "cancelled: ctrl-c received\n"
        assert captured.out == ""

    def test_mid_pipeline_ctrl_c_respects_quiet(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # --quiet suppresses the cancelled line like the fatal-error line
        # (aws attaches no result printer under --quiet); the rc stays 1.
        (tmp_path / "a.txt").write_bytes(b"x")

        def interrupt(*_args: object, **_kwargs: object) -> object:
            raise KeyboardInterrupt

        monkeypatch.setattr("boto3_s3.localstorage.LocalFileGenerator.scan_children", interrupt)
        ctx, _ = _recording_ctx([])
        rc = cli.main(["cp", str(tmp_path), "s3://bucket/pre/", "--recursive", "--quiet"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 1
        assert captured.err == ""

    def test_expected_size_non_integer_ignored_off_the_stream_route(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # aws only converts --expected-size on the streaming-upload route; on a
        # plain (non-stream) upload the value is untouched and ignored, so a
        # non-integer must not abort the transfer (rc 0, PutObject still issued).
        monkeypatch.chdir(tmp_path)
        Path("a.txt").write_bytes(b"x")
        ctx, calls = _recording_ctx([{}])
        rc = cli.main(["cp", "a.txt", "s3://bucket/k", "--expected-size", "abc"], ctx=ctx)
        assert rc == 0
        assert [c.operation for c in calls] == ["PutObject"]

    def test_missing_stdin_is_a_fatal_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("sys.stdin", None)
        ctx, calls = _recording_ctx([])
        rc = cli.main(["cp", "-", "s3://bucket/k"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 1
        assert (
            "fatal error: stdin is required for this operation, but is not available"
            in captured.err
        )
        assert calls == []

    def test_stdout_download_writes_raw_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import io

        shim = _StdoutShim()
        monkeypatch.setattr("sys.stdout", shim)
        ctx, calls = _recording_ctx(
            [
                {"ContentLength": 4, "ETag": '"foo"'},
                {"Body": io.BytesIO(b"foo\n"), "ContentLength": 4, "ETag": '"foo"'},
            ]
        )
        rc = cli.main(["cp", "s3://bucket/streaming.txt", "-"], ctx=ctx)
        assert rc == 0
        assert [c.operation for c in calls] == ["HeadObject", "GetObject"]
        assert shim.buffer.getvalue() == b"foo\n"
        assert shim.text == ""  # no result/progress lines around the bytes


class TestNoOverwrite:
    def test_upload_carries_if_none_match(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        ctx, calls = _recording_ctx([{}])
        rc = cli.main(["cp", str(src), "s3://b/k", "--no-overwrite"], ctx=ctx)
        assert rc == 0
        assert calls[0].params["IfNoneMatch"] == "*"

    def test_existing_object_is_a_silent_skip(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        ctx, _ = _recording_ctx([_client_error("PreconditionFailed", 412, "PutObject")])
        rc = cli.main(["cp", str(src), "s3://b/k", "--no-overwrite"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 0
        assert captured.err == ""

    def test_existing_download_destination_is_skipped(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"keep me")
        ctx, calls = _recording_ctx([{"ContentLength": 3, "LastModified": None, "ETag": '"e"'}])
        rc = cli.main(["cp", "s3://b/k", str(dest), "--no-overwrite"], ctx=ctx)
        assert rc == 0
        assert [c.operation for c in calls] == ["HeadObject"]
        assert dest.read_bytes() == b"keep me"


class TestChecksumOptions:
    def test_checksum_algorithm_flows_to_put_object(self, tmp_path: Path) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        ctx, calls = _recording_ctx([{}])
        rc = cli.main(["cp", str(src), "s3://b/k", "--checksum-algorithm", "SHA256"], ctx=ctx)
        assert rc == 0
        assert calls[0].params["ChecksumAlgorithm"] == "SHA256"

    def test_checksum_mode_flows_to_the_download(self, tmp_path: Path) -> None:
        import io

        ctx, calls = _recording_ctx(
            [
                {"ContentLength": 1, "ETag": '"e"'},
                {"Body": io.BytesIO(b"x"), "ContentLength": 1, "ETag": '"e"'},
            ]
        )
        rc = cli.main(
            ["cp", "s3://b/k", str(tmp_path / "out"), "--checksum-mode", "ENABLED"], ctx=ctx
        )
        assert rc == 0
        assert calls[0].params["ChecksumMode"] == "ENABLED"
        assert calls[1].params["ChecksumMode"] == "ENABLED"

    def test_checksum_algorithm_rejects_downloads(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws-cli _raise_if_paths_type_incorrect_for_param (rc 252 for cp and mv
        # alike).
        rc = cli.main(
            ["cp", "s3://b/k", str(tmp_path / "f"), "--checksum-algorithm", "CRC32"],
            ctx=_failing_factory_ctx(),
        )
        assert rc == 252
        assert (
            "Expected checksum-algorithm parameter to be used with one of following "
            "path formats: <LocalPath> <S3Uri>, <S3Uri> <S3Uri>. "
            "Instead, received <S3Uri> <LocalPath>." in capsys.readouterr().err
        )

    def test_checksum_mode_rejects_uploads(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        rc = cli.main(
            ["cp", str(src), "s3://b/k", "--checksum-mode", "ENABLED"],
            ctx=_failing_factory_ctx(),
        )
        assert rc == 252
        assert (
            "Expected checksum-mode parameter to be used with one of following "
            "path formats: <S3Uri> <LocalPath>. "
            "Instead, received <LocalPath> <S3Uri>." in capsys.readouterr().err
        )

    def test_checksum_algorithm_allows_streaming_uploads(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `cp - s3://b/k` is locals3 for the pairing check (the aws-cli
        # validates before streaming starts), so the algorithm passes
        # validation - whatever stdin then yields under the runner is past
        # the 252 boundary.
        ctx, _ = _recording_ctx([{}])
        rc = cli.main(["cp", "-", "s3://b/k", "--checksum-algorithm", "SHA256"], ctx=ctx)
        assert rc != 252
        assert "Expected checksum-algorithm parameter" not in capsys.readouterr().err


def _case_listing() -> dict[str, Any]:
    import datetime as dt

    stamp = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    return {
        "Contents": [
            {"Key": "cc/A.txt", "Size": 1, "LastModified": stamp, "ETag": '"u"'},
            {"Key": "cc/a.txt", "Size": 1, "LastModified": stamp, "ETag": '"l"'},
        ]
    }


def _get_response() -> dict[str, Any]:
    import io

    return {"Body": io.BytesIO(b"x"), "ContentLength": 1, "ETag": '"u"'}


class TestSourceScanWiring:
    """The CLI delivers --follow-symlinks / --page-size as Storage constructor
    config (resolve_locations), not per-operation arguments - pin that the
    flags still shape the walk / listing end to end."""

    def test_no_follow_symlinks_reaches_the_source_walk(self, tmp_path: Path) -> None:
        (tmp_path / "real.txt").write_bytes(b"x")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
        ctx, calls = _recording_ctx([{}])
        rc = cli.main(
            ["cp", str(tmp_path), "s3://bucket/pre/", "--recursive", "--no-follow-symlinks"],
            ctx=ctx,
        )
        assert rc == 0
        assert [c.params["Key"] for c in calls if c.operation == "PutObject"] == ["pre/real.txt"]

    def test_follow_symlinks_is_the_default(self, tmp_path: Path) -> None:
        (tmp_path / "real.txt").write_bytes(b"x")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
        ctx, calls = _recording_ctx([{}, {}])
        rc = cli.main(["cp", str(tmp_path), "s3://bucket/pre/", "--recursive"], ctx=ctx)
        assert rc == 0
        assert sorted(c.params["Key"] for c in calls if c.operation == "PutObject") == [
            "pre/link.txt",
            "pre/real.txt",
        ]

    def test_page_size_reaches_the_source_listing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --page-size is baked into the S3Storage the CLI builds; the paginator
        # translates it to MaxKeys on the wire.
        monkeypatch.chdir(tmp_path)
        ctx, calls = _recording_ctx([_case_listing(), _get_response(), _get_response()])
        rc = cli.main(["cp", "s3://b/cc/", "out", "--recursive", "--page-size", "5"], ctx=ctx)
        assert rc == 0
        assert calls[0].operation == "ListObjectsV2"
        assert calls[0].params["MaxKeys"] == 5

    def test_the_cli_posture_reaches_the_source_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Ctrl-C is process-fatal in the CLI: the S3 the CLI builds declares
        # wait_on_interrupt=False once, and the operation threads it into the
        # ScanOptions of every scan it starts (here the upload's source walk);
        # the library default keeps waiting.
        import boto3_s3

        scan_waits: list[bool] = []

        class _RecLocal(boto3_s3.LocalStorage):
            def scan(self, options: Any = None, *, cancel_token: Any = None) -> Any:
                assert options is not None
                scan_waits.append(options.wait_on_interrupt)
                return super().scan(options, cancel_token=cancel_token)

        monkeypatch.setattr(boto3_s3, "LocalStorage", _RecLocal)
        (tmp_path / "f.txt").write_bytes(b"x")
        ctx, _calls = _recording_ctx([{}])
        rc = cli.main(["cp", str(tmp_path), "s3://bucket/pre/", "--recursive"], ctx=ctx)
        assert rc == 0
        assert scan_waits == [False]


class TestCaseConflict:
    def test_skip_warns_and_skips_the_twin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        ctx, calls = _recording_ctx(
            [_case_listing(), _get_response()], transfer_config=_CASE_CONFLICT_CONFIG
        )
        rc = cli.main(
            ["cp", "s3://b/cc/", "out", "--recursive", "--case-conflict", "skip"], ctx=ctx
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert "warning: Skipping b/cc/a.txt -> " in captured.err
        assert "differs only by case" in captured.err
        assert [c.operation for c in calls] == ["ListObjectsV2", "GetObject"]

    def test_notice_prints_even_under_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws prints these advisories with a direct uni_print that bypasses
        # the (absent, under --quiet) result printer.
        monkeypatch.chdir(tmp_path)
        ctx, _ = _recording_ctx(
            [_case_listing(), _get_response()], transfer_config=_CASE_CONFLICT_CONFIG
        )
        rc = cli.main(
            ["cp", "s3://b/cc/", "out", "--recursive", "--case-conflict", "skip", "--quiet"],
            ctx=ctx,
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert "warning: Skipping b/cc/a.txt -> " in captured.err

    def test_error_mode_is_a_fatal_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        ctx, _ = _recording_ctx(
            [_case_listing(), _get_response()], transfer_config=_CASE_CONFLICT_CONFIG
        )
        rc = cli.main(
            ["cp", "s3://b/cc/", "out", "--recursive", "--case-conflict", "error"], ctx=ctx
        )
        captured = capsys.readouterr()
        assert rc == 1
        assert "fatal error: Failed to download b/cc/a.txt -> " in captured.err

    def test_invalid_choice_is_252(self) -> None:
        assert cli.main(["cp", "s3://b/cc/", "out", "--case-conflict", "bogus"]) == 252


class TestS3ExpressCaseConflict:
    def test_skip_is_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(
            [
                "cp",
                "s3://bucket--usw2-az1--x-s3/",
                "out",
                "--recursive",
                "--case-conflict",
                "skip",
            ],
            ctx=_failing_factory_ctx(),
        )
        assert rc == 252
        assert "`skip` is not a valid value" in capsys.readouterr().err

    def test_warn_emits_the_standing_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.chdir(tmp_path)
        ctx, calls = _recording_ctx([{"Contents": [], "CommonPrefixes": []}])
        rc = cli.main(
            [
                "cp",
                "s3://bucket--usw2-az1--x-s3/",
                "out",
                "--recursive",
                "--case-conflict",
                "warn",
            ],
            ctx=ctx,
        )
        captured = capsys.readouterr()
        assert rc == 0
        assert "warning: Recursive copies/moves" in captured.err
        # aws emits this via uni_print with NO trailing newline (measured); the
        # warning is the only thing on stderr for this empty listing.
        assert captured.err.endswith("s3-case-insensitivity.html.")
        assert [c.operation for c in calls] == ["ListObjectsV2"]
