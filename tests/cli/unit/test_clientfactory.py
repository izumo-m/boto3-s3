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
from botocore.exceptions import NoRegionError
from botocore.utils import JSONFileCache

from boto3_s3 import (
    Boto3S3Error,
    ConfigurationError,
    InvalidConfigError,
    InvalidValueError,
    ValidationError,
)
from boto3_s3_cli import clientfactory, globalargs, s3errormsg
from boto3_s3_cli.cli import exit_code_for
from boto3_s3_cli.commands import transferargs


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
        real_build_session = clientfactory.build_session

        def counting_build_session(namespace: argparse.Namespace) -> Any:
            session = real_build_session(namespace)
            sessions.append(session)
            return session

        monkeypatch.setattr(clientfactory, "build_session", counting_build_session)
        s3 = clientfactory.build_s3(args)

        assert s3.session is not None
        assert s3.session.profile_name == "bound"
        assert s3.client().meta.endpoint_url == "http://localhost:9000"
        assert s3.aws_config().get_str("s3.multipart_threshold") == "17"
        # "One session": the client and the config read above both came off the
        # single session build_s3 made - nothing built a second one.
        assert sessions == [s3.session]
        # The S3-level endpoint copy feeds the CRT lane's explicit-endpoint
        # pin (design/crt.md); it must be the same string build_client applies,
        # so the Transferrer's meta-equality gate recognizes the CLI client.
        assert s3._endpoint_url == "http://localhost:9000"  # pyright: ignore[reportPrivateUsage]

    def test_build_s3_takes_aws_clis_posture_on_absent_credentials(self) -> None:
        # aws-cli hands its CRT client a credentials delegate built from
        # whatever the session resolved - None included - so a credential-less
        # CRT upload fails inside the delegate rather than dropping to classic.
        # The library keeps boto3's opposite default; the CLI opts in
        # (design/crt.md section 4).
        s3 = clientfactory.build_s3(_parse([]))
        assert s3._crt_allow_absent_credentials is True  # pyright: ignore[reportPrivateUsage]

    def test_build_s3_takes_aws_clis_posture_on_lock_contention(self) -> None:
        # aws-cli's factory acquires the cross-process CRT lock best-effort
        # and, under an explicit 'crt' preference, builds the CRT client
        # regardless - so construction-time failures surface under contention
        # too, where the library default would silently run classic and, on a
        # dryrun, report success (design/crt.md section 6).
        s3 = clientfactory.build_s3(_parse([]))
        assert s3._crt_allow_lockless is True  # pyright: ignore[reportPrivateUsage]

    @pytest.mark.parametrize(
        ("argv", "env", "expected"),
        [
            ([], {"AWS_REGION": "eu-west-1"}, "eu-west-1"),
            (["--region", "ap-northeast-1"], {"AWS_REGION": "eu-west-1"}, "ap-northeast-1"),
            ([], {}, None),
        ],
        ids=["env", "explicit-wins", "nothing-resolves"],
    )
    def test_build_s3_declares_aws_clis_resolved_region_for_the_crt_engine(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
        env: dict[str, str],
        expected: str | None,
    ) -> None:
        # aws-cli resolves the CRT region from its own region chain, so an
        # environment that resolves nothing declares None - where botocore
        # hands the *client* the `aws-global` pseudo-region and the CRT would
        # never see the absence (design/crt.md section 6).
        for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        for var, value in env.items():
            monkeypatch.setenv(var, value)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        s3 = clientfactory.build_s3(_parse(argv))
        assert s3._crt_region == expected  # pyright: ignore[reportPrivateUsage]
        if expected is None:
            # The contrast that makes the declaration necessary at all.
            assert s3.client().meta.region_name == "aws-global"

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
        real_resolve = clientfactory._resolve_region  # pyright: ignore[reportPrivateUsage]
        walks: list[str | None] = []

        def counting_resolve(explicit: str | None, session: Any) -> Any:
            resolved = real_resolve(explicit, session)
            walks.append(resolved)
            return resolved

        monkeypatch.setattr(clientfactory, "_resolve_region", counting_resolve)
        s3 = clientfactory.build_s3(_parse([]))
        first, second = s3.client(), s3.client()
        assert walks == [None]  # the one walk, and it resolved nothing
        # Threading it changes no outcome: both clients land where the chain
        # said, which for an unresolved region is botocore's `aws-global`.
        assert first.meta.region_name == second.meta.region_name == "aws-global"

    def test_an_explicitly_threaded_region_reaches_the_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The threading seam itself. A supplied region wins; a supplied None -
        # the chain's own answer when nothing resolves, which no `args.region`
        # can express - must land exactly where the unthreaded call lands,
        # never send the client back through the chain.
        args = _parse([])
        assert clientfactory.build_client(args, region="sa-east-1").meta.region_name == "sa-east-1"
        assert (
            clientfactory.build_client(args, region=None).meta.region_name
            == clientfactory.build_client(args).meta.region_name
        )
        for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        assert clientfactory.build_client(args, region=None).meta.region_name == "aws-global"

    def test_cli_sessions_install_the_fast_timestamp_parser(self) -> None:
        # Every CLI-built session registers the library's fast_parse_timestamp
        # on its response-parser factory before any client exists; listing
        # timestamps then parse at C speed (tests/lib/test_sessions.py pins
        # the parser's value-equality with botocore).
        from boto3_s3 import fast_parse_timestamp

        session = clientfactory.build_session(_parse([]))
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
        client = clientfactory.build_client(args)
        assert client.meta.region_name == "us-west-2"
        assert client.meta.endpoint_url == "http://localhost:9000"

    def test_defaults_build_an_s3_client(self) -> None:
        client = clientfactory.build_client(_parse([]))
        assert client.meta.service_model.service_name == "s3"

    def test_retry_defaults_match_aws_v2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws v2's bundled botocore hard-codes retry_mode='standard' /
        # max_attempts=3 as its defaults (stock botocore: legacy / 5), so
        # every request retries like aws s3's.
        monkeypatch.delenv("AWS_RETRY_MODE", raising=False)
        monkeypatch.delenv("AWS_MAX_ATTEMPTS", raising=False)
        client = clientfactory.build_client(_parse([]))
        assert client.meta.config.retries == {"mode": "standard", "total_max_attempts": 3}

    def test_retry_env_overrides_beat_the_aws_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The aws default only fills in; the user's env/config still wins.
        monkeypatch.setenv("AWS_RETRY_MODE", "adaptive")
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "7")
        client = clientfactory.build_client(_parse([]))
        assert client.meta.config.retries == {"mode": "adaptive", "total_max_attempts": 7}

    def test_broken_max_attempts_is_reported_ahead_of_a_broken_retry_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws resolves the attempts first - its bundled `_compute_retry_config`
        # calls `_compute_retry_max_attempts` before `_compute_retry_mode`, and
        # the int conversion is the config store's read-time type - so with both
        # values broken the int cast is what fails (measured on the pinned
        # aws-cli: `aws: [ERROR]: invalid literal for int() with base 10:
        # 'abc'`, rc 255). Validating the mode first would report the mode.
        monkeypatch.setenv("AWS_RETRY_MODE", "bogus")
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "abc")
        with pytest.raises(ValueError) as excinfo:
            clientfactory.build_client(_parse([]))
        assert str(excinfo.value) == "invalid literal for int() with base 10: 'abc'"

    def test_broken_config_max_attempts_also_wins_over_the_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same order through the profile, where both values are read off the
        # scoped config rather than the environment.
        config_file = tmp_path / "config"
        config_file.write_text("[default]\nretry_mode = bogus\nmax_attempts = abc\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        for var in ("AWS_RETRY_MODE", "AWS_MAX_ATTEMPTS"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValueError) as excinfo:
            clientfactory.build_client(_parse([]))
        assert str(excinfo.value) == "invalid literal for int() with base 10: 'abc'"

    def test_scalar_s3_section_fails_at_client_build_like_aws(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `s3 = max_concurrent_requests=1` (a scalar where a section is
        # expected): both aws v2 and stock botocore die at client construction
        # (_resolve_use_dualstack_endpoint calls .get on the str) with
        # AttributeError "'str' object has no attribute 'get'" -> the general
        # backstop's rc 255 and the same message (measured on the pinned
        # aws-cli and this CLI, ls and cp alike). Pinned so a future flow
        # reorder (reading [s3] ahead of the client build) cannot turn the
        # typo into a silent default.
        config_file = tmp_path / "config"
        config_file.write_text("[default]\ns3 = max_concurrent_requests=1\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
        with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
            clientfactory.build_client(_parse([]))

    @pytest.mark.parametrize("mode", ["legacy", "bogus", "LEGACY", ""])
    def test_an_unsupported_retry_mode_is_rejected_in_aws_s_words(
        self, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        # aws v2's bundled botocore restricts retry modes to standard/adaptive
        # and names those two in its report. The installed botocore accepts
        # "legacy" (its own valid mode) and, for the rest, names all three -
        # so every one of these values needs the aws report, measured on the
        # pinned aws-cli at rc 255.
        monkeypatch.setenv("AWS_RETRY_MODE", mode)
        with pytest.raises(InvalidConfigError) as excinfo:
            clientfactory.build_client(_parse([]))
        assert str(excinfo.value) == (
            f'Invalid value provided to "mode": "{mode}" must be one of: "standard" or "adaptive"'
        )
        assert exit_code_for(excinfo.value) == 255

    def test_empty_retry_env_is_present_and_fatal_like_aws(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Present-wins, like the profile/region chains: aws treats an empty
        # AWS_MAX_ATTEMPTS as a fatal value (rc 255), never as unset - int("")
        # -> ValueError, which main's backstop maps to 255. (The empty mode is
        # the last case of the test above.)
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "")
        with pytest.raises(ValueError, match="invalid literal"):
            clientfactory.build_client(_parse([]))

    def test_aws_region_env_honored_like_aws_v2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws v2 resolves AWS_REGION ahead of AWS_DEFAULT_REGION; stock
        # botocore only knows AWS_DEFAULT_REGION (which the test fixture pins
        # to us-east-1), so this passes only through build_client's explicit
        # injection.
        monkeypatch.setenv("AWS_REGION", "eu-west-3")
        client = clientfactory.build_client(_parse([]))
        assert client.meta.region_name == "eu-west-3"

    def test_explicit_region_beats_aws_region_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-west-3")
        client = clientfactory.build_client(_parse(["--region", "us-west-2"]))
        assert client.meta.region_name == "us-west-2"

    def test_aws_region_beats_aws_default_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli's region chain lists AWS_REGION ahead of AWS_DEFAULT_REGION;
        # stock botocore never adopted AWS_REGION, so _resolve_region restores it.
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-1")
        client = clientfactory.build_client(_parse([]))
        assert client.meta.region_name == "eu-central-1"

    def test_empty_aws_region_is_present_wins_like_aws(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli's env providers are present-wins: AWS_REGION="" selects the
        # empty region (botocore -> "Invalid endpoint", rc 255 like aws), it does
        # NOT fall through to AWS_DEFAULT_REGION (the old `or None` wrongly did).
        monkeypatch.setenv("AWS_REGION", "")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-1")
        with pytest.raises(ValueError, match="Invalid endpoint"):
            clientfactory.build_client(_parse([]))

    def test_imds_region_is_the_final_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws-cli's chain ends in IMDSRegionProvider; with nothing else set the
        # EC2 instance region is used (boto3-s3 must not silently default to
        # us-east-1). Stock botocore keeps IMDS for smart-defaults only, so
        # _resolve_region wires it in explicitly. Fake the provider (no network).
        import botocore.utils

        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        class _FakeIMDS:
            def __init__(self, *args: object, **kwargs: object) -> None: ...

            def provide(self) -> str:
                return "ap-southeast-2"

        monkeypatch.setattr(botocore.utils, "IMDSRegionProvider", _FakeIMDS)
        client = clientfactory.build_client(_parse([]))
        assert client.meta.region_name == "ap-southeast-2"

    def test_a_rejected_imds_probe_reads_as_no_region(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # botocore's region fetcher swallows only the retries-exceeded failure,
        # so a service answering the IMDS address that is not EC2's - it
        # rejects the token request outright - raises BadIMDSRequestError out
        # of the chain. aws-cli carries its own fetcher for exactly that catch,
        # so on such a host aws resolves no region and runs on; escaping here
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
        assert clientfactory.build_client(_parse([])).meta.region_name == "aws-global"

    def test_us_east_1_resolves_regional_endpoint(self) -> None:
        # aws v2 resolves us-east-1 to the regional endpoint, not the legacy
        # global one (aws-cli functional-test expectations); build_client pins
        # the same resolution.
        client = clientfactory.build_client(_parse(["--region", "us-east-1"]))
        assert client.meta.endpoint_url == "https://s3.us-east-1.amazonaws.com"

    def test_presigned_urls_are_sigv4_even_in_us_east_1(self) -> None:
        # Stock botocore downgrades presigned URLs to SigV2 where the region
        # still accepts it; aws v2's botocore has no SigV2 at all. The pinned
        # s3v4 keeps presign output aws-shaped.
        client = clientfactory.build_client(_parse(["--region", "us-east-1"]))
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": "bucket", "Key": "key"}, ExpiresIn=60
        )
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url

    def test_symmetric_sigv4_signers_stay_pure_python(self) -> None:
        # With awscrt importable (the dev group installs botocore[crt]; the
        # dist leaves CRT to the opt-in `crt` extra) stock botocore swaps the
        # symmetric SigV4 families to CRT signers, whose presigner renders
        # X-Amz-Expires after X-Amz-SignedHeaders. aws v2's bundled botocore
        # hard-pins the pure-Python classes; build_client restores that
        # table (a no-op re-assert when awscrt is absent).
        from botocore import auth

        client = clientfactory.build_client(_parse(["--region", "us-east-1"]))
        assert auth.AUTH_TYPE_MAPS["s3v4"] is auth.S3SigV4Auth
        assert auth.AUTH_TYPE_MAPS["s3v4-query"] is auth.S3SigV4QueryAuth
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": "bucket", "Key": "key"}, ExpiresIn=60
        )
        assert url.index("X-Amz-Expires=") < url.index("X-Amz-SignedHeaders=")

    @pytest.mark.parametrize("alias", ["test.mrap", "my-alias.mrap", "plainname"])
    def test_mrap_target_lifts_the_pin_to_sigv4a(self, alias: str) -> None:
        # An explicit signature_version suppresses botocore's auth-scheme
        # resolution, and an MRAP endpoint must resolve to asymmetric SigV4a
        # (region set `*`) - measured against aws 2.36.1, whose MRAP presign
        # signs AWS4-ECDSA-P256-SHA256 for each of these aliases (the empty
        # ARN region is the whole test; the `.mrap` suffix is a convention).
        # The s3v4 pin stands down when a positional names an MRAP ARN; the
        # dev environment's awscrt (always present, design/testing.md
        # section 4) then signs SigV4a offline.
        arn = f"arn:aws:s3::123456789012:accesspoint/{alias}"
        args = _parse(["--region", "us-east-1"])
        args.path = f"s3://{arn}/key"
        client = clientfactory.build_client(args)
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": arn, "Key": "key"}, ExpiresIn=60
        )
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
    def test_outpost_target_lifts_the_pin_to_sigv4a(self, region: str, arn: str) -> None:
        # An S3 Outposts access point resolves to SigV4a like an MRAP, which
        # the pin would suppress - measured against aws 2.36.1, whose presign
        # for each of these signs AWS4-ECDSA-P256-SHA256 with a region-less
        # `.../s3-outposts/aws4_request` scope. Every separator mix, an
        # uppercase access point name, a dashed outpost id and a non-`aws`
        # partition are all one shape to botocore's ARN parse, which is what
        # the stand-down probe reads.
        args = _parse(["--region", region])
        args.path = f"s3://{arn}/key"
        client = clientfactory.build_client(args)
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": arn, "Key": "key"}, ExpiresIn=60
        )
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
    def test_outpost_alias_target_lifts_the_pin_to_sigv4a(self, alias: str) -> None:
        # An Outposts access point *alias* has no ARN, but resolves to the same
        # SigV4a endpoint as the ARN does - measured against aws 2.36.1, whose
        # presign for each of these signs AWS4-ECDSA-P256-SHA256 with a
        # region-less `.../s3-outposts/aws4_request` scope. The endpoint rules
        # read the alias by position (a `--op-s3` suffix, the hardware type 50
        # characters from the end, a host-label outpost id before it), which is
        # what the stand-down probe reproduces.
        args = _parse(["--region", "us-west-2"])
        args.path = f"s3://{alias}/key"
        client = clientfactory.build_client(args)
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": alias, "Key": "key"}, ExpiresIn=60
        )
        assert "X-Amz-Algorithm=AWS4-ECDSA-P256-SHA256" in url
        assert "X-Amz-Region-Set=" in url
        assert "%2Fus-west-2%2F" not in url  # SigV4a scope carries no region

    def test_outpost_alias_scheme_less_presign_lifts_the_pin(self) -> None:
        # presign takes its target with or without s3://, and aws resolves the
        # auth scheme off the final Bucket either way.
        alias = "test-accessp-o0b1d075431d83bebde8xz5w8ijx1qzlbp3i3kuse10--op-s3"
        args = _parse(["--region", "us-west-2"])
        args.path = f"{alias}/key"  # no s3:// scheme
        client = clientfactory.build_client(args)
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": alias, "Key": "key"}, ExpiresIn=60
        )
        assert "X-Amz-Algorithm=AWS4-ECDSA-P256-SHA256" in url

    @pytest.mark.parametrize(
        "bucket",
        [
            # A character short of the 50 the rules need to find a hardware
            # type, and a dotted alias: aws presigns both plain SigV4.
            "0b1d075431d83bebde8xz5w8ijx1qzlbp3i3kuse10--op-s3",
            "myap-1234.op-01234567890123456--op-s3",
            # Full length, but not the suffix.
            "test-accessp-o0b1d075431d83bebde8xz5w8ijx1qzlbp3i3kuse10--op-s4",
        ],
    )
    def test_near_miss_outpost_aliases_keep_the_pin(self, bucket: str) -> None:
        args = _parse(["--region", "us-west-2"])
        args.path = f"s3://{bucket}/key"
        client = clientfactory.build_client(args)
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": "key"}, ExpiresIn=60
        )
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url

    def test_local_op_s3_suffix_lookalike_keeps_the_pin(self) -> None:
        # As with --x-s3: a transfer positional without the scheme is a local
        # path, however alias-shaped its name is, so the pin stays.
        args = _parse(["--region", "us-west-2"])
        args.paths = [
            "./test-accessp-o0b1d075431d83bebde8xz5w8ijx1qzlbp3i3kuse10--op-s3",
            "s3://plain-bucket/key",
        ]
        client = clientfactory.build_client(args)
        resolver = client._ruleset_resolver  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        assert resolver._requested_auth_scheme == "s3v4"  # pyright: ignore[reportPrivateUsage]

    def test_transfer_positional_list_lifts_the_pin_too(self) -> None:
        # The transfer family carries a two-item `paths` list; an MRAP ARN on
        # either side lifts the pin for the command's client.
        args = _parse(["--region", "us-east-1"])
        args.paths = ["./local.txt", "s3://arn:aws:s3::123456789012:accesspoint/test.mrap/k"]
        client = clientfactory.build_client(args)
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": "arn:aws:s3::123456789012:accesspoint/test.mrap", "Key": "k"},
            ExpiresIn=60,
        )
        assert "X-Amz-Algorithm=AWS4-ECDSA-P256-SHA256" in url

    def test_plain_paths_keep_the_sigv4_pin(self) -> None:
        # A plain-bucket positional (and a plain access-point ARN) keeps the
        # always-SigV4 pin - only the MRAP shape needs SigV4a.
        args = _parse(["--region", "us-east-1"])
        args.paths = "s3://plain-bucket/key"
        client = clientfactory.build_client(args)
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": "plain-bucket", "Key": "key"}, ExpiresIn=60
        )
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url

    def test_plain_access_point_arn_keeps_the_sigv4_pin(self) -> None:
        # Region-qualified access-point ARN: not the MRAP shape (that one has
        # an empty region field), so the SigV4 pin stays.
        arn = "arn:aws:s3:us-east-1:123456789012:accesspoint/plain-ap"
        args = _parse(["--region", "us-east-1"])
        args.paths = [f"s3://{arn}/key", "./local.txt"]
        client = clientfactory.build_client(args)
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": arn, "Key": "key"}, ExpiresIn=60
        )
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url

    def test_s3express_target_lifts_the_pin(self) -> None:
        # A directory bucket must resolve to sigv4-s3express with CreateSession
        # credentials; an explicit signature_version suppresses that resolution
        # (s3v4 matches the scheme name up to the first dash) and signs a plain
        # SigV4 request instead - measured: a pinned express presign scopes
        # `.../s3/aws4_request` where the unpinned flow dials CreateSession,
        # like aws v2's bundled botocore. Signing needs that live CreateSession
        # call, so assert on the exact input botocore's per-request scheme
        # selection keys on: the requested auth scheme is unset, leaving the
        # endpoint's `sigv4-s3express` alive. (`meta.config.signature_version`
        # cannot serve here - botocore backfills it with the *resolved*
        # version, `s3v4` for S3 with or without the pin.)
        args = _parse(["--region", "us-east-1"])
        args.path = "s3://mybkt--use1-az4--x-s3/key"
        client = clientfactory.build_client(args)
        resolver = client._ruleset_resolver  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        assert resolver._requested_auth_scheme is None  # pyright: ignore[reportPrivateUsage]

    def test_s3express_transfer_positional_lifts_the_pin_too(self) -> None:
        args = _parse(["--region", "us-east-1"])
        args.paths = ["./local.txt", "s3://mybkt--use1-az4--x-s3/k"]
        client = clientfactory.build_client(args)
        resolver = client._ruleset_resolver  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        assert resolver._requested_auth_scheme is None  # pyright: ignore[reportPrivateUsage]

    def test_s3express_scheme_less_presign_lifts_the_pin(self) -> None:
        # presign takes its target with or without s3:// (unlike the transfer
        # family, where scheme-less means local). A scheme-less directory
        # bucket must still lift the pin so signing dials CreateSession -
        # otherwise the URL signs plain SigV4 and is unusable. aws resolves the
        # auth scheme off the final Bucket, so its scheme-less presign works.
        args = _parse(["--region", "us-east-1"])
        args.path = "mybkt--use1-az4--x-s3/key"  # no s3:// scheme
        client = clientfactory.build_client(args)
        resolver = client._ruleset_resolver  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        assert resolver._requested_auth_scheme is None  # pyright: ignore[reportPrivateUsage]

    def test_local_x_s3_suffix_lookalike_keeps_the_pin(self) -> None:
        # A local path can plausibly end in --x-s3; only the s3:// form names
        # a directory bucket, so the pin stays.
        args = _parse(["--region", "us-east-1"])
        args.paths = ["./backup--x-s3", "s3://plain-bucket/key"]
        client = clientfactory.build_client(args)
        resolver = client._ruleset_resolver  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
        assert resolver._requested_auth_scheme == "s3v4"  # pyright: ignore[reportPrivateUsage]

    def test_no_sign_request_still_wins_for_s3express(self) -> None:
        # --no-sign-request overrides to UNSIGNED even for a directory bucket,
        # exactly as it overrides the s3v4 pin: the bare zonal-endpoint URL.
        args = _parse(["--no-sign-request", "--region", "us-east-1"])
        args.path = "s3://mybkt--use1-az4--x-s3/key"
        client = clientfactory.build_client(args)
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": "mybkt--use1-az4--x-s3", "Key": "key"},
            ExpiresIn=60,
        )
        assert url == "https://mybkt--use1-az4--x-s3.s3express-use1-az4.us-east-1.amazonaws.com/key"

    def test_no_sign_request_still_wins_for_mrap(self) -> None:
        # --no-sign-request overrides to UNSIGNED even for an MRAP target,
        # exactly as it overrides the s3v4 pin: aws emits the bare URL.
        args = _parse(["--no-sign-request", "--region", "us-east-1"])
        args.path = "s3://arn:aws:s3::123456789012:accesspoint/test.mrap/key"
        client = clientfactory.build_client(args)
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": "arn:aws:s3::123456789012:accesspoint/test.mrap", "Key": "key"},
            ExpiresIn=60,
        )
        assert url == "https://test.mrap.accesspoint.s3-global.amazonaws.com/key"

    def test_no_sign_request_presigns_to_a_bare_url(self) -> None:
        # --no-sign-request must still override to UNSIGNED: aws emits the
        # plain object URL with no query at all.
        client = clientfactory.build_client(_parse(["--no-sign-request", "--region", "us-east-1"]))
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": "bucket", "Key": "key"}, ExpiresIn=60
        )
        assert url == "https://bucket.s3.us-east-1.amazonaws.com/key"

    def test_schemeless_endpoint_is_a_usage_error(self) -> None:
        # aws rejects --endpoint-url without a scheme at parse time (rc 252);
        # without this, botocore raises a bare ValueError at client creation.
        with pytest.raises(ValidationError) as excinfo:
            clientfactory.build_client(
                _parse(["--region", "us-east-1", "--endpoint-url", "example.com"])
            )
        assert 'Bad value for --endpoint-url "example.com": scheme is missing' in str(excinfo.value)

    @pytest.mark.parametrize(
        "value", ["192.168.0.9:9000", "127.0.0.1:9000/prefix", "+http://127.0.0.1:9000"]
    )
    def test_a_scheme_needs_an_ascii_letter_in_front_like_the_python_aws_ships(
        self, value: str
    ) -> None:
        # `--endpoint-url 192.168.0.9:9000` is what a user types with http://
        # forgotten in front of a MinIO / Ceph address. Only from Python 3.11 on
        # does urlsplit require an ASCII letter to lead a scheme, so on the
        # supported floor `192.168.0.9` (and `+http`) parses AS the scheme; a
        # plain truthiness test then passes the value to botocore for a
        # different message and rc, where aws - frozen on 3.14 - rejects it at
        # parse time. Measured on the pinned aws-cli: rc 252 and the wording
        # below, for every command surface.
        with pytest.raises(ValidationError) as excinfo:
            clientfactory.build_client(_parse(["--region", "us-east-1", "--endpoint-url", value]))
        assert exit_code_for(excinfo.value) == 252
        assert str(excinfo.value) == (
            f'Bad value for --endpoint-url "{value}": scheme is missing.  '
            "Must be of the form http://<hostname>/ or https://<hostname>/"
        )

    @pytest.mark.parametrize(
        "value", [" http://127.0.0.1:9000", "HTTP://127.0.0.1:9000", "h://127.0.0.1:9000"]
    )
    def test_an_unusual_but_present_scheme_still_passes(self, value: str) -> None:
        # The other direction of the same gate, which a first-character test on
        # the raw value would get wrong: urlsplit strips leading whitespace
        # before it looks for the scheme and lower-cases what it finds, so all
        # three of these carry one and aws accepts them (measured: the leading
        # space and the upper case connect to 127.0.0.1:9000, the one-letter
        # scheme reaches botocore's own `Custom endpoint ... was not a valid
        # URI` - never the 252 above).
        clientfactory.validate_endpoint_url(_parse(["--endpoint-url", value]))

    def test_zero_timeout_means_no_timeout(self) -> None:
        # aws maps a 0 timeout to None ("no timeout"); botocore rejects a
        # literal 0 with ValueError, which would otherwise crash client creation.
        client = clientfactory.build_client(
            _parse(
                ["--region", "us-east-1", "--cli-read-timeout", "0", "--cli-connect-timeout", "0"]
            )
        )
        assert client.meta.config.read_timeout is None
        assert client.meta.config.connect_timeout is None

    def test_integer_timeout_is_applied(self) -> None:
        client = clientfactory.build_client(
            _parse(
                ["--region", "us-east-1", "--cli-read-timeout", "5", "--cli-connect-timeout", "7"]
            )
        )
        assert client.meta.config.read_timeout == 5
        assert client.meta.config.connect_timeout == 7

    @pytest.mark.parametrize("flag", ["--cli-read-timeout", "--cli-connect-timeout"])
    def test_noninteger_timeout_maps_to_255_not_a_parse_error(self, flag: str) -> None:
        # aws coerces the timeouts in a post-parse handler (int()), so a non-integer
        # value raises there and exits 255 - not the parse-time 252 an argparse
        # type=int would give. The arg must still parse (no type=int rejecting it up
        # front), and build_client must surface InvalidValueError, whose rc is the
        # general 255 - not ValidationError's 252 or ConfigurationError's 253.
        args = _parse(["--region", "us-east-1", flag, "abc"])
        with pytest.raises(InvalidValueError) as excinfo:
            clientfactory.build_client(args)
        assert exit_code_for(excinfo.value) == 255

    def test_unknown_profile_maps_to_a_library_error(self) -> None:
        # A bad --profile raises raw botocore ProfileNotFound; build_client must
        # translate it (-> InvalidConfigError, rc 255: aws's general handler, not
        # the 253 pair) so it does not escape as an uncaught traceback.
        with pytest.raises(InvalidConfigError) as excinfo:
            clientfactory.build_client(
                _parse(["--profile", "boto3_s3_definitely_nonexistent_profile"])
            )
        assert exit_code_for(excinfo.value) == 255
        assert "boto3_s3_definitely_nonexistent_profile" in str(excinfo.value)

    def test_aws_profile_env_beats_aws_default_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli (bundled botocore) resolves AWS_PROFILE ahead of
        # AWS_DEFAULT_PROFILE; stock botocore reverses the two (botocore #1725),
        # so a bare session would pick the wrong one. build_client resolves the
        # profile itself to keep `aws s3` parity. The unknown profile surfaces in
        # the ProfileNotFound message, revealing which env var won.
        monkeypatch.setenv("AWS_PROFILE", "boto3_s3_from_aws_profile")
        monkeypatch.setenv("AWS_DEFAULT_PROFILE", "boto3_s3_from_default_profile")
        with pytest.raises(Boto3S3Error) as excinfo:
            clientfactory.build_client(_parse([]))
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
            clientfactory.build_client(_parse([]))
        assert "boto3_s3_from_default_profile" in str(excinfo.value)

    def test_profile_flag_beats_both_profile_envs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # --profile is the top of the chain, ahead of either env var.
        monkeypatch.setenv("AWS_PROFILE", "boto3_s3_from_aws_profile")
        monkeypatch.setenv("AWS_DEFAULT_PROFILE", "boto3_s3_from_default_profile")
        with pytest.raises(Boto3S3Error) as excinfo:
            clientfactory.build_client(_parse(["--profile", "boto3_s3_from_flag"]))
        assert "boto3_s3_from_flag" in str(excinfo.value)

    def test_empty_profile_flag_falls_through_to_env_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws's _handle_top_level_args binds --profile only under a truthy guard,
        # so --profile "" is ignored and the env chain wins (aws then reaches the
        # server, rc 254, not ProfileNotFound). resolve_profile matches: the empty
        # flag falls through to AWS_PROFILE.
        monkeypatch.setenv("AWS_PROFILE", "boto3_s3_from_aws_profile")
        assert clientfactory.resolve_profile(_parse(["--profile", ""])) == (
            "boto3_s3_from_aws_profile"
        )

    def test_empty_profile_flag_with_no_env_is_the_default_profile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With the flag empty and no profile env, resolve_profile yields None so
        # boto3 uses the default profile - aws reaches the server rather than
        # failing on an empty profile name.
        monkeypatch.delenv("AWS_PROFILE", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
        assert clientfactory.resolve_profile(_parse(["--profile", ""])) is None

    def test_partial_credentials_map_to_255_not_253(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws has no handler for PartialCredentialsError -> GeneralExceptionHandler
        # (255), unlike NoCredentials/NoRegion (253). build_client maps it to
        # InvalidConfigError, whose rc is the general 255, not 253.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
        monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
        with pytest.raises(InvalidConfigError) as excinfo:
            clientfactory.build_client(_parse(["--region", "us-east-1"]))
        assert exit_code_for(excinfo.value) == 255


class TestProfileSessionBinding:
    """Only a truthy ``--profile`` may be bound onto the session, as aws does.

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
        # The whole point: aws signs with the env keys here (measured, rc 0),
        # where an instance-bound profile would report "Unable to locate
        # credentials" (rc 253) because the profile carries none of its own.
        monkeypatch.setenv("AWS_PROFILE", "named")
        assert self._access_key(clientfactory.build_session(_parse([]))) == self._ENV_KEY

    def test_env_named_profile_still_selects_the_profile_s_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Not promoting the env profile must not stop it selecting the profile:
        # the region comes from `[profile named]`, not `[default]`.
        monkeypatch.setenv("AWS_PROFILE", "named")
        assert clientfactory.build_client(_parse([])).meta.region_name == "eu-north-1"

    def test_profile_flag_takes_the_profile_s_credentials(self) -> None:
        # The flag IS bound, so botocore disables the env provider and the
        # profile's own keys sign - aws does the same (measured).
        session = clientfactory.build_session(_parse(["--profile", "named"]))
        assert self._access_key(session) == self._PROFILE_KEY

    def test_env_named_profile_is_not_a_session_instance_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_PROFILE", "named")
        botocore_session = clientfactory.build_session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        assert botocore_session.instance_variables().get("profile") is None
        assert botocore_session.get_config_variable("profile") == "named"

    def test_profile_flag_is_bound_as_a_session_instance_variable(self) -> None:
        session = clientfactory.build_session(_parse(["--profile", "named"]))
        botocore_session = session._session  # pyright: ignore[reportPrivateUsage]
        assert botocore_session.instance_variables().get("profile") == "named"

    def test_empty_profile_flag_leaves_the_env_profile_unpromoted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws's truthy guard: `--profile ""` is not bound, so the env chain
        # still names the profile and the env credentials survive.
        monkeypatch.setenv("AWS_PROFILE", "named")
        session = clientfactory.build_session(_parse(["--profile", ""]))
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
            clientfactory.build_client(_parse([]))
        assert exit_code_for(excinfo.value) == 255
        assert "The config profile () could not be found" in str(excinfo.value)

    def test_aws_profile_beats_aws_default_profile_through_the_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The env order now lives in the session's own config chain, so the
        # aws-cli order has to be re-declared there: stock botocore reads
        # AWS_DEFAULT_PROFILE first and would resolve `[default]`'s sa-east-1.
        monkeypatch.setenv("AWS_PROFILE", "named")
        monkeypatch.setenv("AWS_DEFAULT_PROFILE", "default")
        assert clientfactory.build_client(_parse([])).meta.region_name == "eu-north-1"

    def test_every_entry_point_opens_its_session_the_same_way(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One opener behind all four session builders: a site left on
        # `Session(profile=...)` would silently reinstate the promotion.
        opened: list[Any] = []
        real_opener = clientfactory._open_botocore_session  # pyright: ignore[reportPrivateUsage]

        def recording(args: argparse.Namespace) -> Any:
            session = real_opener(args)
            opened.append(session)
            return session

        monkeypatch.setattr(clientfactory, "_open_botocore_session", recording)
        monkeypatch.setenv("AWS_PROFILE", "named")
        clientfactory.validate_profile(_parse([]))
        clientfactory.build_client(_parse([]))
        clientfactory.build_service_client("sts", _parse([]))
        clientfactory.build_session(_parse([]))
        assert len(opened) == 4
        assert [session.instance_variables().get("profile") for session in opened] == [None] * 4
        assert [session.get_config_variable("profile") for session in opened] == ["named"] * 4


class TestCredentialCache:
    """Assumed-role / web-identity / SSO credentials are cached on disk, as aws does.

    botocore's own cache is a per-process dict, so every invocation would
    re-call ``AssumeRole`` and an ``mfa_serial`` profile would prompt for a
    code every time - fatal in a non-interactive run. aws replaces it with a
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
        # An entry planted where aws's own runs put theirs: reading it back
        # through a provider's cache pins the directory (aws writes
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
        session = clientfactory.build_session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        caches = self._caches(session)
        # botocore's own cache, with aws's write and expiry rendering on top
        # (the two tests below) - and one class for all three providers.
        assert {type(cache) for cache in caches} == {type(caches[0])}
        assert [isinstance(cache, JSONFileCache) for cache in caches] == [True] * 3
        for cache in caches:
            assert "planted-key" in cache
            assert cache["planted-key"] == {"AccessKeyId": "ASIAPLANTED"}

    def test_an_expiry_is_stored_the_way_aws_stores_it(self, tmp_path: Path) -> None:
        # aws's cli_timestamp_format handler makes the response parser return
        # ISO-8601 *strings*, so an STS Expiration lands in the file verbatim -
        # `2026-08-13T11:27:42+09:00`, measured. This CLI parses timestamps to
        # datetime for speed, and botocore's default rendering would write
        # `2026-08-13T11:27:42UTC+09:00` (`strftime('%Z')` of a
        # datetime.timezone), which dateutil reads back with POSIX's inverted
        # sign: measured, the entry stayed "valid" 18 hours past its expiry -
        # for this CLI *and* for an aws sharing ~/.aws/cli/cache.
        cache = self._caches(clientfactory.build_session(_parse([]))._session)[0]  # pyright: ignore[reportPrivateUsage]
        expiry = datetime(2026, 8, 13, 11, 27, 42, tzinfo=timezone(timedelta(hours=9)))
        cache["expiring-key"] = {"AccessKeyId": "ASIAX", "Expiration": expiry}
        assert (self._cache_dir(tmp_path) / "expiring-key.json").read_text() == (
            '{"AccessKeyId": "ASIAX", "Expiration": "2026-08-13T11:27:42+09:00"}'
        )
        # The plain `Z` wire form aws's STS actually sends renders the same on
        # both sides too.
        cache["utc-key"] = {"Expiration": datetime(2099, 1, 1, tzinfo=timezone.utc)}
        assert (self._cache_dir(tmp_path) / "utc-key.json").read_text() == (
            '{"Expiration": "2099-01-01T00:00:00+00:00"}'
        )

    def test_an_entry_is_written_straight_to_its_final_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws's bundled botocore opens the entry itself (O_WRONLY | O_CREAT,
        # 0600); the installed one writes a `tempfile.mkstemp` file and
        # renames it, so a failed write named a random `tmpXXXXXXXX.tmp` where
        # aws named the entry (measured on an unwritable cache directory: rc
        # 255 on both, different paths, and ours differed between its own
        # runs). Tripwire: mkstemp must not be reached at all.
        import tempfile

        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the credential cache must not write through a temp file")

        monkeypatch.setattr(tempfile, "mkstemp", refuse)
        cache = self._caches(clientfactory.build_session(_parse([]))._session)[0]  # pyright: ignore[reportPrivateUsage]
        cache["direct-key"] = {"AccessKeyId": "ASIADIRECT"}
        entry = self._cache_dir(tmp_path) / "direct-key.json"
        assert entry.read_text() == '{"AccessKeyId": "ASIADIRECT"}'
        assert sorted(path.suffix for path in self._cache_dir(tmp_path).iterdir()) == [
            ".json",
            ".json",
        ]
        if sys.platform != "win32":
            assert stat.S_IMODE(entry.stat().st_mode) == 0o600
        # Rewriting a shorter value must not leave the old tail behind (aws
        # truncates after opening).
        cache["direct-key"] = {"A": "B"}
        assert entry.read_text() == '{"A": "B"}'

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
    @pytest.mark.skipif(
        getattr(os, "geteuid", lambda: -1)() == 0, reason="root ignores the write bit"
    )
    def test_an_unwritable_cache_names_the_entry_like_aws(self, tmp_path: Path) -> None:
        # The report a user greps to fix the permissions: aws names the cache
        # entry (`.../c3cb4750....json`), byte-stable across runs.
        cache_dir = self._cache_dir(tmp_path)
        cache = self._caches(clientfactory.build_session(_parse([]))._session)[0]  # pyright: ignore[reportPrivateUsage]
        cache_dir.chmod(0o500)
        try:
            with pytest.raises(PermissionError) as excinfo:
                cache["denied-key"] = {"AccessKeyId": "ASIADENIED"}
        finally:
            cache_dir.chmod(0o700)
        assert excinfo.value.filename == str(cache_dir / "denied-key.json")

    def test_a_fetched_credential_lands_in_that_directory(self, tmp_path: Path) -> None:
        # The write half: whatever a provider caches becomes a file under the
        # shared directory, so the next invocation (and aws) can read it.
        session = clientfactory.build_session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        self._caches(session)[0]["written-key"] = {"AccessKeyId": "ASIAWRITTEN"}
        assert (self._cache_dir(tmp_path) / "written-key.json").read_text() == (
            '{"AccessKeyId": "ASIAWRITTEN"}'
        )

    def test_every_entry_point_gets_the_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws emits `session-initialized` once per run, so no session may be
        # left with botocore's in-memory dict: the sessions the startup gate
        # and the non-S3 client builder open assume the same role.
        opened: list[Any] = []
        real_opener = clientfactory._open_botocore_session  # pyright: ignore[reportPrivateUsage]

        def recording(args: argparse.Namespace) -> Any:
            session = real_opener(args)
            opened.append(session)
            return session

        monkeypatch.setattr(clientfactory, "_open_botocore_session", recording)
        clientfactory.validate_profile(_parse([]))
        clientfactory.build_client(_parse([]))
        clientfactory.build_service_client("sts", _parse([]))
        clientfactory.build_session(_parse([]))
        assert len(opened) == 4
        for session in opened:
            for cache in self._caches(session):
                assert isinstance(cache, JSONFileCache)
                assert cache["planted-key"] == {"AccessKeyId": "ASIAPLANTED"}

    def test_an_unknown_profile_is_still_reported_the_same_way(self) -> None:
        # Injecting the cache builds the provider chain while the session is
        # opened, which is where an unknown profile now raises: pin that the
        # startup gate still reports it with aws's wording and rc (measured
        # against the pinned aws-cli: rc 255, `The config profile
        # (nosuchprofile) could not be found`).
        with pytest.raises(InvalidConfigError) as excinfo:
            clientfactory.validate_profile(_parse(["--profile", "nosuchprofile"]))
        assert exit_code_for(excinfo.value) == 255
        assert str(excinfo.value) == "The config profile (nosuchprofile) could not be found"


class TestApiVersionsIsIgnored:
    """aws-cli v2 dropped ``api_versions`` in 2.0.0, so it must not bind here.

    The installed botocore still reads the key in ``create_client``; a config
    left over from aws-cli v1 would otherwise fail every command with ``Unable
    to load data for: s3/<version>/service-2`` where aws runs normally
    (measured against the pinned aws-cli, rc 255 vs rc 0).
    """

    @pytest.fixture(autouse=True)
    def _stale_api_versions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config = tmp_path / "config"
        config.write_text(
            "[default]\nregion = us-east-1\napi_versions =\n  s3 = 1999-01-01\n  sts = 1999-01-01\n"
        )
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))

    def test_the_s3_client_still_builds_on_the_current_model(self) -> None:
        client = clientfactory.build_client(_parse([]))
        assert client.meta.service_model.api_version == "2006-03-01"

    def test_service_clients_ignore_it_too(self) -> None:
        client = clientfactory.build_service_client("sts", _parse([]))
        assert client.meta.service_model.api_version != "1999-01-01"

    def test_the_session_pins_an_empty_map(self) -> None:
        session = clientfactory.build_session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        assert session.get_config_variable("api_versions") == {}


