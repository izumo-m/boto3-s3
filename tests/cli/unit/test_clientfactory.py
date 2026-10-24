"""Unit tests for boto3_s3_cli.clientfactory (global options -> boto3 client)."""

from __future__ import annotations

import argparse
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import InvalidConfigError as BotocoreInvalidConfigError
from botocore.exceptions import NoRegionError
from botocore.utils import JSONFileCache

from boto3_s3 import (
    Boto3S3Error,
    ConfigurationError,
    InvalidConfigError,
    InvalidValueError,
    ValidationError,
    awsclisession,
)
from boto3_s3_cli import clientfactory, globalargs, s3errormsg
from boto3_s3_cli.cli import exit_code_for
from boto3_s3_cli.commands import transferargs


def _client(args: argparse.Namespace) -> Any:
    """The command's S3 client for *args* (`build_s3`'s ``client()``)."""
    return clientfactory.build_s3(args).client()


def _session(args: argparse.Namespace) -> Any:
    """The session the command opens for *args*."""
    s3 = clientfactory.build_s3(args)
    assert s3.session is not None
    return s3.session


def _service_client(service: str, args: argparse.Namespace, *, region: str | None = None) -> Any:
    """A client of *service* the command builds for *args* (mv's resolver clients)."""
    return clientfactory.build_s3(args).service_client(service, region=region)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    globalargs.add_common_arguments(parser)
    return parser.parse_args(argv)


def _parse_transfer(argv: list[str]) -> argparse.Namespace:
    """A cp/mv/sync namespace, which is the only place ``--sse`` exists.

    Built from the real transfer surface rather than by setting attributes, so
    the option's own spelling and ``dest`` are what the client builders read.
    """
    parser = argparse.ArgumentParser()
    globalargs.add_common_arguments(parser)
    transferargs.add_transfer_arguments(parser)
    return parser.parse_args(argv)


