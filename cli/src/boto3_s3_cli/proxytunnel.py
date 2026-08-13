"""The CONNECT request the official aws-cli distribution sends.

An HTTPS request routed through an HTTPS proxy opens its tunnel with a CONNECT
request, and the shape of that request belongs to the interpreter rather than
to botocore. CPython 3.12 rewrote it: ``CONNECT host:port HTTP/1.1`` carrying
the ``Host`` header every HTTP/1.1 request owes its recipient (RFC 9110
section 7.2), where 3.10 and 3.11 send ``CONNECT host:port HTTP/1.0`` with no
``Host`` at all. urllib3 carries its own copy of the old shape for the releases
whose stdlib predates the fix, so a newer urllib3 does not move it either.

aws-cli's official distribution runs on a frozen CPython 3.14, so aws always
sends the new shape. Against a proxy that answers 400 to a ``Host``-less
CONNECT - the conforming reading - every aws command therefore succeeds and
every command here failed, on the host interpreters this package supports.
Pinning the new shape puts one CONNECT on the wire across all of them, the way
`boto3_s3.mimetable` pins the bundled interpreter's MIME tables.

The pin is the CLI's, not the library's: `clientfactory` installs it as it
builds a client, so importing ``boto3_s3`` alone leaves an application's
connections to its own interpreter and urllib3.
"""

from __future__ import annotations

import http.client
from typing import Any

# The request-line version the old shape hardcodes. Finding it among the
# constants of the tunnel opener actually in effect is what says "this
# interpreter, with this urllib3, still writes the pre-3.12 CONNECT" - a
# question no version comparison answers on its own, since urllib3 substitutes
# its own opener on some releases and leaves the stdlib's in place on others.
_OLD_REQUEST_LINE = b"HTTP/1.0"

# CPython's own reader for the proxy's response headers, which the 3.12+ tunnel
# opener uses. Looked up rather than imported so that its absence can decline
# the pin instead of failing an import.
_read_headers: Any = getattr(http.client, "_read_headers", None)


def pin_connect_request() -> None:
    """Give urllib3's connections CPython 3.12+'s CONNECT request.

    The replacement goes onto urllib3's own ``HTTPConnection``, which is where
    botocore's connection classes inherit theirs from, so it reaches the S3
    client, the STS / SSO clients the credential chain builds for itself, and
    nothing outside this process.

    Every precondition is a reason to decline quietly rather than to fail: no
    urllib3, no tunnel opener to replace, an opener that no longer writes the
    old request line (an interpreter that already sends aws's shape - and this
    module's own replacement, so a second call is a no-op), or a stdlib without
    the header reader the replacement uses. A future urllib3 or CPython that
    stops matching leaves the pin dormant instead of breaking the tunnel.
    """
    try:
        from urllib3.connection import HTTPConnection
    except ImportError:
        return
    if _read_headers is None:
        return
    if not _sends_old_connect(getattr(HTTPConnection, "_tunnel", None)):
        return
    HTTPConnection._tunnel = _tunnel  # pyright: ignore[reportPrivateUsage]


def _sends_old_connect(tunnel: object) -> bool:
    """Does this tunnel opener write the pre-3.12 request line?

    Read off the function's own constants: both spellings of the old shape -
    the stdlib's, and urllib3's backported copy - build the request line from a
    literal carrying the version, while every implementation of the new shape
    takes it from the connection's ``_http_vsn_str`` instead. An opener whose
    constants say nothing is treated as new, which is the safe direction: the
    pin stays out.
    """
    consts: tuple[object, ...] = getattr(getattr(tunnel, "__code__", None), "co_consts", ())
    return any(isinstance(const, bytes) and _OLD_REQUEST_LINE in const for const in consts)


def _tunnel(self: Any) -> None:
    """CPython 3.12+'s ``HTTPConnection._tunnel``, for interpreters without it.

    Two things separate it from the shape 3.10 and 3.11 write. The request line
    carries the connection's own HTTP version - 1.1 - instead of a hardcoded
    1.0; and a ``Host`` header naming the tunnel's authority is generated when
    the caller supplied none.

    CPython generates that header in ``set_tunnel`` rather than here. Doing it
    here keeps the pin to a single method and, more to the point, keeps it from
    writing into the header mapping the caller passed in - which pre-3.12
    ``set_tunnel`` stores without copying, so a generated ``Host`` would land in
    urllib3's shared proxy headers and be replayed, stale, at the next tunnel
    host. The bytes reaching the wire are the same either way: the supplied
    headers first, in order, then the generated ``Host``.

    The request-target and header validations CPython's own copy carries are
    left out. They guard against input shapes botocore does not put on this
    path, and each one is a further private name the pin would have to find
    before it could install itself at all.
    """
    host = _wrap_ipv6(self._tunnel_host.encode("idna"))
    port: int = self._tunnel_port
    lines = [b"CONNECT %s:%d %s\r\n" % (host, port, self._http_vsn_str.encode("ascii"))]
    supplied_host = False
    for header, value in self._tunnel_headers.items():
        supplied_host = supplied_host or header.lower() == "host"
        lines.append(f"{header}: {value}\r\n".encode("latin-1"))
    if not supplied_host:
        lines.append(b"Host: %s:%d\r\n" % (host, port))
    lines.append(b"\r\n")
    # One send() rather than one per line, as CPython does: it lets the host OS
    # pick a sensible packet size instead of emitting a series of small ones.
    self.send(b"".join(lines))
    del lines

    response = self.response_class(self.sock, method=self._method)
    try:
        (_version, code, message) = response._read_status()
        self._raw_proxy_headers = _read_headers(response.fp)
        if self.debuglevel > 0:
            for raw_header in self._raw_proxy_headers:
                print("header:", raw_header.decode())
        if code != http.HTTPStatus.OK:
            self.close()
            raise OSError(f"Tunnel connection failed: {code} {message.strip()}")
    finally:
        response.close()


def _wrap_ipv6(host: bytes) -> bytes:
    """Bracket a bare IPv6 literal, as CPython's tunnel opener does.

    urllib3 reaches ``set_tunnel`` with the host already stripped of brackets,
    and an authority-form request target needs them back.
    """
    if b":" in host and not host.startswith(b"["):
        return b"[" + host + b"]"
    return host
