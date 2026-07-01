"""The transfer result/progress printer (``aws s3`` ``ResultPrinter`` parity).

``TransferPrinter`` consumes the library's ``on_result`` / ``on_progress``
callbacks - fired from s3transfer worker threads - and renders aws-style
lines:

- results (stdout): ``upload: ./a.txt to s3://b/k`` with the ``(dryrun)``
  prefix; local sides rendered relative to the cwd at print time
  (``naming.relative_path``, the aws-cli renderer), S3 sides as ``s3://...``.
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

Suppression matrix (rm's ``_DeletePrinter`` precedent): ``--quiet``
builds no output at all - failures included - while the warned/failed
counters still feed the exit code; ``--only-show-errors`` drops successes
and progress but keeps dryrun lines; ``--no-progress`` drops only progress.
``--progress-frequency N`` throttles progress repaints to one per N seconds;
0 (the default) applies the ``_MIN_REPAINT_INTERVAL`` floor - repaints run
inline on the s3transfer worker threads, so unthrottled they would serialize
the workers behind console I/O. ``--progress-multiline`` terminates each
progress statement with a newline instead of rewriting.

One lock serializes counters and writes (multipart parts complete on several
threads); streams are looked up via ``sys`` on every call so in-process tests
capture worker-thread writes (rm precedent).
"""

from __future__ import annotations

import sys
import threading
import time
from typing import TYPE_CHECKING, TextIO

from boto3_s3 import OpOutcome
from boto3_s3.naming import relative_path
from boto3_s3_cli.output import human_readable_size

if TYPE_CHECKING:
    from boto3_s3 import OpResult, TransferProgress


# Repaint floor when --progress-frequency is 0 (the default). Progress fires
# per transferred chunk (~256 KiB) on the s3transfer worker threads and the
# write+flush runs inline on the calling worker, so an unthrottled repaint
# serializes concurrent workers behind console I/O (aws-cli instead decouples
# rendering onto its dedicated printer thread). ~10 repaints/s keeps the meter
# live while bounding the inline cost; the first paint is never delayed.
_MIN_REPAINT_INTERVAL = 0.1


def _render_path(path: str | None) -> str:
    """aws's path rendering: S3 sides verbatim, local sides cwd-relative."""
    if path is None:
        return ""
    if path.startswith("s3://"):
        return path
    return relative_path(path)


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
        # rc inputs - counted even when fully silenced (--quiet).
        self.failed = 0
        self.warned = 0
        # progress bookkeeping
        self._start = time.monotonic()
        self._last_progress_at = float("-inf")
        self._progress_length = 0
        self._expected_files = 0
        self._finished_files = 0
        self._skipped_files = 0
        self._expected_bytes = 0
        self._done_bytes = 0
        self._inflight: dict[str, int] = {}

    @property
    def wants_progress(self) -> bool:
        """Whether the command should wire ``on_progress`` at all."""
        return self._show_progress

    # -- callbacks (worker threads) -----------------------------------------

    def on_progress(self, progress: TransferProgress) -> None:
        with self._lock:
            is_new = progress.key not in self._inflight
            if is_new:
                self._expected_files += 1
                if progress.bytes_total is not None:
                    self._expected_bytes += progress.bytes_total
            previous = self._inflight.get(progress.key, 0)
            self._inflight[progress.key] = progress.bytes_done
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
            self._write_progress_locked(now)

    def on_result(self, result: OpResult) -> None:
        with self._lock:
            if result.outcome is OpOutcome.NOTICE:
                # Display-only advisory text (the case-conflict messages):
                # aws prints these straight to stderr, bypassing both its
                # printers and its warned count - so even --quiet shows them.
                self._write_line_locked(sys.stderr, str(result.error))
                return
            if result.outcome in (OpOutcome.SUCCEEDED, OpOutcome.FAILED):
                self._finished_files += 1
                self._inflight.pop(result.key, None)
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
            if self._quiet:
                return
            verb = result.transfer_type.value
            if result.dest is None:
                # A one-endpoint record (sync's deletions): aws prints
                # `delete: <path>` with no `to` clause.
                location = _render_path(result.src)
            else:
                location = f"{_render_path(result.src)} to {_render_path(result.dest)}"
            if result.outcome is OpOutcome.SUCCEEDED:
                if not self._only_show_errors:
                    self._write_line_locked(sys.stdout, f"{verb}: {location}")
            elif result.outcome is OpOutcome.DRYRUN:
                # aws's OnlyShowErrorsResultPrinter does not override dryrun.
                self._write_line_locked(sys.stdout, f"(dryrun) {verb}: {location}")
            elif result.outcome is OpOutcome.FAILED:
                self._write_line_locked(sys.stderr, f"{verb} failed: {location} {result.error}")
            elif result.outcome is OpOutcome.WARNED:
                self._write_line_locked(sys.stderr, f"warning: {result.error}")
            # SKIPPED is silent (aws prints nothing for non-warning skips).

    # -- rendering (lock held) ----------------------------------------------

    def _write_progress_locked(self, now: float) -> None:
        remaining = self._expected_files - self._finished_files - self._skipped_files
        if self._expected_bytes > 0:
            elapsed = max(now - self._start, 1e-9)
            speed = human_readable_size(self._done_bytes / elapsed)
            statement = (
                f"Completed {human_readable_size(self._done_bytes)}/"
                f"{human_readable_size(self._expected_bytes)} ({speed}/s) "
                f"with {remaining} file(s) remaining"
            )
        else:
            statement = (
                f"Completed {self._finished_files} file(s) with {remaining} file(s) remaining"
            )
        if self._multiline:
            sys.stdout.write(statement + "\n")
        else:
            padded = statement.ljust(self._progress_length)
            self._progress_length = len(statement)
            sys.stdout.write(padded + "\r")
        sys.stdout.flush()

    def _write_line_locked(self, stream: TextIO, text: str) -> None:
        padded = text.ljust(self._progress_length)
        self._progress_length = 0
        stream.write(padded + "\n")
        stream.flush()


__all__ = ["TransferPrinter"]
