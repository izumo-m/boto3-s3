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
from boto3_s3.pathresolver import (
    S3PathResolver,
    has_underlying_s3_path,
    is_mrap_path,
    is_outpost_alias_path,
    is_outpost_path,
    is_s3express_path,
)
from tests.utils.fakes3 import client_error

_AP_ARN = "arn:aws:s3:us-west-2:123456789012:accesspoint/myaccesspoint"
_OUTPOST_ARN = (
    "arn:aws:s3-outposts:us-east-1:123456789012:outpost/op-01234567890123456/accesspoint/my-ap"
)
_MRAP_ARN = "arn:aws:s3::123456789012:accesspoint/mfzwi23gnjvgw.mrap"

# The slots the S3 endpoint rules read an Outposts access point alias with, all
# counted from the end of the bucket name: the 17-character outpost id sits 49
# to 32 characters from the end, the hardware type is the 50th from the end,
# and the name ends in ``--op-s3``.
_ALIAS_OUTPOST_ID = "0b1d075431d83bebd"
_ALIAS_TAIL = "e8xz5w8ijx1qzlbp3i3kuse10--op-s3"


def _outpost_alias(
    hardware_type: str = "o",
    *,
    prefix: str = "test-accessp-",
    outpost_id: str = _ALIAS_OUTPOST_ID,
) -> str:
    """A real-shaped Outposts alias (63 characters) with one slot swapped out."""
    return f"{prefix}{hardware_type}{outpost_id}{_ALIAS_TAIL}"


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
        # One of the two SigV4a shapes the CLI's clientfactory stands its pin
        # down for; `is_outpost_path` below is the other. A plain,
        # region-qualified access point signs symmetric SigV4 like a bucket and
        # must not lift the pin.
        assert is_mrap_path(f"s3://{_MRAP_ARN}/k.txt")
        assert is_mrap_path(f"s3://{_MRAP_ARN}")
        assert not is_mrap_path(f"s3://{_AP_ARN}/k.txt")
        assert not is_mrap_path(f"s3://{_OUTPOST_ARN}/k.txt")
        assert not is_mrap_path("s3://my-alias-s3alias/k.txt")
        assert not is_mrap_path("s3://plain-bucket/k.txt")

    @pytest.mark.parametrize(
        "alias",
        [
            "mfzwi23gnjvgw.mrap",
            "MFZWI23GNJVGW.mrap",
            "my-alias.mrap",
            "my.alias.mrap",
            # botocore never looks for the .mrap suffix: an empty ARN region on
            # an accesspoint resource is the whole test, so any alias spelling
            # resolves SigV4a and the pin has to stand down for it.
            "plainname",
            "plainname.notmrap",
        ],
    )
    def test_every_alias_spelling_botocore_signs_sigv4a(self, alias: str) -> None:
        # Measured against aws 2.36.40: `presign arn:aws:s3::<account>:
        # accesspoint/<alias>/key` answers AWS4-ECDSA-P256-SHA256 with
        # X-Amz-Region-Set=* for each of these. The aws-cli resolver regex
        # above accepts only the first two, which is why the gate reads the
        # ARN structurally instead.
        arn = f"arn:aws:s3::123456789012:accesspoint/{alias}"
        assert is_mrap_path(f"s3://{arn}/k.txt")
        assert is_mrap_path(f"s3://{arn.replace('accesspoint/', 'accesspoint:')}/k.txt")

    @pytest.mark.parametrize(
        "path",
        [
            # A region field turns the same resource into a regional access
            # point, which signs plain SigV4 - the pin must stay on.
            "s3://arn:aws:s3:us-west-2:123456789012:accesspoint/mfzwi23gnjvgw.mrap/k.txt",
            "s3://arn:aws:s3-object-lambda::123456789012:accesspoint/my.mrap/k.txt",
            "s3://arn:aws:s3::123456789012:notaccesspoint/my.mrap/k.txt",
            "s3://arn:aws:s3::123456789012:accesspoint",
            "s3://arn:aws:s3::123456789012/k.txt",
            "s3://arn:aws::us-west-2:123456789012:accesspoint/my.mrap/k.txt",
            "s3://arn:aws:s3::123456789012:/k.txt",
        ],
    )
    def test_near_miss_arns_keep_the_pin(self, path: str) -> None:
        assert not is_mrap_path(path)

    def test_non_s3_strings_never_match(self) -> None:
        # The CLI probes raw positionals before route validation: local paths
        # and the stream sentinel must fall out quietly.
        assert not is_mrap_path("./local/file.txt")
        assert not is_mrap_path("-")


