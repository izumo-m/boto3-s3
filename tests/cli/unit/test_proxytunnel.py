"""The ``boto3-s3`` command installs the CONNECT pin on the clients it builds.

The pin itself is the library's (`boto3_s3.pin_proxy_connect`, tests/lib/
test_proxytunnel.py); the command owns its process, so every client it hands
out is built through the one factory that installs it.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from boto3_s3 import proxytunnel
from boto3_s3_cli import clientfactory, globalargs
from tests.utils.strictproxy import TARGET_PORT as _TARGET_PORT
from tests.utils.strictproxy import StrictProxy as _StrictProxy


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    globalargs.add_common_arguments(parser)
    return parser.parse_args(argv)


def _old_shape_tunnel(self: Any) -> None:
    """A stand-in for the openers 3.10 / 3.11 and urllib3's copy provide."""
    self.send(b"CONNECT %s:%d HTTP/1.0\r\n\r\n" % (self._tunnel_host.encode("ascii"), 8443))


class TestTheClientFactoryInstallsIt:
    def test_building_a_client_pins_the_connect_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from botocore.awsrequest import AWSConnection
        from urllib3.connection import HTTPConnection

        monkeypatch.setattr(HTTPConnection, "_tunnel", _old_shape_tunnel)
        held = vars(AWSConnection).get("_tunnel")
        if held is not None:
            monkeypatch.delattr(AWSConnection, "_tunnel")
        clientfactory.build_client(_parse(["--region", "us-east-1"]))
        assert AWSConnection._tunnel is proxytunnel._tunnel  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
        if held is None:
            delattr(AWSConnection, "_tunnel")

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