def _provider_client(session: Any, service: str = "sts") -> Any:
    """A client built the way the credential providers build their own.

    botocore's assume-role / web-identity / SSO providers create their STS and
    SSO clients through ``session.create_client(...)`` with no region, no
    config and no verify - so whatever those clients resolve, they resolve off
    the session alone. That is the surface aws configures at startup and this
    CLI configured per client, which is why these tests reach for it directly.
    """
    return session.create_client(service)


class TestNoSignRequestPosture:
    """``--no-sign-request`` is the session's posture, and ``--sse aws:kms`` beats it.

    aws's startup handler writes UNSIGNED into the session default client
    config, and its s3 ClientFactory adds ``Config(signature_version='s3v4')``
    for one case only: ``--sse aws:kms``. botocore merges the per-client config
    on top of the session's, so that case signs and resolves credentials while
    every other unsigned run stays anonymous - and the clients botocore builds
    for *itself* inherit UNSIGNED either way. Measured on the pinned aws-cli
    against a counting fake endpoint: ``cp``/``mv``/``sync``/``cp s3:// s3://``
    with ``--no-sign-request --sse aws:kms`` and no credentials are rc 1
    ``Unable to locate credentials`` with nothing sent (this CLI uploaded
    anonymously at rc 0), a broken ``source_profile`` under the same flags is
    aws's rc 255 profile report, and with a working assume-role profile aws
    sends an *unsigned* ``AssumeRole`` and then signs the upload with what it
    got back.
    """

    _KMS_ARGV = ("./local.txt", "s3://bkt/key", "--region", "us-east-1", "--no-sign-request")

    @pytest.fixture
    def _broken_role_profile(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A default profile whose credentials cannot be built, and no env keys.

        Resolving credentials at all is then observable: the assume-role
        provider fails the whole build (rc 255, aws's own report), where an
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
        # The placement, not just the value: aws puts UNSIGNED in the session
        # default client config, so the credential chain's own STS / SSO
        # clients are unsigned too - a per-client Config never reached them.
        from botocore import UNSIGNED

        args = _parse(["--region", "us-east-1", "--no-sign-request"])
        boto3_session = clientfactory.build_session(args)
        session = boto3_session._session  # pyright: ignore[reportPrivateUsage]
        client = clientfactory.build_client(args, session=boto3_session)
        assert client.meta.config.signature_version is UNSIGNED
        assert _provider_client(session).meta.config.signature_version is UNSIGNED

    def test_sse_aws_kms_signs_and_resolves_credentials(self, _broken_role_profile: None) -> None:
        # aws's one per-client signature_version. It beats the session's
        # UNSIGNED in botocore's merge, which restores signing *and* the
        # credential resolution botocore skips for an unsigned client.
        args = _parse_transfer([*self._KMS_ARGV, "--sse", "aws:kms"])
        with pytest.raises(InvalidConfigError) as excinfo:
            clientfactory.build_client(args)
        assert 'source_profile "missingprofile"' in str(excinfo.value)
        assert exit_code_for(excinfo.value) == 255

    def test_sse_aws_kms_pins_the_signature_the_way_aws_does(self) -> None:
        args = _parse_transfer([*self._KMS_ARGV, "--sse", "aws:kms"])
        assert clientfactory.build_client(args).meta.config.signature_version == "s3v4"

    @pytest.mark.parametrize(
        "extra",
        [[], ["--sse", "AES256"], ["--sse-c", "AES256"], ["--sse-kms-key-id", "somekey"]],
        ids=["bare", "sse-aes256", "sse-c", "sse-kms-key-id"],
    )
    def test_every_other_unsigned_run_stays_anonymous(
        self, _broken_role_profile: None, extra: list[str]
    ) -> None:
        # The comparison is aws's own exact string against ``--sse``, so no
        # neighbouring option restores signing - and an anonymous client asks
        # for no credentials at all, which is why the broken profile is silent.
        from botocore import UNSIGNED

        client = clientfactory.build_client(_parse_transfer([*self._KMS_ARGV, *extra]))
        assert client.meta.config.signature_version is UNSIGNED

    def test_the_path_resolver_clients_stay_anonymous_too(self) -> None:
        # aws's `S3PathResolver.from_session` names no config, so its
        # s3control / sts clients take the session's UNSIGNED even on the run
        # where --sse aws:kms signs the transfer.
        from botocore import UNSIGNED

        args = _parse_transfer([*self._KMS_ARGV, "--sse", "aws:kms"])
        client = clientfactory.build_service_client("sts", args, region="us-east-1")
        assert client.meta.config.signature_version is UNSIGNED

    def test_a_signed_run_is_untouched_by_the_kms_case(self, _broken_role_profile: None) -> None:
        # Without --no-sign-request the s3v4 pin was always there; --sse
        # aws:kms changes nothing, credentials included.
        for extra in ([], ["--sse", "aws:kms"]):
            args = _parse_transfer(["./local.txt", "s3://bkt/key", "--region", "us-east-1", *extra])
            with pytest.raises(InvalidConfigError):
                clientfactory.build_client(args)


class TestRegionSessionBinding:
    """The resolved region binds to the session, as aws's driver binds it.

    aws installs its region chain as the session's own ``region`` provider and
    binds a truthy ``--region`` at its head, so the clients *botocore* builds
    for itself - chiefly the credential chain's STS client - resolve the same
    region as the S3 client. Measured against the pinned aws-cli: with an
    assume-role profile carrying no ``region`` key, ``--region eu-west-1``
    (or ``AWS_REGION``) has aws call ``sts.eu-west-1.amazonaws.com`` and exit
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

    @pytest.mark.parametrize("build", ["client", "service_client"])
    def test_a_builder_that_opens_its_own_session_binds_it_too(
        self, monkeypatch: pytest.MonkeyPatch, build: str
    ) -> None:
        # Both client builders can open a session themselves, and the binding
        # is a property of the session, not of the caller.
        opened: list[Any] = []
        real_opener = clientfactory._open_botocore_session  # pyright: ignore[reportPrivateUsage]

        def recording(args: argparse.Namespace) -> Any:
            session = real_opener(args)
            opened.append(session)
            return session

        monkeypatch.setattr(clientfactory, "_open_botocore_session", recording)
        args = _parse(["--region", "eu-north-1"])
        if build == "client":
            clientfactory.build_client(args)
        else:
            clientfactory.build_service_client("sts", args)
        assert [_provider_client(session).meta.region_name for session in opened] == ["eu-north-1"]

    def test_an_unresolved_region_binds_nothing(self) -> None:
        # aws's truthy guard. Nothing resolved means the session keeps
        # botocore's own (identical) answer - and the NoRegion envelope that
        # answer produces for s3control stays exactly where it was.
        s3 = clientfactory.build_s3(_parse([]))
        session = self._session_of(s3)
        assert session.instance_variables().get("region") is None
        assert session.get_config_variable("region") is None
        with pytest.raises(ConfigurationError) as excinfo:
            clientfactory.build_service_client("s3control", _parse([]), session=s3.session)
        assert exit_code_for(excinfo.value) == 253

    def test_an_empty_region_flag_binds_nothing_and_still_fails_the_build(self) -> None:
        # `--region ""` is falsy, so it never heads aws's chain - and here the
        # rest of the chain answers nothing either, so nothing binds. The empty
        # string still reaches the client as itself and fails construction on
        # both tools (rc 255) before any credential is fetched.
        args = _parse(["--region", ""])
        session = clientfactory.build_session(args)._session  # pyright: ignore[reportPrivateUsage]
        clientfactory._bind_region(  # pyright: ignore[reportPrivateUsage]
            session,
            args.region,
            clientfactory._resolve_region(args.region, session),  # pyright: ignore[reportPrivateUsage]
        )
        assert session.get_config_variable("region") is None
        with pytest.raises(ValueError, match="Invalid endpoint"):
            clientfactory.build_client(args)

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
        # Only a truthy --region heads aws's chain, so a falsy one drops out of
        # it entirely and the session takes the next source's answer. Measured:
        # `--region "" ` with AWS_REGION=eu-west-1 has aws sign AssumeRole in
        # eu-west-1 (rc 255 at the S3 endpoint), where this CLI called STS not
        # at all and exited 253 with NoRegion. The client still gets the empty
        # string itself, which is the half aws passes straight through.
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
        # answer - and aws binds it, signing with an empty region scope.
        # Measured with an assume-role profile: aws called STS once, scoped
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

    def test_the_region_chain_is_still_walked_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Binding must not cost a second walk: the chain's last link is the
        # IMDS probe (seconds on a host with no region and no metadata
        # service), which is why the answer is threaded rather than re-asked.
        real_resolve = clientfactory._resolve_region  # pyright: ignore[reportPrivateUsage]
        walks: list[str | None] = []

        def counting_resolve(explicit: str | None, session: Any) -> Any:
            resolved = real_resolve(explicit, session)
            walks.append(resolved)
            return resolved

        monkeypatch.setenv("AWS_REGION", "eu-west-2")
        monkeypatch.setattr(clientfactory, "_resolve_region", counting_resolve)
        s3 = clientfactory.build_s3(_parse([]))
        s3.client()
        s3.client()
        assert walks == ["eu-west-2"]


class TestSessionRetryPosture:
    """aws v2's retry defaults belong to the session, not to one client.

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
        session = clientfactory.build_session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        assert session.get_config_variable("retry_mode") == "standard"
        assert session.get_config_variable("max_attempts") == 3
        assert _provider_client(session).meta.config.retries == {
            "mode": "standard",
            "total_max_attempts": 3,
        }

    def test_the_user_s_overrides_still_win_there(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_RETRY_MODE", "adaptive")
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "7")
        session = clientfactory.build_session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        assert _provider_client(session).meta.config.retries == {
            "mode": "adaptive",
            "total_max_attempts": 7,
        }

    def test_the_cli_timeouts_reach_a_provider_client(self) -> None:
        # aws's globalargs writes both timeouts into the session default client
        # config, so a stalled STS aborts on the flag's budget: measured,
        # `--cli-read-timeout 2` against an STS stalling 8s is rc 255 and a
        # read timeout on aws, where this CLI ran to botocore's default and
        # reported the server's own error at rc 254.
        args = _parse(["--cli-read-timeout", "2", "--cli-connect-timeout", "3"])
        session = clientfactory.build_session(args)._session  # pyright: ignore[reportPrivateUsage]
        client = _provider_client(session)
        assert client.meta.config.read_timeout == 2
        assert client.meta.config.connect_timeout == 3

    def test_the_default_timeouts_are_aws_s_own(self) -> None:
        # aws defaults both to botocore's 60s rather than leaving them unset.
        session = clientfactory.build_session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        default = session.get_default_client_config()
        assert (default.connect_timeout, default.read_timeout) == (60, 60)

    def test_a_zero_timeout_reaches_it_as_no_timeout(self) -> None:
        session = clientfactory.build_session(_parse(["--cli-read-timeout", "0"]))._session  # pyright: ignore[reportPrivateUsage]
        assert _provider_client(session).meta.config.read_timeout is None


class TestConfigErrorOrder:
    """A config carrying several mistakes reports the one aws reports.

    Measured on the pinned aws-cli, its order is: the profile's ``services``
    section, then the raw ``[s3]`` read, then the ``max_attempts`` int cast,
    then ``[s3] addressing_style``, and only then the attempt range and the
    retry mode. Resolving the retry configuration before ``create_client``, as
    this module used to, put the last two in front of all of them.
    """

    def _config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
        config = tmp_path / "config"
        config.write_text(body)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        for var in ("AWS_RETRY_MODE", "AWS_MAX_ATTEMPTS"):
            monkeypatch.delenv(var, raising=False)

    _SERVICES = 'The profile is configured to use the services section but the "nope" '
    _ADDRESSING = "S3 addressing style bogus is invalid."

    def test_a_missing_services_section_outranks_a_bad_retry_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._config(tmp_path, monkeypatch, "[default]\nservices = nope\nretry_mode = legacy\n")
        with pytest.raises(InvalidConfigError, match=self._SERVICES):
            clientfactory.build_client(_parse([]))

    def test_a_missing_services_section_outranks_a_bad_attempt_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._config(tmp_path, monkeypatch, "[default]\nservices = nope\nmax_attempts = abc\n")
        with pytest.raises(InvalidConfigError, match=self._SERVICES):
            clientfactory.build_client(_parse([]))

    def test_a_missing_services_section_outranks_a_degenerate_s3_section(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._config(tmp_path, monkeypatch, "[default]\nservices = nope\ns3 =\n")
        with pytest.raises(InvalidConfigError, match=self._SERVICES):
            clientfactory.build_client(_parse([]))

    def test_an_explicit_endpoint_stands_the_services_lookup_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # botocore consults the services section only when the client resolves
        # its own endpoint, so with --endpoint-url the retry mode is what aws
        # reports (measured).
        self._config(tmp_path, monkeypatch, "[default]\nservices = nope\nretry_mode = legacy\n")
        with pytest.raises(InvalidConfigError, match='Invalid value provided to "mode"'):
            clientfactory.build_client(_parse(["--endpoint-url", "http://localhost:9000"]))

    def test_a_degenerate_s3_section_outranks_a_bad_attempt_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._config(tmp_path, monkeypatch, "[default]\ns3 =\nmax_attempts = abc\n")
        with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
            clientfactory.build_client(_parse([]))

    def test_a_bad_attempt_count_outranks_a_bogus_addressing_style(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._config(
            tmp_path,
            monkeypatch,
            "[default]\nmax_attempts = abc\ns3 =\n  addressing_style = bogus\n",
        )
        with pytest.raises(ValueError, match="invalid literal for int"):
            clientfactory.build_client(_parse([]))

    @pytest.mark.parametrize("broken", ["retry_mode = legacy", "max_attempts = 0"])
    def test_a_bogus_addressing_style_outranks_the_retry_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
    ) -> None:
        self._config(
            tmp_path, monkeypatch, f"[default]\n{broken}\ns3 =\n  addressing_style = bogus\n"
        )
        with pytest.raises(InvalidConfigError, match=self._ADDRESSING):
            clientfactory.build_client(_parse([]))

    def test_a_bad_attempt_range_outranks_a_bad_retry_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both live in botocore's own Config validation, which checks the
        # attempt count first (measured: aws reports the count).
        self._config(tmp_path, monkeypatch, "[default]\nmax_attempts = 0\nretry_mode = legacy\n")
        with pytest.raises(InvalidConfigError, match='Value provided to "max_attempts"'):
            clientfactory.build_client(_parse([]))

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            ("[default]\nservices = nope\n", _SERVICES),
            ("[default]\ns3 =\n  addressing_style = bogus\n", _ADDRESSING),
            ("[default]\nretry_mode = legacy\n", 'Invalid value provided to "mode"'),
        ],
        ids=["services", "addressing", "retry-mode"],
    )
    def test_each_error_alone_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str, expected: str
    ) -> None:
        # The controls: reordering must not change what a single mistake says.
        self._config(tmp_path, monkeypatch, body)
        with pytest.raises(InvalidConfigError, match=expected):
            clientfactory.build_client(_parse([]))


