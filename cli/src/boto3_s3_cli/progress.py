"""The transfer result/progress printer (``aws s3`` ``ResultPrinter`` parity).

``TransferPrinter`` consumes the library's ``on_result`` / ``on_progress``
callbacks - fired from s3transfer worker threads - and renders aws-style
lines:

- results (stdout): ``upload: ./a.txt to s3://b/k`` with the ``(dryrun)``
  prefix; local sides rendered relative to the cwd at print time
  (``LocalStorage.relative_path``, the aws-cli renderer), S3 sides as ``s3://...``.
- failures / warnings (stderr): ``upload failed: ... <error>`` /
  ``warning: <body>`` - the bodies arrive aws-cli-worded from the library.
- progress (stdout): ``Completed 1.2 MiB/9.0 MiB (3.4 MiB/s) with 1 file(s)
  remaining`` rewritten in place via aws-cli's carriage-return protocol:
  each statement is left-justified to the previous statement's length (so a
  shorter line blots the longer one out) and ends with ``\\r``; a result line
  ends with ``\\n`` and resets the padding. aws applies no isatty gate and
  neither does this printer - piped output carries the same ``\\r`` segments
  (the golden normalization strips them). aws's ``~total (calculating...)``
  markers for a still-running enumeration are not reproduced (the library
  has no enumeration-finished signal); console identity is non-contractual
  (docs/aws-cli-option-handling.md section 6).

Rendering is decoupled from the transfer workers, aws-cli's results-pipeline
shape (its ``ResultProcessor`` thread): the worker-side callbacks only update
the counters (always exact, independent of rendering) and
enqueue a slim record; one dedicated printer thread drains the queue and does
every ``write``/``flush``, so console I/O never blocks a worker. Because a
single thread renders in queue order, line ordering and the ``\\r`` padding
protocol are exactly the single-threaded behavior. Use the printer as a
context manager around the run: ``__exit__`` drains and joins the thread, so
by the time the command returns every line has been written. A dead output
stream (``BrokenPipeError`` on a closed pipe) stops rendering but keeps
draining - the run and its exit code are unaffected.

One deliberate deviation from aws-cli: the queue is **bounded**
(``_QUEUE_MAX`` records) where aws's result queue is unbounded. aws never
slows a transfer for console output but grows memory without limit when the
consumer stalls; here a consumer stalled long enough to fall ``_QUEUE_MAX``
records behind (e.g. ``sync`` piped into a stopped pager) back-pressures the
transfer instead (documented in docs/aws-cli-option-handling.md section 6).

Suppression matrix (rm's ``_DeletePrinter`` precedent): ``--quiet``
builds no output at all - failures included - while the ``warned`` counter
still feeds the exit code (rc 2; a failure's rc 1 comes from the library's
``BatchError``, so ``failed`` is observational only, kept exact all the
same); ``--only-show-errors`` drops successes
and progress but keeps dryrun lines; ``--no-progress`` drops only progress.
``--progress-frequency N`` throttles progress repaints to one per N seconds;
0 (the default) applies the ``_MIN_REPAINT_INTERVAL`` floor, which also
keeps repaint records entering the queue at chunk-independent rate.
``--progress-multiline`` terminates each progress statement with a newline
instead of rewriting.

The counter lock serializes worker-side bookkeeping (multipart parts complete
on several threads); streams are looked up via ``sys`` on every write so
in-process tests capture the printer thread's output (rm precedent).
"""

from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, TextIO

from boto3_s3 import OpOutcome
from boto3_s3.localstorage import LocalStorage
from boto3_s3_cli.output import human_readable_size

if TYPE_CHECKING:
    from boto3_s3 import OpResult, TransferProgress

# Repaint floor when --progress-frequency is 0 (the default). Progress fires
# per transferred chunk (~256 KiB) on the s3transfer worker threads; the floor
# keeps repaint records entering the queue at a chunk-independent ~10/s (and
# the terminal readable). The first paint is never delayed.
_MIN_REPAINT_INTERVAL = 0.1

