"""``reusable_after_interrupt=False``: a Ctrl-C leaves at once, with requests still out.

Nothing can cancel a request already out, so the client here is a real botocore
client whose requests a ``before-send`` stub holds until the test releases them,
as a server that does not answer would. The operation must hand the interrupt
back while they are held, start nothing new, report nothing more, and leave a
new ``S3`` able to run in the same process.
"""

from __future__ import annotations

import io
import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, Literal

import boto3
import pytest
from botocore.awsrequest import AWSResponse

from boto3_s3 import S3, IOStorage, ParallelFilter, Storage
from boto3_s3.deleter import S3Deleter
from boto3_s3.s3storage import S3Storage
from boto3_s3.storage import StorageCapability
from boto3_s3.transfer import TransferItem, Transferrer
from boto3_s3.transferconfig import TransferConfig
from boto3_s3.types import FileInfo, FileKind, OpOutcome, OpResult, TransferProgress, TransferType
from tests.utils.interrupts import interrupt_main_in, main_waits_in, skip_without_pthread_kill

_CLASSIC = TransferConfig(preferred_transfer_client="classic")
_SERIAL_REQUESTS = TransferConfig(preferred_transfer_client="classic", max_concurrency=1)
_HOLD = 30.0
_WELL_UNDER_THE_HOLD = 5.0


class _Raw(io.BytesIO):
    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        yield self.read()


class _HeldEndpoint:
    """A client whose body-carrying requests wait for ``release``, and what was sent."""

    def __init__(self, *, answered_at_once: int = 0) -> None:
        session = boto3.session.Session(
            aws_access_key_id="x", aws_secret_access_key="y", region_name="us-east-1"
        )
        self.client: Any = session.client("s3", endpoint_url="http://127.0.0.1:1")
        self.release = threading.Event()
        self.held = threading.Event()
        # Set once a request has got past the hold, answered or failed: the body
        # of a transfer cancelled by then raises as it is read.
        self.finished = threading.Event()
        self.sent: list[str] = []
        self._answered_at_once = answered_at_once
        self.client.meta.events.register("before-send.s3", self._answer)

    def _answer(self, request: Any, **_kwargs: Any) -> AWSResponse:
        url, method = request.url, request.method
        if method == "GET":
            xml = (
                b"<ListBucketResult><IsTruncated>false</IsTruncated>"
                b"<Contents><Key>p/k</Key><Size>1</Size><ETag>&quot;e&quot;</ETag>"
                b"<LastModified>2020-01-01T00:00:00.000Z</LastModified></Contents>"
                b"</ListBucketResult>"
            )
            return AWSResponse(url, 200, {}, _Raw(xml))
        if method == "POST" and "uploads" in url:
            self.sent.append("create")
            xml = (
                b"<InitiateMultipartUploadResult><Bucket>b</Bucket><Key>k</Key>"
                b"<UploadId>u</UploadId></InitiateMultipartUploadResult>"
            )
            return AWSResponse(url, 200, {}, _Raw(xml))
        if method == "DELETE":
            self.sent.append("abort")
            return AWSResponse(url, 204, {}, _Raw(b""))
        self.sent.append("request")
        try:
            if self.sent.count("request") > self._answered_at_once:
                self.held.set()
                assert self.release.wait(_HOLD)
            # What the HTTP layer would read, a progress report's occasion.
            body = request.body
            if hasattr(body, "read"):
                while body.read(64 * 1024):
                    pass
        finally:
            self.finished.set()
        if "delete" in url:
            return AWSResponse(url, 200, {}, _Raw(b"<DeleteResult></DeleteResult>"))
        return AWSResponse(url, 200, {"ETag": '"e"'}, _Raw(b""))


def _thread_ended(name: str) -> bool:
    """Whether the threads named *name* end within a few seconds."""
    return _comes_true(lambda: not any(name in thread.name for thread in threading.enumerate()))


