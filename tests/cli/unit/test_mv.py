"""Unit tests for the ``boto3-s3 mv`` subcommand (dispatch + exit codes).

The rc shape (docs/cli.md section 6): mv is
cp with the onto-itself guard family in front - the local-local pair, any
``-`` path, the onto-itself shapes (recursive included), and the
checksum/path-format pairing are all 252 before any client factory runs;
``--validate-same-s3-paths`` resolution failures keep their class (an
unresolvable path 252, a failing s3control/sts call 254 via the kept
ClientError cause); the access-point warning goes to stderr without
touching the rc. ``--expected-size`` does not exist on mv (Unknown options,
252).
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest
from boto3.s3.transfer import TransferConfig

from boto3_s3_cli import cli
from boto3_s3_cli.commands.base import Context
from tests.utils.recorder import ApiCall, make_recording_client

_SYNC = TransferConfig(use_threads=False)

_AP_ARN = "arn:aws:s3:us-west-2:123456789012:accesspoint/myaccesspoint"

_ONTO_ITSELF = "Cannot mv a file onto itself"
_WARNING_TOKEN = "may resolve to same underlying s3 object(s)"


def _failing_factory_ctx() -> Context:
    def factory(_args: argparse.Namespace) -> Any:
        raise AssertionError("client factory must not be called on this path")

    def service_factory(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("service client factory must not be called on this path")

    return Context(client_factory=factory, service_client_factory=service_factory)


class _FakeS3Control:
    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        self.calls: list[dict[str, Any]] = []

    def get_access_point(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"Bucket": self.bucket}


class _FakeSts:
    def __init__(self, account: str = "123456789012") -> None:
        self.account = account
        self.calls = 0

    def get_caller_identity(self) -> dict[str, Any]:
        self.calls += 1
        return {"Account": self.account}


def _resolver_ctx(
    *,
    bucket: str,
    responses: list[dict[str, Any] | Exception] | None = None,
) -> tuple[Context, _FakeS3Control, _FakeSts, list[tuple[str, Any]], list[ApiCall]]:
    """A Context whose service factory hands out fakes and records regions."""
    s3control = _FakeS3Control(bucket)
    sts = _FakeSts()
    service_calls: list[tuple[str, Any]] = []
    client, calls = make_recording_client(responses or [])

    def service_factory(service: str, _args: argparse.Namespace, *, region: Any = None) -> Any:
        service_calls.append((service, region))
        return {"s3control": s3control, "sts": sts}[service]

    ctx = Context(
        client_factory=lambda _args: client,
        service_client_factory=service_factory,
        transfer_config=_SYNC,
    )
    return ctx, s3control, sts, service_calls, calls


def _head_response() -> dict[str, Any]:
    from datetime import datetime, timezone

    return {
        "ContentLength": 1,
        "LastModified": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "ETag": '"e"',
    }


class TestUsageErrors:
    def test_local_to_local_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["mv", "a.txt", "b.txt"], ctx=_failing_factory_ctx())
        assert rc == 252
        err = capsys.readouterr().err
        assert "usage: boto3-s3 mv" in err
        assert "Error: Invalid argument type" in err

    @pytest.mark.parametrize(
        "argv",
        [["mv", "-", "s3://b/k"], ["mv", "s3://b/k", "-"], ["mv", "-", "s3://b/k", "--recursive"]],
    )
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
        rc = cli.main(["mv", "-", "-"], ctx=_failing_factory_ctx())
        assert rc == 252
        assert "Error: Invalid argument type" in capsys.readouterr().err

    def test_onto_itself_is_252(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["mv", "s3://b/k.txt", "s3://b/k.txt"], ctx=_failing_factory_ctx())
        assert rc == 252
        assert (
            "Cannot mv a file onto itself: s3://b/k.txt - s3://b/k.txt" in capsys.readouterr().err
        )

    def test_onto_itself_implied_basename(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(["mv", "s3://b/d/a.txt", "s3://b/d/"], ctx=_failing_factory_ctx())
        assert rc == 252
        assert "Cannot mv a file onto itself: s3://b/d/a.txt - s3://b/d/" in capsys.readouterr().err

    def test_onto_itself_keyless_destination_is_normalized(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(["mv", "s3://b/k.txt", "s3://b"], ctx=_failing_factory_ctx())
        assert rc == 252
        assert "Cannot mv a file onto itself: s3://b/k.txt - s3://b/" in capsys.readouterr().err

    def test_onto_itself_applies_to_recursive_too(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The aws-cli's faithful false positive (rc 252).
        rc = cli.main(["mv", "--recursive", "s3://b/d", "s3://b/"], ctx=_failing_factory_ctx())
        assert rc == 252
        assert "Cannot mv a file onto itself: s3://b/d - s3://b/" in capsys.readouterr().err

    def test_checksum_algorithm_rejects_downloads(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = cli.main(
            ["mv", "s3://b/k", str(tmp_path / "f"), "--checksum-algorithm", "CRC32"],
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
            ["mv", str(src), "s3://b/k", "--checksum-mode", "ENABLED"],
            ctx=_failing_factory_ctx(),
        )
        assert rc == 252
        assert (
            "Expected checksum-mode parameter to be used with one of following "
            "path formats: <S3Uri> <LocalPath>. "
            "Instead, received <LocalPath> <S3Uri>." in capsys.readouterr().err
        )

    def test_expected_size_is_not_an_mv_option(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(
            ["mv", "s3://b/k", "s3://b/k2", "--expected-size", "5"],
            ctx=_failing_factory_ctx(),
        )
        assert rc == 252
        assert "Unknown options" in capsys.readouterr().err


class TestGeneralErrors:
    def test_missing_local_source_is_255_before_the_factory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "no-such.txt"
        rc = cli.main(["mv", str(missing), "s3://b/k"], ctx=_failing_factory_ctx())
        assert rc == 255
        assert f"The user-provided path {missing} does not exist." in capsys.readouterr().err

    def test_non_integer_page_size_is_255(self, capsys: pytest.CaptureFixture[str]) -> None:
        rc = cli.main(
            ["mv", "s3://b/k", "s3://b/k2", "--page-size", "abc"], ctx=_failing_factory_ctx()
        )
        assert rc == 255
        assert "invalid literal" in capsys.readouterr().err


class TestValidateSamePaths:
    def test_flag_resolves_and_rejects_the_same_underlying_path(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctx, s3control, sts, _, calls = _resolver_ctx(bucket="underlying")
        rc = cli.main(
            ["mv", f"s3://{_AP_ARN}/k.txt", "s3://underlying/k.txt", "--validate-same-s3-paths"],
            ctx=ctx,
        )
        assert rc == 252
        # The message reports the *original* URIs, not the resolved ones.
        assert (
            f"Cannot mv a file onto itself: s3://{_AP_ARN}/k.txt - s3://underlying/k.txt"
            in capsys.readouterr().err
        )
        assert s3control.calls == [{"AccountId": "123456789012", "Name": "myaccesspoint"}]
        assert sts.calls == 0
        assert calls == []  # no S3 client work happened

    def test_flag_lets_a_different_underlying_bucket_through(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ctx, _, _, _, calls = _resolver_ctx(
            bucket="elsewhere", responses=[_head_response(), {}, {}]
        )
        rc = cli.main(
            ["mv", f"s3://{_AP_ARN}/k.txt", "s3://underlying/k.txt", "--validate-same-s3-paths"],
            ctx=ctx,
        )
        assert rc == 0
        assert [call.operation for call in calls] == ["HeadObject", "CopyObject", "DeleteObject"]
        assert "move:" in capsys.readouterr().out

    def test_alias_resolution_asks_sts(self) -> None:
        ctx, s3control, sts, _, _ = _resolver_ctx(bucket="underlying")
        rc = cli.main(
            ["mv", "s3://my-ap-s3alias/k.txt", "s3://underlying/k.txt", "--validate-same-s3-paths"],
            ctx=ctx,
        )
        assert rc == 252
        assert sts.calls >= 1
        assert s3control.calls[0]["Name"] == "my-ap-s3alias"

    def test_source_region_routes_the_source_resolver(self) -> None:
        ctx, _, _, service_calls, _ = _resolver_ctx(bucket="underlying")
        cli.main(
            [
                "mv",
                f"s3://{_AP_ARN}/k.txt",
                "s3://underlying/k.txt",
                "--validate-same-s3-paths",
                "--source-region",
                "eu-west-1",
                "--region",
                "us-east-2",
            ],
            ctx=ctx,
        )
        # Source-side s3control in --source-region, destination's in
        # --region, sts without one (aws-cli from_session wiring).
        assert service_calls == [
            ("s3control", "eu-west-1"),
            ("sts", None),
            ("s3control", "us-east-2"),
            ("sts", None),
        ]

    def test_env_var_true_enables_validation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS", "true")
        ctx, s3control, _, _, _ = _resolver_ctx(bucket="underlying")
        rc = cli.main(["mv", f"s3://{_AP_ARN}/k.txt", "s3://underlying/k.txt"], ctx=ctx)
        assert rc == 252
        assert s3control.calls != []

    def test_env_var_one_is_not_true(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws-cli ensure_boolean: only the string 'true' counts (=1 still warns
        # instead of validating).
        monkeypatch.setenv("AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS", "1")
        ctx, s3control, _, _, _ = _resolver_ctx(
            bucket="underlying", responses=[_head_response(), {}, {}]
        )
        rc = cli.main(["mv", f"s3://{_AP_ARN}/k.txt", "s3://other/k.txt"], ctx=ctx)
        assert rc == 0
        assert s3control.calls == []
        assert _WARNING_TOKEN in capsys.readouterr().err

    def test_different_keys_skip_resolution_entirely(self) -> None:
        ctx, _, _, service_calls, _ = _resolver_ctx(
            bucket="underlying", responses=[_head_response(), {}, {}]
        )
        rc = cli.main(
            ["mv", f"s3://{_AP_ARN}/k.txt", "s3://other/renamed.txt", "--validate-same-s3-paths"],
            ctx=ctx,
        )
        assert rc == 0
        assert service_calls == []

    def test_warning_without_the_flag(self, capsys: pytest.CaptureFixture[str]) -> None:
        ctx, _, _, service_calls, _ = _resolver_ctx(
            bucket="unused", responses=[_head_response(), {}, {}]
        )
        rc = cli.main(["mv", f"s3://{_AP_ARN}/k.txt", "s3://other/k.txt"], ctx=ctx)
        assert rc == 0
        assert service_calls == []
        err = capsys.readouterr().err
        assert _WARNING_TOKEN in err
        assert "AWS_CLI_S3_MV_VALIDATE_SAME_S3_PATHS environment variable to true" in err

    def test_warning_prints_even_under_quiet(self, capsys: pytest.CaptureFixture[str]) -> None:
        # aws writes it straight to stderr during validation, outside the
        # result printer --quiet silences.
        ctx, _, _, _, _ = _resolver_ctx(bucket="unused", responses=[_head_response(), {}, {}])
        rc = cli.main(["mv", f"s3://{_AP_ARN}/k.txt", "s3://other/k.txt", "--quiet"], ctx=ctx)
        assert rc == 0
        assert _WARNING_TOKEN in capsys.readouterr().err

    def test_plain_buckets_with_the_flag_resolve_without_calls(self) -> None:
        ctx, s3control, sts, _, _ = _resolver_ctx(
            bucket="unused", responses=[_head_response(), {}, {}]
        )
        rc = cli.main(["mv", "s3://b1/k.txt", "s3://b2/k.txt", "--validate-same-s3-paths"], ctx=ctx)
        assert rc == 0
        assert s3control.calls == []
        assert sts.calls == 0


class TestSuccessShapes:
    def test_upload_move_line_and_source_deletion(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        client, calls = make_recording_client([{}])
        ctx = Context(client_factory=lambda _args: client, transfer_config=_SYNC)
        rc = cli.main(["mv", str(src), "s3://b/k.txt"], ctx=ctx)
        assert rc == 0
        assert [call.operation for call in calls] == ["PutObject"]
        assert not src.exists()
        assert "move: " in capsys.readouterr().out

    def test_dryrun_move_line(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        client, calls = make_recording_client([])
        ctx = Context(client_factory=lambda _args: client, transfer_config=_SYNC)
        rc = cli.main(["mv", str(src), "s3://b/k.txt", "--dryrun"], ctx=ctx)
        assert rc == 0
        assert calls == []
        assert src.exists()
        assert "(dryrun) move: " in capsys.readouterr().out

    def test_delete_failure_prints_move_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import os as os_module

        src = tmp_path / "a.txt"
        src.write_bytes(b"x")
        client, _ = make_recording_client([{}])
        ctx = Context(client_factory=lambda _args: client, transfer_config=_SYNC)

        def _boom(path: object) -> None:
            raise OSError(13, "Permission denied", str(path))

        monkeypatch.setattr(os_module, "remove", _boom)
        rc = cli.main(["mv", str(src), "s3://b/k.txt"], ctx=ctx)
        assert rc == 1
        err = capsys.readouterr().err
        assert "move failed: " in err
        assert "[Errno 13] Permission denied" in err
