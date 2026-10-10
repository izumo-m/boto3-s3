"""``boto3_s3.crtrequest`` - single S3 requests through a real CRT client.

The sender runs against a real awscrt S3 client pointed at a 127.0.0.1
server that records each request and plays back a scripted answer, so what
is pinned is the wire request the CRT client sends and how its answer is
read. A request the server drops is not exercised here: the CRT client
re-sends it six times with backoff (about nine seconds, measured), which the
deleter's own tests stand in for with the awscrt error it ends in.
"""

from __future__ import annotations

import socket
import threading
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("s3transfer.crt")  # the CRT surface the sender rides
import awscrt.auth
import awscrt.s3
import botocore.session
from botocore.config import Config
from botocore.exceptions import ClientError
from s3transfer.crt import create_s3_crt_client

from boto3_s3 import crtrequest
from boto3_s3.crtrequest import CrtRequestSender


class _Server:
    """A 127.0.0.1 HTTP server answering one scripted reply per request.

    Every reply closes its connection, so the CRT client never reuses one the
    server has already left. ``requests`` holds (request line, lower-cased
    headers, body) for each request received.
    """

    def __init__(self, replies: list[bytes]) -> None:
        self.replies = list(replies)
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._socket.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(65536)
                head, _, body = data.partition(b"\r\n\r\n")
                line, *header_lines = head.decode("latin-1").split("\r\n")
                headers = {
                    name.strip().lower(): value.strip()
                    for name, _, value in (h.partition(":") for h in header_lines)
                }
                while len(body) < int(headers.get("content-length", "0")):
                    body += conn.recv(65536)
                self.requests.append((line, headers, body))
                if self.replies:  # past the script: drop the connection unanswered
                    conn.sendall(self.replies.pop(0))

    def close(self) -> None:
        self._socket.close()


def _reply(status: str, headers: dict[str, str] | None = None, body: bytes = b"") -> bytes:
    lines = [f"HTTP/1.1 {status}", "Connection: close", f"Content-Length: {len(body)}"]
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


@pytest.fixture
def serve() -> Any:
    servers: list[_Server] = []

    def start(*replies: bytes) -> tuple[_Server, CrtRequestSender]:
        server = _Server(list(replies))
        servers.append(server)
        crt_client = create_s3_crt_client(
            "us-east-1",
            crt_credentials_provider=awscrt.auth.AwsCredentialsProvider.new_static(
                "AKID", "SECRET"
            ),
            use_ssl=False,
        )
        sender = CrtRequestSender(
            crt_client,
            botocore.session.Session(),
            {
                "region_name": "us-east-1",
                "endpoint_url": server.url,
                "config": Config(s3={"addressing_style": "path"}),
            },
        )
        return server, sender

    yield start
    for server in servers:
        server.close()


_DELETE_RESULT = (
    b'<?xml version="1.0" encoding="UTF-8"?><DeleteResult>'
    b"<Deleted><Key>a</Key></Deleted>"
    b"<Error><Key>b</Key><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
    b"</DeleteResult>"
)


