"""Keep a request body's read failure from reaching awscrt's request signer.

With awscrt importable, botocore signs symmetric SigV4 with awscrt's signer,
which reads the request body itself to hash it wherever the payload is signed
(on a plain-HTTP endpoint, above all). An exception a read raises there - a
disk error, the one s3transfer's part reader raises to stop the other parts
of an upload that has already failed, or a Ctrl-C landing in the read -
reaches awscrt's native layer: awscrt writes the traceback to standard error
itself and fails the request with its own ``AWS_ERROR_UNKNOWN``, and the
exception is gone. aws-cli's bundled botocore signs with the pure-Python
signer, where the same exception propagates out of the signing as it is
(design/transfer.md section 9).

`kept_signing_read_failures` registers two handlers on a client's public
events for a span. ``before-sign`` (an event botocore hands the request to
for changing it) wraps a streaming body in `_SigningReadGuard`, which keeps
what a read raises - an interrupt included - and answers the end of the body
instead; the signer signs a short body and returns. ``request-created``,
where botocore signs, runs the second handler last, after the signer: it
puts the request's own body back and raises the kept exception, from the
signing step as the pure-Python signer's raise comes - before anything is
sent and outside botocore's retries, so the request is neither sent nor
retried. A body that reads cleanly is signed and sent unchanged: the
signature, the headers and the bytes are those of an unwatched request, and
the body sent is the request's own object. Every signer meets the same
watch: awscrt's SigV4a signer is kept from the exception the same way, and a
pure-Python one (S3 Express's) comes out of it with the exception it would
raise.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator


class _SigningReadGuard:
    """A streaming request body that keeps what a read raises while it is signed.

    It offers exactly the read methods the body offers - awscrt picks among
    ``readinto1`` / ``readinto`` / ``read`` by what the body has, and
    s3transfer's part reader has ``read`` alone - each passing the call
    through; other attributes the signer looks up (``seek``, ``tell``) are
    the body's own. It is the request's body only between ``before-sign`` and
    the end of the signing, never while the request is sent. The first
    exception a read raises, an interrupt included, is kept in ``failure``,
    and that read and every later one answer the end of the body.
    """

    _READS = frozenset({"read", "read1", "readinto", "readinto1"})

    def __init__(self, body: Any) -> None:
        self.body = body
        self.failure: BaseException | None = None

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.body, name)
        if name not in self._READS or not callable(attr):
            return attr
        end: bytes | int = 0 if name.startswith("readinto") else b""

        def guarded(*args: Any) -> Any:
            if self.failure is not None:
                return end
            try:
                return attr(*args)
            except BaseException as exc:
                # awscrt runs the read in its native layer, where an
                # interrupt would be lost in its own error too: kept, and
                # raised on the signing thread by `_restore_body`.
                self.failure = exc
                return end

        return guarded


def _guard_body(request: Any, **_kwargs: Any) -> None:
    """``before-sign``: watch a streaming body while the request is signed."""
    data = request.data
    if hasattr(data, "read") and not isinstance(data, _SigningReadGuard):
        request.data = _SigningReadGuard(data)


def _restore_body(request: Any, **_kwargs: Any) -> None:
    """``request-created``, after the signer: put the body back, and raise what it kept."""
    data = request.data
    if isinstance(data, _SigningReadGuard):
        request.data = data.body
        if data.failure is not None:
            raise data.failure


def register(client: Any) -> bool:
    """Watch the bodies *client*'s S3 requests are signed with; False when it cannot.

    A client that does not expose botocore's event seam (a stand-in) is left
    alone. The ``before-sign`` handler goes last, after botocore's own. The
    ``request-created`` one goes last on ``request-created.s3``, where the
    client registers its signer, so it runs after the signing; a handler
    registered on an operation's own ``request-created.s3.<operation>`` runs
    before both.
    """
    events = getattr(getattr(client, "meta", None), "events", None)
    if events is None or not hasattr(events, "register_last"):
        return False
    events.register_last("before-sign.s3", _guard_body)
    events.register_last("request-created.s3", _restore_body)
    return True


def unregister(client: Any) -> None:
    """Remove what `register` added to *client* (one registration of each)."""
    events = client.meta.events
    events.unregister("before-sign.s3", _guard_body)
    events.unregister("request-created.s3", _restore_body)


@contextmanager
def kept_signing_read_failures(client: Any) -> Generator[None, None, None]:
    """`register` on *client* for the span of the ``with`` block."""
    registered = register(client)
    try:
        yield
    finally:
        if registered:
            unregister(client)


__all__ = ["kept_signing_read_failures", "register", "unregister"]
