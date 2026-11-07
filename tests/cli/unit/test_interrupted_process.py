"""The console process on a real Ctrl-C and on a failure, against a local endpoint.

The console path ends with ``os._exit`` (``cli._end_interrupted``), which an
in-process test cannot reach, so these run the CLI in a process of its own.
"""

from __future__ import annotations

import http.server
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from tests.utils.interrupts import skip_without_pthread_kill

_CONSOLE = "import sys; from boto3_s3_cli.cli import main; sys.exit(main())"
_HOLD = 30.0


class _Endpoint:
    """A local endpoint that answers a HEAD and holds every other request unanswered.

    It takes a request's body first, so nothing but the answer is outstanding:
    the client is waiting on the server, as on one that does not answer.
    """

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        endpoint = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _hold(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                endpoint.reached.set()
                endpoint.release.wait(_HOLD)
                self.close_connection = True

            do_GET = do_PUT = do_POST = do_DELETE = _hold  # noqa: N815

            def do_HEAD(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", "1")
                self.send_header("ETag", '"e"')
                self.send_header("Last-Modified", "Wed, 01 Jan 2020 00:00:00 GMT")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def endpoint() -> Generator[_Endpoint, None, None]:
    server = _Endpoint()
    yield server
    server.close()


def _start(
    arguments: list[str], engine: str, endpoint: _Endpoint, tmp_path: Path, **popen: Any
) -> subprocess.Popen[str]:
    config = tmp_path / "config"
    config.write_text(f"[default]\ns3 =\n    preferred_transfer_client = {engine}\n")
    return subprocess.Popen(
        [sys.executable, "-c", _CONSOLE, *arguments, "--endpoint-url", endpoint.url],
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
            # Windows cannot start its sockets without it.
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "HOME": str(tmp_path),
            "AWS_ACCESS_KEY_ID": "x",
            "AWS_SECRET_ACCESS_KEY": "y",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_CONFIG_FILE": str(config),
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
        },
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **popen,
    )


def _interrupt_once_reached(process: subprocess.Popen[str], endpoint: _Endpoint) -> tuple[str, str]:
    """A SIGINT once the command waits on the endpoint, and what it wrote by its end."""
    try:
        assert endpoint.reached.wait(_HOLD), "the command never reached the endpoint"
        process.send_signal(signal.SIGINT)
        return process.communicate(timeout=10)
    finally:
        process.kill()


def _arguments(command: str, tmp_path: Path) -> list[str]:
    source = tmp_path / "f"
    source.write_bytes(b"x")
    return {
        "cp": ["cp", str(source), "s3://b/k"],
        "ls": ["ls", "s3://b/p/", "--recursive"],
        "rm": ["rm", "s3://b/p/", "--recursive"],
    }[command]


@skip_without_pthread_kill
@pytest.mark.parametrize("engine", ["classic", "crt"])
@pytest.mark.parametrize("command", ["cp", "ls", "rm"])
def test_a_ctrl_c_ends_it_at_once_with_the_request_unanswered(
    command: str, engine: str, endpoint: _Endpoint, tmp_path: Path
) -> None:
    pytest.importorskip("awscrt")
    process = _start(_arguments(command, tmp_path), engine, endpoint, tmp_path)
    stdout, stderr = _interrupt_once_reached(process, endpoint)
    assert process.returncode == 130
    assert stdout == ""
    assert stderr == "cancelled: ctrl-c received\n"


@skip_without_pthread_kill
def test_quiet_writes_no_line(endpoint: _Endpoint, tmp_path: Path) -> None:
    arguments = [*_arguments("cp", tmp_path), "--quiet"]
    process = _start(arguments, "classic", endpoint, tmp_path)
    assert _interrupt_once_reached(process, endpoint) == ("", "")
    assert process.returncode == 130


@skip_without_pthread_kill
def test_a_process_without_a_stderr_still_ends(endpoint: _Endpoint, tmp_path: Path) -> None:
    process = _start(
        _arguments("cp", tmp_path), "classic", endpoint, tmp_path, preexec_fn=lambda: os.close(2)
    )
    stdout, _stderr = _interrupt_once_reached(process, endpoint)
    assert process.returncode == 130
    assert stdout == ""


_FULL_STDERR = """
import fcntl, os, signal, sys, threading, time
from boto3_s3_cli import cli

def press_once_writing():
    main = threading.main_thread().ident
    while sys._current_frames()[main].f_code.co_name != "_end_interrupted":
        time.sleep(0.005)
    time.sleep(0.1)
    signal.pthread_kill(main, signal.SIGINT)

flags = fcntl.fcntl(2, fcntl.F_GETFL)
fcntl.fcntl(2, fcntl.F_SETFL, flags | os.O_NONBLOCK)
try:
    while True:
        os.write(2, b"x")
except BlockingIOError:
    fcntl.fcntl(2, fcntl.F_SETFL, flags)
threading.Thread(target=press_once_writing, daemon=True).start()
cli._end_interrupted(quiet=False)
"""


@skip_without_pthread_kill
def test_another_ctrl_c_ends_a_line_a_full_stderr_holds_up(tmp_path: Path) -> None:
    # Unread until the end: the write stays held up, and only that Ctrl-C can end it.
    process = subprocess.Popen(
        [sys.executable, "-c", _FULL_STDERR],
        env={"PYTHONPATH": os.environ.get("PYTHONPATH", ""), "HOME": str(tmp_path)},
        stderr=subprocess.PIPE,
    )
    try:
        assert process.wait(timeout=20) == 130
    finally:
        process.kill()
        assert process.stderr is not None
        process.stderr.close()


@pytest.mark.parametrize("engine", ["classic", "crt"])
def test_a_failure_as_a_transfer_is_submitted_still_ends(
    engine: str, endpoint: _Endpoint, tmp_path: Path
) -> None:
    # The CRT manager settles such an item inside its submit, with a future that
    # never reports done: nothing may wait for that future.
    pytest.importorskip("awscrt")
    (tmp_path / "file").write_bytes(b"")
    arguments = ["cp", "s3://b/k", str(tmp_path / "file" / "sub" / "k")]
    process = _start(arguments, engine, endpoint, tmp_path)
    try:
        stdout, stderr = process.communicate(timeout=20)
    finally:
        process.kill()
    assert process.returncode == 1
    assert stdout == ""
    assert stderr.startswith("download failed:")
