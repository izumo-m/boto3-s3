"""Act on a Ctrl-C the moment it arrives, ahead of its ``KeyboardInterrupt``.

aws-cli stops reporting results at a Ctrl-C: the transfer futures still in
flight are cancelled, the first of them is reported as cancelled, and its
result processor drops everything after that. This command's deletes are
batched, and a batch request already out cannot be cancelled, so the
``KeyboardInterrupt`` reaches the command only after the deleter has waited
for that request - by then up to a whole batch of completions has been
reported. Hooking the signal itself lets a printer stop at the moment aws's
would.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any

__all__ = ["on_interrupt"]


@contextmanager
def on_interrupt(callback: Callable[[], None]) -> Generator[None, None, None]:
    """Call ``callback`` when SIGINT arrives during the block, then handle it as before.

    The handler in place when the block starts still runs after ``callback``
    - Python's default raises ``KeyboardInterrupt`` - and is restored when the
    block ends. Only the main thread of the main interpreter may install a
    signal handler, so anywhere else (an in-process test driving the CLI from
    a worker thread, say) the block runs unhooked.
    """
    installed = False
    previous: Any = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.getsignal(signal.SIGINT)

        def handler(signum: int, frame: Any) -> None:
            callback()
            if callable(previous):
                previous(signum, frame)
            elif previous != signal.SIG_IGN:
                raise KeyboardInterrupt

        try:
            signal.signal(signal.SIGINT, handler)
            installed = True
        except ValueError:
            pass
    try:
        yield
    finally:
        if installed:
            # A handler installed outside Python reads back as None, which
            # signal.signal refuses; the default disposition is its nearest form.
            signal.signal(signal.SIGINT, previous if previous is not None else signal.SIG_DFL)
