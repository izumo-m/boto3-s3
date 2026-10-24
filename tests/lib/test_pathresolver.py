"""``boto3_s3.pathresolver``: the ``--validate-same-s3-paths`` machinery.

A faithful port of aws-cli's ``S3PathResolver`` with injected clients; these
tests pin the dispatch (which path shape talks to which API) and the error
translation that keeps a ClientError cause (CLI rc 254 - `aws` exits 254 when
GetCallerIdentity fails during validation).
"""

from __future__ import annotations

import errno
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.exceptions import InvalidConfigError as BotocoreInvalidConfigError

from boto3_s3.exceptions import (
    Boto3S3Error,
    ConfigurationError,
    InvalidConfigError,
    ValidationError,
)
from boto3_s3.pathresolver import (
    S3PathResolver,
    has_underlying_s3_path,
    is_s3express_accesspoint_path,
    is_s3express_path,
)
from tests.utils.fakes3 import client_error

_AP_ARN = "arn:aws:s3:us-west-2:123456789012:accesspoint/myaccesspoint"
_OUTPOST_ARN = (
    "arn:aws:s3-outposts:us-east-1:123456789012:outpost/op-01234567890123456/accesspoint/my-ap"
)
_MRAP_ARN = "arn:aws:s3::123456789012:accesspoint/mfzwi23gnjvgw.mrap"


class _FakeS3Control:
    def __init__(
        self,
        *,
        bucket: str = "underlying-bucket",
        mrap_pages: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        access_point: dict[str, Any] | None = None,
    ) -> None:
        self.bucket = bucket
        self.mrap_pages = list(mrap_pages or [])
        self.error = error
        self.access_point = access_point
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_access_point(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("GetAccessPoint", kwargs))
        if self.error is not None:
            raise self.error
        if self.access_point is not None:
            return self.access_point
        return {"Bucket": self.bucket}

    def list_multi_region_access_points(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("ListMultiRegionAccessPoints", kwargs))
        if self.error is not None:
            raise self.error
        return self.mrap_pages.pop(0)


class _FakeSts:
    def __init__(
        self,
        *,
        account: str = "123456789012",
        error: Exception | None = None,
        identity: dict[str, Any] | None = None,
    ) -> None:
        self.account = account
        self.error = error
        self.identity = identity
        self.calls: list[str] = []

    def get_caller_identity(self) -> dict[str, Any]:
        self.calls.append("GetCallerIdentity")
        if self.error is not None:
            raise self.error
        if self.identity is not None:
            return self.identity
        return {"Account": self.account}


class TestHasUnderlyingS3Path:
    def test_arn_and_alias_shapes(self) -> None:
        assert has_underlying_s3_path(f"s3://{_AP_ARN}/k.txt")
        assert has_underlying_s3_path(f"s3://{_OUTPOST_ARN}/k.txt")
        assert has_underlying_s3_path(f"s3://{_MRAP_ARN}/k.txt")
        assert has_underlying_s3_path("s3://my-alias-s3alias/k.txt")
        assert has_underlying_s3_path("s3://my-outpost-alias--op-s3/k.txt")
        # An S3 Express access point: aws-cli's test misses it, and a same-key move
        # onto its directory bucket deletes the object on real S3.
        assert has_underlying_s3_path("s3://my-ap--use1-az4--xa-s3/k.txt")

    def test_plain_buckets_do_not_match(self) -> None:
        assert not has_underlying_s3_path("s3://plain-bucket/k.txt")
        assert not has_underlying_s3_path("s3://bucket-s3alias-not-suffix/k.txt")
        # Scheme-less input splits the same way (split_bucket_key handles both).
        assert not has_underlying_s3_path("plain-bucket/k.txt")


class TestIsS3ExpressPath:
    def test_only_the_scheme_qualified_suffix_matches(self) -> None:
        # The bucket suffix, but only under the s3:// scheme: raw positionals
        # may be local paths, and a local name can plausibly end in --x-s3.
        assert is_s3express_path("s3://mybkt--use1-az4--x-s3/k.txt")
        assert is_s3express_path("s3://mybkt--use1-az4--x-s3")
        assert not is_s3express_path("s3://plain-bucket/k.txt")
        assert not is_s3express_path("s3://plain-bucket/inner--x-s3")  # key, not bucket
        assert not is_s3express_path("./backup--x-s3")
        assert not is_s3express_path("backup--x-s3")
        assert not is_s3express_path("-")