class TestIsOutpostPath:
    def test_only_the_outpost_arn_shape_matches(self) -> None:
        # The second SigV4a stand-down shape: an Outposts access point resolves
        # to asymmetric SigV4a, measured against aws 2.36.40 (its presign signs
        # AWS4-ECDSA-P256-SHA256). An Outposts access point *alias* is a plain
        # bucket name rather than an ARN, so this probe does not answer for it -
        # `is_outpost_alias_path` does.
        assert is_outpost_path(f"s3://{_OUTPOST_ARN}/k.txt")
        assert is_outpost_path(f"s3://{_OUTPOST_ARN}")
        assert is_outpost_path(f"s3://{_OUTPOST_ARN.replace('accesspoint/', 'accesspoint:')}/k")
        assert is_outpost_path(f"s3://{_OUTPOST_ARN.replace('arn:aws:', 'arn:aws-us-gov:')}/k")
        assert not is_outpost_path(f"s3://{_AP_ARN}/k.txt")
        assert not is_outpost_path(f"s3://{_MRAP_ARN}/k.txt")
        assert not is_outpost_path("s3://my-alias--op-s3/k.txt")
        assert not is_outpost_path("s3://plain-bucket/k.txt")

    @pytest.mark.parametrize(
        "resource",
        [
            "outpost/op-01234567890123456/accesspoint/myap",
            # botocore folds ":" into "/" before splitting the resource, so
            # every separator mix is the same ARN to it.
            "outpost:op-01234567890123456:accesspoint:myap",
            "outpost:op-01234567890123456/accesspoint/myap",
            "outpost/op-01234567890123456:accesspoint:myap",
            # ... and it constrains neither the outpost id nor the access
            # point name the way the aws-cli resolver regex does.
            "outpost/op-0123-4567890123456/accesspoint/myap",
            "outpost/op-01234567890123456/accesspoint/MyAP",
            "outpost/op-01234567890123456/accesspoint/ab",
            f"outpost/op-01234567890123456/accesspoint/{'a' * 63}",
        ],
    )
    def test_every_arn_spelling_botocore_signs_sigv4a(self, resource: str) -> None:
        # Measured against aws 2.36.40: each of these presigns
        # AWS4-ECDSA-P256-SHA256 with a region-less credential scope.
        arn = f"arn:aws:s3-outposts:us-west-2:123456789012:{resource}"
        assert is_outpost_path(f"s3://{arn}/k.txt")

    @pytest.mark.parametrize(
        "path",
        [
            # An Outposts *bucket* ARN and a bare accesspoint resource are not
            # access points on an outpost; neither is an outpost resource under
            # another service. All keep the pin.
            "s3://arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-0123/bucket/mybkt/k.txt",
            "s3://arn:aws:s3-outposts:us-west-2:123456789012:accesspoint/myap/k.txt",
            "s3://arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-0123/k.txt",
            "s3://arn:aws:s3:us-west-2:123456789012:outpost/op-0123/accesspoint/myap/k.txt",
            "s3://arn:aws:s3-outposts:us-west-2:123456789012:OUTPOST/op-0123/accesspoint/ap/k",
            "s3://arn:aws:s3-outposts:us-west-2:123456789012:outpost/op-0123/ACCESSPOINT/ap/k",
            "s3://arn:aws::us-west-2:123456789012:outpost/op-0123/accesspoint/myap/k.txt",
        ],
    )
    def test_near_miss_arns_keep_the_pin(self, path: str) -> None:
        assert not is_outpost_path(path)

    def test_non_s3_strings_never_match(self) -> None:
        assert not is_outpost_path("./local/file.txt")
        assert not is_outpost_path("-")


