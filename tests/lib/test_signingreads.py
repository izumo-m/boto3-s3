"""Tests for boto3_s3.signingreads: a body read that fails while its request is signed.

The client is a real botocore client on a plain-HTTP endpoint, so the payload
is signed and the signer reads the body; a ``before-send`` stub answers each
request without a network. The body raises on every read once the test arms
it from ``before-sign``, which lands the failure in whichever signer botocore
picked: awscrt's when awscrt is importable, the pure-Python one otherwise.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore import auth
from botocore.awsrequest import AWSResponse

from boto3_s3 import Boto3S3Error, s3storage, signingreads
from boto3_s3.s3storage import S3Storage
from boto3_s3.signingreads import _SigningReadGuard, kept_signing_read_failures
from boto3_s3.transfer import TransferItem, Transferrer
from boto3_s3.transferconfig import TransferConfig
from boto3_s3.types import FileInfo, OpOutcome, OpResult, TransferType

_TRACEBACK_MARK = "Treating Python exception"


def _awscrt_signs() -> bool:
    """Whether botocore signs symmetric SigV4 with awscrt's signer here."""
    return auth.AUTH_TYPE_MAPS["s3v4"].__module__.startswith("botocore.crt")


class _Body(io.BytesIO):
    """A body whose first read once armed raises ``failure`` (EIO by default).

    Only that one read fails: a read after it (the stub's, were the request
    sent) succeeds, so nothing but the kept exception can fail the request.
    """

    armed = False
    failure: BaseException = OSError(5, "Input/output error")

    def _check(self) -> None:
        if self.armed:
            self.armed = False
            raise self.failure

    def read(self, size: int | None = -1, /) -> bytes:
        self._check()
        return super().read(size)

    def read1(self, size: int | None = -1, /) -> bytes:
        self._check()
        return super().read1(size)

    def readinto(self, buffer: Any, /) -> int:
        self._check()
        return super().readinto(buffer)

    def readinto1(self, buffer: Any, /) -> int:
        self._check()
        return super().readinto1(buffer)


class _Endpoint:
    """A client whose requests are answered locally, and what they carried."""

    def __init__(self) -> None:
        session = boto3.session.Session(
            aws_access_key_id="x", aws_secret_access_key="y", region_name="us-east-1"
        )
        self.client: Any = session.client("s3", endpoint_url="http://127.0.0.1:1")
        self.signs = 0
        self.sent: list[tuple[dict[str, str], bytes, type]] = []
        self.client.meta.events.register("before-sign.s3", self._count_sign)
        self.client.meta.events.register("before-send.s3", self._answer)

    def _count_sign(self, **_kwargs: Any) -> None:
        self.signs += 1

    def _answer(self, request: Any, **_kwargs: Any) -> AWSResponse:
        body = request.body
        body_type = type(body)
        data = body.read() if hasattr(body, "read") else (body or b"")
        headers = {
            key.lower(): value.decode() if isinstance(value, bytes) else value
            for key, value in request.headers.items()
        }
        self.sent.append((headers, data, body_type))
        response = AWSResponse(request.url, 200, {"ETag": '"etag"'}, None)

        class _Raw(io.BytesIO):
            def stream(self, *_args: Any, **_kwargs: Any) -> Any:
                yield b""

        response.raw = _Raw(b"")
        return response

    def arm_at_signing(self, body: _Body) -> None:
        def arm(**_kwargs: Any) -> None:
            body.armed = True

        self.client.meta.events.register("before-sign.s3", arm)


