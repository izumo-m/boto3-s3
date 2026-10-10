"""``boto3_s3_cli.interrupts`` - acting on a Ctrl-C ahead of its KeyboardInterrupt."""

from __future__ import annotations

import signal
import threading

import pytest

from boto3_s3_cli.interrupts import on_interrupt


def test_the_callback_runs_and_the_interrupt_still_raises() -> None:
    seen: list[str] = []
    before = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt), on_interrupt(lambda: seen.append("stop")):
        signal.raise_signal(signal.SIGINT)
    assert seen == ["stop"]
    assert signal.getsignal(signal.SIGINT) is before


def test_an_ignored_interrupt_stays_ignored() -> None:
    seen: list[str] = []
    before = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with on_interrupt(lambda: seen.append("stop")):
            signal.raise_signal(signal.SIGINT)
        assert seen == ["stop"]
        assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, before)


def test_off_the_main_thread_the_block_runs_unhooked() -> None:
    # Only the main thread may install a handler; elsewhere nothing changes.
    handlers: list[object] = []

    def run() -> None:
        with on_interrupt(lambda: None):
            handlers.append(signal.getsignal(signal.SIGINT))

    before = signal.getsignal(signal.SIGINT)
    worker = threading.Thread(target=run)
    worker.start()
    worker.join()
    assert handlers == [before]