def _comes_true(check: Callable[[], object]) -> bool:
    """Whether *check* comes true within a few seconds."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.01)
    return False


def _settled(transferrer: Transferrer) -> bool:
    """Whether every transfer *transferrer* accepted has run its done subscribers."""
    return _comes_true(lambda: not transferrer._futures)  # pyright: ignore[reportPrivateUsage]


def _works_in_a_new_s3(source: Path) -> None:
    fresh = _HeldEndpoint()
    fresh.release.set()
    results: list[OpResult] = []
    S3(reusable_after_interrupt=False).cp(
        source,
        S3Storage("s3://b/again", client=fresh.client),
        transfer_config=_CLASSIC,
        on_result=results.append,
    )
    assert [result.outcome for result in results] == [OpOutcome.SUCCEEDED]


def _file(tmp_path: Path, name: str = "f", size: int = 1024) -> Path:
    path = tmp_path / name
    with path.open("wb") as file:
        file.truncate(size)
    return path


def _interrupt_main_once_held(endpoint: _HeldEndpoint) -> None:
    """From another thread: a real SIGINT to the main thread, with a request held."""
    main = threading.main_thread().ident
    assert main is not None
    assert endpoint.held.wait(_HOLD)
    signal.pthread_kill(main, signal.SIGINT)


def _cp(s3: S3, endpoint: _HeldEndpoint, tmp_path: Path, **callbacks: Any) -> None:
    target = S3Storage("s3://b/k", client=endpoint.client)
    s3.cp(_file(tmp_path), target, transfer_config=_CLASSIC, **callbacks)


def _cp_stream(s3: S3, endpoint: _HeldEndpoint, tmp_path: Path, **callbacks: Any) -> None:
    target = S3Storage("s3://b/k", client=endpoint.client)
    s3.cp(IOStorage(io.BytesIO(b"x")), target, transfer_config=_CLASSIC, **callbacks)


def _sync(s3: S3, endpoint: _HeldEndpoint, tmp_path: Path, **callbacks: Any) -> None:
    source = tmp_path / "tree"
    source.mkdir()
    _file(source)
    target = S3Storage("s3://b/p/", client=endpoint.client)
    s3.sync(source, target, transfer_config=_CLASSIC, **callbacks)


def _rm(s3: S3, endpoint: _HeldEndpoint, tmp_path: Path, **callbacks: Any) -> None:
    callbacks.pop("on_progress")
    target = S3Storage("s3://b/p/", client=endpoint.client)
    s3.rm(target, recursive=True, transfer_config=_CLASSIC, **callbacks)


@skip_without_pthread_kill
class TestCtrlC:
    @pytest.mark.parametrize("route", [_cp, _cp_stream, _sync, _rm])
    def test_it_leaves_with_the_request_out_and_a_new_s3_works(
        self, route: Callable[..., None], tmp_path: Path
    ) -> None:
        endpoint = _HeldEndpoint()
        results: list[OpResult] = []
        progress: list[TransferProgress] = []
        threading.Thread(target=_interrupt_main_once_held, args=(endpoint,)).start()
        started = time.monotonic()
        try:
            with pytest.raises(KeyboardInterrupt):
                route(
                    S3(reusable_after_interrupt=False),
                    endpoint,
                    tmp_path,
                    on_result=results.append,
                    on_progress=progress.append,
                )
            assert time.monotonic() - started < _WELL_UNDER_THE_HOLD
            _works_in_a_new_s3(_file(tmp_path, "again"))
            reported = len(results), [record.bytes_done for record in progress]
        finally:
            endpoint.release.set()
        assert endpoint.finished.wait(_HOLD)
        assert _thread_ended("boto3-s3-deleter")
        # The caller has gone on: its callbacks get nothing more, though the
        # held request's record would come only after it has finished.
        time.sleep(0.3)
        assert (len(results), [record.bytes_done for record in progress]) == reported

    def test_one_in_the_wait_cancels_what_has_not_started(self, tmp_path: Path) -> None:
        endpoint = _HeldEndpoint()

        def interrupt_the_wait() -> None:
            # The Transferrer's own wait: its ``__exit__`` sleeping on a Wake.
            assert endpoint.held.wait(_HOLD)
            interrupt_main_in("__exit__", waiting_in="wait_until")

        transferrer = Transferrer(
            TransferType.UPLOAD,
            endpoint.client,
            transfer_config=_SERIAL_REQUESTS,
            reusable_after_interrupt=False,
        )
        threading.Thread(target=interrupt_the_wait).start()
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            with transferrer:
                for index in range(5):
                    transferrer.submit(_item(_file(tmp_path, f"f{index}"), f"k{index}"))
        assert time.monotonic() - started < _WELL_UNDER_THE_HOLD
        endpoint.release.set()
        assert _settled(transferrer)
        assert endpoint.sent == ["request"]

    def test_the_default_posture_waits_for_the_request(self, tmp_path: Path) -> None:
        source = _file(tmp_path)
        endpoint = _HeldEndpoint()
        started = time.monotonic()
        threading.Timer(0.3, endpoint.release.set).start()
        S3().cp(source, S3Storage("s3://b/k", client=endpoint.client), transfer_config=_CLASSIC)
        assert time.monotonic() - started >= 0.2


class _NotingPool(ThreadPoolExecutor):
    """A pool that tells when a decision has been handed to it."""

    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self.submitted = threading.Event()

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        future = super().submit(fn, *args, **kwargs)
        self.submitted.set()
        return future


@skip_without_pthread_kill
class TestCtrlCWithDecisionsOnTheCallersPool:
    """A ``ParallelFilter``'s decisions: the pool is the caller's, and no shutdown here ends it."""

    def _sync(
        self, reusable: bool, decide: Callable[[Any], bool], pool: Any, tmp_path: Path
    ) -> None:
        source = tmp_path / "tree"
        source.mkdir()
        _file(source)
        endpoint = _HeldEndpoint()
        endpoint.release.set()
        S3(reusable_after_interrupt=reusable).sync(
            source,
            S3Storage("s3://b/p/", client=endpoint.client),
            create_filter=ParallelFilter(decide, pool),
            transfer_config=_CLASSIC,
        )

    def _interrupt_once(self, event: threading.Event) -> None:
        # With the pool's one place taken, the next wait is the loop's for a result,
        # by when it has noted what the pool was just handed.
        assert event.wait(_HOLD)
        interrupt_main_in("run_sync_pairs", waiting_in="wait_until")

    def test_it_leaves_with_a_decision_running(self, tmp_path: Path) -> None:
        running = threading.Event()
        release = threading.Event()

        def decide(_pair: Any) -> bool:
            running.set()
            release.wait(_HOLD)
            return False

        with _NotingPool() as pool:
            threading.Thread(target=self._interrupt_once, args=(running,)).start()
            started = time.monotonic()
            try:
                with pytest.raises(KeyboardInterrupt):
                    self._sync(False, decide, pool, tmp_path)
                assert time.monotonic() - started < _WELL_UNDER_THE_HOLD
            finally:
                release.set()

    def test_a_decision_still_queued_never_runs(self, tmp_path: Path) -> None:
        release = threading.Event()
        decided: list[Any] = []

        def decide(pair: Any) -> bool:
            decided.append(pair)
            return False

        with _NotingPool() as pool:
            # The only worker is taken, so the decision waits in the pool's queue.
            pool.submit(release.wait, _HOLD)
            pool.submitted.clear()
            threading.Thread(target=self._interrupt_once, args=(pool.submitted,)).start()
            try:
                with pytest.raises(KeyboardInterrupt):
                    self._sync(False, decide, pool, tmp_path)
            finally:
                release.set()
        # The pool has run everything it was left with.
        assert decided == []

    def test_the_default_posture_waits_for_a_decision_running(self, tmp_path: Path) -> None:
        running = threading.Event()
        interrupted = threading.Event()
        finished = threading.Event()

        def decide(_pair: Any) -> bool:
            running.set()
            assert interrupted.wait(_HOLD)
            time.sleep(0.2)
            finished.set()
            return False

        def interrupt() -> None:
            self._interrupt_once(running)
            interrupted.set()

        with _NotingPool() as pool:
            threading.Thread(target=interrupt).start()
            with pytest.raises(KeyboardInterrupt):
                self._sync(True, decide, pool, tmp_path)
            assert finished.is_set()


class TestInterruptReachingTheExit:
    def test_nothing_is_reported_after_it_when_no_transfer_ran(self) -> None:
        # A dry run builds no manager, and a warning can still come from the walk.
        results: list[OpResult] = []
        transferrer = Transferrer(
            TransferType.UPLOAD,
            _HeldEndpoint().client,
            on_result=results.append,
            reusable_after_interrupt=False,
        )
        with pytest.raises(KeyboardInterrupt):
            with transferrer:
                transferrer.warner.warn("from before the interrupt")
                raise KeyboardInterrupt
        transferrer.warner.warn("from the page pull")
        assert [result.outcome for result in results] == [OpOutcome.WARNED]

    def test_what_has_not_started_never_starts(self, tmp_path: Path) -> None:
        endpoint = _HeldEndpoint()
        results: list[OpResult] = []
        transferrer = Transferrer(
            TransferType.UPLOAD,
            endpoint.client,
            transfer_config=_SERIAL_REQUESTS,
            on_result=results.append,
            reusable_after_interrupt=False,
        )
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            with transferrer:
                for index in range(5):
                    transferrer.submit(_item(_file(tmp_path, f"f{index}"), f"k{index}"))
                assert endpoint.held.wait(_HOLD)
                raise KeyboardInterrupt
        assert time.monotonic() - started < _WELL_UNDER_THE_HOLD
        endpoint.release.set()
        assert _settled(transferrer)
        assert endpoint.sent == ["request"]
        assert results == []

    def test_a_multipart_upload_is_aborted_behind_the_caller(self, tmp_path: Path) -> None:
        source = _file(tmp_path, size=16 * 1024 * 1024)
        endpoint = _HeldEndpoint()
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            with Transferrer(
                TransferType.UPLOAD,
                endpoint.client,
                transfer_config=_SERIAL_REQUESTS,
                reusable_after_interrupt=False,
            ) as transferrer:
                transferrer.submit(_item(source, "big"))
                assert endpoint.held.wait(_HOLD)
                raise KeyboardInterrupt
        assert time.monotonic() - started < _WELL_UNDER_THE_HOLD
        assert "abort" not in endpoint.sent
        endpoint.release.set()
        assert _comes_true(lambda: "abort" in endpoint.sent)


class _InterruptedCleanup:
    """A manager whose cleanup a Ctrl-C cuts short, as a second press would."""

    def shutdown(self) -> None:
        raise KeyboardInterrupt

    def __exit__(self, *_args: Any) -> None:
        raise KeyboardInterrupt


class TestNothingIsReportedOnceTheCallerHasTheException:
    """Under either posture, the exception a run ends with reaches the caller
    only once its callbacks have been stopped: a warning the walk sends after
    that, as a page still in flight would, reaches nobody."""

    def _transferrer(self, reusable: bool, on_result: Callable[[OpResult], None]) -> Transferrer:
        return Transferrer(
            TransferType.UPLOAD,
            _HeldEndpoint().client,
            on_result=on_result,
            reusable_after_interrupt=reusable,
        )

    @pytest.mark.parametrize("reusable", [True, False])
    def test_the_blocks_own(self, reusable: bool) -> None:
        results: list[OpResult] = []
        transferrer = self._transferrer(reusable, results.append)
        with pytest.raises(ValueError, match="fatal"):
            with transferrer:
                transferrer.warner.warn("before")
                raise ValueError("fatal")
        transferrer.warner.warn("after")
        assert [result.outcome for result in results] == [OpOutcome.WARNED]

    @pytest.mark.parametrize("reusable", [True, False])
    def test_one_noted_on_a_worker(self, reusable: bool) -> None:
        calls: list[OpResult] = []

        def on_result(result: OpResult) -> None:
            calls.append(result)
            raise ValueError("from on_result")

        transferrer = self._transferrer(reusable, on_result)
        with pytest.raises(ValueError, match="from on_result"):
            with transferrer:
                worker = threading.Thread(target=transferrer.warner.warn, args=("noted",))
                worker.start()
                worker.join()
        # On this thread a call would raise its exception on the spot.
        transferrer.warner.warn("after")
        assert len(calls) == 1

    @pytest.mark.parametrize("reusable", [True, False])
    def test_an_interrupt_out_of_the_cleanup(self, reusable: bool) -> None:
        results: list[OpResult] = []
        transferrer = self._transferrer(reusable, results.append)
        with pytest.raises(KeyboardInterrupt):
            with transferrer:
                transferrer._manager = _InterruptedCleanup()  # pyright: ignore[reportPrivateUsage]
                raise ValueError("fatal")
        transferrer.warner.warn("after")
        assert results == []


class TestEverythingElseIsAsUnderTheDefault:
    def test_any_other_exception_still_waits_and_names_itself(self, tmp_path: Path) -> None:
        # The interrupt alone leaves at once: another failure's records still
        # come, naming it, and its caller finds nothing running.
        endpoint = _HeldEndpoint()
        results: list[OpResult] = []
        started = time.monotonic()
        threading.Timer(0.3, endpoint.release.set).start()
        with pytest.raises(ValueError, match="boom"):
            with Transferrer(
                TransferType.UPLOAD,
                endpoint.client,
                transfer_config=_SERIAL_REQUESTS,
                on_result=results.append,
                reusable_after_interrupt=False,
            ) as transferrer:
                transferrer.submit(_item(_file(tmp_path), "k0"))
                transferrer.submit(_item(_file(tmp_path, "g"), "k1"))
                assert endpoint.held.wait(_HOLD)
                raise ValueError("boom")
        assert time.monotonic() - started >= 0.2
        cancelled = [result for result in results if result.outcome is OpOutcome.CANCELLED]
        assert cancelled
        assert "boom" in str(cancelled[0].error)

    @pytest.mark.parametrize("reusable", [True, False])
    def test_an_exception_noted_on_a_worker_reaches_the_caller(
        self, reusable: bool, tmp_path: Path
    ) -> None:
        # What on_result raises is noted on a worker and raised from __exit__.
        endpoint = _HeldEndpoint()
        endpoint.release.set()

        def on_result(_result: OpResult) -> None:
            raise ValueError("from on_result")

        with pytest.raises(ValueError, match="from on_result"):
            S3(reusable_after_interrupt=reusable).cp(
                _file(tmp_path),
                S3Storage("s3://b/k", client=endpoint.client),
                transfer_config=_CLASSIC,
                on_result=on_result,
            )

    @pytest.mark.parametrize("reusable", [True, False])
    def test_one_noted_in_the_wait_cancels_what_has_not_started(
        self, reusable: bool, tmp_path: Path
    ) -> None:
        # The first record's on_result raises once the caller waits in __exit__.
        # The one request place is taken until then, so the second request goes
        # out after it and is held, and the third waits behind that one.
        endpoint = _HeldEndpoint(answered_at_once=1)
        calls: list[OpResult] = []

        def on_result(result: OpResult) -> None:
            calls.append(result)
            if len(calls) == 1:
                main_waits_in("__exit__", waiting_in="wait_until")
            raise ValueError("from on_result")

        transferrer = Transferrer(
            TransferType.UPLOAD,
            endpoint.client,
            transfer_config=_SERIAL_REQUESTS,
            on_result=on_result,
            reusable_after_interrupt=reusable,
        )

        def release_once_cancelled() -> None:
            _comes_true(lambda: transferrer._cancel_initiated)  # pyright: ignore[reportPrivateUsage]
            endpoint.release.set()

        threading.Thread(target=release_once_cancelled).start()
        with pytest.raises(ValueError, match="from on_result"):
            with transferrer:
                for index in range(3):
                    transferrer.submit(_item(_file(tmp_path, f"f{index}"), f"k{index}"))
        assert endpoint.sent.count("request") < 3

    def test_a_future_settled_before_it_is_tracked_is_not_tracked(self) -> None:
        # The CRT manager runs an item's done subscribers inside its submit when
        # the item fails there, with a future that then never reports done.
        class _NeverDone:
            meta = SimpleNamespace(user_context={})

            def done(self) -> bool:
                return False

        endpoint = _HeldEndpoint()
        transferrer = Transferrer(TransferType.UPLOAD, endpoint.client)
        future = _NeverDone()
        transferrer._forget_future(future)  # pyright: ignore[reportPrivateUsage]
        transferrer._track_future(future)  # pyright: ignore[reportPrivateUsage]
        assert not transferrer._futures  # pyright: ignore[reportPrivateUsage]

    def test_no_progress_is_delivered_after_leaving(self) -> None:
        progress: list[TransferProgress] = []
        endpoint = _HeldEndpoint()
        transferrer = Transferrer(TransferType.UPLOAD, endpoint.client, on_progress=progress.append)
        transferrer._left = True  # pyright: ignore[reportPrivateUsage]
        record = TransferProgress(
            transfer_type=TransferType.UPLOAD, compare_key="k", bytes_done=1, bytes_total=1
        )
        transferrer._deliver_progress(record)  # pyright: ignore[reportPrivateUsage]
        assert progress == []

    @pytest.mark.parametrize("reusable", [True, False])
    def test_a_done_subscribers_exit_reaches_the_caller(
        self, reusable: bool, tmp_path: Path
    ) -> None:
        # A reader whose close raises SystemExit on a worker: its item is never
        # forgotten by the subscribers after it, which must not hold the run.
        class _Reader(io.BytesIO):
            def close(self) -> None:
                raise SystemExit(9)

        class _Source(Storage):
            scheme = "mem"
            capabilities = StorageCapability.OPEN_READ | StorageCapability.SORTABLE_SCAN

            def as_text(self) -> str:
                return "mem://x"

            def open(
                self, key: str, mode: Literal["rb", "wb"], *, size: int | None = None
            ) -> BinaryIO:
                return _Reader(b"x")

            def scan_pages(self, options: Any) -> Any:
                yield [FileInfo(key="k", kind=FileKind.FILE, size=1, compare_key="k")]

            def get_fileinfo(self, key: str = "", *, on_warning: object = None) -> FileInfo | None:
                return FileInfo(key=key, kind=FileKind.FILE, size=1, compare_key="k")

            def delete(self, info: FileInfo) -> None:
                raise NotImplementedError

        endpoint = _HeldEndpoint()
        endpoint.release.set()
        with pytest.raises(SystemExit):
            S3(reusable_after_interrupt=reusable).cp(
                _Source(),
                S3Storage("s3://b/p/", client=endpoint.client),
                recursive=True,
                transfer_config=_CLASSIC,
            )


def _item(source: Path, key: str) -> TransferItem:
    return TransferItem(
        compare_key=key,
        size=source.stat().st_size,
        src_path=str(source),
        dest_bucket="b",
        dest_key=key,
    )


class TestTheDeleter:
    def test_an_interrupt_leaves_with_the_batch_out(self) -> None:
        endpoint = _HeldEndpoint()
        storage = S3Storage("s3://b/", client=endpoint.client)
        results: list[OpResult] = []
        started = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            with S3Deleter(
                storage,
                on_result=results.append,
                transfer_config=_CLASSIC,
                reusable_after_interrupt=False,
            ) as deleter:
                deleter.submit(FileInfo(key="k", storage=storage))
                deleter.flush()
                assert endpoint.held.wait(_HOLD)
                raise KeyboardInterrupt
        assert time.monotonic() - started < _WELL_UNDER_THE_HOLD
        endpoint.release.set()
        assert _thread_ended("boto3-s3-deleter")
        assert results == []
