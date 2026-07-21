"""Unit tests for boto3_s3.deleter.S3Deleter (batching, results, threading).

Uses a hand-rolled fake S3 client (no moto dependency); the fake records each
``delete_objects`` / ``delete_object`` call, plays back scripted responses or
exceptions, and can hold a batch call in flight on a ``threading.Event`` gate
so the async behaviors are tested deterministically.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from boto3_s3 import (
    AccessDeniedError,
    Boto3S3Error,
    ConfigurationError,
    LocalStorage,
    NotFoundError,
    OpOutcome,
    OpResult,
    S3Deleter,
    S3FileInfo,
    S3Storage,
    TransferType,
    TransportError,
    ValidationError,
)
from boto3_s3.deleter import S3_DELETE_BATCH
from tests.utils.fakes3 import client_error


class _FakeS3Client:
    """Record delete calls and play back separate batch / single scripts.

    Script items return a dict or raise an Exception; an exhausted script
    returns ``{}``. When ``gate`` is set, ``delete_objects`` blocks on it after
    recording the call, so a test can hold a batch in flight.
    """

    def __init__(
        self,
        script: list[dict[str, Any] | Exception] | None = None,
        *,
        single_script: list[dict[str, Any] | Exception] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.single_calls: list[dict[str, Any]] = []
        self.call_threads: list[int] = []
        self.script = list(script or [])
        self.single_script = list(single_script or [])
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        self.call_threads.append(threading.get_ident())
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=5.0), "test gate was never released"
        action: dict[str, Any] | Exception = self.script.pop(0) if self.script else {}
        if isinstance(action, Exception):
            raise action
        return action

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.single_calls.append(kwargs)
        action: dict[str, Any] | Exception = self.single_script.pop(0) if self.single_script else {}
        if isinstance(action, Exception):
            raise action
        return action


def _deleter(fake: _FakeS3Client, **kwargs: Any) -> S3Deleter:
    return S3Deleter(S3Storage("s3://bucket/prefix/", client=fake), **kwargs)


def _info(key: str) -> S3FileInfo:
    """A minimal listing entry for ``S3Deleter.submit`` (only ``key`` matters;
    with no ``compare_key`` stamped, the emitted record's ``compare_key`` falls
    back to the full key)."""
    return S3FileInfo(key=key)


def _keys(calls: list[dict[str, Any]]) -> list[list[str]]:
    return [[obj["Key"] for obj in call["Delete"]["Objects"]] for call in calls]


def _client_error(
    code: str = "AccessDenied", status: int = 403, operation: str = "DeleteObjects"
) -> ClientError:
    return client_error(code, status, operation, message="boom")


def _deleter_worker_alive() -> bool:
    return any(
        t.name.startswith("boto3-s3-deleter") and t.is_alive() for t in threading.enumerate()
    )


class TestConstruction:
    @pytest.mark.parametrize("batch_size", [0, -1, S3_DELETE_BATCH + 1])
    def test_batch_size_out_of_bounds_rejected(self, batch_size: int) -> None:
        # Exact type: the caller-argument guard raises inside the taxonomy,
        # like the storage-type guard above it.
        with pytest.raises(ValidationError, match="batch_size") as excinfo:
            _deleter(_FakeS3Client(), batch_size=batch_size)
        assert type(excinfo.value) is ValidationError

    @pytest.mark.parametrize("batch_size", [1, S3_DELETE_BATCH])
    def test_batch_size_bounds_accepted(self, batch_size: int) -> None:
        _deleter(_FakeS3Client(), batch_size=batch_size).close()

    def test_client_resolved_eagerly_on_caller_thread(self) -> None:
        class _BrokenStorage(S3Storage):
            def get_client(self) -> Any:
                raise RuntimeError("no client for you")

        with pytest.raises(RuntimeError, match="no client for you"):
            S3Deleter(_BrokenStorage("s3://bucket/"))

    def test_non_s3_storage_rejected(self) -> None:
        # Duck-typed callers (e.g. S3.resolve routing a bare "bucket/key"
        # to LocalStorage) must fail inside the taxonomy, not AttributeError.
        with pytest.raises(ValidationError, match="S3Storage"):
            S3Deleter(LocalStorage("/tmp/somewhere"))


class TestBatching:
    def test_submit_buffers_until_batch_size(self) -> None:
        fake = _FakeS3Client()
        deleter = _deleter(fake, batch_size=3)
        deleter.submit(_info("a"))
        deleter.submit(_info("b"))
        assert fake.calls == []
        deleter.submit(_info("c"))  # reaches batch_size -> auto-flush
        deleter.close()
        assert _keys(fake.calls) == [["a", "b", "c"]]
        assert fake.calls[0]["Bucket"] == "bucket"

    def test_quiet_true_by_default(self) -> None:
        # Default path (no capture_response): the batch is sent Quiet so the
        # response lists failures only, and every submitted key is synthesized.
        fake = _FakeS3Client()
        deleter = _deleter(fake)
        deleter.submit(_info("a"))
        deleter.close()
        assert fake.calls[0]["Delete"]["Quiet"] is True

    def test_request_payer_forwarded(self) -> None:
        fake = _FakeS3Client()
        deleter = _deleter(fake, request_payer="requester")
        deleter.submit(_info("a"))
        deleter.close()
        assert fake.calls[0]["RequestPayer"] == "requester"

    def test_request_payer_omitted_by_default(self) -> None:
        fake = _FakeS3Client()
        deleter = _deleter(fake)
        deleter.submit(_info("a"))
        deleter.close()
        assert "RequestPayer" not in fake.calls[0]

    def test_flush_sends_partial_batch(self) -> None:
        fake = _FakeS3Client()
        deleter = _deleter(fake, batch_size=10)
        deleter.submit(_info("a"))
        deleter.submit(_info("b"))
        deleter.flush()
        deleter.close()
        assert _keys(fake.calls) == [["a", "b"]]

    def test_flush_empty_buffer_is_noop(self) -> None:
        fake = _FakeS3Client()
        deleter = _deleter(fake)
        deleter.flush()
        deleter.close()
        assert fake.calls == []

    def test_close_flushes_remaining(self) -> None:
        fake = _FakeS3Client()
        deleter = _deleter(fake, batch_size=10)
        deleter.submit(_info("a"))
        deleter.close()
        assert _keys(fake.calls) == [["a"]]

    def test_close_without_flush_abandons_buffer(self) -> None:
        fake = _FakeS3Client()
        deleter = _deleter(fake, batch_size=10)
        deleter.submit(_info("a"))
        deleter.close(flush=False)
        assert fake.calls == []
        assert deleter.succeeded == 0

    def test_batches_split_at_batch_size_preserving_order(self) -> None:
        fake = _FakeS3Client()
        deleter = _deleter(fake, batch_size=2)
        for key in ("a", "b", "c", "d", "e"):
            deleter.submit(_info(key))
        deleter.close()
        assert _keys(fake.calls) == [["a", "b"], ["c", "d"], ["e"]]

    def test_empty_key_rejected(self) -> None:
        # S3 keys are >= 1 char; one empty key would fail its whole batch
        # (botocore rejects the request client-side), so reject it up front.
        fake = _FakeS3Client()
        deleter = _deleter(fake, batch_size=10)
        with pytest.raises(ValidationError, match="empty"):
            deleter.submit(_info(""))
        deleter.submit(_info("a"))  # the deleter stays usable
        deleter.close()
        assert _keys(fake.calls) == [["a"]]

    def test_empty_compare_key_is_preserved(self) -> None:
        # A legitimately empty compare_key (a folder-marker object equal to
        # the listing prefix) must not fall back to the full key - only a
        # None (hand-built entry) does. `or` would swallow it.
        fake = _FakeS3Client()
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=10, on_result=results.append)
        deleter.submit(S3FileInfo(key="prefix/", compare_key=""))
        deleter.close()
        assert [r.compare_key for r in results] == [""]
        assert results[0].src_info is not None
        assert results[0].src_info.key == "prefix/"

    def test_duplicate_keys_pass_through(self) -> None:
        fake = _FakeS3Client()
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=10, on_result=results.append)
        deleter.submit(_info("k"))
        deleter.submit(_info("k"))
        deleter.close()
        assert _keys(fake.calls) == [["k", "k"]]
        assert [r.compare_key for r in results] == ["k", "k"]
        assert deleter.succeeded == 2


class TestXmlIncompatibleFallback:
    @pytest.mark.parametrize(
        ("char", "compatible"),
        [
            ("\t", True),
            ("\n", True),
            ("\r", True),
            ("\x00", False),
            ("\x1f", False),  # last C0 control below the allowed range
            ("\x20", True),  # first allowed non-control
            ("퟿", True),  # last code point before the surrogate block
            ("\ud800", False),  # first surrogate
            ("\udfff", False),  # last surrogate
            ("", True),  # first code point after the surrogate block
            ("�", True),  # last allowed BMP code point
            ("￾", False),  # first terminal BMP noncharacter
            ("￿", False),  # second terminal BMP noncharacter
            ("\U00010000", True),  # first astral code point
            ("\U0010ffff", True),  # last code point
        ],
    )
    def test_xml_char_production_boundaries(self, char: str, compatible: bool) -> None:
        # Pins the predicate exactly at the edges of XML 1.0's Char production,
        # where an off-by-one in the character class would flip silently.
        from boto3_s3.deleter import _delete_objects_compatible

        assert _delete_objects_compatible(f"key-{char}-tail") is compatible

    def test_only_xml_incompatible_keys_use_delete_object(self) -> None:
        fake = _FakeS3Client()
        results: list[OpResult] = []
        keys = (
            "plain",
            "line\nbreak",
            "carriage\rreturn",
            "tab\tkey",
            "control-\x01",
            "noncharacter-\uffff",
        )
        deleter = _deleter(fake, batch_size=10, on_result=results.append)
        for key in keys:
            deleter.submit(_info(key))
        deleter.close()

        # Botocore escapes CR/LF before the XML body is sent; XML 1.0's
        # forbidden controls/noncharacters alone need the per-key URL route.
        assert _keys(fake.calls) == [["plain", "line\nbreak", "carriage\rreturn", "tab\tkey"]]
        assert [call["Key"] for call in fake.single_calls] == [
            "control-\x01",
            "noncharacter-\uffff",
        ]
        assert [result.compare_key for result in results] == list(keys)
        assert all(result.outcome is OpOutcome.SUCCEEDED for result in results)

    def test_all_incompatible_keys_skip_delete_objects(self) -> None:
        fake = _FakeS3Client()
        deleter = _deleter(fake, batch_size=10)
        deleter.submit(_info("nul-\x00"))
        deleter.submit(_info("form-feed-\x0c"))
        deleter.close()
        assert fake.calls == []
        assert [call["Key"] for call in fake.single_calls] == ["nul-\x00", "form-feed-\x0c"]

    def test_single_failure_is_attributed_without_failing_batchable_keys(self) -> None:
        fake = _FakeS3Client(
            single_script=[_client_error(operation="DeleteObject")],
        )
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=10, on_result=results.append, operation="rm")
        deleter.submit(_info("ordinary"))
        deleter.submit(_info("control-\x01"))
        deleter.close()

        assert [result.outcome for result in results] == [
            OpOutcome.SUCCEEDED,
            OpOutcome.FAILED,
        ]
        error = results[1].error
        assert isinstance(error, AccessDeniedError)
        assert error.operation == "rm"
        assert error.key == "control-\x01"
        assert (deleter.succeeded, deleter.failed) == (1, 1)

    def test_single_forwards_request_payer_and_captures_response(self) -> None:
        fake = _FakeS3Client(
            single_script=[
                {
                    "DeleteMarker": True,
                    "VersionId": "v1",
                    "ResponseMetadata": {"HTTPStatusCode": 204},
                }
            ]
        )
        results: list[OpResult] = []
        deleter = _deleter(
            fake,
            request_payer="requester",
            capture_response=True,
            on_result=results.append,
        )
        deleter.submit(_info("control-\x01"))
        deleter.close()

        assert fake.single_calls == [
            {"Bucket": "bucket", "Key": "control-\x01", "RequestPayer": "requester"}
        ]
        assert results[0].extra_info == {"delete": {"DeleteMarker": True, "VersionId": "v1"}}


class TestResults:
    def test_success_results_in_submission_order(self) -> None:
        fake = _FakeS3Client()
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=10, on_result=results.append)
        for key in ("a", "b", "c"):
            deleter.submit(_info(key))
        deleter.close()
        assert [r.compare_key for r in results] == ["a", "b", "c"]
        assert all(r.transfer_type is TransferType.DELETE for r in results)
        assert all(r.outcome is OpOutcome.SUCCEEDED for r in results)
        assert all(r.bytes_transferred == 0 for r in results)
        assert all(r.error is None for r in results)
        assert (deleter.succeeded, deleter.failed) == (3, 0)
        assert deleter.first_error is None

    @pytest.mark.parametrize(
        ("code", "category"),
        [
            ("AccessDenied", AccessDeniedError),
            ("NoSuchBucket", NotFoundError),
            ("NoSuchKey", NotFoundError),
            ("NoSuchVersion", NotFoundError),
            ("NotFound", NotFoundError),
            ("InternalError", TransportError),
            ("SlowDown", TransportError),
            ("ServiceUnavailable", TransportError),
            ("RequestTimeout", TransportError),
            ("SomethingNovel", Boto3S3Error),
        ],
    )
    def test_per_key_error_translation(self, code: str, category: type[Boto3S3Error]) -> None:
        fake = _FakeS3Client(script=[{"Errors": [{"Key": "k", "Code": code, "Message": "msg"}]}])
        results: list[OpResult] = []
        deleter = _deleter(fake, on_result=results.append, operation="delete")
        deleter.submit(_info("k"))
        deleter.close()
        error = results[0].error
        assert type(error) is category
        assert isinstance(error, Boto3S3Error)
        assert (
            str(error) == f"An error occurred ({code}) when calling the DeleteObjects "
            "operation: msg"
        )
        assert (error.operation, error.bucket, error.key) == ("delete", "bucket", "k")
        assert results[0].outcome is OpOutcome.FAILED
        assert (deleter.succeeded, deleter.failed) == (0, 1)

    def test_error_entry_missing_code_and_message_uses_defaults(self) -> None:
        fake = _FakeS3Client(script=[{"Errors": [{"Key": "k"}]}])
        results: list[OpResult] = []
        deleter = _deleter(fake, on_result=results.append)
        deleter.submit(_info("k"))
        deleter.close()
        error = results[0].error
        assert type(error) is Boto3S3Error
        assert str(error) == (
            "An error occurred (Unknown) when calling the DeleteObjects operation: no message"
        )

    def test_mixed_batch_keeps_submission_order(self) -> None:
        fake = _FakeS3Client(
            script=[{"Errors": [{"Key": "b", "Code": "AccessDenied", "Message": "denied"}]}]
        )
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=10, on_result=results.append)
        for key in ("a", "b", "c"):
            deleter.submit(_info(key))
        deleter.close()
        assert [(r.compare_key, r.outcome) for r in results] == [
            ("a", OpOutcome.SUCCEEDED),
            ("b", OpOutcome.FAILED),
            ("c", OpOutcome.SUCCEEDED),
        ]
        assert (deleter.succeeded, deleter.failed) == (2, 1)

    def test_first_error_is_first_failure_across_batches(self) -> None:
        fake = _FakeS3Client(
            script=[
                {"Errors": [{"Key": "a", "Code": "AccessDenied", "Message": "m1"}]},
                {"Errors": [{"Key": "b", "Code": "AccessDenied", "Message": "m2"}]},
            ]
        )
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=1, on_result=results.append)
        deleter.submit(_info("a"))
        deleter.submit(_info("b"))
        deleter.close()
        assert deleter.failed == 2
        assert deleter.first_error is results[0].error

    def test_request_level_client_error_fails_whole_batch(self) -> None:
        boom = _client_error("AccessDenied", 403)
        fake = _FakeS3Client(script=[boom])
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=10, on_result=results.append)
        deleter.submit(_info("a"))
        deleter.submit(_info("b"))
        deleter.close()  # must not raise: the failure is recorded per key
        assert [r.outcome for r in results] == [OpOutcome.FAILED, OpOutcome.FAILED]
        error = results[0].error
        assert isinstance(error, AccessDeniedError)
        # The full ClientError text (what aws-cli prints), matching the shape
        # of the synthesized per-key messages.
        assert str(error) == (
            "An error occurred (AccessDenied) when calling the DeleteObjects operation: boom"
        )
        assert results[1].error is error  # same translated instance for the batch
        assert error.__cause__ is boom
        assert (deleter.succeeded, deleter.failed) == (0, 2)
        assert deleter.first_error is error

    def test_later_batches_run_after_request_failure(self) -> None:
        fake = _FakeS3Client(script=[_client_error(), {}])
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=1, on_result=results.append)
        deleter.submit(_info("a"))
        deleter.submit(_info("b"))
        deleter.close()
        assert _keys(fake.calls) == [["a"], ["b"]]
        assert [r.outcome for r in results] == [OpOutcome.FAILED, OpOutcome.SUCCEEDED]

    def test_request_level_request_timeout_maps_to_transport_error(self) -> None:
        # The shared code table wins over the 4xx status fallback, so both
        # delivery paths (request-level and per-key) classify RequestTimeout
        # the same way.
        fake = _FakeS3Client(script=[_client_error("RequestTimeout", 400)])
        results: list[OpResult] = []
        deleter = _deleter(fake, on_result=results.append)
        deleter.submit(_info("a"))
        deleter.close()
        assert isinstance(results[0].error, TransportError)

    def test_unattributable_error_entries_warn_instead_of_crashing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # An Errors[] entry without Key, or whose key was never submitted,
        # cannot be attributed; the affected key reads as a success (the
        # Quiet=True synthesis limit), so the deleter warns loudly instead of
        # crashing the batch or staying silent.
        fake = _FakeS3Client(
            script=[
                {
                    "Errors": [
                        {"Code": "InternalError", "Message": "no key"},
                        {"Key": "never-submitted", "Code": "AccessDenied", "Message": "m"},
                    ]
                }
            ]
        )
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=10, on_result=results.append)
        deleter.submit(_info("a"))
        deleter.submit(_info("b"))
        with caplog.at_level(logging.WARNING, logger="boto3_s3.deleter"):
            deleter.close()
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED, OpOutcome.SUCCEEDED]
        assert (deleter.succeeded, deleter.failed) == (2, 0)
        assert caplog.text.count("unattributable DeleteObjects error") == 2

    def test_credentials_error_maps_to_configuration_error(self) -> None:
        fake = _FakeS3Client(script=[NoCredentialsError()])
        results: list[OpResult] = []
        deleter = _deleter(fake, on_result=results.append)
        deleter.submit(_info("a"))
        deleter.close()
        assert isinstance(results[0].error, ConfigurationError)

    def test_on_result_exception_reraises_and_counts_started_records(self) -> None:
        seen: list[str] = []

        def explosive(result: OpResult) -> None:
            seen.append(result.compare_key)
            if len(seen) == 2:
                raise RuntimeError("callback boom")

        fake = _FakeS3Client()
        deleter = _deleter(fake, batch_size=10, on_result=explosive)
        for key in ("a", "b", "c"):
            deleter.submit(_info(key))
        with pytest.raises(RuntimeError, match="callback boom"):
            deleter.close()
        assert seen == ["a", "b"]  # "c" was never dispatched
        assert deleter.succeeded == 2  # counted before the callback ran
        with pytest.raises(ValidationError):  # the deleter still ended up closed
            deleter.submit(_info("d"))

    def test_operation_kwarg_threads_into_errors(self) -> None:
        fake = _FakeS3Client(
            script=[{"Errors": [{"Key": "k", "Code": "AccessDenied", "Message": "msg"}]}]
        )
        results: list[OpResult] = []
        deleter = _deleter(fake, on_result=results.append, operation="rm")
        deleter.submit(_info("k"))
        deleter.close()
        error = results[0].error
        assert isinstance(error, Boto3S3Error)
        assert error.operation == "rm"
        with pytest.raises(ValidationError) as exc_info:
            deleter.submit(_info("again"))
        assert exc_info.value.operation == "rm"


class TestCaptureSlots:
    def test_batch_slot_strips_key_and_copies_request_charged(self) -> None:
        # docs/deleter.md: each Deleted[] entry becomes a per-key slot shaped
        # like a single DeleteObject response - the entry minus its Key
        # (already the result's key), plus the batch-wide RequestCharged.
        fake = _FakeS3Client(
            script=[
                {
                    "Deleted": [
                        {
                            "Key": "prefix/a.txt",
                            "DeleteMarker": True,
                            "DeleteMarkerVersionId": "dm1",
                        },
                        {"Key": "prefix/b.txt", "VersionId": "v2"},
                    ],
                    "RequestCharged": "requester",
                }
            ]
        )
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=10, on_result=results.append, capture_response=True)
        deleter.submit(_info("prefix/a.txt"))
        deleter.submit(_info("prefix/b.txt"))
        deleter.close()
        # capture_response flips Quiet off so the response carries Deleted[];
        # without that the per-key slots below could not be reconstructed.
        assert fake.calls[0]["Delete"]["Quiet"] is False
        slots = [(r.extra_info or {}).get("delete") for r in results]
        assert slots[0] == {
            "DeleteMarker": True,
            "DeleteMarkerVersionId": "dm1",
            "RequestCharged": "requester",
        }
        assert slots[1] == {"VersionId": "v2", "RequestCharged": "requester"}
        assert all(slot is not None and "Key" not in slot for slot in slots)


class TestThreading:
    def test_flush_returns_while_batch_in_flight(self) -> None:
        fake = _FakeS3Client()
        fake.gate = threading.Event()
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=2, on_result=results.append)
        try:
            deleter.submit(_info("a"))
            deleter.submit(_info("b"))  # auto-flush; the worker is now held on the gate
            # Reaching these asserts at all proves submit/flush returned while
            # the batch is still in flight.
            assert fake.entered.wait(5.0)
            assert results == []  # no response yet -> nothing recorded
        finally:
            # Always release the worker so a failed assert cannot leak the
            # non-daemon thread into the rest of the session.
            fake.gate.set()
            with contextlib.suppress(Exception):
                deleter.close()
        assert [r.compare_key for r in results] == ["a", "b"]

    def test_second_flush_waits_for_first(self) -> None:
        fake = _FakeS3Client()
        fake.gate = threading.Event()
        deleter = _deleter(fake, batch_size=10)
        try:
            deleter.submit(_info("a"))
            deleter.flush()  # batch 1 in flight, held on the gate
            assert fake.entered.wait(5.0)
            deleter.submit(_info("b"))
            threading.Timer(0.25, fake.gate.set).start()
            start = time.monotonic()
            deleter.flush()  # must wait out batch 1 before dispatching batch 2
            # flush() can only return this late if it waited for the gated
            # batch; without the wait it returns in microseconds.
            assert time.monotonic() - start >= 0.2
        finally:
            fake.gate.set()
            with contextlib.suppress(Exception):
                deleter.close()
        assert _keys(fake.calls) == [["a"], ["b"]]

    def test_close_waits_for_in_flight_batch(self) -> None:
        fake = _FakeS3Client()
        fake.gate = threading.Event()
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=2, on_result=results.append)
        try:
            deleter.submit(_info("a"))
            deleter.submit(_info("b"))
            assert fake.entered.wait(5.0)
            threading.Timer(0.05, fake.gate.set).start()
            deleter.close()  # blocks until the gated batch completes
        finally:
            fake.gate.set()
            with contextlib.suppress(Exception):
                deleter.close(flush=False)
        assert [r.compare_key for r in results] == ["a", "b"]
        assert deleter.succeeded == 2
        assert not _deleter_worker_alive()

    def test_results_dispatched_on_worker_thread(self) -> None:
        threads: list[int] = []
        fake = _FakeS3Client()
        deleter = _deleter(fake, on_result=lambda _r: threads.append(threading.get_ident()))
        deleter.submit(_info("a"))
        deleter.close()
        assert threads[0] != threading.get_ident()
        assert threads[0] == fake.call_threads[0]

    def test_unexpected_worker_exception_reraises_at_next_flush(self) -> None:
        fake = _FakeS3Client(script=[RuntimeError("worker boom")])
        results: list[OpResult] = []
        deleter = _deleter(fake, batch_size=10, on_result=results.append)
        deleter.submit(_info("a"))
        deleter.flush()  # batch 1 will fail with a non-boto (programming) error
        deleter.submit(_info("b"))
        with pytest.raises(RuntimeError, match="worker boom"):
            deleter.flush()
        # The failure was not converted to per-key results, and the buffered
        # key survived the failed flush.
        assert results == []
        assert (deleter.succeeded, deleter.failed) == (0, 0)
        deleter.close(flush=False)
        assert _keys(fake.calls) == [["a"]]

    def test_flush_rechunks_oversized_buffer_after_worker_error(self) -> None:
        # A re-raised worker error leaves the failed flush's keys buffered;
        # once the caller keeps submitting, the buffer exceeds batch_size and
        # must be re-split so no single DeleteObjects call ever carries more
        # than batch_size keys (1000 is an AWS hard limit).
        fake = _FakeS3Client(script=[RuntimeError("worker boom")])
        deleter = _deleter(fake, batch_size=2)
        deleter.submit(_info("a"))
        deleter.submit(_info("b"))  # auto-flush; the batch will fail in the worker
        deleter.submit(_info("c"))
        with pytest.raises(RuntimeError, match="worker boom"):
            deleter.submit(_info("d"))  # auto-flush waits batch 1 -> re-raise; c,d stay
        deleter.submit(_info("e"))  # buffer is now c,d,e: larger than batch_size
        deleter.close()
        assert _keys(fake.calls) == [["a", "b"], ["c", "d"], ["e"]]

    def test_unexpected_worker_exception_reraises_at_close(self) -> None:
        fake = _FakeS3Client(script=[RuntimeError("worker boom")])
        deleter = _deleter(fake, batch_size=10)
        deleter.submit(_info("a"))
        deleter.flush()
        with pytest.raises(RuntimeError, match="worker boom"):
            deleter.close()
        with pytest.raises(ValidationError):  # closed despite the error
            deleter.submit(_info("b"))
        assert not _deleter_worker_alive()

    def test_no_worker_thread_until_first_flush(self) -> None:
        fake = _FakeS3Client()
        deleter = _deleter(fake, batch_size=10)
        deleter.submit(_info("a"))
        assert not _deleter_worker_alive()
        deleter.close(flush=False)
        assert not _deleter_worker_alive()


class TestLifecycle:
    def test_close_is_idempotent(self) -> None:
        fake = _FakeS3Client()
        deleter = _deleter(fake)
        deleter.submit(_info("a"))
        deleter.close()
        deleter.close()
        assert len(fake.calls) == 1

    def test_submit_and_flush_after_close_raise(self) -> None:
        deleter = _deleter(_FakeS3Client())
        deleter.close()
        with pytest.raises(ValidationError, match="deleter is closed"):
            deleter.submit(_info("a"))
        with pytest.raises(ValidationError, match="deleter is closed"):
            deleter.flush()

    def test_context_manager_flushes_on_clean_exit(self) -> None:
        fake = _FakeS3Client()
        with _deleter(fake, batch_size=10) as deleter:
            deleter.submit(_info("a"))
            assert fake.calls == []
        assert _keys(fake.calls) == [["a"]]

    def test_context_manager_body_exception_abandons_buffer_but_waits_in_flight(self) -> None:
        fake = _FakeS3Client()
        fake.gate = threading.Event()
        results: list[OpResult] = []
        with pytest.raises(ValueError, match="body boom"):
            with _deleter(fake, batch_size=2, on_result=results.append) as deleter:
                deleter.submit(_info("a"))
                deleter.submit(_info("b"))  # auto-flush; held in flight on the gate
                # Started before any assert can fail, so __exit__'s close()
                # always gets the gate released and a failing assert is never
                # shadowed by the worker's gate-timeout error.
                threading.Timer(0.2, fake.gate.set).start()
                assert fake.entered.wait(5.0)
                deleter.submit(_info("c"))  # buffered; must be abandoned
                raise ValueError("body boom")
        assert _keys(fake.calls) == [["a", "b"]]  # "c" was never sent
        assert [r.compare_key for r in results] == ["a", "b"]  # the in-flight batch was awaited
