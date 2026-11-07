"""What a classic upload gets from s3transfer's ``request-created`` handler.

s3transfer tells each part its transfer is starting from a handler on botocore's
``request-created`` event, and only then does a part report progress and take
the bandwidth limit. So the client here is a real botocore client, with a
``before-send`` stub answering each request without a network: a stubbed client
object has no such event.
"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from botocore.httpchecksum import AwsChunkedWrapper
from s3transfer.utils import ReadFileChunk

from boto3_s3 import S3, BatchError, Boto3S3Error, IOStorage
from boto3_s3.s3storage import S3Storage
from boto3_s3.transferconfig import TransferConfig
from boto3_s3.types import OpOutcome, OpResult, TransferProgress


class _Endpoint:
    """A client whose requests are answered locally, and the bodies they carried."""

    def __init__(self, url: str, config: Config | None = None) -> None:
        session = boto3.session.Session(
            aws_access_key_id="x", aws_secret_access_key="y", region_name="us-east-1"
        )
        self.client: Any = session.client("s3", endpoint_url=url, config=config)
        self.sent: list[type] = []
        self.client.meta.events.register("before-send.s3", self._answer)

    def _answer(self, request: Any, **_kwargs: Any) -> AWSResponse:
        body = request.body
        self.sent.append(type(body))
        # A block at a time, as the HTTP layer sends it: a single read would be
        # one progress record and, being the first read a bandwidth limit sees,
        # never held back.
        while body.read(256 * 1024):
            pass
        response = AWSResponse(request.url, 200, {"ETag": '"etag"'}, None)

        class _Raw(io.BytesIO):
            def stream(self, *_args: Any, **_kwargs: Any) -> Any:
                yield b""

        response.raw = _Raw(b"")
        return response


class _Pipe(io.RawIOBase):
    """A stream that cannot seek, so s3transfer never learns its size."""

    def __init__(self, size: int) -> None:
        self._left = size

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any, /) -> int:
        count = min(len(buffer), self._left)
        buffer[:count] = b"p" * count
        self._left -= count
        return count


# Each upload path: its endpoint, botocore's checksum setting, the body it sends.
_UPLOAD_PATHS = [
    pytest.param("http://127.0.0.1:1", None, ReadFileChunk, id="plain-http"),
    pytest.param(
        "https://127.0.0.1:1", "when_supported", AwsChunkedWrapper, id="https-aws-chunked"
    ),
    pytest.param("https://127.0.0.1:1", "when_required", ReadFileChunk, id="https-no-checksum"),
]


def _upload_endpoint(url: str, calculation: str | None) -> _Endpoint:
    """An endpoint on one upload path; a botocore without the setting has only the first."""
    if calculation is None:
        return _Endpoint(url)
    try:
        return _Endpoint(url, Config(request_checksum_calculation=calculation))
    except TypeError:
        pytest.skip("this botocore has no request_checksum_calculation")


class _RaisingStream(io.BytesIO):
    """A seekable stream whose reads raise ``failure``: s3transfer hands it to the signer."""

    def __init__(self, failure: BaseException) -> None:
        super().__init__(b"s" * 1024)
        self.failure = failure

    def read(self, size: int | None = -1, /) -> bytes:
        raise self.failure

    def readinto(self, buffer: Any, /) -> int:
        raise self.failure


class TestAStreamRaisingWhereTheSignerReadsIt:
    """A caller's stream, with no checksum: awscrt's signer is its first reader."""

    def _upload(self, failure: BaseException, results: list[OpResult]) -> None:
        endpoint = _upload_endpoint("http://127.0.0.1:1", "when_required")
        S3().cp(
            IOStorage(_RaisingStream(failure)),
            S3Storage("s3://b/k", client=endpoint.client),
            transfer_config=TransferConfig(preferred_transfer_client="classic"),
            on_result=results.append,
        )

    @pytest.mark.parametrize("interrupt", [SystemExit(3), KeyboardInterrupt()])
    def test_an_interrupt_cancels_the_item_and_reaches_the_caller(
        self, interrupt: BaseException
    ) -> None:
        results: list[OpResult] = []
        with pytest.raises(type(interrupt)):
            self._upload(interrupt, results)
        assert [result.outcome for result in results] == [OpOutcome.CANCELLED]

    def test_a_library_error_is_recorded_as_it_is(self) -> None:
        results: list[OpResult] = []
        failure = Boto3S3Error("the stream's own")
        with pytest.raises(BatchError):
            self._upload(failure, results)
        assert [result.error for result in results] == [failure]


@pytest.mark.parametrize(("url", "calculation", "sent_as"), _UPLOAD_PATHS)
class TestTheSignalReachesThePart:
    def test_an_upload_reports_all_its_bytes(
        self, url: str, calculation: str | None, sent_as: type, tmp_path: Path
    ) -> None:
        source = tmp_path / "f"
        source.write_bytes(b"f" * (2 * 1024 * 1024))
        endpoint = _upload_endpoint(url, calculation)
        progress: list[TransferProgress] = []
        S3().cp(
            source,
            S3Storage("s3://b/k", client=endpoint.client),
            transfer_config=TransferConfig(preferred_transfer_client="classic"),
            on_progress=progress.append,
        )
        assert endpoint.sent == [sent_as]
        assert len(progress) > 2
        assert progress[-1].bytes_done == source.stat().st_size

    def test_a_size_unknown_stream_counts_its_bytes(
        self, url: str, calculation: str | None, sent_as: type
    ) -> None:
        size = 2 * 1024 * 1024
        endpoint = _upload_endpoint(url, calculation)
        results: list[OpResult] = []
        S3().cp(
            IOStorage(io.BufferedReader(_Pipe(size))),
            S3Storage("s3://b/k", client=endpoint.client),
            transfer_config=TransferConfig(preferred_transfer_client="classic"),
            on_result=results.append,
        )
        assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]
        assert results[0].bytes_transferred == size

    def test_an_upload_keeps_to_the_bandwidth_limit(
        self, url: str, calculation: str | None, sent_as: type, tmp_path: Path
    ) -> None:
        # Only a lower bound: a busy host can make the upload slower, but
        # nothing can make a limited one this fast.
        size, limit = 3 * 1024 * 1024, 8 * 1024 * 1024
        source = tmp_path / "f"
        source.write_bytes(b"f" * size)
        endpoint = _upload_endpoint(url, calculation)
        started = time.monotonic()
        S3().cp(
            source,
            S3Storage("s3://b/k", client=endpoint.client),
            transfer_config=TransferConfig(
                preferred_transfer_client="classic", max_bandwidth=limit
            ),
        )
        assert time.monotonic() - started >= 0.5 * (size - 1024 * 1024) / limit
