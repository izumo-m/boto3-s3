"""``TransferPrinter``: aws-style result lines, the ``\\r`` protocol, suppression.

The printable shapes are pinned against aws's output: result
lines render local sides cwd-relative (``./a.txt``), progress statements are
left-justified over the previous one and end with ``\\r``, and a following
result line blots the progress out with padding before its newline.
"""

from __future__ import annotations

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
        transfer_type=transfer_type, key=key, outcome=outcome, src=src, dest=dest, error=error
    )


def _progress(key: str, done: int, total: int | None) -> TransferProgress:
    return TransferProgress(
        transfer_type=TransferType.UPLOAD, key=key, bytes_done=done, bytes_total=total
    )


class TestResultLines:
    def test_success_line_renders_local_sides_relative(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        printer = TransferPrinter()
        printer.on_result(
            _result(OpOutcome.SUCCEEDED, src=str(tmp_path / "a.txt"), dest="s3://b/k")
        )
        assert capsys.readouterr().out == "upload: ./a.txt to s3://b/k\n"

    def test_dryrun_prefix(self, capsys: pytest.CaptureFixture[str]) -> None:
        printer = TransferPrinter()
        printer.on_result(_result(OpOutcome.DRYRUN, transfer_type=TransferType.COPY))
        assert capsys.readouterr().out == "(dryrun) copy: s3://b/a.txt to s3://b/copy.txt\n"

    def test_failure_line_goes_to_stderr_with_the_error(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        printer = TransferPrinter()
        printer.on_result(_result(OpOutcome.FAILED, error=RuntimeError("boom")))
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == "upload failed: s3://b/a.txt to s3://b/copy.txt boom\n"
        assert printer.failed == 1

    def test_warning_line_prints_the_body_verbatim(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        printer = TransferPrinter()
        printer.on_result(
            _result(OpOutcome.WARNED, error=RuntimeError("Skipping file x. Not readable."))
        )
        assert capsys.readouterr().err == "warning: Skipping file x. Not readable.\n"
        assert printer.warned == 1

    def test_skipped_is_silent(self, capsys: pytest.CaptureFixture[str]) -> None:
        printer = TransferPrinter()
        printer.on_result(_result(OpOutcome.SKIPPED))
        captured = capsys.readouterr()
        assert (captured.out, captured.err) == ("", "")


class TestSuppression:
    def test_quiet_silences_everything_but_keeps_counts(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        printer = TransferPrinter(quiet=True)
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
        printer = TransferPrinter(only_show_errors=True)
        printer.on_result(_result(OpOutcome.SUCCEEDED))
        printer.on_result(_result(OpOutcome.DRYRUN))
        printer.on_result(_result(OpOutcome.FAILED, error=RuntimeError("boom")))
        printer.on_result(_result(OpOutcome.WARNED, error=RuntimeError("warned")))
        captured = capsys.readouterr()
        assert captured.out == "(dryrun) upload: s3://b/a.txt to s3://b/copy.txt\n"
        assert "upload failed:" in captured.err
        assert "warning: warned" in captured.err

    def test_no_progress_keeps_result_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        printer = TransferPrinter(progress=False)
        assert not printer.wants_progress
        printer.on_progress(_progress("a.txt", 512, 1024))  # bookkeeping only
        printer.on_result(_result(OpOutcome.SUCCEEDED))
        captured = capsys.readouterr()
        assert captured.out == "upload: s3://b/a.txt to s3://b/copy.txt\n"


class TestCarriageReturnProtocol:
    def test_progress_then_result_overwrites_with_padding(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        printer = TransferPrinter()
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
        printer = TransferPrinter()
        printer.on_progress(_progress("a.txt", 0, 1024))
        assert capsys.readouterr().out == ""

    def test_queued_then_skipped_reaches_zero_remaining(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A cp/mv upload/copy 412-skipped under --no-overwrite is queued
        # (on_queued counts it), then SKIPPED. The meter must reconcile it (aws
        # subtracts files_skipped) so "remaining" reaches 0 and _inflight clears.
        printer = TransferPrinter()
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
        printer = TransferPrinter()
        printer.on_progress(_progress("c", 0, 10))  # one real, queued transfer
        printer.on_result(_result(OpOutcome.SKIPPED, key="never-queued"))
        assert printer._skipped_files == 0

    def test_multiline_progress_uses_newlines(self, capsys: pytest.CaptureFixture[str]) -> None:
        printer = TransferPrinter(multiline=True)
        printer.on_progress(_progress("a.txt", 0, 1024))
        printer.on_progress(_progress("a.txt", 512, 1024))
        out = capsys.readouterr().out
        assert "\r" not in out
        assert out.endswith("file(s) remaining\n")

    def test_frequency_throttles_repaints(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = iter([0.0, 1.0, 2.0, 100.0])
        monkeypatch.setattr("boto3_s3_cli.progress.time.monotonic", lambda: next(clock))
        printer = TransferPrinter(frequency=60)  # start consumes 0.0
        printer.on_progress(_progress("a.txt", 0, 4096))  # queued, no paint
        printer.on_progress(_progress("a.txt", 1024, 4096))  # t=1.0 -> paints (first)
        printer.on_progress(_progress("a.txt", 2048, 4096))  # t=2.0 -> throttled
        printer.on_progress(_progress("a.txt", 4096, 4096))  # t=100 -> paints
        out = capsys.readouterr().out
        assert out.count("Completed") == 2
