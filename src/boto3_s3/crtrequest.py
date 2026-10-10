"""One S3 request through the CRT engine's client: ``CrtRequestSender``.

s3transfer's ``CRTTransferManager`` carries uploads, downloads and per-key
``DeleteObject`` requests on the CRT client and has no route for any other
operation. aws-cli deletes through it one key per request; the deleter here
batches its keys into ``DeleteObjects`` (design/deleter.md), so riding the same
engine takes a route s3transfer does not offer. This module is that route -
for the batches, and for the deleter's single ``DeleteObject`` requests too,
so one route reads every delete's answer (``capture_response`` included) -
built the way s3transfer builds its own: a botocore client that neither signs
nor sends serializes the operation, the CRT client signs the request and sends
it as a ``DEFAULT`` meta request under the operation's name, and the answer is
read with botocore's parser for the operation's output shape.

One thing differs from s3transfer's serializer, because its requests carry no
body of their own: the operation's checksum - the ``x-amz-checksum-*`` header,
or ``Content-MD5`` from a botocore that predates them, which ``DeleteObjects``
requires either way - stays on the request rather than being stripped.

What the CRT client does with the request is its own, and is what makes the
route aws-cli's: it re-sends a request that dies without a response, or is
answered with a throttling or server error, on its own retry policy whatever
the botocore client's ``max_attempts`` says (measured: six sends under
``AWS_MAX_ATTEMPTS=1``), and a failure that is not an HTTP answer surfaces as
awscrt's own error, whose text is the one aws-cli prints.

Nothing here imports awscrt at module import time (design/imports.md); the
sender is built only once the CRT engine has been (`crtsupport`).
"""

from __future__ import annotations

import io
import re
import threading
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

__all__ = ["CrtRequestSender"]

# Where the serializer client leaves the request it built, for the after-call
# hook to hand back.
_CONTEXT_KEY = "boto3_s3_crt_request"

# s3transfer's `_S3ArnParamHandler`: the resource part of an access point ARN.
_ACCESS_POINT = re.compile(r"^accesspoint[/:].+$")


def _capture_request(request: Any, **_kwargs: Any) -> None:
    request.context[_CONTEXT_KEY] = request


class _EmptyBody(io.BytesIO):
    """The raw body of the response the serializer client is handed instead
    of sending: botocore reads a raw response through ``stream()``."""

    def stream(self, amt: int = 1024, decode_content: Any = None) -> Any:
        while True:
            chunk = self.read(amt)
            if not chunk:
                return
            yield chunk


def _skip_send(**_kwargs: Any) -> Any:
    from botocore.awsrequest import AWSResponse

    # botocore's own stand-in shape (s3transfer builds the same): no URL and
    # plain headers, which the stubs type more narrowly.
    response: Any = AWSResponse
    return response(None, 200, {}, _EmptyBody(b""))


def _hand_back_request(context: Mapping[str, Any], parsed: dict[str, Any], **_kwargs: Any) -> None:
    parsed["HTTPRequest"] = context[_CONTEXT_KEY].prepare()


