"""``TransferPrinter``: aws-style result lines, the ``\\r`` protocol, suppression.

The printable shapes are pinned against aws's output: result
lines render local sides cwd-relative (``./a.txt``), progress statements are
left-justified over the previous one and end with ``\\r``, and a following
result line blots the progress out with padding before its newline.

Rendering runs on the printer's dedicated thread, so every test drives the
callbacks inside ``with printer:`` - ``__exit__`` drains the queue and joins
the thread, making the captured output complete and deterministic (the
commands get the same guarantee from ``finish_transfer``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from boto3_s3.types import OpOutcome, OpResult, TransferProgress, TransferType
from boto3_s3_cli.progress import TransferPrinter


def _result(
    outcome: OpOutcome,
    *,
    transfer_type: TransferType = TransferType.UPLOAD,
    key: str = "a.txt",
    src: str | None = "s3://b/a.txt",
    dest: str | None = "s3://b/copy.txt",
    error: BaseException | None = None,
) -> OpResult:
    return OpResult(
        transfer_type=transfer_type,
        compare_key=key,
        outcome=outcome,
        src=src,
        dest=dest,
        error=error,
    )


def _progress(key: str, done: int, total: int | None) -> TransferProgress:
    return TransferProgress(
        transfer_type=TransferType.UPLOAD, compare_key=key, bytes_done=done, bytes_total=total
    )


class TestResultLines:
    def test_success_line_renders_local_sides_relative(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with TransferPrinter() as printer:
            printer.on_result(
                _result(OpOutcome.SUCCEEDED, src=str(tmp_path / "a.txt"), dest="s3://b/k")
            )
        # aws-cli's relative_path joins with the native sep (".\\a.txt" on Windows).
        assert capsys.readouterr().out == f"upload: .{os.sep}a.txt to s3://b/k\n"

    def test_stream_token_renders_verbatim(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The stream endpoint token "-" is not a local path: aws prints
        # `upload: - to s3://...`, never a cwd-relative `<cwd>/-`.
        with TransferPrinter() as printer:
            printer.on_result(_result(OpOutcome.SUCCEEDED, src="-", dest="s3://b/k"))
        assert capsys.readouterr().out == "upload: - to s3://b/k\n"

    def test_dryrun_prefix(self, capsys: pytest.CaptureFixture[str]) -> None:
        with TransferPrinter() as printer:
            printer.on_result(_result(OpOutcome.DRYRUN, transfer_type=TransferType.COPY))
        assert capsys.readouterr().out == "(dryrun) copy: s3://b/a.txt to s3://b/copy.txt\n"

    def test_failure_line_goes_to_stderr_with_the_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with TransferPrinter() as printer:
            printer.on_result(_result(OpOutcome.FAILED, error=RuntimeError("boom")))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "upload failed: s3://b/a.txt to s3://b/copy.txt boom\n"
        assert printer.failed == 1

    def test_warning_line_prints_the_body_verbatim(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with TransferPrinter() as printer:
            printer.on_result(
                _result(OpOutcome.WARNED, error=RuntimeError("Skipping file x. Not readable."))
            )
        assert capsys.readouterr().err == "warning: Skipping file x. Not readable.\n"
        assert printer.warned == 1

    def test_skipped_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        with TransferPrinter() as printer:
            printer.on_result(_result(OpOutcome.SKIPPED))
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")

    def test_cancelled_is_silent_and_not_counted_failed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws surfaces only the run's single fatal line for a cancellation
        # (measured live: a fatal mid-listing prints zero per-item lines for
        # the cancelled set, and its recorder never counts them as failures).
        with TransferPrinter() as printer:
            printer.on_result(_result(OpOutcome.CANCELLED, error=RuntimeError("fatal elsewhere")))
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")
        assert printer.failed == 0

    def test_cancelled_backfills_untransferred_remainder(self) -> None:
        # A queued-then-cancelled transfer closes its meter share like a
        # failed one, so the byte progress still reaches the expected total.
        with TransferPrinter() as printer:
            printer.on_progress(_progress("a", 0, 10))  # queued
            printer.on_result(_result(OpOutcome.CANCELLED, key="a", error=RuntimeError("fatal")))
        assert printer._done_bytes == printer._expected_bytes == 10
        assert printer._inflight == {}


class TestSuppression:
    def test_quiet_silences_everything_but_keeps_counts(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with TransferPrinter(quiet=True) as printer:
            printer.on_result(_result(OpOutcome.SUCCEEDED))
            printer.on_result(_result(OpOutcome.FAILED, error=RuntimeError("boom")))
            printer.on_result(_result(OpOutcome.WARNED, error=RuntimeError("warned")))
            printer.on_progress(_progress("a.txt", 10, 10))
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")
        assert (printer.failed, printer.warned) == (1, 1)

    def test_only_show_errors_keeps_dryrun_failures_and_warnings(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with TransferPrinter(only_show_errors=True) as printer:
            printer.on_result(_result(OpOutcome.SUCCEEDED))
            printer.on_result(_result(OpOutcome.DRYRUN))
            printer.on_result(_result(OpOutcome.FAILED, error=RuntimeError("boom")))
            printer.on_result(_result(OpOutcome.WARNED, error=RuntimeError("warned")))
        captured = capsys.readouterr()
        assert captured.out == "(dryrun) upload: s3://b/a.txt to s3://b/copy.txt\n"
        assert "upload failed:" in captured.err
        assert "warning: warned" in captured.err

    def test_no_progress_keeps_result_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        with TransferPrinter(progress=False) as printer:
            assert not printer.wants_progress
            printer.on_progress(_progress("a.txt", 512, 1024))  # bookkeeping only
            printer.on_result(_result(OpOutcome.SUCCEEDED))
        captured = capsys.readouterr()
        assert captured.out == "upload: s3://b/a.txt to s3://b/copy.txt\n"


class TestCarriageReturnProtocol:
    def test_progress_then_result_overwrites_with_padding(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with TransferPrinter() as printer:
            printer.on_progress(_progress("a.txt", 0, 1024))  # queued: bookkeeping, no paint
            printer.on_progress(_progress("a.txt", 512, 1024))
            printer.on_result(_result(OpOutcome.SUCCEEDED, src="s3://b/a", dest="s3://b/c"))
        out = capsys.readouterr().out
        # Stream shape: "<statement>\r<result padded to len(statement)>\n"
        # (aws-cli _adjust_statement_padding; the speed term is time-dependent
        # so only the stable prefix is compared).
        assert out.count("\r") == 1
        statement, line = out.split("\r", 1)
        assert statement.startswith("Completed 512 Bytes/1.0 KiB (")
        assert statement.endswith("with 1 file(s) remaining")
        assert line.startswith("upload: s3://b/a to s3://b/c")
        assert line.endswith("\n")
        assert len(line.rstrip("\n")) == len(statement)

    def test_queued_notification_does_not_paint(self, capsys: pytest.CaptureFixture[str]) -> None:
        with TransferPrinter() as printer:
            printer.on_progress(_progress("a.txt", 0, 1024))
        assert capsys.readouterr().out == ""

    def test_queued_then_skipped_reaches_zero_remaining(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A cp/mv upload/copy 412-skipped under --no-overwrite is queued
        # (on_queued counts it), then SKIPPED. The meter must reconcile it (aws
        # subtracts files_skipped) so "remaining" reaches 0 and _inflight clears.
        with TransferPrinter() as printer:
            for key in ("a", "b"):
                printer.on_progress(_progress(key, 0, 10))  # queued
            printer.on_progress(_progress("c", 0, 10))
            printer.on_result(_result(OpOutcome.SKIPPED, key="a"))
            printer.on_result(_result(OpOutcome.SKIPPED, key="b"))
            printer.on_result(_result(OpOutcome.SUCCEEDED, key="c"))
        capsys.readouterr()
        # 3 expected, 1 finished, 2 skipped -> 0 remaining, no stale in-flight.
        assert printer._expected_files - printer._finished_files - printer._skipped_files == 0
        assert printer._inflight == {}

    def test_pre_submit_skip_does_not_underflow_remaining(self) -> None:
        # A download no-overwrite skip never queues (no on_progress), so a bare
        # SKIPPED must not touch the skipped counter (which would underflow).
        with TransferPrinter() as printer:
            printer.on_progress(_progress("c", 0, 10))  # one real, queued transfer
            printer.on_result(_result(OpOutcome.SKIPPED, key="never-queued"))
        assert printer._skipped_files == 0

    def test_delete_results_do_not_underflow_remaining(self) -> None:
        # sync's deletes ride on_result alone (no queue-time progress signal),
        # so each terminal joins the expected total as it finishes - the
        # meter's "remaining" stays non-negative and the file total counts
        # deletes, as aws's queued delete results do.
        with TransferPrinter() as printer:
            printer.on_progress(_progress("c", 0, 10))  # one real, queued transfer
            for key in ("d1", "d2", "d3"):
                printer.on_result(
                    _result(OpOutcome.SUCCEEDED, transfer_type=TransferType.DELETE, key=key)
                )
            expected = printer._expected_files
            finished = printer._finished_files
            skipped = printer._skipped_files
        assert expected == 4  # the transfer plus the three deletes
        assert expected - finished - skipped == 1  # only the transfer remains

    def test_multiline_progress_uses_newlines(self, capsys: pytest.CaptureFixture[str]) -> None:
        with TransferPrinter(multiline=True) as printer:
            printer.on_progress(_progress("a.txt", 0, 1024))
            printer.on_progress(_progress("a.txt", 512, 1024))
        out = capsys.readouterr().out
        assert "\r" not in out
        assert out.endswith("file(s) remaining\n")

    def test_default_frequency_floors_rapid_repaints(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # frequency=0 (the default) floors repaints at _MIN_REPAINT_INTERVAL:
        # each repaint is a queue record and an inline write on the printer
        # thread, so unthrottled chunk-rate repaints would flood both.
        clock = iter([0.0, 1.0, 1.05, 2.0])
        monkeypatch.setattr("boto3_s3_cli.progress.time.monotonic", lambda: next(clock))
        with TransferPrinter() as printer:  # construction reads no clock
            printer.on_progress(_progress("a.txt", 1024, 4096))  # anchors 0.0, paints t=1.0
            printer.on_progress(_progress("a.txt", 2048, 4096))  # t=1.05 -> floored
            printer.on_progress(_progress("a.txt", 4096, 4096))  # t=2.0 -> paints
        assert capsys.readouterr().out.count("Completed") == 2

    def test_frequency_throttles_repaints(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = iter([0.0, 1.0, 2.0, 100.0])
        monkeypatch.setattr("boto3_s3_cli.progress.time.monotonic", lambda: next(clock))
        with TransferPrinter(frequency=60) as printer:  # construction reads no clock
            printer.on_progress(_progress("a.txt", 0, 4096))  # queued: anchors 0.0, no paint
            printer.on_progress(_progress("a.txt", 1024, 4096))  # t=1.0 -> paints (first)
            printer.on_progress(_progress("a.txt", 2048, 4096))  # t=2.0 -> throttled
            printer.on_progress(_progress("a.txt", 4096, 4096))  # t=100 -> paints
        out = capsys.readouterr().out
        assert out.count("Completed") == 2


class TestPrinterThread:
    def test_finish_is_idempotent(self, capsys: pytest.CaptureFixture[str]) -> None:
        printer = TransferPrinter()
        with printer:
            printer.on_result(_result(OpOutcome.SUCCEEDED))
        printer.finish()  # second finish: no thread left, silently a no-op
        assert capsys.readouterr().out == "upload: s3://b/a.txt to s3://b/copy.txt\n"

    def test_callbacks_after_finish_drop_records_instead_of_enqueueing(self) -> None:
        # finish() joins and discards the drain thread: a late callback (an
        # abandoned scan worker whose generator keeps warning after a fatal,
        # long after the run's records were drained) must drop its record
        # instead of feeding the dead queue - enough blocking puts at the
        # bound would deadlock the prefetch join at generator close.
        printer = TransferPrinter()
        with printer:
            pass
        for _ in range(3):
            printer.on_result(_result(OpOutcome.WARNED, error=RuntimeError("late warning")))
            assert printer._queue.qsize() == 0
        printer.on_progress(_progress("a.txt", 5, 10))
        assert printer._queue.qsize() == 0
        # The worker-side counters still count (they never depend on the
        # printer thread), only the rendering records are dropped.
        assert printer.warned == 3

    def test_dead_stream_stops_rendering_but_not_the_counters(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A closed pipe (BrokenPipeError on write) must not kill the drain:
        # rendering stops, the queue keeps draining, and the rc inputs -
        # counted on the worker side - are unaffected.
        class _DeadStream:
            def write(self, text: str) -> int:
                raise BrokenPipeError

            def flush(self) -> None:  # pragma: no cover - never reached
                raise BrokenPipeError

        monkeypatch.setattr(sys, "stdout", _DeadStream())
        with TransferPrinter() as printer:
            printer.on_result(_result(OpOutcome.SUCCEEDED))  # render raises -> output dead
            printer.on_result(_result(OpOutcome.FAILED, error=RuntimeError("boom")))
        # The FAILED line (stderr) was skipped too: one dead stream silences
        # the printer as a whole, deliberately.
        assert capsys.readouterr().err == ""
        assert (printer.failed, printer.warned) == (1, 0)

    def test_unencodable_key_does_not_silence_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws-cli's uni_print re-encodes with errors='replace' when the console
        # codepage cannot encode a key (a non-ASCII key on a cp932/ascii
        # stream), so one unencodable line does not kill the printer. A broad
        # "dead stream" classification would silence every following line.
        class _AsciiStream:
            encoding = "ascii"

            def __init__(self) -> None:
                self.written: list[str] = []

            def write(self, text: str) -> int:
                text.encode("ascii")  # raises UnicodeEncodeError on non-ASCII
                self.written.append(text)
                return len(text)

            def flush(self) -> None:
                pass

        stream = _AsciiStream()
        monkeypatch.setattr(sys, "stdout", stream)
        with TransferPrinter() as printer:
            printer.on_result(_result(OpOutcome.SUCCEEDED, src="s3://b/名前.txt", dest="s3://b/k"))
            printer.on_result(_result(OpOutcome.SUCCEEDED, src="s3://b/next", dest="s3://b/k2"))
        assert not printer._output_dead
        out = "".join(stream.written)
        # the unencodable key was '?'-replaced, and the following line survived.
        assert "upload: s3://b/??.txt to s3://b/k\n" in out
        assert "upload: s3://b/next to s3://b/k2\n" in out


class TestProgressAccounting:
    def test_speed_anchors_at_first_progress_not_construction(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # aws anchors the transfer-speed denominator at the first queued result,
        # not at printer construction (results.py _record_queued_result).
        # Construction reads no clock; the first on_progress sets the anchor, and
        # the speed uses (paint_time - first_progress_time).
        clock = iter([10.0, 11.0])
        monkeypatch.setattr("boto3_s3_cli.progress.time.monotonic", lambda: next(clock))
        printer = TransferPrinter()
        assert printer._start is None  # construction did not anchor
        with printer:
            printer.on_progress(_progress("a.txt", 0, 1024))  # queued at t=10 -> anchors
            printer.on_progress(_progress("a.txt", 1024, 1024))  # t=11 -> paints, 1.0s elapsed
        assert printer._start == 10.0
        # 1024 bytes over 1.0s -> 1.0 KiB/s.
        statement = capsys.readouterr().out.split("\r", 1)[0]
        assert "(1.0 KiB/s)" in statement

    def test_run_ending_in_skip_clears_residual_meter(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A run whose tail records are SKIPPED (cp/mv --no-overwrite 412 skips
        # paint nothing and do not reset the padding) would leave the last
        # \r-terminated meter on screen. finish() blots it out with a blank
        # padded line ending in \r (aws _clear_progress_if_no_more_expected...).
        with TransferPrinter() as printer:
            printer.on_progress(_progress("a", 0, 10))  # queued
            printer.on_progress(_progress("b", 0, 10))  # queued
            printer.on_progress(_progress("a", 5, 10))  # paints a meter
            printer.on_result(_result(OpOutcome.SKIPPED, key="a"))
            printer.on_result(_result(OpOutcome.SKIPPED, key="b"))
        segments = capsys.readouterr().out.split("\r")
        assert segments[-1] == ""  # trailing \r
        # the clear line is non-empty whitespace of the meter's width.
        assert segments[-2] != "" and segments[-2].strip() == ""
        assert segments[-3].startswith("Completed 5 Bytes/20 Bytes")
        assert len(segments[-2]) == len(segments[-3])

    def test_failed_backfills_untransferred_remainder(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # aws adds a failed file's untransferred remainder to the meter
        # (bytes_failed_to_transfer) so byte progress still reaches the expected
        # total. a failed at 3/10 contributes its missing 7 bytes.
        with TransferPrinter() as printer:
            printer.on_progress(_progress("a", 0, 10))  # queued
            printer.on_progress(_progress("b", 0, 10))  # queued
            printer.on_progress(_progress("a", 3, 10))  # a partway
            printer.on_progress(_progress("b", 10, 10))  # b done
            printer.on_result(_result(OpOutcome.FAILED, key="a", error=RuntimeError("boom")))
            printer.on_result(_result(OpOutcome.SUCCEEDED, key="b"))
        capsys.readouterr()
        assert printer._done_bytes == printer._expected_bytes == 20
        assert printer._inflight == {}
