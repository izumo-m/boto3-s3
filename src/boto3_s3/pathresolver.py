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
client in the path's region and the sts client without one); the pure
string probes - ``has_underlying_s3_path`` / ``is_mrap_path`` /
``is_outpost_path`` / ``is_outpost_alias_path`` / ``is_s3express_path`` - are
callable without any client. aws-cli's
``from_session`` constructor is deliberately not ported.

This module stays SDK-free at import time (design/imports.md): the first path
*split* lazily pulls in ``s3storage`` (and with it ``botocore.exceptions``),
and a client is touched only when a resolution actually calls one.
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

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

# botocore's ``VALID_HOST_LABEL_RE``, verbatim: the RFC 1123 label the S3
# endpoint rules test an Outposts alias with (`_is_host_label`).
_HOST_LABEL_REGEX = re.compile(r"^(?!-)[a-zA-Z\d-]{1,63}(?<!-)$")


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


def is_mrap_path(path: str) -> bool:
    """Whether *path*'s bucket part is a Multi-Region Access Point ARN.

    Pure string inspection like `has_underlying_s3_path`, but read through
    `_parse_arn_bucket` instead of the resolver regex above, because the two
    answer different questions. What makes an ARN an MRAP to botocore is an
    empty region field on an ``s3`` ``accesspoint`` resource - the ``.mrap``
    suffix and the strictly alphanumeric alias the resolver regex demands
    describe the aliases AWS hands out today, not what the endpoint rules
    test. An MRAP request must be signed with asymmetric SigV4a (its region
    set is `*`), which an explicit symmetric `signature_version` in the client
    config would suppress - callers that pin one (the CLI's always-SigV4 pin)
    use this to stand down for MRAP targets.
    """
    arn = _parse_arn_bucket(_split_bucket_key(path)[0])
    if arn is None:
        return False
    return (
        arn.service == "s3"
        and arn.region == ""
        and len(arn.resource_id) > 1
        and arn.resource_id[0] == "accesspoint"
        and arn.resource_id[1] != ""
    )


def is_outpost_path(path: str) -> bool:
    """Whether *path*'s bucket part is an S3 Outposts access-point ARN.

    Pure string inspection like `is_mrap_path`, and structural for the same
    reason: botocore resolves asymmetric SigV4a for an Outposts access point
    (its region set is `*`, and the credential scope drops the region), which
    an explicit symmetric `signature_version` would suppress. Because
    `_parse_arn_bucket` folds ``:`` into ``/`` before splitting the resource,
    ``outpost/op-1/accesspoint/ap``, ``outpost:op-1:accesspoint:ap`` and every
    mix of the two are one shape here, and neither the outpost id nor the
    access point name is constrained - all spellings the resolver regex above
    turns away but botocore signs SigV4a.
    """
    arn = _parse_arn_bucket(_split_bucket_key(path)[0])
    if arn is None:
        return False
    return (
        arn.service == "s3-outposts"
        and len(arn.resource_id) > 3
        and arn.resource_id[0] == "outpost"
        and arn.resource_id[2] == "accesspoint"
    )


def is_outpost_alias_path(path: str) -> bool:
    """Whether an ``s3://`` path names an S3 Outposts access point *alias*.

    The alias carries no ARN, so the endpoint rules recognise it by position
    instead, reading fixed-width slices counted from the end of the bucket
    name: the last 7 characters are the ``--op-s3`` suffix, the 50th from the
    end is the hardware type (``o`` for an Outposts rack, ``e`` for an EC2
    server), and the 17 characters before that are the outpost id, which has
    to be a valid host label. The whole name must also be virtual hostable,
    and the slices need an ASCII name of at least 50 characters - a shorter
    one makes botocore's ``substring`` yield nothing, which drops the branch.
    Anything the branch turns away is an ordinary bucket to the rules and is
    signed plain SigV4, so the stand-down has to reproduce the position test
    rather than look at the suffix alone (`has_underlying_s3_path` above does
    test the bare suffix, but that answers ``mv``'s separate question of which
    spellings might hide another bucket).

    Like `is_s3express_path`, and unlike the ARN probes, the ``s3://`` scheme
    is required: a local path can plausibly end in ``--op-s3``. A resolved
    alias endpoint carries the same asymmetric SigV4a as the Outposts ARN
    (region set `*`), which an explicit symmetric `signature_version` would
    suppress - callers that pin one (the CLI's always-SigV4 pin) stand down
    for it exactly as they do for `is_outpost_path`.
    """
    if not path.startswith("s3://"):
        return False
    bucket, _key = _split_bucket_key(path)
    # ``substring(Bucket, 49, 50, reverse)`` is the branch's longest reach, and
    # every ``substring`` the rules take is ASCII-only; either miss yields
    # nothing, and a rule condition that yields nothing fails.
    if len(bucket) < 50 or not bucket.isascii():
        return False
    hardware_type = bucket[-50:-49]  # substring(Bucket, 49, 50, reverse)
    outpost_id = bucket[-49:-32]  # substring(Bucket, 32, 49, reverse)
    return (
        bucket.endswith("--op-s3")  # substring(Bucket, 0, 7, reverse)
        and hardware_type in ("o", "e")
        and _is_host_label(outpost_id)
        and _is_virtual_hostable_bucket(bucket)
    )


