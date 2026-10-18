"""Unit tests for ``S3.rm`` orchestration and ``rm_filter_root``.

The fake client covers the three wire surfaces rm uses: ListObjectsV2
pagination (enumerating paths), a single blind DeleteObject (the
non-recursive key path), and batched DeleteObjects (via S3Deleter).
aws-cli parity facts asserted here:

- a recursive prefix is normalized to end with "/" before listing, so
  ``rm s3://b/data --recursive`` never touches ``data-sibling.txt``;
- a keyless non-recursive rm sweeps only zero-byte "folder marker"
  objects (any depth);
- the single-key path is blind: no listing, no existence check;
- item failures aggregate into BatchError, pre-item failures (the
  listing itself) propagate as their category error.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import threading
from typing import Any

import pytest
from botocore.exceptions import ClientError, ConnectionClosedError

from boto3_s3 import (
    S3,
    BatchError,
    CancelledError,
    CancelToken,
    FileInfo,
    GlobFilter,
    NotFoundError,
    OpOutcome,
    OpResult,
    S3Storage,
    ValidationError,
    rm_filter_root,
)
from tests.utils.fakes3 import client_error, crt_sender_factory
from tests.utils.interrupts import interrupt_main_in, skip_without_pthread_kill

_MTIME = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)


class _FakePaginator:
    def __init__(self, owner: _FakeS3Client) -> None:
        self._owner = owner

    def paginate(self, **kwargs: Any) -> Any:
        self._owner.list_calls.append(kwargs)
        if self._owner.list_error is not None:
            raise self._owner.list_error
        return iter(self._owner.pages)


class _FakeS3Client:
    """Fake covering the rm wire surface (list / delete_object / delete_objects)."""

    def __init__(
        self,
        pages: list[dict[str, Any]] | None = None,
        *,
        list_error: Exception | None = None,
        delete_object_error: Exception | None = None,
        delete_objects_errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.pages = pages or []
        self.list_error = list_error
        self.delete_object_error = delete_object_error
        self.delete_objects_errors = delete_objects_errors or []
        self.list_calls: list[dict[str, Any]] = []
        self.delete_object_calls: list[dict[str, Any]] = []
        self.delete_objects_calls: list[dict[str, Any]] = []

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator(self)

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_object_calls.append(kwargs)
        if self.delete_object_error is not None:
            raise self.delete_object_error
        return {}

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_objects_calls.append(kwargs)
        return {"Errors": self.delete_objects_errors}


def _obj(key: str, size: int = 1) -> dict[str, Any]:
    return {"Key": key, "Size": size, "LastModified": _MTIME}


def _rm(
    url: str,
    client: _FakeS3Client,
    **kwargs: Any,
) -> list[OpResult]:
    """Run S3().rm with a result collector and return the OpResults.

    ``page_size`` is storage config, so it is passed to the ``S3Storage``
    constructor.
    """
    results: list[OpResult] = []
    page_size = kwargs.pop("page_size", 1000)
    storage = S3Storage(url, client=client, page_size=page_size)
    S3().rm(storage, on_result=results.append, **kwargs)
    return results


class TestRmFilterRoot:
    @pytest.mark.parametrize(
        ("key", "recursive", "expected"),
        [
            ("", False, ""),  # bucket-root marker sweep
            ("", True, ""),  # full-bucket recursive
            ("a.txt", False, ""),  # top-level key roots at bucket
            ("data/a.txt", False, "data/"),  # parent "directory"
            ("data/", False, "data/"),  # explicit marker target roots at itself
            # aws joins the pattern onto the parent with os.path.join, which adds
            # no second "/" after a parent already ending in one, so the entry
            # is matched as "/b" - never as "b" under an "a//" root.
            ("a//b", False, "a/"),
            ("x/a//b", False, "x/a/"),
            ("/b", False, ""),  # a leading "/" is the whole parent: roots at the bucket
            ("//b", False, "/"),
            ("data", True, "data/"),  # recursive normalizes to "/"
            ("data/", True, "data/"),
            ("data/sub", True, "data/sub/"),
        ],
    )
    def test_root(self, key: str, recursive: bool, expected: str) -> None:
        assert rm_filter_root(key, recursive=recursive) == expected


class TestRmSingleKey:
    def test_doubled_slash_key_gets_the_verdict_aws_gives_it(self) -> None:
        # Under rm s3://b/a//b aws matches the entry as "/b" (the pattern is
        # os.path.join-ed onto root "a/"): --exclude b keeps it, and
        # --exclude '*' --include b drops it (pinned aws 2.36.40, dryrun).
        kept = _rm("s3://b/a//b", _FakeS3Client(), dryrun=True, filter=GlobFilter().exclude("b"))
        assert [(r.outcome, r.compare_key) for r in kept] == [(OpOutcome.DRYRUN, "/b")]
        dropped = _rm(
            "s3://b/a//b",
            _FakeS3Client(),
            dryrun=True,
            filter=GlobFilter().exclude("*").include("b"),
        )
        assert dropped == []

    def test_blind_delete_no_listing(self) -> None:
        client = _FakeS3Client()
        results = _rm("s3://b/data/a.txt", client)
        assert client.list_calls == []
        assert client.delete_object_calls == [{"Bucket": "b", "Key": "data/a.txt"}]
        # The record's compare_key is relative to the filter root ("data/"); the
        # full key stays reachable via src_info.key.
        assert [(r.compare_key, r.outcome) for r in results] == [("a.txt", OpOutcome.SUCCEEDED)]

    def test_result_carries_src_info_and_storage(self) -> None:
        # The deleted object rides through on src / src_info / src_storage (aws's
        # delete result shape is src=path, dest=None), so an app can identify and
        # re-reach exactly what was removed.
        client = _FakeS3Client()
        results = _rm("s3://b/data/a.txt", client)
        assert results[0].src == "s3://b/data/a.txt"
        assert results[0].src_info is not None and results[0].src_info.key == "data/a.txt"
        assert isinstance(results[0].src_storage, S3Storage)
        assert results[0].dest_info is None

    def test_request_payer_forwarded(self) -> None:
        client = _FakeS3Client()
        _rm("s3://b/k", client, request_payer="requester")
        assert client.delete_object_calls == [
            {"Bucket": "b", "Key": "k", "RequestPayer": "requester"}
        ]

    def test_trailing_slash_key_is_a_blind_marker_delete(self) -> None:
        # aws: "rm s3://b/dir/" short-circuits to one DeleteObject of "dir/".
        client = _FakeS3Client()
        _rm("s3://b/dir/", client)
        assert client.list_calls == []
        assert client.delete_object_calls == [{"Bucket": "b", "Key": "dir/"}]

    def test_dryrun_makes_no_api_calls(self) -> None:
        client = _FakeS3Client()
        results = _rm("s3://b/no-such", client, dryrun=True)
        assert client.delete_object_calls == []
        assert client.list_calls == []
        assert [(r.compare_key, r.outcome) for r in results] == [("no-such", OpOutcome.DRYRUN)]

    def test_the_single_key_is_reported_as_it_was_written(self) -> None:
        # aws-cli's single-object route hands the argument through, so an
        # Outposts access-point key set off with ":" prints with its colon:
        # `(dryrun) delete: s3://<arn>:k` (measured against the pinned aws
        # 2.36.40, offline). A listed key is always `bucket/key`.
        arn = "arn:aws:s3-outposts:us-east-1:123456789012:outpost/op-1/accesspoint/ap"
        results = _rm(f"s3://{arn}:k", _FakeS3Client(), dryrun=True)
        assert [r.src for r in results] == [f"s3://{arn}:k"]
        assert results[0].src_info is not None and results[0].src_info.key == "k"
        assert [r.src for r in _rm("s3://b/k", _FakeS3Client(), dryrun=True)] == ["s3://b/k"]
        # A name the pattern cuts short (`my_ap` is the access point `my` and
        # the key `_ap`) prints as typed too: `(dryrun) delete: s3://<arn>/my_ap`.
        cut = arn[: -len("ap")] + "my_ap"
        results = _rm(f"s3://{cut}", _FakeS3Client(), dryrun=True)
        assert [r.src for r in results] == [f"s3://{cut}"]
        assert results[0].src_info is not None and results[0].src_info.key == "_ap"

    def test_failure_emits_failed_and_raises_batch_error(self) -> None:
        client = _FakeS3Client(
            delete_object_error=client_error("NoSuchBucket", 404, "DeleteObject")
        )
        results: list[OpResult] = []
        storage = S3Storage("s3://b-no-such/k", client=client)
        with pytest.raises(BatchError) as exc_info:
            S3().rm(storage, on_result=results.append)
        assert (exc_info.value.succeeded, exc_info.value.failed) == (0, 1)
        assert isinstance(exc_info.value.__cause__, NotFoundError)
        assert isinstance(exc_info.value.__cause__.__cause__, ClientError)
        assert [(r.compare_key, r.outcome) for r in results] == [("k", OpOutcome.FAILED)]
        assert isinstance(results[0].error, NotFoundError)

    def test_a_delete_that_dies_without_a_response_is_lost_like_aws(self) -> None:
        # aws-cli's task loses it: no line, rc 0 (measured against the pinned
        # aws through a 127.0.0.1 server that closes every connection).
        client = _FakeS3Client(delete_object_error=ConnectionClosedError(endpoint_url="http://h/"))
        assert _rm("s3://b/k", client) == []
        assert client.delete_object_calls == [{"Bucket": "b", "Key": "k"}]

    def test_excluded_by_matcher_is_silent(self) -> None:
        client = _FakeS3Client()
        keep = GlobFilter().exclude("*").compile()
        results = _rm("s3://b/data/a.txt", client, filter=keep)
        assert client.delete_object_calls == []
        assert results == []

    def test_matcher_sees_parent_relative_key(self) -> None:
        # Root of "data/a.txt" is "data/": the pattern matches the basename.
        client = _FakeS3Client()
        keep = GlobFilter().exclude("a.*").compile()
        assert _rm("s3://b/data/a.txt", client, filter=keep) == []
        assert client.delete_object_calls == []


class TestRmCancellation:
    def test_graceful_cancel_drains_inflight_batch_and_drops_buffer(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj(f"k/{i:04}") for i in range(1001)]}])
        storage = S3Storage("s3://b/k/", client=client)
        token = CancelToken()
        results: list[OpResult] = []

        def cancel_from_first(result: OpResult) -> None:
            results.append(result)
            if len(results) == 1:
                token.cancel()

        with pytest.raises(CancelledError):
            S3().rm(
                storage,
                recursive=True,
                cancel_token=token,
                on_result=cancel_from_first,
            )

        assert len(client.delete_objects_calls) == 1
        assert len(client.delete_objects_calls[0]["Delete"]["Objects"]) == 1000
        assert len(results) == 1000

    def test_blind_path_cancel_from_the_failed_record_wins_over_batcherror(self) -> None:
        # The batched path and the transfers poll the token before rolling the
        # failures into BatchError; the blind single-key path does the same.
        client = _FakeS3Client(
            delete_object_error=client_error("AccessDenied", 403, "DeleteObject")
        )
        storage = S3Storage("s3://b/k", client=client)
        token = CancelToken()
        results: list[OpResult] = []

        def cancel_on_failure(result: OpResult) -> None:
            results.append(result)
            if result.outcome is OpOutcome.FAILED:
                token.cancel()

        with pytest.raises(CancelledError):
            S3().rm(storage, cancel_token=token, on_result=cancel_on_failure)

        assert [r.outcome for r in results] == [OpOutcome.FAILED]
        assert len(client.delete_object_calls) == 1

    def test_dryrun_raises_on_cancelled_token_with_empty_listing(self) -> None:
        client = _FakeS3Client([{"Contents": []}])
        storage = S3Storage("s3://b/k/", client=client)
        token = CancelToken()
        token.cancel()

        with pytest.raises(CancelledError):
            S3().rm(storage, recursive=True, dryrun=True, cancel_token=token)


class TestRmRecursive:
    def test_deletes_all_keys_in_batches(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("data/a"), _obj("data/b")]}])
        results = _rm("s3://b/data/", client, recursive=True)
        assert client.delete_objects_calls == [
            {
                "Bucket": "b",
                "Delete": {
                    "Objects": [{"Key": "data/a"}, {"Key": "data/b"}],
                    "Quiet": True,
                },
            }
        ]
        assert [(r.compare_key, r.outcome) for r in results] == [
            ("a", OpOutcome.SUCCEEDED),
            ("b", OpOutcome.SUCCEEDED),
        ]

    def test_prefix_without_slash_is_normalized(self) -> None:
        # aws-cli appends "/" for recursive targets (FileFormat.s3_format):
        # "data" must list under "data/", never matching "data-sibling.txt".
        client = _FakeS3Client([{"Contents": [_obj("data/a")]}])
        _rm("s3://b/data", client, recursive=True)
        assert client.list_calls[0]["Prefix"] == "data/"

    def test_listing_knobs_forwarded(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/a")]}])
        _rm("s3://b/p/", client, recursive=True, page_size=5, request_payer="requester")
        call = client.list_calls[0]
        assert call["PaginationConfig"] == {"PageSize": 5}
        assert call["RequestPayer"] == "requester"
        assert "Delimiter" not in call
        assert client.delete_objects_calls[0]["RequestPayer"] == "requester"

    def test_markers_are_deleted_too(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/m/", 0), _obj("p/a")]}])
        results = _rm("s3://b/p/", client, recursive=True)
        assert [r.compare_key for r in results] == ["m/", "a"]

    def test_matcher_sees_prefix_relative_key(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("data/a.txt"), _obj("data/sub/x")]}])
        keep = GlobFilter().exclude("sub/*").compile()
        results = _rm("s3://b/data/", client, recursive=True, filter=keep)
        assert [r.compare_key for r in results] == ["a.txt"]

    def test_interleaved_ordering_is_last_match_wins(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/a.txt"), _obj("p/b.bin")]}])
        keep = GlobFilter().include("*.txt").exclude("*").compile()
        assert _rm("s3://b/p/", client, recursive=True, filter=keep) == []
        keep = GlobFilter().exclude("*").include("*.txt").compile()
        results = _rm("s3://b/p/", client, recursive=True, filter=keep)
        assert [r.compare_key for r in results] == ["a.txt"]

    def test_callable_filter_receives_fileinfo(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/big", 10), _obj("p/empty", 0)]}])
        results = _rm(
            "s3://b/p/",
            client,
            recursive=True,
            filter=lambda info: info.size == 0,
        )
        assert [r.compare_key for r in results] == ["empty"]

    def test_dryrun_lists_but_never_deletes(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/a"), _obj("p/b")]}])
        results = _rm("s3://b/p/", client, recursive=True, dryrun=True)
        assert len(client.list_calls) == 1
        assert client.delete_objects_calls == []
        assert [(r.compare_key, r.outcome) for r in results] == [
            ("a", OpOutcome.DRYRUN),
            ("b", OpOutcome.DRYRUN),
        ]

    def test_zero_matches_is_a_silent_success(self) -> None:
        client = _FakeS3Client([{}])
        results = _rm("s3://b/none/", client, recursive=True)
        assert results == []
        assert client.delete_objects_calls == []

    def test_listing_failure_propagates_as_category_error(self) -> None:
        # Pre-item failures are "fatal" (aws rc 1): the category error passes
        # through untouched, not wrapped in BatchError.
        client = _FakeS3Client(list_error=client_error("NoSuchBucket", 404, "ListObjectsV2"))
        with pytest.raises(NotFoundError):
            _rm("s3://b-no-such/", client, recursive=True)
        assert client.delete_objects_calls == []

    def test_item_failures_aggregate_into_batch_error(self) -> None:
        client = _FakeS3Client(
            [{"Contents": [_obj("p/a"), _obj("p/b")]}],
            delete_objects_errors=[{"Key": "p/a", "Code": "AccessDenied", "Message": "denied"}],
        )
        results: list[OpResult] = []
        storage = S3Storage("s3://b/p/", client=client)
        with pytest.raises(BatchError) as exc_info:
            S3().rm(storage, recursive=True, on_result=results.append)
        assert (exc_info.value.succeeded, exc_info.value.failed) == (1, 1)
        outcomes = {r.compare_key: r.outcome for r in results}
        assert outcomes == {"a": OpOutcome.FAILED, "b": OpOutcome.SUCCEEDED}


@skip_without_pthread_kill
class TestRmSingleKeyInterrupted:
    """aws's single delete rides a transfer-manager worker that a Ctrl-C waits
    for, so the in-flight delete is still reported (measured: ``delete:
    s3://b/k`` before the closing line)."""

    def test_a_ctrl_c_waits_for_the_delete_and_records_it(self) -> None:
        class _InterruptedClient(_FakeS3Client):
            def delete_object(self, **kwargs: Any) -> dict[str, Any]:
                interrupt_main_in("_send_through_interrupt")
                return super().delete_object(**kwargs)

        client = _InterruptedClient()
        results: list[OpResult] = []
        with pytest.raises(KeyboardInterrupt):
            S3().rm(S3Storage("s3://b/k", client=client), on_result=results.append)
        assert client.delete_object_calls == [{"Bucket": "b", "Key": "k"}]
        assert [(r.compare_key, r.outcome) for r in results] == [("k", OpOutcome.SUCCEEDED)]

    def test_a_delete_failing_after_the_interrupt_is_not_recorded(self) -> None:
        # On aws's transfer futures a completion wins over the cancel mark and a
        # failure does not (measured: a 403 after the Ctrl-C prints only the
        # closing line).
        class _InterruptedClient(_FakeS3Client):
            def delete_object(self, **kwargs: Any) -> dict[str, Any]:
                interrupt_main_in("_send_through_interrupt")
                return super().delete_object(**kwargs)

        client = _InterruptedClient(
            delete_object_error=client_error("AccessDenied", 403, "DeleteObject")
        )
        results: list[OpResult] = []
        with pytest.raises(KeyboardInterrupt):
            S3().rm(S3Storage("s3://b/k", client=client), on_result=results.append)
        assert results == []

    def test_a_second_ctrl_c_still_waits_but_records_nothing(self) -> None:
        # aws's executor threads are joined as it exits, so its request still
        # completes after a second Ctrl-C - nothing more is printed (measured).
        class _InterruptedTwice(_FakeS3Client):
            def delete_object(self, **kwargs: Any) -> dict[str, Any]:
                interrupt_main_in("_send_through_interrupt")
                interrupt_main_in("_send_through_interrupt")
                return super().delete_object(**kwargs)

        client = _InterruptedTwice()
        results: list[OpResult] = []
        with pytest.raises(KeyboardInterrupt):
            S3().rm(S3Storage("s3://b/k", client=client), on_result=results.append)
        assert client.delete_object_calls == [{"Bucket": "b", "Key": "k"}]
        assert results == []

    def test_a_third_ctrl_c_stops_waiting(self) -> None:
        release = threading.Event()

        class _StallingClient(_FakeS3Client):
            def delete_object(self, **kwargs: Any) -> dict[str, Any]:
                for _ in range(3):
                    interrupt_main_in("_send_through_interrupt")
                release.wait(10)
                return super().delete_object(**kwargs)

        results: list[OpResult] = []
        try:
            with pytest.raises(KeyboardInterrupt):
                S3().rm(S3Storage("s3://b/k", client=_StallingClient()), on_result=results.append)
            assert results == []
        finally:
            release.set()

    @staticmethod
    def _interrupt_the_spawn(monkeypatch: pytest.MonkeyPatch, *, spawn: bool) -> None:
        """Raise a KeyboardInterrupt at the worker's spawn - the one C call an
        interrupt lands before or after - before it runs or once it has."""
        import _thread

        real = _thread.start_new_thread

        def start_new_thread(function: Any, args: Any) -> int:
            if spawn:
                real(function, args)
            raise KeyboardInterrupt

        monkeypatch.setattr(_thread, "start_new_thread", start_new_thread)

    def test_an_interrupt_before_the_worker_exists_goes_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No thread yet, so no request to wait for: the interrupt is not held.
        from boto3_s3 import s3 as s3_module

        monkeypatch.setattr(s3_module, "_SPAWN_GRACE", 0.2)
        self._interrupt_the_spawn(monkeypatch, spawn=False)
        client = _FakeS3Client()
        results: list[OpResult] = []
        with pytest.raises(KeyboardInterrupt):
            S3().rm(S3Storage("s3://b/k", client=client), on_result=results.append)
        assert client.delete_object_calls == []
        assert results == []

    def test_an_interrupt_once_the_worker_runs_is_held(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._interrupt_the_spawn(monkeypatch, spawn=True)
        client = _FakeS3Client()
        results: list[OpResult] = []
        with pytest.raises(KeyboardInterrupt):
            S3().rm(S3Storage("s3://b/k", client=client), on_result=results.append)
        assert client.delete_object_calls == [{"Bucket": "b", "Key": "k"}]
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]

    def test_a_worker_that_cannot_start_fails_the_delete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_start = threading.Thread.start

        def start(self: threading.Thread) -> None:
            if self.name == "boto3-s3-rm-single":
                raise RuntimeError("can't start new thread")
            real_start(self)

        monkeypatch.setattr(threading.Thread, "start", start)
        client = _FakeS3Client()
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            S3().rm(S3Storage("s3://b/k", client=client), on_result=results.append)
        assert client.delete_object_calls == []
        assert [r.outcome for r in results] == [OpOutcome.FAILED]

    @pytest.mark.skipif(sys.version_info < (3, 14), reason="thread_inherit_context is 3.14+")
    def test_the_worker_inherits_the_callers_context_when_threads_do(self) -> None:
        # Under -X thread_inherit_context=1 (the free-threaded build's default)
        # a thread starts with its starter's contextvars; the rm worker is
        # started from a throwaway thread, so it must be handed the caller's.
        code = (
            "import contextvars\n"
            "from boto3_s3 import S3, S3Storage\n"
            "from tests.lib.test_s3_rm import _FakeS3Client\n"
            "var = contextvars.ContextVar('v', default='<unset>')\n"
            "seen = []\n"
            "class Client(_FakeS3Client):\n"
            "    def delete_object(self, **kwargs):\n"
            "        seen.append(var.get())\n"
            "        return super().delete_object(**kwargs)\n"
            "var.set('caller')\n"
            "S3().rm(S3Storage('s3://b/k', client=Client()))\n"
            "print(seen)\n"
        )
        done = subprocess.run(
            [sys.executable, "-X", "thread_inherit_context=1", "-c", code],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "['caller']"

    def test_the_worker_is_a_named_thread(self) -> None:
        # Not a bare _thread thread: a current_thread() call in it (logging's
        # thread name) would leave a Dummy thread in threading.enumerate() on
        # Python 3.10 to 3.12.
        names: list[str] = []

        class _NamingClient(_FakeS3Client):
            def delete_object(self, **kwargs: Any) -> dict[str, Any]:
                names.append(threading.current_thread().name)
                return super().delete_object(**kwargs)

        S3().rm(S3Storage("s3://b/k", client=_NamingClient()))
        assert names == ["boto3-s3-rm-single"]
        assert not any(t.name.startswith("Dummy") for t in threading.enumerate())


class TestRmBucketRootSweep:
    def test_only_folder_markers_are_deleted(self) -> None:
        # aws parity: keyless non-recursive rm lists everything but deletes
        # only zero-byte "/"-terminated markers, at any depth.
        client = _FakeS3Client(
            [{"Contents": [_obj("m1/", 0), _obj("m2/sub/", 0), _obj("keep/x", 3), _obj("z", 0)]}]
        )
        results = _rm("s3://b", client)
        assert client.list_calls[0]["Prefix"] == ""
        # Bucket-root sweep roots the filter at "": compare_key equals the key.
        assert [r.compare_key for r in results] == ["m1/", "m2/sub/"]
        assert client.delete_objects_calls[0]["Delete"]["Objects"] == [
            {"Key": "m1/"},
            {"Key": "m2/sub/"},
        ]

    def test_trailing_slash_form_is_equivalent(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("m/", 0), _obj("keep/x", 3)]}])
        results = _rm("s3://b/", client)
        assert [r.compare_key for r in results] == ["m/"]

    def test_an_outposts_access_point_closed_with_a_colon_reads_as_the_key_slash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws's rm appends "/" to a keyless argument and re-reads it; the
        # Outposts pattern eats the ":" before it, so "...:accesspoint/ap:"
        # names the key "/" (measured: prefix "/" recursive, a blind delete of
        # "/" otherwise) - never the whole access point. A string argument
        # reads that way; an S3Storage is the location it was built as.
        arn = "arn:aws:s3-outposts:us-east-1:123456789012:outpost/op-1/accesspoint/ap"
        client = _FakeS3Client([{"Contents": [_obj("/x", 3)]}])
        s3 = S3()
        monkeypatch.setattr(s3, "client", lambda: client)
        results: list[OpResult] = []
        s3.rm(f"s3://{arn}:", recursive=True, dryrun=True, on_result=results.append)
        assert client.list_calls[0]["Prefix"] == "/"
        assert [r.src for r in results] == [f"s3://{arn}//x"]
        results.clear()
        s3.rm(f"s3://{arn}:", dryrun=True, on_result=results.append)
        assert [r.src for r in results] == [f"s3://{arn}:/"]
        # The scheme rm lets a caller omit names the same location.
        results.clear()
        s3.rm(f"{arn}:", recursive=True, dryrun=True, on_result=results.append)
        assert client.list_calls[-1]["Prefix"] == "/"