# Printer-queue bound - the deliberate aws-cli deviation described in the
# module docstring: a consumer stalled this many records behind back-pressures
# the transfer (workers block on put) instead of growing memory without limit.
# Records are slim (a few strings), so the resident cost is a few MiB.
_QUEUE_MAX = 10_000

# Queue sentinel: drain what remains, then stop the printer thread.
_SHUTDOWN = object()


@dataclass(frozen=True, slots=True)
class _ResultRecord:
    """One result line, reduced to what rendering needs (never the OpResult:
    queuing it would pin its FileInfo / Storage references until printed)."""

    outcome: OpOutcome
    verb: str
    src: str | None
    dest: str | None
    error: str


@dataclass(frozen=True, slots=True)
class _ProgressSnapshot:
    """One progress repaint, captured under the counter lock at decision time
    (so the painted numbers are exactly the state that passed the throttle)."""

    done_bytes: int
    expected_bytes: int
    finished_files: int
    remaining: int
    now: float


def _render_path(path: str | None) -> str:
    """aws's path rendering: S3 sides verbatim, local sides cwd-relative.

    The stream endpoint token ``-`` is also verbatim (aws prints
    ``upload: - to s3://...``); relativizing it would render ``<cwd>/-``.
    """
    if path is None:
        return ""
    if path.startswith("s3://") or path == "-":
        return path
    return LocalStorage.relative_path(path)