def is_s3express_path(path: str) -> bool:
    """Whether an ``s3://`` path names an S3 Express directory bucket
    (aws-cli's ``is_s3express_bucket``: the ``--x-s3`` suffix).

    Pure string inspection like `is_mrap_path`, with one extra gate: the
    ``s3://`` scheme is required, because callers feed raw positionals that
    may be local paths and a local name can plausibly end in ``--x-s3``
    (an MRAP ARN shape cannot). A directory-bucket request must be signed
    ``sigv4-s3express`` with `CreateSession` credentials, which an explicit
    `signature_version` in the client config would suppress - callers that
    pin one (the CLI's always-SigV4 pin) use this to stand down, like
    `is_mrap_path` for SigV4a.
    """
    if not path.startswith("s3://"):
        return False
    bucket, _key = _split_bucket_key(path)
    return bucket.endswith("--x-s3")


def _split_bucket_key(path: str) -> tuple[str, str]:
    """Scheme-stripped ``(bucket, key)`` via the S3 grammar on ``S3Storage``.

    Deferred import: ``s3storage`` top-imports ``botocore.exceptions``, and this
    module must stay SDK-free at import time (its docstring's contract) - the
    same pattern as ``_api_errors`` below.
    """
    from boto3_s3.s3storage import S3Storage

    return S3Storage.split_bucket_key(S3Storage.strip_scheme(path))


class _ArnBucket(NamedTuple):
    """The ARN fields the S3 endpoint rules branch on when picking a signer.

    ``resource_id`` is the resource split botocore's way: ``:`` folded to
    ``/`` first, so one resource notation covers every separator mix.
    """

    service: str
    region: str
    resource_id: list[str]


def _parse_arn_bucket(bucket: str) -> _ArnBucket | None:
    """Read *bucket* as an ARN the way botocore's endpoint rules do.

    botocore's ``aws.parseArn`` ruleset function (``ArnParser`` plus the
    ``resourceId`` split): six ``:``-separated fields with the resource taking
    everything after the fifth colon, and partition / service / resource all
    required. None means the auth scheme cannot come from an ARN at all.

    The stand-down probes above read this rather than the aws-cli regexes at
    the top of the module, since the endpoint rules are what actually decide
    SigV4 against SigV4a; the regexes answer ``mv``'s separate question of
    which spellings it can resolve to an underlying bucket, and are narrower.
    """
    if not bucket.startswith("arn:"):
        return None
    fields = bucket.split(":", 5)
    if len(fields) < 6:
        return None
    partition, service, region, _account, resource = fields[1:]
    if not partition or not service or not resource:
        return None
    return _ArnBucket(service, region, resource.replace(":", "/").split("/"))


def _is_host_label(value: str) -> bool:
    """botocore's ``isValidHostLabel(value, allowSubdomains=false)``.

    The RFC 1123 label the endpoint rules test with: 1 to 63 letters, digits
    and dashes, with no dash at either end and no dot anywhere. Every call
    sits behind `is_outpost_alias_path`'s ASCII check, so the pattern's digit
    class cannot pick up a non-ASCII digit here.
    """
    return _HOST_LABEL_REGEX.match(value) is not None


def _is_virtual_hostable_bucket(bucket: str) -> bool:
    """botocore's ``aws.isVirtualHostableS3Bucket(bucket, allowSubdomains=false)``.

    A host label - which is what caps an alias at 63 characters and rules out
    the dotted spellings - with no uppercase and at least 3 characters.
    botocore also turns away a name shaped like an IPv4 address; that test is
    subsumed, since such a name needs the dots the host label already refuses.
    """
    return len(bucket) >= 3 and bucket == bucket.lower() and _is_host_label(bucket)


class S3PathResolver:
    """Turn access-point-shaped paths into their real ``s3://bucket/key`` forms.

    ``s3control_client`` answers ``GetAccessPoint`` /
    ``ListMultiRegionAccessPoints``; ``sts_client`` supplies the account id
    when an alias carries none. Both are used lazily - a path with a plain
    bucket name resolves to itself without any API call.
    """

    def __init__(self, *, s3control_client: Any, sts_client: Any) -> None:
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
        the ClientError as its cause. Deferred import: keeps this module
        SDK-free at import time (the path split already lazily loads
        ``s3storage`` the same way, so by resolution time it is warm).
        """
        from boto3_s3.s3storage import s3_errors

        return s3_errors(operation="mv")


__all__ = [
    "S3PathResolver",
    "has_underlying_s3_path",
    "is_mrap_path",
    "is_outpost_alias_path",
    "is_outpost_path",
    "is_s3express_path",
]