class TestBuildClient:
    def test_build_s3_binds_client_and_config_to_one_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_file = tmp_path / "config"
        config_file.write_text(
            "[profile bound]\nregion = ap-northeast-1\ns3 =\n  multipart_threshold = 17\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        args = _parse(["--profile", "bound", "--endpoint-url", "http://localhost:9000"])

        sessions: list[Any] = []
        real_open = awsclisession.open_botocore_session

        def counting_open(settings: Any) -> Any:
            session = real_open(settings)
            sessions.append(session)
            return session

        monkeypatch.setattr(awsclisession, "open_botocore_session", counting_open)
        s3 = clientfactory.build_s3(args)

        assert s3.session is not None
        assert s3.session.profile_name == "bound"
        assert s3.client().meta.endpoint_url == "http://localhost:9000"
        assert s3.aws_config().get_str("s3.multipart_threshold") == "17"
        # "One session": the client and the config read above both came off the
        # single session build_s3 made - nothing built a second one.
        assert sessions == [s3.session._session]  # pyright: ignore[reportPrivateUsage]
        # The S3-level endpoint copy feeds the CRT lane's explicit-endpoint
        # pin (design/crt.md); it must be the same string the clients apply,
        # so the Transferrer's meta-equality gate recognizes the CLI client.
        assert s3._endpoint_url == "http://localhost:9000"  # pyright: ignore[reportPrivateUsage]

    def test_build_s3_takes_aws_clis_posture_on_lock_contention(self) -> None:
        # aws-cli's factory acquires the cross-process CRT lock best-effort
        # and, under an explicit 'crt' preference, builds the CRT client
        # regardless - so construction-time failures surface under contention
        # too, where the library default would silently run classic and, on a
        # dryrun, report success (design/crt.md section 6).
        s3 = clientfactory.build_s3(_parse([]))
        assert s3._crt.crt.allow_lockless is True  # pyright: ignore[reportPrivateUsage]

    @pytest.fixture
    def crt_builds(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        """Fake a usable awscrt and record every CRT construction attempt."""
        from boto3_s3 import crtsupport

        seen: list[dict[str, Any]] = []

        def create(client: Any, config: Any, **kwargs: Any) -> Any:
            seen.append({"client": client, **kwargs})
            return object()

        monkeypatch.setattr(crtsupport, "has_minimum_crt_version", lambda: True)
        monkeypatch.setattr(crtsupport, "has_crt_s3transfer", lambda: True)
        monkeypatch.setattr(crtsupport, "acquire_process_lock", lambda: True)
        monkeypatch.setattr(crtsupport, "should_use_crt", lambda preferred: preferred == "crt")
        monkeypatch.setattr(crtsupport, "create_crt_transfer_manager", create)
        return seen

    @pytest.mark.parametrize(
        ("argv", "env", "preferred", "stops"),
        [
            ([], {}, "crt", True),
            ([], {}, "classic", False),
            ([], {"AWS_REGION": "eu-west-1"}, "crt", False),
            (["--region", "ap-northeast-1"], {}, "crt", False),
            (["--region", "aws-global"], {}, "crt", False),
        ],
        ids=[
            "crt-nothing-resolves",
            "classic-nothing-resolves",
            "crt-env",
            "crt-explicit",
            "crt-explicit-aws-global",
        ],
    )
    def test_a_crt_run_needs_a_resolved_region(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        crt_builds: list[dict[str, Any]],
        argv: list[str],
        env: dict[str, str],
        preferred: str,
        stops: bool,
    ) -> None:
        # aws-cli hands its region chain's answer to the CRT client, so a CRT run
        # with nothing resolved stops before the engine is built - where
        # botocore gives the client the `aws-global` pseudo-region the CRT
        # would sign every request for. The classic engine needs no region.
        from boto3_s3 import TransferConfig

        for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        for var, value in env.items():
            monkeypatch.setenv(var, value)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        s3 = clientfactory.build_s3(_parse(argv))
        client = s3.client()
        config = TransferConfig(preferred_transfer_client=preferred)
        if stops:
            assert client.meta.region_name == "aws-global"
            with pytest.raises(ConfigurationError) as info:
                s3.materialize_crt_engine(client, transfer_config=config)
            assert str(info.value) == str(NoRegionError())
            assert exit_code_for(info.value) == 253
            assert crt_builds == []
        else:
            s3.materialize_crt_engine(client, transfer_config=config)
            assert len(crt_builds) == (1 if preferred == "crt" else 0)
            if crt_builds:
                assert "region" not in crt_builds[0]

    def test_the_region_chain_is_walked_once_per_invocation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The chain's last link is the EC2 IMDS probe, which on a host with no
        # region configured and no metadata service answering costs seconds -
        # so walking it once per client instead of once per invocation was
        # measurably slower than aws. `build_s3` resolves it and threads the
        # answer into every client it hands out; the answer cannot change
        # mid-invocation, so this is a pure saving.
        for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        real_resolve = awsclisession.resolve_region
        walks: list[str | None] = []

        def counting_resolve(explicit: str | None, session: Any) -> Any:
            resolved = real_resolve(explicit, session)
            walks.append(resolved)
            return resolved

        monkeypatch.setattr(awsclisession, "resolve_region", counting_resolve)
        s3 = clientfactory.build_s3(_parse([]))
        first, second = s3.client(), s3.client()
        assert walks == [None]  # the one walk, and it resolved nothing
        # Threading it changes no outcome: both clients land where the chain
        # said, which for an unresolved region is botocore's `aws-global`.
        assert first.meta.region_name == second.meta.region_name == "aws-global"

    def test_cli_sessions_install_the_fast_timestamp_parser(self) -> None:
        # Every CLI-built session registers the library's fast_parse_timestamp
        # on its response-parser factory before any client exists; listing
        # timestamps then parse at C speed (tests/lib/test_sessions.py pins
        # the parser's value-equality with botocore).
        from boto3_s3 import fast_parse_timestamp

        session = _session(_parse([]))
        factory = session._session.get_component(  # pyright: ignore[reportPrivateUsage]
            "response_parser_factory"
        )
        parser = factory.create_parser("rest-xml")
        assert (
            parser._timestamp_parser  # pyright: ignore[reportPrivateUsage]
            is fast_parse_timestamp
        )

    def test_region_and_endpoint_applied(self) -> None:
        args = _parse(["--region", "us-west-2", "--endpoint-url", "http://localhost:9000"])
        client = _client(args)
        assert client.meta.region_name == "us-west-2"
        assert client.meta.endpoint_url == "http://localhost:9000"

    def test_defaults_build_an_s3_client(self) -> None:
        client = _client(_parse([]))
        assert client.meta.service_model.service_name == "s3"

    def test_retry_defaults_match_aws_v2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli v2's bundled botocore hard-codes retry_mode='standard' /
        # max_attempts=3 as its defaults (stock botocore: legacy / 5), so
        # every request retries like `aws s3`'s.
        monkeypatch.delenv("AWS_RETRY_MODE", raising=False)
        monkeypatch.delenv("AWS_MAX_ATTEMPTS", raising=False)
        client = _client(_parse([]))
        assert client.meta.config.retries == {"mode": "standard", "total_max_attempts": 3}

    def test_retry_env_overrides_beat_the_aws_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The aws-cli default only fills in; the user's env/config still wins.
        monkeypatch.setenv("AWS_RETRY_MODE", "adaptive")
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "7")
        client = _client(_parse([]))
        assert client.meta.config.retries == {"mode": "adaptive", "total_max_attempts": 7}

    def test_scalar_s3_section_fails_at_client_build_like_aws(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `s3 = max_concurrent_requests=1` (a scalar where a section is
        # expected): both aws-cli v2 and stock botocore die at client construction
        # (_resolve_use_dualstack_endpoint calls .get on the str) with
        # AttributeError "'str' object has no attribute 'get'" -> the general
        # backstop's rc 255 and the same message (measured on the pinned
        # aws-cli and this CLI, ls and cp alike); the library reports it as
        # InvalidConfigError, rc 255 with the same text. Pinned so a future flow
        # reorder (reading [s3] ahead of the client build) cannot turn the
        # typo into a silent default.
        config_file = tmp_path / "config"
        config_file.write_text("[default]\ns3 = max_concurrent_requests=1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        with pytest.raises(InvalidConfigError, match="'str' object has no attribute 'get'"):
            _client(_parse([]))

    def test_empty_retry_env_is_present_and_fatal_like_aws(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Present-wins, like the profile/region chains: aws-cli treats an empty
        # AWS_MAX_ATTEMPTS as a fatal value (rc 255), never as unset - int("")
        # -> ValueError, which the library reports as InvalidConfigError (255).
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "")
        with pytest.raises(InvalidConfigError, match="invalid literal"):
            _client(_parse([]))

    def test_aws_region_env_honored_like_aws_v2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli v2 resolves AWS_REGION ahead of AWS_DEFAULT_REGION; stock
        # botocore only knows AWS_DEFAULT_REGION (which the test fixture pins
        # to us-east-1), so this passes only through the library's region chain's explicit
        # injection.
        monkeypatch.setenv("AWS_REGION", "eu-west-3")
        client = _client(_parse([]))
        assert client.meta.region_name == "eu-west-3"

    def test_explicit_region_beats_aws_region_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-west-3")
        client = _client(_parse(["--region", "us-west-2"]))
        assert client.meta.region_name == "us-west-2"

    def test_aws_region_beats_aws_default_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli's region chain lists AWS_REGION ahead of AWS_DEFAULT_REGION;
        # stock botocore never adopted AWS_REGION, so aws-cli's region chain
        # (awsclisession.region_chain) restores it.
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-1")
        client = _client(_parse([]))
        assert client.meta.region_name == "eu-central-1"

    def test_empty_aws_region_is_present_wins_like_aws(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli's env providers are present-wins: AWS_REGION="" selects the
        # empty region (botocore -> "Invalid endpoint", rc 255 like aws-cli), it does
        # NOT fall through to AWS_DEFAULT_REGION (the old `or None` wrongly did).
        monkeypatch.setenv("AWS_REGION", "")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-1")
        with pytest.raises(InvalidConfigError, match="Invalid endpoint"):
            _client(_parse([]))

    def test_imds_region_is_the_final_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli's chain ends in IMDSRegionProvider; with nothing else set the
        # EC2 instance region is used (boto3-s3 must not silently default to
        # us-east-1). Stock botocore keeps IMDS for smart-defaults only, so
        # the region chain wires it in explicitly. Fake the provider (no network).
        import botocore.utils

        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        class _FakeIMDS:
            def __init__(self, *args: object, **kwargs: object) -> None: ...

            def provide(self) -> str:
                return "ap-southeast-2"

        monkeypatch.setattr(botocore.utils, "IMDSRegionProvider", _FakeIMDS)
        client = _client(_parse([]))
        assert client.meta.region_name == "ap-southeast-2"

    def test_a_rejected_imds_probe_reads_as_no_region(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # botocore's region fetcher swallows only the retries-exceeded failure,
        # so a service answering the IMDS address that is not EC2's - it
        # rejects the token request outright - raises BadIMDSRequestError out
        # of the chain. aws-cli carries its own fetcher for exactly that catch,
        # so on such a host aws-cli resolves no region and runs on; escaping here
        # would instead fail every invocation at the client build (rc 255).
        import botocore.awsrequest
        import botocore.utils

        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        token_request = botocore.awsrequest.AWSRequest(
            method="PUT", url="http://169.254.169.254/latest/api/token"
        )

        class _RejectingIMDS:
            def __init__(self, *args: object, **kwargs: object) -> None: ...

            def provide(self) -> str:
                raise botocore.utils.BadIMDSRequestError(token_request)

        monkeypatch.setattr(botocore.utils, "IMDSRegionProvider", _RejectingIMDS)
        # The unresolved-region client, exactly as with no service answering.
        assert _client(_parse([])).meta.region_name == "aws-global"

    def test_us_east_1_resolves_regional_endpoint(self) -> None:
        # aws-cli v2 resolves us-east-1 to the regional endpoint, not the legacy
        # global one (aws-cli functional-test expectations); the assembly pins
        # the same resolution.
        client = _client(_parse(["--region", "us-east-1"]))
        assert client.meta.endpoint_url == "https://s3.us-east-1.amazonaws.com"

    def test_presigned_urls_are_sigv4_even_in_us_east_1(self) -> None:
        # Stock botocore downgrades a presigned URL to SigV2 where the region
        # still accepts it; aws-cli v2's botocore has no SigV2 at all. The
        # library's presign signs SigV4 for any client (no client pin).
        url = clientfactory.build_s3(_parse(["--region", "us-east-1"])).presign(
            "s3://bucket/key", expires_in=60
        )
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url

    def test_symmetric_sigv4_is_signed_pure_python(self) -> None:
        # With awscrt importable stock botocore signs symmetric SigV4 with
        # awscrt's signer, whose presigner renders X-Amz-Expires after
        # X-Amz-SignedHeaders; aws-cli v2's bundled botocore signs pure
        # Python. The library's presign does the same per call, leaving
        # botocore's process-wide signer table alone.
        url = clientfactory.build_s3(_parse(["--region", "us-east-1"])).presign(
            "s3://bucket/key", expires_in=60
        )
        assert url.index("X-Amz-Expires=") < url.index("X-Amz-SignedHeaders=")

    def test_no_signature_version_is_pinned(self) -> None:
        # aws-cli's clients name none, so botocore's endpoint rules choose the
        # scheme per request - SigV4, or SigV4a / sigv4-s3express where the
        # target needs it (the tests below).
        client = _client(_parse(["--region", "us-east-1"]))
        resolver = client._ruleset_resolver  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        assert resolver._requested_auth_scheme is None  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.parametrize("alias", ["test.mrap", "my-alias.mrap", "plainname"])
    def test_an_mrap_target_signs_sigv4a(self, alias: str) -> None:
        # An MRAP endpoint resolves to asymmetric SigV4a (region set `*`) -
        # measured against aws-cli 2.36.40, whose MRAP presign signs
        # AWS4-ECDSA-P256-SHA256 for each of these aliases (the empty ARN region
        # is the whole test; the `.mrap` suffix is a convention). The dev
        # environment's awscrt (always present, design/testing.md section 4)
        # signs SigV4a offline.
        arn = f"arn:aws:s3::123456789012:accesspoint/{alias}"
        args = _parse(["--region", "us-east-1"])
        args.path = f"s3://{arn}/key"
        url = clientfactory.build_s3(args).presign(f"s3://{arn}/key", expires_in=60)
        assert "X-Amz-Algorithm=AWS4-ECDSA-P256-SHA256" in url
        assert "X-Amz-Region-Set=" in url

    @pytest.mark.parametrize(
        ("region", "arn"),
        [
            (
                "us-west-2",
                "arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-01/accesspoint/apx",
            ),
            (
                "us-west-2",
                "arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-01/accesspoint:apx",
            ),
            (
                "us-west-2",
                "arn:aws:s3-outposts:us-west-2:123456789012:outpost:op-01:accesspoint:apx",
            ),
            (
                "us-west-2",
                "arn:aws:s3-outposts:us-west-2:123456789012:outpost:op-01/accesspoint/apx",
            ),
            (
                "us-west-2",
                "arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-0-1/accesspoint/ApX",
            ),
            (
                "us-gov-west-1",
                "arn:aws-us-gov:s3-outposts:us-gov-west-1:123456789012:outpost/op-01/accesspoint/apx",
            ),
        ],
    )
    def test_an_outposts_target_signs_sigv4a(self, region: str, arn: str) -> None:
        # An S3 Outposts access point resolves to SigV4a like an MRAP -
        # measured against aws-cli 2.36.40, whose presign for each of these
        # signs AWS4-ECDSA-P256-SHA256 with a region-less
        # `.../s3-outposts/aws4_request` scope. Every separator mix, an
        # uppercase access point name, a dashed outpost id and a non-`aws`
        # partition are all one shape to botocore's ARN parse.
        args = _parse(["--region", region])
        args.path = f"s3://{arn}/key"
        url = clientfactory.build_s3(args).presign(f"s3://{arn}/key", expires_in=60)
        assert "X-Amz-Algorithm=AWS4-ECDSA-P256-SHA256" in url
        assert "X-Amz-Region-Set=" in url
        assert f"%2F{region}%2F" not in url  # SigV4a scope carries no region

    @pytest.mark.parametrize(
        "alias",
        [
            "test-accessp-o0b1d075431d83bebde8xz5w8ijx1qzlbp3i3kuse10--op-s3",
            "test-accessp-e0000075431d83bebde8xz5w8ijx1qzlbp3i3kuse10--op-s3",
            "o0b1d075431d83bebde8xz5w8ijx1qzlbp3i3kuse10--op-s3",  # the 50-char floor
            "o0b1d0-5431d83-bcde8xz5w8ijx1qzlbp3i3kuse10--op-s3",  # dashed outpost id
        ],
    )
    def test_an_outposts_alias_signs_sigv4a(self, alias: str) -> None:
        # An Outposts access point *alias* has no ARN, but resolves to the same
        # SigV4a endpoint as the ARN does - measured against aws-cli 2.36.40, whose
        # presign for each of these signs AWS4-ECDSA-P256-SHA256 with a
        # region-less `.../s3-outposts/aws4_request` scope. The endpoint rules
        # read the alias by position (a `--op-s3` suffix, the hardware type 50
        # characters from the end, a host-label outpost id before it).
        args = _parse(["--region", "us-west-2"])
        args.path = f"s3://{alias}/key"
        url = clientfactory.build_s3(args).presign(f"s3://{alias}/key", expires_in=60)
        assert "X-Amz-Algorithm=AWS4-ECDSA-P256-SHA256" in url
        assert "X-Amz-Region-Set=" in url
        assert "%2Fus-west-2%2F" not in url  # SigV4a scope carries no region

    def test_a_scheme_less_outposts_alias_signs_sigv4a(self) -> None:
        # presign takes its target with or without s3://, and aws-cli resolves the
        # auth scheme off the final Bucket either way.
        alias = "test-accessp-o0b1d075431d83bebde8xz5w8ijx1qzlbp3i3kuse10--op-s3"
        args = _parse(["--region", "us-west-2"])
        args.path = f"{alias}/key"  # no s3:// scheme
        url = clientfactory.build_s3(args).presign(f"s3://{alias}/key", expires_in=60)
        assert "X-Amz-Algorithm=AWS4-ECDSA-P256-SHA256" in url

    @pytest.mark.parametrize(
        "bucket",
        [
            # A character short of the 50 the rules need to find a hardware
            # type, and a dotted alias: aws-cli presigns both plain SigV4.
            "0b1d075431d83bebde8xz5w8ijx1qzlbp3i3kuse10--op-s3",
            "myap-1234.op-01234567890123456--op-s3",
            # Full length, but not the suffix.
            "test-accessp-o0b1d075431d83bebde8xz5w8ijx1qzlbp3i3kuse10--op-s4",
        ],
    )
    def test_near_miss_outposts_aliases_sign_sigv4(self, bucket: str) -> None:
        args = _parse(["--region", "us-west-2"])
        args.path = f"s3://{bucket}/key"
        url = clientfactory.build_s3(args).presign(f"s3://{bucket}/key", expires_in=60)
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url

    def test_a_plain_bucket_signs_sigv4(self) -> None:
        # A plain bucket (and a plain access-point ARN) signs SigV4 - only the
        # MRAP and Outposts shapes need SigV4a.
        args = _parse(["--region", "us-east-1"])
        args.paths = "s3://plain-bucket/key"
        url = clientfactory.build_s3(args).presign("s3://plain-bucket/key", expires_in=60)
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url

    def test_a_plain_access_point_arn_signs_sigv4(self) -> None:
        # Region-qualified access-point ARN: not the MRAP shape (that one has
        # an empty region field), so it signs SigV4.
        arn = "arn:aws:s3:us-east-1:123456789012:accesspoint/plain-ap"
        args = _parse(["--region", "us-east-1"])
        args.paths = [f"s3://{arn}/key", "./local.txt"]
        url = clientfactory.build_s3(args).presign(f"s3://{arn}/key", expires_in=60)
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url

    def test_no_sign_request_presigns_a_bare_url_for_s3express(self) -> None:
        # --no-sign-request is UNSIGNED even for a directory bucket: the bare
        # zonal-endpoint URL, as on `aws`.
        args = _parse(["--no-sign-request", "--region", "us-east-1"])
        args.path = "s3://mybkt--use1-az4--x-s3/key"
        client = _client(args)
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": "mybkt--use1-az4--x-s3", "Key": "key"},
            ExpiresIn=60,
        )
        assert url == "https://mybkt--use1-az4--x-s3.s3express-use1-az4.us-east-1.amazonaws.com/key"

    def test_no_sign_request_presigns_a_bare_url_for_mrap(self) -> None:
        # --no-sign-request is UNSIGNED even for an MRAP target: `aws` emits the
        # bare URL.
        args = _parse(["--no-sign-request", "--region", "us-east-1"])
        args.path = "s3://arn:aws:s3::123456789012:accesspoint/test.mrap/key"
        client = _client(args)
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": "arn:aws:s3::123456789012:accesspoint/test.mrap", "Key": "key"},
            ExpiresIn=60,
        )
        assert url == "https://test.mrap.accesspoint.s3-global.amazonaws.com/key"

    def test_no_sign_request_presigns_to_a_bare_url(self) -> None:
        # --no-sign-request must still override to UNSIGNED: `aws` emits the
        # plain object URL with no query at all.
        client = _client(_parse(["--no-sign-request", "--region", "us-east-1"]))
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": "bucket", "Key": "key"}, ExpiresIn=60
        )
        assert url == "https://bucket.s3.us-east-1.amazonaws.com/key"

    def test_schemeless_endpoint_is_a_usage_error(self) -> None:
        # aws-cli rejects --endpoint-url without a scheme at parse time (rc 252);
        # without this, botocore raises a bare ValueError at client creation.
        with pytest.raises(ValidationError) as excinfo:
            _client(_parse(["--region", "us-east-1", "--endpoint-url", "example.com"]))
        assert "example.com" in str(excinfo.value)

    def test_zero_timeout_means_no_timeout(self) -> None:
        # aws-cli maps a 0 timeout to None ("no timeout"); botocore rejects a
        # literal 0 with ValueError, which would otherwise crash client creation.
        client = _client(
            _parse(
                ["--region", "us-east-1", "--cli-read-timeout", "0", "--cli-connect-timeout", "0"]
            )
        )
        assert client.meta.config.read_timeout is None
        assert client.meta.config.connect_timeout is None

    def test_integer_timeout_is_applied(self) -> None:
        client = _client(
            _parse(
                ["--region", "us-east-1", "--cli-read-timeout", "5", "--cli-connect-timeout", "7"]
            )
        )
        assert client.meta.config.read_timeout == 5
        assert client.meta.config.connect_timeout == 7

    @pytest.mark.parametrize("flag", ["--cli-read-timeout", "--cli-connect-timeout"])
    def test_noninteger_timeout_maps_to_255_not_a_parse_error(self, flag: str) -> None:
        # aws-cli coerces the timeouts in a post-parse handler (int()), so a non-integer
        # value raises there and exits 255 - not the parse-time 252 an argparse
        # type=int would give. The arg must still parse (no type=int rejecting it up
        # front), and the build must surface InvalidValueError, whose rc is the
        # general 255 - not ValidationError's 252 or ConfigurationError's 253.
        args = _parse(["--region", "us-east-1", flag, "abc"])
        with pytest.raises(InvalidValueError) as excinfo:
            _client(args)
        assert exit_code_for(excinfo.value) == 255

    def test_unknown_profile_maps_to_a_library_error(self) -> None:
        # A bad --profile raises raw botocore ProfileNotFound; the build must
        # translate it (-> InvalidConfigError, rc 255: aws-cli's general handler, not
        # the 253 pair) so it does not escape as an uncaught traceback.
        with pytest.raises(InvalidConfigError) as excinfo:
            _client(_parse(["--profile", "boto3_s3_definitely_nonexistent_profile"]))
        assert exit_code_for(excinfo.value) == 255
        assert "boto3_s3_definitely_nonexistent_profile" in str(excinfo.value)

    def test_aws_profile_env_beats_aws_default_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli (bundled botocore) resolves AWS_PROFILE ahead of
        # AWS_DEFAULT_PROFILE; stock botocore reverses the two (botocore #1725),
        # so a bare session would pick the wrong one. the build resolves the
        # profile itself to keep `aws s3` parity. The unknown profile surfaces in
        # the ProfileNotFound message, revealing which env var won.
        monkeypatch.setenv("AWS_PROFILE", "boto3_s3_from_aws_profile")
        monkeypatch.setenv("AWS_DEFAULT_PROFILE", "boto3_s3_from_default_profile")
        with pytest.raises(Boto3S3Error) as excinfo:
            _client(_parse([]))
        assert "boto3_s3_from_aws_profile" in str(excinfo.value)
        assert "boto3_s3_from_default_profile" not in str(excinfo.value)

    def test_aws_default_profile_env_used_when_aws_profile_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With only AWS_DEFAULT_PROFILE set it is honored (the second link of the
        # aws-cli env chain), so a subprocess that relied on it keeps working.
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_PROFILE", "boto3_s3_from_default_profile")
        with pytest.raises(Boto3S3Error) as excinfo:
            _client(_parse([]))
        assert "boto3_s3_from_default_profile" in str(excinfo.value)

    def test_profile_flag_beats_both_profile_envs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # --profile is the top of the chain, ahead of either env var.
        monkeypatch.setenv("AWS_PROFILE", "boto3_s3_from_aws_profile")
        monkeypatch.setenv("AWS_DEFAULT_PROFILE", "boto3_s3_from_default_profile")
        with pytest.raises(Boto3S3Error) as excinfo:
            _client(_parse(["--profile", "boto3_s3_from_flag"]))
        assert "boto3_s3_from_flag" in str(excinfo.value)

    def test_partial_credentials_map_to_255_not_253(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli has no handler for PartialCredentialsError -> GeneralExceptionHandler
        # (255), unlike NoCredentials/NoRegion (253). the build maps it to
        # InvalidConfigError, whose rc is the general 255, not 253.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        with pytest.raises(InvalidConfigError) as excinfo:
            _client(_parse(["--region", "us-east-1"]))
        assert exit_code_for(excinfo.value) == 255


class TestProfileSessionBinding:
    """Only a truthy ``--profile`` may be bound onto the session, as aws-cli does.

    botocore reads the session's *instance* variables to decide whether the
    user named a profile explicitly, and drops the environment credential
    provider when one is there (``create_credential_resolver``'s
    ``disable_env_vars``). aws-cli binds the flag alone, leaving ``AWS_PROFILE``
    / ``AWS_DEFAULT_PROFILE`` an ordinary env-level resolution - so the
    widespread "profile for config, env keys for credentials" combination keeps
    working, as measured against the pinned aws-cli.
    """

    _ENV_KEY = "AKIAENVENVENVENVENV0"
    _PROFILE_KEY = "AKIAPROFILEPROFILE00"

    @pytest.fixture(autouse=True)
    def _profile_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = tmp_path / "config"
        config.write_text("[default]\nregion = sa-east-1\n\n[profile named]\nregion = eu-north-1\n")
        credentials = tmp_path / "credentials"
        credentials.write_text(
            f"[named]\naws_access_key_id = {self._PROFILE_KEY}\n"
            "aws_secret_access_key = profile-secret\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", self._ENV_KEY)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-secret")
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    def _access_key(self, session: Any) -> str | None:
        credentials = session.get_credentials()
        return None if credentials is None else credentials.get_frozen_credentials().access_key

    def test_env_named_profile_keeps_the_environment_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The whole point: aws-cli signs with the env keys here (measured, rc 0),
        # where an instance-bound profile would report "Unable to locate
        # credentials" (rc 253) because the profile carries none of its own.
        monkeypatch.setenv("AWS_PROFILE", "named")
        assert self._access_key(_session(_parse([]))) == self._ENV_KEY

    def test_env_named_profile_still_selects_the_profile_s_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not promoting the env profile must not stop it selecting the profile:
        # the region comes from `[profile named]`, not `[default]`.
        monkeypatch.setenv("AWS_PROFILE", "named")
        assert _client(_parse([])).meta.region_name == "eu-north-1"

    def test_profile_flag_takes_the_profile_s_credentials(self) -> None:
        # The flag IS bound, so botocore disables the env provider and the
        # profile's own keys sign - aws-cli does the same (measured).
        session = _session(_parse(["--profile", "named"]))
        assert self._access_key(session) == self._PROFILE_KEY

    def test_env_named_profile_is_not_a_session_instance_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_PROFILE", "named")
        botocore_session = _session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        assert botocore_session.instance_variables().get("profile") is None
        assert botocore_session.get_config_variable("profile") == "named"

    def test_profile_flag_is_bound_as_a_session_instance_variable(self) -> None:
        session = _session(_parse(["--profile", "named"]))
        botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
        assert botocore_session.instance_variables().get("profile") == "named"

    def test_empty_profile_flag_leaves_the_env_profile_unpromoted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli's truthy guard: `--profile ""` is not bound, so the env chain
        # still names the profile and the env credentials survive.
        monkeypatch.setenv("AWS_PROFILE", "named")
        session = _session(_parse(["--profile", ""]))
        botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
        assert botocore_session.instance_variables().get("profile") is None
        assert botocore_session.get_config_variable("profile") == "named"
        assert self._access_key(session) == self._ENV_KEY

    def test_empty_env_profile_is_present_wins_and_fatal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `AWS_PROFILE=` names the empty profile on both tools, which no config
        # file declares - ProfileNotFound, rc 255.
        monkeypatch.setenv("AWS_PROFILE", "")
        with pytest.raises(InvalidConfigError) as excinfo:
            _client(_parse([]))
        assert exit_code_for(excinfo.value) == 255

    def test_aws_profile_beats_aws_default_profile_through_the_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The env order now lives in the session's own config chain, so the
        # aws-cli order has to be re-declared there: stock botocore reads
        # AWS_DEFAULT_PROFILE first and would resolve `[default]`'s sa-east-1.
        monkeypatch.setenv("AWS_PROFILE", "named")
        monkeypatch.setenv("AWS_DEFAULT_PROFILE", "default")
        assert _client(_parse([])).meta.region_name == "eu-north-1"

    def test_every_entry_point_opens_its_session_the_same_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One opener behind both of the command's `S3`s - build_s3's and the
        # test seam's: a site left on `Session(profile=...)` would silently
        # reinstate the promotion.
        from boto3_s3_cli.commands.base import Context

        opened: list[Any] = []
        real_opener = awsclisession.open_botocore_session

        def recording(settings: Any) -> Any:
            session = real_opener(settings)
            opened.append(session)
            return session

        monkeypatch.setattr(awsclisession, "open_botocore_session", recording)
        monkeypatch.setenv("AWS_PROFILE", "named")
        clientfactory.build_s3(_parse([]))
        Context(client_factory=lambda _args: object()).s3(_parse([]))
        assert len(opened) == 2
        assert [session.instance_variables().get("profile") for session in opened] == [None] * 2
        assert [session.get_config_variable("profile") for session in opened] == ["named"] * 2


class TestCredentialCache:
    """Assumed-role / web-identity / SSO credentials are cached on disk, as aws-cli does.

    botocore's own cache is a per-process dict, so every invocation would
    re-call ``AssumeRole`` and an ``mfa_serial`` profile would prompt for a
    code every time - fatal in a non-interactive run. aws-cli replaces it with a
    ``JSONFileCache`` over ``~/.aws/cli/cache``: measured against the pinned
    aws-cli, two consecutive ``ls`` runs under a ``role_arn`` profile make one
    ``AssumeRole`` call and leave a ``<sha1>.json`` there, and the same file
    name is what our botocore computes, so the two tools reuse each other's
    entries.
    """

    _PROVIDERS = ("assume-role", "assume-role-with-web-identity", "sso")

    @pytest.fixture(autouse=True)
    def _home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = tmp_path / "config"
        config.write_text("[default]\nregion = us-east-1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))  # Windows expanduser
        # An entry planted where `aws`'s own runs put theirs: reading it back
        # through a provider's cache pins the directory (aws-cli writes
        # `$HOME/.aws/cli/cache/<sha1>.json`, measured) without reaching into
        # the cache object.
        self._cache_dir(tmp_path).mkdir(parents=True)
        (self._cache_dir(tmp_path) / "planted-key.json").write_text(
            '{"AccessKeyId": "ASIAPLANTED"}'
        )

    def _cache_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "home" / ".aws" / "cli" / "cache"

    def _caches(self, session: Any) -> list[Any]:
        chain = session.get_component("credential_provider")
        return [chain.get_provider(name).cache for name in self._PROVIDERS]

    def test_the_temporary_credential_providers_cache_to_aws_s_directory(
        self, tmp_path: Path
    ) -> None:
        session = _session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        caches = self._caches(session)
        # botocore's own cache, with aws-cli's write and expiry rendering on top
        # (the two tests below) - and one class for all three providers.
        assert {type(cache) for cache in caches} == {type(caches[0])}
        assert [isinstance(cache, JSONFileCache) for cache in caches] == [True] * 3
        for cache in caches:
            assert "planted-key" in cache
            assert cache["planted-key"] == {"AccessKeyId": "ASIAPLANTED"}

    def test_an_expiry_is_stored_the_way_aws_stores_it(self, tmp_path: Path) -> None:
        # aws-cli's cli_timestamp_format handler makes the response parser return
        # ISO-8601 *strings*, so an STS Expiration lands in the file verbatim -
        # `2026-08-13T11:27:42+09:00`, measured. This CLI parses timestamps to
        # datetime for speed, and botocore's default rendering would write
        # `2026-08-13T11:27:42UTC+09:00` (`strftime('%Z')` of a
        # datetime.timezone), which dateutil reads back with POSIX's inverted
        # sign: measured, the entry stayed "valid" 18 hours past its expiry -
        # for this CLI *and* for an aws-cli sharing ~/.aws/cli/cache.
        cache = self._caches(_session(_parse([]))._session)[0]  # pyright: ignore[reportPrivateUsage]
        expiry = datetime(2026, 8, 13, 11, 27, 42, tzinfo=timezone(timedelta(hours=9)))
        cache["expiring-key"] = {"AccessKeyId": "ASIAX", "Expiration": expiry}
        assert (self._cache_dir(tmp_path) / "expiring-key.json").read_text() == (
            '{"AccessKeyId": "ASIAX", "Expiration": "2026-08-13T11:27:42+09:00"}'
        )
        # The plain `Z` wire form aws-cli's STS actually sends renders the same on
        # both sides too.
        cache["utc-key"] = {"Expiration": datetime(2099, 1, 1, tzinfo=timezone.utc)}
        assert (self._cache_dir(tmp_path) / "utc-key.json").read_text() == (
            '{"Expiration": "2099-01-01T00:00:00+00:00"}'
        )

    def test_an_entry_is_written_straight_to_its_final_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli's bundled botocore opens the entry itself (O_WRONLY | O_CREAT,
        # 0600); the installed one writes a `tempfile.mkstemp` file and
        # renames it, so a failed write named a random `tmpXXXXXXXX.tmp` where
        # aws-cli named the entry (measured on an unwritable cache directory: rc
        # 255 on both, different paths, and ours differed between its own
        # runs). Tripwire: mkstemp must not be reached at all.
        import tempfile

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the credential cache must not write through a temp file")

        monkeypatch.setattr(tempfile, "mkstemp", refuse)
        cache = self._caches(_session(_parse([]))._session)[0]  # pyright: ignore[reportPrivateUsage]
        cache["direct-key"] = {"AccessKeyId": "ASIADIRECT"}
        entry = self._cache_dir(tmp_path) / "direct-key.json"
        assert entry.read_text() == '{"AccessKeyId": "ASIADIRECT"}'
        assert sorted(path.suffix for path in self._cache_dir(tmp_path).iterdir()) == [
            ".json",
            ".json",
        ]
        if sys.platform != "win32":
            assert stat.S_IMODE(entry.stat().st_mode) == 0o600
        # Rewriting a shorter value must not leave the old tail behind (aws-cli
        # truncates after opening).
        cache["direct-key"] = {"A": "B"}
        assert entry.read_text() == '{"A": "B"}'

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
    @pytest.mark.skipif(
        getattr(os, "geteuid", lambda: -1)() == 0, reason="root ignores the write bit"
    )
    def test_an_unwritable_cache_names_the_entry_like_aws(self, tmp_path: Path) -> None:
        # The report a user greps to fix the permissions: aws-cli names the cache
        # entry (`.../c3cb4750....json`), byte-stable across runs.
        cache_dir = self._cache_dir(tmp_path)
        cache = self._caches(_session(_parse([]))._session)[0]  # pyright: ignore[reportPrivateUsage]
        cache_dir.chmod(0o500)
        try:
            with pytest.raises(PermissionError) as excinfo:
                cache["denied-key"] = {"AccessKeyId": "ASIADENIED"}
        finally:
            cache_dir.chmod(0o700)
        assert excinfo.value.filename == str(cache_dir / "denied-key.json")

    def test_the_sso_token_cache_is_written_the_same_way(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The `sso-session` provider writes a refreshed token back to
        # ~/.aws/sso/cache through a cache it builds itself, from this class
        # attribute. Through the installed botocore's temp file + rename, a
        # read-only entry in a writable directory was replaced (rc 0 where
        # `aws` is 255), a writable entry in a read-only directory could not be
        # (rc 255 where `aws` is 0), and a 0644 entry became 0600 (measured).
        import tempfile

        from botocore.tokens import SSOTokenProvider

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the token cache must not write through a temp file")

        # Restored after the test: the pin is process-wide by design.
        monkeypatch.setattr(
            SSOTokenProvider, "DEFAULT_CACHE_CLS", SSOTokenProvider.DEFAULT_CACHE_CLS
        )
        _session(_parse([]))
        monkeypatch.setattr(tempfile, "mkstemp", refuse)
        token_dir = tmp_path / "sso-cache"
        token_dir.mkdir()
        entry = token_dir / "token-key.json"
        entry.write_text("{}")
        entry.chmod(0o644)
        cache = SSOTokenProvider.DEFAULT_CACHE_CLS(
            str(token_dir), dumps_func=lambda _value: "dumped"
        )
        cache["token-key"] = {"accessToken": "t"}
        assert entry.read_text() == "dumped"
        if sys.platform != "win32":
            assert stat.S_IMODE(entry.stat().st_mode) == 0o644

    def test_the_sso_provider_is_built_with_that_token_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The provider builds its token cache once, when the chain is built,
        # so the class has to be pinned before that: pinned after, the chain's
        # provider kept the installed botocore's write (measured: the
        # unwritable-cache rc flips back, the entry's mode becomes 0600).
        from botocore.tokens import SSOTokenProvider

        # Back to botocore's own class first: an earlier test's session left
        # the pin in place, which would hide a pin made too late here.
        monkeypatch.setattr(SSOTokenProvider, "DEFAULT_CACHE_CLS", JSONFileCache)
        session = _session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        sso = session.get_component("credential_provider").get_provider("sso")
        token_cache = sso._token_provider._cache  # pyright: ignore[reportAttributeAccessIssue]
        assert type(token_cache) is awsclisession.credential_cache_class()

    def test_a_fetched_credential_lands_in_that_directory(self, tmp_path: Path) -> None:
        # The write half: whatever a provider caches becomes a file under the
        # shared directory, so the next invocation (and aws-cli) can read it.
        session = _session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        self._caches(session)[0]["written-key"] = {"AccessKeyId": "ASIAWRITTEN"}
        assert (self._cache_dir(tmp_path) / "written-key.json").read_text() == (
            '{"AccessKeyId": "ASIAWRITTEN"}'
        )

    def test_every_entry_point_gets_the_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `aws` emits `session-initialized` once per run, so no session may be
        # left with botocore's in-memory dict: the sessions every builder
        # opens assume the same role.
        from boto3_s3_cli.commands.base import Context

        opened: list[Any] = []
        real_opener = awsclisession.open_botocore_session

        def recording(settings: Any) -> Any:
            session = real_opener(settings)
            opened.append(session)
            return session

        monkeypatch.setattr(awsclisession, "open_botocore_session", recording)
        clientfactory.build_s3(_parse([]))
        Context(client_factory=lambda _args: object()).s3(_parse([]))
        assert len(opened) == 2
        for session in opened:
            for cache in self._caches(session):
                assert isinstance(cache, JSONFileCache)
                assert cache["planted-key"] == {"AccessKeyId": "ASIAPLANTED"}

    def test_an_unknown_profile_is_still_a_library_error(self) -> None:
        # Opening the session reads the profile (the format check, the cache
        # injection's provider chain), so an unknown profile raises there: the
        # session builder must still report it as the library's error, rc 255,
        # naming the profile - never as a raw botocore traceback.
        with pytest.raises(InvalidConfigError) as excinfo:
            _session(_parse(["--profile", "nosuchprofile"]))
        assert exit_code_for(excinfo.value) == 255
        assert "nosuchprofile" in str(excinfo.value)


def _provider_client(session: Any, service: str = "sts") -> Any:
    """A client built the way the credential providers build their own.

    botocore's assume-role / web-identity / SSO providers create their STS and
    SSO clients through ``session.create_client(...)`` with no region, no
    config and no verify - so whatever those clients resolve, they resolve off
    the session alone. That is the surface aws-cli configures at startup and this
    CLI configured per client, which is why these tests reach for it directly.
    """
    return session.create_client(service)


class _RequestStoppedError(Exception):
    """Raised from ``before-send`` once the signed request has been seen."""


def _authorization_sent(client: Any, bucket: str) -> str:
    """The ``Authorization`` header a PutObject is signed with, sent nowhere.

    ``before-send`` fires on the finished, signed request; raising there ends
    the call before any connection is made.
    """
    seen: list[str] = []

    def capture(request: Any, **_kwargs: Any) -> None:
        value = request.headers.get("Authorization", b"")
        seen.append(value.decode() if isinstance(value, bytes) else value)
        raise _RequestStoppedError

    client.meta.events.register("before-send.s3.PutObject", capture)
    with pytest.raises(_RequestStoppedError):
        client.put_object(Bucket=bucket, Key="k", Body=b"x", ServerSideEncryption="aws:kms")
    return seen[0]


class TestNoSignRequestPosture:
    """``--no-sign-request`` is the session's posture, ``--sse aws:kms`` included.

    aws-cli's startup handler writes UNSIGNED into the session default client
    config, so every client the session builds is anonymous - the credential
    chain's own STS / SSO clients too. Its s3 ClientFactory also names
    ``Config(signature_version='s3v4')`` under ``--sse aws:kms``, which beats
    the session's UNSIGNED on its botocore client alone: such a run signs,
    resolves credentials and fails without them on the classic lane while its
    CRT lane stays anonymous (measured on the pinned aws-cli). That pin is not
    copied - a ``--no-sign-request`` that signs and differs by engine looks
    like a bug, not a behavior to keep (docs/cli/aws-differences.md) - so
    every unsigned run here is anonymous on both engines.
    """

    _KMS_ARGV = ("./local.txt", "s3://bkt/key", "--region", "us-east-1", "--no-sign-request")

    @pytest.fixture
    def _broken_role_profile(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A default profile whose credentials cannot be built, and no env keys.

        Resolving credentials at all is then observable: the assume-role
        provider fails the whole build (rc 255, aws-cli's own report), where an
        anonymous client never asks.
        """
        config = tmp_path / "config"
        config.write_text(
            "[default]\n"
            "role_arn = arn:aws:iam::123456789012:role/r1\n"
            "source_profile = missingprofile\n"
            "region = us-east-1\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            monkeypatch.delenv(var, raising=False)

    def test_the_unsigned_signature_reaches_the_clients_botocore_builds_itself(self) -> None:
        # The placement, not just the value: aws-cli puts UNSIGNED in the session
        # default client config, so the credential chain's own STS / SSO
        # clients are unsigned too - a per-client Config never reached them.
        from botocore import UNSIGNED

        s3 = clientfactory.build_s3(_parse(["--region", "us-east-1", "--no-sign-request"]))
        assert s3.session is not None
        assert s3.client().meta.config.signature_version is UNSIGNED
        session = s3.session._session  # pyright: ignore[reportPrivateUsage]
        assert _provider_client(session).meta.config.signature_version is UNSIGNED

    @pytest.mark.parametrize(
        "extra",
        [
            [],
            ["--sse", "aws:kms"],
            ["--sse", "AES256"],
            ["--sse-c", "AES256"],
            ["--sse-kms-key-id", "somekey"],
        ],
        ids=["bare", "sse-kms", "sse-aes256", "sse-c", "sse-kms-key-id"],
    )
    def test_every_unsigned_run_stays_anonymous(
        self, _broken_role_profile: None, extra: list[str]
    ) -> None:
        # An anonymous client asks for no credentials at all, which is why the
        # broken profile is silent - aws-cli's --sse aws:kms pin included.
        from botocore import UNSIGNED

        s3 = clientfactory.build_s3(_parse_transfer([*self._KMS_ARGV, *extra]))
        assert s3.client().meta.config.signature_version is UNSIGNED

    @pytest.mark.parametrize(
        ("target", "bucket"),
        [
            (
                "s3://arn:aws:s3::123456789012:accesspoint/mfzwi23gnjvgw.mrap/k",
                "arn:aws:s3::123456789012:accesspoint/mfzwi23gnjvgw.mrap",
            ),
            (
                "s3://arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-01234567890123456/"
                "accesspoint/myap/k",
                "arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-01234567890123456/"
                "accesspoint/myap",
            ),
        ],
        ids=["mrap", "outposts"],
    )
    def test_a_signed_kms_run_signs_the_targets_own_scheme(
        self, monkeypatch: pytest.MonkeyPatch, target: str, bucket: str
    ) -> None:
        # With no signature pinned, an MRAP or Outposts target signs SigV4a
        # under --sse aws:kms as under any other run (aws-cli's s3v4 pin would
        # sign it plain SigV4, a request those endpoints reject).
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        args = _parse_transfer(["./local.txt", target, "--region", "us-west-2", "--sse", "aws:kms"])
        authorization = _authorization_sent(clientfactory.build_s3(args).client(), bucket)
        assert authorization.startswith("AWS4-ECDSA-P256-SHA256 Credential=AKIDEXAMPLE/")

    def test_the_path_resolver_clients_stay_anonymous_too(self) -> None:
        # aws-cli's `S3PathResolver.from_session` names no config, so its
        # s3control / sts clients take the session's UNSIGNED.
        from botocore import UNSIGNED

        args = _parse_transfer([*self._KMS_ARGV, "--sse", "aws:kms"])
        client = _service_client("sts", args, region="us-east-1")
        assert client.meta.config.signature_version is UNSIGNED

    def test_a_signed_run_resolves_credentials(self, _broken_role_profile: None) -> None:
        # Without --no-sign-request the client signs, so the broken profile
        # fails the build, --sse aws:kms or not.
        for extra in ([], ["--sse", "aws:kms"]):
            args = _parse_transfer(["./local.txt", "s3://bkt/key", "--region", "us-east-1", *extra])
            with pytest.raises(InvalidConfigError):
                clientfactory.build_s3(args).client()


class TestRegionSessionBinding:
    """The resolved region binds to the session, as aws-cli's driver binds it.

    aws-cli installs its region chain as the session's own ``region`` provider and
    binds a truthy ``--region`` at its head, so the clients *botocore* builds
    for itself - chiefly the credential chain's STS client - resolve the same
    region as the S3 client. Measured against the pinned aws-cli: with an
    assume-role profile carrying no ``region`` key, ``--region eu-west-1``
    (or ``AWS_REGION``) has aws-cli call ``sts.eu-west-1.amazonaws.com`` and exit
    0, where this CLI called the global endpoint - or, with no other region
    source at all, exited 253 with ``NoRegion`` before a single request.
    """

    @pytest.fixture(autouse=True)
    def _no_ambient_region(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    def _session_of(self, s3: Any) -> Any:
        return s3.session._session

    def test_the_region_flag_reaches_a_client_botocore_builds_itself(self) -> None:
        session = self._session_of(clientfactory.build_s3(_parse(["--region", "eu-west-1"])))
        assert session.get_config_variable("region") == "eu-west-1"
        assert _provider_client(session).meta.region_name == "eu-west-1"

    def test_aws_region_reaches_it_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Stock botocore's session region reads AWS_DEFAULT_REGION alone, so
        # the modern spelling only arrives through the CLI chain.
        monkeypatch.setenv("AWS_REGION", "eu-west-2")
        session = self._session_of(clientfactory.build_s3(_parse([])))
        assert _provider_client(session).meta.region_name == "eu-west-2"

    def test_the_flag_beats_a_profile_region_there_as_well(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The softer face of the same gap: with a profile region *and* a
        # differing --region, the two tools signed AssumeRole in different
        # regions - the wrong regional STS endpoint, and a scope mismatch for
        # an opt-in region.
        config = tmp_path / "config"
        config.write_text("[default]\nregion = ap-south-1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        session = self._session_of(clientfactory.build_s3(_parse(["--region", "eu-west-1"])))
        assert _provider_client(session).meta.region_name == "eu-west-1"

    def test_an_unresolved_region_binds_nothing(self) -> None:
        # aws-cli's truthy guard. Nothing resolved means the session keeps
        # botocore's own (identical) answer - and the NoRegion failure that
        # answer produces for s3control stays exactly where it was.
        s3 = clientfactory.build_s3(_parse([]))
        session = self._session_of(s3)
        assert session.instance_variables().get("region") is None
        assert session.get_config_variable("region") is None
        with pytest.raises(ConfigurationError) as excinfo:
            s3.service_client("s3control")
        assert exit_code_for(excinfo.value) == 253

    def test_an_empty_region_flag_binds_nothing_and_still_fails_the_build(self) -> None:
        # `--region ""` is falsy, so it never heads aws-cli's chain - and here the
        # rest of the chain answers nothing either, so nothing binds. The empty
        # string still reaches the client as itself and fails construction on
        # both tools (rc 255) before any credential is fetched.
        s3 = clientfactory.build_s3(_parse(["--region", ""]))
        assert self._session_of(s3).get_config_variable("region") is None
        with pytest.raises(InvalidConfigError, match="Invalid endpoint"):
            s3.client()

    @pytest.mark.parametrize(
        ("env", "profile_region", "expected"),
        [
            ({"AWS_REGION": "eu-west-1"}, None, "eu-west-1"),
            ({"AWS_DEFAULT_REGION": "eu-west-1"}, None, "eu-west-1"),
            ({}, "eu-west-1", "eu-west-1"),
        ],
        ids=["aws-region", "aws-default-region", "profile"],
    )
    def test_an_empty_region_flag_leaves_the_rest_of_the_chain_to_the_session(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        env: dict[str, str],
        profile_region: str | None,
        expected: str,
    ) -> None:
        # Only a truthy --region heads aws-cli's chain, so a falsy one drops out of
        # it entirely and the session takes the next source's answer. Measured:
        # `--region "" ` with AWS_REGION=eu-west-1 has aws-cli sign AssumeRole in
        # eu-west-1 (rc 255 at the S3 endpoint), where this CLI called STS not
        # at all and exited 253 with NoRegion. The client still gets the empty
        # string itself, which is the half aws-cli passes straight through.
        if profile_region is not None:
            config = tmp_path / "config"
            config.write_text(f"[default]\nregion = {profile_region}\n")
            monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        for var, value in env.items():
            monkeypatch.setenv(var, value)
        session = self._session_of(clientfactory.build_s3(_parse(["--region", ""])))
        assert session.get_config_variable("region") == expected
        assert _provider_client(session).meta.region_name == expected

    @pytest.mark.parametrize(
        "env",
        [
            {"AWS_REGION": ""},
            {"AWS_REGION": "", "AWS_DEFAULT_REGION": "eu-west-1"},
        ],
        ids=["alone", "beats-aws-default-region"],
    )
    def test_a_present_but_empty_aws_region_is_the_answer_that_binds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
    ) -> None:
        # The env links are present-wins, so `AWS_REGION=` *is* the chain's
        # answer - and aws-cli binds it, signing with an empty region scope.
        # Measured with an assume-role profile: aws-cli called STS once, scoped
        # `<date>//sts/aws4_request`, and reached the S3 endpoint (rc 255),
        # where this CLI walked on to AWS_DEFAULT_REGION and the profile - or,
        # with neither set, exited 253 with NoRegion before any request.
        config = tmp_path / "config"
        config.write_text("[default]\nregion = ap-south-1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        for var, value in env.items():
            monkeypatch.setenv(var, value)
        session = self._session_of(clientfactory.build_s3(_parse([])))
        assert session.get_config_variable("region") == ""

    def test_a_client_built_for_another_region_does_not_rebind_the_session(self) -> None:
        # `--source-region`'s client is built from the run's session with its
        # own region; aws-cli's source client takes that region as `region_name`
        # alone, so its credential chain still signs `AssumeRole` in the
        # run's region. Rebinding the shared session put STS in the source
        # region - and with no other region source, made a run aws-cli fails
        # (`You must specify a region.`, rc 1) succeed (measured).
        s3 = clientfactory.build_s3(_parse(["--region", "eu-west-1"]))
        source_client = s3.source_client("us-west-2")
        session = self._session_of(s3)
        assert source_client.meta.region_name == "us-west-2"
        assert session.get_config_variable("region") == "eu-west-1"
        assert _provider_client(session).meta.region_name == "eu-west-1"

    def test_a_source_region_alone_binds_nothing(self) -> None:
        s3 = clientfactory.build_s3(_parse([]))
        s3.source_client("us-west-2")
        assert self._session_of(s3).get_config_variable("region") is None

    def test_the_region_chain_is_still_walked_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Binding must not cost a second walk: the chain's last link is the
        # IMDS probe (seconds on a host with no region and no metadata
        # service), which is why the answer is threaded rather than re-asked.
        real_resolve = awsclisession.resolve_region
        walks: list[str | None] = []

        def counting_resolve(explicit: str | None, session: Any) -> Any:
            resolved = real_resolve(explicit, session)
            walks.append(resolved)
            return resolved

        monkeypatch.setenv("AWS_REGION", "eu-west-2")
        monkeypatch.setattr(awsclisession, "resolve_region", counting_resolve)
        s3 = clientfactory.build_s3(_parse([]))
        s3.client()
        s3.client()
        assert walks == ["eu-west-2"]


class TestSessionRetryPosture:
    """aws-cli v2's retry defaults belong to the session, not to one client.

    Its bundled botocore declares ``retry_mode = standard`` / ``max_attempts =
    3`` as session defaults, so every client inherits them - the STS and SSO
    clients the credential chain builds for itself included. Measured against
    the pinned aws-cli: a failing ``AssumeRole`` is attempted 3 times and the
    report ends ``(reached max retries: 2)``, where this CLI attempted it 5
    times and ended ``(reached max retries: 4)``.
    """

    def test_a_provider_client_inherits_the_aws_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("AWS_RETRY_MODE", "AWS_MAX_ATTEMPTS"):
            monkeypatch.delenv(var, raising=False)
        session = _session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        assert session.get_config_variable("retry_mode") == "standard"
        assert session.get_config_variable("max_attempts") == 3
        assert _provider_client(session).meta.config.retries == {
            "mode": "standard",
            "total_max_attempts": 3,
        }

    def test_the_user_s_overrides_still_win_there(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_RETRY_MODE", "adaptive")
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "7")
        session = _session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        assert _provider_client(session).meta.config.retries == {
            "mode": "adaptive",
            "total_max_attempts": 7,
        }

    def test_the_cli_timeouts_reach_a_provider_client(self) -> None:
        # aws-cli's globalargs writes both timeouts into the session default client
        # config, so a stalled STS aborts on the flag's budget: measured,
        # `--cli-read-timeout 2` against an STS stalling 8s is rc 255 and a
        # read timeout on `aws`, where this CLI ran to botocore's default and
        # reported the server's own error at rc 254.
        args = _parse(["--cli-read-timeout", "2", "--cli-connect-timeout", "3"])
        session = _session(args)._session  # pyright: ignore[reportPrivateUsage]
        client = _provider_client(session)
        assert client.meta.config.read_timeout == 2
        assert client.meta.config.connect_timeout == 3

    def test_the_default_timeouts_are_aws_s_own(self) -> None:
        # aws-cli defaults both to botocore's 60s rather than leaving them unset.
        session = _session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        default = session.get_default_client_config()
        assert (default.connect_timeout, default.read_timeout) == (60, 60)

    def test_a_zero_timeout_reaches_it_as_no_timeout(self) -> None:
        session = _session(_parse(["--cli-read-timeout", "0"]))._session  # pyright: ignore[reportPrivateUsage]
        assert _provider_client(session).meta.config.read_timeout is None


class TestUsEast1RegionalEndpointIsIgnored:
    """``us_east_1_regional_endpoint = legacy`` does not move us-east-1.

    aws-cli v2's bundled botocore dropped the key from its ``[s3]`` table (us-east-1
    is regional there, always), so ``legacy`` from
    ``AWS_S3_US_EAST_1_REGIONAL_ENDPOINT`` or the config key leaves aws-cli on the
    regional endpoint, where the installed botocore would send the request to
    the global one.
    """

    def test_an_env_value_reaches_no_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_S3_US_EAST_1_REGIONAL_ENDPOINT", "legacy")
        args = _parse(["--region", "us-east-1"])
        client = _client(args)
        assert client.meta.config.s3["us_east_1_regional_endpoint"] == "regional"
        assert client.meta.endpoint_url == "https://s3.us-east-1.amazonaws.com"

    def test_a_config_value_reaches_no_client_either(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = tmp_path / "config"
        config.write_text("[default]\ns3 =\n  us_east_1_regional_endpoint = legacy\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        args = _parse(["--region", "us-east-1"])
        client = _client(args)
        assert client.meta.config.s3["us_east_1_regional_endpoint"] == "regional"
        # The pin is what keeps us-east-1 on the regional endpoint aws-cli v2 uses;
        # a dropped key would send it to the legacy global one.
        assert client.meta.endpoint_url == "https://s3.us-east-1.amazonaws.com"


class TestDefaultsMode:
    """A ``defaults_mode`` must not take the CLI down, whatever it is set to.

    The installed botocore implements defaults modes; aws-cli v2's bundled botocore
    has no such setting and runs as if it were unset. Any valid mode other than
    ``legacy`` sends botocore's smart-defaults machinery at the session's
    ``[s3]`` section provider - the one this CLI wraps to pin
    ``us_east_1_regional_endpoint`` - with ``set_default_provider``. Measured:
    every valid mode, config key and ``AWS_DEFAULTS_MODE`` alike, failed
    *every* subcommand at rc 255 with ``'_RegionalS3Section' object has no
    attribute 'set_default_provider'`` where `aws` simply ran (``ls``,
    ``presign``, ``cp``, ``rm``, ``sync``).
    """

    _MODES = ("standard", "in-region", "cross-region", "mobile", "auto")

    @pytest.fixture(autouse=True)
    def _no_imds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # `auto` resolves its mode from the IMDS region when nothing else says
        # so; a host with no metadata service answering would stall the probe.
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")

    @pytest.mark.parametrize("mode", _MODES)
    @pytest.mark.parametrize("source", ["config", "env"])
    def test_every_valid_mode_builds_a_client_with_the_pin_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, source: str
    ) -> None:
        config = tmp_path / "config"
        if source == "config":
            config.write_text(f"[default]\nregion = us-east-1\ndefaults_mode = {mode}\n")
        else:
            config.write_text("[default]\nregion = us-east-1\n")
            monkeypatch.setenv("AWS_DEFAULTS_MODE", mode)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        args = _parse(["--region", "us-east-1"])
        session = _session(args)._session  # pyright: ignore[reportPrivateUsage]
        # The mode really arrives - otherwise the build below proves nothing.
        assert session.get_config_variable("defaults_mode") == mode
        client = _client(args)
        # The smart defaults want `regional` for this key too, so the pin's
        # value survives the merge - as does the endpoint it decides.
        assert client.meta.config.s3["us_east_1_regional_endpoint"] == "regional"
        assert client.meta.endpoint_url == "https://s3.us-east-1.amazonaws.com"
        # And aws-cli v2's retry posture is still the session's.
        assert client.meta.config.retries == {"total_max_attempts": 3, "mode": "standard"}
        _service_client("sts", args, region="us-east-1")

    def test_a_smart_default_for_another_key_reaches_the_section(self) -> None:
        # The wrapper delegates rather than intercepts: a key the pin does not
        # own is botocore's to keep, so a future mode that vends one is not
        # silently dropped.
        from botocore.configprovider import ConstantProvider

        session = _session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        provider = session.get_component("config_store").get_config_provider("s3")
        provider.set_default_provider("addressing_style", ConstantProvider("path"))
        section = provider.provide()
        assert section["addressing_style"] == "path"
        assert section["us_east_1_regional_endpoint"] == "regional"


class TestS3ErrorMsgRegistration:
    def test_every_built_client_rewrites_the_three_s3_messages(self) -> None:
        # aws-cli hangs its s3errormsg handler off `after-call.s3` for the whole
        # session, so the rewrite reaches every operation; emitting the event
        # the way botocore does pins both the key and the wiring.
        client = _client(_parse(["--region", "us-east-1"]))
        parsed: dict[str, Any] = {
            "Error": {
                "Code": "PermanentRedirect",
                "Message": "Please send all future requests to this endpoint.",
                "Endpoint": "bkt.s3.eu-west-1.amazonaws.com",
            }
        }
        client.meta.events.emit(
            "after-call.s3.ListObjectsV2",
            http_response=None,
            parsed=parsed,
            model=None,
            context={},
        )
        assert parsed["Error"]["Message"] == (
            "Please send all future requests to this endpoint: "
            "bkt.s3.eu-west-1.amazonaws.com\n" + s3errormsg.REGION_ERROR_MSG
        )


class TestContextSeams:
    def test_a_service_client_factory_alone_is_used(self) -> None:
        # Without a client_factory the S3 clients stay the command's own, the
        # rewriter included, and only the non-S3 clients are injected.
        from boto3_s3_cli.commands.base import Context

        injected = object()
        asked: list[tuple[str, str | None]] = []

        def service_factory(service: str, _args: Any, *, region: str | None = None) -> Any:
            asked.append((service, region))
            return injected

        s3 = Context(service_client_factory=service_factory).s3(_parse(["--region", "us-east-1"]))
        assert s3.service_client("sts") is injected
        assert s3.service_client("s3control", region="eu-west-1") is injected
        assert asked == [("sts", None), ("s3control", "eu-west-1")]
        parsed: dict[str, Any] = {
            "Error": {"Code": "PermanentRedirect", "Message": "m", "Endpoint": "e"}
        }
        s3.client().meta.events.emit(
            "after-call.s3.ListObjectsV2",
            http_response=None,
            parsed=parsed,
            model=None,
            context={},
        )
        assert parsed["Error"]["Message"].endswith(s3errormsg.REGION_ERROR_MSG)


class TestChecksumDefaultRegistration:
    def test_every_built_client_names_aws_default_where_botocore_would(self) -> None:
        # aws-cli's bundled botocore stamps CRC64NVME on a DeleteObjects that names
        # no algorithm; pip botocore would stamp CRC32. The built client carries
        # the assembly's stamp (awsclisession.register_default_checksum) at
        # provide-client-params, ahead of botocore's own resolution - or
        # nothing, where botocore cannot compute CRC64NVME, so botocore's
        # default stands.
        from botocore import httpchecksum

        client = _client(_parse(["--region", "us-east-1"]))
        params: dict[str, Any] = {"Bucket": "b", "Delete": {"Objects": [{"Key": "k"}]}}
        client.meta.events.emit(
            "provide-client-params.s3.DeleteObjects",
            params=params,
            model=client.meta.service_model.operation_model("DeleteObjects"),
            context={},
        )
        can_compute = "crc64nvme" in getattr(httpchecksum, "_SUPPORTED_CHECKSUM_ALGORITHMS", ())
        assert params.get("ChecksumAlgorithm") == ("CRC64NVME" if can_compute else None)


class TestBuildServiceClient:
    def test_regionless_s3control_raises_no_region_keeping_the_cause(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # s3control has no global-endpoint fallback - unlike s3 and sts, which
        # is why this is the only region-less failure the mapped `aws s3`
        # surface can reach, via mv's --validate-same-s3-paths. The plain
        # ConfigurationError (not the InvalidConfigError refinement) is what
        # carries the rc, with botocore's exception kept as its cause.
        for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
        # No region left anywhere, so botocore would otherwise probe the EC2
        # metadata service (the last link of aws-cli's region chain).
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        with pytest.raises(ConfigurationError) as excinfo:
            _service_client("s3control", _parse([]))
        assert type(excinfo.value) is ConfigurationError
        assert isinstance(excinfo.value.__cause__, NoRegionError)
        assert exit_code_for(excinfo.value) == 253

    def test_timeouts_and_unsigned_are_inherited_like_from_session(self) -> None:
        # aws-cli threads --cli-read-timeout / --cli-connect-timeout and
        # --no-sign-request through the session default client config at startup,
        # so from_session's s3control/sts clients inherit them. service_client
        # folds the same into the client Config.
        from botocore import UNSIGNED

        client = _service_client(
            "sts",
            _parse(
                [
                    "--region",
                    "us-east-1",
                    "--no-sign-request",
                    "--cli-read-timeout",
                    "5",
                    "--cli-connect-timeout",
                    "7",
                ]
            ),
            region="us-east-1",
        )
        assert client.meta.config.signature_version is UNSIGNED
        assert client.meta.config.read_timeout == 5
        assert client.meta.config.connect_timeout == 7

    def test_zero_timeout_means_no_timeout(self) -> None:
        # The 0 -> None ("no timeout") sentinel carries over, exactly as in
        # the S3 client (botocore rejects a literal 0).
        client = _service_client(
            "sts",
            _parse(["--region", "us-east-1", "--cli-read-timeout", "0"]),
            region="us-east-1",
        )
        assert client.meta.config.read_timeout is None

    def test_noninteger_timeout_maps_to_255_not_a_parse_error(self) -> None:
        # A non-integer timeout surfaces as InvalidValueError (rc 255) here too,
        # matching the S3 client rather than crashing client creation.
        with pytest.raises(InvalidValueError) as excinfo:
            _service_client(
                "sts",
                _parse(["--region", "us-east-1", "--cli-connect-timeout", "abc"]),
                region="us-east-1",
            )
        assert exit_code_for(excinfo.value) == 255

    def test_none_region_falls_back_to_the_region_global(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli binds --region into the session at startup, so from_session's
        # region-less sts client (and the source s3control without
        # --source-region) still lands in --region.
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        client = _service_client("sts", _parse(["--region", "eu-central-1"]), region=None)
        assert client.meta.region_name == "eu-central-1"

    def test_none_region_under_an_empty_flag_is_the_sessions_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `--region ""` never binds the session, so aws-cli's region-less clients
        # resolve the rest of its chain: measured, `mv` from an access-point
        # alias with `--validate-same-s3-paths --region ""` and AWS_REGION set
        # resolved and moved (rc 0), where taking the flag itself built them
        # with an empty region (rc 255).
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        args = _parse(["--region", ""])
        client = _service_client("sts", args, region=None)
        assert client.meta.region_name == "eu-west-1"


def _client_verify(client: Any) -> Any:
    """The TLS trust source a built botocore client will actually use.

    botocore records the resolved ``verify`` on the endpoint's HTTP session
    (``True`` is its own default bundle, opened per request).
    """
    return client._endpoint.http_session._verify


def _crt_verify(s3: Any) -> Any:
    """The trust source `build_s3` declares for the CRT client."""
    return s3._crt.crt.verify


def _crt_default_bundle() -> str:
    """The CA file aws-cli's CRT factory falls back to: botocore's bundled one."""
    from botocore.httpsession import DEFAULT_CA_BUNDLE

    return DEFAULT_CA_BUNDLE


class TestMalformedS3Section:
    """A profile `s3` value that is not a section fails the client build.

    `s3 =` with no nested keys survives botocore's section provider as the
    string "" (the provider's truthy guard discards every other non-dict);
    a truthy scalar like `s3 = foo` is discarded there but still sits in the
    raw scoped config botocore's endpoint resolution reads. Either way the
    build fails there (aws-cli fails it too, rc 255) rather than running on
    defaults the profile never asked for.
    """

    def _write_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> None:
        config_file = tmp_path / "config"
        config_file.write_text(content)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))

    def test_empty_s3_section_fails_the_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write_config(tmp_path, monkeypatch, "[default]\ns3 =\n")
        with pytest.raises(InvalidConfigError):
            _client(_parse([]))

    def test_truthy_junk_s3_section_fails_the_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The provider discards this value, but the raw read still returns
        # the string.
        self._write_config(tmp_path, monkeypatch, "[default]\ns3 = foo\n")
        with pytest.raises(InvalidConfigError):
            _client(_parse([]))


_EMPTY_CA_BUNDLE_REPORT = (
    "Invalid CA bundle: the configured value (ca_bundle, AWS_CA_BUNDLE, "
    "REQUESTS_CA_BUNDLE, or verify) resolved to an empty or whitespace-only "
    "string. Provide a valid path to a CA bundle file."
)


class TestVerifyResolution:
    """The botocore clients' TLS trust source: aws-cli's ``verify_ssl``.

    ``--no-verify-ssl`` and ``--ca-bundle`` reach every client as ``verify``,
    and botocore resolves the rest of the chain itself, as it does for
    aws-cli's ``ClientFactory``. The CRT client's is `TestCrtTrustSource`.
    """

    @pytest.fixture(autouse=True)
    def _no_host_ca_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The moto isolation fixture does not scrub these; a developer's own
        # AWS_CA_BUNDLE would otherwise decide the "nothing set" cases.
        monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    def test_no_verify_ssl_disables_verification(self) -> None:
        assert _client_verify(_client(_parse(["--no-verify-ssl"]))) is False

    def test_no_verify_ssl_beats_a_ca_bundle(self, tmp_path: Path) -> None:
        # aws-cli's resolve_verify_ssl only looks at --ca-bundle when
        # --no-verify-ssl is absent.
        bundle = tmp_path / "ca.pem"
        bundle.write_text("")
        argv = ["--no-verify-ssl", "--ca-bundle", str(bundle)]
        assert _client_verify(_client(_parse(argv))) is False

    def test_empty_ca_bundle_flag_is_rejected(self) -> None:
        # `--ca-bundle=` is present-empty, not unset: it stops the chain
        # instead of falling through to the env or the certifi default (a
        # truthy-`or` rewrite of the chain - the b935265 / 51e7831
        # empty-string family - would resolve the default CA here). The empty
        # value it lands on is then refused at client build, with botocore's
        # own wording, rather than read as "verification off" - measured on
        # the pinned aws-cli, which reports exactly this at rc 255.
        with pytest.raises(InvalidConfigError) as excinfo:
            _client(_parse(["--ca-bundle", ""]))
        assert str(excinfo.value) == _EMPTY_CA_BUNDLE_REPORT
        assert exit_code_for(excinfo.value) == 255

    def test_whitespace_only_ca_bundle_flag_is_rejected(self) -> None:
        # aws-cli refuses a whitespace-only value on the same check rather than
        # attempting it as a path (measured); the report is the same one.
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle"):
            _client(_parse(["--ca-bundle", "  "]))

    def test_empty_aws_ca_bundle_env_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_CA_BUNDLE", "")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "req.pem"))
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle"):
            _client(_parse([]))

    def test_empty_requests_ca_bundle_env_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "")
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle"):
            _client(_parse([]))

    @pytest.mark.parametrize("var", ["AWS_CA_BUNDLE", "REQUESTS_CA_BUNDLE"])
    @pytest.mark.parametrize("flag", [["--no-verify-ssl"], ["--ca-bundle", "ca.pem"]])
    def test_a_client_the_sdk_builds_is_held_to_the_check_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str, flag: list[str]
    ) -> None:
        # The flag sets the S3 client's own trust, but the clients the SDK
        # builds from the session - the credential chain's STS / SSO, the CRT
        # serializer - read the session's empty value and took it as
        # "verification off". aws-cli checks every client it builds: measured, an
        # assume-role profile with `AWS_CA_BUNDLE=` beside either flag is rc
        # 255 there, where this fetched credentials over unverified TLS.
        monkeypatch.chdir(tmp_path)
        (tmp_path / "ca.pem").write_text("")
        monkeypatch.setenv(var, "")
        s3 = clientfactory.build_s3(_parse(flag))
        s3.client()
        with pytest.raises(BotocoreInvalidConfigError) as excinfo:
            _provider_client(s3.session._session)  # pyright: ignore[reportOptionalMemberAccess, reportPrivateUsage]
        assert str(excinfo.value) == _EMPTY_CA_BUNDLE_REPORT

    def test_an_assumed_role_is_fetched_through_the_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The fetch builds its STS client when the credentials are first
        # used, through the session. An endpoint nothing listens on keeps an
        # unchecked client from reaching anything.
        config = tmp_path / "config"
        config.write_text(
            "[profile ar]\nrole_arn = arn:aws:iam::123456789012:role/r\n"
            "source_profile = src\nregion = us-east-1\n"
            "[profile src]\naws_access_key_id = AKID\naws_secret_access_key = s\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        monkeypatch.setenv("AWS_ENDPOINT_URL_STS", "http://127.0.0.1:9")
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
        monkeypatch.setenv("AWS_CA_BUNDLE", "")
        s3 = clientfactory.build_s3(_parse(["--profile", "ar", "--no-verify-ssl"]))
        credentials = s3.session.get_credentials()  # pyright: ignore[reportOptionalMemberAccess]
        assert credentials is not None
        with pytest.raises(BotocoreInvalidConfigError) as excinfo:
            credentials.get_frozen_credentials()
        assert str(excinfo.value) == _EMPTY_CA_BUNDLE_REPORT

    def test_the_sso_provider_builds_its_clients_through_the_check(self) -> None:
        # The SSO provider keeps the `create_client` it was handed when the
        # chain was built, so the check has to be on the session first: put
        # on after, legacy SSO and an unexpired `sso-session` fetched their
        # credentials unchecked (measured: rc 0 where `aws` is 255).
        session = _session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        sso = session.get_component("credential_provider").get_provider("sso")
        assert sso._client_creator is session.create_client  # pyright: ignore[reportAttributeAccessIssue]

    def test_a_client_the_sdk_builds_with_a_usable_bundle_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = tmp_path / "ca.pem"
        bundle.write_text("")
        monkeypatch.setenv("AWS_CA_BUNDLE", str(bundle))
        s3 = clientfactory.build_s3(_parse([]))
        client = _provider_client(s3.session._session)  # pyright: ignore[reportOptionalMemberAccess, reportPrivateUsage]
        assert _client_verify(client) == str(bundle)

    def test_no_verify_ssl_beats_an_empty_ca_bundle(self) -> None:
        # The rejection reads the *resolved* value, and `--no-verify-ssl`
        # resolves to False before the chain ever reads the flag - so the pair
        # disables verification instead of failing (measured).
        argv = ["--no-verify-ssl", "--ca-bundle", ""]
        assert _client_verify(_client(_parse(argv))) is False

    def test_ca_bundle_flag_beats_the_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = tmp_path / "flag.pem"
        bundle.write_text("")
        monkeypatch.setenv("AWS_CA_BUNDLE", str(tmp_path / "env.pem"))
        client = _client(_parse(["--ca-bundle", str(bundle)]))
        assert _client_verify(client) == str(bundle)

    def test_aws_ca_bundle_env_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AWS_CA_BUNDLE reaches botocore as the `ca_bundle` config variable,
        # which botocore folds into the client at construction.
        bundle = tmp_path / "env.pem"
        bundle.write_text("")
        monkeypatch.setenv("AWS_CA_BUNDLE", str(bundle))
        assert _client_verify(_client(_parse([]))) == str(bundle)

    def test_profile_ca_bundle_key_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Read off the session the client is built from, so the *resolved*
        # profile decides (a session-less read would take the default one).
        bundle = tmp_path / "profile.pem"
        bundle.write_text("")
        config_file = tmp_path / "config"
        config_file.write_text(f"[default]\n[profile tls]\nca_bundle = {bundle}\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        assert _client_verify(_client(_parse([]))) is True
        client = _client(_parse(["--profile", "tls"]))
        assert _client_verify(client) == str(bundle)

    def test_requests_ca_bundle_env_still_applies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # botocore's own last fallback before its default bundle
        # (EndpointCreator._get_verify_value), which aws-cli's botocore
        # clients honor too.
        bundle = tmp_path / "requests.pem"
        bundle.write_text("")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
        assert _client_verify(_client(_parse([]))) == str(bundle)

    def test_aws_ca_bundle_beats_requests_ca_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_CA_BUNDLE", str(tmp_path / "aws.pem"))
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "requests.pem"))
        client = _client(_parse([]))
        assert _client_verify(client) == str(tmp_path / "aws.pem")

    def test_nothing_set_leaves_botocore_its_default(self) -> None:
        # aws-cli passes verify=None, so botocore opens its default bundle
        # (certifi's, or its own where certifi is absent) per request.
        assert _client_verify(_client(_parse([]))) is True

    def test_no_verify_ssl_reaches_the_service_clients_too(self) -> None:
        client = _service_client("sts", _parse(["--no-verify-ssl"]), region="us-east-1")
        assert _client_verify(client) is False


class TestCrtTrustSource:
    """The CRT client's TLS trust source: aws-cli's CRT factory ``_resolve_verify``.

    aws-cli resolves it apart from its botocore clients: ``--no-verify-ssl``,
    ``--ca-bundle``, the ``ca_bundle`` config variable, then botocore's bundled
    ``cacert.pem`` - never ``REQUESTS_CA_BUNDLE``. `build_s3` declares it as
    ``CrtClientOptions.verify``. Where the CRT client is built,
    ``create_s3_crt_client`` is stubbed (the CRT cannot be exercised
    in-process); the assertion is on the kwargs it receives, following
    tests/lib/test_crtsupport.py.
    """

    @pytest.fixture(autouse=True)
    def _no_host_ca_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    def test_nothing_set_is_botocore_s_bundled_file(self) -> None:
        verify = _crt_verify(clientfactory.build_s3(_parse([])))
        assert verify == _crt_default_bundle()
        assert Path(verify).is_file()

    def test_no_verify_ssl_disables_verification(self, tmp_path: Path) -> None:
        argv = ["--no-verify-ssl", "--ca-bundle", str(tmp_path / "ca.pem")]
        assert _crt_verify(clientfactory.build_s3(_parse(argv))) is False

    def test_ca_bundle_flag_beats_the_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_CA_BUNDLE", str(tmp_path / "env.pem"))
        argv = ["--ca-bundle", str(tmp_path / "flag.pem")]
        assert _crt_verify(clientfactory.build_s3(_parse(argv))) == str(tmp_path / "flag.pem")

    def test_aws_ca_bundle_env_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_CA_BUNDLE", str(tmp_path / "env.pem"))
        assert _crt_verify(clientfactory.build_s3(_parse([]))) == str(tmp_path / "env.pem")

    def test_profile_ca_bundle_key_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Read off the session the clients are built from, so the resolved
        # profile decides.
        bundle = tmp_path / "profile.pem"
        config_file = tmp_path / "config"
        config_file.write_text(f"[default]\n[profile tls]\nca_bundle = {bundle}\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        assert _crt_verify(clientfactory.build_s3(_parse(["--profile", "tls"]))) == str(bundle)

    def test_requests_ca_bundle_is_not_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli's split: its botocore clients honor REQUESTS_CA_BUNDLE and
        # its CRT transfers ignore it (measured on the pinned aws-cli: a
        # bundle the CRT cannot read leaves cp / rm / sync running).
        bundle = tmp_path / "requests.pem"
        bundle.write_text("")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
        s3 = clientfactory.build_s3(_parse([]))
        assert _crt_verify(s3) == _crt_default_bundle()
        assert _client_verify(s3.client()) == str(bundle)

    @pytest.mark.parametrize("value", ["", "  "], ids=["empty", "whitespace"])
    def test_an_empty_ca_bundle_config_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        # Never read as "verification off", with aws-cli's report and rc.
        monkeypatch.setenv("AWS_CA_BUNDLE", value)
        with pytest.raises(InvalidConfigError) as excinfo:
            clientfactory.build_s3(_parse([]))
        assert str(excinfo.value) == _EMPTY_CA_BUNDLE_REPORT
        assert exit_code_for(excinfo.value) == 255

    def _stub_crt(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        s3transfer_crt = pytest.importorskip("s3transfer.crt")
        create_kwargs: list[dict[str, Any]] = []

        def create_client(**kwargs: Any) -> Any:
            create_kwargs.append(kwargs)
            return object()

        class Serializer:
            def __init__(self, session: Any, client_kwargs: dict[str, Any]) -> None: ...

        class Manager:
            def __init__(self, **kwargs: Any) -> None: ...

        # The real BotocoreCRTCredentialsWrapper stays in place: the process
        # lock, the CRT client and the transfer manager are the parts that
        # cannot run in-process, and the singleton's identity check calls the
        # wrapper.
        monkeypatch.setattr(s3transfer_crt, "acquire_crt_s3_process_lock", lambda _name: object())
        monkeypatch.setattr(s3transfer_crt, "create_s3_crt_client", create_client)
        monkeypatch.setattr(s3transfer_crt, "BotocoreCRTRequestSerializer", Serializer)
        monkeypatch.setattr(s3transfer_crt, "CRTTransferManager", Manager)
        return create_kwargs

    @pytest.fixture(autouse=True)
    def _reset_crt_singleton(self) -> Any:
        from boto3_s3 import crtsupport

        crtsupport._reset_for_tests()
        yield
        crtsupport._reset_for_tests()

    @pytest.mark.parametrize(
        ("argv", "requests_bundle"),
        [([], None), ([], "requests.pem"), (["--ca-bundle", "ca.pem"], None)],
        ids=["nothing-set", "requests-ca-bundle", "ca-bundle-flag"],
    )
    def test_the_declared_value_reaches_the_crt_client(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
        requests_bundle: str | None,
    ) -> None:
        from boto3_s3 import crtsupport

        monkeypatch.chdir(tmp_path)
        (tmp_path / "ca.pem").write_text("")
        if requests_bundle is not None:
            (tmp_path / requests_bundle).write_text("")
            monkeypatch.setenv("REQUESTS_CA_BUNDLE", requests_bundle)
        create_kwargs = self._stub_crt(monkeypatch)
        s3 = clientfactory.build_s3(_parse(argv))
        client = s3.client()
        engine_arguments = s3._crt.engine_arguments(client)  # pyright: ignore[reportPrivateUsage]
        assert crtsupport.create_crt_transfer_manager(client, None, **engine_arguments) is not None
        [kwargs] = create_kwargs
        assert kwargs["verify"] == ("ca.pem" if argv else _crt_default_bundle())

    def test_a_plain_library_client_still_defers_to_the_platform_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The library lane stays boto3-faithful: boto3 passes no verify to the
        # CRT either, so a client built outside the CLI keeps mapping to None.
        import boto3

        from boto3_s3 import crtsupport

        create_kwargs = self._stub_crt(monkeypatch)
        client = boto3.client("s3", region_name="us-east-1")
        assert crtsupport.create_crt_transfer_manager(client, None) is not None
        [kwargs] = create_kwargs
        assert kwargs["verify"] is None