class TestTheWatchedRequest:
    def test_a_read_failing_while_signing_fails_the_request_with_its_own_error(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # The read's own exception, raised once after signing: nothing is
        # sent, nothing retried, and awscrt (when it signs) writes nothing.
        endpoint = _Endpoint()
        body = _Body(b"x" * 1000)
        endpoint.arm_at_signing(body)
        with kept_signing_read_failures(endpoint.client), pytest.raises(OSError) as excinfo:
            endpoint.client.put_object(Bucket="b", Key="k", Body=body)
        assert excinfo.value.errno == 5
        assert endpoint.sent == []
        assert endpoint.signs == 1
        assert _TRACEBACK_MARK not in capfd.readouterr().err

    def test_unwatched_awscrt_loses_the_error(self) -> None:
        # Why the watch exists: awscrt's signer takes the read's exception,
        # writes its traceback and fails the request with its own error.
        if not _awscrt_signs():
            pytest.skip("botocore signs with the pure-Python signer here")
        endpoint = _Endpoint()
        body = _Body(b"x" * 1000)
        endpoint.arm_at_signing(body)
        with pytest.raises(Exception) as excinfo:
            endpoint.client.put_object(Bucket="b", Key="k", Body=body)
        assert "AWS_ERROR_UNKNOWN" in str(excinfo.value)

    def test_a_clean_body_is_signed_and_sent_unchanged(self) -> None:
        # The payload hash and the signed headers are those of an unwatched
        # request (the signature itself carries the second it was made in).
        # A stream is the case: botocore hands even a bytes body to the
        # signer as a stream of its own.
        watched, plain = _Endpoint(), _Endpoint()
        payload = b"y" * 100000
        with kept_signing_read_failures(watched.client):
            watched.client.put_object(Bucket="b", Key="k", Body=io.BytesIO(payload))
        plain.client.put_object(Bucket="b", Key="k", Body=io.BytesIO(payload))
        (watched_headers, watched_data, _), (plain_headers, plain_data, _) = (
            watched.sent[0],
            plain.sent[0],
        )
        assert watched_data == plain_data == payload
        assert watched_headers["x-amz-content-sha256"] == hashlib.sha256(payload).hexdigest()
        signed = [
            h["authorization"].split("Signature=")[0] for h in (watched_headers, plain_headers)
        ]
        assert signed[0] == signed[1]
        ignored = {"authorization", "x-amz-date", "amz-sdk-invocation-id"}
        assert {k: v for k, v in watched_headers.items() if k not in ignored} == {
            k: v for k, v in plain_headers.items() if k not in ignored
        }

    def test_the_body_sent_is_the_requests_own(self) -> None:
        # The guard is the body only while the request is signed: what is sent
        # is the request's own object, whatever special methods the HTTP layer
        # uses on it (urllib3 1.x iterates a chunked body).
        endpoint = _Endpoint()
        with kept_signing_read_failures(endpoint.client):
            endpoint.client.put_object(Bucket="b", Key="k", Body=io.BytesIO(b"a"))
        assert [body_type for _, _, body_type in endpoint.sent] == [io.BytesIO]

    def test_the_watch_ends_with_the_block(self) -> None:
        if not _awscrt_signs():
            pytest.skip("botocore signs with the pure-Python signer here")
        endpoint = _Endpoint()
        with kept_signing_read_failures(endpoint.client):
            pass
        body = _Body(b"x" * 1000)
        endpoint.arm_at_signing(body)
        with pytest.raises(Exception, match="AWS_ERROR_UNKNOWN"):
            endpoint.client.put_object(Bucket="b", Key="k", Body=body)

    def test_a_retryable_error_kept_while_signing_is_not_retried(self) -> None:
        # Raised from the signing step, outside botocore's retries, as the
        # pure-Python signer's raise is: a connection error stopping the
        # other parts of a failed upload is not retried on every one of them.
        from botocore.exceptions import EndpointConnectionError

        endpoint = _Endpoint()
        body = _Body(b"x" * 1000)
        body.failure = EndpointConnectionError(endpoint_url="http://127.0.0.1:1")
        endpoint.arm_at_signing(body)
        with kept_signing_read_failures(endpoint.client), pytest.raises(EndpointConnectionError):
            endpoint.client.put_object(Bucket="b", Key="k", Body=body)
        assert endpoint.signs == 1
        assert endpoint.sent == []

    def test_an_interrupt_while_signing_reaches_the_caller(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # awscrt would lose a Ctrl-C landing in the read in its own error.
        endpoint = _Endpoint()
        body = _Body(b"x" * 1000)
        body.failure = KeyboardInterrupt()
        endpoint.arm_at_signing(body)
        with kept_signing_read_failures(endpoint.client), pytest.raises(KeyboardInterrupt):
            endpoint.client.put_object(Bucket="b", Key="k", Body=body)
        assert endpoint.sent == []
        assert _TRACEBACK_MARK not in capfd.readouterr().err

    def test_a_client_without_botocore_events_is_left_alone(self) -> None:
        assert signingreads.register(object()) is False
        with kept_signing_read_failures(object()):
            pass


class TestTheGuard:
    def test_it_offers_only_the_reads_the_body_has(self) -> None:
        class _ReadOnly:
            def read(self, size: int = -1) -> bytes:
                return b"data"[: size if size >= 0 else None]

            def tell(self) -> int:
                return 7

        guard = _SigningReadGuard(_ReadOnly())
        assert hasattr(guard, "read")
        assert not hasattr(guard, "readinto1")
        assert not hasattr(guard, "readinto")
        assert guard.tell() == 7
        assert guard.read(2) == b"da"

    def test_while_signing_the_first_failure_is_kept_and_reads_end(self) -> None:
        body = _Body(b"abc")
        body.armed = True
        guard = _SigningReadGuard(body)
        assert guard.read(1) == b""
        assert guard.readinto(bytearray(2)) == 0
        assert isinstance(guard.failure, OSError)
        body.armed = False
        assert guard.read() == b"", "once failed, reads keep answering the end"

    def test_an_interrupt_is_kept_too(self) -> None:
        class _Interrupted:
            def read(self, size: int = -1) -> bytes:
                raise KeyboardInterrupt

        guard = _SigningReadGuard(_Interrupted())
        assert guard.read() == b""
        assert isinstance(guard.failure, KeyboardInterrupt)


class TestTheClassicEngine:
    def test_an_upload_read_failing_while_signing_fails_the_item_with_it(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # The classic engine's client is watched for the run: the item fails
        # with the read's error (a caller's stream: the base error with it on
        # __cause__), not with awscrt's AWS_ERROR_UNKNOWN, and no traceback.
        endpoint = _Endpoint()
        body = _Body(b"z" * 1000)
        endpoint.arm_at_signing(body)
        results: list[OpResult] = []
        with _transferrer(endpoint, results) as transferrer:
            transferrer.submit(_item(body))
        assert [result.outcome for result in results] == [OpOutcome.FAILED]
        error = results[0].error
        assert isinstance(error, Boto3S3Error)
        assert isinstance(error.__cause__, OSError)
        assert error.__cause__.errno == 5
        assert endpoint.sent == []
        assert _TRACEBACK_MARK not in capfd.readouterr().err

    def test_an_interrupt_while_signing_cancels_the_item_and_reaches_the_caller(
        self, capfd: pytest.CaptureFixture[str]
    ) -> None:
        endpoint = _Endpoint()
        body = _Body(b"z" * 1000)
        body.failure = KeyboardInterrupt()
        endpoint.arm_at_signing(body)
        results: list[OpResult] = []
        with (
            pytest.raises(KeyboardInterrupt),
            _transferrer(endpoint, results) as transferrer,
        ):
            transferrer.submit(_item(body))
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]
        assert endpoint.sent == []
        assert _TRACEBACK_MARK not in capfd.readouterr().err

    def test_the_watch_is_removed_with_the_run(self) -> None:
        if not _awscrt_signs():
            pytest.skip("botocore signs with the pure-Python signer here")
        endpoint = _Endpoint()
        with _transferrer(endpoint, []) as transferrer:
            transferrer.submit(_item(io.BytesIO(b"a")))
        body = _Body(b"x" * 1000)
        endpoint.arm_at_signing(body)
        with pytest.raises(Exception, match="AWS_ERROR_UNKNOWN"):
            endpoint.client.put_object(Bucket="b", Key="k", Body=body)


def _transferrer(endpoint: _Endpoint, results: list[OpResult]) -> Transferrer:
    """A classic-engine upload Transferrer on *endpoint*'s client."""
    return Transferrer(
        TransferType.UPLOAD,
        endpoint.client,
        transfer_config=TransferConfig(preferred_transfer_client="classic"),
        on_result=results.append,
    )


def _item(body: Any) -> TransferItem:
    return TransferItem(
        compare_key="-",
        size=len(body.getvalue()),
        src_fileobj=body,
        src_info=FileInfo(key="-"),
        dest_bucket="b",
        dest_key="k",
    )


class TestPutFile:
    def test_a_file_read_failing_while_signing_fails_with_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
    ) -> None:
        # put_file's one PutObject is watched too: the request failure carries
        # the read's error, not awscrt's AWS_ERROR_UNKNOWN.
        source = tmp_path / "f"
        source.write_bytes(b"q" * 1000)
        opened: list[_FailingFile] = []

        def open_failing(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            stream = _FailingFile(path, mode)
            opened.append(stream)
            return stream

        monkeypatch.setattr(s3storage, "open", open_failing, raising=False)
        endpoint = _Endpoint()

        def arm(**_kwargs: Any) -> None:
            opened[0].armed = True

        endpoint.client.meta.events.register("before-sign.s3", arm)
        with pytest.raises(Boto3S3Error) as excinfo:
            S3Storage("s3://b/", client=endpoint.client).put_file(source, key="k")
        assert isinstance(excinfo.value.__cause__, OSError)
        assert excinfo.value.__cause__.errno == 5
        assert endpoint.sent == []
        assert _TRACEBACK_MARK not in capfd.readouterr().err


class _FailingFile(io.FileIO):
    """A real file whose first read once armed raises EIO (and only that one)."""

    armed = False

    def _check(self) -> None:
        if self.armed:
            self.armed = False
            raise OSError(5, "Input/output error")

    def read(self, size: int | None = -1, /) -> bytes:
        self._check()
        return super().read(size)

    def readinto(self, buffer: Any, /) -> int:
        self._check()
        return super().readinto(buffer)

    def readall(self) -> bytes:
        self._check()
        return super().readall()
