"""Unit tests for boto3_s3_cli.proxytunnel (the CONNECT request's shape).

The oracle is the pinned aws-cli, measured against a recording proxy that
answers 400 to a ``Host``-less CONNECT: aws opens its tunnel with

    CONNECT 127.0.0.1:PORT HTTP/1.1
    Host: 127.0.0.1:PORT

and, when the proxy URL carries credentials, with ``Proxy-Authorization``
ahead of that generated ``Host``. Before the pin this CLI sent
``CONNECT 127.0.0.1:PORT HTTP/1.0`` with no ``Host`` (aws rc 0 / ours rc 255,
``Failed to connect to proxy URL``) on every host interpreter older than 3.12
- the shape those interpreters, and the copy urllib3 carries for them, still
write.

The fake proxy below is the same strict one: it records each CONNECT block and
rejects a block with no ``Host``, so `TestTheProxyIsStrict` can show its teeth
before the rest of the file leans on them.
"""

from __future__ import annotations

import argparse
import http.client
import socket
import sys
import threading
from collections.abc import Iterator
from io import BytesIO
from typing import Any

import pytest

from boto3_s3_cli import clientfactory, globalargs, proxytunnel

# The tunnel target the CONNECT names. Nothing ever connects to it: the fake
# proxy answers the CONNECT itself and never dials upstream.
_TARGET_PORT = 4443


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    globalargs.add_common_arguments(parser)
    return parser.parse_args(argv)


class _StrictProxy:
    """A proxy that records CONNECT blocks and insists on a ``Host`` header.

    RFC 9110 makes ``Host`` mandatory on an HTTP/1.1 request, so a conforming
    proxy may refuse a request without one - which is exactly the deployment
    the divergence bites in. Each recorded block is the request line and its
    headers, as raw lines with the CRLFs removed.
    """

    def __init__(self) -> None:
        self.blocks: list[list[bytes]] = []
        self._lock = threading.Lock()
        self._server = socket.socket()
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self.port: int = self._server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:  # the listening socket was closed at teardown
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(30)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    data += chunk
            except OSError:
                return
            block = data.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
            with self._lock:
                self.blocks.append(block)
            if any(line.lower().startswith(b"host:") for line in block[1:]):
                # 200, then the connection closes: whatever the client meant to
                # tunnel fails, which is all these tests need - the CONNECT
                # itself is already recorded.
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                conn.sendall(
                    b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )

    def close(self) -> None:
        self._server.close()


@pytest.fixture
def proxy() -> Iterator[_StrictProxy]:
    server = _StrictProxy()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def proxy_environment(monkeypatch: pytest.MonkeyPatch, proxy: _StrictProxy) -> _StrictProxy:
    """Route HTTPS through the fake proxy, with nothing of the host's left."""
    for leak in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(leak, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", proxy.url)
    # One attempt: a retry would open a second tunnel and slow the test down
    # without adding anything - the first block is what is under test.
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
    return proxy


class TestTheProxyIsStrict:
    """The fake proxy has the teeth the real deployment has."""

    def test_a_host_less_connect_is_rejected(self, proxy: _StrictProxy) -> None:
        # The pre-pin shape, sent by hand: without this the rest of the file
        # could pass against a proxy that accepts anything.
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=30) as sock:
            sock.sendall(b"CONNECT 127.0.0.1:%d HTTP/1.0\r\n\r\n" % _TARGET_PORT)
            assert sock.recv(4096).startswith(b"HTTP/1.1 400 ")
        assert proxy.blocks == [[b"CONNECT 127.0.0.1:%d HTTP/1.0" % _TARGET_PORT]]

    def test_a_connect_carrying_host_is_accepted(self, proxy: _StrictProxy) -> None:
        with socket.create_connection(("127.0.0.1", proxy.port), timeout=30) as sock:
            sock.sendall(
                b"CONNECT 127.0.0.1:%d HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                % (_TARGET_PORT, _TARGET_PORT)
            )
            assert sock.recv(4096).startswith(b"HTTP/1.1 200 ")