class TestRmTargetValidation:
    def test_service_root_is_rejected(self) -> None:
        # aws reaches the API with Bucket="" and fails botocore validation
        # (rc 1 fatal); the library rejects with the same token up front
        # instead of listing buckets.
        client = _FakeS3Client()
        storage = S3Storage("s3://", client=client)
        with pytest.raises(ValidationError, match="Invalid bucket name"):
            S3().rm(storage)
        assert client.list_calls == []

    def test_non_s3_location_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="rm accepts"):
            S3().rm(123)  # type: ignore[arg-type]

    def test_a_bytes_path_is_rejected_like_any_non_string(self) -> None:
        class _BytesPath:
            def __fspath__(self) -> bytes:
                return b"s3://b/k"

        with pytest.raises(ValidationError, match="rm accepts"):
            S3().rm(_BytesPath())  # type: ignore[arg-type]

    def test_filter_none_deletes_everything_listed(self) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/a")]}])
        results = _rm("s3://b/p/", client, recursive=True, filter=None)
        assert [r.compare_key for r in results] == ["a"]


class TestRmFileInfoOnBlindPath:
    def test_callable_filter_gets_key_only_fileinfo(self) -> None:
        # The blind path never lists, so the FileInfo handed to a callable
        # filter carries the key alone (size/mtime None).
        seen: list[FileInfo] = []

        def keep(info: FileInfo) -> bool:
            seen.append(info)
            return False

        client = _FakeS3Client()
        _rm("s3://b/data/a.txt", client, filter=keep)
        assert [(i.key, i.size, i.mtime) for i in seen] == [("data/a.txt", None, None)]