class CrtRequestSender:
    """Send botocore-serialized S3 requests through one CRT S3 client.

    ``call`` blocks until the request is done and returns the parsed response,
    the dict a botocore client call returns. An HTTP error answer the CRT
    client hands back (one it does not retry, ``AccessDenied`` say) raises the
    botocore ``ClientError`` subclass for its code, read from the answer the
    way s3transfer reads one for the CRT engine; any other failure raises the
    awscrt error itself - a server error the CRT kept getting included, which
    it reports as its own error once its retries are spent. ``call`` may run
    on several threads at once.

    The serializer client is built from the botocore session and client
    arguments the CRT engine's own request serializer was built from
    (`crtsupport`), so a request is addressed exactly like the engine's
    transfers - same endpoint, region, and ``Config`` - and never signed by
    botocore: the CRT client signs it.
    """

    def __init__(self, crt_client: Any, session: Any, client_kwargs: Mapping[str, Any]) -> None:
        from botocore import UNSIGNED
        from botocore.config import Config
        from botocore.parsers import create_parser

        # s3transfer's `_resolve_client_config`: the caller's Config (else
        # the session's default one) with signing turned off on top.
        config = Config(signature_version=UNSIGNED)
        user_config: Any = client_kwargs.get("config") or session.get_default_client_config()
        if user_config is not None:
            config = user_config.merge(config)
        client: Any = session.create_client("s3", **{**client_kwargs, "config": config})
        client.meta.events.register("request-created.s3.*", _capture_request)
        client.meta.events.register("before-send.s3.*", _skip_send)
        client.meta.events.register("after-call.s3.*", _hand_back_request)
        self._client = client
        self._crt_client = crt_client
        self._parser = create_parser(client.meta.service_model.metadata["protocol"])

    def call(self, operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Send ``operation`` (``"DeleteObjects"``) with ``params``; return its response."""
        import awscrt.s3
        from botocore import xform_name

        prepared = getattr(self._client, xform_name(operation))(**params)["HTTPRequest"]
        status: list[int] = []
        response_headers: list[tuple[str, str]] = []
        body = bytearray()
        outcome: list[BaseException | None] = []
        done = threading.Event()

        # awscrt passes every argument by keyword, so the names are its own.
        def on_headers(status_code: int, headers: list[tuple[str, str]], **_kwargs: Any) -> None:
            status.append(status_code)
            response_headers.extend(headers)

        def on_body(chunk: bytes, **_kwargs: Any) -> None:
            body.extend(chunk)

        def on_done(error: BaseException | None = None, **_kwargs: Any) -> None:
            outcome.append(error)
            done.set()

        args: dict[str, Any] = {
            "type": awscrt.s3.S3RequestType.DEFAULT,
            "request": _crt_request(prepared),
            # The CRT requires the official operation name for a DEFAULT
            # request, as s3transfer passes it.
            "operation_name": operation,
            "on_headers": on_headers,
            "on_body": on_body,
            "on_done": on_done,
        }
        signing_config = _signing_config(params.get("Bucket"))
        if signing_config is not None:
            args["signing_config"] = signing_config
        # Held until done: the request object is what keeps it running.
        request = self._crt_client.make_request(**args)
        done.wait()
        del request
        error = outcome[0]
        if error is not None:
            raise self._translate(error, operation) or error
        code = status[0] if status else 200
        if code == 200 and _special_case_error(bytes(body)):
            # botocore's rule for a 200 answer whose body is S3's error, or
            # does not parse at all: a 500. The CRT client already fails an
            # `Error` document itself (re-sending a passing fault first), so
            # what reaches here is a body that is not XML - read as a
            # DeleteObjects result it would list no failure, which says every
            # key was deleted.
            code = 500
        parsed = self._parse(operation, code, response_headers, bytes(body))
        if code >= 300:
            raise self._client_error(parsed, operation)
        return parsed

    def _parse(
        self, operation: str, status_code: int, headers: list[tuple[str, str]], body: bytes
    ) -> dict[str, Any]:
        from botocore.awsrequest import HeadersDict

        shape = self._client.meta.service_model.operation_model(operation).output_shape
        return self._parser.parse(
            {"headers": HeadersDict(headers), "status_code": status_code, "body": body}, shape
        )

    def _client_error(self, parsed: dict[str, Any], operation: str) -> Exception:
        code = parsed.get("Error", {}).get("Code")
        return self._client.exceptions.from_code(code)(parsed, operation)

    def _translate(self, error: BaseException, operation: str) -> Exception | None:
        """s3transfer's ``translate_crt_exception``: an HTTP error answer as the
        botocore error for its code, ``None`` for anything else."""
        import awscrt.s3

        if not isinstance(error, awscrt.s3.S3ResponseError):
            return None
        # Attributes awscrt sets in the constructor, untyped in its stubs.
        answer: Any = error
        status_code: int | None = answer.status_code
        if status_code is None or status_code < 301:
            return None
        name: str = answer.operation_name or operation
        headers: list[tuple[str, str]] = list(answer.headers or [])
        body: bytes = answer.body or b""
        return self._client_error(self._parse(name, status_code, headers, body), name)


def _crt_request(prepared: Any) -> Any:
    """s3transfer's ``_convert_to_crt_http_request``, keeping ``Content-MD5``.

    The CRT client wants a body as a stream and a ``Content-Length`` even for
    an empty body, cannot send ``Transfer-Encoding``, and needs the ``host``
    header botocore leaves to the HTTP layer.
    """
    import awscrt.http

    url: str = prepared.url
    parts = urlsplit(url)
    path = f"{parts.path}?{parts.query}" if parts.query else parts.path
    headers = awscrt.http.HttpHeaders(
        [
            (name, value if isinstance(value, str) else str(value, "utf-8"))
            for name, value in prepared.headers.items()
        ]
    )
    if headers.get("host") is None:
        headers.set("host", parts.netloc)
    body: Any = prepared.body
    if isinstance(body, str):
        body = body.encode("utf-8")
    if isinstance(body, (bytes, bytearray)):
        body = io.BytesIO(body) if body else None
    if headers.get("Content-Length") is None and body is None:
        headers.add("Content-Length", "0")
    if headers.get("Transfer-Encoding") is not None:
        headers.remove("Transfer-Encoding")
    return awscrt.http.HttpRequest(
        method=prepared.method, path=path, headers=headers, body_stream=body
    )


def _signing_config(bucket: Any) -> Any | None:
    """The signing the CRT client cannot work out for itself, as s3transfer sets it.

    A Multi-Region Access Point (an access point ARN with no region) is signed
    with SigV4a for every region, and an S3 Express directory bucket with its
    session signing; both are already URI-encoded by botocore. Everything else
    is the CRT client's default signing.
    """
    from awscrt.auth import AwsSigningAlgorithm, AwsSigningConfig
    from botocore.utils import ArnParser, InvalidArnException

    if not isinstance(bucket, str):
        return None
    try:
        arn = ArnParser().parse_arn(bucket)
    except InvalidArnException:
        arn = None
    if arn is not None:
        if arn["region"] == "" and _ACCESS_POINT.match(arn["resource"]):
            return AwsSigningConfig(
                algorithm=AwsSigningAlgorithm.V4_ASYMMETRIC,
                region="*",
                use_double_uri_encode=False,
                should_normalize_uri_path=False,
            )
        return None
    express: Any = getattr(AwsSigningAlgorithm, "V4_S3EXPRESS", None)
    if express is not None and bucket.endswith("--x-s3"):
        return AwsSigningConfig(
            algorithm=express, use_double_uri_encode=False, should_normalize_uri_path=False
        )
    return None


def _special_case_error(body: bytes) -> bool:
    """botocore's ``_looks_like_special_case_error`` for a 200 answer's body."""
    if not body:
        return False
    import xml.etree.ElementTree as ETree

    try:
        parser = ETree.XMLParser(target=ETree.TreeBuilder(), encoding="utf-8")
        parser.feed(body)
        root = parser.close()
    except ETree.ParseError:
        return True
    return root.tag == "Error"