class TestTheClientsThisCliBuilds:
    """What goes on the wire when a CLI-built client meets an HTTPS proxy."""

    def test_the_tunnel_request_is_the_one_aws_sends(self, proxy_environment: _StrictProxy) -> None:
        client = clientfactory.build_client(
            _parse(["--region", "us-east-1", "--endpoint-url", f"https://127.0.0.1:{_TARGET_PORT}"])
        )
        with pytest.raises(Exception):  # noqa: B017 - the tunnel is cut after the 200
            client.list_buckets()
        assert proxy_environment.blocks[0] == [
            b"CONNECT 127.0.0.1:%d HTTP/1.1" % _TARGET_PORT,
            b"Host: 127.0.0.1:%d" % _TARGET_PORT,
        ]

    def test_proxy_credentials_precede_the_generated_host(
        self, proxy_environment: _StrictProxy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Measured on aws: Proxy-Authorization first, the generated Host last -
        # the order CPython 3.12+ produces by appending its Host to the headers
        # urllib3 supplied.
        monkeypatch.setenv("HTTPS_PROXY", f"http://bob:sec@127.0.0.1:{proxy_environment.port}")
        client = clientfactory.build_client(
            _parse(["--region", "us-east-1", "--endpoint-url", f"https://127.0.0.1:{_TARGET_PORT}"])
        )
        with pytest.raises(Exception):  # noqa: B017 - the tunnel is cut after the 200
            client.list_buckets()
        assert proxy_environment.blocks[0] == [
            b"CONNECT 127.0.0.1:%d HTTP/1.1" % _TARGET_PORT,
            b"Proxy-Authorization: Basic Ym9iOnNlYw==",
            b"Host: 127.0.0.1:%d" % _TARGET_PORT,
        ]


class _FakeResponse:
    """Just enough of ``HTTPResponse`` for the replacement to read a 200 off."""

    def __init__(self, headers: bytes = b"\r\n") -> None:
        self.fp = BytesIO(headers)
        self.closed = False

    def _read_status(self) -> tuple[str, int, str]:
        return ("HTTP/1.1", 200, "Connection Established")

    def close(self) -> None:
        self.closed = True


class _FakeConnection:
    """A connection that records what the tunnel opener writes to it."""

    def __init__(self, host: str, port: int, headers: dict[str, str] | None = None) -> None:
        self._tunnel_host = host
        self._tunnel_port = port
        self._tunnel_headers = dict(headers or {})
        self._http_vsn_str = "HTTP/1.1"
        self._method: str | None = None
        self.sock = None
        self.debuglevel = 0
        self.sent = b""
        self.response = _FakeResponse()

    def response_class(self, sock: Any, method: str | None = None) -> _FakeResponse:
        return self.response

    def send(self, data: bytes) -> None:
        self.sent += data

    def close(self) -> None:  # pragma: no cover - only a non-200 would call it
        raise AssertionError("the 200 path must not close the connection")

    def lines(self) -> list[bytes]:
        return self.sent.split(b"\r\n\r\n", 1)[0].split(b"\r\n")


class TestTheReplacementRequest:
    """The request the replacement writes, read straight off a fake connection."""

    def test_it_generates_the_host_header(self) -> None:
        connection = _FakeConnection("proxy.example", 8443)
        proxytunnel._tunnel(connection)
        assert connection.sent == (
            b"CONNECT proxy.example:8443 HTTP/1.1\r\nHost: proxy.example:8443\r\n\r\n"
        )
        assert connection.response.closed

    def test_a_supplied_host_header_is_not_duplicated(self) -> None:
        # CPython leaves a caller-supplied Host alone, in its own position.
        connection = _FakeConnection(
            "proxy.example", 8443, {"Host": "supplied:1", "X-Extra": "keep"}
        )
        proxytunnel._tunnel(connection)
        assert connection.lines() == [
            b"CONNECT proxy.example:8443 HTTP/1.1",
            b"Host: supplied:1",
            b"X-Extra: keep",
        ]

    def test_supplied_headers_are_left_where_the_caller_put_them(self) -> None:
        connection = _FakeConnection("proxy.example", 8443, {"Proxy-Authorization": "Basic x"})
        proxytunnel._tunnel(connection)
        assert connection.lines() == [
            b"CONNECT proxy.example:8443 HTTP/1.1",
            b"Proxy-Authorization: Basic x",
            b"Host: proxy.example:8443",
        ]

    def test_the_callers_header_mapping_is_left_alone(self) -> None:
        # urllib3 shares one proxy-headers mapping across connections, and
        # pre-3.12 set_tunnel stores it without copying: a Host written into it
        # would be replayed, stale, at the next tunnel host.
        headers: dict[str, str] = {}
        connection = _FakeConnection("proxy.example", 8443, headers)
        connection._tunnel_headers = headers
        proxytunnel._tunnel(connection)
        assert headers == {}

    def test_an_ipv6_literal_gets_its_brackets_back(self) -> None:
        # urllib3 reaches set_tunnel with the brackets already stripped.
        connection = _FakeConnection("::1", 8443)
        proxytunnel._tunnel(connection)
        assert connection.lines() == [b"CONNECT [::1]:8443 HTTP/1.1", b"Host: [::1]:8443"]

    def test_a_refused_tunnel_still_raises(self) -> None:
        connection = _FakeConnection("proxy.example", 8443)
        connection.response._read_status = lambda: ("HTTP/1.1", 400, "Bad Request ")  # pyright: ignore[reportAttributeAccessIssue]
        connection.close = lambda: None  # pyright: ignore[reportAttributeAccessIssue]
        with pytest.raises(OSError, match="Tunnel connection failed: 400 Bad Request"):
            proxytunnel._tunnel(connection)
        assert connection.response.closed


def _old_shape_tunnel(self: Any) -> None:
    """A stand-in for the openers 3.10 / 3.11 and urllib3's copy provide."""
    self.send(b"CONNECT %s:%d HTTP/1.0\r\n\r\n" % (self._tunnel_host.encode("ascii"), 8443))


def _new_shape_tunnel(self: Any) -> None:
    """A stand-in for the opener 3.12+ provides."""
    self.send(b"CONNECT x %s\r\n\r\n" % self._http_vsn_str.encode("ascii"))


class TestWhereThePinApplies:
    """The pin installs on the interpreters that need it, and nowhere else."""

    def test_the_detector_follows_the_interpreter(self) -> None:
        # CPython rewrote the tunnel opener in 3.12; the stdlib's own opener is
        # never touched by the pin, so it stays the honest witness for which
        # side of that line this interpreter is on.
        assert proxytunnel._sends_old_connect(http.client.HTTPConnection._tunnel) == (  # pyright: ignore[reportPrivateUsage]
            sys.version_info < (3, 12)
        )

    def test_an_old_shape_opener_is_replaced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", _old_shape_tunnel)
        proxytunnel.pin_connect_request()
        assert HTTPConnection._tunnel is proxytunnel._tunnel

    def test_a_new_shape_opener_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # What a 3.12+ host gets: the pin finds the shape already right and
        # stands down, so nothing of this module reaches the wire there.
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", _new_shape_tunnel)
        proxytunnel.pin_connect_request()
        assert HTTPConnection._tunnel is _new_shape_tunnel

    def test_pinning_twice_changes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", _old_shape_tunnel)
        proxytunnel.pin_connect_request()
        proxytunnel.pin_connect_request()
        assert HTTPConnection._tunnel is proxytunnel._tunnel

    def test_no_opener_to_read_declines_quietly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", None)
        proxytunnel.pin_connect_request()
        assert HTTPConnection._tunnel is None

    def test_a_stdlib_without_the_header_reader_declines_quietly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", _old_shape_tunnel)
        monkeypatch.setattr(proxytunnel, "_read_headers", None)
        proxytunnel.pin_connect_request()
        assert HTTPConnection._tunnel is _old_shape_tunnel


class TestTheClientFactoryInstallsIt:
    def test_building_a_client_pins_the_connect_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The pin is the CLI's, and every client this CLI hands out is built
        # through the one factory, so that is where it goes on.
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", _old_shape_tunnel)
        clientfactory.build_client(_parse(["--region", "us-east-1"]))
        assert HTTPConnection._tunnel is proxytunnel._tunnel