class TestIsOutpostAliasPath:
    def test_only_the_scheme_qualified_alias_matches(self) -> None:
        # An Outposts access point *alias* reaches the same SigV4a endpoint as
        # the ARN does, so it is a stand-down shape of its own. The s3://
        # scheme is required for the reason `is_s3express_path` requires it:
        # raw positionals may be local paths, and a local name can end in
        # --op-s3.
        alias = _outpost_alias()
        assert is_outpost_alias_path(f"s3://{alias}/k.txt")
        assert is_outpost_alias_path(f"s3://{alias}")
        assert not is_outpost_alias_path(alias)
        assert not is_outpost_alias_path(f"./{alias}")
        assert not is_outpost_alias_path(f"s3://plain-bucket/{alias}")  # key, not bucket
        assert not is_outpost_alias_path("s3://plain-bucket/k.txt")
        assert not is_outpost_alias_path(f"s3://{_OUTPOST_ARN}/k.txt")
        assert not is_outpost_alias_path("-")

    @pytest.mark.parametrize(
        "alias",
        [
            _outpost_alias("o"),
            _outpost_alias("e"),
            # 50 characters is the shortest name the rules can read a hardware
            # type out of, and the outpost id may carry dashes or be all digits.
            _outpost_alias("o", prefix=""),
            _outpost_alias("e", prefix=""),
            _outpost_alias("o", prefix="a"),
            _outpost_alias("o", outpost_id="0b1d0-5431d83-bcd"),
            _outpost_alias("e", outpost_id="01234567890123456"),
            _outpost_alias("e", prefix="my-access-pt-", outpost_id="op-1234567890abcd"),
        ],
    )
    def test_every_alias_spelling_botocore_signs_sigv4a(self, alias: str) -> None:
        # Measured against aws 2.36.40: each of these presigns
        # AWS4-ECDSA-P256-SHA256 with a region-less credential scope and
        # X-Amz-Region-Set=*.
        assert is_outpost_alias_path(f"s3://{alias}/k.txt")

    @pytest.mark.parametrize(
        "bucket",
        [
            # One character short of the 50 the hardware-type slice needs.
            _outpost_alias("o", prefix="")[1:],
            # The aliases an earlier survey sampled: both too short to enter
            # the branch, which is why the suffix alone looked harmless.
            "myendpoint-100000000000-z0afiba4--op-s3",
            "myap-1234.op-01234567890123456--op-s3",
            # Full length, but the last 7 characters are not the suffix.
            _outpost_alias()[:-7] + "-op-s3x",
            _outpost_alias()[:-7] + "--op-s4",
            "--op-s3",
            "a" + "b" * 61 + "c",
        ],
    )
    def test_names_outside_the_branch_keep_the_pin(self, bucket: str) -> None:
        # Measured against aws 2.36.40: each of these presigns plain
        # AWS4-HMAC-SHA256, so dropping the pin would diverge.
        assert not is_outpost_alias_path(f"s3://{bucket}/k.txt")

    @pytest.mark.parametrize(
        "bucket",
        [
            # A hardware type the rules do not know.
            _outpost_alias("x"),
            # Outpost ids that are not host labels.
            _outpost_alias(outpost_id="0b1d07543_d83bebd"),
            _outpost_alias(outpost_id="-b1d075431d83bebd"),
            # Names that are not virtual hostable: a dot, an uppercase letter,
            # and one character past the 63-character host label limit.
            _outpost_alias(prefix="test.accessp-"),
            _outpost_alias(prefix="Test-accessp-"),
            _outpost_alias(prefix="test-accesspt-"),
        ],
    )
    def test_shapes_the_branch_refuses_never_claim_sigv4a(self, bucket: str) -> None:
        # aws resolves no endpoint at all for these (the alias branch raises
        # rather than falling through to the plain-bucket rules), so the pin is
        # unobservable - but the probe still answers the question it is asked.
        assert not is_outpost_alias_path(f"s3://{bucket}/k.txt")

    def test_a_beta_alias_still_enters_the_branch(self) -> None:
        # The rules read a "beta" region prefix out of the same name and then
        # demand a custom endpoint; the auth scheme is SigV4a either way, so
        # the region prefix is not part of the stand-down test.
        beta = _outpost_alias()[:-12] + "beta0--op-s3"
        assert is_outpost_alias_path(f"s3://{beta}/k.txt")


class TestIsS3ExpressPath:
    def test_only_the_scheme_qualified_suffix_matches(self) -> None:
        # The sigv4-s3express stand-down (the CLI's clientfactory) keys on the
        # bucket suffix, but only under the s3:// scheme: raw positionals may
        # be local paths, and a local name can plausibly end in --x-s3.
        assert is_s3express_path("s3://mybkt--use1-az4--x-s3/k.txt")
        assert is_s3express_path("s3://mybkt--use1-az4--x-s3")
        assert not is_s3express_path("s3://plain-bucket/k.txt")
        assert not is_s3express_path("s3://plain-bucket/inner--x-s3")  # key, not bucket
        assert not is_s3express_path("./backup--x-s3")
        assert not is_s3express_path("backup--x-s3")
        assert not is_s3express_path("-")


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
        sts = _FakeSts(error=client_error("InvalidClientTokenId", 403, "GetCallerIdentity"))
        resolver = S3PathResolver(s3control_client=_FakeS3Control(), sts_client=sts)
        with pytest.raises(Boto3S3Error) as excinfo:
            resolver.resolve_underlying_s3_paths("s3://my-ap-s3alias/k.txt")
        assert isinstance(excinfo.value.__cause__, ClientError)
        assert "InvalidClientTokenId" in str(excinfo.value)
