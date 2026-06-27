"""S3 storage backend: ``S3Storage`` (an ``s3://bucket/prefix`` + boto3 client).

Implements ``scan`` (``ListObjectsV2``; ``ListBuckets`` at the bare-``s3://``
service root) over an overridable ``scan_pages`` seam - subclasses customize
per-page entry handling there while keeping scan's prefetch - and exposes
``get_client`` / ``bucket`` / ``key`` so the ``Transferrer`` can drive
``s3transfer`` directly for built-in S3 pairs. ``delete`` is implemented (a
blind ``DeleteObject``); ``open`` is intentionally unimplemented - S3 always
transfers through ``s3transfer`` (built-in pairs and the S3 side of an open-route
custom-backend transfer alike), so no route calls it. The only thing it would
add is direct programmatic S3 stream access, which nothing needs today (see
:meth:`S3Storage.open`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    NoRegionError,
    ParamValidationError,
    PartialCredentialsError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)
from typing_extensions import override

from boto3_s3.exceptions import (
    AccessDeniedError,
    Boto3S3Error,
    ConfigurationError,
    NotFoundError,
    TransportError,
    ValidationError,
)
from boto3_s3.naming import split_bucket_key
from boto3_s3.storage import Storage, StorageCapability
from boto3_s3.types import FileInfo, FileKind, S3FileInfo, ScanOptions

if TYPE_CHECKING:
    from typing import BinaryIO

    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ListBucketsOutputTypeDef, ListObjectsV2OutputTypeDef

# S3Storage.open is intentionally unimplemented - every S3 transfer rides
# s3transfer instead. The Transferrer drives it straight off get_client/bucket/
# key for built-in pairs and the S3 side of an open-route custom transfer, and
# streaming hands a fileobj to s3transfer (s3.py _cp_stream) - so nothing inside
# boto3-s3 calls it (the custom side of an open-route transfer uses its own open,
# never this).
_OPEN_NOT_IMPLEMENTED = (
    "S3Storage.open() is not implemented. S3 transfers go through s3transfer "
    "(driven from get_client/bucket/key) - including the S3 side of an open-route "
    "custom-backend transfer, whose custom side uses its own Storage.open - so "
    "this generic per-object stream primitive has no caller. Implementing it "
    "(GetObject->readable, multipart PutObject->writable committed on close) would "
    "only add direct programmatic S3 stream access (see storage.py)."
)

_CONFIG_ERRORS: tuple[type[BaseException], ...] = (
    NoCredentialsError,
    PartialCredentialsError,
    NoRegionError,
)
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (EndpointConnectionError, BotoConnectionError)

# S3 error Code -> exception category, shared by every translation path: the
# request-level ClientError below and the per-key DeleteObjects ``Errors[]``
# entries in deleter.py (which carry no HTTP status to widen on). The code
# states the intent more precisely than the status, so it is consulted first;
# status-based widening is the fallback.
S3_CODE_CATEGORIES: dict[str, type[Boto3S3Error]] = {
    "AccessDenied": AccessDeniedError,
    "NoSuchBucket": NotFoundError,
    "NoSuchKey": NotFoundError,
    "NoSuchVersion": NotFoundError,
    "NotFound": NotFoundError,
    "InternalError": TransportError,
    "SlowDown": TransportError,
    "ServiceUnavailable": TransportError,
    "RequestTimeout": TransportError,
}


# Resource types the S3 data plane cannot serve through these operations;
# ``aws s3`` rejects them at parse time (ParamValidation -> rc 252) and the
# exit-code charter (docs/overview.md section 3) has us reject them the same way.
_S3_OBJECT_LAMBDA_ARN_RE = re.compile(
    r"^(?P<bucket>arn:(aws).*:s3-object-lambda:[a-z\-0-9]+:[0-9]{12}:"
    r"accesspoint[/:][a-zA-Z0-9\-]{1,63})[/:]?(?P<key>.*)$"
)
_S3_OUTPOST_BUCKET_ARN_RE = re.compile(
    r"^(?P<bucket>arn:(aws).*:s3-outposts:[a-z\-0-9]+:[0-9]{12}:outpost[/:]"
    r"[a-zA-Z0-9\-]{1,63}[/:]bucket[/:]"
    r"[a-zA-Z0-9\-]{1,63})[/:]?(?P<key>.*)$"
)


def _parse_s3_url(url: str) -> tuple[str, str]:
    """Split an ``s3://bucket/key`` URL into ``(bucket, key)`` - no validation.

    Either part may be empty: a bare ``"s3://"`` is the service root (bucket
    listing), and ``"s3:///k"`` parses to an empty bucket. Access-point ARNs
    (plain and Outposts) stay whole in ``bucket`` - the ARN name may itself
    contain ``/`` (aws-cli's ``find_bucket_key``, ported as
    ``naming.split_bucket_key``). The strict aws-cli checks - the unsupported S3
    Object Lambda / Outposts *bucket* ARN forms, and a key with no bucket - are
    deferred to :meth:`S3Storage.validate`, so construction itself never raises.
    """
    rest = url.partition("://")[2]
    return split_bucket_key(rest)


def _translate_client_error(
    exc: ClientError, *, operation: str, bucket: str | None, key: str | None
) -> Boto3S3Error:
    """Map a botocore ``ClientError`` to the matching ``Boto3S3Error`` category.

    The error code is consulted first (:data:`S3_CODE_CATEGORIES`); the HTTP
    status widens codes not in the table. The message is the ClientError's
    full str - the "An error occurred (...)" line aws-cli prints - so these
    read the same as the per-key delete failures synthesized in deleter.py.
    """
    response: Any = exc.response
    code: Any = response.get("Error", {}).get("Code", "")
    status: Any = response.get("ResponseMetadata", {}).get("HTTPStatusCode")

    category: type[Boto3S3Error] | None = S3_CODE_CATEGORIES.get(code)
    if category is None:
        if status == 403:
            category = AccessDeniedError
        elif status == 404:
            category = NotFoundError
        elif isinstance(status, int) and 500 <= status < 600:
            category = TransportError
        elif isinstance(status, int) and 400 <= status < 500:
            category = ValidationError
        else:
            category = Boto3S3Error
    return category(str(exc), operation=operation, bucket=bucket, key=key)


def translate_boto_error(
    exc: BaseException, *, operation: str, bucket: str | None = None, key: str | None = None
) -> Boto3S3Error:
    """Map any transfer-path exception to the matching ``Boto3S3Error``.

    ``ClientError`` goes through the code/status table; botocore's credential,
    transport, and request-shape errors map to their categories
    (``ParamValidationError`` is client-side validation - no HTTP happened -
    which aws-cli files under its usage rc). An existing ``Boto3S3Error``
    passes through unchanged, and anything else - an ``OSError`` raised by
    local file I/O inside s3transfer, say - becomes the base category carrying
    its message (the ``[Errno 21] Is a directory`` text aws prints for a
    directory source survives verbatim).
    """
    if isinstance(exc, Boto3S3Error):
        return exc
    if isinstance(exc, ClientError):
        return _translate_client_error(exc, operation=operation, bucket=bucket, key=key)
    if isinstance(exc, _CONFIG_ERRORS):
        return ConfigurationError(str(exc), operation=operation, bucket=bucket, key=key)
    if isinstance(exc, _TRANSPORT_ERRORS):
        return TransportError(str(exc), operation=operation, bucket=bucket, key=key)
    if isinstance(exc, ParamValidationError):
        return ValidationError(str(exc), operation=operation, bucket=bucket, key=key)
    return Boto3S3Error(str(exc), operation=operation, bucket=bucket, key=key)


@contextmanager
def s3_errors(
    *, operation: str, bucket: str | None = None, key: str | None = None
) -> Generator[None, None, None]:
    """Convert botocore errors from an S3 call into ``Boto3S3Error`` (keeping ``__cause__``)."""
    try:
        yield
    except (ClientError, BotoCoreError) as exc:
        raise translate_boto_error(exc, operation=operation, bucket=bucket, key=key) from exc


def _page_to_infos(
    page: ListObjectsV2OutputTypeDef, *, recursive: bool, prefix: str
) -> list[FileInfo]:
    """Convert one ``ListObjectsV2`` page into ``FileInfo`` items (no I/O).

    Runs on the prefetch worker thread. Non-recursive listings emit one
    ``DIRECTORY``-kind entry per ``CommonPrefixes`` entry (before the page's
    objects); every object becomes a ``FILE``-kind ``S3FileInfo``. ``owner`` reads
    the canonical ``Owner["ID"]`` (present only when listed with ``FetchOwner``).
    ``compare_key`` is stamped as ``key[len(prefix):]`` - ``prefix`` is the listing
    ``Prefix``, so every object key and common-prefix starts with it, and the
    slice is the root-relative key operations and custom filters match against.
    """
    infos: list[FileInfo] = []
    if not recursive:
        for common in page.get("CommonPrefixes", []):
            dir_prefix = common.get("Prefix")
            if dir_prefix is not None:
                infos.append(
                    S3FileInfo(
                        key=dir_prefix,
                        kind=FileKind.DIRECTORY,
                        compare_key=dir_prefix[len(prefix) :],
                    )
                )
    for obj in page.get("Contents", []):
        key = obj.get("Key")
        size = obj.get("Size")
        mtime = obj.get("LastModified")
        if key is None or size is None or mtime is None:
            continue  # ListObjectsV2 always populates these; stay defensive
        etag = obj.get("ETag")
        owner = obj.get("Owner")
        infos.append(
            S3FileInfo(
                key=key,
                size=size,
                mtime=mtime,
                etag=etag.strip('"') if etag else None,
                storage_class=obj.get("StorageClass"),
                owner=owner.get("ID") if owner else None,
                compare_key=key[len(prefix) :],
            )
        )
    return infos


def _page_to_bucket_infos(page: ListBucketsOutputTypeDef) -> list[FileInfo]:
    """Convert one ``ListBuckets`` page into ``BUCKET``-kind ``FileInfo`` items (no I/O).

    Runs on the prefetch worker thread. Each bucket becomes an ``S3FileInfo``
    whose ``key`` is the bucket name and whose ``mtime`` is the bucket's
    ``CreationDate`` (what ``aws s3 ls`` prints next to the name). The service
    root has no prefix, so ``compare_key`` is the bucket name itself.
    """
    infos: list[FileInfo] = []
    for bucket in page.get("Buckets", []):
        name = bucket.get("Name")
        if name is None:
            continue  # ListBuckets always populates Name; stay defensive
        infos.append(
            S3FileInfo(
                key=name, kind=FileKind.BUCKET, mtime=bucket.get("CreationDate"), compare_key=name
            )
        )
    return infos


class S3Storage(Storage):
    """An S3 bucket/prefix as one side of a transfer.

    Wraps an ``s3://bucket/prefix`` location together with the boto3 S3 client
    used to reach it. The ``s3://`` scheme is optional in the constructor:
    ``S3Storage("bucket/key")`` is read the same as ``S3Storage("s3://bucket/key")``
    (intentional library leniency; :meth:`S3.resolve` stays strict and routes a
    bare ``"bucket/key"`` to local instead). An empty bucket part (bare ``"s3://"``) is the
    *service root*: :meth:`scan` then lists the account's buckets instead of
    objects; a key without a bucket (``"s3:///k"``) is rejected by
    :meth:`validate`. The bucket part may be an access-point ARN (plain or
    Outposts), which is passed whole as the ``Bucket`` parameter; S3 Object Lambda
    and Outposts bucket ARNs are rejected like ``aws s3`` rejects them (by
    :meth:`validate`, deferred from construction). When
    ``client`` is omitted, a default ``boto3.client("s3")``
    is built lazily on first use and owned by this instance (released by
    :meth:`close`). The region is not derived from the URL; for a specific
    region / endpoint / profile, pass a pre-built ``client``.

    Thread safety: a built or supplied client is safe to share across threads for
    operation calls. Client *construction* is not, and is deliberately not locked
    here (a library lock cannot serialize against clients the caller builds
    elsewhere). For concurrent use, build the client at a safe time on the caller
    side and pass it in rather than relying on the lazy default.
    """

    scheme: ClassVar[str] = "s3"
    #: S3 resolves a single object (HEAD), enumerates in native UTF-8 byte order
    #: (``ListObjectsV2``), and deletes; ``open`` is intentionally unimplemented
    #: (S3 rides ``s3transfer``), so no ``OPEN_*`` (see :meth:`open`).
    capabilities: ClassVar[StorageCapability] = (
        StorageCapability.GET_FILEINFO
        | StorageCapability.SCAN
        | StorageCapability.SORTED_SCAN
        | StorageCapability.DELETE
    )

    def __init__(self, url: str | os.PathLike[str], *, client: S3Client | None = None) -> None:
        text = os.fspath(url)
        # Explicit construction means "this is an S3 location", so the s3:// scheme
        # is optional here for convenience: "bucket/key" reads the same as
        # "s3://bucket/key" (mirrors aws-cli ls/presign/website). S3.resolve stays
        # strict on purpose (a bare "bucket/key" routes to local), so cp/mv/sync can
        # still tell bare local paths from S3.
        if not text.startswith("s3://"):
            text = f"s3://{text}"
        self._url = text
        self._bucket, self._key = _parse_s3_url(text)
        self._client = client
        self._owns_client = client is None

    @property
    def url(self) -> str:
        return self._url

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def key(self) -> str:
        """The key or prefix part of the URL (may be empty)."""
        return self._key

    @override
    def as_text(self) -> str:
        """Reconstruct the ``s3://bucket/key`` token (:meth:`Storage.as_text`).

        Rebuilt from :attr:`bucket` / :attr:`key`, not from the raw constructor
        input, so a keyless location normalizes to a slashless ``s3://bucket``
        (and the bare service root to ``s3://``) - exactly the token a raw
        ``s3://bucket`` argument carries into ``naming``.
        """
        if self._key:
            return f"s3://{self._bucket}/{self._key}"
        return f"s3://{self._bucket}"

    @override
    def validate(self) -> None:
        """Reject the resource forms ``aws s3`` rejects at parse time (rc 252).

        Deferred from construction (:meth:`Storage.validate`): S3 Object Lambda
        and Outposts *bucket* ARNs (s3api / s3control territory), and a key with
        no bucket (``"s3:///k"``). The library calls this before an operation and
        the CLI at its parity-correct point, so a malformed location fails loud
        instead of reaching the API as a cryptic botocore error. Idempotent.
        """
        rest = self._url.partition("://")[2]
        if _S3_OBJECT_LAMBDA_ARN_RE.match(rest):
            raise ValidationError(
                "s3 commands do not support S3 Object Lambda resources. Use s3api commands instead."
            )
        if _S3_OUTPOST_BUCKET_ARN_RE.match(rest):
            raise ValidationError(
                "s3 commands do not support Outpost Bucket ARNs. Use s3control commands instead."
            )
        if not self._bucket and self._key:
            raise ValidationError(f"s3:// URL has a key but no bucket: {self._url!r}")

    def get_client(self) -> S3Client:
        """Return the boto3 S3 client, building a default one lazily if omitted.

        Memoized: the first call builds (or returns the supplied) client and
        every later call returns the same instance. Deliberately not guarded by
        a lock; build the client on the caller side for concurrent use.
        """
        if self._client is None:
            # Deferred: only the default-client fallback needs boto3, and
            # importing it pulls in s3transfer too (import contract,
            # docs/imports.md). Callers passing a client never load it here.
            import boto3

            self._client = boto3.client("s3")
        return self._client

    def close(self) -> None:
        """Close the lazily-built default client, if this instance owns one."""
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    @override
    def scan_pages(self, options: ScanOptions) -> Iterator[list[FileInfo]]:
        """Yield one ``list[FileInfo]`` per ``ListObjectsV2`` page (paginated).

        ``options.recursive`` omits ``Delimiter`` and yields every object as a
        ``FILE`` entry; non-recursive passes ``Delimiter='/'`` and additionally
        emits one ``DIRECTORY``-kind ``S3FileInfo`` per sub-"directory" (before the
        page's objects). ``FileInfo.key`` is the full S3 key (or the prefix for
        directories), in ListObjectsV2's UTF-8 lexicographic byte order across
        pages - so a recursive stream is directly merge-joinable (the basis of
        ``sync``). ``options.fetch_owner`` sends ``FetchOwner=True`` to populate
        ``S3FileInfo.owner``.

        At the service root (empty bucket part) the pages come from
        ``ListBuckets`` instead - one ``BUCKET``-kind entry per bucket, filtered
        by ``options.bucket_name_prefix`` / ``options.bucket_region``;
        ``recursive`` / ``request_payer`` / ``fetch_owner`` are ignored there,
        and the two bucket filters are ignored for object listings (aws-cli
        parity: ``aws s3 ls`` with no bucket disregards ``--recursive``).

        This is the override seam (see :meth:`Storage.scan_pages`): subclass and
        filter / enrich each page, then ``super().scan_pages(options)``; the work
        runs on :meth:`Storage.scan`'s prefetch worker so it overlaps consumption.
        Fetch-time botocore errors are translated to ``Boto3S3Error`` here and
        surface on the consumer's pull.
        """
        if not self._bucket:
            yield from self._scan_bucket_pages(options)
            return
        paginator = self.get_client().get_paginator("list_objects_v2")
        paging: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": self._key,
            "PaginationConfig": {"PageSize": options.page_size},
        }
        if not options.recursive:
            paging["Delimiter"] = "/"
        if options.request_payer is not None:
            paging["RequestPayer"] = options.request_payer
        if options.fetch_owner:
            paging["FetchOwner"] = True
        with s3_errors(operation="ls", bucket=self._bucket):
            for page in paginator.paginate(**paging):
                yield _page_to_infos(page, recursive=options.recursive, prefix=self._key)

    def _scan_bucket_pages(self, options: ScanOptions) -> Iterator[list[FileInfo]]:
        """Yield one ``list[FileInfo]`` per ``ListBuckets`` page (service root).

        ``bucket_name_prefix`` / ``bucket_region`` map to ``Prefix`` /
        ``BucketRegion`` and are omitted when falsy (matching aws-cli's
        truthiness check, so ``""`` behaves like "not given").
        """
        client = self.get_client()
        with s3_errors(operation="ls"):
            if not client.can_paginate("list_buckets"):
                # Back-compat (floor botocore 1.31, docs/overview.md section 2):
                # the ListBuckets paginator and its Prefix / BucketRegion /
                # MaxBuckets parameters are a late-2024 addition (botocore
                # 1.34.162). Below that, get_paginator("list_buckets") raises
                # OperationNotPageableError, so fall back to a single
                # unpaginated list_buckets() - the era-appropriate aws s3 ls,
                # where the two bucket filters were silently inert. Drop this
                # branch once the botocore floor reaches 1.34.162.
                yield _page_to_bucket_infos(client.list_buckets())
                return
            paginator = client.get_paginator("list_buckets")
            paging: dict[str, Any] = {"PaginationConfig": {"PageSize": options.page_size}}
            if options.bucket_name_prefix:
                paging["Prefix"] = options.bucket_name_prefix
            if options.bucket_region:
                paging["BucketRegion"] = options.bucket_region
            for page in paginator.paginate(**paging):
                yield _page_to_bucket_infos(page)

    @override
    def open(self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None) -> BinaryIO:
        """Intentionally unimplemented - every S3 transfer rides ``s3transfer``.

        No route reaches this: S3<->local / S3<->S3 transfers are driven through
        ``s3transfer`` off ``get_client`` / ``bucket`` / ``key`` (``transfer.py``),
        a stdin/stdout stream is handed to ``s3transfer`` as a fileobj (``s3.py``
        ``_cp_stream``), and the S3 side of an open-route custom-backend transfer
        likewise rides ``s3transfer`` (the *custom* side uses its own ``open``,
        never this). Implementing it (GetObject -> readable stream; multipart
        PutObject -> writable stream committed on ``close()``, honoring the
        ``size`` hint) would only add direct programmatic S3 stream access, which
        nothing needs today. It raises rather than silently misbehaving.
        """
        raise NotImplementedError(_OPEN_NOT_IMPLEMENTED)

    @override
    def delete(self, key: str, *, request_payer: str | None = None) -> None:
        """Delete one object with a single blind ``DeleteObject`` call.

        Blind like ``aws s3 rm``'s single-key path: no listing and no
        HeadObject - deleting a key that does not exist succeeds (S3 returns
        204). ``request_payer`` is an S3-specific knob added on top of the
        cross-backend ``Storage.delete(key)`` signature.
        """
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if request_payer is not None:
            kwargs["RequestPayer"] = request_payer
        with s3_errors(operation="delete", bucket=self._bucket, key=key):
            self.get_client().delete_object(**kwargs)

    @override
    def get_fileinfo(
        self,
        key: str = "",
        *,
        follow_symlinks: bool = True,
        on_warning: Callable[[str], None] | None = None,
    ) -> S3FileInfo | None:
        """HeadObject a single key (:meth:`Storage.get_fileinfo`).

        ``key`` is relative to this storage's location: ``""`` heads
        :attr:`key`, a non-empty ``key`` an entry beneath it. A ``404`` returns
        ``None`` (definitively absent); any other error (``403``, transport, 5xx)
        is raised - existence could not be determined. ``follow_symlinks`` /
        ``on_warning`` do not apply to S3 and are ignored. This is the generic
        HEAD; the SSE-C-aware single-source HEAD lives in the transfer engine.
        """
        target_key = self._key + key
        try:
            with s3_errors(operation="head", bucket=self._bucket, key=target_key):
                head = self.get_client().head_object(Bucket=self._bucket, Key=target_key)
        except NotFoundError:
            return None
        etag = head.get("ETag")
        return S3FileInfo(
            key=target_key,
            size=head.get("ContentLength"),
            mtime=head.get("LastModified"),
            etag=etag.strip('"') if etag else None,
            storage_class=head.get("StorageClass"),
            head=head,
            compare_key=target_key.rsplit("/", 1)[-1],
        )


__all__ = ["S3Storage", "s3_errors", "translate_boto_error"]
