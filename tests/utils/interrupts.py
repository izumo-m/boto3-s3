"""Real Ctrl-Cs delivered to the main thread at a chosen wait.

A test of what a Ctrl-C does to a wait must deliver one the way the terminal
does - a SIGINT handled by the main thread inside the wait - rather than patch
the wait to raise: the interpreter's own handling is part of what is tested
(Python 3.10 to 3.12 mark a thread finished when a ``Thread.join`` on it is
interrupted, which no patched join reproduces). POSIX only
(``signal.pthread_kill``); the tests skip elsewhere.
"""

from __future__ import annotations

import contextlib
import signal
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import CodeType, FrameType

skip_without_pthread_kill = pytest.mark.skipif(
    not hasattr(signal, "pthread_kill"), reason="POSIX signal delivery"
)


def interrupt_main_in(caller: str) -> None:
    """Send the main thread a SIGINT once it waits on an ``Event`` in *caller*.

    Polls the main thread's stack until its innermost frames are
    ``Condition.wait`` under ``Event.wait`` called directly from the function
    named *caller*, then signals and gives the interrupt a moment to land.
    Call it from another thread.
    """
    main = threading.main_thread().ident
    assert main is not None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        frame = sys._current_frames().get(main)  # pyright: ignore[reportPrivateUsage]
        names: list[str] = []
        while frame is not None and len(names) < 3:
            names.append(frame.f_code.co_name)
            frame = frame.f_back
        if names == ["wait", "wait", caller]:
            break
        time.sleep(0.005)
    else:
        raise AssertionError(f"the main thread never waited in {caller}")
    signal.pthread_kill(main, signal.SIGINT)
    time.sleep(0.05)


@contextlib.contextmanager
def interrupt_main_on_call(code: CodeType, caller: str) -> Generator[list[bool]]:
    """Within the block, SIGINT the main thread as it calls *code* from *caller*.

    A profile hook on the main thread fires once, the moment a function with
    the code object *code* is entered from the function named *caller*; the
    interrupt then lands at that function's first instructions - a window too
    narrow to hit from another thread. The yielded list gains an entry when
    it fired. Use it on the main thread.
    """
    main = threading.main_thread().ident
    assert main is not None
    fired: list[bool] = []

    def hook(frame: FrameType, event: str, _arg: object) -> None:
        if event == "call" and frame.f_code is code and not fired:
            back = frame.f_back
            if back is not None and back.f_code.co_name == caller:
                fired.append(True)
                sys.setprofile(None)
                signal.pthread_kill(main, signal.SIGINT)

    sys.setprofile(hook)
    try:
        yield fired
    finally:
        sys.setprofile(None)
