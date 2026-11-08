"""Unit tests for boto3_s3.proxytunnel (`pin_proxy_connect`, the CONNECT request's shape).

The oracle is the pinned aws-cli, measured against the recording proxy in
`tests.utils.strictproxy`, which refuses a ``Host``-less CONNECT.
`TestTheProxyIsStrict` shows its teeth before the rest of the file leans on
them.
"""

from __future__ import annotations

import http.client
import socket
import sys
from collections.abc import Iterator
from io import BytesIO
from typing import Any

import pytest

from boto3_s3 import proxytunnel
from tests.utils.strictproxy import TARGET_PORT as _TARGET_PORT
from tests.utils.strictproxy import StrictProxy as _StrictProxy

# 3.12.0 to 3.12.2 write the new request line but bracket an IPv6 literal in
# the Host header they generate, so their opener is no oracle for that case.
_EARLY_312_IPV6 = pytest.mark.skipif(
    (3, 12) <= sys.version_info[:3] < (3, 12, 3),
    reason="3.12.0 to 3.12.2 bracket an IPv6 Host themselves (left as it is)",
)


@pytest.fixture
def unpinned() -> Iterator[Any]:
    """botocore's ``AWSConnection`` with no opener of its own, restored afterwards.

    Other tests (the CLI's client builds) pin it for the whole session; these
    need to see the pin go on.
    """
    from botocore.awsrequest import AWSConnection

    held = vars(AWSConnection).get("_tunnel")
    if held is not None:
        delattr(AWSConnection, "_tunnel")
    try:
        yield AWSConnection
    finally:
        if "_tunnel" in vars(AWSConnection):
            delattr(AWSConnection, "_tunnel")
        if held is not None:
            AWSConnection._tunnel = held  # pyright: ignore[reportAttributeAccessIssue]


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