class TestTlsKeyLogIsIgnored:
    """``SSLKEYLOGFILE`` decides nothing here, as it decides nothing for aws.

    botocore hands the variable to every client's SSL context under a
    ``sys.flags.ignore_environment`` guard; aws ships a frozen interpreter that
    runs isolated, so the guard is always true there. Measured: with a writable
    path aws writes no key log and this CLI wrote one; with an unopenable path
    aws runs normally (``presign`` rc 0, ``mb`` rc 1 on the refused endpoint)
    and this CLI exited 255 from the open.
    """

    def test_opening_a_session_takes_the_variable_out_of_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        keylog = tmp_path / "keys.log"
        monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))
        clientfactory.build_session(_parse([]))
        assert "SSLKEYLOGFILE" not in os.environ

    def test_no_key_material_is_written_by_a_built_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The quiet half: with the directory present, this CLI used to write
        # the process's TLS session keys where aws writes none. The file is
        # created while the client's HTTP session builds its SSL context, so
        # building the client is the whole exposure.
        keylog = tmp_path / "keys.log"
        monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))
        clientfactory.build_client(_parse([]))
        assert not keylog.exists()

    def test_an_unopenable_path_no_longer_fails_the_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SSLKEYLOGFILE", str(tmp_path / "absent" / "keys.log"))
        assert clientfactory.build_client(_parse([])).meta.service_model.service_name == "s3"

    def test_a_child_process_still_receives_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # aws passes the variable on untouched, and an external `!` alias -
        # the only child this CLI launches - runs without opening a session,
        # so the environment it inherits still carries it.
        from boto3_s3_cli import child_environ

        monkeypatch.setenv("SSLKEYLOGFILE", "/tmp/keys.log")
        assert child_environ()["SSLKEYLOGFILE"] == "/tmp/keys.log"