class TestCrtEngine:
    """A run whose ``TransferConfig`` selects the CRT engine deletes on the CRT
    client, single key and recursive alike, as ``aws s3 rm`` does."""

    @pytest.fixture
    def built(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        from boto3_s3 import deleter as deleter_module
        from boto3_s3 import s3 as s3_module

        built: list[Any] = []
        factory = crt_sender_factory(built)
        monkeypatch.setattr(s3_module, "crt_delete_sender", factory)
        monkeypatch.setattr(deleter_module, "crt_delete_sender", factory)
        return built

    @staticmethod
    def _crt() -> Any:
        from boto3.s3.transfer import TransferConfig

        return TransferConfig(preferred_transfer_client="crt")

    def test_a_single_key_rides_the_crt_client(self, built: list[Any]) -> None:
        client = _FakeS3Client()
        results: list[OpResult] = []
        S3().rm(
            S3Storage("s3://b/k", client=client),
            on_result=results.append,
            request_payer="requester",
            transfer_config=self._crt(),
        )
        ((sender, _, kwargs),) = built
        assert sender.calls == [
            ("DeleteObject", {"Bucket": "b", "Key": "k", "RequestPayer": "requester"})
        ]
        assert kwargs["operation"] == "rm"
        assert [r.outcome for r in results] == [OpOutcome.SUCCEEDED]

    def test_a_single_key_whose_delete_dies_fails(self, built: list[Any]) -> None:
        # aws's CRT client reports it: `delete failed: ... AWS_IO_SOCKET_CLOSED`,
        # rc 1 (measured with preferred_transfer_client = crt).
        awscrt_exceptions = pytest.importorskip("awscrt.exceptions")
        client = _FakeS3Client(delete_object_error=awscrt_exceptions.from_code(1051))
        results: list[OpResult] = []
        with pytest.raises(BatchError):
            S3().rm(
                S3Storage("s3://b/k", client=client),
                on_result=results.append,
                transfer_config=self._crt(),
            )
        assert [r.outcome for r in results] == [OpOutcome.FAILED]
        assert str(results[0].error) == "AWS_IO_SOCKET_CLOSED: socket is closed."

    def test_a_recursive_run_batches_on_it_with_the_instance_postures(
        self, built: list[Any]
    ) -> None:
        client = _FakeS3Client([{"Contents": [_obj("p/a"), _obj("p/b")]}])
        S3(crt_allow_absent_credentials=True, crt_region=None).rm(
            S3Storage("s3://b/p", client=client), recursive=True, transfer_config=self._crt()
        )
        ((sender, _, kwargs),) = built
        assert [op for op, _ in sender.calls] == ["DeleteObjects"]
        assert kwargs["crt_allow_absent_credentials"] is True
        assert kwargs["crt_region"] is None
        assert kwargs["operation"] == "rm"

    def test_a_rehearsal_builds_no_engine(self, built: list[Any]) -> None:
        _rm("s3://b/k", _FakeS3Client(), dryrun=True, transfer_config=self._crt())
        _rm("s3://b/p", _FakeS3Client(), recursive=True, dryrun=True, transfer_config=self._crt())
        assert built == []