class TransferPrinter:
    """Render transfer results and progress with aws-cli's shapes and rules."""

    def __init__(
        self,
        *,
        quiet: bool = False,
        only_show_errors: bool = False,
        progress: bool = True,
        frequency: int = 0,
        multiline: bool = False,
    ) -> None:
        self._quiet = quiet
        self._only_show_errors = only_show_errors
        self._show_progress = progress and not quiet and not only_show_errors
        self._frequency = max(float(frequency), _MIN_REPAINT_INTERVAL)
        self._multiline = multiline
        self._lock = threading.Lock()
        # Counted even when fully silenced (--quiet), on the worker side, so
        # the exit code never depends on the printer thread. Only warned feeds
        # the rc (finish_transfer's rc 2); a failure's rc 1 comes through the
        # library's BatchError, so failed is observational only.
        self.failed = 0
        self.warned = 0
        # progress bookkeeping (worker side, under the lock)
        self._start: float | None = None
        self._last_progress_at = float("-inf")
        self._expected_files = 0
        self._finished_files = 0
        self._skipped_files = 0
        self._expected_bytes = 0
        self._done_bytes = 0
        # key -> (bytes_done, bytes_total); bytes_total lets a FAILED result
        # backfill the untransferred remainder into the meter (aws parity).
        self._inflight: dict[str, tuple[int, int | None]] = {}
        # rendering pipeline
        self._queue: queue.Queue[object] = queue.Queue(maxsize=_QUEUE_MAX)
        self._thread: threading.Thread | None = None
        # printer-thread-only state (no lock: one thread owns them)
        self._progress_length = 0
        self._output_dead = False

    @property
    def wants_progress(self) -> bool:
        """Whether the command should wire ``on_progress`` at all."""
        return self._show_progress

    # -- lifecycle ------------------------------------------------------------

    def __enter__(self) -> TransferPrinter:
        # Daemon: a printer wedged on a dead stream must not hold the process
        # open past the command; finish() joins it on the ordinary path.
        self._thread = threading.Thread(target=self._drain, name="boto3-s3-printer", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.finish()

    def finish(self) -> None:
        """Drain the queue and stop the printer thread (idempotent)."""
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        self._queue.put(_SHUTDOWN)
        thread.join()

    # -- callbacks (worker threads: count and enqueue, never write) -----------

    def on_progress(self, progress: TransferProgress) -> None:
        snapshot: _ProgressSnapshot | None = None
        with self._lock:
            if self._start is None:
                # aws anchors the transfer-speed denominator at the first queued
                # result, not at construction (results.py _record_queued_result);
                # do the same so a long enumeration phase before the first byte
                # does not deflate the displayed rate.
                self._start = time.monotonic()
            is_new = progress.key not in self._inflight
            if is_new:
                self._expected_files += 1
                if progress.bytes_total is not None:
                    self._expected_bytes += progress.bytes_total
            previous = self._inflight.get(progress.key, (0, None))[0]
            self._inflight[progress.key] = (progress.bytes_done, progress.bytes_total)
            self._done_bytes += progress.bytes_done - previous
            if not self._show_progress:
                return
            if is_new and progress.bytes_done == 0:
                # The queue-time notification feeds the expected totals but
                # prints nothing (aws first paints on real byte progress).
                return
            now = time.monotonic()
            if now - self._last_progress_at < self._frequency:
                return
            self._last_progress_at = now
            snapshot = _ProgressSnapshot(
                done_bytes=self._done_bytes,
                expected_bytes=self._expected_bytes,
                finished_files=self._finished_files,
                remaining=self._expected_files - self._finished_files - self._skipped_files,
                now=now,
            )
        # Enqueue outside the lock: a full queue blocks the worker (the
        # documented back-pressure), and blocking while holding the lock
        # would also stall the other workers' bookkeeping.
        self._queue.put(snapshot)

    def on_result(self, result: OpResult) -> None:
        record: _ResultRecord | None = None
        with self._lock:
            if result.outcome is OpOutcome.NOTICE:
                # Display-only advisory text (the case-conflict messages):
                # aws prints these straight to stderr, bypassing both its
                # printers and its warned count - so even --quiet shows them.
                record = _ResultRecord(result.outcome, "", None, None, str(result.error))
            else:
                if result.outcome in (OpOutcome.SUCCEEDED, OpOutcome.FAILED):
                    self._finished_files += 1
                    inflight = self._inflight.pop(result.key, None)
                    if result.outcome is OpOutcome.FAILED and inflight is not None:
                        # aws adds a failed file's untransferred remainder to the
                        # meter (results.py bytes_failed_to_transfer) so the byte
                        # progress still reaches the expected total.
                        done_bytes, total_bytes = inflight
                        if total_bytes is not None:
                            self._done_bytes += total_bytes - done_bytes
                elif (
                    result.outcome is OpOutcome.SKIPPED
                    and self._inflight.pop(result.key, None) is not None
                ):
                    # A queued-then-skipped transfer (cp/mv upload/copy 412 under
                    # --no-overwrite): on_queued already counted it into
                    # _expected_files / _inflight, so reconcile it here or the meter
                    # never reaches 0 (aws subtracts files_skipped). A pre-submit
                    # skip (download no-overwrite) never entered _inflight, so the
                    # pop returns None and it is left untouched.
                    self._skipped_files += 1
                if result.outcome is OpOutcome.FAILED:
                    self.failed += 1
                elif result.outcome is OpOutcome.WARNED:
                    self.warned += 1
                if not self._quiet and self._prints(result.outcome):
                    record = _ResultRecord(
                        result.outcome,
                        result.transfer_type.value,
                        result.src,
                        result.dest,
                        str(result.error) if result.error is not None else "",
                    )
        if record is not None:
            self._queue.put(record)  # outside the lock, like the snapshot

    def _prints(self, outcome: OpOutcome) -> bool:
        if outcome is OpOutcome.SUCCEEDED:
            return not self._only_show_errors
        # aws's OnlyShowErrorsResultPrinter does not override dryrun; SKIPPED
        # is silent (aws prints nothing for non-warning skips).
        return outcome in (OpOutcome.DRYRUN, OpOutcome.FAILED, OpOutcome.WARNED)

    # -- rendering (the printer thread) ----------------------------------------

    def _drain(self) -> None:
        while True:
            record = self._queue.get()
            if record is _SHUTDOWN:
                self._clear_residual_progress()
                return
            if self._output_dead:
                continue
            try:
                if isinstance(record, _ProgressSnapshot):
                    self._write_progress(record)
                else:
                    assert isinstance(record, _ResultRecord)
                    self._render_result(record)
            except Exception:
                # A dead stream (BrokenPipeError on a closed pipe, a full
                # non-blocking pty, ...): stop rendering but keep draining so
                # no worker ever blocks on the queue. The rc inputs live on
                # the worker side and are unaffected; cli.main's own
                # BrokenPipeError handling covers the process-level contract.
                # An unencodable key is not a dead stream: _uni_write handles
                # UnicodeEncodeError inline, so it never reaches here.
                self._output_dead = True

    def _render_result(self, record: _ResultRecord) -> None:
        if record.outcome is OpOutcome.NOTICE:
            self._write_line(sys.stderr, record.error)
            return
        if record.dest is None:
            # A one-endpoint record (sync's deletions): aws prints
            # `delete: <path>` with no `to` clause.
            location = _render_path(record.src)
        else:
            location = f"{_render_path(record.src)} to {_render_path(record.dest)}"
        if record.outcome is OpOutcome.SUCCEEDED:
            self._write_line(sys.stdout, f"{record.verb}: {location}")
        elif record.outcome is OpOutcome.DRYRUN:
            self._write_line(sys.stdout, f"(dryrun) {record.verb}: {location}")
        elif record.outcome is OpOutcome.FAILED:
            self._write_line(sys.stderr, f"{record.verb} failed: {location} {record.error}")
        elif record.outcome is OpOutcome.WARNED:
            self._write_line(sys.stderr, f"warning: {record.error}")

    def _write_progress(self, snapshot: _ProgressSnapshot) -> None:
        if snapshot.expected_bytes > 0:
            start = self._start if self._start is not None else snapshot.now
            elapsed = max(snapshot.now - start, 1e-9)
            speed = human_readable_size(snapshot.done_bytes / elapsed)
            statement = (
                f"Completed {human_readable_size(snapshot.done_bytes)}/"
                f"{human_readable_size(snapshot.expected_bytes)} ({speed}/s) "
                f"with {snapshot.remaining} file(s) remaining"
            )
        else:
            statement = (
                f"Completed {snapshot.finished_files} file(s) "
                f"with {snapshot.remaining} file(s) remaining"
            )
        if self._multiline:
            self._uni_write(sys.stdout, statement + "\n")
        else:
            padded = statement.ljust(self._progress_length)
            self._progress_length = len(statement)
            self._uni_write(sys.stdout, padded + "\r")

    def _write_line(self, stream: TextIO, text: str) -> None:
        padded = text.ljust(self._progress_length)
        self._progress_length = 0
        self._uni_write(stream, padded + "\n")

    def _clear_residual_progress(self) -> None:
        """Blot out a meter left on screen by a run whose tail records paint
        nothing (cp/mv ``--no-overwrite`` 412 skips paint nothing and do not
        reset the padding). aws clears it with a blank padded line ending in
        ``\\r`` (``_clear_progress_if_no_more_expected_transfers``). A tail
        result line already reset ``_progress_length`` to 0, so a normal run
        clears nothing here."""
        if self._output_dead or self._progress_length <= 0:
            return
        try:
            self._uni_write(sys.stdout, " " * self._progress_length + "\r")
        except Exception:
            self._output_dead = True

    def _uni_write(self, stream: TextIO, text: str) -> None:
        """aws-cli's ``uni_print``: on a console/codepage that cannot encode the
        text (a non-ASCII key on a cp932 or ascii stream), re-encode with the
        stream's encoding and ``errors='replace'`` rather than dropping the
        line, so one unencodable key never silences the rest of the run."""
        try:
            stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            stream.write(text.encode(encoding, "replace").decode(encoding))
        stream.flush()


__all__ = ["TransferPrinter"]
