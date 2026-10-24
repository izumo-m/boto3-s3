"""A recording HTTPS proxy that refuses a ``Host``-less CONNECT, for the tunnel tests.

The oracle is the pinned aws-cli, measured against such a proxy: aws-cli opens
its tunnel with

    CONNECT 127.0.0.1:PORT HTTP/1.1
    Host: 127.0.0.1:PORT

and, when the proxy URL carries credentials, with ``Proxy-Authorization``
ahead of that generated ``Host``. Interpreters older than 3.12 - and the copy
urllib3 carries for them - send ``CONNECT 127.0.0.1:PORT HTTP/1.0`` with no
``Host``, which this proxy answers 400 (`aws` rc 0 / an unpinned client fails,
``Failed to connect to proxy URL``).
"""

from __future__ import annotations

import socket
import threading

# The tunnel target the CONNECT names. Nothing ever connects to it: the fake
# proxy answers the CONNECT itself and never dials upstream.
TARGET_PORT = 4443


class StrictProxy:
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
