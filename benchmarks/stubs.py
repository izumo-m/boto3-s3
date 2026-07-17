"""Canned S3 responses for the in-process mode, wired at botocore's send layer.

The responder registers on the ``before-send.s3`` event of a real boto3
client: a handler that returns an ``AWSResponse`` makes botocore skip the
socket send while request serialization, signing, and response parsing all
still run - exactly the per-request CPU work boto3-s3 pays in production.
(Replacing ``_make_api_call``, as the test recorder does, would cut those
out of the measurement.)

Every handler drains the request body before answering. The wire send this
stub replaces is what would otherwise consume an upload's file stream, so
without the drain a configuration that never reads the body during signing
would silently drop the local-read/chunking cost out of the measured path.

The responder is stateless per request (the listing pages are pre-rendered,
immutable bytes), so s3transfer worker threads may call it concurrently.
"""

from __future__ import annotations

import io
import re
import urllib.parse
from typing import TYPE_CHECKING, Any

from botocore.awsrequest import AWSResponse

from benchmarks import workload
from benchmarks.core import BenchmarkError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_XMLNS = "http://s3.amazonaws.com/doc/2006-03-01/"

# Far-future LastModified for stubbed listings: a sync against a real local
# tree must judge every remote object newer than its local file, so the
# no-op scenarios stay no-ops without touching local mtimes.
_LAST_MODIFIED = "2100-01-01T00:00:00.000Z"

_DRAIN_CHUNK = 1024 * 1024

_KEY_RE = re.compile(rb"<Key>([^<]*)</Key>")


class _RawResponse(io.BytesIO):
    """A bytes-backed body with the ``stream()`` iface ``AWSResponse.raw`` needs."""

    def stream(self, **_kwargs: Any) -> Any:
        contents = self.read()
        while contents:
            yield contents
            contents = self.read()


def _drain_body(request: Any, *, keep: bool = False) -> bytes:
    """Read the request body to exhaustion, standing in for the skipped send.

    Returns the bytes only when *keep* is set (DeleteObjects echoes them);
    large upload bodies are discarded chunk by chunk so nothing accumulates.
    """
    body = request.body
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body if keep else b""
    if isinstance(body, str):
        return body.encode() if keep else b""
    parts: list[bytes] = []
    while True:
        chunk = body.read(_DRAIN_CHUNK)
        if not chunk:
            break
        if keep:
            parts.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(parts)


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _response(
    request: Any, *, status: int = 200, headers: dict[str, str] | None = None, body: bytes = b""
) -> AWSResponse:
    all_headers = {"Content-Length": str(len(body))}
    if body:
        all_headers["Content-Type"] = "application/xml"
    if headers:
        all_headers.update(headers)
    return AWSResponse(
        url=request.url, status_code=status, headers=all_headers, raw=_RawResponse(body)
    )


class ListingCorpus:
    """Pre-rendered ``ListObjectsV2`` pages describing one synthetic key set.

    Rendering happens once at scenario setup, so the timed path pays only the
    dict-free page lookup plus botocore's own XML parsing - the same parse
    cost a real listing response carries. The continuation token encodes the
    next page index (``page-<n>``), a pure function of the request, which is
    what keeps the responder stateless.
    """

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str,
        count: int,
        size: int,
        fanout: int = 256,
        page_size: int = 1000,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.count = count
        self.size = size
        keys = workload.keys_for(prefix, count, fanout)
        self.pages: list[bytes] = []
        for start in range(0, max(len(keys), 1), page_size):
            chunk = keys[start : start + page_size]
            truncated = start + page_size < len(keys)
            next_token = f"page-{start // page_size + 1}" if truncated else None
            self.pages.append(self._render_page(chunk, truncated, next_token))

    def _render_page(self, keys: Sequence[str], truncated: bool, next_token: str | None) -> bytes:
        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<ListBucketResult xmlns="{_XMLNS}">',
            f"<Name>{_xml_escape(self.bucket)}</Name>",
            f"<Prefix>{_xml_escape(self.prefix)}</Prefix>",
            f"<KeyCount>{len(keys)}</KeyCount>",
            "<MaxKeys>1000</MaxKeys>",
            f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>",
        ]
        if next_token is not None:
            parts.append(f"<NextContinuationToken>{next_token}</NextContinuationToken>")
        for key in keys:
            parts.append(
                "<Contents>"
                f"<Key>{_xml_escape(key)}</Key>"
                f"<LastModified>{_LAST_MODIFIED}</LastModified>"
                "<ETag>&quot;benchetag&quot;</ETag>"
                f"<Size>{self.size}</Size>"
                "<StorageClass>STANDARD</StorageClass>"
                "</Contents>"
            )
        parts.append("</ListBucketResult>")
        return "".join(parts).encode()

    def page_for(self, request: Any) -> bytes:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.url).query)
        token = query.get("continuation-token", [None])[0]
        index = int(token.removeprefix("page-")) if token else 0
        return self.pages[index]


