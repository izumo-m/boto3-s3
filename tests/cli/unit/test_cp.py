"""Unit tests for the ``boto3-s3 cp`` subcommand (dispatch + exit codes).

The rc shape (design/cli.md section 6): pre-pipeline errors
keep their class - usage errors 252 (path types, SSE-C pairing, streaming
with --recursive / --no-overwrite, ``--metadata`` parsing, blob decoding,
the S3 Express case-conflict rejection), the bare integer conversion and
the missing local source 255 - while
everything the transfer pipeline raises is rc 1 (``<kind> failed:`` per
item, one ``fatal error:`` for run-killers - a non-integer
``--expected-size`` and a missing stdin included), and a warnings-only run
exits 2. The case-conflict advisories are uncounted NOTICEs that print
even under --quiet (aws-cli's direct stderr prints).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context
from tests.utils.fakes3 import MTIME, client_error, get_response, head_response
from tests.utils.harness import built_client_ctx
from tests.utils.host import skip_if_chmod_is_inert
from tests.utils.recorder import ApiCall, make_recording_client

_SYNC = TransferConfig(use_threads=False)
# The case-conflict gate detects a "two S3 twins" conflict via its in-flight set,
# which only holds while the first twin's download is still running. That needs a
# threaded (non-blocking) submit running ahead of completions - aws-cli's own
# tests use a single worker (max_concurrent_requests = 1). _SYNC completes each
# twin before the next is judged, emptying the set.
_CASE_CONFLICT_CONFIG = TransferConfig(max_concurrency=1)


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
        rc = cli.main(["cp", "a.txt", "b.txt"], ctx=built_client_ctx())
        assert rc == 252
        assert "Error: Invalid argument type" in capsys.readouterr().err

    def test_streaming_with_recursive_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["cp", "-", "s3://b/k", "--recursive"], ctx=built_client_ctx())
        assert rc == 252
        assert "Streaming" in capsys.readouterr().err

    def test_streaming_download_rejects_no_overwrite(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["cp", "s3://b/k", "-", "--no-overwrite"], ctx=built_client_ctx())
        assert rc == 252
        assert "--no-overwrite" in capsys.readouterr().err

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
        rc = cli.main(["cp", str(src), "s3://b/k", *argv_extra], ctx=built_client_ctx())
        assert rc == 252
        assert token in capsys.readouterr().err

    def test_sse_c_copy_source_requires_a_copy(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        rc = cli.main(
            ["cp", str(src), "s3://b/k", "--sse-c-copy-source", "--sse-c-copy-source-key", "Zm9v"],
            ctx=built_client_ctx(),
        )
        assert rc == 252
        assert "--sse-c-copy-source" in capsys.readouterr().err

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
        # aws-cli sends an arbitrary key string to the server untouched
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

    @pytest.mark.parametrize("direction", ["upload", "download"])
    def test_a_malformed_grant_is_refused_before_the_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], direction: str
    ) -> None:
        # A usage error on every route: aws-cli finds it only as it maps the
        # first upload or copy (an in-flight fatal error, rc 1), and never on
        # a download, which maps no grants.
        local = tmp_path / "a.txt"
        local.write_bytes(b"x")
        paths = [str(local), "s3://b/k"] if direction == "upload" else ["s3://b/k", str(local)]
        ctx, calls = _recording_ctx([])
        rc = cli.main(["cp", *paths, "--grants", "foo"], ctx=ctx)
        err = capsys.readouterr().err
        assert rc == 252
        assert "--grants" in err
        assert calls == []

    @pytest.mark.parametrize(
        "paths",
        [
            ["{local}", "s3:///k"],
            ["{local}", "s3://"],
            ["s3:///k", "{local}"],
            ["s3://", "{local}"],
            ["s3:///k", "s3://b/k"],
            ["-", "s3:///k"],
        ],
    )
    @pytest.mark.parametrize("dryrun", [[], ["--dryrun"]], ids=["live", "dryrun"])
    def test_a_path_with_no_bucket_is_refused_before_the_run(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        paths: list[str],
        dryrun: list[str],
    ) -> None:
        # aws-cli sends Bucket="" onward: each upload fails on its own (rc 1), a
        # dryrun upload prints its line at rc 0, and a download or listing is
        # a fatal error (rc 1).
        local = tmp_path / "a.txt"
        local.write_bytes(b"x")
        argv = ["cp", *(p.format(local=local) for p in paths), *dryrun]
        ctx, calls = _recording_ctx([])
        rc = cli.main(argv, ctx=ctx)
        assert rc == 252
        assert 'Invalid bucket name ""' in capsys.readouterr().err
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

    def test_missing_local_source_is_255(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = str(tmp_path / "nope.txt")
        rc = cli.main(["cp", missing, "s3://b/k"], ctx=built_client_ctx())
        assert rc == 255
        assert missing in capsys.readouterr().err


class TestPipelineErrors:
    def test_single_source_404_is_a_fatal_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctx, _ = _recording_ctx([client_error("404", 404, "HeadObject")])
        rc = cli.main(["cp", "s3://b/no-such", str(tmp_path / "x")], ctx=ctx)
        err = capsys.readouterr().err
        assert rc == 1
        assert err.startswith("fatal error: ")
        assert "no-such" in err  # the report names the missing key

    @pytest.mark.parametrize(
        ("head", "missing"),
        [
            ({"ContentLength": 1, "ETag": '"e"'}, "LastModified"),
            ({"LastModified": MTIME, "ETag": '"e"'}, "ContentLength"),
        ],
    )
    def test_a_head_missing_a_required_element_is_a_fatal_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        head: dict[str, Any],
        missing: str,
    ) -> None:
        # aws-cli's `_list_single_object` reads ContentLength and LastModified
        # by subscript, so a HEAD without one ends the run like an incomplete
        # listing entry does: a fatal error at rc 1 (measured against the
        # pinned `aws` through a 127.0.0.1 fake). ETag is read with a default
        # there, so its absence transfers.
        ctx, calls = _recording_ctx([head])
        rc = cli.main(["cp", "s3://b/a", str(tmp_path / "x")], ctx=ctx)
        err = capsys.readouterr().err
        assert rc == 1
        assert err.startswith("fatal error: ")
        assert missing in err  # the report names the missing element
        assert [c.operation for c in calls] == ["HeadObject"]

    def test_transfer_failure_is_rc_1_with_a_failed_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        ctx, _ = _recording_ctx([client_error("NoSuchBucket", 404, "PutObject")])
        rc = cli.main(["cp", str(src), "s3://missing-b/k"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 1
        assert "upload failed:" in captured.err
        assert "NoSuchBucket" in captured.err


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
        assert captured.err.startswith("warning: ")
        assert str(src) in captured.err
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

    def test_an_empty_destination_of_a_single_download_is_the_working_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli absolutizes "" to the cwd, a directory, so the object lands
        # there under its own name (rc 0, measured). The library refuses ""
        # as no location at all, so the command has to say "." itself: it
        # failed this with `A local path must not be empty.` (rc 1).
        monkeypatch.chdir(tmp_path)
        ctx, calls = _recording_ctx([head_response(), get_response()])
        rc = cli.main(["cp", "s3://bucket/p/seed", "", "--no-progress"], ctx=ctx)
        assert rc == 0
        assert capsys.readouterr().out == f"download: s3://bucket/p/seed to .{os.sep}seed\n"
        assert [call.operation for call in calls] == ["HeadObject", "GetObject"]
        assert Path("seed").read_bytes() == b"payload"

    def test_an_empty_destination_of_a_recursive_download_is_refused_like_aws(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A dir_op pre-creates its destination, and makedirs("") is aws-cli's
        # own pre-pipeline failure (rc 255, measured) - "" never becomes ".".
        # The OS words the error ([Errno 2] on POSIX, a localized [WinError 3]
        # on Windows); what is pinned is that it names the empty path.
        monkeypatch.chdir(tmp_path)
        ctx, calls = _recording_ctx([])
        rc = cli.main(["cp", "s3://bucket/p/", "", "--recursive"], ctx=ctx)
        assert rc == 255
        assert capsys.readouterr().err.rstrip().endswith(": ''")
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

        # Which client serves which operation depends on --source-region (one
        # client takes both, or the source client takes the HEAD), so answer
        # by operation name, each with its real response shape - CopyObject
        # nests its ETag under CopyObjectResult, unlike HeadObject.
        shapes: dict[str, dict[str, Any]] = {
            "HeadObject": {"ContentLength": 1, "ETag": '"e"', "LastModified": MTIME},
            "CopyObject": {"CopyObjectResult": {"ETag": '"e"', "LastModified": MTIME}},
        }

        def factory(args: argparse.Namespace) -> Any:
            namespaces.append(args)
            client: Any = make_recording_client([])[0]
            client._make_api_call = lambda operation_name, _params: shapes[operation_name]
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
                "--ca-bundle",
                "/ca/bundle.pem",
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
        # Everything else rides over untouched. The TLS settings matter beyond
        # tidiness: build_client resolves `verify` per client, and a source
        # client resolving a different value than the destination would make
        # the CRT singleton reject the second one and silently fall back to
        # classic (crtsupport._is_compatible_request).
        assert source_args.ca_bundle == "/ca/bundle.pem"
        assert source_args.no_verify_ssl == main_args.no_verify_ssl

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
        # aws-cli's stream destination naming appends the source's
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

    def test_expected_size_above_the_threshold_goes_multipart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The hint must reach the engine: a stream cannot be sized up front,
        # so only --expected-size can push it over the multipart threshold
        # (default 8 MiB). The actual bytes stay tiny - the decision is made
        # from the hint alone, exactly aws-cli's use of expected-size.
        monkeypatch.setattr("sys.stdin", _StdinShim(b"foo\n"))
        ctx, calls = _recording_ctx([{"UploadId": "u"}, {"ETag": '"p1"'}, {}])
        rc = cli.main(
            ["cp", "-", "s3://bucket/k", "--expected-size", str(9 * 1024 * 1024)], ctx=ctx
        )
        assert rc == 0
        assert [c.operation for c in calls] == [
            "CreateMultipartUpload",
            "UploadPart",
            "CompleteMultipartUpload",
        ]

    def test_expected_size_non_integer_is_a_fatal_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws-cli converts with a bare int() at submit time -> rc 1
        # ("fatal error: invalid literal ..."), not the parse-time 255.
        monkeypatch.setattr("sys.stdin", _StdinShim(b"x"))
        ctx, _ = _recording_ctx([])
        rc = cli.main(["cp", "-", "s3://bucket/k", "--expected-size", "abc"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 1
        assert "fatal error: invalid literal for int()" in captured.err

    def test_expected_size_non_integer_dryrun_stays_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A dryrun never reaches the submit-time int(): aws-cli branches to the
        # dryrun record before building the live subscribers (measured:
        # `cp - s3://b/k --expected-size abc --dryrun` is rc 0).
        monkeypatch.setattr("sys.stdin", _StdinShim(b"x"))
        ctx, calls = _recording_ctx([])
        rc = cli.main(["cp", "-", "s3://bucket/k", "--expected-size", "abc", "--dryrun"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 0
        assert "(dryrun) upload: - to s3://bucket/k" in captured.out
        assert calls == []

    def test_unexpected_pipeline_exception_is_a_fatal_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # aws-cli wraps the whole pipeline in CommandResultRecorder.__exit__, which
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
        # A Ctrl-C inside the pipeline span is aws-cli's cancelled run - rc 1
        # with one `cancelled: ctrl-c received` line (measured mid-sync on
        # the pinned 2.36.40), never the dispatcher backstop's 130, which
        # stays for the pre-pipeline spans (test_exit_codes). The rc is the
        # match; the closing line is not.
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
        # (aws-cli attaches no result printer under --quiet); the rc stays 1.
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
        # aws-cli only converts --expected-size on the streaming-upload route; on a
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
        assert captured.err.startswith("fatal error: ")
        assert "stdin" in captured.err
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

    def test_stdout_that_cannot_take_the_bytes_fails_the_download(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The bytes fit the process stream's buffer, so only the flush the
        # writer makes as the item settles can find stdout full: the item
        # fails with its line and rc 1, not a success left to Python's flush
        # at exit.
        import errno
        import io

        class _FullBuffer(io.BytesIO):
            def flush(self) -> None:
                raise OSError(errno.ENOSPC, "No space left on device")

        shim = _StdoutShim()
        shim.buffer = _FullBuffer()
        monkeypatch.setattr("sys.stdout", shim)
        ctx, _calls = _recording_ctx(
            [
                {"ContentLength": 4, "ETag": '"foo"'},
                {"Body": io.BytesIO(b"foo\n"), "ContentLength": 4, "ETag": '"foo"'},
            ]
        )
        rc = cli.main(["cp", "s3://bucket/streaming.txt", "-"], ctx=ctx)
        assert rc == 1
        assert capsys.readouterr().err == (
            "download failed: s3://bucket/streaming.txt to - [Errno 28] No space left on device\n"
        )


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
        ctx, _ = _recording_ctx([client_error("PreconditionFailed", 412, "PutObject")])
        rc = cli.main(["cp", str(src), "s3://b/k", "--no-overwrite"], ctx=ctx)
        captured = capsys.readouterr()
        assert rc == 0
        assert (captured.out, captured.err) == ("", "")  # no upload line either

    def test_existing_download_destination_is_skipped(self, tmp_path: Path) -> None:
        dest = tmp_path / "out.bin"
        dest.write_bytes(b"keep me")
        ctx, calls = _recording_ctx([{"ContentLength": 3, "LastModified": MTIME, "ETag": '"e"'}])
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
                {"ContentLength": 1, "ETag": '"e"', "LastModified": MTIME},
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
            ctx=built_client_ctx(),
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
            ctx=built_client_ctx(),
        )
        assert rc == 252
        assert (
            "Expected checksum-mode parameter to be used with one of following "
            "path formats: <S3Uri> <LocalPath>. "
            "Instead, received <LocalPath> <S3Uri>." in capsys.readouterr().err
        )

    def test_checksum_algorithm_allows_streaming_uploads(self) -> None:
        # `cp - s3://b/k` is locals3 for the pairing check (the aws-cli
        # validates before streaming starts), so the algorithm passes
        # validation - whatever stdin then yields under the runner is past
        # the 252 boundary.
        ctx, _ = _recording_ctx([{}])
        rc = cli.main(["cp", "-", "s3://b/k", "--checksum-algorithm", "SHA256"], ctx=ctx)
        assert rc != 252


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
        # reusable_after_interrupt=False once, and the operation threads it into the
        # ScanOptions of every scan it starts (here the upload's source walk);
        # the library default keeps waiting.
        import boto3_s3

        scan_waits: list[bool] = []

        class _RecLocal(boto3_s3.LocalStorage):
            def scan(self, options: Any = None, *, cancel_token: Any = None) -> Any:
                assert options is not None
                scan_waits.append(options.reusable_after_interrupt)
                return super().scan(options, cancel_token=cancel_token)

        # Patch the binding module: transferargs imports LocalStorage at top.
        monkeypatch.setattr("boto3_s3_cli.commands.transferargs.LocalStorage", _RecLocal)
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
        # `aws` prints these advisories with a direct uni_print that bypasses
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
        assert captured.err.startswith("fatal error: ")
        assert "b/cc/a.txt" in captured.err

    def test_invalid_choice_is_252(self) -> None:
        assert cli.main(["cp", "s3://b/cc/", "out", "--case-conflict", "bogus"]) == 252


class TestS3ExpressCaseConflict:
    def test_skip_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The rejection fires before any request: the recording client must
        # stay uncalled, and the destination directory is not created (aws-cli
        # creates it before this check and leaves it behind).
        monkeypatch.chdir(tmp_path)
        ctx, calls = _recording_ctx([])
        rc = cli.main(
            [
                "cp",
                "s3://bucket--usw2-az1--x-s3/",
                "out",
                "--recursive",
                "--case-conflict",
                "skip",
            ],
            ctx=ctx,
        )
        assert rc == 252
        assert "skip" in capsys.readouterr().err
        assert calls == []
        assert not (tmp_path / "out").exists()

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
        # `aws` emits this via uni_print with NO trailing newline (measured); the
        # warning is the only thing on stderr for this empty listing.
        assert captured.err.endswith("s3-case-insensitivity.html.")
        assert [c.operation for c in calls] == ["ListObjectsV2"]
