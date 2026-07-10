"""Resolve access-point-shaped S3 paths to their underlying buckets.

A faithful port of aws-cli's ``S3PathResolver`` (aws-cli
``awscli/customizations/s3/utils.py``), the machinery behind
``aws s3 mv --validate-same-s3-paths``: an access point ARN or alias, an
S3 on Outposts access point ARN, or a Multi-Region Access Point ARN can hide
the bucket a path really lands in, so ``mv`` would copy an object onto
itself and then delete it. Resolving first lets a caller compare the real
``s3://bucket/key`` pairs (``S3Storage.same_path``) before moving anything.

Following the library's connection model, the resolver takes its
``s3control`` and ``sts`` clients from the caller (build them with the
region/verify/profile wiring of your choice - aws builds the s3control
client in the path's region and the sts client without one); only
``has_underlying_s3_path`` is callable without any client. aws-cli's
``from_session`` constructor is deliberately not ported.

This module stays SDK-free at import time (docs/imports.md): botocore is
touched only when a resolution actually calls a client.
"""

from __future__ import annotations

import re
from typing import Any

from boto3_s3.exceptions import ValidationError

# aws-cli regexes, verbatim. ``has_underlying_s3_path`` and the resolution
# dispatch share them; the suffix forms (-s3alias / --op-s3) have no regex.
_S3_ACCESSPOINT_ARN_TO_ACCOUNT_NAME_REGEX = re.compile(
    r"^arn:aws.*:s3:[a-z0-9\-]+:(?P<account>[0-9]{12}):accesspoint[:/](?P<name>[a-z0-9\-]{3,50})$"
)
_S3_OUTPOST_ACCESSPOINT_ARN_TO_ACCOUNT_REGEX = re.compile(
    r"^arn:aws.*:s3-outposts:[a-z0-9\-]+:(?P<account>[0-9]{12}):outpost/"
    r"op-[a-zA-Z0-9]+/accesspoint[:/][a-z0-9\-]{3,50}$"
)
_S3_MRAP_ARN_TO_ACCOUNT_ALIAS_REGEX = re.compile(
    r"^arn:aws:s3::(?P<account>[0-9]{12}):accesspoint[:/](?P<alias>[a-zA-Z0-9]+\.mrap)$"
)


def has_underlying_s3_path(path: str) -> bool:
    """Whether *path*'s bucket part may resolve to a different real bucket.

    True for the three ARN shapes above and for the access-point alias
    suffixes (``-s3alias``, Outposts ``--op-s3``). Pure string inspection -
    this is what gates aws's "may resolve to same underlying s3 object(s)"
    warning when validation is off.
    """
    bucket, _key = _split_bucket_key(path)
    return bool(
        _S3_ACCESSPOINT_ARN_TO_ACCOUNT_NAME_REGEX.match(bucket)
        or _S3_OUTPOST_ACCESSPOINT_ARN_TO_ACCOUNT_REGEX.match(bucket)
        or _S3_MRAP_ARN_TO_ACCOUNT_ALIAS_REGEX.match(bucket)
        or bucket.endswith("-s3alias")
        or bucket.endswith("--op-s3")
    )


def _split_bucket_key(path: str) -> tuple[str, str]:
    """Scheme-stripped ``(bucket, key)`` via the S3 grammar on ``S3Storage``.

    Deferred import: ``s3storage`` top-imports ``botocore.exceptions``, and this
    module must stay SDK-free at import time (its docstring's contract) - the
    same pattern as ``_api_errors`` below.
    """
    from boto3_s3.s3storage import S3Storage

    return S3Storage.split_bucket_key(S3Storage.strip_scheme(path))