class S3Responder:
    """Answer the S3 operations the benchmarked commands issue, off the wire.

    Any operation without a handler raises loudly: an unstubbed call means
    the scenario exercises a path the stub does not model, and the failure
    must surface as a harness bug rather than a retried network error.
    """

    def __init__(self, *, corpus: ListingCorpus | None = None) -> None:
        self._corpus = corpus
        self._handlers: dict[str, Callable[[Any], AWSResponse]] = {
            "ListObjectsV2": self._list_objects_v2,
            "PutObject": self._put_object,
            "DeleteObjects": self._delete_objects,
            "CreateMultipartUpload": self._create_multipart_upload,
            "UploadPart": self._upload_part,
            "CompleteMultipartUpload": self._complete_multipart_upload,
            "AbortMultipartUpload": self._abort_multipart_upload,
        }

    def register(self, client: Any) -> None:
        client.meta.events.register("before-send.s3", self)

    def __call__(self, request: Any, event_name: str = "", **_kwargs: Any) -> AWSResponse:
        operation = event_name.rsplit(".", 1)[-1]
        handler = self._handlers.get(operation)
        if handler is None:
            raise BenchmarkError(f"no stubbed response for S3 operation {operation!r}")
        return handler(request)

    def _list_objects_v2(self, request: Any) -> AWSResponse:
        if self._corpus is None:
            raise BenchmarkError("scenario issued ListObjectsV2 but has no listing corpus")
        _drain_body(request)
        return _response(request, body=self._corpus.page_for(request))

    def _put_object(self, request: Any) -> AWSResponse:
        _drain_body(request)
        return _response(request, headers={"ETag": '"benchetag"'})

    def _delete_objects(self, request: Any) -> AWSResponse:
        body = _drain_body(request, keep=True)
        deleted = "".join(
            f"<Deleted><Key>{key.decode()}</Key></Deleted>" for key in _KEY_RE.findall(body)
        )
        payload = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<DeleteResult xmlns="{_XMLNS}">{deleted}</DeleteResult>'
        ).encode()
        return _response(request, body=payload)

    def _create_multipart_upload(self, request: Any) -> AWSResponse:
        _drain_body(request)
        payload = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<InitiateMultipartUploadResult xmlns="{_XMLNS}">'
            "<Bucket>bench</Bucket><Key>bench</Key>"
            "<UploadId>bench-upload-id</UploadId>"
            "</InitiateMultipartUploadResult>"
        ).encode()
        return _response(request, body=payload)

    def _upload_part(self, request: Any) -> AWSResponse:
        _drain_body(request)
        return _response(request, headers={"ETag": '"benchpartetag"'})

    def _complete_multipart_upload(self, request: Any) -> AWSResponse:
        _drain_body(request)
        payload = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<CompleteMultipartUploadResult xmlns="{_XMLNS}">'
            "<Bucket>bench</Bucket><Key>bench</Key>"
            "<ETag>&quot;benchetag&quot;</ETag>"
            "</CompleteMultipartUploadResult>"
        ).encode()
        return _response(request, body=payload)

    def _abort_multipart_upload(self, request: Any) -> AWSResponse:
        _drain_body(request)
        return _response(request, status=204)