class TestBotocoreClients:
    """What goes on the wire when a botocore client meets an HTTPS proxy."""

    @staticmethod
    def _client(endpoint: str) -> Any:
        import boto3

        session = boto3.session.Session(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            region_name="us-east-1",
        )
        return session.client("s3", endpoint_url=endpoint)

    def test_the_tunnel_request_is_the_one_aws_sends(
        self, unpinned: Any, proxy_environment: _StrictProxy
    ) -> None:
        proxytunnel.pin_proxy_connect()
        client = self._client(f"https://127.0.0.1:{_TARGET_PORT}")
        with pytest.raises(Exception):  # noqa: B017 - the tunnel is cut after the 200
            client.list_buckets()
        assert proxy_environment.blocks[0] == [
            b"CONNECT 127.0.0.1:%d HTTP/1.1" % _TARGET_PORT,
            b"Host: 127.0.0.1:%d" % _TARGET_PORT,
        ]

    @_EARLY_312_IPV6
    def test_an_ipv6_endpoint_is_named_bare_in_the_host_header(
        self, unpinned: Any, proxy_environment: _StrictProxy
    ) -> None:
        # Through the real client stack, so the host form set_tunnel receives
        # is urllib3's own on whichever interpreter runs this - the one thing
        # a hand-built connection cannot show. aws: brackets in the request
        # line, none in the Host header (measured).
        proxytunnel.pin_proxy_connect()
        client = self._client(f"https://[::1]:{_TARGET_PORT}")
        with pytest.raises(Exception):  # noqa: B017 - the tunnel is cut after the 200
            client.list_buckets()
        assert proxy_environment.blocks[0] == [
            b"CONNECT [::1]:%d HTTP/1.1" % _TARGET_PORT,
            b"Host: ::1:%d" % _TARGET_PORT,
        ]

    def test_proxy_credentials_precede_the_generated_host(
        self, unpinned: Any, proxy_environment: _StrictProxy, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Measured on aws: Proxy-Authorization first, the generated Host last -
        # the order CPython 3.12+ produces by appending its Host to the headers
        # urllib3 supplied.
        monkeypatch.setenv("HTTPS_PROXY", f"http://bob:sec@127.0.0.1:{proxy_environment.port}")
        proxytunnel.pin_proxy_connect()
        client = self._client(f"https://127.0.0.1:{_TARGET_PORT}")
        with pytest.raises(Exception):  # noqa: B017 - the tunnel is cut after the 200
            client.list_buckets()
        assert proxy_environment.blocks[0] == [
            b"CONNECT 127.0.0.1:%d HTTP/1.1" % _TARGET_PORT,
            b"Proxy-Authorization: Basic Ym9iOnNlYw==",
            b"Host: 127.0.0.1:%d" % _TARGET_PORT,
        ]

    def test_urllib3_used_directly_keeps_its_interpreters_shape(
        self, unpinned: Any, proxy: _StrictProxy
    ) -> None:
        # The pin is botocore's alone: code that is not botocore keeps what its
        # interpreter writes.
        import urllib3

        proxytunnel.pin_proxy_connect()
        manager = urllib3.ProxyManager(proxy.url, cert_reqs="CERT_NONE", retries=False)
        with pytest.raises(Exception):  # noqa: B017 - refused (old shape) or cut after the 200
            manager.request("GET", f"https://127.0.0.1:{_TARGET_PORT}/")
        version = b"HTTP/1.1" if sys.version_info >= (3, 12) else b"HTTP/1.0"
        assert proxy.blocks[0][0] == b"CONNECT 127.0.0.1:%d %s" % (_TARGET_PORT, version)


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

    @pytest.mark.parametrize("held", ["::1", "[::1]"])
    def test_an_ipv6_literal_is_bracketed_in_the_request_line_only(self, held: str) -> None:
        # What aws-cli sends (measured through a recording proxy:
        # `CONNECT [::1]:8443 HTTP/1.1` / `Host: ::1:8443`): brackets on the
        # request target, none in the generated Host. urllib3 hands set_tunnel
        # the literal bracketed and the port separately; set_tunnel strips the
        # brackets from 3.11.4 on and keeps them before, so the replacement
        # has to give the same answer from either form.
        connection = _FakeConnection(held, 8443)
        proxytunnel._tunnel(connection)
        assert connection.lines() == [b"CONNECT [::1]:8443 HTTP/1.1", b"Host: ::1:8443"]

    @pytest.mark.skipif(
        sys.version_info < (3, 12), reason="older interpreters write the shape being replaced"
    )
    @pytest.mark.parametrize(
        ("host", "headers"),
        [
            ("proxy.example", {}),
            pytest.param("::1", {}, marks=_EARLY_312_IPV6),
            pytest.param(
                "fd00:ec2::254", {"Proxy-Authorization": "Basic x"}, marks=_EARLY_312_IPV6
            ),
            ("proxy.example", {"Host": "supplied:1", "X-Extra": "keep"}),
        ],
    )
    def test_it_writes_what_the_interpreter_aws_runs_on_writes(
        self, host: str, headers: dict[str, str]
    ) -> None:
        """The replacement against the opener it stands in for.

        From 3.12 on the interpreter's own ``set_tunnel`` + ``_tunnel`` write
        aws-cli's request, so on those hosts they are the oracle for the bytes
        the replacement puts on the wire below 3.12.
        """
        real = http.client.HTTPConnection("proxy.invalid", 8080)
        real.set_tunnel(host, 8443, headers=dict(headers))
        written: list[bytes] = []
        real.send = written.append  # pyright: ignore[reportAttributeAccessIssue]
        real.response_class = lambda *args, **kwargs: _FakeResponse()  # pyright: ignore[reportAttributeAccessIssue]
        real._tunnel()  # pyright: ignore[reportAttributeAccessIssue]

        connection = _FakeConnection(host, 8443, headers)
        proxytunnel._tunnel(connection)
        assert connection.sent == b"".join(written)

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

    def test_an_old_shape_opener_is_replaced_on_botocore_alone(
        self, unpinned: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", _old_shape_tunnel)
        proxytunnel.pin_proxy_connect()
        assert unpinned._tunnel is proxytunnel._tunnel  # pyright: ignore[reportPrivateUsage]
        assert HTTPConnection._tunnel is _old_shape_tunnel

    def test_a_new_shape_opener_is_left_alone(
        self, unpinned: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # What a 3.12+ host gets: the pin finds the shape already right and
        # stands down, so nothing of this module reaches the wire there.
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", _new_shape_tunnel)
        proxytunnel.pin_proxy_connect()
        assert "_tunnel" not in vars(unpinned)

    def test_pinning_twice_changes_nothing(
        self, unpinned: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", _old_shape_tunnel)
        proxytunnel.pin_proxy_connect()
        proxytunnel.pin_proxy_connect()
        assert unpinned._tunnel is proxytunnel._tunnel  # pyright: ignore[reportPrivateUsage]

    def test_a_botocore_opener_of_its_own_is_left_alone(
        self, unpinned: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", _old_shape_tunnel)
        unpinned._tunnel = _new_shape_tunnel
        proxytunnel.pin_proxy_connect()
        assert unpinned._tunnel is _new_shape_tunnel

    def test_no_opener_to_read_declines_quietly(
        self, unpinned: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", None)
        proxytunnel.pin_proxy_connect()
        assert "_tunnel" not in vars(unpinned)

    def test_a_stdlib_without_the_header_reader_declines_quietly(
        self, unpinned: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", _old_shape_tunnel)
        monkeypatch.setattr(proxytunnel, "_read_headers", None)
        proxytunnel.pin_proxy_connect()
        assert "_tunnel" not in vars(unpinned)

    def test_importing_the_package_pins_nothing(self, unpinned: Any) -> None:
        # The pin is the application's to ask for (design/overview.md
        # section 3): importing boto3_s3, or this module, installs nothing.
        import importlib

        importlib.reload(proxytunnel)
        assert "_tunnel" not in vars(unpinned)