class TestIsS3ExpressAccessPointPath:
    def test_the_access_point_suffix_matches_under_the_scheme(self) -> None:
        # An access point of a directory bucket (accessPointSuffix --xa-s3),
        # under the s3:// scheme as for the bucket probe.
        assert is_s3express_accesspoint_path("s3://myap--use1-az4--xa-s3/k.txt")
        assert is_s3express_accesspoint_path("s3://myap--use1-az4--xa-s3")
        assert not is_s3express_accesspoint_path("s3://mybkt--use1-az4--x-s3/k.txt")
        assert not is_s3express_accesspoint_path("s3://plain-bucket/inner--xa-s3")
        assert not is_s3express_accesspoint_path("./backup--xa-s3")
        assert not is_s3express_accesspoint_path("-")

    def test_the_bucket_probe_keeps_its_narrower_answer(self) -> None:
        # aws-cli's is_s3express_bucket tests only --x-s3, and the sync
        # rejection mirrors it; the access point probe is a separate question.
        assert not is_s3express_path("s3://myap--use1-az4--xa-s3/k.txt")


class TestResolve:
    @pytest.mark.parametrize("which", ["s3control", "sts"])
    def test_a_client_with_an_empty_ca_bundle_is_refused(self, which: str) -> None:
        # aws-cli refuses the pair when it builds them; the resolver refuses a
        # caller-built one at construction, before any request.
        from types import SimpleNamespace

        empty = SimpleNamespace(_endpoint=SimpleNamespace(http_session=SimpleNamespace(_verify="")))
        clients: dict[str, Any] = {"s3control_client": _FakeS3Control(), "sts_client": _FakeSts()}
        clients[f"{which}_client"] = empty
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle") as exc_info:
            S3PathResolver(**clients)
        assert type(exc_info.value) is InvalidConfigError
        assert exc_info.value.operation == "mv"
        assert isinstance(exc_info.value.__cause__, BotocoreInvalidConfigError)

    def test_an_empty_aws_ca_bundle_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The pair is handed over, so the variable their credential clients
        # resolve first is checked too.
        monkeypatch.setenv("AWS_CA_BUNDLE", "")
        with pytest.raises(InvalidConfigError, match="Invalid CA bundle"):
            S3PathResolver(s3control_client=_FakeS3Control(), sts_client=_FakeSts())

    def test_plain_bucket_passes_through_without_any_call(self) -> None:
        s3control = _FakeS3Control()
        sts = _FakeSts()
        resolver = S3PathResolver(s3control_client=s3control, sts_client=sts)
        assert resolver.resolve_underlying_s3_paths("s3://plain/k.txt") == ["s3://plain/k.txt"]
        assert s3control.calls == []
        assert sts.calls == []

    def test_accesspoint_arn_resolves_via_get_access_point(self) -> None:
        s3control = _FakeS3Control(bucket="real-bucket")
        resolver = S3PathResolver(s3control_client=s3control, sts_client=_FakeSts())
        assert resolver.resolve_underlying_s3_paths(f"s3://{_AP_ARN}/d/k.txt") == [
            "s3://real-bucket/d/k.txt"
        ]
        assert s3control.calls == [
            ("GetAccessPoint", {"AccountId": "123456789012", "Name": "myaccesspoint"})
        ]

    def test_outpost_arn_passes_the_whole_arn_as_name(self) -> None:
        s3control = _FakeS3Control(bucket="outpost-bucket")
        resolver = S3PathResolver(s3control_client=s3control, sts_client=_FakeSts())
        assert resolver.resolve_underlying_s3_paths(f"s3://{_OUTPOST_ARN}/k.txt") == [
            "s3://outpost-bucket/k.txt"
        ]
        assert s3control.calls == [
            ("GetAccessPoint", {"AccountId": "123456789012", "Name": _OUTPOST_ARN})
        ]

    def test_alias_asks_sts_for_the_account(self) -> None:
        s3control = _FakeS3Control(bucket="aliased-bucket")
        sts = _FakeSts(account="999988887777")
        resolver = S3PathResolver(s3control_client=s3control, sts_client=sts)
        assert resolver.resolve_underlying_s3_paths("s3://my-ap-s3alias/k.txt") == [
            "s3://aliased-bucket/k.txt"
        ]
        assert sts.calls == ["GetCallerIdentity"]
        assert s3control.calls == [
            ("GetAccessPoint", {"AccountId": "999988887777", "Name": "my-ap-s3alias"})
        ]

    def test_s3express_access_point_resolves_like_an_alias(self) -> None:
        # GetAccessPoint takes an S3 Express access point's name and answers
        # with its directory bucket (measured on real S3); aws-cli does not
        # resolve this shape at all.
        s3control = _FakeS3Control(bucket="dir--use1-az4--x-s3")
        sts = _FakeSts(account="999988887777")
        resolver = S3PathResolver(s3control_client=s3control, sts_client=sts)
        assert resolver.resolve_underlying_s3_paths("s3://my-ap--use1-az4--xa-s3/k.txt") == [
            "s3://dir--use1-az4--x-s3/k.txt"
        ]
        assert sts.calls == ["GetCallerIdentity"]
        assert s3control.calls == [
            ("GetAccessPoint", {"AccountId": "999988887777", "Name": "my-ap--use1-az4--xa-s3"})
        ]

    def test_s3express_access_point_on_a_predating_botocore_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A botocore before 1.37.24 sends the lookup to the plain s3control
        # endpoint, which answers NoSuchAccessPoint for an access point that
        # exists (measured on 1.37.23): the floor message instead, no request.
        from boto3_s3 import s3storage

        monkeypatch.setattr(s3storage, "version", lambda _name: "1.37.23")
        s3control = _FakeS3Control(bucket="dir--use1-az4--x-s3")
        sts = _FakeSts()
        resolver = S3PathResolver(s3control_client=s3control, sts_client=sts)
        for path in (
            "s3://my-ap--use1-az4--xa-s3/k.txt",
            # The ARN spelling too: it would take the access point ARN route.
            "s3://arn:aws:s3:us-east-1:123456789012:accesspoint/my-ap--use1-az4--xa-s3/k.txt",
        ):
            with pytest.raises(ConfigurationError, match=r"1\.37\.24") as excinfo:
                resolver.resolve_underlying_s3_paths(path)
            assert excinfo.value.operation == "mv"
        assert (s3control.calls, sts.calls) == ([], [])

    def test_mrap_fans_out_per_region_across_pages(self) -> None:
        pages = [
            {"AccessPoints": [{"Alias": "other.mrap", "Regions": []}], "NextToken": "t1"},
            {
                "AccessPoints": [
                    {
                        "Alias": "mfzwi23gnjvgw.mrap",
                        "Regions": [{"Bucket": "bucket-east"}, {"Bucket": "bucket-west"}],
                    }
                ]
            },
        ]
        s3control = _FakeS3Control(mrap_pages=pages)
        resolver = S3PathResolver(s3control_client=s3control, sts_client=_FakeSts())
        assert resolver.resolve_underlying_s3_paths(f"s3://{_MRAP_ARN}/k.txt") == [
            "s3://bucket-east/k.txt",
            "s3://bucket-west/k.txt",
        ]
        assert [call[0] for call in s3control.calls] == ["ListMultiRegionAccessPoints"] * 2
        assert s3control.calls[1][1] == {"AccountId": "123456789012", "NextToken": "t1"}

    def test_mrap_not_found_is_a_validation_error(self) -> None:
        s3control = _FakeS3Control(mrap_pages=[{"AccessPoints": []}])
        resolver = S3PathResolver(s3control_client=s3control, sts_client=_FakeSts())
        with pytest.raises(ValidationError) as excinfo:
            resolver.resolve_underlying_s3_paths(f"s3://{_MRAP_ARN}/k.txt")
        assert "mfzwi23gnjvgw.mrap" in str(excinfo.value)  # names the alias it looked for

    def test_outpost_alias_is_unresolvable(self) -> None:
        s3control = _FakeS3Control()
        resolver = S3PathResolver(s3control_client=s3control, sts_client=_FakeSts())
        with pytest.raises(ValidationError) as excinfo:
            resolver.resolve_underlying_s3_paths("s3://my-outpost--op-s3/k.txt")
        assert excinfo.value.operation == "mv"
        assert s3control.calls == []

    def test_client_errors_translate_with_the_cause_kept(self) -> None:
        # The CLI maps a kept ClientError cause to rc 254 - what `aws` exits
        # when the validation calls themselves fail.
        sts = _FakeSts(error=client_error("InvalidClientTokenId", 403, "GetCallerIdentity"))
        resolver = S3PathResolver(s3control_client=_FakeS3Control(), sts_client=sts)
        with pytest.raises(Boto3S3Error) as excinfo:
            resolver.resolve_underlying_s3_paths("s3://my-ap-s3alias/k.txt")
        assert isinstance(excinfo.value.__cause__, ClientError)
        assert "InvalidClientTokenId" in str(excinfo.value)

    def test_whatever_else_the_request_raised_is_the_base_error(self) -> None:
        # A refreshing credential_process whose program is gone raises
        # outside botocore's family; `aws` prints the text.
        s3control = _FakeS3Control(
            error=FileNotFoundError(errno.ENOENT, "No such file or directory", "/bin/helper")
        )
        resolver = S3PathResolver(s3control_client=s3control, sts_client=_FakeSts())
        with pytest.raises(Boto3S3Error) as excinfo:
            resolver.resolve_underlying_s3_paths(f"s3://{_AP_ARN}/k.txt")
        assert type(excinfo.value) is Boto3S3Error
        assert isinstance(excinfo.value.__cause__, FileNotFoundError)
        assert excinfo.value.operation == "mv"