class S3PathResolver:
    """Turn access-point-shaped paths into their real ``s3://bucket/key`` forms.

    ``s3control_client`` answers ``GetAccessPoint`` /
    ``ListMultiRegionAccessPoints``; ``sts_client`` supplies the account id
    when an alias carries none. Both are used lazily - a path with a plain
    bucket name resolves to itself without any API call.
    """

    def __init__(self, s3control_client: Any, sts_client: Any) -> None:
        self._s3control_client = s3control_client
        self._sts_client = sts_client

    def resolve_underlying_s3_paths(self, path: str) -> list[str]:
        """All ``s3://bucket/key`` forms *path* may land in (aws-cli logic).

        An MRAP fans out to one path per region; everything else resolves to
        a single path. The Outposts access point *alias* cannot be resolved
        (no API exists) and raises aws's usage-shaped error; a plain bucket
        path comes back unchanged.
        """
        bucket, key = _split_bucket_key(path)
        match = _S3_ACCESSPOINT_ARN_TO_ACCOUNT_NAME_REGEX.match(bucket)
        if match:
            return self._resolve_accesspoint_arn(match.group("account"), match.group("name"), key)
        match = _S3_OUTPOST_ACCESSPOINT_ARN_TO_ACCOUNT_REGEX.match(bucket)
        if match:
            # The Outposts GetAccessPoint takes the whole ARN as its Name.
            return self._resolve_accesspoint_arn(match.group("account"), bucket, key)
        match = _S3_MRAP_ARN_TO_ACCOUNT_ALIAS_REGEX.match(bucket)
        if match:
            return self._resolve_mrap_alias(match.group("account"), match.group("alias"), key)
        if bucket.endswith("-s3alias"):
            return self._resolve_accesspoint_alias(bucket, key)
        if bucket.endswith("--op-s3"):
            raise ValidationError(
                "Can't resolve underlying bucket name of s3 outposts "
                "access point alias. Use arn instead to resolve the "
                "bucket name and validate the mv command.",
                operation="mv",
            )
        return [path]

    def _resolve_accesspoint_arn(self, account: str, name: str, key: str) -> list[str]:
        bucket = self._get_access_point_bucket(account, name)
        return [f"s3://{bucket}/{key}"]

    def _resolve_accesspoint_alias(self, alias: str, key: str) -> list[str]:
        account = self._get_account_id()
        bucket = self._get_access_point_bucket(account, alias)
        return [f"s3://{bucket}/{key}"]

    def _resolve_mrap_alias(self, account: str, alias: str, key: str) -> list[str]:
        buckets = self._get_mrap_buckets(account, alias)
        return [f"s3://{bucket}/{key}" for bucket in buckets]

    def _get_access_point_bucket(self, account: str, name: str) -> str:
        with self._api_errors():
            return self._s3control_client.get_access_point(AccountId=account, Name=name)["Bucket"]

    def _get_account_id(self) -> str:
        with self._api_errors():
            return self._sts_client.get_caller_identity()["Account"]

    def _get_mrap_buckets(self, account: str, alias: str) -> list[str]:
        next_token: str | None = None
        while True:
            args: dict[str, Any] = {"AccountId": account}
            if next_token:
                args["NextToken"] = next_token
            with self._api_errors():
                response = self._s3control_client.list_multi_region_access_points(**args)
            for access_point in response["AccessPoints"]:
                if access_point["Alias"] == alias:
                    return [region["Bucket"] for region in access_point["Regions"]]
            next_token = response.get("NextToken")
            if not next_token:
                raise ValidationError(
                    "Couldn't find multi-region access point "
                    f"with alias {alias} in account {account}",
                    operation="mv",
                )

    @staticmethod
    def _api_errors() -> Any:
        """Translate botocore errors, keeping ``__cause__`` (CLI rc 254).

        aws lets the raw ClientError escape to its generic 254 handler
        (a failing GetCallerIdentity exits 254); the
        library shape for that is a translated ``Boto3S3Error`` with
        the ClientError as its cause. Deferred import: resolving a plain
        bucket path must not load botocore.
        """
        from boto3_s3.s3storage import s3_errors

        return s3_errors(operation="mv")


__all__ = ["S3PathResolver", "has_underlying_s3_path"]
