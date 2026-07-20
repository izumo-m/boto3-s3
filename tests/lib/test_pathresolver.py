"""``boto3_s3.pathresolver``: the ``--validate-same-s3-paths`` machinery.

A faithful port of aws-cli's ``S3PathResolver`` with injected clients; these
tests pin the dispatch (which path shape talks to which API), the exact
aws-cli error wordings, and the error translation that keeps a ClientError
cause (CLI rc 254 - aws exits 254 when GetCallerIdentity fails during
validation).
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from boto3_s3.exceptions import Boto3S3Error, ValidationError
from boto3_s3.pathresolver import S3PathResolver, has_underlying_s3_path, is_mrap_path

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
    ) -> None:
        self.bucket = bucket
        self.mrap_pages = list(mrap_pages or [])
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_access_point(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("GetAccessPoint", kwargs))
        if self.error is not None:
            raise self.error
        return {"Bucket": self.bucket}

    def list_multi_region_access_points(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("ListMultiRegionAccessPoints", kwargs))
        if self.error is not None:
            raise self.error
        return self.mrap_pages.pop(0)


class _FakeSts:
    def __init__(self, *, account: str = "123456789012", error: Exception | None = None) -> None:
        self.account = account
        self.error = error
        self.calls: list[str] = []

    def get_caller_identity(self) -> dict[str, Any]:
        self.calls.append("GetCallerIdentity")
        if self.error is not None:
            raise self.error
        return {"Account": self.account}


def _client_error(code: str, operation: str) -> ClientError:
    response: Any = {
        "Error": {"Code": code, "Message": "stub"},
        "ResponseMetadata": {"HTTPStatusCode": 403},
    }
    return ClientError(response, operation)


class TestHasUnderlyingS3Path:
    def test_arn_and_alias_shapes(self) -> None:
        assert has_underlying_s3_path(f"s3://{_AP_ARN}/k.txt")
        assert has_underlying_s3_path(f"s3://{_OUTPOST_ARN}/k.txt")
        assert has_underlying_s3_path(f"s3://{_MRAP_ARN}/k.txt")
        assert has_underlying_s3_path("s3://my-alias-s3alias/k.txt")
        assert has_underlying_s3_path("s3://my-outpost-alias--op-s3/k.txt")

    def test_plain_buckets_do_not_match(self) -> None:
        assert not has_underlying_s3_path("s3://plain-bucket/k.txt")
        assert not has_underlying_s3_path("s3://bucket-s3alias-not-suffix/k.txt")
        # Scheme-less input splits the same way (split_bucket_key handles both).
        assert not has_underlying_s3_path("plain-bucket/k.txt")


class TestIsMrapPath:
    def test_only_the_mrap_arn_shape_matches(self) -> None:
        # The SigV4a stand-down (the CLI's clientfactory) keys on exactly the
        # MRAP shape: plain and Outposts access points sign symmetric SigV4
        # like a bucket, so they must not lift the pin.
        assert is_mrap_path(f"s3://{_MRAP_ARN}/k.txt")
        assert is_mrap_path(f"s3://{_MRAP_ARN}")
        assert not is_mrap_path(f"s3://{_AP_ARN}/k.txt")
        assert not is_mrap_path(f"s3://{_OUTPOST_ARN}/k.txt")
        assert not is_mrap_path("s3://my-alias-s3alias/k.txt")
        assert not is_mrap_path("s3://plain-bucket/k.txt")

    def test_non_s3_strings_never_match(self) -> None:
        # The CLI probes raw positionals before route validation: local paths
        # and the stream sentinel must fall out quietly.
        assert not is_mrap_path("./local/file.txt")
        assert not is_mrap_path("-")


class TestResolve:
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

    def test_mrap_not_found_uses_the_awscli_wording(self) -> None:
        s3control = _FakeS3Control(mrap_pages=[{"AccessPoints": []}])
        resolver = S3PathResolver(s3control_client=s3control, sts_client=_FakeSts())
        with pytest.raises(ValidationError) as excinfo:
            resolver.resolve_underlying_s3_paths(f"s3://{_MRAP_ARN}/k.txt")
        assert str(excinfo.value) == (
            "Couldn't find multi-region access point "
            "with alias mfzwi23gnjvgw.mrap in account 123456789012"
        )

    def test_outpost_alias_is_unresolvable(self) -> None:
        s3control = _FakeS3Control()
        resolver = S3PathResolver(s3control_client=s3control, sts_client=_FakeSts())
        with pytest.raises(ValidationError) as excinfo:
            resolver.resolve_underlying_s3_paths("s3://my-outpost--op-s3/k.txt")
        assert str(excinfo.value) == (
            "Can't resolve underlying bucket name of s3 outposts "
            "access point alias. Use arn instead to resolve the "
            "bucket name and validate the mv command."
        )
        assert s3control.calls == []

    def test_client_errors_translate_with_the_cause_kept(self) -> None:
        # The CLI maps a kept ClientError cause to rc 254 - what aws exits
        # when the validation calls themselves fail.
        sts = _FakeSts(error=_client_error("InvalidClientTokenId", "GetCallerIdentity"))
        resolver = S3PathResolver(s3control_client=_FakeS3Control(), sts_client=sts)
        with pytest.raises(Boto3S3Error) as excinfo:
            resolver.resolve_underlying_s3_paths("s3://my-ap-s3alias/k.txt")
        assert isinstance(excinfo.value.__cause__, ClientError)
        assert "InvalidClientTokenId" in str(excinfo.value)
