"""The CLI's ``[s3]`` wiring: the library resolves the config, the CLI hands it on.

The decision tree and the merge are the library's (tests/lib/test_engine_selection.py).
``TestWiring`` runs the whole path through ``main`` with a recording client and
a temp ``AWS_CONFIG_FILE`` to prove the config reaches the engine.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import boto3
import pytest

from boto3_s3 import S3
from boto3_s3 import crtsupport as crtsupport
from boto3_s3_cli.commands import transferargs
from boto3_s3_cli.commands.base import Context
from tests.utils.fakes3 import MTIME
from tests.utils.harness import run_cli_in_process
from tests.utils.recorder import ApiCall, make_recording_client

_MIB = 1024 * 1024


class TestResolveTransferConfig:
    """The command-facing orchestrator (``transferargs.resolve_transfer_config``)."""

    def _args(self, profile: str | None = None) -> argparse.Namespace:
        return argparse.Namespace(profile=profile)

    def test_injected_config_wins(self) -> None:
        from boto3_s3 import TransferConfig

        injected = TransferConfig(use_threads=False)
        ctx = Context(transfer_config=injected)
        result = transferargs.resolve_transfer_config(ctx, S3(), paths_type="locals3")
        assert result.use_threads is False

    def test_reads_scoped_config_when_not_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text("[default]\ns3 =\n  multipart_threshold = 7\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        ctx = Context()  # no transfer_config
        s3 = S3(session=boto3.Session())
        result = transferargs.resolve_transfer_config(ctx, s3, paths_type="locals3")
        assert result.multipart_threshold == 7
        assert result.preferred_transfer_client == "classic"  # conftest pins is_optimized False


class TestWiring:
    """The ``[s3]`` config actually reaching the transfer engine through ``main``."""

    def _ctx(self, parsed_responses: list[Any]) -> tuple[Context, list[ApiCall]]:
        client, calls = make_recording_client(parsed_responses)
        # No transfer_config injected: the scoped-config path runs.
        return Context(client_factory=lambda _args: client), calls

    def test_client_and_runtime_config_share_the_context_s3_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text(
            "[default]\ns3 =\n  multipart_threshold = 8MB\n"
            "[profile bound]\ns3 =\n  multipart_threshold = 1\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        source = tmp_path / "small.txt"
        source.write_bytes(b"hello")
        client, calls = make_recording_client([{"UploadId": "id"}, {"ETag": '"p1"'}, {}])

        class BoundS3(S3):
            def client(self) -> Any:
                return client

        s3 = BoundS3(session=boto3.Session(profile_name="bound"))

        import botocore.session

        def unexpected_session(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the command must reuse the session bound to S3")

        monkeypatch.setattr(botocore.session, "Session", unexpected_session)
        ctx = Context(s3_factory=lambda _args: s3)

        result = run_cli_in_process(["cp", str(source), "s3://bucket/key"], ctx=ctx)

        assert result.rc == 0, (result.stderr, calls)
        assert [call.operation for call in calls] == [
            "CreateMultipartUpload",
            "UploadPart",
            "CompleteMultipartUpload",
        ]

    def test_multipart_threshold_from_s3_config_forces_multipart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text("[default]\ns3 =\n  multipart_threshold = 1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        source = tmp_path / "small.txt"
        source.write_bytes(b"hello")  # 5 bytes: a PutObject by default (8 MiB threshold)
        ctx, calls = self._ctx([{"UploadId": "id"}, {"ETag": '"p1"'}, {}])
        result = run_cli_in_process(["cp", str(source), "s3://bucket/key"], ctx=ctx)
        assert result.rc == 0, (result.stderr, calls)
        # threshold=1 from [s3] turned the 5-byte upload multipart.
        assert [c.operation for c in calls] == [
            "CreateMultipartUpload",
            "UploadPart",
            "CompleteMultipartUpload",
        ]

    def test_default_config_keeps_the_small_upload_single_part(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text("[default]\nregion = us-east-1\n")  # no [s3] section
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        source = tmp_path / "small.txt"
        source.write_bytes(b"hello")
        ctx, calls = self._ctx([{"ETag": '"p1"'}])
        result = run_cli_in_process(["cp", str(source), "s3://bucket/key"], ctx=ctx)
        assert result.rc == 0, (result.stderr, calls)
        assert [c.operation for c in calls] == ["PutObject"]

    def test_invalid_s3_config_value_exits_255_through_main(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An invalid [s3] value exits 255 through main, like aws, naming the
        # setting (the library's report itself is tests/lib's). The error is
        # raised parsing [s3] before any API call, so it propagates to main's
        # general handler regardless of the recording client.
        config_file = tmp_path / "config"
        config_file.write_text("[default]\ns3 =\n  preferred_transfer_client = bogus\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        source = tmp_path / "small.txt"
        source.write_bytes(b"hello")
        ctx, _calls = self._ctx([])
        result = run_cli_in_process(["cp", str(source), "s3://bucket/key"], ctx=ctx)
        assert result.rc == 255, (result.rc, result.stderr)
        assert "preferred_transfer_client" in result.stderr


# aws's general handler formats a bare AssertionError - awscrt's region check -
# as the prefix alone, with no trailing space (aws-cli errorformat.py).
_EMPTY_REPORT = "boto3-s3: [ERROR]:\n"


def _config_ctx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> tuple[Context, list[ApiCall]]:
    """A Context on a recording client scripted with nothing, plus a temp ``[s3]``.

    The rows below all fail before any request, so an exhausted recorder is the
    guard: a row that slipped past the config would call the client and the root
    conftest would fail the test.
    """
    config_file = tmp_path / "config"
    config_file.write_text(body)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
    client, calls = make_recording_client([])
    return Context(client_factory=lambda _args: client), calls


class TestRmReadsTheRuntimeConfig:
    """``rm`` is an ``S3TransferCommand`` in aws, so it reads ``[s3]`` like cp.

    Measured on the pinned aws-cli: an invalid ``[s3]`` value is 255 for every
    ``rm`` form. The read comes before rm's routes split, so the single delete
    and the bucket-less form that returns early stand for all of them.
    """

    _BAD = "[default]\nregion = us-east-1\ns3 =\n  preferred_transfer_client = bogus\n"

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["rm", "s3://bucket/key"], id="single"),
            pytest.param(["rm", "s3://"], id="bucket-less-keyless"),
        ],
    )
    def test_an_invalid_value_is_255(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
    ) -> None:
        ctx, _calls = _config_ctx(tmp_path, monkeypatch, self._BAD)
        result = run_cli_in_process(argv, ctx=ctx)
        assert result.rc == 255
        assert "preferred_transfer_client" in result.stderr

    def test_a_valid_config_leaves_the_run_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text(
            "[default]\nregion = us-east-1\ns3 =\n  preferred_transfer_client = classic\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        client, calls = make_recording_client([{}])
        ctx = Context(client_factory=lambda _args: client)
        result = run_cli_in_process(["rm", "s3://bucket/key"], ctx=ctx)
        assert (result.rc, result.stdout) == (0, "delete: s3://bucket/key\n")
        assert [call.operation for call in calls] == ["DeleteObject"]


class TestCrtEngineConstructionSlot:
    """The transfer manager is built where aws builds it, ``rm`` included.

    aws's ``_get_transfer_manager`` runs right after the ``[s3]`` read and
    before anything about the run is decided, so a CRT selection pays the whole
    construction even for a ``--dryrun``, and even for ``rm``, whose deletes
    ride it but build it later (design/crt.md sections 4 and 6).
    When no region
    resolves, that construction is awscrt's ``assert isinstance(region, str)``:
    a bare ``AssertionError`` that aws's general handler renders as an empty
    rc-255 report.

    awscrt is faked here (the suite must run without it, and a real CRT client
    would hold the host-wide process lock for the rest of the session): the
    construction itself is stubbed to raise the same bare ``AssertionError``.
    `tests/lib/test_crtsupport.py` pins that the declared region really reaches
    ``create_s3_crt_client``, and `tests/cli/functional/test_crt_no_region.py`
    runs the whole thing against the real awscrt.
    """

    _CRT = "[default]\ns3 =\n  preferred_transfer_client = crt\n"

    @pytest.fixture(autouse=True)
    def _no_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The real shape of these rows: nothing anywhere resolves a region, so
        # the CLI declares None and awscrt is the thing that refuses. Only that
        # refusal is faked below.
        for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    @pytest.fixture
    def crt_constructions(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        """Fake a usable awscrt and record every CRT construction attempt."""
        seen: list[dict[str, Any]] = []

        def create(client: Any, config: Any, **kwargs: Any) -> Any:
            seen.append({"client": client, "config": config, **kwargs})
            raise AssertionError  # awscrt's own, message-less

        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: True)
        monkeypatch.setattr(crtsupport, "has_crt_s3transfer", lambda: True)
        monkeypatch.setattr(crtsupport, "acquire_process_lock", lambda: True)
        monkeypatch.setattr(crtsupport, "should_use_crt", lambda preferred: preferred == "crt")
        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", create)
        return seen

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(["cp", "{file}", "s3://bucket/key"], id="cp-upload"),
            pytest.param(["cp", "s3://bucket/key", "{dir}/dl.txt"], id="cp-download"),
            pytest.param(["cp", "{file}", "s3://bucket/key", "--dryrun"], id="cp-dryrun"),
            pytest.param(["mv", "{file}", "s3://bucket/key"], id="mv"),
            pytest.param(["sync", "{dir}", "s3://bucket/p"], id="sync"),
            pytest.param(["rm", "s3://bucket/key"], id="rm"),
            pytest.param(["rm", "s3://bucket/key", "--dryrun"], id="rm-dryrun"),
            pytest.param(["rm", "s3://bucket/key", "--quiet"], id="rm-quiet"),
            pytest.param(["rm", "s3://"], id="rm-bucket-less"),
        ],
    )
    def test_a_construction_failure_is_the_empty_255_report(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        crt_constructions: list[dict[str, Any]],
        argv: list[str],
    ) -> None:
        source = tmp_path / "f.txt"
        source.write_bytes(b"x")
        ctx, _calls = _config_ctx(tmp_path, monkeypatch, self._CRT)
        resolved = [token.format(file=source, dir=tmp_path) for token in argv]
        result = run_cli_in_process(resolved, ctx=ctx)
        # --quiet suppresses the result printer's lines, never the handler's
        # report; --dryrun never gets to record anything.
        assert (result.rc, result.stdout, result.stderr) == (255, "", _EMPTY_REPORT), resolved
        assert len(crt_constructions) == 1, resolved

    def test_an_s3s3_route_never_reaches_the_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crt_constructions: list[Any]
    ) -> None:
        # aws's _compute_transfer_client_type forces classic for s3s3 (the CRT
        # manager has no copy), so the construction is not attempted at all.
        config_file = tmp_path / "config"
        config_file.write_text(self._CRT)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        client, calls = make_recording_client(
            [{"ContentLength": 1, "ETag": '"e"', "LastModified": MTIME}, {}]
        )
        ctx = Context(client_factory=lambda _args: client, transfer_config=None)
        result = run_cli_in_process(["cp", "s3://bucket/a", "s3://bucket/b"], ctx=ctx)
        assert result.rc == 0, (result.stderr, calls)
        assert crt_constructions == []

    def test_a_classic_selection_never_reaches_the_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crt_constructions: list[Any]
    ) -> None:
        body = "[default]\ns3 =\n  preferred_transfer_client = classic\n"
        ctx, _calls = _config_ctx(tmp_path, monkeypatch, body)
        result = run_cli_in_process(["rm", "s3://bucket/key", "--dryrun"], ctx=ctx)
        assert (result.rc, result.stdout) == (0, "(dryrun) delete: s3://bucket/key\n")
        assert crt_constructions == []