class TestSend:
    def test_a_batch_goes_out_signed_with_its_checksum_and_its_answer_is_read(
        self, serve: Any
    ) -> None:
        server, sender = serve(
            _reply("200 OK", {"x-amz-request-charged": "requester"}, _DELETE_RESULT)
        )
        response = sender.call(
            "DeleteObjects",
            {
                "Bucket": "bkt",
                "Delete": {"Objects": [{"Key": "a"}, {"Key": "b"}], "Quiet": True},
                "RequestPayer": "requester",
            },
        )
        ((line, headers, body),) = server.requests
        assert line == "POST /bkt?delete HTTP/1.1"
        # The checksum DeleteObjects requires stays on the request: the
        # x-amz-checksum-* header, or Content-MD5 from an older botocore.
        assert "content-md5" in headers or any(n.startswith("x-amz-checksum-") for n in headers)
        assert headers["x-amz-request-payer"] == "requester"
        # Signed by the CRT client, not by botocore.
        assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")
        assert b"<Key>a</Key>" in body and b"<Quiet>true</Quiet>" in body
        assert response["Deleted"] == [{"Key": "a"}]
        assert response["Errors"] == [
            {"Key": "b", "Code": "AccessDenied", "Message": "Access Denied"}
        ]
        assert response["RequestCharged"] == "requester"

    def test_a_single_delete_reads_its_headers(self, serve: Any) -> None:
        server, sender = serve(
            _reply("204 No Content", {"x-amz-delete-marker": "true", "x-amz-version-id": "v1"})
        )
        response = sender.call("DeleteObject", {"Bucket": "bkt", "Key": "x\x01y"})
        ((line, headers, _),) = server.requests
        assert line == "DELETE /bkt/x%01y HTTP/1.1"
        assert headers["content-length"] == "0"
        assert response["DeleteMarker"] is True
        assert response["VersionId"] == "v1"
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 204

    def test_an_error_answer_raises_the_botocore_error_for_its_code(self, serve: Any) -> None:
        body = (
            b"<Error><Code>NoSuchBucket</Code>"
            b"<Message>The specified bucket does not exist</Message></Error>"
        )
        _, sender = serve(_reply("404 Not Found", {"Content-Type": "application/xml"}, body))
        with pytest.raises(ClientError) as exc_info:
            sender.call("DeleteObjects", {"Bucket": "bkt", "Delete": {"Objects": [{"Key": "a"}]}})
        assert type(exc_info.value).__name__ == "NoSuchBucket"
        assert str(exc_info.value) == (
            "An error occurred (NoSuchBucket) when calling the DeleteObjects operation: "
            "The specified bucket does not exist"
        )

    def test_an_error_document_inside_a_200_answer_is_the_crt_clients_error(
        self, serve: Any
    ) -> None:
        # The CRT client reads S3's error out of a 200 answer itself; one it
        # will not retry is its own error, which s3transfer leaves untranslated
        # below a 301 as well. (A passing fault, InternalError, is re-sent
        # first: six requests, seven seconds, measured.)
        body = b"<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>"
        server, sender = serve(_reply("200 OK", {}, body))
        with pytest.raises(awscrt.s3.S3ResponseError) as exc_info:
            sender.call("DeleteObjects", {"Bucket": "bkt", "Delete": {"Objects": [{"Key": "a"}]}})
        assert exc_info.value.name == "AWS_ERROR_S3_NON_RECOVERABLE_ASYNC_ERROR"
        assert len(server.requests) == 1

    def test_a_200_answer_that_is_not_xml_is_an_error(self, serve: Any) -> None:
        # botocore's 500 for it; read as a DeleteObjects result it would list
        # no failure, which reads as every key deleted.
        _, sender = serve(_reply("200 OK", {}, b"not xml"))
        with pytest.raises(ClientError) as exc_info:
            sender.call("DeleteObjects", {"Bucket": "bkt", "Delete": {"Objects": [{"Key": "a"}]}})
        assert exc_info.value.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 500

    def test_a_request_botocore_rejects_never_goes_out(self, serve: Any) -> None:
        from botocore.exceptions import ParamValidationError

        server, sender = serve()
        with pytest.raises(ParamValidationError):
            sender.call("DeleteObject", {"Bucket": "bkt"})
        assert server.requests == []


class TestCrtRequest:
    """The conversion from botocore's prepared request."""

    def test_content_md5_stays_and_transfer_encoding_goes(self) -> None:
        prepared = SimpleNamespace(
            method="POST",
            url="http://h:9000/bkt?delete",
            headers={"Content-MD5": "abc==", "Transfer-Encoding": "chunked"},
            body=b"<Delete/>",
        )
        request = crtrequest._crt_request(prepared)  # pyright: ignore[reportPrivateUsage]
        assert request.path == "/bkt?delete"
        assert request.headers.get("Content-MD5") == "abc=="
        assert request.headers.get("Transfer-Encoding") is None
        assert request.headers.get("host") == "h:9000"

    def test_an_empty_body_gets_a_zero_length(self) -> None:
        prepared = SimpleNamespace(method="DELETE", url="http://h/bkt/k", headers={}, body=b"")
        request = crtrequest._crt_request(prepared)  # pyright: ignore[reportPrivateUsage]
        assert request.headers.get("Content-Length") == "0"


class TestSigningConfig:
    """s3transfer's per-bucket signing, for the buckets the CRT cannot sign by default."""

    def test_a_multi_region_access_point_is_signed_for_every_region(self) -> None:
        config = crtrequest._signing_config(  # pyright: ignore[reportPrivateUsage]
            "arn:aws:s3::123456789012:accesspoint/mfzwi23gnjvgw.mrap"
        )
        assert config is not None
        assert config.algorithm == awscrt.auth.AwsSigningAlgorithm.V4_ASYMMETRIC
        assert config.region == "*"
        assert config.use_double_uri_encode is False

    def test_a_directory_bucket_gets_its_session_signing(self) -> None:
        config = crtrequest._signing_config(  # pyright: ignore[reportPrivateUsage]
            "bucket--usw2-az1--x-s3"
        )
        assert config is not None
        assert config.algorithm == awscrt.auth.AwsSigningAlgorithm.V4_S3EXPRESS

    @pytest.mark.parametrize(
        "bucket",
        [
            "bucket",
            "arn:aws:s3:us-west-2:123456789012:accesspoint/ap",
            "arn:aws:s3-outposts:us-east-1:123456789012:outpost/op-1/accesspoint/ap",
        ],
    )
    def test_everything_else_is_the_client_default(self, bucket: str) -> None:
        assert crtrequest._signing_config(bucket) is None  # pyright: ignore[reportPrivateUsage]