class TestUsEast1RegionalEndpointIsIgnored:
    """``us_east_1_regional_endpoint`` decides nothing, and never errors.

    aws v2's bundled botocore dropped the key from its ``[s3]`` table (us-east-1
    is regional there, always), so neither ``AWS_S3_US_EAST_1_REGIONAL_ENDPOINT``
    nor the config key is read or validated. The installed botocore validates
    it for every client that resolves an endpoint ruleset, so an invalid or
    empty value turned ``mv --validate-same-s3-paths`` into rc 255 where aws
    ran the move (measured).
    """

    @pytest.mark.parametrize("value", ["zzz", "", "legacy"])
    def test_an_env_value_reaches_no_client(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("AWS_S3_US_EAST_1_REGIONAL_ENDPOINT", value)
        args = _parse(["--region", "us-east-1"])
        client = clientfactory.build_client(args)
        assert client.meta.config.s3["us_east_1_regional_endpoint"] == "regional"
        clientfactory.build_service_client("s3control", args, region="us-east-1")
        session = clientfactory.build_session(args)._session  # pyright: ignore[reportPrivateUsage]
        assert _provider_client(session).meta.service_model.service_name == "sts"

    @pytest.mark.parametrize("value", ["zzz", "legacy"])
    def test_a_config_value_reaches_no_client_either(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        config = tmp_path / "config"
        config.write_text(f"[default]\ns3 =\n  us_east_1_regional_endpoint = {value}\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        args = _parse(["--region", "us-east-1"])
        client = clientfactory.build_client(args)
        assert client.meta.config.s3["us_east_1_regional_endpoint"] == "regional"
        # The pin is what keeps us-east-1 on the regional endpoint aws v2 uses;
        # a dropped key would send it to the legacy global one.
        assert client.meta.endpoint_url == "https://s3.us-east-1.amazonaws.com"
        clientfactory.build_service_client("sts", args, region="us-east-1")


class TestDefaultsMode:
    """A ``defaults_mode`` must not take the CLI down, whatever it is set to.

    The installed botocore implements defaults modes; aws v2's bundled botocore
    has no such setting and runs as if it were unset. Any valid mode other than
    ``legacy`` sends botocore's smart-defaults machinery at the session's
    ``[s3]`` section provider - the one this CLI wraps to pin
    ``us_east_1_regional_endpoint`` - with ``set_default_provider``. Measured:
    every valid mode, config key and ``AWS_DEFAULTS_MODE`` alike, failed
    *every* subcommand at rc 255 with ``'_RegionalS3Section' object has no
    attribute 'set_default_provider'`` where aws simply ran (``ls``,
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
        session = clientfactory.build_session(args)._session  # pyright: ignore[reportPrivateUsage]
        # The mode really arrives - otherwise the build below proves nothing.
        assert session.get_config_variable("defaults_mode") == mode
        client = clientfactory.build_client(args)
        # The smart defaults want `regional` for this key too, so the pin's
        # value survives the merge - as does the endpoint it decides.
        assert client.meta.config.s3["us_east_1_regional_endpoint"] == "regional"
        assert client.meta.endpoint_url == "https://s3.us-east-1.amazonaws.com"
        # And aws v2's retry posture is still the session's.
        assert client.meta.config.retries == {"total_max_attempts": 3, "mode": "standard"}
        clientfactory.build_service_client("sts", args, region="us-east-1")

    def test_a_smart_default_for_another_key_reaches_the_section(self) -> None:
        # The wrapper delegates rather than intercepts: a key the pin does not
        # own is botocore's to keep, so a future mode that vends one is not
        # silently dropped.
        from botocore.configprovider import ConstantProvider

        session = clientfactory.build_session(_parse([]))._session  # pyright: ignore[reportPrivateUsage]
        provider = session.get_component("config_store").get_config_provider("s3")
        provider.set_default_provider("addressing_style", ConstantProvider("path"))
        section = provider.provide()
        assert section["addressing_style"] == "path"
        assert section["us_east_1_regional_endpoint"] == "regional"

    def test_a_section_provider_without_the_method_fails_as_botocore_would(self) -> None:
        # No defensive swallowing: a wrapped provider that cannot take the
        # write fails where botocore's own deepcopy of it would, naming the
        # object botocore would have named.
        from botocore.configprovider import ConstantProvider

        class _ProvideOnly:
            def provide(self) -> None:
                return None

        section = clientfactory._RegionalS3Section(_ProvideOnly())  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(AttributeError, match="_ProvideOnly"):
            section.set_default_provider("us_east_1_regional_endpoint", ConstantProvider("legacy"))


class TestS3ErrorMsgRegistration:
    def test_every_built_client_rewrites_the_three_s3_messages(self) -> None:
        # aws hangs its s3errormsg handler off `after-call.s3` for the whole
        # session, so the rewrite reaches every operation; emitting the event
        # the way botocore does pins both the key and the wiring.
        client = clientfactory.build_client(_parse(["--region", "us-east-1"]))
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

    def test_non_s3_clients_do_not_get_it(self) -> None:
        # The event key is service-scoped: sts emits `after-call.sts.*`, which
        # the s3 registration must not reach (aws registers on `after-call.s3`).
        client = clientfactory.build_service_client("sts", _parse(["--region", "us-east-1"]))
        parsed: dict[str, Any] = {
            "Error": {"Code": "PermanentRedirect", "Message": "unchanged.", "Endpoint": "e"}
        }
        client.meta.events.emit(
            "after-call.sts.GetCallerIdentity",
            http_response=None,
            parsed=parsed,
            model=None,
            context={},
        )
        assert parsed["Error"]["Message"] == "unchanged."


class TestBuildServiceClient:
    def test_regionless_s3control_raises_no_region_keeping_the_cause(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The production seam behind the enveloped `NoRegion` report
        # (test_exit_codes.py TestUnresolvedConfigReports): s3control has no
        # global-endpoint fallback - unlike s3 and sts, which is why this is the
        # only region-less failure the mapped `aws s3` surface can reach, via
        # mv's --validate-same-s3-paths. The report's aws code is named off the
        # botocore exception the translation keeps as `__cause__`, so dropping
        # that link would silently cost the envelope; and the plain
        # ConfigurationError (not the InvalidConfigError refinement) is what
        # carries the rc.
        for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
        # No region left anywhere, so botocore would otherwise probe the EC2
        # metadata service (the last link of aws's region chain).
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        with pytest.raises(ConfigurationError) as excinfo:
            clientfactory.build_service_client("s3control", _parse([]))
        assert type(excinfo.value) is ConfigurationError
        assert isinstance(excinfo.value.__cause__, NoRegionError)
        assert str(excinfo.value) == "You must specify a region."
        assert exit_code_for(excinfo.value) == 253

    def test_timeouts_and_unsigned_are_inherited_like_from_session(self) -> None:
        # aws threads --cli-read-timeout / --cli-connect-timeout and
        # --no-sign-request through the session default client config at startup,
        # so from_session's s3control/sts clients inherit them. build_service_client
        # folds the same into the client Config.
        from botocore import UNSIGNED

        client = clientfactory.build_service_client(
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
        # build_client (botocore rejects a literal 0).
        client = clientfactory.build_service_client(
            "sts",
            _parse(["--region", "us-east-1", "--cli-read-timeout", "0"]),
            region="us-east-1",
        )
        assert client.meta.config.read_timeout is None

    def test_noninteger_timeout_maps_to_255_not_a_parse_error(self) -> None:
        # A non-integer timeout surfaces as InvalidValueError (rc 255) here too,
        # matching build_client rather than crashing client creation.
        with pytest.raises(InvalidValueError) as excinfo:
            clientfactory.build_service_client(
                "sts",
                _parse(["--region", "us-east-1", "--cli-connect-timeout", "abc"]),
                region="us-east-1",
            )
        assert exit_code_for(excinfo.value) == 255

    def test_none_region_falls_back_to_the_region_global(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws binds --region into the session at startup, so from_session's
        # region-less sts client (and the source s3control without
        # --source-region) still lands in --region; the caller's None must fall
        # back to args.region ahead of the env/config chain.
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        client = clientfactory.build_service_client(
            "sts", _parse(["--region", "eu-central-1"]), region=None
        )
        assert client.meta.region_name == "eu-central-1"


def _client_verify(client: Any) -> Any:
    """The TLS trust source a built client will actually use.

    botocore records the resolved ``verify`` on the endpoint's HTTP session;
    it is the same private attribute ``crtsupport._derive_verify`` reads to
    wire the CRT client, so asserting on it covers both engines.
    """
    return client._endpoint.http_session._verify


def _botocore_default_bundle() -> Any:
    """The CA file botocore itself would use for a client that names none."""
    from botocore.httpsession import get_cert_path

    return get_cert_path(True)


class TestMalformedS3Section:
    """A profile `s3` value that is not a section reaches aws's exact report.

    `s3 =` with no nested keys survives botocore's section provider as the
    string "" (the provider's truthy guard discards every other non-dict);
    a truthy scalar like `s3 = foo` is discarded there but still sits in the
    raw scoped config both tools read. Either way the failure has to stay
    botocore's own - the `.get` its endpoint resolution performs on the raw
    section, which is the line aws prints (rc 255 on both). Anything this
    module puts in front of that read (a `Config(s3=...)` whose merge reaches
    the string first, with `.copy`) would reword it.
    """

    def _write_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> None:
        config_file = tmp_path / "config"
        config_file.write_text(content)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))

    def test_empty_s3_section_fails_with_aws_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._write_config(tmp_path, monkeypatch, "[default]\ns3 =\n")
        with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
            clientfactory.build_client(_parse([]))

    def test_truthy_junk_s3_section_keeps_the_same_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The provider discards this value, the raw read still returns the
        # string, and both tools crash at the same `.get`.
        self._write_config(tmp_path, monkeypatch, "[default]\ns3 = foo\n")
        with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
            clientfactory.build_client(_parse([]))

    def test_the_regional_pin_does_not_reword_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The key aws v2 does not read must not be the one that fails here:
        # botocore writes its `[s3]` env overrides into the section value, and
        # into a string that is `'str' object does not support item
        # assignment`. With AWS_S3_US_EAST_1_REGIONAL_ENDPOINT set, aws still
        # reports the `.get` (measured) because its table has no such entry;
        # dropping the entry is what keeps the reports equal. Its neighbours
        # aws *does* read still report the assignment on both tools.
        self._write_config(tmp_path, monkeypatch, "[default]\ns3 =\n")
        monkeypatch.setenv("AWS_S3_US_EAST_1_REGIONAL_ENDPOINT", "regional")
        with pytest.raises(AttributeError, match="'str' object has no attribute 'get'"):
            clientfactory.build_client(_parse([]))
        monkeypatch.setenv("AWS_S3_USE_ARN_REGION", "true")
        with pytest.raises(TypeError, match="'str' object does not support item assignment"):
            clientfactory.build_client(_parse([]))


class TestVerifyResolution:
    """The TLS trust source, resolved explicitly for every CLI-built client.

    Left as ``None``, botocore resolves it per request while
    ``create_s3_crt_client`` falls back to the *platform* trust store - so the
    two transfer engines would trust different roots (design/crt.md). Every
    step below therefore has to land on a concrete value, and the same one for
    every client the run builds.
    """

    @pytest.fixture(autouse=True)
    def _no_host_ca_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The moto isolation fixture does not scrub these; a developer's own
        # AWS_CA_BUNDLE would otherwise decide the "nothing set" cases.
        monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

    def test_no_verify_ssl_disables_verification(self) -> None:
        assert _client_verify(clientfactory.build_client(_parse(["--no-verify-ssl"]))) is False

    def test_no_verify_ssl_beats_a_ca_bundle(self, tmp_path: Path) -> None:
        # aws-cli's resolve_verify_ssl only looks at --ca-bundle when
        # --no-verify-ssl is absent.
        bundle = tmp_path / "ca.pem"
        bundle.write_text("")
        argv = ["--no-verify-ssl", "--ca-bundle", str(bundle)]
        assert _client_verify(clientfactory.build_client(_parse(argv))) is False

    def test_empty_ca_bundle_flag_disables_verification(self) -> None:
        # `--ca-bundle=` is present-empty, not unset: it stops the chain
        # (measured: aws adopts it the same way, and botocore reads the empty
        # string as verification-off on both tools) instead of falling through
        # to the env or the certifi default. A truthy-`or` rewrite of the
        # chain - the b935265 / 51e7831 empty-string family - resolves the
        # default CA here instead.
        assert _client_verify(clientfactory.build_client(_parse(["--ca-bundle", ""]))) == ""

    def test_empty_aws_ca_bundle_env_stops_the_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_CA_BUNDLE", "")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "req.pem"))
        assert _client_verify(clientfactory.build_client(_parse([]))) == ""

    def test_empty_requests_ca_bundle_env_stops_the_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "")
        assert _client_verify(clientfactory.build_client(_parse([]))) == ""

    def test_ca_bundle_flag_beats_the_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bundle = tmp_path / "flag.pem"
        bundle.write_text("")
        monkeypatch.setenv("AWS_CA_BUNDLE", str(tmp_path / "env.pem"))
        client = clientfactory.build_client(_parse(["--ca-bundle", str(bundle)]))
        assert _client_verify(client) == str(bundle)

    def test_aws_ca_bundle_env_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AWS_CA_BUNDLE reaches botocore as the `ca_bundle` config variable,
        # which botocore already folded into the client at construction (so it
        # reached both engines before the explicit resolution too). Resolving
        # up front must not drop it - only the nothing-set case was ever
        # engine-dependent.
        bundle = tmp_path / "env.pem"
        bundle.write_text("")
        monkeypatch.setenv("AWS_CA_BUNDLE", str(bundle))
        assert _client_verify(clientfactory.build_client(_parse([]))) == str(bundle)

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
        assert _client_verify(clientfactory.build_client(_parse([]))) == _botocore_default_bundle()
        client = clientfactory.build_client(_parse(["--profile", "tls"]))
        assert _client_verify(client) == str(bundle)

    def test_requests_ca_bundle_env_still_applies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # botocore's own last fallback before its default bundle
        # (EndpointCreator._get_verify_value). Resolving up front must not drop
        # it, or a working classic setup would silently change trust anchors.
        bundle = tmp_path / "requests.pem"
        bundle.write_text("")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))
        assert _client_verify(clientfactory.build_client(_parse([]))) == str(bundle)

    def test_aws_ca_bundle_beats_requests_ca_bundle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_CA_BUNDLE", str(tmp_path / "aws.pem"))
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path / "requests.pem"))
        client = clientfactory.build_client(_parse([]))
        assert _client_verify(client) == str(tmp_path / "aws.pem")

    def test_nothing_set_resolves_botocore_s_own_bundle(self) -> None:
        # Not True and not None: the concrete file botocore would have opened
        # at request time (certifi's, or botocore's own where certifi is
        # absent - which is why the expectation is asked of botocore).
        verify = _client_verify(clientfactory.build_client(_parse([])))
        assert verify == _botocore_default_bundle()
        assert Path(verify).is_file()

    def test_every_client_of_a_run_resolves_the_same_value(self) -> None:
        # The CRT singleton refuses a later client whose verify differs
        # (crtsupport._is_compatible_request), so a per-client difference
        # would silently downgrade the second transfer to classic. The
        # --source-region client rides the same build_client path.
        args = _parse([])
        destination = clientfactory.build_client(args)
        source_args = argparse.Namespace(**vars(args))
        source_args.region = "eu-west-1"
        source_args.endpoint_url = None
        source = clientfactory.build_client(source_args)
        service = clientfactory.build_service_client("sts", args, region="us-east-1")
        assert _client_verify(destination) == _botocore_default_bundle()
        assert _client_verify(source) == _client_verify(destination)
        assert _client_verify(service) == _client_verify(destination)

    def test_no_verify_ssl_reaches_the_service_clients_too(self) -> None:
        client = clientfactory.build_service_client(
            "sts", _parse(["--no-verify-ssl"]), region="us-east-1"
        )
        assert _client_verify(client) is False


class TestCrtTrustSource:
    """What the resolved ``verify`` becomes once the CRT engine is wired.

    ``create_s3_crt_client`` is stubbed (the CRT cannot be exercised
    in-process); the assertion is on the kwargs it receives, following
    tests/lib/test_crtsupport.py.
    """

    @pytest.fixture(autouse=True)
    def _no_host_ca_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWS_CA_BUNDLE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)

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

    def test_cli_client_gives_the_crt_the_classic_engine_s_ca_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from boto3_s3 import crtsupport

        create_kwargs = self._stub_crt(monkeypatch)
        client = clientfactory.build_client(_parse([]))
        assert crtsupport.create_crt_transfer_manager(client, None) is not None
        [kwargs] = create_kwargs
        assert kwargs["verify"] == _botocore_default_bundle()

    def test_ca_bundle_flag_reaches_the_crt_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from boto3_s3 import crtsupport

        bundle = tmp_path / "ca.pem"
        bundle.write_text("")
        create_kwargs = self._stub_crt(monkeypatch)
        client = clientfactory.build_client(_parse(["--ca-bundle", str(bundle)]))
        assert crtsupport.create_crt_transfer_manager(client, None) is not None
        [kwargs] = create_kwargs
        assert kwargs["verify"] == str(bundle)

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
