"""Reports quoting the interpreter's own text read as aws's bundled 3.14 words it.

aws prints ``str(exc)`` for a bare ``int()`` failure and for whatever reaches
its general handler, so where that text is the interpreter's the parity target
is the official build's Python (design/cli.md section 2). The older hosts word
two shapes differently: ``int()``'s digit limit before 3.11 and ``json``'s
trailing-comma failures before 3.13. On a newer host these tests pass without
the rewrite; the dev and CI floor (3.10) is where they bite.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from boto3_s3_cli import cli, usage

_DIGIT_LIMIT = (
    "Exceeds the limit (4300 digits) for integer string conversion: value has 5000 "
    "digits; use sys.set_int_max_str_digits() to increase the limit"
)


class TestExceptionText:
    def test_the_digit_limit_gains_its_unit(self) -> None:
        exc = ValueError(
            "Exceeds the limit (4300) for integer string conversion: value has 5000 "
            "digits; use sys.set_int_max_str_digits() to increase the limit"
        )
        assert usage.exception_text(exc) == _DIGIT_LIMIT

    @pytest.mark.parametrize(
        ("doc", "expected"),
        [
            ('{"a": 1,}', "Illegal trailing comma before end of object: line 1 column 8 (char 7)"),
            ("[1, 2,\n]", "Illegal trailing comma before end of array: line 1 column 6 (char 5)"),
            (
                '{"a": 1,',
                "Expecting property name enclosed in double quotes: line 1 column 9 (char 8)",
            ),
        ],
    )
    def test_a_json_decode_failure_reads_as_on_3_13(self, doc: str, expected: str) -> None:
        with pytest.raises(json.JSONDecodeError) as exc_info:
            json.loads(doc)
        assert usage.exception_text(exc_info.value) == expected

    def test_an_error_carrying_its_causes_text_is_rendered_as_the_cause(self) -> None:
        # The library reports a request that raised outside botocore's family
        # as its base error with the original's text and the original on
        # __cause__; the line aws prints is the original's.
        with pytest.raises(json.JSONDecodeError) as exc_info:
            json.loads('{"a": 1,}')
        wrapped = RuntimeError(str(exc_info.value))
        wrapped.__cause__ = exc_info.value
        expected = "Illegal trailing comma before end of object: line 1 column 8 (char 7)"
        assert usage.exception_text(wrapped) == expected
        renamed = RuntimeError("something else")
        renamed.__cause__ = exc_info.value
        assert usage.exception_text(renamed) == "something else"

    def test_a_cause_chain_that_loops_ends_at_the_loop(self) -> None:
        # A plugin's `raise e from e` (or two exceptions naming each other)
        # must not recurse: the text is the one they share.
        looped = ValueError("self-caused")
        looped.__cause__ = looped
        assert usage.exception_text(looped) == "self-caused"
        first, second = RuntimeError("pair"), RuntimeError("pair")
        first.__cause__, second.__cause__ = second, first
        assert usage.exception_text(first) == "pair"

    def test_anything_else_is_its_str(self) -> None:
        assert usage.exception_text(KeyError("Bucket")) == "'Bucket'"
        assert usage.exception_text(ValueError("Invalid endpoint: x")) == "Invalid endpoint: x"


class TestReports:
    @pytest.mark.parametrize("option", ["--cli-read-timeout", "--cli-connect-timeout"])
    def test_a_timeout_past_the_digit_limit(
        self, option: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
        rc = cli.main(["ls", "s3://b", option, "1" * 5000])
        assert rc == 255
        assert capsys.readouterr().err == f"boto3-s3: [ERROR]: {_DIGIT_LIMIT}\n"

    def test_a_config_value_the_sdk_converts_past_the_digit_limit(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # botocore's own int() of max_attempts: a bare ValueError the general
        # report renders.
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1" * 5000)
        rc = cli.main(["ls", "s3://b"])
        assert rc == 255
        assert capsys.readouterr().err == f"boto3-s3: [ERROR]: {_DIGIT_LIMIT}\n"

    @pytest.mark.skipif(sys.platform == "win32", reason="a POSIX credential_process script")
    def test_a_credential_process_answer_with_a_trailing_comma(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        script = tmp_path / "creds.sh"
        script.write_text('#!/bin/sh\nprintf \'%s\' \'{"Version": 1, "AccessKeyId": "a",}\'\n')
        script.chmod(0o755)
        config = tmp_path / "config"
        config.write_text(f"[default]\ncredential_process = {script}\n")
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
        rc = cli.main(["ls", "s3://b"])
        assert rc == 255
        assert capsys.readouterr().err == (
            "boto3-s3: [ERROR]: Illegal trailing comma before end of object: "
            "line 1 column 34 (char 33)\n"
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="a POSIX credential_process script")
    @pytest.mark.parametrize(
        ("argv", "line"),
        [
            (["cp", "f", "s3://b/k"], "upload failed: ./f to s3://b/k "),
            (["rm", "s3://b/k"], "delete failed: s3://b/k "),
            (["cp", "s3://b/", "d", "--recursive"], "fatal error: "),
            (["rm", "s3://b/", "--recursive"], "fatal error: "),
            (["mb", "s3://b"], "make_bucket failed: s3://b "),
            (["rb", "s3://b"], "remove_bucket failed: s3://b "),
            (["ls", "s3://b"], "boto3-s3: [ERROR]: "),
        ],
    )
    def test_a_refresh_that_fails_mid_run(
        self,
        argv: list[str],
        line: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The first answer expires inside botocore's mandatory-refresh window,
        # so the first request's signing calls the process again and gets
        # trailing-comma JSON: the failure arrives inside a request, as the
        # library's base error carrying the JSONDecodeError - on an item's
        # line, a fatal line or the general report alike.
        counter = tmp_path / "count"
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        script = tmp_path / "creds.sh"
        script.write_text(
            "#!/bin/sh\n"
            f"if [ -e {counter} ]; then\n"
            '  printf \'%s\' \'{"Version": 1, "AccessKeyId": "a",}\'\n'
            "else\n"
            f"  touch {counter}\n"
            '  printf \'{"Version": 1, "AccessKeyId": "AKIDEXAMPLE", "SecretAccessKey": "secret",'
            f' "Expiration": "{expiry}"}}\'\n'
            "fi\n"
        )
        script.chmod(0o755)
        config = tmp_path / "config"
        config.write_text(f"[default]\ncredential_process = {script}\n")
        (tmp_path / "f").write_bytes(b"x")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
        monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
        cli.main(argv)
        err = capsys.readouterr().err
        assert (
            f"{line}Illegal trailing comma before end of object: line 1 column 34 (char 33)" in err
        )


class TestEndpointScheme:
    """An endpoint the environment supplies is judged by aws's Python's rule.

    Python 3.10's ``urlsplit`` takes ``1http`` for a scheme, so botocore's
    client build lets ``1http://...`` through and every request then fails on
    the endpoint rules - an item's failure at rc 1, a ``--dryrun`` at rc 0 -
    where aws's 3.14 build refuses it as ``Invalid endpoint`` (rc 255).
    """

    @pytest.mark.parametrize(
        "argv",
        [["cp", "f", "s3://b/k"], ["cp", "f", "s3://b/k", "--dryrun"], ["rm", "s3://b/k"], ["ls"]],
    )
    @pytest.mark.parametrize("variable", ["AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3"])
    def test_a_digit_led_scheme_is_an_invalid_endpoint(
        self,
        argv: list[str],
        variable: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (tmp_path / "f").write_bytes(b"x")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AWS_ENDPOINT_URL", "1http://127.0.0.1:9")
        monkeypatch.setenv(variable, "1http://127.0.0.1:9")
        rc = cli.main(argv)
        assert rc == 255
        assert (
            capsys.readouterr().err == "boto3-s3: [ERROR]: Invalid endpoint: 1http://127.0.0.1:9\n"
        )

    def test_an_endpoint_with_no_scheme_is_left_to_the_requests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `//host:port` parses with a host on every Python and passes aws's
        # client build; aws fails it on the requests, so a --dryrun succeeds.
        (tmp_path / "f").write_bytes(b"x")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AWS_ENDPOINT_URL", "//127.0.0.1:9")
        rc = cli.main(["cp", "f", "s3://b/k", "--dryrun"])
        assert rc == 0
        assert capsys.readouterr().out == "(dryrun) upload: ./f to s3://b/k\n"
