"""Real Ctrl-Cs delivered to the main thread at a chosen wait.

A test of what a Ctrl-C does to a wait must deliver one the way the terminal
does - a SIGINT handled by the main thread inside the wait - rather than patch
the wait to raise: the interpreter's own handling is part of what is tested
(Python 3.10 to 3.12 mark a thread finished when a ``Thread.join`` on it is
interrupted, which no patched join reproduces). POSIX only
(``signal.pthread_kill``); the tests skip elsewhere.
"""

from __future__ import annotations

import signal
import sys
import threading
import time

import pytest

skip_without_pthread_kill = pytest.mark.skipif(
    not hasattr(signal, "pthread_kill"), reason="POSIX signal delivery"
)


def interrupt_main_in(caller: str) -> None:
    """Send the main thread a SIGINT once it waits in *caller*'s `_wait_set`.

    Polls the main thread's stack until its innermost Python frame is a
    ``_wait_set`` (the raw-lock wait the CLI printer holds Ctrl-Cs in) called
    directly from the function named *caller*, then signals and gives the
    interrupt a moment to land. Call it from another thread.
    """
    main = threading.main_thread().ident
    assert main is not None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        frame = sys._current_frames().get(main)  # pyright: ignore[reportPrivateUsage]
        names: list[str] = []
        while frame is not None and len(names) < 2:
            names.append(frame.f_code.co_name)
            frame = frame.f_back
        if names == ["_wait_set", caller]:
            break
        time.sleep(0.005)
    else:
        raise AssertionError(f"the main thread never waited in {caller}")
    signal.pthread_kill(main, signal.SIGINT)
    time.sleep(0.05)
