"""Unit tests for boto3_s3_cli.clientfactory (global options -> boto3 client)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from boto3_s3 import Boto3S3Error, InvalidConfigError, InvalidValueError, ValidationError
from boto3_s3_cli import clientfactory, globalargs
from boto3_s3_cli.cli import exit_code_for


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    globalargs.add_common_arguments(parser)
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

        s3 = clientfactory.build_s3(args)

        assert s3.session is not None
        assert s3.session.profile_name == "bound"
        assert s3.client().meta.endpoint_url == "http://localhost:9000"
        assert s3.aws_config().get_str("s3.multipart_threshold") == "17"

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
        monkeypatch.setenv("AWS_RETRY_MODE", "legacy")
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "7")
        client = clientfactory.build_client(_parse([]))
        assert client.meta.config.retries == {"mode": "legacy", "total_max_attempts": 7}

    def test_empty_retry_env_is_present_and_fatal_like_aws(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Present-wins, like the profile/region chains: aws treats an empty
        # AWS_MAX_ATTEMPTS / AWS_RETRY_MODE as a fatal value (rc 255), never
        # as unset. int("") -> ValueError (main's backstop maps it to 255);
        # an empty mode fails botocore's retry-config validation at client
        # creation -> InvalidConfigError (255).
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "")
        with pytest.raises(ValueError, match="invalid literal"):
            clientfactory.build_client(_parse([]))
        monkeypatch.delenv("AWS_MAX_ATTEMPTS")
        monkeypatch.setenv("AWS_RETRY_MODE", "")
        with pytest.raises(InvalidConfigError):
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


class TestBuildServiceClient:
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
